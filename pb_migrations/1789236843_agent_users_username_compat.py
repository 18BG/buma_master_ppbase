"""Expose PocketBase's auth username for the ``agent_users`` collection.

PocketBase exposes ``username`` as a system field on every auth collection,
including ``agent_users``.  PPBase 0.3.x does not create that physical column
from the exported custom schema, so keep it available for projections and
future authentication compatibility.  Login policy and uniqueness remain part
of checkpoint C3.
"""


USERNAME_FIELD = {
    "id": "agent_users_username_compat",
    "name": "username",
    "type": "text",
    "required": False,
    "system": False,
    "presentable": False,
    "options": {"unique": False, "pattern": ""},
}


async def up(app):
    collection = await app.find_collection("agent_users")
    schema = list(collection.schema or [])
    if any(field.get("name") == "username" for field in schema):
        return
    schema.append(dict(USERNAME_FIELD))
    await app.update_collection(collection.id, {"schema": schema})


async def down(app):
    collection = await app.find_collection("agent_users")
    schema = [field for field in (collection.schema or []) if field.get("name") != "username"]
    await app.update_collection(collection.id, {"schema": schema})
