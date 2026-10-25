"""WS-C runtime proof: PocketBase 0.22.34 sessions survive the C11 import.

Synthetic only, on owned disposable resources. PocketBase 0.22.34 builds its
own fixture (the corrected export imported like scripts/c11_fixture.py, admins
created by ``pocketbase admin create``, users/agent_users with known passwords),
issues tokens over HTTP (some before a password reset or an email change renews
the account tokenKey), e-mails purpose tokens to a local SMTP sink and serves a
realtime subscription. After a clean stop the snapshot is diagnosed (source
version 0.22.34, validated by D-0024) and imported by the single entry point
``scripts.c11_import.import_snapshot`` (records, accounts and settings in one
transaction) into a disposable PostgreSQL 16 container, where every
import_auth refusal is also exercised and rolled back. PPBase then serves it
with the BUMA hooks (the PPBase package the server really loads is recorded)
and the same probes run again, realtime and secret exposure included, then the
session revocation paths (per account, global settings rotation, collection API
and the emergency SQL rotation of the runbook), and last the purpose tokens
PocketBase e-mailed before the import, on accounts dedicated to them.
PocketBase is finally restarted on its untouched source (the rollback path) to
probe the sessions PPBase issued, then runs the same revocation paths and
consumes the same purpose tokens in the same order: the reference every PPBase
status, message and effect must equal. There is no --db/--data argument: no
supplied backup or database is touched.

python -m scripts.c11_auth_runtime --pocketbase /path/to/pocketbase-0.22.34 \
    --schema /path/to/export.json --report /new/report.json
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import email
import email.policy
import json
import os
import re
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import traceback
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx
import jwt
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from scripts.c11_auth_import import ARCHIVE_TABLE, FAIL_STAGES, TOKEN_KEYS, import_auth
from scripts.c11_import import (
    ImportBlocked,
    date_value,
    diagnose,
    digest,
    import_snapshot,
    load_isolated,
    load_schema,
    quote,
    stage_snapshot,
    write_json_new,
)

PROJECT = Path(__file__).resolve().parents[1]
SOURCE_VERSION = "0.22.34"
PASSWORD = "C11-auth-password"
NEW_PASSWORD = "C11-auth-password-new"
ADMIN_PASSWORD = "C11-admin-password"
# Synthetic SMTP credentials, set after the last mail (the sink offers no AUTH);
# only their presence in HTTP responses is reported, never the value.
SMTP_USERNAME, SMTP_PASSWORD = "c11-smtp-user", "C11-smtp-synthetic-password"
ADMIN_EMAIL = "c11-admin@example.invalid"
ADMIN_REF_EMAIL = "c11-admin-ref@example.invalid"
# Holds the admin reset token only: consuming it never touches the admin sessions.
ADMIN_PROBE_EMAIL = "c11-admin-probe@example.invalid"
ADMIN_EMAILS = (ADMIN_EMAIL, ADMIN_REF_EMAIL, ADMIN_PROBE_EMAIL)
ROLE, USER, REF, PROBE = "c11authrole0001", "c11authuser0001", "c11authref00001", "c11authprobe001"
AGENT, ACCESS, SUPPLIER = "c11authagent001", "c11authaccess01", "c11authsupp0001"
PROBE2 = "c11authprobe002"
EMAILS = {
    USER: "c11-user@example.invalid",
    REF: "c11-ref@example.invalid",
    PROBE: "c11-probe@example.invalid",
    PROBE2: "c11-probe2@example.invalid",
    AGENT: "c11-agent@example.invalid",
}
REF_NEW_EMAIL, PROBE_NEW_EMAIL = "c11-ref-new@example.invalid", "c11-probe-new@example.invalid"
PROBE2_NEW_EMAIL = "c11-probe2-new@example.invalid"
USERS_REFRESH = "/api/collections/users/auth-refresh"
AGENTS_REFRESH = "/api/collections/agent_users/auth-refresh"
SUPPLIER_PATH = f"/api/collections/buma_supplier/records/{SUPPLIER}"
ACCESS_PATH = f"/api/collections/buma_caisse_access/records/{ACCESS}"
AGENT_PATH = f"/api/collections/agent_users/records/{AGENT}"
JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")

# label, method, path, credential, expected id. Identical against PocketBase
# and PPBase; every token was issued by PocketBase 0.22.34 before the import.
ACCEPTED_PROBES = (
    ("admin_lists_admins", "GET", "/api/admins", "admin", "ADMIN"),
    ("admin_refresh", "POST", "/api/admins/auth-refresh", "admin", "ADMIN"),
    ("admin_reads_protected_supplier", "GET", SUPPLIER_PATH, "admin", SUPPLIER),
    (
        "admin_ref_token_after_reset_refresh",
        "POST",
        "/api/admins/auth-refresh",
        "admin_ref_after_reset",
        "ADMIN_REF",
    ),
    ("users_refresh", "POST", USERS_REFRESH, "users", USER),
    ("users_reads_admin_role_caisse_access", "GET", ACCESS_PATH, "users", ACCESS),
    ("users_reads_protected_supplier", "GET", SUPPLIER_PATH, "users", SUPPLIER),
    ("users_ref_token_after_changes_refresh", "POST", USERS_REFRESH, "ref_after_changes", REF),
    ("agent_refresh", "POST", AGENTS_REFRESH, "agent", AGENT),
    ("agent_reads_own_record", "GET", AGENT_PATH, "agent", AGENT),
    ("agent_reads_protected_supplier", "GET", SUPPLIER_PATH, "agent", SUPPLIER),
)
REFUSED_PROBES = (
    ("anonymous_protected_supplier", "GET", SUPPLIER_PATH, None, None),
    ("anonymous_users_refresh", "POST", USERS_REFRESH, None, None),
    ("tampered_admin_lists_admins", "GET", "/api/admins", "admin_tampered", None),
    ("tampered_admin_protected_supplier", "GET", SUPPLIER_PATH, "admin_tampered", None),
    ("tampered_users_refresh", "POST", USERS_REFRESH, "users_tampered", None),
    ("tampered_users_protected_supplier", "GET", SUPPLIER_PATH, "users_tampered", None),
    ("tampered_agent_own_record", "GET", AGENT_PATH, "agent_tampered", None),
    ("expired_admin_lists_admins", "GET", "/api/admins", "admin_expired", None),
    ("expired_users_refresh", "POST", USERS_REFRESH, "users_expired", None),
    ("expired_users_protected_supplier", "GET", SUPPLIER_PATH, "users_expired", None),
    ("users_token_on_agent_refresh", "POST", AGENTS_REFRESH, "users", None),
    ("users_token_on_admin_route", "GET", "/api/admins", "users", None),
    ("users_token_on_admin_refresh", "POST", "/api/admins/auth-refresh", "users", None),
    ("admin_token_on_users_refresh", "POST", USERS_REFRESH, "admin", None),
    ("agent_token_on_admin_role_caisse_access", "GET", ACCESS_PATH, "agent", None),
    ("forged_base_collection_protected_supplier", "GET", SUPPLIER_PATH, "users_forged", None),
    ("forged_base_collection_users_refresh", "POST", USERS_REFRESH, "users_forged", None),
    # Issued before PocketBase renewed the account tokenKey (password reset, email
    # change): v0.22.34 models/record.go:942-962, models/admin.go:45-67.
    (
        "users_ref_token_before_password_reset_refresh",
        "POST",
        USERS_REFRESH,
        "ref_before_reset",
        None,
    ),
    (
        "users_ref_token_before_password_reset_protected_supplier",
        "GET",
        SUPPLIER_PATH,
        "ref_before_reset",
        None,
    ),
    (
        "users_ref_token_before_email_change_refresh",
        "POST",
        USERS_REFRESH,
        "ref_before_email_change",
        None,
    ),
    (
        "admin_ref_token_before_reset_lists_admins",
        "GET",
        "/api/admins",
        "admin_ref_before_reset",
        None,
    ),
    (
        "admin_ref_token_before_reset_refresh",
        "POST",
        "/api/admins/auth-refresh",
        "admin_ref_before_reset",
        None,
    ),
)
PROBES = ACCEPTED_PROBES + REFUSED_PROBES
ACCEPTED = {label for label, *_rest in ACCEPTED_PROBES}
TOKEN_KEY_RENEWAL_PROBES = {label for label, *_rest in REFUSED_PROBES if "_before_" in label}
REALTIME_TOPIC = f"buma_supplier/{SUPPLIER}"
# js-sdk v0.21.5 RecordService.subscribe("*") topic (src/services/RecordService.ts:130-134),
# checked against the list rule by v0.22.34 apis/realtime.go:383-386.
REALTIME_COLLECTION_TOPIC = "buma_supplier/*"
# The JS SDK POSTs /api/realtime only for the first listener of a new topic
# (js-sdk v0.21.5 src/services/RealtimeService.ts:85-94) and, on unsubscribe(),
# only while other topics remain (:138-143; the last topic only closes the
# stream). Each POST carries every remaining topic and the current token
# (:258-283), so "new topic" sends both topics and "unsubscribe" the other one.
BOTH_TOPICS = (REALTIME_TOPIC, REALTIME_COLLECTION_TOPIC)
# Same outcome required on both servers, for every key measured. PocketBase
# refuses a subscription only when an earlier one was authorized as another
# auth identity (v0.22.34 apis/realtime.go:187-191 with extractAuthIdFromGetter
# :539-551, the id alone; v0.40.6 apis/realtime.go:218-222 and isSameAuth
# :843-853, collection and id) and stores the identity of every accepted one
# (v0.22.34 :201-203). PPBase keys it collectionId:id without the token type
# (ppbase/api/realtime.py:45-72, refused :186-193, stored :255-258), so a 0.22
# token refreshed into type=auth keeps its identity and a guest may authenticate.
REALTIME_SAME = (
    "subscribe_with_legacy_users_token",
    "anonymous_subscribe",
    "protected_update_status",
    "event_delivered_to_legacy_token_subscriber",
    "event_delivered_to_anonymous_subscriber",
    "legacy_users_token_refresh_status",
    "subscribe_new_topic_same_client_after_refresh",
    "protected_update_after_refresh_status",
    "event_after_refresh_delivered_on_first_topic",
    "event_after_refresh_delivered_on_new_topic",
    "unsubscribe_one_of_two_topics_same_client_after_refresh",
    "native_subscribe",
    "native_subscribe_new_topic_same_client_after_refresh",
    "guest_subscribe",
    "guest_then_legacy_token_new_topic_same_client",
    "legacy_token_then_guest_new_topic_same_client",
    "admin_subscribe_with_legacy_token",
    "admin_refresh_status",
    "admin_subscribe_new_topic_same_client_after_refresh",
)
# Token types, informational: PPBase issues type=auth (see compare()).
REALTIME_TOKEN_TYPES = ("refreshed_token_type", "native_token_type", "admin_refreshed_token_type")
EXPOSURE_PATHS = (
    ("settings", "/api/settings"),
    ("collections_list", "/api/collections?perPage=500"),
    ("users_collection", "/api/collections/users"),
    ("agent_users_collection", "/api/collections/agent_users"),
    ("superusers_collection", "/api/collections/_superusers"),
)
SECRET_MASK = "******"  # v0.22.34 models/settings/settings.go:19-21
REVOKED_PASSWORD = "C11-auth-password-revoked"
# scope, PocketBase 0.22 settings key, then (label, method, path, credential).
GLOBAL_REVOCATION_CHECKS = (
    (
        "record",
        "recordAuthToken",
        (
            ("users_refresh", "POST", USERS_REFRESH, "users"),
            ("agent_refresh", "POST", AGENTS_REFRESH, "agent"),
            ("users_protected_read", "GET", SUPPLIER_PATH, "users"),
        ),
    ),
    ("admin", "adminAuthToken", (("admin_lists_admins", "GET", "/api/admins", "admin"),)),
)
# Probes whose PocketBase status must be reproduced by PPBase.
REVOCATION_SAME = (
    "account_password_change_by_admin",
    "account_previous_token_refresh",
    "account_login_with_new_password",
    "admin_account_password_change",
    "admin_account_previous_token_lists_admins",
    "admin_account_login_with_new_password",
    "open_stream_subscribe",
    "open_stream_update_status",
    # Neither server cuts an open stream: both keep the subscriber's auth state
    # (v0.22.34 apis/realtime.go:202-203, ppbase/api/realtime.py:240-247).
    "open_stream_event_after_settings_record_rotation",
)
# PPBase stores the 0.22 keys in _params.settings (ppbase/api/settings.py:455-458)
# but verifies with _collections.options (ppbase/api/deps.py:94-99, :145-147).
REVOCATION_KNOWN_DIVERGENCES = {
    **{
        f"{name}_after_settings_{scope}_rotation": (
            f"PATCH /api/settings {key} answers 200 without effect: nothing in PPBase "
            "reads it after the import moved the secret into the collection options"
        )
        for scope, key, checks in GLOBAL_REVOCATION_CHECKS
        for name, *_rest in checks
    },
    "open_stream_post_after_settings_record_rotation": (
        "same cause: the token is still valid on PPBase, so the identity check of "
        "ppbase/api/realtime.py:186-193 passes (PocketBase: anonymous, 403 at "
        "apis/realtime.go:187-191)"
    ),
}
# PPBase collection -> session check (method, path, session label).
COLLECTION_ROTATION_CHECKS = {
    "users": ("POST", USERS_REFRESH, "users"),
    "agent_users": ("POST", AGENTS_REFRESH, "agent"),
    "_superusers": ("GET", "/api/admins", "admin"),
}
COLLECTION_PATCH_SHAPES = {
    # Current PocketBase API: auth options at the top level of the collection
    # (v0.40.6 core/collection_model_auth_options.go:139).
    "flat_current_pocketbase_shape": lambda secret: {"authToken": {"secret": secret}},
    # PPBase's own update model nests them (ppbase/models/collection.py:101-117).
    "nested_ppbase_options": lambda secret: {"options": {"authToken": {"secret": secret}}},
}
# Emergency revocation for the runbook (WS-F), proven by ppbase_revocation_paths:
# after the import PocketBase 0.22's global "Invalidate all previously issued
# tokens" has no effective PPBase API path for record sessions. One new random
# secret per auth collection, read by PPBase on the next request; gen_random_uuid()
# draws from pg_strong_random (PostgreSQL >= 13), so the secret never leaves the
# database. Narrow the IN list to revoke only record ('users', 'agent_users') or
# admin ('_superusers') sessions; expect one row per name, secret_length 64.
EMERGENCY_SESSION_REVOCATION_SQL = """UPDATE "_collections"
   SET "options" = jsonb_set("options", '{authToken,secret}',
         to_jsonb(replace(gen_random_uuid()::text || gen_random_uuid()::text, '-', ''))),
       "updated" = now()
 WHERE "type" = 'auth' AND "name" IN ('users', 'agent_users', '_superusers')
RETURNING "name", length("options" -> 'authToken' ->> 'secret') AS secret_length"""
# BUMA legacy probes (contract C3, docs/contracts/authentication.md); PPBase only.
LEGACY_PROBES = (
    ("is_connected_users", "/isConnected", "users", 200, {"message": "true"}),
    ("is_connected_agent", "/isConnected", "agent", 200, {"message": "true"}),
    ("is_connected_admin", "/isConnected", "admin", 400, {"message": "false"}),
    ("is_connected_tampered", "/isConnected", "users_tampered", 400, {"message": "false"}),
    ("is_connected_expired", "/isConnected", "users_expired", 400, {"message": "false"}),
    ("user_health_users", "/master/user/health", "users", 200, "true"),
    ("user_health_expired", "/master/user/health", "users_expired", 200, "false"),
)
USERS_LOGIN = "/api/collections/users/auth-with-password"
AGENTS_LOGIN = "/api/collections/agent_users/auth-with-password"
ADMINS_LOGIN = "/api/admins/auth-with-password"
SUPERUSERS_LOGIN = "/api/collections/_superusers/auth-with-password"
# label, path, identity, password; same calls on both servers.
LOGINS = (
    ("users_by_username", USERS_LOGIN, "c11authuser", PASSWORD),
    ("users_reset_password", USERS_LOGIN, "c11authref", NEW_PASSWORD),
    ("users_wrong_password", USERS_LOGIN, "c11authuser", "wrong!"),
    ("agent_by_email", AGENTS_LOGIN, EMAILS[AGENT], PASSWORD),
    ("agent_by_username", AGENTS_LOGIN, "c11authagent", PASSWORD),
    ("agent_wrong_password", AGENTS_LOGIN, "c11authagent", "x"),
    ("admin_by_email", ADMINS_LOGIN, ADMIN_EMAIL, ADMIN_PASSWORD),
    ("admin_reset_password", ADMINS_LOGIN, ADMIN_REF_EMAIL, NEW_PASSWORD),
    ("admin_wrong_password", ADMINS_LOGIN, ADMIN_EMAIL, "wrong-password"),
    # PocketBase >= 0.23 route only: 404 on 0.22, informational on PPBase.
    ("superusers_collection", SUPERUSERS_LOGIN, ADMIN_EMAIL, ADMIN_PASSWORD),
)
NEW_PASSWORD_FORM = {"password": NEW_PASSWORD, "passwordConfirm": NEW_PASSWORD}
# Purpose tokens PocketBase 0.22.34 e-mails before the import, one per kind and
# account, on accounts no other probe uses: account, signing secret (0.22
# settings key).
PURPOSE_TOKENS = {
    "verification": (PROBE, "recordVerificationToken"),
    "password_reset": (PROBE, "recordPasswordResetToken"),
    "email_change_after_reset": (PROBE, "recordEmailChangeToken"),
    "email_change": (PROBE2, "recordEmailChangeToken"),
    "password_reset_after_email_change": (PROBE2, "recordPasswordResetToken"),
    "admin_password_reset": (ADMIN_PROBE_EMAIL, "adminPasswordResetToken"),
}
# Consumed in this order once every other probe is done: by PPBase after the
# import, then by PocketBase restarted on its untouched source, whose answers
# are the reference. A password reset or an email change renews the account
# tokenKey (v0.22.34 forms/record_password_reset_confirm.go:92 ->
# models/record.go:942-962, forms/record_email_change_confirm.go:129-133,
# forms/admin_password_reset_confirm.go:82 -> models/admin.go:45-60), so the
# other token of that account is then refused: both orders, one account each.
# A wrong password first leaves the email change token usable. Step, token,
# path, extra body.
PURPOSE_STEPS = (
    ("verification", "verification", "/api/collections/users/confirm-verification", {}),
    (
        "password_reset",
        "password_reset",
        "/api/collections/users/confirm-password-reset",
        NEW_PASSWORD_FORM,
    ),
    (
        "email_change_after_reset",
        "email_change_after_reset",
        "/api/collections/users/confirm-email-change",
        {"password": NEW_PASSWORD},
    ),
    (
        "email_change_wrong_password",
        "email_change",
        "/api/collections/users/confirm-email-change",
        {"password": "C11-wrong-password"},
    ),
    (
        "email_change",
        "email_change",
        "/api/collections/users/confirm-email-change",
        {"password": PASSWORD},
    ),
    (
        "password_reset_after_email_change",
        "password_reset_after_email_change",
        "/api/collections/users/confirm-password-reset",
        NEW_PASSWORD_FORM,
    ),
    (
        "admin_password_reset",
        "admin_password_reset",
        "/api/admins/confirm-password-reset",
        NEW_PASSWORD_FORM,
    ),
)
# PocketBase 0.22.34 semantics, required of the reference itself.
PURPOSE_EXPECTED = {
    "verification": 204,
    "password_reset": 204,
    "email_change_after_reset": 400,
    "email_change_wrong_password": 400,
    "email_change": 204,
    "password_reset_after_email_change": 400,
    "admin_password_reset": 204,
}
# Logins after the purpose tokens, same calls on both servers (users log in by
# username only: allowEmailAuth is false in the export). Label, path, identity,
# password, PocketBase 0.22.34 status.
PURPOSE_LOGINS = (
    ("probe_new_password", USERS_LOGIN, "c11authprobe", NEW_PASSWORD, 200),
    ("probe_old_password", USERS_LOGIN, "c11authprobe", PASSWORD, 400),
    ("probe2_unchanged_password", USERS_LOGIN, "c11authprobe2", PASSWORD, 200),
    ("admin_probe_new_password", ADMINS_LOGIN, ADMIN_PROBE_EMAIL, NEW_PASSWORD, 200),
    ("admin_probe_old_password", ADMINS_LOGIN, ADMIN_PROBE_EMAIL, ADMIN_PASSWORD, 400),
)
# What the purpose tokens leave on the dedicated accounts in PocketBase 0.22.34
# (record_verification_confirm.go:98, record_password_reset_confirm.go:92-104,
# record_email_change_confirm.go:129-133, models/admin.go:45-60 clears
# lastResetSentAt), required of the reference, then of PPBase.
PURPOSE_EFFECTS = {
    "probe": {
        "verified_before": False,
        "verified_after": True,
        "token_key_renewed": True,
        "password_changed": True,
        "email_changed": False,
    },
    "probe2": {
        "verified_before": False,
        "verified_after": True,
        "token_key_renewed": True,
        "password_changed": False,
        "email_changed": True,
        "email_is_the_requested_one": True,
    },
    "admin_probe": {
        "token_key_renewed": True,
        "password_changed": True,
        "last_reset_sent_at_before": True,
        "last_reset_sent_at_after": False,
    },
}
# Refusal bodies PPBase answers differently (same statuses). Each pinned on both
# sides: PPBase must give its measured value or PocketBase's (then "resolved");
# anything else fails the run. All three come from 0.22.34 validating the whole
# form and returning its errors through the default error handler (apis/base.go
# :70-79, "Something went wrong while processing your request.", api_error.go:43).
PURPOSE_KNOWN_DIVERGENCES = {
    ("email_change_after_reset", "data_codes"): (
        {"password": "validation_invalid_password", "token": "validation_invalid_token"},
        {"token": "validation_invalid_token"},
        "v0.22.34 forms/record_email_change_confirm.go:41-55 validates the password "
        "too and checkPassword :75-87 fails without a valid token; "
        "ppbase/services/record_auth_service.py:914-948 returns the token error alone",
    ),
    ("email_change_wrong_password", "data_codes"): (
        {"password": "validation_invalid_password"},
        {"password": "validation_invalid_credentials"},
        "same check :75-87; ppbase/services/record_auth_service.py:950-957 uses the "
        "code of a failed login",
    ),
    ("password_reset_after_email_change", "message"): (
        "Something went wrong while processing your request.",
        "Failed to confirm password reset.",
        "v0.22.34 apis/record_auth.go:440-460 returns the form error unchanged; "
        "ppbase/api/record_auth.py:1497-1498 sets its own message",
    ),
}


def command(*args, **kwargs):
    return subprocess.check_output(args, text=True, stderr=subprocess.STDOUT, **kwargs).strip()


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def wait_health(process, base_url: str, name: str) -> None:
    async with httpx.AsyncClient() as client:
        for _ in range(300):
            if process.poll() is not None:
                raise RuntimeError(f"{name} startup failed; see the private log")
            try:
                if (await client.get(base_url + "/api/health")).status_code == 200:
                    return
            except httpx.TransportError:
                pass
            await asyncio.sleep(0.1)
    raise TimeoutError(f"{name} startup")


def stop(process) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


class PocketBaseServer:
    """Owned PocketBase process on a random loopback port, always stopped.

    Reusable by the full rehearsal: ``async with PocketBaseServer(...) as pb``
    then ``await pocketbase_tokens(pb.base_url, ...)``. No hooks, no public dir,
    no automigrate; the data directory must be a disposable copy.
    """

    def __init__(self, binary: Path, data: Path, *, root: Path, migrations: Path | None = None):
        self.binary, self.data, self.root, self.migrations = binary, data, root, migrations
        self.port = free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.process = None

    async def __aenter__(self):
        args = [
            str(self.binary),
            "serve",
            "--http",
            f"127.0.0.1:{self.port}",
            "--dir",
            str(self.data),
            "--hooksDir",
            str(self.root / "no-pb-hooks"),
            "--publicDir",
            str(self.root / "no-pb-public"),
            "--automigrate=false",
        ]
        if self.migrations is not None:
            args += ["--migrationsDir", str(self.migrations)]
        self._log = (self.root / f"pocketbase-{self.port}.log").open("w")
        self.process = subprocess.Popen(
            args, cwd=self.root, stdout=self._log, stderr=subprocess.STDOUT
        )
        try:
            await wait_health(self.process, self.base_url, "PocketBase")
        except BaseException:
            stop(self.process)
            self._log.close()
            raise
        return self

    async def __aexit__(self, *exc):
        stop(self.process)
        self._log.close()


async def pocketbase_tokens(base_url: str, *, admin=None, records=()) -> dict:
    """Issue tokens through PocketBase 0.22's own auth routes; memory only.

    admin: (email, password) -> POST /api/admins/auth-with-password.
    records: (label, collection, identity, password) items ->
    POST /api/collections/{collection}/auth-with-password.
    Returns {label: {"token", "id", "collection"}}; "admin" for the admin.
    """
    issued = {}
    async with httpx.AsyncClient(base_url=base_url, timeout=15) as client:
        if admin is not None:
            response = await client.post(
                "/api/admins/auth-with-password",
                json={"identity": admin[0], "password": admin[1]},
            )
            response.raise_for_status()
            body = response.json()
            issued["admin"] = {
                "token": body["token"],
                "id": body["admin"]["id"],
                "collection": None,
            }
        for label, collection, identity, password in records:
            response = await client.post(
                f"/api/collections/{collection}/auth-with-password",
                json={"identity": identity, "password": password},
            )
            response.raise_for_status()
            body = response.json()
            issued[label] = {
                "token": body["token"],
                "id": body["record"]["id"],
                "collection": collection,
            }
    return issued


class SmtpSink:
    """Loopback SMTP sink capturing PocketBase mails (no TLS/AUTH advertised)."""

    def __init__(self):
        self.messages, self.seen, self.server, self.port = [], set(), None, None

    async def start(self):
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def close(self):
        self.server.close()
        await self.server.wait_closed()

    async def _handle(self, reader, writer):
        writer.write(b"220 c11-sink ESMTP\r\n")
        await writer.drain()
        lines, data = [], False
        while line := await reader.readline():
            if data:
                if line.rstrip(b"\r\n") == b".":
                    self.messages.append(b"".join(lines))
                    lines, data = [], False
                    writer.write(b"250 OK\r\n")
                else:
                    lines.append(line[1:] if line.startswith(b"..") else line)
                    continue
            else:
                verb = line[:4].upper()
                if verb in (b"EHLO", b"HELO"):
                    writer.write(b"250 c11-sink\r\n")
                elif verb == b"DATA":
                    data = True
                    writer.write(b"354 End data with <CR><LF>.<CR><LF>\r\n")
                elif verb == b"QUIT":
                    writer.write(b"221 Bye\r\n")
                    await writer.drain()
                    break
                else:
                    writer.write(b"250 OK\r\n")
            await writer.drain()
        writer.close()

    async def token_for(self, recipient: str, timeout: float = 15) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for index, raw in enumerate(self.messages):
                if index in self.seen:
                    continue
                message = email.message_from_bytes(raw, policy=email.policy.default)
                if recipient not in str(message.get("To", "")):
                    continue
                for part in message.walk():
                    if part.get_content_maintype() == "text":
                        found = JWT_RE.search(part.get_content())
                        if found:
                            self.seen.add(index)
                            return found.group(0)
            await asyncio.sleep(0.1)
        raise TimeoutError("No PocketBase e-mail for the synthetic recipient")


def fixture_migration(definitions: list[dict]) -> str:
    body = f"""
    const PW={json.dumps(PASSWORD)};
    const save=(name, data, password)=>{{
      const r=new Record(dao.findCollectionByNameOrId(name),data);
      if(password) r.setPassword(password);
      dao.saveRecord(r); return r;
    }};
    save("roles", {{id:"{ROLE}",name:"C11 auth admin role",
      permission:{{isAdmin:true}},status:true}});
    save("users", {{id:"{USER}",username:"c11authuser",email:"{EMAILS[USER]}",
      role:"{ROLE}",name:"C11 auth user",verified:true}}, PW);
    save("users", {{id:"{REF}",username:"c11authref",email:"{EMAILS[REF]}",
      name:"C11 reference"}}, PW);
    save("users", {{id:"{PROBE}",username:"c11authprobe",email:"{EMAILS[PROBE]}",
      name:"C11 probe"}}, PW);
    save("users", {{id:"{PROBE2}",username:"c11authprobe2",email:"{EMAILS[PROBE2]}",
      name:"C11 probe 2"}}, PW);
    save("agent_users", {{id:"{AGENT}",username:"c11authagent",email:"{EMAILS[AGENT]}"}}, PW);
    save("buma_caisse_access", {{id:"{ACCESS}",user:"{USER}"}});
    save("buma_supplier", {{id:"{SUPPLIER}",firstname:"C11 protected",author:"{USER}",
      status:true}});
    """
    return (
        "migrate((db)=>{const dao=new Dao(db);dao.importCollections("
        + json.dumps(definitions)
        + ".map(c=>new Collection(c)),true);"
        + body
        + "},()=>{});\n"
    )


def build_auth_fixture(root: Path, binary: Path, schema: Path) -> tuple[Path, Path]:
    """PocketBase builds the synthetic source itself; caller owns ``root``."""
    root.mkdir(parents=True, exist_ok=False)
    migrations = root / "migrations"
    migrations.mkdir()
    definitions = json.loads(schema.read_text())
    (migrations / "1789236901_c11_auth_fixture.js").write_text(fixture_migration(definitions))
    data = root / "data"
    command(str(binary), "migrate", "up", "--dir", str(data), "--migrationsDir", str(migrations))
    for address in ADMIN_EMAILS:
        command(str(binary), "admin", "create", address, ADMIN_PASSWORD, "--dir", str(data))
    return data, migrations


def tamper(token: str) -> str:
    header, payload, signature = token.split(".")
    return ".".join([header, payload, ("A" if signature[0] != "A" else "B") + signature[1:]])


def forged_collection(token: str, collection_id: str) -> str:
    """Same signature, claims pointing at a base collection: never verifiable."""
    header, payload, signature = token.split(".")
    claims = jwt.decode(token, options={"verify_signature": False})
    claims["collectionId"] = collection_id
    forged = jwt.utils.base64url_encode(json.dumps(claims, separators=(",", ":")).encode())
    return ".".join([header, forged.decode(), signature])


def claims_shape(token: str) -> dict:
    claims = jwt.decode(token, options={"verify_signature": False})
    return {
        "alg": jwt.get_unverified_header(token)["alg"],
        "claims": sorted(claims),
        "type": claims.get("type"),
    }


def body_of(response):
    try:
        return response.json()
    except ValueError:
        return response.text


def fresh_client(base_url: str) -> httpx.AsyncClient:
    # One connection per request: a server-side failure cannot leak into the next probe.
    return httpx.AsyncClient(
        base_url=base_url, timeout=15, limits=httpx.Limits(max_keepalive_connections=0)
    )


async def run_probes(base_url: str, credentials: dict) -> dict:
    ids = {"ADMIN": credentials["admin_id"], "ADMIN_REF": credentials["admin_ref_id"]}
    results = {}
    async with fresh_client(base_url) as client:
        for label, method, path, credential, expected in PROBES:
            headers = {"Authorization": credentials[credential]} if credential else {}
            try:
                response = await client.request(method, path, headers=headers)
            except httpx.TransportError as exc:
                # Recorded, then failed by compare(): the server dropped the request.
                results[label] = {"status": "transport_error", "error": type(exc).__name__}
                continue
            body = body_of(response)
            result = {"status": response.status_code}
            if isinstance(body, dict):
                result["keys"] = sorted(body)
                if response.status_code >= 400:
                    result["message"] = body.get("message")
                if "token" in body:
                    result["new_token_type"] = claims_shape(body["token"])["type"]
                if expected is not None and response.status_code == 200:
                    wanted = ids.get(expected, expected)
                    if "items" in body:
                        found = {item.get("id") for item in body["items"]}
                        entity = body["items"][0] if body["items"] else {}
                    else:
                        entity = body.get("record") or body.get("admin") or body
                        found = {entity.get("id")}
                    result["expected_id_returned"] = wanted in found
                    result["entity_keys"] = sorted(entity)
            results[label] = result
    return results


async def run_logins(base_url: str) -> dict:
    results = {}
    async with fresh_client(base_url) as client:
        for label, path, identity, password in LOGINS:
            response = await client.post(path, json={"identity": identity, "password": password})
            body = body_of(response)
            result = {"status": response.status_code}
            if isinstance(body, dict):
                result["keys"] = sorted(body)
                if response.status_code == 200:
                    entity = body.get("record") or body.get("admin") or {}
                    result["entity_keys"] = sorted(entity)
                    result["avatar"] = entity.get("avatar")
                    follow = "/api/admins" if "admin" in body else USERS_REFRESH
                    if path.startswith("/api/collections/agent_users"):
                        follow = AGENTS_REFRESH
                    if path.startswith("/api/collections/_superusers"):
                        follow = "/api/admins"
                    check = await client.request(
                        "GET" if follow == "/api/admins" else "POST",
                        follow,
                        headers={"Authorization": body["token"]},
                    )
                    result["new_token_type"] = claims_shape(body["token"])["type"]
                    result["new_token_follow_up_status"] = check.status_code
            results[label] = result
    return results


class RealtimeClient:
    """Minimal SSE subscriber of /api/realtime, both wire formats.

    PocketBase 0.22.34 writes "event:NAME\\ndata:{...}" (apis/realtime.go:93-99),
    PPBase "event: NAME\\ndata: {...}"; one optional space is stripped as in the
    SSE specification. Subscriptions are POSTed like the JS SDK does.
    """

    def __init__(self, base_url: str):
        self.base_url, self.client_id = base_url, None
        self.queue: asyncio.Queue = asyncio.Queue()
        self.names: list[str] = []
        self._http = self._stream = self._reader = None

    async def __aenter__(self):
        self._http = httpx.AsyncClient(base_url=self.base_url, timeout=httpx.Timeout(15, read=None))
        try:
            self._stream = self._http.stream("GET", "/api/realtime")
            response = await self._stream.__aenter__()
            if response.status_code != 200:
                raise RuntimeError(f"realtime connection answered {response.status_code}")
            self._reader = asyncio.create_task(self._read(response))
            name, data = await asyncio.wait_for(self.queue.get(), 15)
            if name != "PB_CONNECT" or not isinstance(data, dict):
                raise RuntimeError("realtime PB_CONNECT event missing")
            self.client_id = data["clientId"]
        except BaseException:
            await self.__aexit__(None, None, None)
            raise
        return self

    async def _read(self, response):
        name, data = None, []
        async for line in response.aiter_lines():
            if line:
                field, _colon, value = line.partition(":")
                value = value[1:] if value.startswith(" ") else value
                if field == "event":
                    name = value
                elif field == "data":
                    data.append(value)
                continue
            if data:
                raw = "\n".join(data)
                try:
                    parsed = json.loads(raw)
                except ValueError:
                    parsed = raw
                await self.queue.put((name, parsed))
            name, data = None, []

    async def wait_for(self, topic: str, action: str, record_id: str, timeout=10.0) -> bool:
        deadline = time.monotonic() + timeout
        while (remaining := deadline - time.monotonic()) > 0:
            try:
                name, data = await asyncio.wait_for(self.queue.get(), remaining)
            except TimeoutError:
                return False
            self.names.append(name)
            if not isinstance(data, dict):
                continue
            record = data.get("record") or {}
            if name == topic and data.get("action") == action and record.get("id") == record_id:
                return True
        return False

    def received(self, topic: str) -> bool:
        while not self.queue.empty():
            self.names.append(self.queue.get_nowait()[0])
        return topic in self.names

    async def subscribe(self, topics, token=None) -> int:
        headers = {"Authorization": token} if token else {}
        async with fresh_client(self.base_url) as client:
            response = await client.post(
                "/api/realtime",
                json={"clientId": self.client_id, "subscriptions": list(topics)},
                headers=headers,
            )
        return response.status_code

    async def __aexit__(self, *exc):
        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, httpx.HTTPError):
                await self._reader
        if self._stream is not None:
            with contextlib.suppress(httpx.HTTPError, RuntimeError):
                await self._stream.__aexit__(None, None, None)
        if self._http is not None:
            await self._http.aclose()


async def refresh(base_url: str, path: str, token: str) -> dict:
    async with fresh_client(base_url) as client:
        response = await client.post(path, headers={"Authorization": token})
    body = body_of(response)
    new = body.get("token") if isinstance(body, dict) else None
    return {
        "status": response.status_code,
        "token": new,
        "type": claims_shape(new)["type"] if new else None,
    }


async def update_supplier(base_url: str, admin: str, firstname: str) -> int:
    async with fresh_client(base_url) as client:
        update = await client.patch(
            SUPPLIER_PATH, headers={"Authorization": admin}, json={"firstname": firstname}
        )
    return update.status_code


async def realtime_probes(base_url: str, credentials: dict, marker: str) -> tuple[dict, dict]:
    """Same calls on PocketBase 0.22.34 (before) and PPBase (after the import).

    The POST bodies are those the JS SDK 0.21 sends (see BOTH_TOPICS). Returns
    (statuses and booleans, tokens kept in memory for the rollback probes).
    """
    users, admin, results = credentials["users"], credentials["admin"], {}
    async with RealtimeClient(base_url) as subscriber, RealtimeClient(base_url) as anonymous:
        results["subscribe_with_legacy_users_token"] = await subscriber.subscribe(
            [REALTIME_TOPIC], users
        )
        results["anonymous_subscribe"] = await anonymous.subscribe([REALTIME_TOPIC])
        results["protected_update_status"] = await update_supplier(
            base_url, admin, f"C11 realtime {marker}"
        )
        results["event_delivered_to_legacy_token_subscriber"] = await subscriber.wait_for(
            REALTIME_TOPIC, "update", SUPPLIER
        )
        await asyncio.sleep(1)
        results["event_delivered_to_anonymous_subscriber"] = anonymous.received(REALTIME_TOPIC)
        refreshed = await refresh(base_url, USERS_REFRESH, users)
        results["legacy_users_token_refresh_status"] = refreshed["status"]
        results["refreshed_token_type"] = refreshed["type"]
        # pb.collection("buma_supplier").subscribe("*") after the refresh.
        results["subscribe_new_topic_same_client_after_refresh"] = await subscriber.subscribe(
            BOTH_TOPICS, refreshed["token"]
        )
        # Measured, not inferred: what the open stream still receives afterwards.
        results["protected_update_after_refresh_status"] = await update_supplier(
            base_url, admin, f"C11 realtime {marker} again"
        )
        results["event_after_refresh_delivered_on_first_topic"] = await subscriber.wait_for(
            REALTIME_TOPIC, "update", SUPPLIER
        )
        await asyncio.sleep(1)
        results["event_after_refresh_delivered_on_new_topic"] = subscriber.received(
            REALTIME_COLLECTION_TOPIC
        )
        # pb.collection("buma_supplier").unsubscribe("*") while the record topic remains.
        results[
            "unsubscribe_one_of_two_topics_same_client_after_refresh"
        ] = await subscriber.subscribe([REALTIME_TOPIC], refreshed["token"])
    async with RealtimeClient(base_url) as client:
        native = await pocketbase_tokens(
            base_url, records=(("native", "users", "c11authuser", PASSWORD),)
        )
        token = native["native"]["token"]
        results["native_token_type"] = claims_shape(token)["type"]
        results["native_subscribe"] = await client.subscribe([REALTIME_TOPIC], token)
        renewed = await refresh(base_url, USERS_REFRESH, token)
        results["native_subscribe_new_topic_same_client_after_refresh"] = await client.subscribe(
            BOTH_TOPICS, renewed["token"]
        )
    async with RealtimeClient(base_url) as client:
        # Subscribed before the login, then a new topic once authenticated.
        results["guest_subscribe"] = await client.subscribe([REALTIME_TOPIC])
        results["guest_then_legacy_token_new_topic_same_client"] = await client.subscribe(
            BOTH_TOPICS, users
        )
    async with RealtimeClient(base_url) as client:
        # authStore cleared (logout), then a new topic on the same stream.
        await client.subscribe([REALTIME_TOPIC], users)
        results["legacy_token_then_guest_new_topic_same_client"] = await client.subscribe(
            BOTH_TOPICS
        )
    async with RealtimeClient(base_url) as client:
        results["admin_subscribe_with_legacy_token"] = await client.subscribe(
            [REALTIME_TOPIC], admin
        )
        admin_refreshed = await refresh(base_url, "/api/admins/auth-refresh", admin)
        results["admin_refresh_status"] = admin_refreshed["status"]
        results["admin_refreshed_token_type"] = admin_refreshed["type"]
        results["admin_subscribe_new_topic_same_client_after_refresh"] = await client.subscribe(
            BOTH_TOPICS, admin_refreshed["token"]
        )
    return results, {"users_refreshed": refreshed["token"]}


async def secret_exposure(base_url: str, admin_token: str, values: list[str]) -> dict:
    """Does a superuser response carry an imported secret? Booleans only."""
    results = {}
    async with fresh_client(base_url) as client:
        for label, path in EXPOSURE_PATHS:
            response = await client.get(path, headers={"Authorization": admin_token})
            result = {
                "status": response.status_code,
                "serves_imported_secret": any(value in response.text for value in values),
            }
            if label == "settings" and response.status_code == 200:
                body = response.json()
                configs = [body[key] for key in TOKEN_KEYS if isinstance(body.get(key), dict)]
                result["token_configs_in_settings"] = len(configs)
                result["token_configs_masked"] = sum(
                    c.get("secret") == SECRET_MASK for c in configs
                )
                result["smtp_password_masked"] = body["smtp"].get("password") == SECRET_MASK
            results[label] = result
    return results


def settings_response_exposure(response, sent_secret: str, secret_values: list[str]) -> dict:
    """PATCH /api/settings answer: does it echo a secret? Booleans and counts only."""
    result = {
        "status": response.status_code,
        "response_serves_sent_secret": sent_secret in response.text,
        "response_serves_imported_secret": any(value in response.text for value in secret_values),
    }
    body = body_of(response)
    if response.status_code == 200 and isinstance(body, dict):
        configs = [body[key] for key in TOKEN_KEYS if isinstance(body.get(key), dict)]
        smtp = body.get("smtp") if isinstance(body.get("smtp"), dict) else {}
        result["token_configs_in_response"] = len(configs)
        result["token_configs_masked"] = sum(c.get("secret") == SECRET_MASK for c in configs)
        result["smtp_password_masked"] = smtp.get("password") == SECRET_MASK
    return result


def new_secret() -> str:
    # PocketBase's own secret shape (security.RandomString(50)); memory only.
    return secrets.token_urlsafe(48)[:50]


async def revocation_probes(base_url: str, credentials: dict, secret_values: list[str]) -> dict:
    """Session revocation paths a PocketBase 0.22.34 administrator has.

    Same calls on PocketBase 0.22.34 (rollback server, last use of its source)
    and on PPBase after the import: per account through a password change, then
    globally through the dashboard's "Invalidate all previously issued tokens",
    which sends a new random secret through PATCH /api/settings (v0.22.34
    ui/src/components/settings/TokenField.svelte:16-29, PageTokenOptions.svelte:50-66;
    apis/settings.go:48-85, forms/settings_upsert.go:56-73, read back by
    apis/middlewares.go:229-249). Statuses and booleans only.
    """
    results = {}
    async with fresh_client(base_url) as client:

        async def status(method: str, path: str, credential: str, **kwargs) -> int:
            headers = {"Authorization": credentials[credential]}
            return (await client.request(method, path, headers=headers, **kwargs)).status_code

        async def login(path: str, identity: str) -> int:
            body = {"identity": identity, "password": REVOKED_PASSWORD}
            return (await client.post(path, json=body)).status_code

        # Per account: a password change renews the tokenKey (v0.22.34
        # forms/record_upsert.go:682-685 -> models/record.go:942-962, no oldPassword
        # with manage access :544-548; models/admin.go:45-61). PPBase: record_service.py
        # :1039-1044, admin_service.py:129-135.
        change = {"password": REVOKED_PASSWORD, "passwordConfirm": REVOKED_PASSWORD}
        results["account_password_change_by_admin"] = await status(
            "PATCH", f"/api/collections/users/records/{REF}", "admin", json=change
        )
        results["account_previous_token_refresh"] = await status(
            "POST", USERS_REFRESH, "ref_after_changes"
        )
        results["account_login_with_new_password"] = await login(USERS_LOGIN, "c11authref")
        results["admin_account_password_change"] = await status(
            "PATCH", f"/api/admins/{credentials['admin_ref_id']}", "admin", json=change
        )
        results["admin_account_previous_token_lists_admins"] = await status(
            "GET", "/api/admins", "admin_ref_after_reset"
        )
        results["admin_account_login_with_new_password"] = await login(
            ADMINS_LOGIN, ADMIN_REF_EMAIL
        )
        # Globally: record sessions, then admin sessions (the admin token stays valid
        # until its own secret changes). A realtime stream opened with the record
        # token before the rotation shows whether revocation cuts open streams.
        async with RealtimeClient(base_url) as stream:
            results["open_stream_subscribe"] = await stream.subscribe(
                [REALTIME_TOPIC], credentials["users"]
            )
            for scope, key, checks in GLOBAL_REVOCATION_CHECKS:
                secret = new_secret()
                response = await client.patch(
                    "/api/settings",
                    headers={"Authorization": credentials["admin"]},
                    json={key: {"secret": secret}},
                )
                results[f"settings_rotate_{scope}_secret"] = settings_response_exposure(
                    response, secret, secret_values
                )
                for name, method, path, credential in checks:
                    results[f"{name}_after_settings_{scope}_rotation"] = await status(
                        method, path, credential
                    )
                if scope == "record":
                    results["open_stream_update_status"] = await update_supplier(
                        base_url, credentials["admin"], "C11 stream after settings rotation"
                    )
                    results[
                        "open_stream_event_after_settings_record_rotation"
                    ] = await stream.wait_for(REALTIME_TOPIC, "update", SUPPLIER)
                    # The SDK's next POST on that stream still carries the old token.
                    results[
                        "open_stream_post_after_settings_record_rotation"
                    ] = await stream.subscribe(BOTH_TOPICS, credentials["users"])
    return results


async def ppbase_revocation_paths(base_url: str, engine, credentials: dict) -> dict:
    """PPBase only, after revocation_probes: the per-collection secrets the import wrote.

    PocketBase 0.22 has no per-collection token option; PPBase keeps the secret in
    _collections.options (ppbase/api/deps.py:85-90, :130-136). Probes the collection
    API in both shapes, then the emergency SQL rotation, without restarting PPBase.
    """
    results = {"collection_api": {}, "emergency_sql": {}}

    async def sessions() -> dict:
        return await pocketbase_tokens(
            base_url,
            admin=(ADMIN_EMAIL, ADMIN_PASSWORD),
            records=(
                ("users", "users", "c11authuser", PASSWORD),
                ("agent", "agent_users", EMAILS[AGENT], PASSWORD),
            ),
        )

    async with fresh_client(base_url) as client:

        async def status(method: str, path: str, token: str) -> int:
            response = await client.request(method, path, headers={"Authorization": token})
            return response.status_code

        # Sessions verified with the collection secret right before each PATCH;
        # _superusers last, nested shape last: an effective rotation revokes the
        # admin session sending the PATCH.
        live = await sessions()
        for name, (method, path, label) in COLLECTION_ROTATION_CHECKS.items():
            for shape, body in COLLECTION_PATCH_SHAPES.items():
                secret = new_secret()
                before = await status(method, path, live[label]["token"])
                response = await client.patch(
                    f"/api/collections/{name}",
                    headers={"Authorization": live["admin"]["token"]},
                    json=body(secret),
                )
                answer = body_of(response)
                results["collection_api"][f"{name}_{shape}"] = {
                    "status": response.status_code,
                    "message": answer.get("message")
                    if response.status_code >= 400 and isinstance(answer, dict)
                    else None,
                    "response_serves_sent_secret": secret in response.text,
                    "session_status_before": before,
                    "session_status_after": await status(method, path, live[label]["token"]),
                }

        fresh = await sessions()
        targets = {
            "legacy_users_refresh": ("POST", USERS_REFRESH, credentials["users"]),
            "legacy_users_is_connected": ("GET", "/isConnected", credentials["users"]),
            "legacy_agent_refresh": ("POST", AGENTS_REFRESH, credentials["agent"]),
            "ppbase_users_refresh": ("POST", USERS_REFRESH, fresh["users"]["token"]),
            "ppbase_agent_refresh": ("POST", AGENTS_REFRESH, fresh["agent"]["token"]),
            "ppbase_admin_lists_admins": ("GET", "/api/admins", fresh["admin"]["token"]),
        }
        before = {label: await status(*target) for label, target in targets.items()}
        read = text(
            "SELECT name, options -> 'authToken' ->> 'secret' AS secret FROM _collections "
            "WHERE type = 'auth' ORDER BY name"
        )
        async with RealtimeClient(base_url) as stream:
            stream_status = await stream.subscribe([REALTIME_TOPIC], fresh["users"]["token"])
            async with engine.begin() as conn:
                old = {r["name"]: r["secret"] for r in (await conn.execute(read)).mappings()}
                rows = (await conn.execute(text(EMERGENCY_SESSION_REVOCATION_SQL))).mappings().all()
                new = {r["name"]: r["secret"] for r in (await conn.execute(read)).mappings()}
            after = {label: await status(*target) for label, target in targets.items()}
            # Raises unless the original passwords still log in.
            logins = await sessions()
            # A value never written before: an update must really change the record.
            stream_update = await update_supplier(
                base_url, logins["admin"]["token"], "C11 stream after emergency rotation"
            )
            stream_event = await stream.wait_for(REALTIME_TOPIC, "update", SUPPLIER)
            # The SDK's next POST on that stream still carries the revoked token.
            stream_post = await stream.subscribe(BOTH_TOPICS, fresh["users"]["token"])
        follow_up = {
            "users_refresh": await status("POST", USERS_REFRESH, logins["users"]["token"]),
            "agent_refresh": await status("POST", AGENTS_REFRESH, logins["agent"]["token"]),
            "admin_lists_admins": await status("GET", "/api/admins", logins["admin"]["token"]),
        }
    results["emergency_sql"] = {
        "ppbase_restarted": False,
        "rows_updated": sorted(row["name"] for row in rows),
        "secret_lengths": sorted({row["secret_length"] for row in rows}),
        "every_secret_changed": set(old) == set(new) and all(old[n] != new[n] for n in old),
        "secrets_distinct_per_collection": len(set(new.values())) == len(new),
        "status_before": before,
        "status_after": after,
        "original_passwords_log_in_after": True,
        "new_sessions_accepted_after": follow_up,
        "open_stream_subscribe_before": stream_status,
        "open_stream_update_status": stream_update,
        "open_stream_event_after_rotation": stream_event,
        "open_stream_post_with_revoked_token": stream_post,
    }
    return results


async def pocketbase_phase(root: Path, binary: Path, schema: Path, report: dict) -> dict:
    data, migrations = build_auth_fixture(root / "fixture", binary, schema)
    sink = SmtpSink()
    await sink.start()
    secrets_seen = {}
    try:
        async with PocketBaseServer(binary, data, root=root, migrations=migrations) as pb:
            issued = await pocketbase_tokens(
                pb.base_url,
                admin=(ADMIN_EMAIL, ADMIN_PASSWORD),
                records=(
                    ("users", "users", "c11authuser", PASSWORD),
                    ("agent", "agent_users", EMAILS[AGENT], PASSWORD),
                    ("probe", "users", "c11authprobe", PASSWORD),
                    ("probe2", "users", "c11authprobe2", PASSWORD),
                ),
            )
            admin = {"Authorization": issued["admin"]["token"]}
            async with httpx.AsyncClient(base_url=pb.base_url, timeout=15) as client:
                # PocketBase 0.22 serialises admin.avatar; it must survive the import.
                avatar = await client.patch(
                    f"/api/admins/{issued['admin']['id']}", headers=admin, json={"avatar": 7}
                )
                assert avatar.status_code == 200 and avatar.json()["avatar"] == 7
                settings = (await client.get("/api/settings", headers=admin)).json()
                durations = {
                    key: settings[key]["duration"] for key in ("recordAuthToken", "adminAuthToken")
                }
                # Synthetic meta/logs/SMTP through PocketBase's own settings API.
                response = await client.patch(
                    "/api/settings",
                    headers=admin,
                    json={
                        "meta": {
                            "appName": "C11 auth synthetic",
                            "appUrl": "http://c11-auth.example.invalid/",
                            "senderName": "C11 sender",
                            "senderAddress": "c11-sender@example.invalid",
                            "hideControls": True,
                        },
                        "logs": {"maxDays": 3, "minLevel": 0, "logIp": False},
                        "smtp": {
                            "enabled": True,
                            "host": "127.0.0.1",
                            "port": sink.port,
                            "tls": False,
                            "localName": "c11-auth.example.invalid",
                        },
                    },
                )
                assert response.status_code == 200, response.text
                # Short-lived tokens genuinely issued by PocketBase, then restored.
                short = {key: {"duration": 5} for key in durations}
                assert (
                    await client.patch("/api/settings", headers=admin, json=short)
                ).status_code == 200
                expiring = await pocketbase_tokens(
                    pb.base_url,
                    admin=(ADMIN_EMAIL, ADMIN_PASSWORD),
                    records=(("users", "users", "c11authuser", PASSWORD),),
                )
                expires_after = time.monotonic() + 7
                restore = {key: {"duration": value} for key, value in durations.items()}
                assert (
                    await client.patch("/api/settings", headers=admin, json=restore)
                ).status_code == 200

                purpose = {}
                reference = {}

                async def post(path, body, headers=None):
                    response = await client.post(path, json=body, headers=headers or {})
                    assert response.status_code in (200, 204), (path, response.status_code)
                    return response.status_code

                # Sessions issued before the flows below renew the tokenKey of REF
                # (password reset, then email change) and of ADMIN_REF (reset).
                before_renewal = await pocketbase_tokens(
                    pb.base_url,
                    admin=(ADMIN_REF_EMAIL, ADMIN_PASSWORD),
                    records=(("ref", "users", "c11authref", PASSWORD),),
                )
                # Reference: PocketBase 0.22.34 accepts its own purpose tokens.
                await post("/api/collections/users/request-verification", {"email": EMAILS[REF]})
                token = await sink.token_for(EMAILS[REF])
                reference["verification"] = await post(
                    "/api/collections/users/confirm-verification", {"token": token}
                )
                await post("/api/collections/users/request-password-reset", {"email": EMAILS[REF]})
                token = await sink.token_for(EMAILS[REF])
                reference["password_reset"] = await post(
                    "/api/collections/users/confirm-password-reset",
                    {"token": token, "password": NEW_PASSWORD, "passwordConfirm": NEW_PASSWORD},
                )
                ref_session = await pocketbase_tokens(
                    pb.base_url, records=(("ref", "users", "c11authref", NEW_PASSWORD),)
                )
                await post(
                    "/api/collections/users/request-email-change",
                    {"newEmail": REF_NEW_EMAIL},
                    {"Authorization": ref_session["ref"]["token"]},
                )
                token = await sink.token_for(REF_NEW_EMAIL)
                reference["email_change"] = await post(
                    "/api/collections/users/confirm-email-change",
                    {"token": token, "password": NEW_PASSWORD},
                )
                await post("/api/admins/request-password-reset", {"email": ADMIN_REF_EMAIL})
                token = await sink.token_for(ADMIN_REF_EMAIL)
                reference["admin_password_reset"] = await post(
                    "/api/admins/confirm-password-reset",
                    {"token": token, "password": NEW_PASSWORD, "passwordConfirm": NEW_PASSWORD},
                )
                after_renewal = await pocketbase_tokens(
                    pb.base_url,
                    admin=(ADMIN_REF_EMAIL, NEW_PASSWORD),
                    records=(("ref", "users", "c11authref", NEW_PASSWORD),),
                )
                # Probe tokens (PURPOSE_TOKENS): issued before the import on the
                # dedicated accounts, every one before any is consumed; consumed
                # after the import, by PPBase then by PocketBase on its source.
                await post("/api/collections/users/request-verification", {"email": EMAILS[PROBE]})
                purpose["verification"] = await sink.token_for(EMAILS[PROBE])
                for account, label in (
                    (PROBE, "password_reset"),
                    (PROBE2, "password_reset_after_email_change"),
                ):
                    await post(
                        "/api/collections/users/request-password-reset", {"email": EMAILS[account]}
                    )
                    purpose[label] = await sink.token_for(EMAILS[account])
                for session, new_email, label in (
                    ("probe", PROBE_NEW_EMAIL, "email_change_after_reset"),
                    ("probe2", PROBE2_NEW_EMAIL, "email_change"),
                ):
                    await post(
                        "/api/collections/users/request-email-change",
                        {"newEmail": new_email},
                        {"Authorization": issued[session]["token"]},
                    )
                    purpose[label] = await sink.token_for(new_email)
                await post("/api/admins/request-password-reset", {"email": ADMIN_PROBE_EMAIL})
                purpose["admin_password_reset"] = await sink.token_for(ADMIN_PROBE_EMAIL)
                file_token = await client.post("/api/files/token", headers=admin)
                purpose["admin_file"] = file_token.json()["token"]
                file_token = await client.post(
                    "/api/files/token", headers={"Authorization": issued["users"]["token"]}
                )
                purpose["users_file"] = file_token.json()["token"]
                # Last mail sent: synthetic SMTP credentials, for the secret probes.
                smtp = await client.patch(
                    "/api/settings",
                    headers=admin,
                    json={"smtp": {"username": SMTP_USERNAME, "password": SMTP_PASSWORD}},
                )
                assert smtp.status_code == 200, smtp.status_code

            await asyncio.sleep(max(0.0, expires_after - time.monotonic()))
            credentials = {
                "admin": issued["admin"]["token"],
                "admin_id": issued["admin"]["id"],
                "users": issued["users"]["token"],
                "agent": issued["agent"]["token"],
                "admin_tampered": tamper(issued["admin"]["token"]),
                "users_tampered": tamper(issued["users"]["token"]),
                "agent_tampered": tamper(issued["agent"]["token"]),
                "admin_expired": expiring["admin"]["token"],
                "users_expired": expiring["users"]["token"],
                "users_forged": forged_collection(issued["users"]["token"], "ui5alk9pbys9wai"),
                "ref_before_reset": before_renewal["ref"]["token"],
                "ref_before_email_change": ref_session["ref"]["token"],
                "ref_after_changes": after_renewal["ref"]["token"],
                "admin_ref_before_reset": before_renewal["admin"]["token"],
                "admin_ref_after_reset": after_renewal["admin"]["token"],
                "admin_ref_id": after_renewal["admin"]["id"],
            }
            realtime, _tokens = await realtime_probes(pb.base_url, credentials, "pocketbase")
            report["pocketbase_reference"] = {
                "probes": await run_probes(pb.base_url, credentials),
                "logins": await run_logins(pb.base_url),
                "purpose_tokens_accepted_by_pocketbase": reference,
                "realtime": realtime,
            }
            # Background mail interceptors persist last*SentAt after sending.
            await asyncio.sleep(1)
    finally:
        await sink.close()
    report["tokens_issued_by_pocketbase"] = {
        "admin_auth": claims_shape(issued["admin"]["token"]),
        "users_auth": claims_shape(issued["users"]["token"]),
        "agent_users_auth": claims_shape(issued["agent"]["token"]),
        **{name: claims_shape(token) for name, token in purpose.items()},
    }
    report["smtp_sink_messages"] = len(sink.messages)
    # Clean stop done; fold the WAL into the synthetic snapshot like c11_fixture.
    with sqlite3.connect(data / "data.db") as db:
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        db.execute("PRAGMA journal_mode=DELETE")
    (data / "storage").mkdir(exist_ok=True)
    secrets_seen.update(credentials=credentials, purpose=purpose)
    return {"data": data, "migrations": migrations, **secrets_seen}


def source_secret_values(data: Path) -> list[str]:
    """Secrets of the synthetic source, in memory only, for the exposure probes."""
    with sqlite3.connect(
        f"{(data / 'data.db').resolve().as_uri()}?mode=ro&immutable=1", uri=True
    ) as db:
        settings = json.loads(
            db.execute("SELECT value FROM _params WHERE key='settings'").fetchone()[0]
        )
    values = [settings[key]["secret"] for key in TOKEN_KEYS] + [settings["smtp"]["password"]]
    assert all(isinstance(value, str) and len(value) >= 20 for value in values)
    return values


def verify_signatures(data: Path, credentials: dict, purpose: dict) -> dict:
    """Check PocketBase's signing key formula on the synthetic source itself."""
    with sqlite3.connect(
        f"{(data / 'data.db').resolve().as_uri()}?mode=ro&immutable=1", uri=True
    ) as db:
        settings = json.loads(
            db.execute("SELECT value FROM _params WHERE key='settings'").fetchone()[0]
        )
        admin_key = db.execute("SELECT tokenKey FROM _admins WHERE email=?", (ADMIN_EMAIL,))
        admin_key = admin_key.fetchone()[0]
        user_key = db.execute("SELECT tokenKey FROM users WHERE id=?", (USER,)).fetchone()[0]
        ref_key = db.execute("SELECT tokenKey FROM users WHERE id=?", (REF,)).fetchone()[0]
        # Signing key of each purpose token's account (PURPOSE_TOKENS).
        purpose_keys = {}
        for table, column, account in (
            ("users", "id", PROBE),
            ("users", "id", PROBE2),
            ("_admins", "email", ADMIN_PROBE_EMAIL),
        ):
            query = f"SELECT tokenKey FROM {table} WHERE {column}=?"
            purpose_keys[account] = db.execute(query, (account,)).fetchone()[0]
        agent_key = db.execute("SELECT tokenKey FROM agent_users WHERE id=?", (AGENT,))
        agent_key = agent_key.fetchone()[0]
        admin_ref_key = db.execute("SELECT tokenKey FROM _admins WHERE email=?", (ADMIN_REF_EMAIL,))
        admin_ref_key = admin_ref_key.fetchone()[0]

    def valid(token, key, setting):
        try:
            jwt.decode(token, key + settings[setting]["secret"], algorithms=["HS256"])
            return True
        except jwt.InvalidTokenError:
            return False

    checks = {
        "admin_auth_tokenKey_plus_adminAuthToken": valid(
            credentials["admin"], admin_key, "adminAuthToken"
        ),
        "users_auth_tokenKey_plus_recordAuthToken": valid(
            credentials["users"], user_key, "recordAuthToken"
        ),
        "agent_auth_tokenKey_plus_recordAuthToken": valid(
            credentials["agent"], agent_key, "recordAuthToken"
        ),
        **{
            f"{label}_tokenKey_plus_{setting}": valid(
                purpose[label], purpose_keys[account], setting
            )
            for label, (account, setting) in PURPOSE_TOKENS.items()
        },
        "admin_file_tokenKey_plus_adminFileToken": valid(
            purpose["admin_file"], admin_key, "adminFileToken"
        ),
        "users_file_tokenKey_plus_recordFileToken": valid(
            purpose["users_file"], user_key, "recordFileToken"
        ),
        "ref_after_changes_current_tokenKey_plus_recordAuthToken": valid(
            credentials["ref_after_changes"], ref_key, "recordAuthToken"
        ),
        "admin_ref_after_reset_current_tokenKey_plus_adminAuthToken": valid(
            credentials["admin_ref_after_reset"], admin_ref_key, "adminAuthToken"
        ),
    }
    negative = {
        "users_auth_with_admin_secret_rejected": not valid(
            credentials["users"], user_key, "adminAuthToken"
        ),
        "admin_auth_with_record_secret_rejected": not valid(
            credentials["admin"], admin_key, "recordAuthToken"
        ),
        # PocketBase renewed these tokenKeys after the tokens were issued.
        "ref_before_reset_not_signed_with_current_tokenKey": not valid(
            credentials["ref_before_reset"], ref_key, "recordAuthToken"
        ),
        "ref_before_email_change_not_signed_with_current_tokenKey": not valid(
            credentials["ref_before_email_change"], ref_key, "recordAuthToken"
        ),
        "admin_ref_before_reset_not_signed_with_current_tokenKey": not valid(
            credentials["admin_ref_before_reset"], admin_ref_key, "adminAuthToken"
        ),
    }
    assert all(checks.values()) and all(negative.values()), (checks, negative)
    return {**checks, **negative}


async def check_schema(url: str, schema: Path) -> dict:
    """114 definitions / 42 views, the verifier used by c11_rehearsal."""
    from scripts.generate_native_baseline import load_schema as translated_schema
    from scripts.verify_complete_schema import read_collections, verify

    result = verify(translated_schema(schema, include_views=True)[0], await read_collections(url))
    if not result["ok"]:
        raise AssertionError("schema verification failed")
    return {"definitions": result["expected"], "views": result["expected_views"], "ok": True}


async def database_state(engine, definitions) -> dict:
    """Comparable state of everything the import may write (synthetic only)."""
    state = {}
    async with engine.connect() as conn:
        for c in definitions:
            if c["type"] != "view":
                rows = await conn.execute(text(f"SELECT * FROM {quote(c['name'])} ORDER BY id"))
                state[c["name"]] = [dict(r) for r in rows.mappings().all()]
        for table in ("_superusers", "_params"):
            rows = await conn.execute(text(f'SELECT * FROM "{table}" ORDER BY id'))
            state[table] = [dict(r) for r in rows.mappings().all()]
        collections = await conn.execute(
            text("SELECT id, name, type, options FROM _collections ORDER BY name")
        )
        state["collections"] = [dict(r) for r in collections.mappings().all()]
        for table in ("_buma_c11_rowids", ARCHIVE_TABLE):
            exists = await conn.execute(text("SELECT to_regclass(:name)"), {"name": table})
            state[table] = exists.scalar_one()
        # Functions and triggers created by the refusal cases must be rolled back too.
        leftovers = await conn.execute(
            text(
                "SELECT (SELECT count(*) FROM pg_proc WHERE left(proname, 4) = 'c11_') + "
                "(SELECT count(*) FROM pg_trigger WHERE left(tgname, 4) = 'c11_')"
            )
        )
        state["c11_functions_and_triggers"] = leftovers.scalar_one()
    return state


async def remove_fresh_seeds(engine, definitions) -> None:
    # Harness only, on the virgin database it just created (as c11_rehearsal):
    # migration seeds would make load_isolated refuse a non-empty target.
    async with engine.begin() as conn:
        for c in definitions:
            if c["type"] != "view":
                await conn.execute(text(f"DELETE FROM {quote(c['name'])}"))


def ppbase_server_environment() -> dict[str, str]:
    """Environment of the isolated PPBase server.

    This project first (its app/ and scripts/), then the caller's PYTHONPATH,
    which selects the PPBase under test (e.g. a PPBase worktree), as
    scripts/c11_idempotency_runtime.server_environment does. Replacing the
    caller's PYTHONPATH would make the server load the venv's editable PPBase
    while this process and its report name another one.
    """
    paths = [str(PROJECT), *filter(None, os.environ.get("PYTHONPATH", "").split(os.pathsep))]
    return {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(dict.fromkeys(paths)),
        "BUMA_MASTER_ENABLE_INTERNAL_JOBS": "false",
        "PPBASE_AUTO_MIGRATE": "false",
        "PPBASE_STORAGE_BACKEND": "local",
    }


def server_ppbase(cwd: Path, env: dict[str, str]) -> dict:
    """Resolve ppbase and app exactly as the server command does (same python, env, cwd).

    The run is valid only if the server loads the PPBase package this process
    loaded (the one ``ppbase_commit`` names) with the WS-A2 corrections, and
    this project's app/.
    """
    import ppbase

    probe = (
        "import importlib.util, json, app, ppbase; print(json.dumps({"
        "'ppbase': ppbase.__file__, 'app': app.__file__, 'secret_redaction': "
        "importlib.util.find_spec('ppbase.services.secret_redaction') is not None}))"
    )
    loaded = json.loads(command(sys.executable, "-c", probe, cwd=cwd, env=env))
    package = Path(loaded["ppbase"]).resolve().parent
    assert package == Path(ppbase.__file__).resolve().parent, (package, ppbase.__file__)
    assert Path(loaded["app"]).resolve().parent.parent == PROJECT, loaded["app"]
    assert loaded["secret_redaction"], "the server PPBase lacks the WS-A2 corrections"
    try:
        commit = command("git", "-C", str(package), "rev-parse", "HEAD")
        changes = command("git", "-C", str(package), "status", "--porcelain", "--", ".")
    except (OSError, subprocess.CalledProcessError):
        commit = changes = None
    return {
        "package_dir": str(package),
        "git_commit": commit,
        "package_tree_clean": changes == "" if changes is not None else None,
        "same_as_bench_process": True,
        "ws_a2_secret_redaction_module": True,
        "app_from_this_project": True,
    }


def live_server_process(pid: int, env: dict[str, str], cwd: Path) -> dict:
    """PYTHONPATH, cwd and command of the running server, read from /proc."""
    proc = Path("/proc") / str(pid)
    try:
        variables = (proc / "environ").read_bytes().decode().split("\0")
        actual_cwd = Path(os.readlink(proc / "cwd"))
        argv = (proc / "cmdline").read_bytes().decode().split("\0")
    except OSError:  # no /proc (e.g. macOS): the probe above remains the evidence
        return {"source": "unavailable"}
    pythonpath = next(
        (item.split("=", 1)[1] for item in variables if item.startswith("PYTHONPATH=")), None
    )
    assert pythonpath == env["PYTHONPATH"], pythonpath
    assert actual_cwd == cwd.resolve(), actual_cwd
    assert argv[:4] == [sys.executable, "-m", "ppbase", "serve"], argv[:4]
    assert "app.hooks.register:register" in argv, argv
    return {
        "source": "/proc/<server pid>",
        "pythonpath_matches_probe": True,
        "cwd_is_the_probe_cwd": True,
        "command": "python -m ppbase serve --hooks app.hooks.register:register",
    }


async def ppbase_server(root: Path, url: str, published: Path, report: dict):
    """BUMA hooks on the PPBase under test; ``report["server_ppbase"]`` names it.

    A fresh cwd keeps any repository .env out of reach (PPBase reads ./.env).
    """
    cwd = root / "ppbase-cwd"
    cwd.mkdir()
    env = ppbase_server_environment()
    report["server_ppbase"] = server_ppbase(cwd, env)
    port = free_port()
    log = (root / "ppbase.log").open("w")
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
    base = f"http://127.0.0.1:{port}"
    try:
        await wait_health(process, base, "PPBase")
        report["server_ppbase"]["live_process"] = live_server_process(process.pid, env, cwd)
    except BaseException:
        stop(process)
        log.close()
        raise
    return process, log, base


async def legacy_probes(base_url: str, credentials: dict) -> dict:
    results = {}
    async with fresh_client(base_url) as client:
        for label, path, credential, status, expected in LEGACY_PROBES:
            response = await client.get(path, headers={"Authorization": credentials[credential]})
            body = response.text if isinstance(expected, str) else body_of(response)
            assert (response.status_code, body) == (status, expected), (label, response.status_code)
            results[label] = {"status": response.status_code, "body_matches_contract": True}
    return results


async def purpose_probes(base_url: str, purpose: dict) -> dict:
    """PURPOSE_STEPS in order: status, message and error codes (no value)."""
    results = {}
    async with fresh_client(base_url) as client:
        for name, token, path, extra in PURPOSE_STEPS:
            response = await client.post(path, json={"token": purpose[token], **extra})
            body = body_of(response)
            data = body.get("data") if isinstance(body, dict) else None
            results[name] = {
                "status": response.status_code,
                "message": body.get("message") if isinstance(body, dict) else None,
                "data_codes": {
                    field: error.get("code")
                    for field, error in sorted(data.items())
                    if isinstance(error, dict)
                }
                if isinstance(data, dict)
                else None,
            }
    return results


async def purpose_logins(base_url: str) -> dict:
    results = {}
    async with fresh_client(base_url) as client:
        for label, path, identity, password, _status in PURPOSE_LOGINS:
            response = await client.post(path, json={"identity": identity, "password": password})
            results[label] = response.status_code
    return results


def pocketbase_purpose_accounts(data: Path) -> dict:
    """The dedicated purpose-token accounts in a PocketBase data.db; memory only.

    Read with PocketBase stopped (synthetic copy owned by this run).
    """
    with contextlib.closing(sqlite3.connect(data / "data.db")) as db:
        users = {
            row[0]: {
                "verified": bool(row[1]),
                "token_key": row[2],
                "password_hash": row[3],
                "email": row[4],
            }
            for row in db.execute(
                "SELECT id, verified, tokenKey, passwordHash, email FROM users WHERE id IN (?, ?)",
                (PROBE, PROBE2),
            )
        }
        admin = db.execute(
            "SELECT tokenKey, passwordHash, lastResetSentAt FROM _admins WHERE email = ?",
            (ADMIN_PROBE_EMAIL,),
        ).fetchone()
    return {
        "probe": users[PROBE],
        "probe2": users[PROBE2],
        # "" is PocketBase's zero DateTime: no reset e-mail pending.
        "admin_probe": {"token_key": admin[0], "password_hash": admin[1], "reset": admin[2] != ""},
    }


async def ppbase_purpose_accounts(engine) -> dict:
    """Same accounts in the PPBase database; memory only."""
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT id, verified, token_key, password_hash, email FROM users "
                "WHERE id IN (:probe, :probe2)"
            ),
            {"probe": PROBE, "probe2": PROBE2},
        )
        users = {row["id"]: row for row in rows.mappings().all()}
        admin = (
            (
                await conn.execute(
                    text(
                        'SELECT token_key, password_hash, last_reset_sent_at FROM "_superusers" '
                        "WHERE email = :email"
                    ),
                    {"email": ADMIN_PROBE_EMAIL},
                )
            )
            .mappings()
            .one()
        )
    return {
        **{
            name: {
                "verified": users[account]["verified"],
                "token_key": users[account]["token_key"],
                "password_hash": users[account]["password_hash"],
                "email": users[account]["email"],
            }
            for name, account in (("probe", PROBE), ("probe2", PROBE2))
        },
        "admin_probe": {
            "token_key": admin["token_key"],
            "password_hash": admin["password_hash"],
            "reset": admin["last_reset_sent_at"] is not None,
        },
    }


def purpose_effects(before: dict, after: dict) -> dict:
    """What the purpose tokens changed on one server: booleans only."""
    effects = {
        name: {
            "verified_before": before[name]["verified"],
            "verified_after": after[name]["verified"],
            "token_key_renewed": after[name]["token_key"] != before[name]["token_key"],
            "password_changed": after[name]["password_hash"] != before[name]["password_hash"],
            "email_changed": after[name]["email"] != before[name]["email"],
        }
        for name in ("probe", "probe2")
    }
    effects["probe2"]["email_is_the_requested_one"] = after["probe2"]["email"] == PROBE2_NEW_EMAIL
    admin_before, admin_after = before["admin_probe"], after["admin_probe"]
    effects["admin_probe"] = {
        "token_key_renewed": admin_after["token_key"] != admin_before["token_key"],
        "password_changed": admin_after["password_hash"] != admin_before["password_hash"],
        "last_reset_sent_at_before": admin_before["reset"],
        "last_reset_sent_at_after": admin_after["reset"],
    }
    return effects


def pb_date(value: datetime) -> str:
    """PostgreSQL timestamptz back to PocketBase's stored text (UTC, milliseconds)."""
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] + "Z"


async def compare_imported(engine, data: Path) -> dict:
    """Readback against the synthetic source, booleans only in the report."""
    with sqlite3.connect(
        f"{(data / 'data.db').resolve().as_uri()}?mode=ro&immutable=1", uri=True
    ) as db:
        db.row_factory = sqlite3.Row
        admins = [dict(r) for r in db.execute("SELECT _rowid_ AS rid, * FROM _admins")]
        param = dict(
            db.execute(
                "SELECT id, value, created, updated FROM _params WHERE key='settings'"
            ).fetchone()
        )
        settings = json.loads(param["value"])
        users = {
            r["id"]: dict(r) for r in db.execute("SELECT id, tokenKey, passwordHash FROM users")
        }
        agents = {
            r["id"]: dict(r)
            for r in db.execute("SELECT id, tokenKey, passwordHash FROM agent_users")
        }
    async with engine.connect() as conn:
        targets = {
            r["id"]: dict(r)
            for r in (await conn.execute(text("SELECT * FROM _superusers"))).mappings().all()
        }
        options = {
            r["name"]: r["options"]
            for r in (
                await conn.execute(text("SELECT name, options FROM _collections WHERE type='auth'"))
            )
            .mappings()
            .all()
        }
        stored_row = (
            (
                await conn.execute(
                    text("SELECT id, value, created, updated FROM _params WHERE key='settings'")
                )
            )
            .mappings()
            .one()
        )
        stored = stored_row["value"]
        archive = (await conn.execute(text(f'SELECT * FROM "{ARCHIVE_TABLE}"'))).mappings().all()
        imported_users = {
            r["id"]: dict(r)
            for r in (await conn.execute(text("SELECT id, token_key, password_hash FROM users")))
            .mappings()
            .all()
        }
        imported_agents = {
            r["id"]: dict(r)
            for r in (
                await conn.execute(text("SELECT id, token_key, password_hash FROM agent_users"))
            )
            .mappings()
            .all()
        }
    record_pairs = {
        "authToken": "recordAuthToken",
        "passwordResetToken": "recordPasswordResetToken",
        "emailChangeToken": "recordEmailChangeToken",
        "verificationToken": "recordVerificationToken",
        "fileToken": "recordFileToken",
    }
    admin_pairs = {
        "authToken": "adminAuthToken",
        "passwordResetToken": "adminPasswordResetToken",
        "fileToken": "adminFileToken",
    }
    checks = {
        "admins_count": len(targets) == len(admins) == len(ADMIN_EMAILS),
        "admin_hash_tokenKey_email_avatar_dates_identical": all(
            targets[a["id"]]["password_hash"] == a["passwordHash"]
            and targets[a["id"]]["token_key"] == a["tokenKey"]
            and targets[a["id"]]["email"] == a["email"]
            and targets[a["id"]]["avatar"] == a["avatar"]
            and pb_date(targets[a["id"]]["created"]) == a["created"]
            and pb_date(targets[a["id"]]["updated"]) == a["updated"]
            for a in admins
        ),
        "settings_row_id_and_dates_identical": stored_row["id"] == param["id"]
        and pb_date(stored_row["created"]) == param["created"]
        and pb_date(stored_row["updated"]) == param["updated"],
        # Same instant, "" (no reset e-mail pending) as NULL: the 120 s throttle
        # of the 0.22 admin reset route PPBase now serves.
        "admin_last_reset_sent_at_imported": all(
            targets[a["id"]]["last_reset_sent_at"] == date_value(a["lastResetSentAt"])
            for a in admins
        ),
        "admin_with_pending_reset_imported_non_null": any(
            t["last_reset_sent_at"] is not None for t in targets.values()
        ),
        "admin_without_pending_reset_imported_null": any(
            t["last_reset_sent_at"] is None for t in targets.values()
        ),
        "admin_rowids_and_last_reset_archived": sorted(
            (r["record_id"], r["source_rowid"], r["legacy"]["lastResetSentAt"])
            for r in archive
            if r["source_table"] == "_admins"
        )
        == sorted((a["id"], a["rid"], a["lastResetSentAt"]) for a in admins),
        "admin_with_pending_reset_archived_non_empty": any(
            r["legacy"].get("lastResetSentAt") for r in archive if r["source_table"] == "_admins"
        ),
        "record_token_configs_identical_in_users_and_agent_users": all(
            options[name][kind] == settings[key]
            for name in ("users", "agent_users")
            for kind, key in record_pairs.items()
        ),
        "admin_token_configs_identical_in_superusers": all(
            options["_superusers"][kind] == settings[key] for kind, key in admin_pairs.items()
        ),
        "superusers_verification_and_email_change_kept_ppbase_secrets": all(
            options["_superusers"][kind]["secret"]
            not in {settings[key]["secret"] for key in record_pairs.values()}
            for kind in ("verificationToken", "emailChangeToken")
        ),
        "settings_meta_logs_smtp_mapped": all(
            stored["meta"][k] == settings["meta"][k]
            for k in ("appName", "senderName", "senderAddress", "hideControls")
        )
        and stored["meta"]["appURL"] == settings["meta"]["appUrl"].removesuffix("/")
        and all(stored["logs"][k] == settings["logs"][k] for k in ("maxDays", "minLevel"))
        and stored["logs"]["logIP"] == settings["logs"]["logIp"]
        and all(stored["smtp"][k] == v for k, v in settings["smtp"].items()),
        "settings_s3_and_backups_left_at_ppbase_defaults": stored["s3"]["enabled"] is False
        and stored["backups"]["cron"] == "",
        "users_tokenKey_and_hash_identical": set(imported_users) == set(users)
        and all(
            imported_users[i]["token_key"] == u["tokenKey"]
            and imported_users[i]["password_hash"] == u["passwordHash"]
            for i, u in users.items()
        ),
        "agent_users_tokenKey_and_hash_identical": set(imported_agents) == set(agents)
        and all(
            imported_agents[i]["token_key"] == a["tokenKey"]
            and imported_agents[i]["password_hash"] == a["passwordHash"]
            for i, a in agents.items()
        ),
    }
    assert all(checks.values()), {k: v for k, v in checks.items() if not v}
    return checks


def trigger(name: str, table: str, event: str, body: str) -> tuple[str, str]:
    """Synthetic BEFORE trigger, created and rolled back inside one refusal case."""
    return (
        f"CREATE FUNCTION c11_{name}() RETURNS trigger LANGUAGE plpgsql AS "
        f"$$ BEGIN {body} RETURN NEW; END $$",
        f"CREATE TRIGGER c11_{name} BEFORE {event} ON {table} "
        f"FOR EACH ROW EXECUTE FUNCTION c11_{name}()",
    )


# label, SQL run after load_isolated in the same transaction, expected refusal.
REFUSAL_CASES = (
    (
        "settings_row_already_present",
        (
            'INSERT INTO "_params" ("id", "key", "value", "created", "updated") '
            "VALUES ('c11presettings1', 'settings', '{}'::jsonb, now(), now())",
        ),
        "settings_target_not_empty",
    ),
    (
        "archive_table_already_present",
        (f'CREATE TABLE "{ARCHIVE_TABLE}" (c11 integer)',),
        "auth_archive_exists",
    ),
    (
        "records_from_another_snapshot",
        (
            "UPDATE _buma_c11_rowids SET source_rowid = source_rowid + 1000000 "
            f"WHERE record_id = '{USER}'",
        ),
        "records_snapshot_mismatch",
    ),
    (
        "auth_collection_set_changed",
        ("UPDATE _collections SET type = 'base' WHERE name = 'agent_users'",),
        "target_auth_collections_mismatch",
    ),
    (
        "auth_collection_id_changed",
        ("UPDATE _collections SET id = 'c11otherauthid1' WHERE name = 'agent_users'",),
        "target_auth_collection_id_mismatch",
    ),
    (
        "superusers_readback_altered",
        trigger("superusers", '"_superusers"', "INSERT", "NEW.avatar := NEW.avatar + 1;"),
        "superusers_readback_mismatch",
    ),
    (
        "options_readback_altered",
        trigger(
            "options",
            '"_collections"',
            "UPDATE",
            "NEW.options := NEW.options || '{\"c11Altered\": true}'::jsonb;",
        ),
        "auth_options_readback_mismatch",
    ),
    (
        "settings_readback_altered",
        trigger(
            "settings",
            '"_params"',
            "INSERT",
            "NEW.value := NEW.value || '{\"c11Altered\": true}'::jsonb;",
        ),
        "settings_readback_mismatch",
    ),
    (
        "database_error_during_admin_insert",
        trigger(
            "failure",
            '"_superusers"',
            "INSERT",
            "RAISE EXCEPTION 'c11 synthetic database failure';",
        ),
        "auth_import_database_error",
    ),
)


def synthetic_admin_values(data: Path) -> list[str]:
    """Hash, tokenKey and email of the synthetic admins: must never leak in errors."""
    with sqlite3.connect(
        f"{(data / 'data.db').resolve().as_uri()}?mode=ro&immutable=1", uri=True
    ) as db:
        rows = db.execute("SELECT passwordHash, tokenKey, email FROM _admins").fetchall()
    return [value for row in rows for value in row]


async def refusal_cases(engine, url, root, data, staged_report, definitions, baseline) -> dict:
    """Every database-side refusal of import_auth, each rolled back to the baseline."""
    staged = root / "refusals"
    stage_snapshot(data, staged, staged_report)
    leaked = synthetic_admin_values(staged)
    results = {}
    try:
        for label, statements, code in REFUSAL_CASES:
            started = time.monotonic()
            try:
                async with engine.begin() as conn:
                    await load_isolated(conn, staged, definitions)
                    for statement in statements:
                        await conn.execute(text(statement))
                    await import_auth(conn, staged, definitions)
            except ImportBlocked as exc:
                assert str(exc) == code, (label, str(exc))
                result = {"refused_with": str(exc)}
                if code == "auth_import_database_error":
                    rendered = "".join(traceback.format_exception(exc))
                    result["sqlstate"] = exc.sqlstate
                    result["context_chained"] = exc.__context__ is not None
                    result["values_in_message_or_traceback"] = any(
                        value in rendered for value in leaked
                    ) or ("parameters" in rendered)
                    assert exc.sqlstate == "P0001" and not result["context_chained"]
                    assert not result["values_in_message_or_traceback"]
            else:
                raise AssertionError(f"{label} was not refused")
            assert await database_state(engine, definitions) == baseline, label
            result["state_after_rollback_equals_baseline"] = True
            result["seconds"] = round(time.monotonic() - started, 2)
            results[label] = result

        # SQLAlchemy echoes bound values unless hide_parameters=True: refused first.
        echoing = create_async_engine(url, connect_args={"ssl": "disable"})
        try:
            async with echoing.begin() as conn:
                await import_auth(conn, staged, definitions)
        except ImportBlocked as exc:
            assert str(exc) == "engine_hide_parameters_required", exc
            results["engine_without_hide_parameters"] = {"refused_with": str(exc)}
        else:
            raise AssertionError("an engine echoing bound values was accepted")
        finally:
            await echoing.dispose()
        assert await database_state(engine, definitions) == baseline
        results["engine_without_hide_parameters"]["state_after_rollback_equals_baseline"] = True
    finally:
        shutil.rmtree(staged)
    return results


async def postgres_phase(root, url, data, schema, credentials, purpose, secret_values, report):
    """Import into the disposable PostgreSQL, then probe PPBase; returns PPBase tokens."""
    from ppbase.services.database_preparation import prepare_database

    definitions = load_schema(schema)
    diagnostic = diagnose(data, schema, source_version=SOURCE_VERSION)
    # 0.22.34 is a validated source version (D-0024): no blocker is waived.
    assert diagnostic["ready_for_isolated_rehearsal"], [b["code"] for b in diagnostic["blockers"]]
    assert diagnostic["source_version_validated"] is True, diagnostic["source_version_evidence"]
    report["version_gate"] = {
        "source_version": SOURCE_VERSION,
        "validated": True,
        "evidence": diagnostic["source_version_evidence"],
        "blockers": len(diagnostic["blockers"]),
        "waiver_applied": False,
        "rows": diagnostic["rows"],
    }
    # Hashes, tokenKeys and secrets are bound parameters: never echo them.
    engine = create_async_engine(url, connect_args={"ssl": "disable"}, hide_parameters=True)
    try:
        async with engine.connect() as conn:
            tables = "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'"
            assert (await conn.execute(text(tables))).scalar_one() == 0
        report["migrations_applied"] = len(
            await prepare_database(engine, PROJECT / "pb_migrations")
        )
        report["schema_before"] = await check_schema(url, schema)
        seeded = await database_state(engine, definitions)
        assert not seeded["_superusers"] and not seeded["_params"]
        assert seeded["c11_functions_and_triggers"] == 0
        await remove_fresh_seeds(engine, definitions)
        baseline = await database_state(engine, definitions)

        # The single entry point with a failure injected in each accounts stage:
        # records, accounts, options, settings, archives and staging copy go.
        rollback = {}
        for stage in FAIL_STAGES:
            staged = root / f"failed-{stage}"
            try:
                await import_snapshot(
                    engine, data, staged, diagnostic, definitions, auth_fail_after=stage
                )
            except ImportBlocked as exc:
                assert str(exc) == "injected_auth_import_failure", exc
            else:
                raise AssertionError("auth failure was not injected")
            assert not staged.exists(), stage
            assert await database_state(engine, definitions) == baseline, stage
            rollback[stage] = "records_accounts_options_settings_archives_and_staging_rolled_back"
        report["rollback_in_import_snapshot_transaction"] = rollback

        # import_auth's own guards, below the entry point.
        orphan = root / "auth-without-records"
        stage_snapshot(data, orphan, diagnostic)
        try:
            async with engine.begin() as conn:
                await import_auth(conn, orphan, definitions)
        except ImportBlocked as exc:
            assert str(exc) == "records_import_required", exc
            report["auth_without_records"] = str(exc)
        else:
            raise AssertionError("accounts were imported without their records")
        finally:
            shutil.rmtree(orphan)
        assert await database_state(engine, definitions) == baseline
        report["refusals_in_load_isolated_transaction"] = await refusal_cases(
            engine, url, root, data, diagnostic, definitions, baseline
        )

        published = root / "imported"
        evidence = {}
        loaded = await import_snapshot(
            engine, data, published, diagnostic, definitions, report=evidence
        )
        auth_report = evidence["auth"]
        assert auth_report["records_snapshot"]["rows"] == loaded, auth_report["records_snapshot"]
        assert evidence["single_transaction"] == {
            "records": loaded,
            "relation_arrays_recounted": len(evidence["relation_arrays"]),
            "accounts_and_settings": True,
            "committed": True,
        }, evidence["single_transaction"]
        report["import"] = {
            "function": "scripts.c11_import.import_snapshot",
            "records_loaded": loaded,
            "single_transaction": evidence["single_transaction"],
            "auth": auth_report,
        }
        report["readback"] = await compare_imported(engine, published)
        async with engine.connect() as conn:
            exists = (await conn.execute(text("SELECT to_regclass('_requests')"))).scalar_one()
            logs = 0
            if exists is not None:
                logs = (await conn.execute(text('SELECT count(*) FROM "_requests"'))).scalar_one()
            report["logs_db"] = {
                "present_in_source": (data / "logs.db").exists(),
                "staged": (published / "logs.db").exists(),
                "ppbase_request_logs_after_import": logs,
            }
        assert report["logs_db"] == {
            "present_in_source": True,
            "staged": False,
            "ppbase_request_logs_after_import": 0,
        }

        # A second import waits for the lock held by a concurrent writer, then refuses.
        imported_state = await database_state(engine, definitions)
        holding = asyncio.Event()

        async def hold_lock():
            async with engine.begin() as conn:
                await conn.execute(text('LOCK TABLE "_superusers" IN ACCESS EXCLUSIVE MODE'))
                holding.set()
                await asyncio.sleep(1.0)

        async def second_import():
            await holding.wait()
            started = time.monotonic()
            try:
                async with engine.begin() as conn:
                    await import_auth(conn, published, definitions)
            except ImportBlocked as exc:
                return str(exc), time.monotonic() - started
            return "imported_twice", time.monotonic() - started

        _held, (outcome, waited) = await asyncio.gather(hold_lock(), second_import())
        assert outcome == "superusers_target_not_empty" and waited >= 0.8, (outcome, waited)
        assert await database_state(engine, definitions) == imported_state
        report["second_import"] = {
            "outcome": outcome,
            "waited_for_lock_seconds": round(waited, 2),
            "no_duplicate_or_merge": True,
        }
        report["schema_after"] = await check_schema(url, schema)

        # The purpose-token accounts, as imported: the very state of the source.
        purpose_before = await ppbase_purpose_accounts(engine)
        report["purpose_accounts_imported_identically"] = purpose_before == (
            pocketbase_purpose_accounts(data)
        )
        assert report["purpose_accounts_imported_identically"]

        process, log, base = await ppbase_server(root, url, published, report)
        try:
            after_import = {
                "probes": await run_probes(base, credentials),
                "legacy_probes": await legacy_probes(base, credentials),
                "logins": await run_logins(base),
            }
            after_import["realtime"], realtime_tokens = await realtime_probes(
                base, credentials, "ppbase"
            )
            after_import["secret_exposure"] = await secret_exposure(
                base, credentials["admin"], secret_values
            )
            # Sessions PPBase issues after the cutover, probed on the rollback path.
            issued = await pocketbase_tokens(
                base,
                admin=(ADMIN_EMAIL, ADMIN_PASSWORD),
                records=(
                    ("users", "users", "c11authuser", PASSWORD),
                    ("agent", "agent_users", EMAILS[AGENT], PASSWORD),
                ),
            )
            # Changes passwords, settings and collection secrets on purpose.
            after_import["revocation"] = await revocation_probes(base, credentials, secret_values)
            after_import["revocation_ppbase_only"] = await ppbase_revocation_paths(
                base, engine, credentials
            )
            # Last, in PocketBase's order: they renew tokenKeys, change passwords
            # and an e-mail, on accounts no other probe uses.
            after_import["purpose_tokens_from_pocketbase"] = await purpose_probes(base, purpose)
            after_import["purpose_logins"] = await purpose_logins(base)
            report["ppbase_after_import"] = after_import
        finally:
            stop(process)
            log.close()
        after_import["purpose_token_effects"] = purpose_effects(
            purpose_before, await ppbase_purpose_accounts(engine)
        )
    finally:
        await engine.dispose()
    return {
        "admin": issued["admin"]["token"],
        "users": issued["users"]["token"],
        "agent": issued["agent"]["token"],
        "users_refreshed": realtime_tokens["users_refreshed"],
    }


# label, method, path, credential source, credential.
ROLLBACK_PROBES = (
    ("pre_import_users_token_refresh", "POST", USERS_REFRESH, "pocketbase", "users"),
    ("pre_import_admin_token_lists_admins", "GET", "/api/admins", "pocketbase", "admin"),
    ("ppbase_users_token_refresh", "POST", USERS_REFRESH, "ppbase", "users"),
    ("ppbase_users_token_protected_supplier", "GET", SUPPLIER_PATH, "ppbase", "users"),
    ("ppbase_agent_token_refresh", "POST", AGENTS_REFRESH, "ppbase", "agent"),
    (
        "ppbase_refreshed_legacy_users_token_refresh",
        "POST",
        USERS_REFRESH,
        "ppbase",
        "users_refreshed",
    ),
    ("ppbase_admin_token_lists_admins", "GET", "/api/admins", "ppbase", "admin"),
)


async def rollback_phase(
    binary, data, root, migrations, credentials, issued, secret_values, purpose
):
    """PocketBase restarted on its untouched source: the cutover rollback path.

    Last use of the source, then the reference of the PPBase phase: the same
    revocation paths, then the same purpose tokens in the same order.
    """
    tokens = {"pocketbase": credentials, "ppbase": issued}
    probes = {}
    before = pocketbase_purpose_accounts(data)
    async with PocketBaseServer(binary, data, root=root, migrations=migrations) as pb:
        async with fresh_client(pb.base_url) as client:
            for label, method, path, source, name in ROLLBACK_PROBES:
                token = tokens[source][name]
                response = await client.request(method, path, headers={"Authorization": token})
                probes[label] = {
                    "status": response.status_code,
                    "token_type": claims_shape(token)["type"],
                }
        exposure = await secret_exposure(pb.base_url, credentials["admin"], secret_values)
        # Changes passwords and settings secrets on purpose.
        revocation = await revocation_probes(pb.base_url, credentials, secret_values)
        purpose_tokens = await purpose_probes(pb.base_url, purpose)
        logins = await purpose_logins(pb.base_url)
    return {
        "probes": probes,
        "secret_exposure": exposure,
        "revocation": revocation,
        "purpose_tokens": purpose_tokens,
        "purpose_logins": logins,
        # Read once the server has stopped.
        "purpose_token_effects": purpose_effects(before, pocketbase_purpose_accounts(data)),
    }


def compare_probes(report: dict) -> tuple[dict, list]:
    """Every probe: PocketBase 0.22.34's status, envelope and message required."""
    reference = report["pocketbase_reference"]["probes"]
    ppbase = report["ppbase_after_import"]["probes"]
    comparison, messages = {}, []
    for label, *_rest in PROBES:
        pb_status, pp_status = reference[label]["status"], ppbase[label]["status"]
        comparison[label] = {"pocketbase": pb_status, "ppbase": pp_status}
        if label in ACCEPTED:
            assert pb_status == pp_status == 200, (label, pb_status, pp_status)
            assert ppbase[label]["expected_id_returned"], label
            assert reference[label]["expected_id_returned"], label
            # Same envelope and entity fields as PocketBase 0.22.34 returns.
            assert ppbase[label]["keys"] == reference[label]["keys"], label
            assert ppbase[label]["entity_keys"] == reference[label]["entity_keys"], label
        else:
            assert pb_status >= 400 and pp_status == pb_status, (label, pb_status, pp_status)
        if reference[label].get("message") != ppbase[label].get("message"):
            messages.append(
                {
                    "probe": label,
                    "pocketbase": reference[label].get("message"),
                    "ppbase": ppbase[label].get("message"),
                }
            )
    return comparison, messages


def compare_realtime(report: dict) -> dict:
    pocketbase = report["pocketbase_reference"]["realtime"]
    ppbase = report["ppbase_after_import"]["realtime"]
    # Nothing measured escapes the comparison.
    assert set(pocketbase) == set(ppbase) == {*REALTIME_SAME, *REALTIME_TOKEN_TYPES}, sorted(
        set(pocketbase) ^ set(ppbase)
    )
    # PocketBase 0.22.34 reference behaviour (apis/realtime.go:187-203).
    assert pocketbase["subscribe_with_legacy_users_token"] == 204, pocketbase
    assert pocketbase["event_delivered_to_legacy_token_subscriber"] is True, pocketbase
    assert pocketbase["event_delivered_to_anonymous_subscriber"] is False, pocketbase
    assert pocketbase["subscribe_new_topic_same_client_after_refresh"] == 204, pocketbase
    assert pocketbase["event_after_refresh_delivered_on_first_topic"] is True, pocketbase
    assert pocketbase["event_after_refresh_delivered_on_new_topic"] is True, pocketbase
    assert pocketbase["unsubscribe_one_of_two_topics_same_client_after_refresh"] == 204
    assert pocketbase["guest_then_legacy_token_new_topic_same_client"] == 204, pocketbase
    assert pocketbase["legacy_token_then_guest_new_topic_same_client"] == 403, pocketbase
    for key in REALTIME_SAME:
        assert pocketbase[key] == ppbase[key], (key, pocketbase[key], ppbase[key])
    return {key: {"pocketbase": pocketbase[key], "ppbase": ppbase[key]} for key in pocketbase}


def compare_exposure(report: dict) -> dict:
    pocketbase = report["pocketbase_restarted_on_source"]["secret_exposure"]
    ppbase = report["ppbase_after_import"]["secret_exposure"]
    # v0.22.34 apis/settings.go:29-30 serves RedactClone (models/settings/settings.go:281-334);
    # collections carry no token secret in 0.22.
    assert not any(value["serves_imported_secret"] for value in pocketbase.values()), pocketbase
    settings = pocketbase["settings"]
    assert settings["token_configs_in_settings"] == settings["token_configs_masked"] == 8
    assert settings["smtp_password_masked"] is True
    exposure = {
        label: {
            "status": {
                "pocketbase": pocketbase[label]["status"],
                "ppbase": ppbase[label]["status"],
            },
            "serves_imported_secret": {
                "pocketbase": pocketbase[label]["serves_imported_secret"],
                "ppbase": ppbase[label]["serves_imported_secret"],
            },
        }
        for label, _path in EXPOSURE_PATHS
    }
    # PPBase serves no secret either: collections without token or OAuth2
    # secrets, settings masked (ppbase/services/secret_redaction.py, used by
    # ppbase/models/collection.py:235 and ppbase/api/settings.py:381, :479).
    assert not any(value["serves_imported_secret"] for value in ppbase.values()), ppbase
    assert ppbase["settings"]["smtp_password_masked"] is True, ppbase["settings"]
    assert (
        ppbase["settings"]["token_configs_masked"]
        == ppbase["settings"]["token_configs_in_settings"]
    ), ppbase["settings"]
    # PATCH /api/settings answers RedactClone too (v0.22.34 apis/settings.go:75-80),
    # PPBase the same masked shape (ppbase/api/settings.py:478-479).
    pb_revocation = report["pocketbase_restarted_on_source"]["revocation"]
    pp_revocation = report["ppbase_after_import"]["revocation"]
    for scope, _key, _checks in GLOBAL_REVOCATION_CHECKS:
        label = f"settings_rotate_{scope}_secret"
        reference = pb_revocation[label]
        assert reference["status"] == 200 and reference["token_configs_masked"] == 8, reference
        assert reference["smtp_password_masked"] is True, reference
        assert not reference["response_serves_sent_secret"], reference
        assert not reference["response_serves_imported_secret"], reference
        answer = pp_revocation[label]
        assert answer["status"] == 200, answer
        assert not answer["response_serves_sent_secret"], answer
        assert not answer["response_serves_imported_secret"], answer
        assert answer["smtp_password_masked"] is True, answer
        assert answer["token_configs_masked"] == answer["token_configs_in_response"], answer
        exposure[f"patch_{label}"] = {
            key: {"pocketbase": reference.get(key), "ppbase": pp_revocation[label].get(key)}
            for key in (
                "status",
                "response_serves_sent_secret",
                "response_serves_imported_secret",
                "token_configs_in_response",
                "token_configs_masked",
                "smtp_password_masked",
            )
        }
    return exposure


def compare_revocation(report: dict) -> tuple[dict, list, dict]:
    """Revocation paths: PocketBase 0.22.34 reference, PPBase measured."""
    pocketbase = report["pocketbase_restarted_on_source"]["revocation"]
    ppbase = report["ppbase_after_import"]["revocation"]
    only = report["ppbase_after_import"]["revocation_ppbase_only"]
    assert pocketbase["account_password_change_by_admin"] == 200, pocketbase
    assert pocketbase["account_previous_token_refresh"] == 401, pocketbase
    assert pocketbase["account_login_with_new_password"] == 200, pocketbase
    assert pocketbase["admin_account_password_change"] == 200, pocketbase
    assert pocketbase["admin_account_previous_token_lists_admins"] == 401, pocketbase
    assert pocketbase["admin_account_login_with_new_password"] == 200, pocketbase
    for key in REVOCATION_SAME:
        assert pocketbase[key] == ppbase[key], (key, pocketbase[key], ppbase[key])
    divergences = []
    assert pocketbase["open_stream_subscribe"] == 204, pocketbase
    assert pocketbase["open_stream_event_after_settings_record_rotation"] is True, pocketbase
    for key, cause in REVOCATION_KNOWN_DIVERGENCES.items():
        # The rotated secret really revoked the sessions on PocketBase.
        assert pocketbase[key] in (401, 403, 404), (key, pocketbase[key])
        assert ppbase[key] in (200, 204, pocketbase[key]), (key, ppbase[key])
        divergences.append(
            {
                "probe": key,
                "pocketbase_status": pocketbase[key],
                "ppbase_status": ppbase[key],
                "resolved": pocketbase[key] == ppbase[key],
                "cause": cause,
            }
        )
    collection_api = only["collection_api"]
    for label, result in collection_api.items():
        assert result["session_status_before"] == 200, (label, result)
        assert not result["response_serves_sent_secret"], label
    emergency = only["emergency_sql"]
    assert emergency["rows_updated"] == ["_superusers", "agent_users", "users"], emergency
    assert emergency["secret_lengths"] == [64], emergency
    assert emergency["every_secret_changed"] and emergency["secrets_distinct_per_collection"]
    for label in ("ppbase_users_refresh", "ppbase_agent_refresh", "ppbase_admin_lists_admins"):
        assert emergency["status_before"][label] == 200, (label, emergency)
    # Every session verified with the old secrets is refused, without a restart
    # (/isConnected keeps its C3 contract: 400 {"message": "false"}).
    expected_after = {label: 401 for label in emergency["status_after"]}
    expected_after["legacy_users_is_connected"] = 400
    assert emergency["status_after"] == expected_after, emergency
    assert set(emergency["new_sessions_accepted_after"].values()) == {200}, emergency
    # Same as PocketBase after its own global revocation (revocation_probes): the
    # open stream keeps its events, its next POST with the revoked token gets 403.
    assert emergency["open_stream_subscribe_before"] == 204, emergency
    assert emergency["open_stream_update_status"] == 200, emergency
    assert emergency["open_stream_event_after_rotation"] is True, emergency
    assert emergency["open_stream_post_with_revoked_token"] == 403, emergency
    assert pocketbase["open_stream_post_after_settings_record_rotation"] == 403, pocketbase
    comparison = {
        key: {"pocketbase": pocketbase[key], "ppbase": ppbase[key]}
        for key in (*REVOCATION_SAME, *REVOCATION_KNOWN_DIVERGENCES)
    }
    return comparison, divergences, only


def compare(report: dict) -> None:
    reference = report["pocketbase_reference"]["probes"]
    ppbase = report["ppbase_after_import"]["probes"]
    comparison, messages = compare_probes(report)
    # PocketBase 0.22.34 wording everywhere (apis/middlewares.go:94-125).
    assert messages == [], messages
    logins_pb = report["pocketbase_reference"]["logins"]
    logins_pp = report["ppbase_after_import"]["logins"]
    for label, *_rest in LOGINS:
        if label == "superusers_collection":
            continue
        assert logins_pb[label]["status"] == logins_pp[label]["status"], label
        if logins_pb[label]["status"] == 200:
            assert logins_pp[label]["new_token_follow_up_status"] == 200, label
            assert logins_pp[label]["keys"] == logins_pb[label]["keys"], label
            assert logins_pp[label]["entity_keys"] == logins_pb[label]["entity_keys"], label
            assert logins_pp[label]["avatar"] == logins_pb[label]["avatar"], label
    flows = report["pocketbase_reference"]["purpose_tokens_accepted_by_pocketbase"]
    assert all(status == 204 for status in flows.values()), flows
    # The purpose tokens issued before the import: PocketBase restarted on its
    # source is the reference (status, message and error codes), then PPBase.
    source = report["pocketbase_restarted_on_source"]
    purpose_pb, purpose_pp = (
        source["purpose_tokens"],
        report["ppbase_after_import"]["purpose_tokens_from_pocketbase"],
    )
    assert {name: r["status"] for name, r in purpose_pb.items()} == PURPOSE_EXPECTED, purpose_pb
    assert list(purpose_pp) == list(purpose_pb), list(purpose_pp)
    purpose_divergences = []
    for name, result in purpose_pp.items():
        assert result["status"] == purpose_pb[name]["status"], (name, result, purpose_pb[name])
        for field in ("message", "data_codes"):
            reference_value, value = purpose_pb[name][field], result[field]
            known = PURPOSE_KNOWN_DIVERGENCES.get((name, field))
            if known is None:
                assert value == reference_value, (name, field, value, reference_value)
                continue
            pinned_pocketbase, pinned_ppbase, cause = known
            assert reference_value == pinned_pocketbase, (name, field, reference_value)
            assert value in (pinned_ppbase, reference_value), (name, field, value)
            purpose_divergences.append(
                {
                    "step": name,
                    "field": field,
                    "pocketbase": reference_value,
                    "ppbase": value,
                    "resolved": value == reference_value,
                    "cause": cause,
                }
            )
    expected_logins = {label: status for label, *_rest, status in PURPOSE_LOGINS}
    assert source["purpose_logins"] == expected_logins, source["purpose_logins"]
    assert report["ppbase_after_import"]["purpose_logins"] == source["purpose_logins"]
    effects_pb = source["purpose_token_effects"]
    effects_pp = report["ppbase_after_import"]["purpose_token_effects"]
    assert effects_pb == PURPOSE_EFFECTS, effects_pb
    assert effects_pp == effects_pb, (effects_pp, effects_pb)
    envelope = {
        "pocketbase": sorted(
            {tuple(v.get("keys", ())) for k, v in reference.items() if k not in ACCEPTED}
        ),
        "ppbase": sorted(
            {tuple(v.get("keys", ())) for k, v in ppbase.items() if k not in ACCEPTED}
        ),
    }
    # BUMA enables PPBase's PocketBase 0.22 error mode (D-0034): the error bodies
    # must carry the same keys as 0.22.34's ({code, message, data}).
    assert envelope["pocketbase"] == envelope["ppbase"], envelope
    realtime = compare_realtime(report)
    # C3 claims: PocketBase 0.22.34 issues type=authRecord (tokens/tokens.go:4-7),
    # PPBase issues type=auth after its own login or refresh.
    token_type = {
        "pocketbase_login": logins_pb["users_by_username"]["new_token_type"],
        "pocketbase_refresh": reference["users_refresh"]["new_token_type"],
        "ppbase_login": logins_pp["users_by_username"]["new_token_type"],
        "ppbase_refresh": ppbase["users_refresh"]["new_token_type"],
        "ppbase_admin_refresh": ppbase["admin_refresh"]["new_token_type"],
    }
    assert token_type["pocketbase_login"] == token_type["pocketbase_refresh"] == "authRecord"
    assert reference["admin_refresh"]["new_token_type"] == "admin"
    # js-sdk v0.21.5 src/stores/BaseAuthStore.ts:44-53 reads the type claim.
    token_type["js_sdk_0_21_isAuthRecord_after_refresh"] = {
        "pocketbase": token_type["pocketbase_refresh"] == "authRecord",
        "ppbase": token_type["ppbase_refresh"] == "authRecord",
    }
    token_type["js_sdk_0_21_isAdmin_after_admin_refresh"] = {
        "pocketbase": reference["admin_refresh"]["new_token_type"] == "admin",
        "ppbase": token_type["ppbase_admin_refresh"] == "admin",
    }
    token_type["resolved"] = token_type["pocketbase_refresh"] == token_type["ppbase_refresh"]
    exposure = compare_exposure(report)
    revocation, revocation_divergences, revocation_ppbase = compare_revocation(report)
    collection_api = revocation_ppbase["collection_api"]
    rollback = report["pocketbase_restarted_on_source"]["probes"]
    assert rollback["pre_import_users_token_refresh"]["status"] == 200, rollback
    assert rollback["pre_import_admin_token_lists_admins"]["status"] == 200, rollback
    # v0.22.34 apis/middlewares.go:229-249 loads only type=admin|authRecord tokens.
    for key in (
        "ppbase_users_token_refresh",
        "ppbase_agent_token_refresh",
        "ppbase_refreshed_legacy_users_token_refresh",
    ):
        assert rollback[key]["status"] == 401 and rollback[key]["token_type"] == "auth", key
    assert rollback["ppbase_users_token_protected_supplier"]["status"] == 404, rollback
    refusals = report["refusals_in_load_isolated_transaction"]
    assert all(value["state_after_rollback_equals_baseline"] for value in refusals.values())
    report["comparison"] = {
        "probes": comparison,
        "same_status": sum(v["pocketbase"] == v["ppbase"] for v in comparison.values()),
        "total": len(comparison),
        "message_wording_differences": messages,
        "error_envelope_keys": {
            key: [list(item) for item in value] for key, value in envelope.items()
        },
        "purpose_tokens_own_flows_on_pocketbase": flows,
        "purpose_tokens": {
            name: {"pocketbase": purpose_pb[name], "ppbase": purpose_pp[name]}
            for name in purpose_pb
        },
        "purpose_logins": {
            label: {
                "pocketbase": status,
                "ppbase": report["ppbase_after_import"]["purpose_logins"][label],
            }
            for label, status in source["purpose_logins"].items()
        },
        "purpose_token_effects": {"pocketbase": effects_pb, "ppbase": effects_pp},
        "purpose_token_refusal_body_known_divergences": purpose_divergences,
        "realtime": realtime,
        "realtime_same": sum(
            v["pocketbase"] == v["ppbase"] for k, v in realtime.items() if k in REALTIME_SAME
        ),
        "realtime_total": len(REALTIME_SAME),
        "token_type_after_login_or_refresh": token_type,
        "secret_exposure_to_superusers": exposure,
        "rollback_to_pocketbase_sessions": {
            key: {"status": value["status"], "token_type": value["token_type"]}
            for key, value in rollback.items()
        },
        "session_revocation": revocation,
        "session_revocation_known_divergences": revocation_divergences,
        "session_revocation_ppbase_collection_api": collection_api,
        "session_revocation_emergency_sql": revocation_ppbase["emergency_sql"],
    }
    effective = {
        label: result["session_status_after"] == 401 for label, result in collection_api.items()
    }
    report["summary"] = {
        "pre_import_tokens_accepted_after_import": {
            "admin": all(comparison[k]["ppbase"] == 200 for k in ACCEPTED if k.startswith("admin")),
            "users": all(comparison[k]["ppbase"] == 200 for k in ACCEPTED if k.startswith("users")),
            "agent_users": all(
                comparison[k]["ppbase"] == 200 for k in ACCEPTED if k.startswith("agent")
            ),
        },
        "tokens_invalidated_by_pocketbase_tokenKey_renewal_still_refused": all(
            comparison[k]["pocketbase"] == comparison[k]["ppbase"] >= 400
            for k in TOKEN_KEY_RENEWAL_PROBES
        ),
        "original_passwords_accepted_after_import": all(
            logins_pp[k]["status"] == 200
            for k in ("users_by_username", "agent_by_email", "agent_by_username", "admin_by_email")
        ),
        "passwords_changed_in_pocketbase_accepted_after_import": all(
            logins_pp[k]["status"] == 200 for k in ("users_reset_password", "admin_reset_password")
        ),
        "rejections_with_pocketbase_status": sum(
            comparison[k]["pocketbase"] == comparison[k]["ppbase"]
            for k in comparison
            if k not in ACCEPTED
        ),
        "rejections_total": len(comparison) - len(ACCEPTED),
        "status_or_wording_differences_with_pocketbase": len(messages)
        + sum(v["pocketbase"] != v["ppbase"] for v in comparison.values()),
        "purpose_tokens_issued_before_import_continued": all(
            purpose_pp[name]["status"] == 204
            for name, status in PURPOSE_EXPECTED.items()
            if status == 204
        ),
        # Statuses, effects and logins identical; bodies apart from the pinned list.
        "purpose_tokens_statuses_effects_and_logins_like_pocketbase": True,
        "purpose_token_refusal_body_differences": sum(
            not item["resolved"] for item in purpose_divergences
        ),
        "admin_password_reset_route_like_pocketbase": purpose_pp["admin_password_reset"]
        == purpose_pb["admin_password_reset"],
        "buma_legacy_probes_match_contract": True,
        "realtime_matches_pocketbase": True,
        "realtime_new_topic_after_ppbase_refresh_matches_pocketbase": realtime[
            "subscribe_new_topic_same_client_after_refresh"
        ]["ppbase"]
        == 204,
        "js_sdk_0_21_isAuthRecord_after_ppbase_refresh": token_type[
            "js_sdk_0_21_isAuthRecord_after_refresh"
        ]["ppbase"],
        "ppbase_serves_imported_secrets_to_superusers": any(
            exposure[label]["serves_imported_secret"]["ppbase"] for label, _path in EXPOSURE_PATHS
        ),
        "ppbase_settings_patch_response_serves_secrets": any(
            exposure[f"patch_settings_rotate_{scope}_secret"][key]["ppbase"]
            for scope, _key, _checks in GLOBAL_REVOCATION_CHECKS
            for key in ("response_serves_sent_secret", "response_serves_imported_secret")
        ),
        "per_account_revocation_matches_pocketbase": True,
        "global_revocation_by_settings_patch_matches_pocketbase": all(
            item["resolved"] for item in revocation_divergences
        ),
        "collection_api_rotation_effective": effective,
        "emergency_sql_revocation_immediate_without_restart": True,
        "rollback_pocketbase_accepts_ppbase_record_tokens": False,
        "rollback_pocketbase_accepts_ppbase_admin_token": rollback[
            "ppbase_admin_token_lists_admins"
        ]["status"]
        == 200,
        "import_refusals_rolled_back": len(refusals),
    }


async def exercise(root: Path, binary: Path, schema: Path, report: dict, start_postgres) -> None:
    pocketbase = await pocketbase_phase(root, binary, schema, report)
    report["pocketbase_signing_key_checks"] = verify_signatures(
        pocketbase["data"], pocketbase["credentials"], pocketbase["purpose"]
    )
    secret_values = source_secret_values(pocketbase["data"])
    url = start_postgres()
    issued = await postgres_phase(
        root,
        url,
        pocketbase["data"],
        schema,
        pocketbase["credentials"],
        pocketbase["purpose"],
        secret_values,
        report,
    )
    report["pocketbase_restarted_on_source"] = await rollback_phase(
        binary,
        pocketbase["data"],
        root,
        pocketbase["migrations"],
        pocketbase["credentials"],
        issued,
        secret_values,
        pocketbase["purpose"],
    )
    compare(report)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pocketbase", type=Path, required=True)
    parser.add_argument("--schema", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument(
        "--private-logs",
        type=Path,
        help="New private directory receiving the PocketBase/PPBase logs (synthetic data)",
    )
    args = parser.parse_args()
    if args.report.exists():
        parser.error("Report must be a new path")
    if args.private_logs is not None and args.private_logs.exists():
        parser.error("Private log directory must be a new path")
    if command(str(args.pocketbase), "--version") != f"pocketbase version {SOURCE_VERSION}":
        parser.error(f"This proof requires PocketBase {SOURCE_VERSION}")
    name = "buma-c11-ws-c-" + uuid.uuid4().hex[:12]
    password = secrets.token_hex(16)
    report = {
        "timestamp_utc": datetime.now(UTC).isoformat(),
        "scope": "c11_ws_c_auth_settings_session_continuity_synthetic",
        "production_data_used": False,
        "pocketbase": {"version": SOURCE_VERSION, "binary_sha256": digest(args.pocketbase)},
        "ppbase_commit": command(
            "git", "-C", str(Path(__import__("ppbase").__file__).parents[1]), "rev-parse", "HEAD"
        ),
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
            "buma.scope=c11-ws-c-auth-runtime",
            "--publish",
            "127.0.0.1::5432",
            "--env",
            "POSTGRES_USER=c11",
            "--env",
            f"POSTGRES_PASSWORD={password}",
            "--env",
            "POSTGRES_DB=buma_c11_rehearsal",
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
        return f"postgresql+asyncpg://c11:{password}@127.0.0.1:{port}/buma_c11_rehearsal"

    try:
        with tempfile.TemporaryDirectory(prefix="buma-c11-ws-c-auth-") as folder:
            try:
                asyncio.run(
                    exercise(
                        Path(folder),
                        args.pocketbase.resolve(),
                        args.schema.resolve(),
                        report,
                        start_postgres,
                    )
                )
                report["status"] = "passed"
            finally:
                if args.private_logs is not None:
                    args.private_logs.mkdir(mode=0o700)
                    for log in Path(folder).glob("*.log"):
                        shutil.copyfile(log, args.private_logs / log.name)
                        (args.private_logs / log.name).chmod(0o600)
    except BaseException as exc:
        # Never leak tokens, secrets, hashes or bind parameters into the report.
        report["failure_type"] = type(exc).__name__
        raise
    finally:
        if created:
            command("docker", "stop", "--time", "5", name)
        report["cleanup"] = "owned_container_processes_and_temporary_files_removed"
        write_json_new(args.report, report)
        print(json.dumps({"status": report["status"], "report": str(args.report)}))


if __name__ == "__main__":
    main()
