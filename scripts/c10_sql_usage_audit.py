#!/usr/bin/env python3
"""Record a bounded, reproducible search for real callers of the SQL routes."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = {
    "route_sql": re.compile(r"/(?:api/)?sql\b|/api/sql2\b"),
    "execute_sql": re.compile(r"executeSql|execute_sql|executeQuery|querySql|sqlQuery"),
    "delete_sql": re.compile(r"deleteSql"),
    "sql_view": re.compile(r"buma_sql_table"),
    "indirect_sql_segment": re.compile(r"['\"]sql['\"]", re.IGNORECASE),
}
EXTENSIONS = {".ts", ".tsx", ".js", ".jsx", ".dart", ".vue", ".py", ".html", ".sql"}
SKIP = {
    "node_modules",
    ".git",
    ".kilo",
    ".dart_tool",
    "build",
    "dist",
    ".expo",
    "Pods",
    "__pycache__",
    "vendor",
}


def search(path, *, exclude_types=False):
    files, matches = [], []
    if not path.exists():
        return {"root": str(path), "exists": False, "files_examined": 0, "matches": []}
    for file in sorted(path.rglob("*")):
        relative = file.relative_to(path)
        if (
            any(part in SKIP for part in relative.parts)
            or file.suffix not in EXTENSIONS
            or not file.is_file()
        ):
            continue
        if exclude_types and file.name.endswith(".d.ts"):
            continue
        contents = file.read_bytes()
        files.append({"path": str(file), "sha256": hashlib.sha256(contents).hexdigest()})
        for line, content in enumerate(contents.decode("utf-8", errors="replace").splitlines(), 1):
            matched = [label for label, pattern in PATTERNS.items() if pattern.search(content)]
            if matched:
                # File/line/hash identify evidence without dumping unrelated tokens or credentials.
                matches.append({"path": str(file), "line": line, "patterns": matched})
    manifest_digest = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
    return {
        "root": str(path),
        "exists": True,
        "files_examined": len(files),
        "file_manifest_sha256": manifest_digest,
        "matches": matches,
    }


def main():
    source = ROOT.parent / "buma_master_hooks"
    commit = subprocess.check_output(
        ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
    ).strip()
    assert commit == "530018cd20c2acbe88f38b3639e500b5271e2eb2"
    client_paths = [
        "buma_pro_mobile/src",
        "buma_boutique/lib",
        "buma_boutique/tools",
        "buma_boutique_web/src",
        "buma_boutique_web/mobile",
        "buma_boutique_web/relais-client",
        "business-mobile/lib",
        "bumo_backoffice/src",
    ]
    report = {
        "timestamp_utc": datetime.now(UTC).isoformat(),
        "source_commit": commit,
        "user_provided_context": (
            "The Master frontend source is not currently available; access can be requested."
        ),
        "frozen_backend": search(source, exclude_types=True),
        "candidate_clients": [search(ROOT.parent / path) for path in client_paths],
        "conclusion": (
            "No actual Master frontend caller identified in the examined "
            "candidate source directories."
        ),
        "limits": [
            "Does not establish that the routes are unused",
            "The actual Master frontend was not provided",
            "Dynamic/obfuscated route construction and runtime traffic are not covered",
            "Source queries stored in schema/migrations are not evidence of a frontend call",
        ],
    }
    (ROOT / "docs/c10-sql-usage-audit.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    )
    for entry in report["candidate_clients"]:
        print(entry["root"], entry["files_examined"], "files", len(entry["matches"]), "matches")


if __name__ == "__main__":
    main()
