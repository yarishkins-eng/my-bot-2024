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

from app.database.crud.server_squad import get_effective_tariff_squad_uuids
from app.database.models import (
    PublicAccessPoint,
    PublicAccessPointSquadMapping,
    PublicLocation,
    PublicLocationSquadMapping,
    Subscription,
    SubscriptionEntitlementSnapshot,
    SubscriptionEntitlementTerm,
    Tariff,
    TariffAccessPointPolicyItem,
    TariffAccessPointPolicyRevision,
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
    inventory_fingerprint: str | None = None

    def snapshot_payload(self) -> dict[str, object]:
        payload = {
            'location_ids': list(self.location_ids),
            'technical_squad_uuids': list(self.squad_uuids),
            'policy_revision': self.policy_revision,
            'provenance': self.provenance,
        }
        if self.inventory_fingerprint is not None:
            payload['inventory_fingerprint'] = self.inventory_fingerprint
        return payload

    @property
    def snapshot_hash(self) -> str:
        raw = json.dumps(self.snapshot_payload(), sort_keys=True, separators=(',', ':')).encode()
        return sha256(raw).hexdigest()


def _dedupe(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(str(value) for value in values if value))


def _access_point_policy_inventory_fingerprint(points: Iterable[PublicAccessPoint]) -> str:
    """Rebuild the policy fence from point-local entitlement evidence."""

    payload = {
        'points': [
            {
                'id': point.id,
                'entitlement_fingerprint': point.inventory_fingerprint,
                # Pair graph evidence with the append-only catalog epoch. A
                # verified→unsafe→verified ABA transition must not revive an
                # invoice or policy revision that was valid before it.
                'entitlement_revision': int(getattr(point, 'entitlement_revision', 0) or 0),
            }
            for point in sorted(points, key=lambda item: item.id)
        ]
    }
    return sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


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


async def _native_squads(db: AsyncSession, tariff: Tariff) -> ResolvedEntitlement:
    """Resolve the original Bedolaga tariff contract without AP evidence.

    An empty list deliberately retains upstream semantics: all locally
    available Internal Squads.  The caller must use an explicit list during a
    country cutover; this fallback exists for old generic tariffs only.
    """

    try:
        squads = await get_effective_tariff_squad_uuids(db, tariff.allowed_squads)
    except ValueError as exc:
        raise EntitlementResolutionError(str(exc)) from exc
    if not squads:
        raise EntitlementResolutionError('native tariff has no available Internal Squads')
    return ResolvedEntitlement((), _dedupe(squads), 0, 'native_squads')


async def _access_point_policy(
    db: AsyncSession,
    tariff: Tariff,
    *,
    selected_access_point_ids: Iterable[str] | None,
    exception_access_point_ids: Iterable[str] | None,
    for_new_issuance: bool,
    lock_evidence: bool = False,
) -> ResolvedEntitlement:
    """Resolve an append-only Host-title policy without falling back to raw squads."""

    policy_revision = int(getattr(tariff, 'access_point_policy_revision', 0) or 0)
    if policy_revision <= 0:
        raise EntitlementResolutionError('access-point tariff has no active policy revision')
    policy_query = select(TariffAccessPointPolicyRevision).where(
        TariffAccessPointPolicyRevision.tariff_id == tariff.id,
        TariffAccessPointPolicyRevision.revision == policy_revision,
    )
    if lock_evidence:
        policy_query = policy_query.with_for_update()
    policy = await db.scalar(policy_query)
    if policy is None:
        raise EntitlementResolutionError('access-point policy revision is missing')

    rows_query = (
        select(TariffAccessPointPolicyItem, PublicAccessPoint, PublicAccessPointSquadMapping)
        .join(
            PublicAccessPoint,
            PublicAccessPoint.id == TariffAccessPointPolicyItem.public_access_point_id,
        )
        .join(
            PublicAccessPointSquadMapping,
            PublicAccessPointSquadMapping.public_access_point_id == PublicAccessPoint.id,
        )
        .where(
            TariffAccessPointPolicyItem.policy_revision_id == policy.id,
            PublicAccessPointSquadMapping.is_current.is_(True),
        )
    )
    if lock_evidence:
        # The final quote-to-money transition keeps these locks until its
        # transaction commits.  Catalog discovery/update then blocks on the
        # same rows instead of invalidating evidence between the recheck and
        # wallet debit/provider invoice.
        rows_query = rows_query.with_for_update()
    rows = await db.execute(rows_query)
    resolved_rows = list(rows.all())
    if not resolved_rows:
        raise EntitlementResolutionError('access-point policy has no verified mappings')

    policy_ids = _dedupe(point.id for _item, point, _mapping in resolved_rows)
    requested = _dedupe(selected_access_point_ids or policy_ids)
    exceptions = _dedupe(exception_access_point_ids or requested)
    if not set(requested).issubset(policy_ids) or not set(exceptions).issubset(policy_ids):
        raise EntitlementResolutionError('access-point selection or exception expands tariff policy')
    effective = set(requested).intersection(exceptions)
    if not effective:
        raise EntitlementResolutionError('empty access-point entitlement is not sellable')

    squad_keys: list[str] = []
    mapped_ids: set[str] = set()
    effective_points: dict[str, PublicAccessPoint] = {}
    for _item, point, mapping in resolved_rows:
        if point.id not in effective:
            continue
        if (
            point.state != 'verified'
            or not point.tariff_assignable
            or not point.title.strip()
            or not point.inventory_fingerprint
        ):
            raise EntitlementResolutionError('access point is unavailable or inventory drifted')
        if (
            not mapping.is_dedicated_verified
            or not mapping.internal_squad_key
            or mapping.graph_fingerprint != point.graph_fingerprint
            or mapping.inventory_revision != point.inventory_revision
        ):
            raise EntitlementResolutionError('access point does not have a current verified dedicated mapping')
        mapped_ids.add(point.id)
        effective_points[point.id] = point
        squad_keys.append(mapping.internal_squad_key)

    deduped_squads = _dedupe(squad_keys)
    if (
        mapped_ids != effective
        or not deduped_squads
        or policy.inventory_fingerprint != _access_point_policy_inventory_fingerprint(list(effective_points.values()))
    ):
        raise EntitlementResolutionError('access-point mapping is ambiguous or incomplete')
    return ResolvedEntitlement(
        tuple(sorted(effective)),
        deduped_squads,
        policy_revision,
        'access_point_policy',
        policy.inventory_fingerprint,
    )


async def resolve_tariff_entitlement(
    db: AsyncSession,
    tariff: Tariff,
    *,
    selected_location_ids: Iterable[str] | None = None,
    exception_location_ids: Iterable[str] | None = None,
    for_new_issuance: bool = True,
    access_point_quote_context: bool = False,
    lock_access_point_evidence: bool = False,
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
    if mode == 'native_squads':
        return await _native_squads(db, tariff)
    if mode == 'no_locations':
        raise EntitlementResolutionError('this tariff has no sellable locations')
    if mode == 'legacy_snapshot':
        return await _legacy_snapshot(db, tariff)
    if mode == 'access_point_managed':
        if getattr(tariff, 'is_daily', False):
            raise EntitlementResolutionError('access-point tariffs cannot use daily billing')
        if for_new_issuance and not access_point_quote_context:
            # Every legacy bot/cabinet/guest/auto purchase invokes this
            # resolver before its own debit.  Refusing here is the common
            # before-money fence until a path is migrated to Device-First's
            # durable entitlement quote and term transaction.
            raise EntitlementResolutionError('access-point issuance requires a Device-First immutable checkout quote')
        return await _access_point_policy(
            db,
            tariff,
            selected_access_point_ids=selected_location_ids,
            exception_access_point_ids=exception_location_ids,
            for_new_issuance=for_new_issuance,
            lock_evidence=lock_access_point_evidence,
        )
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


async def assert_tariff_sellable(db: AsyncSession, tariff: Tariff) -> None:
    """Central activation/trial gate shared by every tariff writer.

    A normal tariff writer must never turn an unconfigured draft into a sale
    or trial source.  Resolve before any writer commits; the resolver rejects
    empty, stale, unsafe and legacy-without-manifest policies.
    """

    await resolve_tariff_entitlement(db, tariff, for_new_issuance=False)


async def persist_subscription_entitlement_snapshot(
    db: AsyncSession,
    subscription_id: int,
    tariff_id: int | None,
    entitlement: ResolvedEntitlement,
) -> SubscriptionEntitlementSnapshot | None:
    """Store first resolved evidence; updates deliberately fail closed."""

    # Native tariff membership belongs to Subscription.connected_squads.  A
    # second immutable snapshot would immediately conflict with the ordinary
    # admin checkbox propagation that this mode intentionally restores.
    if entitlement.provenance == 'native_squads':
        return None

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
        inventory_fingerprint=entitlement.inventory_fingerprint,
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

    entitlement = await get_effective_subscription_resolved_entitlement(db, subscription_id)
    return entitlement.squad_uuids


async def get_effective_subscription_resolved_entitlement(
    db: AsyncSession,
    subscription_id: int,
    *,
    at: object | None = None,
    allow_access_point_baseline: bool = False,
) -> ResolvedEntitlement:
    """Return the active captured access-point term, otherwise 0100 baseline.

    A future early-renewal term is intentionally ignored until its half-open
    validity window begins.  This is the one read model used for subscriber
    projections and restore/grace paths; it never consults mutable tariff
    policy after a paid term was captured.
    """

    from datetime import UTC, datetime

    effective_at = at if isinstance(at, datetime) else datetime.now(UTC)
    term = await db.scalar(
        select(SubscriptionEntitlementTerm)
        .where(
            SubscriptionEntitlementTerm.subscription_id == subscription_id,
            SubscriptionEntitlementTerm.starts_at <= effective_at,
            SubscriptionEntitlementTerm.ends_at > effective_at,
        )
        .order_by(SubscriptionEntitlementTerm.term_version.desc())
        .limit(1)
    )
    if term is not None:
        entitlement = ResolvedEntitlement(
            _dedupe(term.access_point_ids or []),
            _dedupe(term.technical_squad_keys or []),
            int(term.policy_revision),
            'access_point_policy',
            str(term.inventory_fingerprint),
        )
        if not entitlement.squad_uuids:
            raise EntitlementResolutionError('effective access-point term has no technical squads')
        expected_hash = sha256(
            json.dumps(
                {
                    'subscription_id': term.subscription_id,
                    'term_version': term.term_version,
                    'tariff_id': term.tariff_id,
                    'starts_at': term.starts_at.isoformat(),
                    'ends_at': term.ends_at.isoformat(),
                    'access_point_ids': list(entitlement.location_ids),
                    'technical_squad_keys': list(entitlement.squad_uuids),
                    'policy_revision': entitlement.policy_revision,
                    'inventory_fingerprint': entitlement.inventory_fingerprint,
                    'source_reference': term.source_reference,
                    'provenance': term.provenance,
                },
                sort_keys=True,
                separators=(',', ':'),
            ).encode()
        ).hexdigest()
        if expected_hash != term.grant_hash:
            raise EntitlementResolutionError('effective access-point term hash is invalid')
        return entitlement
    return await get_subscription_resolved_entitlement(
        db,
        subscription_id,
        allow_access_point_baseline=allow_access_point_baseline,
    )


async def get_subscription_resolved_entitlement(
    db: AsyncSession,
    subscription_id: int,
    *,
    allow_access_point_baseline: bool = False,
) -> ResolvedEntitlement:
    """Load and validate the one-row 0100 entitlement baseline.

    Access-point calls from a legacy purchase/renewal route are deliberately
    rejected: the baseline does not prove a future paid term or quote fence.
    Read-only projection callers use ``get_effective...`` instead, which can
    safely fall back to this baseline for a pre-term historical subscription.
    """

    subscription = await db.get(Subscription, subscription_id)
    if subscription is None:
        raise EntitlementResolutionError('subscription does not exist')
    tariff = await db.get(Tariff, subscription.tariff_id) if subscription.tariff_id is not None else None
    if getattr(tariff, 'entitlement_mode', None) == 'native_squads':
        squads = _dedupe(subscription.connected_squads or [])
        if not squads:
            raise EntitlementResolutionError('native subscription has no assigned Internal Squads')
        return ResolvedEntitlement((), squads, 0, 'native_squads')

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
        getattr(snapshot, 'inventory_fingerprint', None),
    )
    if not entitlement.squad_uuids or entitlement.snapshot_hash != snapshot.snapshot_hash:
        raise EntitlementResolutionError('subscription entitlement snapshot is invalid')
    if entitlement.provenance == 'access_point_policy' and not allow_access_point_baseline:
        raise EntitlementResolutionError('access-point paid renewal requires a direct immutable checkout quote')
    return entitlement
