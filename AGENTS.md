# Instructions for agents

## Mission

This repository is the separate Python/PPBase rewrite of
`buma_master_hooks`. The frozen source is the PocketBase TypeScript repository
at `../buma_master_hooks`, branch `fix/uv-paani-sup`, commit `530018c`.

## Non-negotiable PocketBase parity rule

The acceptance criterion is behavioral compatibility with the frozen backend,
not merely a successful Python implementation. Existing applications must be
able to switch their PocketBase SDK/API base URL to PPBase without changing
their calls.

For every route, hook, native CRUD flow and checkpoint, preserve and verify as
applicable:

- the exact HTTP method and legacy path;
- request field names, optional/default values and accepted value shapes;
- authentication model, middleware order, permission lookup and 401/403/404
  behavior;
- response status, content type, JSON envelope, field names, messages and
  record serialization;
- hook timing/order and all persisted side effects, relations and statuses;
- transactional atomicity, rollback, locking, idempotence and retry behavior;
- compatibility with PocketBase SDK operations (`authWithPassword`, refresh,
  collection CRUD, filters, expand, files and realtime) used by clients.

A `200` response, a passing build, or rows existing in PostgreSQL is not enough
to claim parity. Compare the frozen TypeScript and, when relevant, PocketBase
behavior before implementation. Any deliberate or unavoidable difference must
be documented in `docs/decisions-and-divergences.md` before closing the
checkpoint, with its client impact and a regression test. Never silently
"improve", rename, normalize or replace a legacy contract.

## Mandatory handoff procedure

Before doing any work:

1. Read `docs/source-baseline.md`.
2. Read `docs/implementation-plan.md`.
3. Read `docs/migration-ppbase-tasklist.md`.
4. Read `docs/progress/ledger.md`.
5. Read `docs/contracts/README.md` and `docs/contracts/source-inventory.json`.
6. Run `git status --short --branch`.
7. Confirm that the source commit still matches the manifest before regenerating
   any inventory.

Resume at the first checkpoint not marked completed. Do not redo a completed
checkpoint unless a new source commit or failed validation invalidates it.

## Scope and safety

- Work only in this repository; never edit the TypeScript source repository.
- Do not import production data or start external integrations without an
  explicit checkpoint requiring it.
- Do not use fallback/demo data to hide missing schema, routes or records.
- Do not normalize legacy routes, responses or field names without documenting
  the divergence first.
- Do not translate all migrations or all domains in one change.
- Keep one domain per checkpoint and include targeted tests in the same change.
- Do not commit, reset, clean or stage unrelated changes.

## Required checkpoint report

At the end of every checkpoint, update `docs/progress/ledger.md` and the
tasklist with:

- exact scope and files changed;
- source files/routes/collections covered;
- commands and test results;
- known divergences and blockers;
- decision to continue or stop.

An implementation checkpoint is not complete when only the build or lint passes.
For database domains, include response, permission, persistence, rollback and
concurrency evidence as appropriate.

The checkpoint report must also state which request/response envelopes, status
codes, hook effects and PocketBase SDK/native CRUD behaviors were compared with
the frozen source. Unverified behavior must remain explicitly open.

This checkpoint update is mandatory for every agent and every work session. If
the agent is blocked or makes no code change, it must still record the blocker,
the commands run and the exact next action in the ledger before stopping. The
conversation alone is never the project handoff record.

## Current state

- C0 source provenance/inventory: complete for static source analysis.
- C1 PPBase foundation: complete for structure/registration tests.
- C2 technical POC: complete (HTTP CRUD, hook marker, transaction and rollback).
- C2 schema reconciliation: corrected PocketBase export received and recorded.
  The official 72-collection base/auth baseline and 42-view migration are
  validated on a fresh PostgreSQL database. The complete 114-definition schema
  check passes. Both auth collections have physical username compatibility
  fields (`users.username` and `agent_users.username`); login semantics remain
  in C3. The technical `foundation_poc` migration is not part of the official
  migration directory. No production data has been imported.
- C3 auth/permissions, C4 idempotency foundation, C5 settings/referentials,
  C6 people/PDV/SIM/agents and C7 article/stock/stockage are closed only for
  the route and runtime evidence recorded in the ledger. Do not infer that
  their unimplemented legacy `/api2/*` siblings or later domains are complete.
- C8 wallet/caisse is closed for its autonomous wallet/session/caisse scope,
  with runtime evidence recorded in the ledger. C9 owns the explicitly
  cross-domain caisse routes for returns, UV return payments, sale payments
  and reports. C9 is now closed for the 29 routes and local runtime evidence
  recorded in the ledger, with D-0009 corrected: legacy sequential writes and partial failure effects
  are preserved, not strengthened. All inventoried active C10 routes are now
  implemented: 18 remaining routes + 10 caisse routes have 94 paired PocketBase
  0.22.27/PPBase comparisons. Real SDK auth/refresh/CRUD/filter/expand/file and
  realtime update (including subscriber permission revocation) pass. Migration
  1789236847 fixes 27 aliases in nine views. Migration 1789236848 restores the
  actual SQLite rowid semantics for users/article and three views, verified
  against PB 0.22.27/SQLite 3.46.0 (12 paired comparisons, 20 concurrent inserts).
  9 migrations, 114 definitions and 42 views pass; 131 unit tests pass in the
  recorded gate. C10 is CLOSED as of 2026-09-28 by explicit user acceptance
  (D-0020), after the user confirmed PPBase was corrected. This is a new final
  closure; the previous unilateral closure under D-0017 remains withdrawn. On
  2026-09-26 the user explicitly accepted PostgreSQL queries entered by the
  administrator in the desktop console (D-0018), and deferred /sql compatibility
  work because no caller was found in the supplied desktop snapshot. Keep /sql
  registered; do not delete or disable it. Do not reopen universal SQLite SQL
  compatibility as a blocker under this accepted scope. The closure does not
  invent a new recorded desktop/SQL runtime test: historical evidence and its
  limits remain identifiable in docs/c10-runtime-report.json. Do not resume C10
  merely because older ledger entries describe it as open/on hold. The strftime
  probe remains synthetic, not a BUMA call. The next open checkpoint is C11.
  Its preparation was authorized separately on 2026-09-26 and
  now includes read-only ZIP/SQLite diagnostics and a synthetic-only isolated
  import rehearsal (9 records, 3 files, rollback/concurrency/auth/file checks).
  See docs/c11-preparation.md and docs/c11-rehearsal-report.json. C11 remains
  OPEN. On 2026-09-28 the user authorized read-only diagnosis of
  ../master-backup.zip and identified its source version as PocketBase 0.22.34.
  The archive and extracted copy were verified unchanged: 114 definitions
  match, 1,188,247 records were inspected, no PostgreSQL import performed.
  See docs/c11-master-diagnostic.md and its JSON evidence. D-0019 affects 5,841
  records with unlimited relations; never flatten arrays. D-0021 also records
  877 duplicate mobile-idempotency groups (2,391 distinct transactions),
  195,795 missing relation targets and 2,303 missing referenced files. Never
  deduplicate, invent records/files, or retarget legacy relations silently.
  Production import/cutover has not started. Preserve original SQLite rowids during
  import; never guess them for existing records or claim production cutover is
  authorized. See docs/c10-runtime-report.json and docs/contracts/rowid.md.
- Real backup diagnosis: completed offline; production import/cutover not started.
