"""Backend fields for the redesigned cabinet screen (Чат 1 — бэкенд-поля).

These pin the *top-up gates* and the additive subscription-response fields the
new unified screen reads (with frontend defaults) so it can hide purchase
buttons up-front instead of reacting to errors:

* device top-up gate + effective per-device price — must reuse the SAME
  price/limit resolution as ``GET /subscription/devices/price`` (no duplicated
  formula, and ``available:false`` at the device-limit cap even when price > 0);
* traffic top-up gate — mirrors ``GET /subscription/traffic-packages``: the
  tariff's ``can_topup_traffic()`` in tariff mode, OR the global
  ``TRAFFIC_TOPUP_ENABLED`` toggle + packages in classic mode (a sub WITHOUT a
  tariff CAN top up traffic — the gate must not hide that button);
* ``restriction_subscription`` (manual admin purchase block) pushed into the
  user-facing response so the screen hides CTAs before a 403;
* ``disabled_reason_hint`` passes through (computed best-effort upstream).

All fields are additive — the old frontend ignores them.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.cabinet.routes.subscription_modules.helpers import (
    _resolve_device_addon_price,
    _subscription_to_response,
    compute_device_topup_gate,
    compute_traffic_topup_gate,
)
from app.config import settings
from app.database.models import Subscription, Tariff, User


# ----------------------------- device price resolution -----------------------


def test_device_price_prefers_tariff_when_set() -> None:
    """A tariff with an explicit per-device price wins over the global setting."""
    tariff = SimpleNamespace(device_price_kopeks=7000, max_device_limit=4)
    price, max_limit = _resolve_device_addon_price(tariff)
    assert price == 7000
    assert max_limit == 4


def test_device_price_falls_back_to_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """No tariff (classic plan) → global PRICE_PER_DEVICE / MAX_DEVICES_LIMIT."""
    monkeypatch.setattr(settings, 'PRICE_PER_DEVICE', 3000)
    monkeypatch.setattr(settings, 'MAX_DEVICES_LIMIT', 6)
    price, max_limit = _resolve_device_addon_price(None)
    assert price == 3000
    assert max_limit == 6


def test_device_price_tariff_none_price_uses_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """tariff.device_price_kopeks is None → fall back to the global price (per #17.7)."""
    monkeypatch.setattr(settings, 'PRICE_PER_DEVICE', 2500)
    monkeypatch.setattr(settings, 'MAX_DEVICES_LIMIT', 0)
    tariff = SimpleNamespace(device_price_kopeks=None, max_device_limit=None)
    price, max_limit = _resolve_device_addon_price(tariff)
    assert price == 2500
    assert max_limit is None  # MAX_DEVICES_LIMIT == 0 means "no limit"


# ------------------------------- device gate ---------------------------------


def test_device_gate_open_when_price_positive_and_under_limit() -> None:
    tariff = SimpleNamespace(device_price_kopeks=5000, max_device_limit=5)
    sub = SimpleNamespace(device_limit=2)
    can_topup, price = compute_device_topup_gate(sub, tariff)
    assert can_topup is True
    assert price == 5000


def test_device_gate_closed_when_price_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """0 ₽ per device → top-up unavailable → button hidden (owner decision v1.7)."""
    monkeypatch.setattr(settings, 'PRICE_PER_DEVICE', 0)
    monkeypatch.setattr(settings, 'MAX_DEVICES_LIMIT', 0)
    sub = SimpleNamespace(device_limit=1)
    can_topup, price = compute_device_topup_gate(sub, None)
    assert can_topup is False
    assert price == 0


def test_device_gate_closed_at_max_limit_even_with_price() -> None:
    """device_limit already at the cap → closed even though price > 0 (#starter ловетка)."""
    tariff = SimpleNamespace(device_price_kopeks=5000, max_device_limit=3)
    sub = SimpleNamespace(device_limit=3)
    can_topup, price = compute_device_topup_gate(sub, tariff)
    assert can_topup is False
    assert price == 5000  # price still reported for context, but gate is closed


def test_device_gate_open_when_no_max_limit() -> None:
    """max_device_limit None → never capped."""
    tariff = SimpleNamespace(device_price_kopeks=5000, max_device_limit=None)
    sub = SimpleNamespace(device_limit=99)
    can_topup, price = compute_device_topup_gate(sub, tariff)
    assert can_topup is True
    assert price == 5000


# ------------------------------- traffic gate --------------------------------


# Tariff mode (tariffs sales-mode + the sub has a tariff) → tariff's own rule.
def test_traffic_gate_tariff_mode_delegates_true(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    sub = SimpleNamespace(tariff_id=7)
    tariff = SimpleNamespace(can_topup_traffic=lambda: True)
    assert compute_traffic_topup_gate(sub, tariff) is True


def test_traffic_gate_tariff_mode_delegates_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    sub = SimpleNamespace(tariff_id=7)
    tariff = SimpleNamespace(can_topup_traffic=lambda: False)
    assert compute_traffic_topup_gate(sub, tariff) is False


def test_traffic_gate_tariff_mode_missing_tariff_is_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """tariff_id set but tariff object not loaded → safe default (hidden)."""
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    sub = SimpleNamespace(tariff_id=7)
    assert compute_traffic_topup_gate(sub, None) is False


# Classic path (sub WITHOUT a tariff, or classic sales-mode) → global toggle + packages.
def test_traffic_gate_classic_available_for_no_tariff_sub(monkeypatch: pytest.MonkeyPatch) -> None:
    """No tariff_id but global topup ON with a package → available (TRAFFIC_TOPUP_ENABLED
    defaults True). Returning False here would wrongly hide a working button."""
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'TRAFFIC_TOPUP_ENABLED', True)
    monkeypatch.setattr(
        type(settings), 'get_traffic_topup_packages', lambda self: [{'gb': 5, 'price': 1000, 'enabled': True}]
    )
    sub = SimpleNamespace(tariff_id=None)
    assert compute_traffic_topup_gate(sub, None) is True


def test_traffic_gate_classic_global_toggle_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'TRAFFIC_TOPUP_ENABLED', False)
    sub = SimpleNamespace(tariff_id=None)
    assert compute_traffic_topup_gate(sub, None) is False


def test_traffic_gate_classic_no_enabled_package(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'TRAFFIC_TOPUP_ENABLED', True)
    monkeypatch.setattr(
        type(settings), 'get_traffic_topup_packages', lambda self: [{'gb': 5, 'price': 0, 'enabled': True}]
    )
    sub = SimpleNamespace(tariff_id=None)
    assert compute_traffic_topup_gate(sub, None) is False


def test_traffic_gate_classic_mode_tariff_disallows(monkeypatch: pytest.MonkeyPatch) -> None:
    """Classic sales-mode + a tariff with allow_traffic_topup False → hidden."""
    monkeypatch.setattr(settings, 'SALES_MODE', 'classic')
    monkeypatch.setattr(settings, 'TRAFFIC_TOPUP_ENABLED', True)
    monkeypatch.setattr(
        type(settings), 'get_traffic_topup_packages', lambda self: [{'gb': 5, 'price': 1000, 'enabled': True}]
    )
    sub = SimpleNamespace(tariff_id=7)
    tariff = SimpleNamespace(allow_traffic_topup=False, can_topup_traffic=lambda: True)
    assert compute_traffic_topup_gate(sub, tariff) is False


# --------------------- _subscription_to_response field wiring -----------------


def _make_subscription(**overrides) -> Subscription:
    """A transient (no-session) Subscription with the attrs the response needs."""
    now = datetime.now(UTC)
    defaults = dict(
        id=1,
        status='active',
        is_trial=False,
        start_date=now,
        end_date=now + timedelta(days=10),
        traffic_limit_gb=100,
        traffic_used_gb=10.0,
        device_limit=2,
        connected_squads=[],
        autopay_enabled=False,
        autopay_days_before=3,
        subscription_url='https://example/sub',
        tariff_id=None,
    )
    defaults.update(overrides)
    return Subscription(**defaults)


def test_response_exposes_restriction_and_gates_for_tariff_sub(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    tariff = Tariff(
        device_price_kopeks=5000,
        max_device_limit=5,
        traffic_topup_enabled=True,
        traffic_topup_packages={5: 1000},
        traffic_limit_gb=100,  # not unlimited
        is_daily=False,  # transient instance: column default isn't applied until insert
    )
    sub = _make_subscription(tariff_id=42, device_limit=2)
    sub.tariff = tariff  # stored in __dict__, read by the gate (no lazy load)
    user = User(restriction_subscription=True)

    resp = _subscription_to_response(sub, user=user)

    assert resp.restriction_subscription is True
    assert resp.can_topup_devices is True
    assert resp.device_addon_price_kopeks == 5000
    assert resp.can_topup_traffic is True
    assert resp.disabled_reason_hint is None  # default when not passed


def test_response_safe_defaults_for_classic_sub_no_user(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'PRICE_PER_DEVICE', 3000)
    monkeypatch.setattr(settings, 'MAX_DEVICES_LIMIT', 0)
    monkeypatch.setattr(settings, 'TRAFFIC_TOPUP_ENABLED', False)  # global topup off → traffic hidden
    sub = _make_subscription(tariff_id=None, device_limit=1)  # no tariff loaded

    resp = _subscription_to_response(sub, user=None)

    # No admin restriction; traffic toggle off → hidden; devices follow settings price.
    assert resp.restriction_subscription is False
    assert resp.can_topup_traffic is False
    assert resp.can_topup_devices is True
    assert resp.device_addon_price_kopeks == 3000


def test_response_passes_disabled_reason_hint_through() -> None:
    sub = _make_subscription(status='disabled')
    resp = _subscription_to_response(sub, user=None, disabled_reason_hint='channel')
    assert resp.status == 'disabled'
    assert resp.disabled_reason_hint == 'channel'
