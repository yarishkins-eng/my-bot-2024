# Gate 2.2 — one-shot revalidation release card

Status: preparation only. This card does not authorize PR, merge, deploy, or `ENABLE_SHADOW`.

## Purpose

Gate 2.1 is already qualified complete. This optional Gate 2.2 milestone only prepares one bounded
production one-shot to verify that the released shadow-only `expire_at` millisecond comparison behaves
as expected on the live cohort. It is not a scheduler, long-lived shadow, writer, repair, or rollout.

The candidate has no `app/**` runtime change. It only re-pins the already reviewed one-shot controller
and its isolated E2E to the exact currently deployed source and image, adds this card, and keeps the
ordinary deploy classifier on the control-only path.

## Exact compatible boundary

- deployed source and deploy-state SHA:
  `39a0a0dcc5467f6cfe802629213dc3a57273ea25`;
- production engine image ID:
  `sha256:35dd4dfcd12932fc2cba9c84ef0345ada97ec848e1c3cb8efe52d098873f9f86`;
- OCI index:
  `sha256:35dd4dfcd12932fc2cba9c84ef0345ada97ec848e1c3cb8efe52d098873f9f86`;
- owner-only archive SHA-256:
  `fb9684ef36688ebd5dcdc89f79586ac48d69196bf163ca15b25623d8ecc0f355`;
- linux/amd64 manifest:
  `sha256:f44a431e3f89b8857e65608fbb16a5e4235587242dd8314c7c5047ec202786ea`;
- config:
  `sha256:090dc7c8340dab6c90400f8f9d9554878ff3c998c16c883a4f2b44d03ca68ab3`;
- schema must remain `0103`;
- all nine authority tables must remain empty;
- runtime flags must remain `0|0|0|0|1|0`;
- the fixed one-shot sidecar must be absent before and after the run.

The archive stays in owner-only private storage. It must not be committed to the public repository or
published as a public release asset. OCI identity and Docker engine `.Id` remain separate identity
domains.

## Exact public candidate boundary

The diff from the exact base above must contain exactly these seven paths:

1. `.github/scripts/entitlement-shadow-one-shot-control.sh`;
2. `.github/scripts/test-entitlement-shadow-one-shot-e2e.sh`;
3. `.github/workflows/deploy.yml`;
4. `.github/workflows/entitlement-shadow-one-shot-ci.yml`;
5. `.github/workflows/entitlement-shadow-one-shot.yml`;
6. `docs/entitlement_authority/gate2-2-one-shot-revalidation-release-card.md`;
7. `tests/workflows/test_entitlement_shadow_one_shot.py`.

No application code, comparator logic, migrations, schema, `.env`, Dockerfile, Compose, Panel contract,
scheduler, daemon, timer, projector, notification, writer, or repair code may change. The known fake
Panel exact-path P2 is deliberately not fixed here.

## Required evidence before any PR

- local targeted and full tests, Ruff, Bash syntax, YAML parse, and `git diff --check` are green;
- public exact-SHA CI proves the exact seven-file boundary and `runtime_diff=none`;
- a trusted private-main workflow verifies the exact archive chain and runs the isolated E2E without
  skip against the exact current production image;
- the isolated E2E proves one real sampled cycle, same-millisecond equality, adjacent-millisecond drift,
  read-only PostgreSQL enforcement, `Panel writes=0`, bounded deadlines, SIGKILL cleanup, and all
  protected Disable states;
- a reviewer and a skeptic independently return P0=0 and P1=0 on the exact candidate and exact runs.

After that evidence, STOP and request a separate owner GO for PR/merge. Opening or merging the PR is not
authorized by preparation work.

## Protected control-only release

If the owner separately approves PR/merge, the merge must use the protected normal path without admin
bypass. The merge tree must be byte-identical to the reviewed candidate tree. Exact merge CI must pass.
The ordinary production deploy workflow must classify the exact seven-file diff as control-only and
must stop before SSH, checkout, image build, container replacement, database, or Panel access.

After merge, verify that production source, deploy-state, image, bot container ID/start/restart, health,
`.env` hash, flags, schema, authority tables, sidecar/process absence, Panel mutation markers, and semantic
business fingerprints remain unchanged. Known operational lease fields are tracked separately.

Then STOP and request a separate owner GO for exactly one `ENABLE_SHADOW` execution. Merge approval does
not authorize the production one-shot.

## Fresh production preflight before the one-shot

Immediately before any separately approved run, capture a bounded read-only snapshot and require:

- GitHub `main` equals the exact reviewed Gate 2.2 merge/control SHA;
- production source and deploy-state remain the compatible runtime SHA
  `39a0a0dcc5467f6cfe802629213dc3a57273ea25`, and the production image remains the compatible image ID;
- the bot is healthy with no unexpected restart;
- `.env` hash, flags, schema `0103`, and authority tables `9×0` are unchanged;
- no fixed one-shot sidecar or shadow worker exists;
- Panel mutation markers and semantic business fingerprints match the dormant baseline;
- the deterministic live cohort is recorded without exposing user identifiers or raw Panel payloads.

The cohort size is not fixed. Previously observed sub-millisecond cases must compare exact if they are
still eligible. Do not change selection or policy to force a particular sample count.

## One-shot success and STOP rules

One execution is successful only when `sampled > 0`, all sampled rows are accounted for,
`exact=sampled`, `drift=0`,
`critical_drift=0`, `missing=0`, every Panel/contract/owner/comparator/rate-limit error is zero,
`Panel writes=0`, PostgreSQL and business fingerprints are unchanged, and the exact sidecar is absent no
later than 210 seconds after start.

Any mismatch, missing row, Panel error, write marker, timeout, identity/config drift, cleanup uncertainty,
or unexpected production change is STOP. Do not retry the one-shot. Perform only bounded read-only
attribution under a new explicit task. Do not repair data or change policy to make the result pass.

Even after a successful one-shot, STOP. This card does not authorize a scheduler, persistent shadow,
projector, notifications, writer cutover, authority-table population, user access change, or repair.
