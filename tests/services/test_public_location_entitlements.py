import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.cabinet.routes import auth as cabinet_auth
from app.cabinet.routes.admin_public_locations import router as public_location_admin_router
from app.cabinet.routes.subscription_modules.servers import get_available_countries, update_countries
from app.cabinet.routes.subscription_modules.tariff_switch import switch_tariff as cabinet_tariff_switch
from app.database.crud import subscription as subscription_crud
from app.database.models import SubscriptionStatus
from app.handlers.admin import tariffs as telegram_admin_tariffs, users as telegram_admin_users
from app.handlers.subscription import purchase as classic_purchase
from app.services import (
    daily_subscription_service,
    remnawave_service,
    subscription_auto_purchase_service,
    subscription_renewal_service,
)
from app.services.device_first_checkout_service import _direct_sale_snapshot
from app.services.public_location_entitlement_service import (
    EntitlementResolutionError,
    ResolvedEntitlement,
    get_effective_subscription_resolved_entitlement,
    get_subscription_resolved_entitlement,
    resolve_tariff_entitlement,
)
from app.webapi.routes.miniapp import switch_tariff_endpoint, update_subscription_servers_endpoint
from app.webapi.routes.subscriptions import create_subscription
from app.webapi.routes.users import create_user_subscription


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


@pytest.mark.asyncio
async def test_location_managed_resolver_rejects_hidden_before_any_write():
    location = SimpleNamespace(id='pl', visibility='hidden', lifecycle='published', health='healthy')
    mapping = SimpleNamespace(is_dedicated_verified=True, internal_squad_uuid='never-exposed')
    db = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(all=lambda: [(SimpleNamespace(), location, mapping)]))
    )
    tariff = SimpleNamespace(id=1, entitlement_mode='location_managed', location_policy_revision=4)

    with pytest.raises(EntitlementResolutionError, match='not available'):
        await resolve_tariff_entitlement(db, tariff)

    db.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_location_managed_resolver_rejects_unverified_mapping():
    location = SimpleNamespace(id='pl', visibility='visible', lifecycle='published', health='healthy')
    mapping = SimpleNamespace(is_dedicated_verified=False, internal_squad_uuid='never-exposed')
    db = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(all=lambda: [(SimpleNamespace(), location, mapping)]))
    )
    tariff = SimpleNamespace(id=1, entitlement_mode='location_managed', location_policy_revision=4)

    with pytest.raises(EntitlementResolutionError, match='verified dedicated'):
        await resolve_tariff_entitlement(db, tariff)


@pytest.mark.asyncio
async def test_location_managed_resolver_returns_only_exact_safe_mapping():
    location = SimpleNamespace(id='pl', visibility='visible', lifecycle='published', health='healthy')
    mapping = SimpleNamespace(is_dedicated_verified=True, internal_squad_uuid='internal-only')
    db = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(all=lambda: [(SimpleNamespace(), location, mapping)]))
    )
    tariff = SimpleNamespace(id=1, entitlement_mode='location_managed', location_policy_revision=4)

    result = await resolve_tariff_entitlement(db, tariff, selected_location_ids=['pl'])

    assert result.location_ids == ('pl',)
    assert result.squad_uuids == ('internal-only',)
    assert result.policy_revision == 4


@pytest.mark.asyncio
async def test_one_location_can_resolve_to_multiple_verified_dedicated_squads():
    location = SimpleNamespace(id='pl', visibility='visible', lifecycle='published', health='healthy')
    first = SimpleNamespace(is_dedicated_verified=True, internal_squad_uuid='customer-pl-a')
    second = SimpleNamespace(is_dedicated_verified=True, internal_squad_uuid='customer-pl-b')
    db = SimpleNamespace(
        execute=AsyncMock(
            return_value=SimpleNamespace(
                all=lambda: [(SimpleNamespace(), location, first), (SimpleNamespace(), location, second)]
            )
        )
    )
    tariff = SimpleNamespace(id=1, entitlement_mode='location_managed', location_policy_revision=4)

    result = await resolve_tariff_entitlement(db, tariff)

    assert result.location_ids == ('pl',)
    assert result.squad_uuids == ('customer-pl-a', 'customer-pl-b')


@pytest.mark.asyncio
async def test_access_point_snapshot_rebuilds_its_inventory_fingerprint() -> None:
    original = ResolvedEntitlement(
        ('public-point-pl',),
        ('internal-only-pl',),
        2,
        'access_point_policy',
        'point-policy-fingerprint',
    )
    snapshot = SimpleNamespace(
        location_ids=list(original.location_ids),
        technical_squad_uuids=list(original.squad_uuids),
        policy_revision=original.policy_revision,
        provenance=original.provenance,
        inventory_fingerprint=original.inventory_fingerprint,
        snapshot_hash=original.snapshot_hash,
    )
    db = SimpleNamespace(
        get=AsyncMock(return_value=SimpleNamespace(tariff_id=None)),
        scalar=AsyncMock(return_value=snapshot),
    )

    rebuilt = await get_subscription_resolved_entitlement(db, 12, allow_access_point_baseline=True)

    assert rebuilt == original


@pytest.mark.asyncio
async def test_access_point_policy_cannot_be_resolved_from_a_legacy_purchase_path() -> None:
    """The shared resolver is the before-money fence for every old writer."""

    db = SimpleNamespace(execute=AsyncMock())
    tariff = SimpleNamespace(id=77, entitlement_mode='access_point_managed')

    with pytest.raises(EntitlementResolutionError, match='Device-First immutable checkout quote'):
        await resolve_tariff_entitlement(db, tariff)

    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_access_point_daily_tariff_is_fenced_before_any_policy_or_money_work() -> None:
    db = SimpleNamespace(execute=AsyncMock())
    tariff = SimpleNamespace(id=77, entitlement_mode='access_point_managed', is_daily=True)

    with pytest.raises(EntitlementResolutionError, match='cannot use daily billing'):
        await resolve_tariff_entitlement(db, tariff, access_point_quote_context=True)

    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_effective_reader_ignores_a_future_access_point_term_until_its_boundary() -> None:
    starts_at = datetime.now(UTC) + timedelta(days=5)
    ends_at = starts_at + timedelta(days=30)
    term = SimpleNamespace(
        subscription_id=12,
        term_version=2,
        tariff_id=7,
        starts_at=starts_at,
        ends_at=ends_at,
        access_point_ids=['point-new'],
        technical_squad_keys=['squad-new'],
        policy_revision=9,
        inventory_fingerprint='fp-new',
        source_reference='checkout:future',
        provenance='device_first_checkout',
    )
    term.grant_hash = sha256(
        json.dumps(
            {
                'subscription_id': term.subscription_id,
                'term_version': term.term_version,
                'tariff_id': term.tariff_id,
                'starts_at': term.starts_at.isoformat(),
                'ends_at': term.ends_at.isoformat(),
                'access_point_ids': term.access_point_ids,
                'technical_squad_keys': term.technical_squad_keys,
                'policy_revision': term.policy_revision,
                'inventory_fingerprint': term.inventory_fingerprint,
                'source_reference': term.source_reference,
                'provenance': term.provenance,
            },
            sort_keys=True,
            separators=(',', ':'),
        ).encode()
    ).hexdigest()
    baseline = ResolvedEntitlement(('point-old',), ('squad-old',), 8, 'access_point_policy', 'fp-old')
    snapshot = SimpleNamespace(
        location_ids=list(baseline.location_ids),
        technical_squad_uuids=list(baseline.squad_uuids),
        policy_revision=baseline.policy_revision,
        provenance=baseline.provenance,
        inventory_fingerprint=baseline.inventory_fingerprint,
        snapshot_hash=baseline.snapshot_hash,
    )
    # Before boundary: no effective term then 0100 baseline. At boundary: the
    # exact captured future grant replaces it without consulting tariff policy.
    db = SimpleNamespace(
        get=AsyncMock(return_value=SimpleNamespace(tariff_id=None)),
        scalar=AsyncMock(side_effect=[None, snapshot, term]),
    )

    before_boundary = await get_effective_subscription_resolved_entitlement(
        db,
        12,
        at=starts_at - timedelta(seconds=1),
        # Explicit migration-only compatibility read.  Runtime paid paths
        # cannot use an AP one-row baseline outside a captured active term.
        allow_access_point_baseline=True,
    )

    effective = await get_effective_subscription_resolved_entitlement(db, 12, at=starts_at + timedelta(seconds=1))

    assert before_boundary.location_ids == ('point-old',)
    assert effective.location_ids == ('point-new',)
    assert effective.squad_uuids == ('squad-new',)


@pytest.mark.asyncio
async def test_access_point_baseline_is_not_a_runtime_fallback_outside_a_paid_term() -> None:
    baseline = ResolvedEntitlement(('point-old',), ('squad-old',), 8, 'access_point_policy', 'fp-old')
    snapshot = SimpleNamespace(
        location_ids=list(baseline.location_ids),
        technical_squad_uuids=list(baseline.squad_uuids),
        policy_revision=baseline.policy_revision,
        provenance=baseline.provenance,
        inventory_fingerprint=baseline.inventory_fingerprint,
        snapshot_hash=baseline.snapshot_hash,
    )
    db = SimpleNamespace(
        get=AsyncMock(return_value=SimpleNamespace(tariff_id=None)),
        scalar=AsyncMock(side_effect=[None, snapshot]),
    )

    with pytest.raises(EntitlementResolutionError, match='direct immutable checkout quote'):
        await get_effective_subscription_resolved_entitlement(db, 12)


@pytest.mark.asyncio
async def test_legacy_without_owner_manifest_is_fail_closed():
    db = SimpleNamespace(get=AsyncMock(return_value=None))
    tariff = SimpleNamespace(
        id=1, entitlement_mode='legacy_snapshot', allowed_squads=['raw'], location_policy_revision=1
    )

    with pytest.raises(EntitlementResolutionError, match='owner-approved'):
        await resolve_tariff_entitlement(db, tariff)


@pytest.mark.asyncio
async def test_raw_country_post_returns_gone_before_database_or_panel_effect():
    db = SimpleNamespace(commit=AsyncMock(), execute=AsyncMock())
    with pytest.raises(HTTPException) as error:
        await update_countries({'countries': ['raw-panel-uuid']}, SimpleNamespace(), db, None)

    assert error.value.status_code == 410
    db.commit.assert_not_awaited()
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_subscription_locations_use_nontechnical_presentation_manifest(monkeypatch):
    subscription = SimpleNamespace(id=22)
    snapshot = SimpleNamespace(subscription_id=22, tariff_id=3, provenance='legacy_subscription_backfill')
    manifest = SimpleNamespace(
        presentation_locations=[
            {
                'id': 'legacy-de',
                'iso_code': 'DE',
                'label_ru': 'Германия',
                'label_en': 'Germany',
                'flag': '🇩🇪',
                'lifecycle': 'published',
            }
        ]
    )
    monkeypatch.setattr(
        'app.cabinet.routes.subscription_modules.servers.resolve_subscription',
        AsyncMock(return_value=subscription),
    )
    db = SimpleNamespace(scalar=AsyncMock(return_value=snapshot), get=AsyncMock(return_value=manifest))

    result = await get_available_countries(SimpleNamespace(), db, None)

    assert result['legacy_presentation'] is True
    assert result['locations'][0]['iso_code'] == 'DE'
    assert 'technical_squad_uuids' not in result['locations'][0]
    db.get.assert_awaited_once()


@pytest.mark.asyncio
async def test_legacy_webapi_subscription_writers_are_gone_before_database_effect():
    db = SimpleNamespace(commit=AsyncMock(), execute=AsyncMock())
    with pytest.raises(HTTPException) as create_error:
        await create_subscription(SimpleNamespace(), None, db)
    with pytest.raises(HTTPException) as user_error:
        await create_user_subscription(1, SimpleNamespace(), None, db)

    assert create_error.value.status_code == 410
    assert user_error.value.status_code == 410
    db.commit.assert_not_awaited()
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_raw_miniapp_server_mutation_is_gone_before_database_effect():
    db = SimpleNamespace(commit=AsyncMock(), execute=AsyncMock())
    with pytest.raises(HTTPException) as error:
        await update_subscription_servers_endpoint(SimpleNamespace(), db)

    assert error.value.status_code == 410
    db.commit.assert_not_awaited()
    db.execute.assert_not_awaited()


def test_device_first_sale_snapshot_captures_immutable_entitlement_not_tariff_uuids():
    checkout = SimpleNamespace(
        tariff_id=3,
        period_days=30,
        selected_device_limit=2,
        tariff_total_kopeks=90000,
        price_breakdown={},
        pricing_revision=7,
        target_snapshot=None,
        quote_expires_at=SimpleNamespace(isoformat=lambda: '2026-08-08T00:00:00+00:00'),
    )
    tariff = SimpleNamespace(name='Premium', traffic_limit_gb=100, allowed_squads=['must-not-leak'])
    entitlement = ResolvedEntitlement(('pl',), ('customer-pl',), 4, 'tariff')

    snapshot = _direct_sale_snapshot(checkout, tariff, funding_mode='platega', entitlement=entitlement)

    assert snapshot['entitlement']['location_ids'] == ['pl']
    assert snapshot['entitlement_hash'] == entitlement.snapshot_hash
    assert 'allowed_squads' not in snapshot


def test_device_first_access_point_snapshot_round_trips_the_inventory_fingerprint() -> None:
    checkout = SimpleNamespace(
        tariff_id=3,
        period_days=30,
        selected_device_limit=2,
        tariff_total_kopeks=90000,
        price_breakdown={},
        pricing_revision=7,
        target_snapshot=None,
        quote_expires_at=SimpleNamespace(isoformat=lambda: '2026-08-08T00:00:00+00:00'),
    )
    tariff = SimpleNamespace(name='Premium', traffic_limit_gb=100, allowed_squads=['must-not-leak'])
    entitlement = ResolvedEntitlement(
        ('public-point-pl',),
        ('internal-only-pl',),
        4,
        'access_point_policy',
        'point-policy-fingerprint',
    )

    snapshot = _direct_sale_snapshot(checkout, tariff, funding_mode='wallet', entitlement=entitlement)
    raw = snapshot['entitlement']
    rebuilt = ResolvedEntitlement(
        tuple(raw['location_ids']),
        tuple(raw['technical_squad_uuids']),
        int(raw['policy_revision']),
        str(raw['provenance']),
        str(raw['inventory_fingerprint']),
    )

    assert rebuilt.snapshot_hash == snapshot['entitlement_hash']
    assert raw['inventory_fingerprint'] == 'point-policy-fingerprint'


@pytest.mark.asyncio
async def test_replace_subscription_resolves_target_tariff_before_mutation(monkeypatch):
    resolved = ResolvedEntitlement(('pl',), ('customer-pl',), 5, 'tariff')
    resolver = AsyncMock(return_value=resolved)
    persisted = AsyncMock()
    monkeypatch.setattr('app.services.public_location_entitlement_service.resolve_tariff_entitlement', resolver)
    monkeypatch.setattr(
        'app.services.public_location_entitlement_service.persist_subscription_entitlement_snapshot', persisted
    )
    monkeypatch.setattr(subscription_crud, '_lock_subscription_row', AsyncMock())
    monkeypatch.setattr(subscription_crud, 'clear_notifications', AsyncMock())

    subscription = SimpleNamespace(
        id=22,
        user_id=9,
        tariff_id=1,
        connected_squads=['old-squad'],
        autopay_enabled=False,
        autopay_days_before=3,
        status=SubscriptionStatus.EXPIRED.value,
        is_trial=False,
        start_date=None,
        end_date=None,
        traffic_limit_gb=0,
        traffic_used_gb=0,
        purchased_traffic_gb=0,
        traffic_reset_at=None,
        device_limit=1,
        subscription_url='old',
        subscription_crypto_link='old',
        remnawave_short_uuid='old',
        in_grace=False,
        grace_until=None,
        grace_eligible_period_days=None,
        updated_at=None,
    )
    target_tariff = SimpleNamespace(id=2)
    db = SimpleNamespace(
        get=AsyncMock(return_value=target_tariff),
        execute=AsyncMock(),
        flush=AsyncMock(),
    )

    await subscription_crud.replace_subscription(
        db,
        subscription,
        duration_days=30,
        traffic_limit_gb=100,
        device_limit=2,
        connected_squads=['customer-pl'],
        is_trial=False,
        tariff_id=2,
        commit=False,
    )

    resolver.assert_awaited_once_with(db, target_tariff)
    persisted.assert_awaited_once_with(db, subscription.id, 2, resolved)
    assert subscription.tariff_id == 2
    assert subscription.connected_squads == ['customer-pl']


@pytest.mark.asyncio
async def test_daily_charge_stops_before_balance_when_snapshot_is_missing(monkeypatch):
    monkeypatch.setattr(
        'app.services.public_location_entitlement_service.get_subscription_entitlement_squads',
        AsyncMock(side_effect=EntitlementResolutionError('missing snapshot')),
    )
    lock_user = AsyncMock()
    monkeypatch.setattr('app.database.crud.user.lock_user_for_pricing', lock_user)
    deducted = AsyncMock()
    monkeypatch.setattr(daily_subscription_service, 'subtract_user_balance', deducted)

    subscription = SimpleNamespace(
        id=31,
        user_id=7,
        user=SimpleNamespace(id=7, balance_kopeks=10_000),
        tariff=SimpleNamespace(id=4, name='Daily', daily_price_kopeks=100),
        connected_squads=[],
    )
    result = await daily_subscription_service.DailySubscriptionService()._process_single_charge(
        SimpleNamespace(), subscription
    )

    assert result == 'error'
    lock_user.assert_not_awaited()
    deducted.assert_not_awaited()


@pytest.mark.asyncio
async def test_daily_charge_validates_nonempty_projection_against_snapshot_before_balance(monkeypatch):
    monkeypatch.setattr(
        'app.services.public_location_entitlement_service.get_subscription_entitlement_squads',
        AsyncMock(side_effect=EntitlementResolutionError('missing snapshot')),
    )
    deducted = AsyncMock()
    monkeypatch.setattr(daily_subscription_service, 'subtract_user_balance', deducted)

    subscription = SimpleNamespace(
        id=32,
        user_id=7,
        user=SimpleNamespace(id=7, balance_kopeks=10_000),
        tariff=SimpleNamespace(id=4, name='Daily', daily_price_kopeks=100),
        connected_squads=['legacy-raw-squad'],
    )
    result = await daily_subscription_service.DailySubscriptionService()._process_single_charge(
        SimpleNamespace(), subscription
    )

    assert result == 'error'
    deducted.assert_not_awaited()


@pytest.mark.asyncio
async def test_panel_import_and_raw_squad_executors_are_inert_in_tariff_mode(monkeypatch):
    monkeypatch.setattr(type(remnawave_service.settings), 'is_tariffs_mode', lambda _self: True)
    service = object.__new__(remnawave_service.RemnaWaveService)
    db = SimpleNamespace(execute=AsyncMock(), commit=AsyncMock())

    sync_result = await service.sync_users_from_panel(db)
    migration_result = await service.migrate_squad_users(db, 'source', 'target')
    assert await service.add_all_users_to_squad('raw-squad') is False
    assert await service.remove_all_users_from_squad('raw-squad') is False
    assert await service.delete_squad('raw-squad') is False

    assert sync_result == {'created': 0, 'updated': 0, 'errors': 0, 'deleted': 0}
    assert migration_result['error'] == 'controlled_plan_required'
    db.execute.assert_not_awaited()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_classic_checkout_is_fenced_before_fsm_pricing_or_money(monkeypatch):
    # The stale callback must be fenced in the live tariff mode too.
    monkeypatch.setattr(classic_purchase.settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(classic_purchase, 'get_back_keyboard', lambda _language: SimpleNamespace())
    message = SimpleNamespace(edit_text=AsyncMock())
    callback = SimpleNamespace(message=message, answer=AsyncMock())
    state = SimpleNamespace(clear=AsyncMock(), get_data=AsyncMock())

    await classic_purchase.confirm_purchase(callback, state, SimpleNamespace(language='ru'), SimpleNamespace())

    state.clear.assert_awaited_once()
    state.get_data.assert_not_awaited()
    callback.answer.assert_awaited_once()


def test_public_location_router_registers_no_production_executor():
    route_paths = [route.path for route in public_location_admin_router.routes]

    assert any(path.endswith('/prepare-plan') for path in route_paths)
    assert any(path.endswith('/confirm') for path in route_paths)
    assert not any('execute' in path or 'worker' in path for path in route_paths)


@pytest.mark.asyncio
async def test_panel_email_import_is_retired_before_database_or_entitlement_effect():
    db = SimpleNamespace(execute=AsyncMock(), commit=AsyncMock())

    await cabinet_auth._sync_subscription_from_panel_by_email(db, SimpleNamespace(id=7, email='user@example.test'))

    db.execute.assert_not_awaited()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_central_renewal_stops_before_balance_when_snapshot_is_missing(monkeypatch):
    monkeypatch.setattr(
        'app.services.public_location_entitlement_service.get_subscription_resolved_entitlement',
        AsyncMock(side_effect=EntitlementResolutionError('missing snapshot')),
    )
    deducted = AsyncMock()
    monkeypatch.setattr(subscription_renewal_service, 'subtract_user_balance', deducted)

    pricing = SimpleNamespace(final_total=1_000, period_days=30, promo_offer_discount=0)
    subscription = SimpleNamespace(id=81, tariff_id=4)
    db = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(entitlement_mode='legacy_snapshot')))

    with pytest.raises(subscription_renewal_service.SubscriptionRenewalEntitlementError):
        await subscription_renewal_service.SubscriptionRenewalService().finalize(
            db, SimpleNamespace(), subscription, pricing
        )

    deducted.assert_not_awaited()


@pytest.mark.asyncio
async def test_expired_topup_autorenewal_stops_before_balance_when_snapshot_is_missing(monkeypatch):
    now = datetime.now(UTC)
    subscription = SimpleNamespace(
        id=82,
        user_id=7,
        tariff_id=4,
        status=SubscriptionStatus.EXPIRED.value,
        is_trial=False,
        autopay_enabled=True,
        end_date=now - timedelta(days=1),
        tariff=SimpleNamespace(id=4, name='Premium', is_active=True, get_shortest_period=lambda: 30),
    )
    user = SimpleNamespace(id=7, balance_kopeks=10_000, has_had_paid_subscription=True)
    pricing = SimpleNamespace(
        final_total=1_000,
        original_total=1_000,
        is_tariff_mode=True,
        breakdown={},
    )
    monkeypatch.setattr(
        'app.database.crud.subscription.get_subscription_by_user_id', AsyncMock(return_value=subscription)
    )
    monkeypatch.setattr('app.database.crud.user.lock_user_for_pricing', AsyncMock(return_value=user))
    monkeypatch.setattr(
        subscription_auto_purchase_service.pricing_engine,
        'calculate_renewal_price',
        AsyncMock(return_value=pricing),
    )
    monkeypatch.setattr('app.utils.grace.is_in_grace', lambda _subscription: False)
    monkeypatch.setattr(
        'app.services.public_location_entitlement_service.get_subscription_resolved_entitlement',
        AsyncMock(side_effect=EntitlementResolutionError('missing snapshot')),
    )
    deducted = AsyncMock()
    monkeypatch.setattr(subscription_auto_purchase_service, 'subtract_user_balance', deducted)

    result = await subscription_auto_purchase_service.try_auto_extend_expired_after_topup(SimpleNamespace(), user)

    assert result is False
    deducted.assert_not_awaited()


@pytest.mark.asyncio
async def test_cabinet_tariff_switch_is_fenced_before_subscription_or_money():
    with pytest.raises(HTTPException) as error:
        await cabinet_tariff_switch(SimpleNamespace(), SimpleNamespace(), SimpleNamespace())

    assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_saved_daily_cart_is_fenced_before_database_or_money():
    db = SimpleNamespace(execute=AsyncMock(), commit=AsyncMock())

    result = await subscription_auto_purchase_service._auto_purchase_daily_tariff(
        db, SimpleNamespace(id=7), {'tariff_id': 4}
    )

    assert result is False
    db.execute.assert_not_awaited()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_unversioned_legacy_cart_is_fenced_before_database_or_money():
    db = SimpleNamespace(execute=AsyncMock(), commit=AsyncMock())

    result = await subscription_auto_purchase_service._process_legacy_generic_cart(
        db, SimpleNamespace(id=7), {'countries': ['raw-squad'], 'period_days': 30}
    )

    assert result is False
    db.execute.assert_not_awaited()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_telegram_admin_legacy_callbacks_remain_fenced_before_database_effect():
    callback = SimpleNamespace(answer=AsyncMock())
    db = SimpleNamespace(commit=AsyncMock(), execute=AsyncMock())
    db_user = SimpleNamespace()

    # Unwrap auth/error decorators to exercise the first line of each handler.
    raw_user_toggle = telegram_admin_users.toggle_user_server.__wrapped__.__wrapped__
    raw_tariff_confirm = telegram_admin_users.confirm_admin_tariff_change.__wrapped__.__wrapped__
    legacy_buy = telegram_admin_users.admin_buy_subscription.__wrapped__.__wrapped__
    legacy_buy_confirm = telegram_admin_users.admin_buy_subscription_confirm.__wrapped__.__wrapped__
    legacy_buy_execute = telegram_admin_users.admin_buy_subscription_execute.__wrapped__.__wrapped__
    tariff_buy = telegram_admin_users.admin_buy_tariff.__wrapped__.__wrapped__
    tariff_buy_confirm = telegram_admin_users.admin_buy_tariff_confirm.__wrapped__.__wrapped__
    tariff_buy_execute = telegram_admin_users.admin_buy_tariff_execute.__wrapped__.__wrapped__

    await raw_user_toggle(callback, db_user, db)
    await raw_tariff_confirm(callback, db_user, db)
    await legacy_buy(callback, db_user, db)
    await legacy_buy_confirm(callback, db_user, db)
    await legacy_buy_execute(callback, db_user, db)
    await tariff_buy(callback, db_user, db)
    await tariff_buy_confirm(callback, db_user, db)
    await tariff_buy_execute(callback, db_user, db)

    assert callback.answer.await_count == 8
    db.commit.assert_not_awaited()
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_telegram_admin_tariff_picker_displays_native_internal_squad_checkboxes(monkeypatch):
    callback = SimpleNamespace(
        data='admin_tariff_edit_squads:3',
        message=SimpleNamespace(edit_text=AsyncMock()),
        answer=AsyncMock(),
    )
    tariff = SimpleNamespace(id=3, name='Basic', allowed_squads=['de-squad'])
    monkeypatch.setattr(telegram_admin_tariffs, 'get_tariff_by_id', AsyncMock(return_value=tariff))
    monkeypatch.setattr(
        telegram_admin_tariffs,
        'get_all_server_squads',
        AsyncMock(
            return_value=(
                [SimpleNamespace(squad_uuid='de-squad', display_name='Germany')],
                1,
            )
        ),
    )

    raw_tariff_picker = telegram_admin_tariffs.start_edit_tariff_squads.__wrapped__.__wrapped__
    await raw_tariff_picker(callback, SimpleNamespace(language='ru'), SimpleNamespace(), SimpleNamespace())

    callback.message.edit_text.assert_awaited_once()
    rendered_text = callback.message.edit_text.await_args.args[0]
    assert 'Выбрано: 1 из 1' in rendered_text
    callback.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_legacy_telegram_cart_callbacks_are_fenced_before_redis_or_fsm_replay(monkeypatch):
    monkeypatch.setattr(classic_purchase, 'get_back_keyboard', lambda _language: SimpleNamespace())
    message = SimpleNamespace(edit_text=AsyncMock())
    callback = SimpleNamespace(message=message, answer=AsyncMock())
    state = SimpleNamespace(clear=AsyncMock(), get_data=AsyncMock(), set_data=AsyncMock(), set_state=AsyncMock())
    user = SimpleNamespace(id=7, language='ru')

    await classic_purchase.return_to_saved_cart(callback, state, user, SimpleNamespace())
    await classic_purchase.resume_subscription_checkout(callback, state, user)

    assert state.clear.await_count == 2
    state.get_data.assert_not_awaited()
    state.set_data.assert_not_awaited()
    state.set_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_miniapp_tariff_switch_is_gone_before_auth_database_or_money():
    db = SimpleNamespace(execute=AsyncMock(), commit=AsyncMock())

    with pytest.raises(HTTPException) as error:
        await switch_tariff_endpoint(SimpleNamespace(init_data='stale'), db)

    assert error.value.status_code == 410
    db.execute.assert_not_awaited()
    db.commit.assert_not_awaited()
