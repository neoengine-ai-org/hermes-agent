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

4. For non-interactive tool execution, use `--approval-mode=yolo` as the single approval flag. Do **not** combine it with `-y`/`--yolo`; Qwen exits with a usage error when both are present. Prefer:

```bash
qwen --bare --sandbox --approval-mode=yolo \
  --auth-type openai \
  --openai-api-key dummy \
  --openai-base-url "${QWEN_OPS_ENDPOINT:-http://127.0.0.1:11434}/v1" \
  --model qwen3.5:9b \
  --max-wall-time 10m \
  --max-tool-calls 20 \
  --prompt "$(cat "$PROMPT_FILE")"
```

5. Verify tool execution, not just exit code: make the lane create or edit a harmless sentinel file and read it back. Qwen may still print stale warnings that mention `-y`, so the file/readback is the authority.
   Use the bundled preflight before headless lanes when available: `scripts/qwen_headless_tool_preflight.sh`.
6. If native file tools still do not produce the expected artifact under `--approval-mode=yolo`, instruct the run to use `run_shell_command` only and avoid native `glob` / `list_directory`.
7. Bound reruns with `--max-wall-time` and `--max-tool-calls`; do not leave an unbounded agent lane running.
8. If the target PR already merged, rerun a small aftercare checkpoint from current `origin/main` instead of reopening implementation scope.
9. If the user says to make the fix “land in org CI merge,” do not leave it as local config/memory only. Convert the durable lesson into a repo-bundled skill/library update, open a PR, satisfy classifier-required CI/review lanes, merge under policy, and verify the artifact from `origin/main`.

Reference: `references/qwen-watchdog-display-evidence.md` captures the pattern for adding Qwen visibility to founder/watchdog status reports without inflating Codex/Sonnet build-lane counts: report Qwen separately from process-backed controller/conductor evidence, and keep product/CI readiness non-claims explicit.

## QWEN35 governed local assistant workflow

When recycling QWEN35 as a governed local engineering assistant, use the repo-bundled `neoengine_local/qwen35_lane_experience.py` controls before treating a run as candidate evidence:

1. `pick-task --description ...` — only `QWEN35_SAFE_LOW_RISK` is launchable without separate human/protected scope.
2. `recipe --task-type ...` — bind the prompt to fixed allowed commands, forbidden commands, status contract, wall-time/tool-call limits, changed-file globs, and verifier rule.
3. `preflight` — require registry-enforced clean-worktree/canary evidence before Qwen launch.
4. `verify` — independently validate the completion receipt against the worktree and non-claim ceiling.
5. `risk --changed-file ...` — hard-stop `RISK_5_FORBIDDEN_PROTECTED_SURFACE`; route runtime-adjacent diffs to independent review.
6. Normalize any operator-facing result as `QWEN35_CANDIDATE_ASSISTANT_RECEIPT`; QWEN35 remains candidate-only and cannot merge, deploy, accept, or claim live/customer impact.

Non-claims: a successful Qwen aftercare run or merged runbook is not deployment, live, accepted, landed, or product-outcome evidence unless those receipts are independently verified.
