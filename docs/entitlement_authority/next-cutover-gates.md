# Next owner gate — do not execute from Gate 1

Gate 1 ends with an evidence-backed decision only about a **future separate
shadow deploy**. It does not authorize migration, deploy or any flag change.

## Exact next gate: owner-approved shadow release preparation

The owner must separately approve all of the following on an exact committed
candidate SHA:

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
container itself must be restored while Gate 1 rows are unused, downgrade to
`0102` before starting it.

## Later gates, still forbidden

- Writer inventory revalidation and full legacy-writer quiescence/drain.
- One owner-approved fenced canary with real client VPN smoke.
- Production writer cutover and notification switch.
- Historical repair, refund/revoke, bulk cleanup or legacy teardown.

None of these actions was started by Gate 1.
