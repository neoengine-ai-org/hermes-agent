from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "receipt_diff_fingerprint.py"
spec = importlib.util.spec_from_file_location("receipt_diff_fingerprint", MODULE_PATH)
assert spec is not None
receipt_diff_fingerprint = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = receipt_diff_fingerprint
spec.loader.exec_module(receipt_diff_fingerprint)


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return proc.stdout.strip()


@pytest.fixture()
def pr_repo(tmp_path: Path) -> dict[str, object]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "--initial-branch=main")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "test")
    (repo / "a.txt").write_text("base\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "base")
    base_sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-b", "feature")
    (repo / "b.txt").write_text("feature change\n")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-m", "feature")
    head_sha = _git(repo, "rev-parse", "HEAD")
    return {"repo": repo, "base_sha": base_sha, "head_sha": head_sha}


def test_fingerprint_is_deterministic_and_well_formed(pr_repo: dict[str, object]) -> None:
    repo, base_sha, head_sha = str(pr_repo["repo"]), str(pr_repo["base_sha"]), str(pr_repo["head_sha"])
    fp1 = receipt_diff_fingerprint.compute_diff_fingerprint(repo, base_sha, head_sha)
    fp2 = receipt_diff_fingerprint.compute_diff_fingerprint(repo, base_sha, head_sha)
    assert fp1 == fp2
    assert receipt_diff_fingerprint.__name__  # module loaded
    assert fp1.startswith("sha256:") and len(fp1) == len("sha256:") + 64


def test_fingerprint_survives_base_advance_without_pr_content_change(pr_repo: dict[str, object]) -> None:
    repo_path = pr_repo["repo"]
    repo, base_sha, head_sha = str(repo_path), str(pr_repo["base_sha"]), str(pr_repo["head_sha"])
    fp_before = receipt_diff_fingerprint.compute_diff_fingerprint(repo, base_sha, head_sha)

    # Base advances with an unrelated file; PR head merges the new base in
    # (what GitHub update-branch does). The PR's contributed diff is unchanged.
    _git(repo_path, "checkout", "main")  # type: ignore[arg-type]
    (repo_path / "c.txt").write_text("unrelated mainline work\n")  # type: ignore[operator]
    _git(repo_path, "add", "c.txt")  # type: ignore[arg-type]
    _git(repo_path, "commit", "-m", "mainline")  # type: ignore[arg-type]
    new_base_sha = _git(repo_path, "rev-parse", "HEAD")  # type: ignore[arg-type]
    _git(repo_path, "checkout", "feature")  # type: ignore[arg-type]
    _git(repo_path, "merge", "--no-edit", "main")  # type: ignore[arg-type]
    new_head_sha = _git(repo_path, "rev-parse", "HEAD")  # type: ignore[arg-type]

    fp_after = receipt_diff_fingerprint.compute_diff_fingerprint(repo, new_base_sha, new_head_sha)
    assert fp_after == fp_before


def test_fingerprint_changes_when_pr_content_changes(pr_repo: dict[str, object]) -> None:
    repo_path = pr_repo["repo"]
    repo, base_sha = str(repo_path), str(pr_repo["base_sha"])
    fp_before = receipt_diff_fingerprint.compute_diff_fingerprint(repo, base_sha, str(pr_repo["head_sha"]))
    (repo_path / "b.txt").write_text("feature change v2\n")  # type: ignore[operator]
    _git(repo_path, "add", "b.txt")  # type: ignore[arg-type]
    _git(repo_path, "commit", "-m", "feature v2")  # type: ignore[arg-type]
    new_head_sha = _git(repo_path, "rev-parse", "HEAD")  # type: ignore[arg-type]
    fp_after = receipt_diff_fingerprint.compute_diff_fingerprint(repo, base_sha, new_head_sha)
    assert fp_after != fp_before


def test_rejects_short_or_invalid_shas(pr_repo: dict[str, object]) -> None:
    repo = str(pr_repo["repo"])
    with pytest.raises(ValueError):
        receipt_diff_fingerprint.compute_diff_fingerprint(repo, "abc123", str(pr_repo["head_sha"]))
    with pytest.raises(ValueError):
        receipt_diff_fingerprint.compute_diff_fingerprint(repo, str(pr_repo["base_sha"]), "Z" * 40)


def test_cli_exits_nonzero_on_git_failure(tmp_path: Path, pr_repo: dict[str, object]) -> None:
    proc = subprocess.run(
        [
            sys.executable,
            str(MODULE_PATH),
            "--repo",
            str(tmp_path / "not-a-repo"),
            "--base-sha",
            str(pr_repo["base_sha"]),
            "--head-sha",
            str(pr_repo["head_sha"]),
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert proc.returncode == 1
    assert proc.stdout == ""
