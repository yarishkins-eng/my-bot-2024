from __future__ import annotations

from dataclasses import dataclass

from .types import EntitlementSnapshot, compare_snapshots


@dataclass(frozen=True, slots=True)
class ShadowMetric:
    state: str
    mismatch_fields: tuple[str, ...]
    desired_hash_prefix: str
    observed_hash_prefix: str


class ReadOnlyShadowEvaluator:
    """Pure evaluator: no database/session/client mutation method is accepted."""

    @staticmethod
    def evaluate(desired: EntitlementSnapshot, observed: EntitlementSnapshot | None) -> ShadowMetric:
        if observed is None:
            return ShadowMetric('missing', ('panel_uuid',), desired.desired_hash[:12], 'absent')
        comparison = compare_snapshots(desired, observed)
        return ShadowMetric(
            'exact' if comparison.exact else 'drift',
            comparison.mismatch_fields,
            comparison.desired_hash[:12],
            comparison.observed_hash[:12],
        )
