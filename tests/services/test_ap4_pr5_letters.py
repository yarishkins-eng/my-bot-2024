"""Real formatter, monitor and card paths for the follow-up discount line."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.cabinet.routes import admin_auto_messages as api
from app.localization.texts import get_texts
from app.services import monitoring_service as m, trial_ending_discount_service as issuer
from app.services.notification_settings_service import NotificationSettingsService as Settings
from tests.cabinet.test_ap4_pr4_prices import price_env  # noqa: F401
from tests.services.test_ap4_pr3_issuer import setup  # noqa: F401
from tests.services.test_ap4_pr3_letter import options


class PriceDB:
    def __init__(self, extra):
        self.extra = extra
        self.query = None

    @asynccontextmanager
    async def begin_nested(self):
        yield

    async def execute(self, query):
        assert [column.key for column in query.selected_columns] == ['id', 'extra_data']
        self.query = query
        return SimpleNamespace(first=lambda: (91, self.extra))


@pytest.mark.asyncio
@pytest.mark.parametrize('delivered', [False, True])
@pytest.mark.parametrize('claim', [False, True])
@pytest.mark.parametrize('language', ['ru', 'en'])
async def test_real_price_lines_preserve_metadata_claim_and_exact_short_copy(monkeypatch, delivered, claim, language):
    data = options(percent=20, end='2031-10-19T21:17:00+00:00')
    # A customer's promo group changed the pre-personal price; never print a fixed149.
    cell = data['price_matrix'][0]['prices'][0]
    cell['price_kopeks'] = 9600
    cell['breakdown']['promo_offer_discount_kopeks'] = 2400
    user = SimpleNamespace(
        id=7,
        balance_kopeks=5000,
        language=language,
        promo_offer_discount_source='trial_ending_discount',
        promo_offer_discount_percent=20,
        promo_offer_discount_expires_at=datetime(2031, 10, 19, 21, 17, tzinfo=UTC),
    )
    db = PriceDB({'delivered_at': 'earlier'} if delivered else {})
    monkeypatch.setattr(
        'app.services.device_first_checkout_service.build_purchase_options', AsyncMock(return_value=data)
    )
    logged = Mock()
    monkeypatch.setattr(m.logger, 'info', logged)
    monitor = m.MonitoringService.__new__(m.MonitoringService)
    text = await monitor._trial_price_lines(db, user, claim=claim)
    assert isinstance(text, m.TrialDiscountPriceLines) and text.offer_id == 91
    assert db.query is not None
    short = delivered and not claim
    logged.assert_called_once_with('trial_discount_lines', variant='short' if short else 'full', offer_id=91)
    if short:
        expected = (
            '🎁 Скидка −20% ещё действует до 20.10.2031, 00:17 (МСК): 1 месяц — 96 ₽ вместо <s>120 ₽</s>.'
            if language == 'ru'
            else '🎁 Your −20% discount is still valid until 20.10.2031, 00:17 (MSK): 1 month — 96 ₽ instead of <s>120 ₽</s>.'
        )
        assert text == expected
        assert '50 ₽' not in text and '149 ₽' not in text and '\n' not in text
        assert text.count('20%') == text.count('20.10.2031') == text.count('96 ₽') == text.count('120 ₽') == 1
    else:
        assert '3 месяца' in text if language == 'ru' else '3 months' in text
        assert get_texts(language).t('TRIAL_DISCOUNT_ANY_SELECTION') in text


@pytest.mark.asyncio
async def test_other_source_never_reads_delivery_or_gets_short_line(monkeypatch):
    data = options()
    user = SimpleNamespace(id=1, language='ru', balance_kopeks=0, promo_offer_discount_source='legacy')
    db = PriceDB({'delivered_at': 'not this source'})
    monkeypatch.setattr(
        'app.services.device_first_checkout_service.build_purchase_options', AsyncMock(return_value=data)
    )
    text = await m.MonitoringService.__new__(m.MonitoringService)._trial_price_lines(db, user)
    assert isinstance(text, m.TrialDiscountPriceLines) and text.offer_id is None
    assert '3 месяца' in text and 'ещё действует' not in text and db.query is None


@pytest.mark.parametrize('saving', [0, -1, None])
def test_nonpositive_applied_benefit_cannot_build_short_line(saving):
    data = options()
    data['price_matrix'][0]['prices'][0]['breakdown']['promo_offer_discount_kopeks'] = saving
    assert m.format_trial_discount_lines(data, SimpleNamespace(balance_kopeks=0), get_texts('ru'), short=True) == ''


@pytest.mark.asyncio
@pytest.mark.parametrize('carrier', [0, 24, 48])
@pytest.mark.parametrize('enabled', [False, True])
async def test_real_cards_add_the_shared_short_variant_only_after_enabled_carrier(
    price_env, monkeypatch, carrier, enabled
):
    assert Settings.set_trial_ending_discount_carrier(carrier)
    assert Settings.set_trial_ending_discount_enabled(enabled)
    monkeypatch.setattr(api, '_quiet_facts', AsyncMock(return_value=({}, {})))
    monkeypatch.setattr(api, '_sent_counts', AsyncMock(return_value={}))
    monkeypatch.setattr(api, '_claimed_counts', AsyncMock(return_value={}))
    monkeypatch.setattr(api, '_history_for', AsyncMock(return_value=[]))
    order = ['trial-2d', 'trial-1d', 'trial-2h', 'trial-expired']
    context = api._discount_context()
    assert context['carrier_title'] == api.CATALOG_BY_ID[context['carrier_id']]['title']
    assert context['rollout_note'] is None
    for card in order:
        result = await api.get_auto_message(card, None, AsyncMock(), None)
        variants = [
            variant for insert in result.text_inserts if insert.name == 'price_lines' for variant in insert.variants
        ]
        short = [variant for variant in variants if 'скидку уже получил' in variant.when]
        expected = enabled and order.index(card) > order.index(context['carrier_id'])
        assert len(short) == int(expected)
        if expected:
            assert short[0].when == f'скидку уже получил в письме «{context["carrier_title"]}»'
            _, opts = await api._discount_examples(AsyncMock())
            assert short[0].text == m.format_trial_discount_lines(
                opts, SimpleNamespace(balance_kopeks=0), get_texts('ru'), short=True
            )


@pytest.mark.asyncio
@pytest.mark.parametrize('expires', [None, 'future', 'past', 'none'])
async def test_old_letter_postpones_without_mark_then_sends_after_expiry(monkeypatch, expires):
    now = datetime.now(UTC)
    user = SimpleNamespace(
        id=7,
        telegram_id=77,
        promo_offer_discount_percent=0 if expires == 'none' else 20,
        promo_offer_discount_expires_at=None
        if expires in (None, 'none')
        else now + timedelta(days=1 if expires == 'future' else -1),
    )
    sub = SimpleNamespace(id=31, user=user, end_date=now - timedelta(days=1, minutes=1))
    db = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [sub]))),
        refresh=AsyncMock(),
    )
    for name, value in [
        ('are_notifications_globally_enabled', True),
        ('is_trial_expired_discount_enabled', True),
        ('get_trial_expired_discount_trigger_days', 1),
        ('get_trial_expired_discount_percent', 10),
        ('get_trial_expired_discount_valid_hours', 24),
    ]:
        monkeypatch.setattr(Settings, name, lambda value=value: value)
    monkeypatch.setattr(m, 'get_trial_tariff', AsyncMock(return_value=SimpleNamespace(id=5)))
    marked = AsyncMock(return_value=False)
    monkeypatch.setattr(m, 'notification_sent', marked)
    upsert = AsyncMock(return_value=SimpleNamespace(id=44, expires_at=now + timedelta(days=2)))
    monkeypatch.setattr(m, 'upsert_discount_offer', upsert)
    recorded = AsyncMock()
    monkeypatch.setattr(m, 'record_notification', recorded)
    logged = Mock()
    monkeypatch.setattr(m.logger, 'info', logged)
    monitor = m.MonitoringService.__new__(m.MonitoringService)
    monitor.bot = object()
    monitor._send_trial_expired_discount_notification = AsyncMock(return_value=True)
    await monitor._check_trial_expired_discount(db)
    active = expires in (None, 'future')
    assert upsert.await_count == recorded.await_count == int(not active)
    if active:
        logged.assert_called_once_with(
            'trial_expired_discount_postponed', user_id=7, percent=20, expires_at=user.promo_offer_discount_expires_at
        )
        user.promo_offer_discount_expires_at = now - timedelta(seconds=1)
        await monitor._check_trial_expired_discount(db)
        assert upsert.await_count == recorded.await_count == 1
        monitor._send_trial_expired_discount_notification.assert_awaited_once()


@pytest.mark.asyncio
async def test_issuer_traffic_owns_its_client_and_never_uses_monitor_shared_client(setup, monkeypatch):
    db, config, _, _ = setup
    db.refresh = AsyncMock()
    config['audience'] = 1
    monitor = m.MonitoringService.__new__(m.MonitoringService)
    shared = Mock(side_effect=AssertionError('shared client used'))
    monitor.subscription_service = SimpleNamespace(get_api_client=shared)
    traffic = AsyncMock(return_value=1_000_000)

    @asynccontextmanager
    async def client():
        yield SimpleNamespace(get_user_lifetime_traffic_once=traffic)

    own = Mock(return_value=SimpleNamespace(get_api_client=client))
    monkeypatch.setattr(m, 'SubscriptionService', own)
    monkeypatch.setattr(m, 'get_trial_tariff', AsyncMock(return_value=SimpleNamespace(id=5)))
    prepare = AsyncMock(return_value='prepared')
    monkeypatch.setattr(issuer, 'prepare_trial_ending_discount', prepare)
    assert await monitor.prepare_trial_discount(db, db.user, db.subscription, 24) == 'prepared'
    own.assert_called_once_with()
    shared.assert_not_called()
    traffic.assert_awaited_once_with('current-panel')
    assert prepare.await_args.kwargs['lifetime_bytes'] == 1_000_000


@pytest.mark.asyncio
async def test_old_batch_refreshes_later_user_after_concurrent_claim(monkeypatch):
    now = datetime.now(UTC)
    users = [
        SimpleNamespace(id=i, telegram_id=70 + i, promo_offer_discount_percent=0, promo_offer_discount_expires_at=None)
        for i in (1, 2)
    ]
    subs = [SimpleNamespace(id=30 + u.id, user=u, end_date=now - timedelta(days=1, minutes=1)) for u in users]
    stored = {u.id: (0, None) for u in users}
    refreshed = []

    async def refresh(user, *, attribute_names):
        assert attribute_names == ['promo_offer_discount_percent', 'promo_offer_discount_expires_at']
        refreshed.append(user.id)
        user.promo_offer_discount_percent, user.promo_offer_discount_expires_at = stored[user.id]

    db = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: subs))),
        refresh=refresh,
    )
    for name, value in [
        ('are_notifications_globally_enabled', True),
        ('is_trial_expired_discount_enabled', True),
        ('get_trial_expired_discount_trigger_days', 1),
        ('get_trial_expired_discount_percent', 10),
        ('get_trial_expired_discount_valid_hours', 24),
    ]:
        monkeypatch.setattr(Settings, name, lambda value=value: value)
    monkeypatch.setattr(m, 'get_trial_tariff', AsyncMock(return_value=SimpleNamespace(id=5)))
    monkeypatch.setattr(m, 'notification_sent', AsyncMock(return_value=False))
    upsert = AsyncMock(return_value=SimpleNamespace(id=44, expires_at=now + timedelta(days=2)))
    monkeypatch.setattr(m, 'upsert_discount_offer', upsert)
    recorded = AsyncMock()
    monkeypatch.setattr(m, 'record_notification', recorded)
    monitor = m.MonitoringService.__new__(m.MonitoringService)
    monitor.bot = object()

    async def send(user, *_args):
        assert user.id == 1, 'Later user must be postponed after concurrent claim'
        stored[2] = (20, now + timedelta(days=2))  # A's awaited transport lets B's claim commit.
        return True

    monitor._send_trial_expired_discount_notification = AsyncMock(side_effect=send)
    await monitor._check_trial_expired_discount(db)
    assert refreshed == [1, 2]
    assert (
        upsert.await_count == recorded.await_count == monitor._send_trial_expired_discount_notification.await_count == 1
    )
    assert recorded.await_args.args[1] == 1
