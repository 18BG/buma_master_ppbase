from pathlib import Path

from app.hooks.register import HOOK_MODULES


def test_customer_route_and_create_hook_are_registered() -> None:
    source = (Path(__file__).parents[1] / "app/hooks/customer.py").read_text()
    assert '"/master/customer/creates"' in source
    assert 'on_record_create_request("buma_customer"' in source
    assert HOOK_MODULES == (
        "app.hooks.health",
        "app.hooks.auth",
        "app.hooks.customer",
        "app.hooks.supplier",
        "app.hooks.employee",
        "app.hooks.pdv",
        "app.hooks.finance",
        "app.hooks.stockage",
        "app.hooks.poc",
    )


def test_customer_source_contract_keeps_required_firstname_validation() -> None:
    source = (Path(__file__).parents[1] / "app/customers/service.py").read_text()
    assert "Le prénom du client est obligatoire" in source
    assert 'payload["status"] = True' in source
    assert 'payload["author"] = author' in source
