"""Durable test-fixture reset fence with no reusable VPN credentials."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime

from sqlalchemy import select, text

from app.database.models import (
    CheckoutPaymentAttempt,
    Subscription,
    SubscriptionCheckout,
    SubscriptionEntitlementTerm,
    User,
)


RESET_BUSY = frozenset({'resetting', 'failed'})
RESET_MESSAGE = 'Тестовый аккаунт сейчас сбрасывается. Дождитесь завершения сброса у администратора.'


def reset_is_busy(user) -> bool:
    return getattr(user, 'test_reset_state', None) in RESET_BUSY


def has_reset_history(user) -> bool:
    return isinstance(getattr(user, 'test_reset_completed_at', None), datetime)


@asynccontextmanager
async def reset_lock(db, user_id: int):
    """A session advisory lock survives the marker/cleanup commits.

    The dedicated connection holds no row locks during Panel IO. Unlock is
    explicit; an uncertain release invalidates the connection, never pools it.
    """
    async with db.bind.connect() as connection:
        acquired = bool(await connection.scalar(text('SELECT pg_try_advisory_lock(1052026, :id)'), {'id': user_id}))
        await connection.commit()
        try:
            yield acquired
        finally:
            if acquired:
                try:
                    await connection.execute(text('SELECT pg_advisory_unlock(1052026, :id)'), {'id': user_id})
                    await connection.commit()
                except BaseException:
                    await connection.invalidate()
                    raise


async def reset_bypass(db) -> None:
    await db.execute(text("SET LOCAL app.test_account_reset = 'on'"))


async def lock_reset_rows(db, user_id: int) -> None:
    """Drain pre-fence writes before financial checks and the marker commit.

    Parent locks also serialize inserts through foreign keys. These locks are
    released with the marker, before any Panel IO or shared-counter changes.
    """
    from app.services.user_service import _test_reset_delete_plan

    sub_ids = list(await db.scalars(select(Subscription.id).where(Subscription.user_id == user_id).with_for_update()))
    checkout_ids = list(
        await db.scalars(
            select(SubscriptionCheckout.id).where(SubscriptionCheckout.user_id == user_id).with_for_update()
        )
    )
    attempt_ids = list(
        await db.scalars(
            select(CheckoutPaymentAttempt.id)
            .where(CheckoutPaymentAttempt.checkout_id.in_(checkout_ids))
            .with_for_update()
        )
    )
    term_ids = list(
        await db.scalars(
            select(SubscriptionEntitlementTerm.id)
            .where(SubscriptionEntitlementTerm.subscription_id.in_(sub_ids))
            .with_for_update()
        )
    )
    scopes = {
        'users.id': [user_id],
        'subscriptions.id': sub_ids,
        'subscription_checkouts.id': checkout_ids,
        'checkout_payment_attempts.id': attempt_ids,
        'subscription_entitlement_terms.id': term_ids,
    }
    for table, whereclause in _test_reset_delete_plan(scopes):
        await db.execute(select(*table.primary_key.columns).where(whereclause).with_for_update())


async def run_reset(db, user, admin_id, *, confirm: bool, preview_token: str | None = None):
    from app.services.user_service import (
        TestAccountResetPlan,
        _reset_test_account_unlocked,
        _test_reset_blocked_reason,
        is_test_account,
    )

    if not is_test_account(user):
        return TestAccountResetPlan(blocked_reason='Этот аккаунт не отмечен как тестовый.')
    if not confirm:
        return await _reset_test_account_unlocked(db, user, admin_id, confirm=False)

    user_id = user.id
    async with reset_lock(db, user_id) as acquired:
        if not acquired:
            return TestAccountResetPlan(blocked_reason='Сброс уже выполняется. Обновите карточку через минуту.')
        await reset_bypass(db)
        current = await db.scalar(
            select(User).where(User.id == user_id).with_for_update().execution_options(populate_existing=True)
        )
        if current is None or not is_test_account(current):
            await db.rollback()
            return TestAccountResetPlan(blocked_reason='Этот аккаунт больше не отмечен как тестовый.')
        await lock_reset_rows(db, user_id)
        reason = await _test_reset_blocked_reason(db, current)
        if reason:
            await db.rollback()
            return TestAccountResetPlan(blocked_reason=reason)
        preview = await _reset_test_account_unlocked(db, current, admin_id, confirm=False)
        if not preview_token or preview.preview_token != preview_token:
            await db.rollback()
            preview.allowed = False
            preview.blocked_reason = 'Данные изменились. Нажмите «Проверить сброс» и подтвердите новый список.'
            return preview
        # Keep the explicit tombstones across retries. A panel timeout must
        # never make us forget an identity we still have to verify as absent.
        panel_ids = set(current.test_reset_panel_uuids or [])
        if current.remnawave_uuid:
            panel_ids.add(current.remnawave_uuid)
        panel_ids.update(
            value
            for value in (
                await db.scalars(select(Subscription.remnawave_uuid).where(Subscription.user_id == user_id))
            ).all()
            if value
        )
        current.test_reset_panel_uuids = sorted(panel_ids)
        current.test_reset_state = 'resetting'
        current.test_reset_started_at = datetime.now(UTC)
        await db.commit()
        try:
            await reset_bypass(db)
            result = await _reset_test_account_unlocked(db, current, admin_id, confirm=True)
            if result.done:
                return result
        except Exception:
            await db.rollback()
            result = TestAccountResetPlan(blocked_reason='Сброс прерван. Повторите его из этой карточки.')
        # Failure is durable and closed to client/worker writes until retry.
        await db.rollback()
        await reset_bypass(db)
        current = await db.get(User, user_id, populate_existing=True)
        current.test_reset_state = 'failed'
        await db.commit()
        result.reset_state = 'failed'
        return result


async def current_test_subscription(db, user, subscription_id: int) -> bool:
    """Old in-memory retry items cannot provision a later test generation."""
    if reset_is_busy(user):
        return False
    if getattr(user, 'test_reset_state', None) is None:
        return True
    return bool(
        await db.scalar(
            select(Subscription.id).where(Subscription.id == subscription_id, Subscription.user_id == user.id)
        )
    )
