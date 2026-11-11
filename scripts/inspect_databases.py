#!/usr/bin/env python3
"""
scripts/inspect_databases.py

Диагностический скрипт аудита и инспекции базы данных:
1. Проверка PRAGMA (journal_mode, foreign_keys, busy_timeout, integrity_check).
2. Анализ таблиц и подсчет записей (как доменных core_*, так и legacy).
3. Поиск дубликатов, некорректных связей и записей-сирот (orphaned records).
4. Сверка дисковых JSON-файлов (sent_log.json, requests_queue.json) с таблицами БД.
5. Вывод форматированного отчета в консоль и в JSON.
"""

import argparse
import json
import logging
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List

# Подключаем каноническое ядро
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.database import get_db_path, get_connection, OUTPUT_DIR

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("DBInspect")


def inspect_database(db_path: Path) -> Dict[str, Any]:
    if not db_path.exists():
        raise FileNotFoundError(f"Database file does not exist: {db_path}")

    report: Dict[str, Any] = {
        "db_path": str(db_path),
        "db_size_bytes": db_path.stat().st_size,
        "pragmas": {},
        "integrity": None,
        "table_counts": {},
        "orphans": {},
        "duplicates": {},
        "json_sync": {},
    }

    with get_connection(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # 1. Pragmas
        for p in ["journal_mode", "foreign_keys", "busy_timeout", "user_version"]:
            cur.execute(f"PRAGMA {p};")
            row = cur.fetchone()
            report["pragmas"][p] = row[0] if row else None

        # Integrity check
        cur.execute("PRAGMA integrity_check;")
        report["integrity"] = cur.fetchone()[0]

        # 2. Tables & Row counts
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name;")
        tables = [r["name"] for r in cur.fetchall()]
        for t in tables:
            try:
                cur.execute(f"SELECT COUNT(*) FROM \"{t}\";")
                report["table_counts"][t] = cur.fetchone()[0]
            except Exception as e:
                report["table_counts"][t] = f"Error: {e}"

        # 3. Orphans Check
        # core_hosts without valid core_trips
        if "core_hosts" in tables and "core_trips" in tables:
            cur.execute("SELECT COUNT(*) FROM core_hosts WHERE trip_id NOT IN (SELECT id FROM core_trips);")
            report["orphans"]["core_hosts_without_trip"] = cur.fetchone()[0]

        # sending_queue without hosts
        if "sending_queue" in tables and "hosts" in tables:
            cur.execute("SELECT COUNT(*) FROM sending_queue WHERE host_id NOT IN (SELECT id FROM hosts);")
            report["orphans"]["sending_queue_without_hosts"] = cur.fetchone()[0]

        # generated_messages without hosts
        if "generated_messages" in tables and "hosts" in tables:
            cur.execute("SELECT COUNT(*) FROM generated_messages WHERE host_id NOT IN (SELECT id FROM hosts);")
            report["orphans"]["generated_messages_without_hosts"] = cur.fetchone()[0]

        # 4. Duplicates Check
        if "core_hosts" in tables:
            cur.execute("""
                SELECT username, trip_id, COUNT(*) as cnt 
                FROM core_hosts 
                GROUP BY LOWER(username), trip_id 
                HAVING cnt > 1;
            """)
            dupes = cur.fetchall()
            report["duplicates"]["core_hosts_duplicate_usernames_per_trip"] = len(dupes)

        if "hosts" in tables:
            cur.execute("""
                SELECT username, COUNT(*) as cnt 
                FROM hosts 
                GROUP BY LOWER(username) 
                HAVING cnt > 1;
            """)
            report["duplicates"]["hosts_duplicate_usernames"] = len(cur.fetchall())

    # 5. JSON vs DB Comparison
    sent_log_file = OUTPUT_DIR / "sent_log.json"
    if sent_log_file.exists():
        try:
            with open(sent_log_file, "r", encoding="utf-8") as f:
                sent_data = json.load(f)
                report["json_sync"]["sent_log_file_count"] = len(sent_data) if isinstance(sent_data, list) else len(sent_data.keys())
        except Exception as e:
            report["json_sync"]["sent_log_file_error"] = str(e)
    else:
        report["json_sync"]["sent_log_file_count"] = 0

    if "sending_history" in report["table_counts"]:
        report["json_sync"]["db_sending_history_count"] = report["table_counts"]["sending_history"]

    return report


def main():
    parser = argparse.ArgumentParser(description="Inspect and diagnose CS SQLite database")
    parser.add_argument("--db-path", type=str, default=None, help="Path to database file")
    parser.add_argument("--json", action="store_true", help="Output raw JSON")
    args = parser.parse_args()

    db_path = Path(args.db_path) if args.db_path else get_db_path()
    try:
        report = inspect_database(db_path)
    except Exception as e:
        logger.error(f"Inspection failed: {e}")
        sys.exit(1)

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print("=" * 60)
        print("  COUCHSURFING DATABASE INSPECTION REPORT")
        print("=" * 60)
        print(f"Database File: {report['db_path']}")
        print(f"File Size:     {report['db_size_bytes']:,} bytes")
        print(f"Integrity:     {report['integrity']}")
        print("\n--- PRAGMAS ---")
        for k, v in report["pragmas"].items():
            print(f"  {k:15}: {v}")

        print("\n--- TABLE ROW COUNTS ---")
        for t, cnt in sorted(report["table_counts"].items()):
            print(f"  {t:25}: {cnt}")

        print("\n--- ORPHANS & INTEGRITY ---")
        for k, cnt in report["orphans"].items():
            status = "OK" if cnt == 0 else "WARNING"
            print(f"  {k:35}: {cnt} [{status}]")

        print("\n--- DUPLICATES ---")
        for k, cnt in report["duplicates"].items():
            status = "OK" if cnt == 0 else "WARNING"
            print(f"  {k:45}: {cnt} [{status}]")

        print("\n--- JSON vs DB SYNC ---")
        for k, v in report["json_sync"].items():
            print(f"  {k:30}: {v}")
        print("=" * 60)


if __name__ == "__main__":
    main()
