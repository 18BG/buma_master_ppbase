"""Source-compatible role and application permission resolution."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any


def _record_value(record: Mapping[str, Any] | None, field: str, default: Any = None) -> Any:
    if not isinstance(record, Mapping):
        return default
    return record.get(field, default)


def parse_permission(value: Any) -> dict[str, Any]:
    """Parse the PocketBase JSON ``roles.permission`` value safely."""
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    return {}


def _actions(info: Any) -> list[str]:
    if isinstance(info, Mapping):
        raw = info.get("actions", [])
    elif isinstance(info, str):
        try:
            raw = json.loads(info).get("actions", [])
        except (TypeError, ValueError, AttributeError):
            raw = []
    else:
        raw = []
    return [str(item) for item in raw] if isinstance(raw, list) else []


def _admin_permissions(app_name: str, app_roles: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    permissions: dict[str, Any] = {
        "isAdmin": True,
        "create": True,
        "update": True,
        "remove": True,
        "read": True,
        "manage": True,
    }
    for app_role in app_roles:
        if _record_value(app_role, "name") != app_name:
            continue
        for action in _actions(_record_value(app_role, "info")):
            key = action.split(":", 1)[0].strip()
            if key:
                permissions[key] = True
        break
    return permissions


def get_app_permission(
    role: Mapping[str, Any] | None,
    app_name: str,
    app_roles: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Resolve the permission object used by the legacy backend.

    Admin roles receive the default full CRUD/manage set and the extra action
    flags declared by ``buma_apps_roles.info.actions``. Non-admin roles only
    receive the named application object from ``roles.permission``.
    """
    permission = parse_permission(_record_value(role, "permission"))
    if not permission:
        return {"isAdmin": False}
    if permission.get("isAdmin") is True:
        return _admin_permissions(app_name, app_roles)
    selected = permission.get(app_name)
    return dict(selected) if isinstance(selected, Mapping) else {}
