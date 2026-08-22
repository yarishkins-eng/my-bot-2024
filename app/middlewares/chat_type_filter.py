"""Middleware to ignore non-private chat messages.

When the bot is added as admin to a group or supergroup (including forums
with topics), it should silently drop all incoming messages and callback
queries from those chats. Only private (DM) interactions are processed.

Not registered on chat_member — channel_member.py needs ChatMemberUpdated
events from groups/channels to track required channel subscriptions.
Not registered on pre_checkout_query — no chat context, always private.
"""

from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from aiogram import BaseMiddleware
from aiogram.enums import ChatType
from aiogram.types import CallbackQuery, Message, TelegramObject

from app.handlers.manager_alerts import is_manager_alert_setup_command


logger = structlog.get_logger(__name__)


class ChatTypeFilterMiddleware(BaseMiddleware):
    """Drop messages and callback queries from non-private chats."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        chat = None
        if isinstance(event, Message):
            chat = event.chat
        elif isinstance(event, CallbackQuery) and event.message:
            chat = event.message.chat

        # The manager-alert forum needs two owner-only setup commands.  Let only
        # these exact commands through; every other group interaction remains
        # ignored as before.
        is_manager_alert_setup = (
            isinstance(event, Message)
            and event.chat.type in {ChatType.GROUP, ChatType.SUPERGROUP}
            and is_manager_alert_setup_command(event.text)
        )

        if chat is not None and chat.type != ChatType.PRIVATE and not is_manager_alert_setup:
            logger.debug(
                'Dropping non-private chat event',
                chat_id=chat.id,
                chat_type=chat.type,
            )
            return None

        return await handler(event, data)
