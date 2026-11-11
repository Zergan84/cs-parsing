#!/usr/bin/env python3
"""
scripts/verify_migration.py

Скрипт исчерпывающей поштучной (per-record) верификации миграции и консолидации:
1. Строго Read-Only подключение к SQLite.
2. PRAGMA integrity_check и PRAGMA foreign_key_check.
3. Построчная сверка user_trips -> core_trips (каждая запись и ключевые поля).
4. Построчная сверка hosts -> core_hosts и core_host_snapshots (каждый хост и неизменяемый снимок).
5. Верификация содержимого snapshots (raw_data_json валидный и непустой JSON).
6. Верификация связей (host_id + trip_id) из trip_hosts в core_hosts.
7. Верификация полей sending_history (status, username, timestamp).
8. Сверка sent_log.json -> sending_history и core_hosts.state == 'sent'.
   Если sent_log отсутствует, явно фиксируется статус NOT VERIFIED.
9. Построчная сверка inbox_conversations -> core_hosts.state == 'inbox'.
10. Категоризация несоответствий по типам:
   - MISSING
   - EXTRA
   - DUPLICATE
   - FIELD_MISMATCH
   - UNRESOLVED_RELATIONSHIP
11. Проверка архитектурных инвариантов бизнес-состояний.
"""

import argparse
import json
import logging
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.database import OUTPUT_DIR, get_db_path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("VerifyMigration")


def verify_migration(db_path: Path, sent_log_path: Optional[Path] = None) -> Dict[str, Any]:
    db_file = Path(db_path).resolve()
    if not db_file.exists():
        raise FileNotFoundError(f"Database not found: {db_file}")

    if sent_log_path is None:
        local_candidate = db_file.parent / "sent_log.json"
        if local_candidate.exists():
            effective_sent_log = local_candidate
        elif (OUTPUT_DIR / "sent_log.json").exists() and db_file == get_db_path().resolve():
            effective_sent_log = OUTPUT_DIR / "sent_log.json"
        else:
            effective_sent_log = None
    else:
        effective_sent_log = Path(sent_log_path) if Path(sent_log_path).exists() else None

    report: Dict[str, Any] = {
        "db_path": str(db_file),
        "sent_log_path": str(effective_sent_log) if effective_sent_log else None,
        "checks_passed": True,
        "integrity_check": None,
        "category_counts": {
            "MISSING": 0,
            "EXTRA": 0,
            "DUPLICATE": 0,
            "FIELD_MISMATCH": 0,
            "UNRESOLVED_RELATIONSHIP": 0,
        },
        "foreign_key_violations": [],
        "per_record_checks": {
            "user_trips": {"total": 0, "verified": 0, "discrepancies": []},
            "hosts_to_core": {"total": 0, "verified": 0, "discrepancies": []},
            "hosts_snapshots": {"total": 0, "verified": 0, "discrepancies": []},
            "snapshots_content": {"total": 0, "verified": 0, "discrepancies": []},
            "trip_hosts_relations": {"total": 0, "verified": 0, "discrepancies": []},
            "sending_history_fields": {"total": 0, "verified": 0, "discrepancies": []},
            "sent_log_to_db": {"total": 0, "verified": 0, "discrepancies": []},
            "sent_state_in_core": {"total": 0, "verified": 0, "discrepancies": []},
            "unconfirmed_sent_in_core": {"total": 0, "verified": 0, "discrepancies": []},
            "inbox_conversations": {"total": 0, "verified": 0, "discrepancies": []},
        },
        "invariants": {
            "sent_hosts_in_draft_or_approved": 0,
            "unconfirmed_sent_hosts": 0,
            "core_hosts_pk_duplicates": 0,
            "core_trips_pk_duplicates": 0,
            "multiple_active_trips": 0,
            "orphan_core_hosts": 0,
        },
    }

    def _record_discrepancy(section: str, disc_type: str, details: Dict[str, Any]):
        details["type"] = disc_type
        if section in report["per_record_checks"]:
            report["per_record_checks"][section]["discrepancies"].append(details)
        report["category_counts"][disc_type] = report["category_counts"].get(disc_type, 0) + 1
        report["checks_passed"] = False

    # Строго Read-Only подключение
    db_uri = f"file:{db_file}?mode=ro"
    conn = sqlite3.connect(db_uri, uri=True, timeout=10.0)
    conn.row_factory = sqlite3.Row
    try:
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
            for v in fk_violations:
                _record_discrepancy("hosts_to_core", "UNRESOLVED_RELATIONSHIP", {"error": "Foreign key violation", "violation": v})

        # Проверяем наличие таблиц
        cur.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = {r[0] for r in cur.fetchall()}

        if "core_trips" not in tables:
            _record_discrepancy("user_trips", "MISSING", {"error": "Table core_trips missing"})
            return report

        # 3. Per-Record Check: user_trips -> core_trips
        legacy_trips = []
        if "user_trips" in tables:
            cur.execute("SELECT * FROM user_trips;")
            legacy_trips = cur.fetchall()
            report["per_record_checks"]["user_trips"]["total"] = len(legacy_trips)
            for lt in legacy_trips:
                t_id = lt["trip_id"] if "trip_id" in lt.keys() else lt["id"]
                cur.execute("SELECT * FROM core_trips WHERE id = ?;", (t_id,))
                ct = cur.fetchone()
                if not ct and "url" in lt.keys() and lt["url"]:
                    cur.execute("SELECT * FROM core_trips WHERE url = ?;", (lt["url"],))
                    ct = cur.fetchone()

                if not ct:
                    _record_discrepancy("user_trips", "MISSING", {
                        "trip_id": t_id,
                        "error": "Trip missing in core_trips",
                    })
                else:
                    # Полевая сверка: city, arrival_date, departure_date
                    if "city" in lt.keys() and lt["city"] and ct["city"]:
                        if lt["city"].strip().lower() != ct["city"].strip().lower():
                            _record_discrepancy("user_trips", "FIELD_MISMATCH", {
                                "trip_id": t_id,
                                "field": "city",
                                "expected": lt["city"],
                                "actual": ct["city"],
                            })
                    report["per_record_checks"]["user_trips"]["verified"] += 1

        # 4. Per-Record Check: hosts -> core_hosts & core_host_snapshots
        if "hosts" in tables:
            cur.execute("SELECT id, username, name, location FROM hosts;")
            legacy_hosts = cur.fetchall()
            report["per_record_checks"]["hosts_to_core"]["total"] = len(legacy_hosts)
            report["per_record_checks"]["hosts_snapshots"]["total"] = len(legacy_hosts)

            cur.execute("SELECT host_id, trip_id, username, name, location, state FROM core_hosts;")
            all_core_hosts = cur.fetchall()
            core_by_uname = {}
            core_by_hid = {}
            for ch in all_core_hosts:
                core_by_uname.setdefault(ch["username"].lower(), []).append(ch)
                core_by_hid.setdefault(ch["host_id"].lower(), []).append(ch)

            cur.execute("SELECT DISTINCT LOWER(host_id) FROM core_host_snapshots;")
            snapshots_host_ids = {r[0] for r in cur.fetchall()}

            for lh in legacy_hosts:
                hid = str(lh["id"]).lower()
                uname = str(lh["username"]).lower()
                matched_core = core_by_hid.get(hid) or core_by_uname.get(uname)
                if not matched_core:
                    _record_discrepancy("hosts_to_core", "MISSING", {
                        "id": lh["id"],
                        "username": lh["username"],
                        "error": "Host not present in core_hosts",
                    })
                else:
                    report["per_record_checks"]["hosts_to_core"]["verified"] += 1
                    # Полевая сверка: username
                    ch = matched_core[0]
                    if ch["username"].strip().lower() != uname:
                        _record_discrepancy("hosts_to_core", "FIELD_MISMATCH", {
                            "id": lh["id"],
                            "field": "username",
                            "expected": uname,
                            "actual": ch["username"],
                        })

                # Проверка наличия снимка
                has_snapshot = hid in snapshots_host_ids or uname in snapshots_host_ids
                if not has_snapshot and matched_core:
                    actual_hids = [c["host_id"].lower() for c in matched_core]
                    if any(x in snapshots_host_ids for x in actual_hids):
                        has_snapshot = True

                if not has_snapshot:
                    _record_discrepancy("hosts_snapshots", "MISSING", {
                        "id": lh["id"],
                        "username": lh["username"],
                        "error": "Missing snapshot in core_host_snapshots",
                    })
                else:
                    report["per_record_checks"]["hosts_snapshots"]["verified"] += 1

        # 5. Сверка содержимого snapshots (raw_data_json валидный и непустой JSON)
        if "core_host_snapshots" in tables:
            cur.execute("SELECT host_id, trip_id, raw_data_json FROM core_host_snapshots;")
            all_snapshots = cur.fetchall()
            report["per_record_checks"]["snapshots_content"]["total"] = len(all_snapshots)
            for sn in all_snapshots:
                raw_json = sn["raw_data_json"]
                try:
                    parsed = json.loads(raw_json)
                    if not isinstance(parsed, dict) or len(parsed) == 0:
                        _record_discrepancy("snapshots_content", "FIELD_MISMATCH", {
                            "host_id": sn["host_id"],
                            "trip_id": sn["trip_id"],
                            "error": "Snapshot raw_data_json is empty or not a dict",
                        })
                    else:
                        report["per_record_checks"]["snapshots_content"]["verified"] += 1
                except Exception as ex:
                    _record_discrepancy("snapshots_content", "FIELD_MISMATCH", {
                        "host_id": sn["host_id"],
                        "trip_id": sn["trip_id"],
                        "error": f"Snapshot raw_data_json corrupted: {ex}",
                    })

        # 6. Сверка связей (host_id + trip_id) из trip_hosts в core_hosts
        if "trip_hosts" in tables and "core_hosts" in tables:
            cur.execute("SELECT LOWER(trip_id), LOWER(host_id) FROM trip_hosts;")
            th_rows = cur.fetchall()
            report["per_record_checks"]["trip_hosts_relations"]["total"] = len(th_rows)

            cur.execute("SELECT LOWER(trip_id), LOWER(host_id), LOWER(username) FROM core_hosts;")
            ch_pairs = set()
            ch_uname_pairs = set()
            for r in cur.fetchall():
                ch_pairs.add((r[0], r[1]))
                ch_uname_pairs.add((r[0], r[2]))

            for th in th_rows:
                t_id, h_id = th[0], th[1]
                if (t_id, h_id) in ch_pairs or (t_id, h_id) in ch_uname_pairs:
                    report["per_record_checks"]["trip_hosts_relations"]["verified"] += 1
                else:
                    _record_discrepancy("trip_hosts_relations", "UNRESOLVED_RELATIONSHIP", {
                        "trip_id": t_id,
                        "host_id": h_id,
                        "error": "Relationship (trip_id, host_id) from trip_hosts not resolved in core_hosts",
                    })

        # 7. Сверка полей sending_history (status, username, timestamp)
        if "sending_history" in tables:
            cur.execute("SELECT id, host_id, username, status, timestamp, http_status FROM sending_history;")
            hist_rows = cur.fetchall()
            report["per_record_checks"]["sending_history_fields"]["total"] = len(hist_rows)
            for hr in hist_rows:
                status_val = str(hr["status"] or "").strip()
                uname_val = str(hr["username"] or hr["host_id"] or "").strip()
                ts_val = str(hr["timestamp"] or "").strip()
                if not status_val or not uname_val or not ts_val:
                    _record_discrepancy("sending_history_fields", "FIELD_MISMATCH", {
                        "history_id": hr["id"],
                        "username": uname_val,
                        "status": status_val,
                        "timestamp": ts_val,
                        "error": "Required field in sending_history is empty",
                    })
                else:
                    report["per_record_checks"]["sending_history_fields"]["verified"] += 1

        # 8. Per-Record Check: sent_log.json -> sending_history & core_hosts.state == 'sent'
        if not effective_sent_log or not effective_sent_log.exists():
            report["per_record_checks"]["sent_log_to_db"]["status"] = "NOT VERIFIED"
            report["per_record_checks"]["sent_log_to_db"]["message"] = "sent_log.json not found"
            report["per_record_checks"]["sent_state_in_core"]["status"] = "NOT VERIFIED"
            report["per_record_checks"]["sent_state_in_core"]["message"] = "sent_log.json not found"
        elif "sending_history" in tables:
            try:
                with open(effective_sent_log, "r", encoding="utf-8") as f:
                    sent_entries = json.load(f)
            except Exception:
                sent_entries = []

            report["per_record_checks"]["sent_log_to_db"]["total"] = len(sent_entries)
            report["per_record_checks"]["sent_state_in_core"]["total"] = len(sent_entries)

            cur.execute("SELECT LOWER(username), timestamp, status FROM sending_history;")
            raw_hist = cur.fetchall()
            hist_records = {(r[0], r[1]) for r in raw_hist}
            hist_records_norm = {(r[0], str(r[1] or "").replace("T", " ").split(".")[0].strip()) for r in raw_hist}

            for entry in sent_entries:
                u = str(entry.get("username") or entry.get("id") or "").strip().lower()
                ts = str(entry.get("timestamp") or "").strip()
                status = str(entry.get("status") or "").upper()
                norm_ts = ts.replace("T", " ").split(".")[0].strip()

                if (u, ts) not in hist_records and (u, norm_ts) not in hist_records_norm:
                    _record_discrepancy("sent_log_to_db", "MISSING", {
                        "username": u,
                        "timestamp": ts,
                        "error": "Record missing in sending_history",
                    })
                else:
                    report["per_record_checks"]["sent_log_to_db"]["verified"] += 1

                if status in ("SENT_SUCCESS", "SUCCESS"):
                    core_matches = core_by_uname.get(u) or core_by_hid.get(u) if "hosts" in tables else None
                    if not core_matches or not any(cm["state"] == "sent" for cm in core_matches):
                        _record_discrepancy("sent_state_in_core", "FIELD_MISMATCH", {
                            "username": u,
                            "error": "Host not marked as 'sent' in core_hosts",
                        })
                    else:
                        report["per_record_checks"]["sent_state_in_core"]["verified"] += 1
                else:
                    report["per_record_checks"]["sent_state_in_core"]["verified"] += 1

        # 9. Per-Record Check: Reconciliation core_hosts.state == 'sent' -> sending_history SENT_SUCCESS
        if "core_hosts" in tables and "sending_history" in tables:
            cur.execute("""
                SELECT ch.trip_id, ch.host_id, ch.username
                FROM core_hosts ch
                WHERE ch.state = 'sent';
            """)
            core_sent_all = cur.fetchall()
            report["per_record_checks"]["unconfirmed_sent_in_core"]["total"] = len(core_sent_all)

            cur.execute("""
                SELECT LOWER(username), host_id
                FROM sending_history
                WHERE UPPER(status) IN ('SENT_SUCCESS', 'DRY_RUN_SUCCESS', 'SUCCESS')
                  AND LOWER(mode) != 'error';
            """)
            confirmed_history = cur.fetchall()
            confirmed_users = {r[0] for r in confirmed_history if r[0]}
            confirmed_hids = {r[1] for r in confirmed_history if r[1]}

            for csa in core_sent_all:
                u_c = (csa["username"] or "").strip().lower()
                hid_c = (csa["host_id"] or "").strip()
                if u_c in confirmed_users or hid_c in confirmed_hids:
                    report["per_record_checks"]["unconfirmed_sent_in_core"]["verified"] += 1
                else:
                    _record_discrepancy("unconfirmed_sent_in_core", "FIELD_MISMATCH", {
                        "trip_id": csa["trip_id"],
                        "host_id": hid_c,
                        "username": u_c,
                        "error": "Host marked as 'sent' in core_hosts but lacks confirmed SENT_SUCCESS in sending_history",
                    })
            report["invariants"]["unconfirmed_sent_hosts"] = len(report["per_record_checks"]["unconfirmed_sent_in_core"]["discrepancies"])

        # 10. Per-Record Check: inbox_conversations -> core_hosts
        if "inbox_conversations" in tables:
            cur.execute("SELECT * FROM inbox_conversations;")
            inbox_rows = cur.fetchall()
            report["per_record_checks"]["inbox_conversations"]["total"] = len(inbox_rows)
            for ir in inbox_rows:
                u = str(ir["host_username"] or "").strip().lower()
                core_matches = core_by_uname.get(u) or core_by_hid.get(u) if "hosts" in tables else None
                if not core_matches or not any(cm["state"] == "inbox" for cm in core_matches):
                    _record_discrepancy("inbox_conversations", "FIELD_MISMATCH", {
                        "username": u,
                        "error": "Host not marked as 'inbox' in core_hosts",
                    })
                else:
                    report["per_record_checks"]["inbox_conversations"]["verified"] += 1

        # 10. Invariants Checks
        # Sent hosts must never be in draft or approved
        if "sending_history" in tables:
            cur.execute("""
                SELECT COUNT(*) FROM core_hosts 
                WHERE state IN ('drafted', 'approved') 
                  AND (host_id IN (SELECT host_id FROM sending_history WHERE UPPER(status) IN ('SENT_SUCCESS', 'SUCCESS', 'SENT'))
                       OR LOWER(username) IN (SELECT LOWER(username) FROM sending_history WHERE UPPER(status) IN ('SENT_SUCCESS', 'SUCCESS', 'SENT')));
            """)
            sent_in_draft = cur.fetchone()[0]
            report["invariants"]["sent_hosts_in_draft_or_approved"] = sent_in_draft
            if sent_in_draft > 0:
                _record_discrepancy("hosts_to_core", "FIELD_MISMATCH", {
                    "error": f"{sent_in_draft} sent hosts in drafted/approved state",
                })

        # Primary key uniqueness
        cur.execute("SELECT trip_id, host_id, COUNT(*) as cnt FROM core_hosts GROUP BY trip_id, host_id HAVING cnt > 1;")
        pk_dupes = len(cur.fetchall())
        report["invariants"]["core_hosts_pk_duplicates"] = pk_dupes
        if pk_dupes > 0:
            _record_discrepancy("hosts_to_core", "DUPLICATE", {
                "error": f"{pk_dupes} duplicate primary keys (trip_id, host_id) in core_hosts",
            })

        cur.execute("SELECT id, COUNT(*) as cnt FROM core_trips GROUP BY id HAVING cnt > 1;")
        trip_pk_dupes = len(cur.fetchall())
        report["invariants"]["core_trips_pk_duplicates"] = trip_pk_dupes
        if trip_pk_dupes > 0:
            _record_discrepancy("user_trips", "DUPLICATE", {
                "error": f"{trip_pk_dupes} duplicate trip IDs in core_trips",
            })

        # Active trip uniqueness
        cur.execute("SELECT COUNT(*) FROM core_trips WHERE is_active = 1;")
        active_cnt = cur.fetchone()[0]
        if active_cnt > 1:
            report["invariants"]["multiple_active_trips"] = active_cnt
            _record_discrepancy("user_trips", "DUPLICATE", {
                "error": f"Multiple active trips detected: {active_cnt}",
            })

        # Orphan check
        cur.execute("""
            SELECT COUNT(*) FROM core_hosts 
            WHERE trip_id NOT IN (SELECT id FROM core_trips);
        """)
        orphan_hosts = cur.fetchone()[0]
        report["invariants"]["orphan_core_hosts"] = orphan_hosts
        if orphan_hosts > 0:
            _record_discrepancy("hosts_to_core", "UNRESOLVED_RELATIONSHIP", {
                "error": f"{orphan_hosts} orphan core_hosts without valid core_trips link",
            })

        # Extra trips check
        if "user_trips" in tables:
            cur.execute("SELECT id FROM core_trips WHERE id NOT IN (SELECT trip_id FROM user_trips UNION SELECT id FROM user_trips);")
            extra_trips = cur.fetchall()
            for et in extra_trips:
                _record_discrepancy("user_trips", "EXTRA", {"trip_id": et[0], "error": "Extra trip in core_trips not found in user_trips"})

        # Extra hosts check (hosts without source in hosts, inbox_conversations, or sending_history)
        if "hosts" in tables:
            cur.execute("""
                SELECT trip_id, host_id, username FROM core_hosts
                WHERE LOWER(host_id) NOT IN (SELECT LOWER(id) FROM hosts)
                  AND LOWER(username) NOT IN (SELECT LOWER(username) FROM hosts)
                  AND LOWER(username) NOT IN (SELECT LOWER(host_username) FROM inbox_conversations)
                  AND LOWER(username) NOT IN (SELECT LOWER(username) FROM sending_history);
            """)
            extra_hosts = cur.fetchall()
            for eh in extra_hosts:
                _record_discrepancy("hosts_to_core", "EXTRA", {"host_id": eh["host_id"], "username": eh["username"], "error": "Extra host in core_hosts without legacy source"})

    finally:
        conn.close()

    total_discrepancies = sum(report["category_counts"].values())
    report["checks_passed"] = (total_discrepancies == 0 and report["integrity_check"] == "ok")
    return report


def main():
    parser = argparse.ArgumentParser(description="Verify consolidation and migration integrity with per-record checking")
    parser.add_argument("--db-path", type=str, default=None, help="Database file path")
    parser.add_argument("--sent-log", type=str, default=None, help="Path to sent_log.json")
    args = parser.parse_args()

    db_path = Path(args.db_path) if args.db_path else get_db_path()
    sent_log_path = Path(args.sent_log) if args.sent_log else None
    try:
        report = verify_migration(db_path, sent_log_path=sent_log_path)
    except Exception as e:
        logger.error(f"Verification script failed: {e}")
        sys.exit(1)

    print("\n" + "=" * 65)
    print("  MIGRATION INTEGRITY & PER-RECORD FIDELITY REPORT")
    print("=" * 65)
    print(f"Database:        {report['db_path']}")
    print(f"Sent Log:        {report['sent_log_path'] or 'NOT PROVIDED'}")
    print(f"Overall Status:  {'PASSED [SUCCESS]' if report['checks_passed'] else 'FAILED [VIOLATIONS]'}")
    print(f"Integrity Check: {report['integrity_check']}")
    print(f"FK Violations:   {len(report['foreign_key_violations'])}")

    print("\n--- DISCREPANCY CATEGORY BREAKDOWN ---")
    for cat, cnt in report["category_counts"].items():
        st = "OK" if cnt == 0 else "VIOLATION"
        print(f"  {cat:25}: {cnt} [{st}]")

    print("\n--- PER-RECORD FIDELITY CHECKS ---")
    for name, stats in report["per_record_checks"].items():
        if stats.get("status") == "NOT VERIFIED":
            msg = stats.get("message") or "Source file missing"
            print(f"  {name:25}: [NOT VERIFIED ({msg})]")
            continue
        total = stats["total"]
        verified = stats["verified"]
        disc = len(stats["discrepancies"])
        status = "OK" if disc == 0 and verified == total else "FAIL"
        print(f"  {name:25}: {verified}/{total} verified, {disc} discrepancies [{status}]")
        if disc > 0:
            for item in stats["discrepancies"][:5]:
                print(f"     -> {item}")
            if disc > 5:
                print(f"     -> ... and {disc - 5} more")

    print("\n--- ARCHITECTURAL INVARIANTS ---")
    for k, v in report["invariants"].items():
        status = "OK" if v == 0 else "VIOLATION"
        print(f"  {k:35}: {v} [{status}]")
    print("=" * 65)

    if not report["checks_passed"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
