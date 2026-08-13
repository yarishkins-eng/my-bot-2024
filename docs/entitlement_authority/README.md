# Entitlement Authority — Gate 1 dormant foundation

Gate 1 is deployed at `80bf7e7262e174ef8fa6ada0dcce503571c6395e`
with schema `0103`. Gate 2 preparation adds a separately armed, read-only
shadow candidate. All four entitlement flags still default to `false`, its
independent kill switch defaults to `true`, and no production deployment or
flag change is part of candidate preparation.

## Implemented boundary

- Nine additive entitlement tables: User-owned identity, immutable sources,
  overlays, projection commands, observations/webhook dedupe, notification
  intents, restricted cleanup command and tombstone.
- Exact normalized snapshot/comparator, provenance/deny reducer and pure
  command state transitions.
- Test/fake-only strict Panel interface: one-shot mutation and DISABLED CREATE.
  A returned CREATE UUID is not trusted: a fresh canonical GET must prove the
  deterministic owner and exact DISABLED snapshot before durable UUID binding
  or any ACTIVE PATCH. Finalize also requires a fresh current-generation GET.
- Deterministic Panel owner proof uses the valid unique username
  `te-<sha256(owner_key)[:32]>`; CREATE, recovery lookup and canonical reads
  require that exact value, and mismatch is quarantined without a sentinel.
- Unknown remote outcomes and lease takeovers are observation-only and remain
  quarantined because RemnaWave 2.8.1 has no CAS/ETag.
- Read-only shadow evaluator/runtime with a forced PostgreSQL read-only
  transaction, a sequential rate-limited redacted Panel GET and aggregate-only
  metrics. It emits no ID, UUID, owner proof, raw snapshot/response or stable
  hash prefix and has no mutation interface.
- Two-stage erasure removes new authority links/snapshots immediately, keeps
  only encrypted restricted cleanup locators until verified terminal cleanup,
  raises a durable 30-day operator alert, and deletes terminal evidence after
  90 days. Evidence linking is serialized on the identity row, so an in-flight
  webhook or canonical observation cannot relink PII after erasure commits.
  If an unbound CREATE may have reached Panel, cleanup retains the encrypted
  deterministic username and atomically accepts a late exact UUID receipt
  without restoring the identity binding. An identity with no Panel binding
  and no unknown remote outcome is terminal immediately; unknown prior remote
  outcomes cannot become terminal automatically.

## Evidence index

- [ADR](adr-entitlement-authority.md)
- [Migration/restore proof](migration-design.md)
- [Writer closure](writer-matrix.md)
- [RemnaWave contract](remnawave-api-contract.md)
- [Test report](test-report.md)
- [Production/backup audit](production-audit.md)
- [Independent reviews](reviews.md)
- [Next owner gate](next-cutover-gates.md)
- [Gate 2 shadow release card](gate2-shadow-release-card.md)
- `evidence/phase0_writer_inventory.json` — frozen Phase 0 baseline
- `evidence/writer_inventory.json` / `writer_closure.json` / `writer_union.json`
- `evidence/gate1_affected_test_manifest.txt`
- `evidence/runtime_writer_coverage_gate1.json`

The owner-only Phase 0 PII catalogue remains outside Git and was not read or
shared. The protected backup is outside Git and was restored only into a
Unix-socket-only local PostgreSQL instance. No row-level production data was
inspected.
