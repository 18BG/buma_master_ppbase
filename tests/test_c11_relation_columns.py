"""D-0019, BUMA part: the three maxSelect-null relations are multiple.

Unit tests cover the shared multiplicity rule in the legacy helpers, the C5
seed, the decisions of migration 1789236850 (with a fake migration app) and
the physical relation check of the verifiers (strict "fresh" default). The
PostgreSQL behaviour (conversion, refusals, lock, rollback, down, replay,
fresh and partially migrated databases, down/up of the last five migrations,
single relation limit, stale prepared statements) is proven by
``scripts/c11_relation_columns_runtime.py``.

The last test runs the official PocketBase binaries, when available:

    BUMA_C11_POCKETBASE_0_22_27=/path/to/pocketbase-0.22.27
    BUMA_C11_POCKETBASE_0_22_34=/path/to/pocketbase-0.22.34
    BUMA_SCHEMA_EXPORT=/path/to/buma-master-pb_schema-corrected.json
"""

import asyncio
import copy
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest
from ppbase.models.field_multiplicity import field_is_multiple

from app.legacy.records import prepared
from app.legacy.views import source_view_row
from scripts import verify_complete_schema
from scripts.verify_schema_baseline import build_parser, verify, verify_relation_columns

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = Path(
    os.environ.get(
        "BUMA_SCHEMA_EXPORT", "/Users/omarsisss/Downloads/buma-master-pb_schema-corrected.json"
    )
)
MIGRATION = ROOT / "pb_migrations/1789236850_c11_relation_multiple_columns.py"
C5 = ROOT / "pb_migrations/1789236846_c5_reference_defaults.py"
BINARIES = {"0.22.27": "BUMA_C11_POCKETBASE_0_22_27", "0.22.34": "BUMA_C11_POCKETBASE_0_22_34"}


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def export() -> list[dict]:
    if not SCHEMA.is_file():
        pytest.skip("BUMA_SCHEMA_EXPORT is not available")
    return json.loads(SCHEMA.read_text())


def relation(max_select="absent", *, flat=False, kind="relation"):
    options = {"collectionId": "target000000001"}
    if max_select != "absent":
        options["maxSelect"] = max_select
    if flat:
        return {"name": "rel", "type": kind, **options}
    return {"name": "rel", "type": kind, "options": options}


def test_migration_targets_exactly_the_multiple_relations_of_the_export():
    module = load(MIGRATION, "d0019_targets")
    multiple = {
        (collection["name"], field["name"]): field["options"]["maxSelect"]
        for collection in export()
        if collection["type"] != "view"
        for field in collection["schema"]
        if field["type"] == "relation" and field_is_multiple(field)
    }
    assert multiple == {column: None for column in module.COLUMNS}


@pytest.mark.parametrize(
    "field,value,expected",
    [
        (relation(None), "pdv000000000001", ["pdv000000000001"]),
        (relation(None), "", []),
        (relation(None), ["a", "b"], ["a", "b"]),
        (relation(None, flat=True), "pdv000000000001", ["pdv000000000001"]),
        (relation(2), "pdv000000000001", ["pdv000000000001"]),
        (relation(1), "pdv000000000001", "pdv000000000001"),
        (relation(0), "pdv000000000001", "pdv000000000001"),
        (relation(), "pdv000000000001", "pdv000000000001"),
        (relation(None, kind="select"), "A", "A"),
        (relation(None, kind="file"), "a.png", "a.png"),
    ],
)
def test_legacy_writes_use_the_shared_multiplicity_rule(field, value, expected):
    assert prepared(field, value) == expected


def test_legacy_write_of_the_exported_customer_pdv_is_a_list():
    customer = next(c for c in export() if c["name"] == "buma_customer")
    pdv = next(f for f in customer["schema"] if f["name"] == "pdv")
    assert prepared(pdv, "pdv000000000001") == ["pdv000000000001"]


def test_view_rows_default_multiple_relations_to_empty_lists():
    schema = [
        {**relation(None), "name": "many"},
        {**relation(1), "name": "one"},
        {**relation(None, kind="select"), "name": "choice"},
    ]
    assert source_view_row({"many": None, "one": None, "choice": None}, schema) == {
        "many": [],
        "one": "",
        "choice": "",
    }


class SeedApp:
    def __init__(self):
        self.inserts: dict[str, tuple[str, dict]] = {}

    async def execute_sql(self, sql, params=None):
        table = sql.split('"')[1]
        if table == "buma_customer":
            self.inserts[params["p_id"]] = (sql, params)


def test_c5_seed_leaves_customer_divers_pdv_to_the_column_default():
    app = SeedApp()
    asyncio.run(load(C5, "c5_seed").up(app))
    sql, params = app.inserts["CUSTOMER_DIVERS"]
    # master.setup.ts l.52-60 omits pdv; PocketBase stores '[]' (see below). The
    # column default is '{}' (TEXT[]) or, before 1789236850, '' (VARCHAR(15)):
    # binding either '' or a list would break one of the two shapes.
    assert '"pdv"' not in sql.split("VALUES")[0] and "p_pdv" not in params
    columns = sql.split("(", 1)[1].split(")", 1)[0]
    assert '"customer_no"' in columns and '"firstname"' in columns
    assert {key for key, value in params.items() if isinstance(value, list)} == set()


class Result:
    def __init__(self, rows=(), scalars=()):
        self.rows, self.values = list(rows), list(scalars)

    def mappings(self):
        return self

    def first(self):
        return self.rows[0] if self.rows else None

    def one(self):
        assert len(self.rows) == 1
        return self.rows[0]

    def scalars(self):
        return SimpleNamespace(all=lambda: self.values)


ARRAY = {"type": "text[]", "not_null": True, "column_default": "'{}'::text[]"}
LEGACY = {"type": "character varying(15)", "not_null": True, "column_default": "''"}


class MigrationApp:
    """Answers the catalog/count queries of 1789236850 and records statements."""

    def __init__(self, module, states, *, counts=None, max_select=None):
        self.module, self.states, self.counts = module, states, counts or {}
        self.max_select, self.statements = max_select, []

    async def find_collection(self, name):
        column = dict(self.module.COLUMNS)[name]
        field = {"name": column, "type": "relation", "options": {"maxSelect": self.max_select}}
        return SimpleNamespace(type="base", schema=[field])

    async def execute_sql(self, sql, params=None):
        self.statements.append(sql)
        if sql is self.module._COLUMN_SQL:
            return Result([self.states[(params["table"], params["column"])]])
        if sql is self.module._DEPENDENT_VIEWS_SQL:
            return Result()
        if sql.startswith("SELECT count(*) AS total"):
            table = sql.rsplit('"', 2)[1]
            return Result([self.counts[table]])
        return Result()

    def changes(self):
        return [s for s in self.statements if s.startswith(("LOCK", "ALTER", "DROP", "CREATE"))]


def states(**overrides):
    module = load(MIGRATION, "d0019_states")
    result = {column: dict(ARRAY) for column in module.COLUMNS}
    for table, state in overrides.items():
        column = dict(module.COLUMNS)[table]
        result[(table, column)] = state
    return module, result


def test_up_has_no_effect_when_the_columns_already_are_text_arrays():
    module, current = states()
    app = MigrationApp(module, current)
    asyncio.run(module.up(app))
    assert app.changes() == []
    assert set(app.statements) == {module._COLUMN_SQL}


@pytest.mark.parametrize(
    "state",
    [
        {"type": "character varying(15)[]", "not_null": True, "column_default": "'{}'"},
        {"type": "jsonb", "not_null": True, "column_default": None},
        {"type": "text", "not_null": True, "column_default": "''::text"},
        {**LEGACY, "not_null": False},
        {**ARRAY, "column_default": None},
    ],
)
def test_up_and_down_refuse_unexpected_physical_shapes_before_any_change(state):
    module, current = states(buma_wallet=state)
    for direction in (module.up, module.down):
        app = MigrationApp(module, current)
        with pytest.raises(module.RelationColumnError):
            asyncio.run(direction(app))
        assert app.changes() == []


def test_up_and_down_refuse_metadata_that_is_not_a_multiple_relation():
    module, current = states(buma_customer=dict(LEGACY))
    for direction in (module.up, module.down):
        app = MigrationApp(module, current, max_select=1)
        with pytest.raises(module.RelationColumnError, match="not a multiple relation"):
            asyncio.run(direction(app))
        assert app.changes() == []


def test_up_refuses_a_column_used_by_a_view(monkeypatch):
    module, current = states(buma_customer=dict(LEGACY))
    app = MigrationApp(module, current)
    original = app.execute_sql

    async def execute_sql(sql, params=None):
        if sql is module._DEPENDENT_VIEWS_SQL and params["table"] == "buma_customer":
            app.statements.append(sql)
            return Result(scalars=["some_view"])
        return await original(sql, params)

    monkeypatch.setattr(app, "execute_sql", execute_sql)
    with pytest.raises(module.RelationColumnError, match="used by views"):
        asyncio.run(module.up(app))
    assert [s.split(" IN ")[0] for s in app.changes()] == ['LOCK TABLE "buma_customer"']


def test_migration_requires_the_corrected_ppbase(monkeypatch):
    module, current = states()
    monkeypatch.setitem(sys.modules, "ppbase.models.field_multiplicity", None)
    with pytest.raises(module.RelationColumnError, match="multiplicity rule"):
        asyncio.run(module.up(MigrationApp(module, current)))


def test_up_refusal_reports_counts_only_and_changes_nothing():
    module, current = states(buma_customer=dict(LEGACY))
    counts = {"buma_customer": {"total": 9, "empty": 3, "json_empty": 1, "refused": 2, "nulls": 0}}
    app = MigrationApp(module, current, counts=counts)
    with pytest.raises(module.RelationColumnError) as error:
        asyncio.run(module.up(app))
    message = str(error.value)
    assert message.startswith("buma_customer.pdv: 2 value(s) contain '['")
    assert "strpos(\"pdv\", '[') > 0" in message
    assert [s.split(" IN ")[0] for s in app.changes()] == ['LOCK TABLE "buma_customer"']


def test_down_refusal_reports_counts_only_and_changes_nothing():
    module, current = states()
    counts = {
        table: {"total": 4, "empty": 2, "multiple": 0, "refused": 0, "nulls": 0}
        for table, _ in module.COLUMNS
    }
    counts["buma_wallet"] = {"total": 4, "empty": 1, "multiple": 1, "refused": 1, "nulls": 0}
    app = MigrationApp(module, current, counts=counts)
    with pytest.raises(module.RelationColumnError) as error:
        asyncio.run(module.down(app))
    assert str(error.value).startswith(
        "buma_wallet.managers: 1 array(s) have more than one element and 1 element(s)"
    )
    assert len(app.changes()) == 1 and app.changes()[0].startswith("LOCK TABLE")


def test_conversion_statements_match_the_ppbase_ddl():
    module = load(MIGRATION, "d0019_sql")
    assert module.array_index_statement("buma_wallet", "managers") == (
        'CREATE INDEX IF NOT EXISTS "idx_buma_wallet_managers" '
        'ON "public"."buma_wallet" USING GIN ("managers")'
    )
    assert module.scalar_index_statement("buma_wallet", "managers") == (
        'CREATE INDEX IF NOT EXISTS "idx_buma_wallet_managers" '
        'ON "public"."buma_wallet" ("managers") WHERE "managers" != \'\''
    )
    up = module.up_value_counts_sql("buma_wallet", "managers")
    assert "strpos(\"managers\", '[') > 0 AND \"managers\" <> '[]'" in up
    down = module.down_value_counts_sql("buma_wallet", "managers")
    assert "char_length(" in down and "> 15" in down and "cardinality(" in down


def physical(column_type, default, method):
    return {"columns": {"rel": {"type": column_type, "not_null": True, "default": default}},
            "indexes": {"idx_coll_rel": method}}  # fmt: skip


def definitions(max_select):
    return [{"name": "coll", "type": "base", "schema": [relation(max_select)]}]


@pytest.mark.parametrize(
    "max_select,state,mode,ok",
    [
        (None, ("text[]", "'{}'::text[]", "gin"), "fresh", True),
        (None, ("text[]", "'{}'::text[]", "gin"), "migrated", True),
        (None, ("character varying(15)", "''::character varying", "btree"), "migrated", False),
        (None, ("text[]", "'{}'::text[]", "btree"), "fresh", False),
        (1, ("text", "''::text", "btree"), "fresh", True),
        (1, ("character varying(15)", "''::character varying", "btree"), "fresh", False),
        (1, ("character varying(15)", "''::character varying", "btree"), "migrated", True),
        (1, ("text", "NULL::text", "btree"), "migrated", False),
        (1, ("text[]", "'{}'::text[]", "gin"), "migrated", False),
    ],
)
def test_relation_column_check(max_select, state, mode, ok):
    rows = [{"name": "coll", "type": "base", "physical": physical(*state)}]
    assert verify_relation_columns(definitions(max_select), rows, mode)["ok"] is ok


def test_relation_column_check_hints_at_migrated_mode_only_for_varchar_single_relations():
    varchar = ("character varying(15)", "''::character varying", "btree")
    rows = [{"name": "coll", "type": "base", "physical": physical(*varchar)}]
    strict = verify_relation_columns(definitions(1), rows, "fresh")
    assert strict["ok"] is False and "--relation-columns migrated" in strict["hint"]
    assert verify_relation_columns(definitions(1), rows, "migrated")["hint"] is None
    # A wrong multiple relation is never explained away by the hint.
    assert verify_relation_columns(definitions(None), rows, "fresh")["hint"] is None


@pytest.mark.parametrize("parser", [build_parser, verify_complete_schema.build_parser])
def test_verifier_clis_are_strict_unless_migrated_is_asked(parser):
    assert parser().parse_args([]).relation_columns == "fresh"
    assert parser().parse_args(["--relation-columns", "migrated"]).relation_columns == "migrated"
    with pytest.raises(SystemExit):
        parser().parse_args(["--relation-columns", "any"])


def test_relation_column_check_needs_physical_rows_and_keeps_metadata_contract():
    report = verify_relation_columns(definitions(None), [{"name": "coll", "type": "base"}], "fresh")
    assert report["errors"] == ["coll: physical columns were not read"]
    with pytest.raises(ValueError):
        verify_relation_columns(definitions(None), [], "any")
    # Metadata keeps maxSelect null: the stored export is never rewritten.
    field = relation(None)
    field["options"]["collectionId"] = "c"
    row = {
        "id": "c",
        "name": "coll",
        "type": "base",
        "system": False,
        "schema": [field],
        "indexes": [],
        "list_rule": None,
        "view_rule": None,
        "create_rule": None,
        "update_rule": None,
        "delete_rule": None,
        "options": {},
        "physical": physical("text[]", "'{}'::text[]", "gin"),
    }
    expected = [{"id": "c", "name": "coll", "type": "base", "system": False,
                 "schema": [copy.deepcopy(field)], "options": {}}]  # fmt: skip
    assert verify(expected, [row])["relation_columns"] is None
    assert verify(expected, [row], relation_columns="fresh")["ok"] is True
    changed = copy.deepcopy(row)
    changed["schema"][0]["options"]["maxSelect"] = 2147483647
    assert verify(expected, [changed], relation_columns="fresh")["metadata_mismatches"] == [
        "coll: schema"
    ]


# --- PocketBase reference ----------------------------------------------------

# Stored scalar shapes, as PocketBase reads them for a multiple relation.
SHAPES = (
    ("wsecust00000002", ""),
    ("wsecust00000003", "[]"),
    ("wsecust00000004", "wsepdv000000001"),
    ("wsecust00000005", "wsep0001,wsep02"),
    ("wsecust00000006", "['wseuser01']"),
    ("wsecust00000007", '["a1","b2"]'),
    ("wsecust00000008", "[ ]"),
)
PROBE = r"""
migrate((db) => {
  const dao = new Dao(db);
  dao.importCollections(__WSE_COLLECTIONS__.map((c) => new Collection(c)), true);
  const save = (name, data) => {
    const record = new Record(dao.findCollectionByNameOrId(name), data);
    dao.saveRecord(record);
    return record;
  };
  dao.saveCollection(new Collection({name: "wse_probe", type: "base",
    schema: [{name: "observed", type: "text", options: {}}]}));
  // master.setup.ts l.53-60: CUSTOMER_DIVERS is created without pdv.
  save("buma_customer", {id: "CUSTOMER_DIVERS", type: "PERSONNE",
    firstname: "CLIENT DIVERS", status: true});
  // master.retour.functions.ts l.24-41, l.43-58, then linkCustomerToPdv l.96-99.
  const customer = save("buma_customer", {id: "wsecust00000001", firstname: "Client Inconnu",
    lastname: "", name: "Client Inconnu", kind: "CUSTOMER_PDV", status: true, solde: 0,
    due: 0, author: ""});
  const pdv = save("buma_pdv", {id: "wsepdv000000001", firstname: "Client Inconnu",
    lastname: "", name: "Client Inconnu", client: customer.id, status: true, daily_goal: 0,
    author: ""});
  const before = customer.getString("pdv");
  if (customer.getString("pdv") !== pdv.id) {
    customer.set("pdv", pdv.id);
    dao.saveRecord(customer);
  }
  save("wse_probe", {id: "wseprobe0000001", observed: JSON.stringify({before: before,
    after: customer.getString("pdv"), list: customer.getStringSlice("pdv")})});
  // Record.Set -> PrepareValue of each scalar shape on the multiple relation.
  for (const [id, value] of __WSE_SHAPES__) save("buma_customer", {id: id, pdv: value});
  // PocketBase's own single <-> multiple conversion of stored values
  // (daos/record_table_sync.go normalizeSingleVsMultipleFieldChanges).
  const target = dao.findCollectionByNameOrId("buma_pdv").id;
  const switching = (maxUp, maxDown) => [["wseup0000000001", "wse_up", maxUp],
    ["wsedown00000001", "wse_down", maxDown]].map(([id, name, max]) => new Collection({
      id: id, name: name, type: "base", schema: [{id: id + "r", name: "rel",
      type: "relation", options: {collectionId: target, maxSelect: max}}]}));
  dao.importCollections(switching(1, null), false);
  const insert = (table, id, value) => dao.db().newQuery("INSERT INTO " + table +
    " (id, created, updated, rel) VALUES ({:id}, '', '', {:value})")
    .bind({id: id, value: value}).execute();
  for (const [id, value] of __WSE_SHAPES__) insert("wse_up", id, value);
  for (const [id, value] of [["d1", "[]"], ["d2", '["x1"]'], ["d3", '["x1","x2"]']]) {
    insert("wse_down", id, value);
  }
  dao.importCollections(switching(null, 1), false);
  // sale.ts l.725-736 (/api2/return): new Record + saveRecord, no target check,
  // on the single relation buma_return.pdv (maxSelect 1, TEXT column).
  for (const [id, value] of __WSE_SINGLE__) save("buma_return", {id: id, pdv: value});
}, (db) => {});
"""
# Over-long ids in a single relation: kept as is by PocketBase (TEXT column).
SINGLE = (("wsereturn000001", "wseorphan0000016"), ("wsereturn000002", "wseorphan000015 "))


@pytest.fixture(params=sorted(BINARIES))
def pocketbase(request):
    value = os.environ.get(BINARIES[request.param])
    if not value:
        pytest.skip(f"{BINARIES[request.param]} is not set")
    return request.param, Path(value)


def test_pocketbase_reference_for_maxselect_null_relation_values(pocketbase, tmp_path):
    version, binary = pocketbase
    output = subprocess.run([binary, "--version"], capture_output=True, text=True, check=True)
    assert output.stdout.strip() == f"pocketbase version {version}"
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    shapes = json.dumps([list(shape) for shape in SHAPES])
    probe = (
        PROBE.replace("__WSE_SHAPES__", shapes)
        .replace("__WSE_SINGLE__", json.dumps([list(item) for item in SINGLE]))
        .replace("__WSE_COLLECTIONS__", json.dumps(export()))
    )
    (migrations / "1700000000_wse_probe.js").write_text(probe)
    data = tmp_path / "pb_data"
    completed = subprocess.run(
        [binary, "migrate", "up", "--dir", data, "--migrationsDir", migrations],
        capture_output=True,
        text=True,
        timeout=180,
    )
    # PocketBase 0.22 prints migration errors but exits 0: check the effects.
    assert completed.returncode == 0, completed.stderr
    uri = (data / "data.db").resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as db:
        applied = {row[0] for row in db.execute("SELECT file FROM _migrations")}
        assert "1700000000_wse_probe.js" in applied, completed.stdout + completed.stderr
        observed = json.loads(db.execute("SELECT observed FROM wse_probe").fetchone()[0])
        pdv = dict(db.execute("SELECT id, pdv FROM buma_customer"))
        switched_up = dict(db.execute("SELECT id, rel FROM wse_up"))
        switched_down = dict(db.execute("SELECT id, rel FROM wse_down"))
        returns = dict(db.execute("SELECT id, pdv FROM buma_return"))
    # getString of a multiple relation is "", so the source always rewrites pdv.
    assert observed == {"before": "", "after": "", "list": ["wsepdv000000001"]}
    assert pdv["wsecust00000001"] == '["wsepdv000000001"]'
    assert pdv["CUSTOMER_DIVERS"] == "[]"
    # PrepareValue (list.ToUniqueStringSlice) only decodes strings containing "[".
    assert {key: json.loads(pdv[key]) for key, _ in SHAPES} == {
        "wsecust00000002": [],
        "wsecust00000003": [],
        "wsecust00000004": ["wsepdv000000001"],
        "wsecust00000005": ["wsep0001,wsep02"],
        "wsecust00000006": ["['wseuser01']"],
        "wsecust00000007": ["a1", "b2"],
        "wsecust00000008": [],
    }
    # normalizeSingleVsMultipleFieldChanges, the reference of 1789236850 up:
    # identical for '', '[]', ids and commas; the '[' shapes 1789236850 refuses
    # get a value that depends on how they were written (wrapped or decoded).
    assert switched_up == {
        "wsecust00000002": "[]",
        "wsecust00000003": "[]",
        "wsecust00000004": '["wsepdv000000001"]',
        "wsecust00000005": '["wsep0001,wsep02"]',
        "wsecust00000006": json.dumps(["['wseuser01']"]),
        "wsecust00000007": '["a1","b2"]',
        "wsecust00000008": "[ ]",
    }
    # Multiple -> single keeps the last element; 1789236850 down refuses instead.
    assert switched_down == {"d1": "", "d2": "x1", "d3": "x2"}
    # A new PPBase database (TEXT) keeps them as is too; a migrated one keeps
    # VARCHAR(15) single relations (scripts/c11_relation_columns_runtime.py).
    assert returns == dict(SINGLE)
