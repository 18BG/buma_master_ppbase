"""Read-only relationship/file audit, independent of import conversion failures.

An independent SQL implementation (json_each, LEFT JOIN) of the relation
counts that the C11 diagnostic reports as preserved anomalies, with its own
reading of the field definitions (``source_multiple`` below, not the
importer's): every relation and file field, including unlimited source arrays.
It never makes those arrays importable and never rewrites data or schema.
Storage comes from a real storage/ directory or, without any file byte, from
the private manifest. Shared with the importer: the read-only snapshot, the
storage inventory/manifest readers, the file key rule and the report writer.
"""

from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from pathlib import Path

from scripts.c11_import import (
    ImportBlocked,
    array_value,
    digest,
    file_key,
    inventory_fingerprint,
    path_present,
    quote,
    read_manifest,
    snapshot,
    storage_inventory,
    write_json_new,
)


def source_multiple(field):
    """JSON list storage, read independently of the importer.

    PocketBase 0.22.34 IsMultiple: maxSelect > 1 for select/file (a plain int,
    null decoding to 0), nil or > 1 for a relation (models/schema/
    schema_field.go:626-627, 667-668, 693, 715-716). For every definition
    PocketBase 0.22 writes this is also the PPBase rule that the importer
    calls (ppbase.models.field_multiplicity, D-0019): a relation maxSelect 0
    or less (refused by PB validation, :709; none in the export; refused by
    the importer) is stored, hence audited, single. A relation without the
    key, a nil pointer for 0.22.34, is audited as the list it stores (and
    refused by the importer).
    """
    maximum = (field.get("options") or {}).get("maxSelect")
    if field["type"] == "relation":
        return maximum is None or maximum > 1
    return (maximum or 1) > 1


def relation_audit(db, collection, field, target):
    table, column = quote(collection["name"]), quote(field["name"])
    total = db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    report = {
        "collection": collection["name"],
        "field": field["name"],
        "target": target["name"],
        "rows": total,
        "multiple": source_multiple(field),
    }
    if report["multiple"]:
        invalid = db.execute(
            f"SELECT COUNT(*) FROM {table} WHERE "
            f"CASE WHEN json_valid({column}) THEN json_type({column})!='array' ELSE 1 END"
        ).fetchone()[0]
        report["invalid_array_rows"] = invalid
        # The CASE prevents malformed source values from hiding other rows.
        references = (
            f"SELECT t.id AS owner, j.value AS reference FROM {table} t, "
            f"json_each(CASE WHEN json_valid(t.{column}) THEN "
            f"CASE WHEN json_type(t.{column})='array' THEN t.{column} ELSE '[]' END "
            "ELSE '[]' END) j"
        )
        report["array_length_distribution"] = {
            str(r[0]): r[1]
            for r in db.execute(
                f"SELECT json_array_length({column}),COUNT(*) "
                f"FROM {table} WHERE json_valid({column}) GROUP BY json_array_length({column})"
            )
        }
    else:
        references = (
            f"SELECT id AS owner,{column} AS reference FROM {table} "
            f"WHERE {column} IS NOT NULL AND {column}!=''"
        )
    count = db.execute(f"SELECT COUNT(*),COUNT(DISTINCT owner) FROM ({references})").fetchone()
    missing = (
        f"SELECT r.owner,r.reference FROM ({references}) r LEFT JOIN "
        f"{quote(target['name'])} target ON target.id=r.reference WHERE target.id IS NULL"
    )
    misses = db.execute(
        f"SELECT COUNT(*),COUNT(DISTINCT owner),COUNT(DISTINCT reference) FROM ({missing})"
    ).fetchone()
    report.update(
        {
            "references": count[0],
            "rows_with_references": count[1],
            "missing_references": misses[0],
            "rows_with_missing_references": misses[1],
            "distinct_missing_targets": misses[2],
        }
    )
    if misses[0]:
        # Public diagnostic reports contain counts/shapes, never source values.
        report["missing_reference_lengths"] = {
            str(r[0]): r[1]
            for r in db.execute(
                f"SELECT LENGTH(reference),COUNT(*) FROM ({missing}) GROUP BY LENGTH(reference)"
            )
        }
    return report


def files_audit(db, definitions, inventory):
    files = set(inventory)
    referenced, missing, summaries = set(), set(), []
    filenames = Counter(Path(key).name for key in files)
    for c in definitions:
        if c["type"] == "view":
            continue
        for field in c["schema"]:
            if field["type"] != "file":
                continue
            summary = {
                "collection": c["name"],
                "field": field["name"],
                "rows_with_files": 0,
                "references": 0,
                "missing_references": 0,
                "rows_with_missing_files": 0,
                "invalid_values": 0,
                "missing_filename_exists_elsewhere": 0,
            }
            own_missing = set()
            for row in db.execute(f"SELECT id,{quote(field['name'])} FROM {quote(c['name'])}"):
                value = row[1]
                try:
                    values = (
                        array_value(value) if source_multiple(field) else ([value] if value else [])
                    )
                    if values:
                        summary["rows_with_files"] += 1
                    has_missing = False
                    for filename in values:
                        if not isinstance(filename, str):
                            raise ImportBlocked("invalid_file_name")
                        key = file_key(c["id"], row["id"], filename)
                        referenced.add(key)
                        summary["references"] += 1
                        if key not in files:
                            summary["missing_references"] += 1
                            missing.add(key)
                            own_missing.add(key)
                            has_missing = True
                            summary["missing_filename_exists_elsewhere"] += bool(
                                filenames[filename]
                            )
                    summary["rows_with_missing_files"] += has_missing
                except (ValueError, TypeError):
                    summary["invalid_values"] += 1
            summary["distinct_missing_files"] = len(own_missing)
            summaries.append(summary)
    extras = files - referenced
    names = {c["id"]: c["name"] for c in definitions}
    extra_collections = Counter(
        names.get(key.split("/")[0], "unknown_collection") for key in extras
    )
    return {
        "fields": summaries,
        "storage_objects": len(files),
        "storage_bytes": sum(f["bytes"] for f in inventory.values()),
        "referenced_unique_files": len(referenced),
        "present_referenced_files": len(referenced & files),
        "missing_unique_files": len(missing),
        "unreferenced_files": len(extras),
        "unreferenced_by_collection": dict(extra_collections),
    }, {"missing": sorted(missing), "unreferenced": sorted(extras)}


def duplicate_audit(db):
    results = []
    for field in ("system_id", "mobile_message_id"):
        groups = (
            f"SELECT type,author,{quote(field)},COUNT(*) n FROM buma_transaction "
            f"WHERE {quote(field)}!='' GROUP BY type,author,{quote(field)} HAVING COUNT(*)>1"
        )
        totals = db.execute(
            f"SELECT COUNT(*),COALESCE(SUM(n),0),COALESCE(SUM(n-1),0),"
            f"COALESCE(MAX(n),0) FROM ({groups})"
        ).fetchone()
        results.append(
            {
                "field": field,
                "scope": ["type", "author", field],
                "groups": totals[0],
                "rows_in_groups": totals[1],
                "rows_above_one_per_key": totals[2],
                "largest_group": totals[3],
            }
        )
    return results


def audit(
    data: Path,
    *,
    source_version: str | None = None,
    progress=None,
    manifest: Path | None = None,
):
    started = time.monotonic()
    before = digest(data / "data.db")
    if manifest is None:
        inventory = storage_inventory(data / "storage")
        storage = {"storage_mode": "directory", "file_bytes_verified": True}
    else:
        if path_present(data / "storage"):
            raise ImportBlocked("storage_directory_and_manifest_both_supplied")
        inventory = read_manifest(manifest, data / "data.db")["storage"]
        storage = {"storage_mode": "manifest", "file_bytes_verified": False}
    report = {
        "scope": "offline_complete_relation_file_diagnostic",
        "production_authorized": False,
        "source_version": source_version,
        "source_version_evidence": "user_supplied",
        "data_db_sha256": before,
        **storage,
        "file_bytes_copied": False,
        "relations": [],
    }
    with snapshot(data) as db:
        definitions = [dict(row) for row in db.execute("SELECT * FROM _collections")]
        for c in definitions:
            c["schema"] = json.loads(c["schema"])
        by_id = {c["id"]: c for c in definitions}
        for c in definitions:
            if c["type"] == "view":
                continue
            for field in c["schema"]:
                if field["type"] == "relation":
                    target = by_id[field["options"]["collectionId"]]
                    report["relations"].append(relation_audit(db, c, field, target))
            if progress:
                progress(c["name"])
        report["files"], private_files = files_audit(db, definitions, inventory)
        report["idempotency"] = duplicate_audit(db)
        # Verify all auth values without disclosing identifiers, hashes or keys.
        report["auth"] = []
        for c in definitions:
            if c["type"] != "auth":
                continue
            row = db.execute(
                f"SELECT COUNT(*) records, "
                "SUM(CASE WHEN passwordHash='' THEN 1 ELSE 0 END) empty_password_hash, "
                "SUM(CASE WHEN passwordHash LIKE '$2%' THEN 1 ELSE 0 END) bcrypt_hashes, "
                "MIN(LENGTH(tokenKey)) token_key_min_length,"
                "MAX(LENGTH(tokenKey)) token_key_max_length "
                f"FROM {quote(c['name'])}"
            ).fetchone()
            report["auth"].append({"collection": c["name"], **dict(row)})
        settings = json.loads(
            db.execute("SELECT value FROM _params WHERE key='settings'").fetchone()[0]
        )
        report["storage_s3_enabled_in_snapshot"] = bool(settings.get("s3", {}).get("enabled"))
        report["backup_s3_enabled_in_snapshot"] = bool(
            settings.get("backups", {}).get("s3", {}).get("enabled")
        )
        # Imported to _superusers by import_snapshot (scripts.c11_auth_import).
        report["admin_accounts"] = db.execute("SELECT COUNT(*) FROM _admins").fetchone()[0]
        # These are only consistency clues, never a relation repair proposal.
        report["legacy_relation_clues"] = {
            "sale_customer_references_found_in_current_customer": db.execute(
                "SELECT COUNT(*) FROM buma_sale s JOIN buma_customer c ON c.id=s.customer "
                "LEFT JOIN buma_customer_old old ON old.id=s.customer "
                "WHERE s.customer!='' AND old.id IS NULL"
            ).fetchone()[0],
            "old_customer_table_rows": db.execute(
                "SELECT COUNT(*) FROM buma_customer_old"
            ).fetchone()[0],
        }
    after = digest(data / "data.db")
    if after != before:
        raise ImportBlocked("source_changed_during_audit")
    report["source_unchanged"] = True
    report["file_manifest_sha256"] = inventory_fingerprint(inventory)
    report["relations_summary"] = {
        "fields_checked": len(report["relations"]),
        "references_checked": sum(r["references"] for r in report["relations"]),
        "missing_references": sum(r["missing_references"] for r in report["relations"]),
        "fields_with_missing_references": sum(
            r["missing_references"] > 0 for r in report["relations"]
        ),
    }
    report["elapsed_seconds"] = round(time.monotonic() - started, 2)
    return report, private_files


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--source-version", required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--private-report", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, help="Private manifest instead of storage/")
    args = parser.parse_args()
    for path in (args.report, args.private_report):
        if path_present(path):
            parser.error(f"{path} must be a new path")
    last = time.monotonic()

    def progress(name):
        nonlocal last
        if time.monotonic() - last > 10:
            print(json.dumps({"checked_through_collection": name}), flush=True)
            last = time.monotonic()

    report, private = audit(
        args.data, source_version=args.source_version, progress=progress, manifest=args.manifest
    )
    # File keys may carry undecodable bytes: private details are ASCII-escaped.
    write_json_new(args.private_report, private, ascii=True)
    write_json_new(args.report, report)
    print(json.dumps({"report": str(args.report), "elapsed_seconds": report["elapsed_seconds"]}))


if __name__ == "__main__":
    main()
