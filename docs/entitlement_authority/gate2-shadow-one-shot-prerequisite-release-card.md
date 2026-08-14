# Gate 2 one-shot shadow prerequisite release card

This card releases only a dormant, protected one-shot control path. It does
not authorize `ENABLE_SHADOW` or a live production observation.

## Immutable compatibility boundary

- Compatible deployed source SHA:
  `103094b96f96a412463753e56e3d996311b182ec`.
- Compatible deployed image ID:
  `sha256:52df4d9531f5bb5084af19752cdcf593609687a35da2a0fa26c2995aac2d8b1e`.
- Owner-only archive SHA-256:
  `cc305348078ae92b4320758f84fbb1f176688e73be548266cd9ca4446026342b`.
- OCI index:
  `sha256:52df4d9531f5bb5084af19752cdcf593609687a35da2a0fa26c2995aac2d8b1e`.
- linux/amd64 manifest:
  `sha256:39545077b550badb008c76b81312706f69085a0f79a79705b6bbe6ad3ad6c276`.
- linux/amd64 config:
  `sha256:133309254d834f18ec0a50f9b57d7c37cdd73fda9b57bf7bdcb7ae8084f1fe67`.
- Schema: exactly `0103`; all nine authority tables must be empty.
- Production `.env` SHA-256:
  `dc35bf7aa92d570c5f190b3e7ccb8e2f22aa87b5d3d46f9277d63252fbd1057c`.
- Effective runtime flags before and after any future observation must be
  checkout/projector/ready/shadow `false`, kill switch `true`, and
  multi-tariff `false`.

Any drift is a hard stop. Updating this boundary requires a new reviewed
candidate, exact-image E2E, exact-SHA CI, reviewer GO, skeptic GO, and a new
owner decision.

The OCI index and a portable Docker `.Id` are distinct evidence. The private
E2E verifies the full archive/index/amd64-manifest/config chain, loads only
linux/amd64, and uses the OCI index only as the runtime reference accepted by
the compatible containerd image store. It does not compare the OCI index with
Docker `.Id`. The config digest remains separate archive content evidence.

The archive remains owner-only (`0700` directory, `0600` local file) and is
available only to a manual workflow in a separate private companion
repository. The public repository and fork workflows receive neither the
archive nor credentials for that repository. The private workflow has no
production credentials, no production host access, no PR/fork trigger, and
publishes no artifact. Candidate code runs only inside a disposable
Docker-in-Docker sandbox with outer network mode `none`; the job fails unless
the sandbox reports a containerd-backed image store.
The exact companion harness commit is
`760d840c37303290a8823530db4daa97c34f4004`; both its Docker-in-Docker base
and PostgreSQL fixture are registry-digest pinned.

## Released control surface

The manual workflow accepts only `ENABLE_SHADOW` and `DISABLE_SHADOW`. It is
main-only, uses the existing protected production Environment, shares the
production deploy concurrency group, validates the current `origin/main`, and
transfers fixed primitives through a unique temporary directory with exact
SHA-256 verification.

The Environment deployment record is the authoritative approval. The
workflow actor is only `request_actor`. Required fixed phrases are:

- `OWNER_APPROVED_ENABLE_SHADOW` for a separately approved future observation;
- `OWNER_APPROVED_DISABLE_SHADOW` for emergency cleanup or the release
  rehearsal.

The exact release-card input is this repository path:
`docs/entitlement_authority/gate2-shadow-one-shot-prerequisite-release-card.md`.

## One-shot safety contract

The sidecar uses the immutable compatible image, UID/GID `1000:1000`, a
read-only root, bounded tmpfs, no capabilities, no-new-privileges, fixed
memory/CPU/PID limits, `restart=no`, no healthcheck, and exactly the bot DB
network plus `remnawave-network`. It receives only the DB and read-only Panel
credentials, a dummy bot token, and reviewed fixed policy values. It has no
Docker socket, Redis, Telegram, payment, SMTP, cabinet, bot-data, or shared-log
mount/credentials.

The external read-only entrypoint calls the existing public source, Panel
provider, policy and `ReadOnlyShadowRunner` exactly once. The existing source
owns `SET TRANSACTION READ ONLY` and the local statement timeout. The existing
Panel method owns the redacted GET and `max_retries=0` boundary. Incidental
application logs are dropped; stdout contains exactly one validated bounded
aggregate JSON event. The controller validates the live created-container
inspect state, attaches before start with `docker start -a`, and treats
missing, malformed, oversized, or non-zero-process evidence as a failed
Enable after cleanup and postchecks.

The in-container cycle deadline is 180 seconds, TERM deadline 195 seconds,
hard kill follows after 10 seconds, and the fixed-name auto-remove container
must be absent no later than 210 seconds after start. It self-terminates after
controller SIGKILL or SSH response loss. A second Enable while the fixed name
exists fails closed.

## Emergency Disable

Disable depends only on Docker and the fixed container identity
`teplo-entitlement-shadow-one-shot` with label
`teplo.role=entitlement-shadow-one-shot`.

- Absent is an idempotent no-op.
- Running, stopped, or paused owned containers are removed.
- A foreign fixed-name container fails closed and is never removed.
- Raw logs are never published. Strict aggregate evidence is retained when it
  validates; otherwise the summary says `observation_evidence=unproved` while
  cleanup still proceeds.
- The production bot is never stopped, paused, recreated, or restarted.

## Required release evidence

Before the dormant release:

1. local full tests, Ruff, format, shell syntax, YAML parse and diff checks;
2. mandatory isolated Linux E2E against the exact compatible image, disposable
   PostgreSQL schema with one LIMITED candidate, and a mutation-recording fake
   Panel;
3. exact candidate-SHA CI, then independent reviewer and skeptic GO with no
   P0/P1 or unresolved verification;
4. protected PR/merge with merge tree equivalent to the reviewed candidate;
5. exact merge-SHA CI and fresh reviewer/skeptic GO;
6. ordinary deploy proves the exact control-only classification stopped before
   SSH, source checkout, build, or container switch;
7. protected `DISABLE_SHADOW` absent no-op rehearsal with unchanged production
   bot identity/image/start/restart count, `.env`, schema, business-data
   fingerprints, Panel mutation markers, authority-table counts and worker
   count.

For the current prerequisite verification, the owner explicitly prohibited
merge, deploy, and production `ENABLE_SHADOW`. Work stops after a new exact
candidate SHA, green public candidate CI, green private exact-image E2E, and
fresh reviewer/skeptic verdicts. Those results do not authorize release.

After that rehearsal, stop. Production `ENABLE_SHADOW` remains forbidden until
a separate explicit owner GO.
