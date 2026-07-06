from __future__ import annotations

import json
from pathlib import Path

from neoengine_local.cron_observability_collector import (
    collect_existing_cron_outputs,
    iter_receipts,
    redact_cron_sample,
)


def _write_receipt(root: Path, job: str, run: str, name: str, content: str) -> Path:
    path = root / ".hermes" / "cron" / "output" / job / run / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def test_collector_is_self_contained_budgeted_and_writes_redacted_ledger(tmp_path):
    hermes_home = tmp_path / ".hermes"
    receipt = _write_receipt(
        tmp_path,
        "job-123",
        "20260626T170000Z",
        "output.md",
        "Authorization: Bearer live-token\napi_key = abc123\nstatus ok\n",
    )

    summary = collect_existing_cron_outputs(
        hermes_home=hermes_home,
        max_new=10,
        max_scan_files=10,
        budget_seconds=5,
        now="2026-06-26T17:00:00Z",
    )

    assert summary["status"] == "PASS"
    assert summary["captured"] == 1
    assert summary["known_files"] == 1
    assert summary["redacted_records"] == 1
    assert summary["mode"] == "script_only_self_contained_budgeted_collector"

    ledger_path = Path(summary["ledger_path"])
    records = [json.loads(line) for line in ledger_path.read_text().splitlines()]
    assert len(records) == 1
    assert records[0]["path"] == str(receipt)
    assert records[0]["job_id"] == "job-123"
    assert "Bearer [REDACTED]" in records[0]["sample"]
    assert "api_key = [REDACTED]" in records[0]["sample"]
    assert "live-token" not in records[0]["sample"]
    assert "abc123" not in records[0]["sample"]

    summary_path = hermes_home / "state" / "cron-observability" / "collector-summary.json"
    assert json.loads(summary_path.read_text())["status"] == "PASS"


def test_collector_skips_unchanged_files_and_stays_silent_on_no_new_capture(tmp_path, capsys):
    hermes_home = tmp_path / ".hermes"
    _write_receipt(tmp_path, "job-123", "run", "output.md", "status ok\n")

    first = collect_existing_cron_outputs(hermes_home=hermes_home, now="2026-06-26T17:01:00Z")
    second = collect_existing_cron_outputs(hermes_home=hermes_home, now="2026-06-26T17:02:00Z")

    assert first["captured"] == 1
    assert second["captured"] == 0
    ledger_path = hermes_home / "state" / "cron-observability" / "run-ledger.jsonl"
    assert len(ledger_path.read_text().splitlines()) == 1
    assert capsys.readouterr().out == ""


def test_iter_receipts_respects_scan_limit_and_marks_budget_exhausted(tmp_path):
    cron_output = tmp_path / ".hermes" / "cron" / "output"
    for idx in range(5):
        _write_receipt(tmp_path, f"job-{idx}", "run", "output.md", f"status {idx}\n")

    paths, budget_exhausted = iter_receipts(cron_output, max_scan_files=2, deadline=10**12)

    assert len(paths) == 2
    assert budget_exhausted is True


def test_redactor_handles_common_secret_shapes():
    redacted, changed = redact_cron_sample("token: abc\npassword=def\nAuthorization: Bearer ghi")

    assert changed is True
    assert "abc" not in redacted
    assert "def" not in redacted
    assert "ghi" not in redacted
    assert redacted.count("[REDACTED]") == 3
