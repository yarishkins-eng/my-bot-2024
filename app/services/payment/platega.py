"""Mixin для интеграции платежей Platega."""

from __future__ import annotations

import html
import uuid
from datetime import UTC, datetime
from importlib import import_module
from typing import Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import (
    CheckoutPaymentAttempt,
    DeviceAddonTopupAttempt,
    DeviceFirstDepositOutbox,
    DeviceFirstOutbox,
    PaymentMethod,
    SubscriptionCheckout,
    TransactionType,
    User,
)
from app.services.platega_service import PlategaService
from app.utils.payment_logger import payment_logger as logger
from app.utils.user_utils import format_referrer_info


# Причина отказа автооформления доплаты (ВК-16) — словами для карточки владельцу: он не программист.
_OWNER_REFUSAL_WHY = {
    'expired': 'клиент оплатил позже чем через час',
    'replaced': 'клиент открыл новый счёт на доплату',
    'cancelled': 'клиент отменил или изменил заказ в боте',
    'disabled': 'автооформление выключено для этого клиента',
    'price_changed': 'цена изменилась',
    'balance_short': 'не хватило баланса',
    'subscription_changed': 'подписка клиента изменилась, пока шла оплата',
    'already_purchased': 'после заказа было другое списание за подписку',
    'open_order': 'у клиента открыт другой заказ',
    'order_on_review': 'у клиента заказ на разборе — загляните в «Заказы на разборе»',
    'restricted': 'у клиента запрет на покупку подписки',
    'account_erasure': 'клиент удаляет аккаунт',
    'unavailable': 'этот заказ сейчас не продаётся',
}


class PlategaPaymentMixin:
    """Логика создания и обработки платежей Platega."""

    _SUCCESS_STATUSES = {'CONFIRMED'}
    _FAILED_STATUSES = {'FAILED', 'CANCELED', 'EXPIRED'}
    _POST_PAID_REVERSAL_STATUSES = {'CHARGEBACKED'}
    _PENDING_STATUSES = {'PENDING', 'INPROGRESS'}

    @staticmethod
    def _is_direct_device_first_payment(payment: Any) -> bool:
        """Direct-sale callbacks must not retain raw webhook payloads/signatures."""
        metadata = getattr(payment, 'metadata_json', None) or {}
        return metadata.get('settlement_mode') == 'direct_purchase_v2'

    @staticmethod
    async def _get_durable_device_addon_attempt(db: AsyncSession, payment_id: int) -> DeviceAddonTopupAttempt | None:
        """Classify an add-on payment by its durable FK, not mutable JSON."""
        return await db.scalar(
            select(DeviceAddonTopupAttempt).where(DeviceAddonTopupAttempt.platega_payment_id == payment_id)
        )

    @staticmethod
    async def _get_durable_direct_attempt(db: AsyncSession, payment_id: int) -> CheckoutPaymentAttempt | None:
        """Classify a redacted payment through its retained Direct attempt.

        This does not validate or mutate financial state.  The Device-First
        service re-locks and verifies the full payment -> attempt -> checkout
        graph before a terminal result can change any projection.
        """
        return await db.scalar(
            select(CheckoutPaymentAttempt).where(
                CheckoutPaymentAttempt.platega_payment_id == payment_id,
                CheckoutPaymentAttempt.settlement_mode == 'direct_purchase_v2',
            )
        )

    async def _mark_direct_post_paid_reversal(
        self,
        db: AsyncSession,
        *,
        payment: Any,
        provider_status: str,
        lease_token: str | None = None,
        lease_epoch: int | None = None,
    ) -> None:
        """Fence a contradictory post-paid provider terminal status for review.

        A direct sale never turns into a negative generic payment or a balance
        operation after it was paid. The subscription/outbox may already have
        progressed, so an operator must reconcile rather than an automatic
        deprovision, re-provision, refund, or second charge taking place.
        """
        metadata = getattr(payment, 'metadata_json', None) or {}
        attempt_id = metadata.get('device_first_attempt_id')
        if not isinstance(attempt_id, int):
            durable_attempt = await self._get_durable_direct_attempt(db, payment.id)
            attempt_id = getattr(durable_attempt, 'id', None)
        reason = f'post_paid_provider_terminal:{provider_status.lower()}'
        # Direct sale creation/final commit lock this row first.  Taking the
        # same per-user lock before the old checkout becomes operator_review
        # makes the fence serializable: a later draft cannot race through to a
        # provider POST or a wallet debit behind a contradictory callback.
        payment_user_id = getattr(payment, 'user_id', None)
        if payment_user_id is not None:
            await db.execute(select(User).where(User.id == payment_user_id).with_for_update())
        attempt = None
        if isinstance(attempt_id, int):
            attempt_query = select(CheckoutPaymentAttempt).where(CheckoutPaymentAttempt.id == attempt_id)
            if lease_token is not None and lease_epoch is not None:
                attempt_query = attempt_query.where(
                    CheckoutPaymentAttempt.lease_token == lease_token,
                    CheckoutPaymentAttempt.lease_epoch == lease_epoch,
                    CheckoutPaymentAttempt.lease_expires_at >= datetime.now(UTC),
                )
            attempt = (await db.execute(attempt_query.with_for_update())).scalar_one_or_none()

        if attempt is None or attempt.settlement_mode != 'direct_purchase_v2':
            # A polling worker that lost its lease must not overwrite a newer
            # callback's decision. A webhook path has no lease and therefore
            # still preserves an unclassifiable paid record for an operator.
            if lease_token is not None or lease_epoch is not None:
                logger.warning('direct_device_first_reversal_lease_lost', payment_id=payment.id)
                return
            payment.status = 'OPERATOR_REVIEW'
            payment.updated_at = datetime.now(UTC)
            await db.commit()
            logger.error('direct_device_first_reversal_missing_attempt', payment_id=payment.id)
            return

        payment.status = 'OPERATOR_REVIEW'
        # Preserve the fact that a receipt was accepted. Setting this false
        # would let generic legacy code reinterpret an already settled sale.
        payment.updated_at = datetime.now(UTC)
        checkout = (
            await db.execute(
                select(SubscriptionCheckout).where(SubscriptionCheckout.id == attempt.checkout_id).with_for_update()
            )
        ).scalar_one_or_none()
        attempt.status = 'operator_review'
        attempt.reconciliation_reason = reason
        if checkout is not None:
            checkout.lifecycle_state = 'operator_review'
            checkout.terminal_reason = reason
            outboxes = list(
                (
                    await db.execute(
                        select(DeviceFirstOutbox)
                        .where(
                            DeviceFirstOutbox.checkout_id == checkout.id,
                            DeviceFirstOutbox.settlement_mode == 'direct_purchase_v2',
                            DeviceFirstOutbox.status.in_(['pending', 'retry', 'processing']),
                        )
                        .with_for_update()
                    )
                ).scalars()
            )
            for outbox in outboxes:
                outbox.status = 'operator_review'
                outbox.last_error = reason
            # 🔴 РФ-1, найдено ревью денег. Заморозить выдачу мало: с этого этапа прямая
            # продажа заводит ещё и работу выплаты партнёру. Провайдер деньги забрал, а
            # очередь через считаные минуты заплатила бы 100 ₽ + процент с денег, которых
            # у нас уже нет — на заказе 1 990 ₽ это почти 700 ₽ из своего кармана.
            # Гасим, помечая шаг выполненным.
            # ⛔ ЗДЕСЬ СТОЯЛО «тем же приёмом, что и возврат оператором» — с 29.08.2026
            # (РФ-4) это ЛОЖНЫЙ ОРИЕНТИР: возврат оператором больше так не делает, он
            # наоборот ЗАВОДИТ выплату партнёру. Ссылаться на него как на образец
            # «как погасить комиссию» нельзя. 🟢 Сам этот забор от РФ-4 не пострадал и
            # даже усилился: запрос берёт ВСЕ работы заказа с `referral_status != 'done'`,
            # то есть новую работу возврата он тоже погасит, если платёж отзовут ДО выплаты.
            # ⛔ Уже выплаченную комиссию это не отзывает — отзыва в проекте нет вовсе.
            referral_jobs = list(
                (
                    await db.execute(
                        select(DeviceFirstDepositOutbox)
                        .where(
                            DeviceFirstDepositOutbox.checkout_id == checkout.id,
                            DeviceFirstDepositOutbox.referral_status != 'done',
                        )
                        .with_for_update()
                    )
                ).scalars()
            )
            for job in referral_jobs:
                job.referral_status = 'done'
                job.updated_at = datetime.now(UTC)
        await db.commit()
        logger.warning('direct_device_first_post_paid_reversal', payment_id=payment.id, status=provider_status)

    async def create_platega_payment(
        self,
        db: AsyncSession,
        *,
        user_id: int | None,
        amount_kopeks: int,
        description: str,
        language: str,
        payment_method_code: int,
        return_url: str | None = None,
        failed_url: str | None = None,
        extra_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        service: PlategaService | None = getattr(self, 'platega_service', None)
        if not service or not service.is_configured:
            logger.error('Platega сервис не инициализирован')
            return None

        if payment_method_code not in settings.get_platega_active_methods():
            logger.warning(
                'Запрошен выключенный метод Platega',
                payment_method_code=payment_method_code,
            )
            return None

        if amount_kopeks < settings.PLATEGA_MIN_AMOUNT_KOPEKS:
            logger.warning(
                'Сумма Platega меньше минимальной: <',
                amount_kopeks=amount_kopeks,
                PLATEGA_MIN_AMOUNT_KOPEKS=settings.PLATEGA_MIN_AMOUNT_KOPEKS,
            )
            return None

        if amount_kopeks > settings.PLATEGA_MAX_AMOUNT_KOPEKS:
            logger.warning(
                'Сумма Platega больше максимальной: >',
                amount_kopeks=amount_kopeks,
                PLATEGA_MAX_AMOUNT_KOPEKS=settings.PLATEGA_MAX_AMOUNT_KOPEKS,
            )
            return None

        correlation_id = uuid.uuid4().hex
        payload_token = f'platega:{correlation_id}'

        amount_value = amount_kopeks / 100

        effective_return_url = return_url or settings.get_platega_return_url()
        effective_failed_url = failed_url or settings.get_platega_failed_url()

        try:
            response = await service.create_payment(
                payment_method=payment_method_code,
                amount=amount_value,
                currency=settings.PLATEGA_CURRENCY,
                description=description,
                return_url=effective_return_url,
                failed_url=effective_failed_url,
                payload=payload_token,
            )
        except Exception as error:  # pragma: no cover - network errors
            logger.exception('Ошибка Platega при создании платежа', error=error)
            return None

        if not response:
            logger.error('Platega вернул пустой ответ при создании платежа')
            return None

        transaction_id = response.get('transactionId') or response.get('id')
        redirect_url = PlategaService.parse_redirect_url(response)
        status = str(response.get('status') or 'PENDING').upper()
        expires_at = PlategaService.parse_expires_at(response.get('expiresIn'))

        # ВК-16 (16а-1): намерение доплаты кладётся сюда (`topup_intent`). Свои ключи — первыми: перекрыть
        # базовые им нельзя. ⛔ Ключи device-first (`settlement_mode`, `device_first_attempt_id`) уводят вебхук
        # в ветку прямой продажи без зачисления — через этот параметр их не передавать.
        metadata = {
            **(extra_metadata or {}),
            'raw_response': response,
            'language': language,
            'selected_method': payment_method_code,
        }

        payment_module = import_module('app.services.payment_service')

        payment = await payment_module.create_platega_payment(
            db,
            user_id=user_id,
            amount_kopeks=amount_kopeks,
            currency=settings.PLATEGA_CURRENCY,
            description=description,
            status=status,
            payment_method_code=payment_method_code,
            correlation_id=correlation_id,
            platega_transaction_id=transaction_id,
            redirect_url=redirect_url,
            return_url=effective_return_url,
            failed_url=effective_failed_url,
            payload=payload_token,
            metadata=metadata,
            expires_at=expires_at,
        )

        logger.info(
            'Создан Platega платеж для пользователя (метод , сумма ₽)',
            transaction_id=transaction_id or payment.id,
            user_id=user_id,
            payment_method_code=payment_method_code,
            amount_value=amount_value,
        )

        return {
            'local_payment_id': payment.id,
            'transaction_id': transaction_id,
            'redirect_url': redirect_url,
            'status': status,
            'expires_at': expires_at,
            'correlation_id': correlation_id,
            'payload': payload_token,
        }

    async def process_platega_webhook(
        self,
        db: AsyncSession,
        payload: dict[str, Any],
    ) -> bool:
        payment_module = import_module('app.services.payment_service')

        transaction_id = str(payload.get('id') or '').strip()
        payload_token = payload.get('payload')

        payment = None
        if transaction_id:
            payment = await payment_module.get_platega_payment_by_transaction_id(db, transaction_id)
        if not payment and payload_token:
            payment = await payment_module.get_platega_payment_by_correlation_id(
                db, str(payload_token).replace('platega:', '')
            )

        if not payment:
            logger.warning('Platega webhook: платеж не найден', transaction_id=transaction_id)
            return False

        # Lock payment row immediately to prevent concurrent webhook processing (TOCTOU race)
        platega_crud = import_module('app.database.crud.platega')
        locked = await platega_crud.get_platega_payment_by_id_for_update(db, payment.id)
        if not locked:
            logger.error('Platega: не удалось заблокировать платёж', payment_id=payment.id)
            return False
        payment = locked
        device_addon_attempt = await self._get_durable_device_addon_attempt(db, payment.id)
        if device_addon_attempt is not None:
            # Add-on callbacks are authenticated by the existing webhook
            # endpoint, but canonical GET remains the only settlement proof.
            # The dedicated handler records an exact correlation and wakes its
            # reconciler without exposing the payment to generic cart/autopay.
            from app.services.device_addon_payment_service import handle_device_addon_platega_callback

            return await handle_device_addon_platega_callback(db, payment=payment, payload=payload)

        direct_device_first = self._is_direct_device_first_payment(payment)
        durable_attempt = None
        if not direct_device_first:
            durable_attempt = await self._get_durable_direct_attempt(db, payment.id)
            direct_device_first = durable_attempt is not None

        status_raw = str(payload.get('status') or '').upper()
        if not status_raw:
            logger.warning('Platega webhook без статуса для платежа', payment_id=payment.id)
            return False

        if direct_device_first:
            # Direct checkout is deliberately not handled by the generic
            # Platega state machine below.  Its callback may arrive before
            # POST has returned the transaction id; its terminal/PENDING
            # view may also be older than canonical GET.  First determine
            # whether the immutable provider identity has been bound while
            # holding the payment lock.
            from app.services.device_first_payment_service import (
                _queue_direct_callback_for_canonical_reconciliation,
                settle_device_first_platega_payment,
            )

            attempt_id = (payment.metadata_json or {}).get('device_first_attempt_id')
            attempt = durable_attempt
            if attempt is None and isinstance(attempt_id, int):
                attempt = (
                    await db.execute(
                        select(CheckoutPaymentAttempt)
                        .where(CheckoutPaymentAttempt.id == attempt_id)
                        .execution_options(populate_existing=True)
                    )
                ).scalar_one_or_none()
            if attempt is None or not attempt.provider_payment_id:
                await _queue_direct_callback_for_canonical_reconciliation(
                    db,
                    payment_id=payment.id,
                    payload=payload,
                    reason='provider_callback_before_identity_binding',
                    attempt_id=getattr(attempt, 'id', None),
                )
                return True

            if status_raw in self._SUCCESS_STATUSES:
                result = await settle_device_first_platega_payment(
                    db,
                    payment=payment,
                    payload=payload,
                )
                return result is not None

            if status_raw in self._POST_PAID_REVERSAL_STATUSES:
                await self._mark_direct_post_paid_reversal(
                    db,
                    payment=payment,
                    provider_status=status_raw,
                )
                return True

            if status_raw in self._FAILED_STATUSES and payment.is_paid:
                # A paid sale receiving a later cancellation is a financial
                # contradiction, not an abandoned invoice. Freeze fulfilment
                # for accountable review before any generic callback path.
                await self._mark_direct_post_paid_reversal(
                    db,
                    payment=payment,
                    provider_status=status_raw,
                )
                return True

            # Signed terminal/PENDING/unknown callbacks are receipt evidence
            # only.  Canonical GET decides live, terminal or conflicting
            # state; a stale callback can therefore never reopen, cancel or
            # downgrade a direct payment.
            callback_reason = (
                'provider_terminal_callback_awaiting_canonical'
                if status_raw in self._FAILED_STATUSES
                else 'provider_pending_callback_awaiting_canonical'
                if status_raw in self._PENDING_STATUSES
                else 'provider_callback_unknown_status'
            )
            await _queue_direct_callback_for_canonical_reconciliation(
                db,
                payment_id=payment.id,
                payload=payload,
                reason=callback_reason,
                attempt_id=getattr(attempt, 'id', None),
            )
            return True

        if status_raw in self._SUCCESS_STATUSES:
            if payment.is_paid:
                logger.info('Platega платеж уже помечен как оплачен', correlation_id=payment.correlation_id)
                # Update callback payload without releasing the lock prematurely
                if not direct_device_first:
                    payment.callback_payload = payload
                if transaction_id and not payment.platega_transaction_id:
                    payment.platega_transaction_id = transaction_id
                payment.updated_at = datetime.now(UTC)
                await db.commit()
                return True

            # Inline field updates — NO intermediate commit that would release FOR UPDATE lock
            payment.status = status_raw
            if not direct_device_first:
                payment.callback_payload = payload
            if transaction_id and not payment.platega_transaction_id:
                payment.platega_transaction_id = transaction_id
            payment.updated_at = datetime.now(UTC)
            await db.flush()

            result = await self._finalize_platega_payment(db, payment, payload)
            if result is None:
                logger.error('Platega webhook: финализация не удалась', payment_id=payment.id)
                return False
            return True

        if status_raw in self._POST_PAID_REVERSAL_STATUSES:
            await payment_module.update_platega_payment(
                db,
                payment=payment,
                status=status_raw,
                callback_payload=payload,
                platega_transaction_id=transaction_id or None,
            )
            return True

        if status_raw in self._FAILED_STATUSES:
            await payment_module.update_platega_payment(
                db,
                payment=payment,
                status=status_raw,
                callback_payload=None if direct_device_first else payload,
                platega_transaction_id=transaction_id or None,
                is_paid=False,
            )
            logger.info('Platega платеж перешёл в статус', correlation_id=payment.correlation_id, status_raw=status_raw)
            return True

        await payment_module.update_platega_payment(
            db,
            payment=payment,
            status=status_raw,
            callback_payload=None if direct_device_first else payload,
            platega_transaction_id=transaction_id or None,
        )
        return True

    async def get_platega_payment_status(
        self,
        db: AsyncSession,
        local_payment_id: int,
    ) -> dict[str, Any] | None:
        payment_module = import_module('app.services.payment_service')
        payment = await payment_module.get_platega_payment_by_id(db, local_payment_id)
        if not payment:
            return None

        # Device-first v2 is settled only by its verified callback or its
        # lease-fenced worker. Generic user-initiated checks are local reads:
        # they may observe state but cannot contact the provider or settle it.
        if (
            await self._get_durable_device_addon_attempt(db, payment.id)
            or self._is_direct_device_first_payment(payment)
            or await self._get_durable_direct_attempt(db, payment.id)
        ):
            return {
                'payment': payment,
                'status': payment.status,
                'is_paid': payment.is_paid,
                'remote': None,
            }

        service: PlategaService | None = getattr(self, 'platega_service', None)
        remote_status: str | None = None
        remote_payload: dict[str, Any] | None = None

        if service and payment.platega_transaction_id:
            try:
                remote_payload = await service.get_transaction(payment.platega_transaction_id)
            except Exception as error:  # pragma: no cover - network errors
                logger.error(
                    'Ошибка Platega при получении транзакции',
                    platega_transaction_id=payment.platega_transaction_id,
                    error=error,
                )

        if remote_payload:
            remote_status = str(remote_payload.get('status') or '').upper()
            status_changed = remote_status and remote_status != payment.status

            if remote_status in self._SUCCESS_STATUSES and not payment.is_paid:
                # Lock payment row before finalization to prevent concurrent double-processing
                platega_crud = import_module('app.database.crud.platega')
                locked = await platega_crud.get_platega_payment_by_id_for_update(db, payment.id)
                if not locked:
                    logger.error('Platega status check: не удалось заблокировать платёж', payment_id=payment.id)
                elif locked.is_paid:
                    # Another concurrent handler already processed — skip
                    logger.info('Platega платеж уже оплачен после блокировки', correlation_id=locked.correlation_id)
                    payment = locked
                else:
                    payment = locked
                    payment.status = remote_status
                    if not self._is_direct_device_first_payment(payment):
                        payment.callback_payload = remote_payload
                        payment.metadata_json = {
                            **(getattr(payment, 'metadata_json', {}) or {}),
                            'remote_status': remote_payload,
                        }
                    payment.updated_at = datetime.now(UTC)
                    await db.flush()
                    result = await self._finalize_platega_payment(db, payment, remote_payload)
                    if result is not None:
                        payment = result
            elif status_changed:
                # 🔴 Мина OQ (ВК-16, 16а-2, заявка 2): метаданные пишем на перечитанной под замком строке и дописываем
                # только своё поле. Прежняя запись присваивала их целиком из снимка начала прохода (автопроверка, «Проверить»
                # клиента и админа, кнопки чат-админки) — метка «заменено»/«отменено» и исход доплаты откатывались бы.
                platega_crud = import_module('app.database.crud.platega')
                locked = await platega_crud.get_platega_payment_by_id_for_update(db, payment.id)
                if locked is not None:
                    payment = locked
                    payment.status = remote_status
                    if not self._is_direct_device_first_payment(payment):
                        payment.metadata_json = {**(payment.metadata_json or {}), 'remote_status': remote_payload}
                    payment.updated_at = datetime.now(UTC)
                await db.commit()

        return {
            'payment': payment,
            'status': payment.status,
            'is_paid': payment.is_paid,
            'remote': remote_payload,
        }

    async def _finalize_platega_payment(
        self,
        db: AsyncSession,
        payment: Any,
        payload: dict[str, Any] | None,
    ) -> Any:
        payment_module = import_module('app.services.payment_service')

        paid_at = None
        if isinstance(payload, dict):
            paid_at_raw = payload.get('paidAt') or payload.get('confirmedAt')
            if paid_at_raw:
                try:
                    paid_at_parsed = datetime.fromisoformat(str(paid_at_raw))
                    paid_at = paid_at_parsed if paid_at_parsed.tzinfo else paid_at_parsed.replace(tzinfo=UTC)
                except ValueError:
                    paid_at = None

        # FOR UPDATE lock already acquired by caller — just check idempotency
        if payment.transaction_id:
            logger.info(
                'Platega платеж уже связан с транзакцией',
                correlation_id=payment.correlation_id,
                transaction_id=payment.transaction_id,
            )
            return payment

        # Read fresh metadata AFTER lock to avoid stale data
        metadata = dict(getattr(payment, 'metadata_json', {}) or {})

        # A durable add-on FK always wins over mutable metadata.  This branch
        # is defensive (normal webhook/status paths intercept earlier) and
        # prevents a future generic call site from crediting the same invoice
        # through cart/daily/autopay side effects.
        if await self._get_durable_device_addon_attempt(db, payment.id):
            from app.services.device_addon_payment_service import handle_device_addon_platega_callback

            if isinstance(payload, dict):
                await handle_device_addon_platega_callback(db, payment=payment, payload=payload)
            return payment

        # Device-first owns its exact provider amount, ledger idempotency and
        # explicit-arm fulfillment. It must not fall through to the generic
        # top-up/cart/autopay hooks.
        if metadata.get('device_first_attempt_id') is not None or await self._get_durable_direct_attempt(
            db, payment.id
        ):
            from app.services.device_first_payment_service import settle_device_first_platega_payment

            return await settle_device_first_platega_payment(
                db,
                payment=payment,
                payload=payload,
            )

        # --- Guest purchase flow (landing page) ---
        from app.services.payment.common import try_fulfill_guest_purchase

        guest_result = await try_fulfill_guest_purchase(
            db,
            metadata=metadata,
            payment_amount_kopeks=payment.amount_kopeks,
            provider_payment_id=payment.correlation_id,
            provider_name='platega',
        )
        if guest_result is not None:
            return payment

        if payload is not None:
            metadata['webhook'] = payload

        # Inline field assignments instead of update_platega_payment() which commits
        # and would release the FOR UPDATE lock prematurely
        payment.status = 'CONFIRMED'
        payment.is_paid = True
        if paid_at is not None:
            payment.paid_at = paid_at
        payment.metadata_json = metadata
        if payload is not None:
            payment.callback_payload = payload
        payment.updated_at = datetime.now(UTC)

        balance_already_credited = bool(metadata.get('balance_credited'))

        invoice_message = metadata.get('invoice_message') or {}
        if getattr(self, 'bot', None):
            chat_id = invoice_message.get('chat_id')
            message_id = invoice_message.get('message_id')
            if chat_id and message_id:
                try:
                    await self.bot.delete_message(chat_id, message_id)
                except Exception as delete_error:  # pragma: no cover - depends on bot rights
                    logger.warning('Не удалось удалить Platega счёт', message_id=message_id, delete_error=delete_error)
                else:
                    metadata.pop('invoice_message', None)

        user = await payment_module.get_user_by_id(db, payment.user_id)
        if not user:
            logger.error('Пользователь не найден для Platega', user_id=payment.user_id)
            return payment

        # Убеждаемся, что промогруппы загружены в асинхронном контексте,
        # чтобы избежать попыток ленивой загрузки без greenlet
        await db.refresh(user, attribute_names=['promo_group', 'user_promo_groups'])
        for user_promo_group in getattr(user, 'user_promo_groups', []):
            await db.refresh(user_promo_group, attribute_names=['promo_group'])

        promo_group = user.get_primary_promo_group()
        subscription = getattr(user, 'subscription', None)
        referrer_info = format_referrer_info(user)

        transaction_external_id = (
            str(payload.get('id'))
            if isinstance(payload, dict) and payload.get('id')
            else payment.platega_transaction_id
        )

        existing_transaction = None
        if transaction_external_id:
            existing_transaction = await payment_module.get_transaction_by_external_id(
                db,
                transaction_external_id,
                PaymentMethod.PLATEGA,
            )

        platega_name = settings.get_platega_display_name()
        method_display = settings.get_platega_method_display_name(payment.payment_method_code)
        description = (
            f'Пополнение через {platega_name} ({method_display})'
            if method_display
            else f'Пополнение через {platega_name}'
        )

        transaction = existing_transaction
        created_transaction = False

        if not transaction:
            transaction = await payment_module.create_transaction(
                db,
                user_id=payment.user_id,
                type=TransactionType.DEPOSIT,
                amount_kopeks=payment.amount_kopeks,
                description=description,
                payment_method=PaymentMethod.PLATEGA,
                external_id=transaction_external_id or payment.correlation_id,
                is_completed=True,
                created_at=getattr(payment, 'created_at', None),
                commit=False,
            )
            created_transaction = True

        await payment_module.link_platega_payment_to_transaction(db, payment=payment, transaction_id=transaction.id)

        should_credit_balance = created_transaction or not balance_already_credited

        if not should_credit_balance:
            logger.info('Platega платеж уже зачислил баланс ранее', correlation_id=payment.correlation_id)
            return payment

        # Lock user row to prevent concurrent balance race conditions
        from app.database.crud.user import lock_user_for_update

        user = await lock_user_for_update(db, user)

        old_balance = user.balance_kopeks
        was_first_topup = not user.has_made_first_topup

        user.balance_kopeks += payment.amount_kopeks
        user.updated_at = datetime.now(UTC)
        await db.commit()
        await db.refresh(user)

        # ВК-16 (16а-2): доплата под заказ оформляет его сама (решение владельца 05.10.2026). Место — строго здесь:
        # депозит уже закоммичен, а побочные эффекты ниже ещё не взяли `User FOR UPDATE` в этой сессии без коммита
        # (`maybe_assign_promo_group_by_total_spent`) — иначе своя сессия оформления ждала бы его до таймаута.
        from app.services import device_first_checkout_service as dfc

        has_topup_intent = dfc.topup_intent_of(payment) is not None
        topup_intent = await dfc.complete_topup_intent(payment_id=payment.id) if has_topup_intent else None
        if topup_intent is not None:
            # Финальная запись ниже присваивает метаданные ЦЕЛИКОМ из этого словаря — без строки исход затёрся бы.
            metadata[dfc.TOPUP_INTENT_KEY] = topup_intent
        fulfilled = topup_intent is not None and topup_intent.get('status') == 'fulfilled'
        # Списание шло в своей сессии: `user` здесь помнит баланс ДО него — для слов человеку и владельцу читаем свежий.
        # Свежий баланс и при отказе: «уже была оплата» другим путём в эти секунды списала его в чужой сессии (волна 2
        # заявки 3а), а `user` вебхука помнит баланс сразу после зачисления.
        balance_left = (
            await db.scalar(select(User.balance_kopeks).where(User.id == user.id))
            if has_topup_intent
            else user.balance_kopeks
        )
        if has_topup_intent:
            # Старая корзина и автопродление эти деньги не тратят (ловушка 8 стартера) — гасим корзину и её метку ДО
            # сообщения и карточки: иначе хвост промолчит «автопокупка объяснится сама», кнопка поведёт в удалённую
            # корзину, а карточка владельцу пообещает покупку, которой не будет.
            try:
                from app.services.user_cart_service import user_cart_service

                await user_cart_service.delete_user_cart(user.id)
                await user_cart_service.clear_topup_intent(user.id)
            except Exception as error:
                logger.error('Не удалось погасить корзину после доплаты под заказ', user_id=user.id, error=error)

        # Emit deferred side-effects after atomic commit
        from app.database.crud.transaction import emit_transaction_side_effects

        await emit_transaction_side_effects(
            db,
            transaction,
            amount_kopeks=payment.amount_kopeks,
            user_id=payment.user_id,
            type=TransactionType.DEPOSIT,
            payment_method=PaymentMethod.PLATEGA,
            external_id=transaction_external_id or payment.correlation_id,
        )

        topup_status = '🆕 Первое пополнение' if was_first_topup else '🔄 Пополнение'

        try:
            from app.services.referral_service import process_referral_topup

            await process_referral_topup(
                db,
                user.id,
                payment.amount_kopeks,
                getattr(self, 'bot', None),
            )
        except Exception as error:
            logger.error('Ошибка обработки реферального пополнения Platega', error=error)

        if was_first_topup and not user.has_made_first_topup and not user.referred_by_id:
            user.has_made_first_topup = True
            await db.commit()
            await db.refresh(user)

        method_title = settings.get_platega_method_display_title(payment.payment_method_code)

        # Оформленному «Пополнение успешно… подписка сама не оплатится» было бы ложью; «✅ Ваша VPN-подписка готова» и
        # меню подписчика придут из очереди выдачи — но только когда панель выдаст доступ (при сбое — повтор через
        # минуты, при разборе — никогда). Поэтому о деньгах говорим сразу и честно: получены, заказ оформлен.
        # 🔴 Мина OT: «готова» не придёт, если выдача ушла на разбор или панель молчит, — поэтому не обещаем его
        # безусловно, а говорим, что делать, если его нет, и называем остаток на балансе.
        if getattr(self, 'bot', None) and user.telegram_id and fulfilled:
            try:
                english = getattr(user, 'language', None) == 'en'
                support_url = settings.get_support_contact_url()
                await self.bot.send_message(
                    user.telegram_id,
                    (
                        f'✅ <b>Payment received: {settings.format_price(payment.amount_kopeks)}</b>\n\n'
                        'Your order is paid from the balance. When the subscription is connected, you will get the '
                        '"Your VPN subscription is ready" message. If it has not arrived in 10 minutes, please '
                        f'contact support.\n\nBalance left: {settings.format_price(balance_left)}'
                        if english
                        else f'✅ <b>Оплата получена: {settings.format_price(payment.amount_kopeks)}</b>\n\n'
                        'Заказ оформлен с баланса. Когда подписка подключится, придёт сообщение «Ваша VPN-подписка '
                        'готова». Если его нет через 10 минут — напишите в поддержку.\n\n'
                        f'На балансе осталось: {settings.format_price(balance_left)}'
                    ),
                    parse_mode='HTML',
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[
                            [
                                InlineKeyboardButton(
                                    text='Contact support' if english else 'Написать в поддержку', url=support_url
                                )
                            ]
                        ]
                    )
                    if support_url
                    else None,
                )
            except Exception as error:
                logger.error('Ошибка отправки уведомления пользователю Platega', error=error)
        refusal, refusal_sent = None, False
        if getattr(self, 'bot', None) and user.telegram_id and topup_intent is not None and not fulfilled:
            # Отказ автооформления — ОДНО сообщение с кнопкой по причине вместо «Пополнение успешно» с общим хвостом
            # (замысел v2, правило 5; план ВК, 16а-2, заявка 2). Не собралось — ниже прежнее «Пополнение успешно»:
            # никогда не тишина (правило 3).
            try:
                from app.handlers.subscription.device_first import topup_intent_refusal_message

                refusal = topup_intent_refusal_message(
                    user, topup_intent, amount_kopeks=payment.amount_kopeks, balance_kopeks=balance_left
                )
            except Exception as error:
                logger.error('Не собралось сообщение отказа автооформления доплаты', user_id=user.id, error=error)
        if refusal is not None:
            try:
                await self.bot.send_message(user.telegram_id, refusal[0], parse_mode='HTML', reply_markup=refusal[1])
                refusal_sent = True
            except Exception as error:
                logger.error('Ошибка отправки отказа автооформления доплаты', user_id=user.id, error=error)
        elif getattr(self, 'bot', None) and user.telegram_id and not fulfilled:
            try:
                keyboard = await self.build_topup_success_keyboard(user)
                # 🔴 Последняя строка сообщения РАЗНАЯ у двух разных людей, и это решение, а не
                # небрежность. Прежняя фраза «Баланс пополнен автоматически!» ничего не добавляла
                # к сумме строкой выше, зато ставила точку — и клиент 106 прочитала её как
                # «покупка закрыта»: деньги легли на баланс, подписка кончилась, 249 ₽ пролежали
                # сутки. Тому, кому ещё есть что оформлять, называем оставшийся шаг; тому, у кого
                # подписка с запасом, оставляем прежний текст без единого изменения.
                from app.services.payment.common import topup_pending_purchase_hint

                # ⛔ Экранируем: с этой правки хвост стал ПЕРЕВОДИМЫМ текстом, а сообщение уходит
                # с `parse_mode='HTML'`. Одна угловая скобка от переводчика — и `send_message`
                # бросит, а `except` ниже только пишет в лог: человек не получит НИЧЕГО о своих
                # деньгах. Раньше хвост был литералом в коде, и достать его было некому.
                # Доплата под заказ сюда попадает, только если её исход не записался вовсе: корзина и её метка уже
                # погашены, так что хвост не ждёт старую автопокупку, а заборы автоплатежа и длинной подписки остаются.
                hint = await topup_pending_purchase_hint(user)
                tail = html.escape(hint or 'Баланс пополнен автоматически!')
                await self.bot.send_message(
                    user.telegram_id,
                    (
                        '✅ <b>Пополнение успешно!</b>\n\n'
                        f'💰 Сумма: {settings.format_price(payment.amount_kopeks)}\n'
                        f'🦊 Способ: {method_title}\n'
                        f'🆔 Транзакция: {transaction.id}\n\n'
                        f'{tail}'
                    ),
                    parse_mode='HTML',
                    reply_markup=keyboard,
                )
            except Exception as error:
                logger.error('Ошибка отправки уведомления пользователю Platega', error=error)

        if getattr(self, 'bot', None):
            try:
                from app.services.admin_notification_service import AdminNotificationService

                notification_service = AdminNotificationService(self.bot)
                await notification_service.send_balance_topup_notification(
                    user,
                    transaction,
                    old_balance,
                    topup_status=topup_status,
                    referrer_info=referrer_info,
                    subscription=subscription,
                    promo_group=promo_group,
                    db=db,
                    auto_next_step=(
                        f'заказ на {topup_intent.get("period_days")} дн., устройств {topup_intent.get("devices")} '
                        f'оформлен сам с баланса, на балансе осталось {settings.format_price(balance_left)}'
                        if fulfilled
                        else None
                    ),
                    # Карточка — после сообщения клиенту и по факту отправки (заявка 3б): раньше она писала
                    # «отправлено объяснение» до отправки и клиенту без Telegram, которому уходит обычное письмо.
                    intent_refused=(
                        'Заказ по доплате бот сам не оформил: '
                        f'{_OWNER_REFUSAL_WHY.get(topup_intent.get("reason"), "технический отказ, смотреть журнал")}'
                        ' — деньги остались на балансе, '
                        + (
                            'клиенту отправлено объяснение'
                            if refusal_sent
                            else 'объяснение в бот клиенту не ушло (нет Telegram или сбой отправки — смотреть журнал)'
                        )
                        if topup_intent is not None and not fulfilled
                        else None
                    ),
                )
            except Exception as error:
                logger.error('Ошибка отправки админ уведомления Platega', error=error)

        try:
            from app.services.payment.common import send_cart_notification_after_topup

            if not has_topup_intent:
                await send_cart_notification_after_topup(user, payment.amount_kopeks, db, getattr(self, 'bot', None))
            else:
                # Деньги доплаты под заказ: старая цепочка (суточная, корзина, автопродление истёкшей) их не трогает —
                # иначе вторая покупка поверх оформленной. Из неё зовём забор удаления аккаунта и письмо тем, у кого
                # нет Telegram (им сообщение выше не уходит).
                from app.services.account_erasure_service import mark_late_legacy_payment_for_manual_review
                from app.services.payment.common import notify_email_user_topup

                if not await mark_late_legacy_payment_for_manual_review(db, user):
                    await notify_email_user_topup(user, payment.amount_kopeks, balance_kopeks=balance_left)
        except Exception as error:
            logger.error(
                'Ошибка при работе с сохраненной корзиной для пользователя',
                user_id=payment.user_id,
                error=error,
                exc_info=True,
            )

        metadata['balance_change'] = {
            'old_balance': old_balance,
            'new_balance': user.balance_kopeks,
            'credited_at': datetime.now(UTC).isoformat(),
        }
        metadata['balance_credited'] = True
        if has_topup_intent:
            # 🔴 Метаданные ниже присваиваются ЦЕЛИКОМ. Намерение берём со строки под её замком: исход пишет оформление
            # в своей сессии, метку отмены — кнопка бота; снимок начала прохода вернул бы `pending` (план ВК, 16а-2 (в)).
            platega_crud = import_module('app.database.crud.platega')
            fresh_intent = dfc.topup_intent_of(await platega_crud.get_platega_payment_by_id_for_update(db, payment.id))
            if fresh_intent is not None:
                metadata[dfc.TOPUP_INTENT_KEY] = fresh_intent

        await payment_module.update_platega_payment(
            db,
            payment=payment,
            metadata=metadata,
        )

        logger.info(
            '✅ Обработан Platega платеж для пользователя',
            correlation_id=payment.correlation_id,
            user_id=payment.user_id,
        )

        return payment
