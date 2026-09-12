"""Serialization contract for device add-on top-up attempts."""

from types import SimpleNamespace

import pytest

from app.services.device_addon_service import serialize_topup_attempt


@pytest.mark.parametrize(
    ('status', 'reason', 'expected'),
    [
        ('terminal', 'provider_create_rejected:400', 'rejected'),
        ('terminal', 'provider_terminal:canceled:canonical_invoice_mismatch', 'not_paid'),
        ('terminal', 'closed_by_operator', 'not_paid'),
        ('terminal', None, 'not_paid'),
        ('pending', 'provider_create_rejected:400', None),
    ],
)
def test_topup_attempt_exposes_only_safe_terminal_category(
    status: str,
    reason: str | None,
    expected: str | None,
) -> None:
    attempt = SimpleNamespace(
        public_id='attempt-public-id',
        requested_amount_kopeks=500,
        payment_method='platega',
        method_key='2',
        status=status,
        reconciliation_reason=reason,
        credited_amount_kopeks=None,
        payment_url=None,
        holds_invoice_slot=False,
    )

    payload = serialize_topup_attempt(attempt)

    assert payload['terminal_category'] == expected
    assert 'reconciliation_reason' not in payload
