"""The 0.22.34 reference conclusion must not depend on the gate it opens."""

from pathlib import Path

import pytest

from scripts import c11_import
from scripts.c11_version_reference import (
    ASSESSED_VERSION,
    VERSION_GATE_BLOCKER,
    gate_closed_for,
    importer_view,
    only_version_gate_blocks,
)


def gate(blockers_0_22_34, blockers_0_22_27=()):
    return {
        "0.22.34": {"with_source_version": {"blocker_codes": list(blockers_0_22_34)}},
        "0.22.27": {"with_source_version": {"blocker_codes": list(blockers_0_22_27)}},
    }


def test_only_the_version_gate_may_block_the_assessed_version():
    assert only_version_gate_blocks(gate([VERSION_GATE_BLOCKER]))
    assert not only_version_gate_blocks(gate([]))
    assert not only_version_gate_blocks(gate([VERSION_GATE_BLOCKER, "schema_drift"]))
    assert not only_version_gate_blocks(gate(["source_version_evidence_invalid"]))
    assert not only_version_gate_blocks(gate([VERSION_GATE_BLOCKER], ["schema_drift"]))


def test_gate_is_measured_closed_even_without_the_published_evidence(monkeypatch):
    entry = dict(c11_import.VALIDATED_SOURCE_VERSIONS[ASSESSED_VERSION])
    entry["evidence"] = "docs/absent-c11-reference-during-a-replay.json"
    monkeypatch.setitem(c11_import.VALIDATED_SOURCE_VERSIONS, ASSESSED_VERSION, entry)
    # A replay that writes the report to its documented path runs without it.
    assert c11_import.source_version_status(ASSESSED_VERSION)[0] == (
        "source_version_evidence_invalid"
    )
    with gate_closed_for(ASSESSED_VERSION):
        assert c11_import.source_version_status(ASSESSED_VERSION) == (VERSION_GATE_BLOCKER, None)
    assert c11_import.VALIDATED_SOURCE_VERSIONS[ASSESSED_VERSION] is entry


def gate_only_diagnose(raise_for=None):
    """A diagnose stub whose only blocker is the importer's version gate."""

    def diagnose(data, schema, *, source_version=None, **_):
        if raise_for is not None and source_version == raise_for:
            raise RuntimeError("diagnose failed")
        code = c11_import.source_version_status(source_version)[0]
        return {
            "blockers": [{"code": code}] if code else [],
            "warnings": [],
            "rows": 0,
            "relations_checked": 0,
            "files": 0,
            "unreferenced_files": 0,
            "collections": [],
            "internal_tables_not_imported": [],
        }

    return diagnose


def test_importer_view_measures_the_assessed_version_without_the_evidence(monkeypatch):
    entry = dict(c11_import.VALIDATED_SOURCE_VERSIONS[ASSESSED_VERSION])
    entry["evidence"] = "docs/absent-c11-reference-during-a-replay.json"
    monkeypatch.setitem(c11_import.VALIDATED_SOURCE_VERSIONS, ASSESSED_VERSION, entry)
    monkeypatch.setattr(c11_import, "diagnose", gate_only_diagnose())
    view = importer_view(Path("unused"), Path("unused"), ASSESSED_VERSION)
    assert view["with_source_version"]["blocker_codes"] == [VERSION_GATE_BLOCKER]
    assert c11_import.VALIDATED_SOURCE_VERSIONS[ASSESSED_VERSION] is entry


def test_importer_view_restores_the_registry_when_diagnose_raises(monkeypatch):
    entry = c11_import.VALIDATED_SOURCE_VERSIONS[ASSESSED_VERSION]
    # Restored by monkeypatch even if importer_view loses it, so a regression
    # here does not make later import tests fail too.
    monkeypatch.setitem(c11_import.VALIDATED_SOURCE_VERSIONS, ASSESSED_VERSION, entry)
    monkeypatch.setattr(c11_import, "diagnose", gate_only_diagnose(raise_for=ASSESSED_VERSION))
    with pytest.raises(RuntimeError, match="diagnose failed"):
        importer_view(Path("unused"), Path("unused"), ASSESSED_VERSION)
    assert c11_import.VALIDATED_SOURCE_VERSIONS[ASSESSED_VERSION] is entry


def measured_report(real_shape_blockers=(VERSION_GATE_BLOCKER,)):
    """The parts of a reference report that its conclusion is computed from."""

    def profile(blockers):
        return {
            "storage_identical": True,
            "unexpected_formats": {"0.22.27": [], "0.22.34": []},
            "view_schemas": {
                version: {"equal_to_export_without_field_ids": 42, "views": 42}
                for version in ("0.22.27", "0.22.34")
            },
            "importer_diagnostic_differences": [],
            "importer_diagnostic": gate(blockers),
        }

    return {
        "source": {"storage_paths_changed": []},
        "binaries": {
            "binaries": {
                version: {"embedded_sqlite_version": "3.45.1"} for version in ("0.22.27", "0.22.34")
            },
            "profiles": {
                "default": profile([VERSION_GATE_BLOCKER]),
                "real_shape": profile(list(real_shape_blockers)),
            },
            "default_profile_vs_fb381ac": {"rows": {"identical": True}},
        },
        "real_backup_formats": {
            "header": {"last_writer_sqlite_version": 3_045_001},
            "assessment": {"all_match": True},
        },
    }


def test_the_reference_accepts_0_22_34_only_when_every_profile_is_blocked_by_the_gate_alone():
    from scripts.c11_version_reference import STATEMENT, conclusions

    accepted = conclusions(measured_report())
    assert accepted["only_version_gate_blocks_0_22_34_all_profiles"] is True
    assert accepted["importer_can_accept_0_22_34"] is True
    assert accepted["statement"] == STATEMENT

    # The importer's registry reads its version from real_shape: another blocker
    # there must withhold the conclusion even when the default fixture is clean.
    refused = conclusions(measured_report([VERSION_GATE_BLOCKER, "schema_drift"]))
    assert refused["only_version_gate_blocks_0_22_34_default_fixture"] is True
    assert refused["only_version_gate_blocks_0_22_34_all_profiles"] is False
    assert refused["importer_can_accept_0_22_34"] is False
    assert refused["statement"] == "Not established: see checks."
