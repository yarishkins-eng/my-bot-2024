# Next owner gate — Gate 2 dormant code deploy only

Gate 1 is deployed at exact `80bf7e7262e174ef8fa6ada0dcce503571c6395e`
and schema `0103`. Gate 2 candidate preparation does not authorize PR merge,
deploy or any flag change.

## Exact next gate: owner-approved dormant Gate 2 code deploy

After the exact candidate SHA, exact-SHA CI and both independent verdicts are
reported, the owner may separately approve only:

1. a protected PR merge and code-only deploy with all four entitlement flags
   false and the shadow kill switch true;
2. no migration: live schema must remain exactly `0103` and all dormant tables
   empty;
3. post-deploy health, logs, one legacy worker, no shadow/projector task, no
   Panel mutation and unchanged checkout/AP/payment/webhook/notification flows.

Only after that evidence may the owner consider another explicit decision to
set `SHADOW=true` and `KILL_SWITCH=false` for the bounded observation window in
[the Gate 2 release card](gate2-shadow-release-card.md). That enablement is not
part of the dormant deploy request.

Stop immediately on any mutation, schema/flag/SHA ambiguity, PII/raw response,
unexpected worker or legacy regression. A dormant code rollback is an ordinary
protected revert; schema remains `0103`. Do not downgrade or run the migration
workflow for Gate 2.

## Later gates, still forbidden

- Writer inventory revalidation and full legacy-writer quiescence/drain.
- One owner-approved fenced canary with real client VPN smoke.
- Production writer cutover and notification switch.
- Historical repair, refund/revoke, bulk cleanup or legacy teardown.

None of these actions was started by Gate 1.
