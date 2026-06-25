"""Provider-canary dynamic repair guards for bounded NeoEngine runtime lanes.

The runtime supervisor should repair narrow, known-local dependency drift (for
example a Homebrew dylib link disappearing under a CLI binary) without weakening
provider auth, governance, or queue semantics.  Everything here is deliberately
small and auditable: classify an error, run one bounded local repair, then retry
exactly once and record receipt fields on the returned canary result.
"""
from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable
from typing import Any

CommandRunner = Callable[..., Any]
CanaryRetry = Callable[[], dict[str, Any]]

_HOMEBREW_DYLIB_RE = re.compile(
    r"/opt/homebrew/opt/(?P<dependency>[A-Za-z0-9_.+-]+)/lib/[^\s:'\"]+\.dylib"
)


def _copy_result(result: dict[str, Any]) -> dict[str, Any]:
    copied = dict(result)
    copied["blockers"] = list(result.get("blockers") or [])
    return copied


def repair_enabled_from_env() -> bool:
    """Return whether bounded local provider repair is enabled.

    Operators can revert to observe-only/no-mutation behavior immediately with:
    ``HERMES_PROVIDER_CANARY_REPAIR=0``.
    """

    return os.environ.get("HERMES_PROVIDER_CANARY_REPAIR", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
        "disabled",
    }


def classify_provider_canary_failure(provider: str, blockers: list[str] | tuple[str, ...]) -> dict[str, Any]:
    """Classify a provider canary failure for safe local auto-repair.

    Currently only Codex/Homebrew dylib-link drift is repairable.  Auth errors,
    command-not-found, model/policy failures, and non-Codex providers remain
    non-repairable so they surface as real blockers.
    """

    provider = (provider or "").lower()
    text = "\n".join(str(blocker) for blocker in blockers or [])
    if provider != "codex":
        return {"repairable": False, "blocker_type": "unsupported_provider"}
    if "library not loaded" not in text.lower() and "dyld" not in text.lower():
        return {"repairable": False, "blocker_type": "unclassified"}
    match = _HOMEBREW_DYLIB_RE.search(text)
    if not match:
        return {"repairable": False, "blocker_type": "dyld_non_homebrew"}
    dependency = match.group("dependency")
    return {
        "repairable": True,
        "blocker_type": "homebrew_dylib_missing",
        "dependency": dependency,
        "repair_plan": [["brew", "link", "--overwrite", dependency]],
    }


def maybe_repair_provider_canary_failure(
    provider: str,
    failed_result: dict[str, Any],
    retry_canary: CanaryRetry,
    *,
    command_runner: CommandRunner = subprocess.run,
    enabled: bool | None = None,
) -> dict[str, Any]:
    """Repair a narrow provider-canary blocker and retry once.

    The original canary result is returned with explicit repair receipt fields
    unless a repair succeeds and the retry passes, in which case the passing
    retry result is returned with the same receipt fields attached.
    """

    if failed_result.get("status") == "PASS":
        result = _copy_result(failed_result)
        result.update({"repair_attempted": False, "repair_status": "NOT_NEEDED"})
        return result

    if enabled is None:
        enabled = repair_enabled_from_env()
    if not enabled:
        result = _copy_result(failed_result)
        result.update({"repair_attempted": False, "repair_status": "DISABLED"})
        return result

    classification = classify_provider_canary_failure(provider, failed_result.get("blockers") or [])
    if not classification.get("repairable"):
        result = _copy_result(failed_result)
        result.update({"repair_attempted": False, "repair_status": "NOT_REPAIRABLE", "repair_classification": classification})
        return result

    repair_actions: list[dict[str, Any]] = []
    for cmd in classification.get("repair_plan") or []:
        proc = command_runner(cmd, text=True, capture_output=True, timeout=45)
        repair_actions.append(
            {
                "cmd": cmd,
                "returncode": getattr(proc, "returncode", None),
                "stdout_tail": str(getattr(proc, "stdout", "") or "")[-500:],
                "stderr_tail": str(getattr(proc, "stderr", "") or "")[-500:],
            }
        )
        if getattr(proc, "returncode", 1) != 0:
            result = _copy_result(failed_result)
            result.update(
                {
                    "repair_attempted": True,
                    "repair_status": "REPAIR_COMMAND_FAILED",
                    "repair_classification": classification,
                    "repair_actions": repair_actions,
                }
            )
            return result

    retry_result = _copy_result(retry_canary())
    retry_result.update(
        {
            "repair_attempted": True,
            "repair_status": "REPAIRED_PASS" if retry_result.get("status") == "PASS" else "REPAIR_ATTEMPTED_FAILED",
            "repair_classification": classification,
            "repair_actions": repair_actions,
        }
    )
    return retry_result
