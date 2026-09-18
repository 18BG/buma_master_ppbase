# Source baseline

## Frozen reference

- repository: `/Users/omarsisss/Documents/wproject/protech/buma_master_hooks`
- branch: `fix/uv-paani-sup`
- commit: `530018cd20c2acbe88f38b3639e500b5271e2eb2`
- commit subject: `fix: make transaction creation idempotent`
- PocketBase migration directory: `migrationsDir/`
- migration file count at baseline: 326
- static inventory: 113 route registrations, 9 hook registrations, 62 collection references and 51 transaction/SQL sites

## Corrected schema export received

- file: `/Users/omarsisss/Downloads/buma-master-pb_schema-corrected.json`
- collections: 114 (70 base, 2 auth, 42 views)
- SHA-256: `9d99e7b54118c41e0a303dd4805b900c7ee7b526b0cc63c83d044b7305b2313d`
- validation : export JSON lisible et structurellement cohérent (IDs/noms
  uniques, relations vers des collections existantes) ;
- rapport de réconciliation : `docs/contracts/schema-migration-reconciliation.json` ;
- statut manifeste : export corrigé analysé ; baseline officielle `base`/`auth`
  et migration des 42 vues générées depuis cet export puis validées sur une
  base PostgreSQL vide.

This corrected export is the authoritative final shape for the PPBase baseline.
Historical JS migrations remain evidence for evolution and missing fields, but
are not executed directly.

## Important scope

This baseline includes the idempotency changes associated with
`system_id`/`mobile_message_id`. The source repository remains read-only from
this project.

The 326 JavaScript migrations are historical PocketBase migrations. They are
not executed directly by PPBase. The final PostgreSQL baseline will be
generated only after the source schema is reconciled with the live PocketBase
export.

## Missing external artifacts

- Dashboard snapshot with rules, indexes, relations and view definitions;
- production/staging backup (`data.db`, storage and version metadata);
- non-sensitive authentication fixtures;
- deployment and PostgreSQL target decision.
