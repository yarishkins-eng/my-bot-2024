import hashlib
import json
from types import SimpleNamespace

import structlog
from sqlalchemy import and_, exists, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased, selectinload

from app.database.models import (
    AccountErasureRequest,
    CheckoutPaymentAttempt,
    DeviceFirstDepositOutbox,
    DeviceFirstNotificationOutbox,
    DeviceFirstOutbox,
    DeviceFirstReconciliationCredit,
    PlategaPayment,
    PromoGroup,
    Subscription,
    SubscriptionCheckout,
    SubscriptionStatus,
    Tariff,
    Transaction,
    User,
)
from app.services.device_first_eligibility import normalize_device_purchase_options


logger = structlog.get_logger(__name__)


_CHECKOUT_TERMINAL_STATES = ('ready', 'cancelled', 'expired')
_DIRECT_PROVIDER_ATTEMPT_OPEN_STATES = ('creating', 'pending', 'paid_processing')
_HISTORICAL_ERASED_DIRECT_MISMATCH = 'direct_payment_binding_mismatch'
_APPROVED_FINAL_ERASURE_ARCHIVE_TARIFF_ID = 3
_APPROVED_FINAL_ERASURE_ARCHIVE_COUNT = 5
# Owner-pinned from a read-only canonical-Platega CANCELED verification and a
# matching local P/U/A/C snapshot. It is not a configurable policy: any drift
# in this exact archived cohort must fail closed and keep the tariff edit
# blocked for manual financial review.
_APPROVED_FINAL_ERASURE_ARCHIVE_SHA256 = 'ee4478122f393ee2930424bcabae91afd22e30bcab0e7521b15d57cf77159e81'


def _normalized_squad_selection(values: list[str] | None) -> tuple[str, ...]:
    return tuple(dict.fromkeys(str(value) for value in (values or []) if value))


def _manifest_timestamp(value: object) -> str | None:
    if value is None:
        return None
    isoformat = getattr(value, 'isoformat', None)
    return isoformat() if callable(isoformat) else str(value)


def _archive_manifest_sha256(rows: list[dict[str, object]]) -> str | None:
    """Return the exact, non-configurable fingerprint of the archived cohort."""

    if len(rows) != _APPROVED_FINAL_ERASURE_ARCHIVE_COUNT:
        return None
    if any(int(row['tariff_id']) != _APPROVED_FINAL_ERASURE_ARCHIVE_TARIFF_ID for row in rows):
        return None
    if len({int(row['checkout_id']) for row in rows}) != _APPROVED_FINAL_ERASURE_ARCHIVE_COUNT:
        return None
    if len({int(row['attempt_id']) for row in rows}) != _APPROVED_FINAL_ERASURE_ARCHIVE_COUNT:
        return None
    if len({int(row['payment_id']) for row in rows}) != _APPROVED_FINAL_ERASURE_ARCHIVE_COUNT:
        return None
    if len({str(row['provider_payment_id']) for row in rows}) != _APPROVED_FINAL_ERASURE_ARCHIVE_COUNT:
        return None
    variants = [str(row['variant']) for row in rows]
    if variants.count('direct_direct_missing_marker') != 4:
        return None
    if variants.count('direct_legacy_missing_marker') != 1:
        return None

    encoded = json.dumps(
        {'version': 3, 'entries': sorted(rows, key=lambda row: str(row['provider_payment_id']))},
        sort_keys=True,
        separators=(',', ':'),
        ensure_ascii=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _final_erasure_archive_candidate_query(tariff_id: int):
    """Select only the documented no-money final-erasure archive cohort."""

    other_attempt = aliased(CheckoutPaymentAttempt)
    exactly_one_payment_attempt = (
        select(func.count(other_attempt.id))
        .where(other_attempt.checkout_id == SubscriptionCheckout.id)
        .scalar_subquery()
        == 1
    )
    no_money_or_provisioning = and_(
        ~exists(
            select(DeviceFirstReconciliationCredit.id).where(
                DeviceFirstReconciliationCredit.checkout_id == SubscriptionCheckout.id
            )
        ),
        ~exists(select(Transaction.id).where(Transaction.device_first_checkout_id == SubscriptionCheckout.id)),
        ~exists(select(DeviceFirstOutbox.id).where(DeviceFirstOutbox.checkout_id == SubscriptionCheckout.id)),
        ~exists(
            select(DeviceFirstDepositOutbox.id).where(DeviceFirstDepositOutbox.checkout_id == SubscriptionCheckout.id)
        ),
        ~exists(
            select(DeviceFirstNotificationOutbox.id).where(
                DeviceFirstNotificationOutbox.checkout_id == SubscriptionCheckout.id
            )
        ),
    )
    return (
        select(
            SubscriptionCheckout.id.label('checkout_id'),
            SubscriptionCheckout.tariff_id.label('tariff_id'),
            SubscriptionCheckout.user_id.label('checkout_user_id'),
            SubscriptionCheckout.lifecycle_state.label('checkout_lifecycle_state'),
            SubscriptionCheckout.terminal_reason.label('checkout_terminal_reason'),
            SubscriptionCheckout.quote_state.label('checkout_quote_state'),
            SubscriptionCheckout.funding_state.label('checkout_funding_state'),
            SubscriptionCheckout.fulfillment_state.label('checkout_fulfillment_state'),
            SubscriptionCheckout.provisioning_state.label('checkout_provisioning_state'),
            SubscriptionCheckout.settlement_mode.label('checkout_settlement_mode'),
            SubscriptionCheckout.created_subscription_id.label('checkout_created_subscription_id'),
            SubscriptionCheckout.debit_transaction_id.label('checkout_debit_transaction_id'),
            CheckoutPaymentAttempt.id.label('attempt_id'),
            CheckoutPaymentAttempt.status.label('attempt_status'),
            CheckoutPaymentAttempt.reconciliation_reason.label('attempt_reconciliation_reason'),
            CheckoutPaymentAttempt.settlement_mode.label('attempt_settlement_mode'),
            CheckoutPaymentAttempt.provider_payment_id.label('provider_payment_id'),
            CheckoutPaymentAttempt.requested_amount_kopeks.label('attempt_requested_amount_kopeks'),
            CheckoutPaymentAttempt.currency.label('attempt_currency'),
            CheckoutPaymentAttempt.provider_method_code.label('attempt_provider_method_code'),
            CheckoutPaymentAttempt.credited_amount_kopeks.label('attempt_credited_amount_kopeks'),
            PlategaPayment.id.label('payment_id'),
            PlategaPayment.user_id.label('payment_user_id'),
            PlategaPayment.platega_transaction_id.label('payment_transaction_id'),
            PlategaPayment.amount_kopeks.label('payment_amount_kopeks'),
            PlategaPayment.currency.label('payment_currency'),
            PlategaPayment.payment_method_code.label('payment_method_code'),
            PlategaPayment.status.label('payment_status'),
            PlategaPayment.is_paid.label('payment_is_paid'),
            PlategaPayment.paid_at.label('payment_paid_at'),
            PlategaPayment.transaction_id.label('payment_local_transaction_id'),
            AccountErasureRequest.id.label('erasure_request_id'),
            AccountErasureRequest.state.label('erasure_state'),
            AccountErasureRequest.panel_state.label('erasure_panel_state'),
            AccountErasureRequest.panel_cleanup_uuids.label('erasure_panel_cleanup_uuids'),
            AccountErasureRequest.finalized_at.label('erasure_finalized_at'),
            User.account_erased_at.label('user_account_erased_at'),
            User.account_erasure_requested_at.label('user_account_erasure_requested_at'),
        )
        .join(User, User.id == SubscriptionCheckout.user_id)
        .join(AccountErasureRequest, AccountErasureRequest.user_id == User.id)
        .join(CheckoutPaymentAttempt, CheckoutPaymentAttempt.checkout_id == SubscriptionCheckout.id)
        .join(PlategaPayment, PlategaPayment.id == CheckoutPaymentAttempt.platega_payment_id)
        .where(
            SubscriptionCheckout.tariff_id == tariff_id,
            SubscriptionCheckout.lifecycle_state == 'operator_review',
            SubscriptionCheckout.terminal_reason == _HISTORICAL_ERASED_DIRECT_MISMATCH,
            SubscriptionCheckout.quote_state == 'expired',
            SubscriptionCheckout.funding_state == 'invoice_terminal',
            SubscriptionCheckout.fulfillment_state == 'not_started',
            SubscriptionCheckout.provisioning_state == 'not_started',
            SubscriptionCheckout.settlement_mode.in_({'direct_purchase_v2', 'legacy_deposit'}),
            SubscriptionCheckout.created_subscription_id.is_(None),
            SubscriptionCheckout.debit_transaction_id.is_(None),
            User.account_erased_at.is_not(None),
            User.account_erasure_requested_at.is_not(None),
            AccountErasureRequest.state == 'completed',
            AccountErasureRequest.finalized_at.is_not(None),
            AccountErasureRequest.panel_state == 'deactivated',
            CheckoutPaymentAttempt.provider == 'platega',
            CheckoutPaymentAttempt.settlement_mode == 'direct_purchase_v2',
            CheckoutPaymentAttempt.status == 'operator_review',
            CheckoutPaymentAttempt.reconciliation_reason == _HISTORICAL_ERASED_DIRECT_MISMATCH,
            CheckoutPaymentAttempt.credited_amount_kopeks == 0,
            PlategaPayment.user_id == SubscriptionCheckout.user_id,
            PlategaPayment.status == 'OPERATOR_REVIEW',
            PlategaPayment.is_paid.is_(False),
            PlategaPayment.paid_at.is_(None),
            PlategaPayment.transaction_id.is_(None),
            PlategaPayment.platega_transaction_id.is_not(None),
            PlategaPayment.platega_transaction_id == CheckoutPaymentAttempt.provider_payment_id,
            PlategaPayment.amount_kopeks == CheckoutPaymentAttempt.requested_amount_kopeks,
            func.upper(PlategaPayment.currency) == 'RUB',
            func.upper(CheckoutPaymentAttempt.currency) == 'RUB',
            PlategaPayment.payment_method_code == CheckoutPaymentAttempt.provider_method_code,
            exactly_one_payment_attempt,
            no_money_or_provisioning,
        )
        .order_by(PlategaPayment.platega_transaction_id)
    )


def _archive_manifest_row(row: dict[str, object]) -> dict[str, object] | None:
    checkout_mode = str(row['checkout_settlement_mode'])
    if checkout_mode == 'direct_purchase_v2':
        variant = 'direct_direct_missing_marker'
    elif checkout_mode == 'legacy_deposit':
        variant = 'direct_legacy_missing_marker'
    else:  # Defensive: the SQL query is deliberately duplicated by the hash.
        return None
    return {
        'tariff_id': int(row['tariff_id']),
        'checkout_id': int(row['checkout_id']),
        'checkout_user_id': int(row['checkout_user_id']),
        'checkout_lifecycle_state': str(row['checkout_lifecycle_state']),
        'checkout_terminal_reason': str(row['checkout_terminal_reason']),
        'checkout_quote_state': str(row['checkout_quote_state']),
        'checkout_funding_state': str(row['checkout_funding_state']),
        'checkout_fulfillment_state': str(row['checkout_fulfillment_state']),
        'checkout_provisioning_state': str(row['checkout_provisioning_state']),
        'checkout_settlement_mode': checkout_mode,
        'checkout_created_subscription_id': row['checkout_created_subscription_id'],
        'checkout_debit_transaction_id': row['checkout_debit_transaction_id'],
        'attempt_id': int(row['attempt_id']),
        'attempt_status': str(row['attempt_status']),
        'attempt_reconciliation_reason': str(row['attempt_reconciliation_reason']),
        'attempt_settlement_mode': str(row['attempt_settlement_mode']),
        'provider_payment_id': str(row['provider_payment_id']),
        'attempt_requested_amount_kopeks': int(row['attempt_requested_amount_kopeks']),
        'attempt_currency': str(row['attempt_currency']).upper(),
        'attempt_provider_method_code': int(row['attempt_provider_method_code']),
        'attempt_credited_amount_kopeks': int(row['attempt_credited_amount_kopeks']),
        'payment_id': int(row['payment_id']),
        'payment_user_id': int(row['payment_user_id']),
        'payment_transaction_id': str(row['payment_transaction_id']),
        'payment_amount_kopeks': int(row['payment_amount_kopeks']),
        'payment_currency': str(row['payment_currency']).upper(),
        'payment_method_code': int(row['payment_method_code']),
        'payment_status': str(row['payment_status']),
        'payment_is_paid': bool(row['payment_is_paid']),
        'payment_paid_at': _manifest_timestamp(row['payment_paid_at']),
        'payment_local_transaction_id': row['payment_local_transaction_id'],
        'erasure_request_id': int(row['erasure_request_id']),
        'erasure_state': str(row['erasure_state']),
        'erasure_panel_state': str(row['erasure_panel_state']),
        'erasure_panel_cleanup_uuids': row['erasure_panel_cleanup_uuids'],
        'erasure_finalized_at': _manifest_timestamp(row['erasure_finalized_at']),
        'user_account_erased_at': _manifest_timestamp(row['user_account_erased_at']),
        'user_account_erasure_requested_at': _manifest_timestamp(row['user_account_erasure_requested_at']),
        'variant': variant,
    }


async def _archive_manifest_rows(db: AsyncSession, *, tariff_id: int) -> list[dict[str, object]]:
    result = await db.execute(_final_erasure_archive_candidate_query(tariff_id))
    rows = []
    for row in result.mappings():
        manifest_row = _archive_manifest_row(dict(row))
        if manifest_row is None:
            return []
        rows.append(manifest_row)
    return rows


async def _lock_archive_manifest_rows(db: AsyncSession, rows: list[dict[str, object]]) -> bool:
    """Lock the archive in the same Payment -> User -> Attempt -> Checkout order as reconciliation."""

    lock_targets = (
        (PlategaPayment, 'payment_id'),
        (User, 'payment_user_id'),
        (CheckoutPaymentAttempt, 'attempt_id'),
        (SubscriptionCheckout, 'checkout_id'),
        (AccountErasureRequest, 'erasure_request_id'),
    )
    for model, key in lock_targets:
        expected_ids = sorted({int(row[key]) for row in rows})
        locked_ids = (
            (await db.execute(select(model.id).where(model.id.in_(expected_ids)).order_by(model.id).with_for_update()))
            .scalars()
            .all()
        )
        if locked_ids != expected_ids:
            return False
    return True


async def _approved_final_erasure_archive_checkout_ids(
    db: AsyncSession,
    *,
    tariff_id: int,
) -> frozenset[int]:
    """Return the five owner-pinned historical checkout IDs, or no exception."""

    if tariff_id != _APPROVED_FINAL_ERASURE_ARCHIVE_TARIFF_ID:
        return frozenset()
    initial_rows = await _archive_manifest_rows(db, tariff_id=tariff_id)
    if _archive_manifest_sha256(initial_rows) != _APPROVED_FINAL_ERASURE_ARCHIVE_SHA256:
        return frozenset()
    if not await _lock_archive_manifest_rows(db, initial_rows):
        return frozenset()
    locked_rows = await _archive_manifest_rows(db, tariff_id=tariff_id)
    if _archive_manifest_sha256(locked_rows) != _APPROVED_FINAL_ERASURE_ARCHIVE_SHA256:
        return frozenset()
    return frozenset(int(row['checkout_id']) for row in locked_rows)


async def _assert_tariff_squad_change_has_no_live_checkout(db: AsyncSession, tariff: Tariff) -> None:
    """Do not invalidate any live checkout's captured access entitlement."""

    # A direct provider attempt deliberately stays ``paid_processing`` after
    # a successful sale.  It is then an idempotency/reconciliation record,
    # not an open invoice.  Once the checkout has reached its fully delivered
    # ``ready`` state, retaining that historical attempt must not permanently
    # prevent an administrator from changing a tariff's future squad choice.
    # Do *not* relax this for ``fulfilling`` / ``retry``: those still have a
    # live provision path and therefore must keep the tariff fence.
    fully_delivered_checkout = (
        (SubscriptionCheckout.lifecycle_state == 'ready')
        & (SubscriptionCheckout.funding_state == 'funded')
        & (SubscriptionCheckout.fulfillment_state == 'fulfilled')
        & (SubscriptionCheckout.provisioning_state == 'ready')
    )
    live_provider_attempt = exists(
        select(CheckoutPaymentAttempt.id).where(
            CheckoutPaymentAttempt.checkout_id == SubscriptionCheckout.id,
            CheckoutPaymentAttempt.status.in_(_DIRECT_PROVIDER_ATTEMPT_OPEN_STATES),
            ~fully_delivered_checkout,
        )
    )
    approved_archive_checkout_ids = await _approved_final_erasure_archive_checkout_ids(db, tariff_id=tariff.id)
    live_checkout_id = await db.scalar(
        select(SubscriptionCheckout.id)
        .where(
            SubscriptionCheckout.tariff_id == tariff.id,
            or_(
                and_(
                    SubscriptionCheckout.lifecycle_state.not_in(_CHECKOUT_TERMINAL_STATES),
                    SubscriptionCheckout.id.not_in(approved_archive_checkout_ids),
                ),
                live_provider_attempt,
            ),
        )
        .limit(1)
    )
    if live_checkout_id is not None:
        raise ValueError(
            'Cannot change Internal Squads while this tariff has a live checkout or Platega invoice. '
            'Finish its safe reconciliation first.'
        )


def _normalize_period_prices(period_prices: dict[int, int] | None) -> dict[str, int]:
    """Нормализует цены периодов в формат {str: int}."""
    if not period_prices:
        return {}

    normalized: dict[str, int] = {}

    for key, value in period_prices.items():
        try:
            period = int(key)
            price = int(value)
        except (TypeError, ValueError):
            continue

        if period > 0 and price >= 0:
            normalized[str(period)] = price

    return normalized


async def get_all_tariffs(
    db: AsyncSession,
    *,
    include_inactive: bool = False,
    offset: int = 0,
    limit: int | None = None,
) -> list[Tariff]:
    """Получает все тарифы с опциональной фильтрацией по активности."""
    query = select(Tariff).options(selectinload(Tariff.allowed_promo_groups))

    if not include_inactive:
        query = query.where(Tariff.is_active.is_(True))

    query = query.order_by(Tariff.display_order, Tariff.id)

    if offset:
        query = query.offset(offset)
    if limit is not None:
        query = query.limit(limit)

    result = await db.execute(query)
    return result.scalars().all()


async def get_tariff_by_id(
    db: AsyncSession,
    tariff_id: int,
    *,
    with_promo_groups: bool = True,
) -> Tariff | None:
    """Получает тариф по ID."""
    query = select(Tariff).where(Tariff.id == tariff_id)

    if with_promo_groups:
        query = query.options(selectinload(Tariff.allowed_promo_groups))

    result = await db.execute(query)
    return result.scalars().first()


async def count_tariffs(db: AsyncSession, *, include_inactive: bool = False) -> int:
    """Подсчитывает количество тарифов."""
    query = select(func.count(Tariff.id))

    if not include_inactive:
        query = query.where(Tariff.is_active.is_(True))

    result = await db.execute(query)
    return int(result.scalar_one())


async def get_trial_tariff(db: AsyncSession) -> Tariff | None:
    """Получает тариф, доступный для триала (is_trial_available=True).

    Триальный тариф может быть неактивным — это сделано специально,
    чтобы он не отображался в списке покупки, но использовался для триала
    со своими лимитами (трафик, устройства, серверы).

    Сортируется по updated_at DESC, чтобы вернуть последний установленный
    триальный тариф (на случай если их несколько).
    """
    query = (
        select(Tariff)
        .where(Tariff.is_trial_available.is_(True))
        .options(selectinload(Tariff.allowed_promo_groups))
        .order_by(Tariff.updated_at.desc().nullslast(), Tariff.id.desc())
        .limit(1)
    )
    result = await db.execute(query)
    return result.scalars().first()


async def set_trial_tariff(db: AsyncSession, tariff_id: int) -> Tariff | None:
    """Устанавливает тариф как триальный (снимает флаг с других тарифов)."""
    tariff = await get_tariff_by_id(db, tariff_id)
    if tariff is None:
        return None
    if getattr(tariff, 'entitlement_mode', None) == 'access_point_managed':
        raise ValueError('access-point tariffs cannot be configured as trials')
    from app.services.public_location_entitlement_service import assert_tariff_sellable

    await assert_tariff_sellable(db, tariff)
    # Снимаем флаг с всех тарифов
    await db.execute(Tariff.__table__.update().values(is_trial_available=False))

    # Устанавливаем флаг на выбранный тариф
    tariff.is_trial_available = True
    await db.commit()
    await db.refresh(tariff)

    return tariff


async def clear_trial_tariff(db: AsyncSession) -> None:
    """Снимает флаг триала со всех тарифов."""
    await db.execute(Tariff.__table__.update().values(is_trial_available=False))
    await db.commit()


async def get_all_active_tariffs(db: AsyncSession) -> list[Tariff]:
    """Get all active tariffs."""
    result = await db.execute(select(Tariff).where(Tariff.is_active.is_(True)).order_by(Tariff.tier_level))
    return list(result.scalars().all())


async def get_tariffs_for_user(
    db: AsyncSession,
    promo_group_id: int | None = None,
) -> list[Tariff]:
    """
    Получает тарифы, доступные для пользователя с учетом его промогруппы.
    Если у тарифа нет ограничений по промогруппам - он доступен всем.
    """
    query = (
        select(Tariff)
        .options(selectinload(Tariff.allowed_promo_groups))
        .where(Tariff.is_active.is_(True))
        .order_by(Tariff.display_order, Tariff.id)
    )

    result = await db.execute(query)
    tariffs = result.scalars().all()

    # Фильтруем по промогруппе
    available_tariffs = []
    for tariff in tariffs:
        if not tariff.allowed_promo_groups:
            # Нет ограничений - доступен всем
            available_tariffs.append(tariff)
        elif promo_group_id is not None:
            # Проверяем, есть ли промогруппа пользователя в списке разрешенных
            if any(pg.id == promo_group_id for pg in tariff.allowed_promo_groups):
                available_tariffs.append(tariff)
        # else: пользователь без промогруппы, а у тарифа есть ограничения - пропускаем

    return available_tariffs


async def create_tariff(
    db: AsyncSession,
    name: str,
    *,
    description: str | None = None,
    display_order: int = 0,
    is_active: bool = True,
    traffic_limit_gb: int = 100,
    device_limit: int = 1,
    device_price_kopeks: int | None = None,
    max_device_limit: int | None = None,
    device_purchase_options: list[int] | None = None,
    allowed_squads: list[str] | None = None,
    server_traffic_limits: dict[str, dict] | None = None,
    period_prices: dict[int, int] | None = None,
    tier_level: int = 1,
    is_trial_available: bool = False,
    allow_traffic_topup: bool = True,
    promo_group_ids: list[int] | None = None,
    traffic_topup_enabled: bool = False,
    traffic_topup_packages: dict[str, int] | None = None,
    max_topup_traffic_gb: int = 0,
    is_daily: bool = False,
    daily_price_kopeks: int = 0,
    # Произвольное количество дней
    custom_days_enabled: bool = False,
    price_per_day_kopeks: int = 0,
    min_days: int = 1,
    max_days: int = 365,
    # Произвольный трафик при покупке
    custom_traffic_enabled: bool = False,
    traffic_price_per_gb_kopeks: int = 0,
    min_traffic_gb: int = 1,
    max_traffic_gb: int = 1000,
    # Видимость в разделе подарков
    show_in_gift: bool = True,
    # Режим сброса трафика
    traffic_reset_mode: str | None = None,  # DAY, WEEK, MONTH, MONTH_ROLLING, NO_RESET, None = глобальная настройка
    # Внешний сквад RemnaWave
    external_squad_uuid: str | None = None,
    # New tariffs use the upstream Internal Squad contract.  Legacy/AP modes
    # remain readable compatibility states and are never created here.
    entitlement_mode: str = 'native_squads',
) -> Tariff:
    """Создает новый тариф."""
    if entitlement_mode != 'native_squads':
        raise ValueError('new tariffs must use the native Internal Squad entitlement mode')
    if is_active or is_trial_available:
        from app.services.public_location_entitlement_service import (
            EntitlementResolutionError,
            assert_tariff_sellable,
        )

        try:
            await assert_tariff_sellable(
                db,
                SimpleNamespace(entitlement_mode='native_squads', allowed_squads=allowed_squads or []),
            )
        except EntitlementResolutionError as exc:
            raise ValueError(f'tariff has no available Internal Squad selection: {exc}') from exc
    normalized_prices = _normalize_period_prices(period_prices)
    normalized_device_options = normalize_device_purchase_options(
        device_purchase_options,
        base_device_limit=max(1, device_limit),
        max_device_limit=max_device_limit,
        device_price_kopeks=device_price_kopeks,
    )

    tariff = Tariff(
        name=name.strip(),
        description=description.strip() if description else None,
        display_order=max(0, display_order),
        is_active=is_active,
        traffic_limit_gb=max(0, traffic_limit_gb),
        device_limit=max(1, device_limit),
        device_price_kopeks=device_price_kopeks,
        max_device_limit=max_device_limit,
        device_purchase_options=normalized_device_options,
        allowed_squads=allowed_squads or [],
        entitlement_mode=entitlement_mode,
        server_traffic_limits=server_traffic_limits or {},
        period_prices=normalized_prices,
        tier_level=max(1, tier_level),
        is_trial_available=is_trial_available,
        allow_traffic_topup=allow_traffic_topup,
        traffic_topup_enabled=traffic_topup_enabled,
        traffic_topup_packages=traffic_topup_packages or {},
        max_topup_traffic_gb=max(0, max_topup_traffic_gb),
        is_daily=is_daily,
        daily_price_kopeks=max(0, daily_price_kopeks),
        # Произвольное количество дней
        custom_days_enabled=custom_days_enabled,
        price_per_day_kopeks=max(0, price_per_day_kopeks),
        min_days=max(1, min_days),
        max_days=max(1, max_days),
        # Произвольный трафик при покупке
        custom_traffic_enabled=custom_traffic_enabled,
        traffic_price_per_gb_kopeks=max(0, traffic_price_per_gb_kopeks),
        min_traffic_gb=max(1, min_traffic_gb),
        max_traffic_gb=max(1, max_traffic_gb),
        # Видимость в разделе подарков
        show_in_gift=show_in_gift,
        # Режим сброса трафика
        traffic_reset_mode=traffic_reset_mode,
        # Внешний сквад
        external_squad_uuid=external_squad_uuid,
    )

    db.add(tariff)
    await db.flush()

    # Добавляем промогруппы если указаны
    if promo_group_ids:
        promo_groups_result = await db.execute(select(PromoGroup).where(PromoGroup.id.in_(promo_group_ids)))
        promo_groups = promo_groups_result.scalars().all()
        # Refresh чтобы избежать lazy load в async контексте
        await db.refresh(tariff, ['allowed_promo_groups'])
        tariff.allowed_promo_groups = list(promo_groups)

    await db.commit()
    await db.refresh(tariff)

    logger.info(
        'Создан тариф',
        tariff_name=tariff.name,
        tariff_id=tariff.id,
        tier_level=tariff.tier_level,
        traffic_limit_gb=tariff.traffic_limit_gb,
        device_limit=tariff.device_limit,
        normalized_prices=normalized_prices,
    )

    return tariff


async def update_tariff(
    db: AsyncSession,
    tariff: Tariff,
    *,
    name: str | None = None,
    description: str | None = None,
    display_order: int | None = None,
    is_active: bool | None = None,
    traffic_limit_gb: int | None = None,
    device_limit: int | None = None,
    device_price_kopeks: int | None = ...,  # ... = не передан, None = сбросить
    max_device_limit: int | None = ...,  # ... = не передан, None = сбросить (без лимита)
    device_purchase_options: list[int] | None = ...,  # ... = не передан, None = legacy
    allowed_squads: list[str] | None = None,
    server_traffic_limits: dict[str, dict] | None = None,
    period_prices: dict[int, int] | None = None,
    tier_level: int | None = None,
    is_trial_available: bool | None = None,
    trial_duration_days: int | None = ...,  # ... = не передан, None = сбросить к дефолту (TRIAL_DURATION_DAYS)
    allow_traffic_topup: bool | None = None,
    promo_group_ids: list[int] | None = None,
    traffic_topup_enabled: bool | None = None,
    traffic_topup_packages: dict[str, int] | None = None,
    max_topup_traffic_gb: int | None = None,
    is_daily: bool | None = None,
    daily_price_kopeks: int | None = None,
    # Произвольное количество дней
    custom_days_enabled: bool | None = None,
    price_per_day_kopeks: int | None = None,
    min_days: int | None = None,
    max_days: int | None = None,
    # Произвольный трафик при покупке
    custom_traffic_enabled: bool | None = None,
    traffic_price_per_gb_kopeks: int | None = None,
    min_traffic_gb: int | None = None,
    max_traffic_gb: int | None = None,
    # Видимость в разделе подарков
    show_in_gift: bool | None = None,
    # Режим сброса трафика
    traffic_reset_mode: str | None = ...,  # ... = не передан, None = сбросить к глобальной настройке
    # Внешний сквад RemnaWave
    external_squad_uuid: str | None = ...,  # ... = не передан, None = убрать внешний сквад
) -> Tariff:
    """Обновляет существующий тариф."""
    # The direct checkout birth and provider-intent paths take this same
    # tariff-row lock.  Keep it through the live-checkout query and the mode
    # mutation so an AP invoice cannot be born between those two operations.
    if allowed_squads is not None:
        locked_tariff = await db.get(Tariff, tariff.id, with_for_update=True, populate_existing=True)
        if locked_tariff is None:
            raise ValueError('tariff no longer exists')
        tariff = locked_tariff

    selection_changed = allowed_squads is not None and _normalized_squad_selection(
        allowed_squads
    ) != _normalized_squad_selection(tariff.allowed_squads)
    will_transition_to_native = allowed_squads is not None and tariff.entitlement_mode != 'native_squads'
    if selection_changed or will_transition_to_native:
        await _assert_tariff_squad_change_has_no_live_checkout(db, tariff)

    if getattr(tariff, 'entitlement_mode', None) == 'access_point_managed':
        if is_daily is True:
            raise ValueError('access-point tariffs cannot be configured as daily')
        if is_trial_available is True:
            raise ValueError('access-point tariffs cannot be configured as trials')
    activating_or_enabling_trial = (is_active is True and not tariff.is_active) or (
        is_trial_available is True and not tariff.is_trial_available
    )
    if activating_or_enabling_trial or allowed_squads is not None:
        from app.services.public_location_entitlement_service import (
            EntitlementResolutionError,
            assert_tariff_sellable,
        )

        # When an administrator activates a historical tariff and selects
        # native squads in the same request, evaluate that proposed state
        # without mutating (or autoflushing) the persisted ORM object first.
        candidate = (
            SimpleNamespace(entitlement_mode='native_squads', allowed_squads=allowed_squads)
            if allowed_squads is not None
            else tariff
        )
        try:
            await assert_tariff_sellable(db, candidate)
        except EntitlementResolutionError as exc:
            if allowed_squads is None:
                raise
            raise ValueError(f'tariff has no available Internal Squad selection: {exc}') from exc
    revision_before = (
        tariff.is_active,
        tariff.traffic_limit_gb,
        tariff.device_limit,
        tariff.device_price_kopeks,
        tariff.max_device_limit,
        tuple(tariff.device_purchase_options or ()),
        tuple(sorted((tariff.period_prices or {}).items())),
        tuple(tariff.allowed_squads or ()),
        tariff.is_daily,
        tariff.custom_days_enabled,
        tariff.custom_traffic_enabled,
    )
    if name is not None:
        tariff.name = name.strip()
    if description is not None:
        tariff.description = description.strip() if description else None
    if display_order is not None:
        tariff.display_order = max(0, display_order)
    if is_active is not None:
        tariff.is_active = is_active
    if traffic_limit_gb is not None:
        tariff.traffic_limit_gb = max(0, traffic_limit_gb)
    if device_limit is not None:
        tariff.device_limit = max(1, device_limit)
    if device_price_kopeks is not ...:
        # Если передан device_price_kopeks (включая None) - обновляем
        tariff.device_price_kopeks = device_price_kopeks
    if max_device_limit is not ...:
        # Если передан max_device_limit (включая None) - обновляем
        tariff.max_device_limit = max_device_limit
    if device_purchase_options is not ...:
        tariff.device_purchase_options = normalize_device_purchase_options(
            device_purchase_options,
            base_device_limit=int(tariff.device_limit or 1),
            max_device_limit=tariff.max_device_limit,
            device_price_kopeks=tariff.device_price_kopeks,
        )
    if allowed_squads is not None:
        tariff.allowed_squads = allowed_squads
        # This is the explicit forward-only boundary from frozen historical
        # manifests to the normal Bedolaga tariff editor.  Existing issued
        # subscriptions retain their own connected_squads until propagation.
        tariff.entitlement_mode = 'native_squads'
    if server_traffic_limits is not None:
        tariff.server_traffic_limits = server_traffic_limits
    if allow_traffic_topup is not None:
        tariff.allow_traffic_topup = allow_traffic_topup
    if period_prices is not None:
        tariff.period_prices = _normalize_period_prices(period_prices)
    if tier_level is not None:
        tariff.tier_level = max(1, tier_level)
    if is_trial_available is not None:
        tariff.is_trial_available = is_trial_available
    if trial_duration_days is not ...:
        # Передан (включая None) — обновляем. None = использовать TRIAL_DURATION_DAYS.
        tariff.trial_duration_days = trial_duration_days
    if traffic_topup_enabled is not None:
        tariff.traffic_topup_enabled = traffic_topup_enabled
    if traffic_topup_packages is not None:
        tariff.traffic_topup_packages = traffic_topup_packages
    if max_topup_traffic_gb is not None:
        tariff.max_topup_traffic_gb = max(0, max_topup_traffic_gb)
    if is_daily is not None:
        tariff.is_daily = is_daily
    if daily_price_kopeks is not None:
        tariff.daily_price_kopeks = max(0, daily_price_kopeks)
    # Произвольное количество дней
    if custom_days_enabled is not None:
        tariff.custom_days_enabled = custom_days_enabled
    if price_per_day_kopeks is not None:
        tariff.price_per_day_kopeks = max(0, price_per_day_kopeks)
    if min_days is not None:
        tariff.min_days = max(1, min_days)
    if max_days is not None:
        tariff.max_days = max(1, max_days)
    # Произвольный трафик при покупке
    if custom_traffic_enabled is not None:
        tariff.custom_traffic_enabled = custom_traffic_enabled
    if traffic_price_per_gb_kopeks is not None:
        tariff.traffic_price_per_gb_kopeks = max(0, traffic_price_per_gb_kopeks)
    if min_traffic_gb is not None:
        tariff.min_traffic_gb = max(1, min_traffic_gb)
    if max_traffic_gb is not None:
        tariff.max_traffic_gb = max(1, max_traffic_gb)
    # Видимость в разделе подарков
    if show_in_gift is not None:
        tariff.show_in_gift = show_in_gift
    # Режим сброса трафика
    if traffic_reset_mode is not ...:
        tariff.traffic_reset_mode = traffic_reset_mode
    # Внешний сквад
    if external_squad_uuid is not ...:
        tariff.external_squad_uuid = external_squad_uuid

    revision_after = (
        tariff.is_active,
        tariff.traffic_limit_gb,
        tariff.device_limit,
        tariff.device_price_kopeks,
        tariff.max_device_limit,
        tuple(tariff.device_purchase_options or ()),
        tuple(sorted((tariff.period_prices or {}).items())),
        tuple(tariff.allowed_squads or ()),
        tariff.is_daily,
        tariff.custom_days_enabled,
        tariff.custom_traffic_enabled,
    )
    if revision_after != revision_before:
        tariff.pricing_revision = int(tariff.pricing_revision or 1) + 1

    # Обновляем промогруппы если указаны
    if promo_group_ids is not None:
        if promo_group_ids:
            promo_groups_result = await db.execute(select(PromoGroup).where(PromoGroup.id.in_(promo_group_ids)))
            promo_groups = promo_groups_result.scalars().all()
            tariff.allowed_promo_groups = list(promo_groups)
        else:
            tariff.allowed_promo_groups = []

    await db.commit()
    await db.refresh(tariff)

    logger.info('Обновлен тариф', tariff_name=tariff.name, tariff_id=tariff.id)

    return tariff


async def delete_tariff(db: AsyncSession, tariff: Tariff) -> bool:
    """
    Удаляет тариф.
    FK с ondelete=RESTRICT — удаление невозможно, если есть привязанные подписки.
    Вызывающий код должен проверить отсутствие активных подписок до вызова.
    """
    tariff_id = tariff.id
    tariff_name = tariff.name

    # Подсчитываем подписки с этим тарифом
    subscriptions_count = await db.execute(
        select(func.count(Subscription.id)).where(Subscription.tariff_id == tariff_id)
    )
    affected_subscriptions = subscriptions_count.scalar_one()

    # Удаляем тариф (FK RESTRICT — подписок с tariff_id быть не должно)
    await db.delete(tariff)
    await db.commit()

    logger.info(
        'Удален тариф',
        tariff_name=tariff_name,
        tariff_id=tariff_id,
        affected_subscriptions=affected_subscriptions,
    )

    return True


async def get_tariff_subscriptions_count(db: AsyncSession, tariff_id: int) -> int:
    """Подсчитывает количество подписок на тарифе."""
    result = await db.execute(select(func.count(Subscription.id)).where(Subscription.tariff_id == tariff_id))
    return int(result.scalar_one())


async def get_active_subscriptions_count_by_tariff_id(db: AsyncSession, tariff_id: int) -> int:
    """Подсчитывает количество активных (active/trial) подписок на тарифе."""
    active_statuses = [SubscriptionStatus.ACTIVE.value, SubscriptionStatus.TRIAL.value]
    result = await db.execute(
        select(func.count(Subscription.id)).where(
            Subscription.tariff_id == tariff_id,
            Subscription.status.in_(active_statuses),
        )
    )
    return int(result.scalar_one())


async def set_tariff_promo_groups(
    db: AsyncSession,
    tariff: Tariff,
    promo_group_ids: list[int],
) -> Tariff:
    """Устанавливает промогруппы для тарифа."""
    if promo_group_ids:
        promo_groups_result = await db.execute(select(PromoGroup).where(PromoGroup.id.in_(promo_group_ids)))
        promo_groups = promo_groups_result.scalars().all()
        tariff.allowed_promo_groups = list(promo_groups)
    else:
        tariff.allowed_promo_groups = []

    await db.commit()
    await db.refresh(tariff)

    return tariff


async def add_promo_group_to_tariff(
    db: AsyncSession,
    tariff: Tariff,
    promo_group_id: int,
) -> bool:
    """Добавляет промогруппу к тарифу."""
    promo_group = await db.get(PromoGroup, promo_group_id)
    if not promo_group:
        return False

    if promo_group not in tariff.allowed_promo_groups:
        tariff.allowed_promo_groups.append(promo_group)
        await db.commit()

    return True


async def remove_promo_group_from_tariff(
    db: AsyncSession,
    tariff: Tariff,
    promo_group_id: int,
) -> bool:
    """Удаляет промогруппу из тарифа."""
    for pg in tariff.allowed_promo_groups:
        if pg.id == promo_group_id:
            tariff.allowed_promo_groups.remove(pg)
            await db.commit()
            return True
    return False


async def get_tariffs_with_subscriptions_count(
    db: AsyncSession,
    *,
    include_inactive: bool = False,
) -> list[tuple]:
    """Получает тарифы с количеством подписок."""
    query = (
        select(Tariff, func.count(Subscription.id))
        .outerjoin(Subscription, Subscription.tariff_id == Tariff.id)
        .group_by(Tariff.id)
        .order_by(Tariff.display_order, Tariff.id)
    )

    if not include_inactive:
        query = query.where(Tariff.is_active.is_(True))

    result = await db.execute(query)
    return result.all()


async def reorder_tariffs(
    db: AsyncSession,
    tariff_order: list[int],
) -> None:
    """Изменяет порядок отображения тарифов."""
    for order, tariff_id in enumerate(tariff_order):
        await db.execute(update(Tariff).where(Tariff.id == tariff_id).values(display_order=order))

    logger.info('Изменен порядок тарифов', tariff_order=tariff_order)


async def sync_default_tariff_from_config(db: AsyncSession) -> Tariff | None:
    """
    Синхронизирует дефолтный тариф из конфига (.env) в БД.
    Создаёт тариф "Стандартный" только если в БД нет тарифов.
    Существующий тариф НЕ перезаписывается — админ управляет им через кабинет.

    Returns:
        Tariff или None если не требуется синхронизация
    """
    from app.config import PERIOD_PRICES, settings

    # Проверяем есть ли тарифы в БД
    result = await db.execute(select(func.count(Tariff.id)))
    tariff_count = result.scalar() or 0

    # Собираем цены из конфига
    period_prices = {}
    for period, price in PERIOD_PRICES.items():
        if price > 0:
            period_prices[str(period)] = price

    if not period_prices:
        logger.warning('Нет цен в конфиге для создания дефолтного тарифа')
        return None

    # Ищем тариф с именем "Стандартный" или первый тариф
    result = await db.execute(select(Tariff).where(Tariff.name == 'Стандартный').limit(1))
    existing_tariff = result.scalar_one_or_none()

    if existing_tariff:
        # Тариф уже существует — НЕ перезаписываем настройки из конфига.
        # Админ управляет тарифом через кабинет, синхронизация не нужна.
        logger.info(
            "Дефолтный тариф 'Стандартный' уже существует, пропускаем sync из конфига",
            existing_tariff_id=existing_tariff.id,
        )
        return existing_tariff

    if tariff_count == 0:
        # Создаём новый дефолтный тариф
        new_tariff = Tariff(
            name='Стандартный',
            description='Базовый тарифный план',
            is_active=True,
            is_trial_available=True,
            traffic_limit_gb=settings.DEFAULT_TRAFFIC_LIMIT_GB,
            device_limit=settings.DEFAULT_DEVICE_LIMIT,
            tier_level=1,
            display_order=0,
            period_prices=period_prices,
            allowed_squads=[],  # Все серверы по умолчанию
            server_traffic_limits={},
        )
        db.add(new_tariff)
        await db.commit()
        await db.refresh(new_tariff)
        logger.info("Создан дефолтный тариф 'Стандартный' из конфига", period_prices=period_prices)
        return new_tariff

    return None


async def load_period_prices_from_db(db: AsyncSession) -> None:
    """
    Загружает периоды/цены из тарифа в PERIOD_PRICES.
    Работает ТОЛЬКО в режиме tariffs. В режиме classic используются цены из .env.
    """
    from app.config import set_period_prices_from_db, settings

    # В режиме classic НЕ загружаем цены из тарифов - используем .env
    if settings.is_classic_mode():
        logger.info('Режим classic: цены периодов берутся из .env, тарифы игнорируются')
        return

    try:
        # Ищем тариф "Стандартный" или первый активный тариф
        result = await db.execute(
            select(Tariff).where(Tariff.is_active.is_(True)).order_by(Tariff.display_order, Tariff.id).limit(1)
        )
        tariff = result.scalar_one_or_none()

        if not tariff:
            logger.info('Активные тарифы не найдены, используются цены из .env')
            return

        if not tariff.period_prices:
            logger.warning('Тариф найден, но period_prices пуст', tariff_name=tariff.name, tariff_id=tariff.id)
            return

        # Преобразуем строковые ключи в int
        period_prices = {int(days): int(price) for days, price in tariff.period_prices.items() if int(price) > 0}

        if period_prices:
            set_period_prices_from_db(period_prices)
            logger.info(
                "Загружены периоды из тарифа '%s': %s",
                tariff.name,
                {f'{d}д': f'{p // 100}₽' for d, p in period_prices.items()},
            )
        else:
            logger.warning('Тариф не имеет активных периодов (все цены = 0)', tariff_name=tariff.name)

    except Exception as e:
        logger.error('Ошибка загрузки периодов из БД', e=e)


async def ensure_tariffs_synced(db: AsyncSession) -> None:
    """
    Проверяет и синхронизирует тарифы при запуске.
    Вызывается при старте бота.
    """
    try:
        await sync_default_tariff_from_config(db)
        # Загружаем периоды из БД в PERIOD_PRICES
        await load_period_prices_from_db(db)
    except Exception as e:
        logger.error('Ошибка синхронизации тарифов', e=e)
