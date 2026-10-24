"""WS-C runtime proof: PocketBase 0.22.34 sessions survive the C11 import.

Synthetic only, on owned disposable resources. PocketBase 0.22.34 builds its
own fixture (the corrected export imported like scripts/c11_fixture.py, admins
created by ``pocketbase admin create``, users/agent_users with known passwords),
issues tokens over HTTP and e-mails purpose tokens to a local SMTP sink. After
a clean stop the snapshot goes through diagnose, stage_snapshot, load_isolated
and import_auth in one transaction of a disposable PostgreSQL 16 container,
then PPBase serves it with the BUMA hooks and the same probes run again.
There is no --db/--data argument: no supplied backup or database is touched.

python -m scripts.c11_auth_runtime --pocketbase /path/to/pocketbase-0.22.34 \
    --schema /path/to/export.json --report /new/report.json
"""

from __future__ import annotations

import argparse
import asyncio
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
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx
import jwt
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from scripts.c11_auth_import import ARCHIVE_TABLE, import_auth
from scripts.c11_import import (
    ImportBlocked,
    diagnose,
    digest,
    load_isolated,
    load_schema,
    quote,
    stage_snapshot,
    write_json_new,
)

PROJECT = Path(__file__).resolve().parents[1]
SOURCE_VERSION = "0.22.34"
VERSION_GATE = "source_pocketbase_version_unvalidated"
PASSWORD = "C11-auth-password"
NEW_PASSWORD = "C11-auth-password-new"
ADMIN_PASSWORD = "C11-admin-password"
ADMIN_EMAIL = "c11-admin@example.invalid"
ADMIN_REF_EMAIL = "c11-admin-ref@example.invalid"
ROLE, USER, REF, PROBE = "c11authrole0001", "c11authuser0001", "c11authref00001", "c11authprobe001"
AGENT, ACCESS, SUPPLIER = "c11authagent001", "c11authaccess01", "c11authsupp0001"
EMAILS = {
    USER: "c11-user@example.invalid",
    REF: "c11-ref@example.invalid",
    PROBE: "c11-probe@example.invalid",
    AGENT: "c11-agent@example.invalid",
}
REF_NEW_EMAIL, PROBE_NEW_EMAIL = "c11-ref-new@example.invalid", "c11-probe-new@example.invalid"
USERS_REFRESH = "/api/collections/users/auth-refresh"
AGENTS_REFRESH = "/api/collections/agent_users/auth-refresh"
SUPPLIER_PATH = f"/api/collections/buma_supplier/records/{SUPPLIER}"
ACCESS_PATH = f"/api/collections/buma_caisse_access/records/{ACCESS}"
AGENT_PATH = f"/api/collections/agent_users/records/{AGENT}"
JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")

# label, method, path, credential. Identical against PocketBase and PPBase.
PROBES = (
    ("admin_lists_admins", "GET", "/api/admins", "admin", "ADMIN"),
    ("admin_refresh", "POST", "/api/admins/auth-refresh", "admin", "ADMIN"),
    ("admin_reads_protected_supplier", "GET", SUPPLIER_PATH, "admin", SUPPLIER),
    ("users_refresh", "POST", USERS_REFRESH, "users", USER),
    ("users_reads_admin_role_caisse_access", "GET", ACCESS_PATH, "users", ACCESS),
    ("users_reads_protected_supplier", "GET", SUPPLIER_PATH, "users", SUPPLIER),
    ("agent_refresh", "POST", AGENTS_REFRESH, "agent", AGENT),
    ("agent_reads_own_record", "GET", AGENT_PATH, "agent", AGENT),
    ("agent_reads_protected_supplier", "GET", SUPPLIER_PATH, "agent", SUPPLIER),
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
)
ACCEPTED = {label for label, *_rest in PROBES[:9]}
# Independent of the import (same on any PPBase database); proposed fixes in
# docs/contracts/c11-auth-import.md. Any other status difference fails the run.
KNOWN_PPBASE_DIVERGENCES = {
    "admin_token_on_users_refresh": (
        "ppbase/api/record_auth.py:1122-1126 answers 403 to an admin token; PocketBase "
        "v0.22.34 apis/middlewares.go:97-100 RequireSameContextRecordAuth answers 401"
    ),
    "forged_base_collection_protected_supplier": (
        "ppbase/api/deps.py:112-127 selects token_key before checking the collection "
        "type: 500; PocketBase v0.22.34 daos/record.go:427-434 ignores the token (anonymous)"
    ),
    "forged_base_collection_users_refresh": (
        "same ppbase/api/deps.py:112-127 defect; PocketBase v0.22.34 answers 401"
    ),
}
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
    for address in (ADMIN_EMAIL, ADMIN_REF_EMAIL):
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
                    wanted = credentials["admin_id"] if expected == "ADMIN" else expected
                    if "items" in body:
                        found = {item.get("id") for item in body["items"]}
                    else:
                        found = {(body.get("record") or body.get("admin") or body).get("id")}
                    result["expected_id_returned"] = wanted in found
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
                # Probe tokens: issued before the import, never consumed by PocketBase.
                await post("/api/collections/users/request-verification", {"email": EMAILS[PROBE]})
                purpose["verification"] = await sink.token_for(EMAILS[PROBE])
                await post(
                    "/api/collections/users/request-password-reset", {"email": EMAILS[PROBE]}
                )
                purpose["password_reset"] = await sink.token_for(EMAILS[PROBE])
                await post(
                    "/api/collections/users/request-email-change",
                    {"newEmail": PROBE_NEW_EMAIL},
                    {"Authorization": issued["probe"]["token"]},
                )
                purpose["email_change"] = await sink.token_for(PROBE_NEW_EMAIL)
                await post("/api/admins/request-password-reset", {"email": ADMIN_EMAIL})
                purpose["admin_password_reset"] = await sink.token_for(ADMIN_EMAIL)
                file_token = await client.post("/api/files/token", headers=admin)
                purpose["admin_file"] = file_token.json()["token"]
                file_token = await client.post(
                    "/api/files/token", headers={"Authorization": issued["users"]["token"]}
                )
                purpose["users_file"] = file_token.json()["token"]

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
            }
            report["pocketbase_reference"] = {
                "probes": await run_probes(pb.base_url, credentials),
                "logins": await run_logins(pb.base_url),
                "purpose_tokens_accepted_by_pocketbase": reference,
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
    return {"data": data, **secrets_seen}


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
        probe_key = db.execute("SELECT tokenKey FROM users WHERE id=?", (PROBE,)).fetchone()[0]
        agent_key = db.execute("SELECT tokenKey FROM agent_users WHERE id=?", (AGENT,))
        agent_key = agent_key.fetchone()[0]

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
        "verification_tokenKey_plus_recordVerificationToken": valid(
            purpose["verification"], probe_key, "recordVerificationToken"
        ),
        "password_reset_tokenKey_plus_recordPasswordResetToken": valid(
            purpose["password_reset"], probe_key, "recordPasswordResetToken"
        ),
        "email_change_tokenKey_plus_recordEmailChangeToken": valid(
            purpose["email_change"], probe_key, "recordEmailChangeToken"
        ),
        "admin_reset_tokenKey_plus_adminPasswordResetToken": valid(
            purpose["admin_password_reset"], admin_key, "adminPasswordResetToken"
        ),
        "admin_file_tokenKey_plus_adminFileToken": valid(
            purpose["admin_file"], admin_key, "adminFileToken"
        ),
        "users_file_tokenKey_plus_recordFileToken": valid(
            purpose["users_file"], user_key, "recordFileToken"
        ),
    }
    negative = {
        "users_auth_with_admin_secret_rejected": not valid(
            credentials["users"], user_key, "adminAuthToken"
        ),
        "admin_auth_with_record_secret_rejected": not valid(
            credentials["admin"], admin_key, "recordAuthToken"
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
        options = await conn.execute(
            text("SELECT name, options FROM _collections WHERE type='auth' ORDER BY name")
        )
        state["auth_options"] = [dict(r) for r in options.mappings().all()]
        for table in ("_buma_c11_rowids", ARCHIVE_TABLE):
            exists = await conn.execute(text("SELECT to_regclass(:name)"), {"name": table})
            state[table] = exists.scalar_one()
    return state


async def remove_fresh_seeds(conn, definitions) -> None:
    # Harness only, on the virgin database it just created (as c11_rehearsal).
    for c in definitions:
        if c["type"] != "view":
            await conn.execute(text(f"DELETE FROM {quote(c['name'])}"))


async def ppbase_server(root: Path, url: str, published: Path):
    port = free_port()
    env = {
        **os.environ,
        "PYTHONPATH": str(PROJECT),
        "BUMA_MASTER_ENABLE_INTERNAL_JOBS": "false",
        "PPBASE_AUTO_MIGRATE": "false",
        "PPBASE_STORAGE_BACKEND": "local",
    }
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
        cwd=PROJECT,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        await wait_health(process, base, "PPBase")
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
    calls = {
        "verification": ("/api/collections/users/confirm-verification", {}),
        "password_reset": (
            "/api/collections/users/confirm-password-reset",
            {"password": NEW_PASSWORD, "passwordConfirm": NEW_PASSWORD},
        ),
        "email_change": ("/api/collections/users/confirm-email-change", {"password": PASSWORD}),
        "admin_password_reset": (
            "/api/admins/confirm-password-reset",
            {"password": NEW_PASSWORD, "passwordConfirm": NEW_PASSWORD},
        ),
    }
    results = {}
    async with fresh_client(base_url) as client:
        for name, (path, extra) in calls.items():
            response = await client.post(path, json={"token": purpose[name], **extra})
            body = body_of(response)
            results[name] = {
                "status": response.status_code,
                "message": body.get("message") if isinstance(body, dict) else None,
            }
    return results


async def compare_imported(engine, data: Path) -> dict:
    """Readback against the synthetic source, booleans only in the report."""
    with sqlite3.connect(
        f"{(data / 'data.db').resolve().as_uri()}?mode=ro&immutable=1", uri=True
    ) as db:
        db.row_factory = sqlite3.Row
        admins = [dict(r) for r in db.execute("SELECT _rowid_ AS rid, * FROM _admins")]
        settings = json.loads(
            db.execute("SELECT value FROM _params WHERE key='settings'").fetchone()[0]
        )
        users = {
            r["id"]: dict(r) for r in db.execute("SELECT id, tokenKey, passwordHash FROM users")
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
        stored = (
            await conn.execute(text("SELECT value FROM _params WHERE key='settings'"))
        ).scalar_one()
        archive = (await conn.execute(text(f'SELECT * FROM "{ARCHIVE_TABLE}"'))).mappings().all()
        imported_users = {
            r["id"]: dict(r)
            for r in (await conn.execute(text("SELECT id, token_key, password_hash FROM users")))
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
        "admins_count": len(targets) == len(admins) == 2,
        "admin_hash_tokenKey_email_avatar_dates_identical": all(
            targets[a["id"]]["password_hash"] == a["passwordHash"]
            and targets[a["id"]]["token_key"] == a["tokenKey"]
            and targets[a["id"]]["email"] == a["email"]
            and targets[a["id"]]["avatar"] == a["avatar"]
            and targets[a["id"]]["created"].strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] + "Z"
            == a["created"]
            for a in admins
        ),
        "admin_last_reset_sent_at_not_activated": all(
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
        "settings_meta_logs_smtp_mapped": stored["meta"]["appName"] == settings["meta"]["appName"]
        and stored["meta"]["appURL"] == settings["meta"]["appUrl"].removesuffix("/")
        and stored["logs"]["logIP"] == settings["logs"]["logIp"]
        and all(stored["smtp"][k] == v for k, v in settings["smtp"].items()),
        "settings_s3_and_backups_left_at_ppbase_defaults": stored["s3"]["enabled"] is False
        and stored["backups"]["cron"] == "",
        "users_tokenKey_and_hash_identical": all(
            imported_users[i]["token_key"] == u["tokenKey"]
            and imported_users[i]["password_hash"] == u["passwordHash"]
            for i, u in users.items()
        ),
    }
    assert all(checks.values()), {k: v for k, v in checks.items() if not v}
    return checks


async def postgres_phase(root, url, data, schema, credentials, purpose, report):
    from ppbase.services.database_preparation import prepare_database

    definitions = load_schema(schema)
    diagnostic = diagnose(data, schema, source_version=SOURCE_VERSION)
    codes = {b["code"] for b in diagnostic["blockers"]}
    assert codes <= {VERSION_GATE}, codes
    # WS-D owns the 0.22.34 version gate; this synthetic auth proof records the
    # waiver instead of guessing a version. Any other blocker stops the run.
    staged_report = {
        **diagnostic,
        "ready_for_isolated_rehearsal": True,
        "c11_auth_runtime_waived_blockers": sorted(codes),
    }
    report["version_gate"] = {
        "source_version": SOURCE_VERSION,
        "waived_blockers": sorted(codes),
        "other_blockers": 0,
        "rows": diagnostic["rows"],
    }
    engine = create_async_engine(url, connect_args={"ssl": "disable"})
    try:
        async with engine.connect() as conn:
            tables = "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'"
            assert (await conn.execute(text(tables))).scalar_one() == 0
        report["migrations_applied"] = len(
            await prepare_database(engine, PROJECT / "pb_migrations")
        )
        report["schema_before"] = await check_schema(url, schema)
        baseline = await database_state(engine, definitions)
        assert not baseline["_superusers"] and not baseline["_params"]

        rollback = {}
        for stage in ("admins", "options", "settings"):
            staged = root / f"failed-{stage}"
            stage_snapshot(data, staged, staged_report)
            try:
                async with engine.begin() as conn:
                    await remove_fresh_seeds(conn, definitions)
                    await load_isolated(conn, staged, definitions)
                    await import_auth(conn, staged, definitions, fail_after=stage)
            except ImportBlocked as exc:
                assert str(exc) == "injected_auth_import_failure", exc
            else:
                raise AssertionError("auth failure was not injected")
            finally:
                shutil.rmtree(staged)
            assert await database_state(engine, definitions) == baseline
            rollback[stage] = "records_accounts_options_settings_and_archives_rolled_back"
        report["rollback_in_load_isolated_transaction"] = rollback

        orphan = root / "auth-without-records"
        stage_snapshot(data, orphan, staged_report)
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

        published = root / "imported"
        stage_snapshot(data, published, staged_report)
        async with engine.begin() as conn:
            await remove_fresh_seeds(conn, definitions)
            loaded = await load_isolated(conn, published, definitions)
            auth_report = await import_auth(conn, published, definitions)
        report["import"] = {"records_loaded": loaded, "auth": auth_report}
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

        process, log, base = await ppbase_server(root, url, published)
        try:
            report["ppbase_after_import"] = {
                "probes": await run_probes(base, credentials),
                "legacy_probes": await legacy_probes(base, credentials),
                "purpose_tokens_from_pocketbase": await purpose_probes(base, purpose),
                "logins": await run_logins(base),
            }
        finally:
            stop(process)
            log.close()
        after = await database_state(engine, definitions)
        probe_before = next(r for r in imported_state["users"] if r["id"] == PROBE)
        probe_after = next(r for r in after["users"] if r["id"] == PROBE)
        admin_before = {r["id"]: r for r in imported_state["_superusers"]}
        admin_after = {r["id"]: r for r in after["_superusers"]}
        keys = ("password_hash", "token_key", "verified", "email")
        report["purpose_tokens_left_records_unchanged"] = all(
            probe_before[k] == probe_after[k] for k in keys
        ) and all(
            admin_before[i]["password_hash"] == admin_after[i]["password_hash"]
            and admin_before[i]["token_key"] == admin_after[i]["token_key"]
            for i in admin_before
        )
        assert report["purpose_tokens_left_records_unchanged"]
    finally:
        await engine.dispose()


def compare(report: dict) -> None:
    reference = report["pocketbase_reference"]["probes"]
    ppbase = report["ppbase_after_import"]["probes"]
    comparison, divergences, messages = {}, [], []
    for label, *_rest in PROBES:
        pb_status, pp_status = reference[label]["status"], ppbase[label]["status"]
        comparison[label] = {"pocketbase": pb_status, "ppbase": pp_status}
        if label in ACCEPTED:
            assert pb_status == pp_status == 200, (label, pb_status, pp_status)
            assert ppbase[label]["expected_id_returned"], label
            assert reference[label]["expected_id_returned"], label
        elif label in KNOWN_PPBASE_DIVERGENCES:
            # Still refused (no data served), but not with PocketBase's status.
            assert pb_status >= 400 and pp_status != 200, (label, pb_status, pp_status)
            divergences.append(
                {
                    "probe": label,
                    "pocketbase_status": pb_status,
                    "ppbase_status": pp_status,
                    "resolved": pb_status == pp_status,
                    "cause": KNOWN_PPBASE_DIVERGENCES[label],
                }
            )
        else:
            assert pb_status >= 400 and pp_status == pb_status, (label, pb_status, pp_status)
        if label not in KNOWN_PPBASE_DIVERGENCES and reference[label].get("message") != ppbase[
            label
        ].get("message"):
            messages.append(
                {
                    "probe": label,
                    "pocketbase": reference[label].get("message"),
                    "ppbase": ppbase[label].get("message"),
                }
            )
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
    purpose_pb = report["pocketbase_reference"]["purpose_tokens_accepted_by_pocketbase"]
    purpose_pp = report["ppbase_after_import"]["purpose_tokens_from_pocketbase"]
    assert all(status == 204 for status in purpose_pb.values()), purpose_pb
    assert all(result["status"] >= 400 for result in purpose_pp.values()), purpose_pp
    envelope = {
        "pocketbase": sorted(
            {tuple(v.get("keys", ())) for k, v in reference.items() if k not in ACCEPTED}
        ),
        "ppbase": sorted(
            {tuple(v.get("keys", ())) for k, v in ppbase.items() if k not in ACCEPTED}
        ),
    }
    report["comparison"] = {
        "probes": comparison,
        "same_status": sum(v["pocketbase"] == v["ppbase"] for v in comparison.values()),
        "total": len(comparison),
        "known_pre_existing_ppbase_divergences": divergences,
        "message_wording_differences": messages,
        "error_envelope_keys": {
            key: [list(item) for item in value] for key, value in envelope.items()
        },
        "purpose_tokens": {
            name: {"pocketbase": purpose_pb[name], "ppbase": purpose_pp[name]["status"]}
            for name in purpose_pb
        },
    }
    report["summary"] = {
        "pre_import_tokens_accepted_after_import": {
            "admin": all(comparison[k]["ppbase"] == 200 for k in ACCEPTED if k.startswith("admin")),
            "users": all(comparison[k]["ppbase"] == 200 for k in ACCEPTED if k.startswith("users")),
            "agent_users": all(
                comparison[k]["ppbase"] == 200 for k in ACCEPTED if k.startswith("agent")
            ),
        },
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
        "known_pre_existing_ppbase_divergences": len(divergences),
        "purpose_tokens_issued_before_import_continued": False,
        "buma_legacy_probes_match_contract": True,
    }


async def exercise(root: Path, binary: Path, schema: Path, report: dict, start_postgres) -> None:
    pocketbase = await pocketbase_phase(root, binary, schema, report)
    report["pocketbase_signing_key_checks"] = verify_signatures(
        pocketbase["data"], pocketbase["credentials"], pocketbase["purpose"]
    )
    url = start_postgres()
    await postgres_phase(
        root,
        url,
        pocketbase["data"],
        schema,
        pocketbase["credentials"],
        pocketbase["purpose"],
        report,
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
