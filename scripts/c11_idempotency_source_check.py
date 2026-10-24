"""Read-only D-0022 evidence on a closed backup copy and the frozen source.

Aggregates only: counts, dates, migration and index names; no record values.
Run as python -m scripts.c11_idempotency_source_check --data ... --report ...
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from scripts.c11_import import (
    ImportBlocked,
    digest,
    idempotency_key_conflicts,
    snapshot,
    write_json_new,
)

SOURCE_MIGRATION = "1785599959_updated_buma_transaction.js"
MOBILE_GROUPS = (
    "SELECT type,author,mobile_message_id FROM buma_transaction WHERE mobile_message_id!='' "
    "GROUP BY type,author,mobile_message_id HAVING COUNT(*)>1"
)
# The source lookup takes the first row SQLite yields (rowid order); PPBase
# orders by created. Per group: same untied row, tie on the first created, or
# a first-rowid row that is not the earliest created.
FIRST_ROW_BREAKDOWN = (
    "WITH r AS (SELECT t._rowid_ AS rid,t.created,t.type,t.author,t.mobile_message_id "
    f"FROM buma_transaction t JOIN ({MOBILE_GROUPS}) g ON g.type=t.type AND g.author=t.author "
    "AND g.mobile_message_id=t.mobile_message_id), "
    "f AS (SELECT type,author,mobile_message_id,MIN(rid) rid,MIN(created) created "
    "FROM r GROUP BY type,author,mobile_message_id), "
    "c AS (SELECT (SELECT created FROM r WHERE r.rid=f.rid) first_rowid_created,f.created,"
    "(SELECT COUNT(*) FROM r x WHERE x.type=f.type AND x.author=f.author "
    "AND x.mobile_message_id=f.mobile_message_id AND x.created=f.created) ties FROM f) "
    "SELECT SUM(first_rowid_created=created AND ties=1),"
    "SUM(first_rowid_created=created AND ties>1),SUM(first_rowid_created>created) FROM c"
)


def _scalar(db, sql: str):
    return db.execute(sql).fetchone()[0]


def check(data: Path, source_migrations: Path) -> dict:
    before = digest(data / "data.db")
    source = sorted(path.name for path in source_migrations.glob("*.js"))
    with snapshot(data) as db:
        applied = {row[0] for row in db.execute("SELECT file FROM _migrations")}
        rows, oldest, newest = db.execute(
            "SELECT COUNT(*),MIN(t.created),MAX(t.created) FROM buma_transaction t "
            f"JOIN ({MOBILE_GROUPS}) g ON g.type=t.type AND g.author=t.author "
            "AND g.mobile_message_id=t.mobile_message_id"
        ).fetchone()
        same, tied, inverted = db.execute(FIRST_ROW_BREAKDOWN).fetchone()
        report = {
            "scope": "d0022_read_only_aggregates",
            "production_authorized": False,
            "data_db_sha256": before,
            "source_migrations": len(source),
            "source_migrations_not_applied_in_snapshot": [n for n in source if n not in applied],
            "source_index_migration_applied": SOURCE_MIGRATION in applied,
            "buma_transaction_physical_indexes": [
                row[0]
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='index' "
                    "AND tbl_name='buma_transaction' ORDER BY name"
                )
            ],
            "system_id_nonempty_rows": _scalar(
                db, "SELECT COUNT(*) FROM buma_transaction WHERE system_id!=''"
            ),
            "idem_prefixed_rows": _scalar(
                db, "SELECT COUNT(*) FROM buma_transaction WHERE system_id LIKE 'idem:%'"
            ),
            "source_index_conflict_groups": idempotency_key_conflicts(db),
            "mobile_duplicate_groups": _scalar(db, f"SELECT COUNT(*) FROM ({MOBILE_GROUPS})"),
            "mobile_duplicate_rows": rows,
            "mobile_duplicate_oldest_created": oldest,
            "mobile_duplicate_newest_created": newest,
            "mobile_duplicate_first_row_by_rowid_vs_created": {
                "same_untied_row": same,
                "tie_on_first_created": tied,
                "first_rowid_not_earliest_created": inverted,
            },
        }
    if digest(data / "data.db") != before:
        raise ImportBlocked("source_changed_during_check")
    report["source_unchanged"] = True
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, required=True, help="Closed backup directory")
    parser.add_argument("--source-migrations", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True, help="New private JSON report")
    args = parser.parse_args()
    report = check(args.data, args.source_migrations)
    write_json_new(args.report, report)
    print(json.dumps({"report": str(args.report), "source_unchanged": report["source_unchanged"]}))


if __name__ == "__main__":
    main()
