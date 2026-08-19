#!/usr/bin/env bash
# Repair-first wrapper around the byte-preserved V1 Hermes test runner.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV="$REPO_ROOT/.venv"
MODE_ARGS=("$@")
for arg in "$@"; do
  if [[ "$arg" == "--receipt-mode" ]]; then
    VENV="${HERMES_RECEIPT_VENV:-$REPO_ROOT/.bootstrap-proof-venv}"
    export HERMES_RECEIPT_VENV="$VENV"
    break
  fi
done
python3 -S "$SCRIPT_DIR/bootstrap_stage0_v2.py" ensure \
  --repair --venv "$VENV" --operation-id validate-bootstrap
exec "$SCRIPT_DIR/run_tests_v1.sh" "${MODE_ARGS[@]}"
