"""Safe, opt-in alert delivery for the manager Telegram forum group.

The manager group is deliberately isolated from the full administrator alerts:
nothing reaches it unless it was named manager-safe on purpose. Backups, error
reports, raw infrastructure webhooks and their attachments are never routed
through this service.

Two opt-in routes exist, and neither is a default:

* `mirror_admin_category` copies whole admin categories named in the allow-list
  below. Categories missing from it can never be copied this way.
* a single notification may name its own topic (`_send_message(manager_topic=)`)
  when only part of an admin category is manager-safe. Advertising campaign
  alerts reach MARKETING that way, while the rest of the `promo` category stays
  admin-only.

Only the first route is self-limiting: a category absent from the allow-list
cannot be copied however it is called. The other two are limited by tests that
pin their call sites by name in `tests/services/test_manager_alert_service.py`,
so an accidental new one fails the suite until it is added there on purpose.

What those tests do NOT catch, stated plainly so nobody trusts them further
than they reach: they read the call as it is written, so building the argument
elsewhere and unpacking it (`**routing`) reaches this module unpinned. They are
a guard against absent-mindedness, not against someone routing around them.

Delivery here is best-effort and always secondary. A copy is attempted only
after the admin chat already accepted the message, it inherits that category's
on/off switch, and under Telegram flood control it is dropped rather than
retried (see `send`) — deliberately, because retrying would make a client wait.
"""

import json
from enum import StrEnum
from pathlib import Path

import structlog
from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)


logger = structlog.get_logger(__name__)


class ManagerAlertTopic(StrEnum):
    TICKETS = 'tickets'
    SUBSCRIPTIONS = 'subscriptions'
    PAYMENTS = 'payments'
    REPORTS = 'reports'
    SERVICE_STATUS = 'service_status'
    MARKETING = 'marketing'


class ManagerAlertSettingsService:
    """Stores the manager forum chat and topic IDs outside git and .env."""

    _storage_path: Path = Path('data/manager_alert_settings.json')
    _data: dict = {}
    _loaded: bool = False

    @classmethod
    def _ensure_dir(cls) -> None:
        try:
            cls._storage_path.parent.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            logger.warning('Failed to ensure manager alert settings directory', error=exc)

    @classmethod
    def _load(cls) -> None:
        if cls._loaded:
            return
        cls._ensure_dir()
        try:
            raw = json.loads(cls._storage_path.read_text(encoding='utf-8')) if cls._storage_path.exists() else {}
            cls._data = raw if isinstance(raw, dict) else {}
        except Exception as exc:
            logger.warning('Failed to load manager alert settings', error=exc)
            cls._data = {}
        cls._loaded = True

    @classmethod
    def _save(cls) -> bool:
        cls._ensure_dir()
        try:
            cls._storage_path.write_text(json.dumps(cls._data, ensure_ascii=False, indent=2), encoding='utf-8')
            return True
        except Exception as exc:
            logger.warning('Failed to save manager alert settings', error=exc)
            return False

    @classmethod
    def bind_topic(cls, *, chat_id: int, topic: ManagerAlertTopic, thread_id: int) -> bool:
        """Bind one topic. All bindings must belong to one manager group."""
        if not isinstance(chat_id, int) or chat_id >= 0 or not isinstance(thread_id, int) or thread_id <= 0:
            return False

        cls._load()
        configured_chat_id = cls._data.get('chat_id')
        if configured_chat_id is not None and configured_chat_id != chat_id:
            logger.warning(
                'Refusing manager topic from another chat', configured_chat_id=configured_chat_id, chat_id=chat_id
            )
            return False

        cls._data['chat_id'] = chat_id
        topics = cls._data.setdefault('topics', {})
        if not isinstance(topics, dict):
            topics = {}
            cls._data['topics'] = topics
        topics[topic.value] = thread_id
        return cls._save()

    @classmethod
    def get_recipient(cls, topic: ManagerAlertTopic) -> tuple[int, int] | None:
        cls._load()
        chat_id = cls._data.get('chat_id')
        topics = cls._data.get('topics')
        thread_id = topics.get(topic.value) if isinstance(topics, dict) else None
        if isinstance(chat_id, int) and chat_id < 0 and isinstance(thread_id, int) and thread_id > 0:
            return chat_id, thread_id
        return None

    @classmethod
    def configured_topics(cls) -> set[ManagerAlertTopic]:
        return {topic for topic in ManagerAlertTopic if cls.get_recipient(topic) is not None}


# `promo`, `partners`, `infrastructure` and `errors` are absent on purpose: a
# whole-category copy of them would leak admin-only events. Where a single
# notification inside such a category is manager-safe, it names its own topic
# instead of being added here.
_ADMIN_CATEGORY_TO_MANAGER_TOPIC: dict[str, ManagerAlertTopic] = {
    'tickets': ManagerAlertTopic.TICKETS,
    'trials': ManagerAlertTopic.SUBSCRIPTIONS,
    'purchases': ManagerAlertTopic.PAYMENTS,
    'renewals': ManagerAlertTopic.PAYMENTS,
    'balance': ManagerAlertTopic.PAYMENTS,
    'addons': ManagerAlertTopic.PAYMENTS,
}


class ManagerAlertService:
    """Best-effort sender which exposes only manager-safe alert categories."""

    async def send(self, bot: Bot, topic: ManagerAlertTopic, text: str) -> bool:
        recipient = ManagerAlertSettingsService.get_recipient(topic)
        if not recipient:
            return False

        chat_id, thread_id = recipient
        try:
            await bot.send_message(
                chat_id=chat_id,
                message_thread_id=thread_id,
                text=text,
                parse_mode='HTML',
                disable_web_page_preview=True,
            )
            logger.info('Manager alert sent', topic=topic.value)
            return True
        except TelegramRetryAfter as exc:
            # Копия менеджеру намеренно НЕ ретраится: `send` вызывается внутри
            # обработки клиентского /start, и сон здесь задержал бы ответ живому
            # человеку. Отдельная ветка нужна, чтобы потеря была видна в логах
            # как известное поведение, а не как «Unexpected»: без неё
            # TelegramRetryAfter (наследник TelegramAPIError, а не перечисленных
            # ниже) молча уходил в общий except. Копия остаётся best-effort —
            # надёжной её сделает только очередь, а это отдельная работа.
            logger.warning(
                'Manager alert dropped by flood control (no retry by design)',
                topic=topic.value,
                retry_after=getattr(exc, 'retry_after', None),
            )
            return False
        except (TelegramForbiddenError, TelegramBadRequest, TelegramNetworkError, TelegramServerError) as exc:
            logger.warning('Failed to send manager alert', topic=topic.value, error=str(exc)[:200])
            return False
        except Exception as exc:
            logger.warning('Unexpected manager alert delivery error', topic=topic.value, error=str(exc)[:200])
            return False

    async def mirror_admin_category(self, bot: Bot, category: str | None, text: str) -> bool:
        """Mirror only the explicit allow-list; never copy keyboards or files."""
        topic = _ADMIN_CATEGORY_TO_MANAGER_TOPIC.get(category or '')
        if not topic:
            return False
        return await self.send(bot, topic, text)

    async def send_service_status(self, bot: Bot, *, restored: bool, event_count: int) -> bool:
        count_suffix = f' ({event_count} событий)' if event_count > 1 else ''
        if restored:
            text = (
                '<b>✅ Статус сервиса</b>\n\n'
                f'Связь с VPN-локацией восстановлена{count_suffix}.\n'
                'Техническая команда получила уведомление.'
            )
        else:
            text = (
                '<b>🚨 Статус сервиса</b>\n\n'
                f'Потеряна связь с VPN-локацией{count_suffix}.\n'
                'Техническая команда получила уведомление.'
            )
        return await self.send(bot, ManagerAlertTopic.SERVICE_STATUS, text)


manager_alert_service = ManagerAlertService()
