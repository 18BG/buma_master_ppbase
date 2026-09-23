from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.hooks import legacy_records


class Hooks:
    def __init__(self):
        self.callbacks = {}

    def collect(self, *collections, **options):
        def register(fn):
            self.callbacks[options.get("id", "")] = fn
            return fn

        return register

    on_record_create_request = collect
    on_record_update_request = collect
    on_record_delete_request = collect
    post = collect


@pytest.fixture
def hooks(monkeypatch):
    registry = Hooks()
    legacy_records.register(registry)
    monkeypatch.setattr(legacy_records, "get_engine", lambda: object())
    return registry.callbacks


def event(data=None, auth=None):
    def error(status, message, data=None):
        return HTTPException(status_code=status, detail={"message": message, "data": data or {}})

    return SimpleNamespace(
        data=data or {},
        auth=auth,
        next=AsyncMock(return_value="next"),
        engine=object(),
        record_id="sim",
        _http_error=error,
    )


@pytest.mark.parametrize(
    "auth,expected",
    [
        (None, ""),
        ({"type": "admin", "id": "root"}, ""),
        ({"type": "auth", "collectionName": "_superusers", "id": "root"}, ""),
        ({"type": "auth", "collectionName": "users", "id": "user"}, "user"),
    ],
)
async def test_author_status_overrides_supplied_values(hooks, auth, expected):
    e = event({"author": "spoofed", "status": False}, auth)
    assert await hooks["legacy_author_status"](e) == "next"
    assert e.data == {"author": expected, "status": True}
    e.next.assert_awaited_once()


async def test_caisse_state_is_overridden_on_native_creation(hooks):
    e = event({"state": "OPEN", "status": False}, {"type": "auth", "id": "user"})
    await hooks["legacy_caisse_closed"](e)
    assert e.data == {"state": "CLOSE", "status": True, "author": "user"}


async def test_employee_debit_precedes_debt_save_failure(hooks, monkeypatch):
    monkeypatch.setattr(
        legacy_records, "read", AsyncMock(return_value={"id": "employee", "solde": 100})
    )
    write = AsyncMock()
    monkeypatch.setattr(legacy_records, "save", write)
    e = event({"employee": "employee", "amount": 20}, {"type": "auth", "id": "user"})
    e.next.side_effect = RuntimeError("later debt form failure")
    with pytest.raises(RuntimeError, match="later debt"):
        await hooks["legacy_employee_debt"](e)
    assert write.await_args.args[1:] == ("buma_employee", "employee", {"solde": 80})
    assert e.data["state"] == "UNPAID" and e.data["author"] == "user"


@pytest.mark.parametrize(
    "body,denied",
    [
        ({"number": "changed"}, True),
        ({"network": "PAANI"}, False),
        ({"pdv": ""}, False),
        ({"notes": "changed"}, True),
    ],
)
async def test_sim_guard_keeps_exact_protected_field_set(hooks, monkeypatch, body, denied):
    monkeypatch.setattr(legacy_records, "exists", AsyncMock(return_value=True))
    monkeypatch.setattr(
        legacy_records, "read", AsyncMock(return_value={"id": "sim", "number": "old", "notes": ""})
    )
    e = event(body)
    if denied:
        with pytest.raises(HTTPException) as result:
            await hooks["legacy_sim_protection"](e)
        assert (
            result.value.status_code == 400 and "champs critiques" in result.value.detail["message"]
        )
        e.next.assert_not_awaited()
    else:
        assert await hooks["legacy_sim_protection"](e) == "next"


async def test_wallet_guard_checks_active_transactions_only(monkeypatch):
    lookup = AsyncMock(return_value=False)
    monkeypatch.setattr(legacy_records, "exists", lookup)
    assert await legacy_records.deletion_error(object(), "buma_wallet", {"id": "w"}) is None
    assert lookup.await_args.args[1:] == (
        "buma_wallet_transactions",
        "wallet",
        "w",
        "AND status=TRUE",
    )


@pytest.mark.parametrize("identifier", ["short", "a" * 14, "a" * 16])
async def test_request_id_length_is_checked_before_business_hooks(hooks, identifier):
    e = event({"id": identifier})
    with pytest.raises(HTTPException) as result:
        await hooks["legacy_request_id_length"](e)
    assert result.value.status_code == 400
    assert result.value.detail["data"]["id"] == {
        "code": "validation_length_invalid",
        "message": "The length must be exactly 15.",
    }
    e.next.assert_not_awaited()


@pytest.mark.parametrize("identifier", [None, "", "a" * 15])
async def test_generated_or_valid_request_ids_continue(hooks, identifier):
    e = event({"id": identifier})
    assert await hooks["legacy_request_id_length"](e) == "next"
