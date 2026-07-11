"""Tests for write-time cron output rotation in save_job_output."""

import pytest

import cron.jobs as jobs


@pytest.fixture()
def tmp_output(tmp_path, monkeypatch):
    monkeypatch.setattr("cron.jobs.HERMES_DIR", tmp_path)
    monkeypatch.setattr("cron.jobs.CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr("cron.jobs.JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr("cron.jobs.OUTPUT_DIR", tmp_path / "cron" / "output")
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


def test_rotation_disabled_with_zero(tmp_output, monkeypatch):
    monkeypatch.setenv("HERMES_CRON_OUTPUT_KEEP", "0")
    job_dir = tmp_output / "job2"
    job_dir.mkdir(parents=True)
    for i in range(5):
        (job_dir / f"2026-01-0{i + 1}_00-00-00.md").write_text(f"run {i}")
    jobs.save_job_output("job2", "newest run")
    assert len(_run_outputs("job2")) == 6


def test_rotation_default_and_garbage_env(monkeypatch):
    monkeypatch.setenv("HERMES_CRON_OUTPUT_KEEP", "not-a-number")
    assert jobs._output_keep_last() == 200
    monkeypatch.delenv("HERMES_CRON_OUTPUT_KEEP")
    assert jobs._output_keep_last() == 200
    monkeypatch.setenv("HERMES_CRON_OUTPUT_KEEP", "-5")
    assert jobs._output_keep_last() == 0


def test_rotation_failure_never_breaks_write(tmp_output, monkeypatch):
    monkeypatch.setenv("HERMES_CRON_OUTPUT_KEEP", "1")

    def boom(job_output_dir, keep):
        raise AssertionError("must be swallowed upstream")

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
