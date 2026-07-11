#!/bin/bash
# Hermes home retention cron wrapper.
#
# Modes:
#   check    (default) — report-only health gate; on over-threshold writes a
#            handoff alert JSON (the handoff IS the alert) and still exits 0.
#   execute  — archive-mode retention sweep; failures also become handoffs.
#
# Mirrors ai-org's qwen-ops-evidence-check-cron.sh contract: the wrapper
# never returns non-zero to launchd; conductor lanes consume the handoffs.
#
# Env overrides:
#   HERMES_RETENTION_ROOT         hermes home (default ~/.hermes)
#   HERMES_RETENTION_MODULE_DIR   repo checkout containing neoengine_local/
#   HERMES_RETENTION_HANDOFF_DIR  where alert JSONs land
#   HERMES_RETENTION_EXTRA_ARGS   appended to the python invocation
set -u

# launchd may start us without HOME; derive it rather than abort under set -u
HOME="${HOME:-$(eval echo "~$(id -un)")}"
export HOME

MODE="${1:-check}"
ROOT="${HERMES_RETENTION_ROOT:-$HOME/.hermes}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODULE_DIR="${HERMES_RETENTION_MODULE_DIR:-$(dirname "$SCRIPT_DIR")}"
HANDOFF_DIR="${HERMES_RETENTION_HANDOFF_DIR:-$ROOT/state/hermes-home-retention/handoffs}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"

run_retention() {
    local flag="$1"
    # EXTRA_ARGS come first so the pinned --root always wins (argparse
    # last-occurrence semantics) — extra args cannot re-root the sweep
    # shellcheck disable=SC2086
    (cd "$MODULE_DIR" && python3 -m neoengine_local.hermes_home_retention \
        ${HERMES_RETENTION_EXTRA_ARGS:-} $flag --root "$ROOT" 2>&1)
}

write_handoff() {
    local status="$1" report="$2"
    mkdir -p "$HANDOFF_DIR" || return 0
    # bound the payload: a huge report must not blow the execve env limit
    # and silently lose the only alert (keep head + tail of the report)
    local bounded
    if [ "${#report}" -gt 80000 ]; then
        bounded="$(printf '%s\n' "$report" | head -c 60000)
[... report truncated for handoff; full output in the launchd log ...]
$(printf '%s\n' "$report" | tail -c 20000)"
    else
        bounded="$report"
    fi
    HANDOFF_TARGET="$HANDOFF_DIR/${STAMP}_home_retention_${status}.json" \
    HANDOFF_STATUS="$status" \
    HANDOFF_MODE="$MODE" \
    HANDOFF_STAMP="$STAMP" \
    HANDOFF_REPORT="$bounded" \
    python3 - <<'PYEOF'
import json, os
payload = {
    "schema": "hermes.home_retention_alert.v1",
    "created_utc": os.environ["HANDOFF_STAMP"],
    "status": os.environ["HANDOFF_STATUS"],
    "mode": os.environ["HANDOFF_MODE"],
    "report": os.environ["HANDOFF_REPORT"].splitlines(),
    "recommended_action": (
        "python3 -m neoengine_local.hermes_home_retention --root ~/.hermes "
        "--execute (archive mode; operator-gated)"
    ),
    "source": "scripts/hermes-home-retention-cron.sh",
}
with open(os.environ["HANDOFF_TARGET"], "w") as fh:
    json.dump(payload, fh, indent=2, sort_keys=True)
PYEOF
}

case "$MODE" in
    check)
        REPORT="$(run_retention --check)"
        RC=$?
        echo "$REPORT"
        if [ "$RC" -ne 0 ]; then
            write_handoff "over_threshold" "$REPORT"
        fi
        ;;
    execute)
        REPORT="$(run_retention --execute)"
        RC=$?
        echo "$REPORT"
        if [ "$RC" -ne 0 ]; then
            write_handoff "execute_failed" "$REPORT"
        fi
        ;;
    *)
        echo "usage: $0 [check|execute]" >&2
        ;;
esac

exit 0
