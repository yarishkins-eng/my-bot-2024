"""Sanitized reproduction of the twelve Phase 0 evidence tests.

These tests intentionally prove unsafe legacy behaviour on the untouched
``origin/main`` paths.  Target-foundation tests live in separate modules and
must never weaken these evidence assertions.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import asyncpg
import pytest

from app.external.remnawave_api import (
    RemnaWaveAPI,
    RemnaWaveAPIError,
)
from app.services import subscription_service as subscription_service_module
from app.services.subscription_service import SubscriptionService


DATABASE_URL = os.environ.get('ENTITLEMENT_AUTHORITY_TEST_DATABASE_URL')
if DATABASE_URL is None:
    pytest.skip('isolated entitlement authority PostgreSQL is not configured', allow_module_level=True)
ROOT = Path(__file__).resolve().parents[2]
INVENTORY = ROOT / 'docs/entitlement_authority/evidence/phase0_writer_inventory.json'
RUNTIME = ROOT / 'docs/entitlement_authority/evidence/runtime_writer_coverage.json'
MANIFEST = ROOT / 'docs/entitlement_authority/evidence/affected_test_manifest.txt'


class _Response:
    def __init__(self, status: int, body: dict) -> None:
        self.status = status
        self.headers: dict[str, str] = {}
        self._body = json.dumps(body)

    async def text(self) -> str:
        return self._body


class _ResponseContext:
    def __init__(self, response: _Response) -> None:
        self.response = response

    async def __aenter__(self) -> _Response:
        return self.response

    async def __aexit__(self, *_args: object) -> None:
        return None


class _SequencedSession:
    def __init__(self, outcomes: list[_Response | Exception]) -> None:
        self.outcomes = outcomes
        self.calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def request(self, *args: object, **kwargs: object) -> _ResponseContext:
        self.calls.append((args, kwargs))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return _ResponseContext(outcome)


def _legacy_user() -> SimpleNamespace:
    return SimpleNamespace(id=7, remnawave_uuid='panel-existing')


def _legacy_subscription() -> SimpleNamespace:
    return SimpleNamespace(
        id=11,
        user_id=7,
        remnawave_uuid='panel-existing',
        subscription_url='https://redacted.invalid/sub',
    )


@asynccontextmanager
async def _api_context(api: object):
    yield api


@pytest.mark.asyncio
async def test_phase0_existing_binding_with_stale_fields_returns_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    service = SubscriptionService()
    api = SimpleNamespace(get_user_by_uuid=AsyncMock(return_value=SimpleNamespace(status='DISABLED')))
    service.get_api_client = lambda: _api_context(api)  # type: ignore[method-assign]
    service.update_remnawave_user = AsyncMock()
    service.create_remnawave_user = AsyncMock()
    monkeypatch.setattr(subscription_service_module, 'get_user_by_id', AsyncMock(return_value=_legacy_user()))

    ready, error = await service.ensure_subscription_synced(AsyncMock(), _legacy_subscription())

    assert (ready, error) == (True, None)
    service.update_remnawave_user.assert_not_awaited()
    service.create_remnawave_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_phase0_get_timeout_returns_ready_without_create(monkeypatch: pytest.MonkeyPatch) -> None:
    service = SubscriptionService()
    api = SimpleNamespace(get_user_by_uuid=AsyncMock(side_effect=TimeoutError('redacted')))
    service.get_api_client = lambda: _api_context(api)  # type: ignore[method-assign]
    service.update_remnawave_user = AsyncMock()
    service.create_remnawave_user = AsyncMock()
    monkeypatch.setattr(subscription_service_module, 'get_user_by_id', AsyncMock(return_value=_legacy_user()))

    ready, error = await service.ensure_subscription_synced(AsyncMock(), _legacy_subscription())

    assert (ready, error) == (True, None)
    service.create_remnawave_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_phase0_mutating_patch_retries_after_503(monkeypatch: pytest.MonkeyPatch) -> None:
    api = RemnaWaveAPI('http://panel.invalid', 'test')
    api.session = _SequencedSession(
        [
            _Response(503, {'message': 'temporary'}),
            _Response(200, {'response': {'ok': True}}),
        ]
    )  # type: ignore[assignment]
    monkeypatch.setattr(asyncio, 'sleep', AsyncMock())

    result = await api._make_request('PATCH', '/api/users', {'uuid': 'opaque'})

    assert result == {'response': {'ok': True}}
    assert len(api.session.calls) == 2


@pytest.mark.asyncio
async def test_phase0_lost_create_response_sends_second_post(monkeypatch: pytest.MonkeyPatch) -> None:
    api = RemnaWaveAPI('http://panel.invalid', 'test')
    api.session = _SequencedSession(
        [
            aiohttp.ClientConnectionError('response lost'),
            _Response(200, {'response': {'ok': True}}),
        ]
    )  # type: ignore[assignment]
    monkeypatch.setattr(asyncio, 'sleep', AsyncMock())

    await api._make_request('POST', '/api/users', {'status': 'DISABLED'})

    assert len(api.session.calls) == 2
    assert all(call[0][0] == 'POST' for call in api.session.calls)


def _panel_user() -> SimpleNamespace:
    return SimpleNamespace(uuid='panel-one', hwid_device_limit=None)


@pytest.mark.asyncio
async def test_phase0_a039_create_degrades_external_squad() -> None:
    api = RemnaWaveAPI('http://panel.invalid', 'test')
    payloads: list[dict[str, object]] = []

    async def request(*args: object) -> dict:
        payloads.append(dict(args[2]))  # type: ignore[arg-type]
        if len(payloads) == 1:
            raise RemnaWaveAPIError('generic update error', 400, {'errorCode': 'A039'})
        return {'response': {}}

    api._make_request = AsyncMock(side_effect=request)
    api._parse_user = lambda _data: _panel_user()  # type: ignore[method-assign]
    api.enrich_user_with_happ_link = AsyncMock(side_effect=lambda value: value)

    await api.create_user(
        username='opaque',
        expire_at=datetime.now(UTC) + timedelta(days=1),
        external_squad_uuid='squad-one',
    )

    assert payloads[0]['externalSquadUuid'] == 'squad-one'
    assert 'externalSquadUuid' not in payloads[1]


@pytest.mark.asyncio
async def test_phase0_a039_patch_degrades_external_squad() -> None:
    api = RemnaWaveAPI('http://panel.invalid', 'test')
    payloads: list[dict[str, object]] = []

    async def request(*args: object) -> dict:
        payloads.append(dict(args[2]))  # type: ignore[arg-type]
        if len(payloads) == 1:
            raise RemnaWaveAPIError('generic update error', 400, {'errorCode': 'A039'})
        return {'response': {}}

    api._make_request = AsyncMock(side_effect=request)
    api._parse_user = lambda _data: _panel_user()  # type: ignore[method-assign]
    api.enrich_user_with_happ_link = AsyncMock(side_effect=lambda value: value)

    await api.update_user(uuid='panel-one', external_squad_uuid='squad-one')

    assert payloads[0]['externalSquadUuid'] == 'squad-one'
    assert 'externalSquadUuid' not in payloads[1]


async def _deadlock_cycle(*, application_labels: bool) -> None:
    conn = await asyncpg.connect(DATABASE_URL)
    schema = f'phase0_{"app" if application_labels else "minimal"}'
    try:
        await conn.execute(f'DROP SCHEMA IF EXISTS {schema} CASCADE')
        await conn.execute(f'CREATE SCHEMA {schema}')
        await conn.execute(f'CREATE TABLE {schema}.users (id integer PRIMARY KEY)')
        await conn.execute(f'CREATE TABLE {schema}.subscriptions (id integer PRIMARY KEY)')
        await conn.execute(f'INSERT INTO {schema}.users VALUES (1)')
        await conn.execute(f'INSERT INTO {schema}.subscriptions VALUES (1)')
    finally:
        await conn.close()
    first = await asyncpg.connect(DATABASE_URL)
    second = await asyncpg.connect(DATABASE_URL)
    try:
        tx1 = first.transaction()
        tx2 = second.transaction()
        await tx1.start()
        await tx2.start()
        await first.execute(f'SELECT id FROM {schema}.users WHERE id=1 FOR UPDATE')
        await second.execute(f'SELECT id FROM {schema}.subscriptions WHERE id=1 FOR UPDATE')
        wait_one = asyncio.create_task(first.execute(f'SELECT id FROM {schema}.subscriptions WHERE id=1 FOR UPDATE'))
        wait_two = asyncio.create_task(second.execute(f'SELECT id FROM {schema}.users WHERE id=1 FOR UPDATE'))
        outcomes = await asyncio.gather(wait_one, wait_two, return_exceptions=True)
        assert sum(isinstance(item, asyncpg.DeadlockDetectedError) for item in outcomes) == 1
    finally:
        await first.close()
        await second.close()


@pytest.mark.asyncio
async def test_phase0_real_postgres_minimal_lock_cycle() -> None:
    await _deadlock_cycle(application_labels=False)


@pytest.mark.asyncio
async def test_phase0_real_postgres_application_lock_order_cycle() -> None:
    """The two tables model Direct User→Subscription and AP Subscription→User."""

    await _deadlock_cycle(application_labels=True)


@pytest.mark.asyncio
async def test_phase0_process_loss_before_claim_commit_rolls_back() -> None:
    conn = await asyncpg.connect(DATABASE_URL)
    observer = await asyncpg.connect(DATABASE_URL)
    try:
        await conn.execute('DROP TABLE IF EXISTS phase0_claims')
        await conn.execute('CREATE TABLE phase0_claims (id integer PRIMARY KEY, state text NOT NULL)')
        tx = conn.transaction()
        await tx.start()
        await conn.execute("INSERT INTO phase0_claims VALUES (1, 'claimed')")
        await conn.close()  # process/connection loss before commit
        assert await observer.fetchval('SELECT count(*) FROM phase0_claims') == 0
    finally:
        if not conn.is_closed():
            await conn.close()
        await observer.close()


@pytest.mark.asyncio
async def test_phase0_financial_rollback_leaves_zero_projection_commands() -> None:
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        await conn.execute('DROP TABLE IF EXISTS phase0_projection_commands')
        await conn.execute('CREATE TABLE phase0_projection_commands (id integer PRIMARY KEY, source_key text)')
        tx = conn.transaction()
        await tx.start()
        await conn.execute("INSERT INTO phase0_projection_commands VALUES (1, 'opaque-source')")
        await tx.rollback()
        assert await conn.fetchval('SELECT count(*) FROM phase0_projection_commands') == 0
    finally:
        await conn.close()


def test_phase0_static_writer_inventory_baseline_is_exact() -> None:
    data = json.loads(INVENTORY.read_text(encoding='utf-8'))
    assert data['sha256'] == '724d620dd11f0b878d5d802bf8a8330c8d73cd8a75b09abf31bfd374190634c9'
    assert data['counts']['raw_endpoints'] == 44
    assert data['counts']['startup_registrations'] == 61
    assert data['syntax_errors'] == []
    assert data['unknown_raw_requests'] == []


def test_phase0_runtime_evidence_is_bound_to_inventory_and_manifest() -> None:
    inventory = json.loads(INVENTORY.read_text(encoding='utf-8'))
    runtime = json.loads(RUNTIME.read_text(encoding='utf-8'))
    manifest_hash = hashlib.sha256(MANIFEST.read_bytes()).hexdigest()
    assert runtime['inventory_sha256'] == inventory['sha256']
    assert runtime['test_manifest_sha256'] == manifest_hash
    assert runtime['sections']['raw_endpoints']['total'] == 44
    assert runtime['sections']['startup_registrations']['total'] == 61
