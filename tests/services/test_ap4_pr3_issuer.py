"""Issuer decisions and ownership/freshness boundaries; PostgreSQL lock proof is a separate profile."""

from copy import deepcopy
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy.dialects import postgresql

from app.database.models import DiscountOffer, SentNotification, Subscription, User
from app.services import trial_ending_discount_service as service


class Transaction:
    def __init__(self, db, *, nested=False):
        self.db, self.nested = db, nested

    async def __aenter__(self):
        self.previous = deepcopy(self.db.user.__dict__), self.db.offers.copy()

    async def __aexit__(self, kind, *_args):
        if kind is not None:
            self.db.user.__dict__.clear()
            self.db.user.__dict__.update(self.previous[0])
            self.db.offers[:] = self.previous[1]
            self.db.rollbacks += 1
        elif not self.nested:
            self.db.commits += 1


class IssueDB:
    def __init__(self):
        self.user = SimpleNamespace(
            id=41,
            telegram_id=101,
            status='active',
            has_had_paid_subscription=False,
            restriction_subscription=False,
            account_erasure_requested_at=None,
            account_erased_at=None,
            test_account_enabled=True,
            test_reset_state='ready',
            test_reset_started_at=None,
            remnawave_uuid='current-panel',
            promo_offer_discount_percent=0,
            promo_offer_discount_source=None,
            promo_offer_discount_expires_at=None,
        )
        self.subscription = SimpleNamespace(
            id=71,
            user_id=41,
            is_trial=True,
            tariff_id=5,
            status='active',
            end_date=datetime.now(UTC) + timedelta(hours=23, minutes=59),
            remnawave_uuid='current-panel',
        )
        self.offers, self.queries = [], []
        self.current_id = 71
        self.sent_id = None
        self.commits = self.rollbacks = self.flushes = 0
        self.after_flush = lambda: None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        pass

    def begin(self):
        return Transaction(self)

    def begin_nested(self):
        return Transaction(self, nested=True)

    async def scalar(self, statement):
        self.queries.append(statement)
        description = statement.column_descriptions[0]
        entity = description['entity']
        if entity is User:
            return self.user
        if entity is Subscription:
            return self.subscription if description['name'] == 'Subscription' else self.current_id
        if entity is SentNotification:
            return self.sent_id
        if entity is DiscountOffer:
            return (
                self.offers[0]
                if self.offers and description['name'] == 'DiscountOffer'
                else (19 if self.offers else None)
            )
        raise AssertionError(str(statement))

    def add(self, offer):
        offer.id = 19
        self.offers.append(offer)

    async def flush(self):
        self.flushes += 1
        self.after_flush()


@pytest.fixture
def setup(monkeypatch):
    db = IssueDB()
    config = dict(enabled=True, carrier=24, discount_percent=20, audience=1, post_trial_hours=6)
    monkeypatch.setattr(
        service.NotificationSettingsService, 'get_trial_ending_discount_settings', lambda: config.copy(), raising=False
    )
    monkeypatch.setattr(service.NotificationSettingsService, 'get_trial_warn_hours', lambda: 6)
    monkeypatch.setattr(service.NotificationSettingsService, 'is_enabled', lambda _key: True)
    monkeypatch.setattr(service.NotificationSettingsService, 'are_notifications_globally_enabled', lambda: True)
    monkeypatch.setattr(service.settings, 'WEBHOOK_NOTIFY_USER_ENABLED', True)
    monkeypatch.setattr(service.settings, 'WEBHOOK_NOTIFY_SUB_EXPIRING', True)

    def factory():
        return db

    monkeypatch.setattr(service, 'AsyncSessionLocal', factory)
    monkeypatch.setattr(service, 'get_trial_tariff', AsyncMock(return_value=SimpleNamespace(id=5)))
    seen = []

    async def options(_db, user, *, subscription):
        assert subscription is db.subscription
        seen.append(
            (user.promo_offer_discount_percent, user.promo_offer_discount_source, user.promo_offer_discount_expires_at)
        )
        return {'eligible': True, 'price_matrix': [{'prices': [{'price_kopeks': 11900}]}]}

    monkeypatch.setattr(service, 'build_purchase_options', options)
    args = dict(
        user_id=41,
        subscription_id=71,
        carrier=24,
        panel_uuid='current-panel',
        panel_end=db.subscription.end_date,
        lifetime_bytes=1_000_000,
        reset_started_at=None,
    )
    return db, config, args, seen


async def test_offer_is_claimed_once_with_own_committed_term_and_precise_frozen_result(setup):
    db, _config, args, seen = setup
    result = await service.prepare_trial_ending_discount(**args)
    assert result is not None
    assert (result.offer_id, result.user_id, result.subscription_id, result.lifetime_bytes) == (19, 41, 71, 1_000_000)
    assert result.panel_uuid == 'current-panel' and result.panel_end == db.subscription.end_date
    assert result.expires_at >= result.panel_end + timedelta(hours=6)
    offer = db.offers[0]
    assert offer.notification_type == service.TRIAL_ENDING_DISCOUNT_SOURCE
    assert offer.claimed_at is not None and offer.is_active is False and offer.effect_type == 'percent_discount'
    assert offer.expires_at == result.expires_at and offer.extra_data['carrier'] == 24
    assert 'delivered_at' not in offer.extra_data
    assert seen[0] == (0, None, None)
    assert seen[1] == (20, service.TRIAL_ENDING_DISCOUNT_SOURCE, result.expires_at)
    assert db.commits == 1 and db.user.promo_offer_discount_percent == 20
    assert service.validate_prepared(result, db.user, db.subscription)
    with pytest.raises(FrozenInstanceError):
        result.expires_at = result.expires_at + timedelta(days=1)
    expiry = offer.expires_at
    assert await service.prepare_trial_ending_discount(**args) is None
    assert len(db.offers) == 1 and offer.expires_at == expiry
    statements = [str(query.compile(dialect=postgresql.dialect())) for query in db.queries]
    assert 'FOR UPDATE' in statements[0] and 'FOR UPDATE' in statements[1]


@pytest.mark.parametrize('value', [None, True, '1000000', 1_000_000.0, -1, 999_999])
async def test_unknown_or_unused_panel_evidence_never_enters_transaction(setup, value):
    db, _config, args, _seen = setup
    args['lifetime_bytes'] = value
    assert await service.prepare_trial_ending_discount(**args) is None
    assert db.queries == [] and db.offers == []


@pytest.mark.parametrize(
    'gate,reason',
    [
        ('traffic', 'unknown_or_unused_panel_traffic'),
        ('rollout', 'rollout'),
        ('paid', 'person_ineligible'),
        ('current', 'not_current_subscription'),
        ('offer', 'personal_offer_exists'),
        ('sent', 'carrier_already_sent'),
    ],
)
async def test_each_rejection_has_exactly_one_info_reason(setup, monkeypatch, gate, reason):
    db, _config, args, _seen = setup
    if gate == 'traffic':
        args['lifetime_bytes'] = None
    if gate == 'rollout':
        monkeypatch.setattr(service, 'TRIAL_ENDING_DISCOUNT_ROLLOUT', 'off')
    if gate == 'paid':
        db.user.has_had_paid_subscription = True
    if gate == 'current':
        db.current_id = 99
    if gate == 'offer':
        db.user.promo_offer_discount_percent = 10
    if gate == 'sent':
        db.sent_id = 1
    log = Mock()
    monkeypatch.setattr(service.logger, 'info', log)
    assert await service.prepare_trial_ending_discount(**args) is None
    log.assert_called_once_with('trial_ending_discount_skipped', user_id=args['user_id'], reason=reason)


@pytest.mark.parametrize(
    ('field', 'value'),
    [
        ('status', 'deleted'),
        ('telegram_id', None),
        ('has_had_paid_subscription', True),
        ('restriction_subscription', True),
        ('account_erasure_requested_at', datetime.now(UTC)),
        ('account_erased_at', datetime.now(UTC)),
        ('test_reset_state', 'resetting'),
        ('test_reset_state', 'failed'),
        ('test_reset_started_at', datetime.now(UTC)),
    ],
)
async def test_fresh_user_gates_fail_closed(setup, field, value):
    db, _config, args, seen = setup
    setattr(db.user, field, value)
    assert await service.prepare_trial_ending_discount(**args) is None
    assert not db.offers and seen == []


@pytest.mark.parametrize(
    ('field', 'value'),
    [('user_id', 99), ('is_trial', False), ('tariff_id', 4), ('status', 'expired'), ('remnawave_uuid', 'retired')],
)
async def test_fresh_subscription_must_be_canonical_and_bound(setup, field, value):
    db, _config, args, seen = setup
    setattr(db.subscription, field, value)
    assert await service.prepare_trial_ending_discount(**args) is None
    assert not db.offers and seen == []


@pytest.mark.parametrize('rollout', ['off', 'ALL', 'stand', 'typo', ''])
async def test_unknown_rollout_never_issues(setup, monkeypatch, rollout):
    db, _config, args, seen = setup
    monkeypatch.setattr(service, 'TRIAL_ENDING_DISCOUNT_ROLLOUT', rollout)
    assert await service.prepare_trial_ending_discount(**args) is None
    assert not db.offers and not seen


@pytest.mark.parametrize('expiry', [None, 'future'])
async def test_other_active_offer_including_permanent_is_preserved(setup, expiry):
    db, _config, args, seen = setup
    db.user.promo_offer_discount_percent = 5
    db.user.promo_offer_discount_source = 'other'
    db.user.promo_offer_discount_expires_at = None if expiry is None else datetime.now(UTC) + timedelta(days=1)
    previous = db.user.__dict__.copy()
    assert await service.prepare_trial_ending_discount(**args) is None
    assert db.user.__dict__ == previous and not db.offers and not seen


async def test_any_historical_new_source_row_blocks_second_issue(setup):
    db, _config, args, seen = setup
    db.offers.append(
        SimpleNamespace(id=19, claimed_at=None, is_active=False, expires_at=datetime.now(UTC) - timedelta(days=2))
    )
    assert await service.prepare_trial_ending_discount(**args) is None
    assert len(db.offers) == 1 and not seen


async def test_requested_old_trial_cannot_replace_current_other_subscription(setup):
    db, _config, args, seen = setup
    db.current_id = 99
    assert await service.prepare_trial_ending_discount(**args) is None
    assert not db.offers and not seen
    query = str(db.queries[-1].compile(dialect=postgresql.dialect()))
    assert 'CASE WHEN' in query and 'end_date DESC NULLS LAST' in query and 'created_at DESC' in query


async def test_already_sent_carrier_blocks_issue_with_term_scoped_query(setup):
    db, _config, args, seen = setup
    db.sent_id = 17
    assert await service.prepare_trial_ending_discount(**args) is None
    assert not db.offers and not seen
    query = str(db.queries[-1].compile(dialect=postgresql.dialect()))
    assert 'sent_notifications.created_at >=' in query and 'sent_notifications.subscription_id =' in query


@pytest.mark.parametrize('stage', [1, 2, 3])
async def test_settings_change_after_each_await_rolls_back_issue(setup, monkeypatch, stage):
    db, config, args, _seen = setup
    original = service.build_purchase_options
    calls = 0

    async def changing_options(*params, **kwargs):
        nonlocal calls
        calls += 1
        result = await original(*params, **kwargs)
        if calls == stage:
            config['discount_percent'] = 21
        return result

    monkeypatch.setattr(service, 'build_purchase_options', changing_options)
    if stage == 3:
        db.after_flush = lambda: config.update(enabled=False)
    assert await service.prepare_trial_ending_discount(**args) is None
    assert db.offers == [] and db.user.promo_offer_discount_percent == 0
    assert db.user.promo_offer_discount_source is None and db.user.promo_offer_discount_expires_at is None


@pytest.mark.parametrize('candidate', [False, True])
async def test_zero_or_ineligible_price_never_publishes_discount(setup, monkeypatch, candidate):
    db, _config, args, _seen = setup
    original = service.build_purchase_options

    async def zero_options(*params, **kwargs):
        result = await original(*params, **kwargs)
        if bool(db.user.promo_offer_discount_percent) is candidate:
            result['price_matrix'][0]['prices'][0]['price_kopeks'] = 0
        return result

    monkeypatch.setattr(service, 'build_purchase_options', zero_options)
    assert await service.prepare_trial_ending_discount(**args) is None
    assert not db.offers and db.user.promo_offer_discount_percent == 0


@pytest.mark.parametrize('carrier', [0, 24, 48])
async def test_all_three_carriers_and_switches_bind_to_real_window(setup, carrier):
    db, config, args, _seen = setup
    config['carrier'] = args['carrier'] = carrier
    db.subscription.end_date = args['panel_end'] = (
        datetime.now(UTC) + timedelta(hours=carrier or 3) - timedelta(minutes=1)
    )
    assert await service.prepare_trial_ending_discount(**args) is not None


async def test_all_audience_and_owner_stand_override(setup):
    db, config, args, _seen = setup
    config['audience'] = 0
    args['lifetime_bytes'] = None
    db.user.test_account_enabled = False
    assert await service.prepare_trial_ending_discount(**args) is None
    db.user.test_account_enabled = True
    result = await service.prepare_trial_ending_discount(**args)
    assert result is not None and result.lifetime_bytes is None


@pytest.mark.parametrize('hour', [0, 5, 8, 9, 23])
def test_expiry_moves_only_moscow_night_to_09(hour):
    moscow = service._MOSCOW
    raw = datetime(2026, 10, 17, hour, 15, tzinfo=moscow)
    expiry = service._discount_expiry(raw - timedelta(hours=6), 6).astimezone(moscow)
    assert expiry == (raw.replace(hour=9, minute=0) if hour < 9 else raw)


@pytest.mark.parametrize('change', ['paid', 'reset', 'uuid', 'term', 'source', 'percent', 'expiry'])
async def test_prepared_result_cannot_cross_lifecycle_or_offer_change(setup, change):
    db, _config, args, _seen = setup
    result = await service.prepare_trial_ending_discount(**args)
    assert service.validate_prepared(result, db.user, db.subscription)
    if change == 'paid':
        db.user.has_had_paid_subscription = True
    elif change == 'reset':
        db.user.test_reset_started_at = datetime.now(UTC)
    elif change == 'uuid':
        db.subscription.remnawave_uuid = 'new-generation'
    elif change == 'term':
        db.subscription.end_date += timedelta(seconds=1)
    elif change == 'source':
        db.user.promo_offer_discount_source = 'other'
    elif change == 'percent':
        db.user.promo_offer_discount_percent = 21
    else:
        db.user.promo_offer_discount_expires_at += timedelta(seconds=1)
    assert not service.validate_prepared(result, db.user, db.subscription)


async def test_first_delivery_merges_metadata_without_committing_or_extending(setup):
    db, _config, args, _seen = setup
    result = await service.prepare_trial_ending_discount(**args)
    previous = db.offers[0].extra_data.copy()
    commits = db.commits
    assert await service.mark_trial_discount_delivered(db, offer_id=result.offer_id, user_id=result.user_id)
    first = db.offers[0].extra_data['delivered_at']
    assert all(db.offers[0].extra_data[key] == value for key, value in previous.items())
    assert not await service.mark_trial_discount_delivered(db, offer_id=result.offer_id, user_id=result.user_id)
    assert db.offers[0].extra_data['delivered_at'] == first
    assert db.commits == commits and db.offers[0].expires_at == result.expires_at
    query = db.queries[-1].compile(dialect=postgresql.dialect())
    assert query.params['user_id_1'] == result.user_id and query.params['id_1'] == result.offer_id
