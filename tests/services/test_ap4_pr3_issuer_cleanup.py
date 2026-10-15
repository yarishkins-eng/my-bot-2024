"""Real SQL interleaving: an expired snapshot must not erase a later personal offer."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import JSON, Column, MetaData, Table, create_engine, insert, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from app.database.models import DiscountOffer, User


class CleanupDB:
    def __init__(self, *, replace=None, source='ordinary'):
        self.engine = create_engine('sqlite://')
        Table(
            'users',
            MetaData(),
            *(
                Column(
                    column.name,
                    JSON() if isinstance(column.type, JSONB) else column.type,
                    primary_key=column.primary_key,
                )
                for column in User.__table__.columns
            ),
        ).create(self.engine)
        self.expired = datetime.now(UTC) - timedelta(hours=1)
        self.replace = replace
        self.commits = 0
        with self.engine.begin() as connection:
            connection.execute(
                insert(User).values(
                    id=41,
                    status='active',
                    promo_offer_discount_percent=10,
                    promo_offer_discount_source=source,
                    promo_offer_discount_expires_at=self.expired,
                )
            )
        self.session = Session(self.engine, autoflush=False)

    async def execute(self, statement):
        result = self.session.execute(statement)
        if self.replace is not None and statement.is_select:
            # The candidate SELECT is consumed before another writer changes the slot.
            rows = result.all()
            self.session.execute(update(User).where(User.id == 41).values(**self.replace))
            self.session.commit()
            self.replace = None
            return SimpleNamespace(
                all=lambda: rows, scalars=lambda: SimpleNamespace(all=lambda: [row[0] for row in rows])
            )
        return result

    async def commit(self):
        self.commits += 1
        self.session.commit()

    async def scalar(self, statement):
        return self.session.scalar(statement)

    async def flush(self):
        self.session.flush()

    async def rollback(self):
        self.session.rollback()

    def slot(self):
        self.session.expire_all()
        return self.session.execute(
            select(
                User.promo_offer_discount_percent,
                User.promo_offer_discount_source,
                User.promo_offer_discount_expires_at,
            )
        ).one()

    def close(self):
        self.session.close()
        self.engine.dispose()


@pytest.mark.parametrize('replacement', ['source', 'percent', 'expiry', 'all', 'null-expiry'])
async def test_stale_cleanup_does_not_clear_any_changed_promo_field(monkeypatch, replacement):
    from app.services import trial_ending_discount_service as service

    future = datetime.now(UTC) + timedelta(days=2)
    changes = {
        'source': {'promo_offer_discount_source': 'trial_ending_discount'},
        'percent': {'promo_offer_discount_percent': 20},
        'expiry': {'promo_offer_discount_expires_at': future},
        'all': {
            'promo_offer_discount_source': 'trial_ending_discount',
            'promo_offer_discount_percent': 20,
            'promo_offer_discount_expires_at': future,
        },
        'null-expiry': {'promo_offer_discount_expires_at': None},
    }[replacement]
    db = CleanupDB(replace=changes)
    log = AsyncMock()
    monkeypatch.setattr(service, 'get_latest_claimed_offer_for_user', AsyncMock(return_value=None))
    monkeypatch.setattr(service, 'log_promo_offer_action', log)
    try:
        assert await service.cleanup_expired_discounts_safely(db) == 0
        percent, source, expiry = db.slot()
        assert percent == changes.get('promo_offer_discount_percent', 10)
        assert source == changes.get('promo_offer_discount_source', 'ordinary')
        expected_expiry = changes.get('promo_offer_discount_expires_at', db.expired)
        assert expiry == expected_expiry
        log.assert_not_awaited()
    finally:
        db.close()


@pytest.mark.parametrize('source', ['ordinary', 'trial_ending_discount', None])
async def test_expired_unchanged_slot_clears_and_logs_only_actual_update(monkeypatch, source):
    from app.services import trial_ending_discount_service as service

    db = CleanupDB(source=source)
    log = AsyncMock()
    offer = SimpleNamespace(id=91, effect_type='percent_discount', discount_percent=10)
    lookup = AsyncMock(return_value=offer)
    monkeypatch.setattr(service, 'get_latest_claimed_offer_for_user', lookup)
    monkeypatch.setattr(service, 'log_promo_offer_action', log)
    try:
        assert await service.cleanup_expired_discounts_safely(db) == 1
        assert db.slot() == (0, None, None)
        assert db.commits == 1
        assert log.await_args.kwargs == {
            'user_id': 41,
            'offer_id': 91 if source else None,
            'action': 'disabled',
            'source': source,
            'percent': 10,
            'effect_type': 'percent_discount' if source else None,
            'details': {'reason': 'offer_expired'},
        }
        assert await service.cleanup_expired_discounts_safely(db) == 0
        assert log.await_count == 1
    finally:
        db.close()


@pytest.mark.parametrize('expiry', [None, 'future'])
async def test_cleanup_keeps_active_and_permanent_offers(monkeypatch, expiry):
    from app.services import trial_ending_discount_service as service

    db = CleanupDB()
    db.session.execute(
        update(User).values(
            promo_offer_discount_expires_at=None if expiry is None else datetime.now(UTC) + timedelta(days=1)
        )
    )
    db.session.commit()
    log = AsyncMock()
    monkeypatch.setattr(service, 'log_promo_offer_action', log)
    try:
        assert await service.cleanup_expired_discounts_safely(db) == 0
        assert db.slot()[0] == 10
        log.assert_not_awaited()
    finally:
        db.close()


@pytest.mark.parametrize('other', ['user', 'source', 'id'])
async def test_delivery_sql_rejects_wrong_offer_identity_and_source(other):
    from app.services import trial_ending_discount_service as service

    db = CleanupDB()
    DiscountOffer.__table__.create(db.engine)
    db.session.execute(
        insert(DiscountOffer).values(
            id=91,
            user_id=41,
            notification_type='ordinary' if other == 'source' else service.TRIAL_ENDING_DISCOUNT_SOURCE,
            discount_percent=20,
            expires_at=datetime.now(UTC) + timedelta(days=1),
            claimed_at=datetime.now(UTC),
            is_active=False,
            extra_data={'carrier': 24},
        )
    )
    db.session.commit()
    try:
        assert not await service.mark_trial_discount_delivered(
            db, offer_id=90 if other == 'id' else 91, user_id=42 if other == 'user' else 41
        )
        assert db.session.get(DiscountOffer, 91).extra_data == {'carrier': 24}
        assert db.commits == 0
    finally:
        db.close()


async def test_delivery_sql_participates_in_outer_rollback():
    from app.services import trial_ending_discount_service as service

    db = CleanupDB()
    DiscountOffer.__table__.create(db.engine)
    db.session.execute(
        insert(DiscountOffer).values(
            id=91,
            user_id=41,
            notification_type=service.TRIAL_ENDING_DISCOUNT_SOURCE,
            discount_percent=20,
            expires_at=datetime.now(UTC) + timedelta(days=1),
            claimed_at=datetime.now(UTC),
            is_active=False,
            extra_data={'carrier': 24},
        )
    )
    db.session.commit()
    try:
        assert await service.mark_trial_discount_delivered(db, offer_id=91, user_id=41)
        assert db.session.get(DiscountOffer, 91).extra_data['delivered_at']
        assert db.commits == 0
        await db.rollback()
        assert db.session.get(DiscountOffer, 91).extra_data == {'carrier': 24}
    finally:
        db.close()
