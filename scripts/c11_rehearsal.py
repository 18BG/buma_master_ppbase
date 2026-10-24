"""Complete synthetic C11 rehearsal in an owned, disposable Docker PostgreSQL.

No --db or --data argument exists: this cannot target a supplied backup or
server. Run from the BUMA root; the report (and --private-logs) must be new::

    python -m scripts.c11_rehearsal --pocketbase /path/to/pocketbase-0.22.34 \
        --schema /path/to/buma-master-pb_schema-corrected.json \
        --report /new/report.json [--profile real_shape|default] \
        [--private-logs /new/dir] [--container-tag NAME]

The PocketBase version is the one the binary reports
(``scripts.c11_fixture.pocketbase_version``); the fixture builder must accept
it and ``scripts.c11_import`` must have validated it, else nothing runs.
Profile ``real_shape`` (the default) carries the shapes of the real 0.22.34
backup; ``default`` is the historical 9-record input. Scenario, synthetic
data only:

1. PocketBase builds the fixture itself (``scripts.c11_fixture``).
2. PocketBase serves it: tokens are issued over its HTTP API before the import
   (admin, users, agent_users), then the reference probes run (sessions and
   refresh, logins, lists, filters and expand on the multiple relations,
   orphans, custom IDs, undeclared selects, JSON null, user dates, files,
   refusals) and PocketBase stops cleanly; its realtime probe (a write) runs
   on a copy, so the snapshot imported is the one the probes read.
3. Read-only diagnosis of the stopped snapshot: ready, preserved anomalies
   counted against the fixture documentation, negative cases.
4. Disposable PostgreSQL 16: migrations, schema and relation columns checked;
   an injected record failure, an injected accounts-stage failure after the
   complete record load and an accounts precondition refusal, each leaving
   exactly the empty migrated state; two concurrent imports, of which exactly
   one commits the complete ``import_snapshot`` (records, accounts, settings).
5. Independent checks in PostgreSQL (rowids, values, arrays, orphans, dates,
   superusers and token secrets), schema check again.
6. An isolated PPBase server (BUMA hooks, jobs disabled) on the PPBase this
   process loads (the caller's PYTHONPATH, read back from the live server
   process): the same probes with the tokens PocketBase issued before the
   import, then its realtime probe. Answers are compared one by one with
   PocketBase's (status, body with JSON number literals, token shape, file
   bytes and headers): a difference fails unless it is a declared probe
   divergence (KNOWN_DIVERGENCES, status pair) or matches one narrow, cited
   class (DIVERGENCE_CLASSES) that exists on any PPBase database.
7. Source unchanged, only the expected writes, schema checked after the HTTP
   probes, every process stopped, container and files removed.

The public report holds statuses, booleans, counts and synthetic labels; no
token, password hash, tokenKey, secret or email.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import inspect
import json
import os
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from scripts import c11_fixture as fixture
from scripts.c11_auth_runtime import (
    PocketBaseServer,
    RealtimeClient,
    claims_shape,
    free_port,
    fresh_client,
    pocketbase_tokens,
    ppbase_server_environment,
    stop,
    tamper,
    wait_health,
)
from scripts.c11_import import (
    UNBOUNDED_RELATION_ARRAYS,
    VALIDATED_SOURCE_VERSIONS,
    ImportBlocked,
    date_value,
    diagnose,
    digest,
    import_snapshot,
    introspect_target,
    load_schema,
    quote,
    snapshot,
    source_multiple,
    source_version_status,
    storage_inventory,
    stored_references,
    write_json_new,
)

PROJECT = Path(__file__).resolve().parents[1]
DATABASE = "buma_c11_rehearsal"
PROFILES = fixture.PROFILES
DEFAULT_PROFILE = "real_shape"
ALL = frozenset(PROFILES)
REAL = frozenset({"real_shape"})
USER, DENIED, NO_EMAIL, NO_EMAIL_2 = (
    "c11user00000001",
    "c11user00000002",
    "c11user00000003",
    "c11user00000004",
)
AGENT, AGENT_NO_EMAIL = "c11agentuser001", "c11agentuser002"
SUPPLIER = "c11supp00000001"
SUPPLIER_PATH = f"/api/collections/buma_supplier/records/{SUPPLIER}"
REALTIME_TOPIC = f"buma_supplier/{SUPPLIER}"
USERS_LOGIN = "/api/collections/users/auth-with-password"
AGENTS_LOGIN = "/api/collections/agent_users/auth-with-password"
ADMINS_LOGIN = "/api/admins/auth-with-password"
USERS_REFRESH = "/api/collections/users/auth-refresh"
AGENTS_REFRESH = "/api/collections/agent_users/auth-refresh"
WRONG_PASSWORD = "C11-wrong-password"
THUMB_SUFFIX = ":thumb_100x100"
AUTH_ARCHIVE = "_buma_c11_auth_archive"

# Sessions opened on PocketBase before the import: label, collection, record
# id, identity used (username or email, from scripts.c11_fixture.ACCOUNTS).
SESSIONS = (
    ("users", "users", USER, "username", ALL),  # role isAdmin
    ("denied", "users", DENIED, "username", ALL),  # no role
    ("agent", "agent_users", AGENT, "email", ALL),
    ("users_no_email", "users", NO_EMAIL, "username", REAL),
    ("agent_no_email", "agent_users", AGENT_NO_EMAIL, "username", REAL),
)


def account(record: str) -> tuple[str, str, str]:
    """(username, email, password) of a synthetic fixture account."""
    for rows in fixture.ACCOUNTS.values():
        for _collection, identifier, username, email, password in rows:
            if identifier == record:
                return username, email, password
    raise KeyError(record)


def admin_account() -> tuple[str, str]:
    ((_c, _id, _u, email, password),) = (
        row for row in fixture.ACCOUNTS["real_shape"] if row[0] == "_admins"
    )
    return email, password


@dataclass(frozen=True)
class Probe:
    """One HTTP call made identically on PocketBase and on PPBase."""

    label: str
    method: str
    path: str
    credential: str | None = None
    params: dict | None = None
    profiles: frozenset = ALL
    expected: int | None = None  # PocketBase status, also PPBase's unless a known divergence


def records(collection: str, record: str = "") -> str:
    return f"/api/collections/{collection}/records" + (f"/{record}" if record else "")


def real(label, path, credential="admin", params=None, *, expected=200, method="GET") -> Probe:
    """A probe of the real_shape profile, the only one with a PocketBase admin."""
    return Probe(label, method, path, credential, params, REAL, expected)


def listing(label, collection, credential, **params) -> Probe:
    return real(label, records(collection), credential, {"sort": "id", **params})


CUSTOMERS = ("c11cust00000001", "c11cust00000002", "c11cust00000003")
# Filters on the multiple relations and the IDs PocketBase 0.22.34 returns. On
# the field itself "=" and "?=" compare the stored JSON text (v0.22.34
# resolvers/record_field_resolve_runner.go:454-462); ":each", ".id" and "~"
# reach the elements (:419-450, :657-671); without "?" a nested condition must
# hold for every element (multi-match: :679-681, tools/search/filter.go:184-210).
RELATION_FILTERS = (
    ("customers_pdv_equals_any", "buma_customer", "admin", 'pdv ?= "c11pdv000000001"', []),
    ("customers_pdv_equals", "buma_customer", "admin", 'pdv = "c11pdv000000001"', []),
    ("customers_pdv_like", "buma_customer", "admin", 'pdv ~ "c11pdv000000001"', CUSTOMERS[:2]),
    (
        "customers_pdv_each_any",
        "buma_customer",
        "admin",
        'pdv:each ?= "c11pdv000000001"',
        CUSTOMERS[:2],
    ),
    (
        "customers_pdv_id_any",
        "buma_customer",
        "admin",
        'pdv.id ?= "c11pdv000000001"',
        CUSTOMERS[:2],
    ),
    ("customers_pdv_length_2", "buma_customer", "admin", "pdv:length = 2", CUSTOMERS[:1]),
    ("customers_pdv_length_0", "buma_customer", "admin", "pdv:length = 0", CUSTOMERS[2:]),
    ("customers_pdv_name_any", "buma_customer", "admin", 'pdv.name ?~ "two"', CUSTOMERS[:1]),
    ("customers_pdv_name_every", "buma_customer", "admin", 'pdv.name ~ "two"', []),
    (
        "tickets_tag_each_any",
        "buma_pilotage_ticket",
        "users",
        'tag:each ?= "c11tag000000002"',
        ["c11ticket000001"],
    ),
    # The joined element: "~" finds an ID inside it, no element equals one.
    (
        "wallets_managers_like",
        "buma_wallet",
        "admin",
        'managers ~ "c11user00000002"',
        ["c11wallet000001"],
    ),
    ("wallets_managers_each_any", "buma_wallet", "admin", 'managers:each ?= "c11user00000001"', []),
)
ACCESS = records("buma_caisse_access", "c11access000001")
PROBES = (
    # Sessions issued by PocketBase before the import, refreshed.
    Probe("users_refresh", "POST", USERS_REFRESH, "users", expected=200),
    Probe("denied_refresh", "POST", USERS_REFRESH, "denied", expected=200),
    Probe("agent_refresh", "POST", AGENTS_REFRESH, "agent", expected=200),
    real("users_no_email_refresh", USERS_REFRESH, "users_no_email", method="POST"),
    real("agent_no_email_refresh", AGENTS_REFRESH, "agent_no_email", method="POST"),
    real("admin_refresh", "/api/admins/auth-refresh", method="POST"),
    real("admin_lists_admins", "/api/admins"),
    # Reads under those sessions.
    Probe("users_reads_admin_role_caisse_access", "GET", ACCESS, "users", expected=200),
    Probe("users_reads_protected_supplier", "GET", SUPPLIER_PATH, "users", expected=200),
    real("admin_reads_protected_supplier", SUPPLIER_PATH),
    Probe("agent_reads_own_record", "GET", records("agent_users", AGENT), "agent", expected=200),
    real("agent_no_email_reads_own", records("agent_users", AGENT_NO_EMAIL), "agent_no_email"),
    # Expected refusals (None: the status is only compared with PocketBase's).
    Probe("denied_reads_admin_role_caisse_access", "GET", ACCESS, "denied", expected=404),
    Probe("anonymous_protected_supplier", "GET", SUPPLIER_PATH, expected=404),
    Probe("anonymous_users_refresh", "POST", USERS_REFRESH, expected=401),
    Probe("tampered_users_refresh", "POST", USERS_REFRESH, "users_tampered", expected=401),
    Probe("tampered_users_protected_supplier", "GET", SUPPLIER_PATH, "users_tampered"),
    real("tampered_admin_lists_admins", "/api/admins", "admin_tampered", expected=401),
    Probe("users_token_on_admin_route", "GET", "/api/admins", "users", expected=401),
    Probe("users_token_on_agent_refresh", "POST", AGENTS_REFRESH, "users"),
    # Native reads of the historical default records.
    Probe(
        "anonymous_public_article",
        "GET",
        records("buma_article", "c11art000000001"),
        expected=200,
    ),
    Probe(
        "users_filtered_transactions_expand",
        "GET",
        records("buma_transaction"),
        "users",
        {"filter": 'article="c11art000000001"', "expand": "article,author"},
        expected=200,
    ),
    Probe("users_list_users", "GET", records("users"), "users", {"sort": "id"}, expected=200),
    Probe(
        "ressources_file_lists",
        "GET",
        records("buma_ressources"),
        "users",
        {"sort": "id"},
        expected=200,
    ),
    real("admin_list_users", records("users"), params={"sort": "id"}),
    # Multiple relations (D-0019): stored order and expand, then RELATION_FILTERS.
    # The admin reads past the role rules of these collections (see the rule
    # probes below).
    listing("customers_expand_pdv", "buma_customer", "admin", expand="pdv"),
    listing("tickets_expand_tag", "buma_pilotage_ticket", "users", expand="tag"),
    listing("wallets_expand_managers", "buma_wallet", "admin", expand="managers"),
    *(
        listing(label, collection, credential, filter=expression)
        for label, collection, credential, expression, _ids in RELATION_FILTERS
    ),
    # Orphans, custom IDs, undeclared values, JSON null, user dates.
    listing("sales_expand_orphan_customer", "buma_sale", "users", expand="customer"),
    listing("sim_balance_orphan_author", "buma_sim_balance_activity", "users", expand="author"),
    listing("payments_expand_custom_id", "buma_payment", "users", expand="method,sale"),
    listing("return_duplicates", "buma_transaction", "users", filter='type = "RETURN"'),
    listing(
        "tickets_by_user_date",
        "buma_pilotage_ticket",
        "users",
        filter='delais >= "2025-03-01 07:00:00.000Z"',
    ),
    real(
        "article_orphan_category",
        records("buma_article", "c11art000000002"),
        "users",
        {"expand": "categorie"},
    ),
    real(
        "customer_orphan_type",
        records("buma_customer", "c11cust00000001"),
        params={"expand": "type,pdv"},
    ),
    real("custom_id_apps_role", records("buma_apps_roles", "synthetic"), "users"),
    real("custom_id_payment_method", records("buma_payment_method", "C11_PAY_METHOD1"), None),
    real("custom_id_config", records("buma_config", "C11_CONFIG_REC1"), "users"),
    real("undeclared_select_sale", records("buma_sale", "c11sale00000002"), None),
    real("json_null_text_user", records("users", NO_EMAIL), "users"),
    # The role rules of these collections under record sessions; the joined
    # managers element under "~" (wallet listRule) and "?=" (viewRule).
    listing("users_lists_customers_by_role", "buma_customer", "users"),
    listing("denied_lists_wallets", "buma_wallet", "denied"),
    Probe(
        "denied_views_joined_wallet",
        "GET",
        records("buma_wallet", "c11wallet000001"),
        "denied",
        profiles=REAL,
        expected=404,
    ),
)

# Logins with the original passwords (and refusals), same calls on both servers.
LOGINS = (
    ("users_by_username", USERS_LOGIN, USER, "username", None, ALL, 200),
    ("agent_by_email", AGENTS_LOGIN, AGENT, "email", None, ALL, 200),
    ("agent_by_username", AGENTS_LOGIN, AGENT, "username", None, ALL, 200),
    ("users_no_email_by_username", USERS_LOGIN, NO_EMAIL, "username", None, REAL, 200),
    ("users_no_email_2_by_username", USERS_LOGIN, NO_EMAIL_2, "username", None, REAL, 200),
    ("agent_no_email_by_username", AGENTS_LOGIN, AGENT_NO_EMAIL, "username", None, REAL, 200),
    ("admin_by_email", ADMINS_LOGIN, "_admin", "email", None, REAL, 200),
    ("users_wrong_password", USERS_LOGIN, USER, "username", WRONG_PASSWORD, ALL, 400),
    ("agent_wrong_password", AGENTS_LOGIN, AGENT, "email", WRONG_PASSWORD, ALL, 400),
    ("admin_wrong_password", ADMINS_LOGIN, "_admin", "email", WRONG_PASSWORD, REAL, 400),
)

# Differences between PocketBase 0.22.34 and PPBase answers that exist on any
# BUMA/PPBase database, independently of the import: accepted only with their
# declared pair of statuses; any other difference fails the rehearsal.
UNDECLARED_PERMISSION_KEY = (
    "pre-existing BUMA C3 defect, independent of the import: app/auth/context.py:45-94 "
    "(merge_live_auth_context) gives a False default only to the permission keys that "
    "buma_apps_roles.info.actions declares and to role.*/user.*; an undeclared key of the "
    "rule (customer.read, wallet.read, wallet.list) is bound as '' and PostgreSQL refuses "
    "'' = TRUE (asyncpg DataError, HTTP 500). PocketBase v0.22.34 compares an empty "
    "operand without error (tools/search/filter.go:290-330, resolveEqualExpr)"
)
KNOWN_DIVERGENCES: dict[str, dict] = {
    "users_lists_customers_by_role": {
        "pocketbase": 200,
        "ppbase": 500,
        "cause": UNDECLARED_PERMISSION_KEY,
    },
    "denied_lists_wallets": {"pocketbase": 200, "ppbase": 500, "cause": UNDECLARED_PERMISSION_KEY},
    "denied_views_joined_wallet": {
        "pocketbase": 404,
        "ppbase": 500,
        "cause": UNDECLARED_PERMISSION_KEY,
    },
}
KNOWN_DIVERGENCES["customers_pdv_name_every"] = {
    "pocketbase": 200,
    "ppbase": 200,
    "paths": ("/items", "/totalItems", "/totalPages"),
    "cause": (
        "PPBase defect on the D-0019 arrays, independent of the import: a nested field of a "
        "multiple relation without '?' matches when any element matches "
        "(ppbase/services/filter_parser.py:1268-1282, EXISTS only); PocketBase v0.22.34 "
        "requires every element (multi-match, resolvers/record_field_resolve_runner.go:"
        "679-681, tools/search/filter.go:184-210)"
    ),
}
# Answer differences that exist on any PPBase database, independently of the
# import, by class: each difference path must match one narrow rule of
# divergence_class(); anything else fails the rehearsal. PPBase lines cited
# here and above: commit 526397a (branch fix/d0019-relation-maxselect).
DIVERGENCE_CLASSES = {
    "error_envelope_code_vs_status": (
        "documented (docs/contracts/c11-auth-import.md section 5): PocketBase v0.22.34 "
        "{code, message, data} (apis/api_error.go:12-15), PPBase {status, message, data}"
    ),
    "auth_error_message_wording": (
        "documented (docs/contracts/c11-auth-import.md section 5): 401/403 messages, same status"
    ),
    "failed_login_identity_detail": (
        "PPBase adds data.identity validation_invalid_credentials to the 400 of a failed "
        "login (ppbase/api/record_auth.py:352-357, ppbase/api/admins.py:172); PocketBase "
        "v0.22.34 answers data {}"
    ),
    "empty_date_served_null": (
        'PocketBase v0.22.34 serves an empty date as "" (tools/types/datetime.go:44-53); '
        "PPBase stores it as SQL NULL and serves null (ppbase/models/record.py:129-133)"
    ),
    "auth_email_hidden_despite_manage_rule": (
        "PocketBase v0.22.34 shows auth emails to admins, the owner and requesters "
        "satisfying the collection manageRule (apis/record_helpers.go:216-270; "
        "users.manageRule is @request.auth.role.permission.isAdmin = true); PPBase only "
        "to superusers and the owner (ppbase/models/record.py:80-101)"
    ),
    "empty_expand_object": (
        "PocketBase v0.22.34 omits expand when nothing was expanded "
        "(models/record.go:556-559); PPBase serves expand {} "
        "(ppbase/services/expand_service.py:240-242)"
    ),
    "empty_list_total_pages": (
        "PocketBase v0.22.34 totalPages = ceil(0 / perPage) = 0 "
        "(tools/search/provider.go:254); PPBase max(1, ...) (ppbase/models/record.py:196-201)"
    ),
    "default_thumb_size_serves_original": (
        "PocketBase v0.22.34 always serves the 100x100 thumbnail (apis/file.go:27, :163-164; "
        "here the one it stored, thumbs_<name>/100x100_<name>); PPBase only the sizes the "
        "field declares (ppbase/api/files.py:280-283; the BUMA file fields declare none) "
        "and serves the original bytes"
    ),
    "file_content_disposition_missing": (
        "PocketBase v0.22.34 sends Content-Disposition inline/attachment with the file name "
        "(tools/filesystem/filesystem.go:393-418); PPBase none on its default storage "
        "stream (ppbase/api/files.py:1056-1110)"
    ),
    "json_error_content_type_charset": (
        "PocketBase answers errors as application/json; charset=UTF-8, PPBase application/json"
    ),
}
# (PocketBase type, PPBase type) -> (PocketBase claims, PPBase claims). Tokens
# PocketBase 0.22.34 issues: tokens/record.go:13-27 (authRecord) and
# tokens/admin.go:11-17 (admin). Tokens PPBase issues on login and refresh:
# ppbase/services/auth_service.py:410 (iat), :440-443 (admin), :476-483 (auth,
# refreshable); pre-existing, documented in docs/contracts/c11-auth-import.md
# ("Type des nouveaux jetons"). Tokens issued before the import stay accepted.
TOKEN_SHAPES = {
    ("authRecord", "auth"): (
        ["collectionId", "exp", "id", "type"],
        ["collectionId", "exp", "iat", "id", "refreshable", "type"],
    ),
    ("admin", "admin"): (["exp", "id", "type"], ["exp", "iat", "id", "type"]),
}


# ---------------------------------------------------------------------------
# Fixture facts and generic helpers.
# ---------------------------------------------------------------------------


def command(*args, **kwargs):
    return subprocess.check_output(args, text=True, stderr=subprocess.STDOUT, **kwargs).strip()


def typed_json(raw: bytes):
    """JSON with number literals kept: 0 and 0.0 are different client answers."""
    return json.loads(
        raw,
        parse_int=lambda literal: ("number", literal),
        parse_float=lambda literal: ("number", literal),
        parse_constant=lambda literal: ("number", literal),
    )


def differences(left, right, path="") -> list[str]:
    if isinstance(left, dict) and isinstance(right, dict):
        out = []
        for key in sorted(set(left) | set(right), key=str):
            if key not in left or key not in right:
                out.append(f"{path}/{key}: {'pocketbase' if key in left else 'ppbase'} only")
            else:
                out.extend(differences(left[key], right[key], f"{path}/{key}"))
        return out
    if isinstance(left, list) and isinstance(right, list):
        if len(left) != len(right):
            return [f"{path}: {len(left)} != {len(right)} items"]
        out = []
        for index, (a, b) in enumerate(zip(left, right, strict=True)):
            out.extend(differences(a, b, f"{path}[{index}]"))
        return out
    return [] if left == right else [path]


def file_references(data: Path, definitions: list[dict]) -> list[dict]:
    """Every file reference of the stopped snapshot, in schema and rowid order."""
    references = []
    with snapshot(data) as db:
        for c in definitions:
            if c["type"] == "view":
                continue
            for f in c["schema"]:
                if f["type"] != "file":
                    continue
                multiple = source_multiple(f)
                rows = db.execute(
                    f"SELECT id, {quote(f['name'])} FROM {quote(c['name'])} ORDER BY _rowid_"
                )
                for record, value in rows:
                    for index, name in enumerate(stored_references(value, multiple)):
                        key = f"{c['id']}/{record}/{name}"
                        references.append(
                            {
                                "label": f"{c['name']}.{f['name']}:{record}:{index}",
                                "key": key,
                                "present": (data / "storage" / key).is_file(),
                                "image": name.lower().endswith(".png"),
                            }
                        )
    return references


def unreferenced_objects(data: Path, references: list[dict]) -> list[dict]:
    """Storage objects no record field names (.attrs sidecars, deleted records)."""
    referenced = {r["key"] for r in references}
    objects = []
    for key in storage_inventory(data / "storage"):
        parts = key.split("/")
        if len(parts) != 3 or key in referenced:
            continue  # thumbnails live one level deeper
        kind = "attrs_sidecar" if parts[2].endswith(".attrs") else "other"
        objects.append({"label": f"unreferenced:{parts[1]}:{kind}:{len(objects)}", "key": key})
    return objects


# ---------------------------------------------------------------------------
# HTTP probes, identical on both servers.
# ---------------------------------------------------------------------------


def credentials_for(tokens: dict) -> dict:
    found = {label: item["token"] for label, item in tokens.items()}
    found["users_tampered"] = tamper(found["users"])
    if "admin" in found:
        found["admin_tampered"] = tamper(found["admin"])
    return found


def answer(response) -> dict:
    """Status, typed body with any token replaced by its shape (never its value)."""
    try:
        body = typed_json(response.content)
    except ValueError:
        body = {"non_json_bytes_sha256": hashlib.sha256(response.content).hexdigest()}
    token = None
    if isinstance(body, dict) and isinstance(body.get("token"), str):
        try:
            token = claims_shape(body["token"])
        except Exception:  # noqa: BLE001 - a malformed token stays a visible difference
            token = {"malformed": True}
        body = {**body, "token": "<token>"}
    return {
        "status": response.status_code,
        "body": body,
        "token": token,
        # Plain JSON for the absolute checks; memory only, never reported.
        "json": response.json() if response.content.startswith(b"{") else None,
    }


async def json_probes(base: str, credentials: dict, profile: str) -> dict:
    results = {}
    async with fresh_client(base) as client:
        for probe in PROBES:
            if profile not in probe.profiles:
                continue
            headers = {}
            if probe.credential:
                headers["Authorization"] = credentials[probe.credential]
            response = await client.request(
                probe.method, probe.path, params=probe.params, headers=headers
            )
            results[probe.label] = answer(response)
    return results


async def login_probes(base: str, profile: str) -> dict:
    results = {}
    admin_email, admin_password = admin_account()
    async with fresh_client(base) as client:
        for label, path, record, kind, password, profiles, _status in LOGINS:
            if profile not in profiles:
                continue
            if record == "_admin":
                identity, original = admin_email, admin_password
            else:
                username, email, original = account(record)
                identity = username if kind == "username" else email
            response = await client.post(
                path, json={"identity": identity, "password": password or original}
            )
            results[label] = answer(response)
    return results


async def file_probes(base: str, references: list[dict], unreferenced: list[dict]) -> dict:
    results = {}
    async with fresh_client(base) as client:
        for item in references + unreferenced:
            response = await client.get("/api/files/" + item["key"])
            results[item["label"]] = {
                "status": response.status_code,
                "sha256": hashlib.sha256(response.content).hexdigest(),
                "content": response.content,
                "content_type": response.headers.get("content-type"),
                "content_disposition": response.headers.get("content-disposition"),
            }
            if item.get("present") and item.get("image"):
                thumb = await client.get("/api/files/" + item["key"], params={"thumb": "100x100"})
                results[item["label"] + THUMB_SUFFIX] = {
                    "status": thumb.status_code,
                    "sha256": hashlib.sha256(thumb.content).hexdigest(),
                    "content": thumb.content,
                    "content_type": thumb.headers.get("content-type"),
                    "content_disposition": thumb.headers.get("content-disposition"),
                }
    return results


async def realtime_probe(base: str, credentials: dict) -> dict:
    """A protected record topic: one subscriber with a PocketBase token, one anonymous."""
    writer = credentials.get("admin") or credentials["users"]
    async with RealtimeClient(base) as subscriber, RealtimeClient(base) as anonymous:
        results = {
            "subscribe_with_pocketbase_users_token": await subscriber.subscribe(
                [REALTIME_TOPIC], credentials["users"]
            ),
            "anonymous_subscribe": await anonymous.subscribe([REALTIME_TOPIC]),
        }
        firstname = "C11 realtime update"
        async with fresh_client(base) as client:
            update = await client.patch(
                SUPPLIER_PATH, headers={"Authorization": writer}, json={"firstname": firstname}
            )
        results["update_by_" + ("admin" if "admin" in credentials else "users")] = (
            update.status_code
        )
        results["update_kept_value"] = update.json().get("firstname") == firstname
        results["event_delivered_to_token_subscriber"] = await subscriber.wait_for(
            REALTIME_TOPIC, "update", SUPPLIER
        )
        await asyncio.sleep(1)
        results["event_delivered_to_anonymous_subscriber"] = anonymous.received(REALTIME_TOPIC)
    body = typed_json(update.content)
    body.pop("updated", None)  # the time of each server's own update
    return {"results": results, "update_body": body}


async def read_probes(base: str, credentials: dict, profile: str, files: dict) -> dict:
    """Every probe that does not write: sessions, reads, logins and files."""
    return {
        "json": await json_probes(base, credentials, profile),
        "logins": await login_probes(base, profile),
        "files": await file_probes(base, files["references"], files["unreferenced"]),
    }


def _path_parts(path: str) -> list:
    """'/items[2]/expand: ppbase only' -> ['items', 2, 'expand']."""
    parts = []
    for piece in path.split(":", 1)[0].strip("/").split("/"):
        name, _bracket, rest = piece.partition("[")
        if name:
            parts.append(name)
        for index in filter(None, rest.replace("]", "").split("[")):
            parts.append(int(index))
    return parts


def _at(body, parts):
    for part in parts:
        if isinstance(part, int):
            body = body[part] if isinstance(body, list) and part < len(body) else None
        else:
            body = body.get(part) if isinstance(body, dict) else None
    return body


def _error_envelope(body) -> bool:
    return isinstance(body, dict) and set(body) in (
        {"code", "message", "data"},
        {"status", "message", "data"},
    )


NUMBER_0, NUMBER_1 = ("number", "0"), ("number", "1")


def divergence_class(path, reference, mine, *, credential, field_types) -> str | None:
    """The DIVERGENCE_CLASSES rule a difference path satisfies, or None."""
    parts = _path_parts(path)
    if _error_envelope(reference) and _error_envelope(mine):
        same_status = reference.get("code") == mine.get("status")
        if path in ("/code: pocketbase only", "/status: ppbase only") and same_status:
            return "error_envelope_code_vs_status"
        if (
            path == "/message"
            and same_status
            and reference["code"]
            in (
                ("number", "401"),
                ("number", "403"),
            )
        ):
            return "auth_error_message_wording"
        if (
            path == "/data/identity: ppbase only"
            and reference["code"] == ("number", "400")
            and reference["data"] == {}
        ):
            return "failed_login_identity_detail"
        return None
    if not parts:
        return None
    parent_ref, parent_mine = _at(reference, parts[:-1]), _at(mine, parts[:-1])
    key = parts[-1]
    if not isinstance(parent_ref, dict) or not isinstance(parent_mine, dict):
        return None
    collection = parent_ref.get("collectionName")
    if (
        path.endswith(f"/{key}")
        and isinstance(key, str)
        and parent_ref.get(key) == ""
        and key in parent_mine
        and parent_mine[key] is None
        and field_types.get((collection, key)) == "date"
    ):
        return "empty_date_served_null"
    if (
        path.endswith("/email: pocketbase only")
        and collection == "users"
        and credential == "users"  # role isAdmin: users.manageRule holds
    ):
        return "auth_email_hidden_despite_manage_rule"
    if path.endswith("/expand: ppbase only") and parent_mine.get("expand") == {}:
        return "empty_expand_object"
    if (
        path == "/totalPages"
        and (reference.get("totalItems"), reference["totalPages"]) == (NUMBER_0, NUMBER_0)
        and (mine.get("totalItems"), mine["totalPages"]) == (NUMBER_0, NUMBER_1)
    ):
        return "empty_list_total_pages"
    return None


def png_size(content: bytes) -> tuple[int, int] | None:
    if content[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    return int.from_bytes(content[16:20], "big"), int.from_bytes(content[20:24], "big")


def file_divergences(label: str, reference: dict, mine: dict, original: dict | None) -> tuple:
    """(classes, unexplained) for one file probe: status, bytes and headers.

    ``original`` is PPBase's answer for the original file of a thumbnail probe.
    """
    classes, unexplained = [], []
    if reference["status"] != mine["status"]:
        unexplained.append(f"status {reference['status']} != {mine['status']}")
    elif reference["sha256"] != mine["sha256"]:
        if original is not None and reference["status"] == 200:
            if (
                png_size(reference["content"]) == (100, 100)
                and mine["sha256"] == original["sha256"]
                and original["status"] == 200
            ):
                classes.append("default_thumb_size_serves_original")
            else:
                unexplained.append("thumbnail")
        else:
            try:
                ref, own = json.loads(reference["content"]), json.loads(mine["content"])
            except ValueError:
                ref = own = None
            if (
                _error_envelope(ref)
                and _error_envelope(own)
                and (ref.get("code"), ref["message"], ref["data"])
                == (own.get("status"), own["message"], own["data"])
            ):
                classes.append("error_envelope_code_vs_status")
            else:
                unexplained.append("bytes")
    if reference["content_disposition"] != mine["content_disposition"]:
        if reference["content_disposition"] and mine["content_disposition"] is None:
            classes.append("file_content_disposition_missing")
        else:
            unexplained.append("content_disposition")
    if reference["content_type"] != mine["content_type"]:
        if (reference["content_type"], mine["content_type"]) == (
            "application/json; charset=UTF-8",
            "application/json",
        ):
            classes.append("json_error_content_type_charset")
        else:
            unexplained.append("content_type")
    return classes, unexplained


def compare_servers(pocketbase: dict, ppbase: dict, profile: str, field_types: dict) -> dict:
    """Answer by answer; the public comparison holds labels, statuses and JSON paths.

    A probe-declared divergence (KNOWN_DIVERGENCES) is accepted only with its
    pair of statuses and, when given, within its JSON paths; every other
    difference must satisfy a DIVERGENCE_CLASSES rule; a token is compared by
    its algorithm, claim names and type (TOKEN_SHAPES).
    """
    credentials = {probe.label: probe.credential for probe in PROBES}
    compared, unexpected, declared_found, classes, tokens = {}, [], {}, {}, {}

    def classified(name: str, label: str) -> None:
        classes.setdefault(name, set()).add(label)

    for group in ("json", "logins"):
        for label, reference in pocketbase[group].items():
            mine = ppbase[group][label]
            found = differences(reference["body"], mine["body"])
            pair = (reference["status"], mine["status"])
            declared = KNOWN_DIVERGENCES.get(label)
            unexplained = []
            if declared:
                within = all(
                    any(path.startswith(prefix) for prefix in declared.get("paths", ("",)))
                    for path in found
                )
                if pair == (declared["pocketbase"], declared["ppbase"]) and found and within:
                    declared_found[label] = {
                        "pocketbase_status": pair[0],
                        "ppbase_status": pair[1],
                        "paths": found if "paths" in declared else len(found),
                        "cause": declared["cause"],
                    }
                elif pair[0] == pair[1] and not found:
                    declared_found[label] = {"resolved": True, "cause": declared["cause"]}
                else:
                    unexplained = [f"status {pair[0]} != {pair[1]}", *found]
            else:
                if pair[0] != pair[1]:
                    unexplained.append(f"status {pair[0]} != {pair[1]}")
                for path in found:
                    name = divergence_class(
                        path,
                        reference["body"],
                        mine["body"],
                        credential=credentials.get(label),
                        field_types=field_types,
                    )
                    if name:
                        classified(name, label)
                    else:
                        unexplained.append(path)
            if unexplained:
                unexpected.append({"probe": label, "differences": unexplained})
            if reference["token"] or mine["token"]:
                tokens[label] = token_comparison(reference["token"], mine["token"])
                if not tokens[label]["expected"]:
                    unexpected.append({"probe": label, "differences": ["token_shape"]})
            compared[label] = {
                "status": mine["status"],
                "same_as_pocketbase": not found and pair[0] == pair[1],
            }
    for label, reference in pocketbase["files"].items():
        original = None
        if label.endswith(THUMB_SUFFIX):
            original = ppbase["files"][label.removesuffix(THUMB_SUFFIX)]
        names, unexplained = file_divergences(label, reference, ppbase["files"][label], original)
        for name in names:
            classified(name, "file:" + label)
        if unexplained:
            unexpected.append({"probe": "file:" + label, "differences": unexplained})
        compared["file:" + label] = {
            "status": ppbase["files"][label]["status"],
            "same_as_pocketbase": not names and not unexplained,
        }
    realtime_found = differences(pocketbase["realtime"]["results"], ppbase["realtime"]["results"])
    realtime_found += differences(
        pocketbase["realtime"]["update_body"], ppbase["realtime"]["update_body"]
    )
    for path in list(realtime_found):
        name = divergence_class(
            path,
            pocketbase["realtime"]["update_body"],
            ppbase["realtime"]["update_body"],
            credential=None,
            field_types=field_types,
        )
        if name:
            classified(name, "realtime")
            realtime_found.remove(path)
    compared["realtime"] = {"same_as_pocketbase": not realtime_found}
    if realtime_found:
        unexpected.append({"probe": "realtime", "differences": realtime_found})
    return {
        "profile": profile,
        "probes": len(compared),
        "identical": sum(1 for e in compared.values() if e["same_as_pocketbase"]),
        "declared_divergences": declared_found,
        "divergence_classes": {
            name: {
                "probes": sorted(labels),
                "count": len(labels),
                "cause": DIVERGENCE_CLASSES[name],
            }
            for name, labels in sorted(classes.items())
        },
        "token_shapes": tokens,
        "unexpected_differences": unexpected,
        "results": compared,
    }


def token_comparison(reference: dict | None, mine: dict | None) -> dict:
    """Shapes of the token PocketBase and PPBase return for the same call."""
    pair = (reference or {}).get("type"), (mine or {}).get("type")
    expected = TOKEN_SHAPES.get(pair)
    return {
        "pocketbase": reference,
        "ppbase": mine,
        "expected": bool(expected)
        and reference is not None
        and mine is not None
        and reference["alg"] == mine["alg"]
        and reference["claims"] == expected[0]
        and mine["claims"] == expected[1],
    }


def check_absolute(results: dict, profile: str, data: Path, files: dict, server: str) -> list:
    """Expected answers from the fixture documentation, on one server."""
    checked = []
    json_results, logins = results["json"], results["logins"]
    for probe in PROBES:
        if profile in probe.profiles and probe.expected is not None:
            status = json_results[probe.label]["status"]
            expected = probe.expected
            if server == "ppbase" and probe.label in KNOWN_DIVERGENCES:
                expected = KNOWN_DIVERGENCES[probe.label]["ppbase"]
            assert status == expected, (server, probe.label, status)
    for label, *_rest, profiles, status in LOGINS:
        if profile in profiles:
            assert logins[label]["status"] == status, (server, label, logins[label]["status"])
            if status == 200:
                assert logins[label]["token"]["type"], (server, label)
    checked.append("expected_statuses_and_logins")
    article = json_results["anonymous_public_article"]["json"]
    assert article["sell_price"] == 12.75 and "rowid" not in article, server
    listed = json_results["users_filtered_transactions_expand"]["json"]
    assert set(listed) == {"page", "perPage", "totalItems", "totalPages", "items"}, server
    assert listed["totalItems"] == 1, server
    (item,) = listed["items"]
    assert item["quantity"] == 2.5 and item["expand"]["article"]["id"] == "c11art000000001"
    assert item["expand"]["author"]["id"] == USER, server
    checked.append("native_filtered_list_envelope_and_expand")
    for item in files["references"]:
        got = results["files"][item["label"]]
        if item["present"]:
            source = (data / "storage" / item["key"]).read_bytes()
            assert got["status"] == 200 and got["content"] == source, (server, item["label"])
        else:
            assert got["status"] == 404, (server, item["label"])
    checked.append("present_files_exact_bytes_absent_files_404")
    realtime = results["realtime"]["results"]
    assert realtime["event_delivered_to_token_subscriber"] is True, (server, realtime)
    assert realtime["event_delivered_to_anonymous_subscriber"] is False, (server, realtime)
    checked.append("realtime_pocketbase_token_subscriber_only")
    if profile == "real_shape":
        checked += check_real_shape_answers(json_results, server)
    return checked


def ids(listing: dict) -> list[str]:
    return [item["id"] for item in listing["items"]]


def check_real_shape_answers(results: dict, server: str) -> list:
    shapes = fixture.REAL_SHAPE
    customers = {i["id"]: i for i in results["customers_expand_pdv"]["json"]["items"]}
    for record, expected in shapes["unlimited_relations"]["buma_customer.pdv"].items():
        assert customers[record]["pdv"] == expected, (server, record)
        expanded = [e["id"] for e in customers[record].get("expand", {}).get("pdv", [])]
        assert expanded == expected, (server, record, expanded)  # stored order
    tickets = {i["id"]: i for i in results["tickets_expand_tag"]["json"]["items"]}
    for record, expected in shapes["unlimited_relations"]["buma_pilotage_ticket.tag"].items():
        assert tickets[record]["tag"] == expected, (server, record)
        assert [e["id"] for e in tickets[record].get("expand", {}).get("tag", [])] == expected
    wallets = {i["id"]: i for i in results["wallets_expand_managers"]["json"]["items"]}
    for record, expected in shapes["unlimited_relations"]["buma_wallet.managers"].items():
        assert wallets[record]["managers"] == expected, (server, record)  # never split
        assert not wallets[record].get("expand", {}).get("managers"), (server, record)
    checked = ["multiple_relations_values_and_expand_in_stored_order"]
    for label, _collection, _credential, _expression, expected in RELATION_FILTERS:
        found = ids(results[label]["json"])
        if label in KNOWN_DIVERGENCES and server == "ppbase":
            continue  # its declared PPBase answer is checked by compare_servers
        assert found == list(expected), (server, label, found)  # synthetic IDs only
    checked.append("multiple_relation_filters_as_pocketbase_0_22")
    sales = {i["id"]: i for i in results["sales_expand_orphan_customer"]["json"]["items"]}
    for record, value in shapes["orphan_relations"]["buma_sale.customer"].items():
        assert sales[record]["customer"] == value, (server, record)
        assert not sales[record].get("expand", {}).get("customer"), (server, record)
    (simbal,) = results["sim_balance_orphan_author"]["json"]["items"]
    assert simbal["author"] == "1" and not simbal.get("expand", {}).get("author"), server
    category = results["article_orphan_category"]["json"]
    assert category["categorie"] == "0" and not category.get("expand", {}).get("categorie")
    customer = results["customer_orphan_type"]["json"]
    assert customer["type"] == "PERSONNE" and not customer.get("expand", {}).get("type")
    checked.append("orphans_served_as_stored_without_expand")
    for label in ("custom_id_apps_role", "custom_id_payment_method", "custom_id_config"):
        assert results[label]["json"]["id"] in shapes["custom_record_ids"].values(), label
    (payment,) = results["payments_expand_custom_id"]["json"]["items"]
    assert payment["expand"]["method"]["id"] == "C11_PAY_METHOD1", server
    checked.append("custom_ids_read_and_expanded")
    assert results["undeclared_select_sale"]["json"]["kind"] == "C11_UNDECLARED", server
    assert results["json_null_text_user"]["json"]["more"] is None, server
    checked.append("undeclared_select_and_json_null")
    dates = shapes["user_dates"]
    (ticket_date,) = dates["buma_pilotage_ticket.delais"].values()
    assert tickets["c11ticket000001"]["delais"] == ticket_date, server
    assert simbal["date"] == dates["buma_sim_balance_activity.date"]["c11simbal000001"]
    returns = {i["id"]: i for i in results["return_duplicates"]["json"]["items"]}
    mobile = dates["buma_transaction.mobile_transaction_date"]
    assert {r: returns[r]["mobile_transaction_date"] for r in mobile} == mobile, server
    assert ids(results["tickets_by_user_date"]["json"]) == ["c11ticket000001"], server
    checked.append("user_dates_served_and_filtered")
    ressources = {i["id"]: i for i in results["ressources_file_lists"]["json"]["items"]}
    assert ressources["c11file00000004"]["file"] == [], server
    checked.append("empty_multi_file_list")
    rules = ("users_lists_customers_by_role", "denied_lists_wallets", "denied_views_joined_wallet")
    if server == "pocketbase" or not KNOWN_DIVERGENCES.keys() & set(rules):
        customers = ids(results["users_lists_customers_by_role"]["json"])
        assert customers == sorted(shapes["unlimited_relations"]["buma_customer.pdv"]), server
        assert ids(results["denied_lists_wallets"]["json"]) == ["c11wallet000001"], server
        assert results["denied_views_joined_wallet"]["status"] == 404, server
        checked.append("role_rules_and_the_joined_managers_element")
    return checked


# ---------------------------------------------------------------------------
# PocketBase before the import.
# ---------------------------------------------------------------------------


def token_requests(profile: str):
    records = []
    for label, collection, record, kind, profiles in SESSIONS:
        if profile in profiles:
            username, email, password = account(record)
            records.append((label, collection, username if kind == "username" else email, password))
    return (admin_account() if profile == "real_shape" else None), records


async def pocketbase_phase(binary, data, migrations, root, profile, report) -> dict:
    """PocketBase serves the fixture: sessions and reference answers, then a clean stop.

    The realtime probe writes (one supplier update): PocketBase runs it on a
    copy of the stopped snapshot, so the imported snapshot is exactly the one
    the read probes saw and PPBase starts from the same state.
    """
    definitions = load_schema_cached(report)
    references = file_references(data, definitions)
    files = {"references": references, "unreferenced": unreferenced_objects(data, references)}
    admin, records = token_requests(profile)
    async with PocketBaseServer(binary, data, root=root, migrations=migrations) as server:
        tokens = await pocketbase_tokens(server.base_url, admin=admin, records=records)
        credentials = credentials_for(tokens)
        answers = await read_probes(server.base_url, credentials, profile, files)
        process = server.process
    assert process.poll() is not None
    sidecars = {
        suffix: (data / f"data.db{suffix}").stat().st_size
        for suffix in ("-wal", "-journal", "-shm")
        if (data / f"data.db{suffix}").exists()
    }
    # The importer reads only a closed snapshot: no non-empty WAL or journal.
    assert not sidecars.get("-wal") and not sidecars.get("-journal"), sidecars
    copy = root / "pocketbase-realtime-copy"
    shutil.copytree(data, copy)
    async with PocketBaseServer(binary, copy, root=root, migrations=migrations) as server:
        answers["realtime"] = await realtime_probe(server.base_url, credentials)
    shutil.rmtree(copy)
    report["pocketbase_before_import"] = {
        "sessions_issued": {
            label: claims_shape(item["token"])["type"] for label, item in sorted(tokens.items())
        },
        "token_claims": {
            label: claims_shape(item["token"])["claims"] for label, item in sorted(tokens.items())
        },
        "probes": sum(len(answers[g]) for g in ("json", "logins", "files")) + 1,
        "file_references": {
            "present": sum(1 for r in references if r["present"]),
            "absent": sum(1 for r in references if not r["present"]),
            "unreferenced_objects_probed": len(files["unreferenced"]),
        },
        "stopped_cleanly": True,
        "sqlite_sidecars_after_stop": sidecars,
        "realtime_probe_on_a_copy": True,
    }
    return {"tokens": tokens, "answers": answers, "files": files}


_SCHEMA_CACHE: dict = {}


def load_schema_cached(report: dict) -> list[dict]:
    path = Path(report["schema_path"])
    if path not in _SCHEMA_CACHE:
        _SCHEMA_CACHE[path] = load_schema(path)
    return _SCHEMA_CACHE[path]


# ---------------------------------------------------------------------------
# Read-only diagnosis.
# ---------------------------------------------------------------------------


def expected_anomalies(profile: str) -> tuple[dict, dict]:
    """Preserved anomalies the fixture documents: {(collection, field): references}."""
    if profile != "real_shape":
        return {}, {}
    relations, files = {}, {}
    for path, records in fixture.REAL_SHAPE["orphan_relations"].items():
        collection, name = path.split(".")
        relations[(collection, name)] = len(records)
    for path, records in fixture.REAL_SHAPE["missing_files"].items():
        collection, name = path.split(".")
        files[(collection, name)] = sum(len(names) for names in records.values())
    return relations, files


def check_preserved_anomalies(diagnostic: dict, profile: str) -> dict:
    relations, files = expected_anomalies(profile)
    anomalies = diagnostic["preserved_source_anomalies"]
    found_relations = {
        (i["collection"], i["field"]): i["references"]
        for i in anomalies["dangling_relations"]["fields"]
    }
    found_files = {
        (i["collection"], i["field"]): i["references"] for i in anomalies["missing_files"]["fields"]
    }
    assert found_relations == relations, found_relations
    assert found_files == files, found_files
    assert anomalies["blocking"] is False
    return {
        "dangling_relation_fields": len(found_relations),
        "dangling_references": sum(found_relations.values()),
        "missing_file_fields": len(found_files),
        "missing_file_references": sum(found_files.values()),
        "match_fixture_documentation": True,
        "policy": anomalies["policy"],
    }


def negative_diagnostics(data, root, schema, *, source_version="0.22.27"):
    """Each altered snapshot is synthetic; no fixture silently repairs a source.

    Every case is compared with the diagnosis of the unaltered fixture and
    alters one chosen record, so the contract holds whatever that fixture
    already contains (default or real_shape profile). Orphan relations and
    absent referenced files are preserved anomalies: same blockers and
    readiness as the unaltered fixture, exactly the injected anomaly added,
    nothing invented, removed or redirected. Values that PocketBase 0.22.34
    serves otherwise than PPBase will (jsonb number notation, a single value
    stored as a JSON array, a list with duplicates) are warnings: same
    blockers and readiness, one more affected row for that code and field.
    Every other altered case must add its own blocker (code, and field when
    one is named), a json text that PocketBase serves as a string included.
    Manifest mode is exercised without any storage/ bytes.
    """
    from scripts.c11_import import directory_manifest

    reference = diagnose(data, schema, source_version=source_version)
    article_collection = next(c["id"] for c in load_schema(schema) if c["name"] == "buma_article")

    def first(table, where="1"):
        return f"(SELECT min(rowid) FROM {table} WHERE {where})"

    with snapshot(data) as db:
        # A transaction whose article exists, and an ID that no row uses.
        transaction = db.execute(
            "SELECT min(t.rowid) FROM buma_transaction t JOIN buma_article a ON a.id=t.article"
        ).fetchone()[0]
        absent = next(
            candidate
            for candidate in (f"c11absent{index:06d}" for index in range(1000))
            if not db.execute(
                "SELECT 1 FROM buma_article WHERE id=? UNION ALL "
                "SELECT 1 FROM buma_transaction WHERE article=?",
                (candidate, candidate),
            ).fetchone()
        )
        # A present single file of buma_article.medias (maxSelect 1).
        record, filename = db.execute(
            "SELECT id, medias FROM buma_article WHERE medias<>'' ORDER BY rowid"
        ).fetchone()
    removed = f"{article_collection}/{record}/{filename}"
    if transaction is None or not (data / "storage" / removed).is_file():
        raise AssertionError("fixture lacks a valid article relation or a present medias file")

    def altered(name, sql=None):
        clone = root / name
        shutil.copytree(data, clone)
        if sql:
            with sqlite3.connect(clone / "data.db") as conn:
                conn.execute(sql)
        return clone

    def codes(report):
        return {b["code"] for b in report["blockers"]}

    def anomaly_counts(report, kind):
        width = "distinct_targets" if kind == "dangling_relations" else "distinct_files"
        return {
            (item["collection"], item["field"]): (item["references"], item["records"], item[width])
            for item in report["preserved_source_anomalies"][kind]["fields"]
        }

    cases = (
        # (result name, expected code, expected field or None, SQL on one copy)
        (
            "duplicate_idempotency_key",
            "duplicate_idempotency_key",
            None,
            "INSERT INTO buma_transaction (id,type,author,system_id) SELECT 'duplicate000001',"
            "type,author,system_id FROM buma_transaction WHERE rowid="
            + first("buma_transaction", "system_id LIKE 'idem:%'"),
        ),
        (
            "unbounded_relation_target_scalar",
            "unbounded_relation_target_scalar",
            "pdv",
            "INSERT INTO buma_customer (id,pdv) "
            "VALUES ('multirel0000001','[\"pdv000000000001\",\"pdv000000000002\"]')",
        ),
        (
            "schema_drift",
            "schema_drift",
            "listRule",
            "UPDATE _collections SET listRule='C11 intentional drift' WHERE name='users'",
        ),
        (
            "invalid_number",
            "invalid_number",
            "sell_price",
            "UPDATE buma_article SET sell_price='broken' WHERE rowid=" + first("buma_article"),
        ),
        (
            "nul_in_postgresql_text",
            "nul_in_postgresql_text",
            "firstname",
            "UPDATE buma_supplier SET firstname='C11'||char(0)||'nul' WHERE rowid="
            + first("buma_supplier"),
        ),
        (
            "nul_in_postgresql_jsonb",
            "nul_in_postgresql_jsonb",
            "specification",
            'UPDATE buma_article SET specification=\'{"k":"\\u0000"}\' WHERE rowid='
            + first("buma_article"),
        ),
        (
            "jsonb_duplicate_keys_collapsed",
            "jsonb_duplicate_keys_collapsed",
            "specification",
            'UPDATE buma_article SET specification=\'{"k":1,"k":2}\' WHERE rowid='
            + first("buma_article"),
        ),
        (
            "invalid_unicode_for_postgresql",
            "invalid_unicode_for_postgresql",
            "file",
            'UPDATE buma_ressources SET file=\'["one.bin","\\ud800.bin"]\' WHERE rowid='
            + first("buma_ressources"),
        ),
        (
            "date_not_pocketbase_layout",
            "date_not_pocketbase_layout",
            "created",
            "UPDATE buma_supplier SET created='2024-02-29T12:34:56.789Z' WHERE rowid="
            + first("buma_supplier"),
        ),
        (
            "auth_legacy_nul",
            "nul_in_postgresql_jsonb",
            "auth_legacy",
            "UPDATE users SET lastResetSentAt='C11'||char(0) WHERE rowid=" + first("users"),
        ),
        (
            # Leading whitespace: PocketBase serves this text as a JSON string.
            "json_text_served_as_string_by_pocketbase",
            "json_text_served_as_string_by_pocketbase",
            "specification",
            "UPDATE buma_article SET specification=' {\"k\":1}' WHERE rowid="
            + first("buma_article"),
        ),
    )
    results = {}
    for name, code, field, sql in cases:
        report = diagnose(altered(name, sql), schema, source_version=source_version)
        added = [b for b in report["blockers"] if b not in reference["blockers"]]
        if (
            not any(b["code"] == code and field in (None, b.get("field")) for b in added)
            or report["ready_for_isolated_rehearsal"]
        ):
            raise AssertionError((name, report["blockers"]))
        results[name] = "blocked"

    def preserved(report, kind, key, expected):
        if (
            codes(report) != codes(reference)
            or report["ready_for_isolated_rehearsal"] != reference["ready_for_isolated_rehearsal"]
            or report["preserved_source_anomalies"]["blocking"]
        ):
            raise AssertionError((kind, report["blockers"]))
        before, after = anomaly_counts(reference, kind), anomaly_counts(report, kind)
        old = before.pop(key, (0, 0, 0))
        new = after.pop(key, (0, 0, 0))
        if after != before or tuple(n - o for n, o in zip(new, old, strict=True)) != expected:
            raise AssertionError((kind, report["preserved_source_anomalies"][kind]))

    def warning_counts(report):
        return {
            (w["code"], w["collection"], w["field"]): w.get("affected_rows", 0)
            for w in report["warnings"]
            if isinstance(w, dict)
        }

    for name, sql, key in (
        (
            "jsonb_number_notation_normalized",
            "UPDATE buma_article SET specification='{\"k\":1e2}' WHERE rowid="
            + first("buma_article"),
            ("jsonb_number_notation_normalized", "buma_article", "specification"),
        ),
        (
            "single_value_served_as_last_element",
            'UPDATE buma_article SET kind=\'["UV_RS","AUTRE"]\' WHERE rowid='
            + first("buma_article"),
            ("single_value_served_as_last_element", "buma_article", "kind"),
        ),
        (
            "list_served_deduplicated_by_pocketbase",
            'UPDATE buma_ressources SET file=\'["one.bin","one.bin"]\' WHERE rowid='
            + first("buma_ressources"),
            ("list_served_deduplicated_by_pocketbase", "buma_ressources", "file"),
        ),
    ):
        report = diagnose(altered(name, sql), schema, source_version=source_version)
        before, after = warning_counts(reference), warning_counts(report)
        if (
            codes(report) != codes(reference)
            or report["ready_for_isolated_rehearsal"] != reference["ready_for_isolated_rehearsal"]
            or after.get(key, 0) - before.get(key, 0) != 1
        ):
            raise AssertionError((name, report["blockers"], report["warnings"]))
        results[name] = "warning_not_blocking"

    report = diagnose(
        altered(
            "dangling_relation",
            f"UPDATE buma_transaction SET article='{absent}' WHERE rowid={transaction}",
        ),
        schema,
        source_version=source_version,
    )
    # One reference, one record and one distinct target more on this field.
    preserved(report, "dangling_relations", ("buma_transaction", "article"), (1, 1, 1))
    results["dangling_relation"] = "preserved_anomaly_not_blocking"
    clone = altered("missing_file")
    (clone / "storage" / removed).unlink()
    report = diagnose(clone, schema, source_version=source_version)
    preserved(report, "missing_files", ("buma_article", "medias"), (1, 1, 1))
    if (report["files"], report["unreferenced_files"]) != (
        reference["files"] - 1,
        reference["unreferenced_files"],
    ):
        raise AssertionError(report["files"])
    results["missing_file"] = "preserved_anomaly_not_blocking"

    # Manifest mode: data.db and the private-format manifest, no storage/ tree.
    manifest = directory_manifest(data)
    bundle = root / "manifest_bundle"
    bundle.mkdir()
    shutil.copyfile(data / "data.db", bundle / "data.db")
    manifest_path = root / "manifest.json"
    write_json_new(manifest_path, manifest)
    report = diagnose(bundle, schema, source_version=source_version, manifest=manifest_path)
    same = (
        "ready_for_isolated_rehearsal",
        "file_manifest_sha256",
        "files",
        "file_bytes",
        "unreferenced_files",
        "preserved_source_anomalies",
        "blockers",
        "warnings",
    )
    if (report["storage_mode"], report["file_bytes_copied"], report["file_bytes_verified"]) != (
        "manifest",
        False,
        False,
    ) or any(report[key] != reference[key] for key in same):
        raise AssertionError(report)
    results["manifest_mode"] = "same_diagnosis_same_file_fingerprint_no_bytes_copied"

    def manifest_case(name, change):
        variant = json.loads(json.dumps(manifest))
        change(variant)
        path = root / f"{name}.json"
        write_json_new(path, variant)
        return diagnose(bundle, schema, source_version=source_version, manifest=path)

    report = manifest_case(
        "manifest_missing_file",
        lambda m: m["files"].remove(
            next(f for f in m["files"] if f["path"] == "storage/" + removed)
        ),
    )
    preserved(report, "missing_files", ("buma_article", "medias"), (1, 1, 1))
    results["manifest_missing_file"] = "preserved_anomaly_not_blocking"

    def wal(m):
        m["files"].append(
            {"path": "data.db-wal", "bytes": 32, "sha256": "0" * 64, "crc32": "0" * 8}
        )

    def other_db(m):
        next(f for f in m["files"] if f["path"] == "data.db")["sha256"] = "0" * 64

    for name, change, code in (
        ("manifest_wal", wal, "live_or_incomplete_sqlite_snapshot"),
        ("manifest_data_db", other_db, "data_db_manifest_mismatch"),
    ):
        try:
            manifest_case(name, change)
        except ImportBlocked as exc:
            if str(exc) != code:
                raise
        else:
            raise AssertionError(name)
        results[name] = "blocked"
    return results


# ---------------------------------------------------------------------------
# PostgreSQL: state, import, independent checks.
# ---------------------------------------------------------------------------


async def table_state(engine, definitions):
    result = {}
    async with engine.connect() as conn:
        for c in definitions:
            if c["type"] != "view":
                rows = (
                    (await conn.execute(text(f"SELECT * FROM {quote(c['name'])} ORDER BY id")))
                    .mappings()
                    .all()
                )
                result[c["name"]] = [dict(r) for r in rows]
    return result


async def full_state(engine, definitions) -> dict:
    """Everything the import may write, in memory only (secrets never reported)."""
    state = await table_state(engine, definitions)
    async with engine.connect() as conn:
        for table in ("_superusers", "_params"):
            rows = await conn.execute(text(f'SELECT * FROM "{table}" ORDER BY id'))
            state[table] = [dict(r) for r in rows.mappings().all()]
        rows = await conn.execute(text("SELECT id, options FROM _collections ORDER BY id"))
        state["_collections.options"] = [dict(r) for r in rows.mappings().all()]
        for table in ("_buma_c11_rowids", AUTH_ARCHIVE):
            exists = await conn.execute(text("SELECT to_regclass(:name)"), {"name": table})
            state[table] = exists.scalar_one()
    return state


def changed_rows(before: dict, after: dict) -> list[tuple]:
    changes = []
    for table in sorted(set(before) | set(after)):
        old, new = before.get(table), after.get(table)
        if not isinstance(old, list) or not isinstance(new, list):
            if old != new:
                changes.append((table, None, ["table"]))
            continue
        old_rows = {row.get("id"): row for row in old}
        new_rows = {row.get("id"): row for row in new}
        for key in sorted(set(old_rows) | set(new_rows), key=str):
            a, b = old_rows.get(key), new_rows.get(key)
            if a is None or b is None:
                changes.append((table, key, ["row"]))
            elif a != b:
                changes.append((table, key, sorted(k for k in a if a[k] != b.get(k))))
    return changes


async def check_schema(url, schema):
    from scripts.generate_native_baseline import load_schema as translated_schema
    from scripts.verify_complete_schema import read_collections, verify

    report = verify(translated_schema(schema, include_views=True)[0], await read_collections(url))
    if not report["ok"]:
        raise AssertionError("schema verification failed")
    return {"definitions": report["expected"], "views": report["expected_views"], "ok": True}


# Refusals meaning "another import already committed": the records check of
# load_isolated, or the accounts readiness check that import_snapshot may run
# first (scripts.c11_auth_import emptiness checks).
TAKEN_TARGET_CODES = frozenset(
    {"target_not_empty", "superusers_target_not_empty", "settings_target_not_empty"}
)


async def import_once(engine, data, destination, diagnostic, definitions, **failure):
    """The rehearsal's only import call: scripts.c11_import.import_snapshot.

    Contract of the wave-3 importer (WS-B3): stage the diagnosed snapshot,
    then load the records (load_isolated) and import accounts, token secrets
    and settings (import_auth) in one transaction; on any failure nothing
    remains and the fresh staging directory is removed. ``failure`` is empty,
    ``fail_after`` (records) or ``auth_fail_after`` (import_auth stage). The
    rehearsal checks these effects itself instead of trusting the report.
    """
    unknown = set(failure) - set(inspect.signature(import_snapshot).parameters)
    if unknown:
        raise AssertionError(
            f"scripts.c11_import.import_snapshot has no {sorted(unknown)} parameter: the "
            "wave-3 importer (WS-B3) must let a failure be injected in its accounts stage"
        )
    evidence: dict = {}
    rows = await import_snapshot(
        engine, data, destination, diagnostic, definitions, report=evidence, **failure
    )
    return rows, evidence


async def remove_fresh_seeds(engine, definitions) -> None:
    # Harness only, on the virgin database initialized by this run: migration
    # seeds would make load_isolated refuse a non-empty target. Never part of
    # the import; no existing environment is ever cleared.
    async with engine.begin() as conn:
        for c in definitions:
            if c["type"] != "view":
                await conn.execute(text(f"DELETE FROM {quote(c['name'])}"))


async def refused_import(engine, data, staged, diagnostic, definitions, code, **failure):
    try:
        await import_once(engine, data, staged, diagnostic, definitions, **failure)
    except ImportBlocked as exc:
        assert str(exc) == code, (code, str(exc))
    else:
        raise AssertionError(f"import_snapshot committed instead of failing with {code}")
    assert not staged.exists(), staged


async def failure_rollbacks(engine, data, root, diagnostic, definitions, empty) -> dict:
    """Each failure leaves exactly the empty migrated state: records, archives,
    superusers, settings and auth collection options included."""
    results = {}
    await refused_import(
        engine,
        data,
        root / "failed-records",
        diagnostic,
        definitions,
        "injected_import_failure",
        fail_after=3,
    )
    assert await full_state(engine, definitions) == empty
    results["records_injected_failure"] = "nothing_remains_no_staging"

    # The last accounts stage fails after every record, the superusers and the
    # auth collection options were written in the same transaction.
    await refused_import(
        engine,
        data,
        root / "failed-accounts",
        diagnostic,
        definitions,
        "injected_auth_import_failure",
        auth_fail_after="settings",
    )
    assert await full_state(engine, definitions) == empty
    results["accounts_injected_failure_after_complete_record_load"] = (
        "records_accounts_options_settings_rolled_back_no_staging"
    )

    # A refused accounts precondition (a leftover auth archive: harness decoy,
    # empty, dropped afterwards) refuses the whole import, records included.
    async with engine.begin() as conn:
        await conn.execute(text(f'CREATE TABLE "{AUTH_ARCHIVE}" (c11_decoy integer)'))
    await refused_import(
        engine, data, root / "refused-accounts", diagnostic, definitions, "auth_archive_exists"
    )
    async with engine.begin() as conn:
        await conn.execute(text(f'DROP TABLE "{AUTH_ARCHIVE}"'))
    assert await full_state(engine, definitions) == empty
    results["accounts_precondition_refusal"] = "nothing_remains_no_staging"
    return results


async def concurrent_imports(engine, data, root, diagnostic, definitions) -> dict:
    destinations = [root / "import-a", root / "import-b"]

    async def attempt(destination):
        try:
            return await import_once(engine, data, destination, diagnostic, definitions)
        except ImportBlocked as exc:
            return str(exc)

    outcomes = await asyncio.gather(*(attempt(d) for d in destinations))
    winners = [i for i, o in enumerate(outcomes) if isinstance(o, tuple)]
    assert len(winners) == 1, [o if isinstance(o, str) else "committed" for o in outcomes]
    (winner,) = winners
    loser = 1 - winner
    rows, evidence = outcomes[winner]
    assert rows == diagnostic["rows"] and outcomes[loser] in TAKEN_TARGET_CODES, outcomes[loser]
    assert destinations[winner].is_dir() and not destinations[loser].exists()
    return {
        "published": destinations[winner],
        "rows": rows,
        "evidence": evidence,
        "report": {
            "attempts": 2,
            "committed": 1,
            "refused": outcomes[loser],
            "loser_staging_removed": True,
            "rows": rows,
        },
    }


def source_settings(data: Path) -> tuple[dict, list[dict]]:
    with contextlib.closing(
        sqlite3.connect(f"{(data / 'data.db').resolve().as_uri()}?mode=ro&immutable=1", uri=True)
    ) as db:
        db.row_factory = sqlite3.Row
        settings = json.loads(
            db.execute("SELECT value FROM _params WHERE key='settings'").fetchone()[0]
        )
        admins = [dict(r) for r in db.execute("SELECT * FROM _admins ORDER BY _rowid_")]
    return settings, admins


def jsonish(value):
    return json.loads(value) if isinstance(value, str) else value


async def check_imported(engine, data, definitions, final, profile) -> dict:
    """Independent reads of the imported database (booleans and counts only)."""
    with snapshot(data) as db:
        async with engine.connect() as conn:
            archived = (await conn.execute(text("SELECT * FROM _buma_c11_rowids"))).mappings().all()
            for row in archived:
                c = next(c for c in definitions if c["id"] == row["collection_id"])
                source = db.execute(
                    f"SELECT _rowid_ FROM {quote(c['name'])} WHERE id=?", (row["record_id"],)
                ).fetchone()
                assert source[0] == row["source_rowid"]
    users = {r["id"]: r["rowid"] for r in final["users"]}
    articles = {r["id"]: r["rowid"] for r in final["buma_article"]}
    expected_users = {USER: 41, DENIED: 93}
    expected_articles = {"c11art000000001": 7001}
    if profile == "real_shape":
        expected_users |= {NO_EMAIL: 94, NO_EMAIL_2: 95}
        expected_articles |= {"c11art000000002": 7002}
    assert users == expected_users and articles == expected_articles, (users, articles)
    supplier = next(r for r in final["buma_supplier"] if r["id"] == SUPPLIER)
    assert supplier["status"] is False and supplier["solde"] == -17.25
    assert supplier["created"].isoformat() == "2024-02-29T12:34:56.789000+00:00"
    result = {
        "rows": len(archived),
        "source_rowids_all_tables": True,
        "native_rowids_users_article": True,
        "negative_rowid_archived": any(r["source_rowid"] == -17 for r in archived),
        "timestamps_status_balances": True,
    }
    settings, admins = source_settings(data)
    async with engine.connect() as conn:
        superusers = (await conn.execute(text('SELECT * FROM "_superusers"'))).mappings().all()
        params = (
            (await conn.execute(text("SELECT value FROM \"_params\" WHERE key='settings'")))
            .scalars()
            .all()
        )
        options = {
            r["name"]: jsonish(r["options"])
            for r in (
                await conn.execute(text("SELECT name, options FROM _collections WHERE type='auth'"))
            ).mappings()
        }
    by_id = {r["id"]: r for r in superusers}
    assert len(superusers) == len(admins), (len(superusers), len(admins))
    for admin in admins:
        target = by_id[admin["id"]]
        assert (target["email"], target["password_hash"], target["token_key"]) == (
            admin["email"],
            admin["passwordHash"],
            admin["tokenKey"],
        )
    assert len(params) == 1
    secrets_kept = {
        "users": options["users"]["authToken"]["secret"] == settings["recordAuthToken"]["secret"],
        "agent_users": options["agent_users"]["authToken"]["secret"]
        == settings["recordAuthToken"]["secret"],
        "_superusers": options["_superusers"]["authToken"]["secret"]
        == settings["adminAuthToken"]["secret"],
    }
    assert all(secrets_kept.values()), secrets_kept
    result["accounts"] = {
        "superusers": len(superusers),
        "superusers_equal_source_admins": True,
        "settings_rows": len(params),
        "auth_token_secrets_equal_source": secrets_kept,
    }
    if profile == "real_shape":
        shapes = fixture.REAL_SHAPE
        tables = {
            "buma_customer.pdv": final["buma_customer"],
            "buma_pilotage_ticket.tag": final["buma_pilotage_ticket"],
            "buma_wallet.managers": final["buma_wallet"],
        }
        for path, records in shapes["unlimited_relations"].items():
            name = path.split(".")[1]
            stored = {r["id"]: r[name] for r in tables[path]}
            assert stored == records, (path, stored)  # arrays: order and elements kept
        for path, records in shapes["orphan_relations"].items():
            collection, name = path.split(".")
            stored = {r["id"]: r[name] for r in final[collection] if r["id"] in records}
            if path == "buma_wallet.managers":
                stored = {k: v[0] for k, v in stored.items()}
            assert stored == records, (path, stored)
        for path, records in shapes["user_dates"].items():
            collection, name = path.split(".")
            stored = {r["id"]: r[name] for r in final[collection] if r["id"] in records}
            assert stored == {r: date_value(v) for r, v in records.items()}, path
        ressources = {r["id"]: r["file"] for r in final["buma_ressources"]}
        assert ressources["c11file00000004"] == []
        more = {r["id"]: r["more"] for r in final["users"]}
        assert more[NO_EMAIL] is None
        result["real_shape"] = {
            "unlimited_relation_arrays_identical": True,
            "orphans_stored_as_is": True,
            "user_dates_exact": True,
            "empty_multi_file_list": True,
            "json_null": True,
        }
    return result


# ---------------------------------------------------------------------------
# Isolated PPBase server.
# ---------------------------------------------------------------------------


def server_ppbase(root: Path, env: dict) -> dict:
    """Resolve ppbase/app as the server command will (same python, environment and cwd)."""
    import ppbase

    probe = (
        "import importlib.util, json, app, ppbase; print(json.dumps({"
        "'ppbase': ppbase.__file__, 'app': app.__file__, 'multiplicity_rule': "
        "importlib.util.find_spec('ppbase.models.field_multiplicity') is not None}))"
    )
    loaded = json.loads(command(sys.executable, "-c", probe, cwd=root, env=env))
    package = Path(loaded["ppbase"]).resolve().parent
    assert package == Path(ppbase.__file__).resolve().parent, (package, ppbase.__file__)
    assert Path(loaded["app"]).resolve().parent.parent == PROJECT, loaded["app"]
    assert loaded["multiplicity_rule"], "server PPBase lacks the D-0019 multiplicity rule"
    try:
        commit = command("git", "-C", str(package), "rev-parse", "HEAD")
    except (OSError, subprocess.CalledProcessError):
        commit = None
    return {
        "package_dir": str(package),
        "git_commit": commit,
        "same_as_rehearsal_process": True,
        "relation_multiplicity_rule": True,
        "app_from_this_project": True,
    }


def live_server_process(pid: int, env: dict, root: Path) -> dict:
    proc = Path("/proc") / str(pid)
    try:
        variables = (proc / "environ").read_bytes().decode().split("\0")
        cwd = Path(os.readlink(proc / "cwd"))
        argv = (proc / "cmdline").read_bytes().decode().split("\0")
    except OSError:  # no /proc (e.g. macOS): the probe above remains the evidence
        return {"source": "unavailable"}
    pythonpath = next(
        (item.split("=", 1)[1] for item in variables if item.startswith("PYTHONPATH=")), None
    )
    assert pythonpath == env["PYTHONPATH"], pythonpath
    assert cwd == root.resolve(), cwd
    assert argv[:4] == [sys.executable, "-m", "ppbase", "serve"], argv[:4]
    assert "app.hooks.register:register" in argv, argv
    return {
        "source": "/proc/<server pid>",
        "pythonpath_matches_probe": True,
        "cwd_is_the_probe_cwd": True,
        "command": "python -m ppbase serve --hooks app.hooks.register:register",
    }


@contextlib.asynccontextmanager
async def ppbase_server(root: Path, url: str, published: Path, report: dict):
    """BUMA on PPBase, after a complete offline import; internal jobs disabled.

    The cwd is a fresh directory, so no repository .env reaches the server; the
    PYTHONPATH puts this project first, then the caller's (the PPBase tested).
    """
    cwd = root / "ppbase-cwd"
    cwd.mkdir()
    env = ppbase_server_environment()
    report["server_ppbase"] = server_ppbase(cwd, env)
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    with (root / "ppbase.log").open("w") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "ppbase",
                "serve",
                "--db",
                url,
                "--dir",
                str(published),
                "--migrations-dir",
                str(PROJECT / "pb_migrations"),
                "--hooks",
                "app.hooks.register:register",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            cwd=cwd,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            await wait_health(process, base, "PPBase")
            report["server_ppbase"]["live_process"] = live_server_process(process.pid, env, cwd)
            yield base
        finally:
            stop(process)
    report["server_ppbase"]["stopped"] = process.poll() is not None


# ---------------------------------------------------------------------------
# Scenario.
# ---------------------------------------------------------------------------


async def exercise(root, binary, schema, version, profile, report, start_postgres):
    from ppbase.services.database_preparation import prepare_database

    report["schema_path"] = str(schema)
    definitions = load_schema_cached(report)
    data = fixture.create_fixture(root / "fixture", binary, schema, profile=profile)
    migrations = root / "fixture" / "migrations"
    pocketbase = await pocketbase_phase(binary, data, migrations, root, profile, report)

    original_hash = digest(data / "data.db")
    original_storage = storage_inventory(data / "storage")
    diagnostic = diagnose(data, schema, source_version=version)
    assert diagnostic["ready_for_isolated_rehearsal"], diagnostic["blockers"]
    assert diagnostic["data_db_sha256"] == original_hash
    report["diagnostic"] = diagnostic
    report["preserved_anomalies"] = check_preserved_anomalies(diagnostic, profile)
    report["negative_cases"] = negative_diagnostics(
        data, root / "negative", schema, source_version=version
    )

    url = start_postgres()
    # Hashes, tokenKeys and secrets are bound parameters: never echo them.
    engine = create_async_engine(url, connect_args={"ssl": "disable"}, hide_parameters=True)
    try:
        # Enforce a virgin target independently of its random Docker name.
        async with engine.connect() as conn:
            tables = "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'"
            assert (await conn.execute(text(tables))).scalar_one() == 0
        migrations_applied = await prepare_database(engine, PROJECT / "pb_migrations")
        report["migrations_applied"] = len(migrations_applied)
        report["schema_before"] = await check_schema(url, schema)
        async with engine.connect() as conn:
            columns = (
                (
                    await conn.execute(
                        text(
                            "SELECT table_name,column_name,data_type,udt_name "
                            "FROM information_schema.columns "
                            "WHERE table_schema='public' AND ("
                            "(table_name='buma_customer' AND column_name='pdv') OR "
                            "(table_name='buma_pilotage_ticket' AND column_name='tag') OR "
                            "(table_name='buma_wallet' AND column_name='managers'))"
                        )
                    )
                )
                .mappings()
                .all()
            )
            # D-0019: the three maxSelect-null relations are multiple, text[]
            # like PocketBase's JSON arrays (no element length limit).
            assert len(columns) == 3
            assert all(c["data_type"] == "ARRAY" and c["udt_name"] == "_text" for c in columns)
            report["unbounded_relation_target_columns"] = [dict(c) for c in columns]
            spec = await introspect_target(conn, definitions)
        from scripts.verify_schema_baseline import read_collections as read_physical_rows
        from scripts.verify_schema_baseline import verify_relation_columns

        # Database created by the corrected PPBase: every single relation is text.
        relation_columns = verify_relation_columns(
            definitions, await read_physical_rows(url), "fresh"
        )
        assert relation_columns["ok"], relation_columns["errors"]
        report["relation_columns"] = {
            key: relation_columns[key] for key in ("mode", "multiple", "single")
        }
        # The diagnosis again, every value checked against its target column.
        typed = diagnose(data, schema, source_version=version, target_columns=spec)
        assert typed["ready_for_isolated_rehearsal"], typed["blockers"]
        assert typed["target_types_checked"] is True
        report["diagnostic_with_target_types"] = {
            "ready_for_isolated_rehearsal": True,
            "columns_checked": typed["target_preflight"]["columns_checked"],
            "blockers": typed["blockers"],
        }
        baseline = await full_state(engine, definitions)
        report["seed_rows_before"] = sum(
            len(v) for k, v in baseline.items() if isinstance(v, list) and not k.startswith("_")
        )
        assert not baseline["_superusers"] and not baseline["_params"]
        await remove_fresh_seeds(engine, definitions)
        empty = await full_state(engine, definitions)
        report["failure_rollback"] = await failure_rollbacks(
            engine, data, root, diagnostic, definitions, empty
        )
        imported = await concurrent_imports(engine, data, root, diagnostic, definitions)
        published = imported["published"]
        report["concurrent_import"] = imported["report"]
        auth = imported["evidence"].get("auth")
        report["import"] = {
            "function": "scripts.c11_import.import_snapshot",
            "rows": imported["rows"],
            "storage_mode": imported["evidence"].get("storage_mode"),
            # import_auth's own value-free report (counts, states, no secret).
            "auth": auth,
        }
        final = await table_state(engine, definitions)
        assert sum(len(rows) for rows in final.values()) == diagnostic["rows"]
        report["preservation"] = await check_imported(engine, data, definitions, final, profile)
        report["preservation"]["file_count"] = len(storage_inventory(published / "storage"))
        report["schema_after"] = await check_schema(url, schema)

        before_http = await full_state(engine, definitions)
        server_report: dict = {}
        credentials = credentials_for(pocketbase["tokens"])  # issued before the import
        async with ppbase_server(root, url, published, server_report) as base:
            answers = await read_probes(base, credentials, profile, pocketbase["files"])
            answers["realtime"] = await realtime_probe(base, credentials)  # the only write
        report["server_ppbase"] = server_report.pop("server_ppbase")
        # The comparison is recorded first: a failed absolute check keeps it.
        field_types = {(c["name"], f["name"]): f["type"] for c in definitions for f in c["schema"]}
        comparison = compare_servers(pocketbase["answers"], answers, profile, field_types)
        report["comparison_with_pocketbase"] = comparison
        report["absolute_checks"] = {
            "pocketbase": check_absolute(
                pocketbase["answers"], profile, data, pocketbase["files"], "pocketbase"
            ),
            "ppbase": check_absolute(answers, profile, data, pocketbase["files"], "ppbase"),
        }
        after_http = await full_state(engine, definitions)
        changes = changed_rows(before_http, after_http)
        # The realtime probe is the only probe write: one supplier name and its
        # date. Without any superuser (a source without admin, e.g. the default
        # profile) PPBase's start also stores its one-time setup token in
        # _params (ppbase/app.py:189-198, ppbase/services/setup_service.py:13-38),
        # where PocketBase 0.22 writes nothing.
        setup = [
            change
            for change in changes
            if change[0] == "_params"
            and change[2] == ["row"]
            and not before_http["_superusers"]
            and [r["key"] for r in after_http["_params"] if r["id"] == change[1]]
            == ["_setup_token"]
        ]
        expected = [("buma_supplier", SUPPLIER, ["firstname", "updated"])]
        assert [c for c in changes if c not in setup] == expected and len(setup) <= 1, changes
        report["writes_during_http_probes"] = {
            "realtime_supplier_update": True,
            "ppbase_setup_token_without_superuser": bool(setup),
        }
        assert digest(data / "data.db") == original_hash
        assert storage_inventory(data / "storage") == original_storage
        assert storage_inventory(published / "storage") == original_storage
        report["source_unchanged"] = True
        report["no_business_hook_replay"] = True
        report["schema_after_http"] = await check_schema(url, schema)
        if comparison["unexpected_differences"]:
            raise AssertionError("PPBase answers differ from PocketBase's (see report)")
    finally:
        await engine.dispose()


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--pocketbase", type=Path, required=True)
    parser.add_argument("--schema", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--profile", choices=PROFILES, default=DEFAULT_PROFILE)
    parser.add_argument(
        "--private-logs",
        type=Path,
        help="New private directory (0700) receiving the PocketBase and PPBase logs",
    )
    parser.add_argument(
        "--container-tag",
        default="rehearsal",
        help="Middle part of the container name buma-c11-<tag>-<random>",
    )
    args = parser.parse_args()
    if args.report.exists():
        parser.error("Report must be a new path")
    if args.private_logs is not None and args.private_logs.exists():
        parser.error("Private log directory must be a new path")
    if not args.container_tag.replace("-", "").isalnum():
        parser.error("--container-tag must be alphanumeric with dashes")
    version = fixture.pocketbase_version(args.pocketbase)
    if version not in fixture.POCKETBASE_VERSIONS:
        parser.error(
            f"The binary reports PocketBase {version}; the C11 fixture builder accepts "
            + " or ".join(fixture.POCKETBASE_VERSIONS)
        )
    code, evidence = source_version_status(version)
    if code:
        parser.error(
            f"scripts/c11_import.py does not accept PocketBase {version} ({code}): this "
            "rehearsal imports only a source version the importer validated"
        )
    name = f"buma-c11-{args.container_tag}-{uuid.uuid4().hex[:12]}"
    password = secrets.token_hex(16)
    report = {
        "timestamp_utc": datetime.now(UTC).isoformat(),
        "scope": "c11_complete_synthetic_rehearsal",
        "profile": args.profile,
        "pocketbase": {"version": version, "binary_sha256": digest(args.pocketbase)},
        "importer": {
            "source_version_evidence": evidence,
            "validated_source_versions": sorted(VALIDATED_SOURCE_VERSIONS),
            "unbounded_relation_arrays": UNBOUNDED_RELATION_ARRAYS,
        },
        "c10_resumed": False,
        "production_data_used": False,
        "status": "failed",
    }
    created = False

    def start_postgres():
        nonlocal created
        command(
            "docker",
            "run",
            "--detach",
            "--rm",
            "--name",
            name,
            "--label",
            "buma.scope=c11-synthetic-rehearsal",
            "--publish",
            "127.0.0.1::5432",
            "--env",
            "POSTGRES_USER=c11",
            "--env",
            f"POSTGRES_PASSWORD={password}",
            "--env",
            f"POSTGRES_DB={DATABASE}",
            "postgres:16-alpine",
        )
        created = True
        port = command("docker", "port", name, "5432/tcp").rsplit(":", 1)[1]
        for _ in range(150):
            try:
                command("docker", "exec", name, "pg_isready", "-U", "c11", "-h", "127.0.0.1")
                break
            except subprocess.CalledProcessError:
                time.sleep(0.2)
        return f"postgresql+asyncpg://c11:{password}@127.0.0.1:{port}/{DATABASE}"

    started = time.monotonic()
    try:
        with tempfile.TemporaryDirectory(prefix="buma-c11-rehearsal-") as folder:
            root = Path(folder)
            try:
                asyncio.run(
                    exercise(
                        root,
                        args.pocketbase.resolve(),
                        args.schema.resolve(),
                        version,
                        args.profile,
                        report,
                        start_postgres,
                    )
                )
                report["status"] = "passed"
            finally:
                if args.private_logs is not None:
                    args.private_logs.mkdir(mode=0o700)
                    for log in root.rglob("*.log"):
                        target = args.private_logs / log.name
                        shutil.copyfile(log, target)
                        target.chmod(0o600)
    except BaseException as exc:
        # Never leak tokens, secrets, hashes or bind parameters into the report.
        report["failure_type"] = type(exc).__name__
        raise
    finally:
        report.pop("schema_path", None)
        cleanup = {"temporary_directory_removed": True}
        if created:
            command("docker", "stop", "--time", "5", name)
            for _ in range(50):
                if not command("docker", "ps", "-a", "--filter", f"name=^/{name}$", "-q"):
                    break
                time.sleep(0.2)
            cleanup["container_removed"] = not command(
                "docker", "ps", "-a", "--filter", f"name=^/{name}$", "-q"
            )
        report["cleanup"] = cleanup
        report["elapsed_seconds"] = round(time.monotonic() - started, 1)
        write_json_new(args.report, report)
        print(json.dumps({"status": report["status"], "report": str(args.report)}))


if __name__ == "__main__":
    main()
