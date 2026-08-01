"""Short, direct-sale-only recovery loop for Device-First v2.

The general monitoring cycle intentionally remains coarse.  This worker has a
small bounded batch and only touches rows explicitly labelled
``direct_purchase_v2``; legacy checkout and generic balance recovery stay on
their established paths.
"""

from __future__ import annotations

import asyncio

import structlog

from app.config import settings
from app.database.database import AsyncSessionLocal
from app.services.device_first_checkout_service import (
    process_device_first_notification_outbox,
    process_direct_provisioning_outbox,
)
from app.services.device_first_payment_service import reconcile_device_first_payments


logger = structlog.get_logger(__name__)


class DeviceFirstRecoveryService:
    """Recover direct receipts, provisioning and notifications without global polling."""

    def __init__(self) -> None:
        self._running = False

    @property
    def is_running(self) -> bool:
        return self._running

    async def run_once(self, *, bot) -> tuple[int, int, int]:
        """Run one bounded v2-only recovery pass in one isolated DB session."""
        async with AsyncSessionLocal() as db:
            reconciled = await reconcile_device_first_payments(db, limit=20, direct_only=True)
            provisioned = await process_direct_provisioning_outbox(db, limit=20)
            notified = await process_device_first_notification_outbox(db, bot=bot, limit=20)
        return reconciled, provisioned, notified

    async def start(self, *, bot) -> None:
        if self._running:
            logger.warning('device_first_recovery_worker_already_running')
            return
        self._running = True
        interval_seconds = settings.get_device_first_recovery_worker_interval_seconds()
        logger.info('device_first_recovery_worker_started', interval_seconds=interval_seconds)
        try:
            while self._running:
                try:
                    await self.run_once(bot=bot)
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    logger.error('device_first_recovery_worker_cycle_failed', error=type(error).__name__)
                await asyncio.sleep(interval_seconds)
        except asyncio.CancelledError:
            raise
        finally:
            self._running = False
            logger.info('device_first_recovery_worker_stopped')

    def stop(self) -> None:
        self._running = False


device_first_recovery_service = DeviceFirstRecoveryService()
