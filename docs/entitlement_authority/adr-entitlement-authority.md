# ADR: dormant single entitlement authority

Status: **Gate 1 deployed; Gate 2 read-only shadow candidate preparation**.

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
has a durable deterministic intent and is always DISABLED. Its response UUID
is only a claim: a fresh canonical GET by that UUID must prove both the exact
deterministic owner username and the complete expected DISABLED snapshot
before the UUID may be bound and before ACTIVE PATCH. A malformed/stale claim,
read failure or mismatch leaves the identity unbound and remote-outcome-unknown.
Lost CREATE/PATCH never causes a blind retry. A takeover performs deterministic
lookup/canonical GET only; without remote CAS it cannot clear unknown outcome
automatically.

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
webhook evidence, and retains cleanup locators only AES-GCM encrypted plus
HMAC fingerprints. Webhook and canonical-observation writers first take the
same identity row lock, so either their evidence commits before erasure and is
deleted by it, or it observes terminal markers and remains unlinked/absent.
For an unbound CREATE with a possible remote outcome, erasure retains the
encrypted deterministic Panel username; a late successful POST receipt is
handed directly to the locked cleanup command as an encrypted UUID without
relinking the identity. If no Panel binding or possible CREATE exists and
there is no unknown remote outcome, the same transaction records
`cleanup_terminal` with no ciphertext.
A preceding unknown mutation quarantines cleanup and blocks terminal state.
Verified DELETE/canonical 404 clears ciphertext immediately; terminal evidence
expires after 90 days, unresolved evidence does not. Once erasure begins, a
stale source appender is rejected under the identity lock, late webhooks remain
unlinked, and terminal/final states cannot regress or reintroduce snapshots.
After terminal evidence reaches its 90-day retention limit, the identity's
durable erasure markers still prevent a second cleanup lifecycle from starting.

## Consequences

Gate 2 may wire only a double-interlocked read-only task: forced read-only SQL,
one redacted rate-limited canonical GET, in-memory comparison and anonymized
aggregate metrics. It cannot persist an observation or invoke the coordinator.
Checkout, projector and ready-notification integrations remain absent.

The exact JSON decoder is strict before shadow comparison. The low-level UUID
bind proof boundary and missing Panel CAS/ETag remain explicit writer-cutover
blockers; a GET-only shadow does not relax them. Any runtime enablement or
writer integration requires a later separate owner gate.
