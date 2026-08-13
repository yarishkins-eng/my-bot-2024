#!/usr/bin/env python3
"""Lease-bound entrypoint for the isolated Gate 2 read-only sidecar.

The bot application is intentionally not started.  This process runs only the
reviewed read-only shadow service and exits fail-closed when the host-owned
lease is malformed, superseded, or expired.  The container has restart policy
``no``, so a shadow circuit STOP is durable across process/container restarts.
"""

from __future__ import annotations

import asyncio
import os
import stat
import time
from pathlib import Path

import structlog

from app.services.entitlement_authority.shadow_runtime import build_production_shadow_service


logger = structlog.get_logger(__name__)
_LEASE_CHECK_SECONDS = 2.0
_ALLOWED_KEYS = {
    'format_version',
    'phase',
    'action',
    'runtime_mode',
    'policy_version',
    'workflow_sha',
    'deployed_sha',
    'image',
    'workflow_run_id',
    'workflow_run_attempt',
    'approval_actor',
    'release_card_reference',
    'expires_epoch',
    'completed_at',
}


class ShadowLeaseRefused(RuntimeError):
    """The host-owned shadow lease is not safe to honor."""


def _required_environment(name: str) -> str:
    value = os.environ.get(name, '')
    if not value:
        raise ShadowLeaseRefused(f'missing_{name.lower()}')
    return value


def _read_lease(path: Path) -> dict[str, str]:
    try:
        metadata = path.stat()
        raw = path.read_text(encoding='utf-8')
    except (OSError, UnicodeError) as error:
        raise ShadowLeaseRefused('lease_unreadable') from error
    if not stat.S_ISREG(metadata.st_mode):
        raise ShadowLeaseRefused('lease_not_regular')

    values: dict[str, str] = {}
    for line in raw.splitlines():
        if not line or '=' not in line:
            raise ShadowLeaseRefused('lease_malformed')
        key, value = line.split('=', 1)
        if key not in _ALLOWED_KEYS or key in values or not value or any(character.isspace() for character in value):
            raise ShadowLeaseRefused('lease_malformed')
        values[key] = value
    if set(values) != _ALLOWED_KEYS:
        raise ShadowLeaseRefused('lease_malformed')
    return values


def _lease_is_valid(path: Path) -> bool:
    try:
        values = _read_lease(path)
        expected = {
            'format_version': '2',
            'action': 'ENABLE_SHADOW',
            'runtime_mode': 'enabled',
            'policy_version': 'gate2-readonly-v1',
            'workflow_sha': _required_environment('TEPLO_SHADOW_WORKFLOW_SHA'),
            'deployed_sha': _required_environment('TEPLO_SHADOW_DEPLOYED_SHA'),
            'image': _required_environment('TEPLO_SHADOW_IMAGE_ID'),
            'workflow_run_id': _required_environment('TEPLO_SHADOW_WORKFLOW_RUN_ID'),
            'workflow_run_attempt': _required_environment('TEPLO_SHADOW_WORKFLOW_RUN_ATTEMPT'),
        }
        if any(values[key] != value for key, value in expected.items()):
            return False
        if values['phase'] not in {'prepared', 'completed'}:
            return False
        if values['phase'] == 'prepared' and values['completed_at'] != 'pending':
            return False
        if values['phase'] == 'completed' and values['completed_at'] == 'pending':
            return False
        expires_epoch = int(values['expires_epoch'])
    except (ShadowLeaseRefused, ValueError):
        return False
    return expires_epoch > int(time.time())


async def _wait_for_lease_loss(path: Path) -> None:
    while _lease_is_valid(path):
        await asyncio.sleep(_LEASE_CHECK_SECONDS)


async def _run() -> int:
    lease_path = Path(_required_environment('TEPLO_SHADOW_LEASE_FILE'))
    if not _lease_is_valid(lease_path):
        raise ShadowLeaseRefused('lease_not_active')
    service = build_production_shadow_service()
    if service is None:
        logger.warning('entitlement_shadow_sidecar_refused')
        return 64

    service_task = asyncio.create_task(service.run())
    lease_task = asyncio.create_task(_wait_for_lease_loss(lease_path))
    done, _ = await asyncio.wait({service_task, lease_task}, return_when=asyncio.FIRST_COMPLETED)

    if lease_task in done:
        logger.warning('entitlement_shadow_lease_lost')
        service.stop()
        try:
            await asyncio.wait_for(service_task, timeout=5.0)
        except TimeoutError:
            service_task.cancel()
            await asyncio.gather(service_task, return_exceptions=True)
        return 75

    lease_task.cancel()
    await asyncio.gather(lease_task, return_exceptions=True)
    logger.warning('entitlement_shadow_sidecar_stopped')
    return 76


def main() -> int:
    try:
        return asyncio.run(_run())
    except (ShadowLeaseRefused, ValueError):
        logger.warning('entitlement_shadow_sidecar_refused')
        return 64


if __name__ == '__main__':
    raise SystemExit(main())
