from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any


_STATUSES = {'ACTIVE', 'DISABLED', 'LIMITED', 'EXPIRED'}
_STRATEGIES = {'NO_RESET', 'DAY', 'WEEK', 'MONTH', 'MONTH_ROLLING'}


def normalize_utc(value: datetime | str) -> datetime:
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00')) if isinstance(value, str) else value
    if parsed.tzinfo is None:
        raise ValueError('entitlement timestamps must be timezone-aware')
    return parsed.astimezone(UTC)


def _uuid_set(values: Iterable[object] | None) -> tuple[str, ...]:
    if values is None:
        raise ValueError('internal squads must be explicit, including an empty list')
    normalized = tuple(sorted({str(item) for item in values}))
    if any(not item for item in normalized):
        raise ValueError('internal squad identifiers must be non-empty')
    return normalized


@dataclass(frozen=True, slots=True)
class EntitlementSnapshot:
    owner_key: str
    panel_uuid: str | None
    status: str
    expire_at: datetime
    traffic_limit_bytes: int
    traffic_limit_strategy: str
    hwid_device_limit: int | None
    internal_squads: tuple[str, ...]
    external_squad_uuid: str | None
    provenance: str
    generation: int
    reset_epoch: int = 0
    revoke_epoch: int = 0
    deny_overlays: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, 'status', self.status.upper())
        object.__setattr__(self, 'traffic_limit_strategy', self.traffic_limit_strategy.upper())
        object.__setattr__(self, 'expire_at', normalize_utc(self.expire_at))
        object.__setattr__(self, 'internal_squads', _uuid_set(self.internal_squads))
        object.__setattr__(self, 'deny_overlays', tuple(sorted(set(self.deny_overlays))))
        if not self.owner_key or not self.provenance:
            raise ValueError('owner and provenance must be explicit')
        if self.status not in _STATUSES:
            raise ValueError(f'unsupported status: {self.status}')
        if self.traffic_limit_strategy not in _STRATEGIES:
            raise ValueError(f'unsupported traffic strategy: {self.traffic_limit_strategy}')
        if self.traffic_limit_bytes < 0:
            raise ValueError('traffic bytes cannot be negative; zero means unlimited')
        if self.hwid_device_limit is not None and self.hwid_device_limit < 0:
            raise ValueError('HWID limit cannot be negative')
        if self.generation <= 0 or self.reset_epoch < 0 or self.revoke_epoch < 0:
            raise ValueError('generation must be positive and epochs non-negative')

    def canonical(self) -> dict[str, Any]:
        return {
            'deny_overlays': list(self.deny_overlays),
            'expire_at': self.expire_at.isoformat(timespec='microseconds').replace('+00:00', 'Z'),
            'external_squad_uuid': self.external_squad_uuid,
            'generation': self.generation,
            'hwid_device_limit': self.hwid_device_limit,
            'internal_squads': list(self.internal_squads),
            'owner_key': self.owner_key,
            'panel_uuid': self.panel_uuid,
            'provenance': self.provenance,
            'reset_epoch': self.reset_epoch,
            'revoke_epoch': self.revoke_epoch,
            'status': self.status,
            'traffic_limit_bytes': self.traffic_limit_bytes,
            'traffic_limit_strategy': self.traffic_limit_strategy,
        }

    @property
    def desired_hash(self) -> str:
        raw = json.dumps(self.canonical(), ensure_ascii=True, sort_keys=True, separators=(',', ':'))
        return hashlib.sha256(raw.encode()).hexdigest()

    def bind(self, panel_uuid: str) -> EntitlementSnapshot:
        if not panel_uuid:
            raise ValueError('Panel UUID must be non-empty')
        return replace(self, panel_uuid=panel_uuid)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> EntitlementSnapshot:
        required = {
            'owner_key',
            'panel_uuid',
            'status',
            'expire_at',
            'traffic_limit_bytes',
            'traffic_limit_strategy',
            'hwid_device_limit',
            'internal_squads',
            'external_squad_uuid',
            'provenance',
            'generation',
        }
        missing = required - value.keys()
        if missing:
            raise ValueError(f'missing exact entitlement fields: {sorted(missing)}')
        return cls(
            owner_key=str(value['owner_key']),
            panel_uuid=str(value['panel_uuid']) if value['panel_uuid'] is not None else None,
            status=str(value['status']),
            expire_at=normalize_utc(value['expire_at']),
            traffic_limit_bytes=int(value['traffic_limit_bytes']),
            traffic_limit_strategy=str(value['traffic_limit_strategy']),
            hwid_device_limit=(int(value['hwid_device_limit']) if value['hwid_device_limit'] is not None else None),
            internal_squads=_uuid_set(value['internal_squads']),
            external_squad_uuid=(
                str(value['external_squad_uuid']) if value['external_squad_uuid'] is not None else None
            ),
            provenance=str(value['provenance']),
            generation=int(value['generation']),
            reset_epoch=int(value.get('reset_epoch', 0)),
            revoke_epoch=int(value.get('revoke_epoch', 0)),
            deny_overlays=tuple(str(item) for item in value.get('deny_overlays', ())),
        )


_COMPARISON_FIELDS = tuple(EntitlementSnapshot.__dataclass_fields__)


@dataclass(frozen=True, slots=True)
class SnapshotComparison:
    exact: bool
    mismatch_fields: tuple[str, ...]
    desired_hash: str
    observed_hash: str


def compare_snapshots(desired: EntitlementSnapshot, observed: EntitlementSnapshot) -> SnapshotComparison:
    mismatch = tuple(field for field in _COMPARISON_FIELDS if getattr(desired, field) != getattr(observed, field))
    return SnapshotComparison(
        exact=not mismatch,
        mismatch_fields=mismatch,
        desired_hash=desired.desired_hash,
        observed_hash=observed.desired_hash,
    )
