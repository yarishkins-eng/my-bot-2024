from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Self
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.cabinet.routes import admin_access_points as access_point_routes
from app.cabinet.routes.admin_access_points import (
    AccessPointPlanConfirmationRequest,
    AccessPointPlanRequest,
    AccessPointPolicyRequest,
    router as access_point_router,
)
from app.cabinet.routes.subscription_modules import purchase as cabinet_purchase_routes
from app.config import settings
from app.services import public_access_point_service
from app.services.public_access_point_inventory import InventoryHost, InventorySnapshot, InventorySquad
from app.services.public_access_point_service import (
    AccessPointPolicyError,
    VerifiedLegacyAccessPointEquivalence,
    apply_catalog_assessment,
    capture_access_point_entitlement_term,
    get_effective_public_access_points,
    get_tariff_public_access_points,
    is_term_owned_access_point_subscription,
    process_due_access_point_term_projections,
    read_consistent_inventory,
    record_verified_legacy_access_point_conversion,
)
from app.services.public_location_entitlement_service import EntitlementResolutionError, ResolvedEntitlement
from app.webapi.routes import miniapp as miniapp_routes


class _Savepoint:
    def __init__(self) -> None:
        self.rolled_back = False

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, exc_type, _exc, _traceback) -> bool:
        self.rolled_back = exc_type is not None
        return False


class _FixtureInventoryClient:
    def __init__(self, snapshots: list[InventorySnapshot]):
        self._snapshots = snapshots

    async def read_access_point_inventory(self) -> InventorySnapshot:
        return self._snapshots.pop(0)


def _snapshot(revision: str, *, title: str = 'Польша') -> InventorySnapshot:
    return InventorySnapshot(
        revision=revision,
        hosts=(InventoryHost(host_key='host-pl', title=title, squad_keys=('customer-pl',)),),
        squads=(InventorySquad(key='customer-pl', is_dedicated=True, is_healthy=True, raw_members=('raw-pl',)),),
    )


@pytest.mark.asyncio
async def test_discovery_double_read_rejects_inventory_drift() -> None:
    client = _FixtureInventoryClient([_snapshot('r1'), _snapshot('r2')])

    with pytest.raises(AccessPointPolicyError, match='inventory'):
        await read_consistent_inventory(client)


@pytest.mark.asyncio
async def test_discovery_double_read_accepts_same_redacted_graph() -> None:
    assessment = await read_consistent_inventory(_FixtureInventoryClient([_snapshot('r1'), _snapshot('r1')]))

    assert assessment.points[0].title == 'Польша'
    assert assessment.points[0].assignable is True


def test_policy_fingerprint_includes_monotonic_entitlement_epoch_but_not_title() -> None:
    point = SimpleNamespace(id='point-pl', title='Польша', inventory_fingerprint='graph-a', entitlement_revision=4)
    before = public_access_point_service._policy_inventory_fingerprint([point])

    point.title = 'Польша 2'
    assert public_access_point_service._policy_inventory_fingerprint([point]) == before

    # Returning to the same graph after an unhealthy/drifted interval must
    # still invalidate old unpaid quotes and old policy revisions.
    point.entitlement_revision = 6
    assert public_access_point_service._policy_inventory_fingerprint([point]) != before


def test_access_point_policy_rejects_raw_implementation_fields() -> None:
    with pytest.raises(ValidationError):
        AccessPointPolicyRequest.model_validate(
            {
                'access_point_ids': ['opaque-point-id'],
                'expected_revision': 0,
                'reason': 'approved policy',
                'internal_squad_key': 'forged-raw-key',
            }
        )


def test_access_point_admin_router_exposes_no_executor_route() -> None:
    paths = [route.path for route in access_point_router.routes]

    assert any(path.endswith('/access-points/discovery') for path in paths)
    assert any(path.endswith('/access-points/catalog') for path in paths)
    assert not any('execute' in path or 'worker' in path for path in paths)


def test_access_point_discovery_requires_live_owner_arm_even_if_client_is_injected(monkeypatch) -> None:
    monkeypatch.setattr(settings, 'ACCESS_POINT_INVENTORY_DRY_RUN_ENABLED', False)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(access_point_inventory_client=object())))

    with pytest.raises(HTTPException) as exc_info:
        access_point_routes._get_read_only_inventory_client(request)

    assert exc_info.value.status_code == 503


def test_catalog_apply_arm_refuses_an_expired_or_disabled_window(monkeypatch) -> None:
    monkeypatch.setattr(settings, 'ACCESS_POINT_INVENTORY_CATALOG_APPLY_ENABLED', False)

    with pytest.raises(HTTPException) as exc_info:
        access_point_routes._require_catalog_apply_arm()

    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_plan_confirmation_uses_postgres_subscription_dml_fence() -> None:
    db = SimpleNamespace(
        get_bind=Mock(return_value=SimpleNamespace(dialect=SimpleNamespace(name='postgresql'))),
        execute=AsyncMock(),
    )

    await access_point_routes._lock_subscription_table_for_plan_confirmation(db)

    statement = db.execute.await_args.args[0]
    assert str(statement) == 'LOCK TABLE subscriptions IN SHARE ROW EXCLUSIVE MODE'


@pytest.mark.asyncio
async def test_access_point_plan_is_durable_redacted_and_has_no_executor(monkeypatch) -> None:
    tariff = SimpleNamespace(
        id=9,
        entitlement_mode='access_point_managed',
        access_point_policy_revision=3,
        updated_at=None,
    )
    point = SimpleNamespace(id='point-pl', title='Польша')
    resolved = SimpleNamespace(
        provenance='access_point_policy',
        location_ids=('point-pl',),
        squad_uuids=('private-squad-key',),
        inventory_fingerprint='inventory-v3',
    )
    db = SimpleNamespace(
        get=AsyncMock(return_value=tariff),
        scalar=AsyncMock(side_effect=[tariff, None]),
        add=Mock(),
        commit=AsyncMock(),
    )
    monkeypatch.setattr(access_point_routes, '_plan_subscriptions', AsyncMock(return_value=([], [], {})))
    monkeypatch.setattr(access_point_routes, '_plan_preimage_subscriptions', Mock(return_value=([], {})))
    monkeypatch.setattr(
        access_point_routes,
        '_access_point_plan_target',
        AsyncMock(return_value=(resolved, {'point-pl': point})),
    )
    monkeypatch.setattr(access_point_routes.AuditLogCRUD, 'create', AsyncMock())

    preview = await access_point_routes.prepare_access_point_plan(
        9,
        AccessPointPlanRequest(reason='Owner reviewed current access'),
        SimpleNamespace(id=4),
        db,
    )

    assert preview['target_access_points'] == [{'id': 'point-pl', 'title': 'Польша'}]
    assert 'private-squad-key' not in str(preview)
    plan = db.add.call_args.args[0]
    assert plan.scope['execution_enabled'] is False
    assert plan.preimage['tariff']['target_technical_squad_keys'] == ['private-squad-key']
    assert plan.state == 'previewed'
    db.commit.assert_awaited_once()

    replay_db = SimpleNamespace(
        get=AsyncMock(return_value=tariff),
        scalar=AsyncMock(side_effect=[tariff, plan]),
        add=Mock(),
        commit=AsyncMock(),
    )
    replay = await access_point_routes.prepare_access_point_plan(
        9,
        AccessPointPlanRequest(reason='Owner reviewed current access'),
        SimpleNamespace(id=4),
        replay_db,
    )

    assert replay['plan_id'] == plan.id
    assert replay['state'] == 'previewed'
    replay_db.add.assert_not_called()
    replay_db.commit.assert_awaited_once()

    confirm_db = SimpleNamespace(
        scalar=AsyncMock(side_effect=[plan, tariff, None]),
        add=Mock(),
        commit=AsyncMock(),
    )
    confirmation = await access_point_routes.confirm_access_point_plan(
        9,
        plan.id,
        AccessPointPlanConfirmationRequest(
            plan_hash=plan.plan_hash,
            reason='Second privileged confirmation',
        ),
        SimpleNamespace(id=5),
        confirm_db,
    )

    assert confirmation['state'] == 'confirmed_execution_disabled'
    assert confirmation['execution_enabled'] is False
    assert 'private-squad-key' not in str(confirmation)
    confirm_db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_access_point_plan_confirmation_rejects_changed_subscription_or_term_preimage(monkeypatch) -> None:
    scope = {
        'kind': 'access_point_active_user_preview',
        'tariff_id': 9,
        'policy_revision': 3,
        'inventory_fingerprint': 'inventory-v3',
        'target_access_point_ids': ['point-pl'],
    }
    preimage = {'subscriptions': [{'subscription_id': 17, 'status': 'active'}], 'panel_identity_scope': []}
    plan = SimpleNamespace(
        id='plan-9',
        state='previewed',
        actor_user_id=4,
        reason='Owner reviewed current access',
        scope=scope,
        preimage=preimage,
        policy_revision=3,
    )
    plan.plan_hash = access_point_routes._immutable_hash(
        {
            'scope': plan.scope,
            'preimage': plan.preimage,
            'actor_user_id': plan.actor_user_id,
            'reason': plan.reason,
        }
    )
    db = SimpleNamespace(scalar=AsyncMock(side_effect=[plan, SimpleNamespace(id=9)]))
    monkeypatch.setattr(access_point_routes, '_plan_subscriptions', AsyncMock(return_value=([], [], {})))
    monkeypatch.setattr(
        access_point_routes,
        '_plan_preimage_subscriptions',
        Mock(return_value=([{'subscription_id': 17, 'status': 'expired'}], {})),
    )

    with pytest.raises(HTTPException, match='Subscription scope or active entitlement evidence changed'):
        await access_point_routes.confirm_access_point_plan(
            9,
            plan.id,
            AccessPointPlanConfirmationRequest(plan_hash=plan.plan_hash, reason='Second privileged confirmation'),
            SimpleNamespace(id=5),
            db,
        )


@pytest.mark.asyncio
async def test_legacy_conversion_requires_injected_readback_evidence_and_prepopulates_policy(monkeypatch) -> None:
    tariff = SimpleNamespace(
        id=11,
        entitlement_mode='legacy_snapshot',
        is_daily=False,
        allowed_squads=['legacy-private-squad'],
        access_point_policy_revision=0,
    )
    manifest = SimpleNamespace(manifest_hash='legacy-manifest-hash', squad_uuids=['legacy-private-squad'])
    point = SimpleNamespace(
        id='point-pl',
        state='verified',
        tariff_assignable=True,
        inventory_fingerprint='point-fingerprint',
    )
    db = SimpleNamespace(
        scalar=AsyncMock(side_effect=[tariff, manifest, None]),
        execute=AsyncMock(return_value=SimpleNamespace(scalars=lambda: [point])),
        add=Mock(),
        flush=AsyncMock(),
        begin_nested=Mock(return_value=_Savepoint()),
    )

    class _Verifier:
        async def verify_dedicated_equivalence(self, *, tariff, manifest):
            assert tariff.id == 11
            assert manifest.manifest_hash == 'legacy-manifest-hash'
            return VerifiedLegacyAccessPointEquivalence(
                legacy_manifest_hash='legacy-manifest-hash',
                access_point_ids=('point-pl',),
                prepared_operation_reference='protected-release-123',
                readback_evidence_hash='a' * 64,
            )

    policy = SimpleNamespace(revision=1, selection_hash='selection-hash', inventory_fingerprint='policy-hash')
    replace = AsyncMock(return_value=policy)
    monkeypatch.setattr(public_access_point_service, 'replace_tariff_access_point_policy', replace)
    monkeypatch.setattr(
        public_access_point_service,
        'resolve_tariff_entitlement',
        AsyncMock(
            return_value=SimpleNamespace(
                provenance='access_point_policy',
                location_ids=('point-pl',),
                inventory_fingerprint='policy-hash',
            )
        ),
    )

    conversion = await record_verified_legacy_access_point_conversion(
        db,
        tariff_id=11,
        actor_user_id=7,
        reason='Verified dedicated-equivalence preparation',
        verifier=_Verifier(),
    )

    replace.assert_awaited_once_with(
        db,
        tariff,
        access_point_ids=['point-pl'],
        expected_revision=0,
        actor_user_id=7,
        reason='Verified dedicated-equivalence preparation',
    )
    assert conversion.policy_revision == 1
    assert conversion.prepared_operation_reference == 'protected-release-123'
    assert conversion.readback_evidence_hash == 'a' * 64
    assert conversion.conversion_hash


@pytest.mark.asyncio
async def test_failed_legacy_conversion_rolls_back_its_savepoint_before_outer_commit(monkeypatch) -> None:
    tariff = SimpleNamespace(
        id=11,
        entitlement_mode='legacy_snapshot',
        is_daily=False,
        allowed_squads=['legacy-private-squad'],
        access_point_policy_revision=0,
    )
    manifest = SimpleNamespace(manifest_hash='legacy-manifest-hash', squad_uuids=['legacy-private-squad'])
    point = SimpleNamespace(
        id='point-pl',
        state='verified',
        tariff_assignable=True,
        inventory_fingerprint='point-fingerprint',
    )
    savepoint = _Savepoint()
    db = SimpleNamespace(
        scalar=AsyncMock(side_effect=[tariff, manifest, None]),
        execute=AsyncMock(return_value=SimpleNamespace(scalars=lambda: [point])),
        add=Mock(),
        flush=AsyncMock(),
        begin_nested=Mock(return_value=savepoint),
        commit=AsyncMock(),
    )

    class _Verifier:
        async def verify_dedicated_equivalence(self, *, tariff, manifest):
            return VerifiedLegacyAccessPointEquivalence(
                legacy_manifest_hash='legacy-manifest-hash',
                access_point_ids=('point-pl',),
                prepared_operation_reference='protected-release-123',
                readback_evidence_hash='a' * 64,
            )

    monkeypatch.setattr(
        public_access_point_service,
        'replace_tariff_access_point_policy',
        AsyncMock(return_value=SimpleNamespace(revision=1, selection_hash='selection', inventory_fingerprint='fp')),
    )
    monkeypatch.setattr(
        public_access_point_service,
        'resolve_tariff_entitlement',
        AsyncMock(side_effect=EntitlementResolutionError('catalog drift')),
    )

    with pytest.raises(AccessPointPolicyError, match='protected conversion'):
        await record_verified_legacy_access_point_conversion(
            db,
            tariff_id=11,
            actor_user_id=7,
            reason='Verified dedicated-equivalence preparation',
            verifier=_Verifier(),
        )

    # An outer workflow may now safely catch the response error and commit
    # unrelated work; the temporary conversion/policy savepoint is gone.
    assert savepoint.rolled_back is True
    await db.commit()
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_access_point_tariff_card_never_serializes_retained_legacy_squad_keys(monkeypatch) -> None:
    public_points = (public_access_point_service.EffectivePublicAccessPoint(id='point-pl', title='Польша'),)
    tariff = SimpleNamespace(
        id=9,
        name='Тариф с точками',
        description=None,
        tier_level=1,
        traffic_limit_gb=100,
        device_limit=1,
        device_price_kopeks=0,
        period_prices={},
        allowed_squads=['private-conversion-squad'],
        is_active=True,
        is_daily=False,
        daily_price_kopeks=0,
        custom_days_enabled=False,
        price_per_day_kopeks=0,
        min_days=1,
        max_days=30,
        custom_traffic_enabled=False,
        traffic_price_per_gb_kopeks=0,
        min_traffic_gb=1,
        max_traffic_gb=100,
        traffic_topup_enabled=False,
        max_topup_traffic_gb=0,
        traffic_reset_mode=None,
    )
    cabinet_lookup = AsyncMock(return_value=public_points)
    miniapp_lookup = AsyncMock(return_value=public_points)
    monkeypatch.setattr(cabinet_purchase_routes, 'get_tariff_public_access_points', cabinet_lookup)
    monkeypatch.setattr(miniapp_routes, 'get_tariff_public_access_points', miniapp_lookup)

    cabinet_card = await cabinet_purchase_routes._build_tariff_response(SimpleNamespace(), tariff)
    miniapp_card = await miniapp_routes._build_tariff_model(SimpleNamespace(), tariff)

    assert cabinet_card['servers'] == []
    assert cabinet_card['access_points'] == [{'id': 'point-pl', 'title': 'Польша'}]
    assert miniapp_card.servers == []
    assert [(point.id, point.title) for point in miniapp_card.access_points] == [('point-pl', 'Польша')]
    assert 'private-conversion-squad' not in str((cabinet_card, miniapp_card))


@pytest.mark.asyncio
async def test_tariff_public_points_fail_closed_when_current_policy_catalog_is_invalid() -> None:
    tariff = SimpleNamespace(id=9, entitlement_mode='access_point_managed', access_point_policy_revision=2)
    policy = SimpleNamespace(id=42)
    invalid_point = SimpleNamespace(
        id='point-pl',
        title='Польша',
        state='needs_reconcile',
        tariff_assignable=False,
        inventory_fingerprint=None,
    )
    db = SimpleNamespace(
        scalar=AsyncMock(return_value=policy),
        execute=AsyncMock(return_value=SimpleNamespace(all=lambda: [(SimpleNamespace(), invalid_point)])),
    )

    assert await get_tariff_public_access_points(db, tariff) == ()


@pytest.mark.asyncio
async def test_legacy_tariff_public_presentation_and_cards_never_fall_back_to_squad_uuid(monkeypatch) -> None:
    tariff = SimpleNamespace(
        id=9,
        entitlement_mode='legacy_snapshot',
        allowed_squads=['private-legacy-squad'],
        name='Исторический тариф',
        description=None,
        tier_level=1,
        traffic_limit_gb=100,
        device_limit=1,
        device_price_kopeks=0,
        period_prices={},
        is_active=True,
        is_daily=False,
        daily_price_kopeks=0,
        custom_days_enabled=False,
        price_per_day_kopeks=0,
        min_days=1,
        max_days=30,
        custom_traffic_enabled=False,
        traffic_price_per_gb_kopeks=0,
        min_traffic_gb=1,
        max_traffic_gb=100,
        traffic_topup_enabled=False,
        max_topup_traffic_gb=0,
        traffic_reset_mode=None,
    )
    manifest = SimpleNamespace(
        presentation_locations=[
            {'id': 'legacy-pl', 'label_ru': 'Польша', 'technical_squad_uuid': 'private-legacy-squad'}
        ]
    )
    public_points = await get_tariff_public_access_points(SimpleNamespace(get=AsyncMock(return_value=manifest)), tariff)

    assert [(point.id, point.title) for point in public_points or ()] == [('legacy-pl', 'Польша')]
    assert 'private-legacy-squad' not in str(public_points)

    monkeypatch.setattr(
        cabinet_purchase_routes, 'get_tariff_public_access_points', AsyncMock(return_value=public_points)
    )
    monkeypatch.setattr(miniapp_routes, 'get_tariff_public_access_points', AsyncMock(return_value=public_points))
    cabinet_card = await cabinet_purchase_routes._build_tariff_response(SimpleNamespace(), tariff)
    miniapp_card = await miniapp_routes._build_tariff_model(SimpleNamespace(), tariff)

    assert cabinet_card['servers'] == []
    assert cabinet_card['access_points'] == [{'id': 'legacy-pl', 'title': 'Польша'}]
    assert miniapp_card.servers == []
    assert [(point.id, point.title) for point in miniapp_card.access_points] == [('legacy-pl', 'Польша')]
    assert 'private-legacy-squad' not in str((cabinet_card, miniapp_card))


@pytest.mark.asyncio
async def test_miniapp_current_tariff_uses_captured_term_not_future_offer_policy(monkeypatch) -> None:
    tariff = SimpleNamespace(
        id=9,
        name='Тариф с точками',
        description=None,
        tier_level=1,
        traffic_limit_gb=100,
        device_limit=1,
        allowed_squads=['private-current-squad'],
        period_prices={'30': 10000},
        get_price_for_period=lambda _days: 10000,
        is_daily=False,
        daily_price_kopeks=0,
    )
    subscription = SimpleNamespace(id=17, tariff_id=9)
    effective_lookup = AsyncMock(
        return_value=(public_access_point_service.EffectivePublicAccessPoint(id='term-pl', title='Текущая Польша'),)
    )
    offer_lookup = AsyncMock(
        return_value=(
            public_access_point_service.EffectivePublicAccessPoint(id='future-fi', title='Будущая Финляндия'),
        )
    )
    monkeypatch.setattr(miniapp_routes, 'get_effective_public_access_points', effective_lookup)
    monkeypatch.setattr(miniapp_routes, 'get_tariff_public_access_points', offer_lookup)

    db = SimpleNamespace()
    model = await miniapp_routes._build_current_tariff_model(
        db,
        tariff,
        subscription=subscription,
    )

    assert [(point.id, point.title) for point in model.access_points] == [('term-pl', 'Текущая Польша')]
    effective_lookup.assert_awaited_once_with(db, subscription)
    offer_lookup.assert_not_awaited()


@pytest.mark.asyncio
async def test_user_presentation_uses_current_public_points_not_term_squad_keys() -> None:
    now = datetime.now(UTC)
    subscription = SimpleNamespace(id=17, tariff_id=9)
    tariff = SimpleNamespace(entitlement_mode='access_point_managed')
    term = SimpleNamespace(
        subscription_id=17,
        starts_at=now - timedelta(days=1),
        ends_at=now + timedelta(days=1),
        term_version=1,
        access_point_ids=['point-pl'],
        technical_squad_keys=['private-squad-uuid'],
    )
    point = SimpleNamespace(id='point-pl', title='Польша')
    db = SimpleNamespace(
        get=AsyncMock(return_value=tariff),
        scalar=AsyncMock(return_value=term),
        execute=AsyncMock(return_value=SimpleNamespace(scalars=lambda: [point])),
    )

    points = await get_effective_public_access_points(db, subscription, now=now)

    assert points is not None
    assert [(point.id, point.title) for point in points] == [('point-pl', 'Польша')]
    assert 'private-squad-uuid' not in {value for point in points for value in (point.id, point.title)}


@pytest.mark.asyncio
async def test_converted_tariff_keeps_legacy_snapshot_public_and_raw_writers_unfenced_until_ap_term() -> None:
    subscription = SimpleNamespace(id=17, tariff_id=9)
    tariff = SimpleNamespace(entitlement_mode='access_point_managed')
    snapshot = SimpleNamespace(subscription_id=17, tariff_id=9, provenance='legacy_subscription_backfill')
    manifest = SimpleNamespace(
        presentation_locations=[
            {
                'id': 'legacy-pl',
                'label_ru': 'Польша',
                'label_en': 'Poland',
                'technical_squad_uuid': 'must-not-leak',
            }
        ]
    )
    presentation_db = SimpleNamespace(
        get=AsyncMock(side_effect=[tariff, manifest]),
        scalar=AsyncMock(side_effect=[None, snapshot]),
    )

    points = await get_effective_public_access_points(presentation_db, subscription, now=datetime.now(UTC))

    assert points is not None
    assert [(point.id, point.title) for point in points] == [('legacy-pl', 'Польша')]
    assert 'must-not-leak' not in str(points)

    fence_db = SimpleNamespace(
        get=AsyncMock(return_value=tariff),
        scalar=AsyncMock(side_effect=[None, snapshot]),
    )
    assert await is_term_owned_access_point_subscription(fence_db, subscription) is False

    term_fence_db = SimpleNamespace(
        get=AsyncMock(return_value=tariff),
        scalar=AsyncMock(return_value=51),
    )
    assert await is_term_owned_access_point_subscription(term_fence_db, subscription) is True


@pytest.mark.asyncio
async def test_missing_host_is_reconciled_and_its_current_mapping_is_disabled() -> None:
    point = SimpleNamespace(
        id='point-pl',
        panel_host_key='host-pl',
        title='Польша',
        state='verified',
        state_reason=None,
        tariff_assignable=True,
        presentation_revision=1,
        entitlement_revision=1,
        inventory_revision='r1',
        inventory_fingerprint='old-evidence',
    )
    mapping = SimpleNamespace(
        public_access_point_id='point-pl',
        internal_squad_key='internal-only-pl',
        is_current=True,
        is_dedicated_verified=True,
    )
    result_points = SimpleNamespace(scalars=lambda: [point])
    result_mappings = SimpleNamespace(scalars=lambda: [mapping])
    db = SimpleNamespace(execute=AsyncMock(side_effect=[result_points, result_mappings]), add=Mock())
    empty_inventory = InventorySnapshot(revision='r2', hosts=(), squads=())

    result = await apply_catalog_assessment(db, assessment=empty_inventory_assessment(empty_inventory), dry_run=False)

    assert result.updated[0].state == 'needs_reconcile'
    assert point.tariff_assignable is False
    assert point.inventory_fingerprint != 'old-evidence'
    assert mapping.is_current is False
    assert mapping.is_dedicated_verified is False


def empty_inventory_assessment(snapshot: InventorySnapshot):
    from app.services.public_access_point_inventory import assess_inventory

    return assess_inventory(snapshot)


@pytest.mark.asyncio
async def test_paid_term_is_flushed_under_subscription_lock_before_money_can_commit() -> None:
    starts_at = datetime(2026, 8, 9, tzinfo=UTC)
    ends_at = starts_at + timedelta(days=30)
    added = []
    db = SimpleNamespace(
        # subscription row fence, then no prior source idempotency record
        scalar=AsyncMock(side_effect=[17, None]),
        execute=AsyncMock(return_value=SimpleNamespace(scalars=list)),
        add=added.append,
        flush=AsyncMock(),
    )
    entitlement = ResolvedEntitlement(('point-pl',), ('internal-pl',), 3, 'access_point_policy', 'fp-pl')

    term = await capture_access_point_entitlement_term(
        db,
        subscription_id=17,
        tariff=SimpleNamespace(id=9),
        starts_at=starts_at,
        ends_at=ends_at,
        source_reference='checkout:opaque-1',
        provenance='device_first_checkout',
        resolved_entitlement=entitlement,
    )

    assert term.subscription_id == 17
    assert term.term_version == 1
    assert term.access_point_ids == ['point-pl']
    assert term.technical_squad_keys == ['internal-pl']
    assert term.source_reference == 'checkout:opaque-1'
    # Both term evidence and its canonical Panel-projection instruction must
    # be durable before the enclosing payment transaction can commit.
    assert db.flush.await_count == 2
    assert any(getattr(item, 'term_id', object()) is term.id for item in added)


@pytest.mark.asyncio
async def test_duplicate_callback_returns_identical_term_without_creating_a_second_grant() -> None:
    starts_at = datetime(2026, 8, 9, tzinfo=UTC)
    ends_at = starts_at + timedelta(days=30)
    existing = SimpleNamespace(
        subscription_id=17,
        tariff_id=9,
        starts_at=starts_at,
        ends_at=ends_at,
        access_point_ids=['point-pl'],
        technical_squad_keys=['internal-pl'],
        policy_revision=3,
        inventory_fingerprint='fp-pl',
        provenance='device_first_checkout',
    )
    db = SimpleNamespace(
        scalar=AsyncMock(side_effect=[17, existing]),
        execute=AsyncMock(),
        add=Mock(),
        flush=AsyncMock(),
    )
    entitlement = ResolvedEntitlement(('point-pl',), ('internal-pl',), 3, 'access_point_policy', 'fp-pl')

    result = await capture_access_point_entitlement_term(
        db,
        subscription_id=17,
        tariff=SimpleNamespace(id=9),
        starts_at=starts_at,
        ends_at=ends_at,
        source_reference='checkout:opaque-1',
        provenance='device_first_checkout',
        resolved_entitlement=entitlement,
    )

    assert result is existing
    db.execute.assert_not_awaited()
    db.add.assert_not_called()
    db.flush.assert_not_awaited()


@pytest.mark.asyncio
async def test_early_paid_term_creates_a_delayed_projection_outbox() -> None:
    starts_at = datetime.now(UTC) + timedelta(days=5)
    ends_at = starts_at + timedelta(days=30)
    added = []
    db = SimpleNamespace(
        scalar=AsyncMock(side_effect=[17, None]),
        execute=AsyncMock(return_value=SimpleNamespace(scalars=list)),
        add=added.append,
        flush=AsyncMock(),
    )
    entitlement = ResolvedEntitlement(('point-pl',), ('internal-pl',), 3, 'access_point_policy', 'fp-pl')

    term = await capture_access_point_entitlement_term(
        db,
        subscription_id=17,
        tariff=SimpleNamespace(id=9),
        starts_at=starts_at,
        ends_at=ends_at,
        source_reference='checkout:future',
        provenance='device_first_checkout',
        resolved_entitlement=entitlement,
    )

    assert len(added) == 2
    outbox = added[1]
    assert outbox.term_id == term.id
    assert outbox.subscription_id == 17
    assert outbox.effective_at == starts_at
    assert db.flush.await_count == 2


@pytest.mark.asyncio
async def test_due_term_projects_exact_captured_squads_and_failed_panel_keeps_old_projection() -> None:
    starts_at = datetime.now(UTC) - timedelta(seconds=1)
    term = SimpleNamespace(
        id=51,
        starts_at=starts_at,
        ends_at=starts_at + timedelta(days=30),
        technical_squad_keys=['squad-new'],
    )
    outbox = SimpleNamespace(
        id=41,
        term_id=51,
        subscription_id=17,
        state='pending',
        attempts=0,
        claim_epoch=0,
        claimed_at=None,
        delivered_at=None,
        last_error=None,
    )
    subscription = SimpleNamespace(id=17, connected_squads=['squad-old'], updated_at=None)
    claim_result = SimpleNamespace(scalars=lambda: [outbox])
    db = SimpleNamespace(
        execute=AsyncMock(return_value=claim_result),
        scalar=AsyncMock(side_effect=[outbox, term, subscription]),
        flush=AsyncMock(),
        commit=AsyncMock(),
        rollback=AsyncMock(),
    )
    panel_projection = AsyncMock(return_value=True)

    projected = await process_due_access_point_term_projections(
        db,
        now=datetime.now(UTC),
        panel_projection=panel_projection,
    )

    assert projected == 1
    assert subscription.connected_squads == ['squad-new']
    assert outbox.state == 'delivered'
    panel_projection.assert_awaited_once_with(db, subscription, term)

    # The retry path rolls the local squad update back before re-arming the
    # durable outbox; a transient Panel failure cannot leak a future policy.
    failed_outbox = SimpleNamespace(**outbox.__dict__)
    failed_outbox.state = 'pending'
    failed_subscription = SimpleNamespace(id=17, connected_squads=['squad-old'], updated_at=None)
    failed_db = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(scalars=lambda: [failed_outbox])),
        scalar=AsyncMock(side_effect=[failed_outbox, term, failed_subscription, failed_outbox]),
        flush=AsyncMock(),
        commit=AsyncMock(),
        rollback=AsyncMock(),
    )
    failed_panel = AsyncMock(return_value=False)

    projected = await process_due_access_point_term_projections(
        failed_db,
        now=datetime.now(UTC),
        panel_projection=failed_panel,
    )

    assert projected == 0
    assert failed_subscription.connected_squads == ['squad-old']
    assert failed_outbox.state == 'pending'
    failed_db.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_due_term_passes_immutable_expiry_to_real_panel_projection_path() -> None:
    starts_at = datetime.now(UTC) - timedelta(seconds=1)
    ends_at = starts_at + timedelta(days=30)
    term = SimpleNamespace(
        id=52,
        starts_at=starts_at,
        ends_at=ends_at,
        technical_squad_keys=['squad-current'],
    )
    outbox = SimpleNamespace(
        id=42,
        term_id=52,
        subscription_id=18,
        state='pending',
        attempts=0,
        claim_epoch=0,
        claimed_at=None,
        delivered_at=None,
        last_error=None,
    )
    subscription = SimpleNamespace(id=18, connected_squads=['squad-old'], updated_at=None)
    db = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(scalars=lambda: [outbox])),
        scalar=AsyncMock(side_effect=[outbox, term, subscription]),
        flush=AsyncMock(),
        commit=AsyncMock(),
        rollback=AsyncMock(),
    )
    synced = AsyncMock(return_value=(True, None))

    with patch('app.services.subscription_service.SubscriptionService.ensure_subscription_synced', synced):
        projected = await process_due_access_point_term_projections(db, now=datetime.now(UTC))

    assert projected == 1
    synced.assert_awaited_once_with(
        db,
        subscription,
        force_panel_sync=True,
        commit=False,
        access_point_term_projection=True,
        access_point_term_ends_at=ends_at,
    )


@pytest.mark.asyncio
async def test_stale_projection_claim_cannot_write_to_panel() -> None:
    starts_at = datetime.now(UTC) - timedelta(seconds=1)
    term = SimpleNamespace(
        id=51,
        starts_at=starts_at,
        ends_at=starts_at + timedelta(days=30),
        technical_squad_keys=['squad-new'],
    )
    # This worker carries epoch 1, but a successor has already reclaimed the
    # job as epoch 2.  It must stop before changing local or Panel access.
    outbox = SimpleNamespace(
        id=41,
        term_id=51,
        subscription_id=17,
        state='processing',
        attempts=2,
        claim_epoch=2,
        claimed_at=datetime.now(UTC),
        delivered_at=None,
        last_error=None,
    )
    subscription = SimpleNamespace(id=17, connected_squads=['squad-old'], updated_at=None)
    db = SimpleNamespace(
        # No due rows are claimed in this invocation; supply the stale claim
        # directly to the processor's scalar reload path by replacing claim.
        execute=AsyncMock(return_value=SimpleNamespace(scalars=list)),
        scalar=AsyncMock(side_effect=[outbox, term, subscription]),
        flush=AsyncMock(),
        commit=AsyncMock(),
        rollback=AsyncMock(),
    )

    from app.services import public_access_point_service as service_module

    original_claim = service_module.claim_due_access_point_term_projections
    service_module.claim_due_access_point_term_projections = AsyncMock(
        return_value=[
            service_module.AccessPointTermProjection(
                outbox_id=41,
                term_id=51,
                subscription_id=17,
                claim_epoch=1,
            )
        ]
    )
    panel_projection = AsyncMock(return_value=True)
    try:
        projected = await process_due_access_point_term_projections(
            db,
            now=datetime.now(UTC),
            panel_projection=panel_projection,
        )
    finally:
        service_module.claim_due_access_point_term_projections = original_claim

    assert projected == 0
    assert subscription.connected_squads == ['squad-old']
    panel_projection.assert_not_awaited()
