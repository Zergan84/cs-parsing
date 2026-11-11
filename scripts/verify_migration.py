#!/usr/bin/env python3
"""
scripts/verify_migration.py

Скрипт строгой верификации результатов консолидации и миграции базы данных:
1. PRAGMA integrity_check и PRAGMA foreign_key_check.
2. Проверка нулевых потерь данных:
   - Все user_trips присутствуют в core_trips.
   - Все хосты из hosts учтены в core_hosts.
   - Неизменяемые снимки core_host_snapshots сохранены.
   - Записи отправки sent_log.json перенесены в sending_history.
3. Проверка инвариантов бизнес-состояний:
   - Ни один хост со статусом 'sent' не находится в статусе 'drafted' или 'approved'.
   - Все хосты в inbox_conversations имеют статус 'inbox' в core_hosts.
   - Отсутствие сирот и дубликатов первичных ключей.
"""

import argparse
import json
import logging
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.database import get_connection, get_db_path, OUTPUT_DIR

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("VerifyMigration")


def verify_migration(db_path: Path) -> Dict[str, Any]:
    if not db_path.exists():
        raise FileNotFoundError(f"Database not found: {db_path}")

    report: Dict[str, Any] = {
        "db_path": str(db_path),
        "checks_passed": True,
        "integrity_check": None,
        "foreign_key_violations": [],
        "invariants": {},
        "data_loss_checks": {},
    }

    with get_connection(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # 1. PRAGMA integrity_check
        cur.execute("PRAGMA integrity_check;")
        int_res = cur.fetchone()[0]
        report["integrity_check"] = int_res
        if int_res != "ok":
            report["checks_passed"] = False

        # 2. PRAGMA foreign_key_check
        cur.execute("PRAGMA foreign_key_check;")
        fk_violations = [dict(r) for r in cur.fetchall()]
        report["foreign_key_violations"] = fk_violations
        if fk_violations:
            report["checks_passed"] = False

        # 3. Data Loss Checks
        # user_trips vs core_trips
        cur.execute("SELECT COUNT(*) FROM user_trips WHERE trip_id NOT IN (SELECT id FROM core_trips);")
        missing_trips = cur.fetchone()[0]
        report["data_loss_checks"]["missing_trips_in_core"] = missing_trips
        if missing_trips > 0:
            report["checks_passed"] = False

        # Total hosts check
        cur.execute("SELECT COUNT(DISTINCT username) FROM hosts;")
        total_legacy_hosts = cur.fetchone()[0]
        cur.execute("SELECT COUNT(DISTINCT username) FROM core_hosts;")
        total_core_hosts = cur.fetchone()[0]
        report["data_loss_checks"]["legacy_unique_hosts"] = total_legacy_hosts
        report["data_loss_checks"]["core_unique_hosts"] = total_core_hosts
        if total_core_hosts < total_legacy_hosts:
            report["checks_passed"] = False
            report["data_loss_checks"]["missing_hosts_count"] = total_legacy_hosts - total_core_hosts

        # Sent history vs sent_log.json
        cur.execute("SELECT COUNT(*) FROM sending_history WHERE status = 'sent';")
        db_sent_count = cur.fetchone()[0]
        report["data_loss_checks"]["db_sent_count"] = db_sent_count

        sent_file = OUTPUT_DIR / "sent_log.json"
        if sent_file.exists():
            with open(sent_file, "r", encoding="utf-8") as f:
                raw_sent = json.load(f)
                file_sent_count = len(raw_sent) if isinstance(raw_sent, list) else len(raw_sent.keys())
                report["data_loss_checks"]["json_file_sent_count"] = file_sent_count
                if db_sent_count < file_sent_count:
                    report["checks_passed"] = False
                    report["data_loss_checks"]["missing_sent_records"] = file_sent_count - db_sent_count

        # 4. Invariants Check
        # Invariant 1: Sent hosts must not have drafted or approved state in core_hosts
        cur.execute("""
            SELECT COUNT(*) FROM core_hosts 
            WHERE state IN ('drafted', 'approved') 
              AND (host_id IN (SELECT host_id FROM sending_history) 
                   OR LOWER(username) IN (SELECT LOWER(username) FROM sending_history));
        """)
        sent_in_draft = cur.fetchone()[0]
        report["invariants"]["sent_hosts_in_draft_or_approved"] = sent_in_draft
        if sent_in_draft > 0:
            report["checks_passed"] = False

        # Invariant 2: No duplicate primary keys in core_hosts
        cur.execute("SELECT trip_id, host_id, COUNT(*) as cnt FROM core_hosts GROUP BY trip_id, host_id HAVING cnt > 1;")
        pk_dupes = len(cur.fetchall())
        report["invariants"]["core_hosts_pk_duplicates"] = pk_dupes
        if pk_dupes > 0:
            report["checks_passed"] = False

        # Invariant 3: Snapshots exist for all core_hosts
        cur.execute("""
            SELECT COUNT(*) FROM core_hosts ch
            LEFT JOIN core_host_snapshots chs ON ch.trip_id = chs.trip_id AND ch.host_id = chs.host_id
            WHERE chs.host_id IS NULL;
        """)
        hosts_without_snapshots = cur.fetchone()[0]
        report["invariants"]["core_hosts_without_snapshot"] = hosts_without_snapshots

        # Invariant 4: Inbox conversations match core_hosts
        cur.execute("""
            SELECT COUNT(*) FROM inbox_conversations ic
            WHERE LOWER(COALESCE(ic.host_username, '')) NOT IN (SELECT LOWER(username) FROM core_hosts WHERE state = 'inbox');
        """)
        inbox_mismatches = cur.fetchone()[0]
        report["invariants"]["inbox_not_marked_in_core_hosts"] = inbox_mismatches
        if inbox_mismatches > 0:
            report["checks_passed"] = False

    return report


def main():
    parser = argparse.ArgumentParser(description="Verify consolidation and migration integrity")
    parser.add_argument("--db-path", type=str, default=None, help="Database file path")
    args = parser.parse_args()

    db_path = Path(args.db_path) if args.db_path else get_db_path()
    try:
        report = verify_migration(db_path)
    except Exception as e:
        logger.error(f"Verification script failed: {e}")
        sys.exit(1)

    print("\n" + "=" * 60)
    print("  MIGRATION INTEGRITY & INVARIANTS VERIFICATION")
    print("=" * 60)
    print(f"Database:        {report['db_path']}")
    print(f"Overall Status:  {'PASSED [SUCCESS]' if report['checks_passed'] else 'FAILED [VIOLATIONS]'}")
    print(f"Integrity Check: {report['integrity_check']}")
    print(f"FK Violations:   {len(report['foreign_key_violations'])}")

    print("\n--- ZERO DATA LOSS CHECKS ---")
    for k, v in report["data_loss_checks"].items():
        print(f"  {k:35}: {v}")

    print("\n--- ARCHITECTURAL INVARIANTS ---")
    for k, v in report["invariants"].items():
        status = "OK" if v == 0 else "VIOLATION"
        print(f"  {k:35}: {v} [{status}]")
    print("=" * 60)

    if not report["checks_passed"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
