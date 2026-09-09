"""Device management endpoints.

GET /subscription/devices
POST /subscription/devices (legacy)
DELETE /subscription/devices/{hwid}
DELETE /subscription/devices
POST /subscription/devices/purchase
GET /subscription/devices/reduction-info
POST /subscription/devices/reduce
GET /subscription/devices/price
POST /subscription/devices/save-cart
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query as QueryParam, status
from pydantic import BaseModel
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.cabinet.utils.device_ownership import verify_hwid_belongs_to_user
from app.config import settings
from app.database.crud.user_device_alias import (
    delete_alias,
    get_aliases_for_user,
    normalize_alias,
    set_alias,
)
from app.database.models import Subscription, User
from app.services.subscription_service import SubscriptionService

from ...dependencies import get_cabinet_db, get_current_cabinet_user
from ...schemas.subscription import DevicePurchaseRequest
from .helpers import resolve_subscription


logger = structlog.get_logger(__name__)

router = APIRouter()


def _resolve_panel_uuid(subscription: Subscription | None, user: User) -> str | None:
    """Resolve RemnaWave panel UUID: per-subscription in multi-tariff, user-level otherwise.

    Multi-tariff: each subscription is its OWN panel user — return the sub's UUID
    and do NOT fall back to ``user.remnawave_uuid`` when it's null. The fallback
    would read/operate on another tariff's panel user, making HWID devices/limit
    look shared across tariffs (баг с общим лимитом «по наименьшему тарифу»).
    """
    if settings.is_multi_tariff_enabled() and subscription is not None:
        return subscription.remnawave_uuid
    return user.remnawave_uuid


@router.post('/devices')
async def purchase_devices_legacy(
    request: DevicePurchaseRequest,
    subscription_id: int | None = QueryParam(None, description='Subscription ID for multi-tariff'),
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Old clients must reload the signed-quote device purchase flow."""
    raise HTTPException(
        status_code=409,
        detail={
            'code': 'quote_required',
            'message': 'Обновите кабинет и подтвердите актуальную цену докупки устройств.',
            'continuation_path': '/subscription/device-topup/new',
        },
    )


@router.post('/devices/purchase')
async def purchase_devices(
    request: DevicePurchaseRequest,
    subscription_id: int | None = QueryParam(None, description='Subscription ID for multi-tariff'),
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Old clients must reload the signed-quote device purchase flow."""
    raise HTTPException(
        status_code=409,
        detail={
            'code': 'quote_required',
            'message': 'Обновите кабинет и подтвердите актуальную цену докупки устройств.',
            'continuation_path': '/subscription/device-topup/new',
        },
    )


@router.post('/devices/save-cart')
async def save_devices_cart(
    request: DevicePurchaseRequest,
    subscription_id: int | None = QueryParam(None, description='Subscription ID for multi-tariff'),
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
) -> dict[str, bool]:
    """Old clients must reload the signed-quote device purchase flow."""
    raise HTTPException(
        status_code=409,
        detail={
            'code': 'quote_required',
            'message': 'Обновите кабинет и подтвердите актуальную цену докупки устройств.',
            'continuation_path': '/subscription/device-topup/new',
        },
    )


@router.get('/devices/price')
async def get_device_price(
    devices: int = 1,
    subscription_id: int | None = QueryParam(None, description='Subscription ID for multi-tariff'),
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Compatibility projection of the authoritative device add-on calculator."""
    from app.services.device_addon_service import DeviceAddonError, calculate_device_addon

    try:
        calculation = await calculate_device_addon(
            db,
            user=user,
            subscription_id=subscription_id,
            devices_to_add=devices,
        )
    except DeviceAddonError as error:
        return {'available': False, 'reason': str(error), 'code': error.code}
    total = calculation.price_kopeks
    current = calculation.original_device_limit
    maximum = calculation.max_device_limit
    result = {
        'available': True,
        'devices': devices,
        'price_per_device_kopeks': total // devices,
        'price_per_device_label': settings.format_price(total // devices),
        'total_price_kopeks': total,
        'total_price_label': settings.format_price(total),
        'current_device_limit': current,
        'max_device_limit': maximum,
        'can_add': maximum - current if maximum is not None else None,
        'days_left': calculation.days_left,
        'base_device_price_kopeks': calculation.monthly_price_kopeks,
    }
    if calculation.discount_percent:
        result.update(
            {
                'discount_percent': calculation.discount_percent,
                'discount_kopeks': max(0, calculation.base_price_kopeks - total),
                'base_total_price_kopeks': calculation.base_price_kopeks,
                'original_price_per_device_kopeks': calculation.base_price_kopeks // devices,
            }
        )
    return result


# ============ Device Management (list/delete) ============


@router.get('/devices')
async def get_devices(
    subscription_id: int | None = QueryParam(None, description='Subscription ID for multi-tariff'),
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
) -> dict[str, Any]:
    """Get list of connected devices."""
    from app.services.remnawave_service import RemnaWaveService

    subscription = await resolve_subscription(db, user, subscription_id)

    if not subscription:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='No subscription found',
        )

    _puuid = _resolve_panel_uuid(subscription, user)
    if not _puuid:
        # No panel uuid = account not provisioned yet (a brand-new user), NOT a
        # panel failure → panel_ok stays True so the screen still lights up
        # "Подключить". Distinct from the `except` branch below (real failure).
        return {
            'devices': [],
            'total': 0,
            'device_limit': subscription.device_limit or 0,
            'panel_ok': True,
        }

    try:
        service = RemnaWaveService()
        async with service.get_api_client() as api:
            response = await api.get_user_devices_all(_puuid)

            devices_list = response.get('devices', [])
            # Подтягиваем все локальные alias'ы юзера одним запросом — дешевле
            # чем N+1 при сборке списка устройств. Aliases декоративны: при
            # сбое чтения возвращаем список без них, а не 500.
            try:
                aliases = await get_aliases_for_user(db, user.id)
            except Exception as alias_error:
                logger.warning(
                    'Failed to load device aliases, falling back to defaults',
                    user_id=user.id,
                    error=str(alias_error)[:200],
                )
                aliases = {}

            formatted_devices = []
            for device in devices_list:
                hwid = device.get('hwid') or device.get('deviceId') or device.get('id')
                platform = device.get('platform') or device.get('platformType') or 'Unknown'
                model = device.get('deviceModel') or device.get('model') or device.get('name') or 'Unknown'
                created_at = device.get('updatedAt') or device.get('lastSeen') or device.get('createdAt')

                formatted_devices.append(
                    {
                        'hwid': hwid,
                        'platform': platform,
                        'device_model': model,
                        'created_at': created_at,
                        # Локальное имя, заданное юзером. None — алиаса нет,
                        # фронт фоллбэчит на platform/device_model.
                        'local_name': aliases.get(hwid) or None,
                    }
                )

            return {
                'devices': formatted_devices,
                'total': response.get('total', len(formatted_devices)),
                'device_limit': subscription.device_limit or 0,
                'panel_ok': True,
            }

    except Exception as e:
        # Панель медленная/недоступна — деградируем мягко (пустой список) и логируем
        # WARNING, как соседние читатели устройств (device_ownership, miniapp), чтобы
        # транзиентный таймаут панели не спамил админ-чат ошибками.
        logger.warning('Failed to load devices from RemnaWave (panel slow/unavailable)', error=str(e)[:200])
        # Real panel failure: panel_ok=False so the screen shows a "panel error"
        # state instead of a false "Подключить" on the degraded empty list.
        return {
            'devices': [],
            'total': 0,
            'device_limit': subscription.device_limit or 0,
            'panel_ok': False,
        }


class DeviceRenameRequest(BaseModel):
    """Payload for `PATCH /subscription/devices/{hwid}/name`.

    `name` accepts either a non-empty string (set/update) or null/empty
    string (clear the alias and fall back to the default platform/model
    label). Length is capped at ALIAS_MAX_LENGTH on the backend.
    """

    name: str | None = None


# Hwid ownership validation lives in app.cabinet.utils.device_ownership —
# shared between the user-facing rename endpoint below and the admin
# override in app/cabinet/routes/admin_users.py. Keeps both call sites
# from drifting on multi-tariff semantics again.


@router.patch('/devices/{hwid}/name')
async def rename_device(
    hwid: str,
    request: DeviceRenameRequest,
    subscription_id: int | None = QueryParam(None, description='Subscription ID for multi-tariff'),
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
) -> dict[str, Any]:
    """Set/clear a local alias for the user's HWID device.

    Scope is per-(user, hwid), so the alias is visible across ALL of the
    user's subscriptions in multi-tariff mode — same physical device, same
    nickname.

    Empty/null `name` clears the alias and returns `{local_name: null}`.
    """
    # Subscription resolution здесь только для access-проверки: убеждаемся,
    # что юзер действительно владеет устройством через какую-то из своих
    # подписок. Сам alias всё равно глобальный per (user, hwid).
    subscription = await resolve_subscription(db, user, subscription_id)
    if not subscription:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='No subscription found')

    hwid = (hwid or '').strip()
    if not hwid:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='hwid is required')

    # Guard against orphan rows: only accept rename requests for devices
    # the user actually owns in RemnaWave panel right now. Multi-tariff
    # aware (unions devices across all panel UUIDs the user holds).
    if not await verify_hwid_belongs_to_user(user, hwid):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Device not found on your account',
        )

    normalized = normalize_alias(request.name)
    if normalized:
        saved = await set_alias(db, user.id, hwid, normalized)
        return {'hwid': hwid, 'local_name': saved}

    await delete_alias(db, user.id, hwid)
    return {'hwid': hwid, 'local_name': None}


@router.delete('/devices/{hwid}')
async def delete_device(
    hwid: str,
    subscription_id: int | None = QueryParam(None, description='Subscription ID for multi-tariff'),
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
) -> dict[str, Any]:
    """Delete a specific device by HWID."""
    from app.services.remnawave_service import RemnaWaveService

    subscription = await resolve_subscription(db, user, subscription_id)

    if not subscription:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='No subscription found',
        )

    _puuid = _resolve_panel_uuid(subscription, user)
    if not _puuid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='User UUID not found',
        )

    try:
        service = RemnaWaveService()
        async with service.get_api_client() as api:
            delete_data = {'userUuid': _puuid, 'hwid': hwid}
            await api._make_request('POST', '/api/hwid/devices/delete', data=delete_data)

            return {
                'success': True,
                'message': 'Device deleted successfully',
                'deleted_hwid': hwid,
            }

    except Exception as e:
        logger.error('Error deleting device', error=e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to delete device',
        )


@router.delete('/devices')
async def delete_all_devices(
    subscription_id: int | None = QueryParam(None, description='Subscription ID for multi-tariff'),
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
) -> dict[str, Any]:
    """Delete all connected devices."""
    from app.services.remnawave_service import RemnaWaveService

    subscription = await resolve_subscription(db, user, subscription_id)

    if not subscription:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='No subscription found',
        )

    _puuid = _resolve_panel_uuid(subscription, user)
    if not _puuid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='User UUID not found',
        )

    try:
        service = RemnaWaveService()
        async with service.get_api_client() as api:
            # Get all devices first
            response = await api.get_user_devices_all(_puuid)

            if not response:
                return {
                    'success': True,
                    'message': 'No devices to delete',
                    'deleted_count': 0,
                }

            devices_list = response.get('devices', [])
            if not devices_list:
                return {
                    'success': True,
                    'message': 'No devices to delete',
                    'deleted_count': 0,
                }

            deleted_count = 0
            for device in devices_list:
                device_hwid = device.get('hwid')
                if device_hwid:
                    try:
                        delete_data = {'userUuid': _puuid, 'hwid': device_hwid}
                        await api._make_request('POST', '/api/hwid/devices/delete', data=delete_data)
                        deleted_count += 1
                    except Exception as device_error:
                        logger.error('Error deleting device', device_hwid=device_hwid, device_error=device_error)

            return {
                'success': True,
                'message': f'Deleted {deleted_count} devices',
                'deleted_count': deleted_count,
            }

    except Exception as e:
        logger.error('Error deleting all devices', error=e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to delete devices',
        )


# ============ Device Reduction ============


@router.get('/devices/reduction-info')
async def get_device_reduction_info(
    subscription_id: int | None = QueryParam(None, description='Subscription ID for multi-tariff'),
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
) -> dict[str, Any]:
    """Get info about device limit reduction availability."""
    from app.services.remnawave_service import RemnaWaveService

    subscription = await resolve_subscription(db, user, subscription_id)

    if not subscription:
        return {
            'available': False,
            'reason': 'No subscription found',
            'current_device_limit': 0,
            'min_device_limit': 1,
            'can_reduce': 0,
            'connected_devices_count': 0,
        }

    # Check if it's a trial subscription
    if subscription.is_trial:
        return {
            'available': False,
            'reason': 'Device reduction is not available for trial subscriptions',
            'current_device_limit': subscription.device_limit or 1,
            'min_device_limit': 1,
            'can_reduce': 0,
            'connected_devices_count': 0,
        }

    # Minimum device limit for decrease is always 1 (tariff's device_limit is the
    # number of devices included at purchase, not the floor for decrease)
    min_device_limit = 1

    current_device_limit = subscription.device_limit or 1

    # Can't reduce below minimum
    if current_device_limit <= min_device_limit:
        return {
            'available': False,
            'reason': 'Already at minimum device limit',
            'current_device_limit': current_device_limit,
            'min_device_limit': min_device_limit,
            'can_reduce': 0,
            'connected_devices_count': 0,
        }

    # Get connected devices count
    connected_devices_count = 0
    _puuid = _resolve_panel_uuid(subscription, user)
    if _puuid:
        try:
            service = RemnaWaveService()
            async with service.get_api_client() as api:
                response = await api.get_user_devices_all(_puuid)
                if response:
                    connected_devices_count = response.get('total', 0)
        except Exception as e:
            logger.warning('Failed to get connected devices count (panel slow/unavailable)', error=str(e)[:200])

    can_reduce = current_device_limit - min_device_limit

    return {
        'available': True,
        'current_device_limit': current_device_limit,
        'min_device_limit': min_device_limit,
        'can_reduce': can_reduce,
        'connected_devices_count': connected_devices_count,
    }


@router.post('/devices/reduce')
async def reduce_devices(
    request: dict[str, int],
    subscription_id: int | None = QueryParam(None, description='Subscription ID for multi-tariff'),
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
) -> dict[str, Any]:
    """Reduce device limit (no refund)."""
    from app.services.remnawave_service import RemnaWaveService

    new_device_limit = request.get('new_device_limit')
    if not new_device_limit or new_device_limit < 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Invalid new_device_limit',
        )

    # Resolve subscription (ownership validated), then lock the row for concurrent safety
    resolved = await resolve_subscription(db, user, subscription_id)
    if not resolved:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='No subscription found')

    result = await db.execute(
        select(Subscription)
        .where(and_(Subscription.id == resolved.id, Subscription.user_id == user.id))
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    subscription = result.scalar_one_or_none()

    if not subscription:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='No subscription found',
        )

    from app.services.public_access_point_service import AccessPointPolicyError, assert_no_manual_access_point_grant

    try:
        await assert_no_manual_access_point_grant(db, subscription, action='device reduction')
    except AccessPointPolicyError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={'code': 'access_point_addon_unsupported', 'message': str(error)},
        ) from error

    if subscription.is_trial:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Device reduction is not available for trial subscriptions',
        )

    # Minimum device limit for decrease is always 1 (tariff's device_limit is the
    # number of devices included at purchase, not the floor for decrease)
    min_device_limit = 1

    current_device_limit = subscription.device_limit or 1

    # Validate new limit
    if new_device_limit >= current_device_limit:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='New device limit must be less than current limit',
        )

    if new_device_limit < min_device_limit:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f'Cannot reduce below minimum device limit ({min_device_limit}) for your tariff',
        )

    # Get connected devices and remove excess (last connected ones)
    connected_devices_count = 0
    devices_removed_count = 0
    _puuid = _resolve_panel_uuid(subscription, user)
    if _puuid:
        try:
            service = RemnaWaveService()
            async with service.get_api_client() as api:
                response = await api.get_user_devices_all(_puuid)
                if response:
                    devices_list = response.get('devices', [])
                    connected_devices_count = len(devices_list)

                    # If connected devices exceed new limit, remove excess (last connected)
                    if connected_devices_count > new_device_limit:
                        devices_to_remove = connected_devices_count - new_device_limit
                        logger.info(
                            'Removing excess devices for user had new limit',
                            devices_to_remove=devices_to_remove,
                            user_id=user.id,
                            connected_devices_count=connected_devices_count,
                            new_device_limit=new_device_limit,
                        )

                        # Sort by date (oldest first) and remove the last ones
                        sorted_devices = sorted(
                            devices_list,
                            key=lambda d: d.get('updatedAt') or d.get('createdAt') or '\xff',
                        )
                        devices_to_delete = sorted_devices[-devices_to_remove:]

                        for device in devices_to_delete:
                            device_hwid = device.get('hwid')
                            if device_hwid:
                                try:
                                    delete_data = {'userUuid': _puuid, 'hwid': device_hwid}
                                    await api._make_request('POST', '/api/hwid/devices/delete', data=delete_data)
                                    devices_removed_count += 1
                                    logger.info('Removed device for user', device_hwid=device_hwid, user_id=user.id)
                                except Exception as del_error:
                                    logger.error('Error removing device', device_hwid=device_hwid, del_error=del_error)
        except Exception as e:
            logger.error('Error checking/removing devices', error=e)

    old_device_limit = current_device_limit
    user_id = user.id  # save before potential rollback (expires ORM objects)

    # Update subscription in memory (will be committed by update_remnawave_user on success)
    subscription.device_limit = new_device_limit
    subscription.updated_at = datetime.now(UTC)

    # Update RemnaWave — commits on success, returns None on failure
    subscription_service = SubscriptionService()
    result = await subscription_service.update_remnawave_user(db, subscription)

    if result is None:
        # RemnaWave update failed — rollback local changes
        await db.rollback()
        logger.error(
            'Failed to update RemnaWave after device limit reduction',
            user_id=user_id,
            old_device_limit=old_device_limit,
            new_device_limit=new_device_limit,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail='Не удалось обновить VPN-панель. Попробуйте позже.',
        )

    logger.info(
        'User reduced device limit',
        user_id=user_id,
        old_device_limit=old_device_limit,
        new_device_limit=new_device_limit,
        devices_removed=devices_removed_count if devices_removed_count > 0 else None,
    )

    return {
        'success': True,
        'message': 'Device limit reduced successfully'
        + (f' ({devices_removed_count} devices removed)' if devices_removed_count > 0 else ''),
        'old_device_limit': old_device_limit,
        'new_device_limit': new_device_limit,
        'devices_removed': devices_removed_count,
    }
