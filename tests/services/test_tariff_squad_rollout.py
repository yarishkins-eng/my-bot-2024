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


def _compiled(statement) -> str:
    return str(statement.compile(compile_kwargs={'literal_binds': True}))


def _ids_in_sql(statement) -> list[int]:
    """Настоящие id из скомпилированного запроса, а не наша догадка о них."""

    sql = _compiled(statement)
    if 'subscription_id IN' not in sql:
        return []
    inside = sql.split('subscription_id IN', 1)[1].split('(', 1)[1].split(')', 1)[0]
    return [int(part) for part in inside.replace(' ', '').split(',') if part.strip().isdigit()]


def _tariff_id_in_sql(statement) -> int | None:
    sql = _compiled(statement)
    if 'tariff_squad_rollout_snapshots.tariff_id = ' not in sql:
        return None
    tail = sql.split('tariff_squad_rollout_snapshots.tariff_id = ', 1)[1]
    digits = ''
    for char in tail.strip():
        if not char.isdigit():
            break
        digits += char
    return int(digits) if digits else None


def _subscription_ids_in_sql(statement) -> set[int] | None:
    sql = _compiled(statement)
    if 'subscriptions.id IN' not in sql:
        return None
    inside = sql.split('subscriptions.id IN', 1)[1].split('(', 1)[1].split(')', 1)[0]
    return {int(part) for part in inside.replace(' ', '').split(',') if part.strip().isdigit()}


def _rollout_id_in_sql(statement) -> str | None:
    sql = _compiled(statement)
    if 'rollout_id = ' not in sql:
        return None
    return sql.split('rollout_id = ', 1)[1].split('\n', 1)[0].strip().strip("'").rstrip(' AND').strip()


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
                # И ИСПОЛНЯЕМ его, как это сделала бы база: иначе следующий запрос
                # снимков увидит их непогашенными, и многопортионный сценарий
                # (ради которого всё и чинилось) в тесте не воспроизведётся.
                limited_to = _rollout_id_in_sql(statement)
                limited_tariff = _tariff_id_in_sql(statement)
                for sub_id in _ids_in_sql(statement):
                    for row in self.snapshots:
                        if row.subscription_id != sub_id or row.restored_at is not None:
                            continue
                        if limited_to is not None and row.rollout_id != limited_to:
                            continue
                        if limited_tariff is not None and row.tariff_id != limited_tariff:
                            continue
                        row.restored_at = 'restored'
                return SimpleNamespace()
            live = [row for row in self.snapshots if row.restored_at is None]
            # Граница по тарифу: без неё «Вернуть» для одного тарифа молча откатил бы
            # подписки ЧУЖИХ тарифов. Мутация показала, что фейк этого не замечал.
            wanted_tariff = _tariff_id_in_sql(statement)
            if wanted_tariff is not None:
                live = [row for row in live if row.tariff_id == wanted_tariff]
            # SELECT снимков фильтруется по rollout_id, если он указан в запросе —
            # без этого возврат «по последней раскатке» выглядел бы рабочим.
            wanted = _rollout_id_in_sql(statement)
            if wanted is not None:
                live = [row for row in live if row.rollout_id == wanted]
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: live))
        # Фильтр по списку id обязателен: без него порционные и групповые вызовы
        # получают ВСЕ подписки, и настоящие дефекты в них не воспроизводятся.
        rows = self.subscriptions
        wanted_ids = _subscription_ids_in_sql(statement)
        if wanted_ids is not None:
            rows = [sub for sub in rows if sub.id in wanted_ids]
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))

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

    assert result.shared_account_ids == [1], 'причина названа своим именем, а не «трафик»'
    assert 1 not in result.skipped_traffic_risk_ids, 'это не про трафик'
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


@pytest.mark.asyncio
async def test_restore_reaches_every_portion_of_a_multi_press_rollout():
    """🔴 P0 волны 2: раскатка в несколько нажатий — это несколько rollout_id.

    Возврат «по последней раскатке» вернул бы только хвост и отчитался «Готово»,
    а подписки первых порций остались бы на новых серверах без следа в интерфейсе
    и без пути назад. Одно нажатие «Вернуть» обязано отменить раскатку ЦЕЛИКОМ.
    """

    first = _sub(1, squads=['new-a'])
    second = _sub(2, squads=['new-a'])
    snapshots = [
        _snapshot(1),  # первое нажатие
        _snapshot(2),  # второе нажатие, другой rollout_id
    ]
    snapshots[0].rollout_id = 'press-1'
    snapshots[1].rollout_id = 'press-2'
    db = _FakeSession([first, second], [_user(1), _user(2)], snapshots=snapshots)
    service, _ = _service_with_panel()

    result = await service.restore_tariff_squads(db, 4)

    assert sorted(result.synced_ids) == [1, 2], 'вернулись обе порции, а не только последняя'
    assert first.connected_squads == ['old-a', 'old-b']
    assert second.connected_squads == ['old-a', 'old-b']

    # Повторное нажатие уже ничего не откатывает назад.
    again = await service.restore_tariff_squads(db, 4)
    assert again.total == 0 and again.synced == 0


@pytest.mark.asyncio
async def test_restore_leaves_alone_a_client_who_changed_servers_himself():
    """Клиент сам сменил страну после раскатки — это его выбор, не наш откат."""

    moved = _sub(1, squads=['client-choice'])
    untouched = _sub(2, squads=['new-a'])
    db = _FakeSession([moved, untouched], [_user(1), _user(2)], snapshots=[_snapshot(1), _snapshot(2)])
    service, _ = _service_with_panel()

    result = await service.restore_tariff_squads(db, 4)

    assert result.moved_on_ids == [1]
    assert moved.connected_squads == ['client-choice'], 'его выбор не тронут'
    assert result.synced_ids == [2]


@pytest.mark.asyncio
async def test_preview_skips_exactly_what_the_real_rollout_skips():
    """Сухой прогон обещает показать, кого пропустит. Он обязан видеть все причины."""

    shared = _sub(1)
    alone = _sub(2)
    db = _FakeSession([shared, alone], [_user(1), _user(2)])
    db.extra_user_tariffs = [(shared.user_id, 99)]
    service, _ = _service_with_panel()

    plan = await service.plan_tariff_squad_rollout(db, 4, ['new-a'])

    assert plan['shared_account_ids'] == [1]
    assert plan['would_change'] == 1, 'обещанное число должно совпасть с тем, что реально уедет'
    assert plan['would_change_ids'] == [2]


@pytest.mark.asyncio
async def test_limited_subscription_counts_as_a_live_one_for_the_shared_account_guard():
    """LIMITED — это «трафик кончился», а не «подписки нет».

    Панельный аккаунт такая подписка делит наравне с активной, и раскатка чужого
    тарифа затёрла бы ей доступ так же.
    """

    shared = _sub(1)
    db = _FakeSession([shared], [_user(1)])
    db.extra_user_tariffs = [(shared.user_id, 99)]
    service, _ = _service_with_panel()

    statements: list[object] = []
    original_execute = db.execute

    async def _spy(statement):
        statements.append(statement)
        return await original_execute(statement)

    db.execute = _spy
    await service.propagate_tariff_squads(db, 4, ['new-a'])

    # Читаем СКОМПИЛИРОВАННЫЙ запрос со значениями: без них статусы не видны вовсе,
    # и сторож был бы зелёным при любом наборе.
    guard_sql = [_compiled(st) for st in statements if 'subscriptions.user_id, subscriptions.tariff_id' in str(st)]
    assert guard_sql, 'забор общего аккаунта вообще не спрашивал базу'
    assert 'limited' in guard_sql[0].lower(), guard_sql[0]


@pytest.mark.asyncio
async def test_snapshot_failure_is_explained_and_nothing_is_sent():
    """Без снимка порцию отправлять нельзя — и владелец должен узнать причину."""

    subs = [_sub(1)]
    db = _FakeSession(subs, [_user(1)])
    service, api = _service_with_panel()
    sent: list[dict] = []

    async def _capture(**kwargs):
        sent.append(kwargs)
        return SimpleNamespace(subscription_url='https://vpn/sub/x', happ_crypto_link=None)

    api.update_user = _capture

    async def _boom():
        raise RuntimeError('соединение с базой потеряно')

    db.commit = _boom

    with pytest.raises(ValueError, match='снимок раскатки'):
        await service.propagate_tariff_squads(db, 4, ['new-a'])

    assert sent == [], 'порция не должна была уйти в панель без снимка'


@pytest.mark.asyncio
async def test_restore_never_crosses_into_another_tariff():
    """🔴 Мутация волны 2: снятие границы по тарифу пережило все 2752 теста.

    Без неё «Вернуть» на тарифе 4 молча откатил бы подписки тарифов 5, 6 и 7.
    """

    ours = _sub(1, squads=['new-a'])
    foreign = _sub(2, squads=['new-a'])
    foreign.tariff_id = 99
    mine = _snapshot(1)
    alien = _snapshot(2)
    alien.tariff_id = 99
    db = _FakeSession([ours, foreign], [_user(1), _user(2)], snapshots=[mine, alien])
    service, _ = _service_with_panel()

    result = await service.restore_tariff_squads(db, 4)

    assert result.synced_ids == [1], 'чужой тариф трогать нельзя'
    assert foreign.connected_squads == ['new-a'], 'подписка чужого тарифа не изменилась'
    assert alien.restored_at is None, 'снимок чужого тарифа не должен гаситься'


@pytest.mark.asyncio
async def test_restore_compares_with_the_latest_applied_set_not_the_first():
    """🔴 P1 волны 2: раскатывали дважды — и возврат клеймил подписку «клиент сам сменил».

    Пред-образ берётся из самого раннего снимка, но текущее состояние подписки —
    результат ПОСЛЕДНЕЙ раскатки. Сверка с первой давала ложное клеймо, снимок не
    гасился, и подписка застревала навсегда.
    """

    sub = _sub(1, squads=['v2'])
    first = _snapshot(1, previous=('v0',))
    first.rollout_id, first.applied_squads = 'gen-1', ['v1']
    second = _snapshot(1, previous=('v1',))
    second.id, second.rollout_id, second.applied_squads = 2, 'gen-2', ['v2']
    db = _FakeSession([sub], [_user(1)], snapshots=[first, second])
    service, _ = _service_with_panel()

    result = await service.restore_tariff_squads(db, 4)

    assert result.moved_on_ids == [], 'клиент ничего не менял — клеймо ложное'
    assert result.synced_ids == [1]
    assert sub.connected_squads == ['v0'], 'вернули к истинному «как было», к самому раннему'


@pytest.mark.asyncio
async def test_restore_extinguishes_only_snapshots_of_its_own_tariff():
    """У подписки могут быть снимки ДВУХ тарифов — её переводили между ними.

    Возврат по тарифу 4 не вправе гасить историю тарифа 99: она понадобится, если
    подписку вернут туда. Мутация «снять границу по тарифу в гашении» пережила весь
    набор из 2752 тестов, потому что такого случая никто не строил.
    """

    sub = _sub(1, squads=['new-a'])
    ours = _snapshot(1)
    ours.rollout_id = 'tariff-4-run'
    old_tariff = _snapshot(1, previous=('legacy',))
    old_tariff.id, old_tariff.tariff_id, old_tariff.rollout_id = 2, 99, 'tariff-99-run'
    db = _FakeSession([sub], [_user(1)], snapshots=[ours, old_tariff])
    service, _ = _service_with_panel()

    result = await service.restore_tariff_squads(db, 4)

    assert result.synced_ids == [1]
    assert ours.restored_at is not None, 'свой снимок погашен'
    assert old_tariff.restored_at is None, 'снимок другого тарифа гасить нельзя'
