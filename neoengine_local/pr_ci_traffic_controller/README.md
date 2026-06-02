# PR/CI traffic-controller local runtime helpers

This directory tracks the deterministic local runtime helpers used by the
NeoEngine/NeoWealth Hermes cron sidecars for PR/CI pressure control.

The controller work inbox helper implements the durable watchdog -> PR/CI
controller handoff. Branch namespace collision items require structured evidence
and fail closed with `BRANCH_NAMESPACE_COLLISION_REQUIRES_OPERATOR` unless all
local-only safety checks are proven.

These files are intentionally local-state/runtime helpers: they do not add
GitHub write authority and they do not change protected gates, schedules,
models, workdirs, or `no_agent` cron state.
