# Hermes CI Runtime OS adapter

Hermes vendors the immutable NeoEngine CI Runtime OS policy release `2.1.0`
from source commit `871e416afc55db187d2b6f29c9ff7cac96472223`. The release JSON is
content-addressed by `ci/runtime-os/policy-release.lock.json`; the dependency-free
preflight verifies its identity before installing the test environment.

The adapter is advisory. Its trusted-base `pull_request_target` dispatcher
publishes the stable contexts `Hermes CI required`,
`Review evidence required`, and `Merge admission`, but this change does not make
them required, change branch protection, add approval labels, or enable merging.
Pull requests from forks fail closed in base-owned workflow code before candidate
checkout or execution. Policy, classifier, and receipt validation run from the
trusted base; the workflow does not use a private cross-repository checkout or
token.

Affected tests are selected narrowly. Changes to the classifier, test runner,
packaging or dependency graph, shared CI runtime, workflow, or an unknown
executable path fail closed to all six isolated test slices plus e2e. Protected
`main`, nightly, and manual runs also execute the full proof. Each selected test
file still runs in its own Python interpreter through `scripts/run_tests.sh`.
The selector reads duration data written by the established test workflow's
protected-`main` cache; this adapter never publishes PR duration telemetry. uv
caching is enabled only for protected `main` pushes.

Review routing follows the canonical model: R0-R2 use no CI model; R3 requires
one head-bound post-green adversarial receipt (including a validator-approved
degraded fallback); R4-R5 remain blocked for protected specialist review. The
adapter never grants those reviews itself.
