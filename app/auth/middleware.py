"""Route middleware matching PocketBase's admin-or-record auth guard."""

from __future__ import annotations

from typing import Any


def require_admin_or_record_auth():
    """Allow superuser/admin or any valid auth-record token.

    This is the Python equivalent of ``$apis.requireAdminOrRecordAuth()``.
    """

    async def middleware(event: Any):
        await event.load_auth()
        if not event.has_auth():
            raise event._http_error(401, "The request requires authentication.")
        if not (event.is_superuser() or event.has_record_auth()):
            raise event._http_error(
                403,
                "The authorized auth model is not allowed to perform this action.",
            )
        return await event.next()

    return middleware
