# Gate 2.1 `expire_at` precision release card

Status: candidate preparation only. This card does not authorize merge, deploy,
another production one-shot, or `ENABLE_SHADOW`.

## Scope and base

- Exact release base: `ab5825959363a7477cbcaf2d040c0bd6bb99076b`.
- The only application runtime change is
  `app/services/entitlement_authority/shadow.py`.
- The read-only shadow evaluator ignores only a sub-millisecond remainder when
  both `expire_at` values fall in the same UTC millisecond bucket.
- Adjacent UTC millisecond buckets, every other entitlement field, the global
  comparator, snapshot hashes, receipts, writer and coordinator remain strict.
- There is no migration, `app/config.py`, `main.py`, Dockerfile, Compose,
  dependency, `.env`, Panel mutation, payment, subscription, or user-access
  change. Production schema remains `0103` and all nine authority tables must
  remain empty.

The one previously `authority_unproven` sampled case is separately resolved by
a new owner-confirmed compensation decision. Exact identity and expiry remain
owner-only. This release neither reconstructs a historical payment/admin audit
nor changes that user's current access.

## Candidate-runtime compatibility proof

Before merge, a manual private candidate-runtime E2E must pass without skip.
It starts from the verified owner-only production OCI archive and its exact
linux/amd64 content chain, then creates an offline derived test image by
overlaying only the exact candidate `shadow.py`. The derived image is labelled
with the exact candidate SHA and base OCI index; CI verifies the in-image file
hash equals the candidate file and refuses any other `app/**` runtime diff.

This compatibility image is test evidence, not the future production image and
not a public artifact. The E2E must prove:

- same UTC millisecond bucket with a non-zero microsecond remainder is exact;
- the adjacent millisecond bucket is `expire_at`-only drift;
- the packaged one-shot path samples the synthetic identity and removes its
  sidecar;
- database fingerprint is unchanged, injected DML is rejected, and the fake
  Panel records one GET with zero POST/PATCH/PUT/DELETE per comparator case;
- timeout, controller SIGKILL, hard deadline, Docker query failure, Disable
  states, container restrictions, two isolated networks and aggregate-only
  evidence retain their previously reviewed behavior.

The owner-only base archive remains private. Public CI receives no production
secret, host access, private repository credential, or image archive.

## Release route

This is an ordinary non-migration application deploy. It is deliberately not
the historical Gate 2 control-only route. Merging to `main` runs `deploy.yml`,
which builds the exact merge SHA from a clean Git archive, replaces the bot
container and therefore rebuilds and restarts the production bot.

Staging cannot exercise this dormant comparator against the real Panel and is
not accepted as the compatibility proof. The isolated private E2E covers the
changed comparator; the normal deploy health/log/API/user smoke covers the bot
restart.

The existing production one-shot compatibility boundary remains pinned to
source `103094b96f96a412463753e56e3d996311b182ec` and image
`sha256:52df4d9531f5bb5084af19752cdcf593609687a35da2a0fa26c2995aac2d8b1e`.
After this runtime release, that old `ENABLE_SHADOW` path must fail closed on
the changed production source/image. Updating it requires a different future
candidate and owner decision. This card does not authorize another one-shot.

## Required gates before merge

1. Exact candidate diff matches the reviewed manifest and contains only the
   single `app/**` runtime file plus its tests, generated writer evidence,
   bounded E2E/CI changes and documentation.
2. Local targeted and full tests, Ruff, format, Bash syntax, YAML parse and
   `git diff --check` pass.
3. Public exact-candidate-SHA CI passes both `verify` and the candidate-boundary
   job without production credentials.
4. The private candidate-runtime E2E passes without skip on the same exact
   candidate SHA and records its exact private harness SHA and derived image
   identity.
5. Independent reviewer and skeptic report no P0/P1 or unresolved verification.
6. The owner gives an explicit GO acknowledging that merge triggers a real
   production bot rebuild and restart.

Any mismatch is STOP. A green candidate is not merge authorization.

## Merge and post-deploy evidence

After the separate owner GO:

1. Use a protected PR and prove the merge tree is byte-identical to the reviewed
   candidate tree.
2. Require exact merge-SHA CI and the ordinary deploy to finish successfully.
3. Verify production source/deploy-state equals the merge SHA and record the new
   immutable image ID. A new bot container ID/start time is expected; restart
   count must be zero and health must be `healthy`.
4. Check fresh startup logs, `/health/unified`, the applicable bot API and one
   bounded owner/test-account smoke. Do not perform a payment or VPN mutation.
5. Prove `.env` hash and effective flags are unchanged: checkout/projector/
   ready/shadow `false`, kill switch `true`, multi-tariff `false`.
6. Prove schema `0103`, authority tables `9×0`, sidecar absent, no shadow worker,
   Panel mutation markers unchanged and semantic business fingerprints
   unchanged. Track known legacy lease metadata separately rather than using it
   as a strict business fingerprint.

Then stop. `ENABLE_SHADOW` remains forbidden, and scheduler, projector,
notifications, writer cutover, repair and another one-shot remain outside this
release.

## Rollback

There is no schema or data rollback. A failed deployment must use the existing
workflow's preserved previous-image rollback. If the new healthy deployment
later shows a regression, create an exact `git revert` of the Gate 2.1 merge and
let the ordinary workflow deploy that revert; do not reset history, edit the
server source, or run Compose manually. Re-verify health, logs, source/image,
flags, schema, sidecar absence and the same bounded smoke after rollback.
