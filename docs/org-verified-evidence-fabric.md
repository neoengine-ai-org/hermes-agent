# Org Verified Evidence Fabric

The evidence fabric turns delivery claims into verified operating truth for NeoEngine, NeoWealth, and future orgs.

## Invariants

- No claim verifies itself: agent receipts are candidate evidence until the verifier promotes them.
- Candidate evidence is useful signal, but only verified events change projections.
- Every promoted event binds to an exact subject: org, repo, PR number, head SHA, base SHA, lane, verifier version, and timestamp.
- Evidence decays when the subject moves: old-head receipts become stale and create current-head proof debt.
- Absence of proof is explicit state: missing sidecar/current-head/Mac App evidence is projected as proof debt.
- Dashboards/watchdogs should read projections or canonical events, never raw agent narrative.
- Release readiness is computed from verified evidence; accepted/landed/deployed claims remain forbidden while proof debt or contradictions exist.

## Directory contract

```text
~/.hermes/state/org-evidence/
  inbox/<org>/candidate/          # agent closeout receipts
  events/<org>/events.jsonl       # canonical verified event ledger
  rejected/<org>/                 # checked but insufficient receipts
  stale/<org>/                    # receipts invalidated by subject movement
  superseded/<org>/               # promoted receipts already consumed
  projections/<org>/              # product-progress, proof-debt, contradiction, readiness views
  verifier-runs/<org>/            # verifier run receipts
```

## Agent closeout

Agents write receipts with `scripts/hermes_progress.py receipt` or by calling `write_agent_closeout_receipt()`.

Example:

```bash
scripts/hermes_progress.py receipt \
  --org neoengine \
  --repo neoengine-ai-org/neoengine \
  --pr 726 \
  --head "$HEAD" \
  --base "$BASE" \
  --lane NE-SONNET-01 \
  --agent sonnet \
  --work-packet roadmap-os-726-closeout \
  --claim PR_REBASED \
  --non-claim not_merged \
  --non-claim not_live \
  --non-claim not_accepted \
  --non-claim not_landed
```

## Verification

A no-agent verifier consumes candidate receipts, checks live subject state, promotes/rejects/stales receipts, writes projections, and emits proof debt.

```bash
scripts/hermes_progress.py verify \
  --policy neoengine=neoengine_local/org_evidence_policies/neoengine.policy.json \
  --policy neowealth=neoengine_local/org_evidence_policies/neowealth.policy.json \
  --live-state /path/to/live-state.json
```

Production wrappers should build `live-state.json` from GitHub PR/check APIs, deployment APIs, and local runtime receipts. Tests inject live state directly so verifier behavior stays deterministic.

## Watchdog integration

Founder/CEO/CTO watchdogs should read:

- `projections/<org>/product-progress-proof.json`
- `projections/<org>/proof-debt.json`
- `projections/<org>/contradiction-ledger.json`
- `projections/<org>/release-readiness.json`

They should report material deltas only: critical-path rung advancement/regression, proof debt changes, contradictions, protected-boundary events, blocker closure, and delivery-rung advancement.

## Current policy seeds

- `neoengine.policy.json` seeds Roadmap OS #726 as critical path, #729 as diagnostic, #722 as downstream, and OCR #735–#740 as contained.
- `neowealth.policy.json` seeds NeoWealth PR #625 as a product capability proof-ladder item.

These are policy seeds, not permanent truth. Update them through policy-versioned PRs as critical paths change.
