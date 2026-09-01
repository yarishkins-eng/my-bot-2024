"""Обнуление тестового аккаунта на НАСТОЯЩЕМ PostgreSQL.

Здесь проверяется то, чего подделанная сессия проверить не может: переживёт ли
удаление настоящие внешние ключи. Именно на них молча падает очистка при
`/start` — база запрещает удалить подписку, пока на ней висит снимок прав.

Запускается по требованию:
    TEST_ACCOUNT_RESET_TEST_DATABASE_URL=postgresql+asyncpg://... uv run pytest \
        tests/integration/test_test_account_reset_postgres.py -q
"""

from __future__ import annotations

import importlib
import os
import sys
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio


try:  # pragma: no cover - зависит от окружения
    import asyncpg

    if not hasattr(asyncpg, 'connect'):
        sys.modules.pop('asyncpg', None)
        asyncpg = importlib.import_module('asyncpg')
except ModuleNotFoundError:  # pragma: no cover
    asyncpg = None

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.database.models import (
    AccountErasureRequest,
    Base,
    CheckoutPaymentAttempt,
    GuestPurchase,
    LavaPayment,
    PlategaPayment,
    ReferralEarning,
    ServerSquad,
    Subscription,
    SubscriptionCheckout,
    SubscriptionEntitlementSnapshot,
    SubscriptionServer,
    Tariff,
    Transaction,
    User,
    UserStatus,
)
from app.services import user_service


DATABASE_URL = os.getenv('TEST_ACCOUNT_RESET_TEST_DATABASE_URL')
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not DATABASE_URL,
        reason='TEST_ACCOUNT_RESET_TEST_DATABASE_URL is required for the PostgreSQL reset tests',
    ),
]


async def _panel_ok(user, panel_uuids):
    """Панель в этих тестах не участвует: её путь проверяется отдельно."""
    return True


STAND_TELEGRAM_ID = 7749231125
OUTSIDER_TELEGRAM_ID = 999000111


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine(DATABASE_URL, poolclass=None)
    async with engine.begin() as connection:
        # `drop_all` не умеет разложить цикл subscription_checkouts <-> transactions;
        # для одноразовой базы проще снести схему целиком.
        await connection.execute(text('DROP SCHEMA public CASCADE'))
        await connection.execute(text('CREATE SCHEMA public'))
        await connection.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as db:
        yield db
    await engine.dispose()


async def _seed_person(db, telegram_id: int, *, balance_kopeks: int, checkout_state: str = 'ready') -> User:
    """Человек со всем, что накапливает стенд: подписка, снимок прав, заказ, деньги."""
    now = datetime.now(UTC)
    user = User(
        telegram_id=telegram_id,
        username=f'person{telegram_id}',
        status=UserStatus.ACTIVE.value,
        balance_kopeks=balance_kopeks,
        has_had_paid_subscription=True,
        remnawave_uuid=f'panel-{telegram_id}',
    )
    db.add(user)
    await db.flush()

    squad = ServerSquad(squad_uuid=str(uuid.uuid4()), display_name=f'squad {telegram_id}', current_users=5)
    tariff = Tariff(name=f'tariff {telegram_id}')
    db.add_all([squad, tariff])
    await db.flush()

    subscription = Subscription(
        user_id=user.id,
        end_date=now + timedelta(days=30),
        is_trial=False,
        remnawave_short_id=f'sid{telegram_id}'[:16],
    )
    db.add(subscription)
    await db.flush()

    db.add_all(
        [
            # Ровно эта строка сегодня валит удаление подписки во всём коде.
            SubscriptionEntitlementSnapshot(
                subscription_id=subscription.id,
                location_ids=[1],
                technical_squad_uuids=[squad.squad_uuid],
                policy_revision=1,
                provenance='test',
                snapshot_hash=f'hash-{telegram_id}',
            ),
            SubscriptionServer(subscription_id=subscription.id, server_squad_id=squad.id),
        ]
    )

    checkout = SubscriptionCheckout(
        public_id=f'co-{telegram_id}',
        user_id=user.id,
        tariff_id=tariff.id,
        period_days=30,
        selected_device_limit=1,
        quoted_price_kopeks=19900,
        max_price_kopeks=19900,
        pricing_revision=1,
        quote_expires_at=now + timedelta(minutes=30),
        expires_at=now + timedelta(minutes=30),
        lifecycle_state=checkout_state,
    )
    payment = PlategaPayment(
        user_id=user.id,
        correlation_id=f'corr-{telegram_id}',
        amount_kopeks=19900,
        payment_method_code=1,
        status='CONFIRMED',
        is_paid=True,
    )
    transaction = Transaction(user_id=user.id, type='deposit', amount_kopeks=19900)
    db.add_all([checkout, payment, transaction])
    await db.flush()

    db.add(
        CheckoutPaymentAttempt(
            checkout_id=checkout.id,
            merchant_order_key=f'key-{telegram_id}',
            method_key='sbp',
            provider_method_code=1,
            requested_amount_kopeks=19900,
            status='failed',
            platega_payment_id=payment.id,
        )
    )
    await db.commit()
    await db.refresh(user)
    return user


async def _counts(db, user_id: int) -> dict[str, int]:
    async def one(model, column):
        return int(await db.scalar(select(func.count()).select_from(model).where(column == user_id)) or 0)

    return {
        'subscriptions': await one(Subscription, Subscription.user_id),
        'checkouts': await one(SubscriptionCheckout, SubscriptionCheckout.user_id),
        'payments': await one(PlategaPayment, PlategaPayment.user_id),
        'transactions': await one(Transaction, Transaction.user_id),
        'snapshots': int(
            await db.scalar(
                select(func.count())
                .select_from(SubscriptionEntitlementSnapshot)
                .join(Subscription, Subscription.id == SubscriptionEntitlementSnapshot.subscription_id)
                .where(Subscription.user_id == user_id)
            )
            or 0
        ),
    }


async def test_reset_removes_the_stand_and_leaves_everyone_else_alone(session, monkeypatch) -> None:
    monkeypatch.setattr(settings, 'TEST_ACCOUNT_TELEGRAM_IDS', str(STAND_TELEGRAM_ID))
    stand = await _seed_person(session, STAND_TELEGRAM_ID, balance_kopeks=22450)
    outsider = await _seed_person(session, OUTSIDER_TELEGRAM_ID, balance_kopeks=50000)

    # Живой клиент, которого стенд когда-то пригласил: его строку не трогаем.
    invited = User(telegram_id=555444333, username='invited', referred_by_id=stand.id)
    session.add(invited)
    await session.flush()
    session.add(ReferralEarning(user_id=stand.id, referral_id=invited.id, amount_kopeks=5000, reason='signup'))
    await session.commit()

    deleted_panel_uuids: list[str] = []

    async def _fake_panel_delete(user, panel_uuids):
        deleted_panel_uuids.extend(panel_uuids)
        return True

    monkeypatch.setattr(user_service, '_test_reset_delete_panel_identity', _fake_panel_delete)

    before_outsider = await _counts(session, outsider.id)
    squad_users_before = await session.scalar(select(func.sum(ServerSquad.current_users)))

    # Первое нажатие: только показать.
    preview = await user_service.reset_test_account(session, stand, admin_id=1, confirm=False)
    assert preview.allowed is True
    assert preview.blocked_reason is None
    assert preview.done is False
    assert preview.balance_kopeks == 22450
    assert preview.orders == 1
    assert preview.payments == 1
    assert preview.invited_users == 1
    assert preview.panel_linked is True
    assert deleted_panel_uuids == []
    assert (await _counts(session, stand.id))['subscriptions'] == 1

    # Второе: снести.
    done = await user_service.reset_test_account(session, stand, admin_id=1, confirm=True)
    assert done.done is True
    assert done.panel_deleted is True
    assert deleted_panel_uuids == [f'panel-{STAND_TELEGRAM_ID}']

    after_stand = await _counts(session, stand.id)
    assert after_stand == {
        'subscriptions': 0,
        'checkouts': 0,
        'payments': 0,
        'transactions': 0,
        'snapshots': 0,
    }
    await session.refresh(stand)
    assert stand.balance_kopeks == 0
    assert stand.remnawave_uuid is None
    assert stand.has_had_paid_subscription is False
    assert stand.status == UserStatus.DELETED.value
    assert stand.account_erasure_requested_at is None
    # Тот же Телеграм, та же строка: `/start` заведёт его заново.
    assert stand.telegram_id == STAND_TELEGRAM_ID

    # Никто другой не задет — ни его данные, ни счётчик общего сервера.
    assert await _counts(session, outsider.id) == before_outsider
    await session.refresh(outsider)
    assert outsider.balance_kopeks == 50000
    assert outsider.status == UserStatus.ACTIVE.value
    await session.refresh(invited)
    assert invited.referred_by_id == stand.id
    squad_users_after = await session.scalar(select(func.sum(ServerSquad.current_users)))
    assert squad_users_after == squad_users_before - 1


async def test_reset_refuses_while_an_order_is_still_in_flight(session, monkeypatch) -> None:
    monkeypatch.setattr(settings, 'TEST_ACCOUNT_TELEGRAM_IDS', str(STAND_TELEGRAM_ID))
    stand = await _seed_person(session, STAND_TELEGRAM_ID, balance_kopeks=1000, checkout_state='awaiting_funds')

    plan = await user_service.reset_test_account(session, stand, admin_id=1, confirm=True)

    assert plan.allowed is False
    assert plan.done is False
    assert 'не закончен' in (plan.blocked_reason or '')
    assert (await _counts(session, stand.id))['subscriptions'] == 1
    await session.refresh(stand)
    assert stand.balance_kopeks == 1000
    assert stand.status == UserStatus.ACTIVE.value


async def test_reset_refuses_when_the_provider_has_not_answered_yet(session, monkeypatch) -> None:
    monkeypatch.setattr(settings, 'TEST_ACCOUNT_TELEGRAM_IDS', str(STAND_TELEGRAM_ID))
    stand = await _seed_person(session, STAND_TELEGRAM_ID, balance_kopeks=1000)
    await session.execute(
        text("UPDATE platega_payments SET status = 'VERIFYING', is_paid = false WHERE user_id = :uid"),
        {'uid': stand.id},
    )
    await session.commit()

    plan = await user_service.reset_test_account(session, stand, admin_id=1, confirm=True)

    assert plan.allowed is False
    assert 'не досверен' in (plan.blocked_reason or '')
    assert '199.00' in (plan.blocked_reason or '')
    assert (await _counts(session, stand.id))['subscriptions'] == 1


async def test_reset_refuses_a_staff_account_even_if_it_is_on_the_list(session, monkeypatch) -> None:
    """Забор №2. Ровно тот случай, где список сам себя отменить не может."""
    monkeypatch.setattr(settings, 'TEST_ACCOUNT_TELEGRAM_IDS', str(STAND_TELEGRAM_ID))
    stand = await _seed_person(session, STAND_TELEGRAM_ID, balance_kopeks=1000)
    await session.execute(
        text(
            'INSERT INTO admin_roles (id, name, level, permissions, is_system, is_active) '
            'VALUES (1, :name, 50, :perms, false, true)'
        ),
        {'name': 'Модератор', 'perms': '{}'},
    )
    await session.execute(
        text('INSERT INTO user_roles (user_id, role_id, is_active) VALUES (:uid, 1, true)'),
        {'uid': stand.id},
    )
    await session.commit()

    plan = await user_service.reset_test_account(session, stand, admin_id=1, confirm=True)

    assert plan.allowed is False
    assert 'служебная роль' in (plan.blocked_reason or '')
    assert (await _counts(session, stand.id))['subscriptions'] == 1


async def test_nothing_changes_when_the_panel_refuses_to_delete(session, monkeypatch) -> None:
    monkeypatch.setattr(settings, 'TEST_ACCOUNT_TELEGRAM_IDS', str(STAND_TELEGRAM_ID))
    stand = await _seed_person(session, STAND_TELEGRAM_ID, balance_kopeks=22450)

    async def _panel_says_no(user, panel_uuids):
        return False

    monkeypatch.setattr(user_service, '_test_reset_delete_panel_identity', _panel_says_no)
    squad_users_before = await session.scalar(select(func.sum(ServerSquad.current_users)))
    stand_id = stand.id

    plan = await user_service.reset_test_account(session, stand, admin_id=1, confirm=True)

    assert plan.done is False
    assert plan.allowed is False
    assert 'ничего не тронуто' in (plan.blocked_reason or '').lower()
    assert (await _counts(session, stand_id))['subscriptions'] == 1
    stand = await session.get(User, stand_id)
    assert stand.balance_kopeks == 22450
    assert stand.status == UserStatus.ACTIVE.value
    assert await session.scalar(select(func.sum(ServerSquad.current_users))) == squad_users_before


async def test_a_successful_purchase_does_not_lock_the_button(session, monkeypatch) -> None:
    """Главная ловушка: `paid_processing` остаётся у успешной продажи навсегда.

    Если считать его «деньгами в пути», кнопка ломается ровно на том стенде,
    где владелец впервые довёл покупку до конца.
    """
    monkeypatch.setattr(settings, 'TEST_ACCOUNT_TELEGRAM_IDS', str(STAND_TELEGRAM_ID))
    stand = await _seed_person(session, STAND_TELEGRAM_ID, balance_kopeks=0)
    await session.execute(
        text('UPDATE checkout_payment_attempts SET status = :st'),
        {'st': 'paid_processing'},
    )
    await session.commit()
    monkeypatch.setattr(user_service, '_test_reset_delete_panel_identity', _panel_ok)

    plan = await user_service.reset_test_account(session, stand, admin_id=1, confirm=True)

    assert plan.blocked_reason is None
    assert plan.done is True
    assert (await _counts(session, stand.id))['subscriptions'] == 0


async def test_an_abandoned_invoice_does_not_lock_the_button(session, monkeypatch) -> None:
    """Открыл оплату и закрыл вкладку — счёт протух. Это не деньги в пути."""
    monkeypatch.setattr(settings, 'TEST_ACCOUNT_TELEGRAM_IDS', str(STAND_TELEGRAM_ID))
    stand = await _seed_person(session, STAND_TELEGRAM_ID, balance_kopeks=0)
    await session.execute(text("UPDATE platega_payments SET status = 'EXPIRED', is_paid = false"))
    await session.commit()
    monkeypatch.setattr(user_service, '_test_reset_delete_panel_identity', _panel_ok)

    plan = await user_service.reset_test_account(session, stand, admin_id=1, confirm=True)

    assert plan.blocked_reason is None
    assert plan.done is True


async def test_a_finished_closure_does_not_lock_the_button_forever(session, monkeypatch) -> None:
    """Строка заявки на закрытие не удаляется никогда — запирать по её
    наличию значило бы убить кнопку первым же нажатием «удалить»."""
    monkeypatch.setattr(settings, 'TEST_ACCOUNT_TELEGRAM_IDS', str(STAND_TELEGRAM_ID))
    stand = await _seed_person(session, STAND_TELEGRAM_ID, balance_kopeks=0)
    session.add(AccountErasureRequest(user_id=stand.id, state='completed'))
    await session.commit()
    monkeypatch.setattr(user_service, '_test_reset_delete_panel_identity', _panel_ok)

    plan = await user_service.reset_test_account(session, stand, admin_id=1, confirm=True)

    assert plan.done is True

    # А незакрытая — запирает.
    other = await _seed_person(session, OUTSIDER_TELEGRAM_ID, balance_kopeks=0)
    monkeypatch.setattr(settings, 'TEST_ACCOUNT_TELEGRAM_IDS', str(OUTSIDER_TELEGRAM_ID))
    session.add(AccountErasureRequest(user_id=other.id, state='awaiting_manual_resolution'))
    await session.commit()

    blocked = await user_service.reset_test_account(session, other, admin_id=1, confirm=True)
    assert blocked.done is False
    assert 'закрывается' in (blocked.blocked_reason or '')


async def test_a_paid_gift_of_a_real_buyer_survives(session, monkeypatch) -> None:
    """`SET NULL` на ссылке — указание схемы «строка переживает человека»."""
    monkeypatch.setattr(settings, 'TEST_ACCOUNT_TELEGRAM_IDS', str(STAND_TELEGRAM_ID))
    stand = await _seed_person(session, STAND_TELEGRAM_ID, balance_kopeks=0)
    buyer = User(telegram_id=444333222, username='buyer')
    session.add(buyer)
    await session.flush()
    # Подарок купил ЖИВОЙ человек, активировал — стенд.
    session.add(
        GuestPurchase(
            buyer_user_id=buyer.id,
            user_id=stand.id,
            amount_kopeks=19900,
            status='paid',
            token=f'gift-{STAND_TELEGRAM_ID}',
            contact_type='telegram',
            contact_value=str(STAND_TELEGRAM_ID),
            period_days=30,
        )
    )
    await session.commit()
    monkeypatch.setattr(user_service, '_test_reset_delete_panel_identity', _panel_ok)
    stand_id = stand.id

    plan = await user_service.reset_test_account(session, stand, admin_id=1, confirm=True)

    # Кнопка честно отказывается: про подарки она судить не умеет.
    assert plan.done is False
    assert 'подарки' in (plan.blocked_reason or '')
    assert await session.scalar(select(func.count()).select_from(GuestPurchase)) == 1
    assert (await _counts(session, stand_id))['subscriptions'] == 1


async def test_referrers_earning_does_not_deadlock_the_reset(session, monkeypatch) -> None:
    """Строку заработка реферера мы храним — а она ссылается на транзакцию стенда.

    База запрещает удалить транзакцию, пока ссылка жива (`NO ACTION`). Без
    обнуления ссылки обнуление падало бы КАЖДЫЙ раз, уже после удаления
    пользователя из панели, — то есть стенд оставался бы заперт навсегда.
    """
    monkeypatch.setattr(settings, 'TEST_ACCOUNT_TELEGRAM_IDS', str(STAND_TELEGRAM_ID))
    stand = await _seed_person(session, STAND_TELEGRAM_ID, balance_kopeks=0)
    referrer = User(telegram_id=111222333, username='referrer')
    session.add(referrer)
    await session.flush()
    stand_transaction = await session.scalar(select(Transaction.id).where(Transaction.user_id == stand.id))
    session.add(
        ReferralEarning(
            user_id=referrer.id,
            referral_id=stand.id,
            amount_kopeks=15000,
            reason='referral_first_topup',
            referral_transaction_id=stand_transaction,
        )
    )
    await session.commit()
    monkeypatch.setattr(user_service, '_test_reset_delete_panel_identity', _panel_ok)
    referrer_id = referrer.id

    plan = await user_service.reset_test_account(session, stand, admin_id=1, confirm=True)

    assert plan.done is True, plan.blocked_reason
    # Заработок реферера цел, только указатель на снесённую транзакцию снят.
    earning = (
        await session.execute(select(ReferralEarning).where(ReferralEarning.user_id == referrer_id))
    ).scalar_one()
    assert earning.amount_kopeks == 15000
    assert earning.referral_transaction_id is None


async def test_a_sleeping_payment_gateway_row_locks_the_button(session, monkeypatch) -> None:
    """Кассы с `SET NULL` не сносятся, но держат транзакции ссылкой `NO ACTION`.

    Значит про них нельзя ни судить, ни молчать: строка есть — отказ.
    """
    monkeypatch.setattr(settings, 'TEST_ACCOUNT_TELEGRAM_IDS', str(STAND_TELEGRAM_ID))
    stand = await _seed_person(session, STAND_TELEGRAM_ID, balance_kopeks=0)
    session.add(LavaPayment(user_id=stand.id, order_id='lava-1', amount_kopeks=19900, status='pending'))
    await session.commit()
    monkeypatch.setattr(user_service, '_test_reset_delete_panel_identity', _panel_ok)

    plan = await user_service.reset_test_account(session, stand, admin_id=1, confirm=True)

    assert plan.done is False
    assert 'Lava' in (plan.blocked_reason or '')
    assert (await _counts(session, stand.id))['subscriptions'] == 1


async def test_an_admin_in_the_list_is_still_refused(session, monkeypatch) -> None:
    """Забор №2 существует ровно для ошибки, которую список отменить не может.

    Мутационный прогон показал, что эта строка не была защищена ничем: убери
    её — и админский аккаунт, случайно вписанный в список, обнулился бы молча.
    """
    monkeypatch.setattr(settings, 'TEST_ACCOUNT_TELEGRAM_IDS', str(STAND_TELEGRAM_ID))
    monkeypatch.setattr(settings, 'ADMIN_IDS', str(STAND_TELEGRAM_ID))
    monkeypatch.setattr(user_service, '_test_reset_delete_panel_identity', _panel_ok)
    stand = await _seed_person(session, STAND_TELEGRAM_ID, balance_kopeks=1000)

    plan = await user_service.reset_test_account(session, stand, admin_id=1, confirm=True)

    assert plan.done is False
    assert 'админский' in (plan.blocked_reason or '').lower()
    assert (await _counts(session, stand.id))['subscriptions'] == 1


async def test_a_support_moderator_is_still_refused(session, monkeypatch) -> None:
    """Третий реестр служебных людей живёт в файле, а не в базе."""
    from app.services.support_settings_service import SupportSettingsService

    monkeypatch.setattr(settings, 'TEST_ACCOUNT_TELEGRAM_IDS', str(STAND_TELEGRAM_ID))
    monkeypatch.setattr(SupportSettingsService, 'is_moderator', classmethod(lambda cls, tid: True))
    monkeypatch.setattr(user_service, '_test_reset_delete_panel_identity', _panel_ok)
    stand = await _seed_person(session, STAND_TELEGRAM_ID, balance_kopeks=1000)

    plan = await user_service.reset_test_account(session, stand, admin_id=1, confirm=True)

    assert plan.done is False
    assert 'модератор' in (plan.blocked_reason or '').lower()


async def test_a_staff_role_is_refused_for_the_right_reason(session, monkeypatch) -> None:
    """Панель подменена намеренно: иначе тест краснел бы из-за неё, а не из-за роли."""
    monkeypatch.setattr(settings, 'TEST_ACCOUNT_TELEGRAM_IDS', str(STAND_TELEGRAM_ID))
    monkeypatch.setattr(user_service, '_test_reset_delete_panel_identity', _panel_ok)
    stand = await _seed_person(session, STAND_TELEGRAM_ID, balance_kopeks=1000)
    await session.execute(
        text(
            'INSERT INTO admin_roles (id, name, level, permissions, is_system, is_active) '
            'VALUES (1, :name, 50, :perms, false, true)'
        ),
        {'name': 'Модератор', 'perms': '{}'},
    )
    await session.execute(
        text('INSERT INTO user_roles (user_id, role_id, is_active) VALUES (:uid, 1, true)'),
        {'uid': stand.id},
    )
    await session.commit()

    plan = await user_service.reset_test_account(session, stand, admin_id=1, confirm=True)

    assert 'служебная роль' in (plan.blocked_reason or '')


async def test_a_used_promocode_gets_its_slot_back(session, monkeypatch) -> None:
    """Иначе у кода с лимитом 1 после первой проверки не осталось бы мест."""
    from app.database.models import PromoCode, PromoCodeUse

    monkeypatch.setattr(settings, 'TEST_ACCOUNT_TELEGRAM_IDS', str(STAND_TELEGRAM_ID))
    monkeypatch.setattr(user_service, '_test_reset_delete_panel_identity', _panel_ok)
    stand = await _seed_person(session, STAND_TELEGRAM_ID, balance_kopeks=0)
    promocode = PromoCode(code='TEST1', type='balance', max_uses=1, current_uses=1)
    session.add(promocode)
    await session.flush()
    session.add(PromoCodeUse(promocode_id=promocode.id, user_id=stand.id))
    await session.commit()
    promocode_id = promocode.id

    plan = await user_service.reset_test_account(session, stand, admin_id=1, confirm=True)

    assert plan.done is True, plan.blocked_reason
    assert await session.scalar(select(PromoCode.current_uses).where(PromoCode.id == promocode_id)) == 0


async def test_prices_look_like_a_newcomers_again(session, monkeypatch) -> None:
    """Стенд со скидочной промо-группой показал бы не те цены, что новичок.

    Ровно это расхождение и сорвало приёмку, из-за которой инструмент затеян.
    """
    from app.database.models import PromoGroup

    monkeypatch.setattr(settings, 'TEST_ACCOUNT_TELEGRAM_IDS', str(STAND_TELEGRAM_ID))
    monkeypatch.setattr(user_service, '_test_reset_delete_panel_identity', _panel_ok)
    default_group = PromoGroup(name='Базовая', is_default=True)
    discount_group = PromoGroup(name='Скидка 50', is_default=False)
    session.add_all([default_group, discount_group])
    await session.flush()
    stand = await _seed_person(session, STAND_TELEGRAM_ID, balance_kopeks=0)
    stand.promo_group_id = discount_group.id
    stand.has_made_first_topup = True
    stand.auto_promo_group_assigned = True
    await session.commit()
    default_id, stand_id = default_group.id, stand.id

    plan = await user_service.reset_test_account(session, stand, admin_id=1, confirm=True)

    assert plan.done is True, plan.blocked_reason
    refreshed = await session.get(User, stand_id)
    assert refreshed.promo_group_id == default_id
    assert refreshed.has_made_first_topup is False
    assert refreshed.auto_promo_group_assigned is False
