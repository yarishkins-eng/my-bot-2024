"""Short recovery loop for Device-First v2 and paid order-linked balance topups.

The general monitoring cycle intentionally remains coarse.  This worker has a
small bounded batch for ``direct_purchase_v2`` plus a separate bounded sweep
of credited order-linked topups; generic balance recovery stays on its
established paths.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from html import escape
from time import monotonic
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy import select

from app.config import settings
from app.database.database import AsyncSessionLocal
from app.database.models import PlategaPayment, Transaction
from app.services.device_first_checkout_service import (
    process_device_first_notification_outbox,
    process_direct_provisioning_outbox,
)
from app.services.device_first_deposit_outbox_service import process_device_first_deposit_outbox
from app.services.device_first_payment_service import reconcile_device_first_payments


logger = structlog.get_logger(__name__)

# Сколько секунд добор онбординга имеет право отнять у денежного воркера за один проход.
# Вынесено в константу, чтобы сторож проверял потолок за доли секунды, а не за десять.
ONBOARDING_FOLLOWUP_TIMEOUT_SECONDS = 10


class DeviceFirstRecoveryService:
    """Recover direct receipts, provisioning and notifications without global polling."""

    def __init__(self) -> None:
        self._running = False
        self._topup_sweep_at = float('-inf')
        self._topup_too_old: set[int] = set()
        self._topup_record_alerted: set[int] = set()
        self._topup_cursor: tuple[datetime, int] | None = None

    @property
    def is_running(self) -> bool:
        return self._running

    async def run_once(self, *, bot) -> tuple[int, int, int]:
        """Run one bounded v2-only recovery pass in one isolated DB session."""
        async with AsyncSessionLocal() as db:
            reconciled = await reconcile_device_first_payments(db, limit=20, direct_only=True)
            provisioned = await process_direct_provisioning_outbox(db, limit=20)
            # 🔴 РФ-1, найдено критиком полноты: без этой строки «комиссия в минуты»
            # держалась на ЕДИНСТВЕННОЙ побудке после продажи. Любой её отказ или
            # повтор работы отбрасывал партнёра к часовому ожиданию — фоновый слив
            # депозитной очереди есть только в цикле мониторинга с периодом 60 минут.
            await process_device_first_deposit_outbox(db, limit=20)
            notified = await process_device_first_notification_outbox(db, bot=bot, limit=20)

        # 🔴 Добор онбординга живёт ЗДЕСЬ, а не в своём цикле, по одной причине: это
        # единственный воркер с коротким шагом (10 с), обещать «через 10 минут» в часовом
        # цикле мониторинга нельзя, а отдельный цикл ради одного письма запрещён планом.
        # Своя страховка обязательна: это денежный путь, и сбой онбординга не имеет права
        # останавливать сверку платежей и выдачу.
        try:
            from app.utils.funnel_notify import process_due_referral_onboarding_followups

            # 🔴 Потолок обязателен. Один человек в очереди может стоить до 30 с ожидания
            # соединения с базой плюс до 60 с на отправку в Telegram, а их за проход до
            # двадцати — без потолка добор задержал бы сверку платежей на десятки минут.
            async with asyncio.timeout(ONBOARDING_FOLLOWUP_TIMEOUT_SECONDS):
                await process_due_referral_onboarding_followups(bot=bot, limit=20)
        except Exception as error:
            logger.error('referral_onboarding_followup_failed', error=type(error).__name__)

        if monotonic() - self._topup_sweep_at >= 60:
            self._topup_sweep_at = monotonic()
            try:
                await self._sweep_topup_intents(bot=bot)
            except Exception as error:
                logger.exception('topup_intent_sweep_failed', error=type(error).__name__)
        return reconciled, provisioned, notified

    async def _sweep_topup_intents(self, *, bot) -> None:
        from app.database.crud.platega import get_platega_payment_by_id_for_update
        from app.database.crud.user import get_user_by_id
        from app.services import device_first_checkout_service as dfc
        from app.services.payment.common import send_topup_intent_notice
        from app.services.user_cart_service import user_cart_service

        now = datetime.now(UTC)
        picked = []
        async with AsyncSessionLocal() as db:
            rows = (
                await db.execute(
                    select(PlategaPayment, Transaction.completed_at)
                    .join(Transaction, Transaction.id == PlategaPayment.transaction_id)
                    .where(
                        Transaction.completed_at <= now - timedelta(minutes=5),
                        PlategaPayment.status.is_distinct_from('CHARGEBACKED'),
                    )
                    .order_by(Transaction.completed_at)
                )
            ).all()
            for payment, completed in rows:
                intent = dfc.topup_intent_of(payment)
                if (
                    not intent
                    or intent.get('recovered_at')
                    or (intent.get('decided_at') and (payment.metadata_json or {}).get('balance_credited'))
                ):
                    continue
                if dfc._intent_time(completed) < now - timedelta(hours=24):
                    if payment.id not in self._topup_too_old:
                        logger.warning('topup_intent_sweep_too_old', payment_id=payment.id)
                        self._topup_too_old.add(payment.id)
                else:
                    picked.append((payment.id, dfc._intent_time(completed)))
        # Неудачная тройка не запирает хвост: после каждого прохода продолжаем с другой строки.
        if self._topup_cursor is not None:
            picked.sort(key=lambda row: ((row[1], row[0]) <= self._topup_cursor, row[1], row[0]))
        picked = picked[:3]
        if picked:
            self._topup_cursor = picked[-1][1], picked[-1][0]
        for payment_id, completed in picked:
            try:
                # Решение берёт свои P→U замки. Не держим транзакцию выборки и не ставим timeout на решение/отправку.
                outcome_failure = {}
                final = await dfc.complete_topup_intent(payment_id=payment_id, outcome_failure=outcome_failure)
                async with AsyncSessionLocal() as db:
                    payment = await get_platega_payment_by_id_for_update(db, payment_id)
                    intent = dfc.topup_intent_of(payment) or {}
                    if payment.status == 'CHARGEBACKED' or intent.get('recovered_at'):
                        continue
                    user = await get_user_by_id(db, payment.user_id)
                    if final is None:
                        if outcome_failure.get('failed') and payment_id not in self._topup_record_alerted:
                            await self._topup_alarm(bot, payment, user, intent, completed, 'outcome_failed')
                        logger.warning('topup_intent_sweep_failed', payment_id=payment_id, reason='outcome_failed')
                        continue
                    if user.referred_by_id:
                        logger.warning(
                            'topup_intent_sweep_referral_not_replayed', payment_id=payment_id, user_id=user.id
                        )
                    user_id = payment.user_id
                    await db.rollback()  # доставка сама берёт свежий замок; забор удаления может коммитить
                    user = await get_user_by_id(db, user_id)
                    await user_cart_service.delete_user_cart(user_id)
                    await user_cart_service.clear_topup_intent(user_id)
                    delivery = await send_topup_intent_notice(db, payment_id=payment_id, user=user, bot=bot)
                    payment = await get_platega_payment_by_id_for_update(db, payment_id)
                    intent = dfc.topup_intent_of(payment) or {}
                    if intent.get('recovered_at') or payment.status == 'CHARGEBACKED':
                        continue
                    if intent.get('reason') in {
                        'technical_error',
                        'order_on_review',
                        'restricted',
                        'account_erasure',
                    } or delivery in {'failed', 'no_contact', 'telegram_plain'}:
                        if not await self._topup_alarm(bot, payment, user, intent, completed, delivery):
                            continue
                    payment.metadata_json = {
                        **payment.metadata_json,
                        dfc.TOPUP_INTENT_KEY: {
                            **intent,
                            'recovered_at': datetime.now(UTC).isoformat(),
                            'delivery': delivery,
                        },
                    }
                    await db.commit()
                    logger.info(
                        'topup_intent_sweep',
                        payment_id=payment_id,
                        status=intent.get('status'),
                        reason=intent.get('reason'),
                        age_seconds=int((now - completed).total_seconds()),
                        delivery=delivery,
                    )
            except Exception as error:
                logger.exception(
                    'topup_intent_sweep_failed', payment_id=payment_id, error=type(error).__name__, _admin_notified=True
                )
                # Сбой commit отметки доставки/добора — отдельная техническая тревога, не тишина в журнале.
                try:
                    async with AsyncSessionLocal() as db:
                        payment = await get_platega_payment_by_id_for_update(db, payment_id)
                        user = await get_user_by_id(db, payment.user_id)
                        intent = {**(dfc.topup_intent_of(payment) or {}), 'reason': 'technical_error'}
                        await self._topup_alarm(bot, payment, user, intent, completed, 'record_failed')
                except Exception as alarm_error:
                    logger.error(
                        'topup_intent_sweep_alarm_failed',
                        payment_id=payment_id,
                        error=type(alarm_error).__name__,
                        _admin_notified=True,
                    )

    async def _topup_alarm(self, bot, payment, user, intent, completed, delivery) -> bool:
        from app.services.admin_notification_service import AdminNotificationService, NotificationCategory
        from app.services.payment.platega import _OWNER_REFUSAL_WHY

        if payment.id in self._topup_record_alerted:
            return True
        reason = _OWNER_REFUSAL_WHY.get(intent.get('reason'), 'технический сбой')
        balance = settings.format_price(user.balance_kopeks) if user else 'не прочитан'
        money = (
            f'Деньги на балансе {balance}; по этому заказу бот ничего не списывал.'
            if intent.get('status') == 'refused' and not intent.get('charging_checkout_id')
            else f'Текущий баланс {balance}; проверьте, было ли списание по этому заказу.'
        )
        delivered = (
            'дошло'
            if delivery in {'telegram', 'email_or_cabinet'}
            else 'дошло обычное пополнение без результата заказа'
            if delivery == 'telegram_plain'
            else 'доставка не подтверждена'
        )
        sent = await AdminNotificationService(bot)._send_message(
            '<b>Доплата: нужен разбор — доведено после прерывания</b>\n'
            f'Клиент: {escape(user.full_name) if user else "аккаунт не найден"}, ID {payment.user_id}; платёж {payment.id}\n'
            f'Пришло {settings.format_price(payment.amount_kopeks)} в {completed.astimezone(ZoneInfo("Europe/Moscow")):%d.%m.%Y %H:%M} МСК\n'
            f'Заказ: {intent.get("period_days")} дн., устройств {intent.get("devices")}, цена {settings.format_price(intent.get("quote_kopeks") or 0)}\n'
            f'Причина: {reason}. {money}\nСообщение клиенту: {delivered}.\n'
            'Откройте клиента и заказ, проверьте баланс и напишите клиенту решение.',
            category=NotificationCategory.ERRORS,
        )
        if sent is not False:
            self._topup_record_alerted.add(payment.id)
        return sent is not False

    async def start(self, *, bot) -> None:
        if self._running:
            logger.warning('device_first_recovery_worker_already_running')
            return
        self._running = True
        interval_seconds = settings.get_device_first_recovery_worker_interval_seconds()
        logger.info('device_first_recovery_worker_started', interval_seconds=interval_seconds)
        try:
            while self._running:
                try:
                    await self.run_once(bot=bot)
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    logger.error('device_first_recovery_worker_cycle_failed', error=type(error).__name__)
                await asyncio.sleep(interval_seconds)
        except asyncio.CancelledError:
            raise
        finally:
            self._running = False
            logger.info('device_first_recovery_worker_stopped')

    def stop(self) -> None:
        self._running = False


device_first_recovery_service = DeviceFirstRecoveryService()
