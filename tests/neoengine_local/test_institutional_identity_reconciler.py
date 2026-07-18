"""Tests for the per-profile institutional-identity reconciler.

All mutation is exercised ONLY against pytest ``tmp_path`` stores — never a
real ``~/.hermes`` profile.
"""

import json

import pytest

from neoengine_local.institutional_identity_reconciler import (
    Finding,
    ReconcileConfig,
    Refusal,
    is_qwen_preflight_error,
    main,
    reconcile,
    redact_secrets,
)

IDENTITY_FIELD = "institutional_identity"


def _write_store(tmp_path, jobs, extra=None, name="jobs.json"):
    store = {"jobs": jobs, "updated_at": "2026-07-18T00:00:00Z"}
    if extra:
        store.update(extra)
    path = tmp_path / name
    path.write_text(json.dumps(store))
    return path


def _job(job_id, *, enabled=True, identity=None, command=None, **extra):
    job = {"id": job_id, "enabled": enabled}
    if identity is not None:
        job[IDENTITY_FIELD] = identity
    if command is not None:
        job["command"] = command
    job.update(extra)
    return job


def _cfg(jobs_file, mapping, *, execute=False, explicit=True, **overrides):
    return ReconcileConfig(
        jobs_file=jobs_file,
        jobs_file_explicit=explicit,
        mapping=mapping,
        execute=execute,
        **overrides,
    )


def _by_id(entries):
    return {e.job_id: e for e in entries}


# ---------------------------------------------------------------------------
# null identity
# ---------------------------------------------------------------------------

def test_null_identity_reported_and_bound_on_execute(tmp_path):
    path = _write_store(tmp_path, [_job("j1", command="backup.sh")])
    mapping = {"by_id": {"j1": "identity-alpha"}}

    # dry-run: reported as needing attention, nothing written
    dry = reconcile(_cfg(path, mapping))
    entry = _by_id(dry.entries)["j1"]
    assert entry.finding == Finding.NULL_IDENTITY_NEEDS_BIND
    assert entry.needs_attention is True
    assert entry.target_identity == "identity-alpha"
    assert dry.mutated is False
    assert json.loads(path.read_text())["jobs"][0].get(IDENTITY_FIELD) is None

    # execute: bound, file rewritten with the identity
    ex = reconcile(_cfg(path, mapping, execute=True))
    entry = _by_id(ex.entries)["j1"]
    assert entry.finding == Finding.BOUND
    assert ex.mutated is True
    assert json.loads(path.read_text())["jobs"][0][IDENTITY_FIELD] == "identity-alpha"
    # field-level inverse patch records old->new
    deltas = ex.inverse_patch["field_deltas"]
    assert deltas == [
        {"job_id": "j1", "field": IDENTITY_FIELD, "old": None, "new": "identity-alpha"}
    ]


# ---------------------------------------------------------------------------
# already-correct identity (idempotent no-op)
# ---------------------------------------------------------------------------

def test_already_correct_identity_is_noop(tmp_path):
    path = _write_store(
        tmp_path, [_job("j1", identity="identity-alpha", command="backup.sh")]
    )
    mapping = {"by_id": {"j1": "identity-alpha"}}
    report = reconcile(_cfg(path, mapping, execute=True))
    entry = _by_id(report.entries)["j1"]
    assert entry.finding == Finding.ALREADY_CORRECT
    assert report.mutated is False
    assert report.inverse_patch["field_deltas"] == []


# ---------------------------------------------------------------------------
# inactive/paused profile (skipped, no mutation)
# ---------------------------------------------------------------------------

def test_inactive_profile_skipped_no_mutation(tmp_path):
    path = _write_store(
        tmp_path, [_job("j1", command="backup.sh")], extra={"paused": True}
    )
    mapping = {"by_id": {"j1": "identity-alpha"}}
    report = reconcile(_cfg(path, mapping, execute=True))
    assert report.profile_active is False
    assert report.mutated is False
    entry = _by_id(report.entries)["j1"]
    assert entry.finding == Finding.SKIPPED_INACTIVE_PROFILE
    # nothing written
    assert json.loads(path.read_text())["jobs"][0].get(IDENTITY_FIELD) is None


def test_inactive_profile_via_override(tmp_path):
    path = _write_store(tmp_path, [_job("j1", command="backup.sh")])
    report = reconcile(
        _cfg(path, {"by_id": {"j1": "x"}}, execute=True, profile_active_override=False)
    )
    assert report.profile_active is False
    assert all(e.finding == Finding.SKIPPED_INACTIVE_PROFILE for e in report.entries)
    assert report.mutated is False


# ---------------------------------------------------------------------------
# ambiguous mapping (refused)
# ---------------------------------------------------------------------------

def test_ambiguous_mapping_refused(tmp_path):
    path = _write_store(tmp_path, [_job("j1", command="backup.sh")])
    # by_id says alpha, by_command says beta -> ambiguous
    mapping = {
        "by_id": {"j1": "identity-alpha"},
        "by_command": {"backup.sh": "identity-beta"},
    }
    report = reconcile(_cfg(path, mapping, execute=True))
    entry = _by_id(report.entries)["j1"]
    assert entry.finding == Finding.AMBIGUOUS_MAPPING
    assert report.mutated is False
    assert json.loads(path.read_text())["jobs"][0].get(IDENTITY_FIELD) is None


# ---------------------------------------------------------------------------
# wrong-identity conflict (refused, no silent overwrite)
# ---------------------------------------------------------------------------

def test_wrong_identity_conflict_refused_no_overwrite(tmp_path):
    path = _write_store(
        tmp_path, [_job("j1", identity="identity-alpha", command="backup.sh")]
    )
    mapping = {"by_id": {"j1": "identity-beta"}}  # says beta, job bound to alpha
    report = reconcile(_cfg(path, mapping, execute=True))
    entry = _by_id(report.entries)["j1"]
    assert entry.finding == Finding.WRONG_IDENTITY_CONFLICT
    assert entry.current_identity == "identity-alpha"
    assert entry.target_identity == "identity-beta"
    assert report.mutated is False
    # not silently overwritten
    assert json.loads(path.read_text())["jobs"][0][IDENTITY_FIELD] == "identity-alpha"


# ---------------------------------------------------------------------------
# repeat run (idempotency: second run no change)
# ---------------------------------------------------------------------------

def test_repeat_run_is_idempotent(tmp_path):
    path = _write_store(tmp_path, [_job("j1", command="backup.sh")])
    mapping = {"by_id": {"j1": "identity-alpha"}}

    first = reconcile(_cfg(path, mapping, execute=True))
    assert first.mutated is True
    after_first = path.read_text()

    second = reconcile(_cfg(path, mapping, execute=True))
    assert second.mutated is False
    assert _by_id(second.entries)["j1"].finding == Finding.ALREADY_CORRECT
    assert second.inverse_patch["field_deltas"] == []
    # byte-identical store (idempotent no-op does not rewrite)
    assert path.read_text() == after_first


# ---------------------------------------------------------------------------
# intervening job additions/removals between snapshot and current
# ---------------------------------------------------------------------------

def test_snapshot_drift_reported_not_clobbered(tmp_path):
    # snapshot had j1 and j-removed; current has j1 and j-added
    snapshot = [_job("j1", command="a.sh"), _job("j-removed", command="b.sh")]
    current = [_job("j1", command="a.sh"), _job("j-added", command="c.sh")]
    path = _write_store(tmp_path, current)
    mapping = {"by_id": {"j1": "identity-alpha", "j-added": "identity-gamma"}}

    report = reconcile(
        _cfg(path, mapping, execute=True, snapshot_jobs=snapshot)
    )
    patch = report.inverse_patch
    assert patch["added_jobs"] == ["j-added"]
    assert patch["removed_jobs"] == ["j-removed"]
    # the removed job is NOT resurrected into the store (no blind clobber)
    stored_ids = {j["id"] for j in json.loads(path.read_text())["jobs"]}
    assert stored_ids == {"j1", "j-added"}


# ---------------------------------------------------------------------------
# Qwen preflight error classification
# ---------------------------------------------------------------------------

def test_qwen_preflight_error_classified_not_mutated(tmp_path):
    path = _write_store(
        tmp_path,
        [_job("j1", command="backup.sh", preflight_status="FAILED_TOOLING_OR_CONTEXT")],
    )
    mapping = {"by_id": {"j1": "identity-alpha"}}
    report = reconcile(_cfg(path, mapping, execute=True))
    entry = _by_id(report.entries)["j1"]
    assert entry.finding == Finding.QWEN_PREFLIGHT_ERROR
    assert entry.needs_attention is True
    assert report.mutated is False
    # not bound despite an unambiguous null-identity mapping (fail closed)
    assert json.loads(path.read_text())["jobs"][0].get(IDENTITY_FIELD) is None


def test_is_qwen_preflight_error_accepts_plain_status_field():
    assert is_qwen_preflight_error({"last_status": "error"}) is True
    assert is_qwen_preflight_error({"status": "ERROR: timeout"}) is True
    assert is_qwen_preflight_error({"last_status": "ok"}) is False
    assert is_qwen_preflight_error({}) is False


# ---------------------------------------------------------------------------
# redaction: secret-looking values never appear in the report
# ---------------------------------------------------------------------------

def test_secrets_redacted_from_report(tmp_path):
    secret_token = "AKIAABCDEFGHIJKLMNOP"
    secret_pw = "hunter2SuperSecretValue"
    command = f"deploy.sh --token={secret_token} && export API_PASSWORD={secret_pw}"
    path = _write_store(tmp_path, [_job("j1", command=command)])
    mapping = {"by_id": {"j1": "identity-alpha"}}
    report = reconcile(_cfg(path, mapping, execute=True))

    emitted = json.dumps(report.to_dict())
    assert secret_token not in emitted
    assert secret_pw not in emitted
    assert "REDACTED" in emitted
    entry = _by_id(report.entries)["j1"]
    assert entry.redacted_command is not None
    assert secret_token not in entry.redacted_command
    assert secret_pw not in entry.redacted_command


def test_redact_secrets_unit():
    assert redact_secrets(None) is None
    assert "sk-1234567890" not in redact_secrets("token=sk-1234567890abcdefghijk")
    out = redact_secrets("password: correct-horse-battery-staple-1234")
    assert "correct-horse-battery-staple-1234" not in out


# ---------------------------------------------------------------------------
# refuse to mutate when only a HERMES_HOME default is implied
# ---------------------------------------------------------------------------

def test_execute_refused_without_explicit_jobs_file(tmp_path):
    # store exists at a home-shaped default path but was NOT passed explicitly
    home = tmp_path / "hermes_home"
    (home / "cron").mkdir(parents=True)
    path = home / "cron" / "jobs.json"
    path.write_text(json.dumps({"jobs": [_job("j1", command="backup.sh")]}))
    mapping = {"by_id": {"j1": "identity-alpha"}}

    report = reconcile(
        _cfg(path, mapping, execute=True, explicit=False)
    )
    assert report.refused is True
    assert report.refusal_reason == Refusal.EXECUTE_WITHOUT_EXPLICIT_JOBS_FILE.value
    assert report.mutated is False
    # unchanged on disk
    assert json.loads(path.read_text())["jobs"][0].get(IDENTITY_FIELD) is None


def test_cli_home_execute_refuses_but_jobs_file_execute_binds(tmp_path):
    home = tmp_path / "hermes_home"
    (home / "cron").mkdir(parents=True)
    path = home / "cron" / "jobs.json"
    path.write_text(json.dumps({"jobs": [_job("j1", command="backup.sh")]}))
    mapping_path = tmp_path / "mapping.json"
    mapping_path.write_text(json.dumps({"by_id": {"j1": "identity-alpha"}}))

    # --home + --execute -> refused (exit 2), no mutation
    rc = main(
        ["--home", str(home), "--mapping", str(mapping_path), "--execute"]
    )
    assert rc == 2
    assert json.loads(path.read_text())["jobs"][0].get(IDENTITY_FIELD) is None

    # explicit --jobs-file + --execute -> binds (exit 0)
    rc = main(
        ["--jobs-file", str(path), "--mapping", str(mapping_path), "--execute"]
    )
    assert rc == 0
    assert json.loads(path.read_text())["jobs"][0][IDENTITY_FIELD] == "identity-alpha"


# ---------------------------------------------------------------------------
# misc: disabled kill switch, disabled jobs ignored, no-mapping
# ---------------------------------------------------------------------------

def test_kill_switch_disables_run(tmp_path, monkeypatch):
    monkeypatch.setenv("INSTITUTIONAL_IDENTITY_RECONCILER_DISABLED", "1")
    path = _write_store(tmp_path, [_job("j1", command="backup.sh")])
    report = reconcile(_cfg(path, {"by_id": {"j1": "x"}}, execute=True))
    assert report.disabled is True
    assert report.mutated is False
    assert json.loads(path.read_text())["jobs"][0].get(IDENTITY_FIELD) is None


def test_disabled_jobs_ignored(tmp_path):
    path = _write_store(
        tmp_path,
        [
            _job("j1", command="a.sh", enabled=False),
            _job("j2", command="b.sh"),
        ],
    )
    mapping = {"by_id": {"j1": "identity-alpha", "j2": "identity-beta"}}
    report = reconcile(_cfg(path, mapping, execute=True))
    ids = {e.job_id for e in report.entries}
    assert ids == {"j2"}  # disabled j1 never considered


def test_no_mapping_entry_needs_attention_when_null(tmp_path):
    path = _write_store(tmp_path, [_job("j1", command="orphan.sh")])
    report = reconcile(_cfg(path, {"by_id": {}}, execute=True))
    entry = _by_id(report.entries)["j1"]
    assert entry.finding == Finding.NO_MAPPING
    assert entry.needs_attention is True
    assert report.mutated is False
