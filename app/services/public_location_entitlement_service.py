"""Fail-closed public-location entitlement resolver.

This module is the only place that translates a business location into a
technical RemnaWave Internal Squad.  Routes may expose the DTOs below, but may
not accept or return the technical UUIDs used by the panel.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from hashlib import sha256

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import (
    PublicLocation,
    PublicLocationSquadMapping,
    SubscriptionEntitlementSnapshot,
    Tariff,
    TariffLegacyEntitlementManifest,
    TariffLocationEntitlement,
)


class EntitlementResolutionError(ValueError):
    """A fail-closed entitlement policy or mapping error."""


@dataclass(frozen=True)
class ResolvedEntitlement:
    location_ids: tuple[str, ...]
    squad_uuids: tuple[str, ...]
    policy_revision: int
    provenance: str

    def snapshot_payload(self) -> dict[str, object]:
        return {
            'location_ids': list(self.location_ids),
            'technical_squad_uuids': list(self.squad_uuids),
            'policy_revision': self.policy_revision,
            'provenance': self.provenance,
        }

    @property
    def snapshot_hash(self) -> str:
        raw = json.dumps(self.snapshot_payload(), sort_keys=True, separators=(',', ':')).encode()
        return sha256(raw).hexdigest()


def _dedupe(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(str(value) for value in values if value))


async def list_tariff_assignable_locations(db: AsyncSession) -> list[PublicLocation]:
    result = await db.execute(
        select(PublicLocation)
        .where(
            PublicLocation.tariff_assignable.is_(True),
            PublicLocation.lifecycle.in_(('ready', 'published')),
            PublicLocation.visibility == 'visible',
            PublicLocation.health == 'healthy',
        )
        .order_by(PublicLocation.sort_order, PublicLocation.label_en)
    )
    return list(result.scalars())


async def _legacy_snapshot(db: AsyncSession, tariff: Tariff) -> ResolvedEntitlement:
    manifest = await db.get(TariffLegacyEntitlementManifest, tariff.id)
    if not manifest:
        raise EntitlementResolutionError('legacy tariff has no owner-approved entitlement manifest')
    approved = _dedupe(manifest.squad_uuids or [])
    current = _dedupe(tariff.allowed_squads or [])
    if not approved or current != approved:
        raise EntitlementResolutionError('legacy tariff UUIDs do not exactly match its approved manifest')
    return ResolvedEntitlement((), approved, int(tariff.location_policy_revision or 1), 'legacy_manifest')


async def resolve_tariff_entitlement(
    db: AsyncSession,
    tariff: Tariff,
    *,
    selected_location_ids: Iterable[str] | None = None,
    exception_location_ids: Iterable[str] | None = None,
    for_new_issuance: bool = True,
) -> ResolvedEntitlement:
    """Resolve one tariff to exact, safe internal squads.

    ``selected_location_ids`` can only narrow the tariff policy.  Exceptions
    follow the same rule, so neither input can create an expansion bypass.
    Hidden/deprecated/retired/unhealthy/incomplete mappings are rejected before
    a caller can charge money, mutate a subscription, or call the Panel.
    """

    # A persisted Tariff always has this non-null column after revision 0100.
    # ``getattr`` keeps the error deterministic for malformed in-memory inputs
    # (including legacy test doubles) instead of leaking an AttributeError.
    mode = getattr(tariff, 'entitlement_mode', None) or 'legacy_snapshot'
    if mode == 'no_locations':
        raise EntitlementResolutionError('this tariff has no sellable locations')
    if mode == 'legacy_snapshot':
        return await _legacy_snapshot(db, tariff)
    if mode != 'location_managed':
        raise EntitlementResolutionError('unknown tariff entitlement mode')

    rows = await db.execute(
        select(TariffLocationEntitlement, PublicLocation, PublicLocationSquadMapping)
        .join(PublicLocation, PublicLocation.id == TariffLocationEntitlement.public_location_id)
        .join(PublicLocationSquadMapping, PublicLocationSquadMapping.public_location_id == PublicLocation.id)
        .where(TariffLocationEntitlement.tariff_id == tariff.id)
    )
    resolved_rows = list(rows.all())
    if not resolved_rows:
        raise EntitlementResolutionError('location-managed tariff has no explicit mappings')

    policy_ids = _dedupe(location.id for _entitlement, location, _mapping in resolved_rows)
    requested = _dedupe(selected_location_ids or policy_ids)
    exceptions = _dedupe(exception_location_ids or requested)
    if not set(requested).issubset(policy_ids) or not set(exceptions).issubset(policy_ids):
        raise EntitlementResolutionError('location selection or exception expands tariff policy')
    effective = set(requested).intersection(exceptions)
    if not effective:
        raise EntitlementResolutionError('empty location entitlement is not sellable')

    squads: list[str] = []
    mapped_location_ids: set[str] = set()
    for _entitlement, location, mapping in resolved_rows:
        if location.id not in effective:
            continue
        if for_new_issuance and (
            location.visibility != 'visible'
            or location.lifecycle not in ('ready', 'published')
            or location.health != 'healthy'
        ):
            raise EntitlementResolutionError('location is not available for new issuance')
        if not mapping.is_dedicated_verified or not mapping.internal_squad_uuid:
            raise EntitlementResolutionError('location does not have a verified dedicated squad mapping')
        mapped_location_ids.add(location.id)
        squads.append(mapping.internal_squad_uuid)

    deduped_squads = _dedupe(squads)
    if mapped_location_ids != effective or not deduped_squads:
        raise EntitlementResolutionError('location mapping is ambiguous or incomplete')
    return ResolvedEntitlement(
        tuple(sorted(effective)),
        deduped_squads,
        int(tariff.location_policy_revision or 1),
        'tariff',
    )


async def persist_subscription_entitlement_snapshot(
    db: AsyncSession,
    subscription_id: int,
    tariff_id: int | None,
    entitlement: ResolvedEntitlement,
) -> SubscriptionEntitlementSnapshot:
    """Store first resolved evidence; updates deliberately fail closed."""

    existing = await db.scalar(
        select(SubscriptionEntitlementSnapshot).where(
            SubscriptionEntitlementSnapshot.subscription_id == subscription_id
        )
    )
    if existing:
        if existing.snapshot_hash != entitlement.snapshot_hash:
            raise EntitlementResolutionError('subscription entitlement snapshot is immutable')
        return existing
    snapshot = SubscriptionEntitlementSnapshot(
        subscription_id=subscription_id,
        tariff_id=tariff_id,
        location_ids=list(entitlement.location_ids),
        technical_squad_uuids=list(entitlement.squad_uuids),
        policy_revision=entitlement.policy_revision,
        provenance=entitlement.provenance,
        snapshot_hash=entitlement.snapshot_hash,
    )
    db.add(snapshot)
    return snapshot


async def get_subscription_entitlement_squads(db: AsyncSession, subscription_id: int) -> tuple[str, ...]:
    """Return immutable entitlement evidence for restore/renewal paths.

    A lifecycle change must never re-resolve the current tariff policy for an
    already-issued subscription.  Missing legacy evidence is intentionally a
    manual-reconcile condition, not permission to choose arbitrary squads.
    """

    entitlement = await get_subscription_resolved_entitlement(db, subscription_id)
    return entitlement.squad_uuids


async def get_subscription_resolved_entitlement(db: AsyncSession, subscription_id: int) -> ResolvedEntitlement:
    """Load and validate immutable entitlement evidence for an issued subscription."""

    snapshot = await db.scalar(
        select(SubscriptionEntitlementSnapshot).where(
            SubscriptionEntitlementSnapshot.subscription_id == subscription_id
        )
    )
    if snapshot is None:
        raise EntitlementResolutionError('subscription has no immutable entitlement snapshot')
    entitlement = ResolvedEntitlement(
        _dedupe(snapshot.location_ids or []),
        _dedupe(snapshot.technical_squad_uuids or []),
        int(snapshot.policy_revision),
        str(snapshot.provenance),
    )
    if not entitlement.squad_uuids or entitlement.snapshot_hash != snapshot.snapshot_hash:
        raise EntitlementResolutionError('subscription entitlement snapshot is invalid')
    return entitlement
