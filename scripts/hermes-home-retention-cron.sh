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

MODE="${1:-check}"
ROOT="${HERMES_RETENTION_ROOT:-$HOME/.hermes}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODULE_DIR="${HERMES_RETENTION_MODULE_DIR:-$(dirname "$SCRIPT_DIR")}"
HANDOFF_DIR="${HERMES_RETENTION_HANDOFF_DIR:-$ROOT/state/hermes-home-retention/handoffs}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"

run_retention() {
    local flag="$1"
    # shellcheck disable=SC2086
    (cd "$MODULE_DIR" && python3 -m neoengine_local.hermes_home_retention \
        --root "$ROOT" $flag ${HERMES_RETENTION_EXTRA_ARGS:-} 2>&1)
}

write_handoff() {
    local status="$1" report="$2"
    mkdir -p "$HANDOFF_DIR" || return 0
    HANDOFF_TARGET="$HANDOFF_DIR/${STAMP}_home_retention_${status}.json" \
    HANDOFF_STATUS="$status" \
    HANDOFF_MODE="$MODE" \
    HANDOFF_STAMP="$STAMP" \
    HANDOFF_REPORT="$report" \
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
