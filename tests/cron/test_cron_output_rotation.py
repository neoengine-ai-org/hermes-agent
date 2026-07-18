"""Tests for write-time cron output rotation in save_job_output."""

import pytest

import cron.jobs as jobs


@pytest.fixture()
def tmp_output(tmp_path, monkeypatch):
    monkeypatch.setattr("cron.jobs.HERMES_DIR", tmp_path)
    monkeypatch.setattr("cron.jobs.CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr("cron.jobs.JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr("cron.jobs.OUTPUT_DIR", tmp_path / "cron" / "output")
    # most tests exercise the count cap; the age floor has its own test
    monkeypatch.setenv("HERMES_CRON_OUTPUT_MIN_AGE_DAYS", "0")
    return tmp_path / "cron" / "output"


def _run_outputs(job_id):
    return sorted(p.name for p in (jobs.OUTPUT_DIR / job_id).glob("*.md"))


def test_rotation_caps_outputs(tmp_output, monkeypatch):
    monkeypatch.setenv("HERMES_CRON_OUTPUT_KEEP", "3")
    job_dir = tmp_output / "job1"
    job_dir.mkdir(parents=True)
    for i in range(5):
        (job_dir / f"2026-01-0{i + 1}_00-00-00.md").write_text(f"run {i}")
    jobs.save_job_output("job1", "newest run")
    outputs = _run_outputs("job1")
    assert len(outputs) == 3
    # oldest dropped, newest retained
    assert "2026-01-01_00-00-00.md" not in outputs
    assert "2026-01-02_00-00-00.md" not in outputs
    assert "2026-01-03_00-00-00.md" not in outputs


def test_rotation_age_floor_never_defeats_hard_cap(tmp_output, monkeypatch):
    monkeypatch.setenv("HERMES_CRON_OUTPUT_KEEP", "1")
    monkeypatch.setenv("HERMES_CRON_OUTPUT_MIN_AGE_DAYS", "30")
    job_dir = tmp_output / "job-young"
    job_dir.mkdir(parents=True)
    for i in range(5):
        (job_dir / f"2026-01-0{i + 1}_00-00-00.md").write_text(f"run {i}")
    jobs.save_job_output("job-young", "newest run")
    # All files are younger than the floor, but the count cap remains the
    # authoritative disk-fill backstop for high-frequency jobs.
    assert len(_run_outputs("job-young")) == 1


def test_rotation_disabled_with_zero(tmp_output, monkeypatch):
    monkeypatch.setenv("HERMES_CRON_OUTPUT_KEEP", "0")
    job_dir = tmp_output / "job2"
    job_dir.mkdir(parents=True)
    for i in range(5):
        (job_dir / f"2026-01-0{i + 1}_00-00-00.md").write_text(f"run {i}")
    jobs.save_job_output("job2", "newest run")
    assert len(_run_outputs("job2")) == 6


def test_rotation_default_and_garbage_env(monkeypatch):
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {})
    monkeypatch.setenv("HERMES_CRON_OUTPUT_KEEP", "not-a-number")
    assert jobs._output_keep_last() == 500
    monkeypatch.delenv("HERMES_CRON_OUTPUT_KEEP")
    assert jobs._output_keep_last() == 500
    monkeypatch.setenv("HERMES_CRON_OUTPUT_KEEP", "-5")
    assert jobs._output_keep_last() == 0


def test_rotation_never_escapes_output_dir(tmp_output, monkeypatch):
    monkeypatch.setenv("HERMES_CRON_OUTPUT_KEEP", "1")
    outside = tmp_output.parent.parent / "outside"
    outside.mkdir(parents=True)
    for i in range(5):
        (outside / f"doc-{i}.md").write_text("must survive")
    # a hand-edited traversal job id must not rotate outside OUTPUT_DIR
    with pytest.raises(ValueError, match="Invalid cron job id"):
        jobs.save_job_output("../../outside", "attack")
    assert len(list(outside.glob("*.md"))) == 5


def test_rotation_respects_retention_kill_switch(tmp_output, monkeypatch):
    monkeypatch.setenv("HERMES_CRON_OUTPUT_KEEP", "1")
    monkeypatch.setenv("HERMES_HOME_RETENTION_DISABLED", "1")
    job_dir = tmp_output / "job4"
    job_dir.mkdir(parents=True)
    for i in range(5):
        (job_dir / f"2026-01-0{i + 1}_00-00-00.md").write_text(f"run {i}")
    jobs.save_job_output("job4", "newest run")
    assert len(_run_outputs("job4")) == 6  # kill switch halts rotation


def test_rotation_sorts_by_mtime_not_name(tmp_output, monkeypatch):
    import os as _os
    import time as _time

    monkeypatch.setenv("HERMES_CRON_OUTPUT_KEEP", "2")
    job_dir = tmp_output / "job5"
    job_dir.mkdir(parents=True)
    legacy = job_dir / "zzz-legacy.md"  # sorts LAST by name but is OLDEST
    legacy.write_text("old")
    _os.utime(legacy, (_time.time() - 86400 * 30,) * 2)
    fresh = job_dir / "2026-01-01_00-00-00.md"
    fresh.write_text("fresh")
    jobs.save_job_output("job5", "newest run")
    outputs = _run_outputs("job5")
    assert len(outputs) == 2
    assert "zzz-legacy.md" not in outputs  # oldest by mtime dropped
    assert "2026-01-01_00-00-00.md" in outputs


def test_rotation_failure_never_breaks_write(tmp_output, monkeypatch):
    monkeypatch.setenv("HERMES_CRON_OUTPUT_KEEP", "1")

    # simulate an unexpected rotation failure at the OSError boundary
    real_iterdir = jobs.Path.iterdir

    def flaky_iterdir(self):
        if self.name == "job3":
            raise OSError("disk hiccup")
        return real_iterdir(self)

    monkeypatch.setattr(jobs.Path, "iterdir", flaky_iterdir)
    out = jobs.save_job_output("job3", "content survives")
    assert out.exists()
    assert out.read_text() == "content survives"
