# Migration recovery prerequisite release card

Status: prerequisite only. This release must contain no Alembic revision and
no Gate 1 application code. It does not authorize a Gate 1 migration or
production deployment.

## Scope and invariants

- The migration workflow records a version 2 recovery journal before the
  service switch. The journal binds the exact target/rollback SHAs, old/new
  image IDs, previous/target schema revisions, and the reviewed decision about
  whether the captured old image can run on the target schema.
- The protected recovery workflow reads the live Alembic revision, source SHA,
  container image, deploy state, and journal before any image retag or service
  mutation.
- The old image may run on a distinct previous schema. It may run on the target
  schema, including a database-risk release whose Alembic head is unchanged,
  only when the migration dispatch recorded
  `OLD_IMAGE_TARGET_SCHEMA_COMPATIBLE`. An explicit incompatible decision is
  never inferred away from revision equality.
- An unknown schema, image, source, journal key/version, or contradictory
  durable/live state exits fail-closed without starting an image.
- A retry after any partial recovery step reclassifies the live state. Once the
  old image is healthy with `SKIP_MIGRATION=true`, later bookkeeping failure
  leaves that safe state in place for another retry.
- Recovery audit files are keyed by migration target SHA, so a completed prior
  recovery cannot be mistaken for a later migration.
- A later protected migration retry first verifies and atomically normalizes a
  prior `mode=recovery` deploy marker to the live rollback source/image before
  writing the new journal. Every recovery key is validated, its schema must
  equal the live revision, and its prior target must lie on the exact source
  ancestry to the new target. Unknown, duplicate, or contradictory metadata is
  never overwritten. Same-schema database-risk releases are classified
  explicitly instead of being confused with an unapplied migration.
- Candidate build and Alembic-head inspection use the clean target archive
  while server source remains on the previous SHA. The target source checkout
  occurs only after the complete `prepared` journal is atomically visible, so
  a hard kill always leaves either the intact prior baseline or a recoverable
  journal-bound state.

## Required test evidence

The real read-only classifier is executed as a subprocess against durable
state files for these crash windows:

- prepared journal before the migration;
- target revision committed before `phase=completed`;
- target deploy-state written while the journal is still `prepared`;
- interruption before or during the old-image Compose switch;
- interruption after rollback source checkout;
- interruption after atomic recovery-state write and before/after audit;
- repeated execution of every checkpoint;
- incompatible target schema, legacy journal, unknown revision, and
  contradictory source/image/state combinations.

The repository's full lint/test workflow and two independent exact-SHA reviews
must be green before release.

Local pre-commit evidence: recovery/classifier/deploy-state matrix `38 passed`,
including execution of the extracted production recovery and migration shells
through injected crash points, explicit incompatible same-schema state, and
negative recovery-ancestry guards, and actual `SIGKILL` boundaries on both
sides of the prepared journal; full repository suite
`2414 passed, 5 skipped`;
Ruff format and lint checks passed. Exact-SHA CI and reviews remain mandatory.

## Prerequisite rollout

- Confirm the exact diff contains only protected workflows, the recovery
  classifier, its tests, and this card. It must not contain `migrations/`,
  `app/`, shadow enablement, RemnaWave operations, or user-data operations.
- Release the reviewed exact SHA through the normal non-migration deployment.
  The application image is rebuilt from unchanged application code; Alembic
  must remain at the pre-release revision.
- Verify exact production source/deploy state, one healthy bot container,
  polling startup, stable logs, unchanged schema, and the workflow files on
  `origin/main`.
- Do not exercise the protected recovery operation in production without a
  real migration incident and its separate owner approval.

## Rollback and stop conditions

- Before any migration has created a version 2 journal, rollback is a normal
  `git revert` of the exact prerequisite commit followed by the normal health
  gates.
- After a version 2 migration journal exists, do not remove this prerequisite;
  preserve the workflow required to interpret that durable state.
- Stop on any main/SHA mismatch, schema change, health or polling failure,
  unexpected process, test/review NO-GO, or change outside the stated scope.
- No step in this release may enable shadow, mutate RemnaWave, or change user
  data.
