# Access-point entitlements — local release card

Status: **local candidate only**. Nothing has been pushed or deployed. It
must not be enabled in production without a separate owner go/no-go and the
operational checks below.

## What is protected

- An `access_point_managed` tariff is driven by a validated public Host-title
  policy, never by a user-supplied squad UUID.
- Wallet and external checkout capture immutable quoted evidence before money;
  a late provider callback rechecks the locked evidence and enters operator
  review on drift.
- Every paid access-point period is an immutable term with a durable,
  claim-fenced boundary projection. Early renewal keeps current access until
  the exact captured start time.
- The projection sends the captured term's `ends_at` to the Panel, not the
  mutable subscription end date. Generic grace and raw Panel writes are
  fail-closed for access-point subscriptions.
- Legacy checkout, trial, promo, manual admin, auto-purchase, traffic/device
  add-ons, and raw country-selection paths reject access-point grants before
  money or a Panel write unless they use the term workflow.
- AP and legacy tariff cards expose only public access-point titles from
  validated policy or conversion-manifest evidence; absent evidence fails
  closed and does not fall back to legacy squad UUIDs.
- User APIs expose `access_points` (public point id + Host title) for an AP
  term and return no technical squad UUIDs in status/settings/list responses.
- Active-user changes use a durable, RBAC-protected preview/confirmation plan:
  it stores the complete server-side preimage, takes a PostgreSQL subscription
  writer fence through confirmation, and returns only business labels, counts
  and add/remove/preserved diff. Confirmation explicitly remains
  `confirmed_execution_disabled`; this candidate has no executor route, worker
  or Panel call.
- A legacy conversion can only be recorded through an injection-only service
  supplied by a separately protected, read-back-verified dedicated-equivalence
  operation. It atomically stores the opaque operation/read-back evidence and
  pre-fills the first future AP policy; old legacy snapshots remain legacy
  access until a later paid AP term.
- The tariff editor refuses the AP daily-tariff path before it can create a
  draft. The backend independently rejects an AP daily policy as well.

## Migration and rollback

- `0101_public_access_point_entitlements` is additive and creates no catalog
  row, policy, conversion, term, or Panel request.
- A local rehearsal from the deployed `0100` state completed upgrade to 0101
  and downgrade to 0100 on an empty disposable database. Fresh historical
  replay remains blocked by the pre-existing `0021` JSON operator failure,
  outside this change.
- Downgrade now refuses **before any destructive step** when an AP tariff,
  policy, or term exists. It is not a production rollback mechanism.
- Production rollback is forward-safe: stop new AP issuance, reconcile any
  issued AP tariff to a non-sellable legacy mode under an approved runbook,
  then `git revert <release-commit>`. Keep the additive audit tables; do not
  run an Alembic downgrade against production history.

## Required owner go/no-go checks

- Wire and independently verify a **GET-only**, source-verified RemnaWave
  inventory adapter. The code intentionally ships no guessed Host endpoint;
  discovery fails closed until this adapter is injected.
- Perform an owner-observed, no-write inventory dry run with redacted output,
  then a second matching read before any catalog/policy creation.
- Verify exact AP term-boundary behavior and recovery with a non-production
  Panel fixture. Do not use a live customer or modify production data for this
  check.
- The legacy conversion service is not wired into the application. Its
  verifier may be supplied only by the separately approved protected operation
  after dedicated equivalents have been created and GET/read-back verified.
- Review the final commit hash and use a `git revert` rollback command in the
  change record before any push to `main`.

## Local evidence

- Broader AP/API/grace/plan profile: `78 passed`.
- Final focused AP safety sweep: `23 passed`.
- Final backend suite: `2314 passed, 5 skipped`.
- Two independent final re-reads found no P0/P1. The PostgreSQL writer-fence
  unit asserts SQL emission; a live two-session blocking race remains a
  non-blocking coverage improvement.
- Cabinet worktree (unchanged by this final backend safety pass): type-check,
  unit tests, and production build passed.
- `ruff check` passes for the changed access-point files.
