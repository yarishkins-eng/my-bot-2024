"""Admin public-location policy routes.

No endpoint in this module can send a Panel request.  Editing a tariff affects
future resolver calls only; changing active subscriptions requires a separately
confirmed plan and this build registers no execution route or worker.
"""

from __future__ import annotations

import json
from hashlib import sha256
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.rbac import AuditLogCRUD
from app.database.models import (
    EntitlementChangePlan,
    EntitlementPlanApproval,
    PublicLocation,
    PublicLocationSquadMapping,
    Subscription,
    SubscriptionEntitlementSnapshot,
    Tariff,
    TariffLocationEntitlement,
    User,
)
from app.services.public_location_entitlement_service import list_tariff_assignable_locations

from ..dependencies import get_cabinet_db, require_permission


router = APIRouter(prefix='/admin', tags=['Cabinet Admin Public Locations'])


class LocationPolicyRequest(BaseModel):
    location_ids: list[str] = Field(default_factory=list)
    reason: str = Field(min_length=3, max_length=500)


class LocationPlanConfirmationRequest(BaseModel):
    plan_hash: str = Field(min_length=64, max_length=128)
    reason: str = Field(min_length=3, max_length=500)


def _timestamp(value: object) -> str | None:
    """Make ORM timestamps safe to retain in the immutable JSON preimage."""
    isoformat = getattr(value, 'isoformat', None)
    return isoformat() if callable(isoformat) else None


def _dto(location: PublicLocation, *, selected: bool = False) -> dict[str, object]:
    return {
        'id': location.id,
        'iso_code': location.iso_code,
        'label_ru': location.label_ru,
        'label_en': location.label_en,
        'flag': location.flag,
        'lifecycle': location.lifecycle,
        'visibility': location.visibility,
        'health': location.health,
        'tariff_assignable': location.tariff_assignable,
        'selected': selected,
    }


@router.get('/public-locations/catalog')
async def public_location_catalog(
    _admin: User = Depends(require_permission('tariffs:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> dict[str, object]:
    """Privileged catalog, intentionally free of Squad UUIDs and raw names."""
    return {'locations': [_dto(location) for location in await list_tariff_assignable_locations(db)]}


@router.get('/tariffs/{tariff_id}/locations')
async def tariff_location_policy(
    tariff_id: int,
    _admin: User = Depends(require_permission('tariffs:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> dict[str, object]:
    tariff = await db.get(Tariff, tariff_id)
    if not tariff:
        raise HTTPException(status_code=404, detail='Tariff not found')
    selected = set(
        (
            await db.execute(
                select(TariffLocationEntitlement.public_location_id).where(
                    TariffLocationEntitlement.tariff_id == tariff_id
                )
            )
        ).scalars()
    )
    return {
        'tariff_id': tariff.id,
        'mode': tariff.entitlement_mode,
        'policy_revision': tariff.location_policy_revision,
        'locations': [
            _dto(location, selected=location.id in selected) for location in await list_tariff_assignable_locations(db)
        ],
        'execution_enabled': False,
    }


@router.put('/tariffs/{tariff_id}/locations')
async def replace_tariff_location_policy(
    tariff_id: int,
    request: LocationPolicyRequest,
    admin: User = Depends(require_permission('tariffs:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> dict[str, object]:
    """Replace only future tariff policy; never mass-sync subscriptions."""
    tariff = await db.get(Tariff, tariff_id)
    if not tariff:
        raise HTTPException(status_code=404, detail='Tariff not found')
    wanted = tuple(dict.fromkeys(request.location_ids))
    assignable = {location.id for location in await list_tariff_assignable_locations(db)}
    if not set(wanted).issubset(assignable):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail='One or more locations are not tariff-assignable'
        )
    if not wanted and tariff.is_active:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail='An active tariff cannot have no locations')

    await db.execute(delete(TariffLocationEntitlement).where(TariffLocationEntitlement.tariff_id == tariff.id))
    next_revision = int(tariff.location_policy_revision or 1) + 1
    for location_id in wanted:
        db.add(
            TariffLocationEntitlement(
                tariff_id=tariff.id, public_location_id=location_id, policy_revision=next_revision
            )
        )
    tariff.location_policy_revision = next_revision
    tariff.entitlement_mode = 'location_managed' if wanted else 'no_locations'
    await AuditLogCRUD.create(
        db,
        user_id=admin.id,
        action='tariff_location_policy_replaced',
        resource_type='tariff',
        resource_id=str(tariff.id),
        details={
            'location_ids': list(wanted),
            'policy_revision': next_revision,
            'reason': request.reason,
            'execution_enabled': False,
        },
        status='success',
    )
    await db.commit()
    return {
        'tariff_id': tariff.id,
        'mode': tariff.entitlement_mode,
        'policy_revision': next_revision,
        'location_ids': list(wanted),
        'execution_enabled': False,
    }


@router.post('/tariffs/{tariff_id}/locations/prepare-plan')
async def prepare_location_plan(
    tariff_id: int,
    request: LocationPolicyRequest,
    admin: User = Depends(require_permission('remnawave:manage')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> dict[str, object]:
    """Persist an immutable, non-executable preview for owner review."""
    tariff = await db.get(Tariff, tariff_id)
    if not tariff:
        raise HTTPException(status_code=404, detail='Tariff not found')
    requested_ids = tuple(sorted(set(request.location_ids)))
    assignable = {location.id for location in await list_tariff_assignable_locations(db)}
    if not requested_ids or not set(requested_ids).issubset(assignable):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail='Plan requires explicit tariff-assignable locations'
        )

    current_ids = tuple(
        sorted(
            (
                await db.execute(
                    select(TariffLocationEntitlement.public_location_id).where(
                        TariffLocationEntitlement.tariff_id == tariff_id
                    )
                )
            ).scalars()
        )
    )
    target_mappings = list(
        (
            await db.execute(
                select(PublicLocationSquadMapping).where(
                    PublicLocationSquadMapping.public_location_id.in_(requested_ids),
                    PublicLocationSquadMapping.is_dedicated_verified.is_(True),
                )
            )
        ).scalars()
    )
    mapped_ids = {mapping.public_location_id for mapping in target_mappings if mapping.internal_squad_uuid}
    if mapped_ids != set(requested_ids):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail='Plan has an incomplete dedicated location mapping'
        )

    affected_subscriptions = list(
        (await db.execute(select(Subscription).where(Subscription.tariff_id == tariff_id))).scalars()
    )
    subscription_ids = tuple(sorted(subscription.id for subscription in affected_subscriptions))
    users_by_id: dict[int, User] = {}
    if affected_subscriptions:
        users = list(
            (
                await db.execute(
                    select(User).where(User.id.in_([subscription.user_id for subscription in affected_subscriptions]))
                )
            ).scalars()
        )
        users_by_id = {user.id: user for user in users}
    affected_user_ids = set(users_by_id)
    affected_panel_ids = {
        subscription.remnawave_uuid for subscription in affected_subscriptions if subscription.remnawave_uuid
    }
    # A future executor operates by panel identity, not one tariff row at a
    # time.  Its durable preimage must therefore include every subscription
    # that shares an affected identity, including other tariffs.
    if affected_user_ids or affected_panel_ids:
        criteria = []
        if affected_user_ids:
            criteria.append(Subscription.user_id.in_(affected_user_ids))
        if affected_panel_ids:
            criteria.append(Subscription.remnawave_uuid.in_(affected_panel_ids))
        panel_subscriptions = list((await db.execute(select(Subscription).where(or_(*criteria)))).scalars())
    else:
        panel_subscriptions = []
    panel_user_ids = {subscription.user_id for subscription in panel_subscriptions}
    missing_user_ids = panel_user_ids.difference(users_by_id)
    if missing_user_ids:
        additional_users = list((await db.execute(select(User).where(User.id.in_(missing_user_ids)))).scalars())
        users_by_id.update({user.id: user for user in additional_users})
    panel_tariff_ids = {subscription.tariff_id for subscription in panel_subscriptions if subscription.tariff_id}
    panel_tariffs = (
        list((await db.execute(select(Tariff).where(Tariff.id.in_(panel_tariff_ids)))).scalars())
        if panel_tariff_ids
        else []
    )
    tariff_by_id = {item.id: item for item in panel_tariffs}
    panel_subscription_ids = tuple(sorted(subscription.id for subscription in panel_subscriptions))
    existing_snapshots = (
        list(
            (
                await db.execute(
                    select(SubscriptionEntitlementSnapshot).where(
                        SubscriptionEntitlementSnapshot.subscription_id.in_(panel_subscription_ids)
                    )
                )
            ).scalars()
        )
        if panel_subscription_ids
        else []
    )
    snapshot_by_subscription = {snapshot.subscription_id: snapshot for snapshot in existing_snapshots}
    preimage_subscriptions = []
    panel_identity_scope: dict[str, dict[str, object]] = {}
    target_squads = [
        mapping.internal_squad_uuid
        for mapping in sorted(target_mappings, key=lambda item: (item.public_location_id, item.internal_squad_uuid))
    ]
    for subscription in sorted(panel_subscriptions, key=lambda item: item.id):
        snapshot = snapshot_by_subscription.get(subscription.id)
        user = users_by_id.get(subscription.user_id)
        panel_identity = subscription.remnawave_uuid or getattr(user, 'remnawave_uuid', None)
        subscription_tariff = tariff_by_id.get(subscription.tariff_id)
        subscription_external_squad_uuid = (
            subscription_tariff.external_squad_uuid if subscription_tariff is not None else None
        )
        active_squads = list(subscription.connected_squads or [])
        snapshot_preimage = None
        if snapshot is not None:
            snapshot_preimage = {
                'snapshot_hash': snapshot.snapshot_hash,
                'tariff_id': snapshot.tariff_id,
                'location_ids': list(snapshot.location_ids or []),
                'activeInternalSquads': list(snapshot.technical_squad_uuids or []),
                'policy_revision': snapshot.policy_revision,
                'provenance': snapshot.provenance,
                'created_at': _timestamp(snapshot.created_at),
            }
        preimage_subscriptions.append(
            {
                'subscription_id': subscription.id,
                'user_id': subscription.user_id,
                'status': subscription.status,
                'tariff_id': subscription.tariff_id,
                'selected_tariff_scope': subscription.tariff_id == tariff_id,
                'panelIdentity': panel_identity,
                'activeInternalSquads': active_squads,
                'externalSquadUuid': subscription_external_squad_uuid,
                'snapshot': snapshot_preimage,
                'updated_at': _timestamp(subscription.updated_at),
            }
        )
        if panel_identity:
            aggregate = panel_identity_scope.setdefault(
                panel_identity,
                {
                    'panelIdentity': panel_identity,
                    'subscription_ids': [],
                    'selected_tariff_subscription_ids': [],
                    'excluded_tariff_subscription_ids': [],
                    'activeInternalSquads': [],
                    'externalSquadUuids': [],
                },
            )
            aggregate['subscription_ids'].append(subscription.id)
            if subscription.tariff_id == tariff_id:
                aggregate['selected_tariff_subscription_ids'].append(subscription.id)
            else:
                aggregate['excluded_tariff_subscription_ids'].append(subscription.id)
            aggregate['activeInternalSquads'] = sorted(set(aggregate['activeInternalSquads']).union(active_squads))
            aggregate['externalSquadUuids'] = sorted(
                set(aggregate['externalSquadUuids']).union(
                    [subscription_external_squad_uuid] if subscription_external_squad_uuid else []
                )
            )

    for aggregate in panel_identity_scope.values():
        preserved_squads = set()
        preserved_external_squads = set()
        for subscription in preimage_subscriptions:
            if (
                subscription['panelIdentity'] == aggregate['panelIdentity']
                and not subscription['selected_tariff_scope']
            ):
                preserved_squads.update(subscription['activeInternalSquads'])
                if subscription['externalSquadUuid']:
                    preserved_external_squads.add(subscription['externalSquadUuid'])
        aggregate['targetActiveInternalSquads'] = sorted(preserved_squads.union(target_squads))
        aggregate['targetExternalSquadUuid'] = tariff.external_squad_uuid
        aggregate['targetExternalSquadUuids'] = sorted(
            preserved_external_squads.union([tariff.external_squad_uuid] if tariff.external_squad_uuid else [])
        )

    dedicated_mapping_preimage = [
        {
            'location_id': mapping.public_location_id,
            'inventory_revision': mapping.inventory_revision,
            'membership_hash': mapping.inventory_membership_hash,
            'technical_squad_uuid': mapping.internal_squad_uuid,
            'verified_at': _timestamp(mapping.created_at),
        }
        for mapping in sorted(target_mappings, key=lambda item: (item.public_location_id, item.internal_squad_uuid))
    ]
    target = {
        'location_ids': list(requested_ids),
        'activeInternalSquads': target_squads,
        'externalSquadUuid': tariff.external_squad_uuid,
        'excluded_location_ids': sorted(set(current_ids).difference(requested_ids)),
        'exceptions': [],
        'dedicated_mappings': dedicated_mapping_preimage,
        'per_panel_identity': list(panel_identity_scope.values()),
    }
    manifest = {
        'target': target,
    }
    manifest_hash = sha256(json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    scope = {
        'tariff_id': tariff_id,
        'selected_tariff_ids': [tariff_id],
        'excluded_tariff_ids': [],
        'requested_location_ids': list(requested_ids),
        'excluded_location_ids': target['excluded_location_ids'],
        'exceptions': [],
        'affected_subscription_ids': list(subscription_ids),
        'panel_identity_scope': list(panel_identity_scope.values()),
        'policy_revision': int(tariff.location_policy_revision or 1),
        'execution_enabled': False,
    }
    preimage = {
        'tariff': {
            'tariff_id': tariff.id,
            'mode': tariff.entitlement_mode,
            'policy_revision': int(tariff.location_policy_revision or 1),
            'current_location_ids': list(current_ids),
            'activeInternalSquads': list(tariff.allowed_squads or []),
            'externalSquadUuid': tariff.external_squad_uuid,
            'updated_at': _timestamp(tariff.updated_at),
        },
        'subscriptions': preimage_subscriptions,
        'target': target,
        'manifest_hash': manifest_hash,
        'execution_enabled': False,
    }
    plan_hash = sha256(
        json.dumps({'scope': scope, 'preimage': preimage}, sort_keys=True, separators=(',', ':')).encode()
    ).hexdigest()
    plan = EntitlementChangePlan(
        id=str(uuid4()),
        actor_user_id=admin.id,
        reason=request.reason,
        manifest_hash=manifest_hash,
        plan_hash=plan_hash,
        policy_revision=int(tariff.location_policy_revision or 1),
        scope=scope,
        preimage=preimage,
    )
    db.add(plan)
    await AuditLogCRUD.create(
        db,
        user_id=admin.id,
        action='tariff_location_plan_previewed',
        resource_type='entitlement_plan',
        resource_id=plan.id,
        details={
            'tariff_id': tariff_id,
            'plan_hash': plan_hash,
            'manifest_hash': manifest_hash,
            'affected_subscription_count': len(subscription_ids),
            'reason': request.reason,
            'execution_enabled': False,
        },
        status='success',
    )
    await db.commit()
    return {
        'plan_id': plan.id,
        'state': 'previewed',
        'plan_hash': plan_hash,
        'manifest_hash': manifest_hash,
        'affected_subscription_count': len(subscription_ids),
        'execution_enabled': False,
    }


@router.post('/tariffs/{tariff_id}/locations/plans/{plan_id}/confirm')
async def confirm_location_plan(
    tariff_id: int,
    plan_id: str,
    request: LocationPlanConfirmationRequest,
    admin: User = Depends(require_permission('remnawave:manage')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> dict[str, object]:
    """Durably approve an immutable preview without registering execution."""
    plan = await db.get(EntitlementChangePlan, plan_id)
    if plan is None or int((plan.scope or {}).get('tariff_id', -1)) != tariff_id:
        raise HTTPException(status_code=404, detail='Entitlement plan not found for this tariff')
    expected_plan_hash = sha256(
        json.dumps({'scope': plan.scope, 'preimage': plan.preimage}, sort_keys=True, separators=(',', ':')).encode()
    ).hexdigest()
    if plan.state != 'previewed' or request.plan_hash != plan.plan_hash or expected_plan_hash != plan.plan_hash:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail='Plan is stale or its immutable hash does not match'
        )
    tariff = await db.get(Tariff, tariff_id)
    if tariff is None or int(tariff.location_policy_revision or 1) != plan.policy_revision:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail='Tariff policy revision changed; prepare a new plan'
        )
    existing_approval = await db.scalar(
        select(EntitlementPlanApproval).where(EntitlementPlanApproval.plan_id == plan.id)
    )
    if existing_approval is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail='Plan already has a durable confirmation')

    plan.state = 'confirmed_execution_disabled'
    approval = EntitlementPlanApproval(
        plan_id=plan.id,
        actor_user_id=admin.id,
        permission='remnawave:manage',
        reason=request.reason,
        immutable_hash=plan.plan_hash,
    )
    db.add(approval)
    await AuditLogCRUD.create(
        db,
        user_id=admin.id,
        action='tariff_location_plan_confirmed_execution_disabled',
        resource_type='entitlement_plan',
        resource_id=plan.id,
        details={
            'tariff_id': tariff_id,
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
