"""Acceptance tests for the gateway transport-liveness runbook.

The runbook is the operator-facing surface that documents the supervised
recovery decision. These tests pin the load-bearing content so a future
edit cannot silently drop the exact-command block, the cooldown / flap
rule, or the profile-specific commands an operator pastes during an
incident.

We intentionally do NOT assert the prose verbatim — only the structural
signals that prove the runbook still covers:

  1. Status + doctor first-response commands.
  2. PID-lookup command (for confirming the gateway process is alive).
  3. Profile-specific restart commands (default and named profile).
  4. Post-restart verification block (oneshot + log grep).
  5. Cooldown / flap rule (max 3 restarts in 15 minutes; then operator).
  6. Liveness codes HERMES_TELEGRAM_PAUSED and HERMES_TELEGRAM_STALE.
  7. The "do not chase Qwen / Ollama / Postgres / API" non-claim.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

RUNBOOK = (
    Path(__file__).resolve().parents[2]
    / "docs"
    / "runbooks"
    / "hermes-gateway-transport-liveness.md"
)


@pytest.fixture(scope="module")
def runbook_text() -> str:
    assert RUNBOOK.exists(), f"missing runbook at {RUNBOOK}"
    return RUNBOOK.read_text(encoding="utf-8")


def test_runbook_lists_status_and_doctor_commands(runbook_text: str) -> None:
    assert "hermes gateway status" in runbook_text
    assert "hermes doctor" in runbook_text


def test_runbook_includes_pid_lookup_command(runbook_text: str) -> None:
    """Operators need a single command to confirm the gateway process is
    alive even when the transport is dead. ``hermes gateway pid`` is the
    canonical zero-noise lookup (returns PID or empty)."""
    assert re.search(
        r"hermes\s+gateway\s+(pid|status\s+--pid|status\s+-p)", runbook_text
    ), runbook_text


def test_runbook_documents_default_and_named_profile_restart(runbook_text: str) -> None:
    """Default profile uses the bare command; named profile uses
    ``--profile <name>`` so the operator can tell which slot they touched."""
    assert "hermes gateway restart" in runbook_text
    assert "--profile qwen-ops-runner-conductor" in runbook_text
    assert "hermes --profile qwen-ops-runner-conductor gateway restart" in runbook_text


def test_runbook_documents_post_restart_verification(runbook_text: str) -> None:
    """After a restart, the operator must verify the transport reconnected
    and the agent oneshot succeeds. Both checks are load-bearing."""
    assert "Connected to Telegram" in runbook_text
    assert re.search(r"hermes\s+-z", runbook_text), "expected hermes -z oneshot verification"


def test_runbook_documents_cooldown_and_flap_rule(runbook_text: str) -> None:
    """Cooldown / flap guard prose is canon. Pin the numbers so a casual
    edit cannot drop them — they're load-bearing for incident response."""
    lowered = runbook_text.lower()
    assert "3" in runbook_text and "15" in runbook_text, runbook_text
    assert "operator" in lowered, runbook_text
    # The runbook must explicitly say the supervisor stops restarting
    # after the bound is reached.
    assert re.search(
        r"(max(imum)?\s+\w*\s*3\s+restart|3\s+restart.*15|three\s+restart)",
        lowered,
    ), runbook_text


def test_runbook_documents_corrupt_recovery_state_path(runbook_text: str) -> None:
    """A corrupt cooldown ledger blocks supervised restart. The runbook must
    preserve the non-destructive archive-and-retry path."""
    lowered = runbook_text.lower()
    assert "recovery_state_corrupt" in runbook_text
    assert "gateway_recovery_state.json" in runbook_text
    assert re.search(r"\bmv\s+", runbook_text), runbook_text
    assert "corrupt.$(date" in runbook_text
    assert "let Hermes resolve the profile home" in runbook_text
    assert "do not guess an\nad-hoc `HERMES_HOME`" in runbook_text
    assert "gateway recover --dry-run" in lowered


def test_runbook_documents_liveness_codes(runbook_text: str) -> None:
    assert "HERMES_TELEGRAM_PAUSED" in runbook_text
    assert "HERMES_TELEGRAM_STALE" in runbook_text


def test_runbook_does_not_chase_unrelated_subsystems(runbook_text: str) -> None:
    """The runbook must include the explicit non-claim that Qwen tunnel,
    Ollama, Postgres, and the NeoEngine API are *not* in scope for the
    Telegram-paused recovery path. Without this, operators routinely bounce
    healthy infrastructure during transport incidents."""
    lowered = runbook_text.lower()
    assert "qwen" in lowered
    assert "ollama" in lowered
    assert "postgres" in lowered
    assert "neoengine" in lowered or "api" in lowered
