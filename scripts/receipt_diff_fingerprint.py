#!/usr/bin/env python3
"""Print the PR-contribution diff fingerprint used for receipt mechanical freshness.

The fingerprint is `sha256:<hex>` over the byte-exact output of a pinned
three-dot git diff (the PR's contributed changes relative to the merge base):

    git -c diff.renames=false -c core.quotePath=false \
        diff --no-color --no-ext-diff --unified=3 <base-sha>...<head-sha>

Reviewers stamp this value into a receipt's `diff_fingerprint` field at review
time; the PR risk classifier workflow computes the same value on the current
head and passes it to review_receipt_validator.py as
--current-diff-fingerprint. A receipt whose head/base SHAs went stale purely
because the head moved (e.g. GitHub update-branch base sync) stays valid IFF
the two fingerprints match exactly — any content change to the PR's own diff
(including context drift from base commits touching the same files) changes
the fingerprint and forces a fresh review.

Exit non-zero on any git failure so callers fall back to strict (exact
head-SHA) freshness. This tool makes no protected claims: it is an input to
ADVISORY_PACKET validation, never merge evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys

FULL_SHA_LENGTH = 40


def compute_diff_fingerprint(repo: str, base_sha: str, head_sha: str) -> str:
    for label, value in (("base-sha", base_sha), ("head-sha", head_sha)):
        if len(value) != FULL_SHA_LENGTH or any(c not in "0123456789abcdef" for c in value.lower()):
            raise ValueError(f"--{label} must be a full 40-hex commit SHA, got {value!r}")
    proc = subprocess.run(
        [
            "git",
            "-C",
            repo,
            "-c",
            "diff.renames=false",
            "-c",
            "core.quotePath=false",
            "diff",
            "--no-color",
            "--no-ext-diff",
            "--unified=3",
            f"{base_sha}...{head_sha}",
        ],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return "sha256:" + hashlib.sha256(proc.stdout).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=".", help="path to the git checkout containing both SHAs")
    parser.add_argument("--base-sha", required=True, help="full 40-hex PR base SHA")
    parser.add_argument("--head-sha", required=True, help="full 40-hex PR head SHA")
    args = parser.parse_args(argv)
    try:
        print(compute_diff_fingerprint(args.repo, args.base_sha, args.head_sha))
    except (ValueError, subprocess.CalledProcessError) as exc:
        detail = exc.stderr.decode(errors="replace").strip() if isinstance(exc, subprocess.CalledProcessError) else str(exc)
        print(f"receipt_diff_fingerprint: {detail}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
