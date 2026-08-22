# Hermes V2 candidate observed-proof bindings

Status: plan-first follow-up to protected receipt-substrate merge `c813356f`.

## Finding

The protected canary is fail-closed, but its candidate JSON emits granular
`PASS` literals without naming the executed test node that established each
claim. It also lists the V1 closure suite among `proof_suites` even though V1
closure is executed by a separate workflow step.

## Bounded repair

1. Derive every proof result from the already validated workflow suite result.
2. Bind each granular proof to one exact executed pytest node.
3. List only the three suites executed by the V2 proof step.
4. Assert tracked executable-mode restoration in the existing deletion repair
   test.
5. Keep the artifact candidate-only and preserve all source, retry, authority,
   and strict-descendant publication boundaries.

No recovery implementation, policy, ruleset, provider, credential, release,
production, finance, science, brokerage, trading, or merge authority changes.
