# RemnaWave 2.8.1 API contract

Sources: production GET `/api/system/metadata` (`2.8.1`), the exact official
backend tag `2.8.1` at commit
`ba51868149362d0b9ac0e23133d0532176ccb5a2`, and the clean bot client at base
SHA. No production identity was mutated.

| Contract | Verdict | Evidence |
|---|---|---|
| UUID lookup | proven | GET by UUID returns one exact UUID or 404 |
| username lookup | proven | schema has unique username; GET by username exists |
| Telegram lookup | contradicted for uniqueness | endpoint returns a list; Telegram is nullable/not unique in Panel |
| exact response UUID | proven | create/update response schema includes UUID; production GET inventory parsed it |
| `activeInternalSquads=[]` clears | proven in source | service treats `[]` as a change and repository removes all then adds none; bot client can PATCH `[]` |
| nullable `externalSquadUuid` | proven for PATCH | 2.8.1 DTO accepts null and repository persists nullable field |
| explicit `hwidDeviceLimit=null` | proven in Panel, contradicted in bot client | DTO accepts null; bot `update_user()` omits it when None |
| zero traffic | proven | contract explicitly defines `0` as unlimited |
| read-after-write consistency | unknown | no non-production mutation target; source returns post-transaction row but node/event propagation is asynchronous |
| action idempotency | contradicted/unknown | already enabled/disabled return 400; reset/revoke/delete lost-response safety not proven |
| A039 | contradicted in bot | official 2.8.1 defines A039 as generic “Update user error,” not external-squad-only FK; bot deletes the field and retries PATCH/POST |
| lost CREATE response | unsafe | username is unique but bot retries POST before lookup; duplicate response is A019, not a recovery UUID contract |
| lost PATCH response | unsafe | bot retries PATCH and later may clear UUID and CREATE |
| CAS/ETag | absent | exact 2.8.1 source contains no ETag/If-Match/version precondition on user writes |
| late remote write exclusion | unknown/absent | no provider CAS/fencing token; a delayed old HTTP request can apply after a newer generation read |

## Consequences

- CREATE recovery can use deterministic username only as a quarantine/recovery
  lookup: the exact Panel-legal value is
  `te-<sha256(owner_key)[:32]>`; require one result, exact username match, and
  a canonical field read. Mismatch is an exception/quarantine condition, not
  an in-band sentinel value.
  It still cannot prove that a late first POST will not apply after a second
  generation; therefore no blind second POST is permitted.
- A successful CREATE response UUID is not ownership evidence. Before local
  binding or PATCH, GET that UUID and require the exact deterministic username
  plus the complete expected DISABLED snapshot. A foreign/stale UUID, missing
  canonical row, read failure or field mismatch remains unbound and unknown.
- PATCH/action success must mean `mutation → fresh canonical GET → exact
  current-generation comparison`; an HTTP acknowledgement alone is not proof.
- Without remote CAS, an unknown/late mutating request requires identity-level
  quarantine/operator review and blocks concurrent generation mutation.
- The current client’s universal retry and A039 degradation satisfy immediate
  NO-GO conditions from the task prompt.

## Gate 1 boundary

`StrictPanelClient` is deliberately backed only by the controllable fake/test
transport. Its transport exposes `request_once`; CREATE is forced DISABLED,
the receipt is canonically owner/snapshot-verified before UUID binding, PATCH
sends the full exact payload, DELETE is one-shot, and every mutation exception
becomes `remote_outcome_unknown`. It has no A039 field removal and no mutating
retry.

There is no production `RemnaWaveAPI` adapter or callsite switch. A future
shadow deploy is read-only and therefore does not need a mutating adapter. A
later writer-cutover gate must independently prove a production one-shot
transport and owner-matching deterministic lookup against the exact deployed
Panel version. The absence of CAS/ETag remains a quarantine requirement, not
an assumed recovery contract.

Official references:

- <https://github.com/remnawave/backend/tree/2.8.1>
- <https://github.com/remnawave/python-sdk> (official SDK compatibility table)
