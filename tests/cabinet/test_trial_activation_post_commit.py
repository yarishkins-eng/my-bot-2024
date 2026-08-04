from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.cabinet.routes.subscription_modules.purchase import _run_trial_post_commit
from app.config import settings
from app.services.trial_activation_service import TrialActivationResult


@pytest.mark.asyncio
async def test_existing_trial_without_panel_uuid_repairs_entitlement_without_replaying_side_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A retry must heal the exact partial-commit state seen in production."""

    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: False)
    user = SimpleNamespace(id=174, remnawave_uuid=None)
    subscription = SimpleNamespace(id=126, remnawave_uuid=None)
    result = TrialActivationResult(
        subscription=subscription,
        charged_amount_kopeks=0,
        abandoned_checkout=None,
        already_active=True,
    )
    db = AsyncMock()
    service = MagicMock(is_configured=True)
    service.create_remnawave_user = AsyncMock(return_value=object())

    with (
        patch(
            'app.cabinet.routes.subscription_modules.purchase.SubscriptionService',
            return_value=service,
        ),
        patch('app.services.remnawave_retry_queue.remnawave_retry_queue.enqueue') as enqueue,
        patch(
            'app.cabinet.routes.subscription_modules.purchase.emit_transaction_side_effects',
            new=AsyncMock(),
        ) as emit,
        patch(
            'app.services.admin_notification_service.AdminNotificationService.send_trial_activation_notification',
            new=AsyncMock(),
        ) as notify_admin,
        patch('app.utils.funnel_notify.notify_trial_menu', new=AsyncMock()) as notify_menu,
    ):
        await _run_trial_post_commit(db, user=user, result=result, yandex_cid=None)

    service.create_remnawave_user.assert_awaited_once_with(db, subscription)
    db.refresh.assert_awaited_once_with(subscription)
    enqueue.assert_not_called()
    emit.assert_not_awaited()
    notify_admin.assert_not_awaited()
    notify_menu.assert_not_awaited()


@pytest.mark.asyncio
async def test_existing_trial_with_panel_uuid_is_a_side_effect_free_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: False)
    user = SimpleNamespace(id=174, remnawave_uuid='existing-panel-user')
    subscription = SimpleNamespace(id=126, remnawave_uuid=None)
    result = TrialActivationResult(
        subscription=subscription,
        charged_amount_kopeks=0,
        abandoned_checkout=None,
        already_active=True,
    )
    db = AsyncMock()
    service = MagicMock(is_configured=True)
    service.create_remnawave_user = AsyncMock()

    with patch(
        'app.cabinet.routes.subscription_modules.purchase.SubscriptionService',
        return_value=service,
    ):
        await _run_trial_post_commit(db, user=user, result=result, yandex_cid=None)

    service.create_remnawave_user.assert_not_awaited()
    db.refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_existing_trial_repair_enters_retry_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: False)
    user = SimpleNamespace(id=174, remnawave_uuid=None)
    subscription = SimpleNamespace(id=126, remnawave_uuid=None)
    result = TrialActivationResult(
        subscription=subscription,
        charged_amount_kopeks=0,
        abandoned_checkout=None,
        already_active=True,
    )
    db = AsyncMock()
    service = MagicMock(is_configured=True)
    service.create_remnawave_user = AsyncMock(return_value=None)

    with (
        patch(
            'app.cabinet.routes.subscription_modules.purchase.SubscriptionService',
            return_value=service,
        ),
        patch('app.services.remnawave_retry_queue.remnawave_retry_queue.enqueue') as enqueue,
    ):
        await _run_trial_post_commit(db, user=user, result=result, yandex_cid=None)

    enqueue.assert_called_once_with(subscription_id=126, user_id=174, action='create')
