from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime

from .types import EntitlementSnapshot


HARD_DENIES = ('erasure', 'delete', 'reset', 'admin_block', 'channel_deny')
_PRECEDENCE = (*HARD_DENIES, 'limited', 'grace')
_LIMITED_CLEAR_CODES = {'traffic_increase', 'traffic_reset', 'admin_clear'}


@dataclass(frozen=True, slots=True)
class AuthoritySource:
    source_key: str
    snapshot: EntitlementSnapshot
    authorized: bool = True


@dataclass(frozen=True, slots=True)
class Overlay:
    overlay_type: str
    generation: int
    epoch: int
    active: bool = True
    expires_at: datetime | None = None
    resolution_code: str | None = None


@dataclass(frozen=True, slots=True)
class Reduction:
    snapshot: EntitlementSnapshot | None
    state: str
    blocks_projection: bool
    financial_review: bool
    reason: str | None = None


def reduce_authority(
    sources: list[AuthoritySource],
    overlays: list[Overlay],
    *,
    verified_generation: int | None,
    now: datetime,
) -> Reduction:
    authorized = [source for source in sources if source.authorized]
    if not authorized:
        return Reduction(None, 'unknown', True, False, 'no_authorized_source')
    generation = max(source.snapshot.generation for source in authorized)
    current = [source for source in authorized if source.snapshot.generation == generation]
    hashes = {source.snapshot.desired_hash for source in current}
    if len(hashes) != 1:
        return Reduction(None, 'quarantined', True, False, 'provenance_conflict')
    snapshot = current[0].snapshot
    active = [
        overlay
        for overlay in overlays
        if overlay.active
        and overlay.generation <= generation
        and (overlay.overlay_type == 'limited' or overlay.expires_at is None or overlay.expires_at > now)
    ]
    invalid_limited_clear = any(
        overlay.overlay_type == 'limited' and not overlay.active and overlay.resolution_code not in _LIMITED_CLEAR_CODES
        for overlay in overlays
    )
    if invalid_limited_clear:
        return Reduction(None, 'quarantined', True, False, 'invalid_limited_clear')

    financial = any(overlay.overlay_type == 'financial_review_hold' for overlay in active)
    if financial and verified_generation != generation:
        return Reduction(snapshot, 'financial_review_hold', True, True, 'pre_verified_reversal')

    active_types = {overlay.overlay_type for overlay in active}
    selected = next((kind for kind in _PRECEDENCE if kind in active_types), None)
    if selected in HARD_DENIES:
        return Reduction(
            replace(snapshot, status='DISABLED', deny_overlays=tuple(sorted(active_types))),
            selected,
            False,
            financial,
        )
    if selected == 'limited':
        return Reduction(
            replace(snapshot, status='LIMITED', deny_overlays=tuple(sorted(active_types))),
            'limited',
            False,
            financial,
        )
    if selected == 'grace':
        return Reduction(
            replace(snapshot, status='ACTIVE', deny_overlays=tuple(sorted(active_types))),
            'grace',
            False,
            financial,
        )
    return Reduction(snapshot, 'authorized', False, financial)
