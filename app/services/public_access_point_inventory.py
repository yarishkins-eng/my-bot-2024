"""Pure, read-only inventory validation for subscription-visible access points.

The RemnaWave API is deliberately kept outside this module.  A caller supplies
one coherent, typed snapshot collected by a GET-only adapter; this code then
decides whether a Host title may enter the local catalog.  No panel identity,
internal Squad key or Raw member leaves this server-side boundary.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from hashlib import sha256


@dataclass(frozen=True)
class InventoryHost:
    """Minimal Host shape required for access-point discovery.

    ``host_key`` and ``squad_keys`` are server-side identities.  They must
    never be serialised by a cabinet or user endpoint.
    """

    host_key: str
    title: str
    squad_keys: tuple[str, ...]
    is_hidden: bool = False
    is_disabled: bool = False


@dataclass(frozen=True)
class InventorySquad:
    """Read-only evidence for one customer Internal Squad."""

    key: str
    is_dedicated: bool
    is_healthy: bool
    raw_members: tuple[str, ...]


@dataclass(frozen=True)
class InventorySnapshot:
    """A coherent snapshot returned by an injectable read-only client."""

    revision: str | None
    hosts: tuple[InventoryHost, ...]
    squads: tuple[InventorySquad, ...]

    @property
    def fingerprint(self) -> str:
        payload = {
            'hosts': [
                {
                    'host_key': host.host_key,
                    'title': host.title,
                    'squad_keys': list(host.squad_keys),
                    'is_hidden': host.is_hidden,
                    'is_disabled': host.is_disabled,
                }
                for host in sorted(self.hosts, key=lambda item: item.host_key)
            ],
            'squads': [
                {
                    'key': squad.key,
                    'is_dedicated': squad.is_dedicated,
                    'is_healthy': squad.is_healthy,
                    'raw_members': list(squad.raw_members),
                }
                for squad in sorted(self.squads, key=lambda item: item.key)
            ],
        }
        return sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


@dataclass(frozen=True)
class AccessPointCandidate:
    """Validated server-side candidate; safe fields are a strict subset."""

    host_key: str
    title: str
    state: str
    reason: str | None
    assignable: bool
    squad_keys: tuple[str, ...]
    graph_fingerprint: str
    entitlement_fingerprint: str
    inventory_revision: str | None


@dataclass(frozen=True)
class InventoryAssessment:
    revision: str | None
    fingerprint: str
    points: tuple[AccessPointCandidate, ...]


@dataclass(frozen=True)
class CatalogUpdate:
    """A local diff that deliberately contains no raw identity in its UI DTO."""

    host_key: str
    presentation_changed: bool
    entitlement_changed: bool

    @property
    def invalidate_unpaid_quote(self) -> bool:
        return self.entitlement_changed


def _graph_fingerprint(host: InventoryHost, squads: dict[str, InventorySquad]) -> str:
    payload = {
        'squads': [
            {
                'key': key,
                'is_dedicated': squads[key].is_dedicated,
                'is_healthy': squads[key].is_healthy,
                'raw_members': list(squads[key].raw_members),
            }
            for key in sorted(host.squad_keys)
            if key in squads
        ]
    }
    return sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _entitlement_fingerprint(
    host: InventoryHost,
    *,
    graph_fingerprint: str,
    state: str,
    reason: str | None,
) -> str:
    """Fingerprint only entitlement-relevant evidence for one point.

    A catalog-wide snapshot hash is useful to prove a coherent discovery read,
    but cannot be used as a tariff-policy fence: adding an unrelated Host or
    renaming this Host would otherwise invalidate every unpaid quote.  The
    immutable policy instead captures this point-local evidence, deliberately
    excluding the mutable subscription title.
    """

    payload = {
        'host_key': host.host_key,
        'graph_fingerprint': graph_fingerprint,
        'state': state,
        'reason': reason,
    }
    return sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def assess_inventory(snapshot: InventorySnapshot) -> InventoryAssessment:
    """Validate a Host -> dedicated Internal Squad graph without side effects.

    A candidate is unsafe by default.  It becomes ``verified`` only when it
    has a stable Host identity and non-empty exact title, is visible/enabled,
    every linked Squad exists/is healthy/is dedicated/has Raw evidence, and no
    linked Squad is shared by another independently named Host.
    """

    squads = {squad.key: squad for squad in snapshot.squads if squad.key}
    # Keep the Host title exactly as the panel supplied it for presentation,
    # while treating whitespace-only variants as a duplicate safety conflict.
    title_counts = Counter(host.title.strip() for host in snapshot.hosts if host.title.strip())
    squad_hosts: dict[str, set[str]] = defaultdict(set)
    for host in snapshot.hosts:
        for key in set(host.squad_keys):
            if key:
                squad_hosts[key].add(host.host_key)

    points: list[AccessPointCandidate] = []
    for host in sorted(snapshot.hosts, key=lambda item: item.host_key):
        title = host.title
        reasons: list[str] = []
        if not host.host_key:
            reasons.append('missing_host_identity')
        if not title.strip():
            reasons.append('empty_title')
        elif title_counts[title.strip()] != 1:
            reasons.append('duplicate_title')
        if host.is_hidden:
            reasons.append('hidden_host')
        if host.is_disabled:
            reasons.append('disabled_host')
        if not host.squad_keys:
            reasons.append('missing_squad_mapping')

        for key in sorted(set(host.squad_keys)):
            squad = squads.get(key)
            if squad is None:
                reasons.append('unknown_squad')
                continue
            if not squad.is_dedicated:
                reasons.append('non_dedicated_squad')
            if not squad.is_healthy:
                reasons.append('unhealthy_squad')
            if not squad.raw_members:
                reasons.append('unknown_raw_membership')
            if len(squad_hosts[key]) != 1:
                reasons.append('shared_squad_mapping')

        graph_fingerprint = _graph_fingerprint(host, squads)
        state = 'verified' if not reasons else 'needs_verification'
        reason = ','.join(sorted(set(reasons))) or None
        points.append(
            AccessPointCandidate(
                host_key=host.host_key,
                title=title,
                state=state,
                reason=reason,
                assignable=state == 'verified',
                squad_keys=tuple(sorted(set(host.squad_keys))),
                graph_fingerprint=graph_fingerprint,
                entitlement_fingerprint=_entitlement_fingerprint(
                    host,
                    graph_fingerprint=graph_fingerprint,
                    state=state,
                    reason=reason,
                ),
                inventory_revision=snapshot.revision,
            )
        )
    return InventoryAssessment(snapshot.revision, snapshot.fingerprint, tuple(points))


def plan_catalog_update(
    previous: tuple[AccessPointCandidate, ...] | list[AccessPointCandidate],
    current: tuple[AccessPointCandidate, ...] | list[AccessPointCandidate],
) -> tuple[CatalogUpdate, ...]:
    """Classify local mutations so a Host rename never invalidates a quote."""

    previous_by_host = {point.host_key: point for point in previous}
    updates = []
    for point in current:
        old = previous_by_host.get(point.host_key)
        updates.append(
            CatalogUpdate(
                host_key=point.host_key,
                presentation_changed=old is not None and old.title != point.title,
                entitlement_changed=old is None
                or old.graph_fingerprint != point.graph_fingerprint
                or old.entitlement_fingerprint != point.entitlement_fingerprint
                or old.state != point.state
                or old.assignable != point.assignable,
            )
        )
    return tuple(updates)
