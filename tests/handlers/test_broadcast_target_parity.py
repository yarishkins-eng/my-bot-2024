from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.cabinet.routes import admin_broadcasts as broadcast_routes
from app.cabinet.schemas.broadcasts import (
    BroadcastPreviewRequest,
    CombinedBroadcastCreateRequest,
    EmailPreviewRequest,
)
from app.database.crud import subscription as subscription_crud
from app.database.crud.subscription import get_expiring_subscriptions
from app.database.models import SubscriptionStatus
from app.handlers.admin import messages as admin_messages
from app.services import broadcast_service as broadcast_module


PUBLIC_TARGETS = (
    'all',
    'active',
    'trial',
    'no',
    'expiring',
    'expired',
    'zero',
    'active_zero',
    'trial_zero',
    'tariff_11',
    'custom_today',
)


def _subscription(
    *,
    status: str,
    is_active: bool,
    end_date: datetime,
    is_trial: bool = False,
    traffic_used_gb: float | None = 0,
    tariff_id: int | None = None,
):
    return SimpleNamespace(
        status=status,
        is_active=is_active,
        end_date=end_date,
        is_trial=is_trial,
        traffic_used_gb=traffic_used_gb,
        tariff_id=tariff_id,
    )


def _user(user_id: int, subscriptions: list, *, paid_before: bool = False):
    return SimpleNamespace(
        id=user_id,
        subscriptions=subscriptions,
        has_had_paid_subscription=paid_before,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize('target', PUBLIC_TARGETS)
async def test_preview_count_is_len_of_the_exact_send_selector(monkeypatch, target: str) -> None:
    """РС-9: у счётчика больше нет собственного второго предиката.

    Старый SQL COUNT расходился с Python-выборкой минимум в четырёх ветках. Этот
    сторож запрещает чинить их по отдельности: preview обязан считать результат
    той же публичной функции, которую затем использует отправка.
    """

    selected = [object(), object(), object()]
    calls: list[str] = []

    async def fake_get_target_users(db, requested_target: str):
        calls.append(requested_target)
        return selected

    monkeypatch.setattr(admin_messages, 'get_target_users', fake_get_target_users)

    assert await admin_messages.get_target_users_count(object(), target) == len(selected)
    assert calls == [target]


@pytest.mark.asyncio
async def test_send_selector_honours_real_activity_boundaries(monkeypatch) -> None:
    """Статус `active` без будущего end_date не является живой подпиской.

    Здесь одновременно заперты старые расхождения active/no/*_zero/expired и
    тарифного фильтра, включая LIMITED с будущей датой.
    """

    now = datetime.now(UTC)
    live_paid = _user(
        1,
        [
            _subscription(
                status=SubscriptionStatus.ACTIVE.value,
                is_active=True,
                end_date=now + timedelta(days=10),
                tariff_id=11,
            )
        ],
    )
    stale_paid = _user(
        2,
        [
            _subscription(
                status=SubscriptionStatus.ACTIVE.value,
                is_active=False,
                end_date=now - timedelta(seconds=1),
                tariff_id=11,
            )
        ],
    )
    live_trial = _user(
        3,
        [
            _subscription(
                status=SubscriptionStatus.ACTIVE.value,
                is_active=True,
                end_date=now + timedelta(days=2),
                is_trial=True,
            )
        ],
    )
    historic_trial = _user(
        4,
        [
            _subscription(
                status=SubscriptionStatus.EXPIRED.value,
                is_active=False,
                end_date=now - timedelta(days=1),
                is_trial=True,
            )
        ],
    )
    limited_future = _user(
        5,
        [
            _subscription(
                status=SubscriptionStatus.LIMITED.value,
                is_active=False,
                end_date=now + timedelta(days=5),
            )
        ],
    )
    disabled = _user(
        6,
        [
            _subscription(
                status=SubscriptionStatus.DISABLED.value,
                is_active=False,
                end_date=now + timedelta(days=5),
            )
        ],
    )
    paid_without_subscription = _user(7, [], paid_before=True)
    live_used = _user(
        8,
        [
            _subscription(
                status=SubscriptionStatus.ACTIVE.value,
                is_active=True,
                end_date=now + timedelta(days=10),
                traffic_used_gb=2,
                tariff_id=11,
            )
        ],
    )
    users = [
        live_paid,
        stale_paid,
        live_trial,
        historic_trial,
        limited_future,
        disabled,
        paid_without_subscription,
        live_used,
    ]

    async def fake_get_users_list(db, *, offset: int, limit: int, status):
        return users if offset == 0 else []

    monkeypatch.setattr(admin_messages, 'get_users_list', fake_get_users_list)

    async def ids(target: str) -> list[int]:
        return [user.id for user in await admin_messages.get_target_users(object(), target)]

    assert await ids('all') == [1, 2, 3, 4, 5, 6, 7, 8]
    assert await ids('active') == [1, 8]
    assert await ids('trial') == [3, 4]
    assert await ids('no') == [2, 4, 5, 6, 7]
    assert await ids('zero') == [1, 3]
    assert await ids('active_zero') == [1]
    assert await ids('trial_zero') == [3]
    assert await ids('expired') == [2, 4, 6, 7]
    assert await ids('tariff_11') == [1, 8]


@pytest.mark.asyncio
@pytest.mark.parametrize('target,days', [('expiring', 3), ('expiring_subscribers', 7)])
async def test_expiring_selector_returns_unique_users(monkeypatch, target: str, days: int) -> None:
    """Две истекающие подписки одного человека — один получатель, не два POST."""

    first = _user(10, [])
    second = _user(20, [])
    expiring_subscriptions = [
        SimpleNamespace(user=first),
        SimpleNamespace(user=first),
        SimpleNamespace(user=second),
        SimpleNamespace(user=None),
    ]

    async def fake_get_users_list(db, *, offset: int, limit: int, status):
        return [first, second] if offset == 0 else []

    async def fake_get_expiring_subscriptions(db, requested_days: int):
        assert requested_days == days
        return expiring_subscriptions

    monkeypatch.setattr(admin_messages, 'get_users_list', fake_get_users_list)
    monkeypatch.setattr(admin_messages, 'get_expiring_subscriptions', fake_get_expiring_subscriptions)

    selected = await admin_messages.get_target_users(object(), target)
    assert [user.id for user in selected] == [10, 20]


@pytest.mark.asyncio
async def test_custom_target_dispatches_to_the_same_custom_selector(monkeypatch) -> None:
    selected = [_user(31, []), _user(32, [])]

    async def fake_get_custom_users(db, criteria: str):
        assert criteria == 'today'
        return selected

    async def forbidden_full_scan(*args, **kwargs):
        raise AssertionError('custom target must not fall through to the generic full scan')

    monkeypatch.setattr(admin_messages, 'get_custom_users', fake_get_custom_users)
    monkeypatch.setattr(admin_messages, 'get_users_list', forbidden_full_scan)

    assert await admin_messages.get_target_users(object(), 'custom_today') == selected
    assert await admin_messages.get_target_users_count(object(), 'custom_today') == 2


@pytest.mark.asyncio
async def test_zero_and_expired_edges_keep_the_actual_semantics(monkeypatch) -> None:
    now = datetime.now(UTC)
    null_traffic = _user(
        41,
        [
            _subscription(
                status=SubscriptionStatus.ACTIVE.value,
                is_active=True,
                end_date=now + timedelta(days=5),
                traffic_used_gb=None,
            )
        ],
    )
    negative_traffic = _user(
        42,
        [
            _subscription(
                status=SubscriptionStatus.ACTIVE.value,
                is_active=True,
                end_date=now + timedelta(days=5),
                traffic_used_gb=-0.5,
            )
        ],
    )
    limited_past = _user(
        43,
        [
            _subscription(
                status=SubscriptionStatus.LIMITED.value,
                is_active=False,
                end_date=now - timedelta(seconds=1),
            )
        ],
    )
    mixed = _user(
        44,
        [
            _subscription(
                status=SubscriptionStatus.ACTIVE.value,
                is_active=True,
                end_date=now + timedelta(days=5),
            ),
            _subscription(
                status=SubscriptionStatus.EXPIRED.value,
                is_active=False,
                end_date=now - timedelta(days=1),
            ),
        ],
    )
    users = [null_traffic, negative_traffic, limited_past, mixed]

    async def fake_get_users_list(db, *, offset: int, limit: int, status):
        from app.database.models import UserStatus

        assert status is UserStatus.ACTIVE
        return users if offset == 0 else []

    monkeypatch.setattr(admin_messages, 'get_users_list', fake_get_users_list)

    assert [u.id for u in await admin_messages.get_target_users(object(), 'zero')] == [41, 42, 44]
    assert [u.id for u in await admin_messages.get_target_users(object(), 'expired')] == [43]
    assert [u.id for u in await admin_messages.get_target_users(object(), 'expired_subscribers')] == [43]


@pytest.mark.asyncio
async def test_telegram_projection_is_unique_and_respects_channel_eligibility(monkeypatch) -> None:
    users = [
        SimpleNamespace(id=1, telegram_id=100, notification_settings={}),
        SimpleNamespace(id=2, telegram_id=None, notification_settings={}),
        SimpleNamespace(id=3, telegram_id=300, notification_settings={'news_enabled': False}),
        SimpleNamespace(id=4, telegram_id=100, notification_settings={}),
        SimpleNamespace(id=5, telegram_id=500, notification_settings={'promo_offers_enabled': False}),
    ]
    calls: list[str] = []

    async def fake_get_target_users(db, target: str):
        calls.append(target)
        return users

    monkeypatch.setattr(broadcast_module, 'get_target_users', fake_get_target_users)

    assert await broadcast_module.resolve_telegram_broadcast_recipient_ids(object(), 'active', 'system') == [100, 300, 500]
    assert await broadcast_module.resolve_telegram_broadcast_recipient_ids(object(), 'trial', 'news') == [100, 500]
    assert await broadcast_module.resolve_telegram_broadcast_recipient_ids(object(), 'expired', 'promo') == [100, 300]
    assert calls == ['active', 'trial', 'expired']


@pytest.mark.asyncio
async def test_telegram_projection_forwards_non_all_target_with_preloaded_users(monkeypatch) -> None:
    preloaded = [SimpleNamespace(id=1, telegram_id=101, notification_settings={})]
    calls: list[tuple[str, bool]] = []

    async def fake_get_target_users(db, target: str, *, preloaded_users=None):
        calls.append((target, preloaded_users is preloaded))
        return preloaded_users

    monkeypatch.setattr(broadcast_module, 'get_target_users', fake_get_target_users)

    for target in ('tariff_17', 'active_zero'):
        assert await broadcast_module.resolve_telegram_broadcast_recipient_ids(
            object(),
            target,
            'system',
            preloaded_users=preloaded,
        ) == [101]
    assert calls == [('tariff_17', True), ('active_zero', True)]


@pytest.mark.asyncio
async def test_email_projection_deduplicates_join_rows_and_respects_opt_out() -> None:
    first = SimpleNamespace(
        id=1,
        email='one@example.com',
        username='one',
        first_name=None,
        last_name=None,
        notification_settings={},
    )
    opted_out = SimpleNamespace(
        id=2,
        email='two@example.com',
        username=None,
        first_name='Two',
        last_name='User',
        notification_settings={'promo_offers_enabled': False},
    )
    news_opted_out = SimpleNamespace(
        id=3,
        email='three@example.com',
        username='three',
        first_name=None,
        last_name=None,
        notification_settings={'news_enabled': False},
    )

    class FakeScalars:
        def __init__(self):
            self.was_unique = False

        def unique(self):
            self.was_unique = True
            return self

        def all(self):
            assert self.was_unique, 'join rows must be deduplicated before projection'
            return [first, opted_out, news_opted_out]

    class FakeResult:
        def scalars(self):
            return FakeScalars()

    class FakeSession:
        async def execute(self, statement):
            return FakeResult()

    expected_by_category = {
        'system': ['one@example.com', 'two@example.com', 'three@example.com'],
        'news': ['one@example.com', 'two@example.com'],
        'promo': ['one@example.com', 'three@example.com'],
    }
    for category, expected_emails in expected_by_category.items():
        recipients = await broadcast_module.resolve_email_broadcast_recipients(
            FakeSession(),
            'active_email',
            category,
        )
        assert [recipient.email for recipient in recipients] == expected_emails


@pytest.mark.asyncio
@pytest.mark.parametrize('days', [3, 7])
async def test_expiring_query_keeps_time_bounds_and_excludes_running_daily_tariffs(
    monkeypatch,
    days: int,
) -> None:
    """Сторожит реальный CRUD, а не уже отфильтрованный mock результата."""

    class FakeScalars:
        def all(self):
            return []

    class FakeResult:
        def scalars(self):
            return FakeScalars()

    class FakeSession:
        statement = None

        async def execute(self, statement):
            self.statement = statement
            return FakeResult()

    fixed_now = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)

    class FrozenDatetime:
        @classmethod
        def now(cls, tz=None):
            assert tz is UTC
            return fixed_now

    monkeypatch.setattr(subscription_crud, 'datetime', FrozenDatetime)
    session = FakeSession()
    await get_expiring_subscriptions(session, days)
    sql = str(session.statement.compile(compile_kwargs={'literal_binds': True}))

    assert 'JOIN users ON subscriptions.user_id = users.id' in sql
    assert 'LEFT OUTER JOIN tariffs ON subscriptions.tariff_id = tariffs.id' in sql
    assert "subscriptions.status = 'active'" in sql
    assert "users.status = 'active'" in sql
    expected_upper = '2026-09-01' if days == 3 else '2026-09-05'
    assert f"subscriptions.end_date <= '{expected_upper} 12:00:00+00:00'" in sql
    assert "subscriptions.end_date > '2026-08-29 12:00:00+00:00'" in sql
    assert 'NOT (tariffs.is_daily IS true AND subscriptions.is_daily_paused IS false)' in sql
    option_paths = {str(option.path) for option in session.statement._with_options}
    assert any('Subscription.user' in path for path in option_paths)
    assert any('Subscription.tariff' in path for path in option_paths)


@pytest.mark.asyncio
async def test_preview_routes_use_channel_projection_with_current_category(monkeypatch) -> None:
    telegram_calls: list[tuple[str, str]] = []
    email_calls: list[tuple[str, str]] = []

    async def fake_telegram(session, target: str, category: str):
        telegram_calls.append((target, category))
        return [101, 202]

    async def fake_email(session, target: str, category: str):
        email_calls.append((target, category))
        return [object()]

    class FakeTariffResult:
        def all(self):
            return [(17,)]

    class FakeSession:
        async def execute(self, statement):
            return FakeTariffResult()

    monkeypatch.setattr(
        broadcast_routes,
        'resolve_telegram_broadcast_recipient_ids',
        fake_telegram,
    )
    monkeypatch.setattr(broadcast_routes, 'resolve_email_broadcast_recipients', fake_email)

    for target, category in (('active_zero', 'news'), ('tariff_17', 'system')):
        telegram = await broadcast_routes.preview_broadcast(
            BroadcastPreviewRequest(target=target, category=category),
            admin=object(),
            db=FakeSession(),
        )
        assert telegram.count == 2
    for target, category in (('expired_email', 'promo'), ('active_email', 'news')):
        email = await broadcast_routes.preview_email_broadcast(
            EmailPreviewRequest(target=target, category=category),
            admin=object(),
            db=FakeSession(),
        )
        assert email.count == 1

    assert telegram_calls == [('active_zero', 'news'), ('tariff_17', 'system')]
    assert email_calls == [('expired_email', 'promo'), ('active_email', 'news')]


@pytest.mark.asyncio
async def test_workers_delegate_to_the_same_channel_projections(monkeypatch) -> None:
    session = object()
    calls: list[tuple[str, str, str]] = []

    class SessionContext:
        async def __aenter__(self):
            return session

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    async def fake_telegram(active_session, target: str, category: str):
        assert active_session is session
        calls.append(('telegram', target, category))
        return [123]

    async def fake_email(active_session, target: str, category: str):
        assert active_session is session
        calls.append(('email', target, category))
        return [broadcast_module._EmailRecipient(email='one@example.com', user_name='One')]

    monkeypatch.setattr(broadcast_module, 'AsyncSessionLocal', SessionContext)
    monkeypatch.setattr(broadcast_module, 'resolve_telegram_broadcast_recipient_ids', fake_telegram)
    monkeypatch.setattr(broadcast_module, 'resolve_email_broadcast_recipients', fake_email)

    for target, category in (('all', 'news'), ('active_zero', 'system')):
        assert await broadcast_module.broadcast_service._fetch_recipients(target, category) == [123]
    for target, category in (('all_email', 'promo'), ('expired_email', 'news')):
        recipients = await broadcast_module.email_broadcast_service._fetch_email_recipients(
            target,
            category,
        )
        assert [recipient.email for recipient in recipients] == ['one@example.com']
    assert calls == [
        ('telegram', 'all', 'news'),
        ('telegram', 'active_zero', 'system'),
        ('email', 'all_email', 'promo'),
        ('email', 'expired_email', 'news'),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize('category', ['news', 'promo'])
async def test_filter_catalog_reuses_one_base_load_and_keeps_category(monkeypatch, category: str) -> None:
    """Parity не должна превращать один экран в N полных чтений users+subs."""

    base_users = [SimpleNamespace(id=1)]
    base_loads: list[str] = []
    projections: list[tuple[str, str, bool]] = []

    async def fake_get_target_users(session, target: str):
        base_loads.append(target)
        return base_users

    async def fake_projection(
        session,
        target: str,
        category: str,
        *,
        preloaded_users=None,
    ):
        projections.append((target, category, preloaded_users is base_users))
        return [100]

    async def fake_tariff_counts(session, actual_category: str, *, preloaded_users=None):
        assert actual_category == category
        assert preloaded_users is base_users
        return {}

    class FakeScalars:
        def all(self):
            return []

    class FakeResult:
        def scalars(self):
            return FakeScalars()

    class FakeSession:
        async def execute(self, statement):
            return FakeResult()

    monkeypatch.setattr(broadcast_routes, 'get_target_users', fake_get_target_users)
    monkeypatch.setattr(
        broadcast_routes,
        'resolve_telegram_broadcast_recipient_ids',
        fake_projection,
    )
    monkeypatch.setattr(broadcast_routes, '_get_tariff_user_counts', fake_tariff_counts)

    response = await broadcast_routes.get_filters(
        category=category,
        admin=object(),
        db=FakeSession(),
    )

    assert base_loads == ['all']
    assert len(response.filters) == len(broadcast_routes.FILTER_LABELS)
    assert len(response.custom_filters) == len(broadcast_routes.CUSTOM_FILTER_LABELS)
    standard_calls = [call for call in projections if not call[0].startswith('custom_')]
    custom_calls = [call for call in projections if call[0].startswith('custom_')]
    assert standard_calls and all(actual == category and reused for _, actual, reused in standard_calls)
    assert custom_calls and all(actual == category and not reused for _, actual, reused in custom_calls)


@pytest.mark.asyncio
async def test_tariff_catalog_forwards_each_category_to_projection(monkeypatch) -> None:
    preloaded = [SimpleNamespace(id=1)]
    calls: list[tuple[str, str, bool]] = []

    class FakeRows:
        def all(self):
            return [(17,), (23,)]

    class FakeSession:
        async def execute(self, statement):
            return FakeRows()

    async def fake_projection(session, target: str, category: str, *, preloaded_users=None):
        calls.append((target, category, preloaded_users is preloaded))
        return [101]

    monkeypatch.setattr(
        broadcast_routes,
        'resolve_telegram_broadcast_recipient_ids',
        fake_projection,
    )

    for category in ('system', 'promo'):
        assert await broadcast_routes._get_tariff_user_counts(
            FakeSession(),
            category,
            preloaded_users=preloaded,
        ) == {17: 1, 23: 1}

    assert calls == [
        ('tariff_17', 'system', True),
        ('tariff_23', 'system', True),
        ('tariff_17', 'promo', True),
        ('tariff_23', 'promo', True),
    ]


@pytest.mark.asyncio
async def test_email_catalog_forwards_each_category_to_all_filters(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []

    async def fake_count(session, target: str, category: str) -> int:
        calls.append((target, category))
        return 1

    monkeypatch.setattr(broadcast_routes, '_get_email_filter_count', fake_count)

    for category in ('system', 'news'):
        response = await broadcast_routes.get_email_filters(
            category=category,
            admin=object(),
            db=object(),
        )
        assert response.total_with_email == 1

    assert calls == [
        *((key, 'system') for key in broadcast_routes.EMAIL_FILTER_LABELS),
        *((key, 'news') for key in broadcast_routes.EMAIL_FILTER_LABELS),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('target', 'required_fragments', 'forbidden_fragments'),
    [
        ('all_email', (), (' JOIN subscriptions ', 'users.auth_type =')),
        ('email_only', ("users.auth_type = 'email'",), (' JOIN subscriptions ',)),
        (
            'telegram_with_email',
            ("users.auth_type = 'telegram'", 'users.telegram_id IS NOT NULL'),
            (' JOIN subscriptions ',),
        ),
        (
            'active_email',
            (' JOIN subscriptions ', "subscriptions.status = 'active'"),
            ("subscriptions.status IN ('expired', 'disabled')",),
        ),
        (
            'expired_email',
            (' JOIN subscriptions ', "subscriptions.status IN ('expired', 'disabled')"),
            ("subscriptions.status = 'active'",),
        ),
    ],
)
async def test_email_target_sql_matrix(
    target: str,
    required_fragments: tuple[str, ...],
    forbidden_fragments: tuple[str, ...],
) -> None:
    """Каждый публичный Email target охраняет свой реальный query predicate."""

    class FakeScalars:
        def unique(self):
            return self

        def all(self):
            return []

    class FakeResult:
        def scalars(self):
            return FakeScalars()

    class FakeSession:
        statement = None

        async def execute(self, statement):
            self.statement = statement
            return FakeResult()

    session = FakeSession()
    assert await broadcast_module.resolve_email_broadcast_recipients(session, target) == []
    sql = str(session.statement.compile(compile_kwargs={'literal_binds': True}))

    for fragment in (
        'users.email IS NOT NULL',
        'users.email_verified = true',
        "users.status = 'active'",
        *required_fragments,
    ):
        assert fragment in sql
    for fragment in forbidden_fragments:
        assert fragment not in sql


def test_category_is_wired_from_create_route_through_both_workers() -> None:
    """Preview и send не могут незаметно разойтись на default `system`."""

    create_source = inspect.getsource(broadcast_routes.create_combined_broadcast)
    telegram_worker_source = inspect.getsource(broadcast_module.BroadcastService._run_broadcast)
    email_worker_source = inspect.getsource(broadcast_module.EmailBroadcastService._run_broadcast)

    telegram_config_source = create_source.split('telegram_config = BroadcastConfig(', 1)[1].split(
        'await broadcast_service.start_broadcast',
        1,
    )[0]
    email_config_source = create_source.split('email_config = EmailBroadcastConfig(', 1)[1].split(
        'await email_broadcast_service.start_broadcast',
        1,
    )[0]
    assert 'category=request.category' in telegram_config_source
    assert 'category=request.category' in email_config_source
    assert 'self._fetch_recipients(config.target, config.category)' in telegram_worker_source
    assert 'self._fetch_email_recipients(config.target, config.category)' in email_worker_source


@pytest.mark.asyncio
async def test_create_route_forwards_exact_target_and_category_to_each_worker(monkeypatch) -> None:
    """Selected non-default targets must survive the DB-record/config seam unchanged."""

    started: list[tuple[str, str, str]] = []

    async def fake_start_telegram(broadcast_id: int, config) -> None:
        started.append(('telegram', config.target, config.category))

    async def fake_start_email(broadcast_id: int, config) -> None:
        started.append(('email', config.target, config.category))

    class FakeRows:
        def all(self):
            return [(17,)]

    class FakeSession:
        def __init__(self):
            self.next_id = 100

        async def execute(self, statement):
            return FakeRows()

        def add(self, broadcast) -> None:
            self.broadcast = broadcast

        async def commit(self) -> None:
            return None

        async def refresh(self, broadcast) -> None:
            if broadcast.id is None:
                broadcast.id = self.next_id
                self.next_id += 1
            if broadcast.created_at is None:
                broadcast.created_at = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)
            broadcast.completed_at = None
            broadcast.blocked_count = 0

    monkeypatch.setattr(broadcast_routes.broadcast_service, 'start_broadcast', fake_start_telegram)
    monkeypatch.setattr(broadcast_routes.email_broadcast_service, 'start_broadcast', fake_start_email)

    admin = SimpleNamespace(id=7, username='owner')
    session = FakeSession()
    await broadcast_routes.create_combined_broadcast(
        CombinedBroadcastCreateRequest(
            channel='telegram',
            target='tariff_17',
            message_text='Telegram message',
            selected_buttons=[],
            category='news',
        ),
        admin=admin,
        db=session,
    )
    await broadcast_routes.create_combined_broadcast(
        CombinedBroadcastCreateRequest(
            channel='telegram',
            target='active_zero',
            message_text='Second Telegram message',
            selected_buttons=[],
            category='system',
        ),
        admin=admin,
        db=session,
    )
    await broadcast_routes.create_combined_broadcast(
        CombinedBroadcastCreateRequest(
            channel='email',
            target='expired_email',
            email_subject='Email subject',
            email_html_content='<p>Email body</p>',
            category='promo',
        ),
        admin=admin,
        db=session,
    )
    await broadcast_routes.create_combined_broadcast(
        CombinedBroadcastCreateRequest(
            channel='email',
            target='active_email',
            email_subject='Second Email subject',
            email_html_content='<p>Second Email body</p>',
            category='news',
        ),
        admin=admin,
        db=session,
    )

    assert started == [
        ('telegram', 'tariff_17', 'news'),
        ('telegram', 'active_zero', 'system'),
        ('email', 'expired_email', 'promo'),
        ('email', 'active_email', 'news'),
    ]


@pytest.mark.asyncio
async def test_chat_broadcast_uses_same_system_telegram_projection(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []

    async def fake_projection(session, target: str, category: str):
        calls.append((target, category))
        return [101, 202]

    monkeypatch.setattr(
        broadcast_module,
        'resolve_telegram_broadcast_recipient_ids',
        fake_projection,
    )

    for target in ('expiring', 'trial_zero'):
        assert await admin_messages._get_telegram_target_recipient_ids(object(), target) == [101, 202]
    assert calls == [('expiring', 'system'), ('trial_zero', 'system')]

    for handler in (
        admin_messages.select_broadcast_target,
        admin_messages.select_custom_criteria,
        admin_messages.confirm_button_selection,
        admin_messages.confirm_broadcast,
    ):
        assert '_get_telegram_target_recipient_ids' in inspect.getsource(handler)
