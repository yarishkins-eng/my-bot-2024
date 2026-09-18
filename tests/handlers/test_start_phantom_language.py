from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.handlers import start as start_module


TELEGRAM_ID = 780004


def _message(language_code: str) -> SimpleNamespace:
    return SimpleNamespace(
        text='/start',
        from_user=SimpleNamespace(
            id=TELEGRAM_ID,
            username='phantom_user',
            first_name='Phantom',
            last_name=None,
            language_code=language_code,
        ),
        bot=MagicMock(),
        answer=AsyncMock(),
    )


def _patch_completion_ui(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(start_module, '_apply_campaign_bonus_if_needed', AsyncMock(return_value=None))
    monkeypatch.setattr(start_module, 'delete_pending_payload_from_redis', AsyncMock())
    monkeypatch.setattr(start_module, '_activate_pending_gift_after_registration', AsyncMock())
    monkeypatch.setattr(start_module, '_persist_pending_subid_after_registration', AsyncMock())
    monkeypatch.setattr('app.database.crud.welcome_text.get_welcome_text_for_user', AsyncMock(return_value=None))
    monkeypatch.setattr(start_module, 'get_active_pinned_message', AsyncMock(return_value=None))
    monkeypatch.setattr(start_module, 'get_main_menu_text', AsyncMock(return_value='menu'))
    monkeypatch.setattr(start_module, 'get_main_menu_keyboard_async', AsyncMock(return_value=None))
    monkeypatch.setattr(type(start_module.settings), 'is_text_main_menu_mode', lambda _self: True)
    monkeypatch.setattr('app.utils.funnel_notify.remember_funnel_menu_message', AsyncMock())


@pytest.mark.asyncio
async def test_phantom_first_claim_gets_resolved_language_and_repeat_start_keeps_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    phantom = SimpleNamespace(
        id=701,
        telegram_id=None,
        username='phantom_user',
        first_name=None,
        last_name=None,
        full_name='Phantom',
        language='ru',
        status='active',
        balance_kopeks=0,
        referred_by_id=None,
        has_had_paid_subscription=False,
        subscriptions=[],
    )

    async def _claim(_db, candidate, **kwargs):
        assert candidate is phantom
        candidate.telegram_id = kwargs['telegram_id']
        candidate.language = kwargs['language']
        return True, candidate

    _patch_completion_ui(monkeypatch)
    monkeypatch.setattr(start_module, 'get_user_by_telegram_id', AsyncMock(return_value=None))
    monkeypatch.setattr(start_module, 'find_phantom_user_by_username', AsyncMock(return_value=phantom))
    monkeypatch.setattr(start_module, 'claim_phantom', _claim)
    first_state = SimpleNamespace(
        get_data=AsyncMock(return_value={'language': 'en'}),
        clear=AsyncMock(),
    )
    db = SimpleNamespace(refresh=AsyncMock(), commit=AsyncMock(), rollback=AsyncMock())

    await start_module.complete_registration(_message('en-US'), first_state, db)

    assert phantom.telegram_id == TELEGRAM_ID
    assert phantom.language == 'en'

    monkeypatch.setattr(start_module, 'get_pending_payload_from_redis', AsyncMock(return_value=None))
    monkeypatch.setattr(start_module, 'get_user_by_telegram_id', AsyncMock(return_value=phantom))
    resolver = MagicMock(side_effect=AssertionError('repeat start must keep persisted language'))
    monkeypatch.setattr(start_module, 'get_telegram_language', resolver)
    repeat_state = SimpleNamespace(
        get_data=AsyncMock(return_value={}),
        update_data=AsyncMock(),
        clear=AsyncMock(),
    )

    await start_module.cmd_start(_message('ru'), repeat_state, db)

    assert phantom.language == 'en'
    resolver.assert_not_called()
