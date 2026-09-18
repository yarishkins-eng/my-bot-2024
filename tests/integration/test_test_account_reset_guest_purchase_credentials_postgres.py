"""PostgreSQL regression: reset revokes guest-delivery bearer credentials.

Run with the same isolated database as the main reset integration suite:

    TEST_ACCOUNT_RESET_TEST_DATABASE_URL=postgresql+asyncpg://... uv run pytest \
        tests/integration/test_test_account_reset_guest_purchase_credentials_postgres.py -q
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.cabinet.routes.landing import _build_purchase_status_response
from app.database.models import GuestPurchase, User
from app.services import user_service
from tests.integration.test_test_account_reset_postgres import (  # noqa: F401 -- pytest fixture registration
    STAND_TELEGRAM_ID,
    _confirmed_reset,
    _panel_ok,
    _seed_person,
    session,
)


DATABASE_URL = os.getenv('TEST_ACCOUNT_RESET_TEST_DATABASE_URL')
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not DATABASE_URL,
        reason='TEST_ACCOUNT_RESET_TEST_DATABASE_URL is required for the PostgreSQL reset tests',
    ),
]


def _credentialed_purchase(*, token: str, user_id: int, buyer_user_id: int | None, is_gift: bool) -> GuestPurchase:
    """A settled delivery with deliberately distinguishable access material."""
    return GuestPurchase(
        token=token,
        contact_type='email',
        contact_value=f'{token}@example.test',
        is_gift=is_gift,
        buyer_user_id=buyer_user_id,
        user_id=user_id,
        period_days=30,
        amount_kopeks=19900,
        payment_id=f'payment-{token}',
        status='delivered',
        delivered_at=datetime.now(UTC),
        subscription_url=f'https://old.example.test/sub/{token}',
        subscription_crypto_link=f'crypto://old/{token}',
        cabinet_password=f'password-{token}',
        auto_login_token=f'login-{token}',
    )


async def test_confirmed_reset_redacts_only_the_test_recipient_guest_credentials(session, monkeypatch) -> None:
    """The public self-purchase URL disappears, without harming sent gifts."""
    monkeypatch.setenv('TEST_ACCOUNT_TELEGRAM_IDS', str(STAND_TELEGRAM_ID))
    monkeypatch.setattr(user_service, '_test_reset_delete_panel_identity', _panel_ok)
    stand = await _seed_person(session, STAND_TELEGRAM_ID, balance_kopeks=0)

    live_buyer = User(telegram_id=44110022, username='live-buyer')
    live_recipient = User(telegram_id=44110023, username='live-recipient')
    session.add_all([live_buyer, live_recipient])
    await session.flush()

    # A direct purchase and a gift received by the reset tester both identify
    # the tester as recipient.  The second one covers the bearer response of
    # the gift claim endpoint, which reads the same stored credential fields.
    self_purchase = _credentialed_purchase(
        token='self-delivery', user_id=stand.id, buyer_user_id=stand.id, is_gift=False
    )
    received_gift = _credentialed_purchase(
        token='received-gift', user_id=stand.id, buyer_user_id=live_buyer.id, is_gift=True
    )
    # The tester sent this gift, but another real user received its VPN access.
    # It is financial history of the tester and must remain completely usable.
    sent_gift = _credentialed_purchase(
        token='sent-gift', user_id=live_recipient.id, buyer_user_id=stand.id, is_gift=True
    )
    session.add_all([self_purchase, received_gift, sent_gift])
    await session.commit()

    # This is the unauthenticated /purchase/{token} payload before reset.
    assert _build_purchase_status_response(self_purchase).subscription_url == self_purchase.subscription_url

    preview = await user_service.reset_test_account(session, stand, admin_id=1, confirm=False)
    assert preview.allowed is True, preview.blocked_reason
    # Preview is read-only: the old bearer still exists until explicit confirm.
    assert (await session.get(GuestPurchase, self_purchase.id)).subscription_url is not None

    result = await _confirmed_reset(session, stand)
    assert result.done is True, result.blocked_reason

    own = await session.scalar(select(GuestPurchase).where(GuestPurchase.id == self_purchase.id))
    received = await session.scalar(select(GuestPurchase).where(GuestPurchase.id == received_gift.id))
    foreign = await session.scalar(select(GuestPurchase).where(GuestPurchase.id == sent_gift.id))
    assert own is not None and received is not None and foreign is not None

    for purchase in (own, received):
        assert purchase.subscription_url is None
        assert purchase.subscription_crypto_link is None
        assert purchase.cabinet_password is None
        assert purchase.auto_login_token is None
        # The purchase remains an auditable financial row.
        assert purchase.status == 'delivered'
        assert purchase.amount_kopeks == 19900

    # The same public builder can no longer disclose the old direct-purchase URL.
    assert _build_purchase_status_response(own).subscription_url is None
    assert _build_purchase_status_response(own).subscription_crypto_link is None

    # Exact recipient ownership matters: the tester's buyer reference alone
    # must never revoke the credentials of a different gift recipient.
    assert foreign.user_id == live_recipient.id
    assert foreign.buyer_user_id == stand.id
    assert foreign.status == 'delivered'
    assert foreign.amount_kopeks == 19900
    assert foreign.subscription_url == 'https://old.example.test/sub/sent-gift'
    assert foreign.subscription_crypto_link == 'crypto://old/sent-gift'
    assert foreign.cabinet_password == 'password-sent-gift'
    assert foreign.auto_login_token == 'login-sent-gift'
