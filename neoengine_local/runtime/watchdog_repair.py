"""Bounded runtime-repair helpers for founder/blocker watchdogs.

Founder-facing watchdogs should not stop at reporting a recoverable runtime
failure.  They should run the same bounded repair ladder used by runtime health
controllers, prove the post-repair state, and only then emit a blocker.  This
module keeps that behavior small, injectable, and testable so local cron scripts
can share the repair contract without embedding ad-hoc subprocess ladders.
"""
from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

CommandRunner = Callable[..., Any]
ProofLoader = Callable[[], dict[str, Any]]

PASS_STATUSES = {"PASS", "REPAIRED_PASS"}
DISABLE_ENV = "HERMES_WATCHDOG_RUNTIME_REPAIR"

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
    """Return true only for runtime proof states that satisfy capacity gates."""

    return proof_status(proof) in PASS_STATUSES and not (proof or {}).get("remaining_blockers")


def _command_result(proc: Any, cmd: list[str]) -> dict[str, Any]:
    return {
        "cmd": cmd,
        "returncode": getattr(proc, "returncode", None),
        "stdout_tail": str(getattr(proc, "stdout", "") or "")[-1000:],
        "stderr_tail": str(getattr(proc, "stderr", "") or "")[-1000:],
    }


def _command_exception_result(exc: BaseException, cmd: list[str]) -> dict[str, Any]:
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
    for cmd in runtime_repair_commands(org, hermes_home=hermes_home):
        try:
            proc = command_runner(cmd, text=True, capture_output=True, timeout=timeout_seconds)
        except BaseException as exc:
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
        if getattr(proc, "returncode", 1) != 0:
            return {
                "runtime_repair_attempted": True,
                "runtime_repair_status": "REPAIR_COMMAND_FAILED",
                "initial_proof_status": proof_status(initial_proof),
                "final_proof_status": proof_status(latest_proof),
                "final_proof": latest_proof,
                "actions": actions,
            }
        latest_proof = dict(load_latest_proof())
        if proof_is_passing(latest_proof):
            return {
                "runtime_repair_attempted": True,
                "runtime_repair_status": "REPAIRED_PASS",
                "initial_proof_status": proof_status(initial_proof),
                "final_proof_status": proof_status(latest_proof),
                "final_proof": latest_proof,
                "actions": actions,
            }

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
