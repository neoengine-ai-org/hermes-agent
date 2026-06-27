---
name: qwen-code
description: "Operate Qwen Code CLI headless lanes, especially local OpenAI-compatible Qwen endpoints."
version: 1.0.0
platforms: [macos, linux]
metadata:
  hermes:
    tags: [qwen, qwen-code, autonomous-agents, headless, cli, timeout]
---

# Qwen Code Headless Lanes

## Timeout and prompt discipline

Qwen Code 0.19.1 may fail headless OpenAI-compatible runs with:

```text
[API Error: Request timeout after 483s. Try reducing input length or increasing timeout in config.
```

Do not treat this as repo/PR evidence. It is lane-health evidence until re-grounded against GitHub/local state.

Resolution pattern:

1. Keep prompts very small and operational. Prefer explicit command lists and a bounded final checkpoint.
2. For local/OpenAI-compatible endpoints, configure `~/.qwen/settings.json`:

```json
{
  "model": {
    "generationConfig": {
      "timeout": 1200000,
      "maxRetries": 1,
      "samplingParams": {
        "temperature": 0.1,
        "max_tokens": 2048
      }
    },
    "skipStartupContext": true,
    "maxWallTimeSeconds": 900,
    "maxToolCalls": 80
  },
  "tools": {
    "approvalMode": "yolo"
  }
}
```

3. Verify with a no-tool smoke test before rerunning the lane:

```bash
qwen --bare --auth-type openai \
  --openai-api-key dummy \
  --openai-base-url "${QWEN_OPS_ENDPOINT:-http://127.0.0.1:11434}/v1" \
  --model qwen3.5:9b \
  --max-wall-time 3m \
  --max-tool-calls 0 \
  --prompt 'Return exactly: QWEN_SMOKE_OK'
```

4. For non-interactive tool execution, use `--approval-mode yolo` as the single approval flag. Do **not** combine it with `-y`/`--yolo`; Qwen exits with a usage error when both are present. Prefer:

```bash
qwen --bare --approval-mode yolo \
  --auth-type openai \
  --openai-api-key dummy \
  --openai-base-url "${QWEN_OPS_ENDPOINT:-http://127.0.0.1:11434}/v1" \
  --model qwen3.5:9b \
  --max-wall-time 10m \
  --max-tool-calls 20 \
  --prompt "$(cat "$PROMPT_FILE")"
```

5. Verify tool execution, not just exit code: make the lane create or edit a harmless sentinel file and read it back. Qwen may still print stale warnings that mention `-y`, so the file/readback is the authority.
6. If native file tools still do not produce the expected artifact under `--approval-mode yolo`, instruct the run to use `run_shell_command` only and avoid native `glob` / `list_directory`.
7. Bound reruns with `--max-wall-time` and `--max-tool-calls`; do not leave an unbounded agent lane running.
8. If the target PR already merged, rerun a small aftercare checkpoint from current `origin/main` instead of reopening implementation scope.
9. If the user says to make the fix “land in org CI merge,” do not leave it as local config/memory only. Convert the durable lesson into a repo-bundled skill/library update, open a PR, satisfy classifier-required CI/review lanes, merge under policy, and verify the artifact from `origin/main`.

Non-claims: a successful Qwen aftercare run or merged runbook is not deployment, live, accepted, landed, or product-outcome evidence unless those receipts are independently verified.
