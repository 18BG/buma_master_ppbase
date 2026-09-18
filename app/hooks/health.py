"""Foundation health routes."""

from __future__ import annotations

from typing import Any


def register(pb: Any) -> None:
    @pb.get("/")
    async def root_health() -> dict[str, str]:
        return {"status": "ok", "service": "buma-master-ppbase"}
