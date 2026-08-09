"""Server-side catalog, policy and immutable-term helpers for access points.

No function in this module owns a RemnaWave client or invokes a write.  The
only inventory input is the typed snapshot from ``public_access_point_inventory``;
that keeps discovery auditable and lets production wire a GET-only adapter in a
separate owner-approved dry run.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Protocol
from uuid import uuid4

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import (
    PublicAccessPoint,
    PublicAccessPointSquadMapping,
    Subscription,
    SubscriptionEntitlementSnapshot,
    SubscriptionEntitlementTerm,
    SubscriptionEntitlementTermProjectionOutbox,
    Tariff,
    TariffAccessPointConversion,
    TariffAccessPointPolicyItem,
    TariffAccessPointPolicyRevision,
    TariffLegacyEntitlementManifest,
)
from app.services.public_access_point_inventory import (
    InventoryAssessment,
    InventorySnapshot,
    assess_inventory,
)
from app.services.public_location_entitlement_service import (
    EntitlementResolutionError,
    ResolvedEntitlement,
    resolve_tariff_entitlement,
)


class AccessPointPolicyError(ValueError):
    """A fail-closed catalog/policy/term invariant error."""


class ReadOnlyAccessPointInventoryClient(Protocol):
    """Production implementations must collect this data with GET/read only."""

    async def read_access_point_inventory(self) -> InventorySnapshot: ...


@dataclass(frozen=True)
class VerifiedLegacyAccessPointEquivalence:
    """Opaque evidence emitted by a separately protected Panel preparation.

    The core application does not create dedicated squads and never accepts a
    raw mapping from HTTP.  The protected release must first create/read back
    every dedicated equivalent, then expose only these redacted audit handles
    to this service.
    """

    legacy_manifest_hash: str
    access_point_ids: tuple[str, ...]
    prepared_operation_reference: str
    readback_evidence_hash: str


class LegacyAccessPointConversionVerifier(Protocol):
    """Injected only by the separately approved dedicated-equivalence release."""

    async def verify_dedicated_equivalence(
        self,
        *,
        tariff: Tariff,
        manifest: TariffLegacyEntitlementManifest,
    ) -> VerifiedLegacyAccessPointEquivalence: ...


async def assert_no_manual_access_point_grant(
    db: AsyncSession,
    subscription: Subscription,
    *,
    action: str,
) -> None:
    """Reject legacy/admin writes that could mint AP access without a term.

    Access-point permissions are financial, time-bounded evidence.  The only
    grant path is the captured quote → immutable term → projection workflow;
    an arbitrary date/status/reactivation mutation cannot synthesize it.
    """
    if await is_term_owned_access_point_subscription(db, subscription):
        raise AccessPointPolicyError(f'access-point subscriptions cannot use manual {action}')


async def is_term_owned_access_point_subscription(db: AsyncSession, subscription: Subscription) -> bool:
    """Whether this *subscription* is governed by AP term evidence.

    A tariff can be converted for future issuance while an old subscriber keeps
    an immutable legacy snapshot.  Treating every row of the converted tariff
    as AP-owned would strand that historical term (or erase its presentation).
    Conversely, any captured AP term — including an early future renewal — is
    enough to fence raw writers before it can reach a boundary.
    """

    if subscription.tariff_id is None:
        return False
    tariff = await db.get(Tariff, subscription.tariff_id)
    if tariff is None or tariff.entitlement_mode != 'access_point_managed':
        return False
    subscription_id = getattr(subscription, 'id', None)
    if subscription_id is None:
        # Malformed input must not become a raw-write escape hatch.
        return True
    term = await db.scalar(
        select(SubscriptionEntitlementTerm.id)
        .where(SubscriptionEntitlementTerm.subscription_id == subscription_id)
        .limit(1)
    )
    if term is not None:
        return True
    snapshot = await db.scalar(
        select(SubscriptionEntitlementSnapshot).where(
            SubscriptionEntitlementSnapshot.subscription_id == subscription_id
        )
    )
    # A missing snapshot is malformed AP state, never permission to fall back
    # to a raw Panel projection.  Only an explicit immutable legacy baseline
    # can remain outside the AP-term fence after a future-only conversion.
    return snapshot is None or not str(snapshot.provenance).startswith('legacy_')


@dataclass(frozen=True)
class CatalogResult:
    id: str
    title: str
    state: str
    reason: str | None
    tariff_assignable: bool
    presentation_revision: int
    entitlement_revision: int


@dataclass(frozen=True)
class CatalogApplyResult:
    inventory_fingerprint: str
    created: tuple[CatalogResult, ...]
    updated: tuple[CatalogResult, ...]


@dataclass(frozen=True)
class AccessPointTermProjection:
    """One due, captured grant selected by the durable projection outbox."""

    outbox_id: int
    term_id: int
    subscription_id: int
    claim_epoch: int


@dataclass(frozen=True)
class EffectivePublicAccessPoint:
    """Redacted, user-safe presentation of one captured AP entitlement."""

    id: str
    title: str


async def get_tariff_public_access_points(
    db: AsyncSession,
    tariff: Tariff,
) -> tuple[EffectivePublicAccessPoint, ...] | None:
    """Return the current public policy presentation without technical keys.

    This is intentionally separate from ``get_effective_public_access_points``:
    a product tariff card describes the current *offer*, while a subscription
    view is governed by its immutable active term.  Both boundaries expose
    only opaque public IDs and titles; neither may fall back to
    ``Tariff.allowed_squads`` after a legacy conversion preserves that field
    as protected historical evidence.
    """

    if tariff.entitlement_mode == 'legacy_snapshot':
        # Legacy Squad IDs are protected historical evidence, not a browser
        # DTO.  If an old tariff lacks its immutable public manifest, redact
        # the card rather than reconstructing names from technical keys.
        manifest = await db.get(TariffLegacyEntitlementManifest, tariff.id)
        if manifest is None:
            return ()
        public_points: list[EffectivePublicAccessPoint] = []
        for location in manifest.presentation_locations or []:
            if not isinstance(location, dict):
                continue
            title = str(location.get('label_ru') or location.get('label_en') or '').strip()
            if title:
                public_points.append(
                    EffectivePublicAccessPoint(
                        id=str(location.get('id') or f'legacy-{len(public_points) + 1}'),
                        title=title,
                    )
                )
        return tuple(public_points)
    if tariff.entitlement_mode != 'access_point_managed':
        return None
    revision = int(getattr(tariff, 'access_point_policy_revision', 0) or 0)
    if revision <= 0:
        return ()
    policy = await db.scalar(
        select(TariffAccessPointPolicyRevision).where(
            TariffAccessPointPolicyRevision.tariff_id == tariff.id,
            TariffAccessPointPolicyRevision.revision == revision,
        )
    )
    if policy is None:
        return ()
    rows = list(
        (
            await db.execute(
                select(TariffAccessPointPolicyItem, PublicAccessPoint)
                .join(
                    PublicAccessPoint,
                    PublicAccessPoint.id == TariffAccessPointPolicyItem.public_access_point_id,
                )
                .where(TariffAccessPointPolicyItem.policy_revision_id == policy.id)
                .order_by(TariffAccessPointPolicyItem.id)
            )
        ).all()
    )
    if not rows or any(
        point.state != 'verified' or not point.tariff_assignable or not point.inventory_fingerprint
        for _item, point in rows
    ):
        # A product card must not turn a stale policy into a technical fallback
        # or advertise a point whose catalog evidence is no longer valid.
        return ()
    return tuple(EffectivePublicAccessPoint(id=point.id, title=point.title) for _item, point in rows)


async def get_effective_public_access_points(
    db: AsyncSession,
    subscription: Subscription,
    *,
    now: datetime | None = None,
) -> tuple[EffectivePublicAccessPoint, ...] | None:
    """Return current AP titles only, or ``None`` for a non-AP subscription.

    User-facing callers must never derive a public country/Host view from
    ``connected_squads``.  For an AP subscription the term is the sole
    authority; an absent/expired term deliberately returns an empty tuple so
    a stale technical squad cannot cross the API boundary.
    """
    if subscription.tariff_id is None:
        return None
    tariff = await db.get(Tariff, subscription.tariff_id)
    if tariff is None or tariff.entitlement_mode != 'access_point_managed':
        return None

    effective_at = now or datetime.now(UTC)
    term = await db.scalar(
        select(SubscriptionEntitlementTerm)
        .where(
            SubscriptionEntitlementTerm.subscription_id == subscription.id,
            SubscriptionEntitlementTerm.starts_at <= effective_at,
            SubscriptionEntitlementTerm.ends_at > effective_at,
        )
        .order_by(SubscriptionEntitlementTerm.term_version.desc())
        .limit(1)
    )
    if term is None:
        # A tariff conversion is future-only.  An already issued legacy
        # snapshot remains readable through its approved non-technical
        # manifest until a later paid AP term replaces it.
        snapshot = await db.scalar(
            select(SubscriptionEntitlementSnapshot).where(
                SubscriptionEntitlementSnapshot.subscription_id == subscription.id
            )
        )
        if snapshot is None or not str(snapshot.provenance).startswith('legacy_'):
            return ()
        manifest = await db.get(TariffLegacyEntitlementManifest, snapshot.tariff_id)
        if manifest is None:
            return ()
        public_points: list[EffectivePublicAccessPoint] = []
        for location in manifest.presentation_locations or []:
            if not isinstance(location, dict):
                continue
            title = str(location.get('label_ru') or location.get('label_en') or '').strip()
            if title:
                public_points.append(
                    EffectivePublicAccessPoint(
                        id=str(location.get('id') or f'legacy-{len(public_points) + 1}'),
                        title=title,
                    )
                )
        return tuple(public_points)

    point_ids = list(term.access_point_ids or [])
    if not point_ids:
        return ()
    points = list((await db.execute(select(PublicAccessPoint).where(PublicAccessPoint.id.in_(point_ids)))).scalars())
    titles_by_id = {point.id: point.title for point in points}
    return tuple(
        EffectivePublicAccessPoint(id=point_id, title=titles_by_id[point_id])
        for point_id in point_ids
        if point_id in titles_by_id
    )


def _hash(payload: dict[str, object]) -> str:
    return sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _policy_inventory_fingerprint(points: list[PublicAccessPoint]) -> str:
    """Combine point-local evidence into a policy/quote fence.

    The input is deliberately order-independent and does not include titles,
    so a Host rename remains presentation-only while a selected point's graph
    or health change invalidates a pending quote.
    """

    return _hash(
        {
            'points': [
                {
                    'id': point.id,
                    'entitlement_fingerprint': point.inventory_fingerprint,
                    # Monotonic epoch closes the ABA case where a Host graph
                    # changes and later returns byte-for-byte to an older
                    # fingerprint while an invoice is still unpaid.
                    'entitlement_revision': int(getattr(point, 'entitlement_revision', 0) or 0),
                }
                for point in sorted(points, key=lambda item: item.id)
            ]
        }
    )


async def read_consistent_inventory(client: ReadOnlyAccessPointInventoryClient) -> InventoryAssessment:
    """Double-read a panel graph and fail closed on any observed drift."""

    first = await client.read_access_point_inventory()
    second = await client.read_access_point_inventory()
    first_assessment = assess_inventory(first)
    second_assessment = assess_inventory(second)
    if first_assessment.fingerprint != second_assessment.fingerprint:
        raise AccessPointPolicyError('inventory changed during read-only discovery')
    if first.revision is not None and second.revision is not None and first.revision != second.revision:
        raise AccessPointPolicyError('inventory revision changed during read-only discovery')
    return second_assessment


def _catalog_result(point: PublicAccessPoint) -> CatalogResult:
    return CatalogResult(
        id=point.id,
        title=point.title,
        state=point.state,
        reason=point.state_reason,
        tariff_assignable=bool(point.tariff_assignable),
        presentation_revision=int(point.presentation_revision or 1),
        entitlement_revision=int(point.entitlement_revision or 1),
    )


async def apply_catalog_assessment(
    db: AsyncSession,
    assessment: InventoryAssessment,
    *,
    dry_run: bool,
) -> CatalogApplyResult:
    """Apply only local catalog metadata and mapping evidence.

    A rename changes presentation metadata only.  Any state/graph/inventory
    mutation bumps the entitlement revision, so a later checkout can compare
    captured evidence before financial commit.  The function never assigns a
    tariff, subscription, term, user or Panel object.
    """

    existing = {point.panel_host_key: point for point in (await db.execute(select(PublicAccessPoint))).scalars()}
    mappings = list((await db.execute(select(PublicAccessPointSquadMapping))).scalars())
    mappings_by_point: dict[str, dict[str, PublicAccessPointSquadMapping]] = {}
    for mapping in mappings:
        mappings_by_point.setdefault(mapping.public_access_point_id, {})[mapping.internal_squad_key] = mapping

    created: list[CatalogResult] = []
    updated: list[CatalogResult] = []
    observed_host_keys = {candidate.host_key for candidate in assessment.points}
    for candidate in assessment.points:
        point = existing.get(candidate.host_key)
        is_new = point is None
        if point is None:
            point = PublicAccessPoint(
                id=str(uuid4()),
                panel_host_key=candidate.host_key,
                title=candidate.title,
                state=candidate.state,
                state_reason=candidate.reason,
                presentation_revision=1,
                entitlement_revision=1,
                inventory_revision=candidate.inventory_revision,
                inventory_fingerprint=candidate.entitlement_fingerprint,
                graph_fingerprint=candidate.graph_fingerprint,
                tariff_assignable=candidate.assignable,
            )
            if not dry_run:
                db.add(point)
        else:
            presentation_changed = point.title != candidate.title
            entitlement_changed = (
                point.graph_fingerprint != candidate.graph_fingerprint
                or point.state != candidate.state
                or point.state_reason != candidate.reason
                or bool(point.tariff_assignable) != candidate.assignable
                or point.inventory_fingerprint != candidate.entitlement_fingerprint
            )
            if not dry_run:
                if presentation_changed:
                    point.title = candidate.title
                    point.presentation_revision = int(point.presentation_revision or 1) + 1
                if entitlement_changed:
                    point.entitlement_revision = int(point.entitlement_revision or 1) + 1
                point.state = candidate.state
                point.state_reason = candidate.reason
                point.tariff_assignable = candidate.assignable
                point.inventory_revision = candidate.inventory_revision
                point.inventory_fingerprint = candidate.entitlement_fingerprint
                point.graph_fingerprint = candidate.graph_fingerprint

        if not dry_run:
            current_mappings = mappings_by_point.get(point.id, {})
            candidate_keys = set(candidate.squad_keys)
            for key, mapping in current_mappings.items():
                mapping.is_current = key in candidate_keys and candidate.assignable
                mapping.is_dedicated_verified = mapping.is_current
                if mapping.is_current:
                    mapping.graph_fingerprint = candidate.graph_fingerprint
                    mapping.inventory_revision = candidate.inventory_revision
            for key in candidate_keys.difference(current_mappings):
                db.add(
                    PublicAccessPointSquadMapping(
                        public_access_point_id=point.id,
                        internal_squad_key=key,
                        is_dedicated_verified=candidate.assignable,
                        is_current=candidate.assignable,
                        graph_fingerprint=candidate.graph_fingerprint,
                        inventory_revision=candidate.inventory_revision,
                    )
                )

        result = _catalog_result(point)
        (created if is_new else updated).append(result)

    # A Host missing from a coherent read is not silently kept sellable.  It
    # may be a transient panel observation issue, so retain the historical
    # local record but fail closed as ``needs_reconcile`` until a later
    # read-only discovery provides complete evidence again.
    for host_key, point in existing.items():
        if host_key in observed_host_keys:
            continue
        reason = 'host_missing_from_consistent_inventory'
        next_fingerprint = _hash(
            {
                'host_key': point.panel_host_key,
                'state': 'needs_reconcile',
                'reason': reason,
                'observed_inventory_fingerprint': assessment.fingerprint,
            }
        )
        if dry_run:
            updated.append(
                CatalogResult(
                    id=point.id,
                    title=point.title,
                    state='needs_reconcile',
                    reason=reason,
                    tariff_assignable=False,
                    presentation_revision=int(point.presentation_revision or 1),
                    entitlement_revision=int(point.entitlement_revision or 1) + 1,
                )
            )
            continue
        point.state = 'needs_reconcile'
        point.state_reason = reason
        point.tariff_assignable = False
        point.inventory_revision = assessment.revision
        point.inventory_fingerprint = next_fingerprint
        point.entitlement_revision = int(point.entitlement_revision or 1) + 1
        for mapping in mappings_by_point.get(point.id, {}).values():
            mapping.is_current = False
            mapping.is_dedicated_verified = False
        updated.append(_catalog_result(point))
    return CatalogApplyResult(assessment.fingerprint, tuple(created), tuple(updated))


async def list_tariff_assignable_access_points(db: AsyncSession) -> list[CatalogResult]:
    points = list(
        (
            await db.execute(
                select(PublicAccessPoint)
                .where(PublicAccessPoint.state == 'verified', PublicAccessPoint.tariff_assignable.is_(True))
                .order_by(PublicAccessPoint.title, PublicAccessPoint.id)
            )
        ).scalars()
    )
    return [_catalog_result(point) for point in points]


async def replace_tariff_access_point_policy(
    db: AsyncSession,
    tariff: Tariff,
    *,
    access_point_ids: list[str],
    expected_revision: int,
    actor_user_id: int,
    reason: str,
) -> TariffAccessPointPolicyRevision:
    """Append a CAS-protected future policy without touching active grants."""

    if getattr(tariff, 'is_daily', False):
        raise AccessPointPolicyError('access-point tariffs cannot use daily billing')
    wanted = tuple(sorted(set(access_point_ids)))
    if not wanted:
        raise AccessPointPolicyError('an access-point policy must select at least one point')
    current_revision = int(getattr(tariff, 'access_point_policy_revision', 0) or 0)
    if expected_revision != current_revision:
        raise AccessPointPolicyError('stale access-point policy revision')
    if tariff.entitlement_mode == 'legacy_snapshot':
        conversion = await db.get(TariffAccessPointConversion, tariff.id)
        if conversion is None:
            raise AccessPointPolicyError('legacy tariff needs an approved dedicated-equivalence conversion')
    elif tariff.entitlement_mode not in ('no_locations', 'access_point_managed'):
        raise AccessPointPolicyError('country-managed tariff needs an explicit conversion before access-point policy')

    points = list((await db.execute(select(PublicAccessPoint).where(PublicAccessPoint.id.in_(wanted)))).scalars())
    if len(points) != len(wanted):
        raise AccessPointPolicyError('unknown access point')
    if any(point.state != 'verified' or not point.tariff_assignable for point in points):
        raise AccessPointPolicyError('an access point needs verification and cannot be assigned')
    if any(not point.inventory_fingerprint for point in points):
        raise AccessPointPolicyError('selected access-point evidence is incomplete')
    inventory_fingerprint = _policy_inventory_fingerprint(points)

    next_revision = current_revision + 1
    selection_hash = _hash(
        {
            'tariff_id': tariff.id,
            'revision': next_revision,
            'access_point_ids': list(wanted),
            'inventory_fingerprint': inventory_fingerprint,
        }
    )
    policy = TariffAccessPointPolicyRevision(
        tariff_id=tariff.id,
        revision=next_revision,
        selection_hash=selection_hash,
        inventory_fingerprint=inventory_fingerprint,
        reason=reason,
        actor_user_id=actor_user_id,
    )
    db.add(policy)
    await db.flush()
    for point_id in wanted:
        db.add(TariffAccessPointPolicyItem(policy_revision_id=policy.id, public_access_point_id=point_id))
    tariff.access_point_policy_revision = next_revision
    tariff.entitlement_mode = 'access_point_managed'
    return policy


async def record_verified_legacy_access_point_conversion(
    db: AsyncSession,
    *,
    tariff_id: int,
    actor_user_id: int,
    reason: str,
    verifier: LegacyAccessPointConversionVerifier,
) -> TariffAccessPointConversion:
    """Record one legacy conversion after a protected, read-back-verified operation.

    This is deliberately a service boundary, not an HTTP route and not a
    background task.  A separately approved production operation supplies the
    verifier only after creating and reading back independently revocable
    dedicated equivalents.  The function never makes a Panel call itself.

    It atomically records the immutable conversion evidence and the first
    pre-filled access-point policy.  Existing subscriptions/terms are not read
    for mutation and no projection is enqueued here.
    """

    if len(reason.strip()) < 3:
        raise AccessPointPolicyError('legacy conversion approval reason is required')
    tariff = await db.scalar(select(Tariff).where(Tariff.id == tariff_id).with_for_update())
    if tariff is None:
        raise AccessPointPolicyError('tariff not found')
    if tariff.entitlement_mode != 'legacy_snapshot':
        raise AccessPointPolicyError('only a legacy tariff can receive its first access-point conversion')
    if tariff.is_daily:
        raise AccessPointPolicyError('access-point tariffs cannot use daily billing')
    manifest = await db.scalar(
        select(TariffLegacyEntitlementManifest)
        .where(TariffLegacyEntitlementManifest.tariff_id == tariff.id)
        .with_for_update()
    )
    if manifest is None or list(tariff.allowed_squads or []) != list(manifest.squad_uuids or []):
        raise AccessPointPolicyError('legacy manifest is absent or differs from the protected tariff baseline')
    existing = await db.scalar(
        select(TariffAccessPointConversion).where(TariffAccessPointConversion.tariff_id == tariff.id).with_for_update()
    )
    if existing is not None:
        raise AccessPointPolicyError('legacy tariff already has a dedicated-equivalence conversion')

    evidence = await verifier.verify_dedicated_equivalence(tariff=tariff, manifest=manifest)
    wanted = tuple(sorted(set(evidence.access_point_ids)))
    if (
        evidence.legacy_manifest_hash != manifest.manifest_hash
        or not wanted
        or len(evidence.prepared_operation_reference.strip()) < 3
        or len(evidence.readback_evidence_hash.strip()) < 32
    ):
        raise AccessPointPolicyError('protected dedicated-equivalence evidence is incomplete or stale')
    points = list(
        (
            await db.execute(select(PublicAccessPoint).where(PublicAccessPoint.id.in_(wanted)).with_for_update())
        ).scalars()
    )
    if len(points) != len(wanted) or any(
        point.state != 'verified' or not point.tariff_assignable or not point.inventory_fingerprint for point in points
    ):
        raise AccessPointPolicyError('protected conversion references an unavailable access point')

    # ``replace_*`` requires a conversion row before it can transition the
    # legacy tariff.  The temporary row, first policy and final readback
    # fence must be one savepoint: a protected caller may catch a validation
    # error to return an HTTP response and later commit its outer transaction.
    # Without this nested rollback a failed readback would leave a real
    # conversion row whose mere presence authorizes later policy changes.
    async with db.begin_nested():
        conversion = TariffAccessPointConversion(
            tariff_id=tariff.id,
            legacy_manifest_hash=manifest.manifest_hash,
            policy_revision=0,
            conversion_hash=_hash(
                {
                    'legacy_manifest_hash': manifest.manifest_hash,
                    'access_point_ids': list(wanted),
                    'prepared_operation_reference': evidence.prepared_operation_reference,
                    'readback_evidence_hash': evidence.readback_evidence_hash,
                    'state': 'pre_policy_flush_only',
                }
            ),
            prepared_operation_reference=evidence.prepared_operation_reference,
            readback_evidence_hash=evidence.readback_evidence_hash,
            approved_by_user_id=actor_user_id,
            approval_reason=reason,
        )
        db.add(conversion)
        await db.flush()
        policy = await replace_tariff_access_point_policy(
            db,
            tariff,
            access_point_ids=list(wanted),
            expected_revision=0,
            actor_user_id=actor_user_id,
            reason=reason,
        )
        try:
            resolved = await resolve_tariff_entitlement(
                db,
                tariff,
                access_point_quote_context=True,
                lock_access_point_evidence=True,
            )
        except EntitlementResolutionError as exc:
            raise AccessPointPolicyError('protected conversion has no current verified dedicated mapping') from exc
        if (
            resolved.provenance != 'access_point_policy'
            or tuple(sorted(resolved.location_ids)) != wanted
            or resolved.inventory_fingerprint != policy.inventory_fingerprint
        ):
            raise AccessPointPolicyError('protected conversion mapping evidence differs from its first policy')
        conversion.policy_revision = policy.revision
        conversion.conversion_hash = _hash(
            {
                'legacy_manifest_hash': manifest.manifest_hash,
                'access_point_ids': list(wanted),
                'policy_revision': policy.revision,
                'selection_hash': policy.selection_hash,
                'inventory_fingerprint': policy.inventory_fingerprint,
                'prepared_operation_reference': evidence.prepared_operation_reference,
                'readback_evidence_hash': evidence.readback_evidence_hash,
            }
        )
        await db.flush()
    return conversion


async def capture_access_point_entitlement_term(
    db: AsyncSession,
    *,
    subscription_id: int,
    tariff: Tariff,
    starts_at: datetime,
    ends_at: datetime,
    source_reference: str,
    provenance: str,
    resolved_entitlement: ResolvedEntitlement,
) -> SubscriptionEntitlementTerm:
    """Capture one exact paid/future grant under a subscription-row fence.

    The caller supplies the already frozen quote entitlement.  Resolving the
    tariff here would reintroduce the exact policy-drift race that terms are
    meant to prevent.  ``flush`` deliberately occurs before the caller's
    debit/capture can be committed, so the unique/exclusion constraints are a
    financial fence rather than eventual audit data.
    """

    if ends_at <= starts_at:
        raise AccessPointPolicyError('entitlement term must have a positive validity window')
    if not source_reference:
        raise AccessPointPolicyError('access-point paid term requires an idempotency source reference')
    if resolved_entitlement.provenance != 'access_point_policy' or not resolved_entitlement.inventory_fingerprint:
        raise AccessPointPolicyError('only a frozen access-point quote can capture an access-point term')

    # Serialize both same-subscription early renewals and a duplicate/late
    # callback.  This lock is taken after the caller's canonical user/checkout
    # fence in Device-First, never before it.
    locked_subscription = await db.scalar(
        select(Subscription.id).where(Subscription.id == subscription_id).with_for_update()
    )
    if locked_subscription is None:
        raise AccessPointPolicyError('subscription disappeared before entitlement term capture')

    existing = await db.scalar(
        select(SubscriptionEntitlementTerm)
        .where(SubscriptionEntitlementTerm.source_reference == source_reference)
        .with_for_update()
    )
    if existing is not None:
        same_source = (
            existing.subscription_id == subscription_id
            and existing.tariff_id == tariff.id
            and existing.starts_at == starts_at
            and existing.ends_at == ends_at
            and tuple(existing.access_point_ids or []) == resolved_entitlement.location_ids
            and tuple(existing.technical_squad_keys or []) == resolved_entitlement.squad_uuids
            and existing.policy_revision == resolved_entitlement.policy_revision
            and existing.inventory_fingerprint == resolved_entitlement.inventory_fingerprint
            and existing.provenance == provenance
        )
        if not same_source:
            raise AccessPointPolicyError('idempotency source was reused with different term evidence')
        return existing

    prior_terms = list(
        (
            await db.execute(
                select(SubscriptionEntitlementTerm)
                .where(SubscriptionEntitlementTerm.subscription_id == subscription_id)
                .order_by(SubscriptionEntitlementTerm.term_version)
                .with_for_update()
            )
        ).scalars()
    )
    if any(starts_at < term.ends_at and ends_at > term.starts_at for term in prior_terms):
        raise AccessPointPolicyError('entitlement term overlaps existing evidence')
    if prior_terms and prior_terms[-1].ends_at >= datetime.now(UTC) and starts_at != prior_terms[-1].ends_at:
        raise AccessPointPolicyError('early paid renewal term must be contiguous with the active captured term')

    version = (prior_terms[-1].term_version if prior_terms else 0) + 1
    grant_hash = _hash(
        {
            'subscription_id': subscription_id,
            'term_version': version,
            'tariff_id': tariff.id,
            'starts_at': starts_at.isoformat(),
            'ends_at': ends_at.isoformat(),
            'access_point_ids': list(resolved_entitlement.location_ids),
            'technical_squad_keys': list(resolved_entitlement.squad_uuids),
            'policy_revision': resolved_entitlement.policy_revision,
            'inventory_fingerprint': resolved_entitlement.inventory_fingerprint,
            'source_reference': source_reference,
            'provenance': provenance,
        }
    )
    term = SubscriptionEntitlementTerm(
        subscription_id=subscription_id,
        tariff_id=tariff.id,
        term_version=version,
        starts_at=starts_at,
        ends_at=ends_at,
        access_point_ids=list(resolved_entitlement.location_ids),
        technical_squad_keys=list(resolved_entitlement.squad_uuids),
        policy_revision=resolved_entitlement.policy_revision,
        inventory_fingerprint=resolved_entitlement.inventory_fingerprint,
        source_reference=source_reference,
        provenance=provenance,
        grant_hash=grant_hash,
    )
    db.add(term)
    await db.flush()
    # Every access-point Panel write, including initial provisioning, travels
    # through this one durable boundary queue.  Letting a checkout retry send
    # raw ``connected_squads`` would race an early-renewal projection and can
    # reintroduce an already-expired grant after its paid boundary.
    db.add(
        SubscriptionEntitlementTermProjectionOutbox(
            term_id=term.id,
            subscription_id=subscription_id,
            effective_at=starts_at,
        )
    )
    await db.flush()
    return term


async def requeue_active_access_point_term_projection(
    db: AsyncSession,
    subscription: Subscription,
    *,
    reason: str,
) -> bool:
    """Re-project an already-paid active AP term after a safe suspension.

    Account/channel restoration must not invent a new end date or use the
    mutable subscription squads.  It only re-arms the durable instruction for
    the term that is effective *right now*; the normal claim/fresh-clock fence
    still owns the eventual Panel write.
    """
    now = datetime.now(UTC)
    term = await db.scalar(
        select(SubscriptionEntitlementTerm)
        .where(
            SubscriptionEntitlementTerm.subscription_id == subscription.id,
            SubscriptionEntitlementTerm.starts_at <= now,
            SubscriptionEntitlementTerm.ends_at > now,
        )
        .order_by(SubscriptionEntitlementTerm.term_version.desc())
        .limit(1)
        .with_for_update()
    )
    if term is None:
        return False
    outbox = await db.scalar(
        select(SubscriptionEntitlementTermProjectionOutbox)
        .where(SubscriptionEntitlementTermProjectionOutbox.term_id == term.id)
        .with_for_update()
    )
    if outbox is None:
        return False
    outbox.state = 'pending'
    outbox.claimed_at = None
    outbox.delivered_at = None
    outbox.last_error = f'reproject:{reason}'
    await db.flush()
    return True


async def claim_due_access_point_term_projections(
    db: AsyncSession,
    *,
    now: datetime | None = None,
    limit: int = 20,
) -> list[AccessPointTermProjection]:
    """Claim due (or abandoned) AP boundary actions under row locks.

    Claiming is committed before the Panel call by the processor.  A worker
    crash therefore leaves an auditable ``processing`` row which another
    worker can safely reclaim after a short lease; the Panel update itself is
    idempotent because it always uses the same immutable term.
    """

    effective_now = now or datetime.now(UTC)
    reclaim_before = effective_now - timedelta(minutes=15)
    candidates = list(
        (
            await db.execute(
                select(SubscriptionEntitlementTermProjectionOutbox)
                .where(
                    SubscriptionEntitlementTermProjectionOutbox.effective_at <= effective_now,
                    or_(
                        SubscriptionEntitlementTermProjectionOutbox.state == 'pending',
                        and_(
                            SubscriptionEntitlementTermProjectionOutbox.state == 'processing',
                            SubscriptionEntitlementTermProjectionOutbox.claimed_at < reclaim_before,
                        ),
                    ),
                )
                .order_by(
                    SubscriptionEntitlementTermProjectionOutbox.effective_at,
                    SubscriptionEntitlementTermProjectionOutbox.id,
                )
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        ).scalars()
    )
    claims: list[AccessPointTermProjection] = []
    for outbox in candidates:
        outbox.state = 'processing'
        outbox.attempts = int(outbox.attempts or 0) + 1
        outbox.claim_epoch = int(outbox.claim_epoch or 0) + 1
        outbox.claimed_at = effective_now
        outbox.last_error = None
        claims.append(
            AccessPointTermProjection(
                outbox_id=outbox.id,
                term_id=outbox.term_id,
                subscription_id=outbox.subscription_id,
                claim_epoch=outbox.claim_epoch,
            )
        )
    if claims:
        await db.commit()
    return claims


async def process_due_access_point_term_projections(
    db: AsyncSession,
    *,
    now: datetime | None = None,
    limit: int = 20,
    panel_projection=None,
) -> int:
    """Project each due immutable term to the Panel, retryably and in order.

    The subscription's live squad column is changed only after its captured
    term becomes effective.  If the Panel write fails, the session rolls that
    local change back and restores the outbox to ``pending`` for a retry.  A
    successful but unacknowledged remote call is safe to replay because the
    payload is exactly the immutable term, never a current tariff resolver.
    ``panel_projection`` exists solely for deterministic tests.
    """

    effective_now = now or datetime.now(UTC)
    claims = await claim_due_access_point_term_projections(db, now=effective_now, limit=limit)
    projected = 0
    for claim in claims:
        subscription = None
        previous_squads: list[str] | None = None
        try:
            outbox = await db.scalar(
                select(SubscriptionEntitlementTermProjectionOutbox)
                .where(SubscriptionEntitlementTermProjectionOutbox.id == claim.outbox_id)
                .with_for_update()
            )
            term = await db.scalar(
                select(SubscriptionEntitlementTerm)
                .where(SubscriptionEntitlementTerm.id == claim.term_id)
                .with_for_update()
            )
            subscription = await db.scalar(
                select(Subscription).where(Subscription.id == claim.subscription_id).with_for_update()
            )
            if outbox is None or term is None or subscription is None:
                raise AccessPointPolicyError('captured entitlement-term projection evidence disappeared')
            # A claim is a fencing token, not merely an advisory state.  A
            # delayed worker must abort if another worker reclaimed it.
            if outbox.state != 'processing' or int(outbox.claim_epoch or 0) != claim.claim_epoch:
                continue
            projection_now = datetime.now(UTC)
            if not (term.starts_at <= projection_now < term.ends_at):
                raise AccessPointPolicyError('captured entitlement term is not effective at projection time')

            target_squads = list(term.technical_squad_keys or [])
            if not target_squads:
                raise AccessPointPolicyError('captured entitlement term has no dedicated squads')
            previous_squads = list(subscription.connected_squads or [])
            subscription.connected_squads = target_squads
            subscription.updated_at = projection_now
            await db.flush()

            if panel_projection is None:
                from app.services.subscription_service import SubscriptionService

                success, _error = await SubscriptionService().ensure_subscription_synced(
                    db,
                    subscription,
                    force_panel_sync=True,
                    commit=False,
                    access_point_term_projection=True,
                    access_point_term_ends_at=term.ends_at,
                )
            else:
                success = bool(await panel_projection(db, subscription, term))
            if not success:
                raise AccessPointPolicyError('Panel did not acknowledge captured entitlement-term projection')

            # ``commit=False`` above keeps this row locked throughout the
            # Panel call.  Re-check the fencing epoch nevertheless: injected
            # adapters must not be able to turn a stale claim into delivery.
            if outbox.state != 'processing' or int(outbox.claim_epoch or 0) != claim.claim_epoch:
                raise AccessPointPolicyError('stale access-point term projection claim')
            outbox.state = 'delivered'
            outbox.delivered_at = datetime.now(UTC)
            outbox.last_error = None
            await db.commit()
            projected += 1
        except Exception as exc:
            # Never leave the new squads committed locally when the remote
            # projection did not succeed.  Reload the already-committed claim
            # and make it retryable with a redacted diagnostic.
            await db.rollback()
            if subscription is not None and previous_squads is not None:
                # The rollback already restores this in a real ORM session;
                # assigning it explicitly also preserves the invariant for a
                # session that was refreshed/expired by a failing adapter.
                subscription.connected_squads = previous_squads
            retry = await db.scalar(
                select(SubscriptionEntitlementTermProjectionOutbox)
                .where(SubscriptionEntitlementTermProjectionOutbox.id == claim.outbox_id)
                .with_for_update()
            )
            if retry is not None:
                # We must never project a term that has already elapsed: it
                # would create a late access switch.  Preserve evidence for an
                # operator instead of retrying it as if it were current.
                retry.state = (
                    'manual_reconcile'
                    if isinstance(exc, AccessPointPolicyError) and 'not effective at projection time' in str(exc)
                    else 'pending'
                )
                retry.last_error = type(exc).__name__
                retry.claimed_at = None
                await db.commit()
    return projected
