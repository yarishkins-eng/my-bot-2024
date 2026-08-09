# RemnaWave access-point source probe — 2026-08-09

Status: owner-approved, read-only contract preflight. This record contains no
Host title, Host UUID, Internal Squad UUID, node address, credential, or raw
inbound value.

## Scope and result

- The probe used the production bot's existing authenticated RemnaWave client
  and only `GET` requests. It made no Panel, database, catalog, tariff, policy,
  subscription, node, Host, or inbound mutation.
- `GET /api/hosts` returned a list whose safe fields included the Host title,
  visibility/disabled flags, and the `(configProfileUuid,
  configProfileInboundUuid)` relation.
- `GET /api/internal-squads` returned matching inbound
  `(profileUuid, uuid)` relations and raw-inbound evidence. The implementation
  hashes this evidence server-side; it never returns the source values.
- `GET /api/internal-squads/{id}/accessible-nodes` was available for every
  observed Squad. Its `activeInbounds` values matched the verified Internal
  Squad inbound **tags** (aggregate only; no values were retained or printed).
  The production adapter additionally requires this exact tag set and current
  `GET /api/nodes` connectivity/disabled/connecting evidence before it can
  call a Squad healthy.
- The observed Host-to-Squad graph was shared rather than one Host to one
  dedicated Squad. It is therefore non-selectable by design. No local catalog
  apply was requested or performed.

## Follow-up boundary

The deploy-time application dry run is separately time-armed and read-only.
It must be observed and recorded before a later, separately approved local
catalog apply. A topology conversion is outside this probe and requires its
own protected operation and owner approval.
