# Gate 2 one-shot shadow prerequisite: bounded design

Status: design only.  Production `ENABLE_SHADOW` is not authorized by this
change.  Compatible dormant runtime: source
`103094b96f96a412463753e56e3d996311b182ec`, schema `0103`, image
`sha256:52df4d9531f5bb5084af19752cdcf593609687a35da2a0fa26c2995aac2d8b1e`.

## Process and container lifecycle

`ENABLE_SHADOW` is a manual, protected action.  It transfers one reviewed
Python entrypoint and one fixed controller into a unique run directory,
verifies both SHA-256 values, and checks the exact deployed SHA/image twice.
The controller refuses any pre-existing fixed-name container.  It creates one
auto-remove sidecar from the already deployed immutable image, connects only
the database and RemnaWave networks, starts it once, removes the exact host
temporary files, and waits for the container to disappear.

The entrypoint constructs the existing public `LegacyPostgresShadowSource`,
`RemnaWaveShadowPanelProvider`, `shadow_policy_from_settings`, and
`ReadOnlyShadowRunner`; it calls `run_once()` exactly once.  It does not call
the scheduled `EntitlementShadowService`, create a task, lease, timer,
watchdog, daemon, or persistent observation state.

The container is `USER 1000:1000`, read-only, `restart=no`, auto-remove, and
has no healthcheck.  It has a bounded tmpfs, all capabilities dropped,
`no-new-privileges`, and fixed memory/CPU/PID limits.  It mounts only the
verified entrypoint read-only.  It receives the minimum database and Panel
credentials plus fixed non-secret policy settings; it receives no Docker
socket, Redis, Telegram, payment, SMTP, cabinet, bot-data, upload, locale, or
shared-log mount/credential.  `BOT_TOKEN` is a dummy value.

## Deadlines and controller loss

The existing runner owns the 180-second whole-cycle deadline.  Container PID
1 is GNU `timeout`: it sends TERM at 195 seconds and KILL 10 seconds later.
The Docker daemon owns `restart=no` and auto-remove, so an SSH/GitHub runner
loss cannot convert the process into a worker.  The controller proves the
fixed container absent by 210 seconds.  The controller reports start only
after the run-owned files have been unlinked; a SIGKILL after that point leaves
no host evidence file and the daemon still terminates/removes the container.

No response is treated as success.  A lost response may leave one bounded
cycle running, but the fixed name makes a concurrent retry fail closed.  A new
Enable is permitted only after a later independent absence proof or a
successful protected Disable.

## Read-only boundaries

The database source begins a PostgreSQL transaction, executes `SET
TRANSACTION READ ONLY`, applies a local 5000 ms statement timeout, reads the
bounded cohort, and rolls back.  The Panel provider uses the existing strict
shadow transport: one redacted GET per candidate, `max_retries=0`, four-second
timeout, no link enrichment, and no mutating method in its interface.

All writer flags remain false.  Shadow true and its kill switch false exist
only in the sidecar environment.  Multi-tariff stays false.  The production
bot `.env`, process, container, image, source, database, Redis, Panel, and user
state are never written or restarted.

## Identity and evidence

The fixed container name is `teplo-entitlement-shadow-one-shot`; its required
label is `teplo.role=entitlement-shadow-one-shot`.  A second label binds the
reviewed control release SHA.  Any existing fixed-name container makes Enable
fail without inspect-based adoption, removal, restart, or replacement.

The entrypoint suppresses incidental application logging and emits exactly
one JSON event.  Both producer and controller enforce an exact event/field/type
allowlist, fixed mismatch/cohort names, fixed stop codes, and bounded integer
values.  Raw lines and exception text are never published or grepped.  Only
the canonical aggregate event plus keyed controller facts appear in the
GitHub workflow log/summary; nothing is retained on the production host.
Missing, malformed, or lost evidence is `observation_evidence=unproved`, never
a successful observation.

## Emergency Disable

`DISABLE_SHADOW` depends only on Docker access.  It does not read or require
the database, Panel, Redis, schema, production source, `.env`, or bot health.
Absent is a successful no-op.  A running, stopped, or paused exact-name
container is removed only after its exact `teplo.role` label is verified.
An unexpected label or ambiguous identity fails closed and cannot touch
`remnawave_bot`.  Available output is accepted only through the same strict
aggregate validator; missing output does not block emergency removal and is
reported as unavailable.  Cleanup names the exact run directory and files;
it uses no glob.

## No persistent control plane

This design requires no scheduler, long lease, systemd unit/timer, watchdog,
revoke tombstone, takeover state machine, source pin journal, or general
production transition supervisor.  The only durable additions are reviewed
Git/workflow/docs/tests files; dormant release leaves the production
application image, source checkout, configuration, and data unchanged.
