"""Bootstrap-suite fixture compatibility for the V2 public test wrapper.

The V1 runner tests intentionally create minimal non-Git repositories. The V2
public wrapper delegates to the byte-preserved ``run_tests_v1.sh`` in those
fixtures, so copy that declared dependency alongside the wrapper before each
legacy runner test executes. Real Git checkouts still require Stage-0 recovery.
"""

from __future__ import annotations

from pathlib import Path
import shutil

import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _include_preserved_v1_runner_in_legacy_fixture(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = request.module
    original = getattr(module, "_runner_fixture", None)
    if original is None or module.__name__.split(".")[-1] != "test_bootstrap_closure":
        return

    def patched(tmp_path: Path) -> Path:
        fixture_root = original(tmp_path)
        scripts = fixture_root / "scripts"
        shutil.copy2(ROOT / "scripts" / "run_tests_v1.sh", scripts / "run_tests_v1.sh")
        return fixture_root

    monkeypatch.setattr(module, "_runner_fixture", patched)
