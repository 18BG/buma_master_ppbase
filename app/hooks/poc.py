"""Small PPBase API POC used only by foundation tests."""

from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse
from ppbase.db.engine import get_engine
from ppbase.services.record_storage_coordinator import run_record_storage_transaction

POC_COLLECTION = "foundation_poc"


def register(pb: Any) -> None:
    @pb.get("/__foundation/poc")
    async def foundation_poc() -> dict[str, Any]:
        return {"ok": True, "stage": "foundation"}

    @pb.post("/__foundation/transaction")
    async def foundation_transaction(request: Request) -> dict[str, Any]:
        """Create two technical records through one request transaction.

        This endpoint is deliberately disabled until the foundation migration
        exists. It is a POC only and is not part of the future BUMA API.
        """
        payload = await request.json()

        async def operation(active_engine: Any) -> dict[str, Any]:
            repository = pb.records(POC_COLLECTION, engine=active_engine)
            first = await repository.create(
                {"name": str(payload.get("first", "first")), "value": 1}
            )
            second = await repository.create(
                {"name": str(payload.get("second", "second")), "value": 2}
            )
            return {"ok": True, "ids": [first.get("id"), second.get("id")]}

        return await run_record_storage_transaction(get_engine(), operation)

    @pb.post("/__foundation/transaction-rollback")
    async def foundation_transaction_rollback(request: Request) -> dict[str, Any]:
        """Force an error after one write; the record must be rolled back."""
        payload = await request.json()

        async def operation(active_engine: Any) -> dict[str, Any]:
            repository = pb.records(POC_COLLECTION, engine=active_engine)
            await repository.create({"name": str(payload.get("name", "rollback")), "value": 99})
            raise RuntimeError("foundation rollback probe")

        try:
            return await run_record_storage_transaction(get_engine(), operation)
        except RuntimeError as exc:
            if str(exc) != "foundation rollback probe":
                raise
            return JSONResponse(
                status_code=500,
                content={"ok": False, "rolled_back": True, "error": str(exc)},
            )

    @pb.on_record_create_request(POC_COLLECTION, id="foundation_poc_create")
    async def foundation_create_hook(event: Any) -> Any:
        event.data["hook_marker"] = "foundation_create_hook"
        return await event.next()
