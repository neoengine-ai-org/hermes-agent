# Hermes Codex role topology v1

## Authority and source lineage

This recipient-local source packet implements the protected semantics owned
by `neoengine-ai-org/ai-org#543` and landed through PR
`neoengine-ai-org/ai-org#555`.

```text
canonical_source_head=231d6f119fe8ddf5558e449099d330f2cdac8747
canonical_protected_merge=7e68823504a6cc329ef4554362e1175e48cfae2b
parent_controller=neoengine-ai-org/ai-org#554
recipient_owner=Hermes PR owner (Issues are disabled)
product_effect_mode=OFF
runtime_admitted=false
```

The source pin is provenance, not permission authority. The active host and
explicit launcher remain the only owners of effective runtime permissions,
selected implementation model, filesystem class, and approval dialogs.

## Disjoint roles

| Role | Purpose | Admitted route | Substantive work |
|---|---|---|---|
| `custom` | `TRACKED_THREAD_BOOTSTRAP_AND_RECOVERY` | admitted Desktop tracked-thread bootstrap routes | never |
| `hermes_worker` | `NATIVE_IMPLEMENTATION_AND_SOURCE_CONVERGENCE` | `NATIVE_SPAWN_AGENT` | one bounded WorkNode |

The first automatic tracked `custom` turn emits only
`RUNTIME_BOOTSTRAP_RECEIPT_ONLY` with `commands=0`. A genuinely fresh second
bootstrap turn may run one harmless scoped read canary and emit
`TRACKED_CHILD_SECOND_TURN_RUNTIME_VERIFIED`. Substantive implementation is
always routed through native `spawn_agent` with
`agent_type=hermes_worker` and an explicit admitted implementation model.

Both roles set `request_permissions_tool=false`. Neither role/config file
pins model, reasoning effort, approval policy, sandbox, filesystem,
writable roots, network, permission profile, or product authority.
Delegation-tool approval is recorded separately from child active-turn
runtime. A host dialog or `waitingOnApproval` produces one typed,
deduplicated `HOST_OWNED_APPROVAL_STALL`; it is never repaired with repeated
generic authorization prose.

## Evidence boundary

Source tests prove only source semantics. They do not prove an installed
Codex/Desktop version, native `agent_type` availability, effective child
role/model, approval policy, filesystem class, first/second-turn execution,
or dialog absence. Those facts require a genuinely fresh recipient session
and trusted parent-side host events. Worker self-report and repository
receipts are rejected as runtime trust roots.

Existing workers are not retroactively changed by a protected merge.
Inventory and renew them only at safe checkpoints after trusted runtime
inspection. `APP_SERVER_EXPLICIT_THREAD` remains `PLAN_ONLY`.

## Product boundary

tool execution, provider routing, credentials, deployment, production, and user-private state remain separately gated.

No source or runtime receipt grants deployment, production, private-state,
financial, science, trading, order, position, capital, or other protected
authority that is not separately admitted by Hermes's local policy.

## Validation and landing

```bash
pytest -q tests/test_codex_role_topology.py
```

The focused test is followed by recipient-native CI/Qwen, independent
exact-head adversarial review, normal expected-head protected merge, signed
protected-main readback, and then fresh-session host canaries. Until those
host canaries pass, the recipient remains `SOURCE_ONLY`.
