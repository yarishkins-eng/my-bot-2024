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
        tariff_id=4,
        status='active',
    )


class _FakeSession:
    """Сессия, помнящая ПОРЯДОК событий: что добавлено и когда зафиксировано."""

    def __init__(self, subscriptions, users, snapshots=None, latest_rollout_id=None):
        self.subscriptions = subscriptions
        self.users = users
        self.snapshots = list(snapshots or [])
        self.latest_rollout_id = latest_rollout_id
        self.update_statements: list[object] = []
        self.extra_user_tariffs: list[tuple[int, int]] = []
        self.added: list[object] = []
        self.events: list[str] = []
        self.commits = 0

    async def execute(self, statement):
        text = str(statement)
        if 'FROM users' in text:
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: self.users))
        if text.strip().startswith('SELECT subscriptions.user_id, subscriptions.tariff_id'):
            pairs = [(sub.user_id, sub.tariff_id) for sub in self.subscriptions]
            pairs += list(self.extra_user_tariffs)
            return SimpleNamespace(all=lambda: pairs)
        if 'tariff_squad_rollout_snapshots' in text:
            if text.strip().upper().startswith('UPDATE'):
                # 🔴 Сохраняем САМ запрос, а не свою догадку о нём: сторож, который
                # сверяется со списком, подсунутым ему же, ничего не проверяет
                # (грабли 17.08). Ниже тест читает скомпилированный SQL.
                self.events.append('mark-restored')
                self.update_statements.append(statement)
                return SimpleNamespace()
            live = [row for row in self.snapshots if row.restored_at is None]
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: live))
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: self.subscriptions))

    async def scalar(self, statement):
        if 'tariff_squad_rollout_snapshots' in str(statement):
            return self.latest_rollout_id
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
    # Первая порция — одна подписка, поэтому её снимок и фиксация идут до панели.
    assert db.events[:2] == ['snapshot:1', 'commit'], db.events
    assert db.events.index('snapshot:2') > first_panel, 'вторая порция снимается после первой'


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
    # Первая порция намеренно из одной подписки: цена ошибки — один клиент, не двадцать пять.
    assert result.url_mismatch_ids == [1]
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


def _snapshot(sub_id: int, *, previous=('old-a', 'old-b'), restored_at=None):
    return SimpleNamespace(
        id=sub_id,
        rollout_id='r-1',
        tariff_id=4,
        subscription_id=sub_id,
        previous_squads=list(previous),
        previous_subscription_url='https://vpn/sub/x',
        applied_squads=['new-a'],
        batch_no=1,
        restored_at=restored_at,
    )


def _user(idx: int):
    return SimpleNamespace(
        id=idx * 10, telegram_id=idx, email=None, full_name='x', username=None, remnawave_uuid=f'u{idx}'
    )


@pytest.mark.asyncio
async def test_restore_marks_only_the_subscriptions_it_actually_returned():
    """🔴 P0 ревью: «все минус упавшие» сжигало пред-образ нетронутым подпискам.

    Подписка с исчерпанным трафиком возврат пропускает — и её строка снимка
    ОБЯЗАНА остаться живой, иначе вернуть её потом будет уже не по чему.
    """

    stranded = _sub(1, squads=['new-a'], used=100.0, limit=100)
    healthy = _sub(2, squads=['new-a'])
    db = _FakeSession([stranded, healthy], [_user(1), _user(2)], snapshots=[_snapshot(1), _snapshot(2)])
    db.latest_rollout_id = 'r-1'
    service, _ = _service_with_panel()

    result = await service.restore_tariff_squads(db, 4)

    assert result.synced_ids == [2], 'вернули только здоровую'
    assert result.skipped_traffic_risk_ids == [1]

    # Читаем НАСТОЯЩИЙ запрос, которым код гасит снимок.
    assert len(db.update_statements) == 1, 'снимок гасится ровно одним запросом'
    sql = str(db.update_statements[0].compile(compile_kwargs={'literal_binds': True}))
    assert 'IN (2)' in sql.replace('IN (2,)', 'IN (2)'), sql
    assert '1' not in sql.split('subscription_id IN', 1)[1].split(')', 1)[0], (
        'пред-образ пропущенной подписки 1 должен уцелеть, а он попал в гашение: ' + sql
    )


@pytest.mark.asyncio
async def test_restore_does_not_write_its_own_snapshot():
    """Иначе возврат сам станет «последней раскаткой» и второе нажатие всё вернёт назад."""

    subs = [_sub(1, squads=['new-a'])]
    db = _FakeSession(subs, [_user(1)], snapshots=[_snapshot(1)])
    db.latest_rollout_id = 'r-1'
    service, _ = _service_with_panel()

    await service.restore_tariff_squads(db, 4)

    assert not any(isinstance(obj, TariffSquadRolloutSnapshot) for obj in db.added)
    assert 'snapshot:1' not in db.events


@pytest.mark.asyncio
async def test_restore_counts_unrestorable_separately_and_never_calls_it_success():
    """Пустой пред-образ вернуть нельзя — это обязано быть видно, а не спрятано в «Готово»."""

    subs = [_sub(1, squads=['new-a'])]
    db = _FakeSession(subs, [_user(1)], snapshots=[_snapshot(1, previous=())])
    db.latest_rollout_id = 'r-1'
    service, _ = _service_with_panel()

    result = await service.restore_tariff_squads(db, 4)

    assert result.unrestorable_ids == [1]
    assert result.synced == 0
    assert result.total == 1, 'знаменатель — все строки снимка, а не только обработанные'


@pytest.mark.asyncio
async def test_fence_is_rechecked_before_every_batch():
    """Заказ клиента может родиться посреди раскатки — забор нужен на каждой порции."""

    subs = [_sub(i) for i in range(1, 6)]
    db = _FakeSession(subs, [_user(i) for i in range(1, 6)])
    service, _ = _service_with_panel()
    calls = {'n': 0}

    async def _fence():
        calls['n'] += 1
        if calls['n'] == 2:
            raise ValueError('this tariff has a live checkout')

    result = await service.propagate_tariff_squads(db, 4, ['new-a'], batch_size=2, recheck_fence=_fence)

    assert calls['n'] == 2, 'забор спрашивается перед каждой порцией'
    assert result.stopped_early is True
    assert result.batches_done == 1, 'вторая порция не должна была уйти в панель'


@pytest.mark.asyncio
async def test_empty_link_from_the_panel_stops_the_rollout():
    """Пустая ссылка — худший случай: клиент остаётся без подключения."""

    subs = [_sub(i) for i in range(1, 5)]
    db = _FakeSession(subs, [_user(i) for i in range(1, 5)])
    service, _ = _service_with_panel(panel_url='')

    result = await service.propagate_tariff_squads(db, 4, ['new-a'], batch_size=2)

    assert result.stopped_early is True
    assert result.url_mismatch_ids == [1]


@pytest.mark.asyncio
async def test_rollout_goes_in_portions_and_reports_the_remainder():
    """Кнопка должна доезжать до конца: уже совпавшие исключаются, остаток виден."""

    subs = [_sub(1), _sub(2), _sub(3, squads=['new-a'])]
    db = _FakeSession(subs, [_user(1), _user(2), _user(3)])
    service, _ = _service_with_panel()

    result = await service.propagate_tariff_squads(db, 4, ['new-a'], limit=1)

    assert result.total == 1, 'за одно нажатие — одна порция'
    assert result.synced_ids == [1]
    assert result.remaining == 1, 'подписка 2 ждёт; подписка 3 уже на месте и не в счёт'


@pytest.mark.asyncio
async def test_client_with_a_second_live_subscription_is_left_alone():
    """Вне мультитарифа панельный аккаунт ОБЩИЙ на человека.

    Если у клиента живёт вторая подписка другого тарифа, раскатка затёрла бы ей
    доступ, за который он заплатил. Сегодня таких на боевом ноль — защита стоит
    на будущее, потому что включение мультитарифа это меняет.
    """

    shared = _sub(1)
    alone = _sub(2)
    db = _FakeSession([shared, alone], [_user(1), _user(2)])
    # у владельца подписки 1 есть ещё одна живая, на другом тарифе
    db.extra_user_tariffs = [(shared.user_id, 99)]
    service, _ = _service_with_panel()

    result = await service.propagate_tariff_squads(db, 4, ['new-a'])

    assert 1 in result.skipped_traffic_risk_ids, 'подписку с общим аккаунтом не трогаем'
    assert result.synced_ids == [2]
    assert shared.connected_squads == ['old-a', 'old-b'], 'её серверы не изменились'


@pytest.mark.asyncio
async def test_batch_reads_fresh_subscription_data_before_touching_the_panel():
    """Клиент мог продлиться, пока раскатка шла по предыдущим порциям.

    Объекты грузятся один раз до цикла и с expire_on_commit=False живут со
    старыми значениями. Отправить в панель устаревший срок или лимит — значит
    отключить того, кто только что заплатил. Проверяем ЭФФЕКТ: в панель обязан
    уйти лимит, появившийся уже после старта раскатки.
    """

    subs = [_sub(1), _sub(2, limit=50)]
    db = _FakeSession(subs, [_user(1), _user(2)])

    async def _refresh(obj, fields=None):
        # 🔴 Предзагрузка тарифа зовёт refresh(sub, ['tariff']) ДО цикла порций и
        # ловила бы этот сторож вместо проверяемого перечитывания. Реагируем только
        # на полное обновление объекта — то самое, что делается перед порцией.
        if fields is not None:
            return
        # имитируем чужой коммит: клиент докупил трафик, пока шла первая порция
        if obj.id == 2:
            obj.traffic_limit_gb = 500

    db.refresh = _refresh
    service, api = _service_with_panel()
    sent: list[dict] = []

    async def _capture(**kwargs):
        sent.append(kwargs)
        return SimpleNamespace(subscription_url='https://vpn/sub/x', happ_crypto_link=None)

    api.update_user = _capture

    await service.propagate_tariff_squads(db, 4, ['new-a'], batch_size=5)

    limits = {call['uuid']: call['traffic_limit_bytes'] for call in sent}
    assert limits['u2'] == 500 * 1024 * 1024 * 1024, 'в панель ушёл устаревший лимит'
