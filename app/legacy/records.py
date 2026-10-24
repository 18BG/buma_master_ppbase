"""Internal legacy DAO writes: schema projection without request-form validation."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from typing import Any

from ppbase.core.id_generator import generate_id
from ppbase.models.field_multiplicity import field_is_multiple
from ppbase.services.record_service import resolve_collection
from sqlalchemy import text

from app.commerce.sequential import read


def number(value):
    try:
        result = float(value or 0)
        return result if math.isfinite(result) else 0
    except (TypeError, ValueError):
        return 0


def js_add(left, right):
    if isinstance(left, str) or isinstance(right, str):
        return str(left) + str(right)
    return left + right


def prepared(field, value):
    kind = field["type"]
    # Shared PPBase rule (D-0019): a relation with maxSelect null is multiple,
    # like PocketBase 0.22.34 RelationOptions.IsMultiple (schema_field.go l.715).
    if field_is_multiple(field):
        return value if isinstance(value, list) else ([value] if value else [])
    if kind == "number":
        return number(value)
    if kind == "bool":
        return value in (True, 1, "true", "1", "TRUE", "True")
    if kind == "date":
        if isinstance(value, datetime):
            return value
        if not value:
            return None
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=UTC)
    if kind == "json":
        return json.dumps(value)
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


async def write(engine: Any, table: str, values: dict, identifier: str | None = None):
    collection = await resolve_collection(engine, table)
    if collection is None or collection.type != "base":
        raise ValueError("Internal legacy writes require an existing base collection")
    fields = {field["name"]: field for field in collection.schema}
    columns = {key: prepared(fields[key], value) for key, value in values.items() if key in fields}
    columns["updated"] = datetime.now(UTC)
    if identifier is None:
        columns["id"] = generate_id()
        columns["created"] = datetime.now(UTC)
        names = ",".join(f'"{name}"' for name in columns)
        params = ",".join(f":{name}" for name in columns)
        statement = f'INSERT INTO "{collection.name}" ({names}) VALUES ({params})'
        identifier = columns["id"]
    else:
        assignments = ",".join(f'"{name}"=:{name}' for name in columns)
        statement = f'UPDATE "{collection.name}" SET {assignments} WHERE id=:_id'
        columns["_id"] = identifier
    async with engine.begin() as connection:
        await connection.execute(text(statement), columns)
    return await read(engine, table, identifier)


async def create(engine, table, values):
    return await write(engine, table, values)


async def save(engine, table, identifier, values):
    return await write(engine, table, values, identifier)
