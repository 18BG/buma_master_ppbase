"""Build synthetic C11 input with PocketBase itself, without BUMA bootstraps.

Profiles (``create_fixture(..., profile=...)``):

- ``default``: the historical rehearsal input, unchanged (9 records in 8
  collections, 3 files written next to the snapshot).
- ``real_shape``: the default records plus the shapes measured in the real
  PocketBase 0.22.34 backup (D-0019, D-0021, D-0022, D-0023 and the WS-D
  format audit): non-empty unlimited relations, a list element made of two
  joined IDs, orphan references, absent referenced files, duplicate mobile
  RETURN keys, custom record IDs, undeclared select values, auth records
  without email, one PocketBase admin, PocketBase settings with non-default
  logs/backups values, and files uploaded through the PocketBase HTTP API
  (``.attrs`` sidecars, thumbnails, files left by a deleted record).
  ``REAL_SHAPE`` documents every case.

Records are created by PocketBase itself: its Dao in JS migrations, the
``admin create`` command and, for uploads, its HTTP API. No BUMA hook,
bootstrap or external call is loaded. Values PocketBase cannot produce through
these paths are written with direct SQL; ``DIRECT_SQL`` lists all of them.
Every value is synthetic (``example.invalid``, ``c11`` identifiers).
"""

from __future__ import annotations

import json
import socket
import sqlite3
import struct
import subprocess
import time
import urllib.error
import urllib.request
import uuid
import zlib
from contextlib import closing
from pathlib import Path

POCKETBASE_VERSIONS = ("0.22.27", "0.22.34")
PROFILES = ("default", "real_shape")

# Synthetic credentials, documented so that a rehearsal can obtain tokens.
PASSWORD = "C11-test-password"
ADMIN_EMAIL = "c11-admin@example.invalid"
ADMIN_PASSWORD = "C11-admin-password"
ACCOUNTS = {
    "default": (
        # (collection, record id, username, email, password)
        ("users", "c11user00000001", "c11admin", "c11@example.invalid", PASSWORD),
        ("users", "c11user00000002", "c11denied", "denied@example.invalid", PASSWORD),
        ("agent_users", "c11agentuser001", "c11agent", "agent@example.invalid", PASSWORD),
    ),
    "real_shape": (
        # All 23 real auth records have an empty email: username-only logins.
        ("users", "c11user00000003", "c11noemail1", "", PASSWORD),
        ("users", "c11user00000004", "c11noemail2", "", PASSWORD),
        ("agent_users", "c11agentuser002", "c11agentnoemail", "", PASSWORD),
        # PocketBase generates the admin id; the email is the identity.
        ("_admins", None, None, ADMIN_EMAIL, ADMIN_PASSWORD),
    ),
}

DEFAULT_MIGRATION = "1789236900_c11_fixture.js"
REAL_SHAPE_MIGRATION = "1789236901_c11_real_shape.js"
REAL_SHAPE_POST_API_MIGRATION = "1789236902_c11_real_shape_post_api.js"

USER = "c11user00000001"
REAL_SHAPE_RECORDS = (
    # (collection, data, password). Saved in this order by PocketBase's Dao:
    # the order fixes the SQLite rowids used by the duplicate cases below.
    ("buma_pdv", {"id": "c11pdv000000001", "name": "C11 PDV one", "status": True}, None),
    ("buma_pdv", {"id": "c11pdv000000002", "name": "C11 PDV two", "status": True}, None),
    ("buma_pilotage_ticket_tag", {"id": "c11tag000000001", "title": "C11 tag one"}, None),
    ("buma_pilotage_ticket_tag", {"id": "c11tag000000002", "title": "C11 tag two"}, None),
    (
        "users",
        # JSON text null, beside the SQL NULL of the other users (both are real).
        {"id": "c11user00000003", "username": "c11noemail1", "name": "C11 no email one"}
        | {"more": "null"},
        PASSWORD,
    ),
    (
        "users",
        {"id": "c11user00000004", "username": "c11noemail2", "name": "C11 no email"},
        PASSWORD,
    ),
    ("agent_users", {"id": "c11agentuser002", "username": "c11agentnoemail"}, PASSWORD),
    (
        "buma_customer",
        {
            "id": "c11cust00000001",
            "firstname": "C11 two pdv",
            "status": True,
            "author": USER,
            "pdv": ["c11pdv000000002", "c11pdv000000001"],  # order is part of the value
            "type": "PERSONNE",  # literal kept by the real backup, no such record
        },
        None,
    ),
    (
        "buma_customer",
        {"id": "c11cust00000002", "firstname": "C11 one pdv", "pdv": ["c11pdv000000001"]},
        None,
    ),
    ("buma_customer", {"id": "c11cust00000003", "firstname": "C11 no pdv"}, None),
    (
        "buma_pilotage_ticket",
        {"id": "c11ticket000001", "title": "C11 two tags", "author": USER}
        | {"tag": ["c11tag000000002", "c11tag000000001"]},
        None,
    ),
    (
        "buma_pilotage_ticket",
        {"id": "c11ticket000002", "title": "C11 one tag", "tag": ["c11tag000000001"]},
        None,
    ),
    (
        "buma_wallet",
        {
            "id": "c11wallet000001",
            "name": "C11 joined managers",
            "status": True,
            "author": USER,
            # One 31-character element: two existing user IDs joined by a comma.
            "managers": ["c11user00000001,c11user00000002"],
            # Referenced file absent from storage (like the real wallet image).
            "image": "c11_absent_wallet_image_Ab12Cd34Ef.png",
        },
        None,
    ),
    ("buma_wallet", {"id": "c11wallet000002", "name": "C11 no managers"}, None),
    # The target of buma_sale.customer is buma_customer_old, empty here as in
    # the real backup: the first ID exists in buma_customer, the second nowhere.
    ("buma_sale", {"id": "c11sale00000001", "customer": "c11cust00000001", "amount": 25}, None),
    (
        "buma_sale",
        # "kind" outside its declared values (UV, AUTRE), as 51,145 real values.
        {"id": "c11sale00000002", "customer": "c11nocustomer01", "amount": 30}
        | {"kind": "C11_UNDECLARED"},
        None,
    ),
    (
        "buma_sim_balance_activity",
        {"id": "c11simbal000001", "author": "1", "id_date": "c11-simbal-1", "balance": 10},
        None,
    ),
    (
        "buma_article",
        {"id": "c11art000000002", "designation": "C11 sentinel category"}
        | {"categorie": "0", "status": True, "sell_price": 3, "buy_price": 2},
        None,
    ),
    (
        "buma_ressources",
        {"id": "c11file00000002", "name": "C11 absent file", "fileName": "c11-absent"}
        | {"author": USER, "status": True, "file": ["c11_absent_resource_Gh56Ij78Kl.pdf"]},
        None,
    ),
    # Custom record IDs, as the 26 real ones: 3 to 9 lowercase letters (18), or
    # 15 characters with uppercase letters, "_" and sometimes digits (8), some
    # referenced by relations (436 real values) or owning files (8 objects).
    ("buma_apps_roles", {"id": "synthetic", "ref": "c11", "name": "C11 short custom id"}, None),
    ("buma_payment_method", {"id": "C11_PAY_METHOD1", "name": "C11 custom id"}, None),
    (
        "buma_payment",
        {"id": "c11payment00001", "method": "C11_PAY_METHOD1", "sale": "c11sale00000001"}
        | {"amount": 25, "author": USER, "status": True},
        None,
    ),
)
# Duplicate (type, author, mobile_message_id) RETURN groups with different
# business values, as the 877 real groups (D-0022). Group "tie" shares its
# first created date (30 real groups); in group "inverted" the smallest rowid is
# not the oldest record (4 real groups, D-0023).
RETURN_DUPLICATES = {
    "c11-mobile-dup-tie": (
        ("c11ret000000001", "2024-11-18 08:00:00.000Z", 1, 5),
        ("c11ret000000002", "2024-11-18 08:00:00.000Z", 2, 5),
        ("c11ret000000003", "2024-11-18 08:05:00.000Z", 1, 7),
    ),
    "c11-mobile-dup-inverted": (
        ("c11ret000000004", "2025-01-10 10:00:00.000Z", 3, 4),
        ("c11ret000000005", "2025-01-10 09:00:00.000Z", 4, 4),
    ),
}
REAL_SHAPE_RECORDS += tuple(
    (
        "buma_transaction",
        {
            "id": record,
            "type": "RETURN",
            "author": USER,
            "mobile_message_id": key,
            "system_id": "",
            "quantity": quantity,
            "price": price,
            "status": True,
            "notes": "C11 duplicate " + record[-1],
            "created": created,
            "updated": created,
        },
        None,
    )
    for key, rows in RETURN_DUPLICATES.items()
    for record, created, quantity, price in rows
)


def _png(width: int = 4, height: int = 4) -> bytes:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        crc = zlib.crc32(kind + payload) & 0xFFFFFFFF
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", crc)

    rows = b"".join(b"\x00" + b"\xc8\x1e\x1e" * width for _ in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(rows, 9))
        + chunk(b"IEND", b"")
    )


# Uploaded as the admin through POST /api/collections/<name>/records: PocketBase
# generates the stored names and the .attrs sidecars; GET ?thumb=100x100
# generates a thumbnail one directory level deeper.
UPLOADS = (
    # (collection, multipart fields, file field, (client name, bytes)..., thumb)
    (
        "buma_ressources",
        {"id": "c11file00000003", "name": "C11 uploaded by PocketBase"}
        | {"fileName": "c11-uploaded", "author": USER, "status": "true"},
        "file",
        (
            ("C11 Thumb Source.png", _png()),
            ("C11 Upload Proof.txt", b"C11 file uploaded through the PocketBase API\n"),
        ),
        True,
    ),
    # Files under a custom 15-character record ID (8 such real objects).
    (
        "buma_config",
        {"id": "C11_CONFIG_REC1", "business_name": "C11 synthetic business"},
        "logo",
        (("C11 Logo.png", _png(6, 3)),),
        True,
    ),
    # Deleted afterwards through PocketBase's Dao, which leaves its files in
    # storage like the 10 real objects whose record no longer exists.
    (
        "buma_ressources",
        {"id": "c11deleted00001", "name": "C11 deleted record", "fileName": "c11-deleted"}
        | {"author": USER},
        "file",
        (("C11 Deleted Record.txt", b"C11 file left by a deleted record\n"),),
        False,
    ),
)
DELETED_RECORDS = (("buma_ressources", "c11deleted00001"),)

# Logs and backups were the only non-secret settings differing from the
# PocketBase defaults in the real backup; synthetic values reproduce that shape.
# The cron is valid but only fires on 29 February, keeping later runs inert.
SETTINGS_OVERRIDES = {"logs.maxDays": 7, "logs.minLevel": 4, "backups.cron": "0 3 29 2 *"}

DIRECT_SQL = (
    # Writes outside PocketBase's record Dao/API, identical in both profiles;
    # real_shape adds none. The first two run as SQL inside the JS migration.
    "users/buma_article/buma_supplier rowid updates: rowids cannot be chosen by the Dao",
    "buma_supplier created/updated: historical default-profile dates",
    "buma_article.medias and buma_ressources.file of the default records: the "
    "default files are written next to the snapshot, not uploaded",
    "PRAGMA wal_checkpoint(TRUNCATE) and journal_mode=DELETE: closed snapshot, no WAL",
)

REAL_SHAPE = {
    "unlimited_relations": {
        "buma_customer.pdv": {
            "c11cust00000001": ["c11pdv000000002", "c11pdv000000001"],
            "c11cust00000002": ["c11pdv000000001"],
            "c11cust00000003": [],
        },
        "buma_pilotage_ticket.tag": {
            "c11ticket000001": ["c11tag000000002", "c11tag000000001"],
            "c11ticket000002": ["c11tag000000001"],
        },
        "buma_wallet.managers": {
            "c11wallet000001": ["c11user00000001,c11user00000002"],
            "c11wallet000002": [],
        },
    },
    "orphan_relations": {
        "buma_sale.customer": {
            "c11sale00000001": "c11cust00000001",
            "c11sale00000002": "c11nocustomer01",
        },
        "buma_sim_balance_activity.author": {"c11simbal000001": "1"},
        "buma_article.categorie": {"c11art000000002": "0"},
        "buma_customer.type": {"c11cust00000001": "PERSONNE"},
        "buma_wallet.managers": {"c11wallet000001": "c11user00000001,c11user00000002"},
    },
    "missing_files": {
        "buma_wallet.image": {"c11wallet000001": ["c11_absent_wallet_image_Ab12Cd34Ef.png"]},
        "buma_ressources.file": {"c11file00000002": ["c11_absent_resource_Gh56Ij78Kl.pdf"]},
    },
    "mobile_return_duplicates": {
        key: [record for record, *_ in rows] for key, rows in RETURN_DUPLICATES.items()
    },
    "custom_record_ids": {
        "buma_apps_roles": "synthetic",
        "buma_payment_method": "C11_PAY_METHOD1",
        "buma_config": "C11_CONFIG_REC1",
    },
    "relation_to_custom_id": {"buma_payment.method": {"c11payment00001": "C11_PAY_METHOD1"}},
    "undeclared_select_values": {"buma_sale.kind": {"c11sale00000002": "C11_UNDECLARED"}},
    "json_null_text": {"users.more": "c11user00000003"},
    "api_uploads": {
        f"{collection}.{field}": fields["id"] for collection, fields, field, *_ in UPLOADS
    },
    "deleted_record_files": dict(DELETED_RECORDS),
    "settings_overrides": SETTINGS_OVERRIDES,
    "accounts": ACCOUNTS,
}


def pocketbase_version(binary: Path) -> str:
    output = subprocess.run(
        [str(binary), "--version"], capture_output=True, text=True, check=True
    ).stdout.strip()
    prefix = "pocketbase version "
    if not output.startswith(prefix):
        raise RuntimeError("Unrecognized PocketBase version output")
    return output[len(prefix) :]


def _pocketbase(*command) -> None:
    # PocketBase 0.22 prints command errors but still exits 0 (pocketbase.go
    # ignores RootCmd.Execute errors): callers also verify the effects.
    completed = subprocess.run([str(part) for part in command], capture_output=True, text=True)
    if completed.returncode or "Error:" in completed.stdout + completed.stderr:
        raise RuntimeError(completed.stdout + completed.stderr)


def _query(data: Path, sql: str, *parameters) -> list[tuple]:
    # Closed explicitly: an idle connection left open keeps PocketBase's WAL
    # database from switching back to journal_mode=DELETE below.
    with closing(sqlite3.connect(data / "data.db")) as db:
        return db.execute(sql, parameters).fetchall()


def _migrate(binary: Path, data: Path, migrations: Path, expected: set[str]) -> None:
    _pocketbase(binary, "migrate", "up", "--dir", data, "--migrationsDir", migrations)
    # One failing JS migration rolls the whole run back without an exit code.
    if not expected <= {row[0] for row in _query(data, "SELECT file FROM _migrations")}:
        raise RuntimeError("PocketBase did not apply the fixture migrations")


def _request(base: str, path: str, *, body=None, token=None, content_type=None):
    headers = {"Content-Type": content_type} if content_type else {}
    if token:
        headers["Authorization"] = token
    request = urllib.request.Request(base + path, data=body, headers=headers)
    # Loopback only: never route the synthetic server through a proxy.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=15) as response:
        return response.status, response.read()


def _multipart(fields: dict, file_field: str, files) -> tuple[bytes, str]:
    boundary = "c11" + uuid.uuid4().hex
    parts = [
        f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode()
        for key, value in fields.items()
    ]
    for filename, content in files:
        disposition = f'form-data; name="{file_field}"; filename="{filename}"'
        parts.append(
            f"--{boundary}\r\nContent-Disposition: {disposition}\r\n"
            "Content-Type: application/octet-stream\r\n\r\n".encode()
            + content
            + b"\r\n"
        )
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), "multipart/form-data; boundary=" + boundary


def _upload_with_api(binary: Path, data: Path, migrations: Path, log_path: Path) -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    with log_path.open("w") as log:
        server = subprocess.Popen(
            [
                str(binary),
                "serve",
                "--http",
                f"127.0.0.1:{port}",
                "--dir",
                str(data),
                "--migrationsDir",
                str(migrations),
                "--automigrate=false",
            ],
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            for _ in range(200):
                if server.poll() is not None:
                    raise RuntimeError("PocketBase fixture server stopped; see its private log")
                try:
                    if _request(base, "/api/health")[0] == 200:
                        break
                except (urllib.error.URLError, ConnectionError):
                    pass
                time.sleep(0.05)
            else:
                raise TimeoutError("PocketBase fixture server startup")
            _, auth = _request(
                base,
                "/api/admins/auth-with-password",
                body=json.dumps({"identity": ADMIN_EMAIL, "password": ADMIN_PASSWORD}).encode(),
                content_type="application/json",
            )
            token = json.loads(auth)["token"]
            for collection, fields, file_field, files, thumb in UPLOADS:
                body, content_type = _multipart(fields, file_field, files)
                _, created = _request(
                    base,
                    f"/api/collections/{collection}/records",
                    body=body,
                    token=token,
                    content_type=content_type,
                )
                record = json.loads(created)
                stored = record[file_field]
                stored = stored if isinstance(stored, list) else [stored]
                if len(stored) != len(files):
                    raise RuntimeError("PocketBase did not store every uploaded file")
                if not thumb:
                    continue
                image = next(name for name in stored if name.endswith(".png"))
                path = f"/api/files/{collection}/{record['id']}/{image}?thumb=100x100"
                _request(base, path)
                # PocketBase serves the original when a thumbnail fails: check it.
                folder = data / "storage" / record["collectionId"] / record["id"]
                if not (folder / f"thumbs_{image}" / f"100x100_{image}").is_file():
                    raise RuntimeError("PocketBase did not create the thumbnail")
        finally:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()


def _real_shape(binary: Path, data: Path, migrations: Path, root: Path) -> None:
    (migrations / REAL_SHAPE_MIGRATION).write_text(
        "migrate((db)=>{const dao=new Dao(db);for(const [name,data,password] of "
        + json.dumps(REAL_SHAPE_RECORDS, ensure_ascii=False)
        + "){const r=new Record(dao.findCollectionByNameOrId(name),data);"
        + "if(password) r.setPassword(password);dao.saveRecord(r);}},()=>{});\n"
    )
    _migrate(binary, data, migrations, {DEFAULT_MIGRATION, REAL_SHAPE_MIGRATION})
    _pocketbase(binary, "admin", "create", ADMIN_EMAIL, ADMIN_PASSWORD, "--dir", data)
    if _query(data, "SELECT COUNT(*) FROM _admins WHERE email=?", ADMIN_EMAIL) != [(1,)]:
        raise RuntimeError("PocketBase did not create the fixture admin")
    _upload_with_api(binary, data, migrations, root / "pocketbase-serve.log")
    # After the server run, so that it never registers the backup cron. A Dao
    # created from the migration db has no app hooks: deleting a record with it
    # keeps the record files, unlike an API delete.
    assignments = "".join(
        f"s.{path}={json.dumps(value)};" for path, value in SETTINGS_OVERRIDES.items()
    )
    (migrations / REAL_SHAPE_POST_API_MIGRATION).write_text(
        "migrate((db)=>{const dao=new Dao(db);const s=dao.findSettings();"
        + assignments
        + "dao.saveSettings(s);for(const [name,id] of "
        + json.dumps(DELETED_RECORDS)
        + ")dao.deleteRecord(dao.findRecordById(name,id));},()=>{});\n"
    )
    _migrate(binary, data, migrations, {REAL_SHAPE_POST_API_MIGRATION})


def create_fixture(root: Path, binary: Path, schema: Path, *, profile: str = "default") -> Path:
    """The caller owns a new disposable directory; no real backup is read."""
    if profile not in PROFILES:
        raise ValueError("Unknown C11 fixture profile")
    if pocketbase_version(binary) not in POCKETBASE_VERSIONS:
        raise ValueError("The C11 fixture requires PocketBase " + " or ".join(POCKETBASE_VERSIONS))
    root.mkdir(exist_ok=False, parents=True)
    migrations = root / "migrations"
    migrations.mkdir()
    definitions = json.loads(schema.read_text())
    fixture_js = r"""
    const save=(name, data, password)=>{
      const r=new Record(dao.findCollectionByNameOrId(name),data);
      if(password) r.setPassword(password);
      dao.saveRecord(r); return r;
    };
    save("roles", {id:"c11role00000001",name:"C11 synthetic",
      permission:{isAdmin:true},status:true});
    save("users", {id:"c11user00000001",username:"c11admin",email:"c11@example.invalid",
      role:"c11role00000001",name:"C11 fixture",verified:true}, "C11-test-password");
    save("users", {id:"c11user00000002",username:"c11denied",
      email:"denied@example.invalid",name:"C11 denied"}, "C11-test-password");
    save("agent_users", {id:"c11agentuser001",username:"c11agent",
      email:"agent@example.invalid"}, "C11-test-password");
    save("buma_caisse_access", {id:"c11access000001",user:"c11user00000001"});
    save("buma_article", {id:"c11art000000001",designation:"C11 decimal article",
      sell_price:12.75,buy_price:8.25,status:true,kind:"AUTRES",
      specification:{nested:[false,0,"é"]},medias:"proof.bin"});
    save("buma_ressources", {id:"c11file00000001",name:"C11 files",
      file:["one.bin","two.bin"],status:true,author:"c11user00000001"});
    save("buma_supplier", {id:"c11supp00000001",firstname:"C11 preserved",
      author:"c11user00000001",status:false,solde:-17.25});
    save("buma_transaction", {id:"c11tx000000001",article:"c11art000000001",
      author:"c11user00000001",price:12.75,quantity:2.5,status:true,type:"VENTE",
      system_id:"idem:c11:proof",notes:"C11 synthetic"});
    dao.db().newQuery("UPDATE users SET rowid=41 WHERE id='c11user00000001'").execute();
    dao.db().newQuery("UPDATE users SET rowid=93 WHERE id='c11user00000002'").execute();
    dao.db().newQuery("UPDATE buma_article SET rowid=7001").execute();
    dao.db().newQuery("UPDATE buma_supplier SET rowid=-17").execute();
    dao.db().newQuery("UPDATE buma_supplier SET created='2024-02-29 12:34:56.789Z',"
      +"updated='2024-03-01 00:00:00.000Z'").execute();
    """
    # File values are inserted below: PB correctly rejects an absent upload in
    # a Record form, while the stopped snapshot must reference actual bytes.
    fixture_js = fixture_js.replace(',medias:"proof.bin"', "")
    fixture_js = fixture_js.replace('file:["one.bin","two.bin"],', "")
    (migrations / DEFAULT_MIGRATION).write_text(
        "migrate((db)=>{const dao=new Dao(db);dao.importCollections("
        + json.dumps(definitions)
        + ".map(c=>new Collection(c)),true);"
        + fixture_js
        + "},()=>{});\n"
    )
    data = root / "data"
    if profile == "real_shape":
        _real_shape(binary, data, migrations, root)
    else:
        _migrate(binary, data, migrations, {DEFAULT_MIGRATION})
    with sqlite3.connect(data / "data.db") as db:
        db.execute("UPDATE buma_article SET medias='proof.bin' WHERE id='c11art000000001'")
        db.execute(
            "UPDATE buma_ressources SET file='[\"one.bin\",\"two.bin\"]' WHERE id='c11file00000001'"
        )
        db.commit()
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        db.execute("PRAGMA journal_mode=DELETE")
    for name, record, filename, content in (
        ("buma_article", "c11art000000001", "proof.bin", b"C11\x00\xff\x01original"),
        ("buma_ressources", "c11file00000001", "one.bin", b"C11 file one"),
        ("buma_ressources", "c11file00000001", "two.bin", b"C11 file two"),
    ):
        collection_id = next(c["id"] for c in definitions if c["name"] == name)
        target = data / "storage" / collection_id / record / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    return data
