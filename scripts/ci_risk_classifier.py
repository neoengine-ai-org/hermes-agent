#!/usr/bin/env python3
"""Hardened entry point for the Hermes PR risk classifier.

The historical classifier implementation is preserved in
``ci_risk_classifier_core.py``. This entry point adds one conservative fallback:
any non-test executable path that the core classifier would otherwise label only
as ``docs_only`` is treated as ``runtime_backend``. That prevents new packaged
code outside the currently enumerated directory prefixes from bypassing runtime
classification and review requirements.
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


def infer_surfaces(files: Iterable[str], body: str) -> set[str]:
    """Infer surfaces with a fail-closed fallback for unknown executables."""

    file_list = list(files)
    surfaces = set(_original_infer_surfaces(file_list, body))
    generic_executable_runtime = False

    for raw_file in file_list:
        normalized = raw_file.replace("\\", "/")
        path = Path(normalized)
        if path.suffix.lower() not in _core.EXECUTABLE_SUFFIXES:
            continue
        if _core.is_test_like_path(normalized):
            continue

        # Ask the established classifier how it understands this path without
        # allowing PR-body prose to supply a surface. A lone docs_only result for
        # executable bytes is the unsafe fallback this guard closes.
        path_surfaces = set(_original_infer_surfaces([normalized], ""))
        if path_surfaces == {"docs_only"}:
            generic_executable_runtime = True
            break

    if generic_executable_runtime:
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
