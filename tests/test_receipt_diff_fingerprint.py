from __future__ import annotations

import hashlib
import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import TypedDict

import pytest  # ty: ignore[unresolved-import]

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "receipt_diff_fingerprint.py"
spec = importlib.util.spec_from_file_location("receipt_diff_fingerprint", MODULE_PATH)
assert spec is not None
receipt_diff_fingerprint = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = receipt_diff_fingerprint
spec.loader.exec_module(receipt_diff_fingerprint)

fp = receipt_diff_fingerprint.compute_content_fingerprint


class PrRepo(TypedDict):
    repo: Path
    base_sha: str
    head_sha: str


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
def pr_repo(tmp_path: Path) -> PrRepo:
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


def test_fingerprint_is_deterministic_and_well_formed(pr_repo: PrRepo) -> None:
    repo, base, head = str(pr_repo["repo"]), pr_repo["base_sha"], pr_repo["head_sha"]
    first = fp(repo, base, head)
    second = fp(repo, base, head)
    assert first == second
    assert first.startswith("sha256:") and len(first) == len("sha256:") + 64


def test_blob_digest_hashes_complete_file_bytes(pr_repo: PrRepo) -> None:
    object_id = _git(pr_repo["repo"], "rev-parse", f"{pr_repo['head_sha']}:b.txt")
    expected = "sha256:" + hashlib.sha256(b"feature change\n").hexdigest()

    assert receipt_diff_fingerprint._git_blob_sha256(str(pr_repo["repo"]), object_id) == expected


def test_fingerprint_uses_content_digest_not_git_object_id(monkeypatch: pytest.MonkeyPatch) -> None:
    base_sha = "a" * 40
    head_sha = "b" * 40
    object_id = b"1" * 40
    digest = ["sha256:" + ("2" * 64)]

    def fake_git_bytes(_repo: str, *args: str | bytes) -> bytes:
        if "diff" in args:
            return b"A\0artifact.bin\0"
        if args and args[0] == "ls-tree":
            return b"100644 blob " + object_id + b"\tartifact.bin\0"
        raise AssertionError(f"unexpected git invocation: {args!r}")

    monkeypatch.setattr(receipt_diff_fingerprint, "_git_bytes", fake_git_bytes)
    monkeypatch.setattr(
        receipt_diff_fingerprint,
        "_git_blob_sha256",
        lambda _repo, _object_id: digest[0],
    )

    first = fp("repo", base_sha, head_sha)
    digest[0] = "sha256:" + ("3" * 64)
    second = fp("repo", base_sha, head_sha)

    assert first != second


def test_non_blob_changed_object_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_git_bytes(_repo: str, *args: str | bytes) -> bytes:
        if "diff" in args:
            return b"M\0vendor/submodule\0"
        if args and args[0] == "ls-tree":
            return b"160000 commit " + (b"1" * 40) + b"\tvendor/submodule\0"
        raise AssertionError(f"unexpected git invocation: {args!r}")

    monkeypatch.setattr(receipt_diff_fingerprint, "_git_bytes", fake_git_bytes)

    with pytest.raises(ValueError, match="unsupported changed object type"):
        fp("repo", "a" * 40, "b" * 40)


def test_fingerprint_survives_base_advance_that_does_not_touch_pr_files(pr_repo: PrRepo) -> None:
    repo_path = pr_repo["repo"]
    repo, base, head = str(repo_path), pr_repo["base_sha"], pr_repo["head_sha"]
    before = fp(repo, base, head)
    # Base advances with an unrelated file; PR head merges it in. b.txt unchanged.
    _git(repo_path, "checkout", "main")
    (repo_path / "c.txt").write_text("unrelated mainline work\n")
    _git(repo_path, "add", "c.txt")
    _git(repo_path, "commit", "-m", "mainline")
    new_base = _sha(repo_path)
    _git(repo_path, "checkout", "feature")
    _git(repo_path, "merge", "--no-edit", "main")
    after = fp(repo, new_base, _sha(repo_path))
    assert after == before


def test_fingerprint_changes_when_pr_content_changes(pr_repo: PrRepo) -> None:
    repo_path = pr_repo["repo"]
    repo, base = str(repo_path), pr_repo["base_sha"]
    before = fp(repo, base, pr_repo["head_sha"])
    (repo_path / "b.txt").write_text("feature change v2\n")
    _git(repo_path, "add", "b.txt")
    _git(repo_path, "commit", "-m", "feature v2")
    after = fp(repo, base, _sha(repo_path))
    assert after != before


def test_fingerprint_changes_on_base_edit_to_other_section_of_a_pr_touched_file(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "--initial-branch=main")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "test")
    top = "\n".join(f"top {i}" for i in range(20))
    bottom = "\n".join(f"bottom {i}" for i in range(20))
    (repo / "gov.txt").write_text(top + "\n\n" + bottom + "\n")
    _git(repo, "add", "gov.txt")
    _git(repo, "commit", "-m", "base gov")
    base = _sha(repo)
    _git(repo, "checkout", "-b", "feature")
    (repo / "gov.txt").write_text(top + "\n\n" + bottom.replace("bottom 19", "bottom 19 PR-EDIT") + "\n")
    _git(repo, "add", "gov.txt")
    _git(repo, "commit", "-m", "pr edits bottom")
    reviewed = fp(str(repo), base, _sha(repo))
    _git(repo, "checkout", "main")
    (repo / "gov.txt").write_text(top.replace("top 0", "top 0 BASE-EDIT") + "\n\n" + bottom + "\n")
    _git(repo, "add", "gov.txt")
    _git(repo, "commit", "-m", "base edits top")
    new_base = _sha(repo)
    _git(repo, "checkout", "feature")
    _git(repo, "merge", "--no-edit", "main")
    after = fp(str(repo), new_base, _sha(repo))
    assert after != reviewed


def test_fingerprint_changes_on_binary_content_change(tmp_path: Path) -> None:
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
    before = fp(str(repo), base, _sha(repo))
    data = bytearray(bytes(range(256)) * 4)
    data[500] ^= 0xFF
    (repo / "blob.bin").write_bytes(bytes(data))
    _git(repo, "add", "blob.bin")
    _git(repo, "commit", "-m", "mutate binary")
    after = fp(str(repo), base, _sha(repo))
    assert after != before


def test_fingerprint_captures_file_deletion(pr_repo: PrRepo) -> None:
    repo_path = pr_repo["repo"]
    before = fp(str(repo_path), pr_repo["base_sha"], pr_repo["head_sha"])
    _git(repo_path, "rm", "b.txt")
    _git(repo_path, "commit", "-m", "remove b")
    after = fp(str(repo_path), pr_repo["base_sha"], _sha(repo_path))
    assert after != before


def test_canonical_serialization_resists_path_delimiter_forgery(tmp_path: Path) -> None:
    def build(repo: Path, files: dict[str, str]) -> tuple[str, str]:
        repo.mkdir()
        _git(repo, "init", "--initial-branch=main")
        _git(repo, "config", "user.email", "t@e.invalid")
        _git(repo, "config", "user.name", "t")
        (repo / "seed.txt").write_text("seed\n")
        _git(repo, "add", "seed.txt")
        _git(repo, "commit", "-m", "base")
        base = _sha(repo)
        _git(repo, "checkout", "-b", "feature")
        for name, content in files.items():
            path = repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
            _git(repo, "add", name)
        _git(repo, "commit", "-m", "feature")
        return base, _sha(repo)

    repo_a = tmp_path / "a"
    tricky_name = "x\nA\t100644\tdeadbeef\ty"
    base_a, head_a = build(repo_a, {tricky_name: "alpha\n"})
    fingerprint_a = fp(str(repo_a), base_a, head_a)

    repo_b = tmp_path / "b"
    base_b, head_b = build(repo_b, {"x": "alpha\n", "y": "beta\n"})
    fingerprint_b = fp(str(repo_b), base_b, head_b)

    assert fingerprint_a != fingerprint_b


def test_rejects_short_or_invalid_shas(pr_repo: PrRepo) -> None:
    repo = str(pr_repo["repo"])
    with pytest.raises(ValueError):
        fp(repo, "abc123", pr_repo["head_sha"])
    with pytest.raises(ValueError):
        fp(repo, pr_repo["base_sha"], "Z" * 40)


def test_cli_exits_nonzero_on_git_failure(tmp_path: Path, pr_repo: PrRepo) -> None:
    proc = subprocess.run(
        [
            sys.executable,
            str(MODULE_PATH),
            "--repo",
            str(tmp_path / "not-a-repo"),
            "--base-sha",
            pr_repo["base_sha"],
            "--head-sha",
            pr_repo["head_sha"],
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert proc.returncode == 1
    assert proc.stdout == ""
