"""Regression for #634720 — the referral invite link must survive tap-to-copy.

The invite is shown inside a <blockquote> with "tap to copy". Telegram keeps
<code> content in the clipboard but DROPS auto-linked raw URLs when copying a
quote, so a plain-URL link fell out of the copied text. The fix wraps the link
in <code>.

Note: since the 22.06.2026 «Привести друга» redesign (fb38cbb4) the BOT invite
carries ONLY the Telegram bot link; the cabinet (web) referral link now lives in
the cabinet's own referral screen. So the invite has a single <code>-wrapped link.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import app.handlers.referral as ref


async def test_create_invite_message_wraps_bot_link_in_code(monkeypatch):
    captured = {}

    async def fake_edit(callback, text, keyboard):
        captured['text'] = text

    monkeypatch.setattr(ref, 'edit_or_answer_photo', fake_edit)
    # get_bot_referral_link is a method on the Settings class — patch on the class.
    monkeypatch.setattr(
        type(ref.settings), 'get_bot_referral_link', lambda self, code, bot: 'https://t.me/bot?start=ref_X'
    )
    monkeypatch.setattr(ref.settings, 'REFERRAL_FIRST_TOPUP_BONUS_KOPEKS', 0)

    db_user = SimpleNamespace(referral_code='X', language='ru')
    bot = MagicMock()
    bot.get_me = AsyncMock(return_value=SimpleNamespace(username='bot'))
    callback = MagicMock()
    callback.bot = bot
    callback.answer = AsyncMock()

    await ref.create_invite_message(callback, db_user)

    html = captured['text']
    # The bot link is wrapped in <code> so tap-to-copy captures it whole.
    assert '<code>https://t.me/bot?start=ref_X</code>' in html
    # <code> tags themselves must NOT be escaped (only the prose/URL content is).
    assert '&lt;code&gt;' not in html
    # Still rendered inside the copyable quote.
    assert '<blockquote>' in html and '</blockquote>' in html
    # Exactly ONE copyable link in the bot invite (cabinet link lives in the cabinet UI).
    assert html.count('<code>') == 1
