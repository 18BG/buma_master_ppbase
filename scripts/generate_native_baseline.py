#!/usr/bin/env python3
"""Generate a native PPBase schema baseline from a PocketBase export.

The final export is the source of truth. Historical JS migrations are analyzed
separately and are never executed by this script.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from ppbase.models.collection import CollectionCreate
from ppbase.services import migration_generator

DEFAULT_SCHEMA = Path(
    os.environ.get(
        "BUMA_SCHEMA_EXPORT", "/Users/omarsisss/Downloads/buma-master-pb_schema-corrected.json"
    )
)
DEFAULT_OUTPUT = Path("pb_migrations/1789236840_buma_master_schema_baseline.py")
DEFAULT_REPORT = Path("docs/schema-baseline-report.json")
BASELINE_TIMESTAMP = 1_790_000_003


def normalize_field(field: dict[str, Any]) -> dict[str, Any]:
    normalized = copy.deepcopy(field)
    field_type = str(normalized.get("type") or "")
    options = normalized.get("options")
    if not isinstance(options, dict):
        options = {}
    options = dict(options)
    for key in ("min", "max"):
        if options.get(key) in ("", None):
            options.pop(key, None)
    if field_type in {"text", "editor", "url", "email"} and options.get("max") == 0:
        options.pop("max", None)
    if field_type in {"editor", "json"} and options.get("maxSize") in ("", None, 0):
        options.pop("maxSize", None)
    normalized["options"] = options
    return normalized


def translate_index(index: str, collection_name: str) -> str:
    """Translate the PocketBase SQLite index form to PostgreSQL SQL."""
    raw = str(index or "").strip()
    if not raw:
        return raw
    translated = raw.replace("`", '"')
    translated = re.sub(r"\s+COLLATE\s+NOCASE\b", "", translated, flags=re.IGNORECASE)
    translated = re.sub(r"\s+IF\s+NOT\s+EXISTS\s+", " ", translated, flags=re.IGNORECASE)
    if not re.match(
        rf'^CREATE\s+(?:UNIQUE\s+)?INDEX\s+"[A-Za-z_][A-Za-z0-9_]*"\s+ON\s+"{re.escape(collection_name)}"\s*\(',
        translated,
        flags=re.IGNORECASE,
    ):
        raise ValueError(f"Unsupported SQLite index for {collection_name}: {raw}")
    return translated


def translate_view_query(collection_name: str, query: str) -> tuple[str, list[str]]:
    """Apply only mechanical, auditable SQLite-to-PostgreSQL translations.

    Complex views are emitted with warnings for a later explicit translation;
    silently pretending that SQLite SQL is PostgreSQL-compatible is forbidden.
    """
    translated = query.strip()
    warnings: list[str] = []
    if "`" in translated:
        translated = translated.replace("`", '"')
        warnings.append("backticks-to-double-quotes")
    camel_case_qualified = re.sub(
        r'(?<!["\'])\b([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*[A-Z][A-Za-z0-9_]*)\b',
        r'\1."\2"',
        translated,
    )
    if camel_case_qualified != translated:
        translated = camel_case_qualified
        warnings.append("camel-case-identifiers-quoted")
    quoted_aliases = re.sub(
        r"\bAS\s+([A-Za-z_][A-Za-z0-9_]*)\b",
        lambda match: (
            'AS "' + match[1] + '"'
            if match[1] != match[1].lower() and match[1] != match[1].upper()
            else match[0]
        ),
        translated,
        flags=re.IGNORECASE,
    )
    if quoted_aliases != translated:
        translated = quoted_aliases
        warnings.append("view-alias-case-preserved")
    if "==" in translated:
        translated = translated.replace("==", "=")
        warnings.append("double-equals-to-equals")
    if re_search(r"\browid\b", translated):
        # PocketBase declares this unknown expression as JSON in its schema,
        # while the SQL value remains an integer, not a JSONB expression.
        warnings.append("sqlite-rowid-preserved-as-integer")
    if "GROUP_CONCAT" in translated.upper():
        translated = _translate_group_concat(translated)
        warnings.append("group-concat-to-string-agg")
    for value in (",NAFAMA", ",NAFAMA,PAANI", ",PAANI", "", " ", "nil", "Inconnue", "UNKNOWN"):
        translated = translated.replace(f'"{value}"', f"'{value}'")
    translated = translated.replace('|| " " ||', "|| ' ' ||")
    translated = translated.replace('|| ""', "|| ''")
    translated = re_sub(
        r"(\b[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?)\s*=\s*null\b",
        r"\1 IS NULL",
        translated,
        flags=re.IGNORECASE,
    )
    if collection_name == "buma_article_list_view":
        join_fix = "COALESCE(stock.stockage, 0) = COALESCE(stockage.id, 0)"
        if join_fix in translated:
            translated = translated.replace(join_fix, "stock.stockage = stockage.id")
            warnings.append("relation-id-coalesce-removed")
    if collection_name == "buma_customer_pdv_list_selector_view":
        alias_filter = "WHERE bcpid = 'nil'"
        if alias_filter in translated:
            translated = translated.replace(
                alias_filter,
                "WHERE COALESCE(bcp.id, 'nil') = 'nil'",
            )
            warnings.append("select-alias-in-where-expanded")
    if collection_name == "buma_customer_mobile_list_view":
        unqualified_bool = re.sub(r"(?<![.\"])(\bisNafama\b)", r'"isNafama"', translated)
        if unqualified_bool != translated:
            translated = unqualified_bool
            warnings.append("unqualified-camel-case-identifier-quoted")
        translated = translated.replace(
            'COALESCE(asn."isNafama", 0)', 'COALESCE(asn."isNafama", false)'
        )
        grouped_flag = '        "isNafama",\n'
        if grouped_flag in translated:
            translated = translated.replace(
                grouped_flag,
                '        BOOL_OR("isNafama") AS "isNafama",\n',
            )
            warnings.append("grouped-bool-to-bool-or")
        if "MAX(\n        COALESCE(bc.updated" in translated:
            translated = translated.replace(
                "MAX(\n        COALESCE(bc.updated", "GREATEST(\n        COALESCE(bc.updated"
            )
            warnings.append("multi-argument-max-to-greatest")
        if "    bc.photo;" in translated and "    ud.user;" not in translated:
            translated = translated.replace("    bc.photo;", "    bc.photo,\n    ud.user;")
            warnings.append("outer-group-by-user-added")
        if "    ud.user;" in translated and '    asn."isNafama";' not in translated:
            translated = translated.replace("    ud.user;", '    ud.user,\n    asn."isNafama";')
            warnings.append("outer-group-by-bool-added")
        if '    asn."isNafama";' in translated and "    pdv.pdv_id;" not in translated:
            translated = translated.replace(
                '    asn."isNafama";',
                '    asn."isNafama",\n'
                "    pdv.pdv_id,\n"
                "    sn.sim_list,\n"
                "    asn.agent_sim_number,\n"
                "    asn.agent_sim_network,\n"
                "    sn.network_list,\n"
                "    pdv.updated,\n"
                "    sn.updated,\n"
                "    asn.updated,\n"
                "    ud.updated;",
            )
            warnings.append("outer-group-by-view-columns-added")
    if collection_name == "buma_sim_list_view" and "MAX(\n    COALESCE(sim.updated" in translated:
        translated = translated.replace(
            "MAX(\n    COALESCE(sim.updated", "GREATEST(\n    COALESCE(sim.updated"
        )
        warnings.append("multi-argument-max-to-greatest")
    if collection_name == "test" and "GROUP BY s.customer, a.id, a.designation" in translated:
        translated = translated.replace(
            "GROUP BY s.customer, a.id, a.designation",
            "GROUP BY s.customer, a.id, a.designation, c.firstname, c.lastname",
        )
        warnings.append("group-by-customer-name-added")
        translated = translated.replace(
            "ORDER BY customerName ASC, totalRevenue DESC",
            'ORDER BY "customerName" ASC, "totalRevenue" DESC',
        )
    if (
        collection_name == "buma_customer_mobile_list_view"
        and "MAX(\n        COALESCE" in translated
    ):
        warnings.append("manual-greatest-review-required")
    return translated, warnings


def re_search(pattern: str, value: str) -> bool:
    import re

    return re.search(pattern, value, flags=re.IGNORECASE) is not None


def re_sub(pattern: str, replacement: str, value: str, flags: int = 0) -> str:
    import re

    return re.sub(pattern, replacement, value, flags=flags)


def _translate_group_concat(query: str) -> str:
    import re

    pattern = re.compile(
        r"GROUP_CONCAT\(\s*(DISTINCT\s+)?([^,()]+?)\s*(?:,\s*'([^']*)')?\s*\)",
        flags=re.IGNORECASE,
    )

    def replacement(match: re.Match[str]) -> str:
        distinct = "DISTINCT " if match.group(1) else ""
        expression = match.group(2).strip()
        separator = match.group(3) or ","
        return f"STRING_AGG({distinct}CAST({expression} AS TEXT), '{separator}')"

    return pattern.sub(replacement, query)


def load_schema(
    schema_path: Path, include_views: bool
) -> tuple[list[dict[str, Any]], str, list[dict[str, Any]]]:
    raw = json.loads(schema_path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("PocketBase schema export must be a JSON array")
    definitions: list[dict[str, Any]] = []
    view_report: list[dict[str, Any]] = []
    for collection in raw:
        if not isinstance(collection, dict):
            continue
        collection_type = str(collection.get("type") or "base")
        if collection_type == "view" and not include_views:
            continue
        fields = [
            normalize_field(field)
            for field in (collection.get("schema") or [])
            if isinstance(field, dict)
        ]
        options = dict(collection.get("options") or {})
        warnings: list[str] = []
        if collection_type == "view":
            query = str(options.get("query") or "").strip()
            if not query:
                raise ValueError(f"View {collection.get('name')!r} has no query")
            options["query"], warnings = translate_view_query(str(collection.get("name")), query)
        indexes = [
            translate_index(index, str(collection.get("name")))
            for index in collection.get("indexes", [])
        ]
        normalized = CollectionCreate.model_validate(
            {
                "id": collection.get("id"),
                "name": collection.get("name"),
                "type": collection_type,
                "system": bool(collection.get("system", False)),
                "schema": fields,
                "indexes": indexes,
                "listRule": collection.get("listRule"),
                "viewRule": collection.get("viewRule"),
                "createRule": collection.get("createRule"),
                "updateRule": collection.get("updateRule"),
                "deleteRule": collection.get("deleteRule"),
                "options": options,
            }
        )
        definition = normalized.model_dump(by_alias=True)
        definitions.append(definition)
        if collection_type == "view":
            view_report.append(
                {
                    "name": collection.get("name"),
                    "warnings": warnings,
                    "source_query_sha256": hashlib.sha256(query.encode()).hexdigest(),
                    "translated_query_sha256": hashlib.sha256(
                        options["query"].encode()
                    ).hexdigest(),
                }
            )
    ids = [str(item.get("id") or "") for item in definitions]
    names = [str(item.get("name") or "") for item in definitions]
    if not all(ids) or len(ids) != len(set(ids)) or len(names) != len(set(names)):
        raise ValueError("Schema has missing or duplicate collection IDs/names")
    return definitions, hashlib.sha256(schema_path.read_bytes()).hexdigest(), view_report


def view_dependencies(definitions: list[dict[str, Any]]) -> dict[str, set[str]]:
    view_names = {
        str(definition["name"]) for definition in definitions if definition.get("type") == "view"
    }
    dependencies: dict[str, set[str]] = {}
    for definition in definitions:
        if definition.get("type") != "view":
            continue
        name = str(definition["name"])
        query = str((definition.get("options") or {}).get("query") or "")
        dependencies[name] = {
            candidate
            for candidate in view_names
            if candidate != name and re.search(rf"\b{re.escape(candidate)}\b", query)
        }
    return dependencies


def render_baseline(definitions: list[dict[str, Any]]) -> str:
    original_time = migration_generator.time.time
    migration_generator.time.time = lambda: BASELINE_TIMESTAMP
    try:
        with tempfile.TemporaryDirectory(prefix="buma-master-baseline-") as temp:
            path = migration_generator.generate_snapshot_migration(
                definitions,
                temp,
                include_system=False,
                view_dependencies=view_dependencies(definitions),
            )
            return Path(path).read_text(encoding="utf-8")
    finally:
        migration_generator.time.time = original_time


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--include-views", action="store_true")
    parser.add_argument("--views-only", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    definitions, digest, view_report = load_schema(args.schema, args.include_views)
    if args.views_only:
        if not args.include_views:
            raise SystemExit("--views-only requires --include-views")
        definitions = [item for item in definitions if item.get("type") == "view"]
    content = render_baseline(definitions)
    if args.check:
        if not args.output.exists() or args.output.read_text(encoding="utf-8") != content:
            raise SystemExit(f"Baseline is stale: {args.output}")
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(content, encoding="utf-8")
    report_path = args.report
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(
            {
                "schema_sha256": digest,
                "include_views": args.include_views,
                "definitions": len(definitions),
                "base_auth": sum(item["type"] != "view" for item in definitions),
                "views": sum(item["type"] == "view" for item in definitions),
                "views_report": view_report,
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps({"definitions": len(definitions), "views": len(view_report), "sha256": digest})
    )


if __name__ == "__main__":
    main()
