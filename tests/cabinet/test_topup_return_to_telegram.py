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


def test_direct_card_payer_is_sent_back_into_telegram(monkeypatch) -> None:
    """🔴 Пётр: баланс ноль, кнопки «Доплатить» он не видит и платит полную цену картой.

    До этапа его возвращали на адрес САЙТА кабинета — то есть на форму входа, потому что во
    внешнем браузере он не авторизован. Замер 24.08.2026: таких **140 из 285** живых людей.
    Расширение этапа на эту дорогу — прямое решение владельца 24.08.2026.

    ⚠️ Сторож переписан в том же коммите: прежний закреплял старый договор («всегда адрес
    сайта») и обязан был покраснеть.
    """
    from app.services import device_first_payment_service as dfps

    monkeypatch.setattr(settings, 'CABINET_URL', 'https://cabinet.example.test', raising=False)
    monkeypatch.setattr(settings, 'BOT_USERNAME', 'teplo_VPN_bot', raising=False)

    order = '550e8400-e29b-41d4-a716-446655440000'
    assert dfps._direct_checkout_return_url(order) == (f'https://t.me/teplo_VPN_bot?startapp=co_{order}_ok')


def test_failed_direct_payment_returns_to_telegram_with_the_failure_mark(monkeypatch) -> None:
    """🔴 ЭТАП 2б: отказ тоже уводим в Телеграм — но ТОЛЬКО с меткой `_fail`.

    ⛔ Сторож ПЕРЕПИСАН 28.08.2026, а не подкручен. Раньше он утверждал обратное («отказ в
    Телеграм не уводим»), и это было верно ровно до тех пор, пока экран заказа не умел
    говорить об отказе. Он научился: `DeviceFirstConfigurator.tsx:159-176` читает
    `payment=failed`, показывает `providerDeclinedNotice` и снимает метку с адреса. Основание
    прежнего запрета исчезло вместе с ним, поэтому и утверждение здесь другое.

    🔴 СТЕРЕЖЁТ МИНУ EX, а не просто «есть диплинк». Хвост `_fail` — единственное, что
    отличает эту правку от вредной: у сборщика умолчание `failed=False` даёт `_ok`, и вызов
    без явного аргумента вернул бы отказавшего с меткой «оплачено». Кабинет по `_ok` не
    показал бы отказ вовсе — то есть наивное снятие запрета было бы хуже формы входа.
    Поэтому проверяем не «в адресе есть t.me», а именно исход.
    """
    from app.services import device_first_payment_service as dfps

    monkeypatch.setattr(settings, 'CABINET_URL', 'https://cabinet.example.test', raising=False)
    monkeypatch.setattr(settings, 'BOT_USERNAME', 'teplo_VPN_bot', raising=False)

    order = '550e8400-e29b-41d4-a716-446655440000'
    failed_url = dfps._direct_checkout_return_url(order, failed=True)

    assert failed_url == f'https://t.me/teplo_VPN_bot?startapp=co_{order}_fail'
    # 🔴 Улика мины EX: исход именно `fail`. Проверка «просто не равно ok-адресу» пропустила бы
    # опечатку в самой метке, а `endswith('_fail')` — подмену номера заказа. Нужны обе.
    assert failed_url.endswith('_fail')
    assert failed_url != dfps._direct_checkout_return_url(order)


def test_successful_direct_payment_still_returns_with_the_ok_mark(monkeypatch) -> None:
    """Второй конец шкалы: правка 2б не должна была тронуть успех.

    Без этой проверки «прокинуть признак» можно было бы выполнить, поменяв умолчание, — и
    тогда УСПЕШНО заплативший получил бы метку отказа, а кабинет объявил бы ему, что оплата
    не прошла. Это ровно тот же вред, что и мина EX, только зеркальный.
    """
    from app.services import device_first_payment_service as dfps

    monkeypatch.setattr(settings, 'CABINET_URL', 'https://cabinet.example.test', raising=False)
    monkeypatch.setattr(settings, 'BOT_USERNAME', 'teplo_VPN_bot', raising=False)

    order = '550e8400-e29b-41d4-a716-446655440000'
    assert dfps._direct_checkout_return_url(order, failed=False).endswith('_ok')


def test_failed_direct_payment_falls_back_to_the_website_keeping_the_failure_mark(monkeypatch) -> None:
    """Диплинк собрать нечем — отказ остаётся на сайте, но признак отказа НЕ теряется.

    Запасной путь существовал и раньше; проверка нужна потому, что 2б переставил его за
    диплинк. Потеряй он здесь `payment=failed` — человек упал бы на экран заказа без единого
    слова о том, что банк отказал, то есть ровно в ту немоту, которую этап и чинит.
    """
    from app.services import device_first_payment_service as dfps

    monkeypatch.setattr(settings, 'CABINET_URL', 'https://cabinet.example.test', raising=False)
    monkeypatch.setattr(settings, 'BOT_USERNAME', None, raising=False)

    failed_url = dfps._direct_checkout_return_url('abc123', failed=True)
    assert failed_url == 'https://cabinet.example.test/subscription/purchase?checkout=abc123&payment=failed'


def test_direct_card_payment_falls_back_to_the_website_not_to_nothing(monkeypatch) -> None:
    """Второй конец шкалы. Диплинк собрать нечем — остаётся прежний адрес сайта.

    Пустоту возвращать НЕЛЬЗЯ: вызывающий трактует её как отказ создать счёт, то есть человек
    не сможет заплатить вовсе. Это дороже плохого возврата.
    """
    from app.services import device_first_payment_service as dfps

    monkeypatch.setattr(settings, 'CABINET_URL', 'https://cabinet.example.test', raising=False)
    monkeypatch.setattr(settings, 'BOT_USERNAME', None, raising=False)

    assert dfps._direct_checkout_return_url('abc123') == (
        'https://cabinet.example.test/subscription/purchase?checkout=abc123'
    )


def test_direct_card_payment_still_refuses_a_telegram_cabinet_url(monkeypatch) -> None:
    """⛔ Прежний запрет остаётся: приклеивать свой путь к чужому хосту нельзя.

    Если `CABINET_URL` окажется телеграмным, а имени бота нет, собрать нечего — и функция
    обязана честно отдать None, а не `https://t.me/subscription/purchase?checkout=…`.
    """
    from app.services import device_first_payment_service as dfps

    monkeypatch.setattr(settings, 'CABINET_URL', 'https://t.me/teplo_VPN_bot', raising=False)
    monkeypatch.setattr(settings, 'BOT_USERNAME', None, raising=False)

    assert dfps._direct_checkout_return_url('abc123') is None


def test_checkout_label_cannot_smuggle_characters(monkeypatch) -> None:
    """Номер заказа уходит в адрес. Всё, что не похоже на номер, обязано дать запасной путь."""
    from app.services import device_first_payment_service as dfps

    monkeypatch.setattr(settings, 'BOT_USERNAME', 'teplo_VPN_bot', raising=False)

    for junk in ('', 'abc def', 'abc/../evil', 'abc&x=1', 'заказ', 'abc\n', 'a' * 65):
        assert dfps._telegram_direct_checkout_return_url(junk) == ''


def test_two_labels_use_separators_that_cannot_appear_inside_them(monkeypatch) -> None:
    """🔴 Договор двух меток. Разделители разные НАМЕРЕННО.

    Номер заказа — `uuid4`: дефисы есть, подчёркиваний нет, поэтому его метка на `_`.
    Имя способа оплаты — наоборот, поэтому его метка на `-`. Один разделитель на обе сделал
    бы разбор двусмысленным. Сторож ловит попытку сложить их одинаково.
    """
    from app.services import device_first_payment_service as dfps

    monkeypatch.setattr(settings, 'BOT_USERNAME', 'teplo_VPN_bot', raising=False)

    order_label = dfps._telegram_direct_checkout_return_url('550e8400-e29b-41d4-a716-446655440000')
    assert '?startapp=co_' in order_label
    assert '?startapp=tup-' in balance_route._telegram_top_up_return_url('platega', failed=False)

    # И ни один способ из списка проверенных не смеет содержать дефис: иначе метка пополнения
    # станет двусмысленной, а кабинет вернёт `null` и приземлит человека на Главную.
    for method in balance_route._TELEGRAM_RETURN_METHODS:
        assert '-' not in method, method
        assert method == method.lower()
