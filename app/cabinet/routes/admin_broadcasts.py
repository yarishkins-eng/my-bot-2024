"""Admin routes for broadcasts in cabinet."""

from datetime import UTC, datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import BroadcastHistory, Tariff, User
from app.handlers.admin.messages import get_target_users
from app.keyboards.admin import BROADCAST_BUTTONS, DEFAULT_BROADCAST_BUTTONS
from app.services.broadcast_service import (
    BroadcastConfig,
    BroadcastMediaConfig,
    EmailBroadcastConfig,
    broadcast_service,
    email_broadcast_service,
    resolve_email_broadcast_recipients,
    resolve_telegram_broadcast_recipient_ids,
)
from app.utils.telegram_html import prepare_telegram_broadcast

from ..dependencies import get_cabinet_db, require_permission
from ..schemas.broadcasts import (
    BroadcastButton,
    BroadcastButtonsResponse,
    BroadcastCreateRequest,
    BroadcastFilter,
    BroadcastFiltersResponse,
    BroadcastListResponse,
    BroadcastPreviewRequest,
    BroadcastPreviewResponse,
    BroadcastResponse,
    BroadcastTariffsResponse,
    CombinedBroadcastCreateRequest,
    EmailFilterItem,
    EmailFiltersResponse,
    EmailPreviewRequest,
    EmailPreviewResponse,
    TariffFilter,
    TariffForBroadcast,
)


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/admin/broadcasts', tags=['Cabinet Admin Broadcasts'])

SAFE_CUSTOM_BROADCAST_CALLBACKS = frozenset(
    {config['callback'] for config in BROADCAST_BUTTONS.values()} | {'menu_buy'}
)


# ============ Filter Labels ============

# 🔴 РС-3. Названия обязаны совпадать с тем, что фильтр РЕАЛЬНО отбирает, — прежние
# обещали не то. Предикаты перепроверены по коду 28.08.2026
# (`app/handlers/admin/messages.py`, `get_target_users`):
#   expiring — конец в ближайшие 3 дня и БЕЗ отделения пробных. А пробная длится ровно
#              3 дня (`TRIAL_DURATION_DAYS`), значит любая живая пробная сидит в этом окне
#              с первой секунды. «Истекающие» на деле означало «почти все пробные»;
#   expired  — нет действующей + есть истёкшая: пробные и платные вперемешку;
#   trial    — любая подписка с флагом «пробная», хоть живая, хоть давно мёртвая;
#   no       — нет ДЕЙСТВУЮЩЕЙ сейчас, а вовсе не «никогда не пробовал»;
#   *_zero   — потрачено 0 ГБ, то есть «ни разу не подключался», а НЕ «кончился трафик».
# ⛔ РС-9 не меняет состав пользовательских сегментов. Канальная проекция ниже
# исключает недоступные адреса и opt-out, а подписи честно называют оставшиеся
# особенности сегмента.
FILTER_LABELS = {
    'all': 'Все активные с Telegram',
    'active': 'Действующая подписка, не пробная',
    'trial': 'Сейчас числится пробной (в т.ч. истёкшая)',
    'no': 'Сейчас без подписки',
    'expiring': 'Заканчивается за 3 дня (включая пробные, без активных суточных)',
    'expired': 'Закончилась (включая пробные)',
    'zero': 'Действующая, 0 ГБ за текущий период',
    'active_zero': 'Действующая не пробная, 0 ГБ за период',
    'trial_zero': 'Действующая пробная, 0 ГБ за период',
}

FILTER_GROUPS = {
    'all': 'basic',
    'active': 'subscription',
    'trial': 'subscription',
    'no': 'subscription',
    'expiring': 'subscription',
    'expired': 'subscription',
    'zero': 'traffic',
    'active_zero': 'traffic',
    'trial_zero': 'traffic',
}

CUSTOM_FILTER_LABELS = {
    'custom_today': 'Регистрация сегодня',
    'custom_week': 'Регистрация за неделю',
    'custom_month': 'Регистрация за месяц',
    'custom_active_today': 'Активны сегодня',
    'custom_inactive_week': 'Неактивны 7+ дней',
    'custom_inactive_month': 'Неактивны 30+ дней',
    'custom_referrals': 'Пришли по рефералу',
    'custom_direct': 'Прямая регистрация',
}

CUSTOM_FILTER_GROUPS = {
    'custom_today': 'registration',
    'custom_week': 'registration',
    'custom_month': 'registration',
    'custom_active_today': 'activity',
    'custom_inactive_week': 'activity',
    'custom_inactive_month': 'activity',
    'custom_referrals': 'source',
    'custom_direct': 'source',
}


# ============ Email Filter Labels ============

EMAIL_FILTER_LABELS = {
    'all_email': 'Все активные с подтверждённым email',
    'email_only': 'Активные email-регистрации с подтверждённым адресом',
    'telegram_with_email': 'Активные Telegram с подтверждённым email',
    'active_email': 'Есть подписка со статусом «активна»',
    'expired_email': 'Есть истёкшая или отключённая подписка',
}

EMAIL_FILTER_GROUPS = {
    'all_email': 'basic',
    'email_only': 'auth_type',
    'telegram_with_email': 'auth_type',
    'active_email': 'subscription',
    'expired_email': 'subscription',
}


# ============ Helper Functions ============


def _serialize_broadcast(broadcast: BroadcastHistory) -> BroadcastResponse:
    """Serialize broadcast to response model."""
    blocked = broadcast.blocked_count or 0
    progress = 0.0
    if broadcast.total_count > 0:
        progress = round((broadcast.sent_count + broadcast.failed_count + blocked) / broadcast.total_count * 100, 1)

    return BroadcastResponse(
        id=broadcast.id,
        target_type=broadcast.target_type,
        message_text=broadcast.message_text,
        has_media=broadcast.has_media,
        media_type=broadcast.media_type,
        media_file_id=broadcast.media_file_id,
        media_caption=broadcast.media_caption,
        total_count=broadcast.total_count,
        sent_count=broadcast.sent_count,
        failed_count=broadcast.failed_count,
        blocked_count=blocked,
        status=broadcast.status,
        admin_id=broadcast.admin_id,
        admin_name=broadcast.admin_name,
        created_at=broadcast.created_at,
        completed_at=broadcast.completed_at,
        progress_percent=progress,
        category=getattr(broadcast, 'category', 'system') or 'system',
        channel=getattr(broadcast, 'channel', 'telegram') or 'telegram',
        email_subject=getattr(broadcast, 'email_subject', None),
        email_html_content=getattr(broadcast, 'email_html_content', None),
    )


async def _get_email_filter_count(db: AsyncSession, target: str, category: str = 'system') -> int:
    """Count the same unique email recipients the worker will materialize."""
    return len(await resolve_email_broadcast_recipients(db, target, category))


def _validate_email_target(target: str) -> bool:
    """Validate email target filter."""
    return target in EMAIL_FILTER_LABELS


async def _get_tariff_user_counts(
    db: AsyncSession,
    category: str = 'system',
    *,
    preloaded_users: list[User] | None = None,
) -> dict[int, int]:
    """Get actual Telegram recipient counts per tariff."""
    result = await db.execute(select(Tariff.id).where(Tariff.is_active == True))
    tariff_ids = [row[0] for row in result.all()]
    return {
        tariff_id: len(
            await resolve_telegram_broadcast_recipient_ids(
                db,
                f'tariff_{tariff_id}',
                category,
                preloaded_users=preloaded_users,
            )
        )
        for tariff_id in tariff_ids
    }


def _validate_target(target: str, tariff_ids: set) -> bool:
    """Validate target value."""
    if target in FILTER_LABELS:
        return True
    if target in CUSTOM_FILTER_LABELS:
        return True
    if target.startswith('tariff_'):
        try:
            tariff_id = int(target.split('_')[1])
            return tariff_id in tariff_ids
        except (ValueError, IndexError):
            return False
    return False


def _validate_buttons(buttons: list[str]) -> bool:
    """Validate button keys."""
    return all(button in BROADCAST_BUTTONS for button in buttons)


def _validate_custom_broadcast_callbacks(custom_buttons) -> None:
    """Reject callbacks that are unsafe or have no stable public handler."""
    invalid_callbacks = sorted(
        {
            button.action_value
            for button in custom_buttons
            if button.action_type == 'callback' and button.action_value not in SAFE_CUSTOM_BROADCAST_CALLBACKS
        }
    )
    if invalid_callbacks:
        allowed = ', '.join(sorted(SAFE_CUSTOM_BROADCAST_CALLBACKS))
        invalid = ', '.join(invalid_callbacks)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f'Unknown callback action: {invalid}. Allowed actions: {allowed}',
        )


# ============ Endpoints ============


@router.get('/filters', response_model=BroadcastFiltersResponse)
async def get_filters(
    category: str = Query('system', pattern='^(system|news|promo)$'),
    admin: User = Depends(require_permission('broadcasts:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> BroadcastFiltersResponse:
    """Get filters with actual unique Telegram recipient counts."""
    preloaded_users = await get_target_users(db, 'all')
    # Basic filters
    filters = []
    for key, label in FILTER_LABELS.items():
        count = len(
            await resolve_telegram_broadcast_recipient_ids(
                db,
                key,
                category,
                preloaded_users=preloaded_users,
            )
        )
        filters.append(
            BroadcastFilter(
                key=key,
                label=label,
                count=count,
                group=FILTER_GROUPS.get(key),
            )
        )

    # Custom filters
    custom_filters = []
    for key, label in CUSTOM_FILTER_LABELS.items():
        count = len(await resolve_telegram_broadcast_recipient_ids(db, key, category))
        custom_filters.append(
            BroadcastFilter(
                key=key,
                label=label,
                count=count,
                group=CUSTOM_FILTER_GROUPS.get(key),
            )
        )

    # Tariff filters
    tariff_counts = await _get_tariff_user_counts(db, category, preloaded_users=preloaded_users)
    result = await db.execute(select(Tariff).where(Tariff.is_active == True).order_by(Tariff.name))
    tariffs = result.scalars().all()

    tariff_filters = []
    for tariff in tariffs:
        tariff_filters.append(
            TariffFilter(
                key=f'tariff_{tariff.id}',
                label=tariff.name,
                tariff_id=tariff.id,
                count=tariff_counts.get(tariff.id, 0),
            )
        )

    return BroadcastFiltersResponse(
        filters=filters,
        tariff_filters=tariff_filters,
        custom_filters=custom_filters,
    )


@router.get('/tariffs', response_model=BroadcastTariffsResponse)
async def get_tariffs(
    category: str = Query('system', pattern='^(system|news|promo)$'),
    admin: User = Depends(require_permission('broadcasts:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> BroadcastTariffsResponse:
    """Get tariffs for broadcast filtering."""
    preloaded_users = await get_target_users(db, 'all')
    tariff_counts = await _get_tariff_user_counts(db, category, preloaded_users=preloaded_users)
    result = await db.execute(select(Tariff).where(Tariff.is_active == True).order_by(Tariff.name))
    tariffs = result.scalars().all()

    return BroadcastTariffsResponse(
        tariffs=[
            TariffForBroadcast(
                id=t.id,
                name=t.name,
                filter_key=f'tariff_{t.id}',
                active_users_count=tariff_counts.get(t.id, 0),
            )
            for t in tariffs
        ]
    )


@router.get('/buttons', response_model=BroadcastButtonsResponse)
async def get_buttons(
    admin: User = Depends(require_permission('broadcasts:read')),
) -> BroadcastButtonsResponse:
    """Get available buttons for broadcasts."""
    default_buttons = set(DEFAULT_BROADCAST_BUTTONS)
    buttons = []
    for key, config in BROADCAST_BUTTONS.items():
        buttons.append(
            BroadcastButton(
                key=key,
                label=config.get('default_text', key),
                default=key in default_buttons,
            )
        )
    return BroadcastButtonsResponse(buttons=buttons)


@router.post('/preview', response_model=BroadcastPreviewResponse)
async def preview_broadcast(
    request: BroadcastPreviewRequest,
    admin: User = Depends(require_permission('broadcasts:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> BroadcastPreviewResponse:
    """Preview broadcast recipients count."""
    # Get tariff IDs for validation
    result = await db.execute(select(Tariff.id))
    tariff_ids = {row[0] for row in result.all()}

    if not _validate_target(request.target, tariff_ids):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f'Invalid target: {request.target}',
        )

    try:
        count = len(await resolve_telegram_broadcast_recipient_ids(db, request.target, request.category))
    except Exception as e:
        logger.error('Failed to get count for target', target=request.target, error=e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to count recipients',
        )

    return BroadcastPreviewResponse(target=request.target, count=count)


@router.post('', response_model=BroadcastResponse, status_code=status.HTTP_201_CREATED)
async def create_broadcast(
    request: BroadcastCreateRequest,
    admin: User = Depends(require_permission('broadcasts:create')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> BroadcastResponse:
    """Create and start a broadcast."""
    # Validate target
    result = await db.execute(select(Tariff.id))
    tariff_ids = {row[0] for row in result.all()}

    if not _validate_target(request.target, tariff_ids):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f'Invalid target: {request.target}',
        )

    # Validate buttons
    if not _validate_buttons(request.selected_buttons):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Invalid button key',
        )

    _validate_custom_broadcast_callbacks(request.custom_buttons)

    try:
        message_text = prepare_telegram_broadcast(request.message_text)
        media_caption = (
            prepare_telegram_broadcast(
                request.media.caption if request.media.caption and request.media.caption.strip() else message_text
            )
            if request.media
            else None
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    media_payload = request.media

    # Create broadcast record
    broadcast = BroadcastHistory(
        target_type=request.target,
        message_text=message_text,
        has_media=media_payload is not None,
        media_type=media_payload.type if media_payload else None,
        media_file_id=media_payload.file_id if media_payload else None,
        media_caption=media_caption,
        total_count=0,
        sent_count=0,
        failed_count=0,
        status='queued',
        admin_id=admin.id,
        admin_name=admin.username or f'Admin #{admin.id}',
        category=request.category,
    )
    db.add(broadcast)
    await db.commit()
    await db.refresh(broadcast)

    # Prepare media config
    media_config = None
    if media_payload:
        media_config = BroadcastMediaConfig(
            type=media_payload.type,
            file_id=media_payload.file_id,
            caption=media_caption or message_text,
        )

    # Create broadcast config
    config = BroadcastConfig(
        target=request.target,
        message_text=message_text,
        selected_buttons=request.selected_buttons,
        media=media_config,
        initiator_name=admin.username or f'Admin #{admin.id}',
        custom_buttons=[btn.model_dump() for btn in request.custom_buttons] if request.custom_buttons else None,
        category=request.category,
    )

    # Start broadcast
    await broadcast_service.start_broadcast(broadcast.id, config)
    await db.refresh(broadcast)

    logger.info(
        'Admin created broadcast for target', admin_id=admin.id, broadcast_id=broadcast.id, target=request.target
    )

    return _serialize_broadcast(broadcast)


@router.get('', response_model=BroadcastListResponse)
async def list_broadcasts(
    admin: User = Depends(require_permission('broadcasts:read')),
    db: AsyncSession = Depends(get_cabinet_db),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> BroadcastListResponse:
    """Get list of broadcasts with pagination."""
    total = await db.scalar(select(func.count(BroadcastHistory.id))) or 0

    result = await db.execute(
        select(BroadcastHistory).order_by(BroadcastHistory.created_at.desc()).offset(offset).limit(limit)
    )
    broadcasts = result.scalars().all()

    return BroadcastListResponse(
        items=[_serialize_broadcast(b) for b in broadcasts],
        total=int(total),
        limit=limit,
        offset=offset,
    )


# ============ Email Broadcast Endpoints ============


@router.get('/email-filters', response_model=EmailFiltersResponse)
async def get_email_filters(
    category: str = Query('system', pattern='^(system|news|promo)$'),
    admin: User = Depends(require_permission('broadcasts:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> EmailFiltersResponse:
    """Get all available email filters with user counts."""
    filters = []
    total_with_email = 0

    for key, label in EMAIL_FILTER_LABELS.items():
        count = await _get_email_filter_count(db, key, category)

        filters.append(
            EmailFilterItem(
                key=key,
                label=label,
                count=count,
                group=EMAIL_FILTER_GROUPS.get(key),
            )
        )

        # Track total with email (all_email filter)
        if key == 'all_email':
            total_with_email = count

    return EmailFiltersResponse(
        filters=filters,
        total_with_email=total_with_email,
    )


@router.post('/email-preview', response_model=EmailPreviewResponse)
async def preview_email_broadcast(
    request: EmailPreviewRequest,
    admin: User = Depends(require_permission('broadcasts:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> EmailPreviewResponse:
    """Preview email broadcast recipients count."""
    if not _validate_email_target(request.target):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f'Invalid email target: {request.target}',
        )

    try:
        count = await _get_email_filter_count(db, request.target, request.category)
    except Exception as e:
        logger.error('Failed to get email count for target', target=request.target, error=e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to count email recipients',
        )

    return EmailPreviewResponse(target=request.target, count=count)


@router.post('/send', response_model=BroadcastResponse, status_code=status.HTTP_201_CREATED)
async def create_combined_broadcast(
    request: CombinedBroadcastCreateRequest,
    admin: User = Depends(require_permission('broadcasts:send')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> BroadcastResponse:
    """Create and start a combined broadcast (telegram/email/both)."""
    # Get tariff IDs for target validation
    result = await db.execute(select(Tariff.id))
    tariff_ids = {row[0] for row in result.all()}

    admin_name = admin.username or f'Admin #{admin.id}'

    telegram_message_text: str | None = None
    media_caption: str | None = None

    # Validate based on channel
    if request.channel in ('telegram', 'both'):
        # Validate telegram target
        if not _validate_target(request.target, tariff_ids):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f'Invalid target: {request.target}',
            )

        try:
            telegram_message_text = prepare_telegram_broadcast(request.message_text)
            media_caption = (
                prepare_telegram_broadcast(
                    request.media.caption
                    if request.media.caption and request.media.caption.strip()
                    else telegram_message_text
                )
                if request.media
                else None
            )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

        # Validate buttons
        if not _validate_buttons(request.selected_buttons):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='Invalid button key',
            )

        _validate_custom_broadcast_callbacks(request.custom_buttons)

    if request.channel in ('email', 'both'):
        # For email channel, target must be email filter or we use telegram target for 'both'
        if request.channel == 'email' and not _validate_email_target(request.target):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f'Invalid email target: {request.target}',
            )

        # Validate email fields
        if not request.email_subject or not request.email_subject.strip():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='Email subject is required for email broadcast',
            )

        if not request.email_html_content or not request.email_html_content.strip():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='Email HTML content is required for email broadcast',
            )

    media_payload = request.media

    # Create broadcast record
    broadcast = BroadcastHistory(
        target_type=request.target,
        message_text=telegram_message_text,
        has_media=media_payload is not None,
        media_type=media_payload.type if media_payload else None,
        media_file_id=media_payload.file_id if media_payload else None,
        media_caption=media_caption,
        total_count=0,
        sent_count=0,
        failed_count=0,
        status='queued',
        admin_id=admin.id,
        admin_name=admin_name,
        category=request.category,
        channel=request.channel,
        email_subject=request.email_subject.strip() if request.email_subject else None,
        email_html_content=request.email_html_content.strip() if request.email_html_content else None,
    )
    db.add(broadcast)
    await db.commit()
    await db.refresh(broadcast)

    # Start broadcasts based on channel
    if request.channel in ('telegram', 'both'):
        assert telegram_message_text is not None
        # Prepare media config
        media_config = None
        if media_payload:
            media_config = BroadcastMediaConfig(
                type=media_payload.type,
                file_id=media_payload.file_id,
                caption=media_caption or telegram_message_text,
            )

        # Create telegram broadcast config
        telegram_config = BroadcastConfig(
            target=request.target,
            message_text=telegram_message_text,
            selected_buttons=request.selected_buttons,
            media=media_config,
            initiator_name=admin_name,
            custom_buttons=[btn.model_dump() for btn in request.custom_buttons] if request.custom_buttons else None,
            category=request.category,
        )

        await broadcast_service.start_broadcast(broadcast.id, telegram_config)

    if request.channel in ('email', 'both'):
        # For 'both' channel, we use 'all_email' as default email target
        # since telegram target won't match email filters
        email_target = request.target if request.channel == 'email' else 'all_email'

        # Create email broadcast config
        email_config = EmailBroadcastConfig(
            target=email_target,
            email_subject=request.email_subject.strip(),
            email_html_content=request.email_html_content.strip(),
            initiator_name=admin_name,
            category=request.category,
        )

        await email_broadcast_service.start_broadcast(broadcast.id, email_config)

    await db.refresh(broadcast)

    logger.info(
        'Admin created broadcast for target',
        admin_id=admin.id,
        channel=request.channel,
        broadcast_id=broadcast.id,
        target=request.target,
    )

    return _serialize_broadcast(broadcast)


@router.get('/{broadcast_id}', response_model=BroadcastResponse)
async def get_broadcast(
    broadcast_id: int,
    admin: User = Depends(require_permission('broadcasts:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> BroadcastResponse:
    """Get broadcast details."""
    broadcast = await db.get(BroadcastHistory, broadcast_id)
    if not broadcast:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Broadcast not found',
        )
    return _serialize_broadcast(broadcast)


@router.post('/{broadcast_id}/stop', response_model=BroadcastResponse)
async def stop_broadcast(
    broadcast_id: int,
    admin: User = Depends(require_permission('broadcasts:send')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> BroadcastResponse:
    """Stop a running broadcast (telegram or email)."""
    broadcast = await db.get(BroadcastHistory, broadcast_id)
    if not broadcast:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Broadcast not found',
        )

    if broadcast.status not in {'queued', 'in_progress', 'cancelling'}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Broadcast is not running',
        )

    # Try to stop both telegram and email broadcasts (one or both may be running)
    channel = getattr(broadcast, 'channel', 'telegram') or 'telegram'

    is_running = False
    if channel in ('telegram', 'both'):
        is_running = await broadcast_service.request_stop(broadcast_id) or is_running

    if channel in ('email', 'both'):
        is_running = await email_broadcast_service.request_stop(broadcast_id) or is_running

    if is_running:
        broadcast.status = 'cancelling'
    else:
        broadcast.status = 'cancelled'
        broadcast.completed_at = datetime.now(UTC)

    await db.commit()
    await db.refresh(broadcast)

    logger.info('Admin stopped broadcast', admin_id=admin.id, broadcast_id=broadcast_id)

    return _serialize_broadcast(broadcast)
