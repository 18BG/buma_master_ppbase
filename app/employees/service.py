"""Employee creation behavior from the frozen BUMA source."""

from __future__ import annotations

from typing import Any

from ppbase.services.record_service import create_record, resolve_collection
from ppbase.services.record_storage_coordinator import run_record_storage_transaction


class EmployeeValidationError(ValueError):
    """Raised for invalid employee account data."""


async def _create_batch(
    engine: Any, accounts: list[dict[str, Any]], author: str
) -> list[dict[str, Any]]:
    persons = await resolve_collection(engine, "buma_person_entity")
    employees = await resolve_collection(engine, "buma_employee")
    created: list[dict[str, Any]] = []
    for account in accounts:
        kind = account.get("type", "PERSONNE")
        person_payload = {
            "address": account.get("address", ""),
            "contact": account.get("contact", ""),
            "email": account.get("email", ""),
            "type": kind,
            "author": author,
            "status": True,
        }
        if kind == "PERSONNE":
            person_payload.update(
                firstname=account.get("firstname", ""),
                lastname=account.get("lastname", ""),
            )
        else:
            person_payload["name"] = account.get("name", "")
        person = await create_record(engine, persons, person_payload)
        created.append(
            await create_record(
                engine,
                employees,
                {
                    "person_entity": person["id"],
                    "author": author,
                    "position": account.get("position", ""),
                    "status": True,
                    "solde": 0,
                },
            )
        )
    return created


async def create_employees_batch(
    engine: Any, accounts: list[dict[str, Any]], author: str
) -> list[dict[str, Any]]:
    if not isinstance(accounts, list) or not accounts:
        raise EmployeeValidationError("La liste des employés est obligatoire")
    return await run_record_storage_transaction(
        engine,
        lambda active_engine: _create_batch(active_engine, accounts, author),
    )
