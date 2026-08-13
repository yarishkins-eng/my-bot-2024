# Gate 2 read-only shadow release card

Production base: `80bf7e7262e174ef8fa6ada0dcce503571c6395e` at schema
revision `0103`. Current authority is candidate preparation, test, push of a
reviewable branch and exact-SHA review only. PR merge, production deploy and
shadow enablement are forbidden until a new explicit owner decision.

## Deploy shape for the next owner decision

If separately approved, the first Gate 2 deploy is code-only but must use the
protected `deploy-migration.yml` path because the repository's migration-risk
guard covers `main.py` and `app/config.py`. Both previous and target schema are
`0103`, so Alembic performs no DDL/DML. The deploy must keep this exact
environment matrix:

| Setting | Required value |
|---|---:|
| `ENTITLEMENT_AUTHORITY_CHECKOUT_ADMISSION_ENABLED` | `false` |
| `ENTITLEMENT_AUTHORITY_PROJECTOR_ENABLED` | `false` |
| `ENTITLEMENT_AUTHORITY_READY_NOTIFICATIONS_ENABLED` | `false` |
| `ENTITLEMENT_AUTHORITY_SHADOW_ENABLED` | `false` |
| `ENTITLEMENT_AUTHORITY_SHADOW_KILL_SWITCH` | `true` |

There is no Gate 2 migration. The candidate must not change Alembic files or
run production DDL/DML. The ordinary application continues at `0103`; all nine
dormant authority tables remain untouched and empty.

Shadow observation itself is another owner gate after a healthy dormant code
deploy. Enabling it requires both `SHADOW=true` and `KILL_SWITCH=false` while
the three writer flags stay false. It starts one in-process task inside the
existing bot container, never a second bot process or a projector worker.

## Read-only boundary

The shadow has exactly two inputs:

1. one PostgreSQL transaction declared `READ ONLY` before any query, with a
   local 5-second statement timeout; and
2. one redacted `GET /api/users/{uuid}` per sampled identity, with no retry,
   no subscription-link enrichment and a 4-second request timeout.

It imports no checkout, payment, AP writer, webhook, notification,
entitlement persistence/coordinator or strict mutation gateway. It has no
authority-table writer. `POST`, `PATCH`, `PUT`, `DELETE` and Panel actions are
outside its interface. A fresh DTO is compared in memory and discarded.

The source preflight stops the cycle before Panel reads on multiple current
subscriptions, user/subscription UUID disagreement, a UUID referenced by
multiple owners, erased identities or multi-tariff mode. No value from Panel
can become desired state or feed a legacy writer.

## Cohort, schedule and budgets

- Priority safety cohort: current LIMITED, grace, access-point and
  `direct_purchase_v2` subscriptions.
- Baseline cohort: deterministic 10% (`1000` basis points) of other current
  paid/trial identities, using a fixed PostgreSQL hash seed.
- Priority rows are selected first; the combined hard cap is 18 identities per
  cycle. IDs and UUIDs exist only in process memory for the duration of a
  cycle.
- One cycle every 900 seconds; sequential Panel reads only; at most 12 GETs per
  minute; no concurrent Panel request.
- Per-GET timeout 4 seconds, database statement timeout 5 seconds, hard
  whole-cycle deadline 180 seconds. The deadline wraps database/pool
  acquisition, source reads, Panel-client open, every rate-limit wait and
  every GET. At the hard cap, the database timeout plus scheduled waits and
  all request timeouts fit within 162 seconds, leaving 18 seconds of circuit
  margin.

The initial observation window, if later authorized, is at least seven full
days and must cover renewal, expiry, grace entry/exit, daily processing,
access-point term boundaries, direct-payment recovery and reordered webhooks.
This card does not authorize that window.

## PII and metrics contract

Only fixed aggregate counters leave the process: sample/exact/drift/missing,
read/contract/owner/rate/comparator errors, critical-drift identity count,
fixed mismatch field names, fixed cohort names, elapsed milliseconds and a
fixed stop code. Logs use schema `entitlement_shadow_metrics_v1` and the
existing 30-day log retention.

Shadow metrics use INFO/WARNING only. They never use ERROR/CRITICAL, so the
existing admin error-forwarder cannot turn a circuit event into a Telegram
notification side effect.

Forbidden in metrics, errors and object representations: database/user/
subscription IDs, Telegram ID, Panel UUID, owner proof, username, email,
subscription URL, squad UUID, raw response, exception text, desired/observed
snapshots and stable snapshot hash or hash prefix. The read-only source never
selects names, usernames, emails, payment fields or subscription URLs.

## Numerical automatic STOP thresholds

The circuit opens and the task exits without automatic restart on:

- one owner/Panel binding mismatch, contract-decoder failure, comparator
  instability or local rate-limit violation;
- two Panel read errors, or at least 10% once 10 identities were sampled;
- two missing Panel users, or at least 10% once 10 were sampled;
- two identities with critical access drift, or at least 10% once 10 were
  sampled;
- four identities with any drift, or at least 20% once 10 were sampled;
- source invariant failure, Panel-cycle open failure or cycle time over 180
  seconds.

Critical access fields are status, expiry, traffic bytes/strategy, HWID limit,
internal squads and external squad. Count thresholds are independent of the
minimum ratio sample.

## Kill switch and recovery

- Automatic: any threshold above ends the isolated sidecar. It has Docker
  restart policy `no`, so circuit STOP is durable across process, Docker and
  host restarts. A seven-day host-owned lease is an additional hard limit.
- Operator: `DISABLE_SHADOW` in the reviewed allowlisted
  `control-entitlement-shadow.yml` workflow deletes only the lease and isolated
  sidecar. It never restarts the production bot and has no dependency on bot
  health, PostgreSQL, RemnaWave or `.env`. The production `.env` remains the
  permanent `SHADOW=false`/`KILL_SWITCH=true` baseline. See
  `gate2-shadow-control-prerequisite-release-card.md`.
- Code rollback, if required, is a protected revert commit followed by
  `deploy-migration.yml`, because the revert still changes `main.py` and
  `app/config.py` and the ordinary deploy guard must stop it. Previous and
  target schema stay `0103`, so this recovery runs no Alembic DDL/DML; no
  downgrade, data repair or Panel action is part of shadow recovery.

## Required evidence before any deploy

1. `origin/main` still equals production base or the candidate is rebuilt and
   re-reviewed from the new exact base.
2. Exact-SHA CI and independent reviewer/skeptic GO with no P0/P1.
3. Diff contains no migration, workflow weakening, Panel mutation method,
   entitlement DML or changes to checkout, AP, payment, webhook, notification
   or legacy worker modules.
4. Real-PostgreSQL source test executes on an isolated `0103` restore, leaves
   relevant row fingerprints unchanged and proves injected DML is rejected by
   PostgreSQL's read-only transaction.
5. Migration/compatibility, Gate 1, affected and full suites are green.
6. A later dormant deploy is accepted only with all four entitlement flags
   false and kill switch true. Health, logs, worker count, empty authority
   tables, unchanged Panel mutation evidence and old flows must be checked.

STOP on any SHA/base/schema/flag mismatch, protected-flow bypass, PII emission,
new worker, DB/Panel mutation, non-empty authority table, degraded legacy flow
or numerical circuit condition. This card grants no PR merge, deploy, shadow
enablement, canary, projector, writer cutover or RemnaWave/user-data mutation.

## P2 decisions

1. **Gate 2 fixed:** the exact snapshot JSON decoder no longer coerces bool to
   int, string to array or lower-case enums and rejects unknown keys. Shadow
   cannot turn a malformed Panel DTO into an apparently exact comparison.
2. **Writer-cutover blocker retained:** low-level `bind_uuid()` has no durable
   keyed receipt-proof token. The sole coordinator caller verifies first, but
   the storage contract must become self-contained before any writer can run.
3. **Writer-cutover blocker retained:** RemnaWave 2.8.1 offers no CAS/ETag
   between canonical GET and UUID bind/PATCH. Gate 2 performs GET only, so the
   TOCTOU cannot cause a shadow mutation; writer enablement remains forbidden
   until a separately approved fence is proved.
