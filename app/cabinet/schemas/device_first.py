"""Schemas for the fused pay-time device-first checkout mutations.

Unlike the deprecated showcase endpoints, these requests carry the full order
(period, devices, funding and the optimistic price token): no durable checkout
exists before the customer explicitly chooses to pay.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class DirectCheckoutCommitRequest(BaseModel):
    """One pay-time funding choice; the server recalculates the price anyway.

    ``expected_tariff_total_kopeks`` is the exact raw ``price_kopeks`` value
    the client rendered from ``purchase-options`` (never rounded).  A mismatch
    is rejected with ``reprice_required`` before any row or invoice is made.
    """

    period_days: int = Field(..., gt=0)
    selected_device_limit: int = Field(..., gt=0)
    funding_mode: str = Field(..., pattern='^(wallet|platega)$')
    method_key: str | None = Field(None, min_length=1, max_length=32)
    expected_tariff_total_kopeks: int = Field(..., gt=0)
