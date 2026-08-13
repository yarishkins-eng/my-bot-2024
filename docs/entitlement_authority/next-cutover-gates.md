# Next owner gate — do not execute from Gate 1

Gate 1 ends with an evidence-backed decision only about a **future separate
shadow deploy**. It does not authorize migration, deploy or any flag change.

## Exact next gate: owner-approved shadow release preparation

The owner has authorized preparation and review, not migration or deploy. After
the exact committed candidate SHA and both independent verdicts are reported,
the owner must separately approve all of the following:

1. a release card and fresh comparison against then-current `origin/main`;
2. protected migration workflow approval for additive `0103`, including a new
   backup/restore check if the SHA/schema/environment changed;
3. deployment with all checkout/projector/ready-notification switches still
   false;
4. only then, a separate explicit approval to enable the read-only shadow flag;
5. redacted metrics/retention/stop thresholds and an observation window that
   covers renewal, grace, daily jobs and reordered webhooks;
6. proof that shadow emitted zero POST/PATCH/DELETE, changed no checkout,
   finance, AP, legacy projection or notification behaviour, and leaked no PII.

Stop immediately on any shadow mutation, provenance last-write-wins, schema
ambiguity, PII/raw response, comparator instability or unexpected legacy
behaviour. Rollback is shadow-flag disable; schema stays additive. If the old
container itself must be restored, use only the captured protected migration
recovery record: the pinned old image starts with `SKIP_MIGRATION=true` on the
unchanged additive schema and must pass its exact health/startup/schema gates.
The protected workflow records `OLD_IMAGE_TARGET_SCHEMA_COMPATIBLE` and writes
the truthful v2 phase `recovered` only after exact state and keyed audit are
durable; ordinary deploy remains blocked on `prepared` or contradictory state.
Do not perform an ad-hoc production downgrade.

## Later gates, still forbidden

- Writer inventory revalidation and full legacy-writer quiescence/drain.
- One owner-approved fenced canary with real client VPN smoke.
- Production writer cutover and notification switch.
- Historical repair, refund/revoke, bulk cleanup or legacy teardown.

None of these actions was started by Gate 1.
