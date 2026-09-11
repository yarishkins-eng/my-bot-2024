"""Post-commit notifications for the device add-on purchase flow."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import settings
from app.services import (
    admin_notification_service as admin_notifications,
    device_addon_payment_service as payment_service,
    device_addon_service as addon_service,
)


@pytest.mark.asyncio
async def test_purchase_post_commit_effects_emit_transaction_and_admin_notification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = MagicMock()
    transaction = SimpleNamespace(id=41, description='Покупка 2 доп. устройств')
    user = SimpleNamespace(id=17)
    subscription = SimpleNamespace(id=29)
    emit = AsyncMock()
    notify = AsyncMock(return_value=True)
    bot = SimpleNamespace(session=SimpleNamespace(close=AsyncMock()))
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_ENABLED', True)
    monkeypatch.setattr(addon_service, 'emit_transaction_side_effects', emit)

    from app import bot_factory

    monkeypatch.setattr(bot_factory, 'create_bot', lambda: bot)
    monkeypatch.setattr(
        admin_notifications,
        'AdminNotificationService',
        lambda actual_bot: SimpleNamespace(send_subscription_update_notification=notify)
        if actual_bot is bot
        else pytest.fail('unexpected bot'),
    )

    await addon_service._run_purchase_post_commit_effects(
        db,
        transaction=transaction,
        user=user,
        subscription=subscription,
        old_device_limit=2,
        new_device_limit=4,
        price_kopeks=12_300,
    )

    emit.assert_awaited_once_with(
        db,
        transaction,
        amount_kopeks=12_300,
        user_id=17,
        type=addon_service.TransactionType.SUBSCRIPTION_PAYMENT,
        payment_method=addon_service.PaymentMethod.BALANCE,
        description=transaction.description,
    )
    notify.assert_awaited_once_with(
        db=db,
        user=user,
        subscription=subscription,
        update_type='devices',
        old_value=2,
        new_value=4,
        price_paid=12_300,
    )
    bot.session.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_purchase_post_commit_effect_failures_are_best_effort(monkeypatch: pytest.MonkeyPatch) -> None:
    db = MagicMock()
    transaction = SimpleNamespace(id=42, description='purchase')
    user = SimpleNamespace(id=18)
    bot = SimpleNamespace(session=SimpleNamespace(close=AsyncMock()))
    monkeypatch.setattr(settings, 'ADMIN_NOTIFICATIONS_ENABLED', True)
    monkeypatch.setattr(
        addon_service,
        'emit_transaction_side_effects',
        AsyncMock(side_effect=RuntimeError('event emitter unavailable')),
    )

    from app import bot_factory

    monkeypatch.setattr(bot_factory, 'create_bot', lambda: bot)
    monkeypatch.setattr(
        admin_notifications,
        'AdminNotificationService',
        lambda _bot: SimpleNamespace(
            send_subscription_update_notification=AsyncMock(side_effect=RuntimeError('telegram unavailable'))
        ),
    )

    await addon_service._run_purchase_post_commit_effects(
        db,
        transaction=transaction,
        user=user,
        subscription=SimpleNamespace(id=30),
        old_device_limit=2,
        new_device_limit=3,
        price_kopeks=5_000,
    )

    bot.session.close.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('language', 'heading', 'button_text'),
    [
        ('ru', 'Пополнение успешно!', 'Вернуться к покупке'),
        ('en', 'Top-up successful!', 'Return to purchase'),
    ],
)
async def test_paid_effect_notifications_send_admin_and_localized_customer_receipts(
    monkeypatch: pytest.MonkeyPatch,
    language: str,
    heading: str,
    button_text: str,
) -> None:
    subscription = SimpleNamespace(id=31)
    db = SimpleNamespace(get=AsyncMock(return_value=subscription))
    bot = SimpleNamespace(send_message=AsyncMock())
    user = SimpleNamespace(
        id=19,
        telegram_id=123_456,
        language=language,
        balance_kopeks=15_000,
        referred_by_id=7,
    )
    transaction = SimpleNamespace(id=43, amount_kopeks=10_000)
    intent = SimpleNamespace(public_id='intent-public-id', subscription_id=31)
    payment = SimpleNamespace(
        metadata_json={
            'device_addon_attempt_id': 44,
            'balance_before_kopeks': 5_000,
            'was_first_topup': True,
        }
    )
    notify = AsyncMock(return_value=True)
    notification_service = SimpleNamespace(
        _get_referrer_info=AsyncMock(return_value='@friend'),
        _get_user_promo_group=AsyncMock(return_value='promo'),
        send_balance_topup_notification=notify,
    )
    monkeypatch.setattr(settings, 'MINIAPP_CUSTOM_URL', 'https://cabinet.example')
    monkeypatch.setattr(admin_notifications, 'AdminNotificationService', lambda actual_bot: notification_service)

    await payment_service._send_paid_effect_notifications(
        db,
        bot=bot,
        user=user,
        transaction=transaction,
        intent=intent,
        payment=payment,
    )

    notify.assert_awaited_once_with(
        user,
        transaction,
        5_000,
        topup_status='🆕 Первое пополнение',
        referrer_info='@friend',
        subscription=subscription,
        promo_group='promo',
        db=db,
    )
    bot.send_message.assert_awaited_once()
    telegram_id, text = bot.send_message.await_args.args
    assert telegram_id == user.telegram_id
    assert heading in text
    assert settings.format_price(10_000) in text
    markup = bot.send_message.await_args.kwargs['reply_markup']
    button = markup.inline_keyboard[0][0]
    assert button.text == button_text
    assert button.web_app.url == 'https://cabinet.example/subscription/device-topup/intent-public-id'


@pytest.mark.asyncio
async def test_admin_topup_failure_does_not_suppress_customer_receipt(monkeypatch: pytest.MonkeyPatch) -> None:
    db = SimpleNamespace(get=AsyncMock(return_value=None))
    bot = SimpleNamespace(send_message=AsyncMock())
    notification_service = SimpleNamespace(
        _get_referrer_info=AsyncMock(side_effect=RuntimeError('admin lookup failed')),
        _get_user_promo_group=AsyncMock(),
        send_balance_topup_notification=AsyncMock(),
    )
    monkeypatch.setattr(settings, 'MINIAPP_CUSTOM_URL', 'https://cabinet.example')
    monkeypatch.setattr(admin_notifications, 'AdminNotificationService', lambda _bot: notification_service)

    await payment_service._send_paid_effect_notifications(
        db,
        bot=bot,
        user=SimpleNamespace(id=20, telegram_id=123_457, language='ru', balance_kopeks=10_000, referred_by_id=7),
        transaction=SimpleNamespace(id=45, amount_kopeks=10_000),
        intent=SimpleNamespace(public_id='intent-2', subscription_id=None),
        payment=SimpleNamespace(metadata_json={'device_addon_attempt_id': 46}),
    )

    bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_customer_receipt_failure_remains_retryable(monkeypatch: pytest.MonkeyPatch) -> None:
    db = SimpleNamespace(get=AsyncMock(return_value=None))
    bot = SimpleNamespace(send_message=AsyncMock(side_effect=RuntimeError('telegram unavailable')))
    monkeypatch.setattr(settings, 'MINIAPP_CUSTOM_URL', 'https://cabinet.example')
    monkeypatch.setattr(
        admin_notifications,
        'AdminNotificationService',
        lambda _bot: SimpleNamespace(
            _get_referrer_info=AsyncMock(return_value='Нет'),
            _get_user_promo_group=AsyncMock(return_value=None),
            send_balance_topup_notification=AsyncMock(return_value=True),
        ),
    )

    with pytest.raises(RuntimeError, match='telegram unavailable'):
        await payment_service._send_paid_effect_notifications(
            db,
            bot=bot,
            user=SimpleNamespace(
                id=21,
                telegram_id=123_458,
                language='ru',
                balance_kopeks=10_000,
                referred_by_id=None,
            ),
            transaction=SimpleNamespace(id=47, amount_kopeks=10_000),
            intent=SimpleNamespace(public_id='intent-3', subscription_id=None),
            payment=SimpleNamespace(metadata_json={'device_addon_attempt_id': 48}),
        )
