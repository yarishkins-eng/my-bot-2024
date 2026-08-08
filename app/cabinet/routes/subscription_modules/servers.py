"""Public-location subscription endpoints.

The historical ``/countries`` mutation accepted raw RemnaWave UUIDs.  It is
kept only as a hard-closed compatibility boundary; clients must move to the
versioned, entitlement-filtered read endpoint.  No money, database mutation,
or Panel call is reachable through the retired route.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query as QueryParam, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import PublicLocation, SubscriptionEntitlementSnapshot, TariffLegacyEntitlementManifest, User

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


async def _retired_raw_update_countries(
    request: dict[str, Any],
    user: User,
    db: AsyncSession,
    subscription_id: int | None,
) -> dict[str, Any]:
    """Unreachable old implementation retained only for short-term source diff review."""
    from app.database.crud.server_squad import add_user_to_servers, get_available_server_squads, get_server_ids_by_uuids
    from app.database.crud.subscription import add_subscription_servers
    from app.database.crud.transaction import create_transaction
    from app.database.crud.user import subtract_user_balance
    from app.database.models import TransactionType
    from app.utils.pricing_utils import apply_percentage_discount, calculate_prorated_price

    subscription = await resolve_subscription(db, user, subscription_id)

    if not subscription:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='No subscription found',
        )

    if subscription.is_trial:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Country management is not available for trial subscriptions',
        )

    selected_countries = request.get('countries', [])
    if not selected_countries:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='At least one country must be selected',
        )

    current_countries = subscription.connected_squads or []
    promo_group_id = user.promo_group_id

    available_servers = await get_available_server_squads(db, promo_group_id=promo_group_id)
    allowed_country_ids = {server.squad_uuid for server in available_servers}

    # Validate selected countries
    for country_uuid in selected_countries:
        if country_uuid not in allowed_country_ids:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f'Country {country_uuid} is not available',
            )

    added = [c for c in selected_countries if c not in current_countries]
    removed = [c for c in current_countries if c not in selected_countries]

    if not added and not removed:
        return {
            'message': 'No changes detected',
            'connected_squads': current_countries,
        }

    # Lock user row to prevent TOCTOU on promo-offer state
    from app.database.crud.user import lock_user_for_pricing

    user = await lock_user_for_pricing(db, user.id)

    # Calculate cost for added servers
    total_cost = 0
    added_names = []
    removed_names = []

    from app.services.pricing_engine import PricingEngine

    servers_discount_percent = PricingEngine.get_addon_discount_percent(user, 'servers', None)

    added_server_prices = []

    for server in available_servers:
        if server.squad_uuid in added:
            server_price_per_month = server.price_kopeks
            if servers_discount_percent > 0:
                discounted_per_month, _ = apply_percentage_discount(
                    server_price_per_month,
                    servers_discount_percent,
                )
            else:
                discounted_per_month = server_price_per_month

            charged_price, charged_days = calculate_prorated_price(
                discounted_per_month,
                subscription.end_date,
            )

            total_cost += charged_price
            added_names.append(server.display_name)
            added_server_prices.append(charged_price)

        if server.squad_uuid in removed:
            removed_names.append(server.display_name)

    # Check balance
    if total_cost > 0 and user.balance_kopeks < total_cost:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=f'Insufficient balance. Need {total_cost / 100:.2f} RUB, have {user.balance_kopeks / 100:.2f} RUB',
        )

    # Deduct balance and update subscription
    if added and total_cost > 0:
        success = await subtract_user_balance(db, user, total_cost, f'Adding countries: {", ".join(added_names)}')
        if not success:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail='Failed to charge balance',
            )

        await create_transaction(
            db=db,
            user_id=user.id,
            type=TransactionType.SUBSCRIPTION_PAYMENT,
            amount_kopeks=total_cost,
            description=f'Adding countries to subscription: {", ".join(added_names)}',
        )

    # Add servers to subscription
    if added:
        added_server_ids = await get_server_ids_by_uuids(db, added)
        if added_server_ids:
            await add_subscription_servers(db, subscription, added_server_ids, added_server_prices)
            try:
                await add_user_to_servers(db, added_server_ids)
            except Exception as e:
                logger.error('Ошибка обновления счётчика серверов', error=e)

    # Update connected squads
    subscription.connected_squads = selected_countries
    subscription.updated_at = datetime.now(UTC)
    await db.commit()

    # Sync with RemnaWave
    try:
        from app.config import settings

        subscription_service = SubscriptionService()
        _has_panel = (
            getattr(subscription, 'remnawave_uuid', None)
            if settings.is_multi_tariff_enabled()
            else getattr(user, 'remnawave_uuid', None)
        )
        if _has_panel:
            await subscription_service.update_remnawave_user(db, subscription, sync_squads=True)
        else:
            await subscription_service.create_remnawave_user(db, subscription)
    except Exception as e:
        logger.error('Failed to sync countries with RemnaWave', error=e)
        from app.services.remnawave_retry_queue import remnawave_retry_queue

        remnawave_retry_queue.enqueue(
            subscription_id=subscription.id,
            user_id=user.id,
            action='update' if _has_panel else 'create',
        )

    await db.refresh(subscription)

    # Yandex.Metrika offline conversion — only when charges happened (adding
    # paid countries). Sibling to #558449 — this endpoint uses a free-form
    # dict body so we read `yandex_cid` defensively (validation matches
    # the regex used by the typed schemas).
    if total_cost > 0:
        raw_cid = request.get('yandex_cid')
        cid: str | None = None
        if isinstance(raw_cid, str):
            import re

            if re.fullmatch(r'[A-Za-z0-9._:-]{4,128}', raw_cid):
                cid = raw_cid
        try:
            from app.services import yandex_offline_conv_service as yandex_conv

            # Purchase event fires centrally from create_transaction; here we
            # only persist the request-body CID synchronously (#558449).
            await yandex_conv.store_cid_only(user.id, cid)
        except Exception as yconv_err:
            logger.debug(
                'yandex_conv purchase hook failed (non-fatal)',
                user_id=user.id,
                error=str(yconv_err),
            )

    return {
        'message': 'Countries updated successfully',
        'added': added_names,
        'removed': removed_names,
        'amount_paid_kopeks': total_cost,
        'connected_squads': subscription.connected_squads,
    }
