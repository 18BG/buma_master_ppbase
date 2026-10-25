"""Serve from thirteen views the values PocketBase 0.22.34 serves from their SQLite views.

PocketBase reads a view column as text (``dbx.NullStringMap``,
``models/record.go`` l.48-58), the view being ``CREATE VIEW name AS SELECT *
FROM (query)`` (``daos/view.go`` l.59); a view whose id is an expression is
wrapped in ``SELECT cast(id as text) id, ...`` (``daos/collection.go``
l.398-445, ``normalizeViewQueryId``). The PostgreSQL translation (1789236842,
corrected by 1789236847 and 1789236848) served other values for the thirteen
views below. They are dropped and created again, without CASCADE (no other view
reads them); their ``_collections`` schema and rules are not touched;
``options.query`` takes the new query, guarded by its SHA-256 before and after.

Types. Every column keeps the type and the collation of the translation, so
PPBase renders filters (``=``, ``!=``, ``?=``, ``?!=``, ``~``, ``<``, ``>``),
sorts and comparisons of two columns on them as before. An aggregate computed
in SQLite's BINARY order (``COLLATE "C"``) takes the default collation back.
Only two kinds of columns change: the id of the seven ``ROW_NUMBER()`` views
(bigint -> text, as PocketBase serves it) and ``uvUser`` of
``buma_customer_mobile_list_view`` (name -> text, below).

* ``payedPercent`` (``buma_sale_info``): SQLite divides two integers with an
  integer result truncated toward zero, and in floating point when either
  operand is real (lang_expr.html, "Operators"); ``sum()`` is an integer when
  every input is an integer (lang_aggfunc.html); a NUMERIC column stores a whole
  real as an integer (datatype3.html 3); a zero divisor gives NULL (PostgreSQL
  raised an error). Still double precision.
* ``ROW_NUMBER() OVER ()`` (seven views): PostgreSQL numbered the rows in the
  order of its plan, which may change between two requests, so that the pages
  of one listing could serve a row twice and lose another. The id is now
  ``CAST(ROW_NUMBER() OVER (order) AS TEXT) COLLATE "C"`` (BINARY, like
  ``cast(id as text)``), numbered in SQLite's order:
  ``buma_caisse_transaction_by_categorie_list_view``, the scan of
  ``buma_caisse_transactions`` in rowid order (plan ``SCAN b``, lookups by id);
  ``buma_caisse_session_info``, its GROUP BY order (caisse, session);
  ``test``, its GROUP BY order (s.customer, a.id, a.designation; NULL first);
  ``buma_sale_info``, ``buma_article_list_stat_view``,
  ``buma_customer_supplier_info_view`` and ``buma_return_info_view`` have one
  row, id ``"1"``. Text ids also make getOne and ``id = "3"`` answer (they
  failed on a bigint).
* ``group_concat`` order is "arbitrary" (lang_aggfunc.html); PocketBase serves
  SQLite's: the rows of the scan of the outer table (rowid order), each joined
  table read through an automatic covering index keyed by the join column,
  then the other used columns in table order, then rowid (plan ``SEARCH ...
  USING AUTOMATIC COVERING INDEX``), the GROUP BY sorter keeping that order;
  ``DISTINCT`` keeps the first occurrence. Text compares with BINARY (memcmp,
  datatype3.html 7): ``COLLATE "C"``. ``pdv_sim`` of the four customer and
  return views, ``assigned_users`` and ``pdv_sim_network`` follow the rowids;
  ``stockages`` and ``stockage`` of the two stock views follow the stock ids
  (the list request). The text type is kept.
* A bare column of an aggregate with several ``max()`` takes the row of the
  last one (lang_select.html 2.5 leaves the choice undefined; observed on
  SQLite 3.45 and on the 0.22.34 binary): ``isNafama`` comes from the first
  sim, in rowid order, that has the greatest ``network`` (a boolean, as
  before); with one ``max()`` it is the first row holding it: ``user`` of
  ``UserData``, which the translation had left unquoted, so that PostgreSQL
  read it as ``CURRENT_USER`` and served the database role as ``uvUser``.

Not changed here, documented divergences:

* A text that is valid JSON (``agent_sim``, ``pdv_sim``, ``number`` of
  ``buma_agent_list_view``): PocketBase prepares it as a json field and serves
  a JSON number or list (``models/schema/schema_field.go`` l.312-343), PPBase
  a string (``number_served_as_text``); ``isNafama``, ``payed`` and
  ``salePayed``: PocketBase serves SQLite's integers 1/0, PPBase true/false
  (``sqlite_integer_boolean_served_as_boolean``); D-0039, acceptance requested.
* A listing without ``sort``: PPBase orders a view that has no ``created``
  field by its first column (``ORDER BY 1``); for the three numbered views
  with several rows that is now the text order of the id ("1", "10", "100",
  ..., "2"), where PocketBase serves SQLite's order (the numbering, or the
  ``ORDER BY`` of ``test``). Every row is served once over the pages;
  ``sort=id`` is PocketBase's text order.
* A number compared with a text id (``id = 3``, ``id > 3``): PocketBase
  compares the text '3' with '3.0' (TEXT affinity, datatype3.html 4.2) and
  finds nothing; PPBase binds a number for a text column and fails, as for
  the id of every base collection.
* A view by id of the two stock views: SQLite plans it without the automatic
  index and concatenates in rowid order; PPBase serves the list's order.
* ``SUM`` of reals (``totalAmount``, ``totalPayed``, ``totalNotPayed``,
  ``totalProfit``, the totals of the other numbered views, ``payedPercent``
  with a real operand): SQLite 3.43+ adds floats with Kahan-Babuska-Neumaier
  compensation (lang_aggfunc.html, sum), PostgreSQL ``SUM(float8)`` plainly,
  so the last digits may differ; PPBase stores numbers as float8, exact for
  whole numbers up to 2^53. The notation of a number may differ too:
  PocketBase serves the text its SQLite driver gives a REAL (Go's shortest
  form, ``3.50000075e+06``), PPBase the JSON encoding of the float8
  (``3500000.75``): same JSON value, D-0029 kind. The real backup has no REAL
  in these sums.

The SQLite rowid of an imported record is kept by the C11 importer in
``_buma_c11_rowids`` (scripts/c11_import.py), a table that only the import
creates and that must not exist before it. ``buma_source_rowids`` returns the
archived rowids of the given records when it exists and no row otherwise, so
the rowid order is ``source rowid NULLS LAST, created, id``: imported records
in their SQLite order, then records created afterwards (no archived rowid) in
their creation order, as SQLite gives a new record the rowid after the
greatest one. The archive must be kept after the cutover (and in every backup
and restore): without it the order falls back to ``created, id``. One lookup
per query and per table, not one function call per row: the archived rows of a
small collection (pdv, sim, customer user), or of the filtered caisse expenses.

Apply with no PPBase server running: seven views change the type of their id,
and a running server keeps asyncpg prepared statements of the old ones.
"""

from __future__ import annotations

import hashlib
import json

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
FUNCTIONS = ("buma_source_rowids(text, text[])",)

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


def text_id(order: str = "") -> str:
    """The id PocketBase serves for ROW_NUMBER() OVER (): its text, BINARY ordered."""
    window = f"ORDER BY {order}" if order else ""
    return f'CAST(ROW_NUMBER() OVER ({window}) AS TEXT) COLLATE "C" as id'


def default_collation(expression: str) -> str:
    """An expression computed in BINARY order, with the translation's collation."""
    return f'{expression} COLLATE "default"'


PDV_ROWIDS = archived_rowids("buma_pdv", "p")
CUSTOMER_USER_ROWIDS = archived_rowids("buma_customer_user", "buma_customer_user")


NUMBER = 'CAST(sim.number AS TEXT) COLLATE "C"'
# sim_numbers of the three customer/return views: p in rowid order, its sims
# through the covering index (pdv, number).
SIM_LIST = (
    "STRING_AGG(CAST(sim.number AS TEXT), ',' ORDER BY " + rowid_order("p") + ", " + NUMBER + ")"
)
AGENT_SIM = default_collation("COALESCE(MAX(CAST(number AS TEXT) COLLATE \"C\"), '')")


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
        "        "
        + default_collation('MAX(CAST(network AS TEXT) COLLATE "C")')
        + " AS agent_sim_network\n",
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
    "buma_sale_info": (
        ("SELECT (ROW_NUMBER() OVER()) as id,\n", f"SELECT {text_id()},\n"),
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
            "SELECT "
            + text_id(rowid_order("f"))
            + ', f.amount, f.created, f."caisseName", f.label\n'
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
    "buma_caisse_session_info": (
        (
            "SELECT\n    (ROW_NUMBER() OVER()) as id,\n",
            "SELECT\n    " + text_id('bct.caisse COLLATE "C", bct.session COLLATE "C"') + ",\n",
        ),
    ),
    "test": (
        (
            "SELECT\n  (ROW_NUMBER() OVER()) as id,\n",
            "SELECT\n  "
            + text_id(
                's.customer COLLATE "C", a.id COLLATE "C" NULLS FIRST, '
                'a.designation COLLATE "C" NULLS FIRST'
            )
            + ",\n",
        ),
    ),
    "buma_article_list_stat_view": ((" (ROW_NUMBER() OVER()) as id;", f" {text_id()};"),),
    "buma_customer_supplier_info_view": (
        ("  (ROW_NUMBER() OVER()) as id,\n", f"  {text_id()},\n"),
    ),
    "buma_return_info_view": (
        ("select (ROW_NUMBER() OVER()) as id, COUNT(r.id)", f"select {text_id()}, COUNT(r.id)"),
    ),
}

# SHA-256 of each stored query before and after this migration (the query of
# 1789236842 as corrected by 1789236847 and 1789236848, i.e. the translation of
# the corrected export by scripts/generate_native_baseline.py).
QUERY_SHA256 = {
    "buma_customer_list_view": (
        "b62cbb0cf037fd1b181db5fb71189b2f766e26ebd5c1b072a373c0d07c25c715",
        "ee964c72ec5fc881ce228e3b4eb877869f8f52a238c7794f9061c6895fe78fcb",
    ),
    "buma_customer_list_view_with_users": (
        "a44ba30eda0a05334df7655aa8f1a23063dc67e8eda2da41d62b9725f840c8d9",
        "de190ce0fab1a5b2860ac7fdc91e00b69695bd0e257a2d98fdd8ac66cb97b6ea",
    ),
    "buma_customer_mobile_list_view": (
        "464885689bf9c5d9f0d30cf9d85892019e46c9f08c5e4b8ede9bb42d2ce93e65",
        "c1fc7b63d7cc7777cab39bc67ab6581e63bf3540ba523cd2259fb64ae6a5bc98",
    ),
    "buma_return_list_view": (
        "dc5d101242df4af8f7f78b6dbe44ab04b1756bab79929218ff19a06d2c873afc",
        "c5824251bf957a2dc77f9f544251290257b8269147548142e4a2c3b649fad7dd",
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
    "buma_caisse_session_info": (
        "46fbd0191b3c01edaafbc6d54dd0f651febc576d9667cdb6b70971d53ef3f36f",
        "9f58f5edbe57b83d375c3d99716630ba6e2d12d8a7602038e3b38127da660ccb",
    ),
    "test": (
        "9633d68f04c32eb866dd619d82ed6da34f9e3eeb43d3727d0e18dad4300c50ef",
        "e21faf7fe83cd25db513e2ecc750f36a621827b6ecd68ed571d0fab585bc1f60",
    ),
    "buma_article_list_stat_view": (
        "4c802e6f9c48ac04e1910f69bf423d4d5091322e7284a123bd738da2020e4d76",
        "8512d73a25d946cfd9dc1f4fcafbe4134478f0b9135a6ee9d1592b205ea58afd",
    ),
    "buma_customer_supplier_info_view": (
        "dc62c307f679ca28cd70a02a3ae96d01dc721160bf037b19235d6af53caf63f2",
        "b669e165b50b51636d6ff166dbeeb70867af4c739940d27425bdc3842c97bfbe",
    ),
    "buma_return_info_view": (
        "b676651b529a01f50568fa35018aff2719683e3476617925c3f767eca9a71479",
        "875897ba6df953541826585a4338e419bf2d6d52e7505a40656abbc8e5f32c4c",
    ),
}


def digest(query: str) -> str:
    return hashlib.sha256(query.encode()).hexdigest()


def rewrite(name: str, query: str, forward: bool) -> str:
    """The query after (forward) or before this migration; refuses any other text."""
    before, after = QUERY_SHA256[name]
    if digest(query) != (before if forward else after):
        raise RuntimeError(f"{name}: unexpected stored view query; refusing to rewrite it")
    for old, new in CHANGES[name] if forward else reversed(CHANGES[name]):
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
    # None of these views reads another, and no other view reads them; no
    # CASCADE, so an unexpected dependent view fails the migration and rolls
    # it back.
    for name in targets:
        await app.execute_sql(f'DROP VIEW "{name}"')
    for name, (collection, options) in targets.items():
        await app.execute_sql(f'CREATE VIEW "{name}" AS {options["query"]}')
        await app.execute_sql(
            "UPDATE _collections SET options = CAST(:options AS jsonb) WHERE id = :id",
            {"options": json.dumps(options), "id": collection.id},
        )


async def up(app):
    await app.execute_sql(SOURCE_ROWIDS)
    await _replace(app, True)


async def down(app):
    await _replace(app, False)
    for function in FUNCTIONS:
        await app.execute_sql(f"DROP FUNCTION {function}")
