"""Admin routes for managing tariffs in cabinet."""

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.rbac import AuditLogCRUD
from app.database.crud.server_squad import get_all_server_squads
from app.database.crud.tariff import (
    assert_tariff_squad_rollout_allowed,
    create_tariff,
    delete_tariff,
    get_all_tariffs,
    get_tariff_by_id,
    get_tariff_subscriptions_count,
    load_period_prices_from_db,
    reorder_tariffs,
    set_tariff_promo_groups,
    update_tariff,
)
from app.database.models import PromoGroup, Subscription, Tariff, Transaction, TransactionType, User
from app.services.subscription_service import SubscriptionService

from ..dependencies import get_cabinet_db, require_permission
from ..schemas.tariffs import (
    PeriodPrice,
    PromoGroupInfo,
    ServerInfo,
    ServerTrafficLimit,
    SquadRolloutPreviewResponse,
    SquadRolloutRequest,
    SquadRolloutResponse,
    TariffCreateRequest,
    TariffDetailResponse,
    TariffListItem,
    TariffListResponse,
    TariffSortOrderRequest,
    TariffStatsResponse,
    TariffToggleResponse,
    TariffTrialResponse,
    TariffUpdateRequest,
)


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/admin/tariffs', tags=['Cabinet Admin Tariffs'])


async def _ensure_tariff_sellable(db: AsyncSession, tariff: Tariff) -> None:
    """Fence every admin activation/trial path before any persistent effect."""

    from app.services.public_location_entitlement_service import EntitlementResolutionError, assert_tariff_sellable

    try:
        await assert_tariff_sellable(db, tariff)
    except EntitlementResolutionError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f'Tariff has no verified entitlement policy: {exc}',
        ) from exc


async def _get_tariff_servers(
    db: AsyncSession, allowed_squads: list[str], server_traffic_limits: dict = None
) -> list[ServerInfo]:
    """Get server info for tariff."""
    servers, _ = await get_all_server_squads(db, available_only=True)
    limits = server_traffic_limits or {}
    result = []
    for server in servers:
        # Получаем индивидуальный лимит трафика для сервера
        server_limit = None
        if server.squad_uuid in limits:
            limit_data = limits[server.squad_uuid]
            if isinstance(limit_data, dict) and 'traffic_limit_gb' in limit_data:
                server_limit = limit_data['traffic_limit_gb']
            elif isinstance(limit_data, int):
                server_limit = limit_data

        result.append(
            ServerInfo(
                id=server.id,
                squad_uuid=server.squad_uuid,
                display_name=server.display_name,
                country_code=server.country_code,
                is_selected=server.squad_uuid in allowed_squads,
                traffic_limit_gb=server_limit,
            )
        )
    return result


async def _get_tariff_promo_groups(db: AsyncSession, tariff: Tariff) -> list[PromoGroupInfo]:
    """Get promo group info for tariff."""
    result = await db.execute(select(PromoGroup).order_by(PromoGroup.name))
    all_groups = result.scalars().all()

    selected_ids = {pg.id for pg in tariff.allowed_promo_groups} if tariff.allowed_promo_groups else set()

    return [
        PromoGroupInfo(
            id=pg.id,
            name=pg.name,
            is_selected=pg.id in selected_ids,
        )
        for pg in all_groups
    ]


def _period_prices_to_list(period_prices: dict) -> list[PeriodPrice]:
    """Convert period_prices dict to list."""
    if not period_prices:
        return []
    return [
        PeriodPrice(days=int(days), price_kopeks=price)
        for days, price in sorted(period_prices.items(), key=lambda x: int(x[0]))
    ]


def _period_prices_to_dict(period_prices: list[PeriodPrice]) -> dict:
    """Convert period_prices list to dict."""
    return {str(pp.days): pp.price_kopeks for pp in period_prices}


@router.get('', response_model=TariffListResponse)
async def list_tariffs(
    include_inactive: bool = True,
    admin: User = Depends(require_permission('tariffs:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get list of all tariffs."""
    tariffs = await get_all_tariffs(db, include_inactive=include_inactive)

    items = []
    for tariff in tariffs:
        subs_count = await get_tariff_subscriptions_count(db, tariff.id)
        items.append(
            TariffListItem(
                id=tariff.id,
                name=tariff.name,
                description=tariff.description,
                is_active=tariff.is_active,
                is_trial_available=tariff.is_trial_available,
                is_daily=tariff.is_daily,
                daily_price_kopeks=tariff.daily_price_kopeks,
                allow_traffic_topup=tariff.allow_traffic_topup,
                show_in_gift=tariff.show_in_gift,
                traffic_limit_gb=tariff.traffic_limit_gb,
                device_limit=tariff.device_limit,
                tier_level=tariff.tier_level,
                display_order=tariff.display_order,
                servers_count=len(tariff.allowed_squads or []),
                subscriptions_count=subs_count,
                created_at=tariff.created_at,
            )
        )

    return TariffListResponse(tariffs=items, total=len(items))


@router.get('/available-servers', response_model=list[ServerInfo])
async def get_available_servers(
    admin: User = Depends(require_permission('tariffs:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get Internal Squads for the native tariff checkbox editor."""
    # A tariff must never advertise an Internal Squad that the backend will
    # reject during issuance or propagation.
    servers, _ = await get_all_server_squads(db, available_only=True, limit=1000)
    return [
        ServerInfo(
            id=server.id,
            squad_uuid=server.squad_uuid,
            display_name=server.display_name,
            country_code=server.country_code,
            is_selected=False,
        )
        for server in servers
    ]


@router.get('/available-external-squads', status_code=status.HTTP_410_GONE)
async def get_available_external_squads(
    admin: User = Depends(require_permission('tariffs:read')),
):
    """External Squad management is intentionally outside the native-node rollout."""
    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail='External Squad management is not available in the native tariff editor.',
    )


@router.put('/order')
async def update_tariff_order(
    request: TariffSortOrderRequest,
    admin: User = Depends(require_permission('tariffs:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Update the display order of tariffs."""
    await reorder_tariffs(db, request.tariff_ids)
    await db.commit()

    logger.info('Admin updated tariff order', admin_id=admin.id, tariff_ids=request.tariff_ids)

    return {'message': 'Tariff order updated successfully'}


@router.get('/{tariff_id}', response_model=TariffDetailResponse)
async def get_tariff(
    tariff_id: int,
    admin: User = Depends(require_permission('tariffs:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get detailed tariff info."""
    tariff = await get_tariff_by_id(db, tariff_id)
    if not tariff:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Tariff not found',
        )

    allowed_squads = tariff.allowed_squads or []
    server_traffic_limits = tariff.server_traffic_limits or {}
    servers = await _get_tariff_servers(db, allowed_squads, server_traffic_limits)
    promo_groups = await _get_tariff_promo_groups(db, tariff)
    subs_count = await get_tariff_subscriptions_count(db, tariff.id)

    # Преобразуем server_traffic_limits в формат для схемы
    server_limits_response = {}
    for uuid, limit_data in server_traffic_limits.items():
        if isinstance(limit_data, dict):
            server_limits_response[uuid] = ServerTrafficLimit(**limit_data)
        elif isinstance(limit_data, int):
            server_limits_response[uuid] = ServerTrafficLimit(traffic_limit_gb=limit_data)

    return TariffDetailResponse(
        id=tariff.id,
        name=tariff.name,
        description=tariff.description,
        is_active=tariff.is_active,
        is_trial_available=tariff.is_trial_available,
        allow_traffic_topup=tariff.allow_traffic_topup,
        traffic_topup_enabled=tariff.traffic_topup_enabled,
        traffic_topup_packages=tariff.traffic_topup_packages or {},
        max_topup_traffic_gb=tariff.max_topup_traffic_gb,
        traffic_limit_gb=tariff.traffic_limit_gb,
        device_limit=tariff.device_limit,
        device_price_kopeks=tariff.device_price_kopeks,
        max_device_limit=tariff.max_device_limit,
        device_purchase_options=tariff.device_purchase_options,
        pricing_revision=tariff.pricing_revision,
        tier_level=tariff.tier_level,
        display_order=tariff.display_order,
        period_prices=_period_prices_to_list(tariff.period_prices),
        allowed_squads=allowed_squads,
        server_traffic_limits=server_limits_response,
        servers=servers,
        promo_groups=promo_groups,
        subscriptions_count=subs_count,
        # Произвольное количество дней
        custom_days_enabled=tariff.custom_days_enabled,
        price_per_day_kopeks=tariff.price_per_day_kopeks,
        min_days=tariff.min_days,
        max_days=tariff.max_days,
        # Произвольный трафик при покупке
        custom_traffic_enabled=tariff.custom_traffic_enabled,
        traffic_price_per_gb_kopeks=tariff.traffic_price_per_gb_kopeks,
        min_traffic_gb=tariff.min_traffic_gb,
        max_traffic_gb=tariff.max_traffic_gb,
        # Дневной тариф
        is_daily=tariff.is_daily,
        daily_price_kopeks=tariff.daily_price_kopeks,
        # Режим сброса трафика
        traffic_reset_mode=tariff.traffic_reset_mode,
        # Внешний сквад
        external_squad_uuid=tariff.external_squad_uuid,
        # Показывать в подарках
        show_in_gift=tariff.show_in_gift,
        created_at=tariff.created_at,
        updated_at=tariff.updated_at,
    )


@router.post('', response_model=TariffDetailResponse)
async def create_new_tariff(
    request: TariffCreateRequest,
    admin: User = Depends(require_permission('tariffs:create')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Create a new tariff."""
    if 'external_squad_uuid' in request.model_fields_set:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail='External Squad changes are outside the native Internal Squad rollout.',
        )
    period_prices_dict = _period_prices_to_dict(request.period_prices)

    # Преобразуем ServerTrafficLimit в dict для хранения
    server_limits_dict = (
        {uuid: limit.model_dump() for uuid, limit in request.server_traffic_limits.items()}
        if request.server_traffic_limits
        else {}
    )

    try:
        tariff = await create_tariff(
            db=db,
            name=request.name,
            description=request.description,
            is_active=request.is_active,
            allow_traffic_topup=request.allow_traffic_topup,
            traffic_topup_enabled=request.traffic_topup_enabled,
            traffic_topup_packages=request.traffic_topup_packages,
            max_topup_traffic_gb=request.max_topup_traffic_gb,
            traffic_limit_gb=request.traffic_limit_gb,
            device_limit=request.device_limit,
            device_price_kopeks=request.device_price_kopeks,
            max_device_limit=request.max_device_limit,
            device_purchase_options=request.device_purchase_options,
            tier_level=request.tier_level,
            period_prices=period_prices_dict,
            allowed_squads=request.allowed_squads,
            server_traffic_limits=server_limits_dict,
            promo_group_ids=request.promo_group_ids or None,
            # Произвольное количество дней
            custom_days_enabled=request.custom_days_enabled,
            price_per_day_kopeks=request.price_per_day_kopeks,
            min_days=request.min_days,
            max_days=request.max_days,
            # Произвольный трафик при покупке
            custom_traffic_enabled=request.custom_traffic_enabled,
            traffic_price_per_gb_kopeks=request.traffic_price_per_gb_kopeks,
            min_traffic_gb=request.min_traffic_gb,
            max_traffic_gb=request.max_traffic_gb,
            # Дневной тариф
            is_daily=request.is_daily,
            daily_price_kopeks=request.daily_price_kopeks,
            # Режим сброса трафика
            traffic_reset_mode=request.traffic_reset_mode,
            # External Squad mutation is deliberately not part of this rollout.
            external_squad_uuid=None,
            # Показывать в подарках
            show_in_gift=request.show_in_gift,
        )
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)) from error

    logger.info('Admin created tariff', admin_id=admin.id, tariff_id=tariff.id, tariff_name=tariff.name)

    # Перезагружаем периоды из БД для синхронизации с ботом
    await load_period_prices_from_db(db)

    # Return full detail
    return await get_tariff(tariff.id, admin, db)


@router.put('/{tariff_id}', response_model=TariffDetailResponse)
async def update_existing_tariff(
    tariff_id: int,
    request: TariffUpdateRequest,
    admin: User = Depends(require_permission('tariffs:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Update an existing tariff."""
    if 'external_squad_uuid' in request.model_fields_set:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail='External Squad changes are outside the native Internal Squad rollout.',
        )
    tariff = await get_tariff_by_id(db, tariff_id)
    if not tariff:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Tariff not found',
        )

    # Capture old values for change detection
    old_squads = list(tariff.allowed_squads) if tariff.allowed_squads else []
    # Build updates dict
    updates = {}
    if request.name is not None:
        updates['name'] = request.name
    if request.description is not None:
        updates['description'] = request.description
    if request.is_active is not None:
        updates['is_active'] = request.is_active
    if request.allow_traffic_topup is not None:
        updates['allow_traffic_topup'] = request.allow_traffic_topup
    if request.traffic_topup_enabled is not None:
        updates['traffic_topup_enabled'] = request.traffic_topup_enabled
    if request.traffic_topup_packages is not None:
        updates['traffic_topup_packages'] = request.traffic_topup_packages
    if request.max_topup_traffic_gb is not None:
        updates['max_topup_traffic_gb'] = request.max_topup_traffic_gb
    if request.traffic_limit_gb is not None:
        updates['traffic_limit_gb'] = request.traffic_limit_gb
    if request.device_limit is not None:
        updates['device_limit'] = request.device_limit
    if request.device_price_kopeks is not None:
        updates['device_price_kopeks'] = request.device_price_kopeks
    if request.max_device_limit is not None:
        updates['max_device_limit'] = request.max_device_limit
    if 'device_purchase_options' in request.model_fields_set:
        updates['device_purchase_options'] = request.device_purchase_options
    if request.tier_level is not None:
        updates['tier_level'] = request.tier_level
    if request.display_order is not None:
        updates['display_order'] = request.display_order
    if request.period_prices is not None:
        updates['period_prices'] = _period_prices_to_dict(request.period_prices)
    if request.allowed_squads is not None:
        updates['allowed_squads'] = request.allowed_squads
    if request.server_traffic_limits is not None:
        # Преобразуем ServerTrafficLimit в dict для хранения
        updates['server_traffic_limits'] = {
            uuid: limit.model_dump() for uuid, limit in request.server_traffic_limits.items()
        }
    # Произвольное количество дней
    if request.custom_days_enabled is not None:
        updates['custom_days_enabled'] = request.custom_days_enabled
    if request.price_per_day_kopeks is not None:
        updates['price_per_day_kopeks'] = request.price_per_day_kopeks
    if request.min_days is not None:
        updates['min_days'] = request.min_days
    if request.max_days is not None:
        updates['max_days'] = request.max_days
    # Произвольный трафик при покупке
    if request.custom_traffic_enabled is not None:
        updates['custom_traffic_enabled'] = request.custom_traffic_enabled
    if request.traffic_price_per_gb_kopeks is not None:
        updates['traffic_price_per_gb_kopeks'] = request.traffic_price_per_gb_kopeks
    if request.min_traffic_gb is not None:
        updates['min_traffic_gb'] = request.min_traffic_gb
    if request.max_traffic_gb is not None:
        updates['max_traffic_gb'] = request.max_traffic_gb
    # Дневной тариф
    if request.is_daily is not None:
        updates['is_daily'] = request.is_daily
    if request.daily_price_kopeks is not None:
        updates['daily_price_kopeks'] = request.daily_price_kopeks
    # Режим сброса трафика (None допускается как значение для сброса к глобальной настройке)
    if 'traffic_reset_mode' in request.model_fields_set:
        updates['traffic_reset_mode'] = request.traffic_reset_mode
    # Показывать в подарках
    if request.show_in_gift is not None:
        updates['show_in_gift'] = request.show_in_gift

    if updates:
        try:
            await update_tariff(db, tariff, **updates)
        except ValueError as error:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)) from error

    # Update promo groups separately
    if request.promo_group_ids is not None:
        await set_tariff_promo_groups(db, tariff, request.promo_group_ids)

    logger.info('Admin updated tariff', admin_id=admin.id, tariff_id=tariff_id)

    # Перезагружаем периоды из БД для синхронизации с ботом
    await load_period_prices_from_db(db)

    # Native membership is propagated only by the explicit, auditable endpoint
    # below.  A fire-and-forget task could leave a tariff changed locally while
    # silently failing to change a paying user's Panel access.
    new_squads = tariff.allowed_squads or []
    squads_changed = request.allowed_squads is not None and sorted(old_squads) != sorted(new_squads)
    if squads_changed:
        logger.info(
            'Native tariff squads saved; explicit sync is required before existing subscriptions change',
            admin_id=admin.id,
            tariff_id=tariff_id,
        )

    return await get_tariff(tariff_id, admin, db)


@router.delete('/{tariff_id}')
async def delete_existing_tariff(
    tariff_id: int,
    admin: User = Depends(require_permission('tariffs:delete')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Delete a tariff."""
    tariff = await get_tariff_by_id(db, tariff_id)
    if not tariff:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Tariff not found',
        )

    subs_count = await get_tariff_subscriptions_count(db, tariff_id)
    await delete_tariff(db, tariff)
    logger.info(
        'Admin deleted tariff (affected subscriptions: )',
        admin_id=admin.id,
        tariff_id=tariff_id,
        tariff_name=tariff.name,
        subs_count=subs_count,
    )

    # Перезагружаем периоды из БД для синхронизации с ботом
    await load_period_prices_from_db(db)

    return {'message': 'Tariff deleted successfully', 'affected_subscriptions': subs_count}


@router.post('/{tariff_id}/toggle', response_model=TariffToggleResponse)
async def toggle_tariff(
    tariff_id: int,
    admin: User = Depends(require_permission('tariffs:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Toggle tariff active status."""
    tariff = await get_tariff_by_id(db, tariff_id)
    if not tariff:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Tariff not found',
        )

    new_status = not tariff.is_active
    if new_status:
        await _ensure_tariff_sellable(db, tariff)
    await update_tariff(db, tariff, is_active=new_status)

    status_text = 'activated' if new_status else 'deactivated'
    logger.info('Admin tariff', admin_id=admin.id, status_text=status_text, tariff_id=tariff_id)

    # Перезагружаем периоды из БД для синхронизации с ботом
    await load_period_prices_from_db(db)

    return TariffToggleResponse(
        id=tariff_id,
        is_active=new_status,
        message=f'Tariff {status_text}',
    )


@router.post('/{tariff_id}/trial', response_model=TariffTrialResponse)
async def toggle_trial_tariff(
    tariff_id: int,
    admin: User = Depends(require_permission('tariffs:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Toggle tariff trial availability.

    When enabling trial on a tariff, removes trial flag from all other tariffs
    (only one tariff can be the trial tariff at a time).
    """
    tariff = await get_tariff_by_id(db, tariff_id)
    if not tariff:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Tariff not found',
        )

    new_status = not tariff.is_trial_available

    if new_status:
        await _ensure_tariff_sellable(db, tariff)

    if new_status:
        # При включении триала - снимаем флаг со ВСЕХ тарифов, затем ставим на текущий
        # Это гарантирует, что триальным будет только один тариф
        await db.execute(Tariff.__table__.update().values(is_trial_available=False))
        await db.commit()
        # Обновляем объект тарифа после массового обновления
        await db.refresh(tariff)

    await update_tariff(db, tariff, is_trial_available=new_status)

    status_text = 'set as trial' if new_status else 'removed from trial'
    logger.info('Admin tariff', admin_id=admin.id, status_text=status_text, tariff_id=tariff_id)

    return TariffTrialResponse(
        id=tariff_id,
        is_trial_available=new_status,
        message=f'Tariff {status_text}',
    )


@router.get('/{tariff_id}/stats', response_model=TariffStatsResponse)
async def get_tariff_stats(
    tariff_id: int,
    admin: User = Depends(require_permission('tariffs:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Get tariff statistics."""
    tariff = await get_tariff_by_id(db, tariff_id)
    if not tariff:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Tariff not found',
        )

    # Count subscriptions
    total_result = await db.execute(select(func.count(Subscription.id)).where(Subscription.tariff_id == tariff_id))
    total_count = total_result.scalar() or 0

    # Count active subscriptions
    active_result = await db.execute(
        select(func.count(Subscription.id)).where(
            Subscription.tariff_id == tariff_id,
            Subscription.status == 'active',
        )
    )
    active_count = active_result.scalar() or 0

    # Count trial subscriptions
    trial_result = await db.execute(
        select(func.count(Subscription.id)).where(
            Subscription.tariff_id == tariff_id,
            Subscription.is_trial == True,
        )
    )
    trial_count = trial_result.scalar() or 0

    # Calculate revenue from subscription payments for users on this tariff
    revenue_result = await db.execute(
        select(func.coalesce(func.sum(func.abs(Transaction.amount_kopeks)), 0))
        .join(Subscription, Transaction.user_id == Subscription.user_id)
        .where(
            Subscription.tariff_id == tariff_id,
            Transaction.type == TransactionType.SUBSCRIPTION_PAYMENT.value,
            Transaction.is_completed == True,
        )
    )
    revenue_kopeks = revenue_result.scalar() or 0

    return TariffStatsResponse(
        id=tariff_id,
        name=tariff.name,
        subscriptions_count=total_count,
        active_subscriptions=active_count,
        trial_subscriptions=trial_count,
        revenue_kopeks=revenue_kopeks,
        revenue_rubles=revenue_kopeks / 100,
    )


async def _assert_rollout_allowed_in_russian(db: AsyncSession, tariff: Tariff) -> None:
    """Тот же забор, но с причиной, понятной владельцу.

    Канонический текст — английский и с внутренним жаргоном («Internal Squads»,
    «Platega invoice», «reconciliation»). Владелец будет упираться в этот отказ
    рутинно: забор срабатывает от ЛЮБОЙ живой корзины клиента, даже шестиминутной.
    """

    try:
        await assert_tariff_squad_rollout_allowed(db, tariff)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                'Сейчас нельзя: у тарифа есть незакрытый заказ клиента. '
                'Это нормально — подождите, пока он оплатится или отменится, и нажмите снова.'
            ),
        ) from exc


async def _load_rollout_tariff(db: AsyncSession, tariff_id: int) -> Tariff:
    """Тариф, пригодный для раскатки, либо понятный отказ."""

    tariff = await get_tariff_by_id(db, tariff_id)
    if not tariff:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Тариф не найден')
    # Раскатка разносит ИМЕННО allowed_squads. У наследованного режима этот набор
    # не является действующим правом, и раскатка по нему выдала бы подпискам не то,
    # за что человек платил.  Сначала сохранение серверов (оно же переводит тариф
    # в native_squads через update_tariff и его забор), только потом раскатка.
    if getattr(tariff, 'entitlement_mode', None) != 'native_squads':
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                'Тариф ещё не переведён на собственный выбор серверов. Сначала сохраните серверы '
                'на экране тарифа, потом раскатывайте их на выданные подписки.'
            ),
        )
    if not tariff.allowed_squads:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail='У тарифа не выбрано ни одного сервера — раскатывать нечего.',
        )
    return tariff


@router.post('/{tariff_id}/squad-rollout/preview', response_model=SquadRolloutPreviewResponse)
async def preview_squad_rollout(
    tariff_id: int,
    admin: User = Depends(require_permission('tariffs:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Сухой прогон раскатки: сколько подписок затронет и какие пропустит.

    Ничего не пишет и не обращается к панели — это безопасный предпросмотр.
    """
    tariff = await _load_rollout_tariff(db, tariff_id)
    plan = await SubscriptionService().plan_tariff_squad_rollout(db, tariff_id, list(tariff.allowed_squads or []))
    logger.info('Сухой прогон раскатки серверов', admin_id=admin.id, tariff_id=tariff_id, plan=plan)
    return SquadRolloutPreviewResponse(**plan)


@router.post('/{tariff_id}/squad-rollout', response_model=SquadRolloutResponse)
async def run_squad_rollout(
    tariff_id: int,
    request: SquadRolloutRequest,
    admin: User = Depends(require_permission('tariffs:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Раскатывает серверы тарифа на выданные подписки порциями.

    Серверы САМОГО тарифа здесь не меняются: их правит только ``update_tariff``
    со своим забором.  Раскатка разносит уже сохранённый набор и берёт ТОТ ЖЕ
    забор живых заказов — она трогает захваченные права ещё жёстче.
    """
    tariff = await _load_rollout_tariff(db, tariff_id)
    await _assert_rollout_allowed_in_russian(db, tariff)

    planned_squads = list(tariff.allowed_squads or [])

    async def _recheck_fence() -> None:
        await assert_tariff_squad_rollout_allowed(db, tariff)
        # Тариф могли отредактировать между порциями — из кабинета или из
        # чат-админки бота, это два независимых пути. Тогда оставшиеся порции
        # разнесли бы уже устаревший набор, молча и без единой ошибки.
        fresh = await db.get(Tariff, tariff_id, populate_existing=True)
        if fresh is None or sorted(fresh.allowed_squads or []) != sorted(planned_squads):
            raise ValueError(
                'Серверы тарифа изменились, пока шла раскатка. Раскатка остановлена — '
                'откройте тариф, проверьте список серверов и запустите раскатку заново.'
            )

    try:
        result = await SubscriptionService().propagate_tariff_squads(
            db,
            tariff_id,
            planned_squads,
            subscription_ids=request.subscription_ids,
            limit=request.limit,
            batch_size=request.batch_size,
            recheck_fence=_recheck_fence,
        )
    except ValueError as exc:
        # Сбой записи снимка и обрыв по изменившимся серверам приходят сюда. Без
        # этого владелец получил бы голый 500 текстом, из которого перехватчик
        # кабинета не достанет причину вовсе — она есть только в JSON с detail.
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    await AuditLogCRUD.create(
        db,
        user_id=admin.id,
        action='tariff_squads_rolled_out',
        resource_type='tariff',
        resource_id=str(tariff_id),
        details={
            'rollout_id': result.rollout_id,
            'total': result.total,
            'synced': result.synced,
            'failed_ids': result.failed_ids,
            'skipped_traffic_risk_ids': result.skipped_traffic_risk_ids,
            'url_mismatch_ids': result.url_mismatch_ids,
            'stopped_early': result.stopped_early,
            # Именно planned_squads: перепроверка забора перечитывает тариф с
            # populate_existing и переписывает tariff.allowed_squads НА МЕСТЕ, поэтому
            # после прерванной раскатки поле показало бы уже новый набор, а не тот,
            # что реально ушёл подпискам.
            'squads_applied': planned_squads,
            'remaining': result.remaining,
        },
        status=_rollout_audit_status(result),
    )
    await db.commit()
    return _rollout_response(tariff_id, result)


@router.post('/{tariff_id}/squad-rollout/restore', response_model=SquadRolloutResponse)
async def restore_squad_rollout(
    tariff_id: int,
    admin: User = Depends(require_permission('tariffs:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Возвращает подписки к снимку последней раскатки этого тарифа."""

    tariff = await _load_rollout_tariff(db, tariff_id)
    await _assert_rollout_allowed_in_russian(db, tariff)

    async def _recheck_fence() -> None:
        await assert_tariff_squad_rollout_allowed(db, tariff)

    try:
        result = await SubscriptionService().restore_tariff_squads(db, tariff_id, recheck_fence=_recheck_fence)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    if not result.rollout_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Для этого тарифа нет ни одного снимка раскатки — возвращать не по чему.',
        )
    await AuditLogCRUD.create(
        db,
        user_id=admin.id,
        action='tariff_squads_rollout_restored',
        resource_type='tariff',
        resource_id=str(tariff_id),
        details={
            'rollout_id': result.rollout_id,
            'total': result.total,
            'synced': result.synced,
            'failed_ids': result.failed_ids,
            'unrestorable_ids': result.unrestorable_ids,
            'stopped_early': result.stopped_early,
        },
        status=_rollout_audit_status(result),
    )
    await db.commit()
    return _rollout_response(tariff_id, result)


def _rollout_audit_status(result) -> str:
    """`success` только если ничего не осталось за бортом.

    Прежняя формула смотрела лишь на упавшие, поэтому возврат, который не вернул
    НИКОГО (все пред-образы пустые), записывался в аудит как полный успех.
    """

    incomplete = (
        result.failed_ids
        or result.stopped_early
        or result.unrestorable_ids
        or result.skipped_traffic_risk_ids
        or result.shared_account_ids
        or result.moved_on_ids
        or result.remaining
    )
    return 'partial' if incomplete else 'success'


def _rollout_response(tariff_id: int, result) -> SquadRolloutResponse:
    # Никаких отсылок к «отчёту ниже» — его нет.  Всё, что владельцу нужно знать,
    # должно уместиться в саму строку: она приходит всплывающим уведомлением.
    # В Telegram успех и ошибка выглядят ОДИНАКОВО — одно нейтральное окно с «OK».
    # Значит тревога обязана стоять первым словом, а не прятаться за «Готово».
    parts: list[str] = []
    if result.url_mismatch_ids:
        parts.append(f'ОСТАНОВЛЕНО: у {len(result.url_mismatch_ids)} изменилась ссылка подключения.')
    elif result.stopped_early:
        parts.append('ОСТАНОВЛЕНО на полпути, снимок сохранён.')
    parts.append(f'Готово: {result.synced} из {result.total}.')
    if result.remaining:
        parts.append(f'Осталось {result.remaining} — нажмите ещё раз.')
    if result.failed_ids:
        parts.append(f'Не удалось: {len(result.failed_ids)}.')
    if result.skipped_traffic_risk_ids:
        parts.append(f'Пропущено (трафик исчерпан): {len(result.skipped_traffic_risk_ids)}.')
    if result.shared_account_ids:
        parts.append(f'Пропущено (вторая подписка у клиента): {len(result.shared_account_ids)}.')
    if result.moved_on_ids:
        parts.append(f'Не тронуто (клиент сам сменил серверы): {len(result.moved_on_ids)}.')
    if result.unrestorable_ids:
        parts.append(f'Нельзя вернуть (пустой снимок): {len(result.unrestorable_ids)}.')
    message = ' '.join(parts)
    return SquadRolloutResponse(
        tariff_id=tariff_id,
        rollout_id=result.rollout_id,
        total=result.total,
        synced=result.synced,
        batches_done=result.batches_done,
        failed_ids=result.failed_ids,
        skipped_traffic_risk_ids=result.skipped_traffic_risk_ids,
        url_mismatch_ids=result.url_mismatch_ids,
        stopped_early=result.stopped_early,
        unrestorable_ids=result.unrestorable_ids,
        shared_account_ids=result.shared_account_ids,
        moved_on_ids=result.moved_on_ids,
        remaining=result.remaining,
        message=message,
    )
