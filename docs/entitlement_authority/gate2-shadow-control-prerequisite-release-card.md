# Gate 2 protected shadow-control prerequisite release card

Production base at preparation: `103094b96f96a412463753e56e3d996311b182ec`,
schema `0103`, Gate 2 dormant, all four entitlement flags `false`, shadow kill
switch `true`. This card authorizes preparation, review, merge and dormant
release of the protected control path only. It does **not** authorize
`ENABLE_SHADOW`.

## Fixed control contract

The manual workflow exposes exactly two choices under the protected
`teplo-vpn-production-controlled-change` Environment:

- `ENABLE_SHADOW`: starts one isolated read-only sidecar only after reusable
  CI, exact-current-main/deploy/image/schema checks and first successful cycle;
- `DISABLE_SHADOW`: removes the sidecar lease and fixed sidecar container only.

There is no arbitrary key, value, command, path or image input. Both actions
require `main`, the exact owner phrase, a strictly validated non-secret
release-card reference and a recorded approval actor. Enable additionally
requires the owner-supplied exact production deploy-state SHA. A stale workflow
re-run is rejected if its checked-out SHA is no longer current `origin/main`.
An approved GitHub re-run of the same current-main workflow (which receives a
new run-attempt number) may only recover the already completed lease of the
same workflow run; it cannot perform a second enable transition.

## Isolation boundary

Shadow never runs inside `remnawave_bot`. Enable does not stop, recreate or
restart that container and proves its ID, image and start time remain unchanged.
The sidecar is created directly from the immutable running bot image ID with:

- restart policy `no`;
- read-only root filesystem and bounded tmpfs;
- all Linux capabilities dropped and `no-new-privileges`;
- memory, CPU and PID limits;
- only the production DB and RemnaWave networks;
- no Docker socket, bot data/log volume, Telegram token, payment credentials,
  SMTP, Redis or cabinet secrets;
- only the DB and RemnaWave credentials needed for read-only observation,
copied to a mode-`0600` temporary env file and deleted immediately after
  container creation;
- a fixed dummy `BOT_TOKEN`, because Settings requires the field although the
  sidecar never constructs a Telegram bot.

The fixed reviewed matrix is hard-set and verified from the created container:
all three writer flags `false`, shadow `true`, kill switch `false`,
`MULTI_TARIFF_ENABLED=false`, 10% cohort, 18 identities per 15-minute cycle,
12 sequential Panel GET/minute, 4-second Panel timeout, 180-second cycle limit
and the reviewed numeric STOP thresholds. DB pool is limited to two
connections with no overflow.

The sidecar entrypoint imports only the Gate 2 read-only service. Every scan
uses a PostgreSQL READ ONLY transaction. Panel access is the reviewed redacted
single GET without retry. It exits when its host-owned lease is missing,
malformed, superseded or expired, and exits when the shadow service opens its
circuit. Since restart policy is `no`, a circuit STOP remains stopped across
Docker or host restarts.

## Permanent baseline and policy control

Production `.env` remains unchanged and permanently defaults to shadow off and
kill switch on. The baseline verifier treats any case-insensitive mention of a
writer/shadow/multi-tariff managed key as unsafe, including comments, quoted,
`export`, whitespace and alternate boolean forms. It does not parse or print
values. Existing unrelated traffic/device policy keys are permitted because
the sidecar overrides and verifies the reviewed effective values. The script
records and rechecks the `.env` SHA-256 fingerprint.

No code path writes `.env`, PostgreSQL, Redis, RemnaWave, payments, webhook,
notifications, subscriptions or user/business data. The production bot is not
addressed by any mutating Docker command.

## Durable bootstrap and hard-kill policy

Before creating the sidecar, Enable writes a five-minute prepared lease and
arms an independent root-owned transient systemd watchdog. The first watchdog
is bound to the fixed name plus exact run labels; after Docker returns a
container ID, a second distinct watchdog is armed against that immutable ID
before the first one is stopped. The shell ERR trap
removes the lease and sidecar on ordinary failure. If the runner, SSH process
or control shell is killed and the trap cannot run, the watchdog independently
removes only its own exact sidecar generation whose lease did not reach a valid
completed state. A stale bootstrap or disable timer cannot remove a later run.
If an Enable retry finds a prepared lease from the same workflow run, it may
only invoke that exact generation's installed watchdog while the existing
timers remain armed, prove lease, secret file and sidecar absence, stop and
verify the old timers, then terminate with `STOP`. It never reuses the same
generation: the owner must start a new protected workflow run with a new run
ID before another Enable attempt.

Enable waits for a real `entitlement_shadow_cycle` with `sampled > 0`, refuses circuit/stopped/
lease-loss markers, rechecks the unchanged bot, dotenv fingerprint and nine
empty authority tables, then atomically changes the lease to `completed` with
a seven-day expiry. The watchdog treats only that exact completed lease as the
commit point, arms a separate host-owned hard-expiry timer, and idempotently
materializes both keyed and latest root-only audits if the workflow response is
lost. A re-run (including a later run-attempt of the same GitHub run) recovers
the same completed lease/audit; it never performs a second enable transition.
Conflicting or orphan state is fail-closed.

If any final Enable or completed-recovery gate fails, cleanup first atomically
copies the exact lease to `failed-enable-cleanup.state`. That immutable copy is
the durable deny/cleanup decision and the byte-for-byte receipt preimage. It is
written before any active receipt, lease or container is removed. Controller
and watchdog retries must finish that same generation's cleanup; they may not
re-evaluate a recovered sidecar as active. The marker remains until all
lease-equal provisional receipts are removed, the exact sidecar is proven
absent and a disabled latest audit is durable. A receipt comparison I/O error
is a STOP, not a mismatch. All production deploy/recovery routes refuse the
marker, and an Enable recovery completes cleanup then requires a fresh
owner-approved workflow run.

The sidecar checks the lease every two seconds. Independently, the host expiry
timer removes that exact container generation at seven days, even if the
sidecar process is paused. Automatic circuit STOP exits and does not restart.
A new observation therefore always needs a new protected owner-approved Enable
run.

## Emergency disable

Disable deliberately skips reusable CI. It still requires current reviewed
workflow code and protected Environment approval, but the production-side
primitive does not depend on Git state, bot health, deploy-state, migration
journal, PostgreSQL, Redis, RemnaWave or `.env`.

It acquires the dedicated control lock and first arms an independent
generation/label-fenced disable helper. Creating the immutable disable
tombstone is the durable commit point: a kill before that point leaves the
running observation unchanged and reports no success; after it, the armed
helper keeps retrying independently and both future Enable and deploy paths
remain blocked. Disable then removes the lease (causing
self-termination), removes only `teplo_entitlement_shadow`, and cancels the
old generation's bootstrap/expiry timers. It never addresses
`remnawave_bot`. If Docker is unavailable after lease removal, it reports
failure rather than falsely claiming success; the helper retries, while
restart policy `no` prevents an old sidecar from returning after a daemon
restart. The disable audit is keyed by workflow run/attempt and is idempotent
under lost response/re-run.

All four production image/schema switch workflows share the same concurrency
group and contain an inline fail-closed guard. Ordinary, migration,
infrastructure and migration-recovery deploys refuse to run while a shadow
lease, disable/failed-enable cleanup marker or sidecar exists. Operators must
complete the appropriate cleanup first, so an observation can never silently
continue against a newer source, image or schema.

## Prerequisite release and verification

The prerequisite is released by protected PR merge and the manual protected
`deploy-infrastructure.yml` workflow for the exact reviewed merge SHA. The
ordinary deploy must stop before checkout/build/container switch because the
candidate changes `.github/**`. Its control-plane-only mode accepts the full
diff only when every path is in the exact reviewed scripts/workflows/docs/tests
allowlist. It checks out the reviewed source and atomically advances deploy
state while preserving the existing bot container ID, image and start time;
it does not build, recreate or restart the bot. A kill after source checkout
is recovered only from a root-owned prepared journal containing the exact
base/target SHA and prior bot container ID, image and start time. A kill after
the deploy-state write is completed by validating and clearing that same
journal. Any restart or recreation is a STOP. While that journal is prepared,
ordinary, migration and migration-recovery deploys, plus any Docker/Compose
infrastructure switch, stop fail-closed under the same host lock; only the
protected infrastructure workflow may consume it. If `main` has advanced, that
workflow first proves and cancels or finalizes the older generation, then
stops; a fresh protected run is required for the newer SHA. Every success gate
requires exact stable container ID, image and start time plus `Running=true`,
`Paused=false` and healthy both before and after the atomic deploy-state write.
Shadow remains
absent. Required checks:

1. exact merge SHA/tree, exact-SHA CI and fresh reviewer/skeptic GO with
   `P0=0`, `P1=0`;
   CI must run the dedicated real-Docker hard-kill/watchdog proof without skip;
2. schema still `0103`; no migration, Compose, Dockerfile, app writer or
   business-flow file changed; production deploy/recovery workflow changes are
   limited to the active-shadow interlock and the protected source-only
   control-plane transition/recovery described above;
3. production bot/PostgreSQL/Redis/HTTP/Telegram healthy; bot ID/start time
   stable during the separate disable rehearsal;
4. all four production entitlement flags `false`, kill switch `true`;
5. no shadow/projector/notification worker and all nine authority tables empty;
6. Panel GET-only verification and zero release-related Panel mutation marker;
7. one protected `DISABLE_SHADOW` rehearsal: absent lease/sidecar, keyed audit,
   no bot restart, unchanged dotenv and unchanged business-state fingerprints.

STOP on any SHA/image/schema/Environment drift, changed `.env`, unexpected
sidecar, bot restart, authority row, Panel mutation, user/business mutation,
missing audit, secret/PII emission or inability to prove idempotent disable.

After the dormant release and rehearsal, stop. `ENABLE_SHADOW`, the seven-day
observation, canary, projector and writer cutover each require later separate
owner decisions. The two known writer-cutover P2 blockers remain outside this
prerequisite: durable receipt proof for `bind_uuid()` and lack of RemnaWave
CAS/ETag.
