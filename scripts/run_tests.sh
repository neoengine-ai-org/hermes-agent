#!/usr/bin/env bash
# Repair-first wrapper around the byte-preserved V1 Hermes test runner.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV="$REPO_ROOT/.venv"
MODE_ARGS=("$@")
RECEIPT_MODE=0
for arg in "$@"; do
  if [[ "$arg" == "--receipt-mode" ]]; then
    RECEIPT_MODE=1
    VENV="${HERMES_RECEIPT_VENV:-$REPO_ROOT/.bootstrap-proof-venv}"
    break
  fi
done
case "$VENV" in
  "$REPO_ROOT"/*) VENV_REL="${VENV#"$REPO_ROOT"/}" ;;
  /*)
    echo "Hermes bootstrap venv escapes the repository: $VENV" >&2
    exit 2
    ;;
  *) VENV_REL="$VENV"; VENV="$REPO_ROOT/$VENV_REL" ;;
esac
case "$VENV_REL" in
  .venv|.bootstrap-proof-venv) ;;
  *)
    echo "Hermes bootstrap venv is not admitted: $VENV_REL" >&2
    exit 2
    ;;
esac
if [[ "$RECEIPT_MODE" -eq 1 ]]; then
  export HERMES_RECEIPT_VENV="$VENV"
fi

# Real repository executions are always repair-first. The V1 hostile test file
# also builds intentionally minimal non-Git runner fixtures to verify preserved
# shared-home and receipt-mode semantics; those fixtures exercise the
# byte-preserved V1 runner directly and are not admitted as V2 checkout proof.
if git -C "$REPO_ROOT" rev-parse --show-toplevel >/dev/null 2>&1; then
  if [[ ! -f "$SCRIPT_DIR/bootstrap_stage0_v2.py" ]]; then
    echo "Hermes Stage-0 bootstrap kernel is missing from a Git checkout" >&2
    exit 2
  fi
  python3 -S "$SCRIPT_DIR/bootstrap_stage0_v2.py" ensure \
    --repair --venv "$VENV_REL" --operation-id validate-bootstrap

  # Stage-0 reconstructs the minimal locked runtime first. Receipt mode then
  # restores the exact lock-backed dev extra so hostile pytest runs without
  # trusting a shared-home environment.
  if [[ "$RECEIPT_MODE" -eq 1 ]]; then
    UV_PROJECT_ENVIRONMENT="$VENV" \
      uv sync --locked --python "$VENV/bin/python" --extra dev
  fi
fi

exec "$SCRIPT_DIR/run_tests_v1.sh" "${MODE_ARGS[@]}"
