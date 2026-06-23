"""Best-effort ``disabled_reason_hint`` resolver (Чат 1 — бэкенд-поля, правка 4).

The DB does not store WHY a subscription is disabled (set in 5+ places: channel
unsubscribe, manual admin/broadcast block, panel sync). We blame the channel
ONLY when the user actually LEFT a required channel whose settings would disable
a subscription of THIS type — mirroring
``ChannelSubscriptionService.should_disable_subscription`` (trials gated by
``CHANNEL_DISABLE_TRIAL_ON_UNSUBSCRIBE`` + per-channel ``disable_trial_on_leave``;
paid subs by per-channel ``disable_paid_on_leave``). That stops a paid sub
disabled by an admin block — whose owner just isn't in a trial-only channel —
from being wrongly told to rejoin the channel.

Otherwise (subscribed / no channels / the left channel wouldn't disable this sub
type / no telegram_id / any error) → ``None`` and the screen shows a neutral
"обратись в поддержку". It reads the cached channel service and must NEVER raise.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.cabinet.routes.subscription_modules.status import _resolve_disabled_reason_hint
from app.config import settings
from app.services.channel_subscription_service import ChannelSubscriptionService


def _patch_unsubscribed(monkeypatch: pytest.MonkeyPatch, channels: list[dict], *, raises: bool = False) -> None:
    async def _fake(self, telegram_id):
        if raises:
            raise RuntimeError('cache down')
        return channels

    # Keep the REAL should_disable_subscription (static, pure) — only stub the
    # channel lookup so we exercise the genuine disable-rule logic.
    monkeypatch.setattr(ChannelSubscriptionService, 'get_unsubscribed_channels', _fake)


@pytest.mark.asyncio
async def test_hint_channel_for_trial_left_channel_that_disables_trials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'CHANNEL_DISABLE_TRIAL_ON_UNSUBSCRIBE', True)
    _patch_unsubscribed(monkeypatch, [{'channel_id': '-100', 'disable_trial_on_leave': True}])
    user = SimpleNamespace(telegram_id=42)
    sub = SimpleNamespace(is_trial=True)
    assert await _resolve_disabled_reason_hint(user, sub) == 'channel'


@pytest.mark.asyncio
async def test_no_false_channel_hint_for_paid_sub_on_trial_only_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    """The skeptic's case: a PAID sub (disabled by something else) whose owner left a
    channel that only disables TRIALS must NOT be told to rejoin the channel."""
    _patch_unsubscribed(monkeypatch, [{'channel_id': '-100', 'disable_trial_on_leave': True}])
    user = SimpleNamespace(telegram_id=42)
    sub = SimpleNamespace(is_trial=False)  # disable_paid_on_leave defaults False
    assert await _resolve_disabled_reason_hint(user, sub) is None


@pytest.mark.asyncio
async def test_hint_channel_for_paid_when_channel_disables_paid(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_unsubscribed(monkeypatch, [{'channel_id': '-100', 'disable_paid_on_leave': True}])
    user = SimpleNamespace(telegram_id=42)
    sub = SimpleNamespace(is_trial=False)
    assert await _resolve_disabled_reason_hint(user, sub) == 'channel'


@pytest.mark.asyncio
async def test_no_hint_for_trial_when_global_toggle_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Global CHANNEL_DISABLE_TRIAL_ON_UNSUBSCRIBE=False overrides per-channel for trials."""
    monkeypatch.setattr(settings, 'CHANNEL_DISABLE_TRIAL_ON_UNSUBSCRIBE', False)
    _patch_unsubscribed(monkeypatch, [{'channel_id': '-100', 'disable_trial_on_leave': True}])
    user = SimpleNamespace(telegram_id=42)
    sub = SimpleNamespace(is_trial=True)
    assert await _resolve_disabled_reason_hint(user, sub) is None


@pytest.mark.asyncio
async def test_no_hint_when_no_unsubscribed_channels(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_unsubscribed(monkeypatch, [])
    user = SimpleNamespace(telegram_id=42)
    sub = SimpleNamespace(is_trial=True)
    assert await _resolve_disabled_reason_hint(user, sub) is None


@pytest.mark.asyncio
async def test_no_hint_without_telegram_id(monkeypatch: pytest.MonkeyPatch) -> None:
    # Email-only user: nothing to check against, no service call needed.
    _patch_unsubscribed(monkeypatch, [{'channel_id': '-100', 'disable_trial_on_leave': True}])
    user = SimpleNamespace(telegram_id=None)
    sub = SimpleNamespace(is_trial=True)
    assert await _resolve_disabled_reason_hint(user, sub) is None


@pytest.mark.asyncio
async def test_never_raises_on_service_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_unsubscribed(monkeypatch, [{'channel_id': '-100'}], raises=True)
    user = SimpleNamespace(telegram_id=42)
    sub = SimpleNamespace(is_trial=True)
    assert await _resolve_disabled_reason_hint(user, sub) is None
