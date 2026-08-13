# Entitlement Authority — Gate 1 dormant foundation

Scope: local, dormant implementation on the deployed recovery-prerequisite base
`5d972ef9d5dd0031d47c185ea23188287dc854c6`, migration `0102 → 0103`.
All four new flags default to `false`; no production callsite is switched to
the strict gateway and no shadow/production deployment is part of this work.

## Implemented boundary

- Nine additive entitlement tables: User-owned identity, immutable sources,
  overlays, projection commands, observations/webhook dedupe, notification
  intents, restricted cleanup command and tombstone.
- Exact normalized snapshot/comparator, provenance/deny reducer and pure
  command state transitions.
- Test/fake-only strict Panel interface: one-shot mutation, DISABLED CREATE,
  durable UUID binding, fresh canonical GET and current-generation finalize.
- Deterministic Panel owner proof uses the valid unique username
  `te-<sha256(owner_key)[:32]>`; CREATE, recovery lookup and canonical reads
  require that exact value, and mismatch is quarantined without a sentinel.
- Unknown remote outcomes and lease takeovers are observation-only and remain
  quarantined because RemnaWave 2.8.1 has no CAS/ETag.
- Pure read-only shadow evaluator. It accepts no DB session or Panel client and
  exposes only hash prefixes and mismatch field names.
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
- `evidence/phase0_writer_inventory.json` — frozen Phase 0 baseline
- `evidence/writer_inventory.json` / `writer_closure.json` / `writer_union.json`
- `evidence/gate1_affected_test_manifest.txt`
- `evidence/runtime_writer_coverage_gate1.json`

The owner-only Phase 0 PII catalogue remains outside Git and was not read or
shared. The protected backup is outside Git and was restored only into a
Unix-socket-only local PostgreSQL instance. No row-level production data was
inspected.
