"""D-0019 runtime proof of migration 1789236850 on an owned, disposable PostgreSQL.

No --db argument exists: this cannot target an existing database. One Docker
postgres:16-alpine container (labelled, random loopback port) holds four
databases and is removed at the end, even after a failure. A child process
loads a PPBase that predates the D-0019 rule (``--legacy-ppbase``, checked)
and prepares three of them; this process loads the corrected PPBase selected
by PYTHONPATH and checks:

* ``before_fix``: the ten migrations of ``--legacy-ref`` (the BUMA commit
  before this change) and one value written by the old record service, then
  synthetic edge values in SQL. Proves the refusals, the lock against
  concurrent writers, the rollback after DDL, the conversion, the failure of
  the cached statements of an engine left running, the verifier functions and
  scripts in both modes, the PPBase model checks, the VARCHAR(15) limit kept by
  the single relations of a migrated database, the refused and accepted down,
  the replay and a down/up of the last five migrations (C5 seed included).
* ``partial``: only the first six migrations (up to 1789236845) by the old
  PPBase, then this branch's five remaining ones by the corrected PPBase.
* ``legacy_head``: this branch's migrations by the old PPBase, which applies
  them up to 1789236849 and stops atomically at 1789236850; the corrected
  PPBase then finishes. The child also records that BUMA's hook modules no
  longer import with the old PPBase.
* ``fresh``: migrated by the corrected PPBase, first without 1789236850 and
  then with it, which must change nothing; verifiers and the 42 views run on
  it, then the single relation writes, the down/up of the last five
  migrations and the verifier scripts with their default (fresh) mode.

A pre-correction PPBase is one without ``ppbase.models.field_multiplicity``
(for instance PPBase commit c6821aa). ``--legacy-ppbase DIR`` loads the
``ppbase`` package of DIR as is. With ``--legacy-ppbase-commit REV``, DIR is a
PPBase Git repository and REV is exported read-only (``git archive``) into the
temporary directory, whatever branch DIR has checked out:

    python -m scripts.c11_relation_columns_runtime --legacy-ppbase ../ppbase \
        --legacy-ppbase-commit c6821aa --report NEW.json

All values are synthetic.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import importlib.util
import io
import json
import os
import secrets
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

PROJECT = Path(__file__).resolve().parents[1]
MIGRATIONS = PROJECT / "pb_migrations"
SCHEMA = Path(
    os.environ.get(
        "BUMA_SCHEMA_EXPORT", "/Users/omarsisss/Downloads/buma-master-pb_schema-corrected.json"
    )
)
D0019 = "1789236850_c11_relation_multiple_columns.py"
LEGACY_MIGRATIONS = (
    "1789236840_buma_master_schema_baseline.py",
    "1789236841_users_username_compat.py",
    "1789236842_buma_master_views.py",
    "1789236843_agent_users_username_compat.py",
    "1789236844_auth_compat.py",
    "1789236845_c4_transaction_idempotency.py",
    "1789236846_c5_reference_defaults.py",
    "1789236847_c10_view_alias_case.py",
    "1789236848_c10_sqlite_rowid.py",
    "1789236849_c11_source_transaction_idempotency.py",
)
HEAD_MIGRATIONS = (*LEGACY_MIGRATIONS, D0019)
PARTIAL_MIGRATIONS = LEGACY_MIGRATIONS[:6]  # up to 1789236845, before the C5 seed
LAST_FIVE = HEAD_MIGRATIONS[6:]  # 1789236846 (C5 seed) .. 1789236850
C5 = LEGACY_MIGRATIONS[6]
COLUMNS = (
    ("buma_customer", "pdv"),
    ("buma_pilotage_ticket", "tag"),
    ("buma_wallet", "managers"),
)
TABLES = tuple(table for table, _ in COLUMNS)
LEGACY_ENV = "BUMA_C11_WSE_LEGACY_DB"
RESULT_PREFIX = "C11_RELATION_COLUMNS_LEGACY "
LEGACY_DATABASES = ("before_fix", "partial", "legacy_head")
# Written by the legacy PPBase record service (old rule: scalar relation).
LEGACY_WRITE = ("buma_customer", "wsecust00000002", "wsepdv000000001")
# table, id, scalar stored before the fix, array after up, scalar after down.
ROWS = (
    ("buma_customer", "CUSTOMER_DIVERS", "", [], ""),  # 1789236846 seed (column default)
    ("buma_customer", "wsecust00000001", "", [], ""),
    ("buma_customer", "wsecust00000002", "wsepdv000000001", ["wsepdv000000001"], None),
    ("buma_customer", "wsecust00000003", "[]", [], ""),
    ("buma_customer", "wsecust00000004", "wsep0001,wsep02", ["wsep0001,wsep02"], None),
    ("buma_pilotage_ticket", "wseticket000001", "", [], ""),
    ("buma_pilotage_ticket", "wseticket000002", "wsetag000000001", ["wsetag000000001"], None),
    ("buma_wallet", "wsewallet000001", "", [], ""),
    ("buma_wallet", "wsewallet000002", "wseuser00000001", ["wseuser00000001"], None),
    ("buma_wallet", "wsewallet000003", "[]", [], ""),
)
# Written by a concurrent transaction that commits while up waits for its lock.
CONCURRENT_ROW = ("buma_wallet", "wsewallet000004", "wseuser00000002")
# Created through the corrected record service after up (PrepareValue).
SERVICE_ROW = ("buma_customer", "wsecust00000005", "wsepdv000000003")
REFUSED_UP = (
    ("python_repr_list", "buma_wallet", "['wseuser01']"),
    ("json_list", "buma_customer", '["a1","b2"]'),
    ("json_empty_list_with_space", "buma_pilotage_ticket", "[ ]"),
)
REFUSED_DOWN = (
    ("two_elements", ["wsepdv000000001", "wsepdv000000002"]),
    ("element_over_15_characters", ["wsepdv0000000016"]),
    ("empty_element", [""]),
    ("element_with_bracket", ["[x]"]),
)
REFUSED_ID = "wserefused00001"
# Single relation (maxSelect 1) written by BUMA without the API target check:
# /api2/return -> app.legacy.commerce.make_return -> app.legacy.records.create,
# like the frozen sale.ts l.725-736 (new Record + Dao.SaveRecord).
SINGLE_RELATION = ("buma_return", "pdv")
LONG_SINGLE_VALUES = (
    ("sixteen_characters", "wseorphan0000016"),
    ("fifteen_characters_and_a_space", "wseorphan000015 "),
)
SINGLE_WRITES_MIGRATED = {
    "sixteen_characters": "refused (SQLSTATE 22001)",
    "fifteen_characters_and_a_space": "stored truncated to 15 characters",
}
SINGLE_WRITES_FRESH = {label: "stored as is" for label, _ in LONG_SINGLE_VALUES}


def command(*args, **kwargs):
    return subprocess.check_output(args, text=True, stderr=subprocess.STDOUT, **kwargs).strip()


def identity(module) -> dict:
    package = Path(module.__file__).resolve().parent
    try:
        commit = command("git", "-C", str(package), "rev-parse", "HEAD")
    except (OSError, subprocess.CalledProcessError):
        commit = None
    return {
        "package_dir": str(package),
        "git_commit": commit,
        "relation_multiplicity_rule": importlib.util.find_spec("ppbase.models.field_multiplicity")
        is not None,
    }


def engine_for(url: str):
    # Column types change under pooled connections here: with asyncpg's prepared
    # statement cache on, a statement prepared before the change fails with
    # InvalidCachedStatementError until SQLAlchemy clears its caches, as in a
    # server left running during the migration (proven in exercise_migrated
    # with an engine configured like ppbase.db.engine.init_engine).
    return create_async_engine(
        url + "?prepared_statement_cache_size=0", connect_args={"ssl": "disable"}
    )


def server_like_engine(url: str):
    """Same options as ppbase.db.engine.init_engine: asyncpg statement cache on."""
    return create_async_engine(url, pool_pre_ping=True, connect_args={"ssl": "disable"})


async def applied_files(engine) -> list[str]:
    async with engine.connect() as conn:
        rows = await conn.execute(text("SELECT file FROM _migrations ORDER BY file"))
        return [row[0] for row in rows.all()]


async def column_type(engine, table: str, column: str) -> str | None:
    async with engine.connect() as conn:
        return (
            await conn.execute(
                text(
                    "SELECT pg_catalog.format_type(attribute.atttypid, attribute.atttypmod) "
                    "FROM pg_catalog.pg_attribute AS attribute "
                    "JOIN pg_catalog.pg_class AS relation ON relation.oid = attribute.attrelid "
                    "JOIN pg_catalog.pg_namespace AS namespace "
                    "ON namespace.oid = relation.relnamespace "
                    "WHERE namespace.nspname = 'public' AND relation.relname = :table "
                    "AND attribute.attname = :column AND NOT attribute.attisdropped"
                ),
                {"table": table, "column": column},
            )
        ).scalar_one_or_none()


async def customer_divers_pdv(engine):
    async with engine.connect() as conn:
        return (
            await conn.execute(text("SELECT pdv FROM buma_customer WHERE id = 'CUSTOMER_DIVERS'"))
        ).scalar_one_or_none()


def hook_import_failure() -> dict:
    """The first BUMA hook module (registration order) whose import fails.

    Registration can fail even earlier: some modules import lazily in register().
    """
    from app.hooks.register import HOOK_MODULES

    for module_name in HOOK_MODULES:
        try:
            importlib.import_module(module_name)
        except ModuleNotFoundError as exc:
            return {"module": module_name, "error": type(exc).__name__, "missing": exc.name}
    return {"module": None, "error": None, "missing": None}


async def legacy_phase(settings: dict) -> dict:
    """Runs in the child process, with the PPBase before the D-0019 rule."""
    import ppbase
    from ppbase.services.database_preparation import prepare_database
    from ppbase.services.record_service import create_record, resolve_collection

    loaded = identity(ppbase)
    if loaded["relation_multiplicity_rule"]:
        raise RuntimeError("--legacy-ppbase already contains the D-0019 multiplicity rule")
    result = {**loaded, "hook_modules": hook_import_failure(), "databases": {}}
    for name, task in settings.items():
        engine = engine_for(task["url"])
        try:
            outcome: dict = {"error": None}
            try:
                await prepare_database(engine, Path(task["migrations"]))
            except Exception as exc:  # recorded by name; the parent asserts it
                outcome["error"] = type(exc).__name__
            outcome["applied"] = await applied_files(engine)
            if task.get("legacy_write"):
                table, record_id, value = LEGACY_WRITE
                collection = await resolve_collection(engine, table)
                written = await create_record(engine, collection, {"id": record_id, "pdv": value})
                outcome["record_service_pdv_is_scalar"] = written["pdv"] == value
            outcome["pdv_column"] = await column_type(engine, "buma_customer", "pdv")
            outcome["customer_divers_pdv"] = await customer_divers_pdv(engine)
            result["databases"][name] = outcome
        finally:
            await engine.dispose()
    return result


async def catalog(engine, tables=TABLES) -> dict:
    """Columns (position, type, NOT NULL, default) and indexes of the tables."""
    async with engine.connect() as conn:
        columns = await conn.execute(
            text(
                "SELECT relation.relname, attribute.attnum, attribute.attname, "
                "pg_catalog.format_type(attribute.atttypid, attribute.atttypmod), "
                "attribute.attnotnull, "
                "pg_catalog.pg_get_expr(definition.adbin, definition.adrelid) "
                "FROM pg_catalog.pg_attribute AS attribute "
                "JOIN pg_catalog.pg_class AS relation ON relation.oid = attribute.attrelid "
                "JOIN pg_catalog.pg_namespace AS namespace "
                "ON namespace.oid = relation.relnamespace "
                "LEFT JOIN pg_catalog.pg_attrdef AS definition "
                "ON definition.adrelid = attribute.attrelid "
                "AND definition.adnum = attribute.attnum "
                "WHERE namespace.nspname = 'public' AND relation.relname = ANY(:tables) "
                "AND attribute.attnum > 0 AND NOT attribute.attisdropped ORDER BY 1, 2"
            ),
            {"tables": list(tables)},
        )
        indexes = await conn.execute(
            text(
                "SELECT tablename, indexname, indexdef FROM pg_indexes "
                "WHERE schemaname = 'public' AND tablename = ANY(:tables) ORDER BY 1, 2"
            ),
            {"tables": list(tables)},
        )
        return {
            "columns": [list(row) for row in columns.all()],
            "indexes": [list(row) for row in indexes.all()],
        }


async def schema_shape(engine) -> dict:
    """Every public column (type, NOT NULL, default; not its position), index and view."""
    async with engine.connect() as conn:
        columns = await conn.execute(
            text(
                "SELECT relation.relname, relation.relkind::text, attribute.attname, "
                "pg_catalog.format_type(attribute.atttypid, attribute.atttypmod), "
                "attribute.attnotnull, "
                "pg_catalog.pg_get_expr(definition.adbin, definition.adrelid) "
                "FROM pg_catalog.pg_attribute AS attribute "
                "JOIN pg_catalog.pg_class AS relation ON relation.oid = attribute.attrelid "
                "JOIN pg_catalog.pg_namespace AS namespace "
                "ON namespace.oid = relation.relnamespace "
                "LEFT JOIN pg_catalog.pg_attrdef AS definition "
                "ON definition.adrelid = attribute.attrelid "
                "AND definition.adnum = attribute.attnum "
                "WHERE namespace.nspname = 'public' AND relation.relkind IN ('r', 'v') "
                "AND attribute.attnum > 0 AND NOT attribute.attisdropped ORDER BY 1, 3"
            )
        )
        indexes = await conn.execute(
            text(
                "SELECT tablename, indexname, indexdef FROM pg_indexes "
                "WHERE schemaname = 'public' ORDER BY 1, 2"
            )
        )
        views = await conn.execute(
            text("SELECT viewname, definition FROM pg_views WHERE schemaname = 'public' ORDER BY 1")
        )
        return {
            "columns": [list(row) for row in columns.all()],
            "indexes": [list(row) for row in indexes.all()],
            "views": [list(row) for row in views.all()],
        }


def three_columns(state: dict) -> dict:
    """The converted columns and their managed index only."""
    wanted = {(table, column) for table, column in COLUMNS}
    names = {f"idx_{table}_{column}" for table, column in COLUMNS}
    return {
        "columns": [row[2:] for row in state["columns"] if (row[0], row[2]) in wanted],
        "indexes": [row for row in state["indexes"] if row[1] in names],
    }


async def values(engine) -> dict:
    result = {}
    async with engine.connect() as conn:
        for table, column in COLUMNS:
            rows = await conn.execute(text(f'SELECT id, "{column}" FROM "{table}" ORDER BY id'))
            result[f"{table}.{column}"] = {row[0]: row[1] for row in rows.all()}
    return result


async def collection_rows(engine, *, timestamps: bool = True) -> dict:
    async with engine.connect() as conn:
        rows = (await conn.execute(text("SELECT * FROM _collections ORDER BY id"))).mappings().all()
    skipped = set() if timestamps else {"created", "updated"}
    return {
        row["name"]: json.dumps(
            {key: value for key, value in row.items() if key not in skipped},
            default=str,
            sort_keys=True,
            ensure_ascii=False,
        )
        for row in rows
    }


async def seed_rows(engine) -> dict:
    """Rows of the C5 seed (1789236846), without their created/updated timestamps."""
    c5 = load_migration(C5)
    tables = {
        "buma_article_stockage": ["STOCK_PRINCIPAL"],
        "buma_payment_method": ["PAYMENT_CASH_12", "PAYMENT_MOBILE_", "PAYMENT_CARTE_1"],
        "buma_customer": ["CUSTOMER_DIVERS"],
        "buma_supplier": ["SUPPLIER_DIVERS"],
        "buma_apps_roles": [role["id"] for role in c5.APP_ROLES],
    }
    rows = {}
    async with engine.connect() as conn:
        for table, ids in tables.items():
            result = await conn.execute(
                text(f'SELECT * FROM "{table}" WHERE id = ANY(:ids) ORDER BY id'), {"ids": ids}
            )
            rows[table] = [
                json.dumps(
                    {k: v for k, v in row.items() if k not in ("created", "updated")},
                    default=str,
                    sort_keys=True,
                )
                for row in result.mappings()
            ]
            assert len(rows[table]) == len(ids), (table, len(rows[table]))
    return rows


async def sql(engine, statement: str, params: dict | None = None) -> None:
    async with engine.begin() as conn:
        await conn.execute(text(statement), params or {})


def expected_values(stage: str) -> dict:
    """Values of the synthetic rows: legacy scalars, arrays after up, scalars after down."""
    result: dict[str, dict] = {f"{t}.{c}": {} for t, c in COLUMNS}
    column = dict(COLUMNS)
    for table, record_id, scalar, array, restored in ROWS:
        value = {"legacy": scalar, "array": array, "down": scalar if restored is None else restored}
        result[f"{table}.{column[table]}"][record_id] = value[stage]
    if stage != "legacy":
        table, record_id, value = CONCURRENT_ROW
        result[f"{table}.{column[table]}"][record_id] = [value] if stage == "array" else value
    return result


async def migration_error(operation) -> str:
    try:
        await operation
    except Exception as exc:  # the migration module is loaded by path: match by name
        if type(exc).__name__ != "RelationColumnError":
            raise
        return str(exc)
    raise AssertionError("1789236850 accepted a state it must refuse")


async def wait_for_lock_wait(engine, timeout: float = 15.0) -> None:
    """Return once a session of this database waits on the migration LOCK TABLE."""
    deadline = time.monotonic() + timeout
    async with engine.connect() as conn:
        while time.monotonic() < deadline:
            waiting = (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM pg_stat_activity "
                        "WHERE datname = current_database() AND wait_event_type = 'Lock' "
                        "AND query LIKE 'LOCK TABLE %'"
                    )
                )
            ).scalar_one()
            await conn.commit()  # statistics snapshots are per transaction
            if waiting:
                return
            await asyncio.sleep(0.05)
    raise TimeoutError("1789236850 did not wait for the concurrent writer")


async def model_check(engine) -> dict:
    """Corrected PPBase model checks on every base/auth collection (rolled back)."""
    from ppbase.db.schema_manager import (
        reconcile_multi_value_columns,
        validate_collection_indexes,
    )
    from ppbase.services.record_service import get_all_collections

    failures: dict[str, str] = {}
    converted: dict[str, list[str]] = {}
    checked = 0
    collections = [c for c in await get_all_collections(engine) if c.type != "view"]
    async with engine.connect() as conn:
        for collection in collections:
            checked += 1
            try:
                async with conn.begin_nested():
                    await validate_collection_indexes(conn, collection)
            except ValueError as exc:
                failures[collection.name] = type(exc).__name__ + ": " + str(exc)[:300]
            names = await reconcile_multi_value_columns(conn, collection)
            if names:
                converted[collection.name] = names
        await conn.rollback()
    return {
        "base_auth_collections": checked,
        "validate_collection_indexes_failures": failures,
        "reconcile_multi_value_columns_would_convert": converted,
    }


async def record_reads(engine, rows: dict) -> dict:
    """The relation value each synthetic record has through the PPBase record service."""
    from ppbase.services.record_service import get_record, resolve_collection

    reads = {}
    for table, column in COLUMNS:
        collection = await resolve_collection(engine, table)
        for record_id in rows[f"{table}.{column}"]:
            reads[record_id] = (await get_record(engine, collection, record_id))[column]
    return reads


def load_migration(name: str = D0019):
    spec = importlib.util.spec_from_file_location(f"wse_{name[:10]}", MIGRATIONS / name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def with_blocker(engine, statement: str, params: dict, operation):
    """Hold an uncommitted write while ``operation`` starts, then commit it."""
    async with engine.connect() as blocker:
        await blocker.begin()
        await blocker.execute(text(statement), params)
        task = asyncio.ensure_future(operation)
        try:
            await wait_for_lock_wait(engine)
        finally:
            await blocker.commit()
        return task


async def single_relation_writes(engine) -> dict:
    """Over-long ids written by BUMA's internal writer into a single relation."""
    from app.legacy.records import create

    table, column = SINGLE_RELATION
    outcome = {}
    for label, value in LONG_SINGLE_VALUES:
        try:
            row = await create(engine, table, {column: value, "note": "wse single relation"})
        except DBAPIError as exc:
            outcome[label] = f"refused (SQLSTATE {getattr(exc.orig, 'sqlstate', None)})"
            continue
        await sql(engine, f'DELETE FROM "{table}" WHERE id = :id', {"id": row["id"]})
        stored = row[column]
        if stored == value:
            outcome[label] = "stored as is"
        else:
            assert stored == value.rstrip(" ")[:15], len(stored)
            outcome[label] = f"stored truncated to {len(stored)} characters"
    async with engine.connect() as conn:
        left = f"SELECT count(*) FROM \"{table}\" WHERE note = 'wse single relation'"
        assert (await conn.execute(text(left))).scalar_one() == 0
    return outcome


async def down_up_last_five(engine, url: str, mode: str) -> dict:
    """Revert 1789236846..1789236850 and apply them again: same schema, rows and seed."""
    from ppbase.services.database_preparation import (
        prepare_database,
        prepare_database_and_revert,
    )

    from scripts.generate_native_baseline import load_schema
    from scripts.verify_schema_baseline import read_collections, verify
    from scripts.verify_view_queries import verify as verify_views

    async def snapshot() -> list:
        return [
            await schema_shape(engine),
            await values(engine),
            await seed_rows(engine),
            await collection_rows(engine, timestamps=False),
            await applied_files(engine),
        ]

    before = await snapshot()
    reverted = await prepare_database_and_revert(engine, MIGRATIONS, count=5)
    assert reverted == list(reversed(LAST_FIVE)), reverted
    # 1789236850 down restored VARCHAR(15); 1789236846 down removed the seed.
    assert await column_type(engine, "buma_customer", "pdv") == "character varying(15)"
    assert await customer_divers_pdv(engine) is None
    assert await prepare_database(engine, MIGRATIONS) == list(LAST_FIVE)
    after = await snapshot()
    labels = ("schema", "values", "seed_rows", "collections", "migrations")
    differing = [label for label, a, b in zip(labels, after, before, strict=True) if a != b]
    assert not differing, differing
    assert after[1]["buma_customer.pdv"]["CUSTOMER_DIVERS"] == []
    check = verify(
        load_schema(SCHEMA, include_views=False)[0],
        await read_collections(url),
        relation_columns=mode,
    )
    assert check["ok"], check
    views = await verify_views(url, load_schema(SCHEMA, include_views=True)[0])
    assert len(views) == 42 and all(item["status"] == "OK" for item in views), views
    return {
        "reverted": reverted,
        "between": "buma_customer.pdv character varying(15), C5 seed removed",
        "replayed": list(LAST_FIVE),
        "equal_after_replay": list(labels),
        "customer_divers_pdv": "empty array",
        "verifier_mode": mode,
        "views_ok": len(views),
    }


def only_varchar_single_errors(errors: list[str]) -> bool:
    suffix = ": single relation stored as character varying(15)"
    return all(error.endswith(suffix) for error in errors)


def verifier_cli(script: str, url: str, *options: str) -> tuple[int, dict]:
    """Run a verifier script like the Makefile targets (database from PPBASE_DATABASE_URL)."""
    completed = subprocess.run(
        [sys.executable, str(PROJECT / "scripts" / script), *options],
        cwd=PROJECT,
        env={**os.environ, "PPBASE_DATABASE_URL": url, "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
    )
    if completed.returncode not in (0, 1):
        raise RuntimeError(f"{script} failed:\n" + completed.stderr[-2000:])
    return completed.returncode, json.loads(completed.stdout)


VERIFIER_SCRIPTS = ("verify_schema_baseline.py", "verify_complete_schema.py")


def verifier_clis_on_migrated_database(url: str) -> dict:
    """Default (fresh) refuses the 188 VARCHAR(15) single relations; migrated accepts."""
    result = {}
    for script in VERIFIER_SCRIPTS:
        code, output = verifier_cli(script, url)
        columns = output["relation_columns"]
        assert code == 1 and columns["mode"] == "fresh", (script, code, columns["mode"])
        assert len(columns["errors"]) == 188 and only_varchar_single_errors(columns["errors"])
        assert columns["hint"] and "--relation-columns migrated" in columns["hint"]
        code_migrated, migrated = verifier_cli(script, url, "--relation-columns", "migrated")
        assert code_migrated == 0 and migrated["ok"], (script, migrated["relation_columns"])
        result[script] = {
            "default": "exit 1: fresh mode, 188 varchar(15) single relations, hint",
            "--relation-columns migrated": "exit 0",
        }
    return result


def verifier_clis_on_fresh_database(url: str) -> dict:
    result = {}
    for script in VERIFIER_SCRIPTS:
        code, output = verifier_cli(script, url)
        assert code == 0 and output["ok"] and output["relation_columns"]["mode"] == "fresh"
        result[script] = {"default": "exit 0 (fresh mode)"}
    return result


async def exercise_migrated(url: str, report: dict) -> dict:
    from ppbase.services.database_preparation import (
        prepare_database,
        prepare_database_and_revert,
    )
    from ppbase.services.migration_runner import MigrationApp
    from ppbase.services.record_service import (
        create_record,
        get_record,
        list_records,
        resolve_collection,
    )

    from scripts.generate_native_baseline import load_schema
    from scripts.verify_complete_schema import read_collections as read_complete
    from scripts.verify_complete_schema import verify as verify_complete
    from scripts.verify_schema_baseline import read_collections, verify
    from scripts.verify_view_queries import verify as verify_views

    engine = engine_for(url)
    definitions = load_schema(SCHEMA, include_views=False)[0]
    view_definitions = load_schema(SCHEMA, include_views=True)[0]
    try:
        for table, record_id, scalar, _, _ in ROWS:
            if record_id == "CUSTOMER_DIVERS" or (table, record_id) == LEGACY_WRITE[:2]:
                continue
            column = dict(COLUMNS)[table]
            await sql(
                engine,
                f'INSERT INTO "{table}" (id, "{column}") VALUES (:id, :value)',
                {"id": record_id, "value": scalar},
            )
        legacy_catalog = await catalog(engine)
        legacy_collections = await collection_rows(engine)
        assert await values(engine) == expected_values("legacy")
        legacy_three = three_columns(legacy_catalog)
        assert [row[1:] for row in legacy_three["columns"]] == [
            ["character varying(15)", True, "''::character varying"]
        ] * 3, legacy_three
        assert all("USING btree" in row[2] for row in legacy_three["indexes"]), legacy_three
        before = verify(definitions, await read_collections(url), relation_columns="migrated")
        assert not before["ok"] and sorted(before["relation_columns"]["errors"]) == sorted(
            [f"{t}.{c}: multiple relation stored as character varying(15)" for t, c in COLUMNS]
            + [f"{t}.{c}: managed index method btree" for t, c in COLUMNS]
        ), before["relation_columns"]
        assert not before["metadata_mismatches"], before["metadata_mismatches"]
        report["legacy_state"] = {
            "three_columns": "character varying(15) NOT NULL DEFAULT '' + btree",
            "record_service_wrote_scalar": True,
            "verifier_errors": len(before["relation_columns"]["errors"]),
        }

        async def unchanged(label: str) -> None:
            assert await catalog(engine) == legacy_catalog, label
            assert await values(engine) == expected_values("legacy"), label
            assert D0019 not in await applied_files(engine), label

        refusals = {}
        for label, table, value in REFUSED_UP:
            column = dict(COLUMNS)[table]
            insert = f'INSERT INTO "{table}" (id, "{column}") VALUES (:id, :value)'
            await sql(engine, insert, {"id": REFUSED_ID, "value": value})
            message = await migration_error(prepare_database(engine, MIGRATIONS))
            assert f"{table}.{column}: 1 value(s) contain '['" in message, message
            await sql(engine, f'DELETE FROM "{table}" WHERE id = :id', {"id": REFUSED_ID})
            await unchanged(label)
            refusals[label] = "refused_atomically"
        # A refused value committed while up waits for its lock is still seen.
        task = await with_blocker(
            engine,
            'INSERT INTO "buma_wallet" (id, managers) VALUES (:id, :value)',
            {"id": REFUSED_ID, "value": "['wseuser01']"},
            prepare_database(engine, MIGRATIONS),
        )
        message = await migration_error(task)
        assert "buma_wallet.managers: 1 value(s) contain '['" in message, message
        await sql(engine, 'DELETE FROM "buma_wallet" WHERE id = :id', {"id": REFUSED_ID})
        await unchanged("concurrent_refusal")
        refusals["committed_while_waiting_for_the_lock"] = "refused_atomically"
        report["up_refusals"] = refusals

        # DDL and data changes belong to the migration transaction.
        module = load_migration()
        async with AsyncSession(engine, expire_on_commit=False) as session:
            await session.begin()
            await module.up(MigrationApp(session, engine))
            inside = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM information_schema.columns "
                        "WHERE table_schema = 'public' AND udt_name = '_text' AND "
                        "(table_name, column_name) IN (('buma_customer', 'pdv'), "
                        "('buma_pilotage_ticket', 'tag'), ('buma_wallet', 'managers'))"
                    )
                )
            ).scalar_one()
            await session.rollback()
        assert inside == 3
        await unchanged("rollback")
        report["rollback_after_ddl"] = "converted_inside_the_transaction_then_fully_restored"

        # An engine left running (statement cache on, like a PPBase server) has
        # prepared a read of pdv before up; it fails once after the type change.
        running = server_like_engine(url)
        stale_read = text("SELECT pdv FROM buma_customer WHERE id = :id")
        stale_failures: list[str] = []
        try:
            async with running.connect() as conn:
                seen = (await conn.execute(stale_read, {"id": LEGACY_WRITE[1]})).scalar_one()
                assert seen == LEGACY_WRITE[2]
            # up waits for a concurrent writer and converts its committed value.
            table, record_id, value = CONCURRENT_ROW
            task = await with_blocker(
                engine,
                'INSERT INTO "buma_wallet" (id, managers) VALUES (:id, :value)',
                {"id": record_id, "value": value},
                prepare_database(engine, MIGRATIONS),
            )
            assert await task == [D0019]
            for _ in range(2):
                try:
                    async with running.connect() as conn:
                        result = await conn.execute(stale_read, {"id": LEGACY_WRITE[1]})
                        seen = result.scalar_one()
                    break
                except DBAPIError as exc:
                    stale_failures.append(type(exc.orig).__name__)
        finally:
            await running.dispose()
        assert stale_failures == ["InvalidCachedStatementError"], stale_failures
        assert seen == [LEGACY_WRITE[2]], seen
        report["engine_left_running_during_up"] = {
            "engine": "options of ppbase.db.engine.init_engine (asyncpg statement cache on)",
            "first_read_of_pdv_after_up": stale_failures[0],
            "next_read": "one-element list",
            "operational_rule": "stop PPBase servers before 1789236850, start them after",
        }

        assert await values(engine) == expected_values("array")
        converted_catalog = await catalog(engine)
        converted_three = three_columns(converted_catalog)
        assert [row[1:] for row in converted_three["columns"]] == [
            ["text[]", True, "'{}'::text[]"]
        ] * 3, converted_three
        assert all("USING gin" in row[2] for row in converted_three["indexes"]), converted_three
        # Only the three columns and their index changed.
        assert [r for r in converted_catalog["columns"] if r not in legacy_catalog["columns"]] == [
            r for r in converted_catalog["columns"] if (r[0], r[2]) in set(COLUMNS)
        ]
        assert sorted(
            r[1] for r in converted_catalog["indexes"] if r not in legacy_catalog["indexes"]
        ) == sorted(f"idx_{t}_{c}" for t, c in COLUMNS)
        after_collections = await collection_rows(engine)
        changed = sorted(
            n for n in legacy_collections if after_collections[n] != legacy_collections[n]
        )
        assert not set(changed) & set(TABLES), changed
        report["up"] = {
            "applied": [D0019],
            "values": {
                "empty_to_empty_array": sum(row[2] == "" for row in ROWS),
                "json_empty_list_to_empty_array": sum(row[2] == "[]" for row in ROWS),
                "scalar_to_one_element_array": sum(len(row[3]) == 1 for row in ROWS) + 1,
                "comma_kept_inside_one_element": True,
            },
            "concurrent_writer": "waited_for_the_lock_then_converted",
            "three_columns": "text[] NOT NULL DEFAULT '{}' + gin",
            "other_columns_and_indexes_unchanged": True,
            "three_collections_metadata_unchanged": True,
            "collection_rows_changed_by_the_corrected_bootstrap": changed,
        }

        after = verify(definitions, await read_collections(url), relation_columns="migrated")
        assert after["ok"], after
        strict = verify(definitions, await read_collections(url), relation_columns="fresh")
        assert not strict["metadata_mismatches"]
        assert len(strict["relation_columns"]["errors"]) == 188
        assert only_varchar_single_errors(strict["relation_columns"]["errors"])
        assert strict["relation_columns"]["hint"], strict["relation_columns"]
        complete = verify_complete(
            view_definitions, await read_complete(url), relation_columns="migrated"
        )
        assert complete["ok"], complete
        views = await verify_views(url, view_definitions)
        assert len(views) == 42 and all(item["status"] == "OK" for item in views), views
        assert await prepare_database(engine, MIGRATIONS) == []
        report["verifiers_after_up"] = {
            "schema_metadata_mismatches": 0,
            "migrated_mode": {
                key: after["relation_columns"][key] for key in ("multiple", "single", "ok")
            },
            "fresh_mode_errors": len(strict["relation_columns"]["errors"]),
            "fresh_mode_errors_are_varchar_single_relations": True,
            "complete_schema_ok_migrated_mode": complete["ok"],
            "views_ok": len(views),
            "second_run_pending": [],
            "clis": verifier_clis_on_migrated_database(url),
        }

        model = await model_check(engine)
        # _superusers is stored in _admins: PPBase's index model does not apply
        # to it, on a fresh database too (checked in exercise_fresh).
        assert set(model["validate_collection_indexes_failures"]) == {"_superusers"}, model
        assert model["reconcile_multi_value_columns_would_convert"] == {}, model
        report["ppbase_model_after_up"] = {
            "base_auth_collections": model["base_auth_collections"],
            "validate_collection_indexes": "ok except _superusers (stored in _admins)",
            "reconcile_multi_value_columns_would_convert": {},
        }

        customers = await resolve_collection(engine, "buma_customer")
        wallets = await resolve_collection(engine, "buma_wallet")
        read = {
            record_id: (await get_record(engine, customers, record_id))["pdv"]
            for record_id in ("CUSTOMER_DIVERS", "wsecust00000002", "wsecust00000004")
        }
        assert read == {
            "CUSTOMER_DIVERS": [],
            "wsecust00000002": ["wsepdv000000001"],
            "wsecust00000004": ["wsep0001,wsep02"],
        }, read
        listed = await list_records(engine, wallets, filter_str='managers ~ "wseuser00000001"')
        assert [item["id"] for item in listed["items"]] == ["wsewallet000002"], listed
        table, record_id, value = SERVICE_ROW
        created = await create_record(engine, customers, {"id": record_id, "pdv": value})
        assert created["pdv"] == [value], created["pdv"]
        report["record_service_after_up"] = {
            "read_shapes": "lists",
            "filter_on_converted_column": "ok",
            "string_write_stored_as_one_element_list": True,
        }

        # The 188 single relations keep VARCHAR(15) here (TEXT on a new database).
        writes = await single_relation_writes(engine)
        assert writes == SINGLE_WRITES_MIGRATED, writes
        report["single_relation_internal_writes"] = {
            "path": "app.legacy.records.create (/api2/return), buma_return.pdv maxSelect 1",
            "column": await column_type(engine, *SINGLE_RELATION),
            "outcomes": writes,
        }

        converted_values = await values(engine)
        down_refusals = {}
        for label, array in REFUSED_DOWN:
            await sql(
                engine,
                "UPDATE buma_customer SET pdv = CAST(:value AS text[]) "
                "WHERE id = 'wsecust00000002'",
                {"value": array},
            )
            message = await migration_error(
                prepare_database_and_revert(engine, MIGRATIONS, count=1)
            )
            assert "buma_customer.pdv:" in message and "VARCHAR(15)" in message, message
            assert await catalog(engine) == converted_catalog, label
            assert D0019 in await applied_files(engine), label
            await sql(
                engine,
                "UPDATE buma_customer SET pdv = :value WHERE id = 'wsecust00000002'",
                {"value": ["wsepdv000000001"]},
            )
            assert await values(engine) == converted_values, label
            down_refusals[label] = "refused_atomically"
        report["down_refusals"] = down_refusals

        assert await prepare_database_and_revert(engine, MIGRATIONS, count=1) == [D0019]
        restored = expected_values("down")
        restored["buma_customer.pdv"][SERVICE_ROW[1]] = SERVICE_ROW[2]
        assert await values(engine) == restored
        assert await catalog(engine) == legacy_catalog
        report["down"] = {
            "catalog_equals_pre_correction": True,
            "values": "one_element_to_scalar_empty_to_empty_string",
            "json_empty_list_rows_come_back_as_empty_string": sum(row[2] == "[]" for row in ROWS),
        }

        assert await prepare_database(engine, MIGRATIONS) == [D0019]
        assert await catalog(engine) == converted_catalog
        assert await values(engine) == converted_values
        report["replay"] = "up_down_up_gives_the_same_catalog_and_arrays"

        report["down_up_last_five"] = await down_up_last_five(engine, url, "migrated")
        assert await catalog(engine) == converted_catalog
        return {
            "three": three_columns(converted_catalog),
            "values": converted_values,
            "reads": await record_reads(engine, converted_values),
        }
    finally:
        await engine.dispose()


async def exercise_partial(url: str, converted: dict, report: dict) -> None:
    """Old PPBase up to 1789236845, then this branch with the corrected PPBase."""
    from ppbase.services.database_preparation import prepare_database
    from ppbase.services.record_service import get_record, resolve_collection

    from scripts.generate_native_baseline import load_schema
    from scripts.verify_complete_schema import read_collections as read_complete
    from scripts.verify_complete_schema import verify as verify_complete
    from scripts.verify_schema_baseline import read_collections, verify
    from scripts.verify_view_queries import verify as verify_views

    engine = engine_for(url)
    try:
        assert await applied_files(engine) == list(PARTIAL_MIGRATIONS)
        assert await column_type(engine, "buma_customer", "pdv") == "character varying(15)"
        assert await prepare_database(engine, MIGRATIONS) == list(LAST_FIVE)
        assert three_columns(await catalog(engine)) == converted["three"]
        assert await values(engine) == {
            "buma_customer.pdv": {"CUSTOMER_DIVERS": []},
            "buma_pilotage_ticket.tag": {},
            "buma_wallet.managers": {},
        }
        definitions = load_schema(SCHEMA, include_views=False)[0]
        view_definitions = load_schema(SCHEMA, include_views=True)[0]
        rows = await read_collections(url)
        migrated = verify(definitions, rows, relation_columns="migrated")
        assert migrated["ok"], migrated
        strict = verify(definitions, rows, relation_columns="fresh")
        assert len(strict["relation_columns"]["errors"]) == 188
        assert only_varchar_single_errors(strict["relation_columns"]["errors"])
        complete = verify_complete(
            view_definitions, await read_complete(url), relation_columns="migrated"
        )
        assert complete["ok"], complete
        views = await verify_views(url, view_definitions)
        assert len(views) == 42 and all(item["status"] == "OK" for item in views), views
        customers = await resolve_collection(engine, "buma_customer")
        assert (await get_record(engine, customers, "CUSTOMER_DIVERS"))["pdv"] == []
        report["partial_legacy_then_head"] = {
            "legacy_ppbase_applied": len(PARTIAL_MIGRATIONS),
            "corrected_ppbase_applied": list(LAST_FIVE),
            "customer_divers_pdv": "column default '' then empty array",
            "three_columns_equal_converted_database": True,
            "verifier_migrated_mode": {
                key: migrated["relation_columns"][key] for key in ("multiple", "single", "ok")
            },
            "complete_schema_ok_migrated_mode": complete["ok"],
            "views_ok": len(views),
        }
    finally:
        await engine.dispose()


async def exercise_legacy_head(url: str, converted: dict, report: dict) -> None:
    """This branch stopped at 1789236850 by the old PPBase, finished by the corrected one."""
    from ppbase.services.database_preparation import prepare_database

    from scripts.generate_native_baseline import load_schema
    from scripts.verify_schema_baseline import read_collections, verify

    engine = engine_for(url)
    try:
        assert await applied_files(engine) == list(LEGACY_MIGRATIONS)
        assert await prepare_database(engine, MIGRATIONS) == [D0019]
        assert three_columns(await catalog(engine)) == converted["three"]
        assert (await values(engine))["buma_customer.pdv"] == {"CUSTOMER_DIVERS": []}
        check = verify(
            load_schema(SCHEMA, include_views=False)[0],
            await read_collections(url),
            relation_columns="migrated",
        )
        assert check["ok"], check
        report["legacy_head_then_corrected_ppbase"] = {
            "corrected_ppbase_applied": [D0019],
            "customer_divers_pdv": "column default '' then empty array",
            "three_columns_equal_converted_database": True,
            "verifier_migrated_mode_ok": True,
        }
    finally:
        await engine.dispose()


async def exercise_fresh(root: Path, url: str, converted: dict, report: dict) -> None:
    from ppbase.services.database_preparation import (
        prepare_database,
        prepare_database_and_revert,
    )
    from ppbase.services.record_service import create_record, resolve_collection

    from scripts.generate_native_baseline import load_schema
    from scripts.verify_complete_schema import read_collections as read_complete
    from scripts.verify_complete_schema import verify as verify_complete
    from scripts.verify_schema_baseline import read_collections, verify
    from scripts.verify_view_queries import verify as verify_views

    first = root / "fresh_first_ten"
    first.mkdir()
    for name in LEGACY_MIGRATIONS:
        (first / name).write_text((MIGRATIONS / name).read_text())
    engine = engine_for(url)
    try:
        assert await prepare_database(engine, first) == list(LEGACY_MIGRATIONS)
        state = (await catalog(engine), await values(engine), await collection_rows(engine))
        assert state[1]["buma_customer.pdv"] == {"CUSTOMER_DIVERS": []}, state[1]
        assert await prepare_database(engine, MIGRATIONS) == [D0019]
        assert (await catalog(engine), await values(engine), await collection_rows(engine)) == state
        assert three_columns(state[0]) == converted["three"]

        definitions = load_schema(SCHEMA, include_views=False)[0]
        complete_definitions = load_schema(SCHEMA, include_views=True)[0]
        baseline = verify(definitions, await read_collections(url), relation_columns="fresh")
        complete = verify_complete(
            complete_definitions, await read_complete(url), relation_columns="fresh"
        )
        assert baseline["ok"] and complete["ok"], (baseline, complete)
        views = await verify_views(url, complete_definitions)
        assert len(views) == 42 and all(item["status"] == "OK" for item in views), views
        fresh_model = await model_check(engine)
        assert set(fresh_model["validate_collection_indexes_failures"]) == {"_superusers"}
        assert fresh_model["reconcile_multi_value_columns_would_convert"] == {}

        # The corrected PrepareValue of each legacy scalar gives the converted array.
        for table, record_id, scalar, _, _ in ROWS:
            if record_id == "CUSTOMER_DIVERS":
                continue
            column = dict(COLUMNS)[table]
            collection = await resolve_collection(engine, table)
            await create_record(engine, collection, {"id": record_id, column: scalar})
        table, record_id, value = CONCURRENT_ROW
        await create_record(
            engine, await resolve_collection(engine, table), {"id": record_id, "managers": value}
        )
        table, record_id, value = SERVICE_ROW
        await create_record(
            engine, await resolve_collection(engine, table), {"id": record_id, "pdv": value}
        )
        prepared = await values(engine)
        assert prepared == converted["values"], prepared
        assert await record_reads(engine, prepared) == converted["reads"]

        writes = await single_relation_writes(engine)
        assert writes == SINGLE_WRITES_FRESH, writes
        single_column = await column_type(engine, *SINGLE_RELATION)
        assert single_column == "text", single_column

        assert await prepare_database_and_revert(engine, MIGRATIONS, count=1) == [D0019]
        assert [row[1:] for row in three_columns(await catalog(engine))["columns"]] == [
            ["character varying(15)", True, "''::character varying"]
        ] * 3
        assert await prepare_database(engine, MIGRATIONS) == [D0019]
        assert three_columns(await catalog(engine)) == converted["three"]
        assert await values(engine) == prepared

        down_up = await down_up_last_five(engine, url, "fresh")
        assert three_columns(await catalog(engine)) == converted["three"]
        assert await values(engine) == prepared
        clis = verifier_clis_on_fresh_database(url)
        report["fresh"] = {
            "1789236850_effect": "none (catalog, values and _collections identical)",
            "customer_divers_pdv": "empty array (column default)",
            "three_columns_equal_converted_database": True,
            "verifiers_fresh_mode": {
                key: baseline["relation_columns"][key] for key in ("multiple", "single", "ok")
            },
            "complete_schema_ok": complete["ok"],
            "verifier_clis": clis,
            "views_ok": len(views),
            "ppbase_model": "same as the converted database",
            "prepare_value_of_legacy_scalars_equals_conversion": True,
            "record_service_reads_equal_converted_database": True,
            "single_relation_internal_writes": {"column": single_column, "outcomes": writes},
            "down_up_replay": "scalar varchar(15) then text[] again",
            "down_up_last_five": down_up,
        }
    finally:
        await engine.dispose()


def legacy_ppbase_dir(args, root: Path) -> tuple[Path, dict]:
    """The directory to put on the child's PYTHONPATH, and how it was obtained."""
    source = args.legacy_ppbase.resolve()
    if args.legacy_ppbase_commit is None:
        return source, {"source": "directory"}
    commit = command(
        "git", "-C", str(source), "rev-parse", "--verify", f"{args.legacy_ppbase_commit}^{{commit}}"
    )
    target = root / "legacy_ppbase"
    target.mkdir()
    archive = subprocess.run(
        ["git", "-C", str(source), "archive", "--format=tar", commit, "ppbase"],
        check=True,
        capture_output=True,
    )
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
        tar.extractall(target, filter="data")
    return target, {"source": "git archive (read-only)", "archived_commit": commit}


def run_legacy_phase(root: Path, urls: dict, legacy: Path, ref: str) -> dict:
    names = command("git", "-C", str(PROJECT), "ls-tree", "--name-only", ref, "pb_migrations/")
    files = sorted(Path(name).name for name in names.splitlines() if name.endswith(".py"))
    assert files == list(LEGACY_MIGRATIONS), files
    folders = {"before_fix": root / "legacy_migrations", "partial": root / "partial_migrations"}
    for key, selected in (("before_fix", LEGACY_MIGRATIONS), ("partial", PARTIAL_MIGRATIONS)):
        folders[key].mkdir()
        for name in selected:
            content = subprocess.check_output(
                ["git", "-C", str(PROJECT), "show", f"{ref}:pb_migrations/{name}"]
            )
            (folders[key] / name).write_bytes(content)
    # The six files of the partial database are unchanged by this branch.
    for name in PARTIAL_MIGRATIONS:
        assert (folders["partial"] / name).read_bytes() == (MIGRATIONS / name).read_bytes(), name
    settings = {
        "before_fix": {
            "url": urls["before_fix"],
            "migrations": str(folders["before_fix"]),
            "legacy_write": True,
        },
        "partial": {"url": urls["partial"], "migrations": str(folders["partial"])},
        "legacy_head": {"url": urls["legacy_head"], "migrations": str(MIGRATIONS)},
    }
    child = subprocess.run(
        [sys.executable, "-m", "scripts.c11_relation_columns_runtime", "--legacy-phase"],
        cwd=PROJECT,
        env={
            **os.environ,
            "PYTHONPATH": str(legacy),
            "PYTHONDONTWRITEBYTECODE": "1",
            LEGACY_ENV: json.dumps(settings),
        },
        capture_output=True,
        text=True,
    )
    if child.returncode:
        raise RuntimeError("legacy phase failed:\n" + child.stderr[-4000:])
    line = next(row for row in child.stdout.splitlines() if row.startswith(RESULT_PREFIX))
    return json.loads(line[len(RESULT_PREFIX) :])


async def exercise(root: Path, urls: dict, args, report: dict) -> None:
    import ppbase

    report["corrected_ppbase"] = identity(ppbase)
    if not report["corrected_ppbase"]["relation_multiplicity_rule"]:
        raise RuntimeError("Run with the corrected PPBase on PYTHONPATH")
    files = sorted(path.name for path in MIGRATIONS.glob("*.py"))
    assert files == list(HEAD_MIGRATIONS), files

    legacy_dir, origin = legacy_ppbase_dir(args, root)
    legacy = run_legacy_phase(root, urls, legacy_dir, args.legacy_ref)
    databases = legacy["databases"]
    before_fix, partial, head = (databases[name] for name in LEGACY_DATABASES)
    assert before_fix["error"] is None and before_fix["applied"] == list(LEGACY_MIGRATIONS)
    assert before_fix["record_service_pdv_is_scalar"] is True
    assert before_fix["pdv_column"] == "character varying(15)"
    assert before_fix["customer_divers_pdv"] == ""
    assert partial["error"] is None and partial["applied"] == list(PARTIAL_MIGRATIONS)
    assert partial["customer_divers_pdv"] is None
    # This branch on the old PPBase: the C5 seed works (column default ''), and
    # 1789236850 refuses before any change; the nine files before it stay applied.
    assert head["error"] == "RelationColumnError", head
    assert head["applied"] == list(LEGACY_MIGRATIONS), head
    assert head["pdv_column"] == "character varying(15)" and head["customer_divers_pdv"] == ""
    hooks = legacy["hook_modules"]
    assert hooks["error"] == "ModuleNotFoundError", hooks
    assert hooks["missing"] == "ppbase.models.field_multiplicity", hooks
    report["legacy_ppbase"] = {
        **origin,
        **{key: legacy[key] for key in ("package_dir", "git_commit", "relation_multiplicity_rule")},
    }
    report["this_branch_with_legacy_ppbase"] = {
        "migrations_applied": len(head["applied"]),
        C5: "applied: CUSTOMER_DIVERS pdv '' (VARCHAR(15) column default)",
        D0019: "refused atomically: RelationColumnError",
        "hook_modules": f"{hooks['module']}: ModuleNotFoundError {hooks['missing']}",
    }
    report["legacy_migrations"] = {"ref": args.legacy_ref, "files": len(LEGACY_MIGRATIONS)}

    converted = await exercise_migrated(urls["before_fix"], report)
    await exercise_partial(urls["partial"], converted, report)
    await exercise_legacy_head(urls["legacy_head"], converted, report)
    await exercise_fresh(root, urls["fresh"], converted, report)


def main():
    if os.environ.get(LEGACY_ENV) and sys.argv[1:] == ["--legacy-phase"]:
        result = asyncio.run(legacy_phase(json.loads(os.environ[LEGACY_ENV])))
        print(RESULT_PREFIX + json.dumps(result))
        return
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--legacy-ppbase",
        type=Path,
        required=True,
        help="directory holding a pre-correction ppbase package, or a PPBase Git "
        "repository with --legacy-ppbase-commit",
    )
    parser.add_argument(
        "--legacy-ppbase-commit",
        help="export this pre-correction PPBase commit read-only (git archive), e.g. c6821aa",
    )
    parser.add_argument("--legacy-ref", default="6c979b6")
    parser.add_argument("--container-prefix", default="buma-c11-ws-e")
    parser.add_argument("--report", type=Path, required=True, help="New JSON report path")
    args = parser.parse_args()
    if args.report.exists():
        parser.error("Report must be a new path")
    name = f"{args.container_prefix}-{uuid.uuid4().hex[:12]}"
    password = secrets.token_hex(16)
    report = {
        "timestamp_utc": datetime.now(UTC).isoformat(),
        "scope": "d0019_relation_columns_runtime",
        "production_data_used": False,
        "real_backup_used": False,
        "postgres_image": "postgres:16-alpine",
        "status": "failed",
    }
    created = False
    started = time.monotonic()
    try:
        with tempfile.TemporaryDirectory(prefix="buma-c11-ws-e-") as folder:
            command(
                "docker",
                "run",
                "--detach",
                "--rm",
                "--name",
                name,
                "--label",
                "buma.scope=c11-ws-e-relation-columns",
                "--publish",
                "127.0.0.1::5432",
                "--env",
                "POSTGRES_USER=wse",
                "--env",
                f"POSTGRES_PASSWORD={password}",
                "--env",
                "POSTGRES_DB=before_fix",
                "postgres:16-alpine",
            )
            created = True
            port = command("docker", "port", name, "5432/tcp").rsplit(":", 1)[1]
            for _ in range(150):
                try:
                    command("docker", "exec", name, "pg_isready", "-U", "wse", "-h", "127.0.0.1")
                    break
                except subprocess.CalledProcessError:
                    time.sleep(0.2)
            others = ("partial", "legacy_head", "fresh")
            psql = ["docker", "exec", name, "psql", "-U", "wse", "-d", "before_fix"]
            for database in others:  # CREATE DATABASE cannot share a transaction
                command(*psql, "-h", "127.0.0.1", "-c", f"CREATE DATABASE {database}")
            base = f"postgresql+asyncpg://wse:{password}@127.0.0.1:{port}"
            urls = {db: f"{base}/{db}" for db in ("before_fix", *others)}
            asyncio.run(exercise(Path(folder), urls, args, report))
            report["status"] = "passed"
    except BaseException as exc:
        # Never leak credentials or bind parameters into the report.
        report["failure_type"] = type(exc).__name__
        raise
    finally:
        if created:
            command("docker", "stop", "--time", "5", name)
        report["duration_seconds"] = round(time.monotonic() - started, 1)
        report["cleanup"] = "owned_container_and_temporary_files_removed"
        args.report.parent.mkdir(parents=True, exist_ok=True)
        with args.report.open("x", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        print(json.dumps({"status": report["status"], "report": str(args.report)}))


if __name__ == "__main__":
    main()
