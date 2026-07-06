#!/usr/bin/env bash
set -euo pipefail

# Preflight Qwen Code headless tool execution by requiring a real sentinel file
# write/readback. Exit-code-only success is not sufficient for non-interactive
# lanes because approval-mode drift can make tool calls no-op.

MODEL="${QWEN_OPS_MODEL:-qwen3.5:9b}"
BASE_URL="${QWEN_OPS_ENDPOINT:-http://127.0.0.1:11434}/v1"
MAX_WALL_TIME="${QWEN_PREFLIGHT_MAX_WALL_TIME:-5m}"
MAX_TOOL_CALLS="${QWEN_PREFLIGHT_MAX_TOOL_CALLS:-8}"
WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/qwen-headless-tool-preflight.XXXXXX")"
SENTINEL="$WORKDIR/QWEN_TOOL_PREFLIGHT_OK"

cleanup() {
  rm -rf "$WORKDIR"
}
trap cleanup EXIT

if ! command -v qwen >/dev/null 2>&1; then
  echo "QWEN_PREFLIGHT_FAIL: qwen CLI not found" >&2
  exit 127
fi

pushd "$WORKDIR" >/dev/null
qwen --bare --approval-mode yolo \
  --auth-type openai \
  --openai-api-key "${QWEN_OPS_API_KEY:-dummy}" \
  --openai-base-url "$BASE_URL" \
  --model "$MODEL" \
  --max-wall-time "$MAX_WALL_TIME" \
  --max-tool-calls "$MAX_TOOL_CALLS" \
  --prompt "Create a file named QWEN_TOOL_PREFLIGHT_OK in the current directory containing exactly OK, then read the file back and end with QWEN_PREFLIGHT_PASS."
popd >/dev/null

if [[ ! -f "$SENTINEL" ]]; then
  echo "QWEN_PREFLIGHT_FAIL: sentinel file was not created: $SENTINEL" >&2
  exit 1
fi

if [[ "$(cat "$SENTINEL")" != "OK" ]]; then
  echo "QWEN_PREFLIGHT_FAIL: sentinel content mismatch" >&2
  exit 1
fi

echo "QWEN_PREFLIGHT_PASS model=$MODEL base_url=$BASE_URL"
