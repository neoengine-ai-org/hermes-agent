"""Profile-scoped gateway transport recovery helpers."""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from gateway.status import (
    HERMES_TELEGRAM_PAUSED,
    HERMES_TELEGRAM_STALE,
    classify_gateway_transport_liveness,
    read_runtime_status,
)
from hermes_constants import get_hermes_home
from utils import atomic_json_write

_log = logging.getLogger(__name__)

RECOVERABLE_CODES = {HERMES_TELEGRAM_PAUSED, HERMES_TELEGRAM_STALE}
DEFAULT_MAX_RESTARTS = 3
DEFAULT_WINDOW_SECONDS = 15 * 60
RECOVERY_STATE_FILE = "gateway_recovery_state.json"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _coerce_datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(raw)
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def recovery_state_path(profile_home: Optional[Path] = None) -> Path:
    return (profile_home or get_hermes_home()) / RECOVERY_STATE_FILE


def load_recovery_state(path: Optional[Path] = None) -> dict[str, Any]:
    state_path = path or recovery_state_path()
    if not state_path.exists():
        return {"restart_events": []}
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _log.warning("Gateway recovery state is unreadable: %s", state_path, exc_info=True)
        return {
            "restart_events": [],
            "state_corrupt": True,
            "state_path": str(state_path),
            "state_error": str(exc),
        }
    if not isinstance(data, dict):
        return {
            "restart_events": [],
            "state_corrupt": True,
            "state_path": str(state_path),
            "state_error": "state root is not an object",
        }
    events = data.get("restart_events")
    if not isinstance(events, list):
        data["restart_events"] = []
        data["state_corrupt"] = True
        data["state_path"] = str(state_path)
        data["state_error"] = "restart_events is not a list"
        return data
    for event in events:
        if not isinstance(event, dict) or _coerce_datetime(event.get("at")) is None:
            data["state_corrupt"] = True
            data["state_path"] = str(state_path)
            data["state_error"] = "restart_events contains an invalid event"
            break
    return data


def save_recovery_state(state: dict[str, Any], path: Optional[Path] = None) -> None:
    state_path = path or recovery_state_path()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json_write(state_path, state)


def recent_restart_events(
    state: dict[str, Any],
    *,
    profile: str,
    now: Optional[datetime] = None,
    window_seconds: int = DEFAULT_WINDOW_SECONDS,
) -> list[dict[str, Any]]:
    now_dt = now or _utc_now()
    cutoff = now_dt - timedelta(seconds=max(0, int(window_seconds)))
    recent: list[dict[str, Any]] = []
    for event in state.get("restart_events", []) or []:
        if not isinstance(event, dict):
            continue
        if str(event.get("profile") or "default") != profile:
            continue
        at = _coerce_datetime(event.get("at"))
        if at is not None and at >= cutoff:
            recent.append(event)
    return recent


def pruned_restart_events(
    state: dict[str, Any],
    *,
    now: Optional[datetime] = None,
    window_seconds: int = DEFAULT_WINDOW_SECONDS,
) -> list[dict[str, Any]]:
    now_dt = now or _utc_now()
    cutoff = now_dt - timedelta(seconds=max(0, int(window_seconds)))
    pruned: list[dict[str, Any]] = []
    for event in state.get("restart_events", []) or []:
        if not isinstance(event, dict):
            continue
        at = _coerce_datetime(event.get("at"))
        if at is not None and at >= cutoff:
            pruned.append(event)
    return pruned


def _state_from_restart_history(
    *,
    profile: str,
    restart_history: Optional[list[Any]],
) -> Optional[dict[str, Any]]:
    if restart_history is None:
        return None
    events: list[dict[str, Any]] = []
    for value in restart_history:
        if isinstance(value, dict):
            event = dict(value)
            event["profile"] = str(event.get("profile") or profile)
            if "at" not in event and "timestamp" in event:
                event["at"] = event.get("timestamp")
        else:
            event = {"profile": profile, "at": value}
        events.append(event)
    return {"restart_events": events}


def _cooldown_remaining_seconds(
    events: list[dict[str, Any]],
    *,
    now: datetime,
    window_seconds: int,
) -> int:
    oldest: Optional[datetime] = None
    for event in events:
        at = _coerce_datetime(event.get("at"))
        if at is None:
            continue
        if oldest is None or at < oldest:
            oldest = at
    if oldest is None:
        return 0
    age = max(0, int((now - oldest).total_seconds()))
    return max(0, int(window_seconds) - age)


def record_recovery_attempt(
    *,
    profile: str,
    code: str,
    outcome: str,
    returncode: Optional[int] = None,
    state_path: Optional[Path] = None,
    now: Optional[datetime] = None,
    window_seconds: int = DEFAULT_WINDOW_SECONDS,
) -> dict[str, Any]:
    now_dt = now or _utc_now()
    state = load_recovery_state(state_path)
    kept = pruned_restart_events(
        state,
        now=now_dt,
        window_seconds=window_seconds,
    )
    event = {
        "at": _iso(now_dt),
        "profile": profile,
        "code": code,
        "outcome": outcome,
    }
    if returncode is not None:
        event["returncode"] = returncode
    kept.append(event)
    state["restart_events"] = kept
    state.pop("state_corrupt", None)
    state.pop("state_error", None)
    state.pop("state_path", None)
    state["updated_at"] = _iso(now_dt)
    save_recovery_state(state, state_path)
    return state


def record_recovery_restart(
    *,
    profile: str,
    code: str,
    state_path: Optional[Path] = None,
    now: Optional[datetime] = None,
    window_seconds: int = DEFAULT_WINDOW_SECONDS,
) -> dict[str, Any]:
    return record_recovery_attempt(
        profile=profile,
        code=code,
        outcome="success",
        state_path=state_path,
        now=now,
        window_seconds=window_seconds,
    )


def profile_restart_command(profile: str, *, python_executable: Optional[str] = None) -> list[str]:
    command = [python_executable or sys.executable, "-m", "hermes_cli.main"]
    if profile and profile != "default":
        command.extend(["--profile", profile])
    command.extend(["gateway", "restart"])
    return command


def profile_restart_display(profile: str) -> str:
    if profile and profile != "default":
        return f"hermes --profile {profile} gateway restart"
    return "hermes gateway restart"


def _first_recoverable_issue(liveness: dict[str, Any]) -> Optional[dict[str, Any]]:
    for issue in liveness.get("platforms", []) or []:
        if isinstance(issue, dict) and issue.get("code") in RECOVERABLE_CODES:
            return issue
    return None


def evaluate_gateway_recovery(
    liveness: dict[str, Any],
    *,
    profile: str = "default",
    state: Optional[dict[str, Any]] = None,
    restart_history: Optional[list[Any]] = None,
    now: Optional[datetime] = None,
    max_restarts: int = DEFAULT_MAX_RESTARTS,
    window_seconds: int = DEFAULT_WINDOW_SECONDS,
) -> dict[str, Any]:
    """Return a restart/noop/intervention decision for one Hermes profile."""
    now_dt = now or _utc_now()
    profile = profile or "default"
    issue = _first_recoverable_issue(liveness or {})
    history_state = _state_from_restart_history(
        profile=profile,
        restart_history=restart_history,
    )
    decision_state = history_state if history_state is not None else state
    state_corrupt = bool((decision_state or {}).get("state_corrupt"))
    recent = recent_restart_events(
        decision_state or {"restart_events": []},
        profile=profile,
        now=now_dt,
        window_seconds=window_seconds,
    )
    restart_command = profile_restart_command(profile)
    restart_command_display = profile_restart_display(profile)

    base = {
        "profile": profile,
        "restart_scope": "hermes_gateway_profile",
        "restart_command": restart_command,
        "restart_command_display": restart_command_display,
        "restart_count_in_window": len(recent),
        "cooldown": {
            "max_restarts": int(max_restarts),
            "window_seconds": int(window_seconds),
            "recent_restarts": len(recent),
        },
        "state_corrupt": state_corrupt,
        "state_path": (
            (decision_state or {}).get("state_path") if state_corrupt else None
        ),
        "state_error": (
            (decision_state or {}).get("state_error") if state_corrupt else None
        ),
        "forbidden_targets": [
            "qwen_tunnel",
            "ollama",
            "postgres",
            "neoengine_api",
            "cockpit",
            "unrelated_hermes_profiles",
        ],
    }

    if issue is None:
        return {
            **base,
            "action": "no_action",
            "command": None,
            "should_restart": False,
            "operator_intervention_required": False,
            "reason": "no_recoverable_transport_failure",
            "cooldown_remaining_seconds": 0,
        }

    code = str(issue.get("code") or "")
    if state_corrupt:
        return {
            **base,
            "action": "operator_intervention_required",
            "command": None,
            "should_restart": False,
            "operator_intervention_required": True,
            "reason": "recovery_state_corrupt",
            "code": code,
            "summary": issue.get("summary"),
            "details": (
                "Gateway recovery cooldown ledger is unreadable; "
                "operator must inspect or clear it before supervised restart"
            ),
            "cooldown_remaining_seconds": 0,
        }

    if len(recent) >= int(max_restarts):
        return {
            **base,
            "action": "operator_intervention_required",
            "command": None,
            "should_restart": False,
            "operator_intervention_required": True,
            "reason": "cooldown_exceeded",
            "code": code,
            "summary": issue.get("summary"),
            "details": f"{code}: restart cooldown exceeded",
            "cooldown_remaining_seconds": _cooldown_remaining_seconds(
                recent,
                now=now_dt,
                window_seconds=window_seconds,
            ),
        }

    return {
        **base,
        "action": "restart_profile",
        "command": restart_command_display,
        "should_restart": True,
        "operator_intervention_required": False,
        "reason": "recoverable_transport_failure",
        "code": code,
        "summary": issue.get("summary"),
        "details": f"{code}: restart affected Hermes gateway profile only",
        "cooldown_remaining_seconds": 0,
    }


def evaluate_current_gateway_recovery(
    *,
    profile: str = "default",
    state_path: Optional[Path] = None,
    now: Optional[datetime] = None,
    max_restarts: int = DEFAULT_MAX_RESTARTS,
    window_seconds: int = DEFAULT_WINDOW_SECONDS,
) -> dict[str, Any]:
    liveness = classify_gateway_transport_liveness(read_runtime_status())
    recovery_state = load_recovery_state(state_path)
    return evaluate_gateway_recovery(
        liveness,
        profile=profile,
        state=recovery_state,
        now=now,
        max_restarts=max_restarts,
        window_seconds=window_seconds,
    )


def execute_gateway_recovery(
    *,
    profile: str = "default",
    state_path: Optional[Path] = None,
    dry_run: bool = False,
    runner: Optional[Callable[..., Any]] = None,
    now: Optional[datetime] = None,
    max_restarts: int = DEFAULT_MAX_RESTARTS,
    window_seconds: int = DEFAULT_WINDOW_SECONDS,
) -> dict[str, Any]:
    """Apply a profile-scoped recovery decision.

    ``runner`` exists for tests and embedding surfaces; production uses
    ``subprocess.run`` and never touches unrelated services.
    """
    liveness = classify_gateway_transport_liveness(read_runtime_status())
    recovery_state = load_recovery_state(state_path)
    decision = evaluate_gateway_recovery(
        liveness,
        profile=profile,
        state=recovery_state,
        now=now,
        max_restarts=max_restarts,
        window_seconds=window_seconds,
    )
    if not decision.get("should_restart") or dry_run:
        return {**decision, "executed": False, "dry_run": bool(dry_run)}

    command = list(decision["restart_command"])
    run = runner or subprocess.run
    result = run(command, check=False)
    returncode = getattr(result, "returncode", 0)
    record_recovery_attempt(
        profile=profile,
        code=str(decision.get("code") or "unknown"),
        outcome="success" if returncode == 0 else "failure",
        returncode=returncode,
        state_path=state_path,
        now=now,
        window_seconds=window_seconds,
    )
    return {
        **decision,
        "executed": True,
        "dry_run": False,
        "returncode": returncode,
        "ok": returncode == 0,
    }
