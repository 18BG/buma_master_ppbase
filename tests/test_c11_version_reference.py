"""The 0.22.34 reference conclusion must not depend on the gate it opens."""

from scripts.c11_version_reference import VERSION_GATE_BLOCKER, only_version_gate_blocks


def gate(blockers_0_22_34, blockers_0_22_27=()):
    return {
        "0.22.34": {"with_source_version": {"blocker_codes": list(blockers_0_22_34)}},
        "0.22.27": {"with_source_version": {"blocker_codes": list(blockers_0_22_27)}},
    }


def test_gate_closed_and_gate_open_both_conclude_true():
    assert only_version_gate_blocks(gate([VERSION_GATE_BLOCKER]))
    assert only_version_gate_blocks(gate([]))


def test_any_other_blocker_keeps_the_conclusion_false():
    assert not only_version_gate_blocks(gate([VERSION_GATE_BLOCKER, "schema_drift"]))
    assert not only_version_gate_blocks(gate(["schema_drift"]))
    assert not only_version_gate_blocks(gate([], ["schema_drift"]))
