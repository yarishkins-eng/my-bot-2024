"""Privileged, non-technical access-point catalog and tariff-policy routes.

The router has no Panel write endpoint and does not register an active-user
executor.  Discovery can only consume an application-injected GET-only client;
without that explicit deployment wiring it returns 503 rather than guessing a
RemnaWave API contract or accepting raw identifiers from the browser.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from hashlib import sha256
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.rbac import AuditLogCRUD
from app.database.models import (
    EntitlementChangePlan,
    EntitlementPlanApproval,
    PublicAccessPoint,
    Subscription,
    SubscriptionEntitlementTerm,
    Tariff,
    TariffAccessPointPolicyItem,
    TariffAccessPointPolicyRevision,
    TariffLegacyEntitlementManifest,
    User,
)
from app.services.public_access_point_service import (
    AccessPointPolicyError,
    ReadOnlyAccessPointInventoryClient,
    apply_catalog_assessment,
    list_tariff_assignable_access_points,
    read_consistent_inventory,
    replace_tariff_access_point_policy,
)
from app.services.public_location_entitlement_service import EntitlementResolutionError, resolve_tariff_entitlement

from ..dependencies import get_cabinet_db, require_permission


router = APIRouter(prefix='/admin', tags=['Cabinet Admin Access Points'])


class AccessPointPolicyRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')

    access_point_ids: list[str] = Field(min_length=1, max_length=100)
    expected_revision: int = Field(ge=0)
    reason: str = Field(min_length=3, max_length=500)


class AccessPointPlanRequest(BaseModel):
    """Reason-only request: target is the already-saved tariff policy.

    Letting a browser submit a second point list here would create a shadow
    policy and make a plan capable of widening access.  The immutable preview
    can therefore only target the current audited policy revision.
    """

    model_config = ConfigDict(extra='forbid')

    reason: str = Field(min_length=3, max_length=500)


class AccessPointPlanConfirmationRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')

    plan_hash: str = Field(min_length=64, max_length=128)
    reason: str = Field(min_length=3, max_length=500)


def _dto(point: PublicAccessPoint, *, selected: bool = False) -> dict[str, object]:
    """Safe DTO: exactly opaque app id + subscription-visible business data."""

    return {
        'id': point.id,
        'title': point.title,
        'state': point.state,
        'reason': point.state_reason,
        'tariff_assignable': bool(point.tariff_assignable),
        'presentation_revision': int(point.presentation_revision or 1),
        'entitlement_revision': int(point.entitlement_revision or 1),
        'selected': selected,
    }


def _get_read_only_inventory_client(request: Request) -> ReadOnlyAccessPointInventoryClient:
    client = getattr(request.app.state, 'access_point_inventory_client', None)
    if client is None or not hasattr(client, 'read_access_point_inventory'):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='Read-only access-point inventory is not configured for this environment',
        )
    return client


def _timestamp(value: object) -> str | None:
    isoformat = getattr(value, 'isoformat', None)
    return isoformat() if callable(isoformat) else None


def _immutable_hash(value: object) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _plan_public_points(point_by_id: dict[str, PublicAccessPoint], point_ids: set[str]) -> list[dict[str, str]]:
    """Return a browser-safe, deterministic public-point list only."""

    return [
        {'id': point_id, 'title': point_by_id[point_id].title}
        for point_id in sorted(point_ids, key=lambda item: (point_by_id[item].title, item))
        if point_id in point_by_id
    ]


def _legacy_presentation(manifest: TariffLegacyEntitlementManifest) -> list[dict[str, str | None]]:
    """Keep legacy coverage readable without inventing a point-to-Squad mapping."""

    rows: list[dict[str, str | None]] = []
    for location in manifest.presentation_locations or []:
        if not isinstance(location, dict):
            continue
        label = str(location.get('label_ru') or location.get('label_en') or '').strip()
        if not label:
            continue
        rows.append(
            {
                'id': str(location.get('id') or f'legacy-{len(rows) + 1}'),
                'title': label,
                'flag': str(location['flag']) if location.get('flag') else None,
            }
        )
    return rows


async def _lock_subscription_table_for_plan_confirmation(db: AsyncSession) -> None:
    """Fence subscription phantoms while a privileged approval is persisted.

    Row locks under ``READ COMMITTED`` cannot block a new matching
    subscription from appearing after the plan recheck.  Confirmation is an
    uncommon, operator-only action with no Panel call, so PostgreSQL takes a
    brief ``SHARE ROW EXCLUSIVE`` lock: it waits for in-flight subscription
    writers, blocks INSERT/UPDATE/DELETE until the approval commits, and makes
    the following scope read stable.  SQLite/unit-test sessions skip it.
    """

    get_bind = getattr(db, 'get_bind', None)
    if get_bind is None:
        return
    bind = get_bind()
    if getattr(getattr(bind, 'dialect', None), 'name', None) != 'postgresql':
        return
    await db.execute(text('LOCK TABLE subscriptions IN SHARE ROW EXCLUSIVE MODE'))


async def _plan_subscriptions(
    db: AsyncSession,
    *,
    tariff_id: int,
    lock_rows: bool = False,
) -> tuple[list[Subscription], list[Subscription], dict[int, SubscriptionEntitlementTerm]]:
    """Freeze the selected tariff plus every shared Panel identity in preimage.

    The result deliberately retains technical evidence only in the server-side
    plan preimage.  Its public response is built separately below.
    """

    selected_query = select(Subscription).where(Subscription.tariff_id == tariff_id)
    if lock_rows:
        selected_query = selected_query.with_for_update()
    selected = list((await db.execute(selected_query)).scalars())
    user_ids = {subscription.user_id for subscription in selected}
    panel_ids = {subscription.remnawave_uuid for subscription in selected if subscription.remnawave_uuid}
    criteria = []
    if user_ids:
        criteria.append(Subscription.user_id.in_(user_ids))
    if panel_ids:
        criteria.append(Subscription.remnawave_uuid.in_(panel_ids))
    related_query = select(Subscription).where(or_(*criteria))
    if lock_rows:
        related_query = related_query.with_for_update()
    related = list((await db.execute(related_query)).scalars()) if criteria else []
    now = datetime.now(UTC)
    related_ids = [subscription.id for subscription in related]
    terms = (
        list(
            (
                await db.execute(
                    (
                        select(SubscriptionEntitlementTerm)
                        .where(
                            SubscriptionEntitlementTerm.subscription_id.in_(related_ids),
                            SubscriptionEntitlementTerm.starts_at <= now,
                            SubscriptionEntitlementTerm.ends_at > now,
                        )
                        .with_for_update()
                        if lock_rows
                        else select(SubscriptionEntitlementTerm).where(
                            SubscriptionEntitlementTerm.subscription_id.in_(related_ids),
                            SubscriptionEntitlementTerm.starts_at <= now,
                            SubscriptionEntitlementTerm.ends_at > now,
                        )
                    )
                )
            ).scalars()
        )
        if related_ids
        else []
    )
    active_terms: dict[int, SubscriptionEntitlementTerm] = {}
    for term in terms:
        if term.subscription_id in active_terms:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail='An active access-point entitlement term is ambiguous; reconcile before planning',
            )
        active_terms[term.subscription_id] = term
    return selected, related, active_terms


def _plan_preimage_subscriptions(
    *,
    tariff_id: int,
    subscriptions: list[Subscription],
    active_terms: dict[int, SubscriptionEntitlementTerm],
) -> tuple[list[dict[str, object]], dict[str, dict[str, object]]]:
    """Build immutable server-only preimage, including raw projection evidence."""

    preimage: list[dict[str, object]] = []
    identities: dict[str, dict[str, object]] = {}
    for subscription in sorted(subscriptions, key=lambda item: item.id):
        # A user-scoped fallback is intentionally stable when this historical
        # row has no Panel identity yet; an executor must still revalidate it.
        panel_identity = subscription.remnawave_uuid or f'user:{subscription.user_id}'
        term = active_terms.get(subscription.id)
        term_preimage: dict[str, object] | None = None
        if term is not None:
            term_preimage = {
                'term_id': term.id,
                'term_version': term.term_version,
                'starts_at': _timestamp(term.starts_at),
                'ends_at': _timestamp(term.ends_at),
                'access_point_ids': list(term.access_point_ids or []),
                'technical_squad_keys': list(term.technical_squad_keys or []),
                'inventory_fingerprint': term.inventory_fingerprint,
                'grant_hash': term.grant_hash,
            }
        preimage.append(
            {
                'subscription_id': subscription.id,
                'user_id': subscription.user_id,
                'tariff_id': subscription.tariff_id,
                'status': subscription.status,
                'selected_tariff_scope': subscription.tariff_id == tariff_id,
                'panel_identity': panel_identity,
                'connected_squads': list(subscription.connected_squads or []),
                'active_term': term_preimage,
                'updated_at': _timestamp(subscription.updated_at),
            }
        )
        aggregate = identities.setdefault(
            panel_identity,
            {
                'panel_identity': panel_identity,
                'subscription_ids': [],
                'selected_tariff_subscription_ids': [],
                'excluded_tariff_subscription_ids': [],
                'active_internal_squads': [],
                'term_grant_hashes': [],
            },
        )
        aggregate['subscription_ids'].append(subscription.id)
        if subscription.tariff_id == tariff_id:
            aggregate['selected_tariff_subscription_ids'].append(subscription.id)
        else:
            aggregate['excluded_tariff_subscription_ids'].append(subscription.id)
        aggregate['active_internal_squads'] = sorted(
            set(aggregate['active_internal_squads']).union(subscription.connected_squads or [])
        )
        if term is not None:
            aggregate['term_grant_hashes'] = sorted(set(aggregate['term_grant_hashes']).union([term.grant_hash]))
    return preimage, {identity: identities[identity] for identity in sorted(identities)}


async def _access_point_plan_target(
    db: AsyncSession,
    tariff: Tariff,
    *,
    lock_evidence: bool,
) -> tuple[object, dict[str, PublicAccessPoint]]:
    """Resolve exactly the stored future policy; no caller may supply a shadow target."""

    try:
        resolved = await resolve_tariff_entitlement(
            db,
            tariff,
            access_point_quote_context=True,
            lock_access_point_evidence=lock_evidence,
        )
    except EntitlementResolutionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    if resolved.provenance != 'access_point_policy' or not resolved.location_ids:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail='Tariff has no verified access-point policy')
    points = list(
        (
            await db.execute(select(PublicAccessPoint).where(PublicAccessPoint.id.in_(resolved.location_ids)))
        ).scalars()
    )
    point_by_id = {point.id: point for point in points}
    if set(resolved.location_ids) != set(point_by_id):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail='Tariff point titles are incomplete')
    return resolved, point_by_id


@router.post('/access-points/discovery')
async def discover_access_points(
    request: Request,
    apply: bool = False,
    admin: User = Depends(require_permission('remnawave:manage')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> dict[str, object]:
    """Read twice, present a redacted local diff, then optionally apply locally.

    ``apply`` only changes the local catalog.  It does not assign a tariff,
    create a subscription/grant, or touch a Panel object.
    """

    assessment = await read_consistent_inventory(_get_read_only_inventory_client(request))
    result = await apply_catalog_assessment(db, assessment, dry_run=not apply)
    if apply:
        await AuditLogCRUD.create(
            db,
            user_id=admin.id,
            action='access_point_catalog_applied',
            resource_type='access_point_catalog',
            resource_id=assessment.fingerprint,
            details={
                'created_count': len(result.created),
                'updated_count': len(result.updated),
                'inventory_fingerprint': assessment.fingerprint,
                'panel_write': False,
                'tariff_mutation': False,
            },
            status='success',
        )
        await db.commit()
    return {
        'applied': apply,
        'inventory_fingerprint': assessment.fingerprint,
        'created': [item.__dict__ for item in result.created],
        'updated': [item.__dict__ for item in result.updated],
        'panel_write': False,
        'tariff_mutation': False,
    }


@router.get('/access-points/catalog')
async def access_point_catalog(
    _admin: User = Depends(require_permission('tariffs:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> dict[str, object]:
    """Tariff picker catalog; unsafe/reconcile points stay out by design."""

    return {'access_points': [item.__dict__ for item in await list_tariff_assignable_access_points(db)]}


@router.get('/access-points/inventory')
async def access_point_inventory_catalog(
    _admin: User = Depends(require_permission('remnawave:manage')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> dict[str, object]:
    """Operator-only catalog that exposes states/reasons but never raw keys."""

    points = list((await db.execute(select(PublicAccessPoint).order_by(PublicAccessPoint.title))).scalars())
    return {'access_points': [_dto(point) for point in points]}


@router.get('/tariffs/{tariff_id}/access-points')
async def tariff_access_point_policy(
    tariff_id: int,
    _admin: User = Depends(require_permission('tariffs:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> dict[str, object]:
    # Serialise plan creation per tariff. This makes a double-click/retry see
    # the already committed deterministic plan instead of racing its unique
    # immutable hash at commit time.
    tariff = await db.get(Tariff, tariff_id, with_for_update=True)
    if tariff is None:
        raise HTTPException(status_code=404, detail='Tariff not found')
    if tariff.entitlement_mode == 'legacy_snapshot':
        manifest = await db.get(TariffLegacyEntitlementManifest, tariff.id)
        return {
            'tariff_id': tariff.id,
            'mode': tariff.entitlement_mode,
            'editable': False,
            'reason': 'legacy_tariff_needs_dedicated_equivalence_preparation',
            'policy_revision': 0,
            'access_points': [],
            'preserved_legacy_access_points': _legacy_presentation(manifest) if manifest is not None else [],
            'execution_enabled': False,
        }
    if tariff.is_daily:
        return {
            'tariff_id': tariff.id,
            'mode': tariff.entitlement_mode,
            'editable': False,
            'reason': 'daily_tariff_not_supported_for_access_points',
            'policy_revision': int(tariff.access_point_policy_revision or 0),
            'access_points': [],
            'preserved_legacy_access_points': [],
            'execution_enabled': False,
        }
    if tariff.entitlement_mode == 'location_managed':
        # Location-managed tariffs retain their dedicated policy editor.  An
        # empty AP picker here would falsely advertise a conversion path that
        # the writer deliberately rejects.
        return {
            'tariff_id': tariff.id,
            'mode': tariff.entitlement_mode,
            'editable': False,
            'reason': 'location_managed_tariff_uses_public_locations',
            'policy_revision': 0,
            'access_points': [],
            'preserved_legacy_access_points': [],
            'execution_enabled': False,
        }
    selected: set[str] = set()
    revision = int(getattr(tariff, 'access_point_policy_revision', 0) or 0)
    if revision:
        policy = await db.scalar(
            select(TariffAccessPointPolicyRevision).where(
                TariffAccessPointPolicyRevision.tariff_id == tariff.id,
                TariffAccessPointPolicyRevision.revision == revision,
            )
        )
        if policy is not None:
            selected = set(
                (
                    await db.execute(
                        select(TariffAccessPointPolicyItem.public_access_point_id).where(
                            TariffAccessPointPolicyItem.policy_revision_id == policy.id
                        )
                    )
                ).scalars()
            )
    points = list(
        (
            await db.execute(
                select(PublicAccessPoint)
                .where(PublicAccessPoint.state == 'verified', PublicAccessPoint.tariff_assignable.is_(True))
                .order_by(PublicAccessPoint.title, PublicAccessPoint.id)
            )
        ).scalars()
    )
    return {
        'tariff_id': tariff.id,
        'mode': tariff.entitlement_mode,
        'editable': True,
        'policy_revision': revision,
        'access_points': [_dto(point, selected=point.id in selected) for point in points],
        'preserved_legacy_access_points': [],
        'execution_enabled': False,
    }


@router.put('/tariffs/{tariff_id}/access-points')
async def replace_access_point_policy(
    tariff_id: int,
    request: AccessPointPolicyRequest,
    admin: User = Depends(require_permission('tariffs:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> dict[str, object]:
    """Append a future-only policy revision with an optimistic-lock fence."""

    tariff = await db.get(Tariff, tariff_id, with_for_update=True)
    if tariff is None:
        raise HTTPException(status_code=404, detail='Tariff not found')
    try:
        policy = await replace_tariff_access_point_policy(
            db,
            tariff,
            access_point_ids=request.access_point_ids,
            expected_revision=request.expected_revision,
            actor_user_id=admin.id,
            reason=request.reason,
        )
    except AccessPointPolicyError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    await AuditLogCRUD.create(
        db,
        user_id=admin.id,
        action='tariff_access_point_policy_appended',
        resource_type='tariff',
        resource_id=str(tariff.id),
        details={
            'policy_revision': policy.revision,
            'selection_hash': policy.selection_hash,
            'inventory_fingerprint': policy.inventory_fingerprint,
            'reason': request.reason,
            'active_subscription_mutation': False,
        },
        status='success',
    )
    await db.commit()
    return {
        'tariff_id': tariff.id,
        'mode': tariff.entitlement_mode,
        'policy_revision': policy.revision,
        'selection_hash': policy.selection_hash,
        'execution_enabled': False,
    }


@router.post('/tariffs/{tariff_id}/access-points/prepare-plan')
async def prepare_access_point_plan(
    tariff_id: int,
    request: AccessPointPlanRequest,
    admin: User = Depends(require_permission('remnawave:manage')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> dict[str, object]:
    """Persist a non-executable AP plan from an already-audited policy.

    This endpoint intentionally has no Panel dependency and no mutation of a
    subscription, term, tariff policy, conversion record or projection outbox.
    It only freezes the evidence a later protected executor would need.
    """

    tariff = await db.scalar(select(Tariff).where(Tariff.id == tariff_id).with_for_update())
    if tariff is None:
        raise HTTPException(status_code=404, detail='Tariff not found')

    selected, related, active_terms = await _plan_subscriptions(db, tariff_id=tariff_id)
    preimage_subscriptions, identities = _plan_preimage_subscriptions(
        tariff_id=tariff_id,
        subscriptions=related,
        active_terms=active_terms,
    )
    selected_ids = {subscription.id for subscription in selected}
    related_selected = [subscription for subscription in related if subscription.id in selected_ids]
    excluded_ids = {subscription.id for subscription in related if subscription.id not in selected_ids}

    if tariff.entitlement_mode == 'legacy_snapshot':
        manifest = await db.get(TariffLegacyEntitlementManifest, tariff.id)
        if manifest is None or list(tariff.allowed_squads or []) != list(manifest.squad_uuids or []):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail='Legacy tariff evidence is incomplete or changed; reconcile before conversion preparation',
            )
        legacy_access = _legacy_presentation(manifest)
        if not legacy_access:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail='Legacy conversion preparation has no approved business presentation',
            )
        scope = {
            'kind': 'access_point_legacy_conversion_preparation',
            'tariff_id': tariff_id,
            'legacy_manifest_hash': manifest.manifest_hash,
            'policy_revision': int(tariff.access_point_policy_revision or 0),
            'affected_subscription_ids': sorted(selected_ids),
            'affected_identity_count': len(identities),
            'excluded_subscription_ids': sorted(excluded_ids),
            'requires_dedicated_equivalence_preparation': True,
            'execution_enabled': False,
        }
        preimage = {
            'tariff': {
                'tariff_id': tariff.id,
                'mode': tariff.entitlement_mode,
                'allowed_squads': list(tariff.allowed_squads or []),
                'updated_at': _timestamp(tariff.updated_at),
            },
            'legacy_manifest': {
                'manifest_hash': manifest.manifest_hash,
                'squad_uuids': list(manifest.squad_uuids or []),
                'membership_hashes': dict(manifest.membership_hashes or {}),
                'presentation_locations': list(manifest.presentation_locations or []),
            },
            'subscriptions': preimage_subscriptions,
            'panel_identity_scope': list(identities.values()),
            'execution_enabled': False,
        }
        manifest_hash = _immutable_hash(
            {
                'legacy_manifest_hash': manifest.manifest_hash,
                'presentation_locations': legacy_access,
                'requires_dedicated_equivalence_preparation': True,
            }
        )
        public_diff = {
            'preserved_legacy_access_points': legacy_access,
            'target_access_points': [],
            'added_access_points': [],
            'removed_access_points': [],
            'requires_dedicated_equivalence_preparation': True,
        }
    elif tariff.entitlement_mode == 'access_point_managed':
        resolved, point_by_id = await _access_point_plan_target(db, tariff, lock_evidence=False)
        target_ids = set(resolved.location_ids)
        old_ids = {
            point_id
            for subscription in related_selected
            for point_id in (active_terms.get(subscription.id).access_point_ids if active_terms.get(subscription.id) else [])
        }
        point_ids = target_ids.union(old_ids)
        if point_ids - set(point_by_id):
            extra_points = list(
                (
                    await db.execute(select(PublicAccessPoint).where(PublicAccessPoint.id.in_(point_ids)))
                ).scalars()
            )
            point_by_id.update({point.id: point for point in extra_points})
        missing_term_ids = sorted(
            subscription.id for subscription in related_selected if subscription.id not in active_terms
        )
        scope = {
            'kind': 'access_point_active_user_preview',
            'tariff_id': tariff_id,
            'policy_revision': int(tariff.access_point_policy_revision or 0),
            'inventory_fingerprint': resolved.inventory_fingerprint,
            'target_access_point_ids': sorted(target_ids),
            'affected_subscription_ids': sorted(selected_ids),
            'affected_identity_count': len(identities),
            'excluded_subscription_ids': sorted(excluded_ids),
            'missing_active_term_subscription_ids': missing_term_ids,
            'execution_enabled': False,
        }
        preimage = {
            'tariff': {
                'tariff_id': tariff.id,
                'mode': tariff.entitlement_mode,
                'policy_revision': int(tariff.access_point_policy_revision or 0),
                'inventory_fingerprint': resolved.inventory_fingerprint,
                'target_access_point_ids': sorted(target_ids),
                'target_technical_squad_keys': list(resolved.squad_uuids),
                'updated_at': _timestamp(tariff.updated_at),
            },
            'subscriptions': preimage_subscriptions,
            'panel_identity_scope': list(identities.values()),
            'execution_enabled': False,
        }
        manifest_hash = _immutable_hash(
            {
                'policy_revision': scope['policy_revision'],
                'inventory_fingerprint': resolved.inventory_fingerprint,
                'target_access_point_ids': scope['target_access_point_ids'],
            }
        )
        public_diff = {
            'preserved_legacy_access_points': [],
            'target_access_points': _plan_public_points(point_by_id, target_ids),
            'added_access_points': _plan_public_points(point_by_id, target_ids - old_ids),
            'removed_access_points': _plan_public_points(point_by_id, old_ids - target_ids),
            'preserved_access_points': _plan_public_points(point_by_id, old_ids.intersection(target_ids)),
            'requires_dedicated_equivalence_preparation': False,
        }
    else:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail='A point plan requires an existing verified access-point policy or approved legacy manifest',
        )

    plan_hash = _immutable_hash(
        {
            'scope': scope,
            'preimage': preimage,
            'actor_user_id': admin.id,
            'reason': request.reason,
        }
    )
    response = {
        'plan_id': None,
        'state': 'previewed',
        'plan_hash': plan_hash,
        'manifest_hash': manifest_hash,
        'affected_subscription_count': len(selected_ids),
        'affected_identity_count': len(identities),
        'excluded_subscription_count': len(excluded_ids),
        'missing_active_term_subscription_count': len(
            (scope or {}).get('missing_active_term_subscription_ids') or []
        ),
        'execution_enabled': False,
        **public_diff,
    }
    existing_plan = await db.scalar(
        select(EntitlementChangePlan).where(EntitlementChangePlan.plan_hash == plan_hash)
    )
    if existing_plan is not None:
        # ``plan_hash`` includes actor and reason, so this can only be the
        # exact same immutable request. Return it idempotently and release the
        # tariff row lock without emitting a second audit event.
        response['plan_id'] = existing_plan.id
        response['state'] = existing_plan.state
        await db.commit()
        return response
    plan = EntitlementChangePlan(
        id=str(uuid4()),
        state='previewed',
        actor_user_id=admin.id,
        reason=request.reason,
        manifest_hash=manifest_hash,
        plan_hash=plan_hash,
        policy_revision=int(scope['policy_revision']),
        scope=scope,
        preimage=preimage,
    )
    db.add(plan)
    await AuditLogCRUD.create(
        db,
        user_id=admin.id,
        action='tariff_access_point_plan_previewed',
        resource_type='entitlement_plan',
        resource_id=plan.id,
        details={
            'tariff_id': tariff_id,
            'kind': scope['kind'],
            'plan_hash': plan_hash,
            'manifest_hash': manifest_hash,
            'affected_subscription_count': len(selected_ids),
            'affected_identity_count': len(identities),
            'excluded_subscription_count': len(excluded_ids),
            'execution_enabled': False,
        },
        status='success',
    )
    await db.commit()
    response['plan_id'] = plan.id
    return response


@router.post('/tariffs/{tariff_id}/access-points/plans/{plan_id}/confirm')
async def confirm_access_point_plan(
    tariff_id: int,
    plan_id: str,
    request: AccessPointPlanConfirmationRequest,
    admin: User = Depends(require_permission('remnawave:manage')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> dict[str, object]:
    """Confirm a frozen plan, explicitly without enabling execution."""

    plan = await db.scalar(select(EntitlementChangePlan).where(EntitlementChangePlan.id == plan_id).with_for_update())
    if plan is None or int((plan.scope or {}).get('tariff_id', -1)) != tariff_id:
        raise HTTPException(status_code=404, detail='Access-point plan not found for this tariff')
    expected_plan_hash = _immutable_hash(
        {
            'scope': plan.scope,
            'preimage': plan.preimage,
            'actor_user_id': plan.actor_user_id,
            'reason': plan.reason,
        }
    )
    if plan.state != 'previewed' or request.plan_hash != plan.plan_hash or expected_plan_hash != plan.plan_hash:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail='Plan is stale or its immutable hash does not match',
        )
    tariff = await db.scalar(select(Tariff).where(Tariff.id == tariff_id).with_for_update())
    if tariff is None:
        raise HTTPException(status_code=404, detail='Tariff not found')

    # A confirmation remains non-executable, but it is still durable audit
    # evidence.  Do not acknowledge a preview once its subscription scope,
    # shared Panel identities, status, technical preimage or active-term grant
    # has changed.  First fence all subscription DML, then lock the exact
    # rows we observed before comparing their complete server-only snapshot.
    await _lock_subscription_table_for_plan_confirmation(db)
    _selected, related, active_terms = await _plan_subscriptions(db, tariff_id=tariff_id, lock_rows=True)
    current_subscriptions, current_identities = _plan_preimage_subscriptions(
        tariff_id=tariff_id,
        subscriptions=related,
        active_terms=active_terms,
    )
    frozen_preimage = plan.preimage or {}
    if (
        frozen_preimage.get('subscriptions') != current_subscriptions
        or frozen_preimage.get('panel_identity_scope') != list(current_identities.values())
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail='Subscription scope or active entitlement evidence changed; prepare a new plan',
        )

    kind = str((plan.scope or {}).get('kind') or '')
    if kind == 'access_point_active_user_preview':
        if int(tariff.access_point_policy_revision or 0) != plan.policy_revision:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail='Tariff policy revision changed; prepare a new plan',
            )
        resolved, _point_by_id = await _access_point_plan_target(db, tariff, lock_evidence=True)
        if (
            resolved.inventory_fingerprint != (plan.scope or {}).get('inventory_fingerprint')
            or sorted(resolved.location_ids) != list((plan.scope or {}).get('target_access_point_ids') or [])
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail='Tariff point evidence changed; prepare a new plan',
            )
    elif kind == 'access_point_legacy_conversion_preparation':
        manifest = await db.get(TariffLegacyEntitlementManifest, tariff_id)
        if (
            tariff.entitlement_mode != 'legacy_snapshot'
            or manifest is None
            or manifest.manifest_hash != (plan.scope or {}).get('legacy_manifest_hash')
            or list(tariff.allowed_squads or []) != list(manifest.squad_uuids or [])
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail='Legacy conversion evidence changed; prepare a new plan',
            )
    else:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail='Plan kind cannot be confirmed')

    existing = await db.scalar(select(EntitlementPlanApproval).where(EntitlementPlanApproval.plan_id == plan.id))
    if existing is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail='Plan already has a durable confirmation')
    plan.state = 'confirmed_execution_disabled'
    db.add(
        EntitlementPlanApproval(
            plan_id=plan.id,
            actor_user_id=admin.id,
            permission='remnawave:manage',
            reason=request.reason,
            immutable_hash=plan.plan_hash,
        )
    )
    await AuditLogCRUD.create(
        db,
        user_id=admin.id,
        action='tariff_access_point_plan_confirmed_execution_disabled',
        resource_type='entitlement_plan',
        resource_id=plan.id,
        details={
            'tariff_id': tariff_id,
            'kind': kind,
            'plan_hash': plan.plan_hash,
            'policy_revision': plan.policy_revision,
            'execution_enabled': False,
            'reason': request.reason,
        },
        status='success',
    )
    await db.commit()
    return {
        'plan_id': plan.id,
        'state': plan.state,
        'plan_hash': plan.plan_hash,
        'execution_enabled': False,
    }
