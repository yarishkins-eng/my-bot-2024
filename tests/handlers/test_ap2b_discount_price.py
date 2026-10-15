"""Цена после claim считается настоящим движком; АП-4 округляет сам итог личной скидки до рублей."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.handlers.subscription import promo
from app.services import monitoring_service as monitoring_module
from app.services.monitoring_service import monitoring_service
from app.services.pricing_engine import pricing_engine


@pytest.mark.asyncio
@pytest.mark.parametrize('balance', [0, 5000, 14900])
async def test_claim_price_uses_the_activated_user_and_real_engine(monkeypatch, balance):
    user = SimpleNamespace(
        id=7,
        language='ru',
        balance_kopeks=balance,
        subscription=None,
        promo_group=None,
        get_primary_promo_group=lambda: None,
        promo_offer_discount_percent=0,
        promo_offer_discount_source=None,
        promo_offer_discount_expires_at=None,
    )
    offer = SimpleNamespace(
        id=77,
        user_id=7,
        claimed_at=None,
        is_active=True,
        expires_at=datetime.now(UTC) + timedelta(hours=24),
        effect_type='percent_discount',
        discount_percent=10,
        notification_type='trial_expired_discount',
        extra_data={},
        bonus_amount_kopeks=0,
    )
    callback = SimpleNamespace(
        data='claim_discount_77', answer=AsyncMock(), message=SimpleNamespace(answer=AsyncMock())
    )

    @asynccontextmanager
    async def nested():
        yield

    db = SimpleNamespace(commit=AsyncMock(), refresh=AsyncMock(), begin_nested=nested)
    monkeypatch.setattr(promo, 'get_offer_by_id', AsyncMock(return_value=offer))
    saved = AsyncMock()
    monkeypatch.setattr(promo, 'mark_offer_claimed', saved)
    monkeypatch.setattr(monitoring_module.settings, 'PLATEGA_MIN_AMOUNT_KOPEKS', 100)
    quotes = {}

    async def options(actual_db, actual_user, *, subscription):
        assert actual_db is db and actual_user is user
        assert subscription is None  # Explicit read-only context replaces status-normalizing lookup.
        assert saved.await_count == 1 and user.promo_offer_discount_percent == 10
        tariff = SimpleNamespace(
            id=3, device_limit=1, device_price_kopeks=5000, is_daily=False, period_prices={'30': 14900, '90': 39900}
        )
        matrix = []
        for days in (30, 90):
            result = await pricing_engine.calculate_tariff_purchase_price(tariff, days, device_limit=1, user=user)
            quotes[days] = result.final_total
            matrix.append({'period_days': days, 'prices': [{'device_limit': 1, 'price_kopeks': result.final_total}]})
        return {'eligible': True, 'tariff': {'base_device_limit': 1}, 'price_matrix': matrix}

    monkeypatch.setattr('app.services.device_first_checkout_service.build_purchase_options', options)
    await promo.claim_discount_offer(callback, user, db)
    message = callback.message.answer.await_args.args[0]
    assert quotes == {30: 13400, 90: 35900}
    assert '1 месяц — 134 ₽' in message
    if balance == 5000:
        assert 'с вашими 50 ₽ на балансе — доплатить 84 ₽' in message.split('\n')
    elif balance == 0:
        assert '3 месяца — 359 ₽' in message
    else:
        assert 'спишутся с баланса, доплачивать не нужно.' in message


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['expired', 'foreign', 'claimed'])
async def test_rejected_claim_does_not_ask_for_a_discounted_price(monkeypatch, kind):
    offer = SimpleNamespace(
        id=77,
        user_id=8 if kind == 'foreign' else 7,
        claimed_at=datetime.now(UTC) if kind == 'claimed' else None,
        is_active=True,
        expires_at=datetime.now(UTC) - timedelta(hours=1)
        if kind == 'expired'
        else datetime.now(UTC) + timedelta(hours=1),
    )
    monkeypatch.setattr(promo, 'get_offer_by_id', AsyncMock(return_value=offer))
    quote = AsyncMock()
    monkeypatch.setattr(monitoring_service, '_trial_price_lines', quote)
    callback = SimpleNamespace(
        data='claim_discount_77', answer=AsyncMock(), message=SimpleNamespace(answer=AsyncMock())
    )
    await promo.claim_discount_offer(
        callback, SimpleNamespace(id=7, language='ru'), SimpleNamespace(commit=AsyncMock())
    )
    quote.assert_not_awaited()
    callback.message.answer.assert_not_awaited()


@pytest.mark.asyncio
async def test_claim_passes_its_existing_subscription_to_price_helper(monkeypatch):
    from app.handlers.subscription import promo

    existing = SimpleNamespace(id=44, tariff_id=99, is_active=False, is_trial=True)
    user = SimpleNamespace(
        id=7,
        language='ru',
        balance_kopeks=0,
        subscription=existing,
        promo_offer_discount_percent=0,
        promo_offer_discount_source=None,
        promo_offer_discount_expires_at=None,
    )
    offer = SimpleNamespace(
        id=77,
        user_id=7,
        claimed_at=None,
        is_active=True,
        expires_at=datetime.now(UTC) + timedelta(hours=24),
        effect_type='percent_discount',
        discount_percent=10,
        notification_type='trial_expired_discount',
        extra_data={},
        bonus_amount_kopeks=0,
    )
    callback = SimpleNamespace(
        data='claim_discount_77',
        answer=AsyncMock(),
        message=SimpleNamespace(answer=AsyncMock()),
    )
    db = SimpleNamespace(commit=AsyncMock(), refresh=AsyncMock())
    monkeypatch.setattr(promo, 'get_offer_by_id', AsyncMock(return_value=offer))
    monkeypatch.setattr(promo, 'mark_offer_claimed', AsyncMock())
    quote = AsyncMock(return_value='')
    monkeypatch.setattr(monitoring_service, '_trial_price_lines', quote)
    await promo.claim_discount_offer(callback, user, db)
    quote.assert_awaited_once_with(db, user, subscription=existing)
