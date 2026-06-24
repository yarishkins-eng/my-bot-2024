"""Grace «бонус 2 дня» в воронке-меню бота.

Пока подписка в grace (VPN ещё жив, но в БД ``status=EXPIRED`` — намеренно, чтобы
автосписание работало), бот должен показывать меню как у ЗАКАНЧИВАЮЩЕЙСЯ платной
(``PAID_EXPIRING``: «Продлить» + «Моя ссылка»), а НЕ как у истёкшей (``PAID_EXPIRED``,
где «Моя ссылка» спрятана). См. ревью после Чата 5: бот не знал про grace.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.config import settings
from app.utils.funnel_state import FunnelState, get_subscriber_state


def _enable_subscriber_funnel(monkeypatch: pytest.MonkeyPatch) -> None:
    # FUNNEL_MENU_ENABLED — обычное поле pydantic (патчим на инстансе). Методы-геттеры
    # на pydantic-модели нельзя setattr-ить на инстансе — патчим на УРОВНЕ КЛАССА.
    monkeypatch.setattr(settings, 'FUNNEL_MENU_ENABLED', True, raising=False)
    settings_cls = type(settings)
    monkeypatch.setattr(settings_cls, 'is_funnel_subscriber_menu_enabled', lambda self: True, raising=False)
    monkeypatch.setattr(settings_cls, 'is_cabinet_mode', lambda self: True, raising=False)
    monkeypatch.setattr(settings_cls, 'is_multi_tariff_enabled', lambda self: False, raising=False)


def test_grace_subscription_maps_to_paid_expiring(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_subscriber_funnel(monkeypatch)
    grace_until = datetime.now(UTC) + timedelta(days=1)
    sub = SimpleNamespace(
        is_trial=False,
        in_grace=True,
        grace_until=grace_until,
        actual_status='expired',
        is_daily_tariff=False,
    )
    user = SimpleNamespace(subscription=sub)

    state, returned = get_subscriber_state(user)

    assert state == FunnelState.PAID_EXPIRING  # VPN жив → меню как у заканчивающейся
    assert returned is sub


def test_truly_expired_without_grace_stays_paid_expired(monkeypatch: pytest.MonkeyPatch) -> None:
    """Регресс: истёкшая БЕЗ grace — по-прежнему PAID_EXPIRED (старое поведение цело)."""
    _enable_subscriber_funnel(monkeypatch)
    sub = SimpleNamespace(
        is_trial=False,
        in_grace=False,
        grace_until=None,
        actual_status='expired',
        is_daily_tariff=False,
    )
    user = SimpleNamespace(subscription=sub)

    state, _returned = get_subscriber_state(user)

    assert state == FunnelState.PAID_EXPIRED


def test_grace_window_closed_is_not_treated_as_grace(monkeypatch: pytest.MonkeyPatch) -> None:
    """in_grace=True, но grace_until уже в прошлом → НЕ grace (is_in_grace вернёт False)."""
    _enable_subscriber_funnel(monkeypatch)
    sub = SimpleNamespace(
        is_trial=False,
        in_grace=True,
        grace_until=datetime.now(UTC) - timedelta(hours=1),
        actual_status='expired',
        is_daily_tariff=False,
    )
    user = SimpleNamespace(subscription=sub)

    state, _returned = get_subscriber_state(user)

    assert state == FunnelState.PAID_EXPIRED
