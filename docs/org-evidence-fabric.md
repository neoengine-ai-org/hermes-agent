# Org Evidence Fabric Control Loop

Hermes Agent includes a policy-driven, all-org evidence fabric for delivery-control receipts. The loop is intentionally local/repo-native: it does **not** claim production cron, protected branch mutation, live deployment, external notification delivery, money movement, banking readiness, accepted delivery, or landed delivery.

## Flow

1. Agents write `org.agent_closeout.v1` candidate receipts with `hermes-progress receipt`.
2. `hermes-progress verify` loads every seeded `neoengine_local/org_evidence_policies/*.policy.json` by default, validates schema/policy, checks injected live state, and promotes only subject-bound claims.
3. Verified events append to `events/<org>/events.jsonl` with idempotency keys, event hashes, and previous-event hashes.
4. Missing, stale, malformed, contradicted, or policy-invalid evidence becomes explicit proof debt.
5. Proof debt becomes deterministic next-action tickets and durable dispatch assignment state.
6. Release gates, watchdog views, graph/index projections, material notifications, anti-theater scorecards, aftercare projections, and release memory read verified projections/ledger state only.

## State directory contract

Default root: `~/.hermes/state/org-evidence`.

- `inbox/<org>/candidate`: raw candidate receipts.
- `events/<org>/events.jsonl`: append-only canonical evidence ledger.
- `projections/<org>`: per-org product progress, proof debt, contradictions, release gates, dispatch state, protected boundaries, indexes, and graph.
- `projections/all-orgs`: all-org watchdog summary, gates, dispatch tickets/state, graph, notifications, scorecard, aftercare, protected-boundaries, and release memory.
- `dispatch/assigned|closed|rejected`: durable assignment lifecycle.
- `memory/<org>|all-orgs`: deterministic daily/weekly release memory.
- `verifier-runs/<org>`: verifier run receipts with policy/live-state hashes and projection hashes.

## Policy model

Adding an org is a policy-file operation: add `neoengine_local/org_evidence_policies/<org>.policy.json` with `schema: org.policy.v1`, `org`, `critical_path`, and `required_checks`. Policies may define critical-path objects, proof ladders, protected boundaries, required checks, invalid/eligible lanes, release gates, materiality thresholds, notification rules, acceptance criteria, deployment environments, and aftercare requirements. Unknown optional fields are tolerated; missing required fields fail closed into policy debt.

## CLI

- `hermes-progress receipt`
- `hermes-progress verify [--dry-run]`
- `hermes-progress policy list|validate`
- `hermes-progress watchdog summarize`
- `hermes-progress dispatch list|assign|close|reconcile`
- `hermes-progress gate evaluate`
- `hermes-progress memory summarize`
- `hermes-progress graph build`
- `hermes-progress notifications list`
- `hermes-progress doctor`

`gate evaluate` returns non-zero when blocking gates fail. `doctor` returns non-zero when policy errors, proof debt, contradictions, or missing all-org projections exist.

## No dark green

Ready/green states include source event IDs, subject binding, verifier/policy hash, proof ladder rung, evidence artifacts, non-claims, and staleness status. Blocked states include missing evidence, next required artifact, eligible lanes, and forbidden claims.

## Production limitations / non-claims

This code implements the repo-native control loop and file projections. It does not install cron, mutate branch protection, send Slack/email/Telegram notifications, deploy services, mark any org live/deployed/accepted/landed, bypass required checks, or activate customer/money/banking readiness.
