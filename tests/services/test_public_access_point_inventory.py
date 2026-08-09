from app.services.public_access_point_inventory import (
    InventoryHost,
    InventorySnapshot,
    InventorySquad,
    assess_inventory,
    plan_catalog_update,
)


def _safe_snapshot(*, first_title: str = 'Польша', second_title: str = 'Польша 2') -> InventorySnapshot:
    return InventorySnapshot(
        revision='fixture-r1',
        hosts=(
            InventoryHost(host_key='host-pl-1', title=first_title, squad_keys=('dedicated-pl-1',)),
            InventoryHost(host_key='host-pl-2', title=second_title, squad_keys=('dedicated-pl-2',)),
        ),
        squads=(
            InventorySquad(key='dedicated-pl-1', is_dedicated=True, is_healthy=True, raw_members=('raw-pl-1',)),
            InventorySquad(key='dedicated-pl-2', is_dedicated=True, is_healthy=True, raw_members=('raw-pl-2',)),
        ),
    )


def test_safe_host_titles_are_independent_assignable_access_points() -> None:
    result = assess_inventory(_safe_snapshot())

    assert [(point.host_key, point.title, point.state) for point in result.points] == [
        ('host-pl-1', 'Польша', 'verified'),
        ('host-pl-2', 'Польша 2', 'verified'),
    ]
    assert all(point.assignable for point in result.points)
    assert result.fingerprint


def test_shared_dedicated_squad_fails_closed_for_every_affected_host() -> None:
    snapshot = InventorySnapshot(
        revision='fixture-r1',
        hosts=(
            InventoryHost(host_key='host-a', title='Польша', squad_keys=('dedicated-shared',)),
            InventoryHost(host_key='host-b', title='Польша 2', squad_keys=('dedicated-shared',)),
        ),
        squads=(InventorySquad(key='dedicated-shared', is_dedicated=True, is_healthy=True, raw_members=('raw-pl',)),),
    )

    result = assess_inventory(snapshot)

    assert {point.state for point in result.points} == {'needs_verification'}
    assert all(not point.assignable for point in result.points)
    assert all('shared' in point.reason for point in result.points)


def test_duplicate_or_hidden_host_titles_are_not_assignable() -> None:
    snapshot = InventorySnapshot(
        revision='fixture-r1',
        hosts=(
            InventoryHost(host_key='host-a', title='Польша', squad_keys=('dedicated-a',)),
            InventoryHost(host_key='host-b', title='Польша', squad_keys=('dedicated-b',), is_hidden=True),
        ),
        squads=(
            InventorySquad(key='dedicated-a', is_dedicated=True, is_healthy=True, raw_members=('raw-a',)),
            InventorySquad(key='dedicated-b', is_dedicated=True, is_healthy=True, raw_members=('raw-b',)),
        ),
    )

    result = assess_inventory(snapshot)

    assert {point.state for point in result.points} == {'needs_verification'}
    assert all(not point.assignable for point in result.points)


def test_host_rename_changes_only_presentation_revision_when_graph_is_unchanged() -> None:
    current = assess_inventory(_safe_snapshot()).points
    renamed = assess_inventory(_safe_snapshot(first_title='Польша (Варшава)')).points

    updates = plan_catalog_update(current, renamed)
    first = next(update for update in updates if update.host_key == 'host-pl-1')

    assert first.presentation_changed is True
    assert first.entitlement_changed is False
    assert first.invalidate_unpaid_quote is False


def test_unrelated_catalog_addition_does_not_change_existing_point_entitlement_evidence() -> None:
    before = _safe_snapshot()
    after = InventorySnapshot(
        revision='fixture-r2',
        hosts=before.hosts + (InventoryHost(host_key='host-fi', title='Финляндия', squad_keys=('dedicated-fi',)),),
        squads=before.squads
        + (InventorySquad(key='dedicated-fi', is_dedicated=True, is_healthy=True, raw_members=('raw-fi',)),),
    )

    old_points = assess_inventory(before).points
    new_points = assess_inventory(after).points
    updates = plan_catalog_update(old_points, new_points)

    assert before.fingerprint != after.fingerprint
    assert next(update for update in updates if update.host_key == 'host-pl-1').entitlement_changed is False
    assert (
        next(point for point in old_points if point.host_key == 'host-pl-1').entitlement_fingerprint
        == next(point for point in new_points if point.host_key == 'host-pl-1').entitlement_fingerprint
    )
