"""Deterministic tests for supervised Hermes gateway transport recovery.

Wave 1 added behavior-level classification for HERMES_TELEGRAM_PAUSED and
HERMES_TELEGRAM_STALE. Wave 2 adds a *decision* layer on top of that
classification: when classification fails red, what (if anything) should the
supervisor do, and which Hermes profile is affected?

These tests pin the contract for ``gateway.recovery.evaluate_gateway_recovery``:

  * Recovery is profile-scoped — touching the affected Hermes profile only.
  * Recovery never touches Qwen, Ollama, Postgres, the NeoEngine API, or
    other Hermes profiles, regardless of how unhealthy the affected profile
    looks.
  * Cooldown / flap protection bounds restart attempts to a small number
    within a rolling window (default: 3 restarts per 15 minutes). After
    that bound is reached the supervisor stops restarting and surfaces an
    operator-intervention requirement instead of looping forever.
  * The decision payload exposes the affected profile, the exact restart
    command (list + display string), the upstream liveness code, and the
    current restart pressure so dashboards and operators can act without
    re-deriving any of it.

No live services. No subprocesses. No clock dependence — every test pins
``now`` and ``restart_events`` explicitly.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from gateway import recovery as gateway_recovery
from gateway import status as gateway_status

evaluate_gateway_recovery = gateway_recovery.evaluate_gateway_recovery


# ── helpers ────────────────────────────────────────────────────────────────


def _liveness_healthy() -> dict[str, Any]:
    return gateway_status.classify_gateway_transport_liveness(None)


def _liveness_paused_telegram() -> dict[str, Any]:
    state = {
        "pid": 12345,
        "gateway_state": "running",
        "platforms": {
            "telegram": {
                "state": "paused",
                "error_message": "auto-paused after 10 consecutive reconnect failures",
                "reconnect_failure_count": 10,
            }
        },
    }
    return gateway_status.classify_gateway_transport_liveness(state)


def _liveness_stale_telegram(now: datetime) -> dict[str, Any]:
    state = {
        "pid": 12345,
        "gateway_state": "running",
        "platforms": {
            "telegram": {
                "state": "connected",
                "last_successful_poll_at": (now - timedelta(seconds=1800)).isoformat(),
            }
        },
    }
    return gateway_status.classify_gateway_transport_liveness(
        state, stale_after_seconds=900, now=now
    )


def _restart_state(*events: tuple[datetime, str]) -> dict[str, Any]:
    """Build the ``state`` dict shape expected by evaluate_gateway_recovery."""
    return {
        "restart_events": [
            {
                "at": ts.astimezone(timezone.utc).isoformat(),
                "profile": profile,
                "code": "HERMES_TELEGRAM_PAUSED",
            }
            for ts, profile in events
        ]
    }


def _flat_command(decision: dict[str, Any]) -> str:
    """Return a single string covering both the argv-list command and the
    operator-display command. Tests use this for substring assertions."""
    parts: list[str] = []
    cmd = decision.get("restart_command")
    if isinstance(cmd, (list, tuple)):
        parts.append(" ".join(str(p) for p in cmd))
    elif isinstance(cmd, str):
        parts.append(cmd)
    display = decision.get("restart_command_display")
    if display:
        parts.append(str(display))
    return " ".join(parts)


# ── action / no-action contract ────────────────────────────────────────────


class TestHealthyLivenessRequiresNoAction:
    def test_healthy_liveness_returns_no_action(self):
        """A green liveness verdict must not trigger any restart."""
        now = datetime(2026, 6, 20, 12, 0, 0, tzinfo=timezone.utc)
        decision = evaluate_gateway_recovery(
            _liveness_healthy(),
            profile="default",
            state=_restart_state(),
            now=now,
        )

        assert decision["action"] == "no_action", decision
        assert decision["should_restart"] is False, decision
        assert decision["operator_intervention_required"] is False, decision

    def test_healthy_liveness_no_action_even_with_recent_history(self):
        """A successful recent restart that healed the transport must not
        keep flapping — once liveness reports healthy, do nothing."""
        now = datetime(2026, 6, 20, 12, 0, 0, tzinfo=timezone.utc)
        decision = evaluate_gateway_recovery(
            _liveness_healthy(),
            profile="default",
            state=_restart_state((now - timedelta(seconds=60), "default")),
            now=now,
        )

        assert decision["action"] == "no_action", decision
        assert decision["should_restart"] is False, decision

    def test_healthy_liveness_still_surfaces_corrupt_recovery_state(self, tmp_path):
        """No restart is needed while transports are healthy, but dashboards
        should still see a corrupt ledger before the next incident."""
        now = datetime(2026, 6, 20, 12, 0, 0, tzinfo=timezone.utc)
        state_path = tmp_path / "gateway_recovery_state.json"
        state_path.write_text("{not-json", encoding="utf-8")

        decision = evaluate_gateway_recovery(
            _liveness_healthy(),
            profile="default",
            state=gateway_recovery.load_recovery_state(state_path),
            now=now,
        )

        assert decision["action"] == "no_action", decision
        assert decision["should_restart"] is False, decision
        assert decision["state_corrupt"] is True, decision
        assert decision["state_path"] == str(state_path), decision


# ── restart-affected-profile contract ─────────────────────────────────────


class TestProfileScopedRestart:
    def test_paused_telegram_recommends_restart_of_affected_profile(self):
        now = datetime(2026, 6, 20, 12, 0, 0, tzinfo=timezone.utc)
        decision = evaluate_gateway_recovery(
            _liveness_paused_telegram(),
            profile="default",
            state=_restart_state(),
            now=now,
        )

        assert decision["action"] == "restart_profile", decision
        assert decision["should_restart"] is True, decision
        assert decision["profile"] == "default", decision
        # restart_command is the argv form; restart_command_display is the
        # operator-display form. Both must reference the canonical hermes
        # gateway restart surface.
        command_text = _flat_command(decision)
        assert "gateway" in command_text and "restart" in command_text, command_text
        assert decision["restart_command_display"] == "hermes gateway restart", decision

    def test_stale_telegram_recommends_restart_of_affected_profile(self):
        now = datetime(2026, 6, 20, 12, 0, 0, tzinfo=timezone.utc)
        decision = evaluate_gateway_recovery(
            _liveness_stale_telegram(now),
            profile="default",
            state=_restart_state(),
            now=now,
        )

        assert decision["action"] == "restart_profile", decision
        assert decision["should_restart"] is True, decision
        assert decision["code"] == "HERMES_TELEGRAM_STALE", decision

    def test_named_profile_emits_profile_scoped_command(self):
        """The qwen-ops-runner-conductor profile must NOT trigger the default
        restart command — it must scope to ``--profile qwen-ops-runner-conductor``.
        Without this the supervisor would bounce the wrong profile when only
        the qwen-ops runner is sick."""
        now = datetime(2026, 6, 20, 12, 0, 0, tzinfo=timezone.utc)
        decision = evaluate_gateway_recovery(
            _liveness_paused_telegram(),
            profile="qwen-ops-runner-conductor",
            state=_restart_state(),
            now=now,
        )

        assert decision["action"] == "restart_profile", decision
        assert decision["profile"] == "qwen-ops-runner-conductor", decision

        command_text = _flat_command(decision)
        # Profile must appear in the command and it must use the canonical
        # ``--profile <name>`` argument shape rather than positional.
        assert "qwen-ops-runner-conductor" in command_text, command_text
        assert "--profile" in command_text, command_text
        assert (
            decision["restart_command_display"]
            == "hermes --profile qwen-ops-runner-conductor gateway restart"
        ), decision

    def test_default_profile_does_not_inject_profile_flag(self):
        """The default profile uses the bare ``hermes gateway restart`` form,
        not ``hermes --profile default gateway restart`` — keep the two
        surfaces distinct so operators reading logs can tell which profile
        the supervisor touched."""
        now = datetime(2026, 6, 20, 12, 0, 0, tzinfo=timezone.utc)
        decision = evaluate_gateway_recovery(
            _liveness_paused_telegram(),
            profile="default",
            state=_restart_state(),
            now=now,
        )

        command_text = _flat_command(decision)
        assert "--profile" not in command_text, command_text

    def test_profile_restart_argv_scopes_profile_before_gateway_command(self):
        """The executable argv must put ``--profile <name>`` before
        ``gateway restart`` so Hermes' early profile parser selects the
        affected profile's HERMES_HOME before any gateway modules import."""
        command = gateway_recovery.profile_restart_command(
            "qwen-ops-runner-conductor",
            python_executable="python",
        )

        assert command == [
            "python",
            "-m",
            "hermes_cli.main",
            "--profile",
            "qwen-ops-runner-conductor",
            "gateway",
            "restart",
        ]


# ── never-touch-unrelated-systems contract ────────────────────────────────


class TestRecoveryDoesNotTouchUnrelatedSystems:
    """The supervisor's authority is limited to the affected Hermes profile.

    A paused Telegram transport on profile A must NEVER produce a decision
    that targets Qwen, Ollama, Postgres, the NeoEngine API, or any other
    Hermes profile. This is the core safety property of the supervised
    recovery layer — without it, transport classification could cascade
    into infrastructure-wide bounces.
    """

    FORBIDDEN_SUBSTRINGS = (
        "qwen",
        "ollama",
        "postgres",
        "postgresql",
        "neoengine api",
        "api server",
        "tunnel",
    )

    def test_restart_command_does_not_mention_unrelated_systems(self):
        now = datetime(2026, 6, 20, 12, 0, 0, tzinfo=timezone.utc)
        decision = evaluate_gateway_recovery(
            _liveness_paused_telegram(),
            profile="default",
            state=_restart_state(),
            now=now,
        )

        # The argv-form command and the operator-display string are what
        # actually get executed / shown — they must not name Qwen, Ollama,
        # Postgres, the NeoEngine API, the tunnel, etc.
        command_text = _flat_command(decision).lower()
        for needle in self.FORBIDDEN_SUBSTRINGS:
            assert needle not in command_text, (
                f"recovery command must not mention {needle!r}: {command_text!r}"
            )

    def test_decision_surfaces_forbidden_targets_explicitly(self):
        """The decision payload exposes ``forbidden_targets`` so downstream
        consumers (dashboard, operator runbook) can show the operator what
        the supervisor will NOT touch even when the transport is fully red."""
        now = datetime(2026, 6, 20, 12, 0, 0, tzinfo=timezone.utc)
        decision = evaluate_gateway_recovery(
            _liveness_paused_telegram(),
            profile="default",
            state=_restart_state(),
            now=now,
        )

        forbidden = decision.get("forbidden_targets") or []
        forbidden_text = " ".join(str(t).lower() for t in forbidden)
        for needle in ("qwen", "ollama", "postgres", "neoengine_api"):
            assert needle in forbidden_text, (
                f"forbidden_targets must include {needle!r}: {forbidden!r}"
            )

    def test_restart_scope_is_profile_only(self):
        """The decision payload pins ``restart_scope`` to a per-profile
        Hermes gateway scope so a dashboard cannot confuse it with a
        broader infra-level recovery."""
        now = datetime(2026, 6, 20, 12, 0, 0, tzinfo=timezone.utc)
        decision = evaluate_gateway_recovery(
            _liveness_paused_telegram(),
            profile="qwen-ops-runner-conductor",
            state=_restart_state(),
            now=now,
        )

        assert decision.get("restart_scope") == "hermes_gateway_profile", decision


# ── cooldown / flap-guard contract ────────────────────────────────────────


class TestCooldownFlapGuard:
    """Recovery is bounded: max 3 restarts per 15 minutes per profile.

    The fourth attempt within the window must NOT restart. It must surface
    an operator-intervention requirement so a human can investigate the
    underlying transport failure (e.g. revoked token, network partition).
    """

    def test_three_restarts_in_fifteen_minutes_blocks_fourth(self):
        now = datetime(2026, 6, 20, 12, 0, 0, tzinfo=timezone.utc)
        state = _restart_state(
            (now - timedelta(minutes=14), "default"),
            (now - timedelta(minutes=9), "default"),
            (now - timedelta(minutes=4), "default"),
        )
        decision = evaluate_gateway_recovery(
            _liveness_paused_telegram(),
            profile="default",
            state=state,
            now=now,
        )

        assert decision["action"] == "operator_intervention_required", decision
        assert decision["should_restart"] is False, decision
        assert decision["operator_intervention_required"] is True, decision
        # Affected profile must still be surfaced so the operator knows
        # which slot to investigate.
        assert decision.get("profile") == "default", decision
        # Cooldown remaining must be > 0 when blocked — the dashboard uses
        # this to schedule a follow-up check rather than spinning. The
        # oldest in-window restart was 14 minutes ago against a 15-minute
        # window, so ~60s should remain; allow generous tolerance.
        cooldown_remaining = decision.get("cooldown_remaining_seconds")
        assert cooldown_remaining is not None and cooldown_remaining > 0, decision
        assert cooldown_remaining <= 15 * 60, decision

    def test_two_restarts_in_window_still_allows_third(self):
        now = datetime(2026, 6, 20, 12, 0, 0, tzinfo=timezone.utc)
        state = _restart_state(
            (now - timedelta(minutes=12), "default"),
            (now - timedelta(minutes=6), "default"),
        )
        decision = evaluate_gateway_recovery(
            _liveness_paused_telegram(),
            profile="default",
            state=state,
            now=now,
        )

        assert decision["action"] == "restart_profile", decision
        assert decision["cooldown"]["recent_restarts"] == 2, decision

    def test_old_restarts_outside_window_do_not_count(self):
        """Restarts older than the window must not block recovery — the
        supervisor uses a *rolling* count, not a lifetime count."""
        now = datetime(2026, 6, 20, 12, 0, 0, tzinfo=timezone.utc)
        state = _restart_state(
            (now - timedelta(minutes=60), "default"),
            (now - timedelta(minutes=45), "default"),
            (now - timedelta(minutes=30), "default"),
            (now - timedelta(minutes=16), "default"),  # just outside default 15-min window
        )
        decision = evaluate_gateway_recovery(
            _liveness_paused_telegram(),
            profile="default",
            state=state,
            now=now,
        )

        assert decision["action"] == "restart_profile", decision
        assert decision["cooldown"]["recent_restarts"] == 0, decision

    def test_cooldown_window_and_max_restarts_round_trip(self):
        """The decision payload must expose the cooldown configuration the
        supervisor used. Operators consume this to verify the active
        thresholds match the runbook's 3/15min bound."""
        now = datetime(2026, 6, 20, 12, 0, 0, tzinfo=timezone.utc)
        decision = evaluate_gateway_recovery(
            _liveness_paused_telegram(),
            profile="default",
            state=_restart_state(),
            now=now,
        )

        cooldown = decision.get("cooldown") or {}
        assert cooldown.get("max_restarts") == 3, decision
        assert cooldown.get("window_seconds") == 15 * 60, decision

    def test_cooldown_is_per_profile(self):
        """One profile flapping must not block recovery on a sibling profile.

        Conductor profile saturates its cooldown; default profile still has
        zero history and must be allowed to restart on its own liveness
        failure. Without this property, a single broken profile would
        silence supervised recovery for the whole fleet."""
        now = datetime(2026, 6, 20, 12, 0, 0, tzinfo=timezone.utc)
        shared_state = _restart_state(
            (now - timedelta(minutes=12), "qwen-ops-runner-conductor"),
            (now - timedelta(minutes=8), "qwen-ops-runner-conductor"),
            (now - timedelta(minutes=2), "qwen-ops-runner-conductor"),
        )

        # qwen-ops-runner-conductor is saturated — must block.
        blocked = evaluate_gateway_recovery(
            _liveness_paused_telegram(),
            profile="qwen-ops-runner-conductor",
            state=shared_state,
            now=now,
        )
        assert blocked["action"] == "operator_intervention_required", blocked

        # default profile is independent — must restart even though the
        # ledger has plenty of conductor entries.
        allowed = evaluate_gateway_recovery(
            _liveness_paused_telegram(),
            profile="default",
            state=shared_state,
            now=now,
        )
        assert allowed["action"] == "restart_profile", allowed
        assert allowed["cooldown"]["recent_restarts"] == 0, allowed

    def test_corrupt_recovery_state_blocks_supervised_restart(self, tmp_path):
        """A corrupt cooldown ledger must not reset restart pressure to zero.
        Block and route to an operator until the file is inspected or cleared."""
        now = datetime(2026, 6, 20, 12, 0, 0, tzinfo=timezone.utc)
        state_path = tmp_path / "gateway_recovery_state.json"
        state_path.write_text("{not-json", encoding="utf-8")

        state = gateway_recovery.load_recovery_state(state_path)
        decision = evaluate_gateway_recovery(
            _liveness_paused_telegram(),
            profile="default",
            state=state,
            now=now,
        )

        assert decision["action"] == "operator_intervention_required", decision
        assert decision["should_restart"] is False, decision
        assert decision["reason"] == "recovery_state_corrupt", decision
        assert decision["state_path"] == str(state_path), decision

    def test_recording_attempt_prunes_old_events_for_all_profiles(self, tmp_path):
        """Pruning is global to the ledger, not only the current profile, so
        long-lived multi-profile supervisors do not accumulate stale history."""
        now = datetime(2026, 6, 20, 12, 0, 0, tzinfo=timezone.utc)
        state_path = tmp_path / "gateway_recovery_state.json"
        state_path.write_text(
            json.dumps({
                "restart_events": [
                    {
                        "at": (now - timedelta(hours=2)).isoformat(),
                        "profile": "default",
                        "code": "HERMES_TELEGRAM_PAUSED",
                    },
                    {
                        "at": (now - timedelta(hours=3)).isoformat(),
                        "profile": "qwen-ops-runner-conductor",
                        "code": "HERMES_TELEGRAM_PAUSED",
                    },
                    {
                        "at": (now - timedelta(minutes=5)).isoformat(),
                        "profile": "qwen-ops-runner-conductor",
                        "code": "HERMES_TELEGRAM_PAUSED",
                    },
                ],
            }),
            encoding="utf-8",
        )

        state = gateway_recovery.record_recovery_attempt(
            profile="default",
            code="HERMES_TELEGRAM_PAUSED",
            outcome="success",
            state_path=state_path,
            now=now,
        )

        events = state["restart_events"]
        assert len(events) == 2, state
        assert {event["profile"] for event in events} == {
            "default",
            "qwen-ops-runner-conductor",
        }
        assert all(
            datetime.fromisoformat(event["at"]) >= now - timedelta(minutes=15)
            for event in events
        ), state


# ── decision-payload shape ────────────────────────────────────────────────


class TestDecisionPayloadShape:
    def test_decision_payload_includes_restart_count(self):
        """The decision must expose how many restarts the supervisor has seen
        in the current window — operators and dashboards consume this to
        plot flap pressure."""
        now = datetime(2026, 6, 20, 12, 0, 0, tzinfo=timezone.utc)
        state = _restart_state(
            (now - timedelta(minutes=10), "default"),
            (now - timedelta(minutes=5), "default"),
        )
        decision = evaluate_gateway_recovery(
            _liveness_paused_telegram(),
            profile="default",
            state=state,
            now=now,
        )

        cooldown = decision.get("cooldown") or {}
        assert cooldown.get("recent_restarts") == 2, decision

    def test_decision_payload_records_liveness_code(self):
        """Reason / details must round-trip the upstream liveness code so a
        dashboard reading only the decision payload can still classify the
        incident as HERMES_TELEGRAM_PAUSED vs HERMES_TELEGRAM_STALE."""
        now = datetime(2026, 6, 20, 12, 0, 0, tzinfo=timezone.utc)
        decision = evaluate_gateway_recovery(
            _liveness_paused_telegram(),
            profile="default",
            state=_restart_state(),
            now=now,
        )

        assert decision.get("code") == "HERMES_TELEGRAM_PAUSED", decision
        summary = decision.get("summary") or ""
        assert "telegram" in summary.lower(), decision


class TestExecuteGatewayRecovery:
    def test_successful_restart_records_profile_cooldown_event(self, monkeypatch, tmp_path):
        """The action path records a restart only after the profile-scoped
        restart command succeeds. This is the durable flap guard ledger."""
        now = datetime(2026, 6, 20, 12, 0, 0, tzinfo=timezone.utc)
        state_path = tmp_path / "gateway_recovery_state.json"
        runtime_state = {
            "pid": 12345,
            "gateway_state": "running",
            "platforms": {
                "telegram": {
                    "state": "paused",
                    "error_message": "auto-paused after 10 consecutive reconnect failures",
                    "reconnect_failure_count": 10,
                }
            },
        }
        calls: list[list[str]] = []

        class _Result:
            returncode = 0

        def _runner(command, check=False):
            calls.append(list(command))
            assert check is False
            return _Result()

        monkeypatch.setattr(gateway_recovery, "read_runtime_status", lambda: runtime_state)

        result = gateway_recovery.execute_gateway_recovery(
            profile="qwen-ops-runner-conductor",
            state_path=state_path,
            runner=_runner,
            now=now,
        )

        assert result["executed"] is True, result
        assert result["ok"] is True, result
        assert calls and "--profile" in calls[0], calls
        assert "qwen-ops-runner-conductor" in calls[0], calls

        recorded = json.loads(state_path.read_text(encoding="utf-8"))
        events = recorded.get("restart_events") or []
        assert len(events) == 1, recorded
        assert events[0]["profile"] == "qwen-ops-runner-conductor", recorded
        assert events[0]["code"] == "HERMES_TELEGRAM_PAUSED", recorded
        assert events[0]["outcome"] == "success", recorded

    def test_failed_restart_attempt_counts_toward_cooldown(self, monkeypatch, tmp_path):
        """A failed restart attempt can flap just as hard as a successful one;
        record it so a broken service manager cannot be hammered forever."""
        now = datetime(2026, 6, 20, 12, 0, 0, tzinfo=timezone.utc)
        state_path = tmp_path / "gateway_recovery_state.json"
        runtime_state = {
            "pid": 12345,
            "gateway_state": "running",
            "platforms": {
                "telegram": {
                    "state": "paused",
                    "error_message": "auto-paused after 10 consecutive reconnect failures",
                    "reconnect_failure_count": 10,
                }
            },
        }

        class _Result:
            returncode = 7

        monkeypatch.setattr(gateway_recovery, "read_runtime_status", lambda: runtime_state)

        result = gateway_recovery.execute_gateway_recovery(
            profile="default",
            state_path=state_path,
            runner=lambda command, check=False: _Result(),
            now=now,
        )

        assert result["executed"] is True, result
        assert result["ok"] is False, result
        assert result["returncode"] == 7, result

        recorded = json.loads(state_path.read_text(encoding="utf-8"))
        events = recorded.get("restart_events") or []
        assert len(events) == 1, recorded
        assert events[0]["profile"] == "default", recorded
        assert events[0]["outcome"] == "failure", recorded
        assert events[0]["returncode"] == 7, recorded


# ── liveness payload severity / process-alive contract ───────────────────


class TestLivenessSeverityForDashboard:
    """The dashboard ``/api/status`` payload needs structured severity for a
    process-alive / transport-dead gateway. Wave 1 added the classification;
    Wave 2 adds the severity + headline that the dashboard consumes so the
    UI can render an unhealthy badge without re-deriving the verdict."""

    def test_process_alive_transport_dead_payload_is_visible(self):
        state = {
            "pid": 12345,
            "gateway_state": "running",
            "platforms": {
                "telegram": {
                    "state": "paused",
                    "error_message": "auto-paused after 10 consecutive reconnect failures",
                }
            },
        }
        liveness = gateway_status.classify_gateway_transport_liveness(state)

        # Severity must be unhealthy; the dashboard reads this directly.
        assert liveness.get("severity") == "unhealthy", liveness
        # The process_alive bit is the discriminator that gives the
        # incident its "transport dead / process alive" shape.
        assert liveness.get("process_alive") is True, liveness
        headline = (liveness.get("headline") or "").lower()
        assert "transport dead" in headline and "process alive" in headline, liveness

    def test_healthy_liveness_payload_reports_healthy_severity(self):
        liveness = gateway_status.classify_gateway_transport_liveness(None)

        assert liveness.get("severity") == "healthy", liveness
        assert liveness.get("process_alive") is False, liveness
