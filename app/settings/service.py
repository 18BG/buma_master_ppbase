"""Settings lookup matching the frozen ``master.settings.ts`` helper."""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine


class SettingsNotFoundError(LookupError):
    """Raised when the canonical BUMA configuration record is absent."""


async def get_settings(executor: AsyncEngine | AsyncConnection) -> dict[str, Any]:
    """Return ``buma_config.CONFIGURATION_1`` or fail explicitly."""
    if isinstance(executor, AsyncConnection):
        connection = executor
        result = await connection.execute(
            text('SELECT * FROM "buma_config" WHERE "id" = :id LIMIT 1'),
            {"id": "CONFIGURATION_1"},
        )
        row = result.mappings().first()
    else:
        async with executor.connect() as connection:
            result = await connection.execute(
                text('SELECT * FROM "buma_config" WHERE "id" = :id LIMIT 1'),
                {"id": "CONFIGURATION_1"},
            )
            row = result.mappings().first()

    if row is None:
        raise SettingsNotFoundError("Settings not found")
    return dict(row)
