import asyncio
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime

import structlog
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.server_squad import get_all_server_squads
from app.database.crud.user import get_user_by_id
from app.database.models import Subscription, SubscriptionStatus, User
from app.external.remnawave_api import RemnaWaveAPI, RemnaWaveAPIError, RemnaWaveUser, TrafficLimitStrategy, UserStatus
from app.utils.grace import is_in_grace, resolve_panel_active_and_expiry
from app.utils.subscription_utils import (
    resolve_hwid_device_limit_for_payload,
)


logger = structlog.get_logger(__name__)


def get_traffic_reset_strategy(tariff=None):
    """Получает стратегию сброса трафика.

    Args:
        tariff: Объект тарифа. Если у тарифа задан traffic_reset_mode,
               используется он, иначе глобальная настройка из конфига.

    Returns:
        TrafficLimitStrategy: Стратегия сброса трафика для RemnaWave API.
    """
    from app.config import settings

    strategy_mapping = {
        'NO_RESET': 'NO_RESET',
        'DAY': 'DAY',
        'WEEK': 'WEEK',
        'MONTH': 'MONTH',
        'MONTH_ROLLING': 'MONTH_ROLLING',
    }

    # Проверяем настройку тарифа
    if tariff is not None:
        tariff_mode = getattr(tariff, 'traffic_reset_mode', None)
        if tariff_mode is not None:
            mapped_strategy = strategy_mapping.get(tariff_mode.upper(), 'NO_RESET')
            logger.info(
                '🔄 Стратегия сброса трафика из тарифа',
                value=getattr(tariff, 'name', 'N/A'),
                tariff_mode=tariff_mode,
                mapped_strategy=mapped_strategy,
            )
            return getattr(TrafficLimitStrategy, mapped_strategy)

    # Используем глобальную настройку
    strategy = settings.DEFAULT_TRAFFIC_RESET_STRATEGY.upper()
    mapped_strategy = strategy_mapping.get(strategy, 'NO_RESET')
    logger.info('🔄 Стратегия сброса трафика из конфига', strategy=strategy, mapped_strategy=mapped_strategy)
    return getattr(TrafficLimitStrategy, mapped_strategy)


def find_panel_echo_mismatch(sent: dict, panel_user: RemnaWaveUser) -> str | None:
    """Сверяет ответ панели ТОЛЬКО по полям, которые реально ушли в этом запросе.

    Диагностика, а не запрет: результат идёт в лог и не влияет на исход синхронизации
    (см. вызов в update_remnawave_user). Функция обязана быть неспособной бросить
    исключение — иначе она сама станет источником отказа выдачи.

    Требовать подтверждения полей, которых в запросе не было, нельзя: часть подписок
    зависла бы в вечном повторе. Сквады уходят только непустым списком, лимит устройств —
    только когда резолвер вернул не None, а при включённом grace `expireAt` равен
    `grace_until`, а не `end_date`. Поэтому источник ожидания — сам payload.

    Returns:
        Имя разошедшегося поля либо None, если всё отправленное подтверждено.
    """
    # Условия — как на проводе: expire_at уходит по truthiness (remnawave_api.py:782-783),
    # остальные по `is not None` (:788, :794). Иначе потребуем подтверждения неотправленного.
    #
    # status первым: это единственное поле, от которого зависит, работает ли VPN вообще.
    # Срок, устройства и серверы могут совпасть все три, а профиль стоять DISABLED.
    if sent.get('status') is not None and getattr(panel_user, 'status', None) != sent['status']:
        return 'status'

    if sent.get('expire_at'):
        expected_expire = sent['expire_at']
        actual_expire = getattr(panel_user, 'expire_at', None)
        if actual_expire is None:
            return 'expire_at'
        expected_utc = expected_expire if expected_expire.tzinfo else expected_expire.replace(tzinfo=UTC)
        actual_utc = actual_expire if actual_expire.tzinfo else actual_expire.replace(tzinfo=UTC)
        # Допуск на округление времени панелью; сдвиг срока меряется днями, не секундами.
        if abs((actual_utc - expected_utc).total_seconds()) > 60:
            return 'expire_at'

    if (
        sent.get('hwid_device_limit') is not None
        and getattr(panel_user, 'hwid_device_limit', None) != sent['hwid_device_limit']
    ):
        return 'hwid_device_limit'

    if sent.get('active_internal_squads'):
        # Форма элемента не гарантирована: в бою приходят словари, но код проекта уже
        # обжигался на этом (см. admin_users.py:3383-3395) и разбирает три формы. Тут
        # исключение недопустимо вдвойне: оно всплывёт как отказ синхронизации.
        actual_squads = set()
        for squad in getattr(panel_user, 'active_internal_squads', None) or []:
            if isinstance(squad, dict):
                squad_uuid = squad.get('uuid')
            elif isinstance(squad, str):
                squad_uuid = squad
            else:
                squad_uuid = getattr(squad, 'uuid', None)
            if squad_uuid:
                actual_squads.add(squad_uuid)
        # Панель может вернуть надмножество (например, сквад от externalSquadUuid),
        # поэтому требуем включения отправленного, а не точного равенства.
        if not set(sent['active_internal_squads']).issubset(actual_squads):
            return 'active_internal_squads'

    return None


@dataclass
class PropagateSquadsResult:
    """Результат применения скводов тарифа к подпискам."""

    total: int = 0
    synced: int = 0
    failed_ids: list[int] = field(default_factory=list)


class SubscriptionService:
    def __init__(self):
        self._config_error: str | None = None
        self.api: RemnaWaveAPI | None = None
        self._last_config_signature: tuple[str, ...] | None = None

        self._refresh_configuration()

    def _refresh_configuration(self) -> None:
        auth_params = settings.get_remnawave_auth_params()
        base_url = (auth_params.get('base_url') or '').strip()
        api_key = (auth_params.get('api_key') or '').strip()
        secret_key = (auth_params.get('secret_key') or '').strip() or None
        username = (auth_params.get('username') or '').strip() or None
        password = (auth_params.get('password') or '').strip() or None
        caddy_token = (auth_params.get('caddy_token') or '').strip() or None
        auth_type = (auth_params.get('auth_type') or 'api_key').strip()

        config_signature = (
            base_url,
            api_key,
            secret_key or '',
            username or '',
            password or '',
            caddy_token or '',
            auth_type,
        )

        if config_signature == self._last_config_signature:
            return

        if not base_url:
            self._config_error = 'REMNAWAVE_API_URL не настроен'
            self.api = None
        elif not api_key:
            self._config_error = 'REMNAWAVE_API_KEY не настроен'
            self.api = None
        else:
            self._config_error = None
            self.api = RemnaWaveAPI(
                base_url=base_url,
                api_key=api_key,
                secret_key=secret_key,
                username=username,
                password=password,
                caddy_token=caddy_token,
                auth_type=auth_type,
            )

        if self._config_error:
            logger.warning(
                'RemnaWave API недоступен. Подписочный сервис будет работать в оффлайн-режиме.',
                config_error=self._config_error,
            )

        self._last_config_signature = config_signature

    @staticmethod
    def _resolve_user_tag(subscription: Subscription) -> str | None:
        if getattr(subscription, 'is_trial', False):
            return settings.get_trial_user_tag()

        return settings.get_paid_subscription_user_tag()

    @property
    def is_configured(self) -> bool:
        return self._config_error is None

    @property
    def configuration_error(self) -> str | None:
        return self._config_error

    def _ensure_configured(self) -> None:
        self._refresh_configuration()
        if not self.api or not self.is_configured:
            raise RemnaWaveAPIError(self._config_error or 'RemnaWave API не настроен')

    @asynccontextmanager
    async def get_api_client(self):
        self._ensure_configured()
        assert self.api is not None
        async with self.api as api:
            yield api

    @staticmethod
    async def _load_user_for_panel_write(db: AsyncSession, user_id: int) -> User | None:
        """Load current account state immediately before an external VPN write.

        A subscription can be held by a background worker for minutes.  Its
        already-loaded ``User`` object is therefore not a safe authorization
        source for enabling VPN access: a financial account closure may have
        committed after the worker fetched it.  ``populate_existing`` makes
        this an explicit database re-read rather than an identity-map hit.
        """
        user = await db.scalar(
            select(User).where(User.id == user_id).with_for_update().execution_options(populate_existing=True)
        )
        if user is None:
            return None
        if getattr(user, 'account_erasure_requested_at', None) is not None:
            logger.warning(
                'panel_write_blocked_for_financial_account_closure',
                user_id=user_id,
            )
            return None
        return user

    @staticmethod
    async def _account_erasure_started(db: AsyncSession, user_id: int) -> bool:
        """Return the durable close marker using a fresh read."""
        return (
            await db.scalar(
                select(User.account_erasure_requested_at)
                .where(User.id == user_id)
                .execution_options(populate_existing=True)
            )
        ) is not None

    async def _delete_panel_identity_if_closed_after_write(
        self,
        api: RemnaWaveAPI,
        db: AsyncSession,
        *,
        user_id: int,
        panel_uuid: str,
    ) -> bool:
        """Compensate the only unavoidable external race during panel create.

        If an account closure committed after the preflight read but before a
        provider request, the closing worker may have deleted the old panel
        identity first.  Delete the identity we just observed before returning
        control; a closing account must never retain a newly-created VPN user.
        """
        if not await self._account_erasure_started(db, user_id):
            return False
        try:
            await api.delete_user(panel_uuid)
        except Exception as error:
            # Account-erasure service has an independent retry state for this
            # exact panel cleanup.  Do not write credentials locally.
            logger.exception(
                'panel_write_compensation_failed_after_financial_closure',
                user_id=user_id,
                panel_uuid=panel_uuid,
                error=error,
            )
            from app.services.account_erasure_service import record_panel_cleanup_retry_for_financial_closure

            await record_panel_cleanup_retry_for_financial_closure(user_id=user_id, panel_uuid=panel_uuid)
        logger.warning(
            'panel_write_compensated_after_financial_closure',
            user_id=user_id,
            panel_uuid=panel_uuid,
        )
        return True

    async def run_guarded_panel_write(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        api: RemnaWaveAPI,
        operation: Callable[[], Awaitable[RemnaWaveUser]],
        panel_uuid: str | None = None,
    ) -> RemnaWaveUser | None:
        """Run a raw panel write behind the financial-erasure fence.

        Some administrative flows need a narrower RemnaWave payload than the
        normal subscription synchroniser.  They must still use the same
        before/after database check as the canonical create flow: a close that
        lands during the HTTP call deletes the just-written identity and the
        caller must not persist new URLs or UUIDs.
        """
        # Take the same row lock used by account closure *before* issuing the
        # remote write.  The caller persists its UUID and commits before a
        # closer can set the marker; if the provider accepted a request but
        # its response was lost, closure then performs a stable-ID panel sweep
        # while the pre-erasure Telegram/email identifiers still exist.
        if await self._load_user_for_panel_write(db, user_id) is None:
            logger.warning('raw_panel_write_blocked_for_financial_account_closure', user_id=user_id)
            return None

        result = await operation()
        if await self._account_erasure_started(db, user_id):
            cleanup_uuid = getattr(result, 'uuid', None) or panel_uuid
            if cleanup_uuid:
                try:
                    await api.delete_user(cleanup_uuid)
                except Exception as error:
                    logger.exception(
                        'raw_panel_write_compensation_failed_after_financial_closure',
                        user_id=user_id,
                        panel_uuid=cleanup_uuid,
                        error=error,
                    )
                    from app.services.account_erasure_service import record_panel_cleanup_retry_for_financial_closure

                    await record_panel_cleanup_retry_for_financial_closure(user_id=user_id, panel_uuid=cleanup_uuid)
            logger.warning(
                'raw_panel_write_compensated_after_financial_closure',
                user_id=user_id,
                panel_uuid=cleanup_uuid,
            )
            return None
        return result

    async def _panel_uuid_is_financially_closing(self, db: AsyncSession, user_uuid: str) -> bool:
        """Resolve a panel UUID to its user and refuse a stale enable call."""
        user = await db.scalar(
            select(User)
            .outerjoin(Subscription, Subscription.user_id == User.id)
            .where(or_(User.remnawave_uuid == user_uuid, Subscription.remnawave_uuid == user_uuid))
            .execution_options(populate_existing=True)
        )
        return bool(user is not None and getattr(user, 'account_erasure_requested_at', None) is not None)

    @staticmethod
    async def _is_access_point_subscription(db: AsyncSession, subscription: Subscription) -> bool:
        """Whether raw Panel writes for this subscription are term-owned."""
        from app.services.public_access_point_service import is_term_owned_access_point_subscription

        return await is_term_owned_access_point_subscription(db, subscription)

    async def create_remnawave_user(
        self,
        db: AsyncSession,
        subscription: Subscription,
        *,
        reset_traffic: bool = False,
        reset_reason: str | None = None,
        commit: bool = True,
        access_point_term_projection: bool = False,
        access_point_term_ends_at: datetime | None = None,
    ) -> RemnaWaveUser | None:
        try:
            if await self._is_access_point_subscription(db, subscription) and not access_point_term_projection:
                logger.warning(
                    'raw_panel_create_blocked_for_access_point_term',
                    subscription_id=subscription.id,
                )
                return None
            if access_point_term_projection and access_point_term_ends_at is None:
                logger.error(
                    'access_point_projection_requires_captured_term_end',
                    subscription_id=subscription.id,
                )
                return None
            user = await self._load_user_for_panel_write(db, subscription.user_id)
            if not user:
                logger.warning(
                    'Создание VPN-профиля отклонено: пользователь не найден или закрывается',
                    user_id=subscription.user_id,
                )
                return None

            validation_success = await self.validate_and_clean_subscription(db, subscription, user)
            if not validation_success:
                logger.error('Ошибка валидации подписки для пользователя', _format_user_log=self._format_user_log(user))
                return None

            # Загружаем tariff заранее, чтобы избежать lazy loading в async контексте
            try:
                await db.refresh(subscription, ['tariff'])
            except Exception:
                pass  # tariff может быть None или уже загружен

            user_tag = self._resolve_user_tag(subscription)

            # Определяем внешний сквад из тарифа
            ext_squad_uuid = subscription.tariff.external_squad_uuid if subscription.tariff else None

            async with self.get_api_client() as api:
                hwid_limit = resolve_hwid_device_limit_for_payload(subscription)

                # Multi-tariff mode: each subscription has its own Remnawave user
                if settings.is_multi_tariff_enabled():

                    async def operation() -> RemnaWaveUser:
                        return await self._create_or_update_remnawave_user_multi(
                            api,
                            user,
                            subscription,
                            user_tag=user_tag,
                            hwid_limit=hwid_limit,
                            ext_squad_uuid=ext_squad_uuid,
                            reset_traffic=reset_traffic,
                            reset_reason=reset_reason,
                            panel_expire_at=access_point_term_ends_at,
                        )
                else:

                    async def operation() -> RemnaWaveUser:
                        return await self._create_or_update_remnawave_user_single(
                            api,
                            user,
                            subscription,
                            user_tag=user_tag,
                            hwid_limit=hwid_limit,
                            ext_squad_uuid=ext_squad_uuid,
                            reset_traffic=reset_traffic,
                            reset_reason=reset_reason,
                            panel_expire_at=access_point_term_ends_at,
                        )

                # Re-acquire the closure fence immediately before the actual
                # provider mutation. Validation above may have committed a
                # stale-UUID repair and therefore released an earlier lock.
                updated_user = await self.run_guarded_panel_write(
                    db,
                    user_id=subscription.user_id,
                    api=api,
                    operation=operation,
                )
                if updated_user is None:
                    return None

                subscription.remnawave_short_uuid = updated_user.short_uuid
                subscription.subscription_url = updated_user.subscription_url
                subscription.subscription_crypto_link = updated_user.happ_crypto_link
                subscription.remnawave_uuid = updated_user.uuid
                # Legacy field — keep in sync for single-mode backward compat
                if not settings.is_multi_tariff_enabled():
                    user.remnawave_uuid = updated_user.uuid

                if commit:
                    await db.commit()
                else:
                    await db.flush()

                logger.info('✅ Создан/обновлен RemnaWave пользователь для подписки', subscription_id=subscription.id)
                logger.info('🔗 Ссылка на подписку', subscription_url=updated_user.subscription_url)
                strategy_name = settings.DEFAULT_TRAFFIC_RESET_STRATEGY
                logger.info('📊 Стратегия сброса трафика', strategy_name=strategy_name)
                return updated_user

        except RemnaWaveAPIError as e:
            # Единственный сигнал владельцу о недоехавшей оплате — без номера подписки
            # его нельзя было связать ни с человеком, ни с заказом (хвост этапа 4.0).
            logger.error(
                'Ошибка RemnaWave API',
                error=e,
                subscription_id=subscription.id,
                user_id=subscription.user_id,
                remnawave_uuid=subscription.remnawave_uuid,
            )
            return None
        except Exception as e:
            logger.error(
                'Ошибка создания RemnaWave пользователя',
                error=e,
                subscription_id=subscription.id,
                user_id=subscription.user_id,
            )
            return None

    async def _create_or_update_remnawave_user_multi(
        self,
        api: RemnaWaveAPI,
        user: User,
        subscription: Subscription,
        *,
        user_tag: str | None,
        hwid_limit: int | None,
        ext_squad_uuid: str | None,
        reset_traffic: bool,
        reset_reason: str | None,
        panel_expire_at: datetime | None,
    ) -> RemnaWaveUser:
        """Multi-tariff mode: each subscription gets its own Remnawave user."""
        description = settings.format_remnawave_user_description(
            full_name=user.full_name,
            username=user.username,
            telegram_id=user.telegram_id,
            email=user.email,
            user_id=user.id,
        )
        common_kwargs = dict(
            status=UserStatus.ACTIVE,
            expire_at=panel_expire_at or subscription.end_date,
            traffic_limit_bytes=self._gb_to_bytes(subscription.traffic_limit_gb),
            traffic_limit_strategy=get_traffic_reset_strategy(subscription.tariff),
            telegram_id=user.telegram_id,
            email=user.email,
            description=description,
        )
        if subscription.connected_squads:
            common_kwargs['active_internal_squads'] = subscription.connected_squads
        if user_tag is not None:
            common_kwargs['tag'] = user_tag
        if hwid_limit is not None:
            common_kwargs['hwid_device_limit'] = hwid_limit
        if ext_squad_uuid is not None:
            common_kwargs['external_squad_uuid'] = ext_squad_uuid

        # If this subscription already has a Remnawave user — update it
        if subscription.remnawave_uuid:
            try:
                existing = await api.get_user_by_uuid(subscription.remnawave_uuid)
                if existing:
                    if settings.RESET_DEVICES_ON_RENEWAL:
                        try:
                            await api.reset_user_devices(existing.uuid)
                        except Exception as hwid_error:
                            logger.warning('⚠️ Не удалось сбросить HWID', hwid_error=hwid_error)

                    updated = await api.update_user(uuid=existing.uuid, **common_kwargs)
                    if reset_traffic:
                        await self._reset_user_traffic(api, updated.uuid, user, reset_reason)
                    return updated
            except Exception:
                logger.warning(
                    '⚠️ Не удалось найти Remnawave юзера по UUID подписки, создаём нового',
                    subscription_id=subscription.id,
                    remnawave_uuid=subscription.remnawave_uuid,
                )

        # New subscription — create a NEW Remnawave user.
        # short_id (6 hex chars) приклеивается к base; helper гарантирует, что
        # итоговая длина ≤ REMNAWAVE_USERNAME_MAX_LENGTH (исторический баг с
        # `didykmarin_email_didykmarin_703_49883b` — 38 chars вместо 36).
        #
        # КРИТИЧНО для multi-tariff: суффикс ОБЯЗАН быть уникален per-subscription,
        # иначе два тарифа одного юзера собирают ОДИНАКОВЫЙ username → панель
        # возвращает одного и того же пользователя → общий HWID-лимит (баг «лимит
        # по наименьшему тарифу»). На пустой/legacy short_id ('' из server_default)
        # падаем на детерминированный per-subscription суффикс по id.
        short_suffix = subscription.remnawave_short_id or f'sub{subscription.id}'
        username = settings.build_remnawave_subscription_username(
            full_name=user.full_name,
            username=user.username,
            telegram_id=user.telegram_id,
            email=user.email,
            user_id=user.id,
            suffix=f'_{short_suffix}',
        )

        updated_user = await api.create_user(username=username, **common_kwargs)
        if reset_traffic:
            await self._reset_user_traffic(api, updated_user.uuid, user, reset_reason)
        return updated_user

    async def _create_or_update_remnawave_user_single(
        self,
        api: RemnaWaveAPI,
        user: User,
        subscription: Subscription,
        *,
        user_tag: str | None,
        hwid_limit: int | None,
        ext_squad_uuid: str | None,
        reset_traffic: bool,
        reset_reason: str | None,
        panel_expire_at: datetime | None,
    ) -> RemnaWaveUser:
        """Single-subscription mode (legacy): one Remnawave user per bot user."""
        description = settings.format_remnawave_user_description(
            full_name=user.full_name,
            username=user.username,
            telegram_id=user.telegram_id,
            email=user.email,
            user_id=user.id,
        )

        # Search for existing Remnawave user
        existing_users = []
        if user.remnawave_uuid:
            try:
                existing_user = await api.get_user_by_uuid(user.remnawave_uuid)
                if existing_user:
                    existing_users = [existing_user]
            except Exception:
                pass

        if not existing_users and user.telegram_id:
            existing_users = await api.get_user_by_telegram_id(user.telegram_id)

        if not existing_users and user.email:
            try:
                existing_users = await api.get_user_by_email(user.email)
            except Exception:
                pass

        common_kwargs = dict(
            status=UserStatus.ACTIVE,
            expire_at=panel_expire_at or subscription.end_date,
            traffic_limit_bytes=self._gb_to_bytes(subscription.traffic_limit_gb),
            traffic_limit_strategy=get_traffic_reset_strategy(subscription.tariff),
            telegram_id=user.telegram_id,
            email=user.email,
            description=description,
        )
        if subscription.connected_squads:
            common_kwargs['active_internal_squads'] = subscription.connected_squads
        if user_tag is not None:
            common_kwargs['tag'] = user_tag
        if hwid_limit is not None:
            common_kwargs['hwid_device_limit'] = hwid_limit
        if ext_squad_uuid is not None:
            common_kwargs['external_squad_uuid'] = ext_squad_uuid

        if existing_users:
            logger.info('🔄 Найден существующий пользователь в панели', _format_user_log=self._format_user_log(user))
            remnawave_user = existing_users[0]

            if settings.RESET_DEVICES_ON_RENEWAL:
                try:
                    await api.reset_user_devices(remnawave_user.uuid)
                    logger.info('🔧 Сброшены HWID устройства', _format_user_log=self._format_user_log(user))
                except Exception as hwid_error:
                    logger.warning('⚠️ Не удалось сбросить HWID', hwid_error=hwid_error)

            updated_user = await api.update_user(uuid=remnawave_user.uuid, **common_kwargs)
            if reset_traffic:
                await self._reset_user_traffic(api, updated_user.uuid, user, reset_reason)
            return updated_user

        logger.info('🆕 Создаем нового пользователя в панели', _format_user_log=self._format_user_log(user))
        username = settings.format_remnawave_username(
            full_name=user.full_name,
            username=user.username,
            telegram_id=user.telegram_id,
            email=user.email,
            user_id=user.id,
        )
        updated_user = await api.create_user(username=username, **common_kwargs)
        if reset_traffic:
            await self._reset_user_traffic(api, updated_user.uuid, user, reset_reason)
        return updated_user

    async def update_remnawave_user(
        self,
        db: AsyncSession,
        subscription: Subscription,
        *,
        reset_traffic: bool = False,
        reset_reason: str | None = None,
        sync_squads: bool = True,
        verify_panel_echo: bool = False,
        commit: bool = True,
        access_point_term_projection: bool = False,
        access_point_term_ends_at: datetime | None = None,
    ) -> RemnaWaveUser | None:
        try:
            if await self._is_access_point_subscription(db, subscription) and not access_point_term_projection:
                logger.warning(
                    'raw_panel_update_blocked_for_access_point_term',
                    subscription_id=subscription.id,
                )
                return None
            if access_point_term_projection and access_point_term_ends_at is None:
                logger.error(
                    'access_point_projection_requires_captured_term_end',
                    subscription_id=subscription.id,
                )
                return None
            user = await self._load_user_for_panel_write(db, subscription.user_id)
            if not user:
                logger.warning(
                    'Обновление VPN-профиля отклонено: пользователь не найден или закрывается',
                    user_id=subscription.user_id,
                )
                return None

            # Resolve the Remnawave UUID: prefer subscription-level in multi-tariff mode
            if settings.is_multi_tariff_enabled():
                remnawave_uuid = subscription.remnawave_uuid
                if not remnawave_uuid:
                    logger.warning(
                        'Multi-tariff: subscription has no remnawave_uuid, cannot update panel',
                        subscription_id=subscription.id,
                        user_id=subscription.user_id,
                    )
                    return None
            else:
                remnawave_uuid = user.remnawave_uuid
            if not remnawave_uuid:
                logger.error('RemnaWave UUID не найден для пользователя', user_id=subscription.user_id)
                return None

            # Загружаем tariff заранее, чтобы избежать lazy loading в async контексте
            try:
                await db.refresh(subscription, ['tariff'])
            except Exception:
                pass  # tariff может быть None или уже загружен

            current_time = datetime.now(UTC)
            # Определяем актуальный статус и дату для панели. Grace-aware: пока идёт
            # «бонус 2 дня» (in_grace), держим пользователя ACTIVE с expireAt=grace_until,
            # хотя в БД статус уже EXPIRED — иначе действие юзера в кабинете в эти 2 дня
            # (докупка/переименование устройства и т.п.) отрубило бы живой VPN.
            # НЕ меняем статус подписки здесь — это задача scheduled job.
            is_actually_active, panel_expire_at = resolve_panel_active_and_expiry(subscription, current_time)
            if access_point_term_projection:
                # A paid AP term is immutable financial evidence.  In
                # particular, an early renewal may already have advanced the
                # mutable subscription end date; never let that future end
                # leak into a current-term re-projection.
                is_actually_active = True
                panel_expire_at = access_point_term_ends_at

            # Логируем если статус и end_date не согласованы (для отладки), но НЕ для grace
            # (там рассинхрон ожидаемый и корректный).
            if (
                subscription.status in (SubscriptionStatus.ACTIVE.value, SubscriptionStatus.TRIAL.value)
                and subscription.end_date <= current_time
                and not is_in_grace(subscription, current_time)
            ):
                logger.warning(
                    '⚠️ update_remnawave_user: подписка имеет статус ACTIVE, но end_date <= now. Отправляем в RemnaWave как DISABLED, но НЕ меняем статус в БД.',
                    subscription_id=subscription.id,
                    end_date=subscription.end_date,
                    current_time=current_time,
                )

            user_tag = self._resolve_user_tag(subscription)

            # Определяем внешний сквад из тарифа
            ext_squad_uuid = subscription.tariff.external_squad_uuid if subscription.tariff else None

            async with self.get_api_client() as api:
                hwid_limit = resolve_hwid_device_limit_for_payload(subscription)

                update_kwargs = dict(
                    uuid=remnawave_uuid,
                    status=UserStatus.ACTIVE if is_actually_active else UserStatus.DISABLED,
                    expire_at=panel_expire_at,
                    traffic_limit_bytes=self._gb_to_bytes(subscription.traffic_limit_gb),
                    traffic_limit_strategy=get_traffic_reset_strategy(subscription.tariff),
                    telegram_id=user.telegram_id,
                    email=user.email,
                    description=settings.format_remnawave_user_description(
                        full_name=user.full_name,
                        username=user.username,
                        telegram_id=user.telegram_id,
                        email=user.email,
                        user_id=user.id,
                    ),
                )

                # Сквады отправляем только при явном sync_squads=True (propagate_squads и пр.)
                # В рутинных обновлениях пропускаем — сквады уже назначены при создании подписки,
                # а пересылка стейловых UUID вызывает FK violation → A039 в RemnaWave
                if sync_squads and subscription.connected_squads:
                    update_kwargs['active_internal_squads'] = subscription.connected_squads

                if user_tag is not None:
                    update_kwargs['tag'] = user_tag

                if hwid_limit is not None:
                    update_kwargs['hwid_device_limit'] = hwid_limit

                # Внешний сквад НЕ пересылаем в рутинных обновлениях — он уже назначен
                # при создании подписки. Стейловый UUID вызывает FK violation → A039.
                # Синхронизация сквадов происходит только при sync_squads=True.
                if sync_squads and ext_squad_uuid is not None:
                    update_kwargs['external_squad_uuid'] = ext_squad_uuid

                updated_user = await api.update_user(**update_kwargs)

                # Сверка отправленного с ответом панели. 🔴 НАМЕРЕННО только запись в лог:
                # что PATCH-ответ вообще содержит activeInternalSquads, доказательств нет
                # (при отсутствии ключа _parse_user подставит [], remnawave_api.py:1791),
                # а ложное несовпадение подвесило бы КАЖДЫЙ оплаченный заказ в вечном
                # повторе — ровно та авария, от которой предостерегал аудит этапа 1.
                # Само подтверждение записи даёт уже сам форс: api.update_user либо вернул
                # объект, либо бросил исключение. Превращать сверку в запрет — только после
                # того, как логи с боевого покажут, что ложных срабатываний нет.
                if verify_panel_echo:
                    # try/except обязателен: PATCH уже прошёл, а ссылка ещё не записана
                    # (:813 ниже). Исключение отсюда потеряло бы свежий subscription_url и
                    # ушло бы в общий except как отказ синхронизации — то есть диагностика
                    # сама стала бы причиной вечного повтора. Формы данных не гарантированы:
                    # connected_squads — свободная JSON-колонка (models.py:2471).
                    try:
                        mismatched_field = find_panel_echo_mismatch(update_kwargs, updated_user)
                    except Exception as echo_error:
                        mismatched_field = None
                        logger.warning('Сверку ответа панели выполнить не удалось', echo_error=echo_error)
                    if mismatched_field is not None:
                        # warning, а не error: error уходит в чат владельцу (logging_handler.py),
                        # а поле activeInternalSquads в PATCH-ответе непроверено — ложные
                        # срабатывания забили бы дедуп и заглушили настоящие сигналы.
                        logger.warning(
                            'Панель не подтвердила отправленное поле',
                            subscription_id=subscription.id,
                            remnawave_uuid=remnawave_uuid,
                            mismatched_field=mismatched_field,
                        )

                if reset_traffic:
                    if settings.is_multi_tariff_enabled():
                        reset_uuid = subscription.remnawave_uuid
                        if not reset_uuid:
                            logger.warning(
                                'Multi-tariff: subscription has no remnawave_uuid, skipping traffic reset',
                                subscription_id=subscription.id,
                                user_id=subscription.user_id,
                            )
                    else:
                        reset_uuid = user.remnawave_uuid
                    if reset_uuid:
                        await self._reset_user_traffic(
                            api,
                            reset_uuid,
                            user,
                            reset_reason,
                        )

                subscription.subscription_url = updated_user.subscription_url
                subscription.subscription_crypto_link = updated_user.happ_crypto_link
                if commit:
                    await db.commit()
                else:
                    await db.flush()

                status_text = 'активным' if is_actually_active else 'истёкшим'
                logger.info(
                    '✅ Обновлен RemnaWave пользователь со статусом',
                    remnawave_uuid=remnawave_uuid,
                    status_text=status_text,
                )
                strategy_name = settings.DEFAULT_TRAFFIC_RESET_STRATEGY
                logger.info('📊 Стратегия сброса трафика', strategy_name=strategy_name)
                return updated_user

        except RemnaWaveAPIError as e:
            logger.error(
                'Ошибка RemnaWave API',
                error=e,
                subscription_id=subscription.id,
                user_id=subscription.user_id,
                remnawave_uuid=subscription.remnawave_uuid,
            )
            return None
        except Exception as e:
            logger.error(
                'Ошибка обновления RemnaWave пользователя',
                error=e,
                subscription_id=subscription.id,
                user_id=subscription.user_id,
            )
            return None

    async def push_panel_state(
        self, db: AsyncSession, subscription: Subscription, *, active: bool, expire_at: datetime
    ) -> bool:
        """Толкает в RemnaWave ТОЛЬКО статус и expireAt одной подписки (без записи в БД).

        Используется grace-флоу: держать VPN живым (active=True, expire_at=grace_until)
        при входе в «бонус 2 дня» и гасить (active=False) когда бонус закончился.
        Возвращает True при успехе, False если панель не настроена / нет UUID / ошибка API.
        """
        try:
            if await self._is_access_point_subscription(db, subscription):
                logger.warning(
                    'raw_panel_state_push_blocked_for_access_point_term',
                    subscription_id=subscription.id,
                )
                return False
            user = await self._load_user_for_panel_write(db, subscription.user_id)
            if not user:
                return False
            if settings.is_multi_tariff_enabled():
                remnawave_uuid = subscription.remnawave_uuid
            else:
                remnawave_uuid = user.remnawave_uuid
            if not remnawave_uuid:
                logger.warning(
                    'push_panel_state: нет remnawave_uuid',
                    subscription_id=subscription.id,
                    user_id=subscription.user_id,
                )
                return False

            async with self.get_api_client() as api:
                await api.update_user(
                    uuid=remnawave_uuid,
                    status=UserStatus.ACTIVE if active else UserStatus.DISABLED,
                    expire_at=expire_at,
                )
            logger.info(
                '🎁 RemnaWave expireAt сдвинут (grace)',
                subscription_id=subscription.id,
                active=active,
                expire_at=expire_at,
            )
            return True
        except Exception as e:
            logger.error('push_panel_state: ошибка обновления панели', subscription_id=subscription.id, error=e)
            return False

    @staticmethod
    def _format_user_log(user) -> str:
        """Форматирует идентификатор пользователя для логов."""
        if user.telegram_id:
            return f'user {user.telegram_id}'
        if user.email:
            return f'user {user.id} ({user.email})'
        return f'user {user.id}'

    async def _reset_user_traffic(
        self,
        api: RemnaWaveAPI,
        user_uuid: str,
        user,  # User object вместо telegram_id
        reset_reason: str | None = None,
    ) -> None:
        if not user_uuid:
            return

        try:
            await api.reset_user_traffic(user_uuid)
            reason_text = f' ({reset_reason})' if reset_reason else ''
            logger.info(
                '🔄 Сброшен трафик RemnaWave', _format_user_log=self._format_user_log(user), reason_text=reason_text
            )
        except Exception as exc:
            logger.warning(
                '⚠️ Не удалось сбросить трафик RemnaWave', _format_user_log=self._format_user_log(user), error=exc
            )

    async def disable_remnawave_user(self, user_uuid: str) -> bool:
        try:
            async with self.get_api_client() as api:
                await api.disable_user(user_uuid)
                logger.info('✅ Отключен RemnaWave пользователь', user_uuid=user_uuid)
                return True

        except Exception as e:
            error_msg = str(e).lower()
            # "User already disabled" - считаем успехом
            if 'already disabled' in error_msg:
                logger.info('✅ RemnaWave пользователь уже отключен', user_uuid=user_uuid)
                return True
            logger.error('Ошибка отключения RemnaWave пользователя', error=e)
            return False

    async def delete_remnawave_user(self, user_uuid: str) -> bool:
        """Полное удаление пользователя из панели RemnaWave (хуки прекращаются)."""
        try:
            async with self.get_api_client() as api:
                await api.delete_user(user_uuid)
                logger.info('🗑 Удалён RemnaWave пользователь', user_uuid=user_uuid)
                return True

        except Exception as e:
            error_msg = str(e).lower()
            if 'not found' in error_msg or 'not exist' in error_msg:
                logger.info('🗑 RemnaWave пользователь уже удалён', user_uuid=user_uuid)
                return True
            logger.error('Ошибка удаления RemnaWave пользователя', error=e, user_uuid=user_uuid)
            return False

    async def enable_remnawave_user(
        self,
        user_uuid: str,
        *,
        db: AsyncSession | None = None,
        access_point_term_projection: bool = False,
    ) -> bool:
        """Включить пользователя в RemnaWave (реактивация)."""
        if db is None:
            # There is no safe way to decide whether this UUID belongs to a
            # financial tombstone without a database session.  Fail closed;
            # all supported callers pass their request/job session.
            logger.error('enable_remnawave_user_requires_db_for_financial_closure_fence', user_uuid=user_uuid)
            return False
        if await self._panel_uuid_is_financially_closing(db, user_uuid):
            logger.warning('panel_enable_blocked_for_financial_account_closure', user_uuid=user_uuid)
            return False
        subscriptions = list(
            (
                await db.execute(
                    select(Subscription)
                    .outerjoin(User, Subscription.user_id == User.id)
                    .where(or_(Subscription.remnawave_uuid == user_uuid, User.remnawave_uuid == user_uuid))
                )
            )
            .scalars()
            .all()
        )
        if not access_point_term_projection:
            for subscription in subscriptions:
                if await self._is_access_point_subscription(db, subscription):
                    logger.warning('raw_panel_enable_blocked_for_access_point_term', user_uuid=user_uuid)
                    return False
        try:
            async with self.get_api_client() as api:
                await api.enable_user(user_uuid)
                logger.info('✅ Включен RemnaWave пользователь', user_uuid=user_uuid)
                return True

        except Exception as e:
            error_msg = str(e).lower()
            # "User already enabled" - считаем успехом
            if 'already enabled' in error_msg:
                logger.info('✅ RemnaWave пользователь уже включен', user_uuid=user_uuid)
                return True
            logger.error('Ошибка включения RemnaWave пользователя', error=e)
            return False

    async def get_remnawave_squads(self) -> list[dict] | None:
        """Получить список internal squads из RemnaWave."""
        try:
            async with self.get_api_client() as api:
                squads = await api.get_internal_squads()
                # Преобразуем в формат для sync_with_remnawave
                result = []
                for squad in squads:
                    result.append(
                        {
                            'uuid': squad.uuid,
                            'name': squad.name,
                        }
                    )
                logger.info('✅ Получено серверов из RemnaWave', result_count=len(result))
                return result

        except Exception as e:
            logger.error('Ошибка получения серверов из RemnaWave', error=e)
            return None

    async def revoke_subscription(self, db: AsyncSession, subscription: Subscription) -> str | None:
        try:
            user = await get_user_by_id(db, subscription.user_id)
            if not user:
                return None
            if settings.is_multi_tariff_enabled():
                revoke_uuid = subscription.remnawave_uuid
                if not revoke_uuid:
                    logger.warning(
                        'Multi-tariff: subscription has no remnawave_uuid, cannot revoke',
                        subscription_id=subscription.id,
                        user_id=subscription.user_id,
                    )
                    return None
            else:
                revoke_uuid = user.remnawave_uuid
            if not revoke_uuid:
                return None

            async with self.get_api_client() as api:
                updated_user = await api.revoke_user_subscription(revoke_uuid)

                subscription.remnawave_short_uuid = updated_user.short_uuid
                subscription.subscription_url = updated_user.subscription_url
                subscription.subscription_crypto_link = updated_user.happ_crypto_link
                await db.commit()

                logger.info('✅ Обновлена ссылка подписки', _format_user_log=self._format_user_log(user))
                return updated_user.subscription_url

        except Exception as e:
            logger.error('Ошибка обновления ссылки подписки', error=e)
            return None

    async def get_subscription_info(self, short_uuid: str) -> dict | None:
        try:
            async with self.get_api_client() as api:
                info = await api.get_subscription_info(short_uuid)
                return info

        except Exception as e:
            logger.error('Ошибка получения информации о подписке', error=e)
            return None

    async def sync_subscription_usage(self, db: AsyncSession, subscription: Subscription) -> bool:
        try:
            user = await get_user_by_id(db, subscription.user_id)
            if not user:
                return False
            if settings.is_multi_tariff_enabled():
                sync_uuid = subscription.remnawave_uuid
                if not sync_uuid:
                    logger.warning(
                        'Multi-tariff: subscription has no remnawave_uuid, cannot sync usage',
                        subscription_id=subscription.id,
                        user_id=subscription.user_id,
                    )
                    return False
            else:
                sync_uuid = user.remnawave_uuid
            if not sync_uuid:
                return False

            async with self.get_api_client() as api:
                remnawave_user = await api.get_user_by_uuid(sync_uuid)
                if not remnawave_user:
                    return False

                used_gb = self._bytes_to_gb(remnawave_user.used_traffic_bytes)
                subscription.traffic_used_gb = used_gb

                await db.commit()

                logger.debug('Синхронизирован трафик для подписки ГБ', subscription_id=subscription.id, used_gb=used_gb)
                return True

        except Exception as e:
            logger.error('Ошибка синхронизации трафика', error=e)
            return False

    async def ensure_subscription_synced(
        self,
        db: AsyncSession,
        subscription: Subscription,
        *,
        force_panel_sync: bool = False,
        commit: bool = True,
        access_point_term_projection: bool = False,
        access_point_term_ends_at: datetime | None = None,
    ) -> tuple[bool, str | None]:
        """
        Проверяет и синхронизирует подписку с RemnaWave при необходимости.

        Если subscription_url отсутствует или данные не синхронизированы,
        пытается обновить/создать пользователя в RemnaWave.

        Returns:
            Tuple[bool, Optional[str]]: (успех, сообщение об ошибке)
        """
        try:
            if access_point_term_projection and access_point_term_ends_at is None:
                return False, 'access_point_projection_requires_captured_term_end'
            user = await get_user_by_id(db, subscription.user_id)
            if not user:
                logger.error('Пользователь не найден для подписки', subscription_id=subscription.id)
                return False, 'user_not_found'

            # Подписке нечего выдавать: это отдельный диагноз, а не повод для бесконечного
            # повтора. Форсируют только пути выдачи после оплаты — им нужен оператор.
            if force_panel_sync and not subscription.connected_squads:
                logger.error(
                    'Синхронизация невозможна: у подписки пустой список серверов',
                    subscription_id=subscription.id,
                )
                return False, 'no_entitlements_to_provision'

            # Проверяем, нужна ли синхронизация
            sub_uuid = subscription.remnawave_uuid if settings.is_multi_tariff_enabled() else user.remnawave_uuid
            needs_sync = force_panel_sync or not subscription.subscription_url or not sub_uuid

            if not needs_sync:
                # Проверяем, существует ли пользователь в RemnaWave
                try:
                    async with self.get_api_client() as api:
                        remnawave_user = await api.get_user_by_uuid(sub_uuid)
                        if not remnawave_user:
                            needs_sync = True
                            logger.warning(
                                'Пользователь не найден в RemnaWave, требуется синхронизация',
                                remnawave_uuid=sub_uuid,
                            )
                except Exception as check_error:
                    logger.warning('Не удалось проверить пользователя в RemnaWave', check_error=check_error)
                    # Продолжаем, возможно проблема временная

            if not needs_sync:
                return True, None

            logger.info(
                'Синхронизация подписки с RemnaWave',
                subscription_id=subscription.id,
                subscription_url=bool(subscription.subscription_url),
                remnawave_uuid=bool(sub_uuid),
            )

            # Пытаемся синхронизировать
            result = None
            created_new_profile = False
            if sub_uuid:
                # Пробуем обновить существующего пользователя
                result = await self.update_remnawave_user(
                    db,
                    subscription,
                    reset_traffic=False,
                    sync_squads=True,
                    # Проекция оплаченного срока проверяется своим механизмом выше по стеку;
                    # здесь сверяем эхо только на обычных путях выдачи после оплаты.
                    verify_panel_echo=force_panel_sync and not access_point_term_projection,
                    commit=commit,
                    access_point_term_projection=access_point_term_projection,
                    access_point_term_ends_at=access_point_term_ends_at,
                )
                # Пересоздавать пользователя можно ТОЛЬКО когда панель прямо ответила «такого
                # нет» (404). update_remnawave_user отдаёт None и на таймауте, и на любой
                # ошибке API: обнулив UUID в этом случае, мы осиротили бы живой профиль в
                # панели и убили бы уже розданный клиенту конфиг.
                if not result:
                    try:
                        async with self.get_api_client() as api:
                            panel_user = await api.get_user_by_uuid(sub_uuid)
                    except Exception as probe_error:
                        logger.warning(
                            'Панель недоступна после неудачного обновления — UUID не трогаем',
                            subscription_id=subscription.id,
                            remnawave_uuid=sub_uuid,
                            probe_error=probe_error,
                        )
                        return False, 'panel_unavailable'
                    if panel_user is not None:
                        logger.warning(
                            'Пользователь в панели есть, но обновление не прошло — отдаём в повтор',
                            subscription_id=subscription.id,
                            remnawave_uuid=sub_uuid,
                        )
                        return False, 'panel_update_failed'
                    logger.warning(
                        'Панель подтвердила отсутствие пользователя, создаём заново',
                        remnawave_uuid=sub_uuid,
                    )
                    # Сбрасываем старый UUID, create_remnawave_user установит новый
                    if settings.is_multi_tariff_enabled():
                        subscription.remnawave_uuid = None
                    else:
                        user.remnawave_uuid = None
                    result = await self.create_remnawave_user(
                        db,
                        subscription,
                        reset_traffic=False,
                        commit=commit,
                        access_point_term_projection=access_point_term_projection,
                        access_point_term_ends_at=access_point_term_ends_at,
                    )
                    created_new_profile = result is not None
            else:
                # Создаём нового пользователя
                result = await self.create_remnawave_user(
                    db,
                    subscription,
                    reset_traffic=False,
                    commit=commit,
                    access_point_term_projection=access_point_term_projection,
                    access_point_term_ends_at=access_point_term_ends_at,
                )
                created_new_profile = result is not None

            # Добивочный PATCH после создания профиля. Честно о причине: api.create_user
            # сквады отправляет (remnawave_api.py:609-610), так что это подстраховка, а не
            # лечение доказанного дефекта панели — та же подстраховка стоит в трёх местах
            # проекта (purchase.py:3412, miniapp.py:7914, daily_subscription_service.py:282).
            # Второй, не менее важный смысл: путь создания профиля иначе НЕ проверяется
            # сверкой вовсе — create_remnawave_user зовёт панель мимо update_remnawave_user.
            if created_new_profile and force_panel_sync and subscription.connected_squads:
                patched = await self.update_remnawave_user(
                    db,
                    subscription,
                    reset_traffic=False,
                    sync_squads=True,
                    verify_panel_echo=not access_point_term_projection,
                    commit=commit,
                    access_point_term_projection=access_point_term_projection,
                    access_point_term_ends_at=access_point_term_ends_at,
                )
                if not patched:
                    logger.warning(
                        'Профиль создан, но серверы дослать не удалось — отдаём в повтор',
                        subscription_id=subscription.id,
                    )
                    return False, 'panel_squads_not_applied'

            if result:
                await db.refresh(subscription)
                await db.refresh(user)
                logger.info(
                    'Подписка успешно синхронизирована с RemnaWave. URL',
                    subscription_id=subscription.id,
                    subscription_url=subscription.subscription_url,
                )
                return True, None
            logger.error('Не удалось синхронизировать подписку с RemnaWave', subscription_id=subscription.id)
            return False, 'sync_failed'

        except RemnaWaveAPIError as api_error:
            logger.error(
                'Ошибка RemnaWave API при синхронизации подписки', subscription_id=subscription.id, api_error=api_error
            )
            return False, 'api_error'
        except Exception as e:
            logger.error('Ошибка синхронизации подписки', subscription_id=subscription.id, error=e)
            return False, 'unknown_error'

    async def validate_and_clean_subscription(self, db: AsyncSession, subscription: Subscription, user: User) -> bool:
        try:
            needs_cleanup = False
            user_log = self._format_user_log(user)

            # In multi-tariff mode, validate per-subscription UUID, not user-level UUID
            check_uuid = subscription.remnawave_uuid if settings.is_multi_tariff_enabled() else user.remnawave_uuid

            if check_uuid:
                try:
                    async with self.get_api_client() as api:
                        remnawave_user = await api.get_user_by_uuid(check_uuid)

                        if not remnawave_user:
                            logger.warning(
                                '⚠️ UUID не найден в панели',
                                user_log=user_log,
                                remnawave_uuid=check_uuid,
                            )
                            needs_cleanup = True
                        elif (
                            user.telegram_id
                            and remnawave_user.telegram_id
                            and remnawave_user.telegram_id != user.telegram_id
                        ):
                            logger.warning(
                                '⚠️ Несоответствие telegram_id для panel',
                                user_log=user_log,
                                telegram_id=remnawave_user.telegram_id,
                            )
                            needs_cleanup = True
                except Exception as api_error:
                    logger.error('❌ Ошибка проверки пользователя в панели', api_error=api_error)
                    needs_cleanup = True

            if subscription.remnawave_short_uuid and not check_uuid:
                logger.warning('⚠️ У подписки есть short_uuid, но нет remnawave_uuid')
                needs_cleanup = True

            if needs_cleanup:
                logger.info('🧹 Очищаем мусорные данные подписки', user_log=user_log)

                subscription.remnawave_short_uuid = None
                subscription.remnawave_uuid = None
                subscription.subscription_url = ''
                subscription.subscription_crypto_link = ''

                if not settings.is_multi_tariff_enabled():
                    user.remnawave_uuid = None

                await db.commit()
                logger.info('✅ Мусорные данные очищены', user_log=user_log)

            return True

        except Exception as e:
            logger.error('❌ Ошибка валидации подписки', _format_user_log=self._format_user_log(user), error=e)
            await db.rollback()
            return False

    async def get_countries_price_by_uuids(
        self,
        country_uuids: list[str],
        db: AsyncSession,
        *,
        promo_group_id: int | None = None,
    ) -> tuple[int, list[int]]:
        try:
            from app.database.crud.server_squad import get_server_squad_by_uuid

            total_price = 0
            prices_list = []

            for country_uuid in country_uuids:
                server = await get_server_squad_by_uuid(db, country_uuid)
                is_allowed = True
                if promo_group_id is not None and server:
                    allowed_ids = {pg.id for pg in server.allowed_promo_groups}
                    is_allowed = promo_group_id in allowed_ids

                if server and server.is_available and not server.is_full and is_allowed:
                    price = server.price_kopeks
                    total_price += price
                    prices_list.append(price)
                    logger.debug('🏷️ Страна ₽', display_name=server.display_name, price=price / 100)
                else:
                    default_price = 0
                    total_price += default_price
                    prices_list.append(default_price)
                    logger.warning(
                        '⚠️ Сервер недоступен, используем базовую цену: ₽',
                        country_uuid=country_uuid,
                        default_price=default_price / 100,
                    )

            logger.info('💰 Общая стоимость стран: ₽', total_price=total_price / 100)
            return total_price, prices_list

        except Exception as e:
            logger.error('Ошибка получения цен стран', error=e)
            default_prices = [0] * len(country_uuids)
            return sum(default_prices), default_prices

    def _gb_to_bytes(self, gb: int | None) -> int:
        if not gb:  # None or 0
            return 0
        return gb * 1024 * 1024 * 1024

    def _bytes_to_gb(self, bytes_value: int) -> float:
        if bytes_value == 0:
            return 0.0
        return bytes_value / (1024 * 1024 * 1024)

    async def propagate_tariff_squads(
        self, db: AsyncSession, tariff_id: int, new_squads: list[str], *, concurrency: int = 5
    ) -> PropagateSquadsResult:
        """Применяет изменение серверов тарифа к активным подпискам и синхронизирует с RemnaWave.

        Если new_squads пустой — означает "все серверы", будут подставлены все доступные.
        Синхронизация с RemnaWave выполняется параллельно с ограничением concurrency.
        Паттерн: предзагрузка данных → параллельные API-вызовы → один commit.
        """
        squads_to_set = list(new_squads)
        if not squads_to_set:
            all_servers, _ = await get_all_server_squads(db, available_only=True, limit=10000)
            squads_to_set = [s.squad_uuid for s in all_servers if s.squad_uuid]
        if not squads_to_set:
            raise ValueError('tariff propagation requires at least one available Internal Squad')

        result = await db.execute(
            select(Subscription).where(
                Subscription.tariff_id == tariff_id,
                Subscription.status.in_([SubscriptionStatus.ACTIVE.value, SubscriptionStatus.TRIAL.value]),
            )
        )
        subscriptions = result.scalars().all()

        if not subscriptions:
            return PropagateSquadsResult(total=0, synced=0)

        previous_squads = {sub.id: list(sub.connected_squads or []) for sub in subscriptions}
        for sub in subscriptions:
            sub.connected_squads = squads_to_set

        # Предзагружаем пользователей и тарифы — никаких DB-операций внутри gather
        user_ids = [sub.user_id for sub in subscriptions]
        users_result = await db.execute(select(User).where(User.id.in_(user_ids)))
        users_map = {u.id: u for u in users_result.scalars().all()}

        for sub in subscriptions:
            try:
                await db.refresh(sub, ['tariff'])
            except Exception as exc:
                logger.warning('Не удалось предзагрузить тариф подписки', subscription_id=sub.id, error=exc)

        # Вычисляем стратегию сброса трафика один раз — все подписки одного тарифа
        sample_tariff = subscriptions[0].tariff or None
        traffic_strategy = get_traffic_reset_strategy(sample_tariff)

        # Параллельная синхронизация: один API-клиент, только HTTP-вызовы внутри gather
        failed_ids: list[int] = []
        synced = 0

        async with self.get_api_client() as api:
            semaphore = asyncio.Semaphore(concurrency)

            async def _sync_one(sub: Subscription) -> bool:
                async with semaphore:
                    try:
                        user = users_map.get(sub.user_id)
                        if not user:
                            return False
                        if settings.is_multi_tariff_enabled():
                            remnawave_uuid = sub.remnawave_uuid
                            if not remnawave_uuid:
                                logger.warning(
                                    'Multi-tariff: subscription has no remnawave_uuid, skipping squad sync',
                                    subscription_id=sub.id,
                                    user_id=sub.user_id,
                                )
                                return False
                        else:
                            remnawave_uuid = user.remnawave_uuid
                        if not remnawave_uuid:
                            return False

                        current_time = datetime.now(UTC)
                        # Grace-aware (см. update_remnawave_user): в «бонусные 2 дня»
                        # держим ACTIVE с expireAt=grace_until, чтобы squad-sync не отрубил VPN.
                        is_actually_active, panel_expire_at = resolve_panel_active_and_expiry(sub, current_time)

                        user_tag = self._resolve_user_tag(sub)
                        ext_squad_uuid = sub.tariff.external_squad_uuid if sub.tariff else None
                        hwid_limit = resolve_hwid_device_limit_for_payload(sub)

                        update_kwargs = dict(
                            uuid=remnawave_uuid,
                            status=UserStatus.ACTIVE if is_actually_active else UserStatus.DISABLED,
                            expire_at=panel_expire_at,
                            traffic_limit_bytes=self._gb_to_bytes(sub.traffic_limit_gb),
                            traffic_limit_strategy=traffic_strategy,
                            telegram_id=user.telegram_id,
                            email=user.email,
                            description=settings.format_remnawave_user_description(
                                full_name=user.full_name,
                                username=user.username,
                                telegram_id=user.telegram_id,
                                email=user.email,
                                user_id=user.id,
                            ),
                        )

                        if sub.connected_squads:
                            update_kwargs['active_internal_squads'] = sub.connected_squads

                        if user_tag is not None:
                            update_kwargs['tag'] = user_tag

                        if hwid_limit is not None:
                            update_kwargs['hwid_device_limit'] = hwid_limit

                        # Не отправляем null — RemnaWave API не принимает null для externalSquadUuid (A039)
                        if ext_squad_uuid is not None:
                            update_kwargs['external_squad_uuid'] = ext_squad_uuid

                        updated_user = await api.update_user(**update_kwargs)

                        # Сохраняем в памяти — commit будет после gather
                        sub.subscription_url = updated_user.subscription_url
                        sub.subscription_crypto_link = updated_user.happ_crypto_link
                        return True

                    except Exception as e:
                        logger.warning(
                            'Не удалось обновить сквады в RemnaWave',
                            subscription_id=sub.id,
                            user_id=sub.user_id,
                            error=e,
                        )
                        return False

            results = await asyncio.gather(*[_sync_one(sub) for sub in subscriptions])

        for i, success in enumerate(results):
            if success:
                synced += 1
            else:
                failed_subscription = subscriptions[i]
                failed_ids.append(failed_subscription.id)
                # Do not claim locally that a Panel change which failed was
                # applied.  A later explicit retry reads this exact preimage.
                failed_subscription.connected_squads = previous_squads[failed_subscription.id]

        # Один commit после всех API-вызовов
        try:
            await db.commit()
        except Exception as commit_error:
            logger.error('Ошибка фиксации транзакции при синхронизации скводов', error=commit_error)
            await db.rollback()
            failed_ids = [sub.id for sub in subscriptions]
            synced = 0

        propagate_result = PropagateSquadsResult(total=len(subscriptions), synced=synced, failed_ids=failed_ids)

        if failed_ids:
            logger.warning(
                'Частичная синхронизация скводов с RemnaWave',
                tariff_id=tariff_id,
                total=propagate_result.total,
                synced=synced,
                failed_ids=failed_ids,
            )
        else:
            logger.info(
                'Обновлены сквады подписок для тарифа',
                tariff_id=tariff_id,
                total=propagate_result.total,
                synced=synced,
            )

        return propagate_result


async def reset_subscription_with_panel(db, user: User, subscription: Subscription) -> dict:
    """Обнулить подписку «как будто не оформляли» и снять доступ в панели RemnaWave,
    НЕ удаляя пользователя из БД (тикеты и аккаунт остаются).

    Панельного пользователя ОТКЛЮЧАЕМ (disable), а не удаляем — обратимо. Дальше юзер
    может купить тариф с нуля. Возвращает ``{'panel_disabled': bool, 'panel_uuid': str|None}``.
    """
    from app.database.crud.subscription import reset_subscription

    # В мультитарифном режиме у каждой подписки свой панельный UUID — НЕ откатываемся
    # на user.remnawave_uuid (это легаси single-tariff UUID, иначе можно отключить
    # не того панельного пользователя). В single-tariff fallback на user корректен.
    if settings.is_multi_tariff_enabled():
        panel_uuid = getattr(subscription, 'remnawave_uuid', None)
    else:
        panel_uuid = getattr(subscription, 'remnawave_uuid', None) or getattr(user, 'remnawave_uuid', None)

    panel_disabled = False
    if panel_uuid:
        try:
            panel_disabled = await SubscriptionService().disable_remnawave_user(panel_uuid)
        except Exception as e:
            logger.warning('Не удалось отключить пользователя в RemnaWave при обнулении подписки', error=e)
    else:
        logger.warning(
            'Обнуление подписки: панельный UUID не найден, отключение в панели пропущено',
            subscription_id=getattr(subscription, 'id', None),
        )

    await reset_subscription(db, subscription)
    return {'panel_disabled': panel_disabled, 'panel_uuid': panel_uuid}
