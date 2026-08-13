# ADR: dormant single entitlement authority

Status: **owner decisions approved; Gate 1 implementation only**.

## Identity and authority

- The current single-tariff canonical identity belongs to `User`. Enabling
  multi-tariff requires a new ADR/migration.
- Immutable payment/sale evidence, AP terms and explicit authorized commands
  own desired access. Mutable `Subscription` is a legacy projection.
- The coordinator loads the exact desired snapshot from the immutable source
  referenced by the command. A caller-supplied snapshot is only a matching
  assertion; it cannot authorize PATCH/CREATE or rewrite the durable hash.
  Source hash/provenance/generation and owner/Panel identity binding are
  revalidated at claim, mutation send-fence, UUID bind and finalization.
- Panel GET/webhook data is observation only. Conflicting provenance is
  `unknown/quarantined`; last-write-wins is forbidden.

## Exact desired state

The canonical snapshot includes owner fingerprint, Panel UUID, status, UTC
expiry, traffic bytes (`0=unlimited`) and strategy, nullable HWID limit, exact
internal squad set including `[]`, nullable external squad, provenance,
generation, reset/revoke epochs and active overlays. READY requires exact
normalized equality after a fresh canonical GET and a second local CAS.

Access precedence is:

```text
erasure/delete/reset/admin block/channel deny > LIMITED > grace > paid/trial grant
```

LIMITED survives ordinary renewal/retry and clears only after proven traffic
increase/reset or explicit admin clear. A financial reversal before VERIFIED
blocks projection; after VERIFIED its review hold is non-access-deny and emits
no automatic revoke/refund/disable.

## Remote fencing

```text
PENDING → CLAIMED → CREATING_DISABLED/UUID_BOUND/MUTATING
        → VERIFYING → VERIFIED/READY
any uncertain send → REMOTE_OUTCOME_UNKNOWN → QUARANTINED
```

Only one identity mutation may be active. A generation/lease send-fence is
committed immediately before HTTP; no PostgreSQL row lock spans HTTP. CREATE
has a durable deterministic intent, is always DISABLED, and its UUID must be
bound before ACTIVE PATCH. Lost CREATE/PATCH never causes a blind retry. A
takeover performs deterministic lookup/canonical GET only; without remote CAS
it cannot clear unknown outcome automatically.

The recovery/ownership key is the Panel-legal unique username
`te-<sha256(owner_key)[:32]>`. The exact value is used for CREATE, lookup and
canonical comparison. A mismatch raises a typed failure and quarantines the
identity; it is never represented by an in-band snapshot value. Panel UUID
binding is also unique in the authority table and protected by a UUID-scoped
PostgreSQL advisory lock, so two identities cannot bind the same remote user.

## Privacy

New tables contain no Telegram identity, name, username, email, subscription
URL, credentials, provider payload or raw webhook body. While an account
exists, the identity may retain `user_id` and operational Panel UUID.

Erasure is `erasure_requested → cleanup_terminal → final_erasure`. The first
stage increments generation, clears `user_id`/raw binding, rotates the owner
key, removes snapshots/observations/overlays/notification intents, unlinks
webhook evidence, and retains the cleanup UUID only AES-GCM encrypted plus
HMAC fingerprints. If no Panel binding exists and there is no unknown remote
outcome, the same transaction records `cleanup_terminal` with no ciphertext.
A preceding unknown mutation quarantines cleanup and blocks terminal state.
Verified DELETE/canonical 404 clears ciphertext immediately; terminal evidence
expires after 90 days, unresolved evidence does not. Once erasure begins, a
stale source appender is rejected under the identity lock, late webhooks remain
unlinked, and terminal/final states cannot regress or reintroduce snapshots.
After terminal evidence reaches its 90-day retention limit, the identity's
durable erasure markers still prevent a second cleanup lifecycle from starting.

## Consequences

Gate 1 is intentionally not wired to production writers, checkout,
notifications or account-erasure routes. Those integrations and any runtime
enablement require the next separately approved shadow/cutover gates.
