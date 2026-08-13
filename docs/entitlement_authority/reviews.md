# Gate 1 independent reviewer and skeptic record

Two independent agents reviewed only the sanitized prompt, detached worktree,
diff, tests and evidence in this directory. They did not receive owner-only
Phase 0 data, the protected backup, production secrets, raw responses or WIP
refs. Reviewer: `gate1_reviewer` (Nietzsche); skeptic: `gate1_skeptic`
(Copernicus).

## First adversarial pass

Both agents independently kept the same six findings; none was dropped:

1. **P0/P1 — stale sent fence:** a reclaimed `sent/mutation_possible` command
   could be cancelled and allow generation N+1 to mutate.
2. **P1 — duplicate Panel UUID:** two identities could race the same remote
   UUID without a UUID-scoped lock/authority uniqueness guarantee.
3. **P1 — owner proof:** fake transport compared an invented owner field rather
   than the real RemnaWave username contract.
4. **P1 — LIMITED expiry:** an expired LIMITED overlay could silently release
   access without explicit clear evidence.
5. **P1 — no-binding erasure:** erasure without a Panel UUID could remain
   pending forever despite requiring no remote cleanup.
6. **P1 — writer classifier:** a broad metadata classification could hide
   startup writers instead of failing closed.

## Fixes and regression proof

- Claim logic now checks `mutation_possible` before stale cancellation; a sent
  command stays quarantined and fences later generations.
- Panel UUID binding uses a UUID-scoped PostgreSQL advisory transaction lock,
  deterministic identity lock order and a new-table unique constraint; a
  conflict quarantines both identities/commands atomically.
- CREATE, recovery lookup and canonical comparison use the exact unique
  `username=te-<sha256(owner_key)[:32]>` contract.
- LIMITED ignores ordinary TTL expiry and releases only for
  `traffic_increase`, `traffic_reset` or `admin_clear` evidence.
- No-binding/no-unknown erasure is terminal immediately with no ciphertext;
  unknown prior remote outcome still quarantines.
- All 61 startup registrations and every other inventory item now require an
  explicit semantic rule; unknown items raise an error. The architecture test
  enforces this closure.

Dedicated regression tests cover every item above. Reviewer and skeptic then
re-ran their gates and independently reported all six findings closed with
zero open P0/P1.

## Final narrow re-review

A later hardening changed owner proof from a synthetic field to the real Panel
username. The reviewer found one additional P1: an in-band sentinel used for
canonical mismatch could theoretically collide with a real owner-key shape.
The implementation now raises immediately on mismatch and the coordinator
quarantines `canonical_contract_invalid`; a regression proves the former
collision cannot become READY. Final reviewer verdict: **PASS, 0 P0/P1**.
Final skeptic KEEP/DROP audit: **KEEP CLOSED, 0 P0/P1, PASS**.

## Review conclusion

Independent double review is closed for the dormant Gate 1 boundary. It does
not approve a production adapter, shadow deployment, flag change or cutover;
those remain separate owner gates.
