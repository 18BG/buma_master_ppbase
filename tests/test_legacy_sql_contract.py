"""Golden responses observed from the frozen handler on PocketBase 0.22.27."""

import pytest

from app.hooks.legacy_sql import EmptyDynamicModel, _shape


@pytest.mark.parametrize(
    "row,fields,expected",
    [
        ({"value": 1}, {"value": 0}, {"value": 1}),
        ({"value": 1}, {"other": ""}, {"other": ""}),
        ({"value": 1}, {"value": ""}, {"value": "1"}),
        ({"value": 1}, {"value": False}, {"value": True}),
        ({"value": "false"}, {"value": False}, {"value": False}),
        ({"value": "0"}, {"value": False}, {"value": False}),
        ({"value": "true"}, {"value": False}, {"value": True}),
    ],
)
def test_dynamic_model_projection_and_types(row, fields, expected):
    assert _shape([row], fields) == [expected]


@pytest.mark.parametrize(
    "initial,detail",
    [
        ("", "converting NULL to string is unsupported"),
        (0, "converting NULL to float64 is unsupported"),
        (False, "sql/driver: couldn't convert <nil> (<nil>) into type bool"),
    ],
)
def test_source_null_scan_errors_are_not_defaulted(initial, detail):
    with pytest.raises(RuntimeError) as result:
        _shape([{"value": None}], {"value": initial})
    assert str(result.value) == 'sql: Scan error on column index 0, name "value": ' + detail


def test_empty_model_is_a_separate_source_failure():
    with pytest.raises(EmptyDynamicModel):
        _shape([], {})
