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

4. If native file tools request approval despite `-y` / `--approval-mode=yolo`, instruct the run to use `run_shell_command` only and avoid native `glob` / `list_directory`.
5. Bound reruns with `--max-wall-time` and `--max-tool-calls`; do not leave an unbounded agent lane running.
6. If the target PR already merged, rerun a small aftercare checkpoint from current `origin/main` instead of reopening implementation scope.

Non-claims: a successful Qwen aftercare run is not deployment, live, accepted, landed, or product-outcome evidence unless those receipts are independently verified.
