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

fp = receipt_diff_fingerprint.compute_content_fingerprint


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return proc.stdout.strip()


def _sha(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture()
def pr_repo(tmp_path: Path) -> dict[str, object]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "--initial-branch=main")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "test")
    (repo / "a.txt").write_text("base line 1\nbase line 2\nbase line 3\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "base")
    base_sha = _sha(repo)
    _git(repo, "checkout", "-b", "feature")
    (repo / "b.txt").write_text("feature change\n")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-m", "feature")
    return {"repo": repo, "base_sha": base_sha, "head_sha": _sha(repo)}


def test_fingerprint_is_deterministic_and_well_formed(pr_repo: dict[str, object]) -> None:
    repo, base, head = str(pr_repo["repo"]), str(pr_repo["base_sha"]), str(pr_repo["head_sha"])
    f1 = fp(repo, base, head)
    f2 = fp(repo, base, head)
    assert f1 == f2
    assert f1.startswith("sha256:") and len(f1) == len("sha256:") + 64


def test_fingerprint_survives_base_advance_that_does_not_touch_pr_files(pr_repo: dict[str, object]) -> None:
    repo_path = pr_repo["repo"]
    repo, base, head = str(repo_path), str(pr_repo["base_sha"]), str(pr_repo["head_sha"])
    before = fp(repo, base, head)
    # Base advances with an UNRELATED file; PR head merges it in. b.txt unchanged.
    _git(repo_path, "checkout", "main")  # type: ignore[arg-type]
    (repo_path / "c.txt").write_text("unrelated mainline work\n")  # type: ignore[operator]
    _git(repo_path, "add", "c.txt")  # type: ignore[arg-type]
    _git(repo_path, "commit", "-m", "mainline")  # type: ignore[arg-type]
    new_base = _sha(repo_path)  # type: ignore[arg-type]
    _git(repo_path, "checkout", "feature")  # type: ignore[arg-type]
    _git(repo_path, "merge", "--no-edit", "main")  # type: ignore[arg-type]
    after = fp(repo, new_base, _sha(repo_path))  # type: ignore[arg-type]
    assert after == before


def test_fingerprint_changes_when_pr_content_changes(pr_repo: dict[str, object]) -> None:
    repo_path = pr_repo["repo"]
    repo, base = str(repo_path), str(pr_repo["base_sha"])
    before = fp(repo, base, str(pr_repo["head_sha"]))
    (repo_path / "b.txt").write_text("feature change v2\n")  # type: ignore[operator]
    _git(repo_path, "add", "b.txt")  # type: ignore[arg-type]
    _git(repo_path, "commit", "-m", "feature v2")  # type: ignore[arg-type]
    after = fp(repo, base, _sha(repo_path))  # type: ignore[arg-type]
    assert after != before


def test_fingerprint_changes_on_base_edit_to_other_section_of_a_pr_touched_file(tmp_path: Path) -> None:
    # Opposite-frontier MAJOR: a base-only edit to a DIFFERENT section of a file
    # the PR also touches must force re-review. Three-dot diff text would miss it;
    # binding to the head blob catches it.
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "--initial-branch=main")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "test")
    # A governance-ish file with a top section and a bottom section, far apart.
    top = "\n".join(f"top {i}" for i in range(20))
    bottom = "\n".join(f"bottom {i}" for i in range(20))
    (repo / "gov.txt").write_text(top + "\n\n" + bottom + "\n")
    _git(repo, "add", "gov.txt")
    _git(repo, "commit", "-m", "base gov")
    base = _sha(repo)
    # PR edits only the BOTTOM section.
    _git(repo, "checkout", "-b", "feature")
    (repo / "gov.txt").write_text(top + "\n\n" + bottom.replace("bottom 19", "bottom 19 PR-EDIT") + "\n")
    _git(repo, "add", "gov.txt")
    _git(repo, "commit", "-m", "pr edits bottom")
    reviewed = fp(repo, base, _sha(repo))
    # Base later edits the TOP section (far from the PR hunk) and the PR syncs it in.
    _git(repo, "checkout", "main")
    (repo / "gov.txt").write_text(top.replace("top 0", "top 0 BASE-EDIT") + "\n\n" + bottom + "\n")
    _git(repo, "add", "gov.txt")
    _git(repo, "commit", "-m", "base edits top")
    new_base = _sha(repo)
    _git(repo, "checkout", "feature")
    _git(repo, "merge", "--no-edit", "main")
    after = fp(repo, new_base, _sha(repo))
    assert after != reviewed  # merged file differs from reviewed content -> re-review


def test_fingerprint_changes_on_binary_content_change(tmp_path: Path) -> None:
    # Opposite-frontier BLOCKER: binary files must bind to full content, not an
    # abbreviated diff prefix.
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "--initial-branch=main")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "test")
    (repo / ".gitattributes").write_text("*.bin binary\n")
    _git(repo, "add", ".gitattributes")
    _git(repo, "commit", "-m", "attrs")
    base = _sha(repo)
    _git(repo, "checkout", "-b", "feature")
    (repo / "blob.bin").write_bytes(bytes(range(256)) * 4)
    _git(repo, "add", "blob.bin")
    _git(repo, "commit", "-m", "add binary")
    before = fp(repo, base, _sha(repo))
    # Change one byte deep in the binary; diff text would still say "Binary files differ".
    data = bytearray(bytes(range(256)) * 4)
    data[500] ^= 0xFF
    (repo / "blob.bin").write_bytes(bytes(data))
    _git(repo, "add", "blob.bin")
    _git(repo, "commit", "-m", "mutate binary")
    after = fp(repo, base, _sha(repo))
    assert after != before


def test_fingerprint_captures_file_deletion(pr_repo: dict[str, object]) -> None:
    repo_path = pr_repo["repo"]
    repo, base = str(repo_path), str(pr_repo["base_sha"])
    before = fp(repo, base, str(pr_repo["head_sha"]))
    _git(repo_path, "rm", "b.txt")  # type: ignore[arg-type]
    _git(repo_path, "commit", "-m", "remove b")  # type: ignore[arg-type]
    after = fp(repo, base, _sha(repo_path))  # type: ignore[arg-type]
    assert after != before


def test_rejects_short_or_invalid_shas(pr_repo: dict[str, object]) -> None:
    repo = str(pr_repo["repo"])
    with pytest.raises(ValueError):
        fp(repo, "abc123", str(pr_repo["head_sha"]))
    with pytest.raises(ValueError):
        fp(repo, str(pr_repo["base_sha"]), "Z" * 40)


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
