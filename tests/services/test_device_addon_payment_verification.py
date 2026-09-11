"""Operator-payment surfaces must never bypass add-on reconciliation."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.database.models import PaymentMethod
from app.services import payment_verification_service as verification
from app.services.payment_verification_service import AutoPaymentVerificationService, PendingPayment


class _SessionContext:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, exc_type, exc, traceback):
        del exc_type, exc, traceback


@pytest.mark.asyncio
async def test_generic_auto_check_excludes_device_addon_platega(monkeypatch: pytest.MonkeyPatch) -> None:
    session = SimpleNamespace(in_transaction=lambda: False)
    record = PendingPayment(
        method=PaymentMethod.PLATEGA,
        local_id=41,
        identifier='addon-provider-id',
        amount_kopeks=10_000,
        status='RECONCILING',
        is_paid=False,
        created_at=datetime.now(UTC),
        user=SimpleNamespace(id=7),
        payment=SimpleNamespace(id=41),
        is_device_addon=True,
    )
    manual_check = AsyncMock()
    monkeypatch.setattr(verification, 'AsyncSessionLocal', lambda: _SessionContext(session))
    monkeypatch.setattr(verification, 'list_recent_pending_payments', AsyncMock(return_value=[record]))
    monkeypatch.setattr(verification, 'run_manual_check', manual_check)
    service = AutoPaymentVerificationService()
    service.set_payment_service(SimpleNamespace())

    await service._run_checks([PaymentMethod.PLATEGA])

    manual_check.assert_not_awaited()
