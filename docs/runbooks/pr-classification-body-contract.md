# Hermes PR classification body contract

Hermes PRs must include a structured classification section before they can claim review or merge readiness.

## Required body blocks

- `## Risk, Complexity, Review, and CI Classification`
- `## Policy decision output`
- `## Non-claims`
- `## RuntimePayloadContract` for runtime or protected-surface changes

## Required classification fields

- Risk class
- Complexity class
- Impacted surfaces
- Required CI lanes
- Required reviews
- RuntimePayloadContract present
- Blocker exemption, if any
- Expected state change

## Local validation

Run the classifier or policy adapter before claiming readiness:

```bash
python3 scripts/ci/org_enablement_policy_check.py --changed-files /tmp/changed-files.txt --repo hermes-agent --json
uv run --with pytest --with pytest-timeout python -m pytest tests/test_ci_risk_classifier.py -q
```

Do not rely on auto-merge until the classifier and repo-specific checks have completed for the current head. If GitHub branch-protection settings cannot be verified locally, say so explicitly.
