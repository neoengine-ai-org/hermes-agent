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
case "$VENV" in
  "$REPO_ROOT"/*) VENV_REL="${VENV#"$REPO_ROOT"/}" ;;
  /*)
    echo "Hermes bootstrap venv escapes the repository: $VENV" >&2
    exit 2
    ;;
  *) VENV_REL="$VENV" ;;
esac
case "$VENV_REL" in
  .venv|.bootstrap-proof-venv) ;;
  *)
    echo "Hermes bootstrap venv is not admitted: $VENV_REL" >&2
    exit 2
    ;;
esac
python3 -S "$SCRIPT_DIR/bootstrap_stage0_v2.py" ensure \
  --repair --venv "$VENV_REL" --operation-id validate-bootstrap
exec "$SCRIPT_DIR/run_tests_v1.sh" "${MODE_ARGS[@]}"
