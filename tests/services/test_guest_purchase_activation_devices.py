"""Этап ДУ-1: подарок поверх существующей подписки не понижает число устройств.

Дефект 2 внешнего ревью 13.09.2026 (мина LF, половина): ветка «продлить существующую с
остатком срока» в `activate_purchase` отдавала `device_limit=tariff.device_limit` (база
подарка = 1), `extend_subscription` присваивал его без `max()`, и панель тут же получала
урезанный лимит — оплаченные устройства отключались. Здесь закреплено правило кассы:
платная — никогда не ниже текущего, пробная — строго база подарка. Плюс забор ДУ-1б:
подарок нельзя применить к действующей подписке другого тарифа (иначе `extend_subscription`
делает смену тарифа и сжигает остаток срока бесплатного тарифа — Team «до 2031»).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services import guest_purchase_service as svc


TRIAL_TARIFF_ID = 5


@pytest.mark.parametrize(
    ('is_trial', 'tariff_id', 'current', 'base', 'expected'),
    [
        (False, 3, 3, 1, 3),  # платная с 3 устройствами: подарок «Базового» не режет
        (False, 3, 1, 1, 1),
        (False, 3, 2, 4, 4),  # база подарка выше текущего — растём до базы
        (False, 3, None, 1, 1),  # лимит не записан — база
        (None, 3, 3, 1, 3),  # is_trial = NULL считается платной, как в кассе
        (True, TRIAL_TARIFF_ID, 3, 1, 1),  # настоящая пробная: строго база, старые устройства не пол
        (True, TRIAL_TARIFF_ID, 1, 1, 1),
        (True, 4, 5, 1, 5),  # «пробная» по флагу на Team — друг владельца, не режем
        (True, 3, 4, 1, 4),  # «пробная» по флагу на «Базовом» (выдана руками) — не режем
    ],
)
def test_gift_extend_device_limit_never_lowers_a_paid_subscription(is_trial, tariff_id, current, base, expected):
    tariff = SimpleNamespace(device_limit=base)
    existing = SimpleNamespace(is_trial=is_trial, tariff_id=tariff_id, device_limit=current)
    assert svc._gift_extend_device_limit(tariff, existing, trial_tariff_id=TRIAL_TARIFF_ID) == expected


def test_without_a_trial_tariff_nobody_counts_as_a_trial():
    """Нет пробного тарифа — никого не считаем пробным (безопасная сторона: устройства не режем)."""
    existing = SimpleNamespace(is_trial=True, tariff_id=5, device_limit=3)
    assert svc._gift_extend_device_limit(SimpleNamespace(device_limit=1), existing, trial_tariff_id=None) == 3


# --- через настоящую activate_purchase, с подставной базой ---------------------------


def _purchase(**overrides):
    base = dict(
        id=11,
        token='T' * 64,
        status=svc.GuestPurchaseStatus.PENDING_ACTIVATION.value,
        recipient_warning=None,
        tariff_id=3,
        user_id=206,
        period_days=30,
        is_gift=True,
        cabinet_password=None,
        auto_login_token=None,
        subscription_url=None,
        subscription_crypto_link=None,
        delivered_at=None,
        gift_message=None,
        payment_method='platega',
        payment_id='p-1',
        amount_kopeks=14_900,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _user():
    return SimpleNamespace(
        id=206,
        auth_type='telegram',
        password_hash='x',
        email_verified=True,
        language='ru',
        account_erasure_requested_at=None,
    )


def _existing(*, tariff_id, is_trial, device_limit, days_left):
    return SimpleNamespace(
        id=142,
        tariff_id=tariff_id,
        is_trial=is_trial,
        device_limit=device_limit,
        end_date=datetime.now(UTC) + timedelta(days=days_left),
        subscription_url='https://sub/old',
        subscription_crypto_link=None,
    )


def _db(purchase, user):
    db = MagicMock()
    results = [
        SimpleNamespace(scalars=lambda: SimpleNamespace(first=lambda: purchase)),
        SimpleNamespace(scalars=lambda: SimpleNamespace(first=lambda: user)),
    ]
    db.execute = AsyncMock(side_effect=results)
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    db.refresh = AsyncMock()
    return db


def _wire(monkeypatch, *, existing, tariff):
    """Подставляем всё вокруг ветки продления; сама ветка и её условия — настоящие."""
    # Метод pydantic-настроек подменяется на классе (поле — на экземпляре), урок 19.08.
    monkeypatch.setattr(type(svc.settings), 'is_multi_tariff_enabled', lambda self: False)
    monkeypatch.setattr(svc, 'get_tariff_by_id', AsyncMock(return_value=tariff))
    monkeypatch.setattr(svc, 'get_trial_tariff', AsyncMock(return_value=SimpleNamespace(id=TRIAL_TARIFF_ID)))
    monkeypatch.setattr(svc, '_purchase_touches_financially_closing_account', AsyncMock(return_value=False))
    monkeypatch.setattr(svc, 'get_subscription_by_user_id', AsyncMock(return_value=existing))
    monkeypatch.setattr(
        'app.services.public_location_entitlement_service.resolve_tariff_entitlement',
        AsyncMock(return_value=SimpleNamespace(squad_uuids=('sq-de',))),
    )
    extended = SimpleNamespace(subscription_url='https://sub/new', subscription_crypto_link=None)
    extend = AsyncMock(return_value=extended)
    replace = AsyncMock(return_value=extended)
    monkeypatch.setattr(svc, 'extend_subscription', extend)
    monkeypatch.setattr(svc, 'replace_subscription', replace)
    monkeypatch.setattr(svc, 'create_paid_subscription', AsyncMock(return_value=extended))
    panel = AsyncMock(return_value=SimpleNamespace())
    monkeypatch.setattr(svc, 'SubscriptionService', lambda: SimpleNamespace(create_remnawave_user=panel))
    monkeypatch.setattr(svc, '_send_admin_notification', AsyncMock())
    return extend, replace


@pytest.mark.asyncio
async def test_gift_over_a_paid_subscription_keeps_its_three_devices(monkeypatch):
    tariff = SimpleNamespace(id=3, name='Базовый', device_limit=1, traffic_limit_gb=0)
    existing = _existing(tariff_id=3, is_trial=False, device_limit=3, days_left=40)
    extend, replace = _wire(monkeypatch, existing=existing, tariff=tariff)
    purchase, user = _purchase(), _user()

    result = await svc.activate_purchase(_db(purchase, user), purchase.token, skip_notification=True)

    extend.assert_awaited_once()
    kwargs = extend.await_args.kwargs
    assert kwargs['device_limit'] == 3, 'оплаченные устройства нельзя отнимать подарком'
    assert kwargs['tariff_id'] == 3
    assert extend.await_args.args[2] == 30, 'подарок продлевает ровно на купленные дни'
    replace.assert_not_awaited()
    assert result.status == svc.GuestPurchaseStatus.DELIVERED.value


@pytest.mark.asyncio
async def test_gift_over_a_trial_takes_exactly_the_gift_base(monkeypatch):
    tariff = SimpleNamespace(id=3, name='Базовый', device_limit=1, traffic_limit_gb=0)
    # Пробная на своём тарифе (5) и с большим лимитом: конверсия подарком разрешена,
    # но пробные устройства полом не становятся.
    existing = _existing(tariff_id=5, is_trial=True, device_limit=3, days_left=2)
    extend, _ = _wire(monkeypatch, existing=existing, tariff=tariff)
    purchase, user = _purchase(), _user()

    await svc.activate_purchase(_db(purchase, user), purchase.token, skip_notification=True)

    extend.assert_awaited_once()
    assert extend.await_args.kwargs['device_limit'] == 1


@pytest.mark.asyncio
async def test_gift_of_another_tariff_over_an_active_paid_subscription_is_refused(monkeypatch):
    """ДУ-1б: другу на Team (бесплатный, до 2031) подарок «Базового» стёр бы срок до 30 дней."""
    tariff = SimpleNamespace(id=3, name='Базовый', device_limit=1, traffic_limit_gb=0)
    existing = _existing(tariff_id=4, is_trial=False, device_limit=3, days_left=1500)
    extend, replace = _wire(monkeypatch, existing=existing, tariff=tariff)
    purchase, user = _purchase(), _user()
    db = _db(purchase, user)

    with pytest.raises(svc.GuestPurchaseError) as error:
        await svc.activate_purchase(db, purchase.token, skip_notification=True)

    assert error.value.status_code == 409
    assert 'другого тарифа' in error.value.message
    extend.assert_not_awaited()
    replace.assert_not_awaited()
    # Откат делает ветка `except GuestPurchaseError`, а не общий `except Exception` (тот дал бы 500).
    db.rollback.assert_awaited()
    assert purchase.status != svc.GuestPurchaseStatus.DELIVERED.value, 'подарок не выдан, деньги дарителя в покупке'


@pytest.mark.asyncio
async def test_gift_over_an_expired_subscription_starts_fresh_on_the_gift_base(monkeypatch):
    """Решение владельца 1А (14.09.2026): истёкшая подписка получает свежий месяц на базе подарка."""
    tariff = SimpleNamespace(id=3, name='Базовый', device_limit=1, traffic_limit_gb=0)
    existing = _existing(tariff_id=3, is_trial=False, device_limit=3, days_left=-5)
    extend, replace = _wire(monkeypatch, existing=existing, tariff=tariff)
    purchase, user = _purchase(), _user()

    await svc.activate_purchase(_db(purchase, user), purchase.token, skip_notification=True)

    extend.assert_not_awaited()
    replace.assert_awaited_once()
    assert replace.await_args.kwargs['device_limit'] == 1


@pytest.mark.asyncio
async def test_multi_tariff_branch_keeps_paid_devices_too(monkeypatch):
    """Спящая мультитарифная ветка (мутация M6): то же правило, что у живой."""
    tariff = SimpleNamespace(id=3, name='Базовый', device_limit=1, traffic_limit_gb=0)
    existing = _existing(tariff_id=3, is_trial=False, device_limit=3, days_left=40)
    extend, _ = _wire(monkeypatch, existing=existing, tariff=tariff)
    monkeypatch.setattr(type(svc.settings), 'is_multi_tariff_enabled', lambda self: True)
    monkeypatch.setattr(
        'app.database.crud.subscription.get_subscription_by_user_and_tariff', AsyncMock(return_value=existing)
    )
    purchase, user = _purchase(), _user()

    await svc.activate_purchase(_db(purchase, user), purchase.token, skip_notification=True)

    extend.assert_awaited_once()
    assert extend.await_args.kwargs['device_limit'] == 3


@pytest.mark.asyncio
async def test_team_friend_flagged_as_trial_is_refused_too(monkeypatch):
    """Критик полноты: 21 из 31 подписки Team несёт is_trial=True «до 2031» — флаг не критерий."""
    tariff = SimpleNamespace(id=3, name='Базовый', device_limit=1, traffic_limit_gb=0)
    existing = _existing(tariff_id=4, is_trial=True, device_limit=3, days_left=1500)
    extend, replace = _wire(monkeypatch, existing=existing, tariff=tariff)
    purchase, user = _purchase(), _user()

    with pytest.raises(svc.GuestPurchaseError) as error:
        await svc.activate_purchase(_db(purchase, user), purchase.token, skip_notification=True)

    assert error.value.status_code == 409
    extend.assert_not_awaited()
    replace.assert_not_awaited()


@pytest.mark.asyncio
async def test_classic_subscription_without_a_tariff_still_takes_the_gift(monkeypatch):
    """Мутация MX1: классическую подписку (tariff_id IS NULL) забор пропускает — как до этапа."""
    tariff = SimpleNamespace(id=3, name='Базовый', device_limit=1, traffic_limit_gb=0)
    existing = _existing(tariff_id=None, is_trial=False, device_limit=2, days_left=40)
    extend, _ = _wire(monkeypatch, existing=existing, tariff=tariff)
    purchase, user = _purchase(), _user()

    await svc.activate_purchase(_db(purchase, user), purchase.token, skip_notification=True)

    extend.assert_awaited_once()
    assert extend.await_args.kwargs['tariff_id'] == 3
    assert extend.await_args.kwargs['device_limit'] == 2


@pytest.mark.asyncio
async def test_expired_subscription_of_another_tariff_is_replaced_not_refused(monkeypatch):
    """Мутация MX19: истёкшая подписка другого тарифа — свежий месяц на базе (решение 1А), не 409."""
    tariff = SimpleNamespace(id=3, name='Базовый', device_limit=1, traffic_limit_gb=0)
    existing = _existing(tariff_id=4, is_trial=False, device_limit=3, days_left=-5)
    extend, replace = _wire(monkeypatch, existing=existing, tariff=tariff)
    purchase, user = _purchase(), _user()

    await svc.activate_purchase(_db(purchase, user), purchase.token, skip_notification=True)

    extend.assert_not_awaited()
    replace.assert_awaited_once()
    assert replace.await_args.kwargs['device_limit'] == 1


# --- диплинк /start GIFT_… показывает причину отказа (волна 2 на дифф, DF6) ------------------


async def _run_deep_link(monkeypatch, *, error):
    """Гоняем настоящий `_activate_pending_gift_after_registration` с подставной покупкой."""
    from app.handlers import start as start_handler

    purchase = SimpleNamespace(
        token='T' * 64,
        is_gift=True,
        buyer_user_id=1,
        status=svc.GuestPurchaseStatus.PAID.value,
        user_id=None,
        tariff=SimpleNamespace(name='Базовый'),
        period_days=30,
    )
    db = MagicMock()
    db.execute = AsyncMock(return_value=SimpleNamespace(scalars=lambda: SimpleNamespace(first=lambda: purchase)))
    db.flush = AsyncMock()
    state = SimpleNamespace(get_data=AsyncMock(return_value={'pending_gift_token': 'T' * 64}))
    monkeypatch.setattr(svc, 'activate_purchase', AsyncMock(side_effect=error))
    answers: list[str] = []

    async def answer_func(text, **_kwargs):
        answers.append(text)

    await start_handler._activate_pending_gift_after_registration(db, state, SimpleNamespace(id=206), answer_func)
    return answers


@pytest.mark.asyncio
async def test_deep_link_shows_the_refusal_reason_instead_of_a_generic_error(monkeypatch):
    answers = await _run_deep_link(
        monkeypatch,
        error=svc.GuestPurchaseError('Подарок нельзя применить к действующей подписке другого тарифа.', 409),
    )
    assert answers == [
        'Не удалось активировать подарок: Подарок нельзя применить к действующей подписке другого тарифа.'
    ]


@pytest.mark.asyncio
async def test_deep_link_keeps_the_generic_text_for_server_failures(monkeypatch):
    answers = await _run_deep_link(
        monkeypatch, error=svc.GuestPurchaseError('Activation failed, please try again', 500)
    )
    assert len(answers) == 1
    assert 'Произошла ошибка при активации подарка' in answers[0]
