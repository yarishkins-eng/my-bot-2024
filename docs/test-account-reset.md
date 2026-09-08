# Test-account reset contract

This feature is opt-in, not a generic account erasure tool. Enrollment and reset
require both `users:delete` and the existing server-configured superadmin identity
(`ADMIN_IDS` or a trusted, verified `ADMIN_EMAILS` identity). Delegated managers
cannot enroll or reset fixtures. Enrollment itself never changes subscriptions
or money. The confirmation identifies the selected user's exact Telegram ID.

## Operator workflow

1. Search for an existing user in Cabinet → Admin → Users (Telegram ID/name).
2. In the user card select **Enroll as tester**, verify the ID, and confirm.
3. **Preview reset** shows balance, subscriptions, transactions, tickets and
   other local history to be removed. Confirm only for a consenting tester.
4. An unfinished reset remains blocked; preview and confirm again to resume.
   Never remove the marker manually to unlock an uncertain remote operation.
5. Send `/start`, activate a new trial, remove the old provider profile in the
   VPN client, and import the new link. Do not reinstall the client application.
6. **Remove from testers** only revokes reset eligibility, without deleting data.

`User.test_account_enabled` is a nullable override: true enrolls, false excludes
even an environment-listed account, null uses `TEST_ACCOUNT_TELEGRAM_IDS`.
Existing environment fixtures continue working without manual enrollment.
Removing membership retains reset history and retired UUIDs to reject late events.

## Safety boundary

- A session advisory lock permits one reset per user. Before remote IO, existing
  user/subscription/checkout/child writes are drained under row locks, financial
  guards and the preview fingerprint are rechecked, then `resetting` is committed.
- Migration 0105 guards writes during `resetting`/`failed`, including child rows
  through `user_id`, `subscription_id`, and `checkout_id`. Normal accounts have
  null state; their business behavior is unchanged. New schema ownership paths
  must receive an explicit reset/guard review in any future migration.
- Remote discovery is a full sweep by exact Telegram/email plus saved UUIDs.
  Every existing identity must match the Telegram owner and have no conflicting
  local owner. Multiple valid identities and an already empty panel are allowed.
  Deleted UUIDs are saved before IO; every DELETE requires a subsequent absent
  GET. Timeout or partial deletion leaves a durable, resumable failed reset.
- Only after remote absence is local history removed, child-first, in one
  transaction. Shared counters are adjusted; other users, their earnings and
  durable audit/financial evidence are preserved. In-flight payments, special
  reconciliation records and privileged target accounts block the operation.
- Current-subscription checks fence outbound create/update/revoke/retry. Inbound
  webhook, import and manual panel-sync snapshots reject retired identities.
  A completed reset bypasses the old destructive second wipe in `/start`.
- No reset preserves a bearer subscription URL. The strict test-link flag also
  forbids cached fallback when the current-link endpoint fails. `/info`,
  `/connection-link`, and `/app-config` share the same nullable `test_reset_at`
  string and `test_link_strict` predicate (membership OR reset history). A tester
  before their first reset legitimately has a null epoch. Removing membership
  never removes the history predicate. Clients must wait for fresh metadata and
  matching generations across all connection surfaces, including QR navigation
  history and installation-guide deep links.
- Delivered guest-purchase financial records survive, but their stored link,
  crypto link, temporary cabinet password and auto-login credential are cleared
  when this reset account is the delivery recipient (`user_id`). A gift merely
  bought by the tester for someone else (`buyer_user_id`) is not redacted.
  Preview does not clear anything; redaction runs inside the confirmed reset
  transaction after verified panel deletion.

This is a clean **subscription/onboarding test cycle**, not deletion of the
person's identity: Telegram/email login, referral identity, external channel
membership and required audit/other people's financial records survive.
Channel observations are invalidated best-effort; a background check can rebuild
them from actual Telegram membership. Such a cache is not trial eligibility.
Webhook events without a UUID are intentionally ignored for reset history users;
they cannot be safely assigned to a generation by Telegram ID alone.

## Verification and release

Run the normal lint/tests plus
`tests/integration/test_test_account_reset_postgres.py` against a disposable local
PostgreSQL database named `teplo_reset_test`, configured with
`TEST_ACCOUNT_RESET_TEST_DATABASE_URL`. This test suite destroys that DB's public
schema and rejects non-local hosts/other names. It never contacts the live panel.
Panel HTTP behavior uses deterministic test doubles; a real iOS import/connect
is still an operator acceptance check after deployment.

0105 is additive and uses bounded DDL waits (2 s lock / 20 s statement timeout).
Deployment requires the protected migration workflow, backup + actual isolated
restore/upgrade, independent review and explicit owner go/no-go. A feature-branch
commit does not authorize production changes.

## Rollback

Keep schema/model migration in a separate commit from the application changes.
Reverting the application commit can retain revision 0105; the frontend is a
separate revert. Do not blindly remove the migration file while the database
reports 0105. Downgrade refuses any recorded membership or reset history.

Before an application rollback, finish or explicitly resolve all busy resets,
pause testing, and reconcile DB membership overrides with the old environment
allowlist: old code does not understand DB enrollment/exclusion or UUID tombstones.
Do not resume destructive test resets on old code. Emergency old-image recovery
uses the protected recovery workflow and its `SKIP_MIGRATION=true` contract, with
an explicitly reviewed target-schema compatibility decision.

A git revert reverses code, never an already executed reset, remote identity
deletion, spent balance or a tester's removed history. Do not restore a complete
production DB merely to recover one tester; that would overwrite newer unrelated
data. Any data recovery needs a separate, precise owner-approved plan.

## Production compatibility findings (2026-09-08)

Revision 0105 stores the retired UUID list as PostgreSQL `json`, which has no
equality operator. Do not apply full-row `DISTINCT` to the mapped User. The
ordinary low-balance monitor now selects eligible user IDs in a subquery, keeping
one notification per user without comparing JSON. Its query uses a savepoint so
a statement error does not roll back earlier work in the monitoring transaction.
Real-PostgreSQL regression tests retain the deployed JSON representation and
check two subscriptions / one notification and survival of an earlier pending
write after an injected SQL error. A future JSONB conversion needs a new forward
migration and its own approval; never rewrite the already-applied 0105.

The existing `recover-after-migration.yml` must NOT be used in webhook mode: it
incorrectly requires the `Aiogram polling запущен` log and would revert the
recovery switch. This overrides the generic emergency-recovery paragraph above.
The incident was stabilized through the normal code-only deploy path, not by
bypassing recovery gates or restoring the production database. Rehearsal of a
correct webhook-aware recovery is separate follow-up work.
