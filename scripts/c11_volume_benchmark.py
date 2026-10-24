"""Synthetic C11 volume benchmark in an owned, resource-limited PostgreSQL.

No --db or --data argument exists: the input is a PocketBase-built fixture
(scripts.c11_fixture.create_fixture, default profile) completed in SQLite with
synthetic rows following the public per-collection counts of the real backup
diagnosis. Nothing real is read. The import runs in manifest mode, like the
server bundle, in separate importer processes so that their peak RSS is their
own. Phases: diagnosis, injected failure and verified rollback, then two
simultaneous imports on the same empty target (one must win, one must refuse).
The container and every temporary file are removed at the end.

Run as python -m scripts.c11_volume_benchmark --pocketbase /path/pocketbase-0.22.27 \
    --schema /path/export.json --report /new/report.json
"""

from __future__ import annotations

import argparse
import asyncio
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
# D-0019: these collections stay blocked until the unbounded-relation wave.
UNBOUNDED_TABLES = {"buma_customer", "buma_pilotage_ticket", "buma_wallet"}
DATABASE = "buma_c11_rehearsal"
ORPHAN_EVERY = 997
MISSING_FILE_EVERY = 1999
BASE_TIME = datetime(2025, 1, 1, tzinfo=UTC)
CHUNK = 10000


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
    }


class ContainerMemory:
    """Samples the container cgroup (memory.current, anon) on the host."""

    def __init__(self, container_id: str):
        root = Path("/sys/fs/cgroup/system.slice") / f"docker-{container_id}.scope"
        self.root = root if root.is_dir() else None
        self.samples = {"current": 0, "anon": 0}
        self.phase = dict(self.samples)
        self.load = 0.0  # host 1-minute load average: concurrent work of others
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)

    def read(self):
        current = int((self.root / "memory.current").read_text())
        stat = dict(line.split() for line in (self.root / "memory.stat").read_text().splitlines())
        return current, int(stat["anon"])

    def run(self):
        while not self.stop.wait(0.25):
            self.load = max(self.load, os.getloadavg()[0])
            try:
                current, anon = self.read()
            except (OSError, ValueError, KeyError):
                continue
            for key, value in (("current", current), ("anon", anon)):
                self.samples[key] = max(self.samples[key], value)
                self.phase[key] = max(self.phase[key], value)

    def start(self):
        if self.root:
            self.thread.start()

    def take_phase(self) -> dict:
        result = {f"max_{key}_bytes": value for key, value in self.phase.items()}
        result["max_host_loadavg_1min"] = round(self.load, 2)
        self.phase, self.load = {"current": 0, "anon": 0}, 0.0
        return result

    def finish(self) -> dict:
        self.stop.set()
        if self.thread.is_alive():
            self.thread.join()
        if not self.root:
            return {"cgroup": "unavailable"}
        return {
            "cgroup_memory_peak_bytes": int((self.root / "memory.peak").read_text()),
            "sampled_max_current_bytes": self.samples["current"],
            "sampled_max_anon_bytes": self.samples["anon"],
            "memory_max_bytes": int((self.root / "memory.max").read_text()),
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
    engine = create_async_engine(os.environ["C11_BENCHMARK_URL"], connect_args={"ssl": "disable"})
    load: dict = {}
    started = time.monotonic()
    try:
        try:
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


async def target_state(url: str, definitions: list[dict]) -> dict:
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
            rowids = None
            if archive:
                rowids = {}
                for c in definitions:
                    if c["name"] in importer.ROWID_TABLES:
                        rowids[c["name"]] = (
                            await conn.execute(
                                text(
                                    f"SELECT count(*) FROM {importer.quote(c['name'])} t "
                                    "JOIN _buma_c11_rowids a ON a.collection_id=:id "
                                    "AND a.record_id=t.id WHERE a.source_rowid=t.rowid"
                                ),
                                {"id": c["id"]},
                            )
                        ).scalar_one()
                archived = (
                    await conn.execute(text("SELECT count(*) FROM _buma_c11_rowids"))
                ).scalar_one()
                rowids["archive_rows"] = archived
        return {
            "counts": counts,
            "archive_table": archive,
            "advisory_locks": advisory,
            "idle_in_transaction": idle,
            "rowid_matches": rowids,
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


async def benchmark(args, root: Path, url: str, monitor: ContainerMemory, report: dict) -> None:
    definitions = importer.load_schema(args.schema)
    staged_root = root / "staging"
    staged_root.mkdir(mode=0o700)
    target = await prepare_target(url, definitions, args.target_columns_out)
    report["target"] = target
    report["memory_phases"] = {"prepare": monitor.take_phase()}

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
        "dangling_references": anomalies["dangling_relations"]["references"],
        "dangling_fields": len(anomalies["dangling_relations"]["fields"]),
        "missing_file_references": anomalies["missing_files"]["references"],
        "files_in_manifest": diagnostic["files"],
        "storage_mode": diagnostic["storage_mode"],
        "file_bytes_copied": diagnostic["file_bytes_copied"],
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
        "fingerprints_match": load["fingerprints_match"],
        "typed_batch_readback": load["typed_batch_readback"],
        "server_row_hashes_unchanged_since_readback": load[
            "server_row_hashes_unchanged_since_readback"
        ],
        "conservation_matches_diagnostic": load["conservation"]["matches_diagnostic"],
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
        "scope": "synthetic_volume_benchmark",
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
            counts = profile_counts(args.scale)
            report["dataset"] = {
                "source": "PocketBase-built fixture (default profile) completed in SQLite",
                "profile": "public per-collection counts, docs/c11-master-initial-diagnostic.json",
                "excluded_collections_d0019": sorted(UNBOUNDED_TABLES),
                "scale": args.scale,
                "orphan_every": ORPHAN_EVERY,
                "missing_file_every": MISSING_FILE_EVERY,
                **complete_fixture(data, definitions, counts),
            }
            bundle = root / "bundle"
            bundle.mkdir(mode=0o700)
            shutil.copyfile(data / "data.db", bundle / "data.db")
            manifest = root / "manifest.json"
            importer.write_json_new(manifest, importer.directory_manifest(data))
            args.bundle, args.manifest = bundle, manifest
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
