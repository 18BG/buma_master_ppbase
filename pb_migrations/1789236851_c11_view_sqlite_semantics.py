"""Serve from ten views the values PocketBase 0.22.34 serves from their SQLite views.

PocketBase reads every view column as text (``dbx.NullStringMap``,
``models/record.go`` l.50-58) and prepares it with the field type of the view
schema; an expression column is a ``json`` field (``daos/view.go`` l.230-238).
A json field turns a text that is a JSON number, list, object, ``null``,
``true`` or ``false`` (first byte a digit, ``-``, ``"``, ``[`` or ``{``) into
that JSON value and quotes any other text (``models/schema/schema_field.go``
l.312-343, served as is by ``tools/types/json_raw.go`` l.26-32). The view
itself is ``CREATE VIEW name AS SELECT * FROM (query)`` (``daos/view.go``
l.59). The PostgreSQL translation (1789236842, 1789236847, 1789236848) served
other values; only the ten views below (and ``test``, recreated unchanged
because it reads ``buma_transaction_list_view``) are dropped and recreated, in
dependency order, without CASCADE. Their ``_collections`` schema and rules are
not touched; ``options.query`` takes the new query.

* Text served as a JSON number (``agent_sim``, ``pdv_sim``, agent ``number``):
  ``buma_pb_json_text`` applies the json PrepareValue rule to the text and
  returns the domain ``buma_pb_json`` (``json``, which keeps the text, so key
  order and ``\\u0000`` stay as in PocketBase); the fallback ``''`` is the JSON
  string ``""`` and the text ``null`` is SQL NULL (served ``null``, like
  JSON_EXTRACT reads it). asyncpg decodes it with the json codec of SQLAlchemy;
  PPBase serves a whole float as an integer (``1e3`` as ``1000``, D-0029 kind).
  json has no equality, so the four customer/return views, which group by or
  select DISTINCT these columns, compute them as text (as before) and convert
  them in an outer projection that keeps every column in its place.
* SQLite booleans are integers (datatype3.html 2.1): ``isNafama``, ``payed``
  and ``salePayed`` are the integers 1/0 of the domain ``buma_sqlite_integer``
  (``smallint``), not booleans.
* ``payedPercent``: SQLite divides two integers with an integer result
  truncated toward zero, and floating point when either operand is real
  (lang_expr.html, "Operators"); ``sum()`` is an integer when every input is
  an integer (lang_aggfunc.html); a NUMERIC column stores a whole real as an
  integer (datatype3.html 3); a zero divisor gives NULL (PostgreSQL raises).
  The integer branch is exact. With a real operand the last digits may differ:
  SQLite 3.43+ sums floats with Kahan-Babuska-Neumaier compensation
  (lang_aggfunc.html, sum), PostgreSQL ``SUM(float8)`` adds them plainly.
* ``ROW_NUMBER() OVER ()`` numbers the rows of SQLite's scan, the rowid order of
  ``buma_caisse_transactions`` (plan ``SCAN b``, lookups by id); PostgreSQL
  renumbered them on each query. PocketBase also wraps a view whose id is an
  expression in ``SELECT cast(id as text) id, ...`` (``daos/collection.go``
  l.398-445, ``normalizeViewQueryId``): the id is a string, sorted as text
  and found by its text; PPBase served a bigint (and its view by id failed).
  Only these two of the seven ROW_NUMBER views change: the other five keep a
  bigint id and an unordered numbering (open, outside the WS-H codes).
* ``group_concat`` order is "arbitrary" (lang_aggfunc.html); PocketBase serves
  SQLite's: the rows of the scan of the outer table (rowid order), each joined
  table read through an automatic covering index keyed by the join column,
  then the other used columns in table order, then rowid (plan ``SEARCH ...
  USING AUTOMATIC COVERING INDEX``), the GROUP BY sorter keeping that order;
  ``DISTINCT`` keeps the first occurrence. Text compares with BINARY
  (memcmp, datatype3.html 7): ``COLLATE "C"``. The order is the one of the
  list request; for ``buma_article_with_no_stock_list_view`` and
  ``buma_stockage_no_manager`` SQLite plans a view by id without that index
  and concatenates there in rowid order, which PPBase does not reproduce.
* A bare column of an aggregate with several ``max()`` takes the row of the
  last one (lang_select.html 2.5 leaves the choice undefined; observed on
  SQLite 3.45 and on the 0.22.34 binary): ``isNafama`` comes from the first
  sim, in rowid order, that has the greatest ``network``; with one ``max()``
  it is the first row holding it (``user`` of ``UserData``, which the
  translation had left unquoted: PostgreSQL read it as ``CURRENT_USER``).

Filters. PocketBase compares a json field through ``JSON_EXTRACT`` with ``IS``
/ ``IS NOT`` for ``=`` / ``!=`` (``resolvers/record_field_resolve_runner.go``
l.465-474, ``tools/dbutils/json.go`` l.30-47, ``tools/search/filter.go``
l.290-317), a number literal as a float64 parameter and ``true`` / ``false``
as the SQL literals 1 / 0 (filter.go l.226-283); SQLite orders NULL < numbers
< text (datatype3.html 4.1). PPBase sends ``column <op> value`` with an
untyped string parameter, an integer or numeric literal, or TRUE / FALSE. The
operators ``= <> < <= > >=`` created here between ``buma_pb_json`` and
``text`` / ``double precision`` / ``boolean`` (both operand orders), and
between ``buma_sqlite_integer`` and ``boolean``, give those PocketBase results:
the domain has no ``=`` of its own, so PostgreSQL resolves an untyped
parameter to ``text`` (typeconv-oper.html, steps 2 and 3) and ``""`` or
``"72000001,72000002"`` never reach the json input function; ``payed = true``
is no longer ``integer = boolean``. Not covered (PPBase answers 500): two json
columns compared with each other, a sort on a ``buma_pb_json`` column on a
PPBase that sorts the column itself (one that sorts ``to_jsonb(column)``, as
PPBase WS-A11 does for json fields, sorts it in jsonb order), a string
compared with a ``buma_sqlite_integer`` (as before for the booleans) and a
number compared with the text ids (``id = 3``; PocketBase compares ``'3'`` with
``'3.0'`` and finds nothing, ``id = "3"`` works). PocketBase filters
JSON_EXTRACT of the stored text and serves PrepareValue: a JSON value after
whitespace (``' 7'``) is a number for its filters and a string when served;
the domain holds the served value (no such sim number in the real backup). An
implicit cast also lets these operators compare a plain ``json`` or
``smallint`` value: no PPBase or BUMA table, view or query has one.

The SQLite rowid of an imported record is kept by the C11 importer in
``_buma_c11_rowids`` (scripts/c11_import.py), a table that only the import
creates and that must not exist before it. ``buma_source_rowids`` returns the
archived rowids of the given records when it exists and no row otherwise, so
the rowid order is ``source rowid NULLS LAST, created, id``: imported records
in their SQLite order, then records created afterwards in their creation
order, as SQLite gives a new record the rowid after the greatest one. The
archive must be kept after the cutover (and in every backup and restore):
without it the order falls back to ``created, id``. One lookup per query and
per table, not one function call per row: the archived rows of a small
collection (pdv, sim, customer user), or of the filtered caisse expenses.

Requires PostgreSQL 16 (``pg_input_is_valid``). Apply with no PPBase server
running: eight of these views change column types, and a running server keeps
asyncpg prepared statements of the old ones.
"""

from __future__ import annotations

import hashlib
import json

MIN_SERVER_VERSION = 160000

JSON_DOMAIN = "buma_pb_json"
INTEGER_DOMAIN = "buma_sqlite_integer"
DOMAINS = (
    (JSON_DOMAIN, "json"),
    (INTEGER_DOMAIN, "smallint"),
)

# A SQL function (inlined in the view, no call per row): NULL stays NULL, and
# so does the text 'null': PocketBase serves it as the JSON null and its
# filters read it through JSON_EXTRACT, which gives SQL NULL.
PB_JSON_TEXT = """
CREATE FUNCTION buma_pb_json_text(value text) RETURNS buma_pb_json
LANGUAGE sql STABLE PARALLEL SAFE AS $$
-- PocketBase 0.22.34 json PrepareValue of a string (schema_field.go l.312-343).
SELECT CAST(CASE
    WHEN value = 'null' THEN NULL
    WHEN value IN ('true', 'false') THEN value::json
    WHEN left(value, 1) IN ('0', '1', '2', '3', '4', '5', '6', '7', '8', '9', '-', '"', '[', '{')
        AND pg_input_is_valid(value, 'json') THEN value::json
    ELSE to_json(value)
END AS buma_pb_json)
$$
"""

# The archived rowids of a collection, or of the given records only.
SOURCE_ROWIDS = """
CREATE FUNCTION buma_source_rowids(p_collection text, p_ids text[] DEFAULT NULL)
RETURNS TABLE (record_id text, source_rowid bigint)
LANGUAGE plpgsql STABLE PARALLEL SAFE ROWS 1000 AS $$
BEGIN
    -- The archive of the C11 import, absent before it (never created here).
    IF to_regclass('public._buma_c11_rowids') IS NULL THEN
        RETURN;
    END IF;
    IF p_ids IS NULL THEN
        RETURN QUERY SELECT archive.record_id, archive.source_rowid
            FROM public._buma_c11_rowids AS archive
            WHERE archive.collection_id = p_collection;
    ELSE
        RETURN QUERY SELECT archive.record_id, archive.source_rowid
            FROM public._buma_c11_rowids AS archive
            WHERE archive.collection_id = p_collection AND archive.record_id = ANY (p_ids);
    END IF;
END
$$
"""

# SQLite comparison of JSON_EXTRACT(value) with a parameter, -1/0/1, NULL when
# either side is NULL (a JSON null is SQL NULL): numbers (a JSON true/false is
# the integer 1/0) < text (datatype3.html 4.1), text by BINARY.
JSON_COMPARE_TEXT = """
CREATE FUNCTION buma_pb_json_cmp(a buma_pb_json, b text) RETURNS integer
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
SELECT CASE
    WHEN b IS NULL THEN NULL
    WHEN json_typeof(a) IN ('number', 'boolean') THEN -1
    WHEN json_typeof(a) = 'string' THEN CASE
        WHEN (a #>> '{}') = b THEN 0
        WHEN (a #>> '{}') COLLATE "C" < b COLLATE "C" THEN -1
        ELSE 1 END
    WHEN json_typeof(a) IN ('array', 'object') THEN CASE
        WHEN a::text = b THEN 0
        WHEN a::text COLLATE "C" < b COLLATE "C" THEN -1
        ELSE 1 END
END
$$
"""
JSON_COMPARE_NUMBER = """
CREATE FUNCTION buma_pb_json_cmp(a buma_pb_json, b double precision) RETURNS integer
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
SELECT CASE
    WHEN b IS NULL THEN NULL
    WHEN json_typeof(a) = 'number' THEN CASE
        WHEN (a #>> '{}')::numeric < b::numeric THEN -1
        WHEN (a #>> '{}')::numeric = b::numeric THEN 0
        ELSE 1 END
    WHEN json_typeof(a) = 'boolean' THEN CASE
        WHEN (a #>> '{}')::boolean::integer < b THEN -1
        WHEN (a #>> '{}')::boolean::integer = b THEN 0
        ELSE 1 END
    WHEN json_typeof(a) IN ('string', 'array', 'object') THEN 1
END
$$
"""
JSON_COMPARE_BOOLEAN = """
CREATE FUNCTION buma_pb_json_cmp(a buma_pb_json, b boolean) RETURNS integer
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
SELECT buma_pb_json_cmp(a, b::integer::double precision)
$$
"""
INTEGER_COMPARE_BOOLEAN = """
CREATE FUNCTION buma_sqlite_integer_cmp(a buma_sqlite_integer, b boolean) RETURNS integer
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
SELECT CASE WHEN a < b::integer THEN -1 WHEN a = b::integer THEN 0 WHEN a > b::integer THEN 1 END
$$
"""

# PostgreSQL operator -> function suffix; the reversed operand order flips <, >.
COMPARISONS = {"=": "eq", "<>": "ne", "<": "lt", "<=": "le", ">": "gt", ">=": "ge"}
SIGNS = {"lt": "<", "le": "<=", "gt": ">", "ge": ">="}
FLIPPED = {"<": ">", "<=": ">=", ">": "<", ">=": "<="}
RESTRICT = {"eq": "eqsel", "ne": "neqsel"}
# domain -> (types it is compared with, SQL telling that the domain value is NULL)
COMPARED = {
    JSON_DOMAIN: (
        ("text", "double precision", "boolean"),
        "(a IS NULL OR json_typeof(a) = 'null')",
    ),
    INTEGER_DOMAIN: (("boolean",), "a IS NULL"),
}


def _comparison_sql(domain: str, other: str, name: str, reverse: bool) -> str:
    """Operator function ``domain <op> other`` (or ``other <op> domain``).

    ``=`` / ``<>`` are PocketBase's ``IS`` / ``IS NOT`` (NULL-safe); the
    ordering operators are NULL when a side is NULL, as in SQLite.
    """
    _, domain_null = COMPARED[domain]
    cmp = f"{domain}_cmp(a, b)"
    if name in ("eq", "ne"):
        body = f"COALESCE({cmp} = 0, {domain_null} AND b IS NULL)"
        body = body if name == "eq" else f"NOT {body}"
    else:
        sign = SIGNS[name]
        body = f"{cmp} {FLIPPED[sign] if reverse else sign} 0"
    arguments = f"b {other}, a {domain}" if reverse else f"a {domain}, b {other}"
    return (
        f"CREATE FUNCTION {domain}_{name}({arguments}) RETURNS boolean\n"
        f"LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$ SELECT {body} $$"
    )


def _comparisons() -> list[tuple[str, str, str, str, str]]:
    """(operator, function, left type, right type, function DDL) of every operator."""
    out = []
    for domain, (others, _) in COMPARED.items():
        for other in others:
            for reverse in (False, True):
                left, right = (other, domain) if reverse else (domain, other)
                for operator, name in COMPARISONS.items():
                    ddl = _comparison_sql(domain, other, name, reverse)
                    out.append((operator, f"{domain}_{name}", left, right, ddl))
    return out


COMPARISON_OPERATORS = _comparisons()
COMPARE_FUNCTIONS = (
    JSON_COMPARE_TEXT,
    JSON_COMPARE_NUMBER,
    JSON_COMPARE_BOOLEAN,
    INTEGER_COMPARE_BOOLEAN,
)
COMPARE_SIGNATURES = (
    "buma_pb_json_cmp(buma_pb_json, text)",
    "buma_pb_json_cmp(buma_pb_json, double precision)",
    "buma_pb_json_cmp(buma_pb_json, boolean)",
    "buma_sqlite_integer_cmp(buma_sqlite_integer, boolean)",
)


def operator_ddl(operator: str, function: str, left: str, right: str) -> str:
    restrict = RESTRICT.get(function.rsplit("_", 1)[1])
    clause = f", RESTRICT = {restrict}" if restrict else ""
    return (
        f"CREATE OPERATOR {operator} (LEFTARG = {left}, RIGHTARG = {right}, "
        f"FUNCTION = {function}{clause})"
    )


FUNCTIONS = ("buma_pb_json_text(text)", "buma_source_rowids(text, text[])")

# Collection ids of the corrected export, checked against _collections.
COLLECTION_IDS = {
    "buma_pdv": "l0wk3fy5xnw6et7",
    "buma_sim": "kr6zwb9oabrvxiq",
    "buma_customer_user": "78g31fn92mr18j5",
    "buma_caisse_transactions": "6y0f0rx6t5eauo1",
}


def archived_rowids(collection: str, alias: str, ids: str | None = None) -> str:
    """Join of the source rowids of ``alias``: of its collection, or of ``ids``.

    The small collections (about 5,200 pdv and sims in the real backup) are
    read whole: an id array built by a subquery would be an InitPlan, which
    keeps PostgreSQL from running buma_return_list_view in parallel. The
    caisse expenses (15,797 of 317,066 transactions) are looked up by id.
    """
    selected = f", ARRAY({ids})" if ids else ""
    return (
        f"LEFT JOIN buma_source_rowids('{COLLECTION_IDS[collection]}'{selected}) "
        f"AS {alias}_rowid ON {alias}_rowid.record_id = {alias}.id"
    )


def rowid_order(alias: str) -> str:
    """SQLite rowid order: imported rowid, then creation order (see module doc)."""
    return f"{alias}_rowid.source_rowid NULLS LAST, {alias}.created, {alias}.id"


def sqlite_integer(expression: str) -> str:
    return f"CAST({expression} AS {INTEGER_DOMAIN})"


PDV_ROWIDS = archived_rowids("buma_pdv", "p")
CUSTOMER_USER_ROWIDS = archived_rowids("buma_customer_user", "buma_customer_user")


NUMBER = 'CAST(sim.number AS TEXT) COLLATE "C"'
# sim_numbers of the three customer/return views: p in rowid order, its sims
# through the covering index (pdv, number).
SIM_LIST = (
    "STRING_AGG(CAST(sim.number AS TEXT), ',' ORDER BY " + rowid_order("p") + ", " + NUMBER + ")"
)
AGENT_SIM = "COALESCE(MAX(CAST(number AS TEXT) COLLATE \"C\"), '')"

# The four customer/return views group by (or select DISTINCT) pdv_sim and
# agent_sim, which json cannot do (it has no equality): they stay text there,
# with the fallback '', and an outer projection serves them as buma_pb_json.
WRAPPED = {
    "buma_customer_list_view": (
        "id created updated solde due kind agent customer_no firstname lastname name address "
        "contact email photo status pdv complete_name pdv_sim agent_sim"
    ),
    "buma_customer_list_view_with_users": (
        "id created updated solde due kind agent customer_no firstname lastname name address "
        "contact email status photo pdv complete_name pdv_sim agent_sim assigned_users"
    ),
    "buma_customer_mobile_list_view": (
        "id created updated solde due kind agent customer_no uvUser firstname lastname name "
        "address contact email status photo isNafama pdv complete_name pdv_sim agent_sim "
        "agent_sim_network pdv_sim_network globalUpdated"
    ),
    "buma_return_list_view": (
        "id amount payed status note designation complete_name number customer_id created "
        "pdv_name pdv_sim agent_sim return_date"
    ),
}
JSON_COLUMNS = ("pdv_sim", "agent_sim")


def wrapper(name: str) -> tuple[str, str]:
    """(head, tail) around the query of a WRAPPED view (its final ``;`` kept)."""
    columns = []
    for column in WRAPPED[name].split():
        quoted = f'w."{column}"' if column != column.lower() else f"w.{column}"
        columns.append(
            f"buma_pb_json_text({quoted}) AS {column}" if column in JSON_COLUMNS else quoted
        )
    return "SELECT " + ", ".join(columns) + "\nFROM (\n", "\n) AS w;"


# SimNumbers reads its sims through the covering index (pdv, network, number,
# updated); DISTINCT network keeps each network's first occurrence.
MOBILE = (
    (
        "WITH SimNumbers AS (",
        "WITH PdvOrder AS (\n"
        "    SELECT p.id, p.client, ROW_NUMBER() OVER (ORDER BY "
        + rowid_order("p")
        + ") AS position\n"
        "    FROM buma_pdv p\n"
        "    " + PDV_ROWIDS + "\n"
        "),\n"
        "SimNumbers AS (",
    ),
    (
        "        STRING_AGG(CAST(sim.number AS TEXT), ',') AS sim_list,\n"
        "        STRING_AGG(DISTINCT CAST(sim.network AS TEXT), ',') AS network_list,\n",
        "        STRING_AGG(CAST(sim.number AS TEXT), ',' ORDER BY p.position, "
        'CAST(sim.network AS TEXT) COLLATE "C", ' + NUMBER + ") AS sim_list,\n",
    ),
    (
        "    FROM buma_pdv p\n"
        "    LEFT JOIN buma_sim sim ON sim.pdv = p.id\n"
        "    GROUP BY p.client\n"
        "),\n"
        "AgentSimNumbers AS (",
        "    FROM PdvOrder p\n"
        "    LEFT JOIN buma_sim sim ON sim.pdv = p.id\n"
        "    GROUP BY p.client\n"
        "),\n"
        "SimNetworks AS (\n"
        "    SELECT client,\n"
        "        STRING_AGG(network, ',' ORDER BY first_position, "
        'network COLLATE "C") AS network_list\n'
        "    FROM (\n"
        "        SELECT p.client, CAST(sim.network AS TEXT) AS network, "
        "MIN(p.position) AS first_position\n"
        "        FROM PdvOrder p\n"
        "        JOIN buma_sim sim ON sim.pdv = p.id\n"
        "        GROUP BY p.client, CAST(sim.network AS TEXT)\n"
        "    ) AS first_networks\n"
        "    GROUP BY client\n"
        "),\n"
        "AgentSimNumbers AS (",
    ),
    (
        "        COALESCE(MAX(CAST(number AS TEXT)), '') AS agent_sim_number,\n"
        '        BOOL_OR("isNafama") AS "isNafama",\n',
        "        " + AGENT_SIM + " AS agent_sim_number,\n"
        '        (ARRAY_AGG("isNafama" ORDER BY CAST(network AS TEXT) COLLATE "C" DESC, '
        + rowid_order("buma_sim")
        + '))[1] AS "isNafama",\n',
    ),
    (
        "    FROM buma_sim\n    WHERE agent != ''\n    GROUP BY agent\n),\nUserData AS (",
        "    FROM buma_sim\n    "
        + archived_rowids("buma_sim", "buma_sim")
        + "\n    WHERE agent != ''\n    GROUP BY agent\n),\nUserData AS (",
    ),
    (
        "        MAX(network) AS agent_sim_network\n",
        '        MAX(CAST(network AS TEXT) COLLATE "C") AS agent_sim_network\n',
    ),
    (
        "        customer,\n        user,\n        MAX(updated) AS updated\n",
        "        customer,\n"
        '        (ARRAY_AGG("user" ORDER BY updated DESC, '
        + rowid_order("buma_customer_user")
        + '))[1] AS "user",\n'
        "        MAX(updated) AS updated\n",
    ),
    (
        "    FROM buma_customer_user\n    GROUP BY customer\n),\nPdvData AS (",
        "    FROM buma_customer_user\n    "
        + CUSTOMER_USER_ROWIDS
        + "\n    GROUP BY customer\n),\nPdvData AS (",
    ),
)

PAYED_PERCENT_OLD = (
    " (SELECT (SUM(remaining) * 100) / SUM(amount) from buma_sale WHERE status = true) AS "
    '"payedPercent"'
)


def _integer(column: str) -> str:
    """A number value SQLite stores as INTEGER in its NUMERIC column."""
    return (
        f"({column} = trunc({column}) AND {column} > -9223372036854775808::float8 "
        f"AND {column} < 9223372036854775808::float8)"
    )


PAYED_PERCENT_NEW = (
    " (SELECT CASE\n"
    f"    WHEN bool_and({_integer('remaining')}) AND bool_and({_integer('amount')})\n"
    f"    THEN div(SUM(CASE WHEN {_integer('remaining')} THEN remaining::bigint END) * 100,\n"
    f"             NULLIF(SUM(CASE WHEN {_integer('amount')} THEN amount::bigint END), 0))\n"
    "         ::double precision\n"
    "    ELSE (SUM(remaining) * 100) / NULLIF(SUM(amount), 0)\n"
    '  END from buma_sale WHERE status = true) AS "payedPercent"'
)

# view -> ((old fragment, new fragment), ...): each fragment occurs exactly once.
CHANGES = {
    "buma_agent_list_view": (
        (
            "  COALESCE(si.number, '') as number,\n",
            "  buma_pb_json_text(COALESCE(si.number, '')) as number,\n",
        ),
    ),
    "buma_customer_list_view": (
        (
            "    FROM buma_pdv p\n    LEFT JOIN buma_sim sim \n",
            "    FROM buma_pdv p\n    " + PDV_ROWIDS + "\n    LEFT JOIN buma_sim sim \n",
        ),
        (
            "STRING_AGG(CAST(sim.number AS TEXT), ',') AS sim_list",
            f"{SIM_LIST} AS sim_list",
        ),
        (
            "COALESCE(MAX(CAST(number AS TEXT)), '') AS agent_sim_number",
            f"{AGENT_SIM} AS agent_sim_number",
        ),
    ),
    "buma_customer_list_view_with_users": (
        ("  FROM buma_pdv AS p\n", "  FROM buma_pdv AS p\n  " + PDV_ROWIDS + "\n"),
        (
            "  FROM buma_customer_user\n  GROUP BY customer\n) AS user_numbers",
            "  FROM buma_customer_user\n  "
            + CUSTOMER_USER_ROWIDS
            + "\n  GROUP BY customer\n) AS user_numbers",
        ),
        (
            "STRING_AGG(CAST(sim.number AS TEXT), ',') AS sim_list",
            f"{SIM_LIST} AS sim_list",
        ),
        (
            "COALESCE(MAX(CAST(number AS TEXT)), '') AS agent_sim_number",
            f"{AGENT_SIM} AS agent_sim_number",
        ),
        (
            "STRING_AGG(CAST(\"user\" AS TEXT), ',') AS user_list",
            "STRING_AGG(CAST(\"user\" AS TEXT), ',' ORDER BY "
            + rowid_order("buma_customer_user")
            + ") AS user_list",
        ),
    ),
    "buma_customer_mobile_list_view": (
        *MOBILE,
        (
            'COALESCE(asn."isNafama", false) AS "isNafama",',
            sqlite_integer('COALESCE(asn."isNafama"::integer, 0)') + ' AS "isNafama",',
        ),
        (
            "COALESCE(sn.network_list, '') AS pdv_sim_network,",
            "COALESCE(snw.network_list, '') AS pdv_sim_network,",
        ),
        (
            "LEFT JOIN SimNumbers sn ON sn.client = bc.id\n",
            "LEFT JOIN SimNumbers sn ON sn.client = bc.id\n"
            "LEFT JOIN SimNetworks snw ON snw.client = bc.id\n",
        ),
        ("    sn.network_list,\n", "    snw.network_list,\n"),
    ),
    "buma_return_list_view": (
        ("    FROM buma_pdv AS p\n", "    FROM buma_pdv AS p\n    " + PDV_ROWIDS + "\n"),
        (
            "STRING_AGG(CAST(sim.number AS TEXT), ',') AS pdv_sim",
            f"{SIM_LIST} AS pdv_sim",
        ),
        (
            "COALESCE(MAX(CAST(number AS TEXT)), '') AS agent_sim_number",
            f"{AGENT_SIM} AS agent_sim_number",
        ),
    ),
    "buma_transaction_list_view": (
        (
            "    WHEN tr.sale != '' THEN sale.amount = sale.payed\n"
            "    ELSE retour.payed\n"
            "  END) as payed,",
            "    WHEN tr.sale != '' THEN "
            + sqlite_integer("(sale.amount = sale.payed)::integer")
            + "\n    ELSE "
            + sqlite_integer("retour.payed::integer")
            + "\n  END) as payed,",
        ),
        (
            '    WHEN sale.payed = sale.amount  THEN true\n    ELSE false\n  END) AS "salePayed",',
            "    WHEN sale.payed = sale.amount  THEN "
            + sqlite_integer("1")
            + "\n    ELSE "
            + sqlite_integer("0")
            + '\n  END) AS "salePayed",',
        ),
    ),
    "buma_sale_info": (
        (
            "SELECT (ROW_NUMBER() OVER()) as id,\n",
            'SELECT CAST(ROW_NUMBER() OVER () AS TEXT) COLLATE "C" as id,\n',
        ),
        (PAYED_PERCENT_OLD, PAYED_PERCENT_NEW),
    ),
    "buma_caisse_transaction_by_categorie_list_view": (
        # The filtered rows once, then their rowids in one archive lookup.
        (
            "SELECT (ROW_NUMBER() OVER()) as id, b.amount,",
            "WITH filtered AS MATERIALIZED (\nSELECT b.id, b.amount,",
        ),
        (
            "AND b.status = true AND c.type = 'EXPENSE'",
            "AND b.status = true AND c.type = 'EXPENSE'\n)\n"
            "SELECT CAST(ROW_NUMBER() OVER (ORDER BY "
            + rowid_order("f")
            + ') AS TEXT) COLLATE "C" as id, f.amount, f.created, f."caisseName", f.label\n'
            "FROM filtered f\n"
            + archived_rowids("buma_caisse_transactions", "f", "SELECT id FROM filtered"),
        ),
    ),
    "buma_article_with_no_stock_list_view": (
        (
            "COALESCE(STRING_AGG(CAST(bs.stockage AS TEXT), ','), '') as stockages",
            "COALESCE(STRING_AGG(CAST(bs.stockage AS TEXT), ',' ORDER BY "
            "CAST(bs.stockage AS TEXT) COLLATE \"C\"), '') as stockages",
        ),
    ),
    "buma_stockage_no_manager": (
        (
            "COALESCE(STRING_AGG(CAST(bs.stockage AS TEXT), ','), '') as stockage",
            "COALESCE(STRING_AGG(CAST(bs.stockage AS TEXT), ',' ORDER BY "
            "CAST(bs.stockage AS TEXT) COLLATE \"C\"), '') as stockage",
        ),
    ),
}

# SHA-256 of each stored query before and after this migration (the query of
# 1789236842 as corrected by 1789236847 and 1789236848, i.e. the translation of
# the corrected export by scripts/generate_native_baseline.py).
QUERY_SHA256 = {
    "buma_agent_list_view": (
        "0fdf28cd0f916dce6fb08b5b4a649579adf2c885b87ad33a6a7bdc22d12bc767",
        "7568bf3daa4eca734a793f4a692c7531ce68ee5e55828e220cce897b18f4c717",
    ),
    "buma_customer_list_view": (
        "b62cbb0cf037fd1b181db5fb71189b2f766e26ebd5c1b072a373c0d07c25c715",
        "24dbab1b2dca61744ea7e27292e0cb5e444af13eb273d69112a1fe1398abe3da",
    ),
    "buma_customer_list_view_with_users": (
        "a44ba30eda0a05334df7655aa8f1a23063dc67e8eda2da41d62b9725f840c8d9",
        "b05318c0e24ffe7ee721eb7be2cd8d3549902661d3a01b0e80be5f0ae03e5319",
    ),
    "buma_customer_mobile_list_view": (
        "464885689bf9c5d9f0d30cf9d85892019e46c9f08c5e4b8ede9bb42d2ce93e65",
        "a1c9efc0cc4a602ca5e9a2f1eafa124f193b24dc39c838cfd8703b6605832503",
    ),
    "buma_return_list_view": (
        "dc5d101242df4af8f7f78b6dbe44ab04b1756bab79929218ff19a06d2c873afc",
        "9ac004bb7b97f8e8322dd570d04b4bebbcc2f71a6b6ff02e07db48c43a2b99d1",
    ),
    "buma_transaction_list_view": (
        "f6e6700591d5cd79f43eef80d8636121c0dca5112a432c692c628392a8204796",
        "db754fc03767e7da0b0d7d21fa875a199657fb766302e019bed7578878b8b424",
    ),
    "buma_sale_info": (
        "d7ae090a8d0c7c04c055e3118dd97010149dfa53b137cabbeef24aef11e279cc",
        "27c1fbac5d74adc1454bd9f888fe31292dd6f889a2c42026fa114435c8eed1bd",
    ),
    "buma_caisse_transaction_by_categorie_list_view": (
        "3c73d61679edf2179ce3ca31fbf19a629785489442d372bb2505b6ee4c2c430c",
        "eb483e4ca694d50bebb65f3942eaa85ac701453a92d16d50d3349a4cb6d1fbfa",
    ),
    "buma_article_with_no_stock_list_view": (
        "0d11d789b11fffa1c1ac8c7a394309bdf14202bd0bc592e5d7e07122e2e1d86a",
        "4829a38c8a1466bfd36367dd3cc573df79c0a97db520ecc8dfc62fff4d1011e0",
    ),
    "buma_stockage_no_manager": (
        "7770019d79ea2b30801eb427b07d94b1f8521ac8cdb9119b05023956c32bbde7",
        "3c13d0a263faca8bf9e8577ac1e6d86ad787c2825d596cabd8840b7b8eec5090",
    ),
}

# Views reading a recreated view (test reads buma_transaction_list_view):
# recreated with their stored query, unchanged.
DEPENDENTS = ("test",)


def digest(query: str) -> str:
    return hashlib.sha256(query.encode()).hexdigest()


def rewrite(name: str, query: str, forward: bool) -> str:
    """The query after (forward) or before this migration; refuses any other text."""
    before, after = QUERY_SHA256[name]
    fragments = CHANGES[name]
    if digest(query) != (before if forward else after):
        raise RuntimeError(f"{name}: unexpected stored view query; refusing to rewrite it")
    if name in WRAPPED and not forward:
        head, tail = wrapper(name)
        query = query.removeprefix(head).removesuffix(tail) + ";"
    for old, new in fragments if forward else reversed(fragments):
        source, target = (old, new) if forward else (new, old)
        if query.count(source) != 1:
            raise RuntimeError(f"{name}: view query fragment not found exactly once")
        query = query.replace(source, target)
    if name in WRAPPED and forward:
        head, tail = wrapper(name)
        if not query.endswith(";") or query.count(";") != 1:
            raise RuntimeError(f"{name}: view query does not end with its only ';'")
        query = head + query[:-1] + tail
    if digest(query) != (after if forward else before):
        raise RuntimeError(f"{name}: rewritten view query differs from the expected one")
    return query


async def _stored_query(app, name: str) -> tuple[object, dict]:
    collection = await app.find_collection(name)
    if collection.type != "view":
        raise RuntimeError(f"{name}: not a view collection")
    return collection, dict(collection.options or {})


async def _replace(app, forward: bool) -> None:
    rows = (
        await app.execute_sql(
            "SELECT id, name FROM _collections WHERE name = ANY(:names)",
            {"names": list(COLLECTION_IDS)},
        )
    ).all()
    if {row[1]: row[0] for row in rows} != COLLECTION_IDS:
        raise RuntimeError("collection ids differ from the corrected export")
    targets = {}
    for name in CHANGES:
        collection, options = await _stored_query(app, name)
        options["query"] = rewrite(name, options["query"], forward)
        targets[name] = (collection, options)
    dependents = {}
    for name in DEPENDENTS:
        collection, options = await _stored_query(app, name)
        dependents[name] = options["query"]
    # Dependents first, then the targets (none reads another); no CASCADE, so
    # an unexpected dependent view fails the migration and rolls it back.
    for name in dependents:
        await app.execute_sql(f'DROP VIEW "{name}"')
    for name in targets:
        await app.execute_sql(f'DROP VIEW "{name}"')
    for name, (collection, options) in targets.items():
        await app.execute_sql(f'CREATE VIEW "{name}" AS {options["query"]}')
        await app.execute_sql(
            "UPDATE _collections SET options = CAST(:options AS jsonb) WHERE id = :id",
            {"options": json.dumps(options), "id": collection.id},
        )
    for name, query in dependents.items():
        await app.execute_sql(f'CREATE VIEW "{name}" AS {query}')


async def up(app):
    version = (
        await app.execute_sql("SELECT current_setting('server_version_num')::int")
    ).scalar_one()
    if version < MIN_SERVER_VERSION:
        raise RuntimeError("buma_pb_json_text needs PostgreSQL 16 (pg_input_is_valid)")
    for domain, base in DOMAINS:
        await app.execute_sql(f"CREATE DOMAIN {domain} AS {base}")
    for ddl in (PB_JSON_TEXT, SOURCE_ROWIDS, *COMPARE_FUNCTIONS):
        await app.execute_sql(ddl)
    for operator, function, left, right, ddl in COMPARISON_OPERATORS:
        await app.execute_sql(ddl)
        await app.execute_sql(operator_ddl(operator, function, left, right))
    await _replace(app, True)


async def down(app):
    await _replace(app, False)
    # No CASCADE: an object that still uses a domain fails the migration.
    for operator, function, left, right, _ in reversed(COMPARISON_OPERATORS):
        await app.execute_sql(f"DROP OPERATOR {operator} ({left}, {right})")
        await app.execute_sql(f"DROP FUNCTION {function}({left}, {right})")
    for signature in reversed(COMPARE_SIGNATURES):
        await app.execute_sql(f"DROP FUNCTION {signature}")
    for function in FUNCTIONS:
        await app.execute_sql(f"DROP FUNCTION {function}")
    for domain, _ in reversed(DOMAINS):
        await app.execute_sql(f"DROP DOMAIN {domain}")
