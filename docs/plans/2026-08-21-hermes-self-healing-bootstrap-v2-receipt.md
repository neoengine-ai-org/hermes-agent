# Hermes self-healing bootstrap V2 receipt lane

## Objective

Turn the protected source merge `518c00b34eb2df7f550a0791bb9c5b657ec38071` into protected-main runtime evidence and a strict-descendant canonical product receipt.

## Reused proof substrate

The receipt lane must execute the already-merged production proof rather than create a second recovery implementation:

- `scripts/bootstrap_stage0_v2.py`
- `scripts/bootstrap_resolver_v2_hardened.py`
- `scripts/bootstrap_resolver_v2_final.py`
- `tests/bootstrap/test_self_healing_bootstrap_v2.py`
- `tests/bootstrap/test_self_healing_bootstrap_v2_final_hardening.py`
- `config/hermes-bootstrap-acquisition-v2.json`
- `config/hermes-bootstrap-acquisition-v2.sha256`

The existing tests cover exact policy pinning, fixed operation selection, deleted Stage-1 recovery, exact tracked mode, malformed lock payload, abrupt process death, live lock ownership, corrupt fingerprinted environment rebuild, exactly-one retry, and healthy no-op.

## Landing order

1. Land a read-only protected receipt workflow and candidate receipt compiler.
2. Read back the protected merge commit and tree.
3. Require a protected-main push run to execute the complete V2 proof suite.
4. Bind the successful run/job/artifact and source ancestry into `evidence/bootstrap-closure/receipt-v1.json` in a strict-descendant evidence-only PR.
5. Merge that receipt through ordinary protected checks and read it back.

## Non-claims

This lane grants no shared-home environment authority, arbitrary package source, provider or credential authority, release, deployment, production, customer-data, finance, science, brokerage, capital, order, position, trading, branch-protection bypass, or automatic merge authority.
