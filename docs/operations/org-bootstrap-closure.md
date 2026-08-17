# Hermes Agent checkout-local bootstrap closure

Status: branch evidence for `HERMES_AGENT_BOOTSTRAP_CLOSURE_READY`.

## Scope

This proof covers the repository-owned development and execution entrypoints: the agent contract, project metadata, canonical test runner, core conversation loop, tool orchestration, toolset registry, and CLI.

Hermes may optionally reuse a user-home virtual environment or user-installed skills, but those surfaces are explicitly `ASSEMBLED_WORKSPACE_ONLY`. Their absence cannot make a clean source checkout structurally incomplete, and their presence cannot satisfy a missing repository file.

## Admission

The self-hosted CI lane validates the exact checkout and runs negative controls for missing, untracked, escaped, and externally satisfied bootstrap dependencies.

A future runtime-specific packet may add bundled skill registry requirements after identifying the exact pre-discovery set. This foundation intentionally does not classify every plugin or optional skill as bootstrap-critical.

## Non-claims

No model provider, API credential, plugin, gateway, messaging platform, release, deployment, production, or user-home installation authority is created.
