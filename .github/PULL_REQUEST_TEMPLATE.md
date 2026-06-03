## What does this PR do?

<!-- Describe the change clearly. What problem does it solve? Why is this approach the right one? -->



## Related Issue

<!-- Link the issue this PR addresses. If no issue exists, consider creating one first. -->

Fixes #

## Type of Change

<!-- Check the one that applies. -->

- [ ] 🐛 Bug fix (non-breaking change that fixes an issue)
- [ ] ✨ New feature (non-breaking change that adds functionality)
- [ ] 🔒 Security fix
- [ ] 📝 Documentation update
- [ ] ✅ Tests (adding or improving test coverage)
- [ ] ♻️ Refactor (no behavior change)
- [ ] 🎯 New skill (bundled or hub)

## Changes Made

<!-- List the specific changes. Include file paths for code changes. -->

- 

## How to Test

<!-- Steps to verify this change works. For bugs: reproduction steps + proof that the fix works. -->

1. 
2. 
3. 

## Risk, Complexity, Review, and CI Classification

- Risk class: R0/R1/R2/R3/R4/R5
- Complexity class: C0/C1/C2/C3/C4/C5
- Impacted surfaces:
- runtime_payload_required: true/false
- RuntimePayloadContract present: yes/no
- maturityLevelTarget: v1/v2/v3/v4/v5/v6/v7/v8/v9/v10
- currentMaturityLevel: v1/v2/v3/v4/v5/v6/v7/v8/v9/v10
- targetMaturityLevel: v1/v2/v3/v4/v5/v6/v7/v8/v9/v10
- ralphLoopStage: runtime_intent/acceptance_boundary/implementation_pass/adversarial_pass/runtime_proof_pass/memory_closeout_pass
- productSurfaceTarget:
- runtimeBehaviorDelta: <!-- What can the system do after this that it could not do before? -->
- userOrSystemVisibleDelta:
- validationTestDelta:
- runtimeProofRequired:
- finiteCloseCondition:
- supportWorkClassification: <!-- N/A or evidence-only/rubric-only/maturity-only/governance-only/closeout-only/adversarial-only/scaffolding-only/planning-only -->
- Bound runtime-bearing packet: <!-- required for support-only work -->
- runtimeBlockerExemption: <!-- only if runtime work is impossible -->
- nonClaims:
- nextRuntimePacketRecommendation:
- Product/runtime artifact:
- Changed runtime/product files:
- Behavior tests:
- Validation commands:
- Explicit non-claims:
- Blocker exemption, if any:
- Secondary review required: yes/no
- Adversarial review required: yes/no
- Opposite-provider adversarial required: yes/no
- Human/protected review required: yes/no
- Founder review required: yes/no
- Required CI lanes:
- Skipped CI lanes and rationale:
- Token class: S/M/L/XL
- Expected state change:
- Stop condition:

## Checklist

<!-- Complete these before requesting review. -->

### Code

- [ ] I've read the [Contributing Guide](https://github.com/NousResearch/hermes-agent/blob/main/CONTRIBUTING.md)
- [ ] My commit messages follow [Conventional Commits](https://www.conventionalcommits.org/) (`fix(scope):`, `feat(scope):`, etc.)
- [ ] I searched for [existing PRs](https://github.com/NousResearch/hermes-agent/pulls) to make sure this isn't a duplicate
- [ ] My PR contains **only** changes related to this fix/feature (no unrelated commits)
- [ ] I've run `pytest tests/ -q` and all tests pass
- [ ] I've added tests for my changes (required for bug fixes, strongly encouraged for features)
- [ ] I've tested on my platform: <!-- e.g. Ubuntu 24.04, macOS 15.2, Windows 11 -->

### Documentation & Housekeeping

<!-- Check all that apply. It's OK to check "N/A" if a category doesn't apply to your change. -->

- [ ] I've updated relevant documentation (README, `docs/`, docstrings) — or N/A
- [ ] I've updated `cli-config.yaml.example` if I added/changed config keys — or N/A
- [ ] I've updated `CONTRIBUTING.md` or `AGENTS.md` if I changed architecture or workflows — or N/A
- [ ] I've considered cross-platform impact (Windows, macOS) per the [compatibility guide](https://github.com/NousResearch/hermes-agent/blob/main/CONTRIBUTING.md#cross-platform-compatibility) — or N/A
- [ ] I've updated tool descriptions/schemas if I changed tool behavior — or N/A

## For New Skills

<!-- Only fill this out if you're adding a skill. Delete this section otherwise. -->

- [ ] This skill is **broadly useful** to most users (if bundled) — see [Contributing Guide](https://github.com/NousResearch/hermes-agent/blob/main/CONTRIBUTING.md#should-the-skill-be-bundled)
- [ ] SKILL.md follows the [standard format](https://github.com/NousResearch/hermes-agent/blob/main/CONTRIBUTING.md#skillmd-format) (frontmatter, trigger conditions, steps, pitfalls)
- [ ] No external dependencies that aren't already available (prefer stdlib, curl, existing Hermes tools)
- [ ] I've tested the skill end-to-end: `hermes --toolsets skills -q "Use the X skill to do Y"`

## Screenshots / Logs

<!-- If applicable, add screenshots or log output showing the fix/feature in action. -->

