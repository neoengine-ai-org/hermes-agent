#!/usr/bin/env python3
"""Hardened entry point for the Hermes PR risk classifier.

The historical classifier implementation is preserved in
``ci_risk_classifier_core.py``. This entry point adds one conservative fallback:
package-like executable paths that the core classifier would otherwise label only
as ``docs_only`` are treated as ``runtime_backend``. Repository tooling paths keep
their established CI/governance classification.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Iterable

_CORE_PATH = Path(__file__).with_name("ci_risk_classifier_core.py")
_CORE_MODULE_NAME = "_hermes_ci_risk_classifier_core"
_SPEC = importlib.util.spec_from_file_location(_CORE_MODULE_NAME, _CORE_PATH)
if _SPEC is None or _SPEC.loader is None:  # pragma: no cover - import bootstrap guard
    raise RuntimeError(f"unable to load classifier core from {_CORE_PATH}")

_core = importlib.util.module_from_spec(_SPEC)
sys.modules[_CORE_MODULE_NAME] = _core
_SPEC.loader.exec_module(_core)

# Any future change to the preserved core is itself a classifier change and must
# remain inside the trusted self-change path set.
_core.SELF_CHANGE_PATHS.add("scripts/ci_risk_classifier_core.py")
_original_infer_surfaces = _core.infer_surfaces

# These paths contain repository tooling, tests, workflow definitions, or
# documentation rather than distributable/runtime packages. Their established
# classifier semantics already come from companion workflow/classifier rules;
# treating every unknown executable below them as runtime would over-escalate
# ordinary CI repairs (for example scripts/ci/*.py).
_NON_RUNTIME_TOOLING_PREFIXES = (
    ".github/",
    "docs/",
    "examples/",
    "nix/",
    "packaging/",
    "scripts/",
    "tests/",
)


def _is_package_like_unknown_executable(path: str) -> bool:
    normalized = path.replace("\\", "/").lstrip("./")
    if normalized.startswith(_NON_RUNTIME_TOOLING_PREFIXES):
        return False
    candidate = Path(normalized)
    if candidate.suffix.lower() not in _core.EXECUTABLE_SUFFIXES:
        return False
    if _core.is_test_like_path(normalized):
        return False
    return set(_original_infer_surfaces([normalized], "")) == {"docs_only"}


def infer_surfaces(files: Iterable[str], body: str) -> set[str]:
    """Infer surfaces with a fail-closed fallback for unknown packages."""

    file_list = list(files)
    surfaces = set(_original_infer_surfaces(file_list, body))

    if any(_is_package_like_unknown_executable(path) for path in file_list):
        surfaces.discard("docs_only")
        surfaces.add("runtime_backend")

    return surfaces


_core.infer_surfaces = infer_surfaces

# Preserve the public module API used by the workflow and existing test suite.
for _name in dir(_core):
    if not _name.startswith("__"):
        globals()[_name] = getattr(_core, _name)
globals()["infer_surfaces"] = infer_surfaces
main = _core.main


if __name__ == "__main__":
    raise SystemExit(main())
