"""Shared hooks for tests/ci."""

from __future__ import annotations

from types import ModuleType

import pytest

_RUNTIME_OS_ADAPTER_TESTS = "test_runtime_os_adapter.py"
_INDEXED_MODULES: set[ModuleType] = set()


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_protocol(item, nextitem):
    """Build the runtime-OS adapter's real-repo reference index lazily.

    The one-time whole-repo parse runs just before the first adapter test
    this process actually runs, so an xdist worker that is never scheduled
    an adapter test (and ``--collect-only`` or a ``-k`` selection without
    one) never builds it. ``tryfirst`` makes this the outermost runtest
    wrapper: the build finishes before pytest-timeout's wrapper starts the
    30 s per-test timer, so it is not charged to that test's budget.
    """
    module = getattr(item, "module", None)
    if (
        module is not None
        and item.path.name == _RUNTIME_OS_ADAPTER_TESTS
        and module not in _INDEXED_MODULES
    ):
        _INDEXED_MODULES.add(module)
        module._build_repository_reference_index()
    return (yield)
