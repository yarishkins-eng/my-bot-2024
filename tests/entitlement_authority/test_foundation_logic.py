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
    for key in ('internal_squads', 'external_squad_uuid', 'hwid_device_limit', 'traffic_limit_bytes'):
        incomplete = dict(value)
        incomplete.pop(key)
        with pytest.raises(ValueError, match='missing exact'):
            EntitlementSnapshot.from_mapping(incomplete)
    with pytest.raises(ValueError, match='timezone-aware'):
        snapshot(expire_at=datetime(2026, 8, 13))


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
    assert exact.desired_hash_prefix == desired.desired_hash[:12]
    assert 'owner-fingerprint' not in repr(exact)
