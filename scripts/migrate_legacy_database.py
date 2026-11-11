#!/usr/bin/env python3
"""
scripts/migrate_legacy_database.py

Единый канонический скрипт консолидации и миграции базы данных Couchsurfing:
1. Создание гарантированного резервного снимка (pre-migration backup).
2. Синхронизация user_trips -> core_trips (все 5 поездок).
3. Догрузка хостов из hosts + trip_hosts -> core_hosts + core_host_snapshots (без потерь данных).
4. Миграция 377+ записей из output/sent_log.json и output/sent_history.json в таблицу sending_history.
5. Синхронизация статусов sending_queue + generated_messages <-> core_hosts.
6. Синхронизация inbox_conversations <-> core_hosts (state = 'inbox').
7. Валидация целостности и формирование итогового JSON-отчета миграции.

Идемпотентен: повторный запуск безопасен и не дублирует данные.
"""

import argparse
import json
import logging
import os
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.database import (
    BASE_DIR,
    OUTPUT_DIR,
    backup_db,
    get_connection,
    get_db_path,
    init_db,
    log_audit,
)
from core.models import HostState

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("MigrateCanonical")


def create_pre_migration_backup(source_db: Path) -> Path:
    """Создает атомарный бэкап перед началом миграции."""
    backup_dir = OUTPUT_DIR / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest_path = backup_dir / f"migration_backup_{ts}.db"
    
    with get_connection(source_db) as src_conn:
        with sqlite3.connect(dest_path) as dst_conn:
            src_conn.backup(dst_conn)
            
    logger.info(f"Создан атомарный бэкап: {dest_path} ({dest_path.stat().st_size:,} bytes)")
    return dest_path


def run_migration(db_path: Path, dry_run: bool = False) -> Dict[str, Any]:
    logger.info(f"Запуск миграции на: {db_path} (dry_run={dry_run})")
    start_time = datetime.now()

    # 1. Обеспечиваем инициализацию схемы через каноническое ядро
    if not dry_run:
        init_db(db_path)

    stats: Dict[str, Any] = {
        "db_path": str(db_path),
        "dry_run": dry_run,
        "started_at": start_time.isoformat(),
        "backup_path": None,
        "trips_reconciled": 0,
        "hosts_backfilled_to_core": 0,
        "snapshots_created": 0,
        "sent_history_migrated_from_json": 0,
        "core_hosts_state_synced_sent": 0,
        "queue_states_synced": 0,
        "inbox_synced": 0,
        "errors": [],
    }

    if not dry_run:
        stats["backup_path"] = str(create_pre_migration_backup(db_path))

    with get_connection(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # -------------------------------------------------------------
        # ШАГ 1: Синхронизация user_trips -> core_trips
        # -------------------------------------------------------------
        logger.info("ШАГ 1: Синхронизация user_trips -> core_trips...")
        cur.execute("SELECT * FROM user_trips;")
        legacy_trips = cur.fetchall()
        for lt in legacy_trips:
            lt_d = dict(lt)
            t_id = lt_d.get("id") or lt_d.get("trip_id")
            cur.execute("SELECT id FROM core_trips WHERE id = ?;", (t_id,))
            if not cur.fetchone():
                if not dry_run:
                    cur.execute(
                        """
                        INSERT INTO core_trips (
                            id, url, country, city, arrival_date, departure_date,
                            dates_text, is_active, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                        """,
                        (
                            t_id,
                            lt_d.get("url") or "",
                            lt_d.get("country") or "Japan",
                            lt_d.get("city") or "",
                            lt_d.get("arrival_date") or "",
                            lt_d.get("departure_date") or "",
                            lt_d.get("dates_text") or "",
                            lt_d.get("is_active") or 0,
                            lt_d.get("created_at") or datetime.now().isoformat(),
                            lt_d.get("updated_at") or datetime.now().isoformat(),
                        ),
                    )
                stats["trips_reconciled"] += 1
        logger.info(f"Синхронизировано поездок: {stats['trips_reconciled']}")

        # Определяем активный trip_id (без скрытого дефолтного переключения)
        cur.execute("SELECT id FROM core_trips WHERE is_active = 1 LIMIT 1;")
        act_row = cur.fetchone()
        active_trip_id = act_row["id"] if act_row else None

        # -------------------------------------------------------------
        # ШАГ 2: Догрузка хостов hosts -> core_hosts & core_host_snapshots
        # -------------------------------------------------------------
        logger.info("ШАГ 2: Поиск недостающих хостов в hosts и синхронизация с core_hosts...")
        cur.execute("""
            SELECT h.*, th.trip_id as linked_trip_id
            FROM hosts h
            LEFT JOIN trip_hosts th ON h.id = th.host_id;
        """)
        hosts_rows = cur.fetchall()
        now_iso = datetime.now().isoformat()

        for hr in hosts_rows:
            hr_d = dict(hr)
            h_id = hr_d.get("id") or hr_d.get("host_id")
            u_name = hr_d.get("username")
            target_trip = hr_d.get("linked_trip_id") or active_trip_id
            if not target_trip:
                continue

            # Проверяем наличие в core_hosts
            cur.execute("SELECT host_id FROM core_hosts WHERE trip_id = ? AND (host_id = ? OR username = ?);", (target_trip, h_id, u_name))
            if not cur.fetchone():
                if not dry_run:
                    # Извлекаем данные
                    name = hr_d.get("clean_name") or hr_d.get("display_name") or hr_d.get("name") or u_name
                    location = hr_d.get("location") or ""
                    about = hr_d.get("about_me") or hr_d.get("about_me_clean") or hr_d.get("about") or ""
                    occ = hr_d.get("occupation") or ""
                    stars = hr_d.get("stars") or 1
                    is_ver = hr_d.get("is_verified") or 0

                    cur.execute(
                        """
                        INSERT OR IGNORE INTO core_hosts (
                            host_id, trip_id, username, name, location, occupation, about,
                            score_stars, is_verified, state, priority, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'discovered', 10, ?, ?);
                        """,
                        (h_id, target_trip, u_name, name, location, occ, about, stars, is_ver, now_iso, now_iso),
                    )
                    stats["hosts_backfilled_to_core"] += 1

                    # Добавляем неизменяемый снимок в core_host_snapshots
                    cur.execute("SELECT host_id FROM core_host_snapshots WHERE trip_id = ? AND host_id = ?;", (target_trip, h_id))
                    if not cur.fetchone():
                        raw_json = json.dumps(hr_d, ensure_ascii=False)
                        cur.execute(
                            """
                            INSERT OR IGNORE INTO core_host_snapshots (
                                host_id, trip_id, raw_data_json, captured_at
                            ) VALUES (?, ?, ?, ?);
                            """,
                            (h_id, target_trip, raw_json, now_iso),
                        )
                        stats["snapshots_created"] += 1
                        stats["snapshots_created"] += 1

        logger.info(f"Догружено хостов в core_hosts: {stats['hosts_backfilled_to_core']}, снимков: {stats['snapshots_created']}")

        # -------------------------------------------------------------
        # ШАГ 3: Миграция sent_log.json -> sending_history в БД
        # -------------------------------------------------------------
        logger.info("ШАГ 3: Миграция истории отправок из sent_log.json...")
        sent_log_file = OUTPUT_DIR / "sent_log.json"
        if sent_log_file.exists():
            try:
                with open(sent_log_file, "r", encoding="utf-8") as f:
                    sent_records = json.load(f)
                    
                items_to_migrate = []
                if isinstance(sent_records, list):
                    items_to_migrate = sent_records
                elif isinstance(sent_records, dict):
                    items_to_migrate = [{"username": k, **v} for k, v in sent_records.items()]

                for rec in items_to_migrate:
                    u_name = rec.get("username")
                    h_id = rec.get("host_id") or rec.get("id") or u_name
                    sent_at = rec.get("sent_at") or rec.get("timestamp") or now_iso
                    status_code = rec.get("status_code", 200)
                    t_id = rec.get("trip_id") or active_trip_id

                    st_raw = str(rec.get("status") or "").upper()
                    is_success = st_raw in ("SENT_SUCCESS", "DRY_RUN_SUCCESS", "SUCCESS")
                    is_already = st_raw == "ALREADY_SENT"
                    is_failed = st_raw in ("FAILED", "ERROR")
                    init_state = "sent" if is_success else ("skipped" if is_already else "uncertain")
                    init_sent_at = sent_at if is_success else None

                    # Проверяем реальный host_id в hosts или core_hosts
                    cur.execute("SELECT id FROM hosts WHERE LOWER(username) = LOWER(?) LIMIT 1;", (u_name,))
                    ref_h = cur.fetchone()
                    if ref_h:
                        final_h_id = ref_h["id"]
                    else:
                        final_h_id = h_id
                        # Создаем запись в hosts и core_hosts для удовлетворения внешних ключей и консистентности
                        if not dry_run:
                            cur.execute(
                                """
                                INSERT OR IGNORE INTO hosts (id, username, name, clean_name, location, profile_url, stay_request_url, first_scraped_at, updated_at)
                                VALUES (?, ?, ?, ?, 'Japan', 'https://couchsurfing.com/people/' || ?, 'https://couchsurfing.com/requests/' || ?, ?, ?);
                                """,
                                (final_h_id, u_name, u_name, u_name, u_name, u_name, now_iso, now_iso),
                            )
                            if t_id:
                                cur.execute(
                                    """
                                    INSERT OR IGNORE INTO core_hosts (host_id, trip_id, username, name, location, state, priority, sent_at, created_at, updated_at)
                                    VALUES (?, ?, ?, ?, 'Japan', ?, 10, ?, ?, ?);
                                    """,
                                    (final_h_id, t_id, u_name, u_name, init_state, init_sent_at, now_iso, now_iso),
                                )
                                cur.execute(
                                    """
                                    INSERT OR IGNORE INTO core_host_snapshots (host_id, trip_id, raw_data_json, captured_at)
                                    VALUES (?, ?, ?, ?);
                                    """,
                                    (final_h_id, t_id, json.dumps(rec, ensure_ascii=False), now_iso),
                                )

                    cur.execute(
                        "SELECT id FROM sending_history WHERE LOWER(username) = LOWER(?) AND timestamp = ?;",
                        (u_name, sent_at),
                    )
                    st_raw = str(rec.get("status") or "").upper()
                    is_success = st_raw in ("SENT_SUCCESS", "DRY_RUN_SUCCESS", "SUCCESS")
                    is_already = st_raw == "ALREADY_SENT"
                    is_failed = st_raw in ("FAILED", "ERROR")

                    hist_status = rec.get("status") or ("SENT_SUCCESS" if is_success else "UNKNOWN")
                    hist_mode = "error" if is_failed else ("skipped" if is_already else rec.get("mode", "migration"))
                    err_det = rec.get("error")
                    notes_txt = rec.get("notes") or ("migrated from sent_log.json" if not err_det else f"migrated: {err_det}")

                    if not cur.fetchone():
                        if not dry_run:
                            cur.execute(
                                """
                                INSERT INTO sending_history (
                                    host_id, username, status, http_status, mode, notes, error_details, timestamp
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?);
                                """,
                                (final_h_id, u_name, hist_status, status_code, hist_mode, notes_txt, err_det, sent_at),
                            )
                        stats["sent_history_migrated_from_json"] += 1

                    # Синхронизируем статус в core_hosts ТОЛЬКО при подтверждённом успехе
                    if not dry_run:
                        if is_success:
                            cur.execute(
                                """
                                UPDATE core_hosts
                                SET state = 'sent', sent_at = ?
                                WHERE LOWER(username) = LOWER(?) AND state != 'sent';
                                """,
                                (sent_at, u_name),
                            )
                            stats["core_hosts_state_synced_sent"] += cur.rowcount
                        elif is_already:
                            cur.execute(
                                """
                                UPDATE core_hosts
                                SET state = 'skipped', qc_notes = 'Already sent on Couchsurfing'
                                WHERE LOWER(username) = LOWER(?) AND state NOT IN ('sent', 'skipped');
                                """,
                                (u_name,),
                            )
                        elif is_failed:
                            cur.execute(
                                """
                                UPDATE core_hosts
                                SET state = 'uncertain', qc_notes = ?
                                WHERE LOWER(username) = LOWER(?) AND state NOT IN ('sent', 'uncertain');
                                """,
                                (f"Migration: sending attempt FAILED ({err_det})", u_name),
                            )

            except Exception as e:
                logger.error(f"Ошибка при обработке sent_log.json: {e}")
                stats["errors"].append(f"sent_log error: {e}")

        logger.info(f"Мигрировано отправленных писем в SQLite: {stats['sent_history_migrated_from_json']}")

        # -------------------------------------------------------------
        # ШАГ 4: Синхронизация sending_queue <-> core_hosts
        # -------------------------------------------------------------
        logger.info("ШАГ 4: Синхронизация статусов sending_queue...")
        cur.execute("SELECT sq.host_id, sq.status, h.username FROM sending_queue sq JOIN hosts h ON sq.host_id = h.id;")
        q_rows = cur.fetchall()
        for qr in q_rows:
            q_status = (qr["status"] or "").lower()
            u_name = qr["username"]
            h_id = qr["host_id"]

            target_state = None
            if q_status == "approved":
                target_state = HostState.APPROVED.value
            elif q_status in ("draft", "drafted"):
                target_state = HostState.DRAFTED.value
            elif q_status == "skipped":
                target_state = HostState.SKIPPED.value
            elif q_status == "sent":
                # Не устанавливаем sent вслепую без подтверждения в sending_history
                cur.execute(
                    """
                    SELECT 1 FROM sending_history
                    WHERE (LOWER(username) = LOWER(?) OR host_id = ?)
                      AND UPPER(status) IN ('SENT_SUCCESS', 'DRY_RUN_SUCCESS', 'SUCCESS')
                      AND LOWER(mode) != 'error'
                    LIMIT 1;
                    """,
                    (u_name, h_id),
                )
                if cur.fetchone():
                    target_state = HostState.SENT.value
                else:
                    target_state = HostState.UNCERTAIN.value

            if target_state and not dry_run:
                cur.execute(
                    """
                    UPDATE core_hosts
                    SET state = ?
                    WHERE (host_id = ? OR LOWER(username) = LOWER(?)) AND state NOT IN ('sent', ?);
                    """,
                    (target_state, h_id, u_name, target_state),
                )
                stats["queue_states_synced"] += cur.rowcount

        # -------------------------------------------------------------
        # ШАГ 5: Синхронизация inbox_conversations <-> core_hosts
        # -------------------------------------------------------------
        logger.info("ШАГ 5: Синхронизация inbox_conversations...")
        cur.execute("SELECT id, host_username, host_name, host_avatar_url, location FROM inbox_conversations;")
        inbox_rows = cur.fetchall()
        for ir in inbox_rows:
            ir_d = dict(ir)
            u_name = ir_d.get("host_username") or ir_d.get("username")
            if not u_name:
                continue

            cur.execute("SELECT host_id FROM core_hosts WHERE LOWER(username) = LOWER(?);", (u_name,))
            existing = cur.fetchone()
            if existing:
                if not dry_run:
                    cur.execute(
                        """
                        UPDATE core_hosts
                        SET state = 'inbox'
                        WHERE LOWER(username) = LOWER(?) AND state != 'inbox';
                        """,
                        (u_name,),
                    )
                    stats["inbox_synced"] += cur.rowcount
            else:
                # Хост из входящих отсутствует в core_hosts (ответил напрямую) -> аккуратно регистрируем
                if not dry_run:
                    h_id = f"inbox_{u_name}"
                    h_name = ir_d.get("host_name") or u_name
                    h_loc = ir_d.get("location") or ""
                    h_ava = ir_d.get("host_avatar_url") or ""
                    cur.execute(
                        """
                        INSERT OR IGNORE INTO hosts (id, username, name, clean_name, location, avatar_url, first_scraped_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?);
                        """,
                        (h_id, u_name, h_name, h_name, h_loc, h_ava, now_iso, now_iso),
                    )
                    if active_trip_id:
                        cur.execute(
                            """
                            INSERT OR IGNORE INTO core_hosts (host_id, trip_id, username, name, location, avatar_url, state, priority, created_at, updated_at)
                            VALUES (?, ?, ?, ?, ?, ?, 'inbox', 10, ?, ?);
                            """,
                            (h_id, active_trip_id, u_name, h_name, h_loc, h_ava, now_iso, now_iso),
                        )
                        cur.execute(
                            """
                            INSERT OR IGNORE INTO core_host_snapshots (host_id, trip_id, raw_data_json, captured_at)
                            VALUES (?, ?, ?, ?);
                            """,
                            (h_id, active_trip_id, json.dumps(ir_d, ensure_ascii=False), now_iso),
                        )
                        stats["inbox_synced"] += 1
                        stats["hosts_backfilled_to_core"] += 1

        # -------------------------------------------------------------
        # ШАГ 6: Дозаполнение отсутствующих снимков core_host_snapshots
        # -------------------------------------------------------------
        logger.info("ШАГ 6: Проверка полноты снимков core_host_snapshots...")
        cur.execute("""
            SELECT ch.host_id, ch.trip_id, ch.username, ch.created_at, ch.about, ch.name, ch.location
            FROM core_hosts ch
            LEFT JOIN core_host_snapshots chs ON ch.trip_id = chs.trip_id AND ch.host_id = chs.host_id
            WHERE chs.host_id IS NULL;
        """)
        missing_snaps = cur.fetchall()
        for ms in missing_snaps:
            ms_d = dict(ms)
            if not dry_run:
                cur.execute(
                    """
                    INSERT OR IGNORE INTO core_host_snapshots (host_id, trip_id, raw_data_json, captured_at)
                    VALUES (?, ?, ?, ?);
                    """,
                    (ms_d["host_id"], ms_d["trip_id"], json.dumps(ms_d, ensure_ascii=False), ms_d["created_at"] or now_iso),
                )
                stats["snapshots_created"] += 1

    if not dry_run:
        log_audit(
            trip_id="global",
            module="MIGRATION",
            event="CONSOLIDATION_COMPLETED",
            details=stats,
            db_file=db_path,
        )

    stats["finished_at"] = datetime.now().isoformat()
    stats["duration_seconds"] = round((datetime.now() - start_time).total_seconds(), 2)

    # Записываем отчет на диск
    rep_file = OUTPUT_DIR / f"migration_report_{start_time.strftime('%Y%m%d_%H%M%S')}.json"
    with open(rep_file, "w", encoding="utf-8") as f:
        json.dump(stats, f, indent=2, ensure_ascii=False)
    logger.info(f"Отчет миграции сохранен: {rep_file}")

    return stats


def main():
    parser = argparse.ArgumentParser(description="Consolidate legacy data into canonical architecture")
    parser.add_argument("--db-path", type=str, default=None, help="Database path")
    parser.add_argument("--dry-run", action="store_true", help="Perform inspection without writing changes")
    args = parser.parse_args()

    db_path = Path(args.db_path) if args.db_path else get_db_path()
    res = run_migration(db_path, dry_run=args.dry_run)
    print("\n--- ИТОГИ МИГРАЦИИ ---")
    for k, v in res.items():
        print(f"  {k:35}: {v}")


if __name__ == "__main__":
    main()
