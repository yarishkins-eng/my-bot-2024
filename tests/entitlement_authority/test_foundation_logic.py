from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from app.services.entitlement_authority.reducer import AuthoritySource, Overlay, reduce_authority
from app.services.entitlement_authority.shadow import ReadOnlyShadowEvaluator
from app.services.entitlement_authority.strict_panel import (
    RemoteOutcomeUnknown,
    StrictPanelClient,
    panel_owner_username,
)
from app.services.entitlement_authority.types import EntitlementSnapshot, compare_snapshots


NOW = datetime(2026, 8, 13, tzinfo=UTC)


def snapshot(**updates: object) -> EntitlementSnapshot:
    values = {
        'owner_key': 'owner-fingerprint',
        'panel_uuid': 'panel-one',
        'status': 'ACTIVE',
        'expire_at': NOW + timedelta(days=30),
        'traffic_limit_bytes': 0,
        'traffic_limit_strategy': 'NO_RESET',
        'hwid_device_limit': None,
        'internal_squads': (),
        'external_squad_uuid': None,
        'provenance': 'paid_sale',
        'generation': 1,
    }
    values.update(updates)
    return EntitlementSnapshot(**values)  # type: ignore[arg-type]


def test_exact_comparator_preserves_zero_empty_and_explicit_null() -> None:
    desired = snapshot()
    same = EntitlementSnapshot.from_mapping(desired.canonical())
    assert compare_snapshots(desired, same).exact
    assert compare_snapshots(desired, replace(same, traffic_limit_bytes=1)).mismatch_fields == ('traffic_limit_bytes',)
    assert compare_snapshots(desired, replace(same, internal_squads=('squad',))).mismatch_fields == ('internal_squads',)
    assert compare_snapshots(desired, replace(same, hwid_device_limit=0)).mismatch_fields == ('hwid_device_limit',)
    assert compare_snapshots(desired, replace(same, external_squad_uuid='external')).mismatch_fields == (
        'external_squad_uuid',
    )


def test_normalizer_rejects_missing_empty_null_fields_and_naive_expiry() -> None:
    value = snapshot().canonical()
    for key in (
        'internal_squads',
        'external_squad_uuid',
        'hwid_device_limit',
        'traffic_limit_bytes',
        'reset_epoch',
        'revoke_epoch',
        'deny_overlays',
    ):
        incomplete = dict(value)
        incomplete.pop(key)
        with pytest.raises(ValueError, match='missing exact'):
            EntitlementSnapshot.from_mapping(incomplete)
    with pytest.raises(ValueError, match='timezone-aware'):
        snapshot(expire_at=datetime(2026, 8, 13))


@pytest.mark.parametrize(
    ('field', 'invalid'),
    [
        ('traffic_limit_bytes', False),
        ('generation', True),
        ('reset_epoch', '0'),
        ('hwid_device_limit', '2'),
        ('internal_squads', 'squad-one'),
        ('internal_squads', [1]),
        ('deny_overlays', 'limited'),
        ('panel_uuid', 123),
        ('external_squad_uuid', False),
        ('status', ['ACTIVE']),
        ('expire_at', NOW),
    ],
)
def test_exact_json_decoder_rejects_coercion_and_non_json_types(field: str, invalid: object) -> None:
    value = snapshot().canonical()
    value[field] = invalid
    with pytest.raises(ValueError):
        EntitlementSnapshot.from_mapping(value)


def test_exact_json_decoder_rejects_unknown_keys_and_noncanonical_enums() -> None:
    extra = snapshot().canonical()
    extra['email'] = 'forbidden@example.test'
    with pytest.raises(ValueError, match='unexpected exact'):
        EntitlementSnapshot.from_mapping(extra)
    for field, invalid in (('status', 'active'), ('traffic_limit_strategy', 'month')):
        value = snapshot().canonical()
        value[field] = invalid
        with pytest.raises(ValueError, match='exact uppercase'):
            EntitlementSnapshot.from_mapping(value)


@pytest.mark.parametrize('kind', ['erasure', 'delete', 'reset', 'admin_block', 'channel_deny'])
def test_hard_deny_precedence_over_limited_grace_and_paid(kind: str) -> None:
    result = reduce_authority(
        [AuthoritySource('sale', snapshot())],
        [
            Overlay('grace', 1, 1),
            Overlay('limited', 1, 2),
            Overlay(kind, 1, 3),
        ],
        verified_generation=1,
        now=NOW,
    )
    assert result.state == kind
    assert result.snapshot is not None and result.snapshot.status == 'DISABLED'


def test_limited_survives_renewal_and_only_proven_clear_is_accepted() -> None:
    renewed = replace(snapshot(), generation=2, expire_at=NOW + timedelta(days=60))
    result = reduce_authority(
        [AuthoritySource('renewal', renewed)],
        [Overlay('limited', 1, 1)],
        verified_generation=None,
        now=NOW,
    )
    assert result.state == 'limited'
    assert result.snapshot is not None and result.snapshot.status == 'LIMITED'
    invalid = reduce_authority(
        [AuthoritySource('renewal', renewed)],
        [Overlay('limited', 1, 1, active=False, resolution_code='ordinary_retry')],
        verified_generation=None,
        now=NOW,
    )
    assert invalid.state == 'quarantined'
    cleared = reduce_authority(
        [AuthoritySource('renewal', renewed)],
        [Overlay('limited', 1, 1, active=False, resolution_code='traffic_reset')],
        verified_generation=None,
        now=NOW,
    )
    assert cleared.state == 'authorized'


def test_limited_never_expires_back_to_active_without_proven_clear() -> None:
    result = reduce_authority(
        [AuthoritySource('renewal', snapshot())],
        [Overlay('limited', 1, 1, expires_at=NOW - timedelta(seconds=1))],
        verified_generation=1,
        now=NOW,
    )
    assert result.state == 'limited'
    assert result.snapshot is not None and result.snapshot.status == 'LIMITED'


def test_reversal_before_verified_blocks_but_after_verified_is_non_access_deny() -> None:
    source = AuthoritySource('sale', snapshot())
    hold = Overlay('financial_review_hold', 1, 1)
    before = reduce_authority([source], [hold], verified_generation=None, now=NOW)
    after = reduce_authority([source], [hold], verified_generation=1, now=NOW)
    assert before.blocks_projection and before.financial_review
    assert before.state == 'financial_review_hold'
    assert not after.blocks_projection and after.financial_review
    assert after.snapshot is not None and after.snapshot.status == 'ACTIVE'


def test_provenance_conflict_quarantines_instead_of_last_write_wins() -> None:
    result = reduce_authority(
        [
            AuthoritySource('paid', snapshot()),
            AuthoritySource('admin', replace(snapshot(), status='DISABLED')),
        ],
        [],
        verified_generation=None,
        now=NOW,
    )
    assert result.state == 'quarantined'
    assert result.snapshot is None


class _StrictTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict | None]] = []
        self.error: Exception | None = None

    async def request_once(self, method: str, endpoint: str, payload: dict | None = None) -> dict:
        self.calls.append((method, endpoint, payload))
        if self.error:
            raise self.error
        return {'response': {'uuid': 'panel-created'}}


@pytest.mark.asyncio
async def test_strict_panel_create_is_disabled_exact_and_never_retries_or_degrades() -> None:
    transport = _StrictTransport()
    client = StrictPanelClient(transport)
    unbound = replace(snapshot(), panel_uuid=None)
    receipt = await client.create_disabled(unbound, panel_owner_username(unbound.owner_key))
    assert receipt.panel_uuid == 'panel-created'
    method, endpoint, payload = transport.calls[0]
    assert (method, endpoint) == ('POST', '/api/users')
    assert payload == {
        'status': 'DISABLED',
        'expireAt': '2026-09-12T00:00:00.000000Z',
        'trafficLimitBytes': 0,
        'trafficLimitStrategy': 'NO_RESET',
        'hwidDeviceLimit': None,
        'activeInternalSquads': [],
        'externalSquadUuid': None,
        'username': panel_owner_username('owner-fingerprint'),
    }
    transport.error = RuntimeError('A039 or lost response')
    with pytest.raises(RemoteOutcomeUnknown):
        await client.patch_exact(snapshot())
    assert len(transport.calls) == 2


def test_shadow_evaluator_is_pure_redacted_and_exact() -> None:
    desired = snapshot()
    exact = ReadOnlyShadowEvaluator.evaluate(desired, snapshot())
    drift = ReadOnlyShadowEvaluator.evaluate(desired, replace(snapshot(), status='DISABLED'))
    assert exact.state == 'exact' and exact.mismatch_fields == ()
    assert drift.state == 'drift' and drift.mismatch_fields == ('status',)
    assert not hasattr(exact, 'desired_hash_prefix')
    assert not hasattr(exact, 'observed_hash_prefix')
    assert 'owner-fingerprint' not in repr(exact)


@pytest.mark.parametrize(
    ('db_expiry', 'panel_expiry'),
    [
        ('2026-09-12T00:00:24.084771+00:00', '2026-09-12T00:00:24.084000+00:00'),
        ('2026-09-12T00:00:24.815158+00:00', '2026-09-12T00:00:24.815000+00:00'),
    ],
)
def test_shadow_expiry_in_same_utc_millisecond_bucket_is_exact(
    db_expiry: str,
    panel_expiry: str,
) -> None:
    desired = snapshot(expire_at=datetime.fromisoformat(db_expiry))
    observed = snapshot(expire_at=datetime.fromisoformat(panel_expiry))

    assert ReadOnlyShadowEvaluator.evaluate(desired, observed) == ReadOnlyShadowEvaluator.evaluate(
        desired,
        desired,
    )


def test_shadow_expiry_is_compared_after_utc_normalization() -> None:
    desired = snapshot(expire_at=datetime.fromisoformat('2026-09-12T03:00:24.084771+03:00'))
    observed = snapshot(expire_at=datetime.fromisoformat('2026-09-12T00:00:24.084000+00:00'))

    metric = ReadOnlyShadowEvaluator.evaluate(desired, observed)

    assert metric.state == 'exact'
    assert metric.mismatch_fields == ()


def test_shadow_adjacent_millisecond_buckets_drift_even_when_one_microsecond_apart() -> None:
    desired = snapshot(expire_at=datetime.fromisoformat('2026-09-12T00:00:24.084999+00:00'))
    observed = snapshot(expire_at=datetime.fromisoformat('2026-09-12T00:00:24.085000+00:00'))

    metric = ReadOnlyShadowEvaluator.evaluate(desired, observed)

    assert metric.state == 'drift'
    assert metric.mismatch_fields == ('expire_at',)


def test_shadow_expiry_difference_of_exactly_one_millisecond_is_drift() -> None:
    desired = snapshot(expire_at=datetime.fromisoformat('2026-09-12T00:00:24.085000+00:00'))
    observed = snapshot(expire_at=datetime.fromisoformat('2026-09-12T00:00:24.084000+00:00'))

    metric = ReadOnlyShadowEvaluator.evaluate(desired, observed)

    assert metric.state == 'drift'
    assert metric.mismatch_fields == ('expire_at',)


def test_shadow_sub_millisecond_expiry_does_not_mask_other_strict_drift() -> None:
    desired = snapshot(expire_at=datetime.fromisoformat('2026-09-12T00:00:24.084771+00:00'))
    observed = snapshot(
        expire_at=datetime.fromisoformat('2026-09-12T00:00:24.084000+00:00'),
        status='DISABLED',
    )

    metric = ReadOnlyShadowEvaluator.evaluate(desired, observed)

    assert metric.state == 'drift'
    assert metric.mismatch_fields == ('status',)


def test_global_comparator_and_snapshot_hashes_remain_strict_for_sub_millisecond_expiry() -> None:
    desired = snapshot(expire_at=datetime.fromisoformat('2026-09-12T00:00:24.084771+00:00'))
    observed = snapshot(expire_at=datetime.fromisoformat('2026-09-12T00:00:24.084000+00:00'))

    comparison = compare_snapshots(desired, observed)

    assert comparison.exact is False
    assert comparison.mismatch_fields == ('expire_at',)
    assert comparison.desired_hash == desired.desired_hash
    assert comparison.observed_hash == observed.desired_hash
    assert comparison.desired_hash != comparison.observed_hash
