"""ИСПР-1: реальное решение/SQL, деньги в SQLite, внешняя покупка и отправки подменены."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text

from app.config import settings
from app.database.models import DeviceAddonIntent, PlategaPayment, Transaction, User
from app.services import device_first_checkout_service as dfc
from app.services.admin_notification_service import AdminNotificationService, OwnerCartHint
from tests.cabinet.test_vk16_topup_autocomplete import (  # noqa: F401
    TRIAL,
    _add_checkout,
    _complete,
    _paid,
    _pay,
    buy,
    checkouts,
    webhook,
)
from tests.cabinet.test_vk16_topup_intent import (  # noqa: F401
    _decide,
    _intent_payment,
    _set_user,
    db,
    env,
    session,
)


@pytest.fixture(autouse=True)
def addon_table(session):
    columns = ', '.join(
        'id INTEGER PRIMARY KEY' if c.name == 'id' else c.name for c in DeviceAddonIntent.__table__.columns
    )
    session.execute(text(f'CREATE TABLE IF NOT EXISTS device_addon_intents ({columns})'))
    session.commit()


async def test_interrupted_own_charge_returns_the_same_order_without_another_purchase(session, buy, env):
    _paid(session, charging_checkout_id=400)
    _add_checkout(
        session,
        checkout_id=400,
        source='topup_intent',
        lifecycle_state='fulfilling',
        financial_committed_at=datetime.now(UTC),
        funding_mode='wallet',
    )
    env.open_checkout.return_value = session.get(dfc.SubscriptionCheckout, 400)
    before = session.get(User, 1).balance_kopeks

    final, _ = await _complete(session)

    assert (final['status'], final['checkout_public_id']) == ('fulfilled', 'chk-400')
    assert buy.create == buy.commit == []
    assert session.get(User, 1).balance_kopeks == before


async def test_charge_marker_is_in_the_same_transaction_before_debit(session, buy):
    _paid(session)

    async def debit(checkout):
        assert dfc.topup_intent_of(session.get(PlategaPayment, 97))['charging_checkout_id'] == checkout.id
        checkout.financial_committed_at = datetime.now(UTC)
        session.commit()
        return checkout

    buy.commit_effect = debit
    final, _ = await _complete(session)
    assert final['status'] == 'fulfilled'


async def test_transient_read_failure_cannot_turn_own_charge_into_a_terminal_refusal(session, buy, monkeypatch):
    _paid(session, charging_checkout_id=400)
    _add_checkout(
        session,
        checkout_id=400,
        source='topup_intent',
        lifecycle_state='fulfilling',
        financial_committed_at=datetime.now(UTC),
        funding_mode='wallet',
    )
    monkeypatch.setattr(
        dfc, '_topup_intent_paid_reference', AsyncMock(side_effect=RuntimeError('temporary read failure'))
    )
    final, _ = await _complete(session)
    assert (final['status'], final['checkout_public_id']) == ('fulfilled', 'chk-400')
    assert buy.create == buy.commit == []


@pytest.mark.parametrize(
    ('description', 'addon', 'checkout', 'purchase'),
    [
        ('Покупка доп. устройств: 1 устройство', False, False, False),
        ('Докупка 10 ГБ трафика', False, False, False),
        ('Добавление 10 ГБ трафика', False, False, False),
        ('Суточная оплата тарифа «Базовый»', False, False, False),
        ('старый неизвестный текст', True, False, False),
        ('Продление тарифа «Базовый»', False, False, True),
        ('Покупка тарифа «10 устройств / трафик»', False, False, True),
        (None, False, False, True),
        ('', False, False, True),
        ('Покупка доп. устройств: тариф', False, True, True),
    ],
)
async def test_only_anchored_addons_are_excluded_everywhere(db, session, buy, description, addon, checkout, purchase):
    _paid(session)
    now = datetime.now(UTC)
    row = Transaction(
        user_id=1,
        type='subscription_payment',
        amount_kopeks=5000,
        description=description,
        created_at=now,
        device_first_checkout_id=700 if checkout else None,
    )
    session.add(row)
    session.commit()
    if addon:
        session.execute(text('INSERT INTO device_addon_intents (id, transaction_id) VALUES (1, :tid)'), {'tid': row.id})
        session.commit()
    assert await dfc._bought_since(db, user_id=1, since=now - timedelta(minutes=1)) is purchase
    assert (await dfc.recent_purchase_summary(db, user_id=1) is not None) is purchase
    if not purchase:
        await dfc.assert_recent_purchase_confirmed(
            db, user_id=1, purchase_context='chat_autostart', confirmed_purchase_id=None
        )
    final, _ = await _complete(session)
    assert final['status'] == ('refused' if purchase else 'fulfilled')
    if purchase:
        assert final['reason'] == 'already_purchased'


@pytest.mark.parametrize(
    ('paid_minutes', 'deposit_minutes', 'fulfilled'), [(50, 70, True), (65, 65, False), (80, 50, True)]
)
async def test_hour_is_until_money_arrival_and_future_paid_at_is_ignored(
    session, buy, paid_minutes, deposit_minutes, fulfilled
):
    now = datetime.now(UTC)
    chosen = now - timedelta(minutes=70)
    payment = _paid(session, created_ago=timedelta(minutes=70))
    transaction = Transaction(
        user_id=1, type='deposit', amount_kopeks=9900, completed_at=chosen + timedelta(minutes=deposit_minutes)
    )
    session.add(transaction)
    session.flush()
    payment.transaction_id = transaction.id
    payment.paid_at = chosen + timedelta(minutes=paid_minutes)
    session.commit()
    final, _ = await _complete(session)
    assert final['status'] == ('fulfilled' if fulfilled else 'refused')
    if not fulfilled:
        assert final['reason'] == 'expired'


@pytest.mark.parametrize('provider', ['missing', 'before_intent', 'no_zone'])
async def test_implausible_or_missing_provider_time_uses_deposit(db, session, provider):
    from app.services.payment.platega import _OWNER_REFUSAL_WHY

    payment = _paid(session)
    chosen = dfc._intent_time(dfc.topup_intent_of(payment)['created_at'])
    arrived = chosen + timedelta(minutes=50)
    transaction = Transaction(user_id=1, type='deposit', amount_kopeks=9900, completed_at=arrived)
    session.add(transaction)
    session.flush()
    payment.transaction_id = transaction.id
    if provider == 'before_intent':
        payment.paid_at = chosen - timedelta(minutes=10)
    elif provider == 'no_zone':
        payment.paid_at = chosen + timedelta(minutes=20)
        payment.callback_payload = {'paidAt': payment.paid_at.replace(tzinfo=None).isoformat()}
    session.commit()
    assert await dfc._topup_intent_paid_reference(db, payment, dfc.topup_intent_of(payment)) == arrived
    assert _OWNER_REFUSAL_WHY['expired'] == 'деньги пришли позже часа после выбора заказа'


@pytest.mark.parametrize('referral_error', [False, True])
@pytest.mark.parametrize('refused', [False, True])
@pytest.mark.parametrize('telegram', [True, False])
async def test_bonus_is_in_receipt_email_and_owner_card(
    db, session, webhook, monkeypatch, referral_error, refused, telegram
):
    _set_user(
        session,
        balance_kopeks=4900,
        has_made_first_topup=True,
        referred_by_id=2,
        telegram_id=777001 if telegram else None,
    )
    _intent_payment(session, payment_id=97, amount_kopeks=10000, **TRIAL)
    if refused:
        await dfc.cancel_topup_intents(db, user_id=1)
    messages = []

    async def referral(*args):
        if referral_error:
            raise RuntimeError('referral unavailable')
        session.execute(text('UPDATE users SET balance_kopeks=balance_kopeks+10000 WHERE id=1'))
        session.add(
            Transaction(
                user_id=1,
                type='referral_reward',
                amount_kopeks=10000,
                description='Бонус новичка за первую оплату',
                is_completed=True,
            )
        )
        session.commit()

    class Admin:
        def __init__(self, bot):
            pass

        async def send_balance_topup_notification(self, user, transaction, old_balance, **kwargs):
            messages.append(
                AdminNotificationService._build_balance_topup_message(
                    AdminNotificationService(webhook.bot),
                    user,
                    transaction,
                    old_balance,
                    topup_status=kwargs['topup_status'],
                    referrer_info='',
                    subscription=None,
                    promo_group=None,
                    referred_bonus_kopeks=kwargs['referred_bonus_kopeks'],
                    cart_hint=OwnerCartHint(
                        text=kwargs['intent_refused'] if refused else kwargs['auto_next_step'],
                        auto=not refused,
                        final=refused,
                    ),
                )
            )

    monkeypatch.setattr('app.services.referral_service.process_referral_topup', referral)
    monkeypatch.setattr('app.services.admin_notification_service.AdminNotificationService', Admin)
    assert await _pay(db, webhook)
    left = (14900 if refused else 0) + (0 if referral_error else 10000)
    if telegram:
        assert settings.format_price(left) in webhook.bot.send_message.await_args.args[1]
    else:
        webhook.bot.send_message.assert_not_awaited()
    assert webhook.email.await_args.kwargs['balance_kopeks'] == left
    assert f'было {settings.format_price(4900)}' in messages[0]
    assert f'стало {settings.format_price(left)}' in messages[0]
    if not referral_error:
        assert ('(в т.ч. бонус за приглашение 100 ₽)' if refused else '(бонус за приглашение)') in messages[0]


@pytest.mark.parametrize('refused', [False, True])
async def test_an_unrelated_balance_increase_is_not_called_a_referral_bonus(db, session, webhook, monkeypatch, refused):
    _intent_payment(session, payment_id=97, amount_kopeks=10000, **TRIAL)
    if refused:
        await dfc.cancel_topup_intents(db, user_id=1)
    messages = []

    class Admin:
        def __init__(self, bot):
            pass

        async def send_balance_topup_notification(self, user, transaction, old_balance, **kwargs):
            messages.append(
                AdminNotificationService._build_balance_topup_message(
                    AdminNotificationService(webhook.bot),
                    user,
                    transaction,
                    old_balance,
                    topup_status=kwargs['topup_status'],
                    referrer_info='',
                    subscription=None,
                    promo_group=None,
                    referred_bonus_kopeks=kwargs['referred_bonus_kopeks'],
                    cart_hint=OwnerCartHint(
                        kwargs['intent_refused'] if refused else kwargs['auto_next_step'],
                        auto=not refused,
                        final=refused,
                    ),
                )
            )

    async def other_credit(*args):
        session.execute(text('UPDATE users SET balance_kopeks=balance_kopeks+10000 WHERE id=1'))
        session.commit()

    monkeypatch.setattr('app.services.referral_service.process_referral_topup', other_credit)
    monkeypatch.setattr('app.services.admin_notification_service.AdminNotificationService', Admin)
    await _pay(db, webhook)
    assert 'бонус' not in messages[0]


async def test_addon_does_not_ask_the_purchase_question_or_block_a2(db, session, env, monkeypatch):
    from app.handlers.subscription import device_first as handler

    session.add(
        Transaction(
            user_id=1,
            type='subscription_payment',
            amount_kopeks=5000,
            description='Покупка доп. устройств: 1 устройство',
            created_at=datetime.now(UTC),
        )
    )
    session.commit()
    assert (await _decide(db, session)).status == 'accepted'
    checkout = SimpleNamespace(public_id='normal-wallet')
    commit = AsyncMock(return_value=checkout)
    monkeypatch.setattr(
        handler,
        'create_or_resume_direct_checkout',
        AsyncMock(return_value=SimpleNamespace(checkout=checkout, proceed_to_payment=True)),
    )
    monkeypatch.setattr(handler, 'commit_direct_wallet_checkout', commit)
    monkeypatch.setattr(handler, '_render_checkout', AsyncMock())
    shown = int((datetime.now(UTC) - timedelta(minutes=1)).timestamp())
    callback = SimpleNamespace(data=f'df:a2:30:1:14900:{shown}', answer=AsyncMock())
    await handler.pay_wallet_fused(callback, session.get(User, 1), db, AsyncMock())
    commit.assert_awaited_once()


async def test_chat_autostart_api_after_addon_reaches_normal_price_validation(db, session, env, monkeypatch):
    from fastapi import HTTPException

    from app.cabinet.routes import device_first as route

    session.add(
        Transaction(
            user_id=1,
            type='subscription_payment',
            amount_kopeks=5000,
            description='Докупка 10 ГБ трафика',
            created_at=datetime.now(UTC),
        )
    )
    session.commit()
    monkeypatch.setattr(route, '_rate_limit', AsyncMock())
    monkeypatch.setattr(route, '_mutation', AsyncMock(return_value=(SimpleNamespace(checkout_id=None), None)))
    monkeypatch.setattr(route, 'get_open_checkout_for_user', AsyncMock(return_value=None))
    monkeypatch.setattr(route, 'store_mutation_result', AsyncMock())
    monkeypatch.setattr(dfc, 'device_first_new_checkouts_enabled', lambda: True)
    env.options.return_value.update(period_options=[30], device_options=[1, 2])
    # Неправильный период даёт обычную проверку цены после реального SQL-забора, а не ложный 409.
    with pytest.raises(HTTPException) as failure:
        await route.checkout_create(
            route.CheckoutCreateRequest(period_days=123, selected_device_limit=1, purchase_context='chat_autostart'),
            idempotency_key='addon-path',
            user=session.get(User, 1),
            db=db,
        )
    assert failure.value.status_code == 422
    assert failure.value.detail['code'] == 'invalid_selection'


async def test_uncommitted_charge_marker_and_debit_roll_back_together(session, buy):
    _paid(session)
    before = session.get(User, 1).balance_kopeks

    async def failed_debit(checkout):
        session.execute(text('UPDATE users SET balance_kopeks=balance_kopeks-14900 WHERE id=1'))
        session.flush()
        raise RuntimeError('crash before financial commit')

    buy.commit_effect = failed_debit
    final, stored = await _complete(session)
    assert final['reason'] == 'technical_error'
    assert stored.get('charging_checkout_id') is None
    assert session.get(User, 1).balance_kopeks == before
