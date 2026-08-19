"""Сторожа порционной раскатки серверов тарифа (пункт 3.2 плана восстановления).

Раскатка переписывает connected_squads выданным подпискам и шлёт набор в панель.
Проверяем то, ради чего пункт и делался: снимок ложится в базу ДО панели, каждая
порция фиксируется отдельно, подписка с израсходованным лимитом не трогается, а
разъехавшаяся ссылка подключения останавливает раскатку.
"""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.database.models import TariffSquadRolloutSnapshot
from app.services.subscription_service import (
    PropagateSquadsResult,
    SubscriptionService,
    _traffic_limit_would_strand,
)


def _sub(sub_id: int, *, squads=None, used=0.0, limit=100, url='https://vpn/sub/x'):
    return SimpleNamespace(
        id=sub_id,
        user_id=sub_id * 10,
        connected_squads=list(squads or ['old-a', 'old-b']),
        traffic_used_gb=used,
        traffic_limit_gb=limit,
        subscription_url=url,
        subscription_crypto_link=None,
        remnawave_uuid=f'uuid-{sub_id}',
        tariff=SimpleNamespace(external_squad_uuid=None),
        status='active',
    )


class _FakeSession:
    """Сессия, помнящая ПОРЯДОК событий: что добавлено и когда зафиксировано."""

    def __init__(self, subscriptions, users):
        self.subscriptions = subscriptions
        self.users = users
        self.added: list[object] = []
        self.events: list[str] = []
        self.commits = 0

    async def execute(self, statement):
        text = str(statement)
        if 'FROM users' in text:
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: self.users))
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: self.subscriptions))

    async def scalar(self, statement):
        return None

    def add(self, obj):
        self.added.append(obj)
        if isinstance(obj, TariffSquadRolloutSnapshot):
            self.events.append(f'snapshot:{obj.subscription_id}')

    async def commit(self):
        self.commits += 1
        self.events.append('commit')

    async def rollback(self):
        self.events.append('rollback')

    async def refresh(self, obj, fields=None):
        return None


def _service_with_panel(panel_url='https://vpn/sub/x'):
    service = SubscriptionService()
    api = AsyncMock()
    api.update_user = AsyncMock(return_value=SimpleNamespace(subscription_url=panel_url, happ_crypto_link=None))

    @asynccontextmanager
    async def _client():
        yield api

    service.get_api_client = _client
    return service, api


@pytest.mark.asyncio
async def test_snapshot_is_committed_before_the_batch_reaches_the_panel():
    """Пред-образ обязан лежать в базе ДО панели, иначе обрыв нечем откатить."""

    subs = [_sub(1), _sub(2)]
    users = [SimpleNamespace(id=10, telegram_id=1, email=None, full_name='a', username=None, remnawave_uuid='u1')]
    users += [SimpleNamespace(id=20, telegram_id=2, email=None, full_name='b', username=None, remnawave_uuid='u2')]
    db = _FakeSession(subs, users)
    service, api = _service_with_panel()

    calls: list[str] = []

    async def _tracked_update(**kwargs):
        calls.append('panel')
        db.events.append('panel')
        return SimpleNamespace(subscription_url='https://vpn/sub/x', happ_crypto_link=None)

    api.update_user = _tracked_update

    result = await service.propagate_tariff_squads(db, 4, ['new-a'], batch_size=25)

    assert result.synced == 2
    first_panel = db.events.index('panel')
    first_commit = db.events.index('commit')
    assert first_commit < first_panel, db.events
    assert db.events[:2] == ['snapshot:1', 'snapshot:2']


@pytest.mark.asyncio
async def test_each_batch_is_committed_separately():
    """Обрыв должен стоить одну порцию, а не всю раскатку."""

    subs = [_sub(i) for i in range(1, 6)]
    users = [
        SimpleNamespace(id=i * 10, telegram_id=i, email=None, full_name='x', username=None, remnawave_uuid=f'u{i}')
        for i in range(1, 6)
    ]
    db = _FakeSession(subs, users)
    service, _ = _service_with_panel()

    result = await service.propagate_tariff_squads(db, 4, ['new-a'], batch_size=2)

    assert result.total == 5
    assert result.batches_done == 3
    # три порции: снимок+фиксация и фиксация результата на каждую
    assert db.commits == 6, db.events


@pytest.mark.asyncio
async def test_subscription_with_spent_traffic_is_left_alone():
    """Отправка израсходованного лимита мгновенно даёт LIMITED — такую не трогаем."""

    spent = _sub(1, used=100.0, limit=100)
    healthy = _sub(2, used=1.0, limit=100)
    users = [
        SimpleNamespace(id=20, telegram_id=2, email=None, full_name='b', username=None, remnawave_uuid='u2'),
    ]
    db = _FakeSession([spent, healthy], users)
    service, _ = _service_with_panel()

    result = await service.propagate_tariff_squads(db, 4, ['new-a'])

    assert result.skipped_traffic_risk_ids == [1]
    assert result.total == 1
    assert spent.connected_squads == ['old-a', 'old-b']
    assert healthy.connected_squads == ['new-a']


@pytest.mark.asyncio
async def test_rollout_stops_when_the_connection_link_moves():
    """Ссылка подключения не должна меняться от смены серверов."""

    subs = [_sub(i) for i in range(1, 5)]
    users = [
        SimpleNamespace(id=i * 10, telegram_id=i, email=None, full_name='x', username=None, remnawave_uuid=f'u{i}')
        for i in range(1, 5)
    ]
    db = _FakeSession(subs, users)
    service, _ = _service_with_panel(panel_url='https://vpn/sub/MOVED')

    result = await service.propagate_tariff_squads(db, 4, ['new-a'], batch_size=2)

    assert result.stopped_early is True
    assert result.url_mismatch_ids == [1, 2]
    assert result.batches_done == 1, 'вторая порция не должна была уйти в панель'


def test_traffic_guard_treats_unlimited_as_safe():
    assert _traffic_limit_would_strand(_sub(1, used=999.0, limit=0)) is False
    assert _traffic_limit_would_strand(_sub(1, used=99.0, limit=100)) is False
    assert _traffic_limit_would_strand(_sub(1, used=100.0, limit=100)) is True


@pytest.mark.asyncio
async def test_preview_changes_nothing():
    """Сухой прогон обязан быть безопасным: ни записи, ни панели."""

    subs = [_sub(1), _sub(2, squads=['new-a'])]
    db = _FakeSession(subs, [])
    service, api = _service_with_panel()

    plan = await service.plan_tariff_squad_rollout(db, 4, ['new-a'])

    assert plan['candidates'] == 2
    assert plan['would_change'] == 1
    assert plan['would_change_ids'] == [1]
    assert db.commits == 0
    assert db.added == []
    api.update_user.assert_not_awaited()


def test_result_carries_everything_the_owner_must_see():
    result = PropagateSquadsResult()
    for field_name in ('rollout_id', 'batches_done', 'skipped_traffic_risk_ids', 'url_mismatch_ids', 'stopped_early'):
        assert hasattr(result, field_name), field_name
