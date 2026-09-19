"""Забор «ещё не платил деньгами» — ВЫЧИСЛЯЕТСЯ настоящим движком, а не проверяется по тексту SQL.

Скептик 19.09.2026 показал: сторож на текст UPDATE пропускает мутации, меняющие смысл
(«AND NOT EXISTS» → «OR NOT EXISTS» привязывает уже платившего; `is_completed` внутри одной ветки
вместо общего условия делает незавершённый возврат «деньгами»). Здесь настоящий
`attach_referrer_if_missing` идёт до настоящего UPDATE на SQLite в памяти с минимальной схемой
тех же таблиц и колонок, что читает предикат, — и «привязался / не привязался» читается из базы.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.services.referral_service import attach_referrer_if_missing


class _AsyncOverSync:
    """Асинхронная обёртка над обычной SQLite-сессией: тот же `execute`, тот же UPDATE, те же строки."""

    def __init__(self, session: Session) -> None:
        self._s = session

    async def execute(self, stmt):
        return self._s.execute(stmt)

    async def commit(self) -> None:
        self._s.commit()

    async def rollback(self) -> None:
        self._s.rollback()

    async def refresh(self, obj) -> None:
        row = self._s.execute(text('SELECT referred_by_id FROM users WHERE id = :id'), {'id': obj.id}).one()
        obj.referred_by_id = row[0]


def _schema() -> Session:
    engine = create_engine('sqlite://')
    with engine.begin() as c:
        c.execute(
            text(
                'CREATE TABLE users (id INTEGER PRIMARY KEY, telegram_id INTEGER, email TEXT, '
                'referred_by_id INTEGER, has_made_first_topup BOOLEAN NOT NULL DEFAULT 0, updated_at TIMESTAMP)'
            )
        )
        c.execute(
            text(
                'CREATE TABLE transactions (id INTEGER PRIMARY KEY, user_id INTEGER, type TEXT, '
                'payment_method TEXT, is_completed BOOLEAN, device_first_checkout_id INTEGER)'
            )
        )
        c.execute(text('INSERT INTO users (id, telegram_id) VALUES (200, 888)'))  # пригласивший
    return Session(engine)


def _add_user(session: Session, user_id: int, *, flag: bool = False) -> SimpleNamespace:
    session.execute(
        text('INSERT INTO users (id, telegram_id, has_made_first_topup) VALUES (:id, :tg, :flag)'),
        {'id': user_id, 'tg': user_id * 10, 'flag': flag},
    )
    session.commit()
    return SimpleNamespace(id=user_id, telegram_id=user_id * 10, referred_by_id=None, email=None)


def _add_tx(session: Session, user_id: int, tx_type: str, method: str | None, *, completed=True, checkout=None):
    session.execute(
        text(
            'INSERT INTO transactions (user_id, type, payment_method, is_completed, device_first_checkout_id) '
            'VALUES (:u, :t, :m, :c, :ch)'
        ),
        {'u': user_id, 't': tx_type, 'm': method, 'c': completed, 'ch': checkout},
    )
    session.commit()


async def _attach(session: Session, user: SimpleNamespace) -> tuple[int | None, int | None]:
    referrer = SimpleNamespace(id=200, telegram_id=888, email=None)
    with (
        patch('app.database.crud.user.get_user_by_referral_code', AsyncMock(return_value=referrer)),
        patch('app.services.referral_service.get_pending_referral', AsyncMock(return_value=None)),
        patch('app.services.referral_service.clear_pending_referral', AsyncMock()),
        patch('app.services.referral_service.process_referral_registration', AsyncMock()),
    ):
        result = await attach_referrer_if_missing(_AsyncOverSync(session), user, referral_code='X', source='t')
    stored = session.execute(text('SELECT referred_by_id FROM users WHERE id = :id'), {'id': user.id}).one()[0]
    return result, stored


CASES = [
    # (имя, флаг, транзакции [(тип, метод, завершена, checkout)], ожидаем привязку)
    ('пробный без транзакций', False, [], True),
    ('бонус за регистрацию (метод NULL)', False, [('deposit', None, True, None)], True),
    ('покупка с баланса', False, [('subscription_payment', 'balance', True, None)], True),
    ('нулевая смена тарифа админом', False, [('subscription_payment', 'balance', True, None)], True),
    ('ручное начисление админом', False, [('deposit', 'manual', True, None)], True),
    ('пополнение через провайдера', False, [('deposit', 'platega', True, None)], False),
    ('прямая оплата картой', False, [('subscription_payment', 'platega', True, 5)], False),
    ('только чек провайдера (заказ застрял)', False, [('provider_receipt', 'platega', True, 5)], False),
    ('оплата Stars', False, [('deposit', 'telegram_stars', True, None)], False),
    ('возврат по заказу на разборе', False, [('deposit', 'manual', True, 7)], False),
    ('незавершённый возврат по разбору', False, [('deposit', 'manual', False, 7)], True),
    ('незавершённое пополнение', False, [('deposit', 'platega', False, None)], True),
    ('пополнение с is_completed NULL (в коде никто не пишет)', False, [('deposit', 'platega', None, None)], True),
    ('флаг оплаты без транзакций', True, [], False),
    ('бонус + позже оплата картой', False, [('deposit', None, True, None), ('deposit', 'platega', True, None)], False),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(('name', 'flag', 'txs', 'expect_attach'), CASES, ids=[c[0] for c in CASES])
async def test_money_fence_evaluated_by_real_engine(name, flag, txs, expect_attach) -> None:
    session = _schema()
    user = _add_user(session, 1, flag=flag)
    for tx_type, method, completed, checkout in txs:
        _add_tx(session, 1, tx_type, method, completed=completed, checkout=checkout)

    result, stored = await _attach(session, user)

    if expect_attach:
        assert result == 200 and stored == 200, f'{name}: должен был привязаться'
        assert user.referred_by_id == 200
    else:
        assert result is None and stored is None, f'{name}: платил — привязки быть не должно'
        assert user.referred_by_id is None


@pytest.mark.asyncio
async def test_other_users_payments_do_not_count() -> None:
    """Подзапрос соотнесён с обновляемой строкой: чужая оплата картой не запирает пробного."""
    session = _schema()
    payer = _add_user(session, 1)
    _add_tx(session, payer.id, 'deposit', 'platega')
    trial = _add_user(session, 2)

    result, stored = await _attach(session, trial)

    assert result == 200 and stored == 200


@pytest.mark.asyncio
async def test_already_attached_row_is_not_overwritten() -> None:
    """Compare-and-set остался: строку с пригласившим второй UPDATE не переписывает."""
    session = _schema()
    user = _add_user(session, 1)
    session.execute(text('UPDATE users SET referred_by_id = 999 WHERE id = 1'))
    session.commit()

    result, stored = await _attach(session, user)

    assert result is None and stored == 999
    assert user.referred_by_id == 999, 'после проигрыша refresh показывает победителя'
