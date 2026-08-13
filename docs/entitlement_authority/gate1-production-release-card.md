# Gate 1 dormant foundation production release card

Owner authorization: 2026-08-13. The protected workflow's `github.sha` is the
exact release candidate and must match the independently reviewed final SHA and
the then-current `origin/main`.

## Allowed change

- Apply additive Alembic revision `0102 → 0103` and deploy the same exact
  source SHA through `.github/workflows/deploy-migration.yml`.
- Keep all four runtime switches false:
  `ENTITLEMENT_AUTHORITY_CHECKOUT_ADMISSION_ENABLED`,
  `ENTITLEMENT_AUTHORITY_PROJECTOR_ENABLED`,
  `ENTITLEMENT_AUTHORITY_READY_NOTIFICATIONS_ENABLED`, and
  `ENTITLEMENT_AUTHORITY_SHADOW_ENABLED`.
- Start no entitlement worker or production adapter. Gate 1 has no production
  callsite for its reducer, strict Panel fake, coordinator, cleanup or shadow
  evaluator.

## Forbidden change

- No shadow enablement or observation window.
- No RemnaWave POST/PATCH/DELETE/action, topology, node, Squad, Host, inbound or
  user mutation.
- No checkout, payment, subscription, user, notification, repair, refund,
  revoke, erasure or backfill mutation beyond unchanged legacy application
  behaviour.
- No manual production source edit, direct Compose deploy, schema downgrade or
  broad service restart.

## Required gates

1. Fresh `origin/main` equality and exact final SHA reviewer/skeptic GO with no
   open P0/P1.
2. Protected GitHub environment restricted to `main`, owner approval required,
   admin bypass disabled.
3. Fresh protected database backup with server/local size and SHA-256 equality;
   isolated restore must be revision `0102` and upgrade to `0103` successfully.
4. Migration workflow inputs `BACKUP_RESTORE_VERIFIED`, `OWNER_APPROVED`, and
   this release-card path.
5. Post-deploy proof: deploy/source exact SHA, schema `0103`, healthy bot,
   PostgreSQL and Redis, clean startup/runtime logs, all four flags false, one
   existing `python main.py` worker only, no new entitlement-table rows, no new
   Panel mutation evidence, and compatible legacy health/user flows.

## Stop and recovery

Stop immediately on SHA drift, non-empty/dirty server source, backup or restore
mismatch, schema not exactly `0102` before or `0103` after, any flag true,
unexpected worker/table row, Panel mutation, unhealthy dependency, new critical
log, or legacy-flow regression.

The migration is additive and its guarded downgrade is allowed only while all
nine new tables are empty. If the candidate cannot run, use the protected
migration recovery record; an old image may start only after the unused schema
is returned to `0102`. Never infer authority to restore user data or mutate
RemnaWave from this card.
