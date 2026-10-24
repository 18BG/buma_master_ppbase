"""D-0019, BUMA part: store the three ``maxSelect: null`` relations as arrays.

PocketBase 0.22.34 treats a relation whose ``options.maxSelect`` is ``null`` as
multiple (``models/schema/schema_field.go`` l.713-717, ``IsMultiple``) and
stores it in a ``JSON DEFAULT '[]' NOT NULL`` column (l.159-161). The corrected
export has exactly three such fields: ``buma_customer.pdv``,
``buma_pilotage_ticket.tag`` and ``buma_wallet.managers``. PPBase before the
D-0019 correction read ``null`` as 1 and created them as scalar
``VARCHAR(15) NOT NULL DEFAULT ''`` columns with a partial btree index. The
corrected PPBase (``ppbase.models.field_multiplicity``) creates them as
``TEXT[] NOT NULL DEFAULT '{}'`` with a GIN index ``idx_<table>_<field>``.

``up`` converts a scalar column of a database migrated before the correction
like PocketBase converts a relation that becomes multiple
(``daos/record_table_sync.go`` l.217-240, also its migration
``1679943780_normalize_single_multiple_values.go`` l.43-65):

* ``''`` -> ``{}`` (``COALESCE(col, '') = ''`` -> ``'[]'``);
* ``'[]'``, the JSON empty list, -> ``{}`` (kept as the empty array; the old
  PPBase also wrote ``str([])`` as ``'[]'``);
* any other value without ``[`` -> the one-element array ``{value}``, unchanged,
  commas included (``json_array(col)``; ``list.ToUniqueStringSlice`` only
  decodes strings that contain ``[``).

Every other value that contains ``[`` is refused: PocketBase would decode it as
a JSON list (or wrap it when invalid), while the old PPBase stored it as one
scalar string (``str(list)`` gives ``"['id']"``), so no conversion is lossless.
A ``NULL`` cannot occur (the column must be ``NOT NULL``). Any other physical
shape (another type, a nullable column, a view using the column) is refused too.
A refusal raises before any change and the runner rolls the migration back.

A column that already has the corrected shape (database created after the
PPBase correction) is left untouched, so ``up`` has no effect there. The
``_collections`` metadata is never written: ``maxSelect`` stays ``null``.
The 188 single relation columns are not touched: they stay ``VARCHAR(15)`` on a
migrated database and are ``TEXT`` on a new one (no client-visible difference).

``down`` restores the pre-correction scalar column and its partial btree index
only when every array has at most one element and that element is not empty,
has at most 15 characters and contains no ``[`` (so that ``up`` gives back the
same array); otherwise it raises and the transaction rolls back. ``'[]'``
values converted by ``up`` come back as ``''`` (PocketBase multiple -> single:
``COALESCE(col, '[]') = '[]'`` -> ``''``).
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

COLUMNS = (
    ("buma_customer", "pdv"),
    ("buma_pilotage_ticket", "tag"),
    ("buma_wallet", "managers"),
)
ARRAY_TYPE = "text[]"
ARRAY_DEFAULT = "'{}'::text[]"
LEGACY_TYPE = "character varying(15)"
LEGACY_LENGTH = 15

_COLUMN_SQL = """
SELECT pg_catalog.format_type(attribute.atttypid, attribute.atttypmod) AS type,
       attribute.attnotnull AS not_null,
       pg_catalog.pg_get_expr(definition.adbin, definition.adrelid) AS column_default
FROM pg_catalog.pg_attribute AS attribute
JOIN pg_catalog.pg_class AS relation ON relation.oid = attribute.attrelid
JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = relation.relnamespace
LEFT JOIN pg_catalog.pg_attrdef AS definition
  ON definition.adrelid = attribute.attrelid AND definition.adnum = attribute.attnum
WHERE namespace.nspname = 'public' AND relation.relname = :table
  AND relation.relkind = 'r' AND attribute.attname = :column
  AND attribute.attnum > 0 AND NOT attribute.attisdropped
"""

_INDEX_SQL = """
SELECT owner_table.relname AS owner_table, access_method.amname AS access_method
FROM pg_catalog.pg_class AS index_relation
JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = index_relation.relnamespace
JOIN pg_catalog.pg_index AS index_metadata ON index_metadata.indexrelid = index_relation.oid
JOIN pg_catalog.pg_class AS owner_table ON owner_table.oid = index_metadata.indrelid
JOIN pg_catalog.pg_am AS access_method ON access_method.oid = index_relation.relam
WHERE namespace.nspname = 'public' AND index_relation.relname = :name
"""

_DEPENDENT_VIEWS_SQL = """
SELECT DISTINCT dependent.relname
FROM pg_catalog.pg_depend AS dependency
JOIN pg_catalog.pg_rewrite AS rewrite ON rewrite.oid = dependency.objid
JOIN pg_catalog.pg_class AS dependent ON dependent.oid = rewrite.ev_class
JOIN pg_catalog.pg_class AS owner_table ON owner_table.oid = dependency.refobjid
JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = owner_table.relnamespace
JOIN pg_catalog.pg_attribute AS attribute
  ON attribute.attrelid = owner_table.oid AND attribute.attnum = dependency.refobjsubid
WHERE dependency.classid = 'pg_catalog.pg_rewrite'::pg_catalog.regclass
  AND dependency.refclassid = 'pg_catalog.pg_class'::pg_catalog.regclass
  AND namespace.nspname = 'public' AND owner_table.relname = :table
  AND attribute.attname = :column AND dependent.oid <> owner_table.oid
ORDER BY 1
"""


class RelationColumnError(RuntimeError):
    """A column state or value that this migration cannot convert losslessly."""


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def index_name(table: str, column: str) -> str:
    """PPBase managed field index name (``schema_manager._field_indexes``)."""
    name = f"idx_{table}_{column}"
    if len(name.encode()) > 63:  # PPBase would hash it; none of the three does.
        raise RelationColumnError(f"{name} exceeds the PostgreSQL identifier limit")
    return name


def array_index_statement(table: str, column: str) -> str:
    """Same statement as the corrected PPBase for a multiple relation."""
    name = index_name(table, column)
    return f'CREATE INDEX IF NOT EXISTS "{name}" ON "public"."{table}" USING GIN ("{column}")'


def scalar_index_statement(table: str, column: str) -> str:
    """Same statement as PPBase before the correction for a single relation."""
    name = index_name(table, column)
    return (
        f'CREATE INDEX IF NOT EXISTS "{name}" '
        f'ON "public"."{table}" ("{column}") WHERE "{column}" != \'\''
    )


def up_value_counts_sql(table: str, column: str) -> str:
    """Aggregate counts of the scalar values; ``refused`` must be zero."""
    q = _quote(column)
    return (
        f"SELECT count(*) AS total, "
        f"count(*) FILTER (WHERE {q} = '') AS empty, "
        f"count(*) FILTER (WHERE {q} = '[]') AS json_empty, "
        f"count(*) FILTER (WHERE strpos({q}, '[') > 0 AND {q} <> '[]') AS refused, "
        f"count(*) FILTER (WHERE {q} IS NULL) AS nulls "
        f"FROM {_quote(table)}"
    )


def down_value_counts_sql(table: str, column: str) -> str:
    """Aggregate counts of the arrays; ``multiple`` and ``refused`` must be zero."""
    q = _quote(column)
    last = f"{q}[pg_catalog.array_upper({q}, 1)]"
    return (
        f"SELECT count(*) AS total, "
        f"count(*) FILTER (WHERE pg_catalog.cardinality({q}) = 0) AS empty, "
        f"count(*) FILTER (WHERE pg_catalog.array_ndims({q}) > 1 "
        f"OR pg_catalog.cardinality({q}) > 1) AS multiple, "
        f"count(*) FILTER (WHERE pg_catalog.cardinality({q}) = 1 AND ({last} IS NULL "
        f"OR {last} = '' OR pg_catalog.char_length({last}) > {LEGACY_LENGTH} "
        f"OR strpos({last}, '[') > 0)) AS refused, "
        f"count(*) FILTER (WHERE {q} IS NULL) AS nulls "
        f"FROM {_quote(table)}"
    )


def _multiplicity_rule():
    try:
        from ppbase.models.field_multiplicity import field_is_multiple
    except ImportError as exc:  # PPBase before the D-0019 correction
        raise RelationColumnError(
            "1789236850 requires the PPBase relation multiplicity rule "
            "(ppbase.models.field_multiplicity, D-0019 correction)"
        ) from exc
    return field_is_multiple


async def _check_metadata(app) -> None:
    """The stored definitions must still describe multiple relations."""
    field_is_multiple = _multiplicity_rule()
    for table, column in COLUMNS:
        collection = await app.find_collection(table)
        field = next(
            (item for item in collection.schema or [] if item.get("name") == column),
            None,
        )
        if (
            collection.type != "base"
            or field is None
            or field.get("type") != "relation"
            or not field_is_multiple(field)
        ):
            raise RelationColumnError(
                f"{table}.{column} is not a multiple relation in _collections"
            )


async def _column(app, table: str, column: str) -> dict:
    result = await app.execute_sql(_COLUMN_SQL, {"table": table, "column": column})
    row = result.mappings().first()
    if row is None:
        raise RelationColumnError(f"{table}.{column} has no physical column")
    return dict(row)


def _is_array_shape(table: str, column: str, state: dict) -> bool:
    """True for the corrected shape, False for a convertible scalar column."""
    kind = state["type"]
    if kind == ARRAY_TYPE:
        if not state["not_null"] or state["column_default"] != ARRAY_DEFAULT:
            raise RelationColumnError(
                f"{table}.{column} is {kind} but not NOT NULL DEFAULT {ARRAY_DEFAULT}"
            )
        return True
    if kind == LEGACY_TYPE:
        if not state["not_null"]:
            raise RelationColumnError(f"{table}.{column} is a nullable {kind} column")
        return False
    raise RelationColumnError(f"{table}.{column} has the unexpected type {kind}")


async def _counts(app, sql: str) -> dict:
    return dict((await app.execute_sql(sql)).mappings().one())


async def _lock(app, tables: list[str]) -> None:
    # Writes committed before the lock are seen by the checks below; later
    # writes wait for this transaction (the checks cannot be raced).
    names = ", ".join(_quote(table) for table in sorted(tables))
    await app.execute_sql(f"LOCK TABLE {names} IN ACCESS EXCLUSIVE MODE")


async def _refuse_dependent_views(app, table: str, column: str) -> None:
    result = await app.execute_sql(_DEPENDENT_VIEWS_SQL, {"table": table, "column": column})
    views = [str(name) for name in result.scalars().all()]
    if views:
        raise RelationColumnError(
            f"{table}.{column} is used by views {views}: they would have to be recreated"
        )


async def _drop_managed_index(app, table: str, column: str) -> None:
    name = index_name(table, column)
    found = (await app.execute_sql(_INDEX_SQL, {"name": name})).mappings().first()
    if found is None:
        return
    if found["owner_table"] != table:
        raise RelationColumnError(f"index {name} belongs to {found['owner_table']}, not {table}")
    await app.execute_sql(f'DROP INDEX "public".{_quote(name)}')


async def _check_index(app, table: str, column: str, method: str) -> None:
    name = index_name(table, column)
    found = (await app.execute_sql(_INDEX_SQL, {"name": name})).mappings().first()
    if found is None or found["owner_table"] != table or found["access_method"] != method:
        raise RelationColumnError(f"index {name} was not created as {method} on {table}")


async def _pending(app, *, to_array: bool) -> list[tuple[str, str]]:
    pending = []
    for table, column in COLUMNS:
        is_array = _is_array_shape(table, column, await _column(app, table, column))
        if is_array != to_array:
            pending.append((table, column))
    return pending


async def up(app):
    await _check_metadata(app)
    pending = await _pending(app, to_array=True)
    if not pending:
        return  # Created after the PPBase correction: nothing to convert.
    await _lock(app, [table for table, _ in pending])
    pending = await _pending(app, to_array=True)
    for table, column in pending:
        await _refuse_dependent_views(app, table, column)
        counts = await _counts(app, up_value_counts_sql(table, column))
        if counts["refused"] or counts["nulls"]:
            raise RelationColumnError(
                f"{table}.{column}: {counts['refused']} value(s) contain '[' (other than "
                f"'[]') and {counts['nulls']} are NULL; they have no lossless array form. "
                f"List them with: SELECT id FROM {_quote(table)} WHERE "
                f"strpos({_quote(column)}, '[') > 0 AND {_quote(column)} <> '[]'"
            )
        logger.info(
            "1789236850 %s.%s: %d rows, %d empty, %d '[]', %d one-element arrays",
            table,
            column,
            counts["total"],
            counts["empty"],
            counts["json_empty"],
            counts["total"] - counts["empty"] - counts["json_empty"],
        )
    for table, column in pending:
        t, c = _quote(table), _quote(column)
        await _drop_managed_index(app, table, column)
        await app.execute_sql(f"ALTER TABLE {t} ALTER COLUMN {c} DROP DEFAULT")
        await app.execute_sql(
            f"ALTER TABLE {t} ALTER COLUMN {c} TYPE TEXT[] USING (CASE "
            f"WHEN {c} IN ('', '[]') THEN ARRAY[]::TEXT[] ELSE ARRAY[{c}::TEXT] END)"
        )
        await app.execute_sql(f"ALTER TABLE {t} ALTER COLUMN {c} SET DEFAULT '{{}}'")
        await app.execute_sql(f"ALTER TABLE {t} ALTER COLUMN {c} SET NOT NULL")
        await app.execute_sql(array_index_statement(table, column))
        await _check_index(app, table, column, "gin")
    for table, column in pending:
        if not _is_array_shape(table, column, await _column(app, table, column)):
            raise RelationColumnError(f"{table}.{column} was not converted")


async def down(app):
    await _check_metadata(app)
    pending = await _pending(app, to_array=False)
    if not pending:
        return
    await _lock(app, [table for table, _ in pending])
    pending = await _pending(app, to_array=False)
    for table, column in pending:
        await _refuse_dependent_views(app, table, column)
        counts = await _counts(app, down_value_counts_sql(table, column))
        if counts["multiple"] or counts["refused"] or counts["nulls"]:
            raise RelationColumnError(
                f"{table}.{column}: {counts['multiple']} array(s) have more than one "
                f"element and {counts['refused']} element(s) are empty, NULL, longer than "
                f"{LEGACY_LENGTH} characters or contain '[' ({counts['nulls']} NULL "
                "arrays); a scalar VARCHAR(15) column cannot keep them"
            )
    for table, column in pending:
        t, c = _quote(table), _quote(column)
        await _drop_managed_index(app, table, column)
        await app.execute_sql(f"ALTER TABLE {t} ALTER COLUMN {c} DROP DEFAULT")
        await app.execute_sql(
            f"ALTER TABLE {t} ALTER COLUMN {c} TYPE VARCHAR({LEGACY_LENGTH}) "
            f"USING (COALESCE({c}[pg_catalog.array_upper({c}, 1)], ''))"
        )
        await app.execute_sql(f"ALTER TABLE {t} ALTER COLUMN {c} SET DEFAULT ''")
        await app.execute_sql(f"ALTER TABLE {t} ALTER COLUMN {c} SET NOT NULL")
        await app.execute_sql(scalar_index_statement(table, column))
        await _check_index(app, table, column, "btree")
    for table, column in pending:
        if _is_array_shape(table, column, await _column(app, table, column)):
            raise RelationColumnError(f"{table}.{column} was not restored")
