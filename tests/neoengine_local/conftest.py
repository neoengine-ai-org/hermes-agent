"""Narrow test-fixture compatibility for Hermes home-retention tests."""

from __future__ import annotations

import os
from pathlib import Path

import pytest


_TARGET_TEST = "test_unpushed_commit_counts_as_dirty_and_content_archived"


@pytest.fixture(autouse=True)
def _stabilize_ephemeral_git_metadata_utime(request, monkeypatch):
    """Ignore only ENOENT races for ephemeral `.git` entries in one fixture.

    The target retention test deliberately snapshots every path under a freshly
    created Git repository and then backdates the snapshot. Modern Git may
    remove transient internal metadata between ``Path.rglob`` and ``os.utime``.
    That race is unrelated to the retention behavior being asserted.

    Keep every non-`.git` path strict, and scope the shim to the one affected
    test so production retention behavior and the rest of the suite remain
    unchanged.
    """

    if request.node.name != _TARGET_TEST:
        yield
        return

    real_utime = os.utime

    def stable_utime(path, *args, **kwargs):
        try:
            return real_utime(path, *args, **kwargs)
        except FileNotFoundError:
            if ".git" not in Path(path).parts:
                raise
            return None

    monkeypatch.setattr(os, "utime", stable_utime)
    yield
