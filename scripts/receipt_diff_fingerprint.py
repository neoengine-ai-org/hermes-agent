#!/usr/bin/env python3
"""Print the PR-contribution content fingerprint used for receipt mechanical freshness.

The fingerprint is `sha256:<hex>` over the **full content** (git blob object IDs,
which are SHA-1 over the complete file bytes) of every path the PR contributes,
at the current head. It is NOT a hash of `git diff` text.

    paths   = git diff --name-status --no-renames <base-sha>...<head-sha>   # 3-dot
    per path at head: <status>\\t<mode>\\t<full-blob-sha>\\t<path>
    fingerprint = sha256(sorted lines)

Why content, not diff text (opposite-frontier review, 2026-07-11):
  1. Binary safety — a `git diff` of a binary/`-diff` path prints only an
     abbreviated blob index ("Binary files differ"), so hashing diff text binds
     unreviewed binary content to a ~7-hex prefix that can be ground to collide.
     Blob object IDs are full 40-hex over the complete file bytes, so a binary
     change cannot keep the same fingerprint without a full SHA-1 collision.
  2. Same-file base drift — a three-dot diff excludes base-only edits to other
     sections of a file the PR also touches (after update-branch those sections
     are identical in base and head, so they never appear in the diff). Binding
     to the file's head blob captures them: any change to a PR-touched file's
     merged content — from the PR or from a base merge into that file — changes
     its blob and forces a fresh review.

A change to a file the PR does NOT touch does not move the fingerprint: that is
outside this PR's reviewed contribution and is governed by its own review.

Reviewers stamp this value into a receipt's `diff_fingerprint` field at review
time; the PR risk classifier workflow computes the same value on the current
head and passes it to review_receipt_validator.py as --current-diff-fingerprint.
A receipt whose head/base SHAs went stale purely because the head moved (e.g. a
base sync) stays valid IFF the two fingerprints match exactly.

Exit non-zero on any git failure so callers fall back to strict (exact head-SHA)
freshness. Makes no protected claims: an input to ADVISORY_PACKET validation,
never merge evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys

FULL_SHA_LENGTH = 40
_ABSENT_BLOB = "0" * 40  # sentinel for a path deleted at head (status D)


def _git_bytes(repo: str, *args: str) -> bytes:
    proc = subprocess.run(
        ["git", "-C", repo, *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return proc.stdout


def _validate_sha(label: str, value: str) -> None:
    if len(value) != FULL_SHA_LENGTH or any(c not in "0123456789abcdef" for c in value.lower()):
        raise ValueError(f"--{label} must be a full 40-hex commit SHA, got {value!r}")


def compute_content_fingerprint(repo: str, base_sha: str, head_sha: str) -> str:
    """sha256 over the head blob content of every path the PR contributes.

    Deterministic and identical when recomputed on a later head that carries the
    same reviewed content, because it binds full blob object IDs (content), not
    commit SHAs or diff text.

    Canonical, byte-exact serialization: everything is done in bytes and every
    record field is NUL-terminated. Git paths can contain any byte EXCEPT NUL,
    so NUL is an unambiguous delimiter — no path can forge a field boundary, and
    trailing path bytes (space/tab/CR/LF) are never stripped. (Opposite-frontier
    review found that ad-hoc \\t-joined + .strip() text let two distinct file
    sets serialize identically.)
    """
    _validate_sha("base-sha", base_sha)
    _validate_sha("head-sha", head_sha)

    # -z gives NUL-delimited, un-quoted records in BYTES: STATUS\0PATH\0...
    # --no-renames so a rename is a delete + add (each bound independently).
    raw = _git_bytes(
        repo,
        "-c",
        "core.quotePath=false",
        "diff",
        "--no-renames",
        "--name-status",
        "-z",
        f"{base_sha}...{head_sha}",
    )
    tokens = raw.split(b"\0")
    changed: list[tuple[bytes, bytes]] = []
    i = 0
    while i < len(tokens):
        status = tokens[i]
        if not status:
            i += 1
            continue
        # copy/rename statuses (C##/R##) carry two paths; --no-renames avoids
        # them, but stay defensive and consume the extra path if present.
        if status[:1] in (b"R", b"C") and i + 2 < len(tokens):
            path = tokens[i + 2]
            i += 3
        else:
            path = tokens[i + 1] if i + 1 < len(tokens) else b""
            i += 2
        if path:
            changed.append((status, path))

    # Canonical record encoding: status\0 mode\0 blob\0 path\0  (all bytes).
    records: list[bytes] = []
    for status, path in sorted(changed, key=lambda item: item[1]):
        if status[:1] == b"D":
            records.append(b"D\x00000000\x00" + _ABSENT_BLOB.encode("ascii") + b"\x00" + path + b"\x00")
            continue
        # ls-tree -z record: "<mode> <type> <blob>\t<path>\0". The mode/type/blob
        # prefix before the TAB is fixed-format and space-separated; take it and
        # ignore the (already-known, exact) path bytes after the tab.
        entry = _git_bytes(repo, "ls-tree", "--full-tree", "-z", head_sha, "--", path)
        first = entry.split(b"\0", 1)[0]
        if not first or b"\t" not in first:
            # Path reported changed but absent at head — treat as deletion.
            records.append(b"D\x00000000\x00" + _ABSENT_BLOB.encode("ascii") + b"\x00" + path + b"\x00")
            continue
        meta = first.split(b"\t", 1)[0]
        mode, _obj_type, blob_sha = meta.split()
        records.append(status + b"\x00" + mode + b"\x00" + blob_sha + b"\x00" + path + b"\x00")

    payload = b"".join(records)
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=".", help="path to the git checkout containing both SHAs")
    parser.add_argument("--base-sha", required=True, help="full 40-hex PR base SHA")
    parser.add_argument("--head-sha", required=True, help="full 40-hex PR head SHA")
    args = parser.parse_args(argv)
    try:
        print(compute_content_fingerprint(args.repo, args.base_sha, args.head_sha))
    except (ValueError, subprocess.CalledProcessError) as exc:
        detail = exc.stderr.decode(errors="replace").strip() if isinstance(exc, subprocess.CalledProcessError) else str(exc)
        print(f"receipt_diff_fingerprint: {detail}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
