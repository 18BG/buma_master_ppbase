"""Native CRUD hooks from index.pb.ts, caisse.ts and master.middleware.ts."""

from __future__ import annotations

from typing import Any

from fastapi import Depends, Request
from fastapi.responses import JSONResponse
from ppbase.api.deps import get_optional_auth
from ppbase.db.engine import get_engine
from sqlalchemy import text

from app.auth.context import AUTH_RULE_CONTEXT, is_record_auth
from app.commerce.sequential import read, save
from app.legacy.stockage import parse_int

AUTHOR_COLLECTIONS = (
    "buma_supplier",
    "buma_payment",
    "buma_customer",
    "buma_person_entity",
    "buma_wallet",
    "buma_employee_position",
    "buma_customer_number",
    "buma_article",
    "buma_article_categorie",
    "buma_caisse_expense_type",
    "buma_pdv",
    "buma_caisse_transactions",
)
PROTECTED_SIM_FIELDS = ("number", "serial", "subscription", "pincode", "pushcode", "notes")
DELETE_COLLECTIONS = (
    "buma_sim",
    "buma_caisse",
    "buma_article",
    "buma_article_stockage",
    "buma_customer",
    "buma_wallet",
    "buma_supplier",
    "buma_employee",
    "buma_agent",
)
AUTH = Depends(get_optional_auth)


def author_id(auth):
    if not is_record_auth(auth) or auth.get("collectionName") == "_superusers":
        return ""
    return str(auth.get("id") or "")


async def exists(engine, table, field, identifier, suffix=""):
    async with engine.connect() as connection:
        return (
            await connection.execute(
                text(f'SELECT id FROM "{table}" WHERE "{field}"=:id {suffix} LIMIT 1'),
                {"id": identifier},
            )
        ).first() is not None


async def deletion_error(engine, collection, record):
    rid = record["id"]
    if collection == "buma_sim":
        if await exists(engine, "buma_transaction", "sim", rid):
            return (
                "Cette SIM est utilisée dans une ou plusieurs transactions "
                "et ne peut pas être supprimée"
            )
    elif collection == "buma_caisse":
        if await exists(engine, "buma_caisse_transactions", "caisse", rid):
            return (
                "Cette caisse est utilisée dans une ou plusieurs transactions "
                "et ne peut pas être supprimée"
            )
    elif collection == "buma_article":
        if await exists(engine, "buma_transaction", "article", rid):
            return (
                "Cet article est utilisé dans une ou plusieurs transactions "
                "et ne peut pas être supprimé"
            )
        if await exists(engine, "buma_article_stock", "article", rid, "AND quantity>0"):
            return "Cet article est utilisé dans un stockage et ne peut pas être supprimé"
    elif collection == "buma_article_stockage":
        if await exists(engine, "buma_article_stock", "stockage", rid):
            return "Ce stockage dispose d'un ou plusieurs articles et ne peut pas être supprimé"
    elif collection == "buma_customer":
        if await exists(
            engine, "buma_caisse_transactions", "customer", rid, "AND status=TRUE"
        ) or await exists(engine, "buma_transaction_list_view", "customer", rid):
            return (
                "Ce client est utilisé dans une ou plusieurs transactions "
                "et ne peut pas être supprimé"
            )
    elif collection == "buma_supplier":
        if await exists(engine, "buma_buy", "supplier", rid):
            return (
                "Ce fournisseur est utilisé dans une ou plusieurs transactions "
                "et ne peut pas être supprimé"
            )
    elif collection == "buma_wallet":
        if await exists(engine, "buma_wallet_transactions", "wallet", rid, "AND status=TRUE"):
            return "Ce porte-feuille dispose d'une ou plusieurs transactions valide"
    elif collection == "buma_employee":
        for table, message in (
            (
                "buma_employee_contract",
                "Cet employé a un ou plusieurs contrats associés et ne peut pas être supprimé",
            ),
            (
                "buma_employee_debt",
                "Cet employé a une ou plusieurs dettes associées et ne peut pas être supprimé",
            ),
            (
                "buma_employee_document",
                "Cet employé a un ou plusieurs documents associés et ne peut pas être supprimé",
            ),
        ):
            if await exists(engine, table, "employee", rid):
                return message
    elif collection == "buma_agent":
        async with engine.connect() as connection:
            customers = [
                dict(row)
                for row in (
                    await connection.execute(
                        text("SELECT * FROM buma_customer WHERE agent=:id"), {"id": rid}
                    )
                ).mappings()
            ]
        for customer in customers:
            name = str(customer.get("firstname") or "") + " " + str(customer.get("lastname") or "")
            if await exists(
                engine, "buma_caisse_transactions", "customer", customer["id"], "AND status=TRUE"
            ):
                return (
                    f"Cet agent a un client ({name}) avec des transactions actives "
                    "et ne peut pas être supprimé"
                )
            if await exists(engine, "buma_transaction_list_view", "customer", customer["id"]):
                return (
                    f"Cet agent a un client ({name}) avec des transactions "
                    "et ne peut pas être supprimé"
                )
    return None


def register(pb: Any):
    @pb.on_record_create_request(id="legacy_request_id_length", priority=-200)
    async def request_id_length(event):
        # PocketBase 0.22 rejects a supplied ID before invoking request hooks.
        # PPBase's storage-safe IDs have a looser length constraint, so reproduce
        # this request-form check locally without changing internal legacy IDs.
        identifier = event.data.get("id")
        if identifier not in (None, "") and len(str(identifier)) != 15:
            raise event._http_error(
                400,
                "Failed to create record.",
                {
                    "id": {
                        "code": "validation_length_invalid",
                        "message": "The length must be exactly 15.",
                    }
                },
            )
        return await event.next()

    @pb.on_record_create_request(*AUTHOR_COLLECTIONS, id="legacy_author_status", priority=-100)
    async def default_author_status(event):
        event.data["author"] = author_id(event.auth)
        event.data["status"] = True
        return await event.next()

    @pb.on_record_create_request("buma_caisse", id="legacy_caisse_closed")
    async def caisse_closed(event):
        event.data.update(author=author_id(event.auth), status=True, state="CLOSE")
        return await event.next()

    @pb.on_record_create_request("buma_employee_debt", id="legacy_employee_debt")
    async def employee_debt(event):
        employee = await read(event.engine, "buma_employee", str(event.data.get("employee") or ""))
        try:
            amount = int(float(event.data.get("amount") or 0))
        except (ValueError, TypeError):
            raise event._http_error(
                400, "Something went wrong while processing your request."
            ) from None
        if not employee or not amount:
            # Source throws Error rather than BadRequestError for these two failures.
            raise event._http_error(400, "Something went wrong while processing your request.")
        # The native PocketBase form transaction includes this hook save. A later
        # debt INSERT failure must roll the employee update back as well.
        await save(
            event.engine,
            "buma_employee",
            employee["id"],
            {"solde": int(employee.get("solde") or 0) - amount},
        )
        event.data.update(author=author_id(event.auth), status=True, state="UNPAID")
        return await event.next()

    @pb.on_record_update_request("buma_sim", id="legacy_sim_protection")
    async def sim_update(event):
        identifier = event.record_id or ""
        if await exists(event.engine, "buma_transaction", "sim", identifier):
            original = await read(event.engine, "buma_sim", identifier)
            if original:
                for field in PROTECTED_SIM_FIELDS:
                    if field in event.data:
                        value = event.data[field]
                        value = "" if value is None else str(value)
                        if value != (original.get(field) or ""):
                            raise event._http_error(
                                400,
                                (
                                    "Cette SIM est utilisée dans une ou plusieurs transactions "
                                    "et les champs critiques (numéro, série, réseau, etc.) "
                                    "ne peuvent pas être modifiés"
                                ),
                            )
        return await event.next()

    @pb.on_record_delete_request(*DELETE_COLLECTIONS, id="legacy_delete_protection")
    async def before_delete(event):
        name = event.collection.name
        original = await read(event.engine, name, event.record_id or "")
        if original:
            message = await deletion_error(event.engine, name, original)
            if message:
                raise event._http_error(400, message)
        return await event.next()

    @pb.post("/master/employee/payDebt")
    async def pay_debt(request: Request, auth: dict[str, Any] | None = AUTH):
        if not auth:
            return JSONResponse(
                status_code=401, content={"message": "The request requires authentication."}
            )
        permissions = AUTH_RULE_CONTEXT.get() or {}
        if not (
            permissions.get("role.permission.isAdmin")
            or permissions.get("role.permission.employee.update")
        ):
            return JSONResponse(
                status_code=403,
                content={"message": "Vous n'etes pas autorisé a effectuer une telle transaction"},
            )
        data = await request.json()
        debt = await read(get_engine(), "buma_employee_debt", data.get("debt") or "")
        employee = await read(get_engine(), "buma_employee", (debt or {}).get("employee") or "")
        if not debt or not employee:
            return JSONResponse(status_code=400, content={"message": "Employee not found"})
        amount = parse_int(data.get("amount"))
        await save(
            get_engine(),
            "buma_employee",
            employee["id"],
            {"solde": int(employee.get("solde") or 0) + amount},
        )
        paid = int(debt.get("paid") or 0) + amount
        values = {"paid": paid, "status": False}
        if int(debt.get("amount") or 0) == paid:
            values["state"] = "PAID"
        await save(get_engine(), "buma_employee_debt", debt["id"], values)
        return JSONResponse(status_code=200, content={"message": "Debt paid"})
