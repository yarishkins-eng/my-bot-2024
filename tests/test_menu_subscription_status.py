from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

from app.handlers.menu import _get_subscription_status


class DummyTexts:
    def t(self, key: str, default: str):  # pragma: no cover - simple stub
        return default


def _build_user_with_subscription(
    actual_status: str,
    is_trial: bool,
    days_left: int,
    *,
    in_grace: bool = False,
    grace_until=None,
):
    subscription = MagicMock()
    subscription.actual_status = actual_status
    subscription.is_trial = is_trial
    subscription.end_date = datetime.now(UTC) + timedelta(days=days_left, hours=1)
    # Реалистичная обычная подписка: НЕ в grace (иначе MagicMock сделал бы атрибут
    # truthy и код ошибочно ушёл бы в grace-ветку).
    subscription.in_grace = in_grace
    subscription.grace_until = grace_until

    user = MagicMock()
    user.subscription = subscription
    user.has_had_paid_subscription = not is_trial
    return user


def test_get_subscription_status_marks_trial_as_trial():
    texts = DummyTexts()
    user = _build_user_with_subscription(actual_status='active', is_trial=True, days_left=5)

    status_text = _get_subscription_status(user, texts)

    assert 'Тестовая подписка' in status_text
    assert 'Активна' not in status_text


def test_get_subscription_status_grace_shows_bonus_not_expired():
    """Grace «бонус 2 дня»: VPN ещё жив (status в БД = expired) → НЕ «Истекла»."""
    texts = DummyTexts()
    grace_until = datetime.now(UTC) + timedelta(days=1)
    user = _build_user_with_subscription(
        actual_status='expired',
        is_trial=False,
        days_left=-1,
        in_grace=True,
        grace_until=grace_until,
    )

    status_text = _get_subscription_status(user, texts)

    assert 'Бонус' in status_text
    assert 'Истекла' not in status_text


def test_get_subscription_status_truly_expired_still_says_expired():
    """Регресс: по-настоящему истёкшая (НЕ в grace) платная — по-прежнему «Истекла»."""
    texts = DummyTexts()
    user = _build_user_with_subscription(
        actual_status='expired', is_trial=False, days_left=-3, in_grace=False
    )

    status_text = _get_subscription_status(user, texts)

    assert 'Истекла' in status_text
