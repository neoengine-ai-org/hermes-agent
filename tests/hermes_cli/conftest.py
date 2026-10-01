"""Fixtures shared across hermes_cli kanban tests."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def all_assignees_spawnable(monkeypatch):
    """Pretend every assignee maps to a real Hermes profile.

    Most dispatcher tests use synthetic assignees ("alice", "bob") that
    don't correspond to actual profile directories on disk. Without this
    patch, the dispatcher's profile-exists guard (PR #20105) routes
    those tasks into ``skipped_nonspawnable`` instead of spawning, which
    would break tests that assert spawn behavior.
    """
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)


@pytest.fixture(autouse=True)
def _suppress_concurrent_hermes_gate(request, monkeypatch):
    """Default ``_detect_concurrent_hermes_instances`` to ``[]`` for every test.

    The Windows update path now refuses to proceed when another
    ``hermes.exe`` is detected (issue #26670). On a developer's Windows
    machine running the test suite via ``hermes`` itself, this would
    flag the running agent as a concurrent instance and abort every
    ``cmd_update`` test. Tests that want to exercise the gate explicitly
    re-patch ``_detect_concurrent_hermes_instances`` with their own
    return value — autouse here gives a clean default without touching
    the rest of the suite.

    Tests that need to call the REAL function (e.g. unit tests for the
    helper itself) opt out with ``@pytest.mark.real_concurrent_gate``.
    """
    if request.node.get_closest_marker("real_concurrent_gate"):
        return
    try:
        from hermes_cli import main as _cli_main
    except Exception:
        return
    monkeypatch.setattr(
        _cli_main, "_detect_concurrent_hermes_instances", lambda *_a, **_k: []
    )


@pytest.fixture(autouse=True)
def _protect_checkout_bytecode_cache(monkeypatch):
    """Keep ``cmd_update`` tests from wiping the shared checkout's ``__pycache__``.

    ``cmd_update`` calls ``_clear_bytecode_cache(PROJECT_ROOT)``, which
    ``rmtree``s every ``__pycache__`` under the real repository.
    ``scripts/run_tests_parallel.py`` runs other test files concurrently
    against that same checkout, so the deletion races their tree walks
    (e.g. ``scripts/ci/runtime_os_adapter.py`` discovery hit
    ``FileNotFoundError: .../tests/__pycache__``). Calls aimed at the live
    checkout, or anywhere inside it, become a no-op; any other root
    (``tmp_path``) still runs the real implementation.
    """
    try:
        from hermes_cli import main as _cli_main
    except Exception:
        return
    real_clear = _cli_main._clear_bytecode_cache
    # Derive the checkout independently of the PROJECT_ROOT the guarded code
    # uses (at call time, from this file's location), so a wrong or patched
    # PROJECT_ROOT cannot steer the real rmtree back at the shared tree.
    own_checkout = Path(__file__).resolve().parents[2]

    def _guarded_clear(root):
        resolved = Path(root).resolve()
        checkouts = {own_checkout, Path(_cli_main.PROJECT_ROOT).resolve()}
        if any(resolved == checkout or checkout in resolved.parents for checkout in checkouts):
            return 0
        return real_clear(root)

    monkeypatch.setattr(_cli_main, "_clear_bytecode_cache", _guarded_clear)
