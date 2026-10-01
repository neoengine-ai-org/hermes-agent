# Hermes CI Runtime OS adapter

The advisory adapter pins NeoEngine policy `2.1.0` from `871e416afc55db187d2b6f29c9ff7cac96472223` through `ci/runtime-os/policy-bundle.lock.json`.

Trusted-base code selects affected isolated tests, including direct-import and monkeypatch consumers, and fails closed to all six slices plus e2e for broad or unknown executable changes. The canonical decision-contract digests and Hermes repository profile are locked with historical parity fixtures. `merge_group` always receives full proof.

## Privilege split

`ci-runtime-os-advisory.yml` executes base-branch code only when triggered by `pull_request_target`. It never checks out or runs the PR head: it fetches the head as inert git objects into the trusted checkout for `git diff --name-only`, classifies risk from that list plus the PR body, and evaluates review evidence and admission from the GitHub API. Trusted jobs on the shared self-hosted pool first discard any workspace residue. `tests/ci/test_pull_request_target_safety.py` fails if any `pull_request_target` workflow passes `github.event.pull_request.head.*`, `github.head_ref`, the merge commit, or `refs/pull/*` to an action input, or runs a materializing git command (checkout, worktree, archive, show, apply, `FETCH_HEAD`, …) in a job that reads those refs.

PR-head execution (environment build, test slices, e2e) runs in `ci-runtime-os-candidate.yml` on `pull_request` under the same job names. The `Await unprivileged candidate proof` job (GitHub-hosted, `actions: read`, so it cannot starve the self-hosted pool) binds `Hermes CI required` to the latest same-repository candidate run for the exact head SHA and PR number, and fails closed when the PR modifies the candidate workflow (its proof would be self-defined) or when no run appears within 20 minutes.

Boundaries this split does not claim:

- `pull_request` and `pull_request_review` runs load workflow files from the PR merge ref. A repository writer can edit either workflow on their branch, so the candidate workflow's read-only permissions and the advisory verdict on review events are only as trustworthy as that writer, which matches the access they already have through any pushed-branch workflow.
- Candidate tests remain PR-controlled code; the result is evidence about the candidate, never policy, review, or admission authority.
- PR-head code still runs on the shared self-hosted `qwen-ops` host, as it did before this split. Workspace cleanup closes residue left between jobs, but a dedicated ephemeral pool is needed for host-level isolation.

Open PRs that predate the candidate workflow, and label- or body-only events on a head with no candidate run, need a push or reopen to create one. After re-running a failed candidate run, re-run the advisory workflow (or push, label, or review) to refresh `Hermes CI required`. Push, merge-group, schedule, and dispatch events still run the proof inline from trusted refs.

One run-scoped immutable `ci-fast` artifact contains the locked Python environment and pinned ripgrep binary; selected slices restore it instead of repeating downloads and dependency setup. Only the environment build may retry once, and only for classified network/infrastructure failures. Duration telemetry is read under a test-manifest plus dependency digest and PR jobs never publish shared telemetry.

`Review evidence required` consumes only one exact-head adversarial GitHub PR review from an authenticated, non-builder repository collaborator for R3. PR-body prose is not a receipt transport. Review ID, actor, commit, timestamp, URL, and 24-hour TTL come from GitHub; provider/model strings remain reviewer metadata rather than cryptographically authenticated claims. R4-R5 fail closed because no authenticated specialist mapping or signed attestation service is installed.

The workflow emits `Hermes CI required`, `Review evidence required`, and `Merge admission` without write permissions, merge behavior, or branch-protection authority. Label, body, review, draft, and merge-group changes refresh the workflow. Admission refetches live PR state and fails closed on stale heads/bases, an advanced protected base, conflict or unknown mergeability, draft state, current or outstanding changes requested, unresolved or ambiguous review threads, and case-normalized canonical opt-out labels. Merge-group code proof runs against the synthetic head, but review/admission deliberately fail closed until a protected authority supplies complete constituent membership and per-member risk classification; GitHub's `associatedPullRequests` lookup is not treated as complete queue membership. Fork candidates fail before self-hosted execution.

R0-R2 use no CI model, R3 uses one post-green adversarial receipt under policy 2.1.0, and protected R4-R5 work remains blocked for the named specialist. Existing CI remains authoritative until the single cross-repository administration transaction; rollback is a normal revert.
