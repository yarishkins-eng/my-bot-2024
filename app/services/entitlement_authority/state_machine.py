from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Stage(StrEnum):
    PENDING = 'pending'
    CLAIMED = 'claimed'
    CREATING_DISABLED = 'creating_disabled'
    UUID_BOUND = 'uuid_bound'
    MUTATING = 'mutating'
    VERIFYING = 'verifying'
    VERIFIED = 'verified'
    READY = 'ready'
    REMOTE_OUTCOME_UNKNOWN = 'remote_outcome_unknown'
    QUARANTINED = 'quarantined'
    CANCELLED = 'cancelled'


@dataclass(frozen=True, slots=True)
class Transition:
    stage: Stage
    may_mutate: bool
    may_finalize: bool
    reason: str | None = None


def claim_transition(
    *,
    command_stage: Stage,
    command_generation: int,
    current_generation: int,
    identity_quarantined: bool,
    remote_outcome_unknown: bool,
    mutation_was_possible: bool,
) -> Transition:
    # A possibly-sent command is a global identity fence even after its local
    # generation becomes stale.  Cancelling it would let a newer generation
    # race a late server-side mutation that RemnaWave cannot CAS-exclude.
    if identity_quarantined or remote_outcome_unknown or mutation_was_possible:
        return Transition(Stage.QUARANTINED, False, False, 'observe_only_takeover')
    if command_generation != current_generation:
        return Transition(Stage.CANCELLED, False, False, 'stale_generation')
    if command_stage == Stage.PENDING:
        return Transition(Stage.CLAIMED, True, False)
    if command_stage == Stage.CLAIMED:
        return Transition(Stage.CLAIMED, True, False, 'same_lease_resume')
    return Transition(Stage.QUARANTINED, False, False, 'unexpected_claim_stage')


def finalize_transition(
    *,
    command_generation: int,
    current_generation: int,
    remote_outcome_unknown: bool,
    exact_canonical_match: bool,
) -> Transition:
    if command_generation != current_generation:
        return Transition(Stage.CANCELLED, False, False, 'stale_generation')
    if remote_outcome_unknown:
        return Transition(Stage.QUARANTINED, False, False, 'remote_outcome_unknown')
    if not exact_canonical_match:
        return Transition(Stage.QUARANTINED, False, False, 'canonical_mismatch')
    return Transition(Stage.READY, False, True)
