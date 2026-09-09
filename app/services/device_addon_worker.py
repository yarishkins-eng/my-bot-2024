"""Durable, narrow RemnaWave fulfillment for paid device add-on intents."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import or_, select

from app.config import settings
from app.database.database import AsyncSessionLocal
from app.database.models import DeviceAddonIntent, Subscription, User
from app.services.account_test_reset_service import reset_is_busy, reset_lock
from app.services.device_addon_service import random_lease_token


if TYPE_CHECKING:
    from aiogram import Bot


logger = structlog.get_logger(__name__)
_LEASE_SECONDS = 45
_MAX_AUTOMATIC_ATTEMPTS = 5


class DeviceAddonWorker:
    """Claims one committed intent at a time and PATCHes only HWID limit."""

    def __init__(self) -> None:
        self._running = False
        self._task: asyncio.Task | None = None
        self._wakeup = asyncio.Event()
        self._bot: Bot | None = None

    async def start(self, *, bot: Bot | None = None) -> None:
        self._bot = bot
        self._running = True
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    def stop(self) -> None:
        self._running = False
        self._wakeup.set()
        if self._task is not None and not self._task.done():
            self._task.cancel()

    def wake(self) -> None:
        self._wakeup.set()

    async def _loop(self) -> None:
        while self._running:
            try:
                await self.run_once(limit=10)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception('device_addon_worker_cycle_failed')
            self._wakeup.clear()
            try:
                await asyncio.wait_for(
                    self._wakeup.wait(), timeout=max(1, settings.DEVICE_ADDON_WORKER_INTERVAL_SECONDS)
                )
            except TimeoutError:
                pass

    async def run_once(self, *, limit: int = 10) -> int:
        """Run payment recovery then claim and fulfill a bounded batch."""
        from app.services.device_addon_payment_service import recover_device_addon_payments

        async with AsyncSessionLocal() as db:
            await recover_device_addon_payments(db, limit=limit, bot=self._bot)
        count = 0
        for _ in range(limit):
            claim = await self._claim_one()
            if claim is None:
                break
            count += 1
            await self._fulfill_claim(*claim)
        return count

    async def _claim_one(self) -> tuple[int, str, int] | None:
        now = datetime.now(UTC)
        async with AsyncSessionLocal() as db:
            intent = (
                await db.execute(
                    select(DeviceAddonIntent)
                    .where(
                        DeviceAddonIntent.purchase_state == 'purchased',
                        DeviceAddonIntent.fulfillment_state == 'pending',
                        DeviceAddonIntent.next_attempt_at <= now,
                        or_(DeviceAddonIntent.lease_expires_at.is_(None), DeviceAddonIntent.lease_expires_at < now),
                    )
                    .order_by(DeviceAddonIntent.next_attempt_at, DeviceAddonIntent.id)
                    .with_for_update(skip_locked=True)
                    .limit(1)
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
            if intent is None:
                return None
            if intent.fulfillment_attempts >= _MAX_AUTOMATIC_ATTEMPTS:
                intent.fulfillment_state = 'needs_attention'
                intent.fulfillment_error_code = 'automatic_attempts_exhausted'
                await db.commit()
                return None
            token = random_lease_token()
            intent.fulfillment_attempts += 1
            intent.lease_epoch += 1
            intent.lease_token = token
            intent.lease_expires_at = now + timedelta(seconds=_LEASE_SECONDS)
            await db.commit()
            return int(intent.id), token, int(intent.lease_epoch)

    async def _mark_claim(
        self,
        intent_id: int,
        token: str,
        epoch: int,
        *,
        state: str,
        error_code: str | None = None,
        delay: timedelta | None = None,
    ) -> None:
        async with AsyncSessionLocal() as db:
            intent = (
                await db.execute(
                    select(DeviceAddonIntent)
                    .where(
                        DeviceAddonIntent.id == intent_id,
                        DeviceAddonIntent.lease_token == token,
                        DeviceAddonIntent.lease_epoch == epoch,
                    )
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
            if intent is None:
                return
            intent.lease_token = None
            intent.lease_expires_at = None
            intent.fulfillment_state = state
            intent.fulfillment_error_code = error_code
            if state == 'ready':
                intent.fulfilled_at = datetime.now(UTC)
            elif delay is not None:
                intent.next_attempt_at = datetime.now(UTC) + delay
            await db.commit()

    async def _load_target(self, *, intent_id: int, token: str, epoch: int):
        """Take only DB locks; callers release them before external HTTP."""
        async with AsyncSessionLocal() as db:
            # A stub locates the owner.  It takes no row lock, so the durable
            # order below stays U -> S -> I everywhere this worker writes.
            stub = (
                await db.execute(select(DeviceAddonIntent).where(DeviceAddonIntent.id == intent_id))
            ).scalar_one_or_none()
            if stub is None:
                return None
            if stub.subscription_id is None or int(stub.subscription_id) != int(stub.target_subscription_id):
                return None
            user = (
                await db.execute(
                    select(User)
                    .where(User.id == stub.user_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
            if user is None:
                return None
            subscription = (
                await db.execute(
                    select(Subscription)
                    .where(Subscription.id == stub.subscription_id, Subscription.user_id == user.id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
            intent = (
                await db.execute(
                    select(DeviceAddonIntent)
                    .where(
                        DeviceAddonIntent.id == intent_id,
                        DeviceAddonIntent.lease_token == token,
                        DeviceAddonIntent.lease_epoch == epoch,
                        DeviceAddonIntent.purchase_state == 'purchased',
                    )
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
            if (
                intent is None
                or subscription is None
                or int(intent.subscription_id or 0) != int(intent.target_subscription_id)
                or reset_is_busy(user)
                or user.status != 'active'
                or getattr(user, 'restriction_subscription', False)
                or getattr(user, 'account_erasure_requested_at', None) is not None
                or int(user.device_addon_generation or 0) != int(intent.device_addon_generation)
                or subscription.status not in {'active', 'trial'}
                or subscription.end_date <= datetime.now(UTC)
            ):
                return None
            panel_uuid = subscription.remnawave_uuid if settings.is_multi_tariff_enabled() else user.remnawave_uuid
            if not panel_uuid or panel_uuid != intent.panel_uuid:
                return None
            return user.id, panel_uuid, int(subscription.device_limit or 1)

    async def _target_still_current(
        self, *, intent_id: int, token: str, epoch: int, expected_uuid: str, expected_limit: int
    ) -> bool:
        target = await self._load_target(intent_id=intent_id, token=token, epoch=epoch)
        return target == (target[0], expected_uuid, expected_limit) if target is not None else False

    async def _finalize_ready(
        self, *, intent_id: int, token: str, epoch: int, expected_uuid: str, expected_limit: int
    ) -> bool:
        """Recheck and mark ready in one U -> S -> I transaction."""
        async with AsyncSessionLocal() as db:
            stub = (
                await db.execute(select(DeviceAddonIntent).where(DeviceAddonIntent.id == intent_id))
            ).scalar_one_or_none()
            if stub is None:
                return False
            if stub.subscription_id is None or int(stub.subscription_id) != int(stub.target_subscription_id):
                return False
            user = (
                await db.execute(
                    select(User)
                    .where(User.id == stub.user_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
            if user is None:
                return False
            subscription = (
                await db.execute(
                    select(Subscription)
                    .where(Subscription.id == stub.subscription_id, Subscription.user_id == user.id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
            intent = (
                await db.execute(
                    select(DeviceAddonIntent)
                    .where(
                        DeviceAddonIntent.id == intent_id,
                        DeviceAddonIntent.lease_token == token,
                        DeviceAddonIntent.lease_epoch == epoch,
                        DeviceAddonIntent.purchase_state == 'purchased',
                    )
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
            panel_uuid = (
                subscription.remnawave_uuid
                if subscription is not None and settings.is_multi_tariff_enabled()
                else (user.remnawave_uuid if user is not None else None)
            )
            if (
                intent is None
                or subscription is None
                or int(intent.subscription_id or 0) != int(intent.target_subscription_id)
                or reset_is_busy(user)
                or user.status != 'active'
                or getattr(user, 'restriction_subscription', False)
                or getattr(user, 'account_erasure_requested_at', None) is not None
                or int(user.device_addon_generation or 0) != int(intent.device_addon_generation)
                or subscription.status not in {'active', 'trial'}
                or subscription.end_date <= datetime.now(UTC)
                or panel_uuid != expected_uuid
                or int(subscription.device_limit or 1) != expected_limit
            ):
                return False
            intent.lease_token = None
            intent.lease_expires_at = None
            intent.fulfillment_state = 'ready'
            intent.fulfillment_error_code = None
            intent.fulfilled_at = datetime.now(UTC)
            await db.commit()
            return True

    async def _fulfill_claim(self, intent_id: int, token: str, epoch: int) -> None:
        target = await self._load_target(intent_id=intent_id, token=token, epoch=epoch)
        if target is None:
            await self._mark_claim(intent_id, token, epoch, state='needs_attention', error_code='stale_or_reset_target')
            return
        user_id, panel_uuid, target_limit = target
        # The reset advisory lock serializes the exact external PATCH with the
        # reset marker.  No row lock spans network IO.
        async with AsyncSessionLocal() as db:
            async with reset_lock(db, user_id) as acquired:
                if not acquired:
                    await self._mark_claim(
                        intent_id,
                        token,
                        epoch,
                        state='pending',
                        error_code='reset_lock_busy',
                        delay=timedelta(seconds=10),
                    )
                    return
                if not await self._target_still_current(
                    intent_id=intent_id,
                    token=token,
                    epoch=epoch,
                    expected_uuid=panel_uuid,
                    expected_limit=target_limit,
                ):
                    await self._mark_claim(
                        intent_id, token, epoch, state='needs_attention', error_code='stale_before_panel_patch'
                    )
                    return
                try:
                    from app.services.remnawave_service import RemnaWaveService

                    service = RemnaWaveService()
                    async with service.get_api_client() as api:
                        updated = await api.update_user(uuid=panel_uuid, hwid_device_limit=target_limit)
                    if (
                        str(getattr(updated, 'uuid', '')) != panel_uuid
                        or int(getattr(updated, 'hwid_device_limit', -1)) != target_limit
                    ):
                        raise RuntimeError('panel_hwid_or_identity_echo_mismatch')
                except Exception as error:
                    logger.warning('device_addon_panel_patch_failed', intent_id=intent_id, error=str(error))
                    await self._mark_claim(
                        intent_id,
                        token,
                        epoch,
                        state='pending',
                        error_code='panel_patch_failed',
                        delay=timedelta(seconds=min(300, 2 ** min(epoch, 8))),
                    )
                    return
                if not await self._finalize_ready(
                    intent_id=intent_id, token=token, epoch=epoch, expected_uuid=panel_uuid, expected_limit=target_limit
                ):
                    # An administrator can lower the limit while the old PATCH
                    # is in flight.  Restore the now-current value only when
                    # the same live panel identity still owns it; the original
                    # intent remains non-ready and is reviewable.
                    current = await self._load_target(intent_id=intent_id, token=token, epoch=epoch)
                    if current is not None and current[1] == panel_uuid:
                        try:
                            service = RemnaWaveService()
                            async with service.get_api_client() as api:
                                echoed = await api.update_user(uuid=panel_uuid, hwid_device_limit=current[2])
                            if (
                                str(getattr(echoed, 'uuid', '')) != panel_uuid
                                or int(getattr(echoed, 'hwid_device_limit', -1)) != current[2]
                            ):
                                raise RuntimeError('panel_hwid_or_identity_compensation_echo_mismatch')
                        except Exception as error:
                            logger.warning(
                                'device_addon_panel_compensation_failed', intent_id=intent_id, error=str(error)
                            )
                    await self._mark_claim(
                        intent_id, token, epoch, state='needs_attention', error_code='stale_after_panel_patch'
                    )
                    return


device_addon_worker = DeviceAddonWorker()
