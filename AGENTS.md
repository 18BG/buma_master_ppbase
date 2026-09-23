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
  are preserved, not strengthened. C10 is in progress: SQL, `/api2/stockage`, and native CRUD hooks are validated; remaining `/api2/*`, master legacy, and middleware routes stay open.
- Production backup/import: not started.
