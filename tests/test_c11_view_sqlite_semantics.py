"""Migration 1789236851: thirteen views served like the SQLite views of PocketBase 0.22.34.

Static checks of the rewritten queries and of the migration's statements (a
fake migration app records them). The column types and collations, served
values, filters, sorts and pages are compared with the previous queries and
with the official 0.22.34 binary by scripts/c11_view_semantics_runtime.py.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.generate_native_baseline import load_schema

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "pb_migrations/1789236851_c11_view_sqlite_semantics.py"
SCHEMA = Path(
    os.environ.get(
        "BUMA_SCHEMA_EXPORT", "/Users/omarsisss/Downloads/buma-master-pb_schema-corrected.json"
    )
)
CONCATENATING = {
    "buma_customer_list_view",
    "buma_customer_list_view_with_users",
    "buma_customer_mobile_list_view",
    "buma_return_list_view",
    "buma_article_with_no_stock_list_view",
    "buma_stockage_no_manager",
}
NUMBERED = {
    "buma_sale_info",
    "buma_caisse_transaction_by_categorie_list_view",
    "buma_caisse_session_info",
    "test",
    "buma_article_list_stat_view",
    "buma_customer_supplier_info_view",
    "buma_return_info_view",
}
VIEWS = CONCATENATING | NUMBERED
# Their values are served as before (text and booleans, D-0039): not touched.
UNCHANGED = {"buma_agent_list_view", "buma_transaction_list_view"}


def load():
    spec = importlib.util.spec_from_file_location("wsh_view_semantics", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def module():
    return load()


@pytest.fixture(scope="module")
def translated():
    if not SCHEMA.is_file():
        pytest.skip("BUMA_SCHEMA_EXPORT is not available")
    definitions, _, _ = load_schema(SCHEMA, include_views=True)
    return {d["name"]: d for d in definitions}


@pytest.fixture(scope="module")
def migrated(module, translated):
    return {
        name: module.rewrite(name, translated[name]["options"]["query"], True) for name in VIEWS
    }


def test_only_the_thirteen_views_change_and_no_view_reads_them(module, translated):
    assert set(module.CHANGES) == set(module.QUERY_SHA256) == VIEWS
    assert not UNCHANGED & set(module.CHANGES)
    # No other view reads a recreated one (so no other DROP is needed), and
    # none of the thirteen reads another (any order of DROP and CREATE works).
    for name, item in translated.items():
        if item["type"] == "view":
            query = item["options"]["query"]
            assert not any(re.search(rf"\b{view}\b", query) for view in VIEWS - {name}), name


def test_every_row_number_view_of_the_export_is_numbered_here(translated):
    row_number = {
        name
        for name, item in translated.items()
        if item["type"] == "view" and "ROW_NUMBER() OVER()" in item["options"]["query"]
    }
    assert row_number == NUMBERED


def test_collection_ids_are_those_of_the_corrected_export(module, translated):
    for name, collection_id in module.COLLECTION_IDS.items():
        assert translated[name]["id"] == collection_id


def test_the_translated_queries_are_the_ones_the_migration_expects(module, translated):
    for name in VIEWS:
        query = translated[name]["options"]["query"]
        before, after = module.QUERY_SHA256[name]
        assert module.digest(query) == before, name
        forward = module.rewrite(name, query, True)
        assert module.digest(forward) == after, name
        assert module.rewrite(name, forward, False) == query, name
        for old, new in module.CHANGES[name]:
            assert query.count(old) == 1 and forward.count(new) == 1, (name, old[:40])


def test_rewrite_refuses_any_other_stored_query(module, translated):
    query = translated["buma_sale_info"]["options"]["query"]
    with pytest.raises(RuntimeError, match="unexpected stored view query"):
        module.rewrite("buma_sale_info", query + " ", True)
    with pytest.raises(RuntimeError, match="unexpected stored view query"):
        module.rewrite("buma_sale_info", query, False)  # not migrated yet
    forward = module.rewrite("buma_sale_info", query, True)
    with pytest.raises(RuntimeError, match="unexpected stored view query"):
        module.rewrite("buma_sale_info", forward, True)  # already migrated


def test_no_type_of_the_translation_changes_but_the_numbered_ids(module, migrated):
    # Text and booleans stay text and booleans (D-0039): no domain, no
    # operator, no json projection, no integer cast of a boolean.
    source = MIGRATION.read_text()
    for absent in ("CREATE DOMAIN", "CREATE OPERATOR", "buma_pb_json", "buma_sqlite_integer"):
        assert absent not in source, absent
    for name, query in migrated.items():
        assert "json" not in query.lower(), name
        assert "::integer" not in query, name
    mobile = migrated["buma_customer_mobile_list_view"]
    assert 'COALESCE(asn."isNafama", false) AS "isNafama",' in mobile
    for name in CONCATENATING - {
        "buma_article_with_no_stock_list_view",
        "buma_stockage_no_manager",
    }:
        query = migrated[name]
        assert re.search(r"COALESCE\([a-z_]+\.(sim_list|pdv_sim), ''\)", query), name
        assert re.search(r"COALESCE\([a-z_]+\.agent_sim_number, ''\)", query), name
        # MAX in SQLite's BINARY order, the translation's collation kept for
        # the sorts and filters of agent_sim.
        assert (
            'COALESCE(MAX(CAST(number AS TEXT) COLLATE "C"), \'\') COLLATE "default" '
            "AS agent_sim_number"
        ) in query, name
    assert (
        'MAX(CAST(network AS TEXT) COLLATE "C") COLLATE "default" AS agent_sim_network'
    ) in mobile


def test_payed_percent_divides_like_sqlite(migrated):
    # Integer division truncated toward zero when both sums are integers,
    # floating point otherwise, NULL for a zero divisor.
    query = migrated["buma_sale_info"]
    assert "div(SUM(CASE WHEN" in query and "remaining::bigint" in query
    assert "NULLIF(SUM(CASE WHEN" in query and "amount::bigint END), 0)" in query
    assert "ELSE (SUM(remaining) * 100) / NULLIF(SUM(amount), 0)" in query
    assert "::double precision" in query
    assert query.count("9223372036854775808::float8") == 8


@pytest.mark.parametrize(
    ("name", "window"),
    [
        (
            "buma_caisse_transaction_by_categorie_list_view",
            "ORDER BY f_rowid.source_rowid NULLS LAST, f.created, f.id",
        ),
        ("buma_caisse_session_info", 'ORDER BY bct.caisse COLLATE "C", bct.session COLLATE "C"'),
        (
            "test",
            'ORDER BY s.customer COLLATE "C", a.id COLLATE "C" NULLS FIRST, '
            'a.designation COLLATE "C" NULLS FIRST',
        ),
        ("buma_sale_info", ""),
        ("buma_article_list_stat_view", ""),
        ("buma_customer_supplier_info_view", ""),
        ("buma_return_info_view", ""),
    ],
)
def test_row_numbers_follow_sqlite_and_are_text_ids(migrated, name, window):
    # A deterministic numbering in SQLite's order (one row: "1"), the id cast
    # to text like PocketBase's normalizeViewQueryId, BINARY ordered.
    query = migrated[name]
    assert "ROW_NUMBER() OVER()" not in query
    assert query.count("ROW_NUMBER()") == 1
    assert f'CAST(ROW_NUMBER() OVER ({window}) AS TEXT) COLLATE "C" as id' in query


def test_the_caisse_expenses_are_numbered_through_their_archived_rowids(migrated):
    query = migrated["buma_caisse_transaction_by_categorie_list_view"]
    assert query.startswith("WITH filtered AS MATERIALIZED (\nSELECT b.id, b.amount,")
    assert (
        'AS TEXT) COLLATE "C" as id, f.amount, f.created, f."caisseName", f.label\n'
        "FROM filtered f\n"
        "LEFT JOIN buma_source_rowids('6y0f0rx6t5eauo1', ARRAY(SELECT id FROM filtered)) "
        "AS f_rowid ON f_rowid.record_id = f.id"
    ) in query


def test_every_concatenation_has_an_order(migrated):
    # No STRING_AGG without ORDER BY, no DISTINCT aggregate left.
    for name, query in migrated.items():
        for match in re.finditer(r"STRING_AGG\(", query):
            depth, end = 0, match.end()
            for end in range(match.end(), len(query)):
                depth += {"(": 1, ")": -1}.get(query[end], 0)
                if depth < 0:
                    break
            assert " ORDER BY " in query[match.end() : end], name
        assert "STRING_AGG(DISTINCT" not in query, name
    for name in ("buma_article_with_no_stock_list_view", "buma_stockage_no_manager"):
        assert 'ORDER BY CAST(bs.stockage AS TEXT) COLLATE "C")' in migrated[name]
    for name in CONCATENATING - {
        "buma_article_with_no_stock_list_view",
        "buma_stockage_no_manager",
        "buma_customer_mobile_list_view",
    }:
        assert (
            "STRING_AGG(CAST(sim.number AS TEXT), ',' ORDER BY p_rowid.source_rowid NULLS "
            'LAST, p.created, p.id, CAST(sim.number AS TEXT) COLLATE "C")'
        ) in migrated[name], name
    mobile = migrated["buma_customer_mobile_list_view"]
    assert "LEFT JOIN SimNetworks snw ON snw.client = bc.id" in mobile
    assert "    snw.network_list,\n" in mobile and "sn.network_list" not in mobile


def test_bare_columns_take_the_sqlite_row(migrated):
    mobile = migrated["buma_customer_mobile_list_view"]
    # isNafama: the first sim (rowid order) holding the greatest network.
    assert (
        '(ARRAY_AGG("isNafama" ORDER BY CAST(network AS TEXT) COLLATE "C" DESC, '
        "buma_sim_rowid.source_rowid NULLS LAST, buma_sim.created, buma_sim.id))[1]"
    ) in mobile
    assert "BOOL_OR" not in mobile
    assert (
        "LEFT JOIN buma_source_rowids('kr6zwb9oabrvxiq') AS buma_sim_rowid "
        "ON buma_sim_rowid.record_id = buma_sim.id"
    ) in mobile
    # UserData.user was unquoted, i.e. CURRENT_USER in PostgreSQL.
    assert "        user,\n" not in mobile
    assert '(ARRAY_AGG("user" ORDER BY updated DESC, ' in mobile


def test_the_only_function_reads_the_import_archive(module):
    rowids = module.SOURCE_ROWIDS
    assert module.FUNCTIONS == ("buma_source_rowids(text, text[])",)
    assert "to_regclass('public._buma_c11_rowids') IS NULL THEN\n        RETURN;" in rowids
    assert "archive.record_id = ANY (p_ids)" in rowids
    assert "CREATE TABLE" not in rowids  # the importer refuses an existing archive
    assert module.rowid_order("p") == "p_rowid.source_rowid NULLS LAST, p.created, p.id"
    # A small collection is read whole (no InitPlan: the views stay parallel).
    assert module.PDV_ROWIDS == (
        "LEFT JOIN buma_source_rowids('l0wk3fy5xnw6et7') AS p_rowid ON p_rowid.record_id = p.id"
    )
    assert "p_ids text[] DEFAULT NULL" in rowids and "IF p_ids IS NULL THEN" in rowids


def test_rowid_orders_read_the_archive_once_per_table(migrated):
    # No per-row function call: one buma_source_rowids join per ordered table.
    expected = {
        "buma_customer_list_view": 1,
        "buma_customer_list_view_with_users": 2,
        "buma_customer_mobile_list_view": 3,
        "buma_return_list_view": 1,
        "buma_caisse_transaction_by_categorie_list_view": 1,
    }
    for name, query in migrated.items():
        assert query.count("buma_source_rowids(") == expected.get(name, 0), name
        assert "buma_source_rowid(" not in query, name


class Result:
    def __init__(self, rows=(), scalar=None):
        self.rows, self.scalar = list(rows), scalar

    def all(self):
        return self.rows

    def scalar_one(self):
        return self.scalar


class App:
    """Records the statements; serves _collections rows from ``queries``."""

    def __init__(self, module, queries, *, ids=None):
        self.module, self.queries = module, dict(queries)
        self.ids = dict(module.COLLECTION_IDS if ids is None else ids)
        self.statements: list[tuple[str, dict | None]] = []

    async def find_collection(self, name):
        return SimpleNamespace(
            id=f"id-{name}", type="view", options={"query": self.queries[name], "other": 1}
        )

    async def execute_sql(self, sql, params=None):
        self.statements.append((sql, params))
        if sql.startswith("SELECT id, name FROM _collections"):
            return Result([(i, n) for n, i in self.ids.items()])
        if sql.startswith("UPDATE _collections"):
            options = json.loads(params["options"])
            self.queries[params["id"].removeprefix("id-")] = options["query"]
            assert options["other"] == 1  # other options are kept
        return Result()

    def ddl(self):
        statements = (s.strip() for s, _ in self.statements)
        return [s.split(" AS ", 1)[0] for s in statements if s.startswith(("DROP", "CREATE"))]


def stored(translated):
    return {name: translated[name]["options"]["query"] for name in VIEWS}


def test_up_creates_the_function_then_recreates_the_views(module, translated, migrated):
    app = App(module, stored(translated))
    asyncio.run(module.up(app))
    ddl = app.ddl()
    assert ddl[0].startswith("CREATE FUNCTION buma_source_rowids(")
    assert ddl[1:14] == [f'DROP VIEW "{n}"' for n in module.CHANGES]
    assert ddl[14:] == [f'CREATE VIEW "{n}"' for n in module.CHANGES]
    assert "CASCADE" not in " ".join(s for s, _ in app.statements)
    assert app.queries == migrated


def test_down_restores_the_previous_queries_and_drops_the_function(module, translated):
    app = App(module, stored(translated))
    asyncio.run(module.up(app))
    up = len(app.statements)
    asyncio.run(module.down(app))
    assert app.queries == stored(translated)
    down = [s.strip() for s, _ in app.statements[up:]]
    assert "CASCADE" not in " ".join(down)
    assert down[-1] == "DROP FUNCTION buma_source_rowids(text, text[])"
    views = [s.split(" AS ", 1)[0] for s in down if "VIEW" in s]
    assert views == [f'DROP VIEW "{n}"' for n in module.CHANGES] + [
        f'CREATE VIEW "{n}"' for n in module.CHANGES
    ]


def test_up_refuses_other_collection_ids_before_any_view_change(module, translated):
    app = App(module, stored(translated), ids={"buma_pdv": "other0000000001"})
    with pytest.raises(RuntimeError, match="collection ids"):
        asyncio.run(module.up(app))
    assert not [s for s in app.ddl() if "VIEW" in s]


def test_up_refuses_an_unexpected_stored_query_before_any_view_change(module, translated):
    queries = stored(translated)
    queries["buma_stockage_no_manager"] += "\n"
    app = App(module, queries)
    with pytest.raises(RuntimeError, match="unexpected stored view query"):
        asyncio.run(module.up(app))
    assert not [s for s in app.ddl() if "VIEW" in s]
