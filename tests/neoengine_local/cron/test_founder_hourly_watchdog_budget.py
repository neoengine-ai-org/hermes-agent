from __future__ import annotations

import ast
from pathlib import Path

WATCHDOG = Path(__file__).resolve().parents[3] / "neoengine_local" / "cron" / "founder_hourly_watchdog" / "neowealth_founder_hourly_watchdog.py"


def _literal_assignments(tree: ast.AST) -> dict[str, object]:
    values: dict[str, object] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            try:
                values[node.targets[0].id] = ast.literal_eval(node.value)
            except Exception:
                pass
    return values


def test_neowealth_hourly_watchdog_has_internal_budget_below_scheduler_timeout() -> None:
    tree = ast.parse(WATCHDOG.read_text())
    values = _literal_assignments(tree)

    assert values["SCHEDULER_TIMEOUT_SECONDS"] == 120
    assert values["RUN_BUDGET_SECONDS"] < values["SCHEDULER_TIMEOUT_SECONDS"]
    assert values["RUN_BUDGET_SECONDS"] <= values["SCHEDULER_TIMEOUT_SECONDS"] - 10


def test_neowealth_hourly_watchdog_uses_budgeted_subprocesses_for_repairs() -> None:
    tree = ast.parse(WATCHDOG.read_text())
    refresh = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "refresh_runtime_proof"
    )
    calls = [node for node in ast.walk(refresh) if isinstance(node, ast.Call)]

    repair_calls = [
        node
        for node in calls
        if isinstance(node.func, ast.Name) and node.func.id in {"run", "run_budgeted"}
    ]
    assert repair_calls, "refresh_runtime_proof must invoke bounded subprocess helpers"
    assert all(
        isinstance(node.func, ast.Name) and node.func.id == "run_budgeted"
        for node in repair_calls
    ), "repair subprocesses must use remaining-budget-aware execution"

    literal_timeouts = [
        kw.value.value
        for node in repair_calls
        for kw in node.keywords
        if kw.arg == "timeout" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, int)
    ]
    assert literal_timeouts
    assert max(literal_timeouts) <= 55


def test_neowealth_hourly_watchdog_skips_repair_when_proof_is_already_fresh() -> None:
    source = WATCHDOG.read_text()
    assert "runtime_proof_is_healthy(load_json(PROOF))" in source
    assert "proof_gate=skipped_already_fresh" in source
