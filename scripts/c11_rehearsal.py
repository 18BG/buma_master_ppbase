"""Synthetic-only C11 rehearsal in an owned, disposable Docker PostgreSQL.

No --db or --data argument exists: this cannot target a supplied backup/server.
Run as python -m scripts.c11_rehearsal --pocketbase /path/to/pocketbase ...
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
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
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from scripts.c11_fixture import create_fixture
from scripts.c11_import import (
    ImportBlocked,
    diagnose,
    digest,
    load_isolated,
    load_schema,
    quote,
    snapshot,
    stage_snapshot,
    storage_inventory,
    write_json_new,
)

PROJECT = Path(__file__).resolve().parents[1]


def command(*args):
    return subprocess.check_output(args, text=True, stderr=subprocess.STDOUT).strip()


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


async def check_schema(url, schema):
    from scripts.generate_native_baseline import load_schema as translated_schema
    from scripts.verify_complete_schema import read_collections, verify

    report = verify(translated_schema(schema, include_views=True)[0], await read_collections(url))
    if not report["ok"]:
        raise AssertionError(report)
    return {"definitions": report["expected"], "views": report["expected_views"], "ok": True}


def negative_diagnostics(data, root, schema):
    """Each rejected snapshot is synthetic; no fixture silently repairs a source."""
    cases = {
        "dangling_relation": "UPDATE buma_transaction SET article='absent000000001'",
        "duplicate_idempotency_key": "INSERT INTO buma_transaction "
        "(id,type,author,system_id) SELECT 'duplicate000001',type,author,system_id "
        "FROM buma_transaction",
        "unbounded_relation_target_scalar": "INSERT INTO buma_customer (id,pdv) "
        "VALUES ('multirel0000001','[\"pdv000000000001\",\"pdv000000000002\"]')",
        "schema_drift": "UPDATE _collections SET listRule='C11 intentional drift' "
        "WHERE name='users'",
        "invalid_number": "UPDATE buma_article SET sell_price='broken'",
    }
    results = {}
    for code, sql in cases.items():
        clone = root / code
        shutil.copytree(data, clone)
        with sqlite3.connect(clone / "data.db") as conn:
            conn.execute(sql)
        report = diagnose(clone, schema, source_version="0.22.27")
        if code not in {b["code"] for b in report["blockers"]}:
            raise AssertionError((code, report["blockers"]))
        results[code] = "blocked"
    clone = root / "missing_file"
    shutil.copytree(data, clone)
    next(p for p in (clone / "storage").rglob("proof.bin")).unlink()
    report = diagnose(clone, schema, source_version="0.22.27")
    if "missing_referenced_file" not in {b["code"] for b in report["blockers"]}:
        raise AssertionError(report["blockers"])
    results["missing_file"] = "blocked"
    return results


async def http_probe(url, data, expected):
    evidence = []
    async with httpx.AsyncClient(base_url=url, timeout=15) as client:
        for identity in ("c11admin", "c11denied"):
            response = await client.post(
                "/api/collections/users/auth-with-password",
                json={"identity": identity, "password": "C11-test-password"},
            )
            assert response.status_code == 200, response.text
            body = response.json()
            assert set(body) == {"token", "record"}
            assert not {"rowid", "passwordHash", "password_hash", "tokenKey", "token_key"} & set(
                body["record"]
            )
            headers = {"Authorization": body["token"]}
            refresh = await client.post("/api/collections/users/auth-refresh", headers=headers)
            assert (
                refresh.status_code == 200
                and refresh.json()["record"]["id"] == body["record"]["id"]
            )
            article = await client.get(
                "/api/collections/buma_article/records/c11art000000001", headers=headers
            )
            access = await client.get(
                "/api/collections/buma_caisse_access/records/c11access000001", headers=headers
            )
            assert article.status_code == 200, article.text
            if identity == "c11admin":
                assert access.status_code == 200, access.text
                assert article.status_code == 200, article.text
                assert article.json()["sell_price"] == 12.75 and "rowid" not in article.json()
                transactions = await client.get(
                    "/api/collections/buma_transaction/records",
                    headers=headers,
                    params={"filter": 'article="c11art000000001"', "expand": "article,author"},
                )
                assert transactions.status_code == 200, transactions.text
                payload = transactions.json()
                assert set(payload) == {"page", "perPage", "totalItems", "totalPages", "items"}
                assert payload["totalItems"] == 1
                item = payload["items"][0]
                assert (
                    item["quantity"] == 2.5 and item["expand"]["article"]["id"] == "c11art000000001"
                )
                assert item["expand"]["author"]["id"] == "c11user00000001"
                for relative in expected:
                    response = await client.get("/api/files/" + relative, headers=headers)
                    assert response.status_code == 200, response.text
                    assert response.content == (data / "storage" / relative).read_bytes()
                evidence.extend(
                    [
                        "native_filtered_list_envelope",
                        "expand_article_author",
                        "three_files_exact_bytes",
                    ]
                )
            else:
                assert access.status_code == 404, access.text
            evidence.append(identity + "_login_refresh_permissions")
        anonymous = await client.get("/api/collections/buma_article/records/c11art000000001")
        assert anonymous.status_code == 200, anonymous.text
        private = await client.get("/api/collections/buma_supplier/records/c11supp00000001")
        assert private.status_code == 404, private.text
        agent = await client.post(
            "/api/collections/agent_users/auth-with-password",
            json={"identity": "agent@example.invalid", "password": "C11-test-password"},
        )
        assert agent.status_code == 200, agent.text
        own = await client.get(
            "/api/collections/agent_users/records/c11agentuser001",
            headers={"Authorization": agent.json()["token"]},
        )
        assert own.status_code == 200, own.text
        bad = await client.post(
            "/api/collections/users/auth-with-password",
            json={"identity": "c11admin", "password": "wrong"},
        )
        assert bad.status_code == 400, bad.text
        evidence += [
            "public_article_200",
            "anonymous_supplier_404",
            "wrong_password_400",
            "agent_users_original_password_owner_read",
        ]
    return evidence


async def exercise(root, url, binary, schema, report):
    from ppbase.services.database_preparation import prepare_database

    data = create_fixture(root / "fixture", binary, schema)
    definitions = load_schema(schema)
    original_hash = digest(data / "data.db")
    diagnostic = diagnose(data, schema, source_version="0.22.27")
    assert diagnostic["ready_for_isolated_rehearsal"], diagnostic["blockers"]
    report["diagnostic"] = diagnostic
    report["negative_cases"] = negative_diagnostics(data, root, schema)
    engine = create_async_engine(url)
    try:
        # Enforce virgin target independently of its random Docker name.
        async with engine.connect() as conn:
            assert (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'"
                    )
                )
            ).scalar_one() == 0
        migrations = await prepare_database(engine, PROJECT / "pb_migrations")
        report["migrations_applied"] = len(migrations)
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
        baseline = await table_state(engine, definitions)
        report["seed_rows_before"] = sum(len(v) for v in baseline.values())

        async def remove_fresh_seeds(conn):
            # Only the virgin database just initialized above; not part of the
            # import primitive. No existing environment is ever truncated.
            for c in definitions:
                if c["type"] != "view":
                    await conn.execute(text(f"DELETE FROM {quote(c['name'])}"))

        staged = root / "failed_attempt"
        stage_snapshot(data, staged, diagnostic)
        try:
            async with engine.begin() as conn:
                await remove_fresh_seeds(conn)
                await load_isolated(conn, staged, definitions, fail_after=3)
        except ImportBlocked as exc:
            assert str(exc) == "injected_import_failure"
        else:
            raise AssertionError("Fault was not injected")
        finally:
            shutil.rmtree(staged)
        assert await table_state(engine, definitions) == baseline
        async with engine.connect() as conn:
            assert (
                await conn.execute(text("SELECT to_regclass('_buma_c11_rowids')"))
            ).scalar_one() is None
        assert not staged.exists()
        report["failure_rollback"] = "all_72_tables_and_seed_values_restored_no_staged_files"

        published = root / "imported"
        files = stage_snapshot(data, published, diagnostic)
        # Both writers see the same empty target initially; only one can load.
        async with engine.begin() as conn:
            await remove_fresh_seeds(conn)

        async def attempt():
            try:
                async with engine.begin() as conn:
                    return await load_isolated(conn, published, definitions)
            except ImportBlocked as exc:
                return str(exc)

        outcomes = await asyncio.gather(attempt(), attempt())
        assert sorted(map(str, outcomes)) == sorted([str(diagnostic["rows"]), "target_not_empty"])
        report["concurrent_import"] = {"outcomes": outcomes, "no_merge_or_duplicates": True}
        final = await table_state(engine, definitions)
        assert sum(len(rows) for rows in final.values()) == diagnostic["rows"]
        with snapshot(data) as db:
            async with engine.connect() as conn:
                archived = (
                    (await conn.execute(text("SELECT * FROM _buma_c11_rowids"))).mappings().all()
                )
                for row in archived:
                    c = next(c for c in definitions if c["id"] == row["collection_id"])
                    source = db.execute(
                        f"SELECT _rowid_ FROM {quote(c['name'])} WHERE id=?", (row["record_id"],)
                    ).fetchone()
                    assert source[0] == row["source_rowid"]
        assert len(archived) == diagnostic["rows"]
        assert {r["id"]: r["rowid"] for r in final["users"]} == {
            "c11user00000001": 41,
            "c11user00000002": 93,
        }
        assert final["buma_article"][0]["rowid"] == 7001
        assert final["buma_supplier"][0]["status"] is False
        assert final["buma_supplier"][0]["solde"] == -17.25
        assert (
            final["buma_supplier"][0]["created"].isoformat() == "2024-02-29T12:34:56.789000+00:00"
        )
        report["preservation"] = {
            "rows": len(archived),
            "ids_relations_typed_readback": True,
            "source_rowids_all_tables": True,
            "native_rowids_users_article": True,
            "negative_rowid_archived": any(r["source_rowid"] == -17 for r in archived),
            "legacy_auth_dates_archived": True,
            "timestamps_status_balances": True,
            "file_bytes": sum(f["bytes"] for f in files.values()),
            "file_count": len(files),
        }
        report["schema_after"] = await check_schema(url, schema)
        # Start BUMA only after a complete offline import; jobs remain disabled.
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        env = {
            **os.environ,
            "BUMA_MASTER_ENABLE_INTERNAL_JOBS": "false",
            "PPBASE_AUTO_MIGRATE": "false",
            "PPBASE_STORAGE_BACKEND": "local",
        }
        with (root / "http.log").open("w") as log:
            server = subprocess.Popen(
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
            try:
                base = f"http://127.0.0.1:{port}"
                async with httpx.AsyncClient() as client:
                    for _ in range(100):
                        if server.poll() is not None:
                            raise RuntimeError("Isolated BUMA startup failed; see private http.log")
                        try:
                            if (await client.get(base + "/api/health")).status_code == 200:
                                break
                        except httpx.ConnectError:
                            pass
                        await asyncio.sleep(0.1)
                    else:
                        raise TimeoutError("BUMA startup")
                report["http_import_checks"] = await http_probe(base, data, files)
            finally:
                server.terminate()
                try:
                    server.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait()
        assert await table_state(engine, definitions) == final
        assert digest(data / "data.db") == original_hash
        assert storage_inventory(published / "storage") == files
        report["source_unchanged"] = True
        report["no_business_hook_replay"] = True
    finally:
        await engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pocketbase", type=Path, required=True)
    parser.add_argument("--schema", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    if args.report.exists():
        parser.error("Report must be a new path")
    if command(str(args.pocketbase), "--version") != "pocketbase version 0.22.27":
        parser.error("This fixture builder requires PocketBase 0.22.27")
    name = "buma-c11-" + uuid.uuid4().hex[:12]
    report = {
        "timestamp_utc": datetime.now(UTC).isoformat(),
        "scope": "synthetic_import_preparation",
        "c10_resumed": False,
        "production_data_used": False,
        "status": "failed",
    }
    created = False
    try:
        with tempfile.TemporaryDirectory(prefix="buma-c11-rehearsal-") as folder:
            root = Path(folder)
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
                "POSTGRES_PASSWORD=c11-local-rehearsal",
                "--env",
                "POSTGRES_DB=buma_c11_rehearsal",
                "postgres:16-alpine",
            )
            created = True
            port = command("docker", "port", name, "5432/tcp").rsplit(":", 1)[1]
            url = (
                f"postgresql+asyncpg://c11:c11-local-rehearsal@127.0.0.1:{port}/buma_c11_rehearsal"
            )
            for _ in range(100):
                try:
                    command(
                        "docker",
                        "exec",
                        name,
                        "pg_isready",
                        "-U",
                        "c11",
                        "-d",
                        "buma_c11_rehearsal",
                    )
                    break
                except subprocess.CalledProcessError:
                    time.sleep(0.1)
            asyncio.run(
                exercise(root, url, args.pocketbase.resolve(), args.schema.resolve(), report)
            )
            report["status"] = "passed"
    except BaseException as exc:
        # Do not leak credentials, raw auth rows or bind parameters to reports.
        report["failure_type"] = type(exc).__name__
        raise
    finally:
        if created:
            command("docker", "stop", "--time", "5", name)
        report["cleanup"] = "owned_container_and_temporary_files_removed"
        write_json_new(args.report, report)
        print(json.dumps({"status": report["status"], "report": str(args.report)}))


if __name__ == "__main__":
    main()
