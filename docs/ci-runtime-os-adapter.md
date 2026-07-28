# Hermes CI Runtime OS adapter

The advisory adapter pins NeoEngine policy `2.1.0` from `871e416afc55db187d2b6f29c9ff7cac96472223` through `ci/runtime-os/policy-bundle.lock.json`.

Trusted-base `pull_request_target` code selects affected isolated tests, including direct-import and monkeypatch consumers, and fails closed to all six slices plus e2e for broad or unknown executable changes. The canonical decision-contract digests and Hermes repository profile are locked with historical parity fixtures. `merge_group` always receives full proof.

One run-scoped immutable `ci-fast` artifact contains the locked Python environment and pinned ripgrep binary; selected slices restore it instead of repeating downloads and dependency setup. Only the environment build may retry once, and only for classified network/infrastructure failures. Duration telemetry is read under a test-manifest plus dependency digest and PR jobs never publish shared telemetry.

`Review evidence required` consumes only one exact-head adversarial GitHub PR review from an authenticated, non-builder repository collaborator for R3. PR-body prose is not a receipt transport. Review ID, actor, commit, timestamp, URL, and 24-hour TTL come from GitHub; provider/model strings remain reviewer metadata rather than cryptographically authenticated claims. R4-R5 fail closed because no authenticated specialist mapping or signed attestation service is installed.

The workflow emits `Hermes CI required`, `Review evidence required`, and `Merge admission` without write permissions, merge behavior, or branch-protection authority. Label, body, review, draft, and merge-group changes refresh the workflow. Admission refetches live PR state and fails closed on stale heads/bases, an advanced protected base, conflict or unknown mergeability, draft state, current or outstanding changes requested, unresolved or ambiguous review threads, and case-normalized canonical opt-out labels. Merge-group code proof runs against the synthetic head, but review/admission deliberately fail closed until a protected authority supplies complete constituent membership and per-member risk classification; GitHub's `associatedPullRequests` lookup is not treated as complete queue membership. Fork candidates fail before self-hosted execution.

R0-R2 use no CI model, R3 uses one post-green adversarial receipt under policy 2.1.0, and protected R4-R5 work remains blocked for the named specialist. Existing CI remains authoritative until the single cross-repository administration transaction; rollback is a normal revert.
