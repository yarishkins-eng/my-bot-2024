"""Сторож единой формы денежных карточек владельцу (этап УВ-2, 13.09.2026).

Каждая карточка собирается НАСТОЯЩИМ сборщиком (`send_*_notification`), а не проверкой
ингредиентов: то, что уходит в `_send_message`, и есть то, что видит владелец.
Форма: строка 1 — что случилось и сколько денег; строка 2 — кто и тариф; строка 3 — что
изменилось, неочевидная цифра объяснена; дальше — только если есть что сказать; дата без секунд.
Числа в фикстурах нарочно не совпадают с умолчаниями настроек (устройство 70 ₽, а не 50 ₽),
чтобы сторож видел, откуда карточка берёт цену.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.admin_notification_service import AdminNotificationService, NotificationCategory


SQUAD_UUID = 'ap-fce2227c94a631d4-01'
FORBIDDEN = (
    'Telegram ID',
    'ID транзакции',
    '<code>',
    'Промогруппа',
    'скидка',
    'Трафик:',
    'Серверы:',
    'Username:',
    'С баланса',
    SQUAD_UUID,
    '<blockquote',
)
END_DATE = datetime(2026, 9, 16, 12, 26, tzinfo=UTC)
NEW_END_DATE = datetime(2026, 10, 16, 12, 26, tzinfo=UTC)


def _service() -> AdminNotificationService:
    service = AdminNotificationService(MagicMock())
    service._send_message = AsyncMock(return_value=True)
    service._record_subscription_event = AsyncMock()
    service._is_enabled = MagicMock(return_value=True)
    service._get_referrer_info = AsyncMock(return_value='@kozyr20 (ID: 123)')
    return service


def _user(**overrides) -> SimpleNamespace:
    base = dict(
        id=144,
        telegram_id=1629864309,
        first_name='nikitaa',
        username='lilgaandelf',
        email=None,
        balance_kopeks=0,
        referred_by_id=123,
        has_had_paid_subscription=True,
        promo_group_id=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _tariff(**overrides) -> SimpleNamespace:
    base = dict(id=3, name='Базовый', period_prices={'30': 14900}, device_limit=1, device_price_kopeks=7000)
    base.update(overrides)
    return SimpleNamespace(**base)


def _subscription(**overrides) -> SimpleNamespace:
    base = dict(
        id=104,
        tariff_id=3,
        device_limit=3,
        traffic_limit_gb=0,
        start_date=datetime(2026, 7, 18, 12, 26, tzinfo=UTC),
        end_date=END_DATE,
        is_trial=False,
        is_active=True,
        connected_squads=[SQUAD_UUID],
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _transaction(**overrides) -> SimpleNamespace:
    base = dict(
        id=594,
        amount_kopeks=-28900,
        payment_method='balance',
        description='Продление подписки на 30 дней (Базовый)',
        external_id=None,
        receipt_uuid=None,
        created_at=datetime(2026, 9, 13, 15, 55, tzinfo=UTC),
        completed_at=datetime(2026, 9, 13, 15, 55, tzinfo=UTC),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _sent(service: AdminNotificationService) -> tuple[str, NotificationCategory]:
    service._send_message.assert_awaited_once()
    args, kwargs = service._send_message.await_args
    return args[0], kwargs['category']


def _assert_card_shape(text: str) -> list[str]:
    lines = text.split('\n')
    assert 4 <= len(lines) <= 6, text
    assert lines[0].startswith('<b>') and lines[0].endswith('</b>'), lines[0]
    assert re.fullmatch(r'<i>\d{2}\.\d{2} \d{2}:\d{2}</i>', lines[-1]), lines[-1]
    for needle in FORBIDDEN:
        assert needle not in text, (needle, text)
    return lines


@pytest.mark.asyncio
async def test_addon_card_explains_prorated_price() -> None:
    service = _service()
    subscription = _subscription(end_date=datetime.now(UTC) + timedelta(days=2, hours=12))
    with patch('app.database.crud.tariff.get_tariff_by_id', AsyncMock(return_value=_tariff())):
        assert await service.send_subscription_update_notification(
            AsyncMock(), _user(), subscription, 'devices', 2, 3, price_paid=700
        )
    text, category = _sent(service)
    lines = _assert_card_shape(text)
    assert category is NotificationCategory.ADDONS
    assert lines[0] == '<b>📱 Докупка устройств — 7 ₽</b>'
    assert lines[1] == 'nikitaa @lilgaandelf · Базовый'
    assert lines[2].startswith('+1 устройство, стало 3. Подписка до ')
    assert lines[2].endswith('осталось 3 дня — поэтому 7 ₽, а не 70 ₽')
    assert '2 → 3' not in text


@pytest.mark.asyncio
async def test_addon_card_keeps_quiet_when_price_does_not_match_formula() -> None:
    service = _service()
    subscription = _subscription(end_date=datetime.now(UTC) + timedelta(days=2, hours=12))
    with patch('app.database.crud.tariff.get_tariff_by_id', AsyncMock(return_value=_tariff())):
        await service.send_subscription_update_notification(
            AsyncMock(), _user(balance_kopeks=5100), subscription, 'devices', 2, 3, price_paid=333
        )
    text, _ = _sent(service)
    lines = _assert_card_shape(text)
    assert 'поэтому' not in text
    assert lines[2].startswith('+1 устройство, стало 3. Подписка до ')
    assert 'На балансе осталось 51 ₽' in lines


@pytest.mark.asyncio
async def test_renewal_card_explains_price_and_never_says_from_balance() -> None:
    service = _service()
    with patch('app.database.crud.tariff.get_tariff_by_id', AsyncMock(return_value=_tariff())):
        assert await service.send_subscription_extension_notification(
            AsyncMock(),
            _user(),
            _subscription(),
            _transaction(),
            30,
            END_DATE,
            new_end_date=NEW_END_DATE,
            balance_after=0,
        )
    text, category = _sent(service)
    lines = _assert_card_shape(text)
    assert category is NotificationCategory.RENEWALS
    assert lines[0] == '<b>⏰ Продление — 289 ₽</b>'
    assert lines[1] == 'nikitaa @lilgaandelf · Базовый'
    assert lines[2] == '+30 дней, до 16.10.2026 · 3 устройства'
    assert lines[3] == 'Цена: 149 ₽ тариф + 2 × 70 ₽ устройства'
    assert 'Пришёл по ссылке' not in text
    assert 'На балансе' not in text  # balance_after=0 — про пустой баланс молчим


@pytest.mark.asyncio
async def test_renewal_card_skips_breakdown_when_sum_differs() -> None:
    service = _service()
    with patch('app.database.crud.tariff.get_tariff_by_id', AsyncMock(return_value=_tariff())):
        await service.send_subscription_extension_notification(
            AsyncMock(), _user(), _subscription(), _transaction(amount_kopeks=-24900), 30, END_DATE
        )
    text, _ = _sent(service)
    _assert_card_shape(text)
    assert 'Цена:' not in text
    assert text.startswith('<b>⏰ Продление — 249 ₽</b>')


@pytest.mark.asyncio
async def test_topup_card_names_the_method_in_owner_words() -> None:
    service = _service()
    transaction = _transaction(
        amount_kopeks=24900, payment_method='platega', description='Пополнение через Platega (СБП (QR))'
    )
    assert await service.send_balance_topup_notification(
        _user(balance_kopeks=25000),
        transaction,
        100,
        topup_status='🔄 Пополнение',
        referrer_info='@kozyr20 (ID: 123)',
        subscription=_subscription(),
        promo_group=SimpleNamespace(name='Пользователь', apply_discounts_to_addons=True),
    )
    text, category = _sent(service)
    lines = _assert_card_shape(text)
    assert category is NotificationCategory.BALANCE
    assert lines[0] == '<b>💰 Пополнение — 249 ₽ по СБП</b>'
    assert lines[1] == 'nikitaa @lilgaandelf · подписка до 16.09'
    assert lines[2] == 'Баланс: 1 ₽ → 250 ₽'
    assert 'Пришёл по ссылке' not in text


@pytest.mark.asyncio
async def test_first_topup_card_shows_referrer_without_internal_id() -> None:
    service = _service()
    transaction = _transaction(amount_kopeks=19900, payment_method='platega', description='Пополнение через Platega')
    await service.send_balance_topup_notification(
        _user(balance_kopeks=19900),
        transaction,
        0,
        topup_status='🆕 Первое пополнение',
        referrer_info='@kozyr20 (ID: 123)',
        subscription=None,
        promo_group=None,
    )
    text, _ = _sent(service)
    lines = _assert_card_shape(text)
    assert lines[0] == '<b>💰 Первое пополнение — 199 ₽ через Platega</b>'
    assert lines[1] == 'nikitaa @lilgaandelf · без подписки'
    assert 'Пришёл по ссылке @kozyr20' in lines
    assert '(ID: 123)' not in text


@pytest.mark.asyncio
async def test_trial_card_is_short_and_names_the_referrer() -> None:
    service = _service()
    subscription = _subscription(
        device_limit=1,
        traffic_limit_gb=5,
        is_trial=True,
        start_date=datetime(2026, 9, 13, 19, 58, tzinfo=UTC),
        end_date=datetime(2026, 9, 16, 19, 58, tzinfo=UTC),
    )
    with patch('app.database.crud.tariff.get_tariff_by_id', AsyncMock(return_value=_tariff())):
        assert await service.send_trial_activation_notification(
            AsyncMock(), _user(has_had_paid_subscription=False), subscription
        )
    text, category = _sent(service)
    lines = _assert_card_shape(text)
    assert category is NotificationCategory.TRIALS
    assert lines[0] == '<b>🎁 Пробный период</b>'
    assert lines[1] == 'nikitaa @lilgaandelf · Базовый'
    assert lines[2] == '3 дня, 1 устройство, 5 ГБ, до 16.09'
    assert lines[3] == 'Пришёл по ссылке @kozyr20'
    assert 'Раньше уже платил' not in text


@pytest.mark.asyncio
async def test_purchase_card_first_purchase_by_card() -> None:
    service = _service()
    transaction = _transaction(
        amount_kopeks=-28900, payment_method='platega', description='Пополнение через Platega (Карты (RUB))'
    )
    with patch('app.database.crud.tariff.get_tariff_by_id', AsyncMock(return_value=_tariff())):
        assert await service.send_subscription_purchase_notification(
            AsyncMock(),
            _user(has_had_paid_subscription=False),
            _subscription(end_date=NEW_END_DATE),
            transaction,
            30,
            purchase_type='first_purchase',
        )
    text, category = _sent(service)
    lines = _assert_card_shape(text)
    assert category is NotificationCategory.PURCHASES
    assert lines[0] == '<b>💎 Первая покупка — 289 ₽ картой</b>'
    assert lines[2] == '30 дней, до 16.10.2026 · 3 устройства'
    assert 'Цена: 149 ₽ тариф + 2 × 70 ₽ устройства' in lines
    assert 'Пришёл по ссылке @kozyr20' in lines


@pytest.mark.asyncio
async def test_purchase_card_renewal_routes_to_renewals_without_referrer() -> None:
    service = _service()
    with patch('app.database.crud.tariff.get_tariff_by_id', AsyncMock(return_value=_tariff())):
        await service.send_subscription_purchase_notification(
            AsyncMock(), _user(), _subscription(end_date=NEW_END_DATE), _transaction(), 30
        )
    text, category = _sent(service)
    lines = _assert_card_shape(text)
    assert category is NotificationCategory.RENEWALS
    assert lines[0] == '<b>⏰ Продление — 289 ₽</b>'
    assert 'Пришёл по ссылке' not in text


@pytest.mark.asyncio
async def test_card_falls_back_to_username_or_id_when_name_is_missing() -> None:
    service = _service()
    with patch('app.database.crud.tariff.get_tariff_by_id', AsyncMock(return_value=None)):
        await service.send_subscription_update_notification(
            AsyncMock(), _user(first_name=None, username='<evil>'), _subscription(tariff_id=None), 'traffic', 50, 100
        )
    text, _ = _sent(service)
    lines = _assert_card_shape(text)
    assert lines[0] == '<b>📊 Докупка трафика — бесплатно</b>'
    assert lines[1] == '@&lt;evil&gt;'
    assert lines[2] == '50 ГБ → 100 ГБ'
