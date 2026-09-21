from pathlib import Path

from app.hooks.register import HOOK_MODULES


def test_employee_and_pdv_routes_are_registered() -> None:
    employee = (Path(__file__).parents[1] / "app/hooks/employee.py").read_text()
    pdv = (Path(__file__).parents[1] / "app/hooks/pdv.py").read_text()
    assert '"/master/employee/create"' in employee
    assert '"/api2/employee/create"' in employee
    assert '"/master/pdv/newSim"' in pdv
    assert "app.hooks.employee" in HOOK_MODULES
    assert "app.hooks.pdv" in HOOK_MODULES
