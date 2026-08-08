"""One-time, owner-approved preservation of legacy tariff entitlements.

This is intentionally an operational command, not an Alembic data migration:
the approved fingerprints below were obtained from a read-only production
snapshot and are reviewed with the protected deployment.  The command never
contacts RemnaWave and never changes a tariff or a subscription's live squads.
It merely records immutable evidence after proving that the current database
still exactly matches that approved snapshot.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from hashlib import sha256

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.database import AsyncSessionLocal
from app.database.models import Subscription, SubscriptionEntitlementSnapshot, Tariff, TariffLegacyEntitlementManifest
from app.services.public_location_entitlement_service import ResolvedEntitlement


# These opaque hashes are the owner-approved production baseline from the
# release card.  They deliberately contain neither a Panel credential nor a
# technical UUID.  Adding, removing, reordering, or substituting a UUID causes
# this command to stop before writing any entitlement evidence.
OWNER_APPROVED_LEGACY_TARIFF_SQUAD_SET_HASHES: dict[int, str] = {
    3: 'b0554b36626a6382d1e72c53d03baf184c4e1b0efbe08cfce11d94d6a55e31dc',
    4: 'b0554b36626a6382d1e72c53d03baf184c4e1b0efbe08cfce11d94d6a55e31dc',
    5: '27f79807269b7d07972b0a8c42afd2c3c6bcee7ef19d47d731ee99355227cbd6',
}
OWNER_APPROVED_ACTIVE_LEGACY_TARIFF_IDS = frozenset({3, 4})
_LEGACY_PRESENTATION_LOCATIONS = (
    {
        'id': 'legacy-de',
        'iso_code': 'DE',
        'label_ru': 'Германия',
        'label_en': 'Germany',
        'flag': '🇩🇪',
        'lifecycle': 'published',
        'sort_order': 10,
    },
    {
        'id': 'legacy-nl',
        'iso_code': 'NL',
        'label_ru': 'Нидерланды',
        'label_en': 'Netherlands',
        'flag': '🇳🇱',
        'lifecycle': 'published',
        'sort_order': 20,
    },
)
OWNER_APPROVAL_PERMISSION = 'owner:production-migration'
_SAFE_APPROVAL_VALUE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:/@-]{0,254}')


class LegacyEntitlementSeedError(RuntimeError):
    """The release must stop rather than infer an entitlement."""


@dataclass(frozen=True)
class _ManifestSeed:
    tariff_id: int
    squad_uuids: tuple[str, ...]
    membership_hashes: dict[str, str]
    presentation_locations: tuple[dict[str, object], ...]
    manifest_hash: str


@dataclass(frozen=True)
class _SubscriptionSnapshotSeed:
    subscription_id: int
    tariff_id: int | None
    entitlement: ResolvedEntitlement


@dataclass(frozen=True)
class LegacyEntitlementSeedPlan:
    manifests: tuple[_ManifestSeed, ...]
    subscription_snapshots: tuple[_SubscriptionSnapshotSeed, ...]
    skipped_empty_subscriptions: int


@dataclass(frozen=True)
class LegacyEntitlementSeedResult:
    manifests_created: int
    manifests_already_present: int
    snapshots_created: int
    snapshots_already_present: int
    skipped_empty_subscriptions: int
    dry_run: bool


def _canonical_squads(values: object) -> tuple[str, ...]:
    """Accept only a non-empty explicit UUID array; ``NULL``/``[]`` is never all."""

    if not isinstance(values, list):
        return ()
    return tuple(dict.fromkeys(str(value) for value in values if isinstance(value, str) and value.strip()))


def _squad_set_hash(squad_uuids: Iterable[str]) -> str:
    raw = json.dumps(list(squad_uuids), separators=(',', ':'), ensure_ascii=True).encode()
    return sha256(raw).hexdigest()


def _manifest_hash(
    tariff_id: int,
    squad_uuids: tuple[str, ...],
    membership_hashes: dict[str, str],
    presentation_locations: tuple[dict[str, object], ...],
) -> str:
    raw = json.dumps(
        {
            'tariff_id': tariff_id,
            'technical_squad_uuids': list(squad_uuids),
            'membership_hashes': membership_hashes,
            'presentation_locations': list(presentation_locations),
            'version': 1,
        },
        sort_keys=True,
        separators=(',', ':'),
    ).encode()
    return sha256(raw).hexdigest()


def _validate_approval_value(value: str, field: str) -> str:
    normalized = value.strip()
    if not _SAFE_APPROVAL_VALUE.fullmatch(normalized):
        raise LegacyEntitlementSeedError(f'{field} is missing or contains unsupported characters')
    return normalized


def build_legacy_entitlement_seed_plan(
    tariffs: Iterable[Tariff], subscriptions: Iterable[Subscription]
) -> LegacyEntitlementSeedPlan:
    """Build all writes in memory, validating every source before a DB mutation."""

    tariffs_by_id = {tariff.id: tariff for tariff in tariffs}
    active_tariff_ids = {tariff.id for tariff in tariffs_by_id.values() if tariff.is_active}
    approved_active_ids = set(OWNER_APPROVED_ACTIVE_LEGACY_TARIFF_IDS)
    if active_tariff_ids != approved_active_ids:
        raise LegacyEntitlementSeedError(
            'active tariff scope differs from the owner-approved legacy manifest baseline'
        )

    required_manifest_ids = set(active_tariff_ids)
    snapshots: list[_SubscriptionSnapshotSeed] = []
    skipped_empty_subscriptions = 0
    for subscription in subscriptions:
        squads = _canonical_squads(subscription.connected_squads)
        if not squads:
            # Disabled/legacy rows with no explicit technical evidence stay in
            # manual-reconcile state.  They cannot regain access through this
            # command because [] and NULL never mean all after cutover.
            skipped_empty_subscriptions += 1
            continue
        tariff = tariffs_by_id.get(subscription.tariff_id)
        if tariff is None or squads != _canonical_squads(tariff.allowed_squads):
            raise LegacyEntitlementSeedError(
                'a subscription has an unknown or non-tariff legacy entitlement and needs manual reconcile'
            )
        required_manifest_ids.add(tariff.id)
        entitlement = ResolvedEntitlement(
            location_ids=(),
            squad_uuids=squads,
            policy_revision=int(tariff.location_policy_revision or 1),
            provenance='legacy_subscription_backfill',
        )
        snapshots.append(
            _SubscriptionSnapshotSeed(
                subscription_id=subscription.id,
                tariff_id=subscription.tariff_id,
                entitlement=entitlement,
            )
        )

    manifests: list[_ManifestSeed] = []
    for tariff_id in sorted(required_manifest_ids):
        tariff = tariffs_by_id[tariff_id]
        if tariff.entitlement_mode != 'legacy_snapshot':
            raise LegacyEntitlementSeedError('an approved legacy tariff is not in legacy_snapshot mode')
        squads = _canonical_squads(tariff.allowed_squads)
        actual_hash = _squad_set_hash(squads)
        expected_hash = OWNER_APPROVED_LEGACY_TARIFF_SQUAD_SET_HASHES.get(tariff_id)
        if not squads or actual_hash != expected_hash:
            raise LegacyEntitlementSeedError('legacy tariff squad set differs from the owner-approved baseline')
        # The legacy technical squad list is the only stable non-Panel snapshot
        # available in this release.  Its hash makes future inventory/relink
        # drift observable without claiming that a live Panel read was made.
        membership_hashes = {'legacy_technical_squad_set_sha256': actual_hash}
        manifests.append(
            _ManifestSeed(
                tariff_id=tariff_id,
                squad_uuids=squads,
                membership_hashes=membership_hashes,
                presentation_locations=_LEGACY_PRESENTATION_LOCATIONS,
                manifest_hash=_manifest_hash(tariff_id, squads, membership_hashes, _LEGACY_PRESENTATION_LOCATIONS),
            )
        )

    return LegacyEntitlementSeedPlan(
        manifests=tuple(manifests),
        subscription_snapshots=tuple(snapshots),
        skipped_empty_subscriptions=skipped_empty_subscriptions,
    )


def _same_manifest(existing: TariffLegacyEntitlementManifest, expected: _ManifestSeed) -> bool:
    return (
        _canonical_squads(existing.squad_uuids) == expected.squad_uuids
        and dict(existing.membership_hashes or {}) == expected.membership_hashes
        and tuple(existing.presentation_locations or []) == expected.presentation_locations
        and existing.manifest_hash == expected.manifest_hash
    )


def _same_snapshot(existing: SubscriptionEntitlementSnapshot, expected: _SubscriptionSnapshotSeed) -> bool:
    entitlement = expected.entitlement
    return (
        existing.tariff_id == expected.tariff_id
        and _canonical_squads(existing.location_ids) == entitlement.location_ids
        and _canonical_squads(existing.technical_squad_uuids) == entitlement.squad_uuids
        and existing.policy_revision == entitlement.policy_revision
        and existing.provenance == entitlement.provenance
        and existing.snapshot_hash == entitlement.snapshot_hash
    )


async def seed_approved_legacy_entitlements(
    db: AsyncSession,
    *,
    approval_actor: str,
    approval_reference: str,
    dry_run: bool = False,
) -> LegacyEntitlementSeedResult:
    """Persist an idempotent release baseline, or fail before any inference."""

    actor = _validate_approval_value(approval_actor, 'approval actor')
    reference = _validate_approval_value(approval_reference, 'approval reference')
    tariffs = list(
        (await db.execute(select(Tariff).order_by(Tariff.id).with_for_update()))
        .scalars()
        .all()
    )
    subscriptions = list(
        (await db.execute(select(Subscription).order_by(Subscription.id).with_for_update()))
        .scalars()
        .all()
    )
    plan = build_legacy_entitlement_seed_plan(tariffs, subscriptions)

    existing_manifests = {
        manifest.tariff_id: manifest
        for manifest in (
            await db.execute(select(TariffLegacyEntitlementManifest).with_for_update())
        ).scalars()
    }
    existing_snapshots = {
        snapshot.subscription_id: snapshot
        for snapshot in (
            await db.execute(select(SubscriptionEntitlementSnapshot).with_for_update())
        ).scalars()
    }

    for expected in plan.manifests:
        existing = existing_manifests.get(expected.tariff_id)
        if existing is not None and not _same_manifest(existing, expected):
            raise LegacyEntitlementSeedError('an existing tariff manifest differs from the approved immutable baseline')
    for expected in plan.subscription_snapshots:
        existing = existing_snapshots.get(expected.subscription_id)
        if existing is not None and not _same_snapshot(existing, expected):
            raise LegacyEntitlementSeedError('an existing subscription snapshot differs from the immutable legacy baseline')

    manifests_created = 0
    snapshots_created = 0
    for expected in plan.manifests:
        if expected.tariff_id in existing_manifests:
            continue
        db.add(
            TariffLegacyEntitlementManifest(
                tariff_id=expected.tariff_id,
                squad_uuids=list(expected.squad_uuids),
                membership_hashes=expected.membership_hashes,
                presentation_locations=list(expected.presentation_locations),
                manifest_hash=expected.manifest_hash,
                approved_by_actor=actor,
                approval_permission=OWNER_APPROVAL_PERMISSION,
                approval_reference=reference,
                approval_reason=(
                    'Owner-approved production cutover: retain the exact existing legacy tariff access; '
                    'no Panel mutation or country cutover is performed.'
                ),
            )
        )
        manifests_created += 1
    for expected in plan.subscription_snapshots:
        if expected.subscription_id in existing_snapshots:
            continue
        entitlement = expected.entitlement
        db.add(
            SubscriptionEntitlementSnapshot(
                subscription_id=expected.subscription_id,
                tariff_id=expected.tariff_id,
                location_ids=list(entitlement.location_ids),
                technical_squad_uuids=list(entitlement.squad_uuids),
                policy_revision=entitlement.policy_revision,
                provenance=entitlement.provenance,
                snapshot_hash=entitlement.snapshot_hash,
            )
        )
        snapshots_created += 1

    await db.flush()
    if dry_run:
        await db.rollback()
    else:
        await db.commit()
    return LegacyEntitlementSeedResult(
        manifests_created=manifests_created,
        manifests_already_present=len(plan.manifests) - manifests_created,
        snapshots_created=snapshots_created,
        snapshots_already_present=len(plan.subscription_snapshots) - snapshots_created,
        skipped_empty_subscriptions=plan.skipped_empty_subscriptions,
        dry_run=dry_run,
    )


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Seed approved immutable legacy tariff and subscription evidence.')
    parser.add_argument('--approval-actor', required=True)
    parser.add_argument('--approval-reference', required=True)
    parser.add_argument('--dry-run', action='store_true')
    return parser.parse_args(argv)


async def _main_async(args: argparse.Namespace) -> int:
    async with AsyncSessionLocal() as db:
        result = await seed_approved_legacy_entitlements(
            db,
            approval_actor=args.approval_actor,
            approval_reference=args.approval_reference,
            dry_run=args.dry_run,
        )
    print(
        'legacy entitlement seed complete '
        f'manifests_created={result.manifests_created} '
        f'manifests_already_present={result.manifests_already_present} '
        f'snapshots_created={result.snapshots_created} '
        f'snapshots_already_present={result.snapshots_already_present} '
        f'skipped_empty_subscriptions={result.skipped_empty_subscriptions} '
        f'dry_run={str(result.dry_run).lower()}'
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(_main_async(_parse_args(argv)))


if __name__ == '__main__':
    raise SystemExit(main())
