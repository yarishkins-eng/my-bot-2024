from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.handlers.subscription.tariff_purchase import show_funnel_tariffs


@pytest.mark.asyncio
async def test_funnel_tariffs_returns_device_first_period_screen_to_main_menu(monkeypatch) -> None:
    """The entry callback must not reopen the same screen in a Back-button loop."""
    monkeypatch.setattr('app.handlers.subscription.tariff_purchase.settings.DEVICE_FIRST_NEW_CHECKOUTS_ENABLED', True)
    callback = SimpleNamespace(answer=AsyncMock())
    user = SimpleNamespace(language='ru', promo_group_id=None)
    state = SimpleNamespace(clear=AsyncMock())
    options = {'eligible': True, 'tariff': {'name': 'Premium'}}

    with (
        patch(
            'app.handlers.subscription.tariff_purchase.get_tariffs_for_user',
            AsyncMock(return_value=[]),
        ),
        patch(
            'app.services.device_first_checkout_service.build_purchase_options',
            AsyncMock(return_value=options),
        ),
        patch(
            'app.handlers.subscription.device_first.show_device_first_entry',
            AsyncMock(return_value=True),
        ) as show_entry,
    ):
        await show_funnel_tariffs(callback, user, AsyncMock(), state)

    show_entry.assert_awaited_once()
    assert show_entry.await_args.kwargs['origin_callback'] == 'back_to_menu'
