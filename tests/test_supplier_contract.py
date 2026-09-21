from pathlib import Path

from app.hooks.register import HOOK_MODULES


def test_supplier_route_and_relation_service_are_registered() -> None:
    source = (Path(__file__).parents[1] / "app/hooks/supplier.py").read_text()
    service = (Path(__file__).parents[1] / "app/suppliers/service.py").read_text()
    assert '"/api2/supplier/creates"' in source
    assert '"buma_person_entity"' in service
    assert '"person_entity": person["id"]' in service
    assert "app.hooks.supplier" in HOOK_MODULES
