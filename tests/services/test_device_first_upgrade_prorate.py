"""Этап ДУ-2: рост числа устройств при продлении оплачивается и за остаток старого срока.

Дефект 1 внешнего ревью 13.09.2026: цена ячейки device-first считала добавленные устройства
только на покупаемый период, а исполнение ставило новый лимит сразу и прибавляло дни к
старому концу — третье устройство на оставшийся год доставалось бесплатно (608 ₽ по ставке
докупки). Здесь закреплено: доплата = `ставка × добавленные × дни остатка // 30` до рубля, в
`devices_price` ДО скидок; ячейки «столько же устройств» — прежние до копейки; дни остатка
замораживаются в котировке, «от какого лимита» берётся живой.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import device_first_checkout_service as service
from app.services.pricing_engine import PricingEngine


def _tariff(**overrides):
    base = dict(
        id=3,
        name='Базовый',
        is_daily=False,
        period_prices={'30': 14_900, '90': 39_900, '180': 69_900, '365': 109_000},
        device_limit=1,
        device_price_kopeks=5_000,
        pricing_revision=9,
        traffic_limit_gb=0,
        description='',
    )
    base.update(overrides)
    tariff = SimpleNamespace(**base)
    tariff.is_available_for_promo_group = lambda _gid: True
    return tariff


def _paid(*, device_limit, days_left, is_trial=False):
    return SimpleNamespace(
        id=30,
        is_trial=is_trial,
        device_limit=device_limit,
        end_date=datetime.now(UTC) + timedelta(days=days_left),
        tariff_id=3,
        status='active',
        updated_at=None,
    )


# --- (а) ядро цены -----------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('period_days', 'devices', 'upgrade_from', 'days_left', 'expected_kopeks'),
    [
        (30, 3, 2, 365, 24_900 + 60_800),  # 249 + 608,33 → 608 ₽ до рубля = 857 ₽
        (30, 2, 2, 365, 19_900),  # столько же устройств — доплаты нет
        (30, 3, 1, 10, 24_900 + 3_300),  # 1 → 3 при 10 дн.: 5000 × 2 × 10 // 30 = 3333 → 33 ₽
        (365, 3, 2, 20, 229_000 + 3_300),  # год вперёд + 20 дн. остатка на третье: 3333 → 33 ₽
        (30, 3, 2, 1, 24_900 + 200),  # один день: 166 коп. → 2 ₽ (полтинник вверх)
        (30, 3, None, 365, 24_900),  # без основания (старые кассы, новые продажи) — прежняя формула
        (30, 3, 2, 0, 24_900),  # остатка нет — прежняя формула
    ],
)
async def test_upgrade_prorate_is_added_only_for_added_devices_on_remaining_days(
    period_days, devices, upgrade_from, days_left, expected_kopeks
):
    price = await PricingEngine().calculate_tariff_purchase_price(
        _tariff(),
        period_days,
        device_limit=devices,
        user=None,
        upgrade_from_device_limit=upgrade_from,
        upgrade_remaining_days=days_left,
    )
    assert price.final_total == expected_kopeks


@pytest.mark.asyncio
async def test_upgrade_prorate_lands_in_the_breakdown_before_discounts():
    price = await PricingEngine().calculate_tariff_purchase_price(
        _tariff(), 30, device_limit=3, user=None, upgrade_from_device_limit=2, upgrade_remaining_days=365
    )
    assert price.breakdown['upgrade_prorate_kopeks'] == 60_800
    assert price.breakdown['upgrade_devices'] == 1
    assert price.breakdown['upgrade_remaining_days'] == 365
    assert price.devices_price == 10_000 + 60_800, 'доплата — часть цены устройств, не отдельная строка'


@pytest.mark.asyncio
async def test_same_device_count_cells_are_unchanged_to_the_kopek():
    """Критерий приёмки: у подписки с 2 устройствами ячейки «2» — 199/549/999/1690 (сетка с боевого 13.09)."""
    for days, expected in ((30, 19_900), (90, 54_900), (180, 99_900), (365, 169_000)):
        price = await PricingEngine().calculate_tariff_purchase_price(
            _tariff(), days, device_limit=2, user=None, upgrade_from_device_limit=2, upgrade_remaining_days=365
        )
        assert price.final_total == expected, f'{days} дн. на 2 устройствах изменились'


# --- (б) основание и матрица -------------------------------------------------------------


def test_prorate_basis_is_zero_for_trial_expired_or_missing_subscription():
    tariff = _tariff()
    now = datetime.now(UTC)
    assert service._upgrade_prorate_basis(None, tariff, now=now) == (None, 0)
    assert service._upgrade_prorate_basis(_paid(device_limit=2, days_left=40, is_trial=True), tariff, now=now) == (
        None,
        0,
    )
    assert service._upgrade_prorate_basis(_paid(device_limit=3, days_left=-5), tariff, now=now) == (None, 0)


def test_prorate_basis_takes_the_larger_of_current_and_base_like_the_addon_does():
    """Паритет с докупкой: у неё бесплатны устройства до базы тарифа (free = included − current)."""
    # Подписка строится ДО снятия `now`: иначе остаток чуть больше 365 суток и ceil даст 366.
    paid = _paid(device_limit=2, days_left=365)
    now = datetime.now(UTC)
    assert service._upgrade_prorate_basis(paid, _tariff(), now=now) == (2, 365)
    assert (
        service._upgrade_prorate_basis(_paid(device_limit=1, days_left=365), _tariff(device_limit=2), now=now)[0] == 2
    )
    # Дни — как у докупки: неполные сутки считаются за сутки, минимум 1.
    naive = SimpleNamespace(
        id=1, is_trial=False, device_limit=2, end_date=(datetime.now(UTC) + timedelta(hours=3)).replace(tzinfo=None)
    )
    assert service._upgrade_prorate_basis(naive, _tariff(), now=now) == (2, 1)


def test_prorate_basis_edges_match_the_addon_on_the_second_boundary():
    """Граничные секунды (мутации M16/M21): конец ровно сейчас — доплаты нет; доли секунды — сутки."""
    now = datetime.now(UTC)
    ends_now = SimpleNamespace(id=1, is_trial=False, device_limit=2, end_date=now)
    assert service._upgrade_prorate_basis(ends_now, _tariff(), now=now) == (None, 0)
    half_second = SimpleNamespace(id=1, is_trial=False, device_limit=2, end_date=now + timedelta(milliseconds=500))
    assert service._upgrade_prorate_basis(half_second, _tariff(), now=now) == (2, 1)
    # Ровно сутки и полсекунды: как у докупки (ceil по дробным секундам) — двое суток.
    day_and_a_bit = SimpleNamespace(
        id=1, is_trial=False, device_limit=2, end_date=now + timedelta(days=1, milliseconds=500)
    )
    assert service._upgrade_prorate_basis(day_and_a_bit, _tariff(), now=now) == (2, 2)


@pytest.mark.asyncio
async def test_matrix_cells_carry_the_frozen_basis_and_only_upgrades_cost_more(monkeypatch):
    tariff = _tariff()
    user = SimpleNamespace(id=9, balance_kopeks=0, get_primary_promo_group=lambda: None)
    eligibility = SimpleNamespace(
        eligible=True, tariff=tariff, device_options=(2, 3), period_options=(30,), default_period_days=30
    )
    monkeypatch.setattr(service.settings, 'DEVICE_FIRST_NEW_CHECKOUTS_ENABLED', True)
    monkeypatch.setattr(service, 'is_device_first_canary_user', lambda _user: True)
    monkeypatch.setattr(service, 'get_tariffs_for_user', AsyncMock(return_value=[tariff]))
    monkeypatch.setattr(service, '_current_subscription', AsyncMock(return_value=_paid(device_limit=2, days_left=365)))
    monkeypatch.setattr(service, 'resolve_single_eligible_tariff', lambda *_a, **_k: eligibility)

    options = await service.build_purchase_options(SimpleNamespace(), user)

    cells = {cell['device_limit']: cell for cell in options['price_matrix'][0]['prices']}
    assert cells[2]['price_kopeks'] == 19_900
    assert cells[2]['breakdown']['upgrade_prorate_kopeks'] == 0
    assert cells[3]['price_kopeks'] == 24_900 + 60_800
    assert cells[3]['breakdown']['upgrade_prorate_kopeks'] == 60_800
    assert cells[3]['breakdown']['upgrade_from_device_limit'] == 2
    assert cells[3]['breakdown']['upgrade_remaining_days'] == 365


# --- (в) перепроверка перед списанием ------------------------------------------------------


def _checkout(*, breakdown, total):
    return SimpleNamespace(
        public_id='c-1',
        period_days=30,
        selected_device_limit=3,
        price_breakdown=breakdown,
        tariff_total_kopeks=total,
        pricing_revision=9,
        expect_no_subscription=False,
        target_snapshot={},
        quote_expires_at=datetime.now(UTC) + timedelta(minutes=20),
        lifecycle_state='confirmed',
        quote_state='valid',
        terminal_reason=None,
        settlement_mode=service.DIRECT_SETTLEMENT_MODE,
    )


async def _run_pre_commit(monkeypatch, *, checkout, target, tariff):
    db = SimpleNamespace(commit=AsyncMock(), scalar=AsyncMock(return_value=tariff))
    monkeypatch.setattr(service, '_require_no_legacy_pending_trial', AsyncMock())
    monkeypatch.setattr(service, '_target_snapshot_drift', lambda *_a, **_k: ())
    monkeypatch.setattr(
        service,
        'tariff_eligibility',
        lambda *_a, **_k: SimpleNamespace(eligible=True, period_options=(30,), device_options=(2, 3, 4)),
    )
    monkeypatch.setattr(service, '_load_checkout_quoted_entitlement', lambda _c: SimpleNamespace(snapshot_hash='h'))
    monkeypatch.setattr(
        'app.services.public_location_entitlement_service.get_subscription_resolved_entitlement',
        AsyncMock(return_value=SimpleNamespace(snapshot_hash='h', squad_uuids=('sq',))),
    )
    monkeypatch.setattr(
        'app.services.public_location_entitlement_service.resolve_tariff_entitlement',
        AsyncMock(return_value=SimpleNamespace(snapshot_hash='h', squad_uuids=('sq',))),
    )
    tariff.entitlement_mode = 'native_squads'
    user = SimpleNamespace(id=9, balance_kopeks=0, get_primary_promo_group=lambda: None)
    return await service._validate_direct_pre_commit(db, checkout=checkout, user=user, target=target, tariff=tariff)


@pytest.mark.asyncio
async def test_frozen_days_keep_the_quote_valid_a_day_later(monkeypatch):
    """Через сутки остаток стал 364, но заказ считает по замороженным 365 — цена сходится."""
    tariff = _tariff()
    checkout = _checkout(breakdown={'upgrade_from_device_limit': 2, 'upgrade_remaining_days': 365}, total=85_700)
    target = _paid(device_limit=2, days_left=364)

    entitlement = await _run_pre_commit(monkeypatch, checkout=checkout, target=target, tariff=tariff)

    assert entitlement is not None, 'перекотировки быть не должно'
    assert checkout.lifecycle_state == 'confirmed'


@pytest.mark.asyncio
async def test_a_device_bought_separately_inside_the_quote_window_forces_a_requote(monkeypatch):
    """Докупил устройство, пока висела котировка: живой лимит 3, доплата за третье уже не нужна."""
    tariff = _tariff()
    checkout = _checkout(breakdown={'upgrade_from_device_limit': 2, 'upgrade_remaining_days': 365}, total=85_700)
    target = _paid(device_limit=3, days_left=365)

    entitlement = await _run_pre_commit(monkeypatch, checkout=checkout, target=target, tariff=tariff)

    assert entitlement is None
    assert checkout.lifecycle_state == 'reprice_required'
    assert checkout.terminal_reason == 'price_changed'


@pytest.mark.asyncio
async def test_a_checkout_born_before_this_stage_is_rechecked_by_the_old_formula(monkeypatch):
    tariff = _tariff()
    checkout = _checkout(breakdown={'base_price_kopeks': 14_900}, total=24_900)
    target = _paid(device_limit=2, days_left=365)

    entitlement = await _run_pre_commit(monkeypatch, checkout=checkout, target=target, tariff=tariff)

    assert entitlement is not None
    assert checkout.lifecycle_state == 'confirmed'


def test_frozen_days_reader_survives_stubs_and_garbage():
    assert service._frozen_upgrade_remaining_days(SimpleNamespace()) == 0
    assert service._frozen_upgrade_remaining_days(SimpleNamespace(price_breakdown=None)) == 0
    assert service._frozen_upgrade_remaining_days(SimpleNamespace(price_breakdown={'upgrade_remaining_days': 'x'})) == 0
    assert service._frozen_upgrade_remaining_days(SimpleNamespace(price_breakdown={'upgrade_remaining_days': 12})) == 12
    assert service._frozen_upgrade_remaining_days(SimpleNamespace(price_breakdown='not-a-dict')) == 0


# --- (г) две двери называют одну сумму ----------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(('current', 'base', 'wanted', 'days_left'), [(2, 1, 3, 365), (1, 2, 3, 365), (3, 1, 3, 40)])
async def test_upgrade_at_renewal_costs_the_same_as_addon_then_renewal(current, base, wanted, days_left):
    """«Докупить до N, потом продлить» == «продлить с N» до рубля (без скидок).

    Формула докупки переписана здесь буквально из `device_addon_service.calculate_device_addon`:
    free = max(0, included − current), chargeable = max(0, add − free), base = int(50 ₽ × chargeable × days / 30).
    """
    tariff = _tariff(device_limit=base)
    add = wanted - current
    free = max(0, base - current)
    chargeable = max(0, add - free)
    addon_kopeks = int(5_000 * chargeable * days_left / 30)
    renew_after_addon = await PricingEngine().calculate_tariff_purchase_price(
        tariff, 30, device_limit=wanted, user=None
    )

    upgrade_from, days = service._upgrade_prorate_basis(
        _paid(device_limit=current, days_left=days_left), tariff, now=datetime.now(UTC)
    )
    renew_with_upgrade = await PricingEngine().calculate_tariff_purchase_price(
        tariff, 30, device_limit=wanted, user=None, upgrade_from_device_limit=upgrade_from, upgrade_remaining_days=days
    )

    two_doors = addon_kopeks + renew_after_addon.final_total
    assert abs(renew_with_upgrade.final_total - two_doors) <= 50, 'двери расходятся больше, чем округление до рубля'
    assert days == max(1, math.ceil(days_left))
