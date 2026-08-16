from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.database.crud import tariff as tariff_crud


def _create_schema(connection) -> None:
    for statement in (
        'CREATE TABLE users (id INTEGER PRIMARY KEY, account_erased_at TEXT, account_erasure_requested_at TEXT)',
        'CREATE TABLE account_erasure_requests ('
        'id INTEGER PRIMARY KEY, user_id INTEGER, state TEXT, panel_state TEXT, panel_cleanup_uuids JSON, finalized_at TEXT)',
        'CREATE TABLE subscription_checkouts ('
        'id INTEGER PRIMARY KEY, tariff_id INTEGER, user_id INTEGER, lifecycle_state TEXT, terminal_reason TEXT, '
        'quote_state TEXT, funding_state TEXT, fulfillment_state TEXT, provisioning_state TEXT, settlement_mode TEXT, '
        'created_subscription_id INTEGER, debit_transaction_id INTEGER)',
        'CREATE TABLE checkout_payment_attempts ('
        'id INTEGER PRIMARY KEY, checkout_id INTEGER, status TEXT, provider TEXT, settlement_mode TEXT, '
        'reconciliation_reason TEXT, credited_amount_kopeks INTEGER, platega_payment_id INTEGER, '
        'provider_payment_id TEXT, requested_amount_kopeks INTEGER, currency TEXT, provider_method_code INTEGER)',
        'CREATE TABLE platega_payments ('
        'id INTEGER PRIMARY KEY, user_id INTEGER, platega_transaction_id TEXT, amount_kopeks INTEGER, currency TEXT, '
        'payment_method_code INTEGER, status TEXT, is_paid BOOLEAN, paid_at TEXT, transaction_id INTEGER)',
        'CREATE TABLE device_first_reconciliation_credits (id INTEGER PRIMARY KEY, checkout_id INTEGER)',
        'CREATE TABLE transactions (id INTEGER PRIMARY KEY, device_first_checkout_id INTEGER)',
        'CREATE TABLE device_first_outbox (id INTEGER PRIMARY KEY, checkout_id INTEGER)',
        'CREATE TABLE device_first_deposit_outbox (id INTEGER PRIMARY KEY, checkout_id INTEGER)',
        'CREATE TABLE device_first_notification_outbox (id INTEGER PRIMARY KEY, checkout_id INTEGER)',
    ):
        connection.execute(text(statement))


def _insert_archived_checkout(connection, *, checkout_id: int, tariff_id: int, checkout_mode: str) -> None:
    connection.execute(
        text(
            'INSERT INTO users (id, account_erased_at, account_erasure_requested_at) '
            f"VALUES ({checkout_id}, '2026-08-04T00:00:00Z', '2026-08-04T00:00:00Z')"
        )
    )
    connection.execute(
        text(
            'INSERT INTO account_erasure_requests '
            '(id, user_id, state, panel_state, panel_cleanup_uuids, finalized_at) '
            f"VALUES ({checkout_id}, {checkout_id}, 'completed', 'deactivated', '[]', '2026-08-04T00:00:00Z')"
        )
    )
    connection.execute(
        text(
            'INSERT INTO subscription_checkouts ('
            'id, tariff_id, user_id, lifecycle_state, terminal_reason, quote_state, funding_state, '
            'fulfillment_state, provisioning_state, settlement_mode, created_subscription_id, debit_transaction_id'
            ') VALUES ('
            f"{checkout_id}, {tariff_id}, {checkout_id}, 'operator_review', 'direct_payment_binding_mismatch', "
            f"'expired', 'invoice_terminal', 'not_started', 'not_started', '{checkout_mode}', NULL, NULL)"
        )
    )
    connection.execute(
        text(
            'INSERT INTO platega_payments ('
            'id, user_id, platega_transaction_id, amount_kopeks, currency, payment_method_code, '
            'status, is_paid, paid_at, transaction_id'
            ') VALUES ('
            f"{checkout_id}, {checkout_id}, 'provider-{checkout_id}', 24900, 'RUB', 2, "
            "'OPERATOR_REVIEW', 0, NULL, NULL)"
        )
    )
    connection.execute(
        text(
            'INSERT INTO checkout_payment_attempts ('
            'id, checkout_id, status, provider, settlement_mode, reconciliation_reason, '
            'credited_amount_kopeks, platega_payment_id, provider_payment_id, requested_amount_kopeks, '
            'currency, provider_method_code'
            ') VALUES ('
            f"{checkout_id}, {checkout_id}, 'operator_review', 'platega', 'direct_purchase_v2', "
            f"'direct_payment_binding_mismatch', 0, {checkout_id}, 'provider-{checkout_id}', 24900, 'RUB', 2)"
        )
    )


def _insert_exact_archive_cohort(connection, *, tariff_id: int = 3) -> None:
    for checkout_id, checkout_mode in (
        (10, 'direct_purchase_v2'),
        (11, 'direct_purchase_v2'),
        (12, 'direct_purchase_v2'),
        (13, 'direct_purchase_v2'),
        (14, 'legacy_deposit'),
    ):
        _insert_archived_checkout(
            connection,
            checkout_id=checkout_id,
            tariff_id=tariff_id,
            checkout_mode=checkout_mode,
        )


def _async_db(sync_db):
    async def execute(statement):
        return sync_db.execute(statement)

    async def scalar(statement):
        return sync_db.scalar(statement)

    return SimpleNamespace(execute=execute, scalar=scalar)


async def _pin_test_manifest(monkeypatch, db) -> None:
    rows = await tariff_crud._archive_manifest_rows(db, tariff_id=3)
    digest = tariff_crud._archive_manifest_sha256(rows)
    assert digest is not None
    monkeypatch.setattr(tariff_crud, '_APPROVED_FINAL_ERASURE_ARCHIVE_SHA256', digest)


@pytest.mark.asyncio
async def test_exact_five_final_erasure_archive_rows_allow_tariff_three_edit(monkeypatch) -> None:
    engine = create_engine('sqlite:///:memory:')
    try:
        with engine.begin() as connection:
            _create_schema(connection)
            _insert_exact_archive_cohort(connection)
        with sessionmaker(engine)() as sync_db:
            db = _async_db(sync_db)
            await _pin_test_manifest(monkeypatch, db)
            await tariff_crud._assert_tariff_squad_change_has_no_live_checkout(db, SimpleNamespace(id=3))
    finally:
        engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'mutation',
    (
        'sixth_archive',
        'second_attempt',
        'payment_amount',
        'coherent_method_drift',
        'payment_paid',
        'erasure_not_final',
        'erasure_cleanup',
        'outbox',
        'deposit_outbox',
        'notification_outbox',
        'credit',
        'transaction',
    ),
)
async def test_archive_manifest_drift_fails_closed(monkeypatch, mutation: str) -> None:
    engine = create_engine('sqlite:///:memory:')
    try:
        with engine.begin() as connection:
            _create_schema(connection)
            _insert_exact_archive_cohort(connection)
        with sessionmaker(engine)() as sync_db:
            db = _async_db(sync_db)
            await _pin_test_manifest(monkeypatch, db)
            if mutation == 'sixth_archive':
                _insert_archived_checkout(
                    sync_db.connection(),
                    checkout_id=15,
                    tariff_id=3,
                    checkout_mode='direct_purchase_v2',
                )
            elif mutation == 'second_attempt':
                sync_db.execute(
                    text(
                        'INSERT INTO checkout_payment_attempts ('
                        'id, checkout_id, status, provider, settlement_mode, reconciliation_reason, '
                        'credited_amount_kopeks, platega_payment_id, provider_payment_id, requested_amount_kopeks, '
                        'currency, provider_method_code'
                        ") VALUES (99, 10, 'failed', 'platega', 'direct_purchase_v2', 'provider_terminal:canceled', "
                        "0, NULL, 'other-provider-10', 24900, 'RUB', 2)"
                    )
                )
            elif mutation == 'payment_amount':
                sync_db.execute(text('UPDATE platega_payments SET amount_kopeks = 1 WHERE id = 10'))
            elif mutation == 'coherent_method_drift':
                sync_db.execute(text('UPDATE platega_payments SET payment_method_code = 3 WHERE id = 10'))
                sync_db.execute(text('UPDATE checkout_payment_attempts SET provider_method_code = 3 WHERE id = 10'))
            elif mutation == 'payment_paid':
                sync_db.execute(text('UPDATE platega_payments SET is_paid = 1 WHERE id = 10'))
            elif mutation == 'erasure_not_final':
                sync_db.execute(text('UPDATE account_erasure_requests SET finalized_at = NULL WHERE id = 10'))
            elif mutation == 'erasure_cleanup':
                sync_db.execute(
                    text('UPDATE account_erasure_requests SET panel_cleanup_uuids = \'["x"]\' WHERE id = 10')
                )
            elif mutation == 'outbox':
                sync_db.execute(text('INSERT INTO device_first_outbox VALUES (10, 10)'))
            elif mutation == 'deposit_outbox':
                sync_db.execute(text('INSERT INTO device_first_deposit_outbox VALUES (10, 10)'))
            elif mutation == 'notification_outbox':
                sync_db.execute(text('INSERT INTO device_first_notification_outbox VALUES (10, 10)'))
            elif mutation == 'credit':
                sync_db.execute(text('INSERT INTO device_first_reconciliation_credits VALUES (10, 10)'))
            else:
                sync_db.execute(text('INSERT INTO transactions VALUES (10, 10)'))
            sync_db.commit()
            with pytest.raises(ValueError, match='live checkout or Platega invoice'):
                await tariff_crud._assert_tariff_squad_change_has_no_live_checkout(db, SimpleNamespace(id=3))
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_correct_archive_never_bypasses_a_separate_live_invoice(monkeypatch) -> None:
    engine = create_engine('sqlite:///:memory:')
    try:
        with engine.begin() as connection:
            _create_schema(connection)
            _insert_exact_archive_cohort(connection)
            connection.execute(
                text(
                    'INSERT INTO subscription_checkouts (id, tariff_id, lifecycle_state) '
                    "VALUES (90, 3, 'awaiting_funds')"
                )
            )
            connection.execute(
                text("INSERT INTO checkout_payment_attempts (id, checkout_id, status) VALUES (90, 90, 'pending')")
            )
        with sessionmaker(engine)() as sync_db:
            db = _async_db(sync_db)
            await _pin_test_manifest(monkeypatch, db)
            with pytest.raises(ValueError, match='live checkout or Platega invoice'):
                await tariff_crud._assert_tariff_squad_change_has_no_live_checkout(db, SimpleNamespace(id=3))
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_archive_for_another_tariff_never_receives_the_pinned_exception(monkeypatch) -> None:
    engine = create_engine('sqlite:///:memory:')
    try:
        with engine.begin() as connection:
            _create_schema(connection)
            _insert_exact_archive_cohort(connection, tariff_id=4)
        with sessionmaker(engine)() as sync_db:
            db = _async_db(sync_db)
            monkeypatch.setattr(tariff_crud, '_APPROVED_FINAL_ERASURE_ARCHIVE_SHA256', 'any-value')
            with pytest.raises(ValueError, match='live checkout or Platega invoice'):
                await tariff_crud._assert_tariff_squad_change_has_no_live_checkout(db, SimpleNamespace(id=4))
    finally:
        engine.dispose()


# --- мина C, безопасная часть: тупиковые заказы больше не запирают тариф -----------------


def _insert_plain_checkout(connection, *, checkout_id: int, tariff_id: int, lifecycle_state: str) -> None:
    """Заказ без платёжной попытки — проверяется ровно первая ветвь забора."""
    connection.execute(
        text(
            'INSERT INTO subscription_checkouts (id, tariff_id, lifecycle_state) '
            f"VALUES ({checkout_id}, {tariff_id}, '{lifecycle_state}')"
        )
    )


def _fence_on_plain_checkout(lifecycle_state: str):
    engine = create_engine('sqlite:///:memory:')
    with engine.begin() as connection:
        _create_schema(connection)
        _insert_plain_checkout(connection, checkout_id=40, tariff_id=3, lifecycle_state=lifecycle_state)
    return engine


@pytest.mark.asyncio
@pytest.mark.parametrize('lifecycle_state', ('conflict', 'reprice_required', 'failed'))
async def test_dead_end_states_no_longer_lock_the_tariff(lifecycle_state: str) -> None:
    """Мина C. Из этих состояний заказ не оживает и выдан быть не может.

    Поиск живого заказа клиента их не выбирает (`device_first_checkout_service.py:389-406`),
    а все входы в выдачу требуют `confirmed` / `awaiting_funds` / `fulfilling`. Значит
    захваченный набор серверов защищать не от чего, а раньше один такой заказ запирал
    смену серверов тарифа навсегда.
    """
    engine = _fence_on_plain_checkout(lifecycle_state)
    try:
        with sessionmaker(engine)() as sync_db:
            await tariff_crud._assert_tariff_squad_change_has_no_live_checkout(
                _async_db(sync_db), SimpleNamespace(id=3)
            )
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_operator_review_still_locks_the_tariff() -> None:
    """🔴 СТОРОЖ. Не добавлять `operator_review` в `_CHECKOUT_TERMINAL_STATES`.

    Доска этапа просила свести набор с `TERMINAL_STATES`, но ревью показало, что для
    `operator_review` это дыра, и он оставлен под забором намеренно:
      1) он оживает штатно — воркер сверки адресно берёт попытки в `operator_review` с
         причиной `provider_invoice_missing_or_elapsed_expiry`
         (`device_first_payment_service.py:2138-2144`) и возвращает их в `pending`/`awaiting_funds`;
      2) на нём часто висят деньги: поздняя оплата ставит `attempt.status='operator_review'`
         вместе с `payment.is_paid=True` (`:2002-2020`), а этот статус не входит в
         `_DIRECT_PROVIDER_ATTEMPT_OPEN_STATES` — вторая ветвь забора его не ловит;
      3) сверка снимка прав с текущим тарифом включается только при
         `provenance == 'access_point_policy'` (`device_first_checkout_service.py:1730`),
         а такого режима нет ни у одного тарифа.
    То есть для оплаченного неразобранного заказа этот забор — единственная защита.
    Размыкать такие заказы должен пункт 4.4, а не расширение набора.
    """
    engine = _fence_on_plain_checkout('operator_review')
    try:
        with sessionmaker(engine)() as sync_db:
            with pytest.raises(ValueError, match='live checkout or Platega invoice'):
                await tariff_crud._assert_tariff_squad_change_has_no_live_checkout(
                    _async_db(sync_db), SimpleNamespace(id=3)
                )
    finally:
        engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize('lifecycle_state', ('draft', 'confirmed', 'awaiting_funds', 'armed', 'fulfilling'))
async def test_open_states_still_lock_the_tariff(lifecycle_state: str) -> None:
    """Сторож против дальнейшего расширения: живой заказ по-прежнему держит забор.

    Любое из этих состояний в терминальном наборе означало бы смену серверов под клиентом,
    который уже видит цену и вот-вот заплатит.
    """
    engine = _fence_on_plain_checkout(lifecycle_state)
    try:
        with sessionmaker(engine)() as sync_db:
            with pytest.raises(ValueError, match='live checkout or Platega invoice'):
                await tariff_crud._assert_tariff_squad_change_has_no_live_checkout(
                    _async_db(sync_db), SimpleNamespace(id=3)
                )
    finally:
        engine.dispose()
