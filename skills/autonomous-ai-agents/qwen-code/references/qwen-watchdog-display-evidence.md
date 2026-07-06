# Qwen watchdog display evidence

When adding Qwen visibility to founder/watchdog status reports, do not inflate
Codex/Sonnet build-lane counts. Qwen ops/controller lanes are a separate runtime
surface and should be reported separately from the existing build-lane proof.

## Display contract

A founder-facing runtime line may include Qwen only when the script has live,
process-backed evidence for the Qwen controller/conductor substrate, for example:

```text
- active agents: Codex 4/4, Sonnet 4/4, Qwen 1/1
- qwen evidence: qwen-ops-runner-conductor, qwen-ops-controller
```

## Evidence rules

- Treat `qwen-ops-runner-conductor` and `qwen-ops-unified-pr-ci-controller.py`
  process evidence as Qwen runtime/controller evidence.
- Keep Qwen as its own `Qwen live/required` count; do not add it to Codex or
  Sonnet build-lane totals.
- Do not count transient shell commands, grep/find invocations, stale receipts,
  or old Computer Use turn-ended payloads as live Qwen runtime evidence.
- A Qwen controller/conductor process proves only the Qwen ops substrate is
  present. It is not product progress, CI green, merge readiness, deployment, or
  production readiness evidence.

## Verification pattern

Before reporting the changed watchdog output, verify both:

1. the watchdog/reporting script compiles or passes its local syntax check; and
2. a live run prints the Qwen line and a concrete `qwen evidence` line.

If Qwen is absent, report `Qwen 0/1` with `qwen evidence: none` instead of
silently omitting the lane or claiming healthy runtime.
