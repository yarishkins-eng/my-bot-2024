"""Schemas for tariff management in cabinet."""

from datetime import datetime

from pydantic import BaseModel, Field


class PeriodPrice(BaseModel):
    """Price for a specific period."""

    days: int = Field(..., ge=1, description='Period in days')
    price_kopeks: int = Field(..., ge=0, description='Price in kopeks')
    price_rubles: float | None = None

    def __init__(self, **data):
        super().__init__(**data)
        if self.price_rubles is None:
            self.price_rubles = self.price_kopeks / 100


class ServerTrafficLimit(BaseModel):
    """Traffic limit for a specific server."""

    traffic_limit_gb: int = Field(0, ge=0, description='0 = use default tariff limit')


class ServerInfo(BaseModel):
    """Server info for tariff."""

    id: int
    squad_uuid: str
    display_name: str
    country_code: str | None = None
    is_selected: bool = False
    traffic_limit_gb: int | None = None  # Индивидуальный лимит для сервера


class PromoGroupInfo(BaseModel):
    """Promo group info for tariff."""

    id: int
    name: str
    is_selected: bool = False


class TariffListItem(BaseModel):
    """Tariff item for list view."""

    id: int
    name: str
    description: str | None = None
    is_active: bool
    is_trial_available: bool
    is_daily: bool = False
    daily_price_kopeks: int = 0
    allow_traffic_topup: bool = True
    show_in_gift: bool = True
    traffic_limit_gb: int
    device_limit: int
    tier_level: int
    display_order: int
    servers_count: int
    subscriptions_count: int
    created_at: datetime

    class Config:
        from_attributes = True


class TariffListResponse(BaseModel):
    """Response with list of tariffs."""

    tariffs: list[TariffListItem]
    total: int


class TariffDetailResponse(BaseModel):
    """Detailed tariff response."""

    id: int
    name: str
    description: str | None = None
    is_active: bool
    is_trial_available: bool
    allow_traffic_topup: bool = True
    traffic_topup_enabled: bool = False
    traffic_topup_packages: dict[str, int] = Field(default_factory=dict)
    max_topup_traffic_gb: int = 0
    traffic_limit_gb: int
    device_limit: int
    device_price_kopeks: int | None = None
    max_device_limit: int | None = None
    device_purchase_options: list[int] | None = None
    pricing_revision: int = 1
    tier_level: int
    display_order: int
    period_prices: list[PeriodPrice]
    allowed_squads: list[str]  # UUIDs
    server_traffic_limits: dict[str, ServerTrafficLimit] = Field(default_factory=dict)  # {uuid: {traffic_limit_gb}}
    servers: list[ServerInfo]
    promo_groups: list[PromoGroupInfo]
    subscriptions_count: int
    # Произвольное количество дней
    custom_days_enabled: bool = False
    price_per_day_kopeks: int = 0
    min_days: int = 1
    max_days: int = 365
    # Произвольный трафик при покупке
    custom_traffic_enabled: bool = False
    traffic_price_per_gb_kopeks: int = 0
    min_traffic_gb: int = 1
    max_traffic_gb: int = 1000
    # Дневной тариф
    is_daily: bool = False
    daily_price_kopeks: int = 0
    # Режим сброса трафика
    traffic_reset_mode: str | None = None  # DAY, WEEK, MONTH, MONTH_ROLLING, NO_RESET, None = глобальная настройка
    # Внешний сквад RemnaWave
    external_squad_uuid: str | None = None
    # Показывать в подарках
    show_in_gift: bool = True
    created_at: datetime
    updated_at: datetime | None = None

    class Config:
        from_attributes = True


class ExternalSquadInfoResponse(BaseModel):
    """External squad info from RemnaWave."""

    uuid: str
    name: str
    members_count: int


UUID_PATTERN = r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'


class TariffCreateRequest(BaseModel):
    """Request to create a tariff."""

    name: str = Field(..., min_length=1, max_length=255)
    description: str | None = None
    is_active: bool = True
    allow_traffic_topup: bool = True
    traffic_topup_enabled: bool = False
    traffic_topup_packages: dict[str, int] = Field(default_factory=dict)
    max_topup_traffic_gb: int = Field(0, ge=0)
    traffic_limit_gb: int = Field(0, ge=0, description='0 = unlimited')
    device_limit: int = Field(1, ge=1)
    device_price_kopeks: int | None = Field(None, ge=0)
    max_device_limit: int | None = Field(None, ge=1)
    device_purchase_options: list[int] | None = None
    tier_level: int = Field(1, ge=1, le=10)
    period_prices: list[PeriodPrice] = Field(default_factory=list)
    # Standard Bedolaga contract: selected RemnaWave Internal Squad UUIDs.
    # An empty list means all locally available squads for legacy generic tariffs.
    allowed_squads: list[str] | None = Field(None, description='Selected Internal Squad UUIDs')
    server_traffic_limits: dict[str, ServerTrafficLimit] = Field(
        default_factory=dict, description='Per-server traffic limits'
    )
    promo_group_ids: list[int] = Field(default_factory=list)
    # Произвольное количество дней
    custom_days_enabled: bool = False
    price_per_day_kopeks: int = Field(0, ge=0)
    min_days: int = Field(1, ge=1)
    max_days: int = Field(365, ge=1)
    # Произвольный трафик при покупке
    custom_traffic_enabled: bool = False
    traffic_price_per_gb_kopeks: int = Field(0, ge=0)
    min_traffic_gb: int = Field(1, ge=1)
    max_traffic_gb: int = Field(1000, ge=1)
    # Дневной тариф
    is_daily: bool = False
    daily_price_kopeks: int = Field(0, ge=0)
    # Режим сброса трафика
    traffic_reset_mode: str | None = None  # DAY, WEEK, MONTH, MONTH_ROLLING, NO_RESET, None = глобальная настройка
    # Внешний сквад RemnaWave
    external_squad_uuid: str | None = Field(None, pattern=UUID_PATTERN)
    # Показывать в подарках
    show_in_gift: bool = True


class TariffUpdateRequest(BaseModel):
    """Request to update a tariff."""

    name: str | None = Field(None, min_length=1, max_length=255)
    description: str | None = None
    is_active: bool | None = None
    allow_traffic_topup: bool | None = None
    traffic_topup_enabled: bool | None = None
    traffic_topup_packages: dict[str, int] | None = None
    max_topup_traffic_gb: int | None = Field(None, ge=0)
    traffic_limit_gb: int | None = Field(None, ge=0)
    device_limit: int | None = Field(None, ge=1)
    device_price_kopeks: int | None = Field(None, ge=0)
    max_device_limit: int | None = Field(None, ge=1)
    device_purchase_options: list[int] | None = None
    tier_level: int | None = Field(None, ge=1, le=10)
    display_order: int | None = Field(None, ge=0)
    period_prices: list[PeriodPrice] | None = None
    allowed_squads: list[str] | None = None
    server_traffic_limits: dict[str, ServerTrafficLimit] | None = None
    promo_group_ids: list[int] | None = None
    # Произвольное количество дней
    custom_days_enabled: bool | None = None
    price_per_day_kopeks: int | None = Field(None, ge=0)
    min_days: int | None = Field(None, ge=1)
    max_days: int | None = Field(None, ge=1)
    # Произвольный трафик при покупке
    custom_traffic_enabled: bool | None = None
    traffic_price_per_gb_kopeks: int | None = Field(None, ge=0)
    min_traffic_gb: int | None = Field(None, ge=1)
    max_traffic_gb: int | None = Field(None, ge=1)
    # Дневной тариф
    is_daily: bool | None = None
    daily_price_kopeks: int | None = Field(None, ge=0)
    # Режим сброса трафика
    traffic_reset_mode: str | None = None  # DAY, WEEK, MONTH, MONTH_ROLLING, NO_RESET, None = глобальная настройка
    # Внешний сквад RemnaWave
    external_squad_uuid: str | None = Field(None, pattern=UUID_PATTERN)
    # Показывать в подарках
    show_in_gift: bool | None = None


class TariffSortOrderRequest(BaseModel):
    """Request to reorder tariffs."""

    tariff_ids: list[int] = Field(..., min_length=1, description='Ordered list of tariff IDs')


class TariffToggleResponse(BaseModel):
    """Response after toggling tariff."""

    id: int
    is_active: bool
    message: str


class TariffTrialResponse(BaseModel):
    """Response after setting trial tariff."""

    id: int
    is_trial_available: bool
    message: str


class TariffStatsResponse(BaseModel):
    """Tariff statistics."""

    id: int
    name: str
    subscriptions_count: int
    active_subscriptions: int
    trial_subscriptions: int
    revenue_kopeks: int
    revenue_rubles: float


class SyncSquadsResponse(BaseModel):
    """Response after syncing squads for tariff subscriptions."""

    tariff_id: int
    tariff_name: str
    total_subscriptions: int
    updated_count: int
    failed_count: int
    skipped_count: int
    errors: list[str] = Field(default_factory=list)


class SquadRolloutRequest(BaseModel):
    """Параметры раскатки серверов тарифа на выданные подписки."""

    subscription_ids: list[int] | None = None
    limit: int | None = Field(default=None, ge=1)
    batch_size: int = Field(default=25, ge=1, le=200)


class SquadRolloutPreviewResponse(BaseModel):
    """Сухой прогон: что произойдёт, если нажать «Раскатать»."""

    tariff_id: int
    squads_to_set: list[str]
    candidates: int
    would_change: int
    would_change_ids: list[int] = Field(default_factory=list)
    skipped_traffic_risk_ids: list[int] = Field(default_factory=list)


class SquadRolloutResponse(BaseModel):
    """Результат раскатки или возврата по снимку."""

    tariff_id: int
    rollout_id: str
    total: int
    synced: int
    batches_done: int
    failed_ids: list[int] = Field(default_factory=list)
    skipped_traffic_risk_ids: list[int] = Field(default_factory=list)
    url_mismatch_ids: list[int] = Field(default_factory=list)
    stopped_early: bool = False
    # Возврат: пред-образ пуст, вернуть по нему нельзя — считаем отдельно от трафика.
    unrestorable_ids: list[int] = Field(default_factory=list)
    # Сколько подписок ещё ждут раскатки: кнопка идёт порциями.
    remaining: int = 0
    message: str
