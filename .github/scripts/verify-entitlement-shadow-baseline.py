#!/usr/bin/env python3
"""Verify that dotenv cannot override the safe Gate 2 code defaults."""

from __future__ import annotations

import argparse
import re
import stat
import sys
from pathlib import Path


SAFE_BASELINE = {
    'ENTITLEMENT_AUTHORITY_CHECKOUT_ADMISSION_ENABLED': 'false',
    'ENTITLEMENT_AUTHORITY_PROJECTOR_ENABLED': 'false',
    'ENTITLEMENT_AUTHORITY_READY_NOTIFICATIONS_ENABLED': 'false',
    'ENTITLEMENT_AUTHORITY_SHADOW_ENABLED': 'false',
    'ENTITLEMENT_AUTHORITY_SHADOW_KILL_SWITCH': 'true',
}
_ASSIGNMENT = re.compile(r'^([A-Z][A-Z0-9_]*)=(true|false)(?:\r?\n)?$')


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
        lines = raw.decode('utf-8').splitlines(keepends=True)
    except UnicodeDecodeError as error:
        raise BaselineRefused('env_not_utf8') from error

    values: dict[str, list[str]] = {key: [] for key in SAFE_BASELINE}
    for line in lines:
        match = _ASSIGNMENT.fullmatch(line)
        if match is not None and match.group(1) in values:
            values[match.group(1)].append(match.group(2))

    if any(len(found) > 1 for found in values.values()):
        raise BaselineRefused('managed_flag_duplicate')
    if any(found and found[0] != SAFE_BASELINE[key] for key, found in values.items()):
        raise BaselineRefused('managed_flag_not_safe')


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
