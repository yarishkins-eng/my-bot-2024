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


class TopUpIntent(BaseModel):
    """ВК-16 (16а-1): заказ, ради которого доплачивают, — срок и устройства. Цену и сумму считает сервер."""

    period_days: int = Field(..., ge=1, le=3650)
    devices: int = Field(..., ge=1, le=100)
    # ВК-16 (16а-2, заявка 3а). `confirmed_purchase_at` — ответ «да» на «Уже оформлено до …. Оплатить ещё период?»:
    # экран возвращает `purchased_at` того вопроса (мина OP); если с тех пор была покупка новее, сервер спросит снова.
    # `change_method` — человек сам выбрал другой способ при живом счёте того же заказа (иначе сервер вернёт тот же
    # счёт — `already_paying`, мина OR). Старый экран их не шлёт — прежнее поведение.
    confirmed_purchase_at: datetime | None = None
    change_method: bool = False


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
    # 🔴 ВК-16 (16а-1). Доплата под заказ: при намерении сумму к оплате считает сервер, а `amount_kopeks` клиента идёт
    # только в обычное пополнение (исход `ordinary`) — поэтому экран обязан слать настоящую сумму доплаты, а не заглушку.
    # Без намерения прежние поля ответа те же, новые — `null`.
    intent: TopUpIntent | None = Field(None, description='Order this top-up pays for (device-first checkout)')


class TopUpResponse(BaseModel):
    """Response with payment info.

    🔴 ВК-16 (16а-1): при намерении `intent_status` говорит исход, и у части исходов счёта нет — поэтому
    `payment_id`/`payment_url` необязательны. Исходы: `accepted` (счёт с намерением — «оформится само»),
    `ordinary` + `intent_reason` (`disabled`, `account_erasure`, `restricted`, `unavailable`, `method_not_supported` —
    обычное пополнение без обещания), `open_order`, `order_on_review` (к заказу не вести — поддержка),
    `already_paying` (тот же неоплаченный счёт того же заказа и способа — его ссылка), `already_paid` (деньги по
    намерению уже пришли, оформляем — ссылки нет), `already_fulfilled`, `balance_covers`, `invoice_not_created`.
    Срок и устройства — того заказа, о котором исход; цена — где известна.
    С заявки 3а (договор для экрана 16в-1): `already_fulfilled` — за последний час была покупка ЛЮБЫМ путём
    (проводка покупки); `purchased_at` — её момент, `subscription_end_date` — конец подписки, `price_kopeks` — цена
    запрошенного срока сейчас. Срок и устройства есть, только если куплен срок в новой кассе; без них (карта, докупка
    устройств или трафика, суточное) экран не пишет «оформлено <срок>», а «за последний час уже была покупка»; без даты —
    без «до …». «Да» — повтор запроса с `intent.confirmed_purchase_at = purchased_at`; сумму к оплате брать из ответа
    `accepted`, а не из прежнего экрана. `already_paying` — живой счёт того же заказа любым способом; `payment_option` —
    каким (код как `id` варианта Platega в `/payment-methods`; может быть `null` или отсутствовать в списке — тогда
    «у вас уже есть неоплаченный счёт»); рядом всегда «оплатить другим способом» — запрос с `intent.change_method`.
    """

    payment_id: str | None = None
    payment_url: str | None = None
    amount_kopeks: int
    amount_rubles: float
    status: str
    expires_at: datetime | None = None
    intent_status: str | None = None
    intent_reason: str | None = None
    checkout_public_id: str | None = None
    period_days: int | None = None
    devices: int | None = None
    price_kopeks: int | None = None
    subscription_end_date: datetime | None = None
    purchased_at: datetime | None = None
    payment_option: str | None = None


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
    # зачислены. При намерении решает `topup_intent_refusal_kind`: шаг есть у `retry`/`order`,
    # у `bought`/`support` и успеха — нет. Без намерения решает `topup_pending_purchase_hint`,
    # та же функция, что у подсказки в чате (ВК-16, договор 3б; решение владельца 05.10).
    # ⛔ Значение по умолчанию `False` — это не «неизвестно», а «молчим». Кабинет на старом
    # боте поля не увидит и обязан показать прежний текст: обещать оставшийся шаг тому, за кого
    # деньги потратит автопокупка или автоплатёж, опаснее, чем промолчать.
    purchase_step_pending: bool = False
    # 🔴 ВК-16 (16а-1). Исход заказа, ради которого доплачивали: `waiting` (денег ещё нет — судьбу счёта говорит
    # `status`) / `processing` (зачислено, оформляем) / `fulfilled` (+ номер заказа) / `refused` (+ причина и номер
    # заказа, если был; после оплаты) / `closed` (+ `replaced`|`cancelled`). `closed` бывает и ПОСЛЕ зачисления,
    # если отмена попала в секунды оформления, а исход не записан (OU); экран смотрит ещё `intent_paid`.
    # Нет намерения — пусто. ВК-16: приоритет исхода над `is_paid` (OH), договор 3б.
    # Экран ждёт ЗАКАЗ, а не деньги (замысел v2, правило 1).
    intent_outcome: str | None = None
    intent_checkout_public_id: str | None = None
    intent_reason: str | None = None
    # ВК-16 (16а-2, заявка 3б) — договор с экраном 16в-2: при `refused`/`closed` причина — из закрытого набора, вид кнопки
    # (`bought` — без кнопки покупки, `order` — к заказу, `support` — в поддержку, `retry` — «Оформить» по предложению,
    # а без него — выбор срока, как бот). Срок, устройства, цена — заказа намерения; предложение — только у отказа.
    # `intent_payment_id` — о каком платеже исход (`null` — намерения нет): если не этот, деньги пришли по СТАРОМУ счёту
    # того же человека (сменил способ или отменил заказ, а заплатил по прежней ссылке) — экран показывает его исход, а
    # не ждёт свой, не показывает ссылку `payment_url` нового и берёт сумму и факт оплаты из `intent_amount_kopeks` и
    # `intent_paid` (а не `amount_kopeks` / `is_paid` записи). Вида кнопки нет у `closed` без денег (бот тогда молчит);
    # купил после отказа любым путём — вид `bought` без предложения.
    intent_refusal_kind: str | None = None
    intent_payment_id: int | None = None
    intent_paid: bool | None = None
    intent_amount_kopeks: int | None = None
    intent_period_days: int | None = None
    intent_devices: int | None = None
    intent_quote_kopeks: int | None = None
    intent_offer_kopeks: int | None = None
    intent_offer_tariff_name: str | None = None

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
