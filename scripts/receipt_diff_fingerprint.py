#!/usr/bin/env python3
"""Print the PR-content fingerprint used for receipt mechanical freshness.

The fingerprint is ``sha256:<hex>`` over every path the PR contributes at the
current head. Each live path is bound by status, mode, object type, path bytes,
and a SHA-256 digest of the complete Git blob bytes. It is not a hash of diff
text and it does not wrap Git's SHA-1 object identifier as if that upgraded the
collision boundary.

    paths = git diff --name-status --no-renames <base-sha>...<head-sha> -z
    live record = status\0mode\0blob\0sha256(content)\0path\0
    deleted record = D\0000000\0absent\0sha256:000...000\0path\0
    fingerprint = sha256(sorted canonical records)

Why complete blob bytes, not diff text or Git SHA-1 IDs:
  1. Binary safety — binary/``-diff`` paths can reduce diff text to an
     abbreviated object index and ``Binary files differ``. Hashing the complete
     blob bytes binds all content.
  2. Same-file base drift — a three-dot diff can omit a base-only edit to a
     different section of a PR-touched file after branch sync. Binding the
     current head blob captures the merged file.
  3. Collision boundary — SHA-256 over SHA-1 object IDs is still limited by
     SHA-1 collision resistance. Hashing the bytes directly preserves the
     stated SHA-256 review-reuse boundary.

A base change to a file the PR does not touch leaves the fingerprint unchanged;
that change is outside this PR's contribution and is governed by its own merge.
Non-blob entries such as gitlinks fail closed so the workflow falls back to
strict exact-head receipt freshness rather than weakening the fingerprint.

Exit non-zero on malformed git output, unsupported object types, or any git
failure. This value is an input to advisory review-receipt validation; it is not
protected approval or standalone merge evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys

FULL_SHA_LENGTH = 40
_ABSENT_CONTENT_DIGEST = "sha256:" + ("0" * 64)


def _git_bytes(repo: str, *args: str | bytes) -> bytes:
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


def _validate_object_id(value: bytes) -> str:
    try:
        decoded = value.decode("ascii")
    except UnicodeDecodeError as exc:  # pragma: no cover - git emits ASCII IDs
        raise ValueError("git emitted a non-ASCII object ID") from exc
    if len(decoded) != FULL_SHA_LENGTH or any(c not in "0123456789abcdef" for c in decoded.lower()):
        raise ValueError(f"git emitted a malformed object ID: {decoded!r}")
    return decoded


def _git_blob_sha256(repo: str, object_id: str) -> str:
    """Return SHA-256 of the complete blob bytes addressed by ``object_id``."""

    content = _git_bytes(repo, "cat-file", "blob", object_id)
    return "sha256:" + hashlib.sha256(content).hexdigest()


def compute_content_fingerprint(repo: str, base_sha: str, head_sha: str) -> str:
    """Return a canonical SHA-256 fingerprint for the PR-touched head content.

    Git paths can contain any byte except NUL, so all record fields are
    NUL-delimited bytes. No path decoding, stripping, or line-oriented parsing
    is used.
    """

    _validate_sha("base-sha", base_sha)
    _validate_sha("head-sha", head_sha)

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
        if status[:1] in (b"R", b"C"):
            raise ValueError("git emitted a rename/copy status despite --no-renames")
        if i + 1 >= len(tokens) or not tokens[i + 1]:
            raise ValueError("git emitted malformed NUL-delimited name-status output")
        changed.append((status, tokens[i + 1]))
        i += 2

    records: list[bytes] = []
    for status, path in sorted(changed, key=lambda item: item[1]):
        if status[:1] == b"D":
            records.append(
                b"D\x00000000\x00absent\x00"
                + _ABSENT_CONTENT_DIGEST.encode("ascii")
                + b"\x00"
                + path
                + b"\x00"
            )
            continue

        entry = _git_bytes(repo, "ls-tree", "--full-tree", "-z", head_sha, "--", path)
        first = entry.split(b"\0", 1)[0]
        if not first or b"\t" not in first:
            raise ValueError("changed path is missing from the current head tree")
        meta = first.split(b"\t", 1)[0].split()
        if len(meta) != 3:
            raise ValueError("git emitted malformed ls-tree metadata")
        mode, object_type, object_id_bytes = meta
        if object_type != b"blob":
            raise ValueError(
                f"unsupported changed object type {object_type.decode(errors='replace')!r}; "
                "mechanical freshness requires complete blob bytes"
            )
        object_id = _validate_object_id(object_id_bytes)
        content_digest = _git_blob_sha256(repo, object_id).encode("ascii")
        records.append(
            status
            + b"\x00"
            + mode
            + b"\x00blob\x00"
            + content_digest
            + b"\x00"
            + path
            + b"\x00"
        )

    return "sha256:" + hashlib.sha256(b"".join(records)).hexdigest()


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
