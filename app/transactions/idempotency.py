"""Source-compatible transaction idempotency lookup helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

IDEMPOTENCY_PREFIX = "idem"
TRANSACTION_TABLE = "buma_transaction"


@dataclass(frozen=True, slots=True)
class IdempotencyLookup:
    """The source lookup tuple used by sale, return, and conversion flows."""

    type: str
    author: str
    system_id: str = ""
    legacy_system_id: str = ""
    mobile_message_id: str = ""


def build_idempotency_key(operation: str, raw_key: str | None = None) -> str:
    """Build the stable ``idem:<operation>:<key>`` source key."""
    normalized_operation = str(operation or "").strip().lower()
    normalized_raw_key = str(raw_key or "").strip()
    if not normalized_raw_key:
        return ""

    prefix = f"{IDEMPOTENCY_PREFIX}:{normalized_operation}:"
    if normalized_raw_key.startswith(prefix):
        return normalized_raw_key
    return f"{prefix}{normalized_raw_key}"


async def _find_on_connection(
    connection: AsyncConnection,
    lookup: IdempotencyLookup,
) -> dict[str, Any] | None:
    """Find one existing transaction using source precedence."""
    candidates = [
        ("system_id", lookup.system_id),
        ("system_id", lookup.legacy_system_id),
        ("mobile_message_id", lookup.mobile_message_id),
    ]
    seen: set[tuple[str, str]] = set()
    for field, value in candidates:
        value = str(value or "").strip()
        candidate = (field, value)
        if not value or candidate in seen:
            continue
        seen.add(candidate)
        result = await connection.execute(
            text(
                f'SELECT * FROM "{TRANSACTION_TABLE}" '
                f'WHERE "type" = :type AND "author" = :author '
                f'AND "{field}" = :value '
                'ORDER BY "created" ASC LIMIT 1'
            ),
            {"type": lookup.type, "author": lookup.author, "value": value},
        )
        row = result.mappings().first()
        if row is not None:
            return dict(row)
    return None


async def find_idempotent_transaction(
    executor: AsyncEngine | AsyncConnection,
    lookup: IdempotencyLookup,
) -> dict[str, Any] | None:
    """Find an existing transaction using a pooled engine or pinned connection."""
    if isinstance(executor, AsyncConnection):
        return await _find_on_connection(executor, lookup)
    async with executor.connect() as connection:
        return await _find_on_connection(connection, lookup)


async def acquire_idempotency_lock(
    connection: AsyncConnection,
    lookup: IdempotencyLookup,
) -> None:
    """Serialize concurrent attempts for the same key in one PostgreSQL tx."""
    key = next(
        (
            str(value).strip()
            for value in (
                lookup.system_id,
                lookup.legacy_system_id,
                lookup.mobile_message_id,
            )
            if str(value or "").strip()
        ),
        "",
    )
    if not key:
        return
    lock_key = f"buma:idempotency:{lookup.type}:{lookup.author}:{key}"
    await connection.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
        {"lock_key": lock_key},
    )
