"""Shared hooks for tests/ci."""

from __future__ import annotations

_RUNTIME_OS_ADAPTER_TESTS = "test_runtime_os_adapter.py"


def pytest_collection_finish(session):
    """Build the runtime-OS adapter's real-repo reference index once.

    Runs after collection and before any test, so the one-time whole-repo
    parse is not charged to a single test's 30 s pytest-timeout budget. It is
    skipped for ``--collect-only`` (e.g. scripts/run_tests_parallel.py's
    test-count pass) and when ``-k``/node selection deselects every adapter
    test.
    """
    if session.config.option.collectonly:
        return
    modules = {
        item.module
        for item in session.items
        if getattr(item, "module", None) is not None
        and item.path.name == _RUNTIME_OS_ADAPTER_TESTS
    }
    for module in modules:
        module._build_repository_reference_index()
