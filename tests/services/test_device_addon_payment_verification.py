"""Operator-payment surfaces must never bypass add-on reconciliation."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.cabinet.routes import admin_payments
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


@pytest.mark.parametrize('attempt_status', ['paid', 'reconciling'])
def test_paid_and_reconciling_attempts_hide_stale_operator_reason(attempt_status: str) -> None:
    attempt = SimpleNamespace(
        status=attempt_status,
        reconciliation_reason='canonical_invoice_mismatch',
        provider_returned_amount_kopeks=10_800,
        requested_amount_kopeks=10_000,
    )

    assert verification._device_addon_reason_text(attempt) is None


def test_terminal_provider_rejection_hides_internal_reason() -> None:
    attempt = SimpleNamespace(
        status='terminal',
        reconciliation_reason='provider_terminal:canceled',
        provider_returned_amount_kopeks=None,
        requested_amount_kopeks=10_000,
    )

    assert verification._device_addon_reason_text(attempt) is None


def test_operator_review_and_mismatch_keep_actionable_reason() -> None:
    operator_review = SimpleNamespace(
        status='operator_review',
        reconciliation_reason='canonical_status_unavailable',
        provider_returned_amount_kopeks=None,
        requested_amount_kopeks=10_000,
    )
    terminal_mismatch = SimpleNamespace(
        status='terminal',
        reconciliation_reason='provider_terminal:canceled:canonical_invoice_mismatch',
        provider_returned_amount_kopeks=10_800,
        requested_amount_kopeks=10_000,
    )

    assert verification._device_addon_reason_text(operator_review) == (
        'Не удалось получить канонический статус Platega.'
    )
    assert verification._device_addon_reason_text(terminal_mismatch) == (
        'Сумма у провайдера не совпала — проверьте настройку комиссии Platega.'
    )


@pytest.mark.parametrize(
    ('status', 'expected'),
    [
        ('PREPARED', ('⏳', 'Готовится счёт')),
        ('DISPATCHING', ('⌛', 'Создаётся счёт')),
        ('CREATION_UNKNOWN', ('⚠️', 'Создание счёта не подтверждено')),
        ('RECONCILING', ('🔄', 'Сверяется')),
        ('OPERATOR_REVIEW', ('⚠️', 'Требует проверки')),
    ],
)
def test_admin_platega_statuses_describe_device_addon_lifecycle(status: str, expected: tuple[str, str]) -> None:
    record = PendingPayment(
        method=PaymentMethod.PLATEGA,
        local_id=41,
        identifier='addon-provider-id',
        amount_kopeks=10_000,
        status=status,
        is_paid=False,
        created_at=datetime.now(UTC),
        user=SimpleNamespace(id=7),
        payment=SimpleNamespace(id=41),
        is_device_addon=True,
    )

    assert admin_payments._get_status_info(record) == expected
