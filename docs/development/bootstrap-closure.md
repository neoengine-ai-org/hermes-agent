# Hermes checkout-local bootstrap closure

Hermes receipt-grade bootstrap proof comes only from repository-owned package
metadata, lockfile, entrypoints, bootstrap modules, test runners, and validator.
It does not borrow a user-home virtualenv, pytest plugin, config, credential,
provider session, global executable, sibling checkout, or cached plugin.

```text
clean Hermes checkout
    |-- pyproject + uv.lock --------uv lock --check------+
    |-- tracked entrypoint modules --isolated import-----+--> local closure
    |-- repo-local proof venv -------provenance----------+
    `-- optional providers/plugins --classify, don't load----> DEFERRED

local closure PASS --deterministic emit---------------------> candidate receipt
shared HOME venv/plugin --developer convenience only--------> NON_RECEIPT
missing/untracked/symlink/drift --fail closed----------------> no receipt
```

## Dependency and authority classes

| Class | Hermes surface |
|---|---|
| `ORG_NATIVE_BOOTSTRAP` | Package metadata, lockfile, core entrypoints, bootstrap/constants modules, validator, and test runner |
| `CHILD_OR_PROVIDER_OPTIONAL` | Provider extras, messaging integrations, optional skills, image/search/voice backends |
| `SHARED_PLATFORM_CONTRACT` | Reserved for a future locally versioned provider contract |
| `ASSEMBLED_WORKSPACE_ONLY` | User config, credentials, cached skills, global CLI, home venv, and home pytest plugin |

The isolated import probe sets an ephemeral `HOME` and `HERMES_HOME`, clears
secret-shaped environment variables, disables network connections, and blocks
optional-provider imports while proving they remain non-required. It checks that every protected console target
imports and resolves to a callable without creating user state.

## Commands

Developer compatibility remains available and explicitly reports provenance:

```bash
scripts/run_tests.sh tests/bootstrap/test_bootstrap_closure.py
scripts/run_tests.sh --local-venv-only tests/bootstrap/test_bootstrap_closure.py
scripts/run_tests.sh --allow-shared-venv tests/bootstrap/test_bootstrap_closure.py
```

Receipt-grade proof requires a clean repository-local environment:

```bash
uv venv .bootstrap-proof-venv --python 3.11
UV_PROJECT_ENVIRONMENT=.bootstrap-proof-venv \
  uv sync --locked --python .bootstrap-proof-venv/bin/python
.bootstrap-proof-venv/bin/python scripts/validate_hermes_bootstrap_closure.py \
  --root . --receipt-venv .bootstrap-proof-venv
UV_PROJECT_ENVIRONMENT=.bootstrap-proof-venv \
  uv sync --locked --python .bootstrap-proof-venv/bin/python --extra dev
HERMES_RECEIPT_VENV="$PWD/.bootstrap-proof-venv" \
  scripts/run_tests.sh --receipt-mode tests/bootstrap/test_bootstrap_closure.py
```

Receipt mode uses a temporary home, disables `pytest_live_guard`, and rejects
shared-home interpreter provenance rather than silently falling back. Candidate
receipts bind the source head/tree, manifest, validator, lockfile, metadata,
mandatory file digests, interpreter provenance, and isolated-smoke result.
Protected-main, CI-run, and exact-head independent review remain typed omissions
until a later observer verifies them.

## Failure, rollback, and non-claims

Any failed check produces a typed blocked state and no receipt. Revert the
bootstrap closure commit to roll back this admission layer; developer behavior
remains available through an explicitly non-receipt mode. Closure grants no API
credential/provider activation, gateway deployment, external call, release,
publication, production readiness, global installation, user-home mutation, or
branch-protection/review bypass.
