"""Source-derived /api2 stock response and permission contracts."""

import json
from unittest.mock import AsyncMock

import pytest
from starlette.requests import Request

from app.hooks import legacy_stockage


class Routes:
    def __init__(self):
        self.routes = {}

    def register(self, method, path):
        def decorator(fn):
            self.routes[method, path] = fn
            return fn

        return decorator

    def post(self, path):
        return self.register("POST", path)

    def get(self, path):
        return self.register("GET", path)


def req(body):
    async def receive():
        return {"type": "http.request", "body": json.dumps(body).encode(), "more_body": False}

    return Request({"type": "http", "method": "POST", "headers": []}, receive)


@pytest.fixture
def routes(monkeypatch):
    route = Routes()
    legacy_stockage.register(route)
    monkeypatch.setattr(legacy_stockage, "get_engine", lambda: object())
    return route.routes


@pytest.mark.parametrize(
    "permission,status", [({}, 200), ({"update": False}, 401), ({"update": True}, 200)]
)
async def test_add_only_rejects_explicit_false(routes, monkeypatch, permission, status):
    monkeypatch.setattr(legacy_stockage, "source_permission", AsyncMock(return_value=permission))
    operation = AsyncMock(return_value={"message": "ok"})
    monkeypatch.setattr(legacy_stockage.stockage, "add_articles", operation)
    response = await routes["POST", "/api2/stockage/add_article"](
        req({"articles": []}), auth={"id": "u"}
    )
    assert response.status_code == status
    assert json.loads(response.body) == {"message": "Unauthorized" if status == 401 else "ok"}
    assert operation.await_count == (0 if status == 401 else 1)


@pytest.mark.parametrize(
    "permission,status",
    [
        ({"canManage": False, "canConvert": True}, 403),
        ({"canManage": True, "canConvert": True}, 200),
    ],
)
async def test_convert_requires_both_derived_permissions(routes, monkeypatch, permission, status):
    monkeypatch.setattr(legacy_stockage, "source_permission", AsyncMock(return_value=permission))
    monkeypatch.setattr(
        legacy_stockage.stockage, "convert", AsyncMock(return_value=(200, {"message": "okay"}))
    )
    response = await routes["POST", "/api2/stockage/convert"](req({}), auth={"id": "u"})
    assert response.status_code == status
    assert json.loads(response.body) == {
        "message": "Not allow to convert stock" if status == 403 else "okay"
    }


async def test_transfer_has_no_domain_permission_guard_or_extra_id(routes, monkeypatch):
    monkeypatch.setattr(legacy_stockage, "source_permission", AsyncMock(return_value={}))
    monkeypatch.setattr(
        legacy_stockage.stockage, "transfer", AsyncMock(return_value={"message": "ok"})
    )
    response = await routes["POST", "/api2/stockage/transfer"](req({}), auth={"id": "u"})
    assert response.status_code == 200 and json.loads(response.body) == {"message": "ok"}


async def test_swallowed_remove_exception_has_empty_body(routes, monkeypatch):
    monkeypatch.setattr(
        legacy_stockage, "source_permission", AsyncMock(return_value={"remove": False})
    )
    monkeypatch.setattr(legacy_stockage.stockage, "remove_articles", AsyncMock(return_value=None))
    response = await routes["POST", "/api2/stockage/remove_article"](req({}), auth={"id": "u"})
    assert response.status_code == 200 and response.body == b""
