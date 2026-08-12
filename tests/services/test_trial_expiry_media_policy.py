from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.monitoring_service import MonitoringService


def _service_with_bot(bot):
    service = MonitoringService.__new__(MonitoringService)
    service.bot = bot
    return service


@pytest.mark.asyncio
async def test_trial_expiry_is_plain_text_and_has_tariffs_button() -> None:
    bot = SimpleNamespace(send_message=AsyncMock())
    service = _service_with_bot(bot)
    user = SimpleNamespace(telegram_id=123, language='ru')

    result = await service._send_trial_expired_notification(user)

    assert result is True
    sent = bot.send_message.await_args.kwargs
    assert 'Пробный период завершён' in sent['text']
    assert [button.callback_data for row in sent['reply_markup'].inline_keyboard for button in row] == [
        'menu_buy',
        'funnel_tariffs',
    ]


@pytest.mark.asyncio
async def test_trial_discount_notification_is_plain_text_and_keeps_dynamic_percent() -> None:
    bot = SimpleNamespace(send_message=AsyncMock())
    service = _service_with_bot(bot)
    user = SimpleNamespace(telegram_id=123, language='ru')

    result = await service._send_trial_expired_discount_notification(
        user,
        subscription=SimpleNamespace(),
        percent=17,
        expires_at=datetime.now(UTC),
        offer_id=42,
    )

    assert result is True
    assert '17%' in bot.send_message.await_args.kwargs['text']
