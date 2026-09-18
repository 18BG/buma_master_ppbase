"""Expose PocketBase's legacy auth username for view compatibility.

PocketBase exposes ``username`` as an auth system field. PPBase 0.3.0 does not
create that physical column, while the BUMA views use it for display. Keep the
compatibility field separate from authentication identity selection; the auth
checkpoint will decide uniqueness and login semantics.
"""


USERNAME_FIELD = {
    "id": "users_username_compat",
    "name": "username",
    "type": "text",
    "required": False,
    "system": False,
    "presentable": False,
    "options": {"unique": False, "pattern": ""},
}


async def up(app):
    collection = await app.find_collection("users")
    schema = list(collection.schema or [])
    if any(field.get("name") == "username" for field in schema):
        return
    schema.append(dict(USERNAME_FIELD))
    await app.update_collection(collection.id, {"schema": schema})


async def down(app):
    collection = await app.find_collection("users")
    schema = [field for field in (collection.schema or []) if field.get("name") != "username"]
    await app.update_collection(collection.id, {"schema": schema})
