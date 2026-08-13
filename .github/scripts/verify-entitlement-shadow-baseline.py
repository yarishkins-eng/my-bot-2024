#!/usr/bin/env python3
"""Fail closed when dotenv can influence the reviewed shadow contract."""

from __future__ import annotations

import argparse
import stat
import sys
from pathlib import Path


# Production currently has none of these names in dotenv.  Rejecting every
# case-insensitive mention is deliberately stricter than attempting to emulate
# the combined Docker Compose, python-dotenv and Pydantic parsing grammars.
_FORBIDDEN_TOKENS = (
    'ENTITLEMENT_AUTHORITY_CHECKOUT_ADMISSION_ENABLED',
    'ENTITLEMENT_AUTHORITY_PROJECTOR_ENABLED',
    'ENTITLEMENT_AUTHORITY_READY_NOTIFICATIONS_ENABLED',
    'ENTITLEMENT_AUTHORITY_SHADOW_',
    'MULTI_TARIFF_ENABLED',
)


class BaselineRefused(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def verify(path: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise BaselineRefused('env_unreadable') from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise BaselineRefused('env_not_regular_file')
    if stat.S_IMODE(metadata.st_mode) & 0o022:
        raise BaselineRefused('env_is_group_or_world_writable')
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise BaselineRefused('env_unreadable') from error
    if b'\x00' in raw:
        raise BaselineRefused('env_contains_nul')
    try:
        text = raw.decode('utf-8').upper()
    except UnicodeDecodeError as error:
        raise BaselineRefused('env_not_utf8') from error
    if any(token in text for token in _FORBIDDEN_TOKENS):
        raise BaselineRefused('managed_shadow_setting_present')


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--env-file', required=True)
    try:
        arguments = parser.parse_args()
        verify(Path(arguments.env_file))
    except BaselineRefused as error:
        print(f'STOP:{error.code}', file=sys.stderr)
        return 64
    except SystemExit:
        print('STOP:invalid_arguments', file=sys.stderr)
        return 64
    print('BASELINE_SAFE')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
