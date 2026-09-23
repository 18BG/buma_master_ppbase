"""C10 SQL handlers, with the source access rules and collection operations."""

from __future__ import annotations

import json
import secrets
from typing import Any

from fastapi import Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from ppbase.db.engine import get_engine
from ppbase.models.collection import CollectionUpdate
from ppbase.services import collection_service
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker


class EmptyDynamicModel(ValueError):
    """PocketBase cannot instantiate the source's empty DynamicModel."""


def _shape(rows: list[dict[str, Any]], fields: Any) -> list[dict[str, Any]]:
    if not isinstance(fields, dict) or not fields:
        raise EmptyDynamicModel("DynamicModel fields are required")
    result = []
    for row in rows:
        shaped = dict(fields)
        for index, (name, value) in enumerate(row.items()):
            if name not in fields:
                continue
            default = fields[name]
            prefix = f'sql: Scan error on column index {index}, name "{name}": '
            if isinstance(default, bool):
                if value is None:
                    reason = "sql/driver: couldn't convert <nil> (<nil>) into type bool"
                elif isinstance(value, str):
                    if value in ("1", "t", "T", "TRUE", "true", "True"):
                        shaped[name] = True
                        continue
                    if value in ("0", "f", "F", "FALSE", "false", "False"):
                        shaped[name] = False
                        continue
                    reason = f"sql/driver: couldn't convert {json.dumps(value)} into type bool"
                elif value in (0, 1):
                    shaped[name] = bool(value)
                    continue
                else:
                    reason = f"sql/driver: couldn't convert {value} into type bool"
                raise RuntimeError(prefix + reason)
            if isinstance(default, (int, float)):
                if value is None:
                    raise RuntimeError(prefix + "converting NULL to float64 is unsupported")
                try:
                    shaped[name] = float(value)
                except (ValueError, TypeError) as exc:
                    raise RuntimeError(
                        prefix + "converting driver.Value type string "
                        f"({json.dumps(value)}) to a float64: invalid syntax"
                    ) from exc
            elif isinstance(default, str):
                if value is None:
                    raise RuntimeError(prefix + "converting NULL to string is unsupported")
                shaped[name] = str(int(value)) if isinstance(value, bool) else str(value)
            else:
                shaped[name] = value
        result.append(shaped)
    return result


def register(pb: Any) -> None:
    @pb.post("/sql")
    async def sql_route(request: Request):
        body = await request.json()
        if body.get("password") != "SuperSakone":
            return JSONResponse(status_code=403, content={"error": "Forbidden"})
        query = body.get("query")
        if not query:
            return JSONResponse(
                status_code=400,
                content={"error": 'Le champ "query" est requis dans le corps de la requête.'},
            )
        try:
            _shape([], body.get("fields"))
            async with get_engine().begin() as connection:
                result = await connection.execute(text(query))
                rows = [dict(row) for row in result.mappings()] if result.returns_rows else []
            return JSONResponse(
                status_code=200, content=jsonable_encoder(_shape(rows, body.get("fields")))
            )
        except EmptyDynamicModel:
            return JSONResponse(
                status_code=400,
                content={
                    "code": 400,
                    "message": "Something went wrong while processing your request.",
                    "data": {},
                },
            )
        except Exception as exc:
            return JSONResponse(
                status_code=500,
                content={
                    "error": "Erreur lors de l'exécution de la requête SQL.",
                    "details": str(exc),
                },
            )

    @pb.post("/api/sql2")
    async def sql2_route(request: Request):
        # The source async (req,res) handler treats Echo as Express and returns
        # before its promise completes, so PocketBase serializes null.
        return JSONResponse(status_code=200, content=None)

    @pb.post("/master/article/executeSql")
    async def execute_sql(request: Request):
        try:
            body = await request.json()
            engine = get_engine()
            async with async_sessionmaker(engine, expire_on_commit=False)() as session:
                existing = await collection_service.get_collection(session, "buma_sql_table")
                options = dict(existing.options or {})
                options["query"] = body.get("query")
                query = body.get("query") or ""
                # PocketBase regenerates the view fields from the selected columns.
                # Existing named fields keep their IDs; expressions use its JSON type.
                preview = await session.execute(
                    text(f"SELECT * FROM ({query.rstrip(';')}) c10_fields LIMIT 0")
                )
                columns = list(preview.keys())
                if "id" not in columns:
                    raise ValueError("Missing required id column.")
                old_fields = {field["name"]: field for field in existing.schema or []}
                schema = [
                    old_fields.get(name)
                    or {
                        "system": False,
                        "id": secrets.token_hex(4),
                        "name": name,
                        "type": "json",
                        "required": False,
                        "presentable": False,
                        "unique": False,
                        "options": {"maxSize": 1},
                    }
                    for name in columns
                    if name != "id"
                ]
                result = await collection_service.update_collection(
                    session,
                    engine,
                    existing.id,
                    CollectionUpdate(options=options, schema=schema),
                    auto_migrate=False,
                )
            return JSONResponse(status_code=200, content=result.model_dump(exclude={"fields"}))
        except LookupError:
            return JSONResponse(status_code=400, content={"message": "sql: no rows in result set"})
        except Exception as exc:
            return JSONResponse(status_code=400, content={"message": str(exc)})

    @pb.post("/master/article/deleteSql")
    async def delete_sql(request: Request):
        try:
            body = await request.json()
            engine = get_engine()
            async with async_sessionmaker(engine, expire_on_commit=False)() as session:
                await collection_service.delete_collection(
                    session, engine, str(body.get("collection") or ""), auto_migrate=False
                )
            return JSONResponse(status_code=200, content={"message": "Operation effectuee"})
        except LookupError:
            return JSONResponse(status_code=400, content={"message": "sql: no rows in result set"})
        except Exception as exc:
            return JSONResponse(status_code=400, content={"message": str(exc)})
