from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, patch

import pytest

from app.handlers.subscription.tariff_purchase import show_funnel_tariffs, show_tariffs_list


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


@pytest.mark.asyncio
async def test_funnel_tariffs_reopens_an_existing_native_order_when_new_orders_are_disabled(monkeypatch) -> None:
    monkeypatch.setattr('app.handlers.subscription.tariff_purchase.settings.DEVICE_FIRST_NEW_CHECKOUTS_ENABLED', False)
    callback = SimpleNamespace(answer=AsyncMock())
    user = SimpleNamespace(language='ru', promo_group_id=None)
    state = SimpleNamespace(clear=AsyncMock())

    with (
        patch(
            'app.handlers.subscription.tariff_purchase.get_tariffs_for_user',
            AsyncMock(return_value=[]),
        ),
        patch(
            'app.handlers.subscription.device_first.show_device_first_entry',
            AsyncMock(return_value=True),
        ) as show_entry,
    ):
        await show_funnel_tariffs(callback, user, AsyncMock(), state)

    show_entry.assert_awaited_once_with(callback, user, ANY, state, origin_callback='back_to_menu')


@pytest.mark.asyncio
async def test_legacy_tariff_entrypoints_redirect_to_device_first_when_eligible() -> None:
    callback = SimpleNamespace(answer=AsyncMock())
    user = SimpleNamespace(language='ru', promo_group_id=None)
    state = SimpleNamespace(clear=AsyncMock())

    with patch(
        'app.handlers.subscription.device_first.show_device_first_entry',
        AsyncMock(return_value=True),
    ) as show_entry:
        await show_tariffs_list(callback, user, AsyncMock(), state)

    show_entry.assert_awaited_once_with(callback, user, ANY, state, origin_callback='back_to_menu')
