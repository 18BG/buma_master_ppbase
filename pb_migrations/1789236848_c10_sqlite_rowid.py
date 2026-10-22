"""Preserve the SQLite row identifiers actually projected by three source views.

PocketBase 0.22 creates users/article as ordinary SQLite rowid tables with a
separate TEXT primary key. The internal bigint is not a PocketBase schema field.
Do not backfill guessed identifiers into existing data; C11 must import them.
"""

import importlib.util
import json
from pathlib import Path


TABLES = ("buma_article", "users")
VIEWS = (
    "buma_article_with_no_stock_list_view",
    "buma_stockage_no_manager",
    "buma_stock_transaction_list_view",
)


def old_queries():
    path = Path(__file__).with_name("1789236842_buma_master_views.py")
    spec = importlib.util.spec_from_file_location("c10_original_views", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return {
        c["name"]: c["options"]["query"] for c in module._CREATED_COLLECTIONS if c["name"] in VIEWS
    }


def restored_query(name, original):
    if name == "buma_stock_transaction_list_view":
        return original.replace("a.id as article_id", "a.rowid as article_id")
    return original.replace("SELECT a.id,", "SELECT \n  a.rowid,\n  a.id,", 1)


ALLOCATOR = """
CREATE FUNCTION buma_allocate_sqlite_rowid() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE maximum bigint; candidate bigint; attempt integer;
BEGIN
    -- Serialize the two allocators transactionally. Unlike nextval(), this
    -- reads committed rows and therefore reuses rolled-back/max-deleted IDs.
    PERFORM pg_advisory_xact_lock(1789236848);
    IF NEW.rowid IS NULL THEN
        EXECUTE format('SELECT max(rowid) FROM %I.%I', TG_TABLE_SCHEMA, TG_TABLE_NAME)
            INTO maximum;
        IF maximum IS NULL THEN NEW.rowid := 1;
        ELSIF maximum < 9223372036854775807 THEN NEW.rowid := maximum + 1;
        ELSE
            FOR attempt IN 1..100 LOOP
                candidate := (('x' || substr(md5(random()::text || clock_timestamp()::text),1,16))
                              ::bit(64)::bigint) & 9223372036854775807;
                IF candidate = 0 THEN CONTINUE; END IF;
                EXECUTE format('SELECT rowid FROM %I.%I WHERE rowid=$1',
                               TG_TABLE_SCHEMA,TG_TABLE_NAME) INTO maximum USING candidate;
                IF maximum IS NULL THEN NEW.rowid := candidate; EXIT; END IF;
            END LOOP;
            IF NEW.rowid IS NULL THEN
                RAISE EXCEPTION 'database or disk is full' USING ERRCODE='53100';
            END IF;
        END IF;
    END IF;
    RETURN NEW;
END $$
"""


async def replace_views(app, forward):
    for name, original in old_queries().items():
        collection = await app.find_collection(name)
        options = dict(collection.options or {})
        options["query"] = restored_query(name, original) if forward else original
        # No other exported view depends on these three. No CASCADE is used;
        # an unexpected external dependency fails the migration atomically.
        await app.execute_sql(f'DROP VIEW "{name}"')
        await app.execute_sql(f'CREATE VIEW "{name}" AS {options["query"]}')
        await app.execute_sql(
            "UPDATE _collections SET options=CAST(:options AS jsonb) WHERE id=:id",
            {"options": json.dumps(options), "id": collection.id},
        )


async def up(app):
    for table in TABLES:
        await app.execute_sql(f'LOCK TABLE "{table}" IN ACCESS EXCLUSIVE MODE')
        count = (await app.execute_sql(f'SELECT COUNT(*) FROM "{table}"')).scalar_one()
        if count:
            raise RuntimeError(
                f"{table}: refusing to invent SQLite rowids for existing records; "
                "preserve source rowid values during the C11 import"
            )
    await app.execute_sql(ALLOCATOR)
    for table in TABLES:
        await app.execute_sql(f'ALTER TABLE "{table}" ADD COLUMN rowid bigint NOT NULL UNIQUE')
        await app.execute_sql(
            f'CREATE TRIGGER buma_sqlite_rowid BEFORE INSERT ON "{table}" '
            "FOR EACH ROW EXECUTE FUNCTION buma_allocate_sqlite_rowid()"
        )
    await replace_views(app, True)


async def down(app):
    # Retain a strict empty-target guard so rollback cannot lose imported IDs.
    for table in TABLES:
        await app.execute_sql(f'LOCK TABLE "{table}" IN ACCESS EXCLUSIVE MODE')
        if (await app.execute_sql(f'SELECT COUNT(*) FROM "{table}"')).scalar_one():
            raise RuntimeError(f"{table}: rowid rollback requires an empty target")
    await replace_views(app, False)
    for table in TABLES:
        await app.execute_sql(f'DROP TRIGGER buma_sqlite_rowid ON "{table}"')
        await app.execute_sql(f'ALTER TABLE "{table}" DROP COLUMN rowid')
    await app.execute_sql("DROP FUNCTION buma_allocate_sqlite_rowid()")
