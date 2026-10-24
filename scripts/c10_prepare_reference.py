#!/usr/bin/env python3
"""Prepare disposable PB 0.22 hooks from the frozen source, never production.

Run the printed command with a PocketBase 0.22.27 binary. The fixture helper
exists only in the temporary reference directory, not in the PPBase app.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rowid-only", action="store_true")
    parser.add_argument("--sql-only", action="store_true")
    args = parser.parse_args()
    if args.rowid_only and args.sql_only:
        parser.error("Choose one isolated reference mode")
    project = Path(__file__).resolve().parents[1]
    source = project.parent / "buma_master_hooks"
    commit = subprocess.check_output(
        ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
    ).strip()
    assert commit == "530018cd20c2acbe88f38b3639e500b5271e2eb2"
    root = Path(tempfile.mkdtemp(prefix="buma-c10-reference-"))
    hooks, migrations = root / "hooks", root / "migrations"
    hooks.mkdir()
    migrations.mkdir()
    # All source modules are transpiled; only the explicit legacy modules below
    # are registered. No setup, job or external integration is launched.
    transpiler = project.parent / "buma_boutique_web/node_modules/typescript"
    script = r"""const fs=require('fs'), path=require('path'), ts=require(process.argv[1]);
    for(const file of fs.readdirSync(process.argv[2])) {
      if(!file.endsWith('.ts') || file.endsWith('.pb.ts')) continue;
      const text=fs.readFileSync(path.join(process.argv[2],file),'utf8');
      const result=ts.transpileModule(text,{compilerOptions:{target:ts.ScriptTarget.ES2020,
        module:ts.ModuleKind.CommonJS}}).outputText;
      fs.writeFileSync(path.join(process.argv[3],file.replace(/\.ts$/,'.js')),result);
    }
    const index=fs.readFileSync(path.join(process.argv[2],'index.pb.ts'),'utf8');
    const sql=index.slice(index.indexOf('routerAdd("POST", "/sql"'));
    fs.writeFileSync(path.join(process.argv[3],'sql-reference.pb.js'),
      ts.transpileModule(sql,{compilerOptions:{target:ts.ScriptTarget.ES2020,
        module:ts.ModuleKind.CommonJS}}).outputText);
    """
    subprocess.run(
        [
            "/opt/homebrew/bin/node",
            "-e",
            script,
            str(transpiler),
            str(source / "src/entries"),
            str(hooks),
        ],
        check=True,
    )
    schema = json.loads(
        Path(
            os.environ.get(
                "BUMA_SCHEMA_EXPORT",
                "/Users/omarsisss/Downloads/buma-master-pb_schema-corrected.json",
            )
        ).read_text()
    )
    (migrations / "1789236800_reference.js").write_text(
        "migrate((db) => {const dao=new Dao(db);dao.importCollections("
        + json.dumps(schema)
        + ".map(c=>new Collection(c)),true);}, () => {});\n"
    )
    (hooks / "reference.pb.js").write_text("""
for (const module of ["caisse", "customer", "sale", "purchase"]) {
  require(`${__hooks}/${module}.js`).runMiddleware();
}
for (const module of ["master.pdv", "master.pilotage", "master.rupture"]) {
  require(`${__hooks}/${module}.js`).run();
}
routerAdd("POST", "/__c10/fixture", (e) => {
  const data=$apis.requestInfo(e).data, dao=$app.dao();
  if(data.action==="list") return e.json(200,dao.findRecordsByExpr(data.table));
  if(data.action==="delete") {
    dao.deleteRecord(dao.findRecordById(data.table,data.id)); return e.json(200,{deleted:true});
  }
  if(data.action==="query") {
    const result=arrayOf(new DynamicModel(data.fields));
    dao.db().newQuery(data.query).all(result);return e.json(200,result);
  }
  if(data.action==="read") return e.json(200,dao.findRecordById(data.table,data.id));
  if(data.action==="update") {
    const r=dao.findRecordById(data.table,data.id);r.load(data.values);dao.saveRecord(r);
    return e.json(200,r);
  }
  const r=new Record(dao.findCollectionByNameOrId(data.table),data.values);
  if(data.table==="users" || data.table==="agent_users") r.setPassword("abcde123");
  dao.saveRecord(r);return e.json(200,r);
});
""")
    (root / "provenance.json").write_text(
        json.dumps({"source_commit": commit, "reference_version": "0.22.27", "hooks": str(hooks)})
    )
    if args.sql_only:
        helper = (hooks / "reference.pb.js").read_text()
        helper = helper[helper.index('routerAdd("POST", "/__c10/fixture"') :]
        helper = helper.replace(
            '  if(data.action==="list")',
            """  if(data.action==="collection")
    return e.json(200,dao.findCollectionByNameOrId(data.table));
  if(data.action==="restore_collection") {
    const c=dao.findCollectionByNameOrId(data.table);
    c.schema=new Schema(data.snapshot.schema);c.options=data.snapshot.options;
    dao.saveCollection(c);
    dao.nonconcurrentDB().newQuery(
      "UPDATE _collections SET schema={:s}, options={:o}, updated={:d} WHERE id={:id}")
      .bind({s:JSON.stringify(data.snapshot.schema),o:JSON.stringify(data.snapshot.options),
             d:data.snapshot.updated,id:c.id}).execute();
    return e.json(200,{restored:true});
  }
  if(data.action==="list")""",
        )
        # Run the actual frozen controller, including its response serialization.
        (hooks / "reference.pb.js").write_text(
            "require(`${__hooks}/master.article.js`).run();\n" + helper
        )
    if args.rowid_only:
        # Only the copied schema and this fixture helper run. No BUMA module,
        # SQL endpoint, job or external integration is loaded for this probe.
        (hooks / "sql-reference.pb.js").unlink()
        helper = (hooks / "reference.pb.js").read_text()
        helper = helper[helper.index('routerAdd("POST", "/__c10/fixture"') :]
        helper = helper.replace('"/__c10/fixture"', '"/__rowid"')
        marker = '  if(data.action==="list")'
        probes = """  if(data.action==="version") {
    const out=new DynamicModel({version:""});
    dao.db().newQuery("SELECT sqlite_version() AS version").one(out); return e.json(200,out);
  }
  if(data.action==="assign_rowid") {
    if (!["buma_article","users"].includes(data.table)) throw new Error("Invalid fixture table");
    dao.db().newQuery("UPDATE "+data.table+" SET rowid={:n} WHERE id={:id}")
      .bind({n:data.value,id:data.id}).execute();return e.json(200,{ok:true});
  }
  if(data.action==="rollback") {
    let attempted;
    try {dao.runInTransaction(tx=>{
      const r=new Record(tx.findCollectionByNameOrId(data.table),data.values);tx.saveRecord(r);
      const out=new DynamicModel({n:0});
      tx.db().newQuery("SELECT rowid AS n FROM "+data.table+" WHERE id={:id}")
        .bind({id:r.id}).one(out);attempted=out.n;throw new Error("forced rollback");
    });}catch(err){}
    return e.json(200,{attempted});
  }
"""
        helper = helper.replace(marker, probes + marker)
        (hooks / "reference.pb.js").write_text(helper)
    print(root)


if __name__ == "__main__":
    main()
