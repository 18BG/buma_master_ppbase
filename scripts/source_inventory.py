#!/usr/bin/env python3
"""Build a read-only contract inventory from the frozen TypeScript source.

This script deliberately records source evidence; it does not execute
PocketBase migrations or modify the source repository.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from collections import defaultdict
from pathlib import Path
from typing import Any

DEFAULT_SOURCE = "/Users/omarsisss/Documents/wproject/protech/buma_master_hooks"
EXPECTED_COMMIT = "530018cd20c2acbe88f38b3639e500b5271e2eb2"


def git_value(source: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(source), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def strip_comments(text: str) -> str:
    """Remove TS comments while preserving string literals and line numbers."""
    output: list[str] = []
    i = 0
    state = "code"
    while i < len(text):
        char = text[i]
        next_char = text[i + 1] if i + 1 < len(text) else ""
        if state == "code":
            if char == "/" and next_char == "/":
                output.extend((" ", " "))
                i += 2
                state = "line"
                continue
            if char == "/" and next_char == "*":
                output.extend((" ", " "))
                i += 2
                state = "block"
                continue
            if char in {"'", '"', "`"}:
                state = char
            output.append(char)
            i += 1
            continue
        if state == "line":
            if char == "\n":
                output.append("\n")
                state = "code"
            else:
                output.append(" ")
            i += 1
            continue
        if state == "block":
            if char == "*" and next_char == "/":
                output.extend((" ", " "))
                i += 2
                state = "code"
            else:
                output.append("\n" if char == "\n" else " ")
                i += 1
            continue
        # String/template literal. Keep content for route extraction.
        output.append(char)
        if char == "\\" and i + 1 < len(text):
            output.append(text[i + 1])
            i += 2
            continue
        if char == state:
            state = "code"
        i += 1
    return "".join(output)


def line_number(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def string_tokens(text: str) -> list[str]:
    return [value for _, value in re.findall(r"(['\"])(.*?)\1", text, flags=re.S)]


def resolve_path(token: str, bases: dict[str, str]) -> str:
    token = token.strip()
    if len(token) >= 2 and token[0] in {"'", '"', "`"} and token[-1] == token[0]:
        value = token[1:-1]
    else:
        value = token
    for name, base in bases.items():
        value = value.replace("${" + name + "}", base)
    return value


def route_inventory(source: Path) -> list[dict[str, Any]]:
    routes: list[dict[str, Any]] = []
    pattern = re.compile(
        r"routerAdd\s*\(\s*(['\"])(.*?)\1\s*,\s*(`(?:\\.|[^`])*`|'(?:\\.|[^'])*'|\"(?:\\.|[^\"])*\")",
        flags=re.S,
    )
    for path in sorted((source / "src" / "entries").glob("*.ts")):
        original = path.read_text(encoding="utf-8", errors="replace")
        cleaned = strip_comments(original)
        bases = {
            name: value
            for name, _, value in re.findall(
                r"const\s+(\w+)\s*=\s*(['\"])(.*?)\2", cleaned, flags=re.S
            )
        }
        for match in pattern.finditer(cleaned):
            method = match.group(2).strip().upper()
            route_path = resolve_path(match.group(3), bases)
            routes.append(
                {
                    "method": method,
                    "path": route_path,
                    "source_file": str(path.relative_to(source)),
                    "source_line": line_number(original, match.start()),
                    "bootstrap": (
                        "legacy-index" if path.name == "index.pb.ts" else "master-or-entry"
                    ),
                    "status": "active",
                }
            )
    return routes


def hook_inventory(source: Path) -> list[dict[str, Any]]:
    hooks: list[dict[str, Any]] = []
    pattern = re.compile(r"\b(on[A-Z][A-Za-z0-9_]*)\s*\((.*?)\)", flags=re.S)
    allowed = {
        "onAfterBootstrap",
        "onRecordBeforeCreateRequest",
        "onRecordBeforeUpdateRequest",
        "onRecordBeforeDeleteRequest",
    }
    for path in sorted((source / "src" / "entries").glob("*.ts")):
        original = path.read_text(encoding="utf-8", errors="replace")
        cleaned = strip_comments(original)
        for match in pattern.finditer(cleaned):
            event = match.group(1)
            if event not in allowed:
                continue
            strings = string_tokens(match.group(0))
            collections = [
                value
                for value in strings
                if value == "users" or value == "roles" or value.startswith("buma_")
            ]
            hooks.append(
                {
                    "event": event,
                    "collections": collections,
                    "source_file": str(path.relative_to(source)),
                    "source_line": line_number(original, match.start()),
                    "status": "active",
                }
            )
    return hooks


def collection_inventory(source: Path) -> list[dict[str, Any]]:
    references: dict[str, set[str]] = defaultdict(set)
    files = sorted((source / "src" / "entries").glob("*.ts"))
    pattern = re.compile(r"(['\"])(buma_[A-Za-z0-9_]+|users|roles)\1")
    for path in files:
        text = strip_comments(path.read_text(encoding="utf-8", errors="replace"))
        for match in pattern.finditer(text):
            references[match.group(2)].add(str(path.relative_to(source)))
    return [
        {
            "name": name,
            "source_files": sorted(files_for_name),
            "schema_status": "requires-live-export",
        }
        for name, files_for_name in sorted(references.items())
    ]


def transaction_inventory(source: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in sorted((source / "src" / "entries").glob("*.ts")):
        original = path.read_text(encoding="utf-8", errors="replace")
        cleaned = strip_comments(original)
        for needle, kind in (
            ("runInTransaction", "transaction"),
            ("newQuery", "raw-sql"),
        ):
            for match in re.finditer(re.escape(needle), cleaned):
                records.append(
                    {
                        "kind": kind,
                        "source_file": str(path.relative_to(source)),
                        "source_line": line_number(original, match.start()),
                    }
                )
    return records


def build_inventory(source: Path) -> dict[str, Any]:
    commit = git_value(source, "rev-parse", "HEAD")
    if commit != EXPECTED_COMMIT:
        raise SystemExit(
            f"Source commit mismatch: expected {EXPECTED_COMMIT}, got {commit}. "
            "Refuse to generate a misleading baseline."
        )
    routes = route_inventory(source)
    hooks = hook_inventory(source)
    collections = collection_inventory(source)
    transactions = transaction_inventory(source)
    return {
        "source": {
            "repository": str(source),
            "branch": git_value(source, "branch", "--show-current"),
            "commit": commit,
            "status": "clean" if not git_value(source, "status", "--short") else "dirty",
        },
        "counts": {
            "routes": len(routes),
            "hooks": len(hooks),
            "collections_referenced": len(collections),
            "transaction_and_sql_sites": len(transactions),
        },
        "routes": routes,
        "hooks": hooks,
        "collections": collections,
        "transaction_sites": transactions,
        "notes": [
            (
                "Routes are source registrations after comment stripping; route behavior "
                "still requires manual contract review."
            ),
            (
                "Collection references are not a schema snapshot; rules, indexes, views "
                "and requiredness require the live export."
            ),
            "SQL sites are inventory evidence only and are never executed by this script.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default=DEFAULT_SOURCE, type=Path)
    parser.add_argument("--output", default="docs/contracts/source-inventory.json", type=Path)
    args = parser.parse_args()
    inventory = build_inventory(args.source.resolve())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(inventory, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(json.dumps(inventory["counts"], sort_keys=True))


if __name__ == "__main__":
    main()
