"""Admin routes for broadcasts in cabinet."""

from datetime import UTC, datetime, timedelta

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select, update
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
from app.utils.message_patch import caption_exceeds_telegram_limit
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
# 🔴 РС-14е. Порядок словаря = порядок кнопок на экране (ответ собирается циклом по нему).
# «Все» стояла ВТОРОЙ строкой, вплотную к «только мне»: промах на одну строку означал отправку
# всей базе (304 человека на 30.08.2026). Единственной защитой было подтверждение с числом.
# Теперь «Все» уехала в конец и в собственную группу `broad` — попасть в неё мимо соседней
# кнопки больше нельзя, а подтверждение осталось вторым рубежом, а не единственным.
# ⛔ НЕ возвращать 'all' наверх «для удобства»: удобство здесь и есть дефект.
FILTER_LABELS = {
    'self': 'Тест: только мне',
    'active': 'Действующая подписка, не пробная',
    'trial': 'Сейчас числится пробной (в т.ч. истёкшая)',
    'no': 'Сейчас без подписки',
    'expiring': 'Заканчивается за 3 дня (включая пробные, без активных суточных)',
    'expired': 'Закончилась (включая пробные)',
    'zero': 'Действующая, 0 ГБ за текущий период',
    'active_zero': 'Действующая не пробная, 0 ГБ за период',
    'trial_zero': 'Действующая пробная, 0 ГБ за период',
    'all': 'Все активные с Telegram',
}

FILTER_GROUPS = {
    'self': 'basic',
    'all': 'broad',  # РС-14е: своя группа, чтобы «Все» не стояла соседней строкой с «только мне»
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

# РС-14е применяется и к почте: у неё «все» так же стояла ПЕРВОЙ строкой в группе `basic`,
# то есть ровно в той раскладке, которую этот же пункт объявил опасной у Телеграма.
# Своей канарейки «только мне» у почты нет вовсе — сухой прогон письма сделать нечем,
# поэтому цена промаха здесь выше, а не ниже.
EMAIL_FILTER_LABELS = {
    'email_only': 'Активные email-регистрации с подтверждённым адресом',
    'telegram_with_email': 'Активные Telegram с подтверждённым email',
    'active_email': 'Есть подписка со статусом «активна»',
    'expired_email': 'Есть истёкшая или отключённая подписка',
    'all_email': 'Все активные с подтверждённым email',
}

EMAIL_FILTER_GROUPS = {
    'all_email': 'broad',
    'email_only': 'auth_type',
    'telegram_with_email': 'auth_type',
    'active_email': 'subscription',
    'expired_email': 'subscription',
}


# ============ Helper Functions ============


def _broadcast_target_label(target: str, tariff_labels: dict[int, str] | None = None) -> str:
    """Return a stable human label for history, never an internal filter key."""
    if target in FILTER_LABELS:
        return FILTER_LABELS[target]
    if target in CUSTOM_FILTER_LABELS:
        return CUSTOM_FILTER_LABELS[target]
    if target in EMAIL_FILTER_LABELS:
        return EMAIL_FILTER_LABELS[target]
    if target.startswith('tariff_'):
        tariff_id = target.removeprefix('tariff_')
        if tariff_id.isdigit():
            if tariff_labels and int(tariff_id) in tariff_labels:
                return f'Тариф «{tariff_labels[int(tariff_id)]}»'
            return f'Тариф #{tariff_id}'
    return 'Неизвестная аудитория'


def _serialize_broadcast(
    broadcast: BroadcastHistory,
    tariff_labels: dict[int, str] | None = None,
) -> BroadcastResponse:
    """Serialize broadcast to response model."""
    blocked = broadcast.blocked_count or 0
    progress = 0.0
    if broadcast.total_count > 0:
        progress = round((broadcast.sent_count + broadcast.failed_count + blocked) / broadcast.total_count * 100, 1)

    return BroadcastResponse(
        id=broadcast.id,
        target_type=broadcast.target_type,
        target_label=_broadcast_target_label(broadcast.target_type, tariff_labels),
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


async def _history_tariff_labels(
    db: AsyncSession,
    broadcasts: list[BroadcastHistory],
) -> dict[int, str]:
    tariff_ids = {
        int(item.target_type.removeprefix('tariff_'))
        for item in broadcasts
        if item.target_type.startswith('tariff_') and item.target_type.removeprefix('tariff_').isdigit()
    }
    if not tariff_ids:
        return {}
    result = await db.execute(select(Tariff.id, Tariff.name).where(Tariff.id.in_(tariff_ids)))
    return {tariff_id: name for tariff_id, name in result.all()}


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


async def _resolve_cabinet_telegram_recipients(
    db: AsyncSession,
    target: str,
    category: str,
    admin: User,
    *,
    preloaded_users: list[User] | None = None,
) -> list[int]:
    """Resolve a cabinet target, binding ``self`` only to the authenticated actor."""
    kwargs = {}
    if preloaded_users is not None:
        kwargs['preloaded_users'] = preloaded_users
    if target == 'self':
        kwargs['actor_user_id'] = getattr(admin, 'id', None)
    return await resolve_telegram_broadcast_recipient_ids(db, target, category, **kwargs)


async def _require_exact_self_recipient(
    db: AsyncSession,
    target: str,
    category: str,
    admin: User,
) -> None:
    """Fail before history/worker unless the authenticated self target is exact-one."""
    if target != 'self':
        return
    recipient_ids = await _resolve_cabinet_telegram_recipients(db, target, category, admin)
    if len(recipient_ids) != 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Self test requires exactly one eligible Telegram recipient',
        )


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
            await _resolve_cabinet_telegram_recipients(
                db,
                key,
                category,
                admin,
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
        count = len(await _resolve_cabinet_telegram_recipients(db, key, category, admin))
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
        count = len(
            await _resolve_cabinet_telegram_recipients(
                db,
                request.target,
                request.category,
                admin,
            )
        )
    except Exception as e:
        logger.error('Failed to get count for target', target=request.target, error=e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to count recipients',
        )

    rendered_message_text = None
    media_caption_separate = False
    if request.message_text is not None:
        try:
            rendered_message_text = prepare_telegram_broadcast(request.message_text)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        media_caption_separate = request.has_media and caption_exceeds_telegram_limit(rendered_message_text)

    return BroadcastPreviewResponse(
        target=request.target,
        count=count,
        rendered_message_text=rendered_message_text,
        media_caption_separate=media_caption_separate,
    )


# 🔴 РС-14г (мина GZ). У рассылок нет ключа идемпотентности и уникального индекса: если ответ
# сервера потерялся, а страница перезагрузилась, ничто не мешает создать вторую такую же кампанию
# и прислать людям второе письмо. Черновик при перезагрузке стирается целиком, поэтому повтор
# требует заново набрать текст и выбрать аудиторию — владелец, работающий один, по дороге заглянет
# в список кампаний. Но рассылками занимается уже не только он.
# Настоящую идемпотентность делать нельзя: это миграция и новая колонка. Достаточно окна: две
# ОДИНАКОВЫЕ кампании одного админа на одну аудиторию за пять минут — почти наверняка не замысел.
# Отказ называет номер уже созданной кампании, чтобы человек пошёл смотреть её, а не гадать.
# ⛔ Ставится ПОСЛЕ канонизации текста: сравнивать надо то, что уйдёт, а не то, что набрали.
DUPLICATE_BROADCAST_WINDOW_MINUTES = 5


async def _reject_duplicate_broadcast(
    db: AsyncSession,
    *,
    admin: User,
    target: str,
    message_text: str | None,
    category: str | None = None,
    media_file_id: str | None = None,
    email_subject: str | None = None,
) -> None:
    """Отбить повтор той же кампании на ту же аудиторию в пределах окна.

    ⛔ Сравнивать только по тексту нельзя, это ревью показало на трёх сценариях:
    у почтовой кампании текста нет вовсе (`message_text IS NULL`), и ключ вырождался в
    «тот же админ + та же аудитория» — два РАЗНЫХ письма подряд считались повтором, а совет
    «измените текст» был неисполним; правка вложения, кнопки или категории перед повтором
    канарейки тоже не снимала отказ, хотя кампания уже другая (категория вдобавок меняет
    состав получателей). Поэтому в ключ входит всё, что делает кампанию другой.
    """
    # Канарейку «только мне» повторяют СПЕЦИАЛЬНО — это способ проверить рассылку перед
    # широкой отправкой, и второе письмо самому себе никому не вредит.
    if target == 'self':
        return
    window_start = datetime.now(UTC) - timedelta(minutes=DUPLICATE_BROADCAST_WINDOW_MINUTES)
    text_matches = (
        BroadcastHistory.message_text.is_(None)
        if message_text is None
        else BroadcastHistory.message_text == message_text
    )
    subject_matches = (
        BroadcastHistory.email_subject.is_(None)
        if email_subject is None
        else BroadcastHistory.email_subject == email_subject
    )
    media_matches = (
        BroadcastHistory.media_file_id.is_(None)
        if media_file_id is None
        else BroadcastHistory.media_file_id == media_file_id
    )
    result = await db.execute(
        select(BroadcastHistory.id)
        .where(
            BroadcastHistory.admin_id == admin.id,
            BroadcastHistory.target_type == target,
            BroadcastHistory.category == category,
            text_matches,
            subject_matches,
            media_matches,
            BroadcastHistory.created_at >= window_start,
        )
        .order_by(BroadcastHistory.id.desc())
        .limit(1)
    )
    existing_id = result.scalars().first()
    if existing_id is None:
        return
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=(
            f'Такая же рассылка на эту аудиторию уже создана — кампания #{existing_id}. '
            f'Откройте её в списке и посмотрите результат. Если повтор нужен намеренно, '
            f'подождите {DUPLICATE_BROADCAST_WINDOW_MINUTES} минут или измените текст.'
        ),
    )


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

    await _require_exact_self_recipient(db, request.target, request.category, admin)

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

    await _reject_duplicate_broadcast(
        db,
        admin=admin,
        target=request.target,
        message_text=message_text,
        category=request.category,
        media_file_id=request.media.file_id if request.media else None,
    )

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
        actor_user_id=admin.id if request.target == 'self' else None,
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
    tariff_labels = await _history_tariff_labels(db, broadcasts)

    return BroadcastListResponse(
        items=[_serialize_broadcast(b, tariff_labels) for b in broadcasts],
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

    if request.channel == 'both' and request.target == 'self':
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Self test supports Telegram only; select an Email target separately',
        )

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

        await _require_exact_self_recipient(db, request.target, request.category, admin)

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

    await _reject_duplicate_broadcast(
        db,
        admin=admin,
        target=request.target,
        message_text=telegram_message_text,
        category=request.category,
        media_file_id=request.media.file_id if request.media else None,
        email_subject=request.email_subject.strip() if request.email_subject else None,
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
            actor_user_id=admin.id if request.target == 'self' else None,
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
    tariff_labels = await _history_tariff_labels(db, [broadcast])
    return _serialize_broadcast(broadcast, tariff_labels)


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

    # Compare-and-set active -> cancelling before touching the worker. A terminal
    # worker write can now win before OR after this atomic statement, but can
    # never be overwritten by a stale ORM object from this request.
    active_statuses = {'queued', 'in_progress', 'cancelling'}
    await db.execute(
        update(BroadcastHistory)
        .where(BroadcastHistory.id == broadcast_id, BroadcastHistory.status.in_(active_statuses))
        .values(status='cancelling')
    )
    await db.commit()
    await db.refresh(broadcast)
    if broadcast.status not in active_statuses:
        return _serialize_broadcast(broadcast)

    # Try to stop both telegram and email broadcasts (one or both may be running)
    channel = getattr(broadcast, 'channel', 'telegram') or 'telegram'

    is_running = False
    if channel in ('telegram', 'both'):
        is_running = await broadcast_service.request_stop(broadcast_id) or is_running

    if channel in ('email', 'both'):
        is_running = await email_broadcast_service.request_stop(broadcast_id) or is_running

    if not is_running:
        await db.execute(
            update(BroadcastHistory)
            .where(BroadcastHistory.id == broadcast_id, BroadcastHistory.status.in_(active_statuses))
            .values(status='cancelled', completed_at=datetime.now(UTC))
        )
        await db.commit()
    await db.refresh(broadcast)

    logger.info('Admin stopped broadcast', admin_id=admin.id, broadcast_id=broadcast_id)

    return _serialize_broadcast(broadcast)
