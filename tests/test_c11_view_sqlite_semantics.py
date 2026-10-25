"""Migration 1789236851: the ten views served like the SQLite views of PocketBase 0.22.34.

Static checks of the rewritten queries and of the migration's statements (a
fake migration app records them). The served values, filters and sorts are
compared with the official 0.22.34 binary by scripts/c11_view_semantics_runtime.py.
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
VIEWS = {
    "buma_agent_list_view",
    "buma_customer_list_view",
    "buma_customer_list_view_with_users",
    "buma_customer_mobile_list_view",
    "buma_return_list_view",
    "buma_transaction_list_view",
    "buma_sale_info",
    "buma_caisse_transaction_by_categorie_list_view",
    "buma_article_with_no_stock_list_view",
    "buma_stockage_no_manager",
}


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


def test_only_the_ten_views_change_and_their_dependent_is_kept(module, translated):
    assert set(module.CHANGES) == set(module.QUERY_SHA256) == VIEWS
    assert module.DEPENDENTS == ("test",)
    assert "buma_transaction_list_view" in translated["test"]["options"]["query"]
    # No other view reads a recreated one (so no other DROP is needed).
    for name, item in translated.items():
        if item["type"] == "view" and name not in VIEWS | {"test"}:
            query = item["options"]["query"]
            assert not any(re.search(rf"\b{view}\b", query) for view in VIEWS), name


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


def test_number_like_texts_are_json_served_like_pocketbase(module, migrated, translated):
    # (a) json PrepareValue of a text: the json domain, the fallback '' is the
    # string "".
    assert (
        "buma_pb_json_text(COALESCE(si.number, '')) as number" in (migrated["buma_agent_list_view"])
    )
    for name in (
        "buma_customer_list_view",
        "buma_customer_list_view_with_users",
        "buma_customer_mobile_list_view",
        "buma_return_list_view",
    ):
        query = migrated[name]
        # DISTINCT / GROUP BY need an equality json lacks: text inside, json
        # in the outer projection, every column in the order of the view.
        head, tail = module.wrapper(name)
        assert query.startswith(head) and query.endswith(tail), name
        assert query.count("buma_pb_json_text(") == 2, name
        assert "buma_pb_json_text(w.pdv_sim) AS pdv_sim" in head, name
        assert "buma_pb_json_text(w.agent_sim) AS agent_sim" in head, name
        columns = module.WRAPPED[name].split()
        fields = [f["name"] for f in translated[name]["schema"]]
        assert set(columns) - {"id", "created", "updated"} == set(fields), name
        assert re.search(r"COALESCE\([a-z_]+\.(sim_list|pdv_sim), ''\)", query), name
        assert re.search(r"COALESCE\([a-z_]+\.agent_sim_number, ''\)", query), name
        assert 'MAX(CAST(number AS TEXT) COLLATE "C")' in query, name
        assert "jsonb" not in query, name


def test_sqlite_integer_booleans_are_integers(migrated):
    # (b) SQLite has no boolean storage class: 1/0 integers of the domain
    # whose operators compare them with true/false like PocketBase.
    mobile = migrated["buma_customer_mobile_list_view"]
    assert (
        'CAST(COALESCE(asn."isNafama"::integer, 0) AS buma_sqlite_integer) AS "isNafama"'
    ) in mobile
    assert "BOOL_OR" not in mobile
    transactions = migrated["buma_transaction_list_view"]
    assert "THEN CAST((sale.amount = sale.payed)::integer AS buma_sqlite_integer)" in transactions
    assert "ELSE CAST(retour.payed::integer AS buma_sqlite_integer)" in transactions
    assert (
        "THEN CAST(1 AS buma_sqlite_integer)\n    ELSE CAST(0 AS buma_sqlite_integer)\n"
        '  END) AS "salePayed"'
    ) in transactions
    assert "THEN true" not in transactions


def test_payed_percent_divides_like_sqlite(migrated):
    # (c) integer division truncated toward zero when both sums are integers,
    # floating point otherwise, NULL for a zero divisor.
    query = migrated["buma_sale_info"]
    assert "div(SUM(CASE WHEN" in query and "remaining::bigint" in query
    assert "NULLIF(SUM(CASE WHEN" in query and "amount::bigint END), 0)" in query
    assert "ELSE (SUM(remaining) * 100) / NULLIF(SUM(amount), 0)" in query
    assert query.count("9223372036854775808::float8") == 8


def test_row_numbers_follow_the_sqlite_scan_and_are_text_ids(migrated):
    # (d) a deterministic numbering in rowid order, id cast to text like
    # PocketBase's normalizeViewQueryId.
    query = migrated["buma_caisse_transaction_by_categorie_list_view"]
    assert "ROW_NUMBER() OVER()" not in query
    assert query.startswith("WITH filtered AS MATERIALIZED (\nSELECT b.id, b.amount,")
    assert (
        "SELECT CAST(ROW_NUMBER() OVER (ORDER BY f_rowid.source_rowid NULLS LAST, f.created, "
        'f.id) AS TEXT) COLLATE "C" as id, f.amount, f.created, f."caisseName", f.label\n'
        "FROM filtered f\n"
        "LEFT JOIN buma_source_rowids('6y0f0rx6t5eauo1', ARRAY(SELECT id FROM filtered)) "
        "AS f_rowid ON f_rowid.record_id = f.id"
    ) in query
    assert migrated["buma_sale_info"].startswith(
        'SELECT CAST(ROW_NUMBER() OVER () AS TEXT) COLLATE "C" as id,'
    )


def test_every_concatenation_has_an_order(migrated):
    # (e) no STRING_AGG without ORDER BY, no DISTINCT aggregate left.
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
    assert (
        "LEFT JOIN buma_source_rowids('kr6zwb9oabrvxiq') AS buma_sim_rowid "
        "ON buma_sim_rowid.record_id = buma_sim.id"
    ) in mobile
    # UserData.user was unquoted, i.e. CURRENT_USER in PostgreSQL.
    assert "        user,\n" not in mobile
    assert '(ARRAY_AGG("user" ORDER BY updated DESC, ' in mobile


def test_functions_follow_pocketbase_and_the_import_archive(module):
    body = module.PB_JSON_TEXT
    assert "RETURNS buma_pb_json" in body
    assert "WHEN value = 'null' THEN NULL" in body  # JSON_EXTRACT's NULL, served null
    assert "WHEN value IN ('true', 'false') THEN value::json" in body
    assert "IN ('0', '1', '2', '3', '4', '5', '6', '7', '8', '9', '-', '\"', '[', '{')" in body
    assert "pg_input_is_valid(value, 'json')" in body and "to_json(value)" in body
    assert "jsonb" not in body
    # A SQL function PostgreSQL inlines (no STRICT, no plpgsql call per row).
    assert "LANGUAGE sql STABLE PARALLEL SAFE" in body and "STRICT" not in body
    # json keeps the text (key order, \u0000); smallint holds SQLite's 0/1.
    assert module.DOMAINS == (("buma_pb_json", "json"), ("buma_sqlite_integer", "smallint"))
    rowids = module.SOURCE_ROWIDS
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

    def __init__(self, module, queries, *, version=160015, ids=None):
        self.module, self.queries, self.version = module, dict(queries), version
        self.ids = dict(module.COLLECTION_IDS if ids is None else ids)
        self.statements: list[tuple[str, dict | None]] = []

    async def find_collection(self, name):
        return SimpleNamespace(
            id=f"id-{name}", type="view", options={"query": self.queries[name], "other": 1}
        )

    async def execute_sql(self, sql, params=None):
        self.statements.append((sql, params))
        if sql.startswith("SELECT current_setting"):
            return Result(scalar=self.version)
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
    return {name: translated[name]["options"]["query"] for name in VIEWS | {"test"}}


def test_up_recreates_the_views_in_dependency_order(module, translated, migrated):
    app = App(module, stored(translated))
    asyncio.run(module.up(app))
    ddl = app.ddl()
    # Domains, then functions and operators, then the views.
    assert ddl[:2] == ["CREATE DOMAIN buma_pb_json", "CREATE DOMAIN buma_sqlite_integer"]
    functions = [s for s in ddl if s.startswith("CREATE FUNCTION")]
    assert len(functions) == 2 + 4 + 48
    assert len([s for s in ddl if s.startswith("CREATE OPERATOR")]) == 48
    first_view = next(i for i, s in enumerate(ddl) if "VIEW" in s)
    assert all("VIEW" in s for s in ddl[first_view:])
    views = [s for s in ddl if "VIEW" in s]
    assert views[0] == 'DROP VIEW "test"'
    assert views[1:11] == [f'DROP VIEW "{n}"' for n in module.CHANGES]
    assert views[11:21] == [f'CREATE VIEW "{n}"' for n in module.CHANGES]
    assert views[21:] == ['CREATE VIEW "test"']
    assert "CASCADE" not in " ".join(s for s, _ in app.statements)
    # The dependent view keeps its stored query; the ten take the new one.
    recreated = [s for s, _ in app.statements if s.startswith('CREATE VIEW "test" AS ')]
    assert recreated == ['CREATE VIEW "test" AS ' + translated["test"]["options"]["query"]]
    assert {n: app.queries[n] for n in VIEWS} == migrated
    assert app.queries["test"] == translated["test"]["options"]["query"]


def test_down_restores_the_previous_queries_and_drops_what_up_created(module, translated):
    app = App(module, stored(translated))
    asyncio.run(module.up(app))
    up = len(app.statements)
    asyncio.run(module.down(app))
    assert app.queries == stored(translated)
    down = [s for s, _ in app.statements[up:]]
    assert "CASCADE" not in " ".join(down)
    # The views first (their old queries use none of these objects), then the
    # operators and functions, the domains last.
    last_view = max(i for i, s in enumerate(down) if "VIEW" in s)
    assert not any("VIEW" in s for s in down[last_view + 1 :])
    dropped = [s for s in down[last_view + 1 :]]
    assert len([s for s in dropped if s.startswith("DROP OPERATOR")]) == 48
    assert dropped[-4:] == [
        "DROP FUNCTION buma_pb_json_text(text)",
        "DROP FUNCTION buma_source_rowids(text, text[])",
        "DROP DOMAIN buma_sqlite_integer",
        "DROP DOMAIN buma_pb_json",
    ]
    created = [s for s, _ in app.statements[:up] if s.startswith("CREATE OPERATOR")]
    assert len(created) == 48
    functions = {
        s.split("CREATE FUNCTION ", 1)[1].split(")", 1)[0]
        for s, _ in app.statements[:up]
        if s.strip().startswith("CREATE FUNCTION")
    }
    assert len(functions) == 54
    assert len([s for s in dropped if s.startswith("DROP FUNCTION")]) == 54


@pytest.mark.parametrize(
    ("version", "ids", "match"),
    [
        (150008, None, "PostgreSQL 16"),
        (160015, {"buma_pdv": "other0000000001"}, "collection ids"),
    ],
)
def test_up_refuses_before_any_view_change(module, translated, version, ids, match):
    app = App(module, stored(translated), version=version, ids=ids)
    with pytest.raises(RuntimeError, match=match):
        asyncio.run(module.up(app))
    assert not [s for s in app.ddl() if "VIEW" in s]


def test_up_refuses_an_unexpected_stored_query_before_any_view_change(module, translated):
    queries = stored(translated)
    queries["buma_stockage_no_manager"] += "\n"
    app = App(module, queries)
    with pytest.raises(RuntimeError, match="unexpected stored view query"):
        asyncio.run(module.up(app))
    assert not [s for s in app.ddl() if "VIEW" in s]


def test_operators_cover_every_form_ppbase_sends_and_no_domain_pair(module):
    # PPBase sends column <op> value: an untyped string, an integer or numeric
    # literal, TRUE/FALSE; in either operand order.
    operators = {(op, left, right) for op, _, left, right, _ in module.COMPARISON_OPERATORS}
    expected = set()
    for other in ("text", "double precision", "boolean"):
        for op in ("=", "<>", "<", "<=", ">", ">="):
            expected |= {(op, "buma_pb_json", other), (op, other, "buma_pb_json")}
    for op in ("=", "<>", "<", "<=", ">", ">="):
        expected |= {(op, "buma_sqlite_integer", "boolean"), (op, "boolean", "buma_sqlite_integer")}
    assert operators == expected
    # No domain = domain: PostgreSQL would then type an untyped parameter as
    # the domain and parse "" with the json input (typeconv-oper.html step 2).
    assert not [o for o in operators if o[1] == o[2]]
    restricts = {
        module.operator_ddl(op, function, left, right).rsplit(", ", 1)[-1]
        for op, function, left, right, _ in module.COMPARISON_OPERATORS
        if op in ("=", "<>")
    }
    assert restricts == {"RESTRICT = eqsel)", "RESTRICT = neqsel)"}


def test_equal_and_not_equal_are_null_safe_like_pocketbase_is(module):
    # PocketBase: json fields compare with IS / IS NOT (filter.go l.308-317).
    bodies = {
        (function, left, right): ddl
        for _, function, left, right, ddl in module.COMPARISON_OPERATORS
    }
    eq = bodies[("buma_pb_json_eq", "buma_pb_json", "text")]
    assert (
        "SELECT COALESCE(buma_pb_json_cmp(a, b) = 0, "
        "(a IS NULL OR json_typeof(a) = 'null') AND b IS NULL)"
    ) in eq
    ne = bodies[("buma_sqlite_integer_ne", "boolean", "buma_sqlite_integer")]
    assert "(b boolean, a buma_sqlite_integer)" in ne
    assert "SELECT NOT COALESCE(buma_sqlite_integer_cmp(a, b) = 0, a IS NULL AND b IS NULL)" in ne
    # The reversed operand order flips the ordering operators.
    lt = bodies[("buma_pb_json_lt", "double precision", "buma_pb_json")]
    assert "SELECT buma_pb_json_cmp(a, b) > 0 $$" in lt
    assert (
        "SELECT buma_pb_json_cmp(a, b) < 0 $$"
        in bodies[("buma_pb_json_lt", "buma_pb_json", "double precision")]
    )


def test_compare_functions_follow_sqlite_storage_classes(module):
    text_cmp, number_cmp, boolean_cmp, integer_cmp = module.COMPARE_FUNCTIONS
    # NULL < INTEGER/REAL < TEXT, text by BINARY (datatype3.html 4.1, 7).
    assert "WHEN json_typeof(a) IN ('number', 'boolean') THEN -1" in text_cmp
    assert '(a #>> \'{}\') COLLATE "C" < b COLLATE "C"' in text_cmp
    assert "WHEN json_typeof(a) IN ('string', 'array', 'object') THEN 1" in number_cmp
    assert "(a #>> '{}')::numeric < b::numeric" in number_cmp
    # true/false are the SQL literals 1/0 for PocketBase (filter.go l.250-258).
    assert "buma_pb_json_cmp(a, b::integer::double precision)" in boolean_cmp
    assert "a < b::integer" in integer_cmp
    for body in module.COMPARE_FUNCTIONS:
        assert "LANGUAGE sql IMMUTABLE PARALLEL SAFE" in body
