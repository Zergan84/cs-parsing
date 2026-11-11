"""
core/ingest_all_kyoto.py - Пакетная миграция и скоринг всех хостов Киото в новую схему core_hosts.
"""

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Dict, Any

from core.collector import CollectorEngine, calculate_match_score
from core.database import get_connection, log_audit
from core.models import HostState

KYOTO_TRIP_ID = "kyoto-trip-3lhdfema5epclndo82mclxklzfytp-3LhDfEmA5Epclndo82MclXkLzFy_tp"
QUEUE_FILE = Path("output/requests_queue.json")


def run_ingestion():
    print(f"=== Запуск пакета скоринга и импорта для {KYOTO_TRIP_ID} ===")
    now_iso = datetime.now().isoformat()
    
    # 1. Проверяем наличие трипа
    with get_connection() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO core_trips (id, url, city, arrival_date, departure_date, is_active, created_at)
            VALUES (?, ?, ?, ?, ?, 1, ?)
            """,
            (
                KYOTO_TRIP_ID,
                f"https://www.couchsurfing.com/c/trips/{KYOTO_TRIP_ID}",
                "Kyoto",
                "2026-11-19",
                "2026-11-21",
                now_iso
            )
        )

    # 2. Получаем актуальные статусы из sending_queue
    sent_ids = set()
    approved_ids = set()
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT host_id, status FROM sending_queue;")
        for row in cur.fetchall():
            hid = str(row[0]).strip().lower()
            st = str(row[1]).strip().lower()
            if st == "sent":
                sent_ids.add(hid)
            elif st in ("approved", "queued"):
                approved_ids.add(hid)

    # 3. Читаем хостов из requests_queue.json
    hosts_to_ingest = []
    if QUEUE_FILE.exists():
        with open(QUEUE_FILE, "r", encoding="utf-8") as f:
            all_raw = json.load(f)
        for h in all_raw:
            t_id = h.get("trip_id") or ""
            # Если хост относится к Киото или город Киото
            city = str(h.get("city") or h.get("location") or "").lower()
            if "kyoto" in city or "3lhdfema" in str(t_id).lower():
                hosts_to_ingest.append(h)

    print(f"Найдено профилей по Киото для скоринга: {len(hosts_to_ingest)}")

    stats = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
    state_stats = {}
    inserted = 0
    updated = 0

    with get_connection() as conn:
        for h in hosts_to_ingest:
            host_id = str(h.get("id") or h.get("username") or "").strip()
            username = str(h.get("username") or host_id).strip()
            if not host_id or not username:
                continue

            # Расчет 1-5★ рейтинга
            h["about"] = h.get("about_me_clean") or h.get("about_me") or ""
            stars, reasons = calculate_match_score(h)
            stats[stars] += 1

            # Определение исходного состояния
            u_low = username.lower()
            h_low = host_id.lower()
            if u_low in sent_ids or h_low in sent_ids:
                state = HostState.SENT.value
            elif u_low in approved_ids or h_low in approved_ids:
                state = HostState.APPROVED.value
            else:
                state = HostState.DISCOVERED.value

            state_stats[state] = state_stats.get(state, 0) + 1

            name = (h.get("clean_name") or h.get("name") or username).strip()
            location = (h.get("location") or h.get("city") or "Kyoto, Japan").strip()
            msg = h.get("generated_message") or ""

            # 1. Снимок
            conn.execute(
                """
                INSERT OR REPLACE INTO core_host_snapshots (host_id, trip_id, raw_data_json, captured_at)
                VALUES (?, ?, ?, ?);
                """,
                (host_id, KYOTO_TRIP_ID, json.dumps(h, ensure_ascii=False), now_iso)
            )

            # 2. Карточка
            conn.execute(
                """
                INSERT INTO core_hosts (
                    host_id, trip_id, username, name, location, gender, age, occupation,
                    about, interests_json, languages_json, countries_json, profile_url,
                    avatar_url, score_stars, score_reasons_json, state, message_draft,
                    qc_passed, qc_notes, sent_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(trip_id, host_id) DO UPDATE SET
                    score_stars = excluded.score_stars,
                    score_reasons_json = excluded.score_reasons_json,
                    state = CASE 
                        WHEN core_hosts.state = 'sent' THEN 'sent' 
                        ELSE excluded.state 
                    END,
                    updated_at = excluded.updated_at;
                """,
                (
                    host_id, KYOTO_TRIP_ID, username, name, location,
                    h.get("estimated_gender"), h.get("age"), h.get("occupation"),
                    h.get("about"),
                    json.dumps(h.get("interests") or [], ensure_ascii=False),
                    json.dumps(h.get("languages") or [], ensure_ascii=False),
                    json.dumps(h.get("countries_visited") or [], ensure_ascii=False),
                    h.get("profile_url"), h.get("avatar_url"), stars,
                    json.dumps(reasons, ensure_ascii=False),
                    state, msg,
                    1 if msg else 0,
                    "Imported from CS scraper",
                    h.get("sent_at"),
                    now_iso, now_iso
                )
            )
            inserted += 1

    log_audit(
        trip_id=KYOTO_TRIP_ID,
        module="COLLECTOR",
        event="BATCH_INGEST_COMPLETED",
        details={"total": inserted, "stars": stats, "states": state_stats}
    )

    print("\n=== Результаты скоринга всей базы Киото ===")
    print(f"Всего обработано хостов: {inserted}")
    print("Распределение по звездам (1-5★ Match):")
    for star in [5, 4, 3, 2, 1]:
        print(f"  {'★' * star}{'☆' * (5-star)} ({star} звезды): {stats[star]} хостов")
    print("\nРаспределение по состояниям:")
    for st, count in state_stats.items():
        print(f"  • {st.upper()}: {count}")


if __name__ == "__main__":
    run_ingestion()
