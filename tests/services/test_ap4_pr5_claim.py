"""Exercise all three real claim entry points; refresh must precede the guard."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

from app.cabinet.routes import promo as cabinet
from app.handlers.subscription import promo as bot
from app.utils import promo_offer as helpers
from app.webapi.routes import miniapp


FUTURE = datetime(2031, 10, 19, 21, 17, tzinfo=UTC)
PAST = datetime(2001, 1, 1, tzinfo=UTC)


class LockedDB:
    def __init__(self, current):
        self.current = current
        self.events = []
        self.commit = AsyncMock()

    async def execute(self, query):
        assert query._for_update_arg is not None
        assert 'FOR UPDATE' in str(query.compile(dialect=postgresql.dialect()))
        self.events.append('lock')

    async def refresh(self, user, *args, attribute_names=None):
        if attribute_names is not None:
            assert self.events == ['lock']
            assert set(attribute_names) >= {'promo_offer_discount_percent', 'promo_offer_discount_expires_at'}
            self.events.append('refresh')
            user.promo_offer_discount_percent, user.promo_offer_discount_expires_at = self.current


def setup(monkeypatch, path, current, language='ru', effect='percent_discount'):
    user = SimpleNamespace(
        id=7,
        language=language,
        promo_offer_discount_percent=77,
        promo_offer_discount_expires_at=FUTURE,
        promo_offer_discount_source='trial_ending_discount',
        has_had_paid_subscription=True,
        updated_at=None,
        subscription=None,
    )
    offer = SimpleNamespace(
        id=404,
        user_id=7,
        claimed_at=None,
        is_active=True,
        expires_at=FUTURE,
        effect_type=effect,
        discount_percent=10,
        bonus_amount_kopeks=0,
        notification_type='expired_discount_wave2',
        extra_data={'active_discount_hours': 24},
    )
    module = {'bot': bot, 'cabinet': cabinet, 'miniapp': miniapp}[path]
    marked = AsyncMock()
    monkeypatch.setattr(module, 'get_offer_by_id', AsyncMock(return_value=offer))
    monkeypatch.setattr(module, 'mark_offer_claimed', marked)
    monkeypatch.setattr(miniapp, 'parse_webapp_init_data', lambda *_: {'user': {'id': 7000}})
    monkeypatch.setattr(miniapp, 'get_user_by_telegram_id', AsyncMock(return_value=user))
    monkeypatch.setattr(
        module.promo_offer_service, 'grant_test_access', AsyncMock(return_value=(False, [], None, 'squads_missing'))
    )
    callback = SimpleNamespace(
        data='claim_discount_404', answer=AsyncMock(), message=SimpleNamespace(answer=AsyncMock())
    )
    db = LockedDB(current)

    async def run():
        if path == 'bot':
            return await bot.claim_discount_offer(callback, user, db)
        if path == 'cabinet':
            return await cabinet.claim_promo_offer(SimpleNamespace(offer_id=404), user, db)
        return await miniapp.claim_promo_offer(404, SimpleNamespace(init_data='test'), db)

    return user, offer, db, callback, marked, run


@pytest.mark.asyncio
@pytest.mark.parametrize('path', ['bot', 'cabinet', 'miniapp'])
@pytest.mark.parametrize('language', ['ru', 'en'])
@pytest.mark.parametrize('percent,expires', [(5, FUTURE), (20, FUTURE), (40, FUTURE), (20, None)])
async def test_active_slot_wins_under_lock_without_consuming_offer(monkeypatch, path, language, percent, expires):
    user, offer, db, callback, marked, run = setup(monkeypatch, path, (percent, expires), language)
    until = '20.10.2031, 00:17'
    if language == 'ru':
        expected = f'У вас уже действует скидка −{percent}%' + (f' до {until} (МСК).' if expires else '.')
    else:
        expected = f'You already have a −{percent}% discount' + (f' until {until} (MSK).' if expires else '.')
    if path == 'bot':
        await run()
        callback.answer.assert_awaited_once_with(expected, show_alert=True)
        callback.message.answer.assert_not_awaited()
    else:
        with pytest.raises(HTTPException) as error:
            await run()
        assert error.value.status_code == 409
        assert error.value.detail == (
            expected if path == 'cabinet' else {'code': 'active_discount_exists', 'message': expected}
        )
    assert db.events == ['lock', 'refresh']
    marked.assert_not_awaited()
    db.commit.assert_not_awaited()
    assert user.promo_offer_discount_percent == percent and user.promo_offer_discount_expires_at == expires
    assert user.promo_offer_discount_source == 'trial_ending_discount'
    assert offer.is_active and offer.claimed_at is None


@pytest.mark.asyncio
@pytest.mark.parametrize('path', ['bot', 'cabinet', 'miniapp'])
@pytest.mark.parametrize('current', [(0, None), (20, PAST)])
async def test_empty_or_expired_slot_is_claimed_after_fresh_read(monkeypatch, path, current):
    user, _, db, callback, marked, run = setup(monkeypatch, path, current)
    result = await run()
    assert db.events == ['lock', 'refresh']
    marked.assert_awaited_once()
    assert user.promo_offer_discount_percent == 10
    assert user.promo_offer_discount_source == 'expired_discount_wave2'
    assert user.promo_offer_discount_expires_at > datetime.now(UTC)
    if path == 'bot':
        callback.message.answer.assert_awaited_once()
    else:
        assert result.success is True


@pytest.mark.asyncio
@pytest.mark.parametrize('path', ['bot', 'cabinet', 'miniapp'])
async def test_test_access_never_takes_the_new_discount_lock(monkeypatch, path):
    _, _, db, callback, marked, run = setup(monkeypatch, path, (20, FUTURE), effect='test_access')
    if path == 'bot':
        await run()
        callback.answer.assert_awaited_once()
    else:
        with pytest.raises(HTTPException):
            await run()
    assert db.events == []
    marked.assert_not_awaited()


def test_expiry_boundary_and_any_source_are_independent_of_price_eligibility(monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return FUTURE

    monkeypatch.setattr(helpers, 'datetime', Clock)
    for source in ['trial_ending_discount', 'promocode', 'legacy']:
        user = SimpleNamespace(
            promo_offer_discount_percent=20,
            promo_offer_discount_expires_at=FUTURE,
            promo_offer_discount_source=source,
            has_had_paid_subscription=True,
        )
        assert helpers.has_active_personal_discount(user) is False
        user.promo_offer_discount_expires_at = FUTURE + timedelta(seconds=1)
        assert helpers.has_active_personal_discount(user) is True
