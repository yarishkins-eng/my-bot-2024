"""ВК-3 (25.09.2026): письмо «лимит трафика» пробному говорит правду и ведёт на оформление.

Лимит пробного суточный: тариф «Пробный» — 5 ГБ, «Режим сброса трафика: Ежедневно»; панель возвращает трафик каждую
ночь в 03:05 МСК. Докупки трафика у пробного нет, а старое письмо советовало «Докупите трафик» и вело двумя кнопками на
Главную. Теперь пробному — «Трафик на сегодня закончился… N ГБ в сутки…» и одна кнопка «💎 Оформить подписку» сразу
на экран оформления. Число и режим сброса берутся из самого события панели — это настоящий лимит этого человека
(поменяет владелец тариф — новые пробные получат новое число и в письме). Платному — старое письмо (у платных тарифов
трафик безлимитный, событие к ним не приходит).

Ночное «Подписка активирована», когда подписка выходит из limited после сброса трафика, приходит без звука —
решение владельца 25.09.2026 «Присылать тихо».

Обработчики зовутся через ту же карту событий, что и приёмник вебхука; отправка ловится на входе в службу доставки,
поэтому настоящие тексты, карты ключей, выключатели и кнопка «Закрыть» участвуют по-настоящему.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import remnawave_webhook_service as webhook
from app.services.notification_delivery_service import NotificationDeliveryService, NotificationType


CABINET = 'https://cabinet.example'
GB = 1024**3

TRIAL_DAILY_RU = (
    '⚠️ <b>Трафик на сегодня закончился</b>\n\n'
    'В пробном периоде — 5 ГБ в сутки, трафик обновляется каждую ночь.\n'
    'Не хотите ждать — оформите подписку: в ней трафик без ограничений.'
)
TRIAL_GENERIC_RU = '⚠️ <b>Трафик пробного периода закончился</b>\n\nОформите подписку: в ней трафик без ограничений.'
PAID_RU = (
    '⚠️ <b>Достигнут лимит трафика</b>\n\n'
    'Вы исчерпали весь доступный трафик по подписке. Докупите трафик или дождитесь сброса.'
)
SUBSCRIBE_ROWS = [
    [('💎 Оформить подписку', f'{CABINET}/subscription/purchase')],
    [('✖️ Закрыть', 'webhook:close')],
]
EN_SUBSCRIBE_ROWS = [
    [('💎 Get a subscription', f'{CABINET}/subscription/purchase')],
    [('✖️ Close', 'webhook:close')],
]


@pytest.fixture(autouse=True)
def _prod_like_settings(monkeypatch):
    s = webhook.settings
    monkeypatch.setattr(s, 'WEBHOOK_NOTIFY_USER_ENABLED', True)
    monkeypatch.setattr(s, 'WEBHOOK_NOTIFY_SUB_LIMITED', True)
    monkeypatch.setattr(s, 'WEBHOOK_NOTIFY_SUB_STATUS', True)
    monkeypatch.setattr(s, 'MAIN_MENU_MODE', 'cabinet')
    monkeypatch.setattr(s, 'MINIAPP_CUSTOM_URL', CABINET)
    monkeypatch.setattr(s, 'MULTI_TARIFF_ENABLED', False)


@pytest.fixture
def delivery(monkeypatch):
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(webhook.notification_delivery_service, 'send_notification', send)
    return send


def _user(language: str = 'ru'):
    return SimpleNamespace(id=688, telegram_id=7_000_688, language=language)


def _sub(*, is_trial: bool = True, status: str = 'active'):
    return SimpleNamespace(id=377, user_id=688, is_trial=is_trial, status=status, updated_at=None)


def _receiver_data(user_dto: dict) -> dict:
    """``data`` обработчика — ровно как его строит приёмник (`app/webserver/remnawave_webhook.py`): поле ``data``
    события панели (пользователь панели), плюс ``_meta`` из конверта."""
    payload = {'scope': 'user', 'event': 'user.limited', 'data': dict(user_dto), 'meta': None}
    data = payload.get('data')
    if not isinstance(data, dict):
        data = {}
    meta = payload.get('meta')
    if isinstance(meta, dict):
        data['_meta'] = meta
    return data


def _limited_event(*, limit_bytes=5 * GB, strategy='DAY', drop=()):
    dto = {'uuid': 'd7849464', 'status': 'LIMITED', 'trafficLimitBytes': limit_bytes, 'trafficLimitStrategy': strategy}
    for key in drop:
        dto.pop(key)
    return _receiver_data(dto)


async def _fire(event: str, subscription, data: dict, *, user=None):
    service = webhook.RemnaWaveWebhookService(MagicMock())
    db = AsyncMock()
    await service._user_handlers[event](db, user or _user(), subscription, data)
    return service


def _letter(delivery):
    delivery.assert_awaited_once()
    kwargs = delivery.await_args.kwargs
    rows = [
        [(button.text, button.web_app.url if button.web_app else button.callback_data) for button in row]
        for row in kwargs['telegram_markup'].inline_keyboard
    ]
    return kwargs['telegram_message'], rows, kwargs


async def test_trial_daily_letter_tells_the_truth_and_leads_to_checkout(delivery):
    await _fire('user.limited', _sub(), _limited_event())

    message, rows, kwargs = _letter(delivery)
    assert message == TRIAL_DAILY_RU
    assert 'Докуп' not in message  # у пробного докупки трафика нет — письмо её не обещает
    assert rows == SUBSCRIBE_ROWS
    assert kwargs['notification_type'] is NotificationType.WEBHOOK_SUB_LIMITED
    assert kwargs['telegram_silent'] is False


@pytest.mark.parametrize('limit_gb', [10, 2, 20])
async def test_number_follows_the_persons_real_limit(delivery, limit_gb):
    await _fire('user.limited', _sub(), _limited_event(limit_bytes=limit_gb * GB))

    message, rows, _ = _letter(delivery)
    assert message == TRIAL_DAILY_RU.replace('— 5 ГБ', f'— {limit_gb} ГБ')
    assert rows == SUBSCRIBE_ROWS


@pytest.mark.parametrize(
    'event',
    [
        _limited_event(strategy='NO_RESET'),
        _limited_event(strategy='MONTH'),
        _limited_event(drop=('trafficLimitStrategy',)),
        _limited_event(limit_bytes=0),
        _limited_event(drop=('trafficLimitBytes',)),
        _limited_event(limit_bytes='не число'),
        _receiver_data({}),
    ],
    ids=['no-reset', 'month', 'no-strategy', 'zero-limit', 'no-limit', 'bad-limit', 'empty-event'],
)
async def test_without_a_daily_reset_the_letter_promises_none(delivery, event):
    await _fire('user.limited', _sub(), event)

    message, rows, _ = _letter(delivery)
    assert message == TRIAL_GENERIC_RU
    assert rows == SUBSCRIBE_ROWS


async def test_english_trial_letter(delivery):
    await _fire('user.limited', _sub(), _limited_event(limit_bytes=10 * GB), user=_user('en'))

    message, rows, _ = _letter(delivery)
    assert message == (
        "⚠️ <b>Today's traffic is used up</b>\n\n"
        'The trial includes 10 GB per day, and traffic resets every night.\n'
        "Don't want to wait? Get a subscription with unlimited traffic."
    )
    assert rows == EN_SUBSCRIBE_ROWS


async def test_english_trial_letter_without_daily_reset(delivery):
    await _fire('user.limited', _sub(), _limited_event(strategy='NO_RESET'), user=_user('en'))

    message, rows, _ = _letter(delivery)
    assert message == '⚠️ <b>Trial traffic is used up</b>\n\nGet a subscription with unlimited traffic.'
    assert rows == EN_SUBSCRIBE_ROWS


async def test_fresh_trial_status_goes_limited_and_gets_the_letter(delivery):
    subscription = _sub(status='trial')

    await _fire('user.limited', subscription, _limited_event())

    message, rows, _ = _letter(delivery)
    assert message == TRIAL_DAILY_RU
    assert rows == SUBSCRIBE_ROWS
    assert subscription.status == 'limited'


async def test_paid_subscription_keeps_the_old_letter(delivery):
    await _fire('user.limited', _sub(is_trial=False), _limited_event(limit_bytes=100 * GB, strategy='MONTH'))

    message, rows, kwargs = _letter(delivery)
    assert message == PAID_RU
    assert rows == [
        [('📈 Докупить трафик', f'{CABINET}/subscription')],
        [('📱 Моя подписка', f'{CABINET}/subscription')],
        [('✖️ Закрыть', 'webhook:close')],
    ]
    assert kwargs['telegram_silent'] is False


@pytest.mark.parametrize('event', [_limited_event(), _limited_event(strategy='NO_RESET')], ids=['daily', 'generic'])
async def test_limited_toggle_silences_the_trial_letter_too(delivery, monkeypatch, event):
    monkeypatch.setattr(webhook.settings, 'WEBHOOK_NOTIFY_SUB_LIMITED', False)

    with patch.object(webhook.logger, 'info') as info:
        await _fire('user.limited', _sub(), event)

    delivery.assert_not_awaited()
    assert info.call_args.kwargs['sent'] is False


async def test_master_switch_silences_every_webhook_letter(delivery, monkeypatch):
    monkeypatch.setattr(webhook.settings, 'WEBHOOK_NOTIFY_USER_ENABLED', False)

    with patch.object(webhook.logger, 'info') as info:
        await _fire('user.limited', _sub(), _limited_event())

    delivery.assert_not_awaited()
    assert info.call_args.kwargs == {'subscription_id': 377, 'variant': 'trial_daily', 'sent': False}


@pytest.mark.parametrize(
    ('subscription', 'event', 'delivered', 'variant'),
    [
        (_sub(), _limited_event(), True, 'trial_daily'),
        (_sub(), _limited_event(strategy='NO_RESET'), True, 'trial'),
        (_sub(is_trial=False), _limited_event(strategy='MONTH'), True, 'paid'),
        (_sub(), _limited_event(), False, 'trial_daily'),
    ],
    ids=['trial-daily', 'trial', 'paid', 'telegram-refused'],
)
async def test_log_line_names_the_variant_and_whether_it_went_out(delivery, subscription, event, delivered, variant):
    delivery.return_value = delivered

    with patch.object(webhook.logger, 'info') as info:
        await _fire('user.limited', subscription, event)

    lines = [call for call in info.call_args_list if call.args == ('Письмо о лимите трафика',)]
    assert len(lines) == 1
    assert lines[0].kwargs == {'subscription_id': 377, 'variant': variant, 'sent': delivered}


async def test_delivery_crash_is_reported_as_not_sent(delivery):
    delivery.side_effect = RuntimeError('telegram down')

    with patch.object(webhook.logger, 'info') as info:
        await _fire('user.limited', _sub(), _limited_event())

    assert info.call_args.kwargs == {'subscription_id': 377, 'variant': 'trial_daily', 'sent': False}


def _reactivate_like_crud():
    async def reactivate(db, subscription, **_kwargs):
        subscription.status = 'active'
        return subscription

    return AsyncMock(side_effect=reactivate)


@pytest.mark.parametrize(
    ('status', 'silent'),
    [('limited', True), ('disabled', False), ('active', False)],
    ids=['night-traffic-reset', 'admin-re-enabled', 'already-active'],
)
async def test_only_the_night_traffic_return_comes_silently(delivery, monkeypatch, status, silent):
    monkeypatch.setattr(webhook, 'reactivate_subscription', _reactivate_like_crud())

    await _fire('user.enabled', _sub(status=status), _receiver_data({'uuid': 'd7849464', 'status': 'ACTIVE'}))

    message, rows, kwargs = _letter(delivery)
    assert message.startswith('✅ <b>Подписка активирована</b>')
    assert rows[0] == [('🔗 Подключиться', f'{CABINET}/subscription')]
    assert kwargs['telegram_silent'] is silent


async def test_silent_reaches_telegram_as_disable_notification():
    bot = SimpleNamespace(send_message=AsyncMock())
    user = SimpleNamespace(id=688, telegram_id=7_000_688, status='active', email=None, email_verified=False)
    service = NotificationDeliveryService()

    sent = await service.send_notification(
        user=user,
        notification_type=NotificationType.WEBHOOK_SUB_ENABLED,
        context={},
        bot=bot,
        telegram_message='ночью',
        telegram_markup=None,
        telegram_silent=True,
    )

    assert sent is True
    assert bot.send_message.await_args.kwargs['disable_notification'] is True


async def test_loud_messages_reach_telegram_exactly_as_before():
    bot = SimpleNamespace(send_message=AsyncMock())
    user = SimpleNamespace(id=688, telegram_id=7_000_688, status='active', email=None, email_verified=False)

    await NotificationDeliveryService().send_notification(
        user=user,
        notification_type=NotificationType.WEBHOOK_SUB_LIMITED,
        context={},
        bot=bot,
        telegram_message='днём',
        telegram_markup=None,
    )

    assert bot.send_message.await_args.kwargs == {
        'chat_id': 7_000_688,
        'text': 'днём',
        'reply_markup': None,
        'parse_mode': 'HTML',
    }
