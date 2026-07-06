from __future__ import annotations

import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / "neoengine_local" / "cron" / "watchdogs" / "neoengine-founder-hourly-watchdog.py"


def load_watchdog_module():
    spec = importlib.util.spec_from_file_location("neoengine_founder_hourly_watchdog", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_qwen_observed_state_counts_fresh_heartbeat_and_gateway_process(tmp_path, monkeypatch):
    module = load_watchdog_module()
    heartbeat_dir = tmp_path / "heartbeats"
    heartbeat_dir.mkdir()
    (heartbeat_dir / "NE-QWEN35-DEV-01.json").write_text(
        """
        {
          "org_id": "neoengine",
          "lane_id": "NE-QWEN35-DEV-01",
          "lane_type": "qwen_developer",
          "agent_type": "qwen3.5",
          "status": "working",
          "updated_at": "2026-06-27T14:00:00Z",
          "not_merge_evidence": true
        }
        """,
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "QWEN_HEARTBEATS", heartbeat_dir)
    monkeypatch.setattr(module, "age_minutes", lambda value: 3 if value == "2026-06-27T14:00:00Z" else None)
    monkeypatch.setattr(module, "now", lambda: "2026-06-27T14:03:00Z")
    monkeypatch.setattr(
        module,
        "run",
        lambda cmd, timeout=5, cwd=None: (
            0,
            "52497 /Users/neoengine/workspace/ai-org/hermes-agent/.venv/bin/python -m hermes_cli.main --profile qwen-ops-runner-conductor gateway run --replace",
        ),
    )

    state = module.qwen_observed_state({"lanes": []})

    assert state["live_count"] == 2
    assert state["stale_count"] == 0
    assert state["gateway_live"] is True
    assert state["summary"] == "Qwen 2 observed"
    assert state["observed"]["NE-QWEN35-DEV-01"]["not_merge_evidence"] is True
    assert state["observed"]["qwen-ops-runner-conductor"]["not_merge_evidence"] is True


def test_qwen_observed_state_reports_stale_without_runtime_capacity_claim(tmp_path, monkeypatch):
    module = load_watchdog_module()
    heartbeat_dir = tmp_path / "heartbeats"
    heartbeat_dir.mkdir()
    (heartbeat_dir / "NE-QWEN35-DEV-01.json").write_text(
        """
        {
          "org_id": "neoengine",
          "lane_id": "NE-QWEN35-DEV-01",
          "lane_type": "qwen_developer",
          "agent_type": "qwen3.5",
          "status": "working",
          "updated_at": "2026-06-26T23:42:25Z",
          "not_merge_evidence": true
        }
        """,
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "QWEN_HEARTBEATS", heartbeat_dir)
    monkeypatch.setattr(module, "age_minutes", lambda value: 99)
    monkeypatch.setattr(module, "run", lambda cmd, timeout=5, cwd=None: (1, ""))

    state = module.qwen_observed_state(
        {
            "lanes": [
                {
                    "lane_id": "NE-QWEN35-DEV-01",
                    "lane_family": "qwen",
                    "provider": "qwen",
                    "process_alive": False,
                    "process_command_matches": False,
                    "log_health": "PASS",
                }
            ]
        }
    )

    assert state["live_count"] == 0
    assert state["stale_count"] == 1
    assert state["summary"] == "Qwen 0 observed, 1 stale"
    assert state["observed"]["NE-QWEN35-DEV-01"]["fresh"] is False
