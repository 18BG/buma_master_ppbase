"""Build synthetic C11 input with PocketBase itself, without BUMA bootstraps."""

from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path


def create_fixture(root: Path, binary: Path, schema: Path) -> Path:
    """The caller owns a new disposable directory; no real backup is read."""
    root.mkdir(exist_ok=False, parents=True)
    migrations = root / "migrations"
    migrations.mkdir()
    definitions = json.loads(schema.read_text())
    fixture_js = r"""
    const save=(name, data, password)=>{
      const r=new Record(dao.findCollectionByNameOrId(name),data);
      if(password) r.setPassword(password);
      dao.saveRecord(r); return r;
    };
    save("roles", {id:"c11role00000001",name:"C11 synthetic",
      permission:{isAdmin:true},status:true});
    save("users", {id:"c11user00000001",username:"c11admin",email:"c11@example.invalid",
      role:"c11role00000001",name:"C11 fixture",verified:true}, "C11-test-password");
    save("users", {id:"c11user00000002",username:"c11denied",
      email:"denied@example.invalid",name:"C11 denied"}, "C11-test-password");
    save("agent_users", {id:"c11agentuser001",username:"c11agent",
      email:"agent@example.invalid"}, "C11-test-password");
    save("buma_caisse_access", {id:"c11access000001",user:"c11user00000001"});
    save("buma_article", {id:"c11art000000001",designation:"C11 decimal article",
      sell_price:12.75,buy_price:8.25,status:true,kind:"AUTRES",
      specification:{nested:[false,0,"é"]},medias:"proof.bin"});
    save("buma_ressources", {id:"c11file00000001",name:"C11 files",
      file:["one.bin","two.bin"],status:true,author:"c11user00000001"});
    save("buma_supplier", {id:"c11supp00000001",firstname:"C11 preserved",
      author:"c11user00000001",status:false,solde:-17.25});
    save("buma_transaction", {id:"c11tx000000001",article:"c11art000000001",
      author:"c11user00000001",price:12.75,quantity:2.5,status:true,type:"VENTE",
      system_id:"idem:c11:proof",notes:"C11 synthetic"});
    dao.db().newQuery("UPDATE users SET rowid=41 WHERE id='c11user00000001'").execute();
    dao.db().newQuery("UPDATE users SET rowid=93 WHERE id='c11user00000002'").execute();
    dao.db().newQuery("UPDATE buma_article SET rowid=7001").execute();
    dao.db().newQuery("UPDATE buma_supplier SET rowid=-17").execute();
    dao.db().newQuery("UPDATE buma_supplier SET created='2024-02-29 12:34:56.789Z',"
      +"updated='2024-03-01 00:00:00.000Z'").execute();
    """
    # File values are inserted below: PB correctly rejects an absent upload in
    # a Record form, while the stopped snapshot must reference actual bytes.
    fixture_js = fixture_js.replace(',medias:"proof.bin"', "")
    fixture_js = fixture_js.replace('file:["one.bin","two.bin"],', "")
    (migrations / "1789236900_c11_fixture.js").write_text(
        "migrate((db)=>{const dao=new Dao(db);dao.importCollections("
        + json.dumps(definitions)
        + ".map(c=>new Collection(c)),true);"
        + fixture_js
        + "},()=>{});\n"
    )
    command = [
        str(binary),
        "migrate",
        "up",
        "--dir",
        str(root / "data"),
        "--migrationsDir",
        str(migrations),
    ]
    completed = subprocess.run(command, capture_output=True, text=True)
    if completed.returncode:
        raise RuntimeError(completed.stdout + completed.stderr)
    data = root / "data"
    with sqlite3.connect(data / "data.db") as db:
        db.execute("UPDATE buma_article SET medias='proof.bin'")
        db.execute('UPDATE buma_ressources SET file=\'["one.bin","two.bin"]\'')
        db.commit()
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        db.execute("PRAGMA journal_mode=DELETE")
    for name, record, filename, content in (
        ("buma_article", "c11art000000001", "proof.bin", b"C11\x00\xff\x01original"),
        ("buma_ressources", "c11file00000001", "one.bin", b"C11 file one"),
        ("buma_ressources", "c11file00000001", "two.bin", b"C11 file two"),
    ):
        collection_id = next(c["id"] for c in definitions if c["name"] == name)
        target = data / "storage" / collection_id / record / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    return data
