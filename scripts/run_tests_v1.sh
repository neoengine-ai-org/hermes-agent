#!/usr/bin/env bash
# Canonical test runner for hermes-agent. Run this instead of calling
# `pytest` directly to guarantee your local run matches CI behavior.
#
# What this script enforces:
#   * Per-file isolation via scripts/run_tests_parallel.py — each test
#     file runs in its own freshly-spawned `python -m pytest <file>`
#     subprocess. No xdist, no shared workers, no module-level leakage
#     between files.
#   * TZ=UTC, LANG=C.UTF-8, PYTHONHASHSEED=0 (deterministic)
#   * Env vars blanked (conftest.py also does this, but this
#     is belt-and-suspenders for anyone running pytest outside our
#     conftest path — e.g. on a single file)
#   * Explicit interpreter provenance. Developer mode preserves the historical
#     shared-home fallback; local-only and receipt modes never use it.
#
# Usage:
#   scripts/run_tests.sh                            # full suite
#   scripts/run_tests.sh -j 4                       # cap parallelism
#   scripts/run_tests.sh tests/agent/               # discover only here
#   scripts/run_tests.sh tests/agent/ tests/acp/    # multiple roots
#   scripts/run_tests.sh tests/foo.py               # single file
#   scripts/run_tests.sh tests/foo.py -- --tb=long  # path + pytest args
#   scripts/run_tests.sh -- -v --tb=long            # pytest args only
#   scripts/run_tests.sh --local-venv-only tests/bootstrap/
#   scripts/run_tests.sh --receipt-mode tests/bootstrap/
#   scripts/run_tests.sh --allow-shared-venv tests/agent/
#
# Everything after a literal '--' is passed through to each per-file
# pytest invocation. Positional path arguments before '--' override
# the default discovery root (tests/).

set -euo pipefail

# ── Locate repo root ────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# ── Parse bootstrap provenance flags ─────────────────────────────────────────
RUNNER_MODE="developer"
LOCAL_VENV_ONLY=0
ALLOW_SHARED_VENV=1
PASSTHROUGH_ARGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --local-venv-only)
      LOCAL_VENV_ONLY=1
      ALLOW_SHARED_VENV=0
      shift
      ;;
    --receipt-mode)
      RUNNER_MODE="receipt"
      LOCAL_VENV_ONLY=1
      ALLOW_SHARED_VENV=0
      shift
      ;;
    --allow-shared-venv)
      ALLOW_SHARED_VENV=1
      shift
      ;;
    *)
      PASSTHROUGH_ARGS+=("$1")
      shift
      ;;
  esac
done
if [[ ${#PASSTHROUGH_ARGS[@]} -gt 0 ]]; then
  set -- "${PASSTHROUGH_ARGS[@]}"
else
  set --
fi

if [[ "$RUNNER_MODE" == "receipt" && "$ALLOW_SHARED_VENV" -eq 1 ]]; then
  echo "error: --receipt-mode cannot be combined with --allow-shared-venv" >&2
  exit 2
fi

# ── Activate venv ───────────────────────────────────────────────────────────
VENV=""
VENV_PROVENANCE=""
EXPLICIT_RECEIPT_VENV=0
CANDIDATES=()
if [[ "$RUNNER_MODE" == "receipt" && "${HERMES_RECEIPT_VENV+x}" == "x" ]]; then
  # The repair-first wrapper has already validated and rebuilt this exact
  # checkout-local environment. It must outrank any stale developer .venv.
  if [[ -z "$HERMES_RECEIPT_VENV" ]]; then
    echo "error: explicit receipt virtualenv is empty" >&2
    exit 1
  fi
  case "$HERMES_RECEIPT_VENV" in
    /*) RECEIPT_VENV_LEXICAL="$HERMES_RECEIPT_VENV" ;;
    *) RECEIPT_VENV_LEXICAL="$REPO_ROOT/$HERMES_RECEIPT_VENV" ;;
  esac
  case "$RECEIPT_VENV_LEXICAL" in
    "$REPO_ROOT/.venv"|"$REPO_ROOT/.bootstrap-proof-venv") ;;
    *)
      echo "error: explicit receipt virtualenv is not admitted: $HERMES_RECEIPT_VENV" >&2
      exit 1
      ;;
  esac
  HERMES_RECEIPT_VENV="$RECEIPT_VENV_LEXICAL"
  if [[ ! -f "$HERMES_RECEIPT_VENV/bin/activate" \
        || ! -x "$HERMES_RECEIPT_VENV/bin/python" ]]; then
    echo "error: explicit receipt virtualenv is missing or incomplete: $HERMES_RECEIPT_VENV" >&2
    exit 1
  fi
  ROOT_REAL="$(cd -- "$REPO_ROOT" && pwd -P)"
  VENV_REAL="$(cd -- "$HERMES_RECEIPT_VENV" 2>/dev/null && pwd -P)" || {
    echo "error: explicit receipt virtualenv cannot be resolved: $HERMES_RECEIPT_VENV" >&2
    exit 1
  }
  case "$VENV_REAL" in
    "$ROOT_REAL"/*) ;;
    *)
      echo "error: explicit receipt virtualenv escapes the repository: $HERMES_RECEIPT_VENV" >&2
      exit 1
      ;;
  esac
  if [[ -L "${HERMES_RECEIPT_VENV%/}" ]]; then
    echo "error: explicit receipt virtualenv cannot be a symlink: $HERMES_RECEIPT_VENV" >&2
    exit 1
  fi
  HERMES_RECEIPT_VENV="$VENV_REAL"
  CANDIDATES+=("$VENV_REAL")
  EXPLICIT_RECEIPT_VENV=1
else
  CANDIDATES+=("$REPO_ROOT/.venv")
fi
if [[ "$RUNNER_MODE" != "receipt" ]]; then
  CANDIDATES+=("$REPO_ROOT/venv")
fi
if [[ "$ALLOW_SHARED_VENV" -eq 1 ]]; then
  CANDIDATES+=("$HOME/.hermes/hermes-agent/venv")
fi
for candidate in "${CANDIDATES[@]}"; do
  if [ -f "$candidate/bin/activate" ]; then
    VENV="$candidate"
    if [[ "$candidate" == "$HOME/.hermes/hermes-agent/venv" ]]; then
      VENV_PROVENANCE="shared-home-non-receipt"
    elif [[ "$EXPLICIT_RECEIPT_VENV" -eq 1 ]]; then
      VENV_PROVENANCE="explicit-receipt-venv"
    elif [[ "$candidate" == "$REPO_ROOT"/* ]]; then
      VENV_PROVENANCE="repo-local"
    else
      VENV_PROVENANCE="explicit-candidate"
    fi
    break
  fi
done

if [ -z "$VENV" ]; then
  echo "error: no virtualenv satisfies runner mode=$RUNNER_MODE local_only=$LOCAL_VENV_ONLY" >&2
  exit 1
fi

PYTHON="$VENV/bin/python"
echo "bootstrap_runner_mode=$RUNNER_MODE"
echo "interpreter=$PYTHON"
echo "interpreter_provenance=$VENV_PROVENANCE"
if [[ "$VENV_PROVENANCE" == "shared-home-non-receipt" ]]; then
  echo "warning: shared HOME venv is developer convenience and cannot emit a bootstrap receipt" >&2
fi


# ── Live-gateway plugin (computed before we drop env) ───────────────────────
EXTRA_PYTHONPATH=""
EXTRA_PYTEST_PLUGINS=""
if [[ "$RUNNER_MODE" != "receipt" && -f "$HOME/.hermes/pytest_live_guard.py" ]]; then
  EXTRA_PYTHONPATH="$HOME/.hermes"
  EXTRA_PYTEST_PLUGINS="pytest_live_guard"
fi

RECEIPT_HOME=""
if [[ "$RUNNER_MODE" == "receipt" ]]; then
  RECEIPT_HOME="$(mktemp -d)"
  trap 'rm -rf -- "$RECEIPT_HOME"' EXIT
  receipt_args=(
    --root "$REPO_ROOT"
    --receipt-venv "$VENV"
  )
  if [[ -n "${HERMES_BOOTSTRAP_RECEIPT_OUT:-}" ]]; then
    receipt_args+=(--receipt-out "$HERMES_BOOTSTRAP_RECEIPT_OUT")
  fi
  HOME="$RECEIPT_HOME" HERMES_HOME="$RECEIPT_HOME/.hermes" \
    "$PYTHON" -I -S "$REPO_ROOT/scripts/validate_hermes_bootstrap_closure.py" "${receipt_args[@]}"
fi


# ── Run in hermetic env ──────────────────────────────────────────────────────
# env -i: start with empty environment, opt-in only what we need.
# No credential var can leak — you'd have to explicitly add it here.
echo "▶ running per-file parallel test suite via run_tests_parallel.py"
echo "  (TZ=UTC LANG=C.UTF-8 PYTHONHASHSEED=0; clean env)"

cd "$REPO_ROOT"

RUN_HOME="$HOME"
ENV_ARGS=(
  "PATH=$PATH"
  "HOME=$RUN_HOME"
  "TZ=UTC"
  "LANG=C.UTF-8"
  "LC_ALL=C.UTF-8"
  "PYTHONHASHSEED=0"
)
if [[ -n "$EXTRA_PYTHONPATH" ]]; then
  ENV_ARGS+=("PYTHONPATH=$EXTRA_PYTHONPATH")
fi
if [[ -n "$EXTRA_PYTEST_PLUGINS" ]]; then
  ENV_ARGS+=("PYTEST_PLUGINS=$EXTRA_PYTEST_PLUGINS")
fi
if [[ "$RUNNER_MODE" == "receipt" ]]; then
  RUN_HOME="$RECEIPT_HOME"
  ENV_ARGS[1]="HOME=$RUN_HOME"
  ENV_ARGS+=("HERMES_HOME=$RUN_HOME/.hermes" "PYTHONNOUSERSITE=1")
  if env -i "${ENV_ARGS[@]}" "$PYTHON" "$SCRIPT_DIR/run_tests_parallel.py" "$@"; then
    exit 0
  else
    exit $?
  fi
fi

exec env -i "${ENV_ARGS[@]}" "$PYTHON" "$SCRIPT_DIR/run_tests_parallel.py" "$@"
