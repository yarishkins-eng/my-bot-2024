"""VK-5 renewal receipt and VK-12 first-purchase chat date; native first screen stays unchanged."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from app.handlers.subscription import device_first as handler
from app.services import device_first_checkout_service as service


END = datetime(2026, 10, 12, 22, 31, tzinfo=UTC)
RENEWED = {
    'ru': '✅ Ваша VPN-подписка готова — продлена до 13.10.2026 01:31 (МСК). VPN работает как раньше.',
    'en': '✅ Your VPN subscription is ready — extended until 13.10.2026 01:31 (Moscow time). '
    'Your VPN keeps working as before.',
}
FIRST = {
    'ru': '✅ Ваша VPN-подписка готова — до 13.10.2026 01:31 (МСК). Откройте кабинет, чтобы подключиться.',
    'en': '✅ Your VPN subscription is ready — until 13.10.2026 01:31 (Moscow time). Open the cabinet to connect.',
}
FALLBACK = {
    'ru': '✅ Ваша VPN-подписка готова. Откройте кабинет, чтобы подключиться.',
    'en': '✅ Your VPN subscription is ready. Open the cabinet to connect.',
}
SCREEN = {'ru': '✅ <b>VPN готов</b>', 'en': '✅ <b>VPN is ready</b>'}
MISSING = object()


async def both_surfaces(monkeypatch, *, language, snapshot, end=END):
    user = SimpleNamespace(id=7, telegram_id=77, language=language, balance_kopeks=0)
    checkout = SimpleNamespace(id=11, user_id=7, target_snapshot=snapshot, fulfilled_end_at=end)
    if snapshot is MISSING:
        del checkout.target_snapshot
    bot = SimpleNamespace(send_message=AsyncMock())
    db = SimpleNamespace(get=AsyncMock(return_value=user))
    await service._send_client_ready_message(db, bot=bot, checkout=checkout)
    render = AsyncMock()
    monkeypatch.setattr(handler, 'serialize_checkout', lambda *a, **k: {'ui_state': 'ready'})
    monkeypatch.setattr(handler, 'edit_or_answer_photo', render)
    await handler._render_checkout(SimpleNamespace(), user, db, checkout)
    return bot.send_message.await_args.args[1], render.await_args.kwargs['caption']


@pytest.mark.asyncio
@pytest.mark.parametrize('language', ['ru', 'en'])
async def test_paid_target_gets_same_moscow_renewal_receipt_on_both_surfaces(monkeypatch, language):
    # The classification cannot depend on matching tariff/id: only the explicit trial flag.
    actual = await both_surfaces(monkeypatch, language=language, snapshot={'is_trial': False})
    assert actual == (RENEWED[language], RENEWED[language])


@pytest.mark.asyncio
@pytest.mark.parametrize('language', ['ru', 'en'])
@pytest.mark.parametrize(
    'snapshot',
    [
        None,
        {},
        {'is_trial': True, 'id': 75, 'tariff_id': 3},
        {'is_trial': None},
        {'is_trial': 0},
        {'is_trial': 'false'},
        MISSING,
    ],
)
async def test_first_purchase_chat_names_moscow_date_but_native_stays_byte_exact(monkeypatch, language, snapshot):
    actual = await both_surfaces(monkeypatch, language=language, snapshot=snapshot)
    assert actual == (FIRST[language], SCREEN[language])


@pytest.mark.asyncio
@pytest.mark.parametrize('language', ['ru', 'en'])
@pytest.mark.parametrize('snapshot', [None, {}, {'is_trial': True}, MISSING])
@pytest.mark.parametrize('end', [None, object()])
async def test_first_purchase_without_a_valid_date_keeps_fallback(monkeypatch, language, snapshot, end):
    actual = await both_surfaces(monkeypatch, language=language, snapshot=snapshot, end=end)
    assert actual == (FALLBACK[language], SCREEN[language])


@pytest.mark.asyncio
@pytest.mark.parametrize('language', ['ru', 'en'])
@pytest.mark.parametrize(
    'end', [END.replace(tzinfo=None), datetime(2026, 10, 13, 6, 31, tzinfo=ZoneInfo('Asia/Shanghai'))]
)
async def test_first_purchase_normalizes_naive_utc_and_other_timezones(monkeypatch, language, end):
    actual = await both_surfaces(monkeypatch, language=language, snapshot={'is_trial': True}, end=end)
    assert actual == (FIRST[language], SCREEN[language])


@pytest.mark.asyncio
@pytest.mark.parametrize('language', ['ru', 'en'])
async def test_first_purchase_formatting_exception_keeps_delivery(monkeypatch, language):
    def broken_zone(*args):
        raise ValueError('timezone unavailable')

    monkeypatch.setattr(service, 'ZoneInfo', broken_zone)
    actual = await both_surfaces(monkeypatch, language=language, snapshot={'is_trial': True})
    assert actual == (FALLBACK[language], SCREEN[language])


@pytest.mark.asyncio
@pytest.mark.parametrize('language', ['ru', 'en'])
@pytest.mark.parametrize(
    'snapshot,end', [({'is_trial': False}, None), ({'is_trial': False}, object()), (object(), END)]
)
async def test_formatting_failure_never_kills_ready_delivery(monkeypatch, language, snapshot, end):
    actual = await both_surfaces(monkeypatch, language=language, snapshot=snapshot, end=end)
    assert actual == (FALLBACK[language], SCREEN[language])
