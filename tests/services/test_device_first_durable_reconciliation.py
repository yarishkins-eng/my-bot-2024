"""Пункт 4.1: сверка заказа обязана ловить подмену подписки, а не прошедшее время.

Слепок снимается при создании заказа и сверяется трижды. Третья сверка стоит **после
оплаты**, и отказ там означает «деньги взяли, подписку не выдали, клиент и тариф заперты».
До этого пункта сравнивались словари целиком, вместе с `updated_at`, который стоит с
`onupdate=func.now()` и прыгает от любой фоновой записи в строку подписки.

Слепки в тестах — настоящей боевой формы, со всеми семью ключами и `updated_at` внутри
(на боевом такие все 15 из 15). Это прямое требование блока «4.1 подробно» плана.
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import device_first_checkout_service as service
from app.services.device_first_checkout_service import (
    ENTITLEMENT_DRIFT_NOTIFICATION_TYPE,
    OWNER_ALERT_NOTIFICATION_TYPE,
    process_device_first_notification_outbox,
)
from app.services.public_location_entitlement_service import ResolvedEntitlement


def live_trial(**overrides):
    """Подписка 134 с боевого: триал тарифа 5, ровно те поля, что попадают в слепок."""
    base = {
        'id': 134,
        'tariff_id': 5,
        'status': 'active',
        'is_trial': True,
        'device_limit': 1,
        'end_date': datetime(2026, 8, 17, 13, 10, 34, tzinfo=UTC),
        'updated_at': datetime(2026, 8, 14, 17, 48, 7, tzinfo=UTC),
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def checkout_stub(**overrides):
    base = {
        'id': 36,
        'public_id': 'checkout-36',
        'user_id': 189,
        'tariff_id': 3,
        'lifecycle_state': 'armed',
        'sale_snapshot': {'tariff_name': 'Базовый'},
    }
    base.update(overrides)
    return SimpleNamespace(**base)


# --- 4.1-A. Что сверка обязана ПРОПУСКАТЬ -------------------------------------------


def test_a_background_write_to_the_subscription_is_not_a_drift():
    """Главный случай пункта. Настоящий боевой слепок + сдвинувшийся `updated_at`.

    Синхронизация трафика (`subscription_service.py:1127`) переписывает строку подписки
    у каждого, кто пользуется VPN, — то есть ровно у того, кто приходит продлевать.
    """
    captured = service._subscription_snapshot(live_trial())
    assert 'updated_at' in captured, 'слепок обязан остаться прежней формы — его читают старые заказы'

    moved = live_trial(updated_at=datetime(2026, 8, 17, 13, 20, 56, tzinfo=UTC))

    assert service._snapshot_identity_drift(captured, moved) == ()


def test_a_trial_expiring_inside_the_payment_window_is_not_a_drift():
    """Монитор ходит раз в час и гасит триал законно. Заплативший не должен это оплачивать.

    Это не выдумка: заказ 36 на боевом создан в 12:41 со слепком `status='active'`,
    а к 13:20 подписка 134 была уже `expired`.
    """
    captured = service._subscription_snapshot(live_trial())
    expired = live_trial(status='expired', updated_at=datetime(2026, 8, 17, 13, 20, 56, tzinfo=UTC))

    assert service._snapshot_identity_drift(captured, expired) == ()


def test_a_shifted_end_date_is_not_a_drift():
    captured = service._subscription_snapshot(live_trial())
    shifted = live_trial(end_date=datetime(2026, 9, 17, tzinfo=UTC))

    assert service._snapshot_identity_drift(captured, shifted) == ()


# --- 4.1-A. Что сверка обязана ПОЙМАТЬ ----------------------------------------------


@pytest.mark.parametrize(
    ('field', 'value', 'expected'),
    [
        ('id', 999, 'id'),
        ('tariff_id', 6, 'tariff_id'),
        ('is_trial', False, 'is_trial'),
    ],
)
def test_identity_change_is_still_a_drift(field, value, expected):
    """Жёстко прописанные имена, а не перебор той же константы, что и код.

    Сторож, ходящий по `_SNAPSHOT_IDENTITY_KEYS`, пропустил бы удаление ключа из набора
    молча — этим уже обжигались на этапе 4.2б.
    """
    captured = service._subscription_snapshot(live_trial())
    changed = live_trial(**{field: value})

    assert service._snapshot_identity_drift(captured, changed) == (expected,)


def test_a_missing_significant_key_counts_as_drift_not_as_match():
    """«Не знаем» — это не «совпало». Иначе сверка выключилась бы сама на старых слепках."""
    captured = service._subscription_snapshot(live_trial())
    del captured['id']

    assert 'id' in service._snapshot_identity_drift(captured, live_trial())


def test_an_empty_snapshot_against_a_live_target_is_a_drift():
    assert service._snapshot_identity_drift({}, live_trial()) == service._SNAPSHOT_IDENTITY_KEYS
    assert service._snapshot_identity_drift(None, live_trial()) == service._SNAPSHOT_IDENTITY_KEYS


def test_a_vanished_target_is_a_drift():
    captured = service._subscription_snapshot(live_trial())

    assert service._snapshot_identity_drift(captured, None) == ('id',)


def test_the_identity_set_never_contains_a_self_moving_field():
    """Сторож на состав набора: вернуть сюда `updated_at` — значит вернуть весь пункт 4.1."""
    assert 'updated_at' not in service._SNAPSHOT_IDENTITY_KEYS
    assert 'status' not in service._SNAPSHOT_IDENTITY_KEYS
    assert 'end_date' not in service._SNAPSHOT_IDENTITY_KEYS
    assert service._SNAPSHOT_IDENTITY_KEYS == ('id', 'tariff_id', 'is_trial')


def test_a_tolerated_change_is_written_to_the_log_not_silently_swallowed():
    """Без этой строки «сверку ослабили» не отличить от «сверка никогда не срабатывала»."""
    captured = service._subscription_snapshot(live_trial())
    expired = live_trial(status='expired')
    checkout = checkout_stub()

    with patch.object(service, '_event') as event:
        drift = service._target_snapshot_drift(checkout, captured, expired, stage='fulfil')

    assert drift == ()
    event.assert_called_once()
    assert event.call_args.kwargs['tolerated'] == 'status'
    assert event.call_args.kwargs['drift'] is None
    assert event.call_args.kwargs['stage'] == 'fulfil'


def test_an_unchanged_snapshot_writes_nothing_at_all():
    captured = service._subscription_snapshot(live_trial())

    with patch.object(service, '_event') as event:
        service._target_snapshot_drift(checkout_stub(), captured, live_trial(), stage='arm')

    event.assert_not_called()


# --- 4.1-Б. Сверка прав: сообщает, но не запрещает ----------------------------------


def _drift_db(*, tariff, resolved, existing_row=None):
    db = MagicMock()
    db.get = AsyncMock(return_value=tariff)
    db.scalar = AsyncMock(return_value=existing_row)
    db.add = MagicMock()
    db.commit = AsyncMock()
    return db


@pytest.mark.asyncio
async def test_a_paid_native_renewal_asks_the_subscription_not_the_tariff():
    """🔴 Ловушка, которая залила бы владельца ложными тревогами в переезд Польши.

    У платной подписки на `native_squads` права берутся из её собственных
    `connected_squads`, а не из `allowed_squads` тарифа. На боевом 105 подписок сидят на
    старых сквадах, а тариф 3 уже переведён на три новых: сравни одно с другим — и
    расхождение отчиталось бы на КАЖДОМ продлении.
    """
    tariff = SimpleNamespace(id=3, entitlement_mode='native_squads')
    target = SimpleNamespace(id=132, is_trial=False)
    from_subscription = AsyncMock(return_value=ResolvedEntitlement((), ('old-squad',), 0, 'native_squads'))
    from_tariff = AsyncMock(return_value=ResolvedEntitlement((), ('new-squad',), 0, 'native_squads'))

    with (
        patch(
            'app.services.public_location_entitlement_service.get_subscription_resolved_entitlement', from_subscription
        ),
        patch('app.services.public_location_entitlement_service.resolve_tariff_entitlement', from_tariff),
    ):
        resolved = await service._resolve_checkout_entitlement(MagicMock(), tariff=tariff, target=target)

    from_subscription.assert_awaited_once()
    from_tariff.assert_not_awaited()
    assert resolved.squad_uuids == ('old-squad',)


@pytest.mark.asyncio
async def test_a_trial_upgrade_asks_the_tariff():
    tariff = SimpleNamespace(id=3, entitlement_mode='native_squads')
    from_subscription = AsyncMock()
    from_tariff = AsyncMock(return_value=ResolvedEntitlement((), ('new-squad',), 0, 'native_squads'))

    with (
        patch(
            'app.services.public_location_entitlement_service.get_subscription_resolved_entitlement', from_subscription
        ),
        patch('app.services.public_location_entitlement_service.resolve_tariff_entitlement', from_tariff),
    ):
        await service._resolve_checkout_entitlement(
            MagicMock(), tariff=tariff, target=SimpleNamespace(id=1, is_trial=True)
        )

    from_tariff.assert_awaited_once()
    from_subscription.assert_not_awaited()


@pytest.mark.asyncio
async def test_drift_queues_one_owner_row_and_never_touches_the_order():
    """Заказ обязан остаться выданным: запрет здесь = деньги взяты, подписки нет."""
    captured = ResolvedEntitlement((), ('old-squad',), 0, 'native_squads')
    current = ResolvedEntitlement((), ('new-squad',), 0, 'native_squads')
    checkout = checkout_stub(lifecycle_state='fulfilling')
    db = _drift_db(tariff=SimpleNamespace(id=3, entitlement_mode='native_squads'), resolved=current)

    with patch.object(service, '_resolve_checkout_entitlement', AsyncMock(return_value=current)):
        await service._report_entitlement_drift_without_blocking(db, checkout=checkout, target=None, captured=captured)

    db.add.assert_called_once()
    queued = db.add.call_args.args[0]
    assert queued.notification_type == ENTITLEMENT_DRIFT_NOTIFICATION_TYPE
    assert queued.checkout_id == checkout.id
    # Состояние заказа не тронуто ничем.
    assert checkout.lifecycle_state == 'fulfilling'
    assert not hasattr(checkout, 'terminal_reason')


@pytest.mark.asyncio
async def test_no_row_when_the_entitlement_did_not_move():
    same = ResolvedEntitlement((), ('squad-1',), 0, 'native_squads')
    db = _drift_db(tariff=SimpleNamespace(id=3, entitlement_mode='native_squads'), resolved=same)

    with patch.object(service, '_resolve_checkout_entitlement', AsyncMock(return_value=same)):
        await service._report_entitlement_drift_without_blocking(
            db, checkout=checkout_stub(), target=None, captured=same
        )

    db.add.assert_not_called()


@pytest.mark.asyncio
async def test_a_second_pass_does_not_queue_a_duplicate_row():
    """Уникальный индекс здесь — страховка, а не механизм: IntegrityError отменил бы продажу."""
    captured = ResolvedEntitlement((), ('old-squad',), 0, 'native_squads')
    current = ResolvedEntitlement((), ('new-squad',), 0, 'native_squads')
    db = _drift_db(tariff=SimpleNamespace(id=3, entitlement_mode='native_squads'), resolved=current, existing_row=7)

    with patch.object(service, '_resolve_checkout_entitlement', AsyncMock(return_value=current)):
        await service._report_entitlement_drift_without_blocking(
            db, checkout=checkout_stub(), target=None, captured=captured
        )

    db.add.assert_not_called()


@pytest.mark.asyncio
async def test_the_drift_check_can_never_break_the_sale():
    """Она стоит ВНУТРИ денежной транзакции. Её собственная поломка не имеет права на отказ."""
    captured = ResolvedEntitlement((), ('old-squad',), 0, 'native_squads')
    db = _drift_db(tariff=SimpleNamespace(id=3, entitlement_mode='native_squads'), resolved=None)

    with patch.object(service, '_resolve_checkout_entitlement', AsyncMock(side_effect=RuntimeError('panel down'))):
        await service._report_entitlement_drift_without_blocking(
            db, checkout=checkout_stub(), target=None, captured=captured
        )

    db.add.assert_not_called()


@pytest.mark.asyncio
async def test_a_missing_tariff_is_silent_too():
    captured = ResolvedEntitlement((), ('old-squad',), 0, 'native_squads')
    db = _drift_db(tariff=None, resolved=None)

    await service._report_entitlement_drift_without_blocking(
        db, checkout=checkout_stub(), target=None, captured=captured
    )

    db.add.assert_not_called()


# --- 4.1-Б. Доставка: строка владельцу не должна уйти клиенту ------------------------


def _outbox_row(row_id, notification_type):
    return SimpleNamespace(
        id=row_id,
        checkout_id=36,
        notification_type=notification_type,
        status='pending',
        lease_token=None,
        lease_expires_at=None,
        sending_at=None,
        sent_at=None,
        last_error=None,
    )


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return SimpleNamespace(all=lambda: list(self._rows))

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None


def _worker_db(rows, *, checkout, user):
    db = MagicMock()
    results = [_Result(rows)]
    for row in rows:
        results.append(_Result([checkout]))
        results.append(_Result([row]))
    db.execute = AsyncMock(side_effect=results)
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    db.add = MagicMock()
    db.scalar = AsyncMock(return_value=0)

    async def _get(model, key):
        if model.__name__ == 'User':
            return user
        if model.__name__ == 'DeviceFirstNotificationOutbox':
            return next((row for row in rows if row.id == key), None)
        return None

    db.get = AsyncMock(side_effect=_get)
    return db


@pytest.mark.asyncio
async def test_the_drift_row_goes_to_the_owner_and_never_to_the_client():
    """🔴 Мутация «убрать ветку из диспетчера» обязана ронять этот тест.

    Без ветки строка проваливается в `else` и клиент получает ВТОРОЕ «✅ Подписка готова»
    по тому же заказу — про изменение, которое его вообще не касается.
    """
    row = _outbox_row(1, ENTITLEMENT_DRIFT_NOTIFICATION_TYPE)
    user = SimpleNamespace(id=189, telegram_id=123456, username='k', language='ru', full_name='Клиент')
    db = _worker_db([row], checkout=checkout_stub(lifecycle_state='ready'), user=user)
    bot = MagicMock()
    bot.send_message = AsyncMock()
    admin = MagicMock()
    admin.is_enabled = True
    admin.send_admin_notification = AsyncMock(return_value=True)

    with (
        patch.object(service, 'queue_owner_order_stuck_alerts', AsyncMock(return_value=0)),
        patch.object(service, 'revive_stale_notifications', AsyncMock(return_value=(0, 0))),
        patch('app.services.admin_notification_service.AdminNotificationService', return_value=admin),
    ):
        sent = await process_device_first_notification_outbox(db, bot=bot, limit=10)

    assert sent == 1
    bot.send_message.assert_not_awaited()
    text = admin.send_admin_notification.await_args.args[0]
    assert 'Подписка готова' not in text
    assert 'ЗАКАЗ ЗАВИС' not in text, 'это не авария — заказ выдан'
    assert 'выдана' in text
    assert row.status == 'sent'


@pytest.mark.asyncio
async def test_the_drift_alert_survives_a_ready_order():
    """`_owner_alert_is_obsolete` гасит тревоги на `ready`. Эта строка ВСЕГДА про `ready`."""
    row = _outbox_row(1, ENTITLEMENT_DRIFT_NOTIFICATION_TYPE)
    user = SimpleNamespace(id=189, telegram_id=123456, username='k', language='ru', full_name='Клиент')
    db = _worker_db([row], checkout=checkout_stub(lifecycle_state='ready'), user=user)
    admin = MagicMock()
    admin.is_enabled = True
    admin.send_admin_notification = AsyncMock(return_value=True)

    with (
        patch.object(service, 'queue_owner_order_stuck_alerts', AsyncMock(return_value=0)),
        patch.object(service, 'revive_stale_notifications', AsyncMock(return_value=(0, 0))),
        patch('app.services.admin_notification_service.AdminNotificationService', return_value=admin),
    ):
        await process_device_first_notification_outbox(db, bot=MagicMock(send_message=AsyncMock()), limit=10)

    assert row.status == 'sent', 'строка не должна гаситься как устаревшая'
    admin.send_admin_notification.assert_awaited_once()


def test_the_two_owner_notification_types_stay_distinct():
    assert ENTITLEMENT_DRIFT_NOTIFICATION_TYPE != OWNER_ALERT_NOTIFICATION_TYPE
    assert len(ENTITLEMENT_DRIFT_NOTIFICATION_TYPE) <= 48, 'колонка notification_type — String(48)'
