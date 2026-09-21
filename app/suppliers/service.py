"""Supplier creation behavior from the frozen BUMA source."""

from __future__ import annotations

from typing import Any

from ppbase.services.record_service import create_record, resolve_collection
from ppbase.services.record_storage_coordinator import run_record_storage_transaction


class SupplierValidationError(ValueError):
    """Raised for a source-compatible supplier payload error."""


async def _create_batch(
    engine: Any, suppliers: list[dict[str, Any]], author: str
) -> list[dict[str, Any]]:
    person_collection = await resolve_collection(engine, "buma_person_entity")
    supplier_collection = await resolve_collection(engine, "buma_supplier")
    created: list[dict[str, Any]] = []
    for supplier in suppliers:
        person = await create_record(
            engine,
            person_collection,
            {
                "firstname": supplier.get("firstname", ""),
                "lastname": supplier.get("lastname", ""),
                "name": supplier.get("name", ""),
                "author": author,
                "type": supplier.get("type", "PERSONNE"),
                "address": supplier.get("address", ""),
                "email": supplier.get("email", ""),
                "contact": supplier.get("contact", ""),
                "status": True,
            },
        )
        created.append(
            await create_record(
                engine,
                supplier_collection,
                {
                    "person_entity": person["id"],
                    "author": author,
                    "status": True,
                    "solde": 0,
                },
            )
        )
    return created


async def create_suppliers_batch(
    engine: Any,
    suppliers: list[dict[str, Any]],
    author: str,
) -> list[dict[str, Any]]:
    """Create person entities and suppliers atomically."""
    if not isinstance(suppliers, list):
        raise SupplierValidationError("La liste des fournisseurs est obligatoire")
    return await run_record_storage_transaction(
        engine,
        lambda active_engine: _create_batch(active_engine, suppliers, author),
    )
