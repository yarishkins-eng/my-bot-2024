import structlog

from app.utils.funnel_notify import _get_redis


logger = structlog.get_logger(__name__)
MAX_LATENESS = 30 * 60


async def trial_connect_queue(method: str, *args, **kwargs):
    client = _get_redis()
    if client is None:
        return None
    try:
        return await getattr(client, method)('trial_connect_reminder_due', *args, **kwargs)
    except Exception as exc:
        logger.warning('Очередь напоминания о подключении недоступна', error=str(exc))
        return None
