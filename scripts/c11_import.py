"""Offline C11 diagnostics and import primitives. No production apply command.

Only a closed, standalone PB snapshot is accepted. The CLI is read-only apart
from its explicit new report file. Rehearsal owns its own disposable database.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import sqlite3
import stat
import zipfile
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

SCHEMA_SHA256 = "9d99e7b54118c41e0a303dd4805b900c7ee7b526b0cc63c83d044b7305b2313d"
ROWID_TABLES = {"users", "buma_article"}
AUTH_MAP = {
    "passwordHash": "password_hash",
    "tokenKey": "token_key",
    "emailVisibility": "email_visibility",
}
AUTH_ARCHIVE = {"lastResetSentAt", "lastVerificationSentAt", "lastLoginAlertSentAt"}
SYSTEM_FIELDS = {"id", "created", "updated"}
AUTH_FIELDS = set(AUTH_MAP) | AUTH_ARCHIVE | {"username", "email", "verified"}


class ImportBlocked(ValueError):
    """An unresolved condition, never a request to repair source records."""


def quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_json_new(path: Path, value: dict) -> None:
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def inspect_archive(path: Path) -> dict:
    """Inspect a large ZIP without extracting data or reading database records.

    Central-directory sizes are declared values, not CRC/content verification.
    The archive checksum streams the compressed file with bounded memory.
    """
    initial = path.stat()
    blockers, names, databases = [], set(), []
    total_bytes = 0
    with zipfile.ZipFile(path) as archive:
        for member in archive.infolist():
            name = member.filename
            parts = PurePosixPath(name).parts
            if name.startswith("/") or ".." in parts or "\\" in name or ":" in name:
                blockers.append("unsafe_archive_member")
            if name in names:
                blockers.append("duplicate_archive_member")
            names.add(name)
            if stat.S_ISLNK(member.external_attr >> 16):
                blockers.append("symlink_in_archive")
            if member.flag_bits & 1:
                blockers.append("encrypted_archive_member")
            if name.endswith(("data.db-wal", "data.db-journal")) and member.file_size:
                blockers.append("live_or_incomplete_sqlite_snapshot")
            if not member.is_dir():
                total_bytes += member.file_size
                if parts and parts[-1] == "data.db":
                    databases.append(name)
    if len(databases) != 1:
        blockers.append("exactly_one_data_db_required")
    checksum = digest(path)
    final = path.stat()
    if (initial.st_size, initial.st_mtime_ns) != (final.st_size, final.st_mtime_ns):
        blockers.append("archive_changed_during_inventory")
    return {
        "mode": "archive_read_only_inventory",
        "production_authorized": False,
        "archive_sha256": checksum,
        "archive_bytes": path.stat().st_size,
        "members": len(names),
        "declared_uncompressed_bytes": total_bytes,
        "data_db_members": databases,
        "blockers": sorted(set(blockers)),
        "ready_for_offline_extraction_review": not blockers,
        "ready_for_isolated_rehearsal": False,
        "content_crc_verified": False,
        "database_schema_and_records_checked": False,
    }


def load_schema(path: Path) -> list[dict]:
    if digest(path) != SCHEMA_SHA256:
        raise ImportBlocked("schema_export_checksum_mismatch")
    return json.loads(path.read_text())


@contextmanager
def snapshot(data: Path):
    path = data / "data.db"
    if not path.is_file() or path.is_symlink() or data.is_symlink():
        raise ImportBlocked("standalone_data_db_required")
    for suffix in ("-wal", "-journal"):
        sidecar = Path(str(path) + suffix)
        if sidecar.exists() and sidecar.stat().st_size:
            raise ImportBlocked("live_or_incomplete_sqlite_snapshot")
    # immutable avoids creating SHM/WAL and does not acquire a write lock.
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA query_only=ON")
        yield db
    finally:
        db.close()


def strict_json(value):
    def invalid(_):
        raise ImportBlocked("non_finite_json")

    return json.loads(value, parse_constant=invalid)


def array_value(value) -> list[str]:
    parsed = strict_json(value) if isinstance(value, str) else value
    if not isinstance(parsed, list) or any(not isinstance(item, str) for item in parsed):
        raise ImportBlocked("invalid_array")
    return parsed


def date_value(value, *, required=False):
    if value == "" and not required:
        return None
    if not isinstance(value, str) or not value:
        raise ImportBlocked("invalid_date")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ImportBlocked("invalid_date") from exc
    if result.tzinfo is None:
        raise ImportBlocked("date_without_timezone")
    return result.astimezone(UTC)


def field_value(field: dict, value):
    kind, options = field["type"], field.get("options") or {}
    if kind == "json":
        return None if value is None else strict_json(value)
    if kind == "date":
        return date_value(value)
    if kind == "bool":
        if value not in (0, 1):
            raise ImportBlocked("invalid_boolean")
        return bool(value)
    if kind == "number":
        if not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ImportBlocked("invalid_number")
        if options.get("onlyInt"):
            if value != int(value) or not -(2**31) <= value < 2**31:
                raise ImportBlocked("integer_outside_target_range")
            return int(value)
        if isinstance(value, int) and int(float(value)) != value:
            raise ImportBlocked("inexact_target_double")
        return float(value)
    if kind == "relation" and options.get("maxSelect") in (None, 0):
        # PB <=0.22: zero means unlimited, stored as a JSON array. PPBase's
        # current DDL/validation treats it as one VARCHAR(15). Never flatten.
        raise ImportBlocked("unbounded_relation_target_scalar")
    if kind in {"file", "select", "relation"} and (options.get("maxSelect") or 1) > 1:
        return array_value(value)
    if kind not in {"text", "editor", "url", "email", "file", "select", "relation"}:
        raise ImportBlocked("unsupported_field_type")
    if not isinstance(value, str):
        raise ImportBlocked("invalid_text")
    if "\x00" in value:
        raise ImportBlocked("nul_in_postgresql_text")
    return value


def mapped_row(collection: dict, row: dict) -> tuple[dict, dict]:
    values = {
        "id": row["id"],
        "created": date_value(row["created"], required=True),
        "updated": date_value(row["updated"], required=True),
    }
    fields = {f["name"]: f for f in collection["schema"]}
    known = set(fields) | SYSTEM_FIELDS | {"__c11_rowid"}
    if collection["type"] == "auth":
        known |= AUTH_FIELDS
        for key in AUTH_FIELDS - AUTH_ARCHIVE:
            value = row[key]
            if key in {"verified", "emailVisibility"}:
                value = field_value({"type": "bool"}, value)
            elif not isinstance(value, str):
                raise ImportBlocked("invalid_auth_field")
            values[AUTH_MAP.get(key, key)] = value
    if set(row) - known:
        raise ImportBlocked("unmapped_source_columns")
    for key, field in fields.items():
        values[key] = field_value(field, row[key])
    if collection["name"] in ROWID_TABLES:
        values["rowid"] = row["__c11_rowid"]
    return values, {key: row[key] for key in AUTH_ARCHIVE if key in row}


def storage_inventory(root: Path) -> dict[str, dict]:
    if not root.is_dir() or root.is_symlink():
        raise ImportBlocked("local_storage_directory_required")
    found = {}
    for directory, directories, files in os.walk(root, followlinks=False):
        for name in directories + files:
            path = Path(directory) / name
            if path.is_symlink():
                raise ImportBlocked("symlink_in_storage")
            if path.is_dir():
                continue
            if not path.is_file():
                raise ImportBlocked("non_regular_storage_object")
            relative = path.relative_to(root).as_posix()
            found[relative] = {"bytes": path.stat().st_size, "sha256": digest(path)}
    return dict(sorted(found.items()))


def file_key(collection: str, record: str, filename: str) -> str:
    for part in (collection, record, filename):
        if not part or part in {".", ".."} or any(c in part for c in ("/", "\\", "\x00")):
            raise ImportBlocked("unsafe_storage_reference")
    return f"{collection}/{record}/{filename}"


def source_rows(db, collection):
    return db.execute(
        f"SELECT _rowid_ AS __c11_rowid, * FROM {quote(collection['name'])} ORDER BY _rowid_"
    )


def idempotency_key_conflicts(db) -> int:
    """Groups that the frozen source unique index would reject (D-0022).

    Only idx_transaction_idempotency exists in the source; SQLite evaluates its
    LIKE predicate here with the same ASCII case folding as PocketBase.
    """
    return db.execute(
        "SELECT COUNT(*) FROM (SELECT 1 FROM buma_transaction WHERE system_id LIKE 'idem:%' "
        "GROUP BY system_id HAVING COUNT(*)>1)"
    ).fetchone()[0]


def diagnose(data: Path, schema: Path, *, source_version: str | None = None) -> dict:
    definitions = load_schema(schema)
    report = {
        "mode": "offline_read_only",
        "source_version": source_version,
        "production_authorized": False,
        "schema_sha256": digest(schema),
        "blockers": [],
        "collections": [],
        "warnings": [],
    }
    blockers = report["blockers"]
    if not source_version:
        blockers.append({"code": "source_pocketbase_version_unknown"})
    elif source_version != "0.22.27":
        blockers.append({"code": "source_pocketbase_version_unvalidated"})
    start_hash = digest(data / "data.db")
    files = storage_inventory(data / "storage")
    referenced = set()
    with snapshot(data) as db:
        if [row[0] for row in db.execute("PRAGMA integrity_check")] != ["ok"]:
            blockers.append({"code": "sqlite_integrity_failure"})
        actual = {row["name"]: dict(row) for row in db.execute("SELECT * FROM _collections")}
        expected = {c["name"]: c for c in definitions}
        for name in set(actual) - set(expected):
            blockers.append({"code": "unexpected_collection", "collection": name})
        relation_count = 0
        for collection in definitions:
            name = collection["name"]
            current = actual.get(name)
            if not current:
                blockers.append({"code": "missing_collection", "collection": name})
                continue
            for key in (
                "id",
                "type",
                "system",
                "schema",
                "indexes",
                "options",
                "listRule",
                "viewRule",
                "createRule",
                "updateRule",
                "deleteRule",
            ):
                value = (
                    strict_json(current[key])
                    if key in {"schema", "indexes", "options"}
                    else current[key]
                )
                wanted = collection[key]
                # PB regenerates view field IDs when importing a schema. Their
                # SQL and field contracts are still compared; no data is copied.
                if key == "schema" and collection["type"] == "view":
                    value = [{k: v for k, v in f.items() if k != "id"} for f in value]
                    wanted = [{k: v for k, v in f.items() if k != "id"} for f in wanted]
                if value != wanted:
                    blockers.append({"code": "schema_drift", "collection": name, "field": key})
            if collection["type"] == "view":
                continue
            table = db.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)
            ).fetchone()
            if not table:
                blockers.append({"code": "missing_table", "collection": name})
                continue
            columns = {
                row["name"].lower() for row in db.execute(f"PRAGMA table_info({quote(name)})")
            }
            if "WITHOUT ROWID" in table[0].upper() or columns & {"rowid", "_rowid_", "oid"}:
                blockers.append({"code": "ambiguous_sqlite_rowid", "collection": name})
                continue
            count, low, high = 0, None, None
            for field in collection["schema"]:
                if field["type"] == "relation" and field["options"].get("maxSelect") in (None, 0):
                    report["warnings"].append(
                        {
                            "code": "unbounded_relation_target_scalar",
                            "collection": name,
                            "field": field["name"],
                        }
                    )
            ids_hash, rows_hash = hashlib.sha256(), hashlib.sha256()
            issue_counts = {}
            for raw in source_rows(db, collection):
                row = dict(raw)
                count += 1
                rowid = row["__c11_rowid"]
                low, high = rowid if low is None else min(low, rowid), rowid
                ids_hash.update((str(rowid) + ":" + row["id"] + "\n").encode())
                rows_hash.update(
                    json.dumps(row, sort_keys=True, ensure_ascii=False).encode() + b"\n"
                )
                try:
                    mapped_row(collection, row)
                    for field in collection["schema"]:
                        if field["type"] not in {"relation", "file"}:
                            continue
                        value = field_value(field, row[field["name"]])
                        items = value if isinstance(value, list) else ([value] if value else [])
                        for item in items:
                            if field["type"] == "file":
                                key = file_key(collection["id"], row["id"], item)
                                referenced.add(key)
                                if key not in files:
                                    raise ImportBlocked("missing_referenced_file")
                            else:
                                target_id = field["options"]["collectionId"]
                                target = next(
                                    (c for c in definitions if c["id"] == target_id), None
                                )
                                if (
                                    not target
                                    or not db.execute(
                                        f"SELECT 1 FROM {quote(target['name'])} WHERE id=? LIMIT 1",
                                        (item,),
                                    ).fetchone()
                                ):
                                    raise ImportBlocked("dangling_relation")
                                relation_count += 1
                except (ImportBlocked, KeyError, ValueError, TypeError) as exc:
                    code = str(exc) if isinstance(exc, ImportBlocked) else "unmappable_value"
                    issue_counts[code] = issue_counts.get(code, 0) + 1
            for code, issues in issue_counts.items():
                blockers.append({"code": code, "collection": name, "affected_rows": issues})
            report["collections"].append(
                {
                    "name": name,
                    "id": collection["id"],
                    "rows": count,
                    "rowid_min": low,
                    "rowid_max": high,
                    "identity_sha256": ids_hash.hexdigest(),
                    "source_rows_sha256": rows_hash.hexdigest(),
                }
            )
        # Only the frozen source index can reject PocketBase-accepted history;
        # duplicate mobile_message_id rows are legacy data to keep (D-0022).
        duplicates = idempotency_key_conflicts(db)
        if duplicates:
            blockers.append(
                {"code": "duplicate_idempotency_key", "field": "system_id", "groups": duplicates}
            )
        internal = {
            r["name"]: db.execute(f"SELECT COUNT(*) FROM {quote(r['name'])}").fetchone()[0]
            for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            if r["name"] not in expected
        }
        report["internal_tables_not_imported"] = internal
        if any(
            count
            for name, count in internal.items()
            if name not in {"_collections", "_migrations", "sqlite_sequence"}
        ):
            report["warnings"].append("internal_records_require_separate_review")
    if digest(data / "data.db") != start_hash:
        raise ImportBlocked("snapshot_changed_during_diagnosis")
    report.update(
        {
            "data_db_sha256": start_hash,
            "files": len(files),
            "file_bytes": sum(f["bytes"] for f in files.values()),
            "file_manifest_sha256": hashlib.sha256(
                json.dumps(files, sort_keys=True).encode()
            ).hexdigest(),
            "unreferenced_files": len(set(files) - referenced),
            "relations_checked": relation_count,
            "rows": sum(c["rows"] for c in report["collections"]),
            "ready_for_isolated_rehearsal": not blockers,
        }
    )
    return report


def stage_snapshot(data: Path, destination: Path, report: dict) -> dict:
    """Private byte-preserving copy; destination must be new and unpublished."""
    if not report["ready_for_isolated_rehearsal"]:
        raise ImportBlocked("diagnostic_blockers")
    destination.mkdir(mode=0o700, exist_ok=False)
    try:
        with snapshot(data):
            before = storage_inventory(data / "storage")
            fingerprint = hashlib.sha256(json.dumps(before, sort_keys=True).encode()).hexdigest()
            if fingerprint != report["file_manifest_sha256"]:
                raise ImportBlocked("storage_changed_after_diagnosis")
            shutil.copyfile(data / "data.db", destination / "data.db")
            (destination / "data.db").chmod(0o600)
            if digest(destination / "data.db") != report["data_db_sha256"]:
                raise ImportBlocked("snapshot_changed_after_diagnosis")
            (destination / "storage").mkdir(mode=0o700)
            for relative in before:
                target = destination / "storage" / relative
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                shutil.copyfile(data / "storage" / relative, target)
                target.chmod(0o600)
            if storage_inventory(destination / "storage") != before:
                raise ImportBlocked("storage_copy_mismatch")
            if storage_inventory(data / "storage") != before:
                raise ImportBlocked("storage_changed_during_copy")
            write_json_new(destination / "manifest.json", {"diagnostic": report, "storage": before})
            return before
    except BaseException:
        # Only the fresh directory exclusively created by this invocation.
        shutil.rmtree(destination)
        raise


async def load_isolated(connection, data: Path, definitions: list[dict], *, fail_after=None):
    """Load on the caller's transaction; requires empty tables, never clears data.

    No hooks/repositories/HTTP writes: historical balances, authors, numbers and
    timestamps must not be recomputed. The rehearsal is the sole CLI caller.
    """
    from sqlalchemy import text

    manifest = json.loads((data / "manifest.json").read_text())
    diagnostic = manifest["diagnostic"]
    if not diagnostic["ready_for_isolated_rehearsal"]:
        raise ImportBlocked("diagnostic_blockers")
    if digest(data / "data.db") != diagnostic["data_db_sha256"]:
        raise ImportBlocked("staged_snapshot_changed")
    if storage_inventory(data / "storage") != manifest["storage"]:
        raise ImportBlocked("staged_storage_changed")
    current_db = (await connection.execute(text("SELECT current_database()"))).scalar_one()
    if current_db != "buma_c11_rehearsal":
        raise ImportBlocked("isolated_rehearsal_database_required")
    tables = [c for c in definitions if c["type"] != "view"]
    # Lock the complete set before checking emptiness, so a concurrent importer
    # cannot pass the same precondition. PostgreSQL constraints remain enabled.
    await connection.execute(
        text(
            "LOCK TABLE " + ",".join(quote(c["name"]) for c in tables) + " IN ACCESS EXCLUSIVE MODE"
        )
    )
    for c in tables:
        if (
            await connection.execute(text(f"SELECT COUNT(*) FROM {quote(c['name'])}"))
        ).scalar_one():
            raise ImportBlocked("target_not_empty")
    await connection.execute(
        text(
            "CREATE TABLE _buma_c11_rowids ("
            "collection_id text NOT NULL, record_id text NOT NULL, source_rowid bigint NOT NULL, "
            "auth_legacy jsonb NOT NULL, PRIMARY KEY(collection_id,record_id), "
            "UNIQUE(collection_id,source_rowid))"
        )
    )
    count = 0
    with snapshot(data) as db:
        for c in tables:
            fields = {f["name"]: f for f in c["schema"]}
            for row in source_rows(db, c):
                raw = dict(row)
                values, archive = mapped_row(c, raw)
                names = list(values)
                bindings, parameters = [], {}
                for index, name in enumerate(names):
                    parameter = f"p{index}"
                    value = values[name]
                    if fields.get(name, {}).get("type") == "json":
                        bindings.append(f"CAST(:{parameter} AS jsonb)")
                        parameters[parameter] = json.dumps(
                            value, ensure_ascii=False, allow_nan=False
                        )
                    else:
                        bindings.append(f":{parameter}")
                        parameters[parameter] = value
                await connection.execute(
                    text(
                        f"INSERT INTO {quote(c['name'])} "
                        f"({','.join(quote(n) for n in names)}) VALUES ({','.join(bindings)})"
                    ),
                    parameters,
                )
                await connection.execute(
                    text(
                        "INSERT INTO _buma_c11_rowids VALUES "
                        "(:collection,:record,:rowid,CAST(:archive AS jsonb))"
                    ),
                    {
                        "collection": c["id"],
                        "record": raw["id"],
                        "rowid": raw["__c11_rowid"],
                        "archive": json.dumps(archive),
                    },
                )
                # Exact typed readback while still inside the transaction.
                result = (
                    (
                        await connection.execute(
                            text(f"SELECT * FROM {quote(c['name'])} WHERE id=:id"),
                            {"id": raw["id"]},
                        )
                    )
                    .mappings()
                    .one()
                )
                if any(result[name] != value for name, value in values.items()):
                    raise ImportBlocked("target_readback_mismatch")
                count += 1
                if count == fail_after:
                    raise ImportBlocked("injected_import_failure")
    return count


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--data", type=Path, help="Closed backup directory")
    inputs.add_argument("--archive", type=Path, help="ZIP inventory only; never extracted")
    parser.add_argument("--schema", type=Path)
    parser.add_argument("--source-version", help="Version supplied with this backup; not guessed")
    parser.add_argument("--report", type=Path, required=True, help="New private JSON report")
    args = parser.parse_args()
    if args.data and not args.schema:
        parser.error("--schema is required with --data")
    try:
        report = (
            inspect_archive(args.archive)
            if args.archive
            else diagnose(args.data, args.schema, source_version=args.source_version)
        )
    except (ImportBlocked, sqlite3.Error, OSError, zipfile.BadZipFile) as exc:
        report = {
            "ready_for_isolated_rehearsal": False,
            "production_authorized": False,
            "error": str(exc) if isinstance(exc, ImportBlocked) else type(exc).__name__,
        }
    write_json_new(args.report, report)
    print(
        json.dumps(
            {
                "report": str(args.report),
                "ready_for_isolated_rehearsal": report["ready_for_isolated_rehearsal"],
            }
        )
    )
    return (
        0
        if (
            report["ready_for_isolated_rehearsal"]
            or report.get("ready_for_offline_extraction_review")
        )
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
