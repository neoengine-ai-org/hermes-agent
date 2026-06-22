from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

MODULE_PATH = (
    Path(__file__).resolve().parents[3]
    / "neoengine_local/cron/mac_postgres_trust_guard/cross_org_mac_postgres_trust_guard.py"
)
spec = importlib.util.spec_from_file_location("mac_postgres_trust_guard", MODULE_PATH)
assert spec and spec.loader
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)


def test_normalize_ed25519_keyscan_pins_approved_host() -> None:
    key = "AAAAC3NzaC1lZDI1NTE5AAAAIEDq"
    stdout = "# 192.168.5.136:22 SSH-2.0-OpenSSH\n192.168.5.136 ssh-rsa ignored\nmac-memory ssh-ed25519 " + key + "\n"

    assert mod.normalize_ed25519_keyscan(stdout, "192.168.5.136") == f"192.168.5.136 ssh-ed25519 {key}"


def test_repo_scope_only_repos_already_using_bridge() -> None:
    assert mod.repo_uses_postgres_bridge({"NEOENGINE_CI_POSTGRES_SSH_HOST"}, set())
    assert mod.repo_uses_postgres_bridge(set(), {"NEOENGINE_CI_POSTGRES_SSH_KNOWN_HOSTS"})
    assert mod.repo_uses_postgres_bridge(set(), {"NEOENGINE_CI_POSTGRES_SSH_KEY"})
    assert not mod.repo_uses_postgres_bridge({"UNRELATED"}, {"ALSO_UNRELATED"})


def test_actionable_events_are_silent_for_healthy_refreshes() -> None:
    events = [
        "neoengine-ai-org/neoengine:NEOENGINE_CI_POSTGRES_SSH_KNOWN_HOSTS:refreshed",
        "qwen:qwen-ops-01:mac-memory:192.168.5.136:verified",
    ]

    assert mod.actionable_events(events, [], "192.168.5.136") == []
    result = mod.GuardResult(
        status="PASS",
        approved_host="192.168.5.136",
        scoped_repos=["neoengine-ai-org/neoengine"],
        events=events,
        failures=[],
        started_at="2026-01-01T00:00:00+00:00",
        finished_at="2026-01-01T00:00:01+00:00",
    )
    assert mod.render_actionable(result) == ""


def test_actionable_events_report_host_drift() -> None:
    result = mod.GuardResult(
        status="PASS",
        approved_host="192.168.5.136",
        scoped_repos=["neoengine-ai-org/neoengine"],
        events=["neoengine-ai-org/neoengine:NEOENGINE_CI_POSTGRES_SSH_HOST:192.168.5.134->192.168.5.136"],
        failures=[],
        started_at="2026-01-01T00:00:00+00:00",
        finished_at="2026-01-01T00:00:01+00:00",
    )

    rendered = mod.render_actionable(result)
    assert "Status: PASS" in rendered
    assert "192.168.5.134->192.168.5.136" in rendered


def test_qwen_alias_script_uses_strict_hostkey_alias() -> None:
    script = mod.qwen_alias_script()

    assert "Host mac-memory mac-memory-codex-pickup" in script
    assert "HostKeyAlias {host}" in script
    assert "StrictHostKeyChecking yes" in script
    assert "UserKnownHostsFile ~/.ssh/known_hosts" in script
