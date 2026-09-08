from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.handlers import start as start_module
from app.services.campaign_service import CampaignBonusResult


TELEGRAM_ID = 780003


def _message() -> SimpleNamespace:
    return SimpleNamespace(
        from_user=SimpleNamespace(
            id=TELEGRAM_ID,
            username='race_user',
            first_name='Race',
            last_name=None,
        ),
        bot=MagicMock(),
        answer=AsyncMock(),
    )


def _state() -> SimpleNamespace:
    return SimpleNamespace(
        get_data=AsyncMock(return_value={'language': 'ru', 'campaign_id': 9}),
        clear=AsyncMock(),
    )


@pytest.mark.asyncio
async def test_two_completion_results_emit_canonical_campaign_message_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Model the create-user unique-race loser returning the winner's row.

    Both handlers have already observed ``user is None``.  The fake create
    boundary returns one shared row, as CRUD does after its telegram_id
    IntegrityError fallback. The fake campaign service models its documented
    ``is_new_registration`` contract; this test verifies the real handler emits
    admin/client bonus messages only for that canonical winner. This
    handler-level test does not prove financial reward serialization.
    """
    user = SimpleNamespace(
        id=501,
        telegram_id=TELEGRAM_ID,
        username='race_user',
        first_name='Race',
        last_name=None,
        full_name='Race',
        language='ru',
        status='active',
        balance_kopeks=0,
        referred_by_id=None,
        has_had_paid_subscription=False,
        subscriptions=[],
    )
    campaign = SimpleNamespace(id=9, name='Campaign 9', is_active=True)
    create_lock = asyncio.Lock()
    created_rows: dict[int, SimpleNamespace] = {}
    create_calls = 0

    async def _create_user(**kwargs):
        nonlocal create_calls
        create_calls += 1
        await asyncio.sleep(0)
        async with create_lock:
            return created_rows.setdefault(kwargs['telegram_id'], user)

    registration_edges: set[tuple[int, int]] = set()
    reward_count = 0
    service_lock = asyncio.Lock()

    class _CampaignService:
        async def apply_campaign_bonus(self, _db, candidate_user, candidate_campaign):
            nonlocal reward_count
            edge = (candidate_campaign.id, candidate_user.id)
            async with service_lock:
                created = edge not in registration_edges
                registration_edges.add(edge)
                if created:
                    reward_count += 1
                    candidate_user.balance_kopeks += 5000
            return CampaignBonusResult(
                success=True,
                bonus_type='balance',
                balance_kopeks=5000,
                is_new_registration=created,
            )

    notifier = SimpleNamespace(send_campaign_registration_notification=AsyncMock())
    texts = SimpleNamespace(
        CAMPAIGN_BONUS_BALANCE='bonus:{name}:{amount}',
        format_price=lambda amount: str(amount),
        t=lambda _key, default, **_kwargs: default,
    )

    monkeypatch.setattr(start_module, 'get_user_by_telegram_id', AsyncMock(return_value=None))
    monkeypatch.setattr(start_module, 'find_phantom_user_by_username', AsyncMock(return_value=None))
    monkeypatch.setattr(start_module, 'generate_unique_referral_code', AsyncMock(return_value='refRace001'))
    monkeypatch.setattr(start_module, 'create_user', _create_user)
    monkeypatch.setattr(start_module, 'get_campaign_by_id', AsyncMock(return_value=campaign))
    monkeypatch.setattr(start_module, 'AdvertisingCampaignService', _CampaignService)
    monkeypatch.setattr(start_module, 'AdminNotificationService', lambda _bot: notifier)
    monkeypatch.setattr('app.services.referral_service.clear_pending_campaign', AsyncMock())
    monkeypatch.setattr(start_module, 'get_texts', lambda _language: texts)
    monkeypatch.setattr(start_module, 'delete_pending_payload_from_redis', AsyncMock())
    monkeypatch.setattr(start_module, '_activate_pending_gift_after_registration', AsyncMock())
    monkeypatch.setattr(start_module, '_persist_pending_subid_after_registration', AsyncMock())
    monkeypatch.setattr('app.database.crud.welcome_text.get_welcome_text_for_user', AsyncMock(return_value=None))
    monkeypatch.setattr(start_module, 'get_active_pinned_message', AsyncMock(return_value=None))
    monkeypatch.setattr(start_module, 'get_main_menu_text', AsyncMock(return_value='menu'))
    monkeypatch.setattr(start_module, 'get_main_menu_keyboard_async', AsyncMock(return_value=None))
    monkeypatch.setattr(type(start_module.settings), 'is_text_main_menu_mode', lambda _self: True)
    monkeypatch.setattr('app.utils.funnel_notify.remember_funnel_menu_message', AsyncMock())

    messages = [_message(), _message()]
    states = [_state(), _state()]
    databases = [SimpleNamespace(refresh=AsyncMock()), SimpleNamespace(refresh=AsyncMock())]

    await asyncio.gather(
        *(
            start_module.complete_registration(message, state, db)
            for message, state, db in zip(messages, states, databases, strict=True)
        )
    )

    assert create_calls == 2
    assert list(created_rows) == [TELEGRAM_ID]
    assert registration_edges == {(9, user.id)}
    assert reward_count == 1
    assert user.balance_kopeks == 5000
    notifier.send_campaign_registration_notification.assert_awaited_once()
    bonus_messages = [
        call.args[0]
        for message in messages
        for call in message.answer.await_args_list
        if call.args and str(call.args[0]).startswith('bonus:')
    ]
    assert bonus_messages == ['bonus:Campaign 9:5000']
