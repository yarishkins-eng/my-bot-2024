# Gate 1 test and fault-injection report

Environment: Python 3.13.14, PostgreSQL 17.10, Unix socket only on port 55449,
full protected-restore DB at production revision `0102`. Panel is an in-process
controllable fake; no production adapter exists in Gate 1.

## Results

- Phase 0 defect evidence: `12 passed` (the unsafe legacy behaviour remains
  reproducible and was not weakened).
- Gate 1 entitlement suite: `110 passed`, no skipped.
- APP-URL-only real-PostgreSQL harness: `79 passed`, proving that the test
  bootstrap does not replace `asyncpg` when only the application database URL
  is configured.
- Exact 37-file affected manifest: `665 passed`, no skipped.
- Real-PostgreSQL concurrency/fault subset: five consecutive runs of
  `73 passed`, no skipped.
- Full repository suite: `2541 passed, 5 skipped`; all five skips are existing
  `tests/integration/test_device_first_postgres_constraints.py` cases skipped
  because their separate optional `DEVICE_FIRST_TEST_DATABASE_URL` was not
  configured. They are outside `tests/entitlement_authority` and outside every
  mandatory Gate 1 scenario.
- Standard no-database GitHub lint path: `2450 passed, 9 skipped`. Four
  entitlement PostgreSQL modules now explicitly skip at module collection when
  their isolated database URLs are absent; with the protected test URLs set,
  the same modules execute the mandatory Gate 1 matrix above with no skips.
- Ruff/formatter, `git diff --check`, compileall: pass.
- Mypy 1.18.2 on 13 changed foundation/security/evidence modules: pass.
- Old-image entitlement-related baseline: `42 passed` plus isolated ORM
  read/write probe against upgraded `0103` with startup migration disabled.
  The deployed prerequisite `5d972ef9…` changes no application/runtime file
  relative to the previously proved old base, so the same image compatibility
  boundary applies.

## Mandatory scenarios

- ✅ stale existing UUID/URL cannot be READY before exact equality.
- ✅ GET timeout produces no READY and no CREATE.
- ✅ PATCH applied/lost response: one PATCH, observation-only takeover, no CREATE.
- ✅ CREATE applied/lost/no-apply/kill: at most one DISABLED candidate and one POST.
- ✅ kill after intent/send fence/POST/UUID bind/ACTIVE PATCH/canonical GET/final commit.
- ✅ stale generation cannot finalize; generation is rechecked at send-fence.
- ✅ caller-supplied bound/unbound snapshots cannot override the immutable
  source or cause PATCH/CREATE; corrupt source/hash/binding stops before HTTP,
  and a lost finalize fence can never write READY.
- ✅ delayed server-side N after N+1: no N+1 mutation, late N never READY, quarantine persists.
- ✅ a reclaimed stale command already marked sent remains a permanent
  mutation-possible fence; generation N+1 cannot mutate behind it.
- ✅ expired lease while old worker lives: takeover observes only.
- ✅ reset/delete/channel/LIMITED across every meaningful remote barrier.
- ✅ real erasure across every meaningful remote barrier clears new PII links,
  blocks stale READY and retains unknown cleanup safely.
- ✅ pre-VERIFIED reversal blocks; post-VERIFIED hold is non-access-deny.
- ✅ AP/Direct canonical lock order has no deadlock and AP term is unchanged.
- ✅ traffic/device add-on, expiry and reset cancel stale generation.
- ✅ zero/multiple/foreign owner lookup quarantines; no arbitrary candidate.
- ✅ owner proof is the exact Panel username hash; a former sentinel-collision
  shape cannot compare equal or become READY.
- ✅ concurrent identities cannot bind the same Panel UUID; the UUID advisory
  lock and authority uniqueness quarantine the conflict atomically.
- ✅ expired LIMITED remains effective until explicit proven clear evidence.
- ✅ duplicate/reordered webhooks cannot change commercial desired state.
- ✅ stale notification cancellation and provider callback idempotency.
- ✅ empty internal squads, nullable external/HWID and zero traffic are exact.
- ✅ financial rollback leaves zero source/command; changed duplicate source
  evidence fails closed.
- ✅ erasure ciphertext is not plaintext, terminal clears it immediately,
  30-day alert is single/durable, unresolved cleanup is retained, terminal
  evidence expires only after 90 days.
- ✅ erasure with no Panel binding becomes terminal without a remote cleanup
  command or encrypted target, unless a prior remote outcome is unknown.
- ✅ stale appenders waiting on the identity lock stop after erasure commit;
  no owner/Panel PII or pending command can revive. Late webhooks stay unlinked
  and repeated terminal marking cannot regress `final_erasure`.
- ✅ schema JSON constraints reject extra PII keys and shadow has no DB/remote
  mutation boundary.
- ✅ every startup writer registration is explicitly classified; unknown
  inventory items fail closed and no blanket metadata fallback is accepted.

Warnings are pre-existing SQLAlchemy/Pydantic deprecations and known mock
resource warnings; no Gate 1 test warning indicates a failed oracle.
