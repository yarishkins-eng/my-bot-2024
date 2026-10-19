from time import monotonic

import structlog

from app.services.notification_settings_service import NotificationSettingsService
from app.utils.funnel_notify import _get_redis


logger = structlog.get_logger(__name__)
MAX_LATENESS = 30 * 60
_last_warning = float('-inf')


async def trial_connect_queue(method: str, *args, **kwargs):
    global _last_warning
    client = _get_redis()
    if client is None:
        return None
    try:
        return await getattr(client, method)('trial_connect_reminder_due', *args, **kwargs)
    except Exception:
        now = monotonic()
        if NotificationSettingsService.is_enabled('trial_connect_reminder') and now - _last_warning >= 600:
            _last_warning = now
            logger.warning('trial_connect_reminder_redis_unavailable')
        return None
