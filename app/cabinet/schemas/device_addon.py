"""Request shapes for the durable manual device add-on API."""

from typing import Literal

from pydantic import BaseModel, Field


class DeviceAddonIntentCreateRequest(BaseModel):
    quote_token: str = Field(..., min_length=16, max_length=4096)
    idempotency_key: str = Field(..., min_length=1, max_length=128)


class DeviceAddonPurchaseRequest(BaseModel):
    quote_token: str = Field(..., min_length=16, max_length=4096)


class DeviceAddonTopupRequest(BaseModel):
    idempotency_key: str = Field(..., min_length=1, max_length=128)
    expected_amount_kopeks: int = Field(..., ge=0)
    payment_method: Literal['platega'] = 'platega'
    payment_option: str = Field(..., min_length=1, max_length=32)
    return_surface: Literal['cabinet', 'telegram'] = 'cabinet'
