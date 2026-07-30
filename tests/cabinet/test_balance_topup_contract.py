"""Regression tests for the cabinet balance top-up contract."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.cabinet.routes import balance as balance_route
from app.cabinet.schemas.balance import PaymentMethodResponse, TopUpRequest
from app.config import settings


def test_topup_request_defers_minimum_to_the_selected_payment_method() -> None:
    """A generic schema must not conflict with the range returned by the API."""
    request = TopUpRequest(amount_kopeks=1, payment_method='platega')
    assert request.amount_kopeks == 1


@pytest.mark.asyncio
async def test_platega_topup_returns_local_id_for_pending_status(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_methods(*_, **__):
        return [
            PaymentMethodResponse(
                id='platega',
                name='Platega',
                min_amount_kopeks=10_000,
                max_amount_kopeks=100_000_000,
                options=[{'id': '2', 'name': 'СБП (QR)'}],
            )
        ]

    class FakePaymentService:
        async def create_platega_payment(self, **_):
            return {
                'local_payment_id': 777,
                'transaction_id': 'external-transaction-id',
                'redirect_url': 'https://pay.example.test/invoice',
                'status': 'PENDING',
                'expires_at': None,
            }

    monkeypatch.setattr(balance_route, 'get_payment_methods', fake_methods)
    monkeypatch.setattr(balance_route, 'PaymentService', FakePaymentService)
    monkeypatch.setattr(settings, 'PLATEGA_ENABLED', True, raising=False)
    monkeypatch.setattr(settings, 'PLATEGA_MERCHANT_ID', 'test-merchant', raising=False)
    monkeypatch.setattr(settings, 'PLATEGA_SECRET', 'test-secret', raising=False)
    monkeypatch.setattr(settings, 'PLATEGA_ACTIVE_METHODS', '2', raising=False)

    response = await balance_route.create_topup(
        request=TopUpRequest(amount_kopeks=10_000, payment_method='platega', payment_option='2'),
        user=SimpleNamespace(id=1, telegram_id=123, language='ru'),
        db=SimpleNamespace(),
    )

    assert response.payment_id == '777'


@pytest.mark.asyncio
async def test_topup_rejects_amount_below_the_range_it_advertised(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_methods(*_, **__):
        return [
            PaymentMethodResponse(
                id='platega',
                name='Platega',
                min_amount_kopeks=10_000,
                max_amount_kopeks=100_000_000,
            )
        ]

    monkeypatch.setattr(balance_route, 'get_payment_methods', fake_methods)

    with pytest.raises(HTTPException) as error:
        await balance_route.create_topup(
            request=TopUpRequest(amount_kopeks=9_999, payment_method='platega'),
            user=SimpleNamespace(id=1),
            db=SimpleNamespace(),
        )

    assert error.value.status_code == 400
    assert error.value.detail == 'Minimum amount is 100.00 RUB'
