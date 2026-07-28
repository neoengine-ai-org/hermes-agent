# Hermes CI Runtime OS adapter

The advisory adapter pins NeoEngine policy `2.1.0` from `871e416afc55db187d2b6f29c9ff7cac96472223` through `ci/runtime-os/policy-bundle.lock.json`.

Trusted-base `pull_request_target` code selects affected isolated tests, fails closed to all six slices plus e2e for broad or unknown executable changes, and emits `Hermes CI required`, `Review evidence required`, and `Merge admission` without changing repository rules or merge authority. Fork candidates fail before self-hosted execution. PRs never publish shared duration telemetry.

R0-R2 use no CI model, R3 uses one post-green adversarial receipt under policy 2.1.0, and protected R4-R5 work remains blocked for the named specialist. Existing CI remains authoritative until the single cross-repository administration transaction; rollback is a normal revert.
