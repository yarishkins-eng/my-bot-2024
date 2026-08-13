# Gate 1 writer inventory and architecture closure

## Reproducible evidence

- Frozen Phase 0 SHA:
  `724d620dd11f0b878d5d802bf8a8330c8d73cd8a75b09abf31bfd374190634c9`.
- Current inventory SHA:
  `8f20df95a7cd084c6e3d841c7058627fa9c744c2c180bc0794d39f2c71878bde`.
- Closure SHA:
  `32920941a3f1787edc1775722c25dea4c6e3e930177c73df0196dbb53be8917f`.
- Baseline/current union SHA:
  `f86b7ead15626e1beb1d8a95918151e6a2ae77a9f0834983c95eeeb50e2bdcf4`.

Current counts: 235 calls, 47 definitions, 551 mutation-field literals,
147 imports, 640 conservatively reachable writer functions/391 entrypoints,
61 startup registrations, zero dynamic raw requests and zero AST errors.

The 44 Phase 0 raw endpoints remain present. Exactly three new raw mutations
exist, all inside the reviewed test/fake-only strict gateway:

- `create_disabled`: one `POST /api/users`, forced `DISABLED`;
- `patch_exact`: one `PATCH /api/users`, exact full payload;
- `delete_once`: one `DELETE /api/users/{panel_uuid}`.

No legacy raw/startup entry disappeared. `writer_union.json` records every
baseline/current addition/removal rather than relying on count equality.
`writer_closure.json` binds every inventory item to one primitive:
`source_mutation`, `overlay`, `project`, `cleanup`, `observation`, or an
explicit `metadata_only` exclusion. Repeated same-line AST literals receive a
stable occurrence ordinal, so entry IDs are unique and exhaustive.

All 61 startup registrations have an explicit semantic classification. The
classifier is fail-closed: no catch-all scanner exclusion exists, and an
unknown startup/call/definition/raw entry raises an error. The 33 metadata-only
startup exclusions were individually reviewed and cannot conceal an access
writer under that label.

## Business mapping

| Surface | Primitive | Gate 1 rule |
|---|---|---|
| payments, purchase, trial, gift, promo, AP term | source mutation | immutable source and command in the same financial transaction |
| block/channel/LIMITED/grace/reset/reversal | overlay | hard deny precedence; LIMITED has explicit clear evidence |
| create/update/enable/device/squad membership | project | generation/lease send-fence, no lock over HTTP, strict canonical verify |
| delete/reset/erasure/merge lifecycle | cleanup | new generation, encrypted restricted target, terminal proof |
| Panel GET/webhook/sync | observation | never owns commercial desired state |
| node lifecycle, labels/order, subscription-page config, Happ encryption | metadata-only | excluded from identity authority; still remains visible in inventory |

## Architecture gates

- Fresh scanner output must match the frozen current hash.
- Any new/missing raw endpoint, startup site, syntax blind spot or dynamic raw
  request fails tests until classified and reviewed.
- Raw access writers cannot be hidden under `metadata_only`.
- The 37-file affected manifest produced 653 passing tests under coverage;
  current runtime evidence is in `runtime_writer_coverage_gate1.json`.
- Runtime line coverage is evidence of execution, not proof against dynamic
  dispatch. Uncovered legacy lines remain forbidden for cutover; Gate 1 does
  not switch or remove them.

Canonical future lock order is
`Payment(if any) → User → Subscription → Identity/Command`. Real PostgreSQL
tests acquire that order concurrently and preserve immutable AP terms without
deadlock. No ordinary row lock spans Panel HTTP.
