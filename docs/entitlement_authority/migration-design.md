# Gate 1 additive migration and restore evidence

## Revision and contents

- Base: `0102`; candidate: `0103_entitlement_authority_dormant.py`.
- Creates nine tables and their indexes/constraints only.
- No seed/backfill/repair, no `users`/`subscriptions` mutation and no Panel
  network code. No unique constraint is added to historical subscription UUIDs.
- `entitlement_identities.panel_uuid` is unique inside the new authority only;
  it does not reinterpret or constrain historical `subscriptions` rows.
- Downgrade first checks every new table is empty and refuses otherwise.

## Protected restore route

- Fresh production backup artifact:
  `entitlement-gate1-release-20260813T055154Z.dump`
- Protected local copy outside Git:
  `/Users/stanis/Тепло ВПН/.private-backups/entitlement-gate1-release-20260813T055154Z.dump`
- Size: `928196` bytes; server/local SHA-256:
  `c6a0e82fcf5479426006b2a434001224e6304ade361e13d1305dae558ef4ad86`.
- Server/local modes were `0600`; no table rows or PII were inspected.
- Full custom-format restore succeeded into `gate1_v2_restore` on
  PostgreSQL 17.10, Unix socket only, `listen_addresses=''`.
- Restored revision was exactly `0102`; archive metadata had 1,447 entries.

## Executed migration matrix

- ✅ restore `0102 → 0103`.
- ✅ all Gate 1 tests on upgraded schema.
- ✅ empty `0103 → 0102`; all nine Gate 1 tables absent.
- ✅ deployed prerequisite (`5d972ef…`) has no application/runtime diff from
  the previously proved old base (`bcfe945…`); its entitlement-related baseline
  remains `42 passed`, plus synthetic ORM read/write on `0102`.
- ✅ repeat `0102 → 0103`; all nine tables present.
- ✅ old-image model read/write on `0103` with startup migration deliberately
  skipped in the isolated probe; the application ignores additive tables.
- ✅ downgrade guard rejects `0103 → 0102` as soon as any Gate 1 table has a
  row. It first takes one `ACCESS EXCLUSIVE` lock across all nine tables, so a
  concurrent insert cannot race the emptiness check/drop transaction. The
  guarded downgrade is an isolated compatibility proof, not the
  production recovery route. The protected `recover-after-migration.yml`
  instead pins the captured old image, sets `SKIP_MIGRATION=true`, verifies
  health/startup and permits it on the unchanged additive `0103` schema; this
  is the old-image compatibility scenario proved above. It writes exact
  recovery deploy-state and target-keyed audit before atomically marking the
  v2 journal `recovered`; partial `prepared` state is retried fail-closed.

The complete empty migration cycle was repeated on both the schema-only test
database and the protected production restore after the final migration
revision. Both ended at `0103` with the UUID uniqueness constraint present.
No external service was configured in the restore environment. The probe only
used a Unix-domain PostgreSQL socket and a synthetic `system_settings` key that
was deleted in the same isolated copy.

## Historical blank-chain defect

A brand-new PostgreSQL 17 database was run from revision zero. The unchanged
historical migration `0021_landing_localized_texts.py` failed exactly at
`WHERE title IS NOT NULL AND title != ''` with
`operator does not exist: json <> unknown`. Gate 1 does not modify applied
history; clean-bootstrap repair needs a separate owner-approved migration
review. Therefore Gate 1 compatibility proof is intentionally the protected
restore route from production head `0102`, not a blank-chain claim.
