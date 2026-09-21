"""Align PPBase auth identity options with the frozen PocketBase schema."""

from __future__ import annotations

from copy import deepcopy


AUTH_IDENTITY_FIELDS = {
    "users": ["username"],
    "agent_users": ["email", "username"],
}
AUTH_PASSWORD_MIN_LENGTH = {"users": 5, "agent_users": 8}


async def up(app):
    for collection_name, identity_fields in AUTH_IDENTITY_FIELDS.items():
        collection = await app.find_collection(collection_name)
        options = deepcopy(collection.options or {})
        password_auth = deepcopy(options.get("passwordAuth") or {})
        password_auth["enabled"] = True
        password_auth["identityFields"] = list(identity_fields)
        options["passwordAuth"] = password_auth
        options["minPasswordLength"] = AUTH_PASSWORD_MIN_LENGTH[collection_name]
        await app.update_collection(collection.id, {"options": options})


async def down(app):
    for collection_name in AUTH_IDENTITY_FIELDS:
        collection = await app.find_collection(collection_name)
        options = deepcopy(collection.options or {})
        password_auth = deepcopy(options.get("passwordAuth") or {})
        password_auth["enabled"] = True
        password_auth["identityFields"] = ["email"]
        options["passwordAuth"] = password_auth
        options.pop("minPasswordLength", None)
        await app.update_collection(collection.id, {"options": options})
