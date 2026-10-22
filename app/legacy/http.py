"""PocketBase 0.22 requireAdminOrRecordAuth's observed failure envelope."""

UNAUTHORIZED = {
    "code": 401,
    "message": "The request requires admin or record authorization token to be set.",
    "data": {},
}

NOT_FOUND = {"code": 404, "message": "The requested resource wasn't found.", "data": {}}
