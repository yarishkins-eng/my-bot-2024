"""Balance and payment schemas for cabinet."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class BalanceResponse(BaseModel):
    """User balance data."""

    balance_kopeks: int
    balance_rubles: float


class TransactionResponse(BaseModel):
    """Transaction history item."""

    id: int
    type: str
    amount_kopeks: int
    amount_rubles: float
    description: str | None = None
    payment_method: str | None = None
    is_completed: bool
    created_at: datetime
    completed_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class TransactionListResponse(BaseModel):
    """Paginated transaction list."""

    items: list[TransactionResponse]
    total: int
    page: int
    per_page: int
    pages: int


class PaymentOptionResponse(BaseModel):
    """Payment method option (e.g. Platega sub-methods)."""

    id: str
    name: str
    description: str | None = None


class PaymentMethodResponse(BaseModel):
    """Available payment method."""

    id: str
    name: str
    description: str | None = None
    min_amount_kopeks: int
    max_amount_kopeks: int
    is_available: bool = True
    options: list[dict[str, Any]] | None = None
    quick_amounts: list[int] = Field(default_factory=list)
    # Если True — кабинет, получив payment_url от провайдера, делает window.location.href
    # сразу (seamless flow внутри MiniApp WebView). Если False — показывает панель
    # "Открыть страницу оплаты" с кнопкой.
    open_url_direct: bool = False


class TopUpRequest(BaseModel):
    """Request to create payment for balance top-up."""

    # The minimum and maximum are method- and user-specific.  ``create_topup``
    # resolves the selected method and applies the exact range returned by
    # ``GET /payment-methods``; this generic schema only rejects non-positive
    # amounts before that lookup.
    amount_kopeks: int = Field(..., ge=1, le=2_000_000_000, description='Amount in kopeks')
    payment_method: str = Field(..., description='Payment method ID')
    payment_option: str | None = Field(None, description='Payment option (e.g. Platega method code)')
    # 🔴 Этап В-1. Где человек находится, когда уходит платить: 'telegram' — внутри
    # мини-приложения, 'web' — в обычном браузере. Определяет, куда платёжная система вернёт
    # его кнопкой «Вернуться в магазин»: в Телеграм или на сайт кабинета. Спросить сервер
    # об этом нельзя — оба запроса выглядят одинаково, знает только сам кабинет.
    # Пусто → прежнее поведение (сайт): старая сборка кабинета поля не шлёт.
    return_surface: str | None = Field(None, description="Where to send the payer back: 'telegram' or 'web'")


class TopUpResponse(BaseModel):
    """Response with payment info."""

    payment_id: str
    payment_url: str
    amount_kopeks: int
    amount_rubles: float
    status: str
    expires_at: datetime | None = None


class StarsInvoiceRequest(BaseModel):
    """Request to create Telegram Stars invoice for balance top-up."""

    amount_kopeks: int = Field(..., ge=100, le=2_000_000_000, description='Amount in kopeks (min 1 ruble)')


class StarsInvoiceResponse(BaseModel):
    """Response with Telegram Stars invoice link."""

    invoice_url: str
    stars_amount: int
    amount_kopeks: int


class PendingPaymentResponse(BaseModel):
    """Pending payment details for manual verification."""

    id: int
    method: str
    method_display: str
    identifier: str
    amount_kopeks: int
    amount_rubles: float
    status: str
    status_emoji: str
    status_text: str
    is_paid: bool
    is_checkable: bool
    created_at: datetime
    expires_at: datetime | None = None
    payment_url: str | None = None
    user_id: int | None = None
    user_telegram_id: int | None = None
    user_username: str | None = None
    # 🔴 Этап ДВ-3. Остался ли за человеком шаг «оформить подписку» после того, как деньги
    # зачислены. Решает ТА ЖЕ функция, что и подсказка в чате (`topup_pending_purchase_hint`),
    # — второго списка условий здесь нет намеренно: разъехавшись, чат и кабинет сказали бы
    # одному человеку разное про одни деньги.
    # ⛔ Значение по умолчанию `False` — это не «неизвестно», а «молчим». Кабинет на старом
    # боте поля не увидит и обязан показать прежний текст: обещать оставшийся шаг тому, за кого
    # деньги потратит автопокупка или автоплатёж, опаснее, чем промолчать.
    purchase_step_pending: bool = False

    model_config = ConfigDict(from_attributes=True)


class PendingPaymentListResponse(BaseModel):
    """Paginated list of pending payments."""

    items: list[PendingPaymentResponse]
    total: int
    page: int
    per_page: int
    pages: int


class ManualCheckResponse(BaseModel):
    """Response after manual payment status check."""

    success: bool
    message: str
    payment: PendingPaymentResponse | None = None
    status_changed: bool = False
    old_status: str | None = None
    new_status: str | None = None


class SavedCardResponse(BaseModel):
    """Saved payment method (card) for recurrent payments."""

    id: int
    method_type: str
    card_last4: str | None = None
    card_type: str | None = None
    title: str | None = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class SavedCardsListResponse(BaseModel):
    """List of saved payment methods."""

    cards: list[SavedCardResponse]
    recurrent_enabled: bool = False
