"""D-0022 runtime proof in an owned, disposable Docker PostgreSQL.

No --db argument exists: this cannot target an existing database. It applies
the official migrations, compares source/C4 index behaviour, reverts and
replays 1789236849 (after 1789236850 and 1789236851, the latest migrations), checks lock+lookup
concurrency without the C4 mobile index, then replays C4, C7 and C9 against an
isolated PPBase server. The server gets the caller's PYTHONPATH after this
project, so PYTHONPATH selects the PPBase under test; the report records the
PPBase it loads (a probe with the server's interpreter, environment and cwd),
which must be the one this process uses, and the PYTHONPATH, cwd and command
read from the live server process (/proc, when available). The container and
temporary files are removed at the end.
Run as python -m scripts.c11_idempotency_runtime --report /new/path.json
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx
import ppbase
from ppbase.services.database_preparation import prepare_database
from ppbase.services.migration_runner import run_migrations_down, run_migrations_up
from ppbase.services.record_storage_coordinator import run_record_storage_transaction
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from app.transactions.idempotency import (
    IdempotencyLookup,
    acquire_idempotency_lock,
    find_idempotent_transaction,
)
from scripts import c4_idempotency_runtime as c4
from scripts import c7_stockage_runtime as c7
from scripts import c9_commerce_runtime as c9
from scripts.c11_import import write_json_new
from scripts.verify_schema_baseline import SOURCE_INDEX_DELTAS

PROJECT = Path(__file__).resolve().parents[1]
MIGRATIONS = PROJECT / "pb_migrations"
D0022 = "1789236849_c11_source_transaction_idempotency.py"
D0019 = "1789236850_c11_relation_multiple_columns.py"
VIEWS = "1789236851_c11_view_sqlite_semantics.py"
RELATION_COLUMNS = (
    ("buma_customer", "pdv"),
    ("buma_pilotage_ticket", "tag"),
    ("buma_wallet", "managers"),
)
SOURCE_INDEX = SOURCE_INDEX_DELTAS["buma_transaction"][0]
SOURCE_NAME = "idx_transaction_idempotency"
C4_SYSTEM = "idx_buma_transaction_idempotency_system"
C4_MOBILE = "idx_buma_transaction_idempotency_mobile"
AUTHOR = "d0022-runtime"

# label, first row, second row, second outcome with the source index, with C4.
CASES = (
    ("same_mobile_message_id", {"mobile": "d0022-m"}, {"mobile": "d0022-m"}, "accepted", C4_MOBILE),
    (
        "same_non_idem_system_id",
        {"system_id": "d0022-legacy"},
        {"system_id": "d0022-legacy"},
        "accepted",
        C4_SYSTEM,
    ),
    (
        "idem_key_other_type_and_author",
        {"system_id": "idem:return:d0022:k"},
        {"system_id": "idem:return:d0022:k", "type_": "RECHARGE", "author": "d0022-other"},
        SOURCE_NAME,
        "accepted",
    ),
    (
        "uppercase_idem_prefix",
        {"system_id": "IDEM:d0022"},
        {"system_id": "IDEM:d0022"},
        SOURCE_NAME,
        C4_SYSTEM,
    ),
    (
        "non_ascii_prefix",
        {"system_id": "İdem:d0022"},
        {"system_id": "İdem:d0022"},
        "accepted",
        C4_SYSTEM,
    ),
    (
        "idem_values_differing_by_case",
        {"system_id": "idem:d0022:case"},
        {"system_id": "IDEM:d0022:case"},
        "accepted",
        "accepted",
    ),
)


def command(*args, **kwargs):
    return subprocess.check_output(args, text=True, stderr=subprocess.STDOUT, **kwargs).strip()


def baseline_indexes() -> list[str]:
    path = MIGRATIONS / "1789236840_buma_master_schema_baseline.py"
    spec = importlib.util.spec_from_file_location("buma_baseline", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    collection = next(c for c in module._CREATED_COLLECTIONS if c["name"] == "buma_transaction")
    return list(collection["indexes"])


async def index_state(engine) -> dict:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT indexname, indexdef FROM pg_indexes "
                "WHERE schemaname='public' AND tablename='buma_transaction'"
            )
        )
        physical = {name: definition for name, definition in rows.all()}
        metadata = (
            await conn.execute(
                text("SELECT indexes FROM _collections WHERE name='buma_transaction'")
            )
        ).scalar_one()
    return {"physical": physical, "metadata": metadata}


def assert_source_state(state: dict, baseline: list[str]) -> None:
    physical = state["physical"]
    assert "UNIQUE" in physical[SOURCE_NAME] and "translate" in physical[SOURCE_NAME], physical
    assert not {C4_SYSTEM, C4_MOBILE} & set(physical), physical
    assert "UNIQUE" not in physical["idx_bEWTmoj"], physical
    assert state["metadata"] == [*baseline, SOURCE_INDEX], state["metadata"]


def assert_c4_state(state: dict, baseline: list[str]) -> None:
    physical = state["physical"]
    assert SOURCE_NAME not in physical, physical
    assert all("UNIQUE" in physical[name] for name in (C4_SYSTEM, C4_MOBILE)), physical
    assert state["metadata"] == baseline, state["metadata"]


async def insert(engine, record_id, *, type_="RETURN", author=AUTHOR, system_id="", mobile=""):
    values = c4._row_values(system_id, mobile, record_id)
    values.update(type=type_, author=author)
    async with engine.begin() as conn:
        await conn.execute(c4.INSERT_SQL, values)


async def outcome(engine, record_id, **row) -> str:
    try:
        await insert(engine, record_id, **row)
    except IntegrityError as exc:
        match = re.search(r'unique constraint "([^"]+)"', str(exc.orig))
        assert match, exc
        return match.group(1)
    return "accepted"


async def cleanup(engine) -> None:
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM buma_transaction WHERE author LIKE 'd0022-%'"))


async def matrix(engine, expected_index: int) -> dict:
    results = {}
    for number, (label, first, second, *expected) in enumerate(CASES):
        first_id = f"d0022a{number:02d}{secrets.token_hex(2)}"
        second_id = f"d0022b{number:02d}{secrets.token_hex(2)}"
        assert await outcome(engine, first_id, **first) == "accepted", label
        results[label] = await outcome(engine, second_id, **second)
        assert results[label] == expected[expected_index], (label, results[label])
    await cleanup(engine)
    return results


async def mobile_only_concurrency(engine, attempts: int = 8) -> dict:
    """No unique index covers this key: lock+lookup alone must keep one row."""
    lookup = IdempotencyLookup(type="RETURN", author=AUTHOR, mobile_message_id="d0022-concurrent")

    async def attempt(index: int) -> str:
        async def operation(active_engine) -> str:
            async with active_engine.connect() as conn:
                await acquire_idempotency_lock(conn, lookup)
                if await find_idempotent_transaction(conn, lookup):
                    return "duplicate"
                values = c4._row_values("", "d0022-concurrent", f"d0022c{index:02d}")
                values.update(type="RETURN", author=AUTHOR)
                await conn.execute(c4.INSERT_SQL, values)
                return "inserted"

        return await run_record_storage_transaction(engine, operation)

    results = await asyncio.gather(*(attempt(index) for index in range(attempts)))
    async with engine.connect() as conn:
        persisted = (
            await conn.execute(
                text("SELECT COUNT(*) FROM buma_transaction WHERE mobile_message_id=:m"),
                {"m": "d0022-concurrent"},
            )
        ).scalar_one()
    assert sorted(results) == ["duplicate"] * (attempts - 1) + ["inserted"], results
    assert persisted == 1, persisted
    await cleanup(engine)
    return {"attempts": attempts, "inserted": 1, "persisted_rows": persisted}


async def relation_column_types(engine) -> dict[str, str]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT table_name || '.' || column_name, data_type || ':' || udt_name "
                "FROM information_schema.columns WHERE table_schema='public' AND ("
                + " OR ".join(
                    f"(table_name='{table}' AND column_name='{column}')"
                    for table, column in RELATION_COLUMNS
                )
                + ")"
            )
        )
        return dict(rows.all())


def server_environment() -> dict[str, str]:
    # This project first (its app/ and scripts/), then the caller's PYTHONPATH,
    # which selects the PPBase under test (e.g. a PPBase worktree).
    paths = [str(PROJECT), *filter(None, os.environ.get("PYTHONPATH", "").split(os.pathsep))]
    return {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(dict.fromkeys(paths)),
        "BUMA_MASTER_ENABLE_INTERNAL_JOBS": "false",
        "PPBASE_AUTO_MIGRATE": "false",
        "PPBASE_STORAGE_BACKEND": "local",
    }


def server_ppbase(root: Path, env: dict[str, str]) -> dict:
    """Resolve ppbase/app exactly as the server command does (same python, env, cwd)."""
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
        "same_as_bench_process": True,
        "relation_multiplicity_rule": True,
        "app_from_this_project": True,
    }


def live_server_process(pid: int, env: dict[str, str], root: Path) -> dict:
    """PYTHONPATH, cwd and command of the running server, read from /proc."""
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
        "pythonpath": pythonpath.split(os.pathsep),
        "cwd_is_the_probe_cwd": True,
        "command": "python -m ppbase serve --hooks app.hooks.register:register",
    }


async def isolated_server(root: Path, url: str, report: dict):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    # A temporary cwd keeps the repository .env (local database) out of reach.
    env = server_environment()
    report["server_ppbase"] = server_ppbase(root, env)
    log = (root / "http.log").open("w")
    server = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "ppbase",
            "serve",
            "--db",
            url,
            "--dir",
            str(root / "pb_data"),
            "--migrations-dir",
            str(MIGRATIONS),
            "--hooks",
            "app.hooks.register:register",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=root,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        async with httpx.AsyncClient() as client:
            for _ in range(200):
                if server.poll() is not None:
                    raise RuntimeError("Isolated PPBase startup failed; see private http.log")
                try:
                    if (await client.get(base + "/api/health")).status_code == 200:
                        report["server_ppbase"]["live_process"] = live_server_process(
                            server.pid, env, root
                        )
                        return server, log, base
                except httpx.ConnectError:
                    pass
                await asyncio.sleep(0.1)
        raise TimeoutError("Isolated PPBase startup")
    except BaseException:
        # The caller only stops a server it received: stop this one here.
        stop_server(server, log)
        raise


def stop_server(server: subprocess.Popen, log) -> None:
    server.terminate()
    try:
        server.wait(timeout=10)
    except subprocess.TimeoutExpired:
        server.kill()
        server.wait()
    log.close()


async def exercise(root: Path, url: str, report: dict) -> None:
    baseline = baseline_indexes()
    engine = create_async_engine(url, connect_args={"ssl": "disable"})
    try:
        async with engine.connect() as conn:
            tables = "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'"
            assert (await conn.execute(text(tables))).scalar_one() == 0
        applied = await prepare_database(engine, MIGRATIONS)
        assert applied[-3:] == [D0022, D0019, VIEWS] and len(applied) == 12, applied
        report["migrations_applied"] = applied
        arrays = {f"{t}.{c}": "ARRAY:_text" for t, c in RELATION_COLUMNS}
        scalars = {f"{t}.{c}": "character varying:varchar" for t, c in RELATION_COLUMNS}
        assert await relation_column_types(engine) == arrays
        report["relation_columns_fresh"] = "text_arrays_1789236850_no_op"
        assert_source_state(await index_state(engine), baseline)
        report["source_index_matrix"] = await matrix(engine, 0)

        # 1789236850 and 1789236851 are applied after D-0022: revert them first.
        assert await run_migrations_down(engine, MIGRATIONS, count=3) == [VIEWS, D0019, D0022]
        assert await relation_column_types(engine) == scalars
        assert_c4_state(await index_state(engine), baseline)
        report["c4_index_matrix_after_rollback"] = await matrix(engine, 1)
        assert await run_migrations_up(engine, MIGRATIONS) == [D0022, D0019, VIEWS]
        assert_source_state(await index_state(engine), baseline)
        assert await relation_column_types(engine) == arrays
        report["rollback_replay"] = "down_restores_c4_metadata_and_indexes_up_restores_source"

        # Historical-style duplicates: the stricter rollback must fail atomically.
        for index in (1, 2):
            await insert(engine, f"d0022h{index:02d}", mobile="d0022-history")
        try:
            await run_migrations_down(engine, MIGRATIONS, count=3)
        except IntegrityError as exc:
            assert C4_MOBILE in str(exc.orig), exc
        else:
            raise AssertionError("C4 rollback accepted duplicate mobile history")
        assert_source_state(await index_state(engine), baseline)
        assert await relation_column_types(engine) == scalars
        # Only the transactions of 1789236851 and 1789236850 were reverted; replay them.
        assert await run_migrations_up(engine, MIGRATIONS) == [D0019, VIEWS]
        assert await relation_column_types(engine) == arrays
        async with engine.connect() as conn:
            kept = "SELECT COUNT(*) FROM buma_transaction WHERE mobile_message_id='d0022-history'"
            assert (await conn.execute(text(kept))).scalar_one() == 2
        await cleanup(engine)
        report["rollback_with_duplicate_history"] = "refused_atomically_rows_and_index_kept"

        report["mobile_only_concurrency"] = await mobile_only_concurrency(engine)
        c4.DATABASE_URL = url
        await c4.main()
        report["c4_runtime"] = "rollback_concurrency_lookup_cleanup_ok"

        server, log, base = await isolated_server(root, url, report)
        try:
            c7.BASE_URL, c7.DATABASE_URL = base, url
            await c7.main()
            report["c7_runtime"] = "passed_with_cleanup"
            c9.BASE_URL, c9.DATABASE_URL = base, url
            c9.TARGET = "disposable Docker PostgreSQL 16 and isolated PPBase HTTP (D-0022)"
            c9.REPORT = root / "c9-runtime-report.json"
            await c9.main()
            c9_report = json.loads(c9.REPORT.read_text())
            report["c9_runtime"] = {
                key: c9_report[key] for key in ("status", "routes_exercised", "proofs", "cleanup")
            }
        finally:
            stop_server(server, log)

        assert_source_state(await index_state(engine), baseline)
        async with engine.connect() as conn:
            left = "SELECT COUNT(*) FROM buma_transaction WHERE author LIKE 'd0022-%'"
            assert (await conn.execute(text(left))).scalar_one() == 0
        report["final_state"] = "source_index_only_no_d0022_rows"
    finally:
        await engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True, help="New JSON report path")
    args = parser.parse_args()
    if args.report.exists():
        parser.error("Report must be a new path")
    name = "buma-d0022-" + uuid.uuid4().hex[:12]
    password = secrets.token_hex(16)
    report = {
        "timestamp_utc": datetime.now(UTC).isoformat(),
        "scope": "d0022_disposable_postgresql_runtime",
        "production_data_used": False,
        "local_buma_database_used": False,
        "status": "failed",
    }
    created = False
    try:
        with tempfile.TemporaryDirectory(prefix="buma-d0022-") as folder:
            root = Path(folder)
            command(
                "docker",
                "run",
                "--detach",
                "--rm",
                "--name",
                name,
                "--label",
                "buma.scope=d0022-idempotency-runtime",
                "--publish",
                "127.0.0.1::5432",
                "--env",
                "POSTGRES_USER=d0022",
                "--env",
                f"POSTGRES_PASSWORD={password}",
                "--env",
                "POSTGRES_DB=buma_d0022_runtime",
                "postgres:16-alpine",
            )
            created = True
            port = command("docker", "port", name, "5432/tcp").rsplit(":", 1)[1]
            url = f"postgresql+asyncpg://d0022:{password}@127.0.0.1:{port}/buma_d0022_runtime"
            for _ in range(150):
                try:
                    command("docker", "exec", name, "pg_isready", "-U", "d0022", "-h", "127.0.0.1")
                    break
                except subprocess.CalledProcessError:
                    time.sleep(0.2)
            asyncio.run(exercise(root, url, report))
            report["status"] = "passed"
    except BaseException as exc:
        # Never leak credentials or bind parameters into the report.
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
