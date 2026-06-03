import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cron import observability


def _job(job_id="job1", name="Test Job", **extra):
    data = {
        "id": job_id,
        "name": name,
        "schedule_display": "every 5m",
        "schedule": {"kind": "interval", "minutes": 5, "display": "every 5m"},
        "no_agent": True,
        "model": None,
        "provider": None,
        "workdir": None,
        "deliver": "local",
        "script": "watchdog.py",
        "prompt": "",
    }
    data.update(extra)
    return data


def test_cron_run_record_schema_validates(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    paths = observability.ensure_observability_install()
    record = observability.capture_cron_run(
        job=_job(),
        scheduled_at="2026-06-02T00:00:00+00:00",
        started_at="2026-06-02T00:00:01+00:00",
        finished_at="2026-06-02T00:00:03+00:00",
        exit_code=0,
        output="POSTURE: TEST_PASS\nPROOF: PASS\nACTIONS_APPLIED: one\nHANDOFFS_CREATED: h1\n",
        cron_output_path=str(tmp_path / "output.md"),
    )
    errors = observability.validate_run_record(record)
    assert errors == []
    assert record["schema"] == "hermes.cron-run-record.v1"
    assert record["status"] == "success"
    assert record["posture"] == "TEST_PASS"
    assert record["proof_status"] == "PASS"
    assert record["actions_applied"] == ["one"]
    assert record["handoffs_created"] == ["h1"]
    assert Path(record["output_paths"]["cron_output"]).name == "output.md"
    assert paths["run_record_schema"].exists()


def test_successful_and_failed_cron_outputs_are_captured_and_deduped(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    ok_path = tmp_path / "ok.md"
    ok_path.write_text("useful action", encoding="utf-8")
    fail_path = tmp_path / "fail.md"
    fail_path.write_text("Script exited with code 2\nstderr: bad", encoding="utf-8")

    first = observability.capture_output_file_once(
        output_path=ok_path,
        job=_job("okjob"),
        started_at="2026-06-02T00:00:00+00:00",
        finished_at="2026-06-02T00:00:01+00:00",
        exit_code=0,
    )
    duplicate = observability.capture_output_file_once(
        output_path=ok_path,
        job=_job("okjob"),
        started_at="2026-06-02T00:00:00+00:00",
        finished_at="2026-06-02T00:00:01+00:00",
        exit_code=0,
    )
    failed = observability.capture_output_file_once(
        output_path=fail_path,
        job=_job("failjob"),
        started_at="2026-06-02T00:00:00+00:00",
        finished_at="2026-06-02T00:00:01+00:00",
        exit_code=2,
    )
    assert first is not None
    assert duplicate is None
    assert failed is not None
    assert failed["status"] == "failed"
    ledger = observability.base_dir() / "cron-run-ledger.jsonl"
    assert len(ledger.read_text(encoding="utf-8").splitlines()) == 2


def test_sensitive_strings_are_redacted_and_qwen_candidate_unreviewed(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    raw = "Authorization: Bearer sk-live-abcdefghijklmnopqrstuvwxyz123456 Plaid access_token=access-sandbox-123 account_number=1234567890123456"
    record = observability.capture_cron_run(
        job=_job(),
        scheduled_at="2026-06-02T00:00:00+00:00",
        started_at="2026-06-02T00:00:00+00:00",
        finished_at="2026-06-02T00:00:01+00:00",
        exit_code=1,
        output=raw,
        qwen_training_candidate=True,
    )
    serialized = json.dumps(record)
    assert "sk-live" not in serialized
    assert "access-sandbox" not in serialized
    assert "1234567890123456" not in serialized
    candidates = (observability.base_dir() / "qwen-training-candidates.jsonl").read_text(encoding="utf-8")
    candidate = json.loads(candidates.splitlines()[0])
    assert candidate["review_status"] == "unreviewed"
    assert "sk-live" not in candidates
    assert (observability.base_dir() / "redaction-log.jsonl").exists()


def test_qwen_candidates_are_deduped_and_quality_labeled(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    kwargs = dict(
        job=_job("proof-job", "Proof Job"),
        scheduled_at="2026-06-02T00:00:00+00:00",
        started_at="2026-06-02T00:00:00+00:00",
        finished_at="2026-06-02T00:00:01+00:00",
        exit_code=0,
        output="PROOF: FAIL\nclaimed success but proof failed\nschedule should change",
        qwen_training_candidate=True,
    )
    observability.capture_cron_run(**kwargs)
    observability.capture_cron_run(**kwargs)

    lines = (observability.base_dir() / "qwen-training-candidates.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    candidate = json.loads(lines[0])
    assert candidate["quality_label"] == "golden"
    assert candidate["quality_reasons"]
    assert candidate["review_status"] == "unreviewed"
    assert candidate["approved_for_training"] is False


def test_agent_review_rollup_names_top_jobs_and_qwen_quality(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    now = datetime(2026, 6, 2, tzinfo=timezone.utc)
    for i in range(6):
        observability.capture_cron_run(
            job=_job("high-value", "High Value"),
            scheduled_at=(now + timedelta(minutes=i)).isoformat(),
            started_at=(now + timedelta(minutes=i)).isoformat(),
            finished_at=(now + timedelta(minutes=i, seconds=1)).isoformat(),
            exit_code=0,
            output="ACTIONS_APPLIED: merged-pr",
        )
        observability.capture_cron_run(
            job=_job("noisy", "Noisy"),
            scheduled_at=(now + timedelta(minutes=i)).isoformat(),
            started_at=(now + timedelta(minutes=i)).isoformat(),
            finished_at=(now + timedelta(minutes=i, seconds=1)).isoformat(),
            exit_code=0,
            output="[SILENT] nothing to report",
        )
    for i in range(3):
        observability.capture_cron_run(
            job=_job("failing", "Failing"),
            scheduled_at=(now + timedelta(minutes=i)).isoformat(),
            started_at=(now + timedelta(minutes=i)).isoformat(),
            finished_at=(now + timedelta(minutes=i, seconds=1)).isoformat(),
            exit_code=1,
            output="same blocker repeated",
            qwen_training_candidate=True,
        )
    observability.capture_cron_run(
        job=_job("handoff", "Handoff"),
        scheduled_at=now.isoformat(),
        started_at=now.isoformat(),
        finished_at=(now + timedelta(seconds=1)).isoformat(),
        exit_code=0,
        output="HANDOFFS_CREATED: h1",
    )
    observability.capture_cron_run(
        job=_job("junk", "Junk Candidate"),
        scheduled_at=now.isoformat(),
        started_at=now.isoformat(),
        finished_at=(now + timedelta(seconds=1)).isoformat(),
        exit_code=0,
        output="generic low signal",
        qwen_training_candidate=True,
    )

    observability.summarize_cron_observability()
    observability.generate_schedule_recommendations()
    rollup = observability.generate_agent_review_rollup()
    assert rollup["highest_value_jobs"][0]["job_id"] == "high-value"
    assert any(j["job_id"] == "noisy" for j in rollup["noisiest_jobs"])
    assert any(j["job_id"] == "failing" for j in rollup["failing_or_flapping_jobs"])
    assert any(j["job_id"] in {"noisy", "failing", "handoff"} for j in rollup["cadence_change_candidates"])
    assert any(h["job_id"] == "handoff" for h in rollup["unconsumed_handoffs"])
    assert rollup["qwen_quality"]["junk_count"] >= 1
    assert (observability.base_dir() / "agent-review-rollup.json").exists()


def test_job_health_summary_rates_schedule_and_review_inbox(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    base = observability.base_dir()
    now = datetime(2026, 6, 2, tzinfo=timezone.utc)
    for i in range(3):
        observability.capture_cron_run(
            job=_job("flap", "Flapping Job"),
            scheduled_at=(now + timedelta(minutes=i)).isoformat(),
            started_at=(now + timedelta(minutes=i)).isoformat(),
            finished_at=(now + timedelta(minutes=i, seconds=1)).isoformat(),
            exit_code=1,
            output="same blocker repeated",
        )
    for i in range(10):
        observability.capture_cron_run(
            job=_job("noop", "Noop Job"),
            scheduled_at=(now + timedelta(minutes=i)).isoformat(),
            started_at=(now + timedelta(minutes=i)).isoformat(),
            finished_at=(now + timedelta(minutes=i, seconds=1)).isoformat(),
            exit_code=0,
            output="[SILENT] nothing to report",
        )
    for i in range(2):
        observability.capture_cron_run(
            job=_job("handoff", "Handoff Job"),
            scheduled_at=(now + timedelta(minutes=i)).isoformat(),
            started_at=(now + timedelta(minutes=i)).isoformat(),
            finished_at=(now + timedelta(minutes=i, seconds=1)).isoformat(),
            exit_code=0,
            output="HANDOFFS_CREATED: h-unconsumed",
        )

    jobs = [_job("flap", "Flapping Job"), _job("noop", "Noop Job"), _job("handoff", "Handoff Job")]
    summary = observability.summarize_cron_observability(jobs=jobs)
    assert summary["jobs"]["flap"]["consecutive_failures"] == 3
    assert summary["jobs"]["noop"]["no_action_rate"] == 1.0
    assert summary["jobs"]["handoff"]["stale_handoff_count"] >= 1

    recs = observability.generate_schedule_recommendations(jobs=jobs)
    assert all(r["requires_human_approval"] is True for r in recs["recommendations"])
    assert any(r["job_id"] == "noop" and r["recommendation"] == "decrease_frequency" for r in recs["recommendations"])

    inbox = observability.generate_agent_review_inbox()
    item_types = {item["issue_type"] for item in inbox["items"]}
    assert "failure" in item_types
    assert "handoff" in item_types
    assert (base / "job-health-summary.json").exists()
    assert (base / "schedule-recommendations.json").exists()
    assert (base / "agent-review-inbox.json").exists()


def test_collector_captures_generic_outputs_without_live_job_ids(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    out_dir = home / "cron" / "output" / "generic-job"
    out_dir.mkdir(parents=True)
    (out_dir / "2026-06-02_00-00-00.md").write_text("# Cron Job: Generic\n\n**Job ID:** generic-job\n\nUseful report", encoding="utf-8")
    (home / "cron").mkdir(parents=True, exist_ok=True)
    (home / "cron" / "jobs.json").write_text(json.dumps({"jobs": []}), encoding="utf-8")
    captured = observability.collect_existing_cron_outputs()
    assert captured["captured"] == 1
    again = observability.collect_existing_cron_outputs()
    assert again["captured"] == 0
