"""Bounded runtime-repair helpers for founder/blocker watchdogs.

Founder-facing watchdogs should not stop at reporting a recoverable runtime
failure.  They should run the same bounded repair ladder used by runtime health
controllers, prove the post-repair state, and only then emit a blocker.  This
module keeps that behavior small, injectable, and testable so local cron scripts
can share the repair contract without embedding ad-hoc subprocess ladders.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CommandRunner = Callable[..., Any]
ProofLoader = Callable[[], dict[str, Any]]

PASS_STATUSES = {"PASS", "REPAIRED_PASS"}
DISABLE_ENV = "HERMES_WATCHDOG_RUNTIME_REPAIR"
RESTART_GUARD_RECOVERY_REASON = "recoverable_restart_guard_with_clean_kanban"
LIVE_LANE_BLOCKER_RE = re.compile(r"^live_[a-z0-9_-]+_build_lanes \d+ < required \d+$")
SUBSTRATE_FAILURE_MARKERS = (
    # This is a compatibility denylist, not the primary safety contract. The
    # nonzero-continue gate also requires explicit clean substrate attestations.
    "KANBAN_DB_MALFORMED",
    "LANE_CONTROL_HEARTBEAT_FAILED",
    "corrupt",
    "checksum mismatch",
    "checksum_mismatch",
    "database disk image is malformed",
    "file is not a database",
    "integrity check failed",
    "sqlite refused",
)
CLEAN_SUBSTRATE_STATUSES = {"OK", "PASS", "CLEAN", "HEALTHY"}
SUBSTRATE_DIAGNOSTIC_CONTEXT_KEYS = (
    "kanban_db_quick_check",
    "lane_health",
    "substrate",
    "diagnostic",
    "diagnostics",
    "integrity",
    "checksum",
)
SUBSTRATE_DIAGNOSTIC_STATUS_KEYS = {"status", "state", "result", "health"}
SUBSTRATE_DIAGNOSTIC_FAILURE_KEYS = (
    "blocker",
    "blockers",
    "error",
    "errors",
    "failure",
    "failures",
)
UNCLEAN_PROOF_SIGNAL_KEY_PARTS = (
    "checksum",
    "corrupt",
    "error",
    "failure",
    "integrity",
    "malformed",
)

DEFAULT_HERMES_HOME = Path.home() / ".hermes"
DEFAULT_AGENT_RUNTIME_DIR = DEFAULT_HERMES_HOME / "state" / "agent-runtime"
DEFAULT_SCRIPT_DIR = DEFAULT_HERMES_HOME / "scripts"

CANONICAL_NEOWEALTH_RETAINED_LANES = {
    "codex",
    "nw-codex-01-fin-mvp-runtime-recovery",
    "nw-sonnet-01-fin-mvp-integration-recovery",
}


def repair_enabled_from_env(env: dict[str, str] | None = None) -> bool:
    """Return whether watchdog runtime repair may mutate local runtime state."""

    env = env or os.environ
    return env.get(DISABLE_ENV, "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
        "disabled",
    }


def proof_status(proof: dict[str, Any] | None) -> str:
    """Normalize known runtime proof status fields."""

    proof = proof or {}
    return str(proof.get("proof_status") or proof.get("status") or "UNKNOWN")


def proof_is_passing(proof: dict[str, Any] | None) -> bool:
    """Return true only for PASS-like proofs with no reported blockers/canary failures."""

    return proof_status(proof) in PASS_STATUSES and not _proof_blockers(proof)


def _proof_blockers(proof: dict[str, Any] | None) -> list[str]:
    proof = proof or {}
    blockers: list[str] = []
    for key in ("remaining_blockers", "blockers", "lane_control_substrate_blockers"):
        raw = proof.get(key) or []
        if not isinstance(raw, (list, tuple, set)):
            raw = [raw]
        for blocker in raw:
            if isinstance(blocker, dict):
                blockers.append(str(blocker.get("blocker") or blocker.get("status") or blocker))
            else:
                blockers.append(str(blocker))
    failures = proof.get("provider_canary_failures") or []
    if not isinstance(failures, (list, tuple, set)):
        failures = [failures]
    for failure in failures:
        if isinstance(failure, dict):
            blockers.append(str(failure.get("blocker") or failure.get("status") or failure))
        else:
            blockers.append(str(failure))
    return blockers


def _proof_timestamp_epoch(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        epoch = float(value)
        if epoch > 10_000_000_000:
            epoch = epoch / 1000
        return epoch
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _proof_evidence_epoch(proof: dict[str, Any]) -> float | None:
    def fresh_from_keys(keys: tuple[str, ...]) -> float | None:
        for key in keys:
            epoch = _proof_timestamp_epoch(proof.get(key))
            if epoch is not None:
                return epoch
        return None

    checked = fresh_from_keys(("checked_at_epoch", "checked_at"))
    if checked is not None:
        return checked
    generated = fresh_from_keys(("updated_at_epoch", "updated_at", "generated_at_epoch", "generated_at"))
    if generated is not None:
        return generated
    return None


def _proof_fresh_enough(
    proof: dict[str, Any],
    min_checked_epoch: float,
    *,
    previous_checked_epoch: float | None = None,
    max_future_skew_seconds: float = 60.0,
) -> bool:
    epoch = _proof_evidence_epoch(proof)
    if epoch is None:
        return False
    if previous_checked_epoch is not None and epoch <= previous_checked_epoch:
        return False
    if epoch < int(min_checked_epoch):
        return False
    if epoch > time.time() + max_future_skew_seconds:
        return False
    return True


def _is_recoverable_restart_guard_blocker(blocker: str) -> bool:
    return (
        blocker == "CRASH_LOOP_DETECTED"
        or bool(LIVE_LANE_BLOCKER_RE.fullmatch(blocker))
    )


def _proof_contains_substrate_failure(value: Any) -> bool:
    if isinstance(value, dict):
        return any(_proof_contains_substrate_failure(k) or _proof_contains_substrate_failure(v) for k, v in value.items())
    if isinstance(value, (list, tuple, set)):
        return any(_proof_contains_substrate_failure(v) for v in value)
    if isinstance(value, str):
        lowered = value.lower()
        return any(marker.lower() in lowered for marker in SUBSTRATE_FAILURE_MARKERS)
    return False


def _clean_kanban_probe(proof: dict[str, Any]) -> bool:
    probe = proof.get("kanban_db_quick_check")
    if not isinstance(probe, dict) or not probe:
        return False
    if probe.get("blocking") or str(probe.get("status") or "").upper() not in CLEAN_SUBSTRATE_STATUSES:
        return False
    return bool(probe.get("db_path"))


def _explicit_substrate_blocker_attestation_is_clean(proof: dict[str, Any]) -> bool:
    if "lane_control_substrate_blockers" not in proof:
        return False
    blockers = proof.get("lane_control_substrate_blockers")
    return isinstance(blockers, (list, tuple, set)) and len(blockers) == 0


def _lane_control_failures_are_clean(proof: dict[str, Any]) -> bool:
    lanes = proof.get("lanes")
    if not isinstance(lanes, list):
        return False
    for lane in lanes:
        if not isinstance(lane, dict):
            return False
        failures = lane.get("lane_control_consecutive_failures")
        heartbeat = lane.get("lane_control_heartbeat")
        lane_has_control_attestation = heartbeat is not None or lane.get("expected_persistent") is True
        if lane_has_control_attestation and "lane_control_consecutive_failures" not in lane:
            return False
        if failures not in (None, 0, "0"):
            return False
        if heartbeat is None:
            continue
        if not isinstance(heartbeat, dict):
            return False
        if _dict_has_unclean_signal(heartbeat):
            return False
        nested_heartbeat = heartbeat.get("heartbeat")
        if isinstance(nested_heartbeat, dict) and _dict_has_unclean_signal(nested_heartbeat):
            return False
    return True


def _has_meaningful_diagnostic_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return value not in (None, "", [], {}, ())


def _dict_has_unclean_signal(value: dict[Any, Any]) -> bool:
    for key, item in value.items():
        lowered_key = str(key).lower()
        if any(marker in lowered_key for marker in UNCLEAN_PROOF_SIGNAL_KEY_PARTS):
            if _has_meaningful_diagnostic_value(item):
                return True
    return False


def _proof_contains_unclean_signal(value: Any) -> bool:
    if isinstance(value, dict):
        if _dict_has_unclean_signal(value):
            return True
        return any(_proof_contains_unclean_signal(item) for item in value.values())
    if isinstance(value, (list, tuple, set)):
        return any(_proof_contains_unclean_signal(item) for item in value)
    return False


def _is_substrate_diagnostic_context(key: str) -> bool:
    lowered = key.lower()
    return any(marker in lowered for marker in SUBSTRATE_DIAGNOSTIC_CONTEXT_KEYS)


def _substrate_diagnostics_are_clean(value: Any, *, in_context: bool = False) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            lowered_key = str(key).lower()
            key_context = in_context or _is_substrate_diagnostic_context(str(key))
            if key_context and lowered_key in SUBSTRATE_DIAGNOSTIC_STATUS_KEYS:
                if str(item or "").upper() not in CLEAN_SUBSTRATE_STATUSES:
                    return False
            if key_context and any(marker in lowered_key for marker in SUBSTRATE_DIAGNOSTIC_FAILURE_KEYS):
                if _has_meaningful_diagnostic_value(item):
                    return False
            if not _substrate_diagnostics_are_clean(item, in_context=key_context):
                return False
        return True
    if isinstance(value, (list, tuple, set)):
        return all(_substrate_diagnostics_are_clean(item, in_context=in_context) for item in value)
    return True


def _proof_has_clean_substrate_attestation(proof: dict[str, Any]) -> bool:
    return (
        _clean_kanban_probe(proof)
        and _explicit_substrate_blocker_attestation_is_clean(proof)
        and _lane_control_failures_are_clean(proof)
        and not _proof_contains_unclean_signal(proof)
        and _substrate_diagnostics_are_clean(proof)
    )


def proof_is_recoverable_restart_guard(
    proof: dict[str, Any] | None,
    *,
    min_checked_epoch: float,
    previous_checked_epoch: float | None = None,
) -> bool:
    """Return true for the stale restart-guard shape that needs another repair step.

    A proof-gate repair command can exit nonzero after it writes a fresh proof
    because the supervisor restart limiter is still latched.  If that fresh
    proof reports an explicit clean substrate attestation, continuing to the
    runtime health loop is safe and bounded. Lane-level kanban
    malformed/heartbeat blockers stay hard blockers because a single global
    quick_check cannot prove that every lane-specific corruption signal is
    stale. Final readiness still requires a later PASS proof.
    """

    proof = proof or {}
    blockers = _proof_blockers(proof)
    if not blockers:
        return False
    if _proof_contains_substrate_failure(proof):
        return False

    if not _proof_fresh_enough(proof, min_checked_epoch, previous_checked_epoch=previous_checked_epoch):
        return False

    if not _proof_has_clean_substrate_attestation(proof):
        return False

    return all(_is_recoverable_restart_guard_blocker(blocker) for blocker in blockers)


def _command_result(proc: Any, cmd: list[str]) -> dict[str, Any]:
    return {
        "cmd": cmd,
        "returncode": getattr(proc, "returncode", None),
        "stdout_tail": str(getattr(proc, "stdout", "") or "")[-1000:],
        "stderr_tail": str(getattr(proc, "stderr", "") or "")[-1000:],
    }


def _command_exception_result(exc: Exception, cmd: list[str]) -> dict[str, Any]:
    """Return redacted command evidence for runner exceptions/timeouts."""

    return {
        "cmd": cmd,
        "returncode": None,
        "exception_type": type(exc).__name__,
        "stdout_tail": str(getattr(exc, "stdout", "") or "")[-1000:],
        "stderr_tail": str(getattr(exc, "stderr", "") or str(exc) or "")[-1000:],
    }


def runtime_repair_commands(
    org: str,
    *,
    hermes_home: Path | str = DEFAULT_HERMES_HOME,
    python_executable: str = sys.executable,
) -> list[list[str]]:
    """Return the bounded repair ladder for a product-org runtime proof.

    Order matters: first ask the proof gate to repair, then run the org runtime
    health loop for supervisor/lane recovery, then retry the proof gate once.
    """

    home = Path(hermes_home)
    return [
        [python_executable, str(home / "state" / "agent-runtime" / "agent_runtime_proof_gate.py"), org, "--repair"],
        [python_executable, str(home / "scripts" / f"{org}-runtime-health-loop.py")],
        [python_executable, str(home / "state" / "agent-runtime" / "agent_runtime_proof_gate.py"), org, "--repair"],
    ]


def run_dynamic_runtime_repair(
    initial_proof: dict[str, Any],
    org: str,
    load_latest_proof: ProofLoader,
    *,
    command_runner: CommandRunner = subprocess.run,
    enabled: bool | None = None,
    hermes_home: Path | str = DEFAULT_HERMES_HOME,
    timeout_seconds: int = 90,
) -> dict[str, Any]:
    """Run a bounded dynamic repair workflow and return a receipt.

    The workflow is intentionally capped at three commands and re-reads proof
    after every successful command.  It never converts product/merge blockers
    into readiness evidence; it only proves whether runtime capacity recovered.
    """

    if proof_is_passing(initial_proof):
        return {
            "runtime_repair_attempted": False,
            "runtime_repair_status": "NOT_NEEDED",
            "initial_proof_status": proof_status(initial_proof),
            "final_proof": dict(initial_proof),
            "actions": [],
        }

    if enabled is None:
        enabled = repair_enabled_from_env()
    if not enabled:
        return {
            "runtime_repair_attempted": False,
            "runtime_repair_status": "DISABLED",
            "initial_proof_status": proof_status(initial_proof),
            "final_proof": dict(initial_proof),
            "actions": [],
        }

    actions: list[dict[str, Any]] = []
    latest_proof = dict(initial_proof)
    previous_loaded_proof_epoch = _proof_evidence_epoch(latest_proof)
    for cmd in runtime_repair_commands(org, hermes_home=hermes_home):
        command_started_at = time.time()
        try:
            proc = command_runner(cmd, text=True, capture_output=True, timeout=timeout_seconds)
        except Exception as exc:
            actions.append(_command_exception_result(exc, cmd))
            return {
                "runtime_repair_attempted": True,
                "runtime_repair_status": "REPAIR_COMMAND_FAILED",
                "initial_proof_status": proof_status(initial_proof),
                "final_proof_status": proof_status(latest_proof),
                "final_proof": latest_proof,
                "actions": actions,
            }
        action = _command_result(proc, cmd)
        actions.append(action)
        try:
            latest_proof = dict(load_latest_proof())
        except Exception as exc:
            action["proof_reload_error"] = {
                "exception_type": type(exc).__name__,
                "error": str(exc)[-1000:],
            }
            return {
                "runtime_repair_attempted": True,
                "runtime_repair_status": "REPAIR_COMMAND_FAILED",
                "initial_proof_status": proof_status(initial_proof),
                "final_proof_status": proof_status(latest_proof),
                "final_proof": latest_proof,
                "actions": actions,
            }
        if getattr(proc, "returncode", 1) != 0:
            if proof_is_recoverable_restart_guard(
                latest_proof,
                min_checked_epoch=command_started_at,
                previous_checked_epoch=previous_loaded_proof_epoch,
            ):
                action["continued_after_nonzero"] = True
                action["continue_reason"] = RESTART_GUARD_RECOVERY_REASON
                previous_loaded_proof_epoch = _proof_evidence_epoch(latest_proof)
                continue
            return {
                "runtime_repair_attempted": True,
                "runtime_repair_status": "REPAIR_COMMAND_FAILED",
                "initial_proof_status": proof_status(initial_proof),
                "final_proof_status": proof_status(latest_proof),
                "final_proof": latest_proof,
                "actions": actions,
            }
        if proof_is_passing(latest_proof):
            return {
                "runtime_repair_attempted": True,
                "runtime_repair_status": "REPAIRED_PASS",
                "initial_proof_status": proof_status(initial_proof),
                "final_proof_status": proof_status(latest_proof),
                "final_proof": latest_proof,
                "actions": actions,
            }
        previous_loaded_proof_epoch = _proof_evidence_epoch(latest_proof)

    return {
        "runtime_repair_attempted": True,
        "runtime_repair_status": "REPAIR_ATTEMPTED_FAILED",
        "initial_proof_status": proof_status(initial_proof),
        "final_proof_status": proof_status(latest_proof),
        "final_proof": latest_proof,
        "actions": actions,
    }


def missing_required_lane_registrations(
    registered_lane_ids: Iterable[str],
    required_lane_ids: Iterable[str] = CANONICAL_NEOWEALTH_RETAINED_LANES,
) -> list[str]:
    """Return canonical lane IDs missing from the shared heartbeat store."""

    registered = {str(lane_id) for lane_id in registered_lane_ids}
    return sorted({str(lane_id) for lane_id in required_lane_ids} - registered)


def lane_registration_preflight_receipt(
    registered_lane_ids: Iterable[str],
    required_lane_ids: Iterable[str] = CANONICAL_NEOWEALTH_RETAINED_LANES,
) -> dict[str, Any]:
    """Build a fail-closed receipt for dispatcher lane registration preflight."""

    missing = missing_required_lane_registrations(registered_lane_ids, required_lane_ids)
    return {
        "status": "PASS" if not missing else "UNKNOWN_DEV_LANE_REGISTRATION_MISSING",
        "missing_lane_ids": missing,
        "required_lane_ids": sorted({str(lane_id) for lane_id in required_lane_ids}),
        "registered_lane_ids": sorted({str(lane_id) for lane_id in registered_lane_ids}),
        "spawn_allowed": not missing,
    }


def watchdog_runtime_status_after_repair(receipt: dict[str, Any]) -> str:
    """Map a repair receipt to the founder-watchdog runtime status line."""

    status = str(receipt.get("runtime_repair_status") or "UNKNOWN")
    if status in {"NOT_NEEDED", "REPAIRED_PASS"}:
        return "RUNTIME_PASS"
    if status == "DISABLED":
        return "RUNTIME_REPAIR_DISABLED"
    if status == "REPAIR_COMMAND_FAILED":
        return "RUNTIME_REPAIR_COMMAND_FAILED"
    return "RUNTIME_REPAIR_ATTEMPTED_FAILED"
