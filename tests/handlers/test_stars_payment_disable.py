from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.handlers.stars_payments import handle_pre_checkout_query


@pytest.mark.anyio('asyncio')
async def test_pre_checkout_rejects_existing_stars_invoice_after_disable(monkeypatch: pytest.MonkeyPatch) -> None:
    """A previously sent invoice must not remain payable after Stars is disabled."""
    monkeypatch.setattr(settings, 'TELEGRAM_STARS_ENABLED', False, raising=False)
    query = SimpleNamespace(
        invoice_payload='balance_101_50000',
        from_user=SimpleNamespace(id=101),
        total_amount=500,
        answer=AsyncMock(),
    )

    await handle_pre_checkout_query(query)

    query.answer.assert_awaited_once_with(
        ok=False,
        error_message='Оплата через Telegram Stars отключена.',
    )
