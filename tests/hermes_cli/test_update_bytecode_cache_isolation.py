"""``cmd_update`` tests must not delete the shared checkout's bytecode caches."""

from __future__ import annotations

import os
import shutil

from hermes_cli import main as cli_main


def test_clear_bytecode_cache_skips_live_checkout(monkeypatch) -> None:
    def _forbidden_rmtree(path, *args, **kwargs):
        raise AssertionError(f"rmtree reached the shared checkout: {path}")

    def _forbidden_walk(top, *args, **kwargs):
        raise AssertionError(f"walked the shared checkout: {top}")

    monkeypatch.setattr(shutil, "rmtree", _forbidden_rmtree)
    monkeypatch.setattr(os, "walk", _forbidden_walk)
    assert cli_main._clear_bytecode_cache(cli_main.PROJECT_ROOT) == 0


def test_clear_bytecode_cache_still_clears_isolated_roots(tmp_path) -> None:
    (tmp_path / "pkg" / "__pycache__").mkdir(parents=True)
    (tmp_path / "pkg" / "__pycache__" / "mod.cpython-311.pyc").write_bytes(b"")
    (tmp_path / ".venv" / "lib" / "__pycache__").mkdir(parents=True)
    assert cli_main._clear_bytecode_cache(tmp_path) == 1
    assert not (tmp_path / "pkg" / "__pycache__").exists()
    assert (tmp_path / ".venv" / "lib" / "__pycache__").exists()


def test_guard_covers_checkout_subtrees_and_ignores_redirected_project_root(monkeypatch, tmp_path) -> None:
    def _forbidden_rmtree(path, *args, **kwargs):
        raise AssertionError(f"rmtree reached the shared checkout: {path}")

    monkeypatch.setattr(shutil, "rmtree", _forbidden_rmtree)
    monkeypatch.setattr(cli_main, "PROJECT_ROOT", tmp_path)
    checkout = cli_main.Path(__file__).resolve().parents[2]
    assert cli_main._clear_bytecode_cache(checkout) == 0
    assert cli_main._clear_bytecode_cache(checkout / "tests") == 0


def test_guard_still_clears_a_redirected_temporary_project_root(monkeypatch, tmp_path) -> None:
    (tmp_path / "pkg" / "__pycache__").mkdir(parents=True)
    monkeypatch.setattr(cli_main, "PROJECT_ROOT", tmp_path)
    assert cli_main._clear_bytecode_cache(cli_main.PROJECT_ROOT) == 1
    assert not (tmp_path / "pkg" / "__pycache__").exists()


def test_guard_never_clears_an_ancestor_of_the_checkout(monkeypatch) -> None:
    def _forbidden_rmtree(path, *args, **kwargs):
        raise AssertionError(f"rmtree reached the shared checkout: {path}")

    monkeypatch.setattr(shutil, "rmtree", _forbidden_rmtree)
    checkout = cli_main.Path(__file__).resolve().parents[2]
    assert cli_main._clear_bytecode_cache(checkout.parent) == 0
