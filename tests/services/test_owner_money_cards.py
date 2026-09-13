"""Сторож единой формы денежных карточек владельцу (этап УВ-2, 13.09.2026).

Каждая карточка собирается НАСТОЯЩИМ сборщиком (`send_*_notification`), а не проверкой
ингредиентов: то, что уходит в `_send_message`, и есть то, что видит владелец.
Форма: строка 1 — что случилось и сколько денег; строка 2 — кто и тариф; строка 3 — что
изменилось, неочевидная цифра объяснена; дальше — только если есть что сказать; дата без секунд.
Числа в фикстурах нарочно не совпадают с умолчаниями настроек (устройство 70 ₽, а не 50 ₽;
пробный 7 дней, а не 3), чтобы сторож видел, откуда карточка берёт цифры.
«Сейчас» заморожено на 13.09.2026 15:55 UTC — иначе формат даты («до 16.10» против
«до 16.10.2026») зависел бы от года, в котором запущен тест.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import admin_notification_service as module
from app.services.admin_notification_service import AdminNotificationService, NotificationCategory


SQUAD_UUID = 'ap-fce2227c94a631d4-01'
FORBIDDEN = (
    'Telegram ID',
    'ID транзакции',
    '<code>',
    'Промогруппа',
    'Скидки:',
    'скидка действует',
    'Трафик:',
    'Серверы:',
    'Username:',
    'С баланса',
    'Пришёл по ссылке',
    'Пригласил',
    SQUAD_UUID,
    '<blockquote',
)
NOW = datetime(2026, 9, 13, 15, 55, tzinfo=UTC)
END_DATE = datetime(2026, 9, 16, 12, 26, tzinfo=UTC)
NEW_END_DATE = datetime(2026, 10, 16, 12, 26, tzinfo=UTC)


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW if tz is None else NOW.astimezone(tz)


@pytest.fixture(autouse=True)
def _frozen_now():
    with patch.object(module, 'datetime', _FrozenDatetime):
        yield


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
        status='active',
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
    assert lines[1].strip(), text  # строка «кто» не бывает пустой
    assert re.fullmatch(r'<i>\d{2}\.\d{2} \d{2}:\d{2}</i>', lines[-1]), lines[-1]
    for needle in FORBIDDEN:
        assert needle not in text, (needle, text)
    return lines


def _patched_tariff(tariff=None):
    return patch('app.database.crud.tariff.get_tariff_by_id', AsyncMock(return_value=tariff))


# ── докупка устройств ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_addon_card_explains_prorated_price() -> None:
    service = _service()
    subscription = _subscription(end_date=NOW + timedelta(days=2, hours=12))
    with _patched_tariff(_tariff()):
        assert await service.send_subscription_update_notification(
            AsyncMock(), _user(), subscription, 'devices', 2, 3, price_paid=700
        )
    text, category = _sent(service)
    lines = _assert_card_shape(text)
    assert category is NotificationCategory.ADDONS
    assert lines[0] == '<b>📱 Докупка устройств — 7 ₽</b>'
    assert lines[1] == 'nikitaa @lilgaandelf · Базовый'
    assert lines[2] == '+1 устройство, стало 3 · 7 ₽ = 70 ₽/мес × 3 дня до конца подписки (16.09)'
    assert '2 → 3' not in text


@pytest.mark.asyncio
async def test_addon_card_keeps_quiet_when_price_does_not_match_formula() -> None:
    service = _service()
    subscription = _subscription(end_date=NOW + timedelta(days=2, hours=12))
    with _patched_tariff(_tariff()):
        await service.send_subscription_update_notification(
            AsyncMock(), _user(balance_kopeks=5100), subscription, 'devices', 2, 3, price_paid=333
        )
    text, _ = _sent(service)
    lines = _assert_card_shape(text)
    assert 'поэтому' not in text
    assert lines[2] == '+1 устройство, стало 3. Подписка до 16.09'
    assert 'На балансе осталось 51 ₽' in lines


@pytest.mark.asyncio
async def test_addon_card_survives_naive_end_date_and_explains_long_terms() -> None:
    service = _service()
    subscription = _subscription(end_date=(NOW + timedelta(days=29, hours=20)).replace(tzinfo=None))
    with _patched_tariff(_tariff()):
        assert await service.send_subscription_update_notification(
            AsyncMock(), _user(), subscription, 'devices', 1, 3, price_paid=14000
        )
    text, _ = _sent(service)
    lines = _assert_card_shape(text)
    assert lines[0] == '<b>📱 Докупка устройств — 140 ₽</b>'
    assert lines[2] == '+2 устройства, стало 3 · 140 ₽ = 2 × 70 ₽/мес × 30 дней до конца подписки (13.10)'


@pytest.mark.asyncio
async def test_addon_card_never_explains_price_of_expired_subscription() -> None:
    service = _service()
    subscription = _subscription(end_date=NOW - timedelta(days=1))
    with _patched_tariff(_tariff()):
        await service.send_subscription_update_notification(
            AsyncMock(), _user(balance_kopeks=30), subscription, 'devices', 2, 3, price_paid=233
        )
    text, _ = _sent(service)
    lines = _assert_card_shape(text)
    assert 'поэтому' not in text and 'осталось' not in text
    assert lines[2] == '+1 устройство, стало 3. Подписка до 12.09'
    assert 'На балансе' not in text  # 30 копеек округляются до «0 ₽» — про такой остаток молчим


@pytest.mark.asyncio
async def test_device_decrease_is_not_called_a_purchase() -> None:
    service = _service()
    with _patched_tariff(_tariff()):
        await service.send_subscription_update_notification(AsyncMock(), _user(), _subscription(), 'devices', 3, 2)
    text, _ = _sent(service)
    lines = _assert_card_shape(text)
    assert lines[0] == '<b>📱 Устройств стало меньше</b>'
    assert lines[2] == 'Устройств: 3 → 2'
    assert 'бесплатно' not in text


@pytest.mark.asyncio
async def test_card_falls_back_to_username_or_id_when_name_is_missing() -> None:
    service = _service()
    with _patched_tariff(None):
        await service.send_subscription_update_notification(
            AsyncMock(), _user(first_name='   ', username='<evil>'), _subscription(tariff_id=None), 'traffic', 50, 100
        )
    text, _ = _sent(service)
    lines = _assert_card_shape(text)
    assert lines[0] == '<b>📊 Докупка трафика — бесплатно</b>'
    assert lines[1] == '@&lt;evil&gt;'
    assert lines[2] == '50 ГБ → 100 ГБ'


# ── продление ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_renewal_card_explains_price_and_never_says_from_balance() -> None:
    service = _service()
    with _patched_tariff(_tariff()):
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
    assert lines[2] == '+30 дней, до 16.10 · 3 устройства'
    assert lines[3] == 'Цена: тариф 149 ₽ + устройства 2 × 70 ₽'
    assert 'По приглашению' not in text
    assert 'На балансе' not in text  # balance_after=0 — про пустой баланс молчим


@pytest.mark.asyncio
async def test_renewal_card_skips_breakdown_when_sum_differs_and_shows_year_when_it_changes() -> None:
    service = _service()
    with _patched_tariff(_tariff(period_prices={'30': 14900, '365': 109000})):
        await service.send_subscription_extension_notification(
            AsyncMock(),
            _user(),
            _subscription(),
            _transaction(amount_kopeks=-24900),
            365,
            END_DATE,
            new_end_date=datetime(2027, 9, 16, 12, 26, tzinfo=UTC),
        )
    text, _ = _sent(service)
    lines = _assert_card_shape(text)
    assert 'Цена:' not in text
    assert lines[0] == '<b>⏰ Продление — 249 ₽</b>'
    assert lines[2] == '+365 дней, до 16.09.2027 · 3 устройства'


# ── пополнение ─────────────────────────────────────────────────────────────────


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
        subscription=_subscription(tariff=_tariff()),
        promo_group=SimpleNamespace(name='Пользователь', apply_discounts_to_addons=True),
    )
    text, category = _sent(service)
    lines = _assert_card_shape(text)
    assert category is NotificationCategory.BALANCE
    assert lines[0] == '<b>💰 Пополнение — 249 ₽ по СБП</b>'
    assert lines[1] == 'nikitaa @lilgaandelf · Базовый до 16.09'
    assert lines[2] == 'Баланс: 1 ₽ → 250 ₽'
    assert 'бонус' not in text
    assert 'По приглашению' not in text


@pytest.mark.asyncio
async def test_first_topup_card_shows_referrer_without_internal_id() -> None:
    service = _service()
    transaction = _transaction(amount_kopeks=19900, payment_method='platega', description='Пополнение через Platega')
    await service.send_balance_topup_notification(
        _user(balance_kopeks=29900),
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
    assert lines[2] == 'Баланс: 0 ₽ → 299 ₽ (в т.ч. бонус 100 ₽)'
    assert 'По приглашению @kozyr20' in lines
    assert '(ID: 123)' not in text


@pytest.mark.parametrize(
    ('subscription', 'expected'),
    [
        (_subscription(tariff=_tariff(), status='limited', is_active=False), 'Базовый до 16.09, трафик исчерпан'),
        (_subscription(tariff=_tariff(), status='disabled', is_active=False), 'Базовый, выключена'),
        (_subscription(status='expired', is_active=False, end_date=NOW - timedelta(days=1)), 'подписка истекла 12.09'),
        (
            _subscription(tariff=_tariff(), status='expired', is_active=False, end_date=NOW - timedelta(days=1)),
            'Базовый, истекла 12.09',
        ),
        (_subscription(is_trial=True), 'пробный до 16.09'),
        (_subscription(is_trial=True, end_date=NOW - timedelta(hours=20)), 'пробный истёк 12.09'),
        (_subscription(status='active', is_active=True), 'подписка до 16.09'),
    ],
)
@pytest.mark.asyncio
async def test_topup_card_names_subscription_state_honestly(subscription, expected: str) -> None:
    service = _service()
    await service.send_balance_topup_notification(
        _user(balance_kopeks=10000),
        _transaction(amount_kopeks=10000, payment_method='manual', description='Пополнение администратором'),
        0,
        topup_status='🔄 Пополнение',
        referrer_info='Нет',
        subscription=subscription,
        promo_group=None,
    )
    text, _ = _sent(service)
    lines = _assert_card_shape(text)
    assert lines[0] == '<b>💰 Пополнение — 100 ₽ вручную</b>'
    assert lines[1] == f'nikitaa @lilgaandelf · {expected}'
    assert 'Комментарий: Пополнение администратором' in lines


# ── пробный ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_trial_card_is_short_and_names_the_referrer() -> None:
    service = _service()
    subscription = _subscription(
        device_limit=1,
        traffic_limit_gb=5,
        is_trial=True,
        start_date=datetime(2026, 9, 13, 19, 58, tzinfo=UTC),
        end_date=datetime(2026, 9, 20, 19, 58, tzinfo=UTC),
    )
    with _patched_tariff(_tariff(name='⏰Пробный', is_trial_available=True)):
        assert await service.send_trial_activation_notification(
            AsyncMock(), _user(has_had_paid_subscription=False), subscription
        )
    text, category = _sent(service)
    lines = _assert_card_shape(text)
    assert category is NotificationCategory.TRIALS
    assert lines[0] == '<b>🎁 Пробный период</b>'
    assert lines[1] == 'nikitaa @lilgaandelf'  # тариф самого пробного заголовок не дублирует
    assert lines[2] == '7 дней, 1 устройство, 5 ГБ, до 20.09'
    assert lines[3] == 'По приглашению @kozyr20'
    assert 'Раньше уже платил' not in text


@pytest.mark.asyncio
async def test_trial_card_flags_a_person_who_already_paid_before() -> None:
    service = _service()
    subscription = _subscription(device_limit=2, is_trial=True, end_date=END_DATE)
    with _patched_tariff(None):
        await service.send_trial_activation_notification(
            AsyncMock(), _user(referred_by_id=None), subscription, charged_amount_kopeks=5000
        )
    text, _ = _sent(service)
    lines = _assert_card_shape(text)
    assert lines[0] == '<b>🎁 Пробный период — 50 ₽</b>'
    assert lines[1] == 'nikitaa @lilgaandelf'
    assert lines[2] == '60 дней, 2 устройства, до 16.09'
    assert lines[3] == '⚠️ Раньше уже платил(а) — пробный выдан повторно'


# ── покупка ────────────────────────────────────────────────────────────────────
# На боевом эта карточка спит с 31.07 (касса кабинета её не зовёт — К-2/Г1). Транзакции
# прямых продаж носят описание «Оплата подписки картой: …» без имени метода, поэтому метка
# здесь — «через Platega»; К-2 обязан передавать способ оплаты явно, а не через описание.


@pytest.mark.asyncio
async def test_purchase_card_first_purchase_via_provider() -> None:
    service = _service()
    transaction = _transaction(
        amount_kopeks=-28900, payment_method='platega', description='Оплата подписки картой: 1 месяц'
    )
    with _patched_tariff(_tariff()):
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
    assert lines[0] == '<b>💎 Первая покупка — 289 ₽ через Platega</b>'
    assert lines[2] == '30 дней, до 16.10 · 3 устройства'
    assert 'Цена: тариф 149 ₽ + устройства 2 × 70 ₽' in lines
    assert 'По приглашению @kozyr20' in lines


@pytest.mark.asyncio
async def test_purchase_card_renewal_routes_to_renewals_without_referrer() -> None:
    service = _service()
    with _patched_tariff(_tariff()):
        await service.send_subscription_purchase_notification(
            AsyncMock(), _user(), _subscription(end_date=NEW_END_DATE), _transaction(), 30
        )
    text, category = _sent(service)
    lines = _assert_card_shape(text)
    assert category is NotificationCategory.RENEWALS
    assert lines[0] == '<b>⏰ Продление — 289 ₽</b>'
    assert 'По приглашению' not in text


@pytest.mark.asyncio
async def test_purchase_card_after_trial_keeps_referrer_even_when_caller_says_renewal() -> None:
    service = _service()
    with _patched_tariff(_tariff()):
        await service.send_subscription_purchase_notification(
            AsyncMock(),
            _user(),
            _subscription(end_date=NEW_END_DATE),
            None,
            30,
            was_trial_conversion=True,
            amount_kopeks=28900,
            purchase_type='renewal',
        )
    text, category = _sent(service)
    lines = _assert_card_shape(text)
    assert category is NotificationCategory.RENEWALS  # маршрутизация — как была у вызывающих
    assert lines[0] == '<b>💎 Покупка после пробного — 289 ₽</b>'
    assert 'По приглашению @kozyr20' in lines


@pytest.mark.asyncio
async def test_purchase_card_tariff_switch_title() -> None:
    service = _service()
    with _patched_tariff(_tariff()):
        await service.send_subscription_purchase_notification(
            AsyncMock(),
            _user(),
            _subscription(end_date=NEW_END_DATE),
            _transaction(),
            30,
            purchase_type='tariff_switch',
        )
    text, _ = _sent(service)
    lines = _assert_card_shape(text)
    assert lines[0] == '<b>🔄 Смена тарифа — 289 ₽</b>'


@pytest.mark.asyncio
async def test_client_without_username_gets_id_so_he_can_be_found() -> None:
    service = _service()
    with _patched_tariff(_tariff()):
        await service.send_subscription_extension_notification(
            AsyncMock(), _user(username=None, first_name='Алексей'), _subscription(), _transaction(), 30, END_DATE
        )
    text, _ = _sent(service)
    lines = _assert_card_shape(text)
    assert lines[1] == 'Алексей · ID 1629864309 · Базовый'


@pytest.mark.asyncio
async def test_discount_from_description_explains_odd_sum_and_renewal_gets_plus() -> None:
    service = _service()
    transaction = _transaction(amount_kopeks=-13400, description='Продление подписки: 1 месяц (скидка 10%)')
    with _patched_tariff(_tariff(device_limit=3)):
        await service.send_subscription_purchase_notification(
            AsyncMock(), _user(), _subscription(end_date=NEW_END_DATE), transaction, 30, purchase_type='renewal'
        )
    text, _ = _sent(service)
    lines = _assert_card_shape(text)
    assert lines[0] == '<b>⏰ Продление — 134 ₽</b>'
    assert lines[2] == '+30 дней, до 16.10 · 3 устройства'
    assert lines[3] == 'Со скидкой 10 %'
