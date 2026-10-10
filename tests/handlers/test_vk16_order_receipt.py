"""ВК-16 · 16б: решение 05.10.2026 заменяет ручное оформление 02.09 только у разрешённой доплаты.

Проверяем реальные сборщики двух экранов, реальный серверный предикат и адрес перехода.
Чтение экрана не создаёт заказ/счёт и не двигает деньги; оплату завершает прежний договор16в-3.
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit

import pytest

from app.config import settings
from app.handlers.subscription import device_first as screen
from app.services import device_first_checkout_service as checkout_service


@pytest.fixture
def environment(monkeypatch):
    monkeypatch.setattr(checkout_service, 'TOPUP_INTENT_ROLLOUT', 'all')
    monkeypatch.setattr(type(settings), 'is_platega_enabled', lambda self: True)
    monkeypatch.setattr(settings, 'PLATEGA_MIN_AMOUNT_KOPEKS', 100)
    monkeypatch.setattr(screen, '_shown_at', lambda: 1_791_000_000)


async def _render(
    surface,
    language,
    balance,
    *,
    methods=True,
    cabinet=True,
    in_flight=False,
    method_keys=('sbp', 'cards_ru', 'crypto'),
    **user_flags,
):
    user = SimpleNamespace(
        id=17,
        language=language,
        balance_kopeks=balance,
        account_erasure_requested_at=None,
        account_erased_at=None,
        restriction_topup=False,
        restriction_subscription=False,
    )
    for key, value in user_flags.items():
        setattr(user, key, value)
    database = AsyncMock()
    database.scalar.return_value = 77 if in_flight else None
    database.get.return_value = SimpleNamespace(name='Базовый' if language == 'ru' else 'Basic')
    options = {
        'tariff': {'name': database.get.return_value.name},
        'period_options': [90],
        'device_options': [3],
        'price_matrix': [{'period_days': 90, 'prices': [{'device_limit': 3, 'price_kopeks': 14_900}]}],
    }
    checkout = SimpleNamespace(
        public_id='receipt-order',
        tariff_id=3,
        tariff_total_kopeks=14_900,
        selected_device_limit=3,
        period_days=90,
        funding_mode='platega' if in_flight else None,
    )
    callback = SimpleNamespace(answer=AsyncMock())
    with (
        patch.object(
            screen,
            'available_platega_methods_for_db',
            AsyncMock(return_value=[{'key': key} for key in method_keys] if methods else []),
        ),
        patch(
            'app.utils.miniapp_buttons.build_cabinet_url',
            side_effect=lambda path: f'https://cabinet.example{path}' if cabinet else None,
        ),
        patch.object(screen, 'edit_or_answer_photo', AsyncMock()) as render,
        patch.object(screen, 'create_or_resume_direct_checkout', AsyncMock()) as create,
        patch.object(screen, 'commit_direct_wallet_checkout', AsyncMock()) as charge,
        patch.object(screen, 'create_platega_attempt', AsyncMock()) as invoice,
    ):
        if surface == 'fused':
            await screen._render_fused_confirmation(callback, user, database, options, days=90, devices=3)
        else:
            await screen._render_direct_payment_methods(callback, user, database, checkout)
    create.assert_not_awaited()
    charge.assert_not_awaited()
    invoice.assert_not_awaited()
    database.commit.assert_not_awaited()
    database.flush.assert_not_awaited()
    database.execute.assert_not_awaited()
    return render.await_args.kwargs['caption'], render.await_args.kwargs['keyboard'].inline_keyboard, database


@pytest.mark.parametrize('surface', ['fused', 'existing'])
@pytest.mark.parametrize('language', ['ru', 'en'])
@pytest.mark.parametrize(
    ('balance', 'used_ru', 'used_en', 'due', 'rest_ru', 'rest_en'),
    [
        (5000, '50', '50', '99', None, None),
        (5050, '50', '50', '99', '0,50', '0.50'),
        (14899, '148', '148', '1', '0,99', '0.99'),
    ],
)
async def test_partial_balance_receipt_has_one_payment_action(
    environment, surface, language, balance, used_ru, used_en, due, rest_ru, rest_en
):
    caption, rows, _ = await _render(surface, language, balance)
    if language == 'ru':
        receipt = f'Цена: <b>149 ₽</b>\nС вашего баланса: −{used_ru} ₽\nК оплате: <b>{due} ₽</b>'
        promise, label, change = 'После оплаты подписка оформится сама', f'💰 Оплатить {due} ₽', '‹ Изменить'
    else:
        receipt = f'Price: <b>₽149</b>\nFrom your balance: −₽{used_en}\nTo pay: <b>₽{due}</b>'
        promise, label, change = (
            'After payment, your subscription will be set up automatically',
            f'💰 Pay ₽{due}',
            '‹ Change',
        )
    assert receipt in caption
    assert caption.count('К оплате:' if language == 'ru' else 'To pay:') == 1
    assert promise in caption
    assert ('Не хватает' if language == 'ru' else 'Shortage') not in caption
    assert ('полной суммой' if language == 'ru' else 'full amount') not in caption
    assert len(rows) == 2
    assert len(rows[0]) == 1
    assert rows[0][0].text == label
    assert rows[1][0].text == change
    assert rows[1][0].callback_data == ('df:e2' if surface == 'fused' else 'df:e:receipt-order')
    if surface == 'fused':
        assert rows[1][1].callback_data == 'df:x2'
    assert sum(button.web_app is not None for row in rows for button in row) == 1
    query = parse_qs(urlsplit(rows[0][0].web_app.url).query)
    assert query == {'returnTo': ['/subscription/purchase?from=checkout&period=90&devices=3'], 'amount': [due]}
    if rest_ru:
        assert (
            f'После оформления {rest_ru} ₽ останется на балансе.'
            if language == 'ru'
            else f'₽{rest_en} will remain on your balance after the order.'
        ) in caption
    else:
        assert ('останется на балансе' if language == 'ru' else 'will remain on your balance') not in caption


@pytest.mark.parametrize('surface', ['fused', 'existing'])
@pytest.mark.parametrize('language', ['ru', 'en'])
async def test_provider_minimum_receipt_balances_with_the_button(environment, monkeypatch, surface, language):
    monkeypatch.setattr(settings, 'PLATEGA_MIN_AMOUNT_KOPEKS', 10_000)
    caption, rows, _ = await _render(surface, language, 10_000)
    assert (
        'С вашего баланса: −49 ₽\nК оплате: <b>100 ₽</b>'
        if language == 'ru'
        else 'From your balance: −₽49\nTo pay: <b>₽100</b>'
    ) in caption
    assert (
        'После оформления 51 ₽ останется на балансе.'
        if language == 'ru'
        else '₽51 will remain on your balance after the order.'
    ) in caption
    assert rows[0][0].text == ('💰 Оплатить 100 ₽' if language == 'ru' else '💰 Pay ₽100')
    assert parse_qs(urlsplit(rows[0][0].web_app.url).query)['amount'] == ['100']


@pytest.mark.parametrize('surface', ['fused', 'existing'])
@pytest.mark.parametrize('language', ['ru', 'en'])
@pytest.mark.parametrize(
    'reason',
    [
        'restriction_topup',
        'restriction_subscription',
        'account_erasure_requested_at',
        'account_erased_at',
        'off',
        'stands',
        'typo',
        'platega_off',
        'no_methods',
        'no_cabinet',
        'full_price_topup',
        'in_flight',
    ],
)
async def test_unavailable_auto_order_keeps_the_old_screen(environment, monkeypatch, surface, language, reason):
    kwargs = {}
    if reason.startswith('restriction_'):
        kwargs[reason] = True
    elif reason.startswith('account_'):
        kwargs[reason] = datetime.now(UTC)
    elif reason in ('off', 'stands', 'typo'):
        monkeypatch.setattr(checkout_service, 'TOPUP_INTENT_ROLLOUT', reason)
        monkeypatch.setattr('app.services.user_service.is_test_account', lambda user: False)
    elif reason == 'platega_off':
        monkeypatch.setattr(type(settings), 'is_platega_enabled', lambda self: False)
    caption, rows, _ = await _render(
        surface,
        language,
        1 if reason == 'full_price_topup' else 5000,
        methods=reason != 'no_methods',
        cabinet=reason != 'no_cabinet',
        in_flight=reason == 'in_flight',
        **kwargs,
    )
    assert ('К оплате: <b>149 ₽</b>' if language == 'ru' else 'To pay: <b>₽149</b>') in caption
    assert ('💳 Баланс:' if language == 'ru' else '💳 Balance:') in caption
    assert ('оформится сама' if language == 'ru' else 'set up automatically') not in caption
    assert ('С вашего баланса' if language == 'ru' else 'From your balance') not in caption
    assert rows[-1][0].text == ('‹ Изменить параметры' if language == 'ru' else '‹ Change options')
    payments = [
        button
        for row in rows
        for button in row
        if button.web_app or (button.callback_data or '').startswith(('df:y:', 'df:y2:'))
    ]
    if reason == 'no_methods':
        assert not payments
        assert rows[0][0].callback_data == 'menu_support'
    else:
        assert sum(' · ' in button.text for button in payments) == 3
        if reason not in ('no_cabinet', 'full_price_topup', 'in_flight'):
            assert payments[0].text == ('💰 Доплатить 99 ₽' if language == 'ru' else '💰 Top up ₽99')
            assert (
                'Доплатите и продолжите покупку' if language == 'ru' else 'Top up and finish the purchase'
            ) in caption


@pytest.mark.parametrize('surface', ['fused', 'existing'])
@pytest.mark.parametrize('language', ['ru', 'en'])
@pytest.mark.parametrize('balance', [0, 14900, 20000])
async def test_zero_and_full_balance_keep_their_exact_screen(environment, surface, language, balance):
    caption, rows, database = await _render(surface, language, balance)
    expected = (
        '💳 <b>Ваш заказ</b>\n\n<b>Базовый</b>\n3 устройства · 3 месяца\nК оплате: <b>149 ₽</b>\n\n'
        + ('Выберите способ оплаты.' if balance == 0 else 'Оплатите с баланса.')
        if language == 'ru'
        else '💳 <b>Your order</b>\n\n<b>Basic</b>\n3 devices · 3 months\nTo pay: <b>₽149</b>\n\n'
        + ('Choose a payment method.' if balance == 0 else 'Pay from your balance.')
    )
    assert caption == expected
    assert rows[-1][0].text == ('‹ Изменить параметры' if language == 'ru' else '‹ Change options')
    database.scalar.assert_not_awaited()
    if balance:
        assert rows[0][0].text == ('Оплатить с баланса · 149 ₽' if language == 'ru' else 'Pay from balance · ₽149')
        assert rows[0][0].callback_data == (
            'df:a2:90:3:14900:1791000000' if surface == 'fused' else 'df:a:receipt-order'
        )
        assert len(rows) == 2
    else:
        assert len(rows) == 4
        for row, method in zip(rows[:3], ('sbp', 'cards_ru', 'crypto'), strict=True):
            query = parse_qs(urlsplit(row[0].web_app.url).query)
            assert query == (
                {'period': ['90'], 'devices': ['3'], 'method': [method], 'autostart': ['1']}
                if surface == 'fused'
                else {'checkout': ['receipt-order'], 'method': [method], 'autostart': ['1']}
            )


@pytest.mark.parametrize('surface', ['fused', 'existing'])
@pytest.mark.parametrize('language', ['ru', 'en'])
@pytest.mark.parametrize('method', ['sbp', 'cards_ru', 'crypto'])
@pytest.mark.parametrize('rollout', ['all', 'off'])
async def test_one_available_method_only_promotes_supported_auto_order(
    environment, monkeypatch, surface, language, method, rollout
):
    # Волны1/2: 05.10.2026 не обещает автоматическое оформление через медленную крипту.
    # При crypto-only сохраняем доступную прямую оплату; в откате старая ручная доплата жива.
    monkeypatch.setattr(checkout_service, 'TOPUP_INTENT_ROLLOUT', rollout)
    caption, rows, _ = await _render(surface, language, 5000, method_keys=(method,))
    auto_order = rollout == 'all' and method != 'crypto'
    assert (('оформится сама' if language == 'ru' else 'set up automatically') in caption) == auto_order
    payments = [button for row in rows for button in row if button.web_app]
    if auto_order:
        assert len(payments) == 1
        assert payments[0].text == ('💰 Оплатить 99 ₽' if language == 'ru' else '💰 Pay ₽99')
    else:
        assert ('С вашего баланса' if language == 'ru' else 'From your balance') not in caption
        expected_count = 2 if rollout == 'off' else 1
        assert len(payments) == expected_count
        assert parse_qs(urlsplit(payments[-1].web_app.url).query)['method'] == [method]
        assert payments[-1].text.endswith(' · 149 ₽' if language == 'ru' else ' · ₽149')
        if rollout == 'off':
            assert payments[0].text == ('💰 Доплатить 99 ₽' if language == 'ru' else '💰 Top up ₽99')
