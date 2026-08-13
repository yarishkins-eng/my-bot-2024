# Incident report: false-ready entitlement projection

## Finding

Current `origin/main` can mark a paid Direct v2 checkout ready without proving
that the current Panel identity exactly matches the paid entitlement.

Minimal path:

1. `process_direct_provisioning_outbox()` calls
   `SubscriptionService.ensure_subscription_synced()`.
2. If local UUID and subscription URL already exist, that method performs only
   a GET/existence check.
3. A found identity is accepted without comparing status, expiry, traffic,
   device limit, internal squads, external squad, owner, or current generation.
4. A GET exception is logged and also accepted: `needs_sync` remains false.
5. The worker writes outbox `done`, checkout `provisioning_state=ready`, and
   checkout `lifecycle_state=ready`; the ready notification may then be sent.

The initial Phase 0 production evidence confirmed that the unsafe state was
observable, while commercial impact remained quarantined: five Direct v2
checkouts were `ready`; one of those five differed from Panel in access fields.
Its historical immutable sale/provider evidence did not pass today's full
validator, so the owner-only report correctly called it a candidate requiring
investigation—not a proven affected customer and not authority for repair.

The owner then identified that sole candidate as their own test account and
test purchase and separately authorized a clean account erasure while retaining
the confirmed Platega payment. At 2026-08-12 18:21:14Z–18:21:16Z the application
workflow anonymized the production bot profile and removed the RemnaWave
identity. Those were production mutations outside the original read-only Phase
0 audit; Platega received no refund/cancel/write. Independent verification at
18:25:31Z passed, and a new strictly read-only rescan at
20:27:51Z–20:29:55Z found **0 current Direct-ready access contradictions and 0
current quarantine cases**.

## Independent P0 mechanisms

- Mutating HTTP retries: `_make_request()` retries POST/PATCH/DELETE/actions on
  429/502/503/504 and `aiohttp.ClientError`; remote outcome may already exist.
- Blind recovery: `ensure_subscription_synced()` falls from failed update to
  clearing the UUID and CREATE. A lost PATCH response can therefore create a
  second identity.
- Field degradation: A039 causes CREATE/PATCH to be repeated after silently
  removing `externalSquadUuid`, then the degraded PATCH can be reported as
  success.
- Stale authority: `user.modified` webhook imports Panel expiry, status,
  traffic limit and URL into mutable commercial subscription rows. Reordered
  webhook observations can roll back later authorized state.
- Identity ambiguity: single-mode discovery accepts `existing_users[0]` from
  Telegram/email lookup. No exact owner/generation quarantine is required.
- Lock/HTTP coupling: normal Panel writes hold `User FOR UPDATE` during HTTP;
  the AP projection holds Subscription/term/outbox locks during HTTP and then
  reaches for User. This supplies the real `Subscription → User` half of the
  cycle against ordinary `User → Subscription` financial flows.

## Classification

- `proven`: current-main false-ready, mutating retry, A039 degradation, blind
  second POST, webhook commercial overwrite, lock-order cycle.
- `historical observed/quarantine`: one Direct-ready production contradiction
  in the initial Phase 0 scan; immutable historical commercial provenance was
  insufficient to authorize repair or call customer impact proven.
- `current observed/quarantine`: zero after the separately authorized terminal
  erasure and fresh GET/SELECT-only rescan. The erased historical checkout is
  retained financial evidence, not a live entitlement to restore.
- `unknown`: exact remote state immediately after a lost CREATE/PATCH response,
  late request application, and action idempotency, because no isolated
  non-production RemnaWave 2.8.1 mutation target was approved.
- `quarantine`: any shared/cross-owner identity, duplicate deterministic
  lookup, ambiguous POST, owner mismatch, or late write must stop projection
  and ready notification pending operator review.

## Containment plan — prepared, not applied

Three independent owner actions, in order of narrowness:

1. New checkout creation: keep
   `DEVICE_FIRST_NEW_CHECKOUTS_ENABLED=false` and
   `DEVICE_FIRST_PUBLIC_ROLLOUT_ENABLED=false`.
2. Projection/provisioning: add a dedicated worker kill switch before any
   future rollout; current code has no clean independent switch.
3. Ready notifications: add a dedicated ready-notification kill switch; do not
   rely on stopping the projection worker.

The historical test checkout is closed and no longer awaits repair or release
from quarantine. Its payment evidence remains retained, while the account and
RemnaWave identity are intentionally absent. A future occurrence must still
follow immutable payment/entitlement and deny/reversal review; this historical
report never authorizes automatic re-projection, refund or revoke.
