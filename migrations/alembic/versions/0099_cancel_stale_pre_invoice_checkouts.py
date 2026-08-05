"""cancel stale pre-invoice device-first checkouts

Revision ID: 0099
Revises: 0098
Create Date: 2026-08-04

The fused pay-time checkout (commit A) stopped creating browsing drafts, but
the deprecated showcase paths accumulated pre-invoice rows that will never be
paid: ``draft``/``confirmed``/``awaiting_funds`` quotes with no payment
attempt and no financial commit.  They already expire from the customer's
point of view after 24h; this one-time UPDATE archives the stale remainder so
the one-open-checkout-per-user index stops trapping owners of an abandoned
showcase.

The guard is deliberately money-safe and matches the discardable-quote
contract used by trial activation:

* ``financial_committed_at IS NULL`` — the quote never reached a funding
  choice, so no sale snapshot can reference it;
* ``NOT EXISTS (checkout_payment_attempts ...)`` — no provider invoice,
  crashed or otherwise, can still deliver money for the row;
* ``fulfillment_state = 'not_started'`` — no fulfilment ever started;
* ``created_at < now() - interval '1 hour'`` — the cutoff removes any race
  with a live showcase or an in-flight fused payment request.

Committed wallet/provider sales, paid-processing attempts, operator holds and
already terminal rows are all outside this guard by construction.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0099'
down_revision: Union[str, None] = '0098'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(bind: sa.engine.Connection, table: str) -> bool:
    return sa.inspect(bind).has_table(table)


def _has_column(bind: sa.engine.Connection, table: str, column: str) -> bool:
    return _has_table(bind, table) and any(item['name'] == column for item in sa.inspect(bind).get_columns(table))


def upgrade() -> None:
    bind = op.get_bind()
    if not (
        _has_table(bind, 'subscription_checkouts')
        and _has_table(bind, 'checkout_payment_attempts')
        and _has_column(bind, 'subscription_checkouts', 'lifecycle_state')
        and _has_column(bind, 'subscription_checkouts', 'fulfillment_state')
        and _has_column(bind, 'subscription_checkouts', 'financial_committed_at')
        and _has_column(bind, 'subscription_checkouts', 'terminal_reason')
        and _has_column(bind, 'subscription_checkouts', 'quote_state')
        and _has_column(bind, 'subscription_checkouts', 'updated_at')
    ):
        return
    op.execute(
        sa.text(
            """
            UPDATE subscription_checkouts
            SET lifecycle_state = 'cancelled',
                quote_state = 'expired',
                terminal_reason = 'checkout_expired',
                updated_at = now()
            WHERE lifecycle_state IN ('draft', 'confirmed', 'awaiting_funds')
              AND fulfillment_state = 'not_started'
              AND financial_committed_at IS NULL
              AND NOT EXISTS (
                  SELECT 1
                  FROM checkout_payment_attempts
                  WHERE checkout_payment_attempts.checkout_id = subscription_checkouts.id
              )
              AND created_at < now() - interval '1 hour'
            """
        )
    )


def downgrade() -> None:
    # No-op by design: the UPDATE above only archives rows that were already
    # unreachable for payment (no attempt, no financial commit, older than the
    # cutoff).  Reverting their audit state would resurrect quotes whose price
    # snapshot is long expired; a fresh quote is always the correct recovery.
    pass
