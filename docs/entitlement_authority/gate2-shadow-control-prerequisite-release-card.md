# Gate 2 protected shadow-control prerequisite release card

Production base at preparation: `103094b96f96a412463753e56e3d996311b182ec`,
schema `0103`, Gate 2 dormant, all four entitlement flags `false`, shadow kill
switch `true`. This card authorizes preparation, review, merge and dormant
release of the protected control path only. It does **not** authorize
`ENABLE_SHADOW`.

## Fixed control contract

The workflow exposes exactly two choices:

- `DISABLE_SHADOW`: runtime `SHADOW=false`, `KILL_SWITCH=true`;
- `ENABLE_SHADOW`: runtime `SHADOW=true`, `KILL_SWITCH=false`.

Both choices hard-set checkout admission, projector and ready notifications to
`false`. There is no arbitrary key, value, command, path or SHA input. The
workflow is manual, serializes with production deploys, requires branch
`main`, the protected `teplo-vpn-production-controlled-change` Environment,
the exact owner phrase, the owner-supplied exact production deploy-state SHA
and a non-secret release-card reference.

Both actions run the full reusable CI first and keep the same protected
Environment approval and runtime preflight. If GitHub Actions or CI itself is
unavailable, the safe state remains the permanent disabled `.env` baseline;
no manual production mutation is authorized by this card.

## State outside Git

The production `.env` is a permanent fail-closed baseline. The control path:

1. allows each managed variable to be absent (the reviewed code defaults) or
   present exactly once;
2. requires any explicit writer/shadow value to be `false` and an explicit
   kill-switch value to be `true`;
3. requires a regular UTF-8 file that is not group/world writable;
4. records a SHA-256 fingerprint before runtime mutation;
5. never writes, copies, prints or exports the file and proves the fingerprint
   unchanged afterwards.

The runtime transition uses a root-only temporary Compose override in
`/var/lib/teplo-vpn/deploy-state`, never in the Git worktree. The fixed
override contains only the five non-secret entitlement booleans. It is deleted
after the transition. A keyed mode-`0600` audit records action, resulting mode,
exact source SHA, unchanged image ID, GitHub run/attempt and completion time;
it contains no user, payment, Panel or secret data.

## Preflight and mutation boundary

Before stopping the bot, the script proves:

- server Git worktree is exact; `origin/main` is the workflow SHA;
- enabling uses the exact currently deployed main SHA; disabling may run when
  deployed SHA is an ancestor of newer main;
- deploy-state SHA/image equal the live source/image;
- bot is healthy; enabling requires migration recovery journal `completed`,
  while emergency disabling accepts only the known `prepared`, `completed` or
  `recovered` phases;
- PostgreSQL revision is exactly `0103`, read through `BEGIN READ ONLY`;
- production `.env` is the safe baseline above.

The transition stops only the bot service, force-creates it from the unchanged
current image with the fixed override, proves the five effective environment
values before start, starts it, waits for health and proves exactly one
`python main.py`. Enabling additionally requires the startup marker
`Read-only shadow запущен`; disabling requires `SHADOW=false`.

No image build/pull, Git checkout, migration, DDL/DML, Panel call, VPN-node
restart, payment, webhook, notification or user-data action belongs to this
path.

## Failure and interruption policy

Any failure after mutation starts invokes a fixed disabled recovery using the
same captured image and then verifies health, process count, logs, effective
flags and unchanged `.env` fingerprint. The requested run still fails so the
partial result is never reported as success. If safe disable cannot be
proven, the bot container is stopped rather than leaving an unverified shadow
runtime active.

A runner/SSH/process kill cannot persist an enabled override: the override
exists only in a root-only temporary directory and the production `.env`
remains disabled. A killed run can temporarily leave the bot stopped or the
current container running; the separately protected `DISABLE_SHADOW` action
recreates and proves the safe disabled runtime. No automatic unreviewed
takeover is attempted.

## Prerequisite release

The prerequisite is released by protected PR merge followed by the ordinary
production deploy of the exact merge SHA. The ordinary deploy runs because
the release-card update is part of the tracked source diff; it installs the
workflow, scripts and tests while preserving the permanent disabled `.env`
baseline.
The ordinary deploy must complete healthy and prove:

- exact source/deploy-state merge SHA and exact built image;
- schema remains `0103`, no migration files changed;
- all four entitlement flags remain `false`, kill switch remains `true`;
- shadow task/metrics and authority workers remain absent;
- all nine authority tables remain empty;
- no Panel mutation or user/business-data mutation occurred.

After those checks, perform one protected `DISABLE_SHADOW` rehearsal only. It
must recreate the same image in disabled mode, keep `.env` byte-identical,
write a keyed audit and leave bot/PostgreSQL/Redis/HTTP/Telegram healthy. This
is the dormant prerequisite release; it is not shadow observation.

STOP on any SHA/image/schema/flag/baseline mismatch, Environment protection
drift, `.env` fingerprint change, unexpected process, authority row, Panel
mutation, missing audit, degraded health, PII/secrets in output, or inability
to prove fail-safe disable.

Only after exact-SHA reviewer and skeptic GO, successful prerequisite release
and successful protected disable rehearsal may the owner issue a separate GO
for `ENABLE_SHADOW` and the seven-day read-only observation window.
