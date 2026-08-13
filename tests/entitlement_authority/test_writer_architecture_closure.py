from __future__ import annotations

import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
INVENTORY = ROOT / 'docs/entitlement_authority/evidence/writer_inventory.json'
BASELINE = ROOT / 'docs/entitlement_authority/evidence/phase0_writer_inventory.json'
CLOSURE = ROOT / 'docs/entitlement_authority/evidence/writer_closure.json'
UNION = ROOT / 'docs/entitlement_authority/evidence/writer_union.json'


def test_fresh_inventory_matches_frozen_current_architecture(tmp_path: Path) -> None:
    generated = tmp_path / 'inventory.json'
    subprocess.run(  # noqa: S603 - reviewed local interpreter and repository tool
        [
            str(ROOT / '.venv/bin/python'),
            str(ROOT / 'docs/entitlement_authority/tools/inventory_entitlement_writers.py'),
            '--root',
            str(ROOT),
            '--output',
            str(generated),
        ],
        check=True,
    )
    expected = json.loads(INVENTORY.read_text(encoding='utf-8'))
    actual = json.loads(generated.read_text(encoding='utf-8'))
    assert actual['sha256'] == expected['sha256']
    assert actual['syntax_errors'] == []
    assert actual['unknown_raw_requests'] == []
    baseline = json.loads(BASELINE.read_text(encoding='utf-8'))
    baseline_raw = {
        (item['path'], item['function'], item['method'], item['endpoint']) for item in baseline['raw_endpoints']
    }
    current_raw = {
        (item['path'], item['function'], item['method'], item['endpoint']) for item in actual['raw_endpoints']
    }
    baseline_startups = {(item['path'], item['function'], item['call']) for item in baseline['startup_registrations']}
    current_startups = {(item['path'], item['function'], item['call']) for item in actual['startup_registrations']}
    assert baseline_raw <= current_raw
    assert baseline_startups <= current_startups
    assert current_startups - baseline_startups == {
        ('main.py', 'main', 'asyncio.create_task(entitlement_shadow_service.run())')
    }
    assert current_raw - baseline_raw == {
        ('app/services/entitlement_authority/strict_panel.py', 'create_disabled', 'POST', "'/api/users'"),
        ('app/services/entitlement_authority/strict_panel.py', 'patch_exact', 'PATCH', "'/api/users'"),
        ('app/services/entitlement_authority/strict_panel.py', 'delete_once', 'DELETE', "f'/api/users/{panel_uuid}'"),
    }


def test_every_writer_inventory_entry_has_reviewed_primitive_or_exclusion() -> None:
    inventory = json.loads(INVENTORY.read_text(encoding='utf-8'))
    closure = json.loads(CLOSURE.read_text(encoding='utf-8'))
    assert closure['inventory_sha256'] == inventory['sha256']
    assert closure['baseline_relation'].startswith('classified_current_inventory')
    for section in (
        'calls',
        'definitions',
        'field_literals',
        'imports',
        'raw_endpoints',
        'reachable_writer_functions',
        'reachable_writer_entrypoints',
        'startup_registrations',
    ):
        assert closure['counts'][section] == len(inventory[section])
    assert len({entry['entry_id'] for entry in closure['entries']}) == len(closure['entries'])
    assert all(entry['primitive'] in closure['allowed_primitives'] for entry in closure['entries'])
    assert all(entry['evidence'] for entry in closure['entries'])


def test_raw_access_writers_are_never_hidden_as_metadata_only() -> None:
    closure = json.loads(CLOSURE.read_text(encoding='utf-8'))
    raw = [entry for entry in closure['entries'] if entry['section'] == 'raw_endpoints']
    access_surfaces = {
        'identity_create',
        'identity_patch',
        'identity_delete',
        'identity_deny',
        'identity_enable',
        'traffic_reset',
        'credential_revoke',
        'device_projection',
        'membership_projection',
        'bulk_membership_projection',
        'catalog_topology',
        'payment_provider',
    }
    assert len(raw) == 47
    assert all(entry['primitive'] != 'metadata_only' for entry in raw if entry['surface'] in access_surfaces)


def test_startup_classification_has_no_blanket_exclusion_and_keeps_known_writers() -> None:
    closure = json.loads(CLOSURE.read_text(encoding='utf-8'))
    startups = [entry for entry in closure['entries'] if entry['section'] == 'startup_registrations']
    assert len(startups) == 62
    assert all(entry['surface'] != 'scanner_false_positive' for entry in startups)
    by_path = {}
    for entry in startups:
        by_path.setdefault(entry['path'], []).append(entry)
    payment = by_path['app/services/payment_verification_service.py']
    dedupe = [
        entry
        for entry in by_path['main.py']
        if entry['symbol'] == 'main' and entry['surface'] == 'legacy_subscription_dedupe'
    ]
    assert len(payment) == 1 and payment[0]['primitive'] == 'source_mutation'
    assert len(dedupe) == 1 and dedupe[0]['primitive'] == 'cleanup'
    shadow = [
        entry
        for entry in by_path['main.py']
        if entry['symbol'] == 'main' and entry['surface'] == 'entitlement_readonly_shadow'
    ]
    assert len(shadow) == 1 and shadow[0]['primitive'] == 'observation'


def test_writer_closure_artifact_is_reproducible() -> None:
    subprocess.run(  # noqa: S603 - reviewed local interpreter and repository tool
        [
            str(ROOT / '.venv/bin/python'),
            str(ROOT / 'docs/entitlement_authority/tools/classify_writer_inventory.py'),
            '--inventory',
            str(INVENTORY),
            '--output',
            str(CLOSURE),
            '--check',
        ],
        check=True,
    )
    subprocess.run(  # noqa: S603 - reviewed local interpreter and repository tool
        [
            str(ROOT / '.venv/bin/python'),
            str(ROOT / 'docs/entitlement_authority/tools/compare_writer_inventories.py'),
            '--baseline',
            str(BASELINE),
            '--current',
            str(INVENTORY),
            '--output',
            str(UNION),
            '--check',
        ],
        check=True,
    )


def test_baseline_current_union_has_no_unmapped_legacy_raw_or_startup_removal() -> None:
    union = json.loads(UNION.read_text(encoding='utf-8'))
    assert union['baseline_sha256'] == json.loads(BASELINE.read_text(encoding='utf-8'))['sha256']
    assert union['current_sha256'] == json.loads(INVENTORY.read_text(encoding='utf-8'))['sha256']
    assert union['sections']['raw_endpoints']['removed_or_renamed'] == []
    assert union['sections']['startup_registrations']['removed_or_renamed'] == []
    assert len(union['sections']['startup_registrations']['new']) == 1
    assert 'entitlement_shadow_service.run' in union['sections']['startup_registrations']['new'][0]['signature']['call']
