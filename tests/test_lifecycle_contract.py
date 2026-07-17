"""Focused contract tests for the inert Hermes lifecycle event surface."""

from dataclasses import replace

import pytest

from lifecycle_contract import (
    COMPATIBILITY_REFERENCE,
    DEFAULT_OFF_CAPABILITIES,
    LIFECYCLE_EVENT_TYPES,
    AuthorityClass,
    Capability,
    DefaultOffLifecycleAdapter,
    LifecycleEvent,
    LifecycleState,
    synthetic_lifecycle_event,
)


EXPECTED_EVENT_TYPES = frozenset({
    "epoch_created",
    "epoch_active",
    "handoff_created",
    "handoff_accepted",
    "handoff_rejected",
    "packet_refreshed",
    "epoch_pr_assembled",
    "epoch_pr_commit_observed",
    "release_verification_observed",
    "local_release_memory_requested",
    "learning_extraction_requested",
    "continuity_rolled_forward",
    "shared_candidate_proposed",
    "memory_equivalence_result",
    "packet_retirement_result",
})


def test_valid_synthetic_event_is_typed_and_inert():
    event = synthetic_lifecycle_event()

    assert isinstance(event, LifecycleEvent)
    assert event.event_type == "epoch_created"
    assert event.commit_sha == "a" * 40
    assert event.evidence_digest == "b" * 64
    assert event.non_claims
    assert event.schema_version == "1.0"
    assert DefaultOffLifecycleAdapter().emit(event) is False


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("commit_sha", "A" * 40),
        ("commit_sha", "a" * 39),
        ("evidence_digest", "b" * 63),
        ("evidence_digest", "B" * 64),
    ],
)
def test_invalid_commit_or_evidence_digest_is_rejected(field, value):
    with pytest.raises(ValueError):
        replace(synthetic_lifecycle_event(), **{field: value})


def test_default_off_structurally_denies_every_live_capability():
    adapter = DefaultOffLifecycleAdapter()

    assert set(DEFAULT_OFF_CAPABILITIES) == set(Capability)
    assert all(not DEFAULT_OFF_CAPABILITIES[capability] for capability in Capability)
    assert all(not adapter.allows(capability) for capability in Capability)
    assert {
        Capability.GBRAIN_WRITE,
        Capability.SCHEDULER,
        Capability.RELEASE_TRIGGER,
        Capability.PACKET_RETIREMENT,
        Capability.MERGE_EVIDENCE,
        Capability.RUNTIME_ACTIVATION,
    } == set(Capability)


def test_event_enumeration_is_exact_and_complete():
    assert LIFECYCLE_EVENT_TYPES == EXPECTED_EVENT_TYPES


def test_event_cannot_escalate_authority_and_requires_non_claims():
    event = synthetic_lifecycle_event()

    with pytest.raises(ValueError, match="non_claims"):
        replace(event, non_claims=())
    with pytest.raises(ValueError, match="authority"):
        replace(event, authority_class=AuthorityClass.EXECUTIVE)

    assert event.authority_class is AuthorityClass.OBSERVATION
    assert event.lifecycle_state is LifecycleState.OBSERVED


def test_compatibility_is_pinned_to_neoengine_799_head():
    assert COMPATIBILITY_REFERENCE.issue == "NeoEngine #799"
    assert COMPATIBILITY_REFERENCE.head == "96a051f2075aaece4f8741dae09425cbf2458a04"
    assert COMPATIBILITY_REFERENCE.schema_digest
