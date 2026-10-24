"""D-0019 runtime proof of migration 1789236850 on an owned, disposable PostgreSQL.

No --db argument exists: this cannot target an existing database. One Docker
postgres:16-alpine container (labelled, random loopback port) holds two
databases and is removed at the end, even after a failure:

* ``before_fix``: a child process loads the PPBase given by ``--legacy-ppbase``
  (checked to predate the D-0019 rule) and applies the ten migrations of
  ``--legacy-ref``, the BUMA commit before this change, then writes one value
  through that PPBase's record service; synthetic edge values follow in SQL.
  This process (corrected PPBase, selected by PYTHONPATH) then proves the
  refusals, the lock against concurrent writers, the rollback after DDL, the
  conversion, the PPBase model checks, the refused and accepted down, and the
  replay.
* ``fresh``: migrated by the corrected PPBase, first without 1789236850 and
  then with it, which must change nothing; verifiers and the 42 views run on it.

All values are synthetic. Run as
python -m scripts.c11_relation_columns_runtime --legacy-ppbase DIR --report NEW.json
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import text
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
COLUMNS = (
    ("buma_customer", "pdv"),
    ("buma_pilotage_ticket", "tag"),
    ("buma_wallet", "managers"),
)
TABLES = tuple(table for table, _ in COLUMNS)
LEGACY_ENV = "BUMA_C11_WSE_LEGACY_DB"
RESULT_PREFIX = "C11_RELATION_COLUMNS_LEGACY "
# Written by the legacy PPBase record service (old rule: scalar relation).
LEGACY_WRITE = ("buma_customer", "wsecust00000002", "wsepdv000000001")
# table, id, scalar stored before the fix, array after up, scalar after down.
ROWS = (
    ("buma_customer", "CUSTOMER_DIVERS", "", [], ""),  # 1789236846 seed
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
    # Column types change under pooled connections here: asyncpg's prepared
    # statement cache would raise InvalidCachedStatementError once per stale
    # statement, as in any server left running during a DDL migration.
    return create_async_engine(
        url + "?prepared_statement_cache_size=0", connect_args={"ssl": "disable"}
    )


async def legacy_phase(url: str, migrations: Path) -> dict:
    """Runs in the child process, with the PPBase before the D-0019 rule."""
    import ppbase
    from ppbase.services.database_preparation import prepare_database
    from ppbase.services.migration_runner import MigrationApp
    from ppbase.services.record_service import create_record, resolve_collection

    loaded = identity(ppbase)
    if loaded["relation_multiplicity_rule"]:
        raise RuntimeError("--legacy-ppbase already contains the D-0019 multiplicity rule")
    engine = engine_for(url)
    try:
        applied = await prepare_database(engine, migrations)
        table, record_id, value = LEGACY_WRITE
        collection = await resolve_collection(engine, table)
        written = await create_record(engine, collection, {"id": record_id, "pdv": value})
        # This branch's 1789236846 and 1789236850 must not run on this PPBase:
        # each is executed in a transaction that is rolled back.
        current = {}
        for name in ("1789236846_c5_reference_defaults.py", D0019):
            module = load_migration(name)
            async with AsyncSession(engine, expire_on_commit=False) as session:
                await session.begin()
                try:
                    await module.up(MigrationApp(session, engine))
                except Exception as exc:
                    current[name] = type(exc).__name__
                    if "expected str, got list" in str(exc):  # asyncpg: [] for VARCHAR
                        current[name] += " (list bound to a varchar column)"
                else:
                    current[name] = "applied"
                finally:
                    await session.rollback()
        return {
            **loaded,
            "migrations": applied,
            "record_service_pdv_is_scalar": written["pdv"] == value,
            "current_migrations_on_this_ppbase": current,
        }
    finally:
        await engine.dispose()


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


async def collection_rows(engine) -> dict:
    async with engine.connect() as conn:
        rows = (await conn.execute(text("SELECT * FROM _collections ORDER BY id"))).mappings().all()
    return {
        row["name"]: json.dumps(dict(row), default=str, sort_keys=True, ensure_ascii=False)
        for row in rows
    }


async def applied_files(engine) -> list[str]:
    async with engine.connect() as conn:
        rows = await conn.execute(text("SELECT file FROM _migrations ORDER BY file"))
        return [row[0] for row in rows.all()]


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


async def exercise_migrated(root: Path, url: str, legacy: Path, ref: str, report: dict) -> dict:
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

    names = command("git", "-C", str(PROJECT), "ls-tree", "--name-only", ref, "pb_migrations/")
    files = sorted(Path(name).name for name in names.splitlines() if name.endswith(".py"))
    assert files == list(LEGACY_MIGRATIONS), files
    folder = root / "legacy_migrations"
    folder.mkdir()
    for name in LEGACY_MIGRATIONS:
        content = subprocess.check_output(
            ["git", "-C", str(PROJECT), "show", f"{ref}:pb_migrations/{name}"]
        )
        (folder / name).write_bytes(content)
    child = subprocess.run(
        [sys.executable, "-m", "scripts.c11_relation_columns_runtime", "--legacy-phase"],
        cwd=PROJECT,
        env={
            **os.environ,
            "PYTHONPATH": str(legacy),
            "PYTHONDONTWRITEBYTECODE": "1",
            LEGACY_ENV: json.dumps({"url": url, "migrations": str(folder)}),
        },
        capture_output=True,
        text=True,
    )
    if child.returncode:
        raise RuntimeError("legacy phase failed:\n" + child.stderr[-4000:])
    line = next(row for row in child.stdout.splitlines() if row.startswith(RESULT_PREFIX))
    legacy_result = json.loads(line[len(RESULT_PREFIX) :])
    assert legacy_result["migrations"] == list(LEGACY_MIGRATIONS), legacy_result
    assert legacy_result["record_service_pdv_is_scalar"] is True
    current = legacy_result["current_migrations_on_this_ppbase"]
    assert current[D0019] == "RelationColumnError", current
    assert current[LEGACY_MIGRATIONS[6]].endswith("(list bound to a varchar column)"), current
    report["legacy_ppbase"] = {
        key: legacy_result[key]
        for key in ("package_dir", "git_commit", "relation_multiplicity_rule")
    }
    report["this_branch_with_legacy_ppbase"] = {
        "1789236846_c5_reference_defaults.py": "refused: " + current[LEGACY_MIGRATIONS[6]],
        D0019: "refused: " + current[D0019],
    }
    report["legacy_migrations"] = {"ref": ref, "files": len(LEGACY_MIGRATIONS)}

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
        table, value = "buma_wallet", "['wseuser01']"
        task = await with_blocker(
            engine,
            'INSERT INTO "buma_wallet" (id, managers) VALUES (:id, :value)',
            {"id": REFUSED_ID, "value": value},
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

        # up waits for a concurrent writer and converts its committed value.
        table, record_id, value = CONCURRENT_ROW
        task = await with_blocker(
            engine,
            'INSERT INTO "buma_wallet" (id, managers) VALUES (:id, :value)',
            {"id": record_id, "value": value},
            prepare_database(engine, MIGRATIONS),
        )
        assert await task == [D0019]
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
        assert (
            not strict["metadata_mismatches"] and len(strict["relation_columns"]["errors"]) == 188
        )
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
            "complete_schema_ok_migrated_mode": complete["ok"],
            "views_ok": len(views),
            "second_run_pending": [],
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
        return {
            "three": three_columns(converted_catalog),
            "values": converted_values,
            "reads": await record_reads(engine, converted_values),
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

        assert await prepare_database_and_revert(engine, MIGRATIONS, count=1) == [D0019]
        assert [row[1:] for row in three_columns(await catalog(engine))["columns"]] == [
            ["character varying(15)", True, "''::character varying"]
        ] * 3
        assert await prepare_database(engine, MIGRATIONS) == [D0019]
        assert three_columns(await catalog(engine)) == converted["three"]
        assert await values(engine) == prepared
        report["fresh"] = {
            "1789236850_effect": "none (catalog, values and _collections identical)",
            "customer_divers_pdv": "empty array",
            "three_columns_equal_converted_database": True,
            "verifiers_fresh_mode": {
                key: baseline["relation_columns"][key] for key in ("multiple", "single", "ok")
            },
            "complete_schema_ok": complete["ok"],
            "views_ok": len(views),
            "ppbase_model": "same as the converted database",
            "prepare_value_of_legacy_scalars_equals_conversion": True,
            "record_service_reads_equal_converted_database": True,
            "down_up_replay": "scalar varchar(15) then text[] again",
        }
    finally:
        await engine.dispose()


async def exercise(root: Path, before_url: str, fresh_url: str, args, report: dict) -> None:
    import ppbase

    report["corrected_ppbase"] = identity(ppbase)
    if not report["corrected_ppbase"]["relation_multiplicity_rule"]:
        raise RuntimeError("Run with the corrected PPBase on PYTHONPATH")
    converted = await exercise_migrated(
        root, before_url, args.legacy_ppbase.resolve(), args.legacy_ref, report
    )
    await exercise_fresh(root, fresh_url, converted, report)


def main():
    if os.environ.get(LEGACY_ENV) and sys.argv[1:] == ["--legacy-phase"]:
        settings = json.loads(os.environ[LEGACY_ENV])
        result = asyncio.run(legacy_phase(settings["url"], Path(settings["migrations"])))
        print(RESULT_PREFIX + json.dumps(result))
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--legacy-ppbase", type=Path, required=True)
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
            command(
                "docker",
                "exec",
                name,
                "psql",
                "-U",
                "wse",
                "-d",
                "before_fix",
                "-h",
                "127.0.0.1",
                "-c",
                "CREATE DATABASE fresh",
            )
            base = f"postgresql+asyncpg://wse:{password}@127.0.0.1:{port}"
            asyncio.run(exercise(Path(folder), f"{base}/before_fix", f"{base}/fresh", args, report))
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
