"""Synthetic C11 volume benchmark in an owned, resource-limited PostgreSQL.

No --db or --data argument exists: the input is a PocketBase-built fixture
(scripts.c11_fixture.create_fixture, default profile) completed in SQLite with
synthetic rows following the public per-collection counts of the real backup
diagnosis. Nothing real is read. The import runs in manifest mode, like the
server bundle. Phases:

1. negative load cases, in process, on a small bundle (the same fixture with
   a few rows per table): each alteration of the empty target, of the
   diagnosis or of the definitions handed to the importer must be refused
   with its exact code and details and leave no effect, and the target is then
   exactly restored (schema signature, function bodies included);
2. diagnosis of the volume bundle, in its own process;
3. injected failure at half of the rows and verified rollback;
4. two simultaneous imports on the same empty target (one must win, one must
   refuse), each in its own process so that its peak RSS is its own.

``--negative-only`` stops after phase 1 with a clean import of the small
bundle on the same target (the retry after every refusal). The anomalies of
every diagnosis and of every post-load recount are compared with the counts
the generator itself produced (ORPHAN_EVERY, MISSING_FILE_EVERY), not only
with each other. The container and every temporary file are removed at the
end.

Run as python -m scripts.c11_volume_benchmark --pocketbase /path/pocketbase-0.22.27 \
    --schema /path/export.json --report /new/report.json
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import platform
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from scripts import c11_import as importer

PROJECT = Path(__file__).resolve().parents[1]
PROFILE = PROJECT / "docs" / "c11-master-initial-diagnostic.json"
# D-0019: importable since WS-B3, but this generator has no multiple-relation
# values: their 5 841 real rows (0.5 %) stay out of the synthetic volume; their
# arrays are loaded and recounted on the PocketBase real_shape fixture instead.
UNBOUNDED_TABLES = {"buma_customer", "buma_pilotage_ticket", "buma_wallet"}
DATABASE = "buma_c11_rehearsal"
ORPHAN_EVERY = 997
MISSING_FILE_EVERY = 1999
BASE_TIME = datetime(2025, 1, 1, tzinfo=UTC)
CHUNK = 10000
# Small bundle of the negative load cases: every table gets a few rows, enough
# for an orphan (row 3) and an absent file (row 1) in each generated field.
NEGATIVE_SCALE = 0.002
NEGATIVE_MINIMUM_ROWS = 5
NEGATIVE_MARKER = "c11-negative-marker"
# Text column of the constraint and trigger cases. The narrowing case needs a
# text field that no view uses (PostgreSQL cannot retype those): chosen at run.
NEGATIVE_TABLE, NEGATIVE_COLUMN = "buma_supplier", "firstname"
MEMORY_KEYS = ("current", "anon", "file", "shmem", "anon_plus_shmem")
MEMORY_NOTE = (
    "memory.peak and memory.current include the reclaimable page cache: cgroup 'file' "
    "(which also counts shmem). The memory that cannot be reclaimed without swap is "
    "anon + shmem (PostgreSQL shared_buffers is shmem). memory.events 'max' counts "
    "reclaims at the limit; 'oom' and 'oom_kill' must stay 0."
)


def command(*args, timeout=None):
    return subprocess.check_output(
        args, text=True, stderr=subprocess.STDOUT, timeout=timeout
    ).strip()


def pb_time(index: int) -> str:
    value = BASE_TIME + timedelta(milliseconds=index)
    return value.strftime("%Y-%m-%d %H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"


def profile_counts(scale: float) -> dict[str, int]:
    """Public per-collection row counts of the real diagnosis (aggregates only)."""
    report = json.loads(PROFILE.read_text())
    return {
        c["name"]: max(round(c["rows"] * scale), 0)
        for c in report["collections"]
        if c["name"] not in UNBOUNDED_TABLES
    }


def negative_counts() -> dict[str, int]:
    """Row counts of the small bundle used by the negative load cases."""
    return {
        name: max(round(count * NEGATIVE_SCALE), NEGATIVE_MINIMUM_ROWS)
        for name, count in profile_counts(1.0).items()
    }


def synthetic_id(table_index: int, index: int) -> str:
    return f"v{table_index:02d}{index:012d}"


def field_generator(field, table, ids, by_id):
    """Deterministic synthetic value of one PB 0.22 field for row ``i``."""
    kind, options, name = field["type"], field.get("options") or {}, field["name"]
    if kind in {"text", "editor"}:
        return lambda i: f"{table}-{name}-{i}"
    if kind == "url":
        return lambda i: f"https://example.invalid/{table}/{i}"
    if kind == "email":
        return lambda i: f"{table}.{i}@example.invalid"
    if kind == "number":
        return lambda i: (i % 10007) * 0.25 - 1000.0
    if kind == "bool":
        return lambda i: i % 2
    if kind == "date":
        return lambda i: "" if i % 5 == 0 else pb_time(i * 37)
    if kind == "json":
        return lambda i: (
            None if i % 7 == 0 else json.dumps({"n": i, "s": "é", "l": [i % 3, True, None, 1.5]})
        )
    if kind == "select":
        values = options.get("values") or [""]
        return lambda i: values[i % len(values)]
    if kind == "file":
        if importer.source_multiple(field):
            return lambda i: (
                json.dumps([f"missing-{i}.bin"]) if i % MISSING_FILE_EVERY == 1 else "[]"
            )
        return lambda i: f"missing-{i}.bin" if i % MISSING_FILE_EVERY == 1 else ""
    if kind == "relation":
        if importer.source_multiple(field):
            raise ValueError("unbounded relations are outside this benchmark wave")
        pool = ids.get(by_id[options["collectionId"]]["name"]) or []

        def relation(i):
            if i % ORPHAN_EVERY == 3:
                return f"orphan{i:09d}"[:15]
            if not pool or i % 4 == 0:
                return ""
            return pool[(i * 31) % len(pool)]

        return relation
    raise ValueError(f"no synthetic generator for {kind}")


def generated_anomalies(definitions: list[dict], added: dict[str, int]) -> dict:
    """Anomalies the generator writes, from its own rules (not from the importer).

    Synthetic row ``i`` of a table holds an orphan in every single relation
    field when ``i % ORPHAN_EVERY == 3`` (``orphan<i>``, distinct per row) and
    an absent file in every file field when ``i % MISSING_FILE_EVERY == 1``.
    """
    relations, files = {}, {}
    relation_records = file_records = 0
    for c in definitions:
        count = added.get(c["name"], 0)
        orphans = len(range(3, count, ORPHAN_EVERY))
        absent = len(range(1, count, MISSING_FILE_EVERY))
        kinds = {f["type"] for f in c["schema"]}
        for field in c["schema"]:
            if field["type"] == "relation" and orphans:
                relations[f"{c['name']}.{field['name']}"] = [orphans, orphans, orphans]
            if field["type"] == "file" and absent:
                files[f"{c['name']}.{field['name']}"] = [absent, absent, absent]
        relation_records += orphans if "relation" in kinds else 0
        file_records += absent if "file" in kinds else 0
    return {
        "dangling_relations": {
            "references": sum(v[0] for v in relations.values()),
            "records": relation_records,
            "fields": dict(sorted(relations.items())),
        },
        "missing_files": {
            "references": sum(v[0] for v in files.values()),
            "records": file_records,
            "fields": dict(sorted(files.items())),
        },
    }


def anomaly_counts(anomalies: dict) -> dict:
    """The ``generated_anomalies`` shape of a diagnosis or recount summary."""
    result = {}
    for kind, width in (
        ("dangling_relations", "distinct_targets"),
        ("missing_files", "distinct_files"),
    ):
        part = anomalies[kind]
        result[kind] = {
            "references": part["references"],
            "records": part["records"],
            "fields": dict(
                sorted(
                    (f"{f['collection']}.{f['field']}", [f["references"], f["records"], f[width]])
                    for f in part["fields"]
                )
            ),
        }
    return result


def require_generated(anomalies: dict, expected: dict, what: str) -> dict:
    """Fail unless ``anomalies`` equals the generator's own counts."""
    found = anomaly_counts(anomalies)
    if found != expected:
        differences = {
            kind: {
                key: (found[kind].get(key), expected[kind].get(key))
                for key in ("references", "records")
                if found[kind].get(key) != expected[kind].get(key)
            }
            | {
                name: (found[kind]["fields"].get(name), expected[kind]["fields"].get(name))
                for name in set(found[kind]["fields"]) | set(expected[kind]["fields"])
                if found[kind]["fields"].get(name) != expected[kind]["fields"].get(name)
            }
            for kind in expected
        }
        # Synthetic counts only: (found, generated) per differing entry.
        raise RuntimeError(f"{what}: anomalies differ from the generated counts {differences}")
    return {
        "dangling_references": expected["dangling_relations"]["references"],
        "missing_file_references": expected["missing_files"]["references"],
        "equal_to_generated_counts": True,
    }


def complete_fixture(data: Path, definitions: list[dict], counts: dict[str, int]) -> dict:
    """Add synthetic rows in SQLite until each table reaches its profile count."""
    started = time.monotonic()
    by_id = {c["id"]: c for c in definitions}
    tables = [c for c in definitions if c["type"] != "view" and c["name"] in counts]
    db = sqlite3.connect(data / "data.db")
    try:
        existing = {
            c["name"]: [r[0] for r in db.execute(f"SELECT id FROM {importer.quote(c['name'])}")]
            for c in tables
        }
        ids, missing = {}, {}
        for index, c in enumerate(tables):
            wanted = max(counts[c["name"]] - len(existing[c["name"]]), 0)
            missing[c["name"]] = wanted
            ids[c["name"]] = existing[c["name"]] + [synthetic_id(index, i) for i in range(wanted)]
        password = db.execute("SELECT passwordHash FROM users LIMIT 1").fetchone()[0]
        added = 0
        for index, c in enumerate(tables):
            name, count = c["name"], missing[c["name"]]
            if not count:
                continue
            columns = [r[1] for r in db.execute(f"PRAGMA table_info({importer.quote(name)})")]
            fields = {f["name"]: f for f in c["schema"]}
            generators = []
            for column in columns:
                if column == "id":
                    generators.append(lambda i, index=index: synthetic_id(index, i))
                elif column in {"created", "updated"}:
                    generators.append(pb_time)
                elif column == "username":
                    generators.append(lambda i, name=name: f"{name}_user_{i}")
                elif column == "email":
                    generators.append(lambda i, name=name: f"{name}.{i}@example.invalid")
                elif column in {"emailVisibility", "verified"}:
                    generators.append(lambda i: i % 2)
                elif column == "tokenKey":
                    generators.append(lambda i, index=index: f"{index:02d}" + f"{i:048d}")
                elif column == "passwordHash":
                    generators.append(lambda i: password)
                elif column in importer.AUTH_ARCHIVE:
                    generators.append(lambda i: "")
                else:
                    generators.append(field_generator(fields[column], name, ids, by_id))
            sql = (
                f"INSERT INTO {importer.quote(name)} ({','.join(map(importer.quote, columns))}) "
                f"VALUES ({','.join('?' * len(columns))})"
            )
            for start in range(0, count, CHUNK):
                rows = (
                    tuple(generate(i) for generate in generators)
                    for i in range(start, min(start + CHUNK, count))
                )
                db.executemany(sql, rows)
            added += count
        db.commit()
        db.execute("PRAGMA journal_mode=DELETE")
        total = sum(
            db.execute(f"SELECT COUNT(*) FROM {importer.quote(c['name'])}").fetchone()[0]
            for c in definitions
            if c["type"] != "view"
        )
    finally:
        db.close()
    for suffix in ("-wal", "-journal", "-shm"):
        sidecar = Path(str(data / "data.db") + suffix)
        if sidecar.exists() and sidecar.stat().st_size:
            raise RuntimeError("synthetic snapshot not closed")
    return {
        "synthetic_rows_added": added,
        "records": total,
        "seconds": round(time.monotonic() - started, 2),
        "data_db_bytes": (data / "data.db").stat().st_size,
        "generated_anomalies": generated_anomalies(definitions, missing),
    }


class ContainerMemory:
    """Samples the container cgroup v2 memory on the host (only our container).

    Every 0.25 s: memory.current and, from memory.stat, anon, file (page cache,
    including shmem) and shmem. ``anon_plus_shmem`` is the part that cannot be
    reclaimed without swap; see MEMORY_NOTE.
    """

    def __init__(self, container_id: str):
        root = Path("/sys/fs/cgroup/system.slice") / f"docker-{container_id}.scope"
        self.root = root if root.is_dir() else None
        self.samples = dict.fromkeys(MEMORY_KEYS, 0)
        self.phase = dict(self.samples)
        self.load = 0.0  # host 1-minute load average: concurrent work of others
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)

    def read(self) -> dict:
        stat = dict(line.split() for line in (self.root / "memory.stat").read_text().splitlines())
        values = {key: int(stat[key]) for key in ("anon", "file", "shmem")}
        values["current"] = int((self.root / "memory.current").read_text())
        values["anon_plus_shmem"] = values["anon"] + values["shmem"]
        return values

    def events(self) -> dict:
        lines = (self.root / "memory.events").read_text().splitlines()
        counters = dict(line.split() for line in lines)
        return {key: int(counters.get(key, 0)) for key in ("max", "oom", "oom_kill")}

    def run(self):
        while not self.stop.wait(0.25):
            self.load = max(self.load, os.getloadavg()[0])
            try:
                values = self.read()
            except (OSError, ValueError, KeyError):
                continue
            for key, value in values.items():
                self.samples[key] = max(self.samples[key], value)
                self.phase[key] = max(self.phase[key], value)

    def start(self):
        if self.root:
            self.thread.start()

    def take_phase(self) -> dict:
        result = {f"max_{key}_bytes": value for key, value in self.phase.items()}
        result["max_host_loadavg_1min"] = round(self.load, 2)
        self.phase, self.load = dict.fromkeys(MEMORY_KEYS, 0), 0.0
        return result

    def finish(self) -> dict:
        self.stop.set()
        if self.thread.is_alive():
            self.thread.join()
        if not self.root:
            return {"cgroup": "unavailable"}
        return {
            "memory_max_bytes": int((self.root / "memory.max").read_text()),
            "cgroup_memory_peak_bytes_including_page_cache": int(
                (self.root / "memory.peak").read_text()
            ),
            **{f"sampled_max_{key}_bytes": value for key, value in self.samples.items()},
            "events": self.events(),
            "interpretation": MEMORY_NOTE,
        }


def child(role: str, *arguments: str) -> dict:
    """Run one importer phase in its own process; return its JSON result."""
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    completed = subprocess.run(
        [sys.executable, "-m", "scripts.c11_volume_benchmark", "--child", role, *arguments],
        cwd=PROJECT,
        env=env,
        capture_output=True,
        text=True,
        timeout=3600,
    )
    lines = [line for line in completed.stdout.splitlines() if line.startswith("{")]
    if completed.returncode or not lines:
        raise RuntimeError(f"{role} child failed: {completed.stderr[-2000:]}")
    result = json.loads(lines[-1])
    if "error_type" in result:
        raise RuntimeError(f"{role} child failed: {result['error_type']}: {result['error']}")
    return result


async def run_child(role: str, arguments: list[str]) -> dict:
    if role == "diagnose":
        bundle, manifest, schema, output = map(Path, arguments[:4])
        report = importer.diagnose(bundle, schema, source_version=arguments[4], manifest=manifest)
        importer.write_json_new(output, report)
        return {
            "seconds": report["elapsed_seconds"],
            "peak_rss_kib": importer.peak_rss_kib(),
            "ready": report["ready_for_isolated_rehearsal"],
        }
    from sqlalchemy.ext.asyncio import create_async_engine

    bundle, manifest, schema, diagnostic_path, staging = map(Path, arguments[:5])
    fail_after = int(arguments[5]) or None
    batch_size = int(arguments[6])
    diagnostic = json.loads(diagnostic_path.read_text())
    definitions = importer.load_schema(schema)
    # import_snapshot requires it: no bound value in SQLAlchemy error messages.
    engine = create_async_engine(
        os.environ["C11_BENCHMARK_URL"], connect_args={"ssl": "disable"}, hide_parameters=True
    )
    load: dict = {}
    started = time.monotonic()
    try:
        try:
            async with importer.cancelled_by_termination():
                outcome = await importer.import_snapshot(
                    engine,
                    bundle,
                    staging,
                    diagnostic,
                    definitions,
                    manifest=manifest,
                    fail_after=fail_after,
                    report=load,
                    batch_size=batch_size,
                )
        except importer.ImportBlocked as exc:
            outcome = str(exc)
        except Exception as exc:  # synthetic data only: report the database error
            return {
                "error_type": type(exc).__name__,
                "sqlstate": getattr(getattr(exc, "orig", exc), "sqlstate", None),
                "error": str(getattr(exc, "orig", exc))[:1500],
                "load": load,
            }
    finally:
        await engine.dispose()
    return {
        "outcome": outcome,
        "seconds": round(time.monotonic() - started, 3),
        "peak_rss_kib": importer.peak_rss_kib(),
        "staging_exists": staging.exists(),
        "load": load,
    }


async def target_state(url: str, definitions: list[dict], *, rowids: bool = True) -> dict:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(url, connect_args={"ssl": "disable"})
    try:
        async with engine.connect() as conn:
            counts = {}
            for c in definitions:
                if c["type"] != "view":
                    counts[c["name"]] = (
                        await conn.execute(
                            text(f"SELECT count(*) FROM {importer.quote(c['name'])}")
                        )
                    ).scalar_one()
            archive = (
                await conn.execute(text("SELECT to_regclass('_buma_c11_rowids') IS NOT NULL"))
            ).scalar_one()
            advisory = (
                await conn.execute(text("SELECT count(*) FROM pg_locks WHERE locktype='advisory'"))
            ).scalar_one()
            idle = (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() "
                        "AND state LIKE 'idle in transaction%'"
                    )
                )
            ).scalar_one()
            # ``rowids=False`` while a foreign table holds the archive's name.
            matches = None
            if archive and rowids:
                matches = {}
                for c in definitions:
                    if c["name"] in importer.ROWID_TABLES:
                        matches[c["name"]] = (
                            await conn.execute(
                                text(
                                    f"SELECT count(*) FROM {importer.quote(c['name'])} t "
                                    "JOIN _buma_c11_rowids a ON a.collection_id=:id "
                                    "AND a.record_id=t.id WHERE a.source_rowid=t.rowid"
                                ),
                                {"id": c["id"]},
                            )
                        ).scalar_one()
                matches["archive_rows"] = (
                    await conn.execute(text("SELECT count(*) FROM _buma_c11_rowids"))
                ).scalar_one()
        return {
            "counts": counts,
            "archive_table": archive,
            "advisory_locks": advisory,
            "idle_in_transaction": idle,
            "rowid_matches": matches,
        }
    finally:
        await engine.dispose()


async def prepare_target(url: str, definitions: list[dict], spec_out: Path | None) -> dict:
    from ppbase.services.database_preparation import prepare_database
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(url, connect_args={"ssl": "disable"})
    try:
        async with engine.connect() as conn:
            assert (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'"
                    )
                )
            ).scalar_one() == 0
        migrations = await prepare_database(engine, PROJECT / "pb_migrations")
        async with engine.begin() as conn:
            # Only the virgin database created by this run: its C5 seeds.
            seeds = 0
            for c in definitions:
                if c["type"] != "view":
                    seeds += (
                        await conn.execute(text(f"DELETE FROM {importer.quote(c['name'])}"))
                    ).rowcount
        async with engine.connect() as conn:
            settings = {
                name: (await conn.execute(text(f"SHOW {name}"))).scalar_one()
                for name in (
                    "server_version",
                    "shared_buffers",
                    "work_mem",
                    "maintenance_work_mem",
                    "max_wal_size",
                    "synchronous_commit",
                )
            }
            if spec_out is not None:
                spec = await importer.introspect_target(conn, definitions)
                importer.write_json_new(spec_out, importer.target_spec_json(spec))
        return {"migrations_applied": len(migrations), "seed_rows_removed": seeds, **settings}
    finally:
        await engine.dispose()


def git_commit(path: Path) -> str | None:
    try:
        return command("git", "-C", str(path), "rev-parse", "HEAD")
    except (subprocess.CalledProcessError, OSError):
        return None


def environment(binary: Path) -> dict:
    import asyncpg
    import ppbase
    import sqlalchemy

    meminfo = dict(
        line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines() if ":" in line
    )
    cpuinfo = Path("/proc/cpuinfo").read_text()
    model = re.search(r"model name\s*:\s*(.+)", cpuinfo)
    others = command("docker", "ps", "--format", "{{.Names}}").splitlines()
    return {
        "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version,
        "asyncpg": asyncpg.__version__,
        "sqlalchemy": sqlalchemy.__version__,
        "ppbase_version": getattr(ppbase, "__version__", None),
        "ppbase_commit": git_commit(Path(ppbase.__file__).resolve().parents[1]),
        "buma_commit": git_commit(PROJECT),
        "buma_worktree_clean": not command("git", "-C", str(PROJECT), "status", "--porcelain"),
        "pocketbase": command(str(binary), "--version"),
        "docker": command("docker", "version", "--format", "{{.Server.Version}}"),
        "machine": {
            "kernel": platform.release(),
            "cpu_model": model.group(1).strip() if model else None,
            "cpu_count": os.cpu_count(),
            "memory_total_kib": int(meminfo["MemTotal"].split()[0]),
        },
        "concurrent_load_at_start": {
            "loadavg_1_5_15": [round(x, 2) for x in os.getloadavg()],
            "other_running_containers": len(others),
        },
    }


SIGNATURE_SQL = """
SELECT 'table ' || c.relname FROM pg_class c
WHERE c.relnamespace = 'public'::regnamespace AND c.relkind IN ('r', 'v', 'm', 'p')
UNION ALL
SELECT 'column ' || a.attrelid::regclass::text || '.' || a.attname || ' '
       || format_type(a.atttypid, a.atttypmod)
       || CASE WHEN a.attnotnull THEN ' not null' ELSE '' END
FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid
WHERE c.relnamespace = 'public'::regnamespace AND c.relkind = 'r'
  AND a.attnum > 0 AND NOT a.attisdropped
UNION ALL
SELECT 'default ' || d.adrelid::regclass::text || '.' || a.attname || ' '
       || pg_get_expr(d.adbin, d.adrelid)
FROM pg_attrdef d JOIN pg_attribute a ON a.attrelid = d.adrelid AND a.attnum = d.adnum
JOIN pg_class c ON c.oid = d.adrelid WHERE c.relnamespace = 'public'::regnamespace
UNION ALL
SELECT 'constraint ' || conrelid::regclass::text || '.' || conname || ' '
       || pg_get_constraintdef(oid)
FROM pg_constraint WHERE connamespace = 'public'::regnamespace
UNION ALL
SELECT 'trigger ' || c.relname || '.' || t.tgname || ' ' || t.tgenabled::text || ' '
       || pg_get_triggerdef(t.oid)
FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid
WHERE c.relnamespace = 'public'::regnamespace AND NOT t.tgisinternal
UNION ALL
SELECT 'function ' || p.oid::regprocedure::text || ' '
       || CASE WHEN p.prokind IN ('f', 'p') THEN md5(pg_get_functiondef(p.oid)) ELSE '' END
FROM pg_proc p WHERE p.pronamespace = 'public'::regnamespace
UNION ALL
SELECT 'event trigger ' || evtname || ' ' || evtenabled::text FROM pg_event_trigger
"""
ALLOCATOR = "public.buma_allocate_sqlite_rowid()"


async def target_signature(engine) -> list[str]:
    """Schema-only signature of the target: tables, columns (type, NOT NULL,
    default), constraints, trigger definitions, function bodies (MD5) and
    event triggers, to prove that every alteration of a negative case was
    undone exactly."""
    from sqlalchemy import text

    async with engine.connect() as conn:
        return sorted(row[0] for row in (await conn.execute(text(SIGNATURE_SQL))).all())


async def run_statements(engine, statements) -> None:
    """One transaction; each statement verbatim (no bind-parameter parsing)."""
    async with engine.begin() as conn:
        for statement in statements:  # one statement per call (prepared protocol)
            await conn.exec_driver_sql(statement)


def trigger_function(name: str, body: str, returned: str = "NULL") -> str:
    return (
        f"CREATE FUNCTION {name}() RETURNS trigger LANGUAGE plpgsql AS "
        f"$c11$ BEGIN {body} RETURN {returned}; END $c11$"
    )


def allocator_replacement(body: str) -> str:
    """The migrated rowid allocator, same name and trigger, another body.

    Rowids are supplied by the importer, so the replacement does not allocate;
    the original definition (pg_get_functiondef) is restored by the teardown.
    """
    return (
        f"CREATE OR REPLACE FUNCTION {ALLOCATOR} RETURNS trigger LANGUAGE plpgsql AS "
        f"$c11$ BEGIN {body} RETURN NEW; END $c11$"
    )


def narrowable_text_field(definitions, spec, diagnostic, view_columns) -> tuple[str, str]:
    """First loaded text field stored as PostgreSQL text and used by no view."""
    rows = {c["name"]: c["rows"] for c in diagnostic["collections"]}
    for collection in definitions:
        if collection["type"] == "view" or not rows.get(collection["name"]):
            continue
        for field in collection["schema"]:
            name = (collection["name"], field["name"])
            column = spec[collection["name"]].get(field["name"])
            if field["type"] == "text" and column and column.type == "text":
                if name not in view_columns:
                    return name
    raise RuntimeError("no text field outside every view")


def negative_cases(
    definitions: list[dict],
    spec: dict,
    diagnostic: dict,
    narrow: tuple[str, str],
    narrow_default: str | None,
    allocator: str,
) -> list[dict]:
    """Runtime refusals expected from import_snapshot on the empty target.

    Only the two migrated rowid allocators may exist as triggers: any other
    trigger (the deferred constraint trigger that would rewrite rows at
    COMMIT) and any event trigger is refused before the first write. The
    readback layers are then reached through the allocator function itself,
    whose body is replaced (``allocator`` is its original definition, restored
    by the teardown): ``first`` is the first loaded rowid table, ``later``
    the last one, ``early`` a base table loaded before ``later`` whose
    columns all have defaults (an extra row can be inserted); the replaced
    allocator changes ``early`` while ``later`` is loaded, after ``early``'s
    batches were read back. ``narrow`` is the text field retyped varchar(3)
    after the diagnosis; its default expression ``narrow_default`` is
    restored verbatim (a type round trip would otherwise leave a cast in it).
    """
    rows = {c["name"]: c["rows"] for c in diagnostic["collections"]}
    loaded = [c for c in definitions if c["type"] != "view" and rows.get(c["name"])]
    order = [c["name"] for c in loaded]
    rowid_loaded = [name for name in order if name in importer.ROWID_TABLES]
    if len(rowid_loaded) != len(importer.ROWID_TABLES):
        raise RuntimeError("negative cases need rows in every rowid table")
    first, later = rowid_loaded[0], rowid_loaded[-1]
    early = next(
        c["name"]
        for c in loaded[: order.index(later)]
        if c["type"] == "base"
        and c["name"] not in importer.ROWID_TABLES
        and all(
            column.name == "id" or column.has_default or not column.not_null
            for column in spec[c["name"]].values()
        )
    )
    column = spec[NEGATIVE_TABLE][NEGATIVE_COLUMN]
    if rows.get(NEGATIVE_TABLE, 0) < 2 or column.type != "text":
        raise RuntimeError("negative cases need two buma_supplier rows with a text firstname")
    table, field = importer.quote(NEGATIVE_TABLE), importer.quote(NEGATIVE_COLUMN)
    narrow_table, narrow_field = (importer.quote(name) for name in narrow)
    archive = importer.ARCHIVE_TABLE
    anomalies = json.loads(json.dumps(diagnostic))
    relation = anomalies["preserved_source_anomalies"]["dangling_relations"]
    if not relation["fields"]:
        raise RuntimeError("the negative bundle must contain orphan relations")
    relation["fields"][0]["references"] -= 1
    relation["references"] -= 1
    counted = json.loads(json.dumps(diagnostic))
    next(c for c in counted["collections"] if c["name"] == early)["rows"] += 1
    restore = [allocator]
    return [
        {
            "name": "rowid_archive_exists",
            "code": "rowid_archive_exists",
            "details": {},
            "setup": [
                f"CREATE TABLE {archive} (marker text)",
                f"INSERT INTO {archive} VALUES ('{NEGATIVE_MARKER}')",
            ],
            "teardown": [f"DROP TABLE {archive}"],
            "keeps_foreign_archive": True,
        },
        {
            # The reviewed scenario: it would rewrite rows at COMMIT, after
            # every verification. Refused before the first write.
            "name": "unexpected_deferred_constraint_trigger",
            "code": "unexpected_target_trigger",
            "details": {
                "kind": "table",
                "collection": NEGATIVE_TABLE,
                "trigger": "c11_negative_deferred",
            },
            "setup": [
                trigger_function(
                    "c11_negative_deferred", f"UPDATE {table} SET {field} = 'changed at commit';"
                ),
                f"CREATE CONSTRAINT TRIGGER c11_negative_deferred AFTER INSERT ON {table} "
                "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
                "EXECUTE FUNCTION c11_negative_deferred()",
            ],
            "teardown": [
                f"DROP TRIGGER c11_negative_deferred ON {table}",
                "DROP FUNCTION c11_negative_deferred()",
            ],
        },
        {
            "name": "unexpected_event_trigger",
            "code": "unexpected_target_trigger",
            "details": {"kind": "event", "triggers": 1},
            "setup": [
                "CREATE FUNCTION c11_negative_event() RETURNS event_trigger LANGUAGE plpgsql "
                "AS $c11$ BEGIN END $c11$",
                "CREATE EVENT TRIGGER c11_negative_event ON ddl_command_end "
                "EXECUTE FUNCTION c11_negative_event()",
            ],
            "teardown": [
                "DROP EVENT TRIGGER c11_negative_event",
                "DROP FUNCTION c11_negative_event()",
            ],
        },
        {
            "name": "target_constraint_violation",
            "code": "target_constraint_violation",
            "details": {
                "collection": NEGATIVE_TABLE,
                "sqlstate": "23514",
                "constraint": "c11_negative_check",
            },
            "setup": [
                f"ALTER TABLE {table} ADD CONSTRAINT c11_negative_check CHECK ({field} = '')"
            ],
            "teardown": [f"ALTER TABLE {table} DROP CONSTRAINT c11_negative_check"],
        },
        {
            # Checked at SET CONSTRAINTS ALL IMMEDIATE, not at COMMIT.
            "name": "deferred_constraint_violation",
            "code": "target_constraint_violation",
            "details": {
                "collection": NEGATIVE_TABLE,
                "sqlstate": "23505",
                "constraint": "c11_negative_deferred_unique",
            },
            "setup": [
                f"ALTER TABLE {table} ADD CONSTRAINT c11_negative_deferred_unique "
                "UNIQUE (status) DEFERRABLE INITIALLY DEFERRED"
            ],
            "teardown": [f"ALTER TABLE {table} DROP CONSTRAINT c11_negative_deferred_unique"],
        },
        {
            "name": "value_exceeds_target_length_at_load",
            "code": "value_exceeds_target_length",
            "details": {"collection": narrow[0], "field": narrow[1]},
            "setup": [f"ALTER TABLE {narrow_table} ALTER COLUMN {narrow_field} TYPE varchar(3)"],
            "teardown": [
                f"ALTER TABLE {narrow_table} ALTER COLUMN {narrow_field} TYPE text",
                f"ALTER TABLE {narrow_table} ALTER COLUMN {narrow_field} "
                + (f"SET DEFAULT {narrow_default}" if narrow_default else "DROP DEFAULT"),
            ],
        },
        {
            "name": "target_readback_mismatch",
            "code": "target_readback_mismatch",
            "details": {"collection": first, "field": "updated"},
            "setup": [
                allocator_replacement("NEW.updated := NEW.updated + interval '1 microsecond';")
            ],
            "teardown": restore,
        },
        {
            "name": "target_changed_after_readback",
            "code": "target_changed_after_readback",
            "details": {"collection": early},
            "setup": [
                allocator_replacement(
                    f"IF TG_TABLE_NAME = '{later}' THEN UPDATE {importer.quote(early)} "
                    "SET updated = updated + interval '1 millisecond' "
                    f"WHERE id = (SELECT min(id) FROM {importer.quote(early)}); END IF;"
                )
            ],
            "teardown": restore,
        },
        {
            "name": "target_row_count_mismatch",
            "code": "target_row_count_mismatch",
            "details": {"collection": early},
            "setup": [
                allocator_replacement(
                    f"IF TG_TABLE_NAME = '{later}' THEN INSERT INTO {importer.quote(early)} "
                    "(id) VALUES ('c11negextra0001') ON CONFLICT (id) DO NOTHING; END IF;"
                )
            ],
            "teardown": restore,
        },
        {
            "name": "definitions_do_not_match_diagnosis",
            "code": "definitions_do_not_match_diagnosis",
            "details": {},
            # A collection left out of the definitions handed to the load.
            "definitions": [c for c in definitions if c["name"] != order[-1]],
        },
        {
            "name": "anomaly_conservation_mismatch",
            "code": "anomaly_conservation_mismatch",
            "details": {},
            "diagnostic": anomalies,  # one orphan reference fewer than the source
        },
        {
            "name": "source_row_count_mismatch",
            "code": "source_row_count_mismatch",
            "details": {"collection": early},
            "diagnostic": counted,  # one row more than the source table
        },
        {
            "name": "injected_import_failure",
            "code": "injected_import_failure",
            "details": {},
            "fail_after": max(diagnostic["rows"] // 2, 1),
        },
    ]


async def negative_phase(
    url, definitions, bundle, manifest, schema, staged_root, version, expected
):
    """Phase 1: each refusal, its exact code and details, and no effect at all.

    ``expected`` holds the anomalies the generator wrote in the small bundle;
    its diagnosis must report exactly those.
    """
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    diagnostic = importer.diagnose(bundle, schema, source_version=version, manifest=manifest)
    if not diagnostic["ready_for_isolated_rehearsal"]:
        raise RuntimeError(f"negative bundle blockers: {diagnostic['blockers']}")
    generated = require_generated(
        diagnostic["preserved_source_anomalies"], expected, "negative bundle diagnosis"
    )
    engine = create_async_engine(url, connect_args={"ssl": "disable"}, hide_parameters=True)
    results = []
    try:
        async with engine.connect() as conn:
            spec = await importer.introspect_target(conn, definitions)
            used = await conn.execute(
                text(
                    "SELECT table_name, column_name FROM information_schema.view_column_usage "
                    "WHERE table_schema = 'public'"
                )
            )
            view_columns = {tuple(row) for row in used.all()}
            narrow = narrowable_text_field(definitions, spec, diagnostic, view_columns)
            narrow_default = (
                await conn.execute(
                    text(
                        "SELECT pg_get_expr(d.adbin, d.adrelid) FROM pg_attrdef d "
                        "JOIN pg_attribute a ON a.attrelid = d.adrelid AND a.attnum = d.adnum "
                        "WHERE d.adrelid = to_regclass(:table) AND a.attname = :column"
                    ),
                    {"table": importer.quote(narrow[0]), "column": narrow[1]},
                )
            ).scalar()
            # Restored verbatim after each case that replaces the allocator body.
            allocator = (
                await conn.execute(
                    text("SELECT pg_get_functiondef(CAST(:function AS regprocedure))"),
                    {"function": ALLOCATOR},
                )
            ).scalar_one()
        before = await target_signature(engine)
        cases = negative_cases(definitions, spec, diagnostic, narrow, narrow_default, allocator)
        for case in cases:
            await run_statements(engine, case.get("setup", []))
            staging = staged_root / f"negative-{case['name']}"
            started = time.monotonic()
            try:
                await importer.import_snapshot(
                    engine,
                    bundle,
                    staging,
                    case.get("diagnostic", diagnostic),
                    case.get("definitions", definitions),
                    manifest=manifest,
                    fail_after=case.get("fail_after"),
                )
            except importer.ImportBlocked as exc:
                code, details = exc.code, exc.details
            else:
                raise RuntimeError(f"negative case {case['name']} was imported")
            seconds = round(time.monotonic() - started, 3)
            foreign = case.get("keeps_foreign_archive", False)
            state = await target_state(url, definitions, rowids=not foreign)
            if foreign:
                async with engine.connect() as conn:
                    kept = (
                        await conn.execute(
                            text(f"SELECT count(*), min(marker) FROM {importer.ARCHIVE_TABLE}")
                        )
                    ).one()
                archive_ok = state["archive_table"] and tuple(kept) == (1, NEGATIVE_MARKER)
            else:
                archive_ok = not state["archive_table"]
            await run_statements(engine, case.get("teardown", []))
            outcome = {
                "name": case["name"],
                "code": code,
                "details": details,
                "expected_code_and_details": (code, details) == (case["code"], case["details"]),
                "seconds": seconds,
                "target_tables_empty": not any(state["counts"].values()),
                "archive_untouched": archive_ok,
                "staging_removed": not staging.exists(),
                "advisory_locks": state["advisory_locks"],
                "idle_in_transaction_sessions": state["idle_in_transaction"],
            }
            outcome["no_effect_verified"] = (
                outcome["target_tables_empty"]
                and archive_ok
                and outcome["staging_removed"]
                and not state["advisory_locks"]
                and not state["idle_in_transaction"]
            )
            results.append(outcome)
            if not (outcome["expected_code_and_details"] and outcome["no_effect_verified"]):
                raise RuntimeError(f"negative case failed: {outcome}")
        after = await target_signature(engine)
        if after != before:
            raise RuntimeError("an alteration of the target was not undone")
    finally:
        await engine.dispose()
    return diagnostic, {
        "bundle_rows": diagnostic["rows"],
        "bundle_anomalies_in_diagnosis": generated,
        "cases": results,
        "all_refused_with_expected_code_and_no_effect": True,
        "target_signature_restored": {
            "objects": len(after),
            "sha256": hashlib.sha256("\n".join(after).encode()).hexdigest(),
            "identical_before_and_after": True,
        },
    }


async def clean_retry(
    url, definitions, bundle, manifest, diagnostic, staged_root, generated
) -> dict:
    """--negative-only: the same target accepts the import after every refusal.

    The recount of the loaded target must equal the generated anomalies.
    """
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(url, connect_args={"ssl": "disable"}, hide_parameters=True)
    evidence: dict = {}
    staging = staged_root / "negative-clean-retry"
    try:
        rows = await importer.import_snapshot(
            engine, bundle, staging, diagnostic, definitions, manifest=manifest, report=evidence
        )
    finally:
        await engine.dispose()
    final = await target_state(url, definitions)
    expected = {c["name"]: c["rows"] for c in diagnostic["collections"]}
    if rows != diagnostic["rows"] or final["counts"] != expected:
        raise RuntimeError("the clean retry did not import the small bundle exactly")
    return {
        "outcome": rows,
        "row_counts_equal_diagnostic": True,
        "rowid_matches": final["rowid_matches"],
        "final_typed_reread": evidence["final_typed_reread"],
        "fingerprints_match": evidence["fingerprints_match"],
        "deferred_constraints_run_before_final_reread": evidence[
            "deferred_constraints_run_before_final_reread"
        ],
        "target_triggers": evidence["preflight"]["target_triggers"],
        "conservation_matches_diagnostic": evidence["conservation"]["matches_diagnostic"],
        "recount_anomalies": require_generated(
            evidence["conservation"], generated, "clean retry recount"
        ),
        "json_normalizations": evidence["json_normalizations"],
        "staging_kept_for_serving": staging.exists(),
    }


async def benchmark(args, root: Path, url: str, monitor: ContainerMemory, report: dict) -> None:
    definitions = importer.load_schema(args.schema)
    staged_root = root / "staging"
    staged_root.mkdir(mode=0o700)
    target = await prepare_target(url, definitions, args.target_columns_out)
    report["target"] = target
    report["memory_phases"] = {"prepare": monitor.take_phase()}

    small, report["negative_load_cases"] = await negative_phase(
        url,
        definitions,
        args.negative_bundle,
        args.negative_manifest,
        args.schema,
        staged_root,
        args.source_version,
        args.negative_expected,
    )
    report["memory_phases"]["negative_load_cases"] = monitor.take_phase()
    if args.negative_only:
        report["negative_load_cases"]["clean_retry_on_the_same_target"] = await clean_retry(
            url,
            definitions,
            args.negative_bundle,
            args.negative_manifest,
            small,
            staged_root,
            args.negative_expected,
        )
        report["memory_phases"]["clean_retry"] = monitor.take_phase()
        return

    diagnostic_path = root / "diagnostic.json"
    report["diagnosis"] = child(
        "diagnose",
        str(args.bundle),
        str(args.manifest),
        str(args.schema),
        str(diagnostic_path),
        args.source_version,
    )
    diagnostic = json.loads(diagnostic_path.read_text())
    if not diagnostic["ready_for_isolated_rehearsal"]:
        raise RuntimeError(f"diagnostic blockers: {diagnostic['blockers']}")
    anomalies = diagnostic["preserved_source_anomalies"]
    report["dataset"]["diagnostic"] = {
        "rows": diagnostic["rows"],
        "relations_checked": diagnostic["relations_checked"],
        "anomalies": require_generated(anomalies, args.volume_expected, "volume diagnosis"),
        "dangling_references": anomalies["dangling_relations"]["references"],
        "dangling_fields": len(anomalies["dangling_relations"]["fields"]),
        "missing_file_references": anomalies["missing_files"]["references"],
        "files_in_manifest": diagnostic["files"],
        "storage_mode": diagnostic["storage_mode"],
        "file_bytes_copied": diagnostic["file_bytes_copied"],
        "warnings": [
            {k: w[k] for k in ("code", "collection", "field", "affected_rows") if k in w}
            for w in diagnostic["warnings"]
            if isinstance(w, dict) and w.get("code") in importer.JSON_WARNING_CODES
        ],
    }
    report["memory_phases"]["diagnosis"] = monitor.take_phase()
    common = [str(args.bundle), str(args.manifest), str(args.schema), str(diagnostic_path)]

    if args.fail_fraction <= 0:
        report["failure_injection"] = {"skipped": True}
    else:
        await failure_phase(args, url, definitions, diagnostic, common, staged_root, report)
    report["memory_phases"]["failure_injection"] = monitor.take_phase()
    await concurrent_phase(args, url, definitions, diagnostic, common, staged_root, report)
    report["memory_phases"]["concurrent_imports"] = monitor.take_phase()


async def failure_phase(args, url, definitions, diagnostic, common, staged_root, report):
    fail_after = max(int(diagnostic["rows"] * args.fail_fraction), 1)
    failed = child(
        "import", *common, str(staged_root / "failed"), str(fail_after), str(args.batch_size)
    )
    after_failure = await target_state(url, definitions)
    no_effect = (
        failed["outcome"] == "injected_import_failure"
        and not failed["staging_exists"]
        and not any(after_failure["counts"].values())
        and not after_failure["archive_table"]
        and after_failure["advisory_locks"] == 0
        and after_failure["idle_in_transaction"] == 0
    )
    report["failure_injection"] = {
        "fail_after_rows": fail_after,
        "outcome": failed["outcome"],
        "seconds_until_rollback_complete": failed["seconds"],
        "peak_rss_kib": failed["peak_rss_kib"],
        "staging_removed": not failed["staging_exists"],
        "target_tables_empty": not any(after_failure["counts"].values()),
        "rowid_archive_absent": not after_failure["archive_table"],
        "advisory_locks": after_failure["advisory_locks"],
        "idle_in_transaction_sessions": after_failure["idle_in_transaction"],
        "no_effect_verified": no_effect,
    }
    if not no_effect:
        raise RuntimeError("failure left an effect")


async def concurrent_phase(args, url, definitions, diagnostic, common, staged_root, report):
    loop = asyncio.get_running_loop()
    attempts = await asyncio.gather(
        *(
            loop.run_in_executor(
                None,
                lambda name=name: child(
                    "import", *common, str(staged_root / name), "0", str(args.batch_size)
                ),
            )
            for name in ("first", "second")
        )
    )
    outcomes = sorted(str(a["outcome"]) for a in attempts)
    if outcomes != sorted([str(diagnostic["rows"]), "target_not_empty"]):
        raise RuntimeError(f"unexpected concurrent outcomes {outcomes}")
    winner = next(a for a in attempts if a["outcome"] == diagnostic["rows"])
    loser = next(a for a in attempts if a["outcome"] == "target_not_empty")
    final = await target_state(url, definitions)
    expected = {c["name"]: c["rows"] for c in diagnostic["collections"]}
    if final["counts"] != expected:
        raise RuntimeError("row counts differ from the diagnostic")
    load = winner["load"]
    rows = load["rows"]
    timings = load["timings_seconds"]
    report["concurrent_imports"] = {
        "outcomes": outcomes,
        "only_one_succeeded": True,
        "loser_outcome": loser["outcome"],
        "loser_staging_removed": not loser["staging_exists"],
        "winner_staging_kept_for_serving": winner["staging_exists"],
    }
    report["import"] = {
        "rows": rows,
        "archive_rows": load["archive_rows"],
        "storage_mode": load["storage_mode"],
        "file_bytes_copied": load["file_bytes_copied"],
        "batch_size": load["batch_size"],
        "timings_seconds": timings,
        "child_wall_seconds": winner["seconds"],
        "rows_per_second": round(rows / timings["total"], 1),
        "importer_peak_rss_kib": winner["peak_rss_kib"],
        "statements": load["statements"],
        "statements_total": sum(load["statements"].values()),
        "previous_per_record_statements": 3 * rows,
        "typed_batch_readback": load["typed_batch_readback"],
        "final_typed_reread": load["final_typed_reread"],
        "fingerprints_match": load["fingerprints_match"],
        "server_row_hashes_unchanged_since_readback": load[
            "server_row_hashes_unchanged_since_readback"
        ],
        "conservation_matches_diagnostic": load["conservation"]["matches_diagnostic"],
        "recount_anomalies": require_generated(
            load["conservation"], args.volume_expected, "volume recount"
        ),
        "deferred_constraints_run_before_final_reread": load[
            "deferred_constraints_run_before_final_reread"
        ],
        "rows_equal_diagnosed_total": load["rows_equal_diagnosed_total"],
        "triggers_unchanged_during_load": load["triggers_unchanged_during_load"],
        "json_normalizations_stated": [
            {k: w[k] for k in ("code", "collection", "field", "affected_rows") if k in w}
            for w in load["json_normalizations"]
        ],
        "preflight_columns_checked": load["preflight"]["columns_checked"],
        "preflight_blockers": load["preflight"]["blockers"],
        "preflight_warnings": [
            {k: w[k] for k in ("code", "collection", "field")}
            for w in load["preflight"]["warnings"]
        ],
        "target_triggers": load["preflight"]["target_triggers"],
        "largest_tables": sorted(load["tables"], key=lambda t: -t["rows"])[:8],
    }
    report["import"]["largest_tables"] = [
        {k: t[k] for k in ("name", "rows", "copy_batches", "seconds")}
        for t in report["import"]["largest_tables"]
    ]
    report["verification_after_commit"] = {
        "row_counts_equal_diagnostic": True,
        "rowid_matches": final["rowid_matches"],
        "advisory_locks": final["advisory_locks"],
        "idle_in_transaction_sessions": final["idle_in_transaction"],
    }
    if final["rowid_matches"]["archive_rows"] != rows:
        raise RuntimeError("rowid archive incomplete")
    for name in importer.ROWID_TABLES:
        if final["rowid_matches"][name] != expected[name]:
            raise RuntimeError(f"{name} rowids differ")


def bundle_of(root: Path, name: str, data: Path) -> tuple[Path, Path]:
    """Server-bundle shape: data.db alone plus the private-format manifest."""
    bundle = root / f"{name}-bundle"
    bundle.mkdir(mode=0o700)
    shutil.copyfile(data / "data.db", bundle / "data.db")
    manifest = root / f"{name}-manifest.json"
    importer.write_json_new(manifest, importer.directory_manifest(data))
    return bundle, manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--child", nargs="+", help=argparse.SUPPRESS)
    parser.add_argument("--pocketbase", type=Path)
    parser.add_argument("--schema", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--scale", type=float, default=1.0)
    parser.add_argument("--batch-size", type=int, default=importer.DEFAULT_BATCH_SIZE)
    parser.add_argument("--fail-fraction", type=float, default=0.5)
    parser.add_argument("--memory", default="2g")
    parser.add_argument("--cpus", default="2")
    parser.add_argument(
        "--target-columns-out",
        type=Path,
        help="New file receiving the migrated target column description (schema only)",
    )
    parser.add_argument(
        "--negative-only",
        action="store_true",
        help="Only the negative load cases and a clean retry on the same target",
    )
    args = parser.parse_args()
    if args.child:
        print(json.dumps(asyncio.run(run_child(args.child[0], args.child[1:]))), flush=True)
        return
    if not (args.pocketbase and args.schema and args.report):
        parser.error("--pocketbase, --schema and --report are required")
    for path in (args.report, args.target_columns_out):
        if path is not None and (path.exists() or path.is_symlink()):
            parser.error(f"{path} must be a new path")
    version = command(str(args.pocketbase), "--version").removeprefix("pocketbase version ")
    if version not in importer.VALIDATED_SOURCE_VERSIONS:
        parser.error(f"fixture builder needs a validated PocketBase version, not {version}")
    args.source_version = version
    from scripts.c11_fixture import create_fixture

    name = "buma-c11-ws-b-" + uuid.uuid4().hex[:12]
    password = secrets.token_hex(16)
    report = {
        "timestamp_utc": datetime.now(UTC).isoformat(),
        "scope": "synthetic_negative_load_cases"
        if args.negative_only
        else "synthetic_volume_benchmark",
        "production_data_used": False,
        "real_backup_read": False,
        "status": "failed",
        "environment": environment(args.pocketbase),
        "container": {"image": "postgres:16-alpine", "memory": args.memory, "cpus": args.cpus},
    }
    created = False
    monitor = None
    started = time.monotonic()
    try:
        with tempfile.TemporaryDirectory(prefix="buma-c11-ws-b-volume-") as folder:
            root = Path(folder)
            data = create_fixture(root / "fixture", args.pocketbase.resolve(), args.schema)
            definitions = importer.load_schema(args.schema)
            # The generated counts are the only anomalies: the fixture has none.
            baseline = importer.diagnose(data, args.schema, source_version=version)
            preserved = baseline["preserved_source_anomalies"]
            if (
                preserved["dangling_relations"]["references"]
                or preserved["missing_files"]["references"]
            ):
                raise RuntimeError("the default fixture profile must carry no anomaly")
            report["fixture_baseline_anomalies"] = 0
            # The small bundle of the negative cases: the same fixture, few rows.
            small = root / "negative-fixture"
            shutil.copytree(data, small)
            completed = complete_fixture(small, definitions, negative_counts())
            args.negative_expected = completed.pop("generated_anomalies")
            report["negative_dataset"] = {
                "scale": NEGATIVE_SCALE,
                "minimum_rows_per_table": NEGATIVE_MINIMUM_ROWS,
                **completed,
            }
            args.negative_bundle, args.negative_manifest = bundle_of(root, "negative", small)
            if not args.negative_only:
                counts = profile_counts(args.scale)
                report["dataset"] = {
                    "source": "PocketBase-built fixture (default profile) completed in SQLite",
                    "profile": "public per-collection counts, "
                    "docs/c11-master-initial-diagnostic.json",
                    "excluded_collections_d0019": sorted(UNBOUNDED_TABLES),
                    "scale": args.scale,
                    "orphan_every": ORPHAN_EVERY,
                    "missing_file_every": MISSING_FILE_EVERY,
                }
                completed = complete_fixture(data, definitions, counts)
                args.volume_expected = completed.pop("generated_anomalies")
                report["dataset"].update(completed)
                args.bundle, args.manifest = bundle_of(root, "volume", data)
            command(
                "docker",
                "run",
                "--detach",
                "--rm",
                "--name",
                name,
                "--label",
                "buma.scope=c11-ws-b-volume-benchmark",
                "--memory",
                args.memory,
                "--cpus",
                args.cpus,
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
            container_id = command("docker", "inspect", "--format", "{{.Id}}", name)
            port = command("docker", "port", name, "5432/tcp").rsplit(":", 1)[1]
            url = f"postgresql+asyncpg://c11:{password}@127.0.0.1:{port}/{DATABASE}"
            for _ in range(300):
                try:
                    command(
                        "docker",
                        "exec",
                        name,
                        "pg_isready",
                        "-U",
                        "c11",
                        "-d",
                        DATABASE,
                        "-h",
                        "127.0.0.1",
                    )
                    break
                except subprocess.CalledProcessError:
                    time.sleep(0.2)
            monitor = ContainerMemory(container_id)
            monitor.start()
            os.environ["C11_BENCHMARK_URL"] = url
            asyncio.run(benchmark(args, root, url, monitor, report))
            report["status"] = "passed"
    except BaseException as exc:
        # Never leak credentials or bind parameters into the report.
        report["failure_type"] = type(exc).__name__
        report["failure"] = str(exc)[:500].replace(password, "***")
        raise
    finally:
        os.environ.pop("C11_BENCHMARK_URL", None)
        if monitor is not None:
            report["container"]["memory"] = {"limit": args.memory, **monitor.finish()}
        if created:
            command("docker", "stop", "--time", "10", name, timeout=120)
        report["concurrent_load_at_end"] = {
            "loadavg_1_5_15": [round(x, 2) for x in os.getloadavg()]
        }
        report["benchmark_wall_seconds"] = round(time.monotonic() - started, 1)
        report["cleanup"] = "owned_container_and_temporary_files_removed"
        importer.write_json_new(args.report, report)
        print(json.dumps({"status": report["status"], "report": str(args.report)}))


if __name__ == "__main__":
    main()
