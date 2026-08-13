# Production audit: initial read-only Phase 0 and closure addendum

## Gate 1 protected-backup audit (2026-08-12T21:26:12Z)

- Preflight re-confirmed deployed/source SHA
  `bcfe945863e64d5922c4998cd4b047e7ea7516d4`, clean server worktree,
  migration `0102`, and healthy bot/PostgreSQL/Redis containers.
- The only Gate 1 production side effect was the standard protected database
  backup at
  `/root/teplo-vpn-pre-release-backups/entitlement-gate1-20260812T212612Z.dump`.
- Server and protected local copy were `928055` bytes, mode `0600`, with exact
  SHA-256
  `c69f862c6810b5a6fdd5666fd5d259ee2e52ec5454b6d2db21a50086da6e3579`.
- Restore/upgrade/downgrade tests used only the local network-isolated copy.
  No backup row, raw dump content or PII was inspected or shared.
- Gate 1 made no production DB DDL/DML, Panel mutation, runtime/config/flag
  change, deploy, repair, commit or push.

## Initial Phase 0 scope and safety

- Audit windows: 2026-08-12 14:58:57Z–15:15:50Z and strengthened
  provenance rescans at 15:43:14Z–15:43:15Z and
  16:08:08Z–16:08:09Z.
- Deployed SHA: `bcfe945863e64d5922c4998cd4b047e7ea7516d4`;
  working tree clean.
- Only SSH status reads, SQL `SET TRANSACTION READ ONLY` + SELECT, Panel GET,
  and aggregate log counts were used.
- Panel scan envelope: GET only, cursor pages of 100, concurrency 1, 200 ms
  delay, maximum 20 pages/2,000 identities, abort on first error or page over
  10 seconds. It completed in 2 pages; latency 35/12 ms.
- Before/after fingerprints combine row count with every relevant row's
  surrogate ID and PostgreSQL `xmin`. They were equal for users,
  subscriptions, checkout/attempt/outbox/notification/provider-event,
  erasure, term and traffic-purchase tables. This is a strong concurrent-write
  detector for the short scan, not a distributed atomic snapshot or proof
  that an external Panel write did not occur independently.
- One payment-attempt row fingerprint changed between the two later scan
  windows, consistent with the live recovery worker. It was stable inside each
  individual before/after window; no cross-window state is treated as one
  snapshot.

All claims above are limited to the original Phase 0 audit windows. They do
not claim that production remained mutation-free after Phase 0.

## Later owner-approved production mutation

- Mutation window: 2026-08-12 18:21:14Z–18:21:16Z (21:21:14–21:21:16 MSK).
- Basis: the owner identified the sole quarantined candidate as their own test
  account and test purchase, requested that the account be reset as if unused
  while the confirmed payment remained at Platega, and then explicitly
  authorized execution.
- Before mutation, exact local financial-graph guards and a canonical Platega
  GET proved the expected test checkout/payment, confirmed amount and currency,
  retained ledgers, and absence of refund, reversal or unresolved credit.
- The sanctioned application workflow was
  `UserService.delete_user_account(..., force_panel_delete=True)` followed by
  `resolve_financial_account_erasure(...,
  resolution_code="balance_writeoff_approved")`; no raw SQL repair was used.
- Production writes anonymized the bot profile, released its Telegram binding,
  completed the erasure record, made the subscription terminal, cleared
  subscription credentials/RemnaWave identifiers/squads and pending delivery
  jobs, and removed the RemnaWave identity.
- There was no Platega refund, cancellation or other provider mutation. The
  confirmed payment and required financial checkout/ledger/provider-event
  evidence remain retained.
- Independent read-only verification at 18:25:31Z passed every closure guard,
  including profile anonymization, released identity, terminal subscription,
  retained payment/ledgers, no refund/reversal, and absent RemnaWave identity.
- Owner-only evidence is outside Git in `closure-result.json` and
  `closure-verification.json` under
  `/private/tmp/teplo-entitlement-owner-report-20260812/` (`0700/0600`). It
  contains no data that should be copied into this sanitized pack.

This action was a later, separately authorized production exception. It was
not part of the original read-only Phase 0 audit and does not change the
foundation/cutover NO-GO.

## Fresh post-closure read-only rescan

- Scan windows: 2026-08-12 20:27:51Z–20:27:52Z for the bounded full Panel scan
  and 20:29:54Z–20:29:55Z for the terminal-erasure classification query.
- The full scan reused the documented envelope: SQL
  `SET TRANSACTION READ ONLY` + SELECT, RemnaWave GET only, one request at a
  time, page size 100 and abort on first error. It completed without error in
  one page against RemnaWave 2.8.1.
- Before/after fingerprints were identical for all ten checked database
  groups. The second query also used `SET TRANSACTION READ ONLY` and SELECT
  only. Therefore this rescan itself performed no production write.
- Current reconciliation: 102 DB identity UUIDs, 100 Panel identities, 100
  found, 2 missing only for expired subscriptions, 0 Panel orphans, and 0
  cross-owner identities.
- All 100 found identities exactly match status, traffic, device limit,
  internal squads and external squad. There are 34 expiry-only differences on
  already EXPIRED legacy identities and 2 missing Panel identities belonging
  only to expired subscriptions: 36 raw, non-actionable legacy observations in
  total. There is no active-access drift cohort.
- Five historical Direct v2 checkouts remain `ready`: 4 retain a current Panel
  binding and 1 is the completed terminal erasure above. There are 0
  unexplained missing Direct identities, **0 current Direct-ready access
  mismatches, and 0 current quarantine cases**.
- The five historical rows still do not satisfy today's full immutable-current
  provenance validator. Those evidence gaps remain a migration limitation,
  but without a current contradiction they do not constitute five quarantine
  cases and do not authorize re-projection.
- Raw aggregate outputs remain owner-only outside Git as
  `post-closure-panel-rescan.json` and
  `post-closure-quarantine-rescan.json` in the directory above.
- The pre-closure `production_owner_contact_report.py` does not model a
  completed terminal erasure. Its existing output is preserved as historical
  evidence and must not be rerun or quoted alone as the current quarantine
  count; the current count comes from the full scan plus the explicit
  terminal-erasure classification query.

## Initial Phase 0 runtime baseline

| Item | Observed |
|---|---|
| Bot migration | `0102` |
| Bot database | PostgreSQL 15.18 |
| RemnaWave | 2.8.1 |
| Bot workers | one `python main.py` process |
| Multi tariff | false |
| Device-First new/public | false / false |
| RemnaWave auto-sync/webhook | true / true |
| Channel requirement | false |
| Traffic monitor/fast | false / false |
| Grace | true |
| Sales mode | tariffs |
| In-memory RemnaWave retry events, last 4h | 0 enqueue / 0 requeue / 0 success / 0 exhausted |

The in-memory queue count itself is not externally observable. A process
restart also destroys it; the log counts are therefore a limitation, not
durable backlog proof.

## Initial Phase 0 database state

- 157 users, 105 subscriptions, 23 checkouts, 19 payment attempts.
- Direct v2: 5 ready, 4 checkout operator-review, 5 payment-attempt
  operator-review, 9 failed attempts, 9 terminal cancellations.
- Provisioning outbox: 5 Direct v2 rows, all `done`; ready notifications: 5,
  all `sent`.
- Three account erasures are `completed/deactivated`.
- No AP terms/projection backlog and no traffic purchases exist.
- All five ready rows have a bound subscription, effective UUID+URL, done
  outbox, paid-processing attempt, and sent notification.

## Initial Phase 0 identity and Panel reconciliation

- 103 unique DB identity UUIDs; 101 Panel identities; 101 found; 2 missing.
- Both DB→Panel missing identities belong to expired subscriptions.
- Zero Panel→DB orphans, including zero ACTIVE orphans.
- Zero cross-owner UUID and zero duplicate subscription UUID.
- The 102 repeated DB rows are the expected single-mode User+Subscription
  mirror, not cross-owner duplicates. This strongly supports a User-owned
  canonical identity for the present topology.
- Owner match: 97 exact; 4 unknown because one side lacks Telegram identity;
  zero mismatch.
- Access comparison among the 101 found identities: status 100 exact/1 drift;
  traffic 100/1; device limit 100/1; squads 101/0; external squad 101/0.
- Expiry 66 exact/35 drift; all 35 Panel EXPIRED identities are counted as
  legacy-history drift by the exact timestamp comparator. Only current active
  entitlement drift is actionable here.
- One `active/nontrial` identity has observable current access drift, and it
  belongs to one of the five Direct v2 ready checkouts.

## Initial Phase 0 potentially impacted-user scope

- Observed candidate count: **1 of 5** Direct v2 ready checkouts.
- Proven from immutable commercial evidence: **0**; quarantine: **1**.
- The strengthened validator recomputed each Direct sale entitlement hash and
  required exact sale amount, provider identity/amount/currency, paid payment
  with ledger, fulfilled expiry, current subscription projection, done outbox
  and sent notification. None of the five historical ready rows passes the
  full current-provenance predicate: 4 have pre-current-format entitlement
  snapshots, all 5 lack the full provider proof expected by today's model, 2
  have later expiry state and 4 have later squad state. These are evidence
  gaps, not proof that payment failed.
- The owner-only file is outside Git at
  `/private/tmp/teplo-entitlement-owner-report-20260812/impacted-users.json`.
- Directory/file modes: `0700/0600`; it was not supplied to reviewer agents.
- It contains only internal IDs, Telegram contact/name, period/amount,
  `quarantine` classification, evidence gaps, mismatch categories, and a
  recommended manual investigation. It excludes URLs, email, credentials,
  provider secret/ID, and callback payload.

This is a narrow **suspected** incident, not proof of continuing mass loss.
The earlier mutable Subscription/Tariff comparison was downgraded after
reviewer challenge; it must not authorize re-projection by itself. No
production flag was changed during the initial Phase 0 audit. The later
owner-approved test-account erasure is documented separately above. Its fresh
post-closure result is 0 current mismatch / 0 current quarantine.

## Provenance matrix

| Desired field | Authorized evidence | Legacy mutable source | Panel observation | Conflict rule |
|---|---|---|---|---|
| owner/identity | account + chosen User scope | User/subscription UUID mirrors | exact response UUID + Telegram owner | mismatch/duplicate → quarantine |
| status | business command + deny overlays | subscription status | Panel status/webhook | Panel cannot grant authority; deny wins |
| expiry | paid term/grant or trial evidence | subscription end/grace | Panel expireAt | immutable grant wins; ambiguity → quarantine |
| traffic limit | tariff/sale/add-on/reset evidence | subscription traffic fields | Panel traffic bytes/webhook | exact empty/zero semantics; no last-write-wins |
| device limit | sale/admin/add-on evidence | subscription device limit | Panel HWID limit | exact null/value; ambiguity → quarantine |
| squads | immutable entitlement snapshot/term | connected_squads | Panel internal squads | exact set including empty |
| external squad | authorized tariff snapshot | current tariff relation | Panel external UUID | exact nullable comparison |
| URL/credentials | verified canonical Panel identity | subscription URL fields | GET response | observation only; never proves entitlement |
| LIMITED/channel/admin/erasure | durable deny command/overlay | mutable status/flags | status/webhook | deny precedence; webhook cannot resurrect |
