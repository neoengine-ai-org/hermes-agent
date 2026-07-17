"""Neutral, default-off lifecycle event contract.

This module is intentionally an inert schema boundary.  It records no events and
contains no integration with GBrain, schedulers, releases, retirement, merging,
or runtime activation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import re
from types import MappingProxyType
from typing import Final, Mapping, Protocol


LIFECYCLE_EVENT_TYPES: Final[frozenset[str]] = frozenset({
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

_SHA1_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")


class LifecycleState(str, Enum):
    """Neutral lifecycle states; these do not authorize an action."""

    OBSERVED = "observed"
    REQUESTED = "requested"
    RESULT = "result"


class AuthorityClass(str, Enum):
    """The contract intentionally permits only non-authorizing observation."""

    OBSERVATION = "observation"
    EXECUTIVE = "executive"


class Capability(str, Enum):
    """All potentially live effects explicitly denied by the default adapter."""

    GBRAIN_WRITE = "gbrain_write"
    SCHEDULER = "scheduler"
    RELEASE_TRIGGER = "release_trigger"
    PACKET_RETIREMENT = "packet_retirement"
    MERGE_EVIDENCE = "merge_evidence"
    RUNTIME_ACTIVATION = "runtime_activation"


DEFAULT_OFF_CAPABILITIES: Final[Mapping[Capability, bool]] = MappingProxyType({
    capability: False for capability in Capability
})


@dataclass(frozen=True, slots=True)
class CompatibilityReference:
    """Pin to the upstream neutral-contract source; do not silently fork it."""

    issue: str
    head: str
    schema_digest: str


COMPATIBILITY_REFERENCE: Final[CompatibilityReference] = CompatibilityReference(
    issue="NeoEngine #799",
    head="96a051f2075aaece4f8741dae09425cbf2458a04",
    # SHA-256 identifier for this contract's compatibility declaration.
    schema_digest="d5b0db302c0b3d5d664239264b44b9c275ea81b068b1438cfe8661324f551d3a",
)


@dataclass(frozen=True, slots=True)
class LifecycleEvent:
    """Typed, evidence-bound event declaration with no operational authority."""

    event_type: str
    organization: str
    repository: str
    epoch_id: str
    packet_id: str
    source_agent: str
    destination_agent: str
    branch: str
    commit_sha: str
    evidence_digest: str
    lifecycle_state: LifecycleState
    authority_class: AuthorityClass
    non_claims: tuple[str, ...]
    schema_version: str

    def __post_init__(self) -> None:
        if self.event_type not in LIFECYCLE_EVENT_TYPES:
            raise ValueError("event_type is not part of the lifecycle contract")
        for field_name in (
            "organization",
            "repository",
            "epoch_id",
            "packet_id",
            "source_agent",
            "destination_agent",
            "branch",
            "schema_version",
        ):
            if not getattr(self, field_name):
                raise ValueError(f"{field_name} is required")
        if not _SHA1_RE.fullmatch(self.commit_sha):
            raise ValueError("commit_sha must be an exact lowercase 40-character SHA")
        if not _SHA256_RE.fullmatch(self.evidence_digest):
            raise ValueError("evidence_digest must be a lowercase SHA-256")
        if self.authority_class is not AuthorityClass.OBSERVATION:
            raise ValueError(
                "authority escalation is prohibited by the neutral contract"
            )
        if not self.non_claims or any(not claim for claim in self.non_claims):
            raise ValueError("non_claims are required")


class LifecycleAdapter(Protocol):
    """Inert adapter boundary; implementations must declare capabilities."""

    def allows(self, capability: Capability) -> bool: ...

    def emit(self, event: LifecycleEvent) -> bool: ...


@dataclass(frozen=True, slots=True)
class DefaultOffLifecycleAdapter:
    """The sole shipped adapter: validates declarations but performs no write."""

    capabilities: Mapping[Capability, bool] = field(
        default_factory=lambda: DEFAULT_OFF_CAPABILITIES
    )

    def __post_init__(self) -> None:
        if dict(self.capabilities) != dict(DEFAULT_OFF_CAPABILITIES):
            raise ValueError("default-off adapter capabilities cannot be changed")

    def allows(self, capability: Capability) -> bool:
        return False

    def emit(self, event: LifecycleEvent) -> bool:
        """Accept schema-shaped input without persisting, triggering, or activating."""
        if not isinstance(event, LifecycleEvent):
            raise TypeError("event must be a LifecycleEvent")
        return False


def synthetic_lifecycle_event() -> LifecycleEvent:
    """A deterministic, non-live fixture for contract consumers and tests."""
    return LifecycleEvent(
        event_type="epoch_created",
        organization="example-org",
        repository="example-repository",
        epoch_id="synthetic-epoch",
        packet_id="synthetic-packet",
        source_agent="source-agent",
        destination_agent="destination-agent",
        branch="synthetic/branch",
        commit_sha="a" * 40,
        evidence_digest="b" * 64,
        lifecycle_state=LifecycleState.OBSERVED,
        authority_class=AuthorityClass.OBSERVATION,
        non_claims=("No action is authorized.",),
        schema_version="1.0",
    )


__all__ = [
    "AuthorityClass",
    "Capability",
    "COMPATIBILITY_REFERENCE",
    "DEFAULT_OFF_CAPABILITIES",
    "DefaultOffLifecycleAdapter",
    "LIFECYCLE_EVENT_TYPES",
    "LifecycleAdapter",
    "LifecycleEvent",
    "LifecycleState",
    "synthetic_lifecycle_event",
]
