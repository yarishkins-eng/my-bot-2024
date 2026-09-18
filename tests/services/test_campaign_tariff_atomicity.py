from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.database.models import AdvertisingCampaignRegistration
from app.services import campaign_service as module


class _DB:
    def __init__(
        self,
        scalar_results,
        events: list[str],
        *,
        flush_error: Exception | None = None,
        refresh_error: Exception | None = None,
    ):
        self._scalar_results = iter(scalar_results)
        self._events = events
        self._flush_error = flush_error
        self._refresh_error = refresh_error
        self.added = []

    async def scalar(self, _statement):
        self._events.append('scalar')
        return next(self._scalar_results)

    def add(self, value):
        self._events.append('add-marker')
        self.added.append(value)

    async def flush(self):
        self._events.append('flush')
        if self._flush_error:
            raise self._flush_error

    async def commit(self):
        self._events.append('commit')

    async def rollback(self):
        self._events.append('rollback')

    async def refresh(self, _value):
        self._events.append('refresh-user')
        if self._refresh_error:
            raise self._refresh_error
        if isinstance(_value, _FragileRow):
            object.__setattr__(_value, 'expired', False)


class _FragileRow(SimpleNamespace):
    """Fail if service reads ORM fields after a simulated panel rollback."""

    expired = False

    def __getattribute__(self, name):
        if name not in {'expired', '__class__', '__dict__'} and object.__getattribute__(self, 'expired'):
            raise AssertionError(f'expired ORM field read: {name}')
        return super().__getattribute__(name)


def _campaign():
    return SimpleNamespace(
        id=4,
        is_active=True,
        partner_user_id=None,
        is_balance_bonus=False,
        is_subscription_bonus=False,
        is_none_bonus=False,
        is_tariff_bonus=True,
        tariff_id=7,
        tariff_duration_days=30,
    )


def _user():
    return SimpleNamespace(id=9, telegram_id=99, email=None)


def _tariff():
    return SimpleNamespace(
        id=7,
        name='Campaign',
        is_active=True,
        traffic_limit_gb=10,
        device_limit=2,
    )


def _patch_common(monkeypatch: pytest.MonkeyPatch, *, tariff=None) -> None:
    monkeypatch.setattr(type(module.settings), 'is_multi_tariff_enabled', lambda _settings: False)
    monkeypatch.setattr(module, 'get_subscription_by_user_id', AsyncMock(return_value=None))
    monkeypatch.setattr(module, 'get_tariff_by_id', AsyncMock(return_value=tariff or _tariff()))
    monkeypatch.setattr(
        'app.services.public_location_entitlement_service.resolve_tariff_entitlement',
        AsyncMock(return_value=SimpleNamespace(squad_uuids=['squad'])),
    )


@pytest.mark.asyncio
async def test_tariff_grant_and_marker_commit_before_panel(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    db = _DB([9, None], events)
    subscription = SimpleNamespace(id=31, user_id=9)

    async def create_paid(*_args, **kwargs):
        events.append('grant')
        assert kwargs['commit'] is False
        assert kwargs['is_trial'] is False
        return subscription

    _patch_common(monkeypatch)
    monkeypatch.setattr(module, 'create_paid_subscription', create_paid)
    service = module.AdvertisingCampaignService()

    async def create_panel(_db, candidate):
        assert candidate is subscription
        events.append('panel')
        return object()

    service.subscription_service.create_remnawave_user = create_panel

    result = await service.apply_campaign_bonus(db, _user(), _campaign())

    assert result.success is True
    assert result.is_new_registration is True
    assert events == ['scalar', 'scalar', 'grant', 'add-marker', 'flush', 'commit', 'panel']
    assert len(db.added) == 1
    marker = db.added[0]
    assert isinstance(marker, AdvertisingCampaignRegistration)
    assert (marker.campaign_id, marker.user_id, marker.tariff_id) == (4, 9, 7)
    assert marker.bonus_type == 'tariff'
    assert marker.balance_bonus_kopeks == 0
    assert marker.subscription_duration_days is None
    assert marker.tariff_duration_days == 30


@pytest.mark.asyncio
async def test_existing_marker_skips_grant_and_panel(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    marker = SimpleNamespace(tariff_id=7, tariff_duration_days=30)
    db = _DB([9, marker], events)
    _patch_common(monkeypatch)
    create_paid = AsyncMock()
    monkeypatch.setattr(module, 'create_paid_subscription', create_paid)
    service = module.AdvertisingCampaignService()
    service.subscription_service.create_remnawave_user = AsyncMock()
    service.subscription_service.update_remnawave_user = AsyncMock()

    result = await service.apply_campaign_bonus(db, _user(), _campaign())

    assert result.success is True
    assert result.is_new_registration is False
    assert events == ['scalar', 'scalar']
    create_paid.assert_not_awaited()
    service.subscription_service.create_remnawave_user.assert_not_awaited()
    service.subscription_service.update_remnawave_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_atomic_flush_rolls_back_and_never_calls_panel(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    db = _DB([9, None], events, flush_error=RuntimeError('marker flush failed'))
    subscription = SimpleNamespace(id=31, user_id=9)
    _patch_common(monkeypatch)
    monkeypatch.setattr(module, 'create_paid_subscription', AsyncMock(return_value=subscription))
    service = module.AdvertisingCampaignService()
    service.subscription_service.create_remnawave_user = AsyncMock()

    with pytest.raises(RuntimeError, match='marker flush failed'):
        await service.apply_campaign_bonus(db, _user(), _campaign())

    assert events == ['scalar', 'scalar', 'add-marker', 'flush', 'rollback']
    module.create_paid_subscription.assert_awaited_once()
    assert module.create_paid_subscription.await_args.kwargs['commit'] is False
    service.subscription_service.create_remnawave_user.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('panel_outcome', ['none', 'raise'])
async def test_failed_post_commit_panel_sync_enqueues_retry(
    monkeypatch: pytest.MonkeyPatch,
    panel_outcome: str,
) -> None:
    events: list[str] = []
    db = _DB([9, None], events)
    subscription = _FragileRow(id=31, user_id=9)
    user = _FragileRow(id=9, telegram_id=99, email=None)
    tariff = _FragileRow(
        id=7,
        name='Campaign',
        is_active=True,
        traffic_limit_gb=10,
        device_limit=2,
    )
    _patch_common(monkeypatch, tariff=tariff)
    monkeypatch.setattr(module, 'create_paid_subscription', AsyncMock(return_value=subscription))
    enqueue = MagicMock()
    monkeypatch.setattr('app.services.remnawave_retry_queue.remnawave_retry_queue.enqueue', enqueue)
    service = module.AdvertisingCampaignService()

    async def failed_panel(_db, _subscription):
        events.append('panel')
        object.__setattr__(subscription, 'expired', True)
        object.__setattr__(user, 'expired', True)
        object.__setattr__(tariff, 'expired', True)
        if panel_outcome == 'raise':
            raise RuntimeError('panel unavailable')

    service.subscription_service.create_remnawave_user = failed_panel

    result = await service.apply_campaign_bonus(db, user, _campaign())

    assert result.success is True
    assert result.is_new_registration is True
    assert events == ['scalar', 'scalar', 'add-marker', 'flush', 'commit', 'panel', 'rollback', 'refresh-user']
    enqueue.assert_called_once_with(subscription_id=31, user_id=9, action='create')


@pytest.mark.asyncio
async def test_failed_user_refresh_propagates_after_retry_is_enqueued(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    db = _DB([9, None], events, refresh_error=RuntimeError('refresh failed'))
    subscription = SimpleNamespace(id=31, user_id=9)
    _patch_common(monkeypatch)
    monkeypatch.setattr(module, 'create_paid_subscription', AsyncMock(return_value=subscription))
    enqueue = MagicMock()
    monkeypatch.setattr('app.services.remnawave_retry_queue.remnawave_retry_queue.enqueue', enqueue)
    service = module.AdvertisingCampaignService()
    service.subscription_service.create_remnawave_user = AsyncMock(return_value=None)

    with pytest.raises(RuntimeError, match='refresh failed'):
        await service.apply_campaign_bonus(db, _user(), _campaign())

    enqueue.assert_called_once_with(subscription_id=31, user_id=9, action='create')
    assert events == ['scalar', 'scalar', 'add-marker', 'flush', 'commit', 'rollback', 'refresh-user']


@pytest.mark.asyncio
async def test_retry_queue_failure_does_not_reclassify_durable_grant(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    db = _DB([9, None], events)
    subscription = SimpleNamespace(id=31, user_id=9)
    _patch_common(monkeypatch)
    monkeypatch.setattr(module, 'create_paid_subscription', AsyncMock(return_value=subscription))
    monkeypatch.setattr(
        'app.services.remnawave_retry_queue.remnawave_retry_queue.enqueue',
        MagicMock(side_effect=RuntimeError('queue unavailable')),
    )
    service = module.AdvertisingCampaignService()
    service.subscription_service.create_remnawave_user = AsyncMock(return_value=None)

    result = await service.apply_campaign_bonus(db, _user(), _campaign())

    assert result.success is True
    assert result.is_new_registration is True
    assert events == ['scalar', 'scalar', 'add-marker', 'flush', 'commit', 'rollback', 'refresh-user']
