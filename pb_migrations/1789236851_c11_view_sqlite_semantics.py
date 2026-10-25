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
  returns jsonb; the fallback ``''`` is the JSON string ``""``.
* SQLite booleans are integers (datatype3.html 2.1): ``isNafama``, ``payed``
  and ``salePayed`` are the integers 1/0, not booleans.
* ``payedPercent``: SQLite divides two integers with an integer result
  truncated toward zero, and floating point when either operand is real
  (lang_expr.html, "Operators"); ``sum()`` is an integer when every input is
  an integer (lang_aggfunc.html); a NUMERIC column stores a whole real as an
  integer (datatype3.html 3); a zero divisor gives NULL (PostgreSQL raises).
* ``ROW_NUMBER() OVER ()`` numbers the rows of SQLite's scan, the rowid order of
  ``buma_caisse_transactions`` (plan ``SCAN b``, lookups by id); PostgreSQL
  renumbered them on each query. PocketBase also wraps a view whose id is an
  expression in ``SELECT cast(id as text) id, ...`` (``daos/collection.go``
  l.398-445, ``normalizeViewQueryId``): the id is a string, sorted as text
  and found by its text; PPBase served a bigint (and its view by id failed).
* ``group_concat`` order is "arbitrary" (lang_aggfunc.html); PocketBase serves
  SQLite's: the rows of the scan of the outer table (rowid order), each joined
  table read through an automatic covering index keyed by the join column,
  then the other used columns in table order, then rowid (plan ``SEARCH ...
  USING AUTOMATIC COVERING INDEX``), the GROUP BY sorter keeping that order;
  ``DISTINCT`` keeps the first occurrence. Text compares with BINARY
  (memcmp, datatype3.html 7): ``COLLATE "C"``.
* A bare column of an aggregate with several ``max()`` takes the row of the
  last one (lang_select.html 2.5 leaves the choice undefined; observed on
  SQLite 3.45 and on the 0.22.34 binary): ``isNafama`` comes from the first
  sim, in rowid order, that has the greatest ``network``; with one ``max()``
  it is the first row holding it (``user`` of ``UserData``, which the
  translation had left unquoted: PostgreSQL read it as ``CURRENT_USER``).

The SQLite rowid of an imported record is kept by the C11 importer in
``_buma_c11_rowids`` (scripts/c11_import.py), a table that only the import
creates and that must not exist before it. ``buma_source_rowid`` reads it when
present and returns NULL otherwise, so the rowid order is ``source rowid NULLS
LAST, created, id``: imported records in their SQLite order, then records
created afterwards in their creation order, as SQLite gives a new record the
rowid after the greatest one.

Requires PostgreSQL 16 (``pg_input_is_valid``). Apply with no PPBase server
running, as the column types of three views change.
"""

from __future__ import annotations

import hashlib
import json

MIN_SERVER_VERSION = 160000

PB_JSON_TEXT = """
CREATE FUNCTION buma_pb_json_text(value text) RETURNS jsonb
LANGUAGE plpgsql STABLE STRICT PARALLEL SAFE AS $$
BEGIN
    -- PocketBase 0.22.34 json PrepareValue of a string (schema_field.go l.312-343).
    IF value IN ('null', 'true', 'false') THEN
        RETURN value::jsonb;
    END IF;
    IF left(value, 1) IN ('0', '1', '2', '3', '4', '5', '6', '7', '8', '9', '-', '"', '[', '{')
       AND pg_input_is_valid(value, 'jsonb') THEN
        RETURN value::jsonb;
    END IF;
    RETURN to_jsonb(value);
END
$$
"""

SOURCE_ROWID = """
CREATE FUNCTION buma_source_rowid(p_collection text, p_record text) RETURNS bigint
LANGUAGE plpgsql STABLE STRICT PARALLEL SAFE AS $$
BEGIN
    -- The archive of the C11 import, absent before it (never created here).
    IF to_regclass('public._buma_c11_rowids') IS NULL THEN
        RETURN NULL;
    END IF;
    RETURN (SELECT archive.source_rowid FROM public._buma_c11_rowids AS archive
            WHERE archive.collection_id = p_collection AND archive.record_id = p_record);
END
$$
"""

FUNCTIONS = ("buma_pb_json_text(text)", "buma_source_rowid(text, text)")

# Collection ids of the corrected export, checked against _collections.
COLLECTION_IDS = {
    "buma_pdv": "l0wk3fy5xnw6et7",
    "buma_sim": "kr6zwb9oabrvxiq",
    "buma_customer_user": "78g31fn92mr18j5",
    "buma_caisse_transactions": "6y0f0rx6t5eauo1",
}


def rowid_order(collection: str, alias: str = "") -> str:
    """SQLite rowid order: imported rowid, then creation order (see module doc)."""
    prefix = alias + "." if alias else ""
    return (
        f"buma_source_rowid('{COLLECTION_IDS[collection]}', {prefix}id) NULLS LAST, "
        f"{prefix}created, {prefix}id"
    )


JSON_EMPTY = "'\"\"'::jsonb"
NUMBER = 'CAST(sim.number AS TEXT) COLLATE "C"'
# sim_numbers of the three customer/return views: p in rowid order, its sims
# through the covering index (pdv, number).
SIM_LIST = (
    "buma_pb_json_text(STRING_AGG(CAST(sim.number AS TEXT), ',' ORDER BY "
    + rowid_order("buma_pdv", "p")
    + ", "
    + NUMBER
    + "))"
)
AGENT_SIM = "buma_pb_json_text(COALESCE(MAX(CAST(number AS TEXT) COLLATE \"C\"), ''))"

# SimNumbers reads its sims through the covering index (pdv, network, number,
# updated); DISTINCT network keeps each network's first occurrence.
MOBILE = (
    (
        "WITH SimNumbers AS (",
        "WITH PdvOrder AS (\n"
        "    SELECT id, client, ROW_NUMBER() OVER (ORDER BY "
        + rowid_order("buma_pdv")
        + ") AS position\n"
        "    FROM buma_pdv\n"
        "),\n"
        "SimNumbers AS (",
    ),
    (
        "        STRING_AGG(CAST(sim.number AS TEXT), ',') AS sim_list,\n"
        "        STRING_AGG(DISTINCT CAST(sim.network AS TEXT), ',') AS network_list,\n",
        "        buma_pb_json_text(STRING_AGG(CAST(sim.number AS TEXT), ',' ORDER BY p.position, "
        'CAST(sim.network AS TEXT) COLLATE "C", ' + NUMBER + ")) AS sim_list,\n",
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
            "COALESCE(sim_numbers.sim_list, '') AS pdv_sim,",
            f"COALESCE(sim_numbers.sim_list, {JSON_EMPTY}) AS pdv_sim,",
        ),
        (
            "COALESCE(agent_sim_numbers.agent_sim_number, '') AS agent_sim\n",
            f"COALESCE(agent_sim_numbers.agent_sim_number, {JSON_EMPTY}) AS agent_sim\n",
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
        (
            "COALESCE(sim_numbers.sim_list, '')            AS pdv_sim,",
            f"COALESCE(sim_numbers.sim_list, {JSON_EMPTY})            AS pdv_sim,",
        ),
        (
            "COALESCE(agent_sim_numbers.agent_sim_number, '') AS agent_sim,",
            f"COALESCE(agent_sim_numbers.agent_sim_number, {JSON_EMPTY}) AS agent_sim,",
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
            'COALESCE(asn."isNafama"::integer, 0) AS "isNafama",',
        ),
        (
            "COALESCE(sn.sim_list, '') AS pdv_sim,",
            f"COALESCE(sn.sim_list, {JSON_EMPTY}) AS pdv_sim,",
        ),
        (
            "COALESCE(asn.agent_sim_number, '') AS agent_sim,",
            f"COALESCE(asn.agent_sim_number, {JSON_EMPTY}) AS agent_sim,",
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
        (
            "COALESCE(sim_numbers.pdv_sim, '') AS pdv_sim,",
            f"COALESCE(sim_numbers.pdv_sim, {JSON_EMPTY}) AS pdv_sim,",
        ),
        (
            "COALESCE(agent_sim_numbers.agent_sim_number, '') AS agent_sim,",
            f"COALESCE(agent_sim_numbers.agent_sim_number, {JSON_EMPTY}) AS agent_sim,",
        ),
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
            "    WHEN tr.sale != '' THEN (sale.amount = sale.payed)::integer\n"
            "    ELSE retour.payed::integer\n"
            "  END) as payed,",
        ),
        (
            '    WHEN sale.payed = sale.amount  THEN true\n    ELSE false\n  END) AS "salePayed",',
            '    WHEN sale.payed = sale.amount  THEN 1\n    ELSE 0\n  END) AS "salePayed",',
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
        (
            "SELECT (ROW_NUMBER() OVER()) as id,",
            "SELECT CAST(ROW_NUMBER() OVER (ORDER BY "
            + rowid_order("buma_caisse_transactions", "b")
            + ') AS TEXT) COLLATE "C" as id,',
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
        "4aca8ec94ccded727d3f6460d3f549be8569e28e1f1f9a9af7fbc0998c654730",
    ),
    "buma_customer_list_view_with_users": (
        "a44ba30eda0a05334df7655aa8f1a23063dc67e8eda2da41d62b9725f840c8d9",
        "5a11eb4a6390d9b3d6cab4d74fea087c3dcfd81d0a02db99760784225d0b4068",
    ),
    "buma_customer_mobile_list_view": (
        "464885689bf9c5d9f0d30cf9d85892019e46c9f08c5e4b8ede9bb42d2ce93e65",
        "5f081d20e298ba72cf2ec55f148b78158c3165abb954cc2ce03c2bb6624e63e2",
    ),
    "buma_return_list_view": (
        "dc5d101242df4af8f7f78b6dbe44ab04b1756bab79929218ff19a06d2c873afc",
        "07bc8c372f31281ca1947b7260f79c8997b16c2e21b9818b267a8b50f3385853",
    ),
    "buma_transaction_list_view": (
        "f6e6700591d5cd79f43eef80d8636121c0dca5112a432c692c628392a8204796",
        "ffd15c4760c4d8fc415866cfc778a38c0c558ae3971d0474222400cb436a76fa",
    ),
    "buma_sale_info": (
        "d7ae090a8d0c7c04c055e3118dd97010149dfa53b137cabbeef24aef11e279cc",
        "27c1fbac5d74adc1454bd9f888fe31292dd6f889a2c42026fa114435c8eed1bd",
    ),
    "buma_caisse_transaction_by_categorie_list_view": (
        "3c73d61679edf2179ce3ca31fbf19a629785489442d372bb2505b6ee4c2c430c",
        "aaef2acb9dace89a38e84bf3120cd595fe4fd3ae17eb0883d188ff4dddfb7c6c",
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
    for old, new in fragments if forward else reversed(fragments):
        source, target = (old, new) if forward else (new, old)
        if query.count(source) != 1:
            raise RuntimeError(f"{name}: view query fragment not found exactly once")
        query = query.replace(source, target)
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
    await app.execute_sql(PB_JSON_TEXT)
    await app.execute_sql(SOURCE_ROWID)
    await _replace(app, True)


async def down(app):
    await _replace(app, False)
    for function in FUNCTIONS:
        await app.execute_sql(f"DROP FUNCTION {function}")
