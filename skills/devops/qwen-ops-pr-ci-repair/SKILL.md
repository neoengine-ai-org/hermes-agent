---
name: qwen-ops-pr-ci-repair
description: "Bounded qwen-ops PR/CI repair workflow: exact-head branch sync/repair, targeted validation, GitHub check polling, and truthful drain-movement readback."
version: 1.0.0
author: qwen-ops-runner-conductor
license: MIT
metadata:
  hermes:
    tags: [qwen-ops, pr-ci, github, drain, exact-head, repair]
---

# Qwen Ops PR/CI Repair

Use this skill when the operator asks to actively reduce PR/CI pressure, repair a failing PR, sync a behind same-repo branch, or prove that a qwen-ops drain action produced terminal movement.

## Authority boundary

- Output label: `STATUS_ONLY` and `not_merge_evidence: true` for PR/CI/drain status.
- Safe scope: exact-head same-repo branch syncs, minimal code/test fixes, reruns, and local receipt/status capture when explicitly authorized by the operator and policy.
- Never merge, approve PR reviews, enable/disable auto-merge, bypass branch protection, label/comment/request review, or treat founder receipts as GitHub approvals.
- Auto-merge enablement is merge-path authority. Audit and report `autoMergeRequest` state, but do not change it unless a separate higher-authority approved mode explicitly permits it.
- Founder receipts clear local qwen protected-escalation reporting only; they do not satisfy GitHub protection.

## Workflow

1. **Read live state first**
   - Query PR head SHA, branch name, draft/protected/cross-repo status, mergeability, and check rollup.
   - Identify whether the primary blocker is deterministic code/test failure, merge-behind, merge conflict, stale/cancelled checks, protected governance, or consumer ACK absence.

2. **Active-drain consent preflight before mutation**
   - When the operator says the count is increasing or asks to "drain the PRs", first run a read-only/live status pass if needed to identify safe branch-sync, conflict-repair, protected, and evidence-gap buckets.
   - Do not assume a frustrated drain request is enough to satisfy local tool approval for mutating commands. Before enabling `QWEN_OPS_ENABLE_ACTIVE_REPAIR=true`, `QWEN_OPS_ACTIVE_REPAIR_DRY_RUN=false`, pushing branch updates, or using action/write tokens, obtain explicit current-turn approval for the bounded active scope.
   - If the local execution safety gate blocks an active/mutating command for missing approval, treat that as a hard stop: do not retry, rephrase, or route around it. Preserve the read-only artifact and ask for explicit active-drain approval that names allowed actions and forbidden actions.
   - A good approval ask states: allowed = exact-head same-repo branch syncs / bounded safe repair / targeted reruns as applicable; forbidden = merges, GitHub PR approvals, labels/comments/review requests, and branch-protection bypass.
   - If an approved active pass shows `ACTION_READY_ACTIVE_REPAIR` / `SAFE_UPDATE_BRANCH_ELIGIBLE` rows but takes few or zero actions, inspect executor gates before concluding there is no work. Some repos require class-specific same-repo branch-sync env gates, and some controller paths require `ORG_ACTIONS_TOKEN` even when the wrapper allows mapped read-token actions. See `references/active-drain-class-gates-and-postcheck.md`.

3. **Exact-head preflight before mutation**
   - Re-read the live PR head SHA and remote branch SHA immediately before any push/update.
   - Compare them to local HEAD. If any differ, stop and rebase/re-plan; do not push over a moved branch.
   - For branch sync, locally merge/fetch `origin/main` into the PR worktree before remote mutation.

3. **Repair narrowly**
   - Copy or adapt already-validated mainline fixes when available rather than inventing broad changes.
   - Resolve only conflicts inside the requested/safe scope.
   - For generated aggregate files, merge intent from both sides instead of choosing one side blindly. For example, when `package.json` `scripts.test` conflicts with independent additions from the PR and `main`, parse/split the command chain and union both sets while preserving order; when `docs/test-inventory.json` conflicts, union new test records and recompute summary counts from the actual array.
   - For repository-generated inventory/audit files, prefer the repo's deterministic update command followed by its `--check`/audit command over hand-editing counts. Stale inventory is a common CI-only blocker after branch syncs.
   - For Roadmap OS / generated-view failures after a branch sync, run the repo's generator and parity/validation checks together; a stale generated view can be the only deterministic CI failure even after mergeability is clean. See `references/roadmap-generated-view-after-sync.md`.
   - For package-local schema/OpenAPI drift gates that import sibling package code, reproduce the CI install shape before pushing. Full-workspace installs can mask dependency-resolution drift; see `references/stale-rollup-and-cross-package-drift-gates.md`.
   - If `main` moves during a long multi-PR drain, re-read each previously repaired PR before final reporting. A PR that was just made mergeable can become behind/conflicting again; repeat exact-head sync/conflict repair rather than reporting the earlier state.
   - If the controller's generated preview times out on clone, treat that as fail-closed preview evidence, not proof of a real merge conflict. A bounded exact-head local preview with a longer timeout is acceptable when policy/operator authorization permits.
   - For multi-PR drain requests, process same-repo `BEHIND` + `MERGEABLE` branches as exact-head branch syncs in isolated worktrees, then handle PR-body/classifier metadata fixes and current-head cancelled reruns before spending time on broad implementation repairs.
   - For org-wide drain turns with mixed blockers, use the bucketed sequence in `references/org-wide-conflict-and-metadata-drain.md`: live snapshot → safe branch syncs → PR-body/classifier metadata repairs → current-head cancelled reruns → narrow conflict drain → final readback.
   - Reserve manual conflict resolution for `DIRTY` / `CONFLICTING` PRs, and only repair narrow conflicts that can be validated locally. Configure git identity in temporary clones before merge previews so identity errors do not masquerade as merge failures.
   - When the operator asks specifically to resolve conflicts, a verified GitHub transition from `DIRTY`/conflicting to `BLOCKED`/non-conflicting is valid pressure movement, but not completion. Report the remaining failed/pending checks explicitly and do not blur conflict drain with all-green drain. See `references/conflict-drain-blocked-not-dirty.md`.
   - For classifier failures with mergeable code, inspect the trusted classifier artifact before changing code. If `merge_blocking_conditions` are PR-body/RuntimePayloadContract metadata (for example missing classification section, expected-state-change fields, or `declared_ci_lanes_weaker_than_classifier:<lane>`), repair the PR body and rerun/poll the classifier. If GitHub keeps evaluating an old PR-body snapshot, force a minimal exact-head same-branch refresh commit only after verifying the live head. Treat remaining protected-review/human-gate blockers as outside qwen merge authority, but report that the metadata blocker was reduced.
   - After branch syncs, generated freshness/evidence files that pin `base_sha` / `base_commit` can become stale again. Re-read current `origin/main`, restamp all related generated evidence fields together, mirror the new stamp into any proof/closeout docs asserted by the gate, run the specific freshness/acceptance tests, exact-head verify, then push. See `references/classifier-metadata-and-restamp-drain.md`.
   - Treat literal conflict markers in workflow YAML and PR-body governance/docs-context misses as deterministic drain blockers. Repair conflict markers in an exact-head worktree with YAML/diff validation; repair PR-body-only governance failures via `gh pr edit --body-file`, then rerun the exact failing governance run and distinguish current rerun status from stale rollup failures. See `references/pr-body-governance-and-workflow-conflict-drain.md`.
   - For fail-closed governance scanners that grep added lines for forbidden instruction literals, check whether the scanner/test fixtures introduce the forbidden literal and red their own PR. Prefer repairing the script/tests to construct the forbidden literal at runtime (for example split `/t''mp` and interpolate in test repos) and validate by replaying the exact added-line grep against the working diff; avoid broad workflow exclusions unless explicitly authorized.
   - When Roadmap OS / docs-impact / governance blockers are PR-body metadata only, repair the PR body, run the repo's local validator if available, and rerun the exact failed validation surface before claiming movement. When backend fails on stale generated test inventory, use the repo's generator/check commands plus JSON parse and `git diff --check`; report any later backend failure as a separate blocker. See `references/pr-body-metadata-and-generated-inventory-drain.md`.

4. **Validate before push**
   - Run syntax/format checks for touched scripts/configs.
   - Run targeted tests for each modified subsystem.
   - Run whitespace/diff checks (`git diff --check` or repo equivalent).
   - Do not count validation commands hidden behind `|| true` as proof; rerun cleanly or preserve the failure as a blocker.

5. **Push only after proof**
   - Use credential handling that never prints tokens.
   - Push only to the same-repo PR branch after exact-head verification.
   - After push, re-read remote branch SHA and PR head until GitHub reflects the new head.

6. **Poll terminal status**
   - Poll PR check rollup/check runs until all checks are terminal, or name the exact remaining pending/failing check.
   - Track cancelled current-head checks separately from failures. If the PR is mergeable and has no failing checks but current-head workflows are `CANCELLED`, rerun only those exact-head cancelled run IDs, then poll until the rollup has `0 pending / 0 failed / 0 cancelled`.
   - If a metadata/PR-body repair makes a new rerun pass but GitHub still keeps an older failed check in the PR rollup, re-read exact head and, when safe, use a minimal same-branch head refresh commit to force a clean current-head rollup; see `references/stale-rollup-and-cross-package-drift-gates.md`.
   - If one long backend/mac smoke check remains, keep polling or report that exact check; do not claim complete drain while pending.
   - If reruns repeatedly create fresh cancelled backend/classifier shards and `backend / modular aggregator (advisory)` fails because an upstream impact classifier or required shard was cancelled, stop the rerun loop after one rerun+sync/refresh cycle and classify it as a CI orchestration/runner cancellation cascade unless logs show a deterministic test failure. See `references/cancelled-rerun-cascades-and-review-receipt-gates.md`.
   - When an approved active-drain pass finds no conflicts/behind branches but current-head cancelled checks remain, batch rerun only deduplicated current-head workflow run IDs extracted from cancelled check `detailsUrl`s, then verify by re-reading the same PR heads. Count only terminal readback movement, not accepted rerun requests. See `references/current-head-rerun-batch-drain.md`.
   - For Hermes-style classifier gates, distinguish PR-body metadata blockers from protected review-receipt blockers. Missing current-head receipts such as `secondary`, `adversarial`, `opposite_frontier`, `opposite_provider_adversarial`, or `finance_sensitive` are not autonomous repair targets; audit/report them and do not fabricate satisfaction.

7. **Run post-action drain readback**
   - Run the qwen-drain/controller status cycle after successful push/check readback.
   - Report before/after queue movement: blocked non-draft count, ready-for-human count, lifecycle transition, failed/pending check deltas.

## Count-increasing active drain mode

When the operator says the PR count is increasing or should be decreasing/staying current, switch from summary/triage posture to measured drain posture:

1. Capture a before snapshot of non-draft blocked PRs, failed-CI PRs counted once, failing-check totals, pending totals, execution buckets, and ready-for-human count.
2. Rank mechanical drain first: exact-head same-repo `SAFE_UPDATE_BRANCH_ELIGIBLE` / behind+mergeable branches, stale/cancelled current-head reruns, and already-authorized founder-receipt/protected-routing cleanup. Do not spend the turn only creating packets or classifications if safe reversible actions are available.
3. Batch safe branch syncs, then immediately run a post-action controller/drain readback. It is acceptable for pending checks to rise after syncs; report that as new CI pressure triggered by movement rather than a failure, and continue polling/repairing terminal failures. If the active pass took no actions despite ready rows, rerun only after checking class env gates/action-token mapping from `references/active-drain-class-gates-and-postcheck.md`.
4. Convert results into pressure deltas: safe-update backlog reduced, blocked count reduced, failed-CI count reduced, failing checks reduced, ready-for-human increased, stale-branch rows converted to conflict/implementation rows because real conflicts were exposed by syncing, or exact gate surfaced (branch-prefix allowlist, class env, action token, protected gate).
5. If deltas show only activity and no pressure movement, say so plainly and name the next blocked authority/scope rather than calling it progress.

## What counts as PR-pressure movement

- PR head updated to the pushed SHA and GitHub confirms it.
- Mergeability becomes clean/MERGEABLE, including a `DIRTY`/conflicting PR becoming non-conflicting but still `BLOCKED` by named checks.
- Failed/pending/cancelled current-head check count decreases, or check rollup reaches all-success.
- qwen-drain lifecycle moves the PR to `READY_FOR_HUMAN_MERGE_DECISION`.
- Org queue metrics improve, e.g. blocked PR count decreases or ready-for-human count increases.

## What does not count by itself

- Local-only sidecar handoff files without consumer ACK/schema/error.
- `qwen-drain --mode active` with `actions_taken=0`.
- A rerun request without terminal check readback.
- Cancelled current-head workflows left un-rerun when the stated goal is "everything green".
- Rerunning cancelled workflows while deterministic current-head failures or active pending checks remain; that is noise unless the failed/pending state is stale or known to depend on the rerun.
- Founder approval receipts.
- Classification/routing activity without blocker/check reduction.

## Reporting format

Keep the final concise and evidence-first:

- Label: `STATUS_ONLY — not_merge_evidence: true`.
- `Changed:` commits/heads pushed.
- `Validated:` local commands and outcomes.
- `GitHub:` head SHA, mergeability, check counts, named failed/pending checks.
- `Drain movement:` queue/lifecycle deltas.
- `Remaining blockers:` exact next owner/action.
- Explicitly state no merge/approval/bypass occurred.

## References

- `references/exact-head-pr-repair-readback.md` — concrete session pattern for same-repo exact-head repair/sync and terminal drain readback.
- `references/current-head-cancelled-check-rerun.md` — pattern for rerunning only current-head cancelled workflow runs and proving all-green status rollup.
- `references/multi-pr-conflict-and-behind-drain.md` — multi-PR queue drain pattern for mixing safe behind-branch syncs with generated-file conflict resolution and truthful check readback.
- `references/roadmap-generated-view-after-sync.md` — Roadmap OS generated-view stale-output repair pattern after exact-head branch sync.
- `references/conflict-drain-blocked-not-dirty.md` — conflict-drain pattern for preserving both sides, proving `DIRTY`→non-conflicting movement, and avoiding noisy reruns when deterministic blockers remain.
- `references/stale-rollup-and-cross-package-drift-gates.md` — stale GitHub rollup cleanup via exact-head refresh and CI reproduction pattern for package-local generated drift gates that import sibling schemas.
- `references/active-drain-approval-gate.md` — active-drain consent gate pattern: run read-only status first, then require explicit bounded approval before mutation; stop if the local execution gate blocks active repair.
- `references/count-increasing-active-drain.md` — response pattern for rising PR counts: take a before snapshot, drain mechanical buckets first, then report real pressure deltas instead of packet/classification volume.
- `references/org-wide-conflict-and-metadata-drain.md` — org-wide green-state drain pattern for mixed behind branches, PR-body/classifier fixes, current-head cancelled reruns, and narrow `DIRTY` conflict resolution.
- `references/classifier-metadata-and-restamp-drain.md` — pattern for reducing classifier PR-body/RuntimePayloadContract blockers and restamping generated freshness/evidence SHAs after base branch movement.
- `references/cancelled-rerun-cascades-and-review-receipt-gates.md` — pattern for handling repeated cancelled backend/classifier rerun cascades, advisory aggregator failures caused by cancelled shards, and protected review-receipt gates without fabricating authority.
- `references/current-head-rerun-batch-drain.md` — approved active-drain pattern for batching current-head cancelled workflow reruns, deduplicating run IDs from check details URLs, and reporting only verified post-rerun pressure movement.
- `references/pr-body-governance-and-workflow-conflict-drain.md` — pattern for resolving workflow YAML conflict markers, body-only governance/docs-context metadata misses, and narrow TypeScript drain blockers while preserving exact-head validation/readback.
- `references/bulk-behind-sync-and-narrow-conflict-drain.md` — org-wide pressure-drain pattern for bulk exact-head `update-branch` syncs, stale-head retry, current-head cancelled-run dedupe/rerun, and isolated worktree conflict repair with generated inventory regeneration.
- `references/pr-body-metadata-and-generated-inventory-drain.md` — pattern for repairing PR-body metadata validators, rerunning exact governance/Roadmap OS checks, and refreshing stale generated test inventory without hand-editing counts.
- `references/update-branch-post-sync-review-gate-readback.md` — pattern for treating GitHub `update-branch` API 202 receipts as only an accepted request, then proving head movement/check state and separating successful stale-branch drain from new/stale review-receipt classifier blockers.
