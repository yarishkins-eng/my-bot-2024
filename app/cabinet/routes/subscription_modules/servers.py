"""Public-location subscription endpoints.

The historical ``/countries`` mutation accepted raw RemnaWave UUIDs.  It is
kept only as a hard-closed compatibility boundary; clients must move to the
versioned, entitlement-filtered read endpoint.  No money, database mutation,
or Panel call is reachable through the retired route.
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query as QueryParam, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import (
    PublicAccessPoint,
    PublicLocation,
    SubscriptionEntitlementSnapshot,
    TariffLegacyEntitlementManifest,
    User,
)

from ...dependencies import get_cabinet_db, get_current_cabinet_user
from .helpers import resolve_subscription


logger = structlog.get_logger(__name__)

router = APIRouter()


@router.get('/countries')
async def get_available_countries(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
    subscription_id: int | None = QueryParam(None, description='Subscription ID for multi-tariff'),
) -> dict[str, Any]:
    """Legacy read adapter returning public DTOs only, never UUIDs."""
    subscription = await resolve_subscription(db, user, subscription_id)
    if not subscription:
        return {'locations': [], 'has_subscription': False, 'legacy_adapter': True}
    snapshot = await db.scalar(
        select(SubscriptionEntitlementSnapshot).where(
            SubscriptionEntitlementSnapshot.subscription_id == subscription.id
        )
    )
    if not snapshot:
        # A pre-cutover subscription has no owner-approved public presentation
        # manifest.  Returning an empty DTO is safer than leaking raw squads.
        return {
            'locations': [],
            'has_subscription': True,
            'legacy_adapter': True,
            'reason': 'legacy_snapshot_unavailable',
        }
    if snapshot.provenance.startswith('legacy_'):
        manifest = await db.get(TariffLegacyEntitlementManifest, snapshot.tariff_id)
        if not manifest:
            return {
                'locations': [],
                'has_subscription': True,
                'legacy_adapter': True,
                'reason': 'legacy_presentation_unavailable',
            }
        presentation = [
            {
                'id': str(item['id']),
                'iso_code': str(item['iso_code']),
                'label_ru': str(item['label_ru']),
                'label_en': str(item['label_en']),
                'flag': str(item['flag']),
                'lifecycle': str(item['lifecycle']),
            }
            for item in (manifest.presentation_locations or [])
            if isinstance(item, dict)
            and all(key in item for key in ('id', 'iso_code', 'label_ru', 'label_en', 'flag', 'lifecycle'))
        ]
        return {
            'locations': presentation,
            'has_subscription': True,
            'legacy_adapter': True,
            'legacy_presentation': True,
        }
    locations = list(
        (await db.execute(select(PublicLocation).where(PublicLocation.id.in_(snapshot.location_ids or [])))).scalars()
    )
    return {
        'locations': [
            {
                'id': location.id,
                'iso_code': location.iso_code,
                'label_ru': location.label_ru,
                'label_en': location.label_en,
                'flag': location.flag,
                'lifecycle': location.lifecycle,
            }
            for location in sorted(locations, key=lambda item: (item.sort_order, item.label_en))
        ],
        'has_subscription': True,
        'legacy_adapter': True,
    }


@router.post('/countries')
async def update_countries(
    request: dict[str, Any],
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
    subscription_id: int | None = QueryParam(None, description='Subscription ID for multi-tariff'),
) -> dict[str, Any]:
    """Hard-close raw UUID writes before any side effect."""
    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail='Raw country UUID mutation is retired; use the public-location entitlement flow.',
    )


@router.get('/v2/locations')
async def get_effective_locations(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
    subscription_id: int | None = QueryParam(None, description='Subscription ID for multi-tariff'),
) -> dict[str, Any]:
    """Versioned public DTO; technical squads are never present in this API."""
    return await get_available_countries(user=user, db=db, subscription_id=subscription_id)


@router.get('/v3/access-points')
async def get_effective_access_points(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
    subscription_id: int | None = QueryParam(None, description='Subscription ID for multi-tariff'),
) -> dict[str, Any]:
    """Return only the user's captured Host titles, never Panel identities.

    A retired point remains visible when it belongs to an already-issued term;
    access for a new term is guarded by the resolver before any payment or
    Panel effect.  This endpoint is deliberately read-only.
    """

    subscription = await resolve_subscription(db, user, subscription_id)
    if not subscription:
        return {'access_points': [], 'has_subscription': False}
    from app.services.public_location_entitlement_service import (
        EntitlementResolutionError,
        get_effective_subscription_resolved_entitlement,
    )

    try:
        entitlement = await get_effective_subscription_resolved_entitlement(db, subscription.id)
    except EntitlementResolutionError:
        # A malformed historical snapshot is not permission to leak a raw
        # projection or guess a current tariff policy.
        return {'access_points': [], 'has_subscription': True}
    if entitlement.provenance != 'access_point_policy':
        return {'access_points': [], 'has_subscription': True}
    point_ids = list(entitlement.location_ids)
    points = list((await db.execute(select(PublicAccessPoint).where(PublicAccessPoint.id.in_(point_ids)))).scalars())
    by_id = {point.id: point for point in points}
    return {
        'access_points': [
            {
                'id': point.id,
                'title': point.title,
                'presentation_revision': int(point.presentation_revision or 1),
            }
            for point_id in point_ids
            if (point := by_id.get(point_id)) is not None
        ],
        'has_subscription': True,
    }
