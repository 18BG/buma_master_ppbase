"""PDV/SIM creation behavior from the frozen BUMA source."""

from __future__ import annotations

from typing import Any

from ppbase.services.record_service import create_record, resolve_collection
from ppbase.services.record_storage_coordinator import run_record_storage_transaction
from sqlalchemy import text


class PdvValidationError(ValueError):
    """Raised for invalid SIM/PDV creation data."""


async def create_sim_with_pdv(
    engine: Any, sim_number: str, author: str, article_id: str | None = None
) -> dict[str, Any]:
    if not sim_number:
        raise PdvValidationError("Numéro de SIM manquant")

    async def operation(active_engine: Any) -> dict[str, Any]:
        sim_collection = await resolve_collection(active_engine, "buma_sim")
        pdv_collection = await resolve_collection(active_engine, "buma_pdv")
        async with active_engine.connect() as connection:
            existing = await connection.execute(
                text('SELECT id FROM "buma_sim" WHERE "number" = :number LIMIT 1'),
                {"number": sim_number},
            )
            if existing.first() is not None:
                raise PdvValidationError("Cette carte SIM existe déjà")

        network = "RS"
        if article_id:
            async with active_engine.connect() as connection:
                row = (
                    (
                        await connection.execute(
                            text('SELECT kind FROM "buma_article" WHERE id = :id LIMIT 1'),
                            {"id": article_id},
                        )
                    )
                    .mappings()
                    .first()
                )
            if row is None:
                raise PdvValidationError("Article introuvable")
            kind = str(row.get("kind") or "")
            if not kind.startswith("UV_"):
                raise PdvValidationError("Article invalide pour la création de SIM")
            network = kind.removeprefix("UV_")

        pdv = await create_record(
            active_engine,
            pdv_collection,
            {"firstname": "créé par C5", "status": True, "author": author},
        )
        sim = await create_record(
            active_engine,
            sim_collection,
            {
                "number": sim_number,
                "network": network,
                "status": True,
                "author": author,
                "pdv": pdv["id"],
            },
        )
        return {"sim": sim, "pdv": pdv}

    return await run_record_storage_transaction(engine, operation)
