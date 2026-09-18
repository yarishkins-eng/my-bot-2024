from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.handlers.subscription import purchase


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('language', 'expected_days', 'unexpected_days'),
    [('ru', '21 день', '21 days'), ('en', '21 days', '21 дней')],
)
async def test_pending_trial_checkout_uses_localized_day_declension(
    monkeypatch, language, expected_days, unexpected_days
):
    checkout = SimpleNamespace(
        tariff_name='Базовый',
        device_limit=1,
        period_days=21,
        amount_kopeks=24_900,
    )
    monkeypatch.setattr(
        purchase,
        'get_trial_checkout_context',
        AsyncMock(return_value=SimpleNamespace(state='pending_invoice', checkout=checkout)),
    )
    monkeypatch.setattr('app.utils.miniapp_buttons.build_cabinet_url', lambda _path: None)
    callback = SimpleNamespace(
        message=SimpleNamespace(edit_text=AsyncMock()),
        answer=AsyncMock(),
    )

    assert await purchase._show_trial_checkout_resolution(
        callback,
        SimpleNamespace(id=17, language=language),
        AsyncMock(),
    )
    message = callback.message.edit_text.await_args.args[0]
    assert expected_days in message
    assert unexpected_days not in message
