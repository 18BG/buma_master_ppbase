"""Customer creation behavior from the frozen BUMA source."""

from __future__ import annotations

from typing import Any

from ppbase.services.record_service import create_record, resolve_collection
from ppbase.services.record_storage_coordinator import run_record_storage_transaction
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


class CustomerValidationError(ValueError):
    """Raised for a source-compatible customer payload error."""


async def _create_batch(
    engine: Any, customers: list[dict[str, Any]], author: str
) -> list[dict[str, Any]]:
    collection = await resolve_collection(engine, "buma_customer")
    async with engine.connect() as connection:
        last_number = (
            await connection.execute(
                text('SELECT COALESCE(MAX("customer_no"), 0) FROM "buma_customer"')
            )
        ).scalar_one()

    created: list[dict[str, Any]] = []
    for index, customer in enumerate(customers, start=1):
        payload = dict(customer)
        payload["status"] = True
        payload["author"] = author
        payload["customer_no"] = int(last_number) + index
        created.append(await create_record(engine, collection, payload))
    return created


async def create_customers_batch(
    engine: AsyncEngine,
    customers: list[dict[str, Any]],
    author: str,
) -> list[dict[str, Any]]:
    """Create a validated customer batch atomically."""
    if not isinstance(customers, list):
        raise CustomerValidationError("La liste des clients est obligatoire")
    if not all(isinstance(item, dict) and str(item.get("firstname", "")) for item in customers):
        raise CustomerValidationError("Le prénom du client est obligatoire")
    return await run_record_storage_transaction(
        engine,
        lambda active_engine: _create_batch(active_engine, customers, author),
    )
