#!/usr/bin/env python3
"""Compare the frozen PocketBase migration history with a final schema export."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

DEFAULT_SCHEMA = Path("/Users/omarsisss/Downloads/buma-master-pb_schema-corrected.json")
DEFAULT_MIGRATIONS = Path(
    "/Users/omarsisss/Documents/wproject/protech/buma_master_hooks/migrationsDir"
)


def migration_inventory(migrations_dir: Path) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for path in sorted(migrations_dir.glob("*.js")):
        match = re.match(r"^(\d+)_(created|updated|deleted)_(.+)\.js$", path.name)
        if not match:
            result.append({"file": path.name, "kind": "other", "target": None})
            continue
        result.append(
            {
                "file": path.name,
                "timestamp": int(match.group(1)),
                "kind": match.group(2),
                "target": match.group(3),
            }
        )
    return result


def analyze(schema_path: Path, migrations_dir: Path) -> dict[str, Any]:
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    if not isinstance(schema, list):
        raise ValueError("Schema export must be a JSON array")
    migrations = migration_inventory(migrations_dir)
    schema_names = sorted(str(item.get("name")) for item in schema if item.get("name"))
    migration_targets = sorted({str(item["target"]) for item in migrations if item.get("target")})
    target_kinds: dict[str, Counter[str]] = defaultdict(Counter)
    for item in migrations:
        target = item.get("target")
        kind = item.get("kind")
        if target and kind:
            target_kinds[str(target)][str(kind)] += 1

    schema_set = set(schema_names)
    migration_set = set(migration_targets)
    return {
        "schema": {
            "path": str(schema_path),
            "sha256": hashlib.sha256(schema_path.read_bytes()).hexdigest(),
            "collections": len(schema),
            "base": sum(item.get("type") == "base" for item in schema),
            "auth": sum(item.get("type") == "auth" for item in schema),
            "views": sum(item.get("type") == "view" for item in schema),
            "names": schema_names,
        },
        "migrations": {
            "path": str(migrations_dir),
            "files": len(migrations),
            "kinds": dict(Counter(str(item.get("kind")) for item in migrations)),
            "targets": migration_targets,
            "target_kinds": {name: dict(kinds) for name, kinds in sorted(target_kinds.items())},
        },
        "reconciliation": {
            "schema_names_without_migration_filename": sorted(schema_set - migration_set),
            "migration_targets_not_in_final_schema": sorted(migration_set - schema_set),
            "schema_names_with_created_migration": sorted(
                name for name in schema_names if target_kinds[name].get("created")
            ),
            "schema_names_without_created_migration": sorted(
                name for name in schema_names if not target_kinds[name].get("created")
            ),
        },
        "note": (
            "Filename reconciliation is evidence about history only. The final schema export "
            "remains authoritative for native PPBase baseline generation."
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA)
    parser.add_argument("--migrations", type=Path, default=DEFAULT_MIGRATIONS)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = analyze(args.schema, args.migrations)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {"schema": report["schema"], "migrations": report["migrations"]},
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
