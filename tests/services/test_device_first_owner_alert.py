"""Этап 4.3+4.8: владелец узнаёт о сорванном заказе, а упавшее уведомление не умирает навсегда.

Главное, что проверяется здесь, — не «сообщение отправилось», а **два замка** запроса-производителя.
Строка аутбокса уведомлений по любому из пяти архивных заказов тарифа 3 навсегда запретила бы менять
его серверы (когорта отбирается условием «строки нет», `app/database/crud/tariff.py:106-110`).
Поэтому запрос собирается отдельной функцией и проверяется по скомпилированному SQL: замок,
вырезанный из кода, роняет тест, даже если поведение на моках осталось прежним.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import device_first_checkout_service as service_module
from app.services.device_first_checkout_service import (
    OWNER_ALERT_NOTIFICATION_TYPE,
    READY_NOTIFICATION_TYPE,
    owner_alert_candidate_query,
    process_device_first_notification_outbox,
    queue_owner_order_stuck_alerts,
    revive_stale_notifications,
)


def _compiled_candidate_sql() -> str:
    query = owner_alert_candidate_query(since=datetime(2026, 8, 16, tzinfo=UTC), limit=20)
    return str(query.compile(compile_kwargs={'literal_binds': False}))


# --- замки производителя: их удаление обязано ронять тест ------------------------------


def test_candidate_query_excludes_erased_users():
    """ЗАМОК 1. Все пять архивных заказов принадлежат УДАЛЁННЫМ пользователям 170 и 173."""
    sql = _compiled_candidate_sql().lower()
    assert 'users.account_erased_at is null' in sql


def test_candidate_query_has_freshness_window():
    """ЗАМОК 2. Архивные заказы не менялись с 03.08.2026 — окно их не достаёт."""
    sql = _compiled_candidate_sql().lower()
    assert 'subscription_checkouts.updated_at >=' in sql


def test_candidate_query_skips_already_queued_checkouts():
    """ЗАМОК 3. Второй проход не создаёт вторую строку по тому же заказу."""
    sql = _compiled_candidate_sql().lower()
    assert 'not (exists' in sql
    assert 'device_first_notification_outbox' in sql


def test_candidate_query_takes_only_stuck_orders():
    sql = _compiled_candidate_sql()
    literal = str(
        owner_alert_candidate_query(since=datetime(2026, 8, 16, tzinfo=UTC), limit=1).compile(
            compile_kwargs={'literal_binds': True}
        )
    )
    assert 'operator_review' in literal
    assert 'device_first_outbox' in sql
    # 🔴 Две причины тревоги соединены ИЛИ. Заменить на И — значит молча выключить весь
    # пункт 4.3 для основного случая (заказ в operator_review вообще без строки выдачи).
    assert ' OR ' in literal.split('WHERE', 1)[1]


def test_candidate_query_skips_users_who_asked_to_be_deleted():
    """Человеку, подавшему заявку на удаление, PII в админ-чат уходить не должно."""
    assert 'users.account_erasure_requested_at is null' in _compiled_candidate_sql().lower()


def test_lookback_window_cannot_reach_the_archived_cohort():
    """Окно должно быть короче, чем расстояние до архивных заказов (03.08 → 16.08)."""
    assert timedelta(days=7) >= service_module.OWNER_ALERT_LOOKBACK


# --- производитель строк ---------------------------------------------------------------


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None


def _db(rows):
    db = MagicMock()
    db.execute = AsyncMock(return_value=_Result(rows))
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    db.add = MagicMock()
    return db


@pytest.mark.asyncio
async def test_owner_row_is_queued_for_a_stuck_order():
    db = _db([32])
    with patch.object(service_module, '_owner_alerts_enabled', return_value=True):
        queued = await queue_owner_order_stuck_alerts(db, limit=20)
    assert queued == 1
    added = db.add.call_args[0][0]
    assert added.checkout_id == 32
    assert added.notification_type == OWNER_ALERT_NOTIFICATION_TYPE


@pytest.mark.asyncio
async def test_nothing_is_queued_when_admin_chat_is_not_configured():
    db = _db([32])
    with patch.object(service_module, '_owner_alerts_enabled', return_value=False):
        queued = await queue_owner_order_stuck_alerts(db, limit=20)
    assert queued == 0
    db.execute.assert_not_awaited()
    db.add.assert_not_called()


@pytest.mark.asyncio
async def test_concurrent_worker_does_not_duplicate_the_row():
    from sqlalchemy.exc import IntegrityError

    db = _db([32])
    db.commit = AsyncMock(side_effect=IntegrityError('insert', {}, Exception('duplicate')))
    with patch.object(service_module, '_owner_alerts_enabled', return_value=True):
        queued = await queue_owner_order_stuck_alerts(db, limit=20)
    assert queued == 0
    db.rollback.assert_awaited()


# --- пункт 4.8: строка `failed` больше не мертва --------------------------------------


class _Update:
    def __init__(self, rowcount):
        self.rowcount = rowcount


def _revive_db(dead_rows, revived_rows, *, doomed=()):
    db = MagicMock()
    db.execute = AsyncMock(side_effect=[_Result(list(doomed)), _Update(dead_rows), _Update(revived_rows)])
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    return db


def _revive_statements(db):
    """Скомпилированный SQL обоих UPDATE — так проверяется их WHERE, а не только результат."""
    from sqlalchemy.dialects import postgresql

    out = []
    for call in db.execute.await_args_list[1:]:
        out.append(str(call.args[0].compile(dialect=postgresql.dialect(), compile_kwargs={'literal_binds': True})))
    return out


@pytest.mark.asyncio
async def test_failed_owner_alert_is_returned_to_pending():
    db = _revive_db(0, 2)
    assert await revive_stale_notifications(db) == (2, 0)
    db.commit.assert_awaited()


@pytest.mark.asyncio
async def test_hopeless_notification_stops_instead_of_looping_forever():
    db = _revive_db(1, 0, doomed=[32])
    assert await revive_stale_notifications(db) == (0, 1)


@pytest.mark.asyncio
async def test_quiet_pass_still_closes_the_shared_transaction():
    """Сессия общая с циклом мониторинга: `rollback` на тихом проходе съел бы чужую работу."""
    db = _revive_db(0, 0)
    assert await revive_stale_notifications(db) == (0, 0)
    db.commit.assert_awaited()
    db.rollback.assert_not_awaited()


@pytest.mark.asyncio
async def test_client_ready_message_is_never_retried():
    """Для клиента `failed` означает НЕИЗВЕСТНЫЙ исход: Telegram мог сообщение принять.

    🔴 Проверяем НАМЕРЕНИЕ, а не буквальную строку: пункт 4.1 добавил владельцу ещё два типа,
    и сторож на `notification_type = 'order_stuck'` покраснел бы, хотя клиентская строка
    по-прежнему не повторяется. Красным он обязан становиться от `'ready'` в списке.
    """
    db = _revive_db(0, 0)
    await revive_stale_notifications(db)
    _dead_sql, revive_sql = _revive_statements(db)
    assert "'order_stuck'" in revive_sql
    assert "status = 'failed'" in revive_sql
    assert f"'{READY_NOTIFICATION_TYPE}'" not in revive_sql, 'клиентская строка не должна повторяться никогда'


@pytest.mark.asyncio
async def test_dead_counts_age_from_creation_and_revive_from_last_try():
    """Перепутать колонки — значит либо не повторять вовсе, либо повторять вечно."""
    db = _revive_db(0, 0)
    await revive_stale_notifications(db)
    dead_sql, revive_sql = _revive_statements(db)
    assert 'created_at <=' in dead_sql and 'updated_at <=' not in dead_sql
    assert 'updated_at <=' in revive_sql
    assert "status='dead'" in dead_sql.replace(' ', '').replace('SETstatus=', 'status=')


@pytest.mark.asyncio
async def test_owner_alert_stuck_in_sending_is_reopened_but_client_one_is_not():
    """Рестарт бота между «взял строку» и «отправил» оставлял её в `sending` навсегда."""
    db = _revive_db(0, 0)
    await revive_stale_notifications(db)
    _dead_sql, revive_sql = _revive_statements(db)
    assert "status = 'sending'" in revive_sql
    assert 'lease_expires_at <=' in revive_sql
    # Оживление ограничено ровно строками ВЛАДЕЛЬЦУ: все его типы внутри, клиентский снаружи.
    # 🔴 Имена ЗАШИТЫ намеренно. Первая версия этого сторожа перебирала
    # `OWNER_NOTIFICATION_TYPES` — ту самую константу, которую код подставляет в `.in_()`, —
    # и потому переживала схлопывание константы до одного типа. Скептик волны 2 это доказал
    # экспериментом. Сторож, ломающийся только вместе с кодом, бесполезен.
    assert "'ready'" not in revive_sql
    for owner_type in ('order_stuck', 'entitlement_drift', 'target_drift'):
        assert f"'{owner_type}'" in revive_sql, f'строка владельцу {owner_type} не оживляется'


# --- развилка отправки: владельцу не должно уйти «✅ Подписка готова» -------------------


def _outbox_row(row_id, notification_type):
    return SimpleNamespace(
        id=row_id,
        checkout_id=100 + row_id,
        notification_type=notification_type,
        status='pending',
        lease_token=None,
        lease_expires_at=None,
        sending_at=None,
        sent_at=None,
        last_error=None,
    )


def _checkout():
    return SimpleNamespace(
        id=101,
        public_id='87bf4880-3a89-4f52-be13-276854d4f8a0',
        user_id=186,
        tariff_id=3,
        period_days=90,
        selected_device_limit=2,
        tariff_total_kopeks=64900,
        lifecycle_state='operator_review',
        provisioning_state='not_started',
        terminal_reason='provider_invoice_missing_or_elapsed_expiry',
        funding_mode='platega',
        debit_transaction_id=None,
        sale_snapshot={'tariff_name': 'Базовый'},
        settlement_mode='direct_purchase_v2',
        updated_at=datetime(2026, 8, 16, 12, 59, tzinfo=UTC),
    )


def _worker_db(rows, *, checkout, user):
    db = MagicMock()
    # Порядок запросов воркера: захват строк → на каждую строку заказ (принудительным
    # перечитыванием, не из памяти сессии) → перечитывание самой строки под аренду.
    results: list[_Result] = [_Result(rows)]
    for row in rows:
        results.append(_Result([checkout] if checkout is not None else []))
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
async def test_owner_alert_does_not_use_the_client_ready_text():
    row = _outbox_row(1, OWNER_ALERT_NOTIFICATION_TYPE)
    user = SimpleNamespace(id=186, telegram_id=6221017268, username='tiger', language='ru', full_name='Tiger')
    db = _worker_db([row], checkout=_checkout(), user=user)
    bot = MagicMock()
    bot.send_message = AsyncMock()
    admin = MagicMock()
    admin.is_enabled = True
    admin.send_admin_notification = AsyncMock(return_value=True)

    with (
        patch.object(service_module, 'queue_owner_order_stuck_alerts', AsyncMock(return_value=0)),
        patch.object(service_module, 'revive_stale_notifications', AsyncMock(return_value=(0, 0))),
        patch('app.services.admin_notification_service.AdminNotificationService', return_value=admin),
    ):
        sent = await process_device_first_notification_outbox(db, bot=bot, limit=10)

    assert sent == 1
    bot.send_message.assert_not_awaited()
    text = admin.send_admin_notification.await_args.args[0]
    assert 'Подписка готова' not in text
    assert 'ЗАКАЗ ЗАВИС' in text
    assert '649' in text
    assert 'Базовый' in text
    assert row.status == 'sent'


@pytest.mark.asyncio
async def test_client_ready_notification_still_goes_to_the_client():
    row = _outbox_row(1, READY_NOTIFICATION_TYPE)
    user = SimpleNamespace(id=185, telegram_id=7454290913, username='krotop999', language='ru', full_name='K')
    db = _worker_db([row], checkout=_checkout(), user=user)
    bot = MagicMock()
    bot.send_message = AsyncMock()

    with (
        patch.object(service_module, 'queue_owner_order_stuck_alerts', AsyncMock(return_value=0)),
        patch.object(service_module, 'revive_stale_notifications', AsyncMock(return_value=(0, 0))),
    ):
        sent = await process_device_first_notification_outbox(db, bot=bot, limit=10)

    assert sent == 1
    bot.send_message.assert_awaited_once()
    assert 'готова' in bot.send_message.await_args.args[1]
    assert row.status == 'sent'


@pytest.mark.asyncio
async def test_undelivered_owner_alert_is_marked_failed_not_sent():
    row = _outbox_row(1, OWNER_ALERT_NOTIFICATION_TYPE)
    user = SimpleNamespace(id=186, telegram_id=1, username=None, language='ru', full_name='Tiger')
    db = _worker_db([row], checkout=_checkout(), user=user)
    bot = MagicMock()
    bot.send_message = AsyncMock()
    admin = MagicMock()
    admin.is_enabled = True
    admin.send_admin_notification = AsyncMock(return_value=False)

    with (
        patch.object(service_module, 'queue_owner_order_stuck_alerts', AsyncMock(return_value=0)),
        patch.object(service_module, 'revive_stale_notifications', AsyncMock(return_value=(0, 0))),
        patch('app.services.admin_notification_service.AdminNotificationService', return_value=admin),
    ):
        sent = await process_device_first_notification_outbox(db, bot=bot, limit=10)

    assert sent == 0
    assert row.status == 'failed'


@pytest.mark.asyncio
async def test_alert_is_not_sent_when_the_order_recovered_by_itself():
    """Панель полежала 30 минут и поднялась: тревога устарела, возврат делать нельзя."""
    row = _outbox_row(1, OWNER_ALERT_NOTIFICATION_TYPE)
    checkout = _checkout()
    checkout.lifecycle_state = 'ready'
    checkout.provisioning_state = 'ready'
    user = SimpleNamespace(id=186, telegram_id=1, username=None, language='ru', full_name='Tiger')
    db = _worker_db([row], checkout=checkout, user=user)
    bot = MagicMock()
    admin = MagicMock()
    admin.is_enabled = True
    admin.send_admin_notification = AsyncMock(return_value=True)

    with (
        patch.object(service_module, 'queue_owner_order_stuck_alerts', AsyncMock(return_value=0)),
        patch.object(service_module, 'revive_stale_notifications', AsyncMock(return_value=(0, 0))),
        patch('app.services.admin_notification_service.AdminNotificationService', return_value=admin),
    ):
        sent = await process_device_first_notification_outbox(db, bot=bot, limit=10)

    assert sent == 0
    admin.send_admin_notification.assert_not_awaited()
    assert row.status == 'obsolete'


# --- вердикт о деньгах: цена ошибки здесь — двойной возврат либо отказ в нём -----------


def _money_db(attempt_status=None, user=None):
    db = MagicMock()
    db.get = AsyncMock(return_value=user or SimpleNamespace(id=186, telegram_id=1, username=None, full_name='Tiger'))
    db.scalar = AsyncMock(return_value=attempt_status)
    return db


@pytest.mark.asyncio
async def test_card_payment_is_never_called_a_balance_debit():
    """`debit_transaction_id` заполняется при ОБОИХ способах оплаты — по нему судить нельзя."""
    checkout = _checkout()
    checkout.funding_mode = 'platega'
    checkout.debit_transaction_id = 77
    verdict = await service_module._money_verdict(_money_db(), checkout)
    assert 'с баланса' not in verdict.replace('С баланса не списывали', '')
    assert 'платёжную систему' in verdict


@pytest.mark.asyncio
async def test_balance_payment_is_reported_as_a_balance_debit():
    checkout = _checkout()
    checkout.funding_mode = 'wallet'
    checkout.debit_transaction_id = 77
    assert 'списаны с баланса клиента' in await service_module._money_verdict(_money_db(), checkout)


@pytest.mark.asyncio
async def test_chargeback_does_not_ask_the_owner_for_a_refund():
    """Платёж уже отозван: возврат сверху — двойная потеря."""
    checkout = _checkout()
    checkout.terminal_reason = 'post_paid_provider_terminal:chargebacked'
    checkout.debit_transaction_id = 77
    verdict = await service_module._money_verdict(_money_db(attempt_status='credited'), checkout)
    assert 'Возврат делать НЕ нужно' in verdict


@pytest.mark.asyncio
async def test_money_already_on_client_balance_is_not_refunded_twice():
    checkout = _checkout()
    checkout.funding_mode = 'platega'
    verdict = await service_module._money_verdict(_money_db(attempt_status='credited'), checkout)
    assert 'Возврат не нужен' in verdict


@pytest.mark.asyncio
async def test_no_visible_debit_sends_the_owner_to_the_provider():
    checkout = _checkout()
    checkout.funding_mode = 'platega'
    checkout.terminal_reason = 'provider_invoice_verification_mismatch'
    assert 'Platega' in await service_module._money_verdict(_money_db(), checkout)


@pytest.mark.asyncio
async def test_expired_unpaid_invoice_does_not_send_the_owner_hunting():
    """Подтверждено Platega по заказу 32: «Платёж не завершён», возврат недоступен."""
    checkout = _checkout()
    checkout.funding_mode = 'platega'
    checkout.terminal_reason = 'provider_invoice_missing_or_elapsed_expiry'
    verdict = await service_module._money_verdict(_money_db(), checkout)
    assert 'не оплатил' in verdict
    assert 'Platega' not in verdict


# --- разметка и данные клиента ---------------------------------------------------------


@pytest.mark.asyncio
async def test_hostile_client_name_cannot_break_the_message():
    checkout = _checkout()
    user = SimpleNamespace(id=186, telegram_id=1, username='<i>x', full_name='<b>Вася</b>')
    text = await service_module._owner_order_stuck_text(_money_db(user=user), checkout)
    assert '<b>Вася</b>' not in text
    assert '&lt;b&gt;Вася&lt;/b&gt;' in text
    assert '&lt;i&gt;x' in text


@pytest.mark.asyncio
async def test_machine_codes_are_translated_for_the_owner():
    checkout = _checkout()
    text = await service_module._owner_order_stuck_text(_money_db(), checkout)
    assert 'Оплату нужно проверить' in text
    assert 'счёт у платёжной системы просрочен или не найден' in text
    # Пункт 4.4 сделал кнопку разбора, и прежняя фраза «кнопки в боте пока нет» стала
    # ложью. Сторож переписан в том же коммите — как и требовала мина H.
    assert 'Кнопки для разбора в боте пока нет' not in text
    assert 'Заказы на разборе' in text


@pytest.mark.asyncio
async def test_owner_is_sent_to_the_button_that_actually_exists():
    """Мина H: текст тревоги — это инструкция, и она обязана вести в ЖИВУЮ кнопку.

    До пункта 4.4 текст звал в кабинет и отговаривал от чат-админки. Теперь разбор
    живёт именно в чат-админке, а прежний совет стал вредным: кабинетный «Сбросить
    подписку» физически удаляет подписку с оплаченным сроком (урок этапа 4.1).
    """
    text = await service_module._owner_order_stuck_text(_money_db(), _checkout())
    assert 'админ-панель' in text
    assert 'Заказы на разборе' in text
    # Ручной возврат в Platega база не видит (мина L) — владельца обязаны предупредить,
    # иначе кнопка возврата предложит отдать те же деньги второй раз.
    assert 'Platega' in text


@pytest.mark.asyncio
async def test_the_alert_never_names_a_dead_button_again():
    """Сторож против рецидива: три пути, которые на 18.08.2026 мертвы или опасны.

    Проверяется собранное сообщение, а не исходник функции: разбор кода по скобкам
    ломается от одной скобки внутри текста (урок этапа 4.1).
    """
    text = await service_module._owner_order_stuck_text(_money_db(), _checkout())
    for dead in ('Обнулить подписку', 'Сбросить подписку', 'Сделать платной'):
        assert dead not in text


@pytest.mark.asyncio
async def test_money_verdict_actually_reaches_the_message():
    """Вердикт можно было выкинуть из текста, и ни один тест этого не замечал."""
    checkout = _checkout()
    checkout.funding_mode = 'wallet'
    checkout.debit_transaction_id = 77
    text = await service_module._owner_order_stuck_text(_money_db(), checkout)
    assert await service_module._money_verdict(_money_db(), checkout) in text


@pytest.mark.asyncio
async def test_client_without_telegram_does_not_break_the_alert():
    """`telegram_id` nullable: подстановка None отбивается Telegram и теряет тревогу целиком."""
    user = SimpleNamespace(id=186, telegram_id=None, username=None, full_name='Почтовый клиент')
    text = await service_module._owner_order_stuck_text(_money_db(user=user), _checkout())
    assert 'tg://user?id=None' not in text
    assert 'телеграма нет' in text


def test_operator_review_is_never_treated_as_a_resolved_order():
    """🔴 Сторож под мину C: свести этот набор с `TERMINAL_STATES` — значит убить тревоги."""
    assert 'operator_review' not in service_module._OWNER_ALERT_RESOLVED_STATES
    assert service_module._owner_alert_is_obsolete(SimpleNamespace(lifecycle_state='operator_review')) is False
    assert service_module._owner_alert_is_obsolete(SimpleNamespace(lifecycle_state='ready')) is True


@pytest.mark.asyncio
async def test_missing_user_row_does_not_produce_garbage_contact():
    checkout = _checkout()
    db = MagicMock()
    db.get = AsyncMock(return_value=None)
    db.scalar = AsyncMock(return_value=None)
    text = await service_module._owner_order_stuck_text(db, checkout)
    assert 'tg ?' not in text
    assert 'карточка недоступна' in text


@pytest.mark.asyncio
async def test_the_stuck_alert_goes_to_the_errors_category():
    """🔴 Мина AI (пункт 4.4): категорию можно было убрать при полностью зелёном наборе.

    Доставка при этом «успешна», но сообщение уходит в другой топик, и владелец не
    видит ничего. У тревоги этапа 4.1 такой сторож был, у этой — не было.
    """
    from app.services.admin_notification_service import NotificationCategory

    admin = MagicMock()
    admin.is_enabled = True
    admin.send_admin_notification = AsyncMock(return_value=True)

    with patch('app.services.admin_notification_service.AdminNotificationService', return_value=admin):
        await service_module._send_owner_order_stuck_alert(_money_db(), bot=MagicMock(), checkout=_checkout())

    assert admin.send_admin_notification.await_args.kwargs['category'] is NotificationCategory.ERRORS


@pytest.mark.asyncio
async def test_the_ready_message_carries_a_way_into_the_cabinet():
    """🔴 Пункт 1 реза. Сообщение «подписка готова» звало открыть кабинет и не давало чем.

    Человек к этому моменту уже ЗАПЛАТИЛ, и адрес возврата после оплаты — https-кабинет,
    а не `t.me`: в браузере он приземляется на экран входа. Дверь обратно — эта кнопка.
    Сторож смотрит на фактически переданную клавиатуру, а не на исходник: проверка по
    тексту файла в этом проекте уже дважды оказывалась пустой.
    """
    bot = MagicMock()
    bot.send_message = AsyncMock()
    db = MagicMock()
    db.get = AsyncMock(return_value=SimpleNamespace(id=5, telegram_id=777, language='ru'))

    await service_module._send_client_ready_message(db, bot=bot, checkout=_checkout())

    markup = bot.send_message.await_args.kwargs['reply_markup']
    assert markup is not None, 'сообщение снова ушло голым текстом'
    buttons = [button for row in markup.inline_keyboard for button in row]
    assert len(buttons) == 1
    # Кнопка обязана куда-то вести: либо в кабинет (web_app), либо в живой обработчик.
    only = buttons[0]
    assert only.web_app is not None or only.callback_data == 'subscription_connect'


@pytest.mark.asyncio
async def test_revive_catches_per_recipient_referral_rows():
    """Оживление обязано ловить строку с получателем в типе (РФ-3).

    🔴 С РФ-3 тип реферальной строки несёт получателя: `referral_reward:133`. Список типов
    сравнивает точно, и один только он эту строку не поднял бы — то есть повтор письма о
    деньгах исчез бы совсем, а это и есть то, ради чего этап делался.

    Мутация «убрать LIKE из оживления» проходила весь набор зелёной: место для сторожа было
    готово в этом же файле, строку просто не дописали. Нашло ревью.
    """
    db = _revive_db(0, 0)
    await revive_stale_notifications(db)
    _dead_sql, revive_sql = _revive_statements(db)

    assert 'referral_reward:%' in revive_sql, 'строка с получателем в типе перестала оживать'
    assert 'LIKE' in revive_sql, 'сравнение по началу типа исчезло — остался только точный список'
    # И общий список никуда не делся: он держит строки владельцу.
    assert "'order_stuck'" in revive_sql
