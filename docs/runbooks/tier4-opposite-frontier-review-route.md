# Tier-4 opposite-frontier review route via Copilot Anthropic

This runbook documents a proven Tier-4 review route for OpenAI-family primary
builder work when direct Claude CLI authentication is unavailable: use the
GitHub Copilot provider with an Anthropic-family model.

The route was proven on PR #23 as a precedent for the routing pattern only:
https://github.com/neoengine-ai-org/hermes-agent/pull/23#issuecomment-2947737849

Do not reuse PR #23's receipt for future PRs. Every PR, and every changed head
SHA, needs its own review and receipt.

## When to use this route

Use this route only when a PR is classified Tier 4 and the primary builder
family is OpenAI. It satisfies the opposite-frontier requirement because the
reviewer model family is Anthropic while the primary builder family is OpenAI.

Do not use OpenAI/Codex as the opposite-frontier reviewer for OpenAI-built work.
Codex can be an engineering reviewer, but it is not opposite-frontier to an
OpenAI primary builder.

## Route details

Use these receipt values when this exact route completed the review:

```yaml
review_type: opposite_frontier
provider: copilot
model: claude-haiku-4.5
provider_family: anthropic
reviewer_family: anthropic
primary_builder_family: openai
family_relation: opposite_frontier
same_provider_fallback: no
```

Fill all remaining receipt fields from the actual review event. Do not fabricate
or infer a completed review from an attempted run, an auth failure, a model list,
or a comment that review was requested.

## Discover and smoke-test available provider routes

From the repository root, inspect the available credentials and confirm Copilot
is usable before requesting a Tier-4 review:

```bash
python3 -m hermes_cli.main auth list
python3 -m hermes_cli.main auth list copilot
python3 -m hermes_cli.main --provider copilot -m claude-haiku-4.5 -z \
  "Reply with exactly: copilot anthropic route available"
```

If `claude-haiku-4.5` is unavailable, do not silently substitute an OpenAI model
and call it opposite-frontier. Find another valid Anthropic-family route or park
the PR with a clear blocker.

## Evidence comment pattern

Post a durable PR comment with the completed review evidence. The comment should
include:

- Reviewer route: `provider=copilot`, `model=claude-haiku-4.5`
- Families: `provider_family=anthropic`, `reviewer_family=anthropic`,
  `primary_builder_family=openai`, `family_relation=opposite_frontier`
- PR number reviewed
- Exact `head_sha_reviewed`
- Exact `base_sha_reviewed` or `unknown` only when the validator/policy allows it
- Verdict and material findings
- Unresolved blockers, if any
- Review timestamp
- Link to the evidence comment or transcript

Example receipt block to paste into the PR body after the review completes:

```yaml
- review_type: opposite_frontier
- provider: copilot
- model: claude-haiku-4.5
- provider_family: anthropic
- reviewer_family: anthropic
- primary_builder_family: openai
- family_relation: opposite_frontier
- reviewer_identity: <reviewer/session identity>
- same_provider_fallback: no
- fallback_reason:
- pr_reviewed: <PR number>
- head_sha_reviewed: <exact reviewed head SHA>
- base_sha_reviewed: <exact base SHA or unknown if permitted>
- verdict: PASS/PASS_WITH_CAVEATS/ACCEPTED_WITH_FINDINGS
- material_findings: <summary or none>
- unresolved_blockers: <none or list>
- protected_claims_checked: <claims checked>
- review_timestamp: <UTC timestamp>
- evidence_url_or_path: <PR evidence comment URL or durable path>
```

## Head SHA freshness rules

The `head_sha_reviewed` must match the current PR head at validation and merge
time. After any code, policy, template, or documentation change to the branch,
repeat the Tier-4 review and replace the receipt with the new exact head SHA.

A stale receipt is a blocker. Prior evidence may be useful background, but it is
not a reusable receipt for the new head.

## Local validation commands

Before claiming readiness, rerun the local classifier and policy checks for the
current changed-file set:

```bash
git diff --name-only origin/main...HEAD > /tmp/changed-files.txt
python3 scripts/ci/org_enablement_policy_check.py \
  --changed-files /tmp/changed-files.txt \
  --repo hermes-agent \
  --json
python3 scripts/ci/test_org_enablement_policy_check.py
python3 scripts/ci/check_org_policy_rules_hash.py --json
uv run --with pytest --with pytest-timeout python -m pytest \
  tests/test_ci_risk_classifier.py \
  tests/test_review_receipt_validator.py \
  -q
```

If GitHub branch protection or CI status cannot be verified locally, state that
explicitly and wait for GitHub checks before merge.

## Non-claims

- This route does not bypass branch protection.
- This route does not weaken the receipt validator or review policy.
- This route does not make failed Claude CLI authentication acceptable evidence.
- This route does not make OpenAI/Codex opposite-frontier for OpenAI-built work.
- This route does not permit fabricated, stale, or cross-PR receipts.
