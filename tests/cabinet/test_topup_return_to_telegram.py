"""Этап В-1: возврат из банка обязан приводить в Телеграм, а не на форму входа.

Живой проход владельца 23.08.2026 (iPhone): после оплаты в Сбербанке кнопка Platega
«Вернуться в магазин» уводила на `cabinet.lilulalu.xyz`, где он не авторизован. Причина —
адрес возврата строился как обычный адрес сайта, а человек в этот момент во ВНЕШНЕМ
браузере, куда сессия мини-приложения не доезжает.

Сторожа на адрес возврата не было ни одного: подменить его на что угодно можно было, не
покрасив ни один тест.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.cabinet.routes import balance as balance_route
from app.cabinet.schemas.balance import PaymentMethodResponse, TopUpRequest
from app.config import settings


# Метки зашиты ЛИТЕРАЛАМИ: их разбирает кабинет (`src/utils/telegramStartParam.ts`), и сторож,
# собирающий ожидание тем же выражением, что и код, доказывал бы только сам себя.
TELEGRAM_SUCCESS_URL = 'https://t.me/teplo_VPN_bot?startapp=tup-platega-ok'
TELEGRAM_FAILED_URL = 'https://t.me/teplo_VPN_bot?startapp=tup-platega-fail'


@pytest.fixture
def platega_env(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Боевое окружение Platega плюс перехват адресов, уходящих провайдеру."""

    async def fake_methods(*_, **__):
        return [
            PaymentMethodResponse(
                id='platega',
                name='Platega',
                min_amount_kopeks=100,
                max_amount_kopeks=100_000_000,
                options=[{'id': '2', 'name': 'СБП (QR)'}],
            )
        ]

    captured: dict[str, object] = {}

    class FakePaymentService:
        async def create_platega_payment(self, **kwargs):
            captured.update(kwargs)
            return {
                'local_payment_id': 777,
                'redirect_url': 'https://pay.example.test/invoice',
                'status': 'PENDING',
                'expires_at': None,
            }

    monkeypatch.setattr(balance_route, 'get_payment_methods', fake_methods)
    monkeypatch.setattr(balance_route, 'PaymentService', FakePaymentService)
    monkeypatch.setattr(settings, 'PLATEGA_ENABLED', True, raising=False)
    monkeypatch.setattr(settings, 'PLATEGA_MERCHANT_ID', 'test-merchant', raising=False)
    monkeypatch.setattr(settings, 'PLATEGA_SECRET', 'test-secret', raising=False)
    monkeypatch.setattr(settings, 'PLATEGA_ACTIVE_METHODS', '2', raising=False)
    monkeypatch.setattr(settings, 'CABINET_URL', 'https://cabinet.example.test', raising=False)
    monkeypatch.setattr(settings, 'BOT_USERNAME', 'teplo_VPN_bot', raising=False)
    return captured


async def _create(**request_kwargs) -> None:
    await balance_route.create_topup(
        request=TopUpRequest(amount_kopeks=6_000, payment_method='platega', payment_option='2', **request_kwargs),
        user=SimpleNamespace(id=1, telegram_id=123, language='ru'),
        db=SimpleNamespace(),
    )


@pytest.mark.asyncio
async def test_telegram_payer_is_sent_back_into_telegram(platega_env) -> None:
    """Ушёл платить из мини-приложения → провайдер вернёт в Телеграм, а не на сайт."""
    await _create(return_surface='telegram')

    assert platega_env['return_url'] == TELEGRAM_SUCCESS_URL
    assert platega_env['failed_url'] == TELEGRAM_FAILED_URL


@pytest.mark.asyncio
async def test_browser_payer_keeps_the_website_return(platega_env) -> None:
    """Второй конец шкалы.

    Проверка «уводит в Телеграм» одна прошла бы и у кода, который уводит туда ВСЕГДА —
    включая человека, который сидит в кабинете обычным браузером и в Телеграм не собирался.
    """
    await _create(return_surface='web')

    assert platega_env['return_url'].startswith('https://cabinet.example.test/')
    assert platega_env['failed_url'].startswith('https://cabinet.example.test/')


@pytest.mark.asyncio
async def test_old_cabinet_build_keeps_previous_behaviour(platega_env) -> None:
    """Кабинет выкладывается первым, но пока не выложен — поля нет, и поведение прежнее."""
    await _create()

    assert platega_env['return_url'].startswith('https://cabinet.example.test/')
    assert platega_env['failed_url'].startswith('https://cabinet.example.test/')


@pytest.mark.asyncio
async def test_unknown_bot_username_never_yields_a_broken_link(platega_env, monkeypatch) -> None:
    """Имя бота приходит из `get_me()` при старте, а не из `.env`.

    Если синхронизация не отработала, собрать диплинк нечем. Тогда обязан остаться прежний
    адрес сайта — но НИКОГДА не обрубок вида `https://t.me/?startapp=…`.
    """
    monkeypatch.setattr(settings, 'BOT_USERNAME', None, raising=False)

    await _create(return_surface='telegram')

    assert platega_env['return_url'].startswith('https://cabinet.example.test/')
    assert 't.me' not in str(platega_env['return_url'])


@pytest.mark.asyncio
async def test_both_outcomes_switch_together(platega_env) -> None:
    """Половина замены оставила бы успех в Телеграме, а отказ — на форме входа."""
    await _create(return_surface='telegram')

    assert ('t.me' in str(platega_env['return_url'])) == ('t.me' in str(platega_env['failed_url']))


def test_only_providers_proven_to_accept_telegram_links_are_switched(monkeypatch) -> None:
    """Часть шлюзов сверяет адрес возврата при СОЗДАНИИ счёта.

    Отказ там означает не «плохой возврат», а «человек вообще не может заплатить». Поэтому
    список закрытый, и расширять его можно только доказав приём t.me у конкретного шлюза.
    """
    monkeypatch.setattr(settings, 'BOT_USERNAME', 'teplo_VPN_bot', raising=False)

    assert balance_route._telegram_top_up_return_url('platega', failed=False) == TELEGRAM_SUCCESS_URL
    for untested in ('yookassa', 'heleket', 'mulenpay', 'telegram_stars'):
        assert balance_route._telegram_top_up_return_url(untested, failed=False) == ''


def test_method_name_cannot_smuggle_characters_into_the_start_param(monkeypatch) -> None:
    """Метка уходит в адрес. Telegram принимает только `A-Za-z0-9_-`, и всё прочее обязано
    давать пустую строку, а не собранную кое-как ссылку."""
    monkeypatch.setattr(settings, 'BOT_USERNAME', 'teplo_VPN_bot', raising=False)

    for junk in ('plate ga', 'platega&x=1', 'platega/../evil', 'платега', ''):
        assert balance_route._telegram_top_up_return_url(junk, failed=False) == ''


# ─────────────────────────────────────────────────────────────────────────────
# 🔴 Граница этапа: прямая оплата картой обязана остаться нетронутой.
# ─────────────────────────────────────────────────────────────────────────────


def test_direct_card_payment_return_url_is_untouched(monkeypatch) -> None:
    """⛔ Прямая оплата картой обязана остаться на адресе САЙТА кабинета.

    `_direct_checkout_return_url` намеренно отказывается от t.me-адресов — на этом держится
    оплата полной ценой. Этап В-1 её не трогал, и сторож обязан покраснеть, если тронут.
    """
    from app.services import device_first_payment_service as dfps

    monkeypatch.setattr(settings, 'CABINET_URL', 'https://cabinet.example.test', raising=False)
    assert dfps._direct_checkout_return_url('abc123') == (
        'https://cabinet.example.test/subscription/purchase?checkout=abc123'
    )

    # А t.me в этой настройке обязан её ОТКЛЮЧИТЬ, а не породить ссылку в никуда.
    monkeypatch.setattr(settings, 'CABINET_URL', 'https://t.me/teplo_VPN_bot', raising=False)
    assert dfps._direct_checkout_return_url('abc123') is None


def test_startapp_builder_refuses_anything_telegram_would_not_accept(monkeypatch) -> None:
    """Второй забор, ниже списка способов.

    Telegram принимает в `startapp` только `A-Za-z0-9_-` (1–512). Собранная мимо этого ссылка
    — это ссылка, по которой человек никуда не попадёт. Забор проверяется отдельно, потому что
    сверху его прикрывает список способов, и через него сюда мусор не доходит.
    """
    from app.utils.miniapp_buttons import build_main_miniapp_startapp_url

    monkeypatch.setattr(settings, 'BOT_USERNAME', 'teplo_VPN_bot', raising=False)

    assert build_main_miniapp_startapp_url('tup-platega-ok') == TELEGRAM_SUCCESS_URL
    for junk in ('tup platega ok', 'tup/platega', 'tup&x=1', 'туп', '', 'a' * 513):
        assert build_main_miniapp_startapp_url(junk) == ''

    # Нет имени бота — нет ссылки. Обрубка `https://t.me/?startapp=…` быть не должно никогда.
    monkeypatch.setattr(settings, 'BOT_USERNAME', None, raising=False)
    assert build_main_miniapp_startapp_url('tup-platega-ok') == ''
