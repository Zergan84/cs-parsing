"""
request_manager.py - Управление очередью персонализированных запросов с SQLite и JSON синхронизацией.

Обеспечивает:
1. Хранение очереди в SQLite (таблицы sending_queue, hosts, profile_analyses, generated_messages).
2. Автоматическую синхронизацию с output/requests_queue.json для обратной совместимости.
3. Обогащение карточек результатами ProfileAnalyzer (Conversation Hooks, Strategy, Matching Facts).
4. Защиту от повторной отправки и некорректных переходов статусов.
"""

import fcntl
import json
import logging
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from database import db_session, init_db
from hosting_guard import get_hosting_guard
from message_generator import generate_stay_request
from personal_context import DEFAULT_GUEST_CONTEXT
from profile_analyzer import get_profile_analyzer

logger = logging.getLogger("RequestManager")

BASE_DIR = Path(__file__).parent
OUTPUT_DIR = Path(os.environ.get("CS_OUTPUT_DIR", str(BASE_DIR / "output"))).resolve()
QUEUE_FILE = OUTPUT_DIR / "requests_queue.json"
CURATED_FILE = OUTPUT_DIR / "hosts_tokyo_curated.json"
RAW_FILE = OUTPUT_DIR / "hosts_tokyo.json"
SENT_HISTORY_FILE = OUTPUT_DIR / "sent_history.json"
SENT_LOG_FILE = OUTPUT_DIR / "sent_log.json"

DEFAULT_CHECK_IN = "2026-11-18"
DEFAULT_CHECK_OUT = "2026-11-20"
DEFAULT_CONTEXT = DEFAULT_GUEST_CONTEXT

# Стоп-фразы: хост явно не принимает гостей — не добавляем в очередь на отправку.
BLACKLIST_PHRASES = (
    "not open to hosting",
    "can't host",
    "cannot host",
    "no hosting",
    "currently not hosting",
    "stopped hosting",
    "not hosting",
)


def is_blacklisted(about_me: Optional[str]) -> Optional[str]:
    """Возвращает сработавшую стоп- фразу или None, если хост подходит."""
    if not about_me:
        return None
    text = str(about_me).strip().lower()
    for phrase in BLACKLIST_PHRASES:
        if phrase in text:
            return phrase
    return None


class RequestQueueManager:
    """Менеджер очереди запросов с двухсторонней персистентностью SQLite + JSON."""

    def __init__(self, queue_file: Optional[Path] = None):
        self.queue_file = Path(queue_file) if queue_file is not None else QUEUE_FILE
        self.queue_file.parent.mkdir(parents=True, exist_ok=True)
        init_db()
        self.sync_from_json_if_db_empty()

    def get_sent_usernames(self) -> Set[str]:
        """Возвращает множество username хостов, которым уже был отправлен запрос."""
        sent = set()
        # 1. Из sent_history.json
        if SENT_HISTORY_FILE.exists():
            try:
                with open(SENT_HISTORY_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    for u in data.get("sent_usernames", []):
                        if u:
                            sent.add(str(u).strip().lower())
            except Exception:
                pass

        # 2. Из sent_log.json
        if SENT_LOG_FILE.exists():
            try:
                with open(SENT_LOG_FILE, "r", encoding="utf-8") as f:
                    logs = json.load(f)
                    for it in logs:
                        st = str(it.get("status", "")).upper()
                        notes = str(it.get("notes", "")).lower()
                        err = str(it.get("error", "")).lower()
                        u = it.get("username") or it.get("id")
                        if not u:
                            continue
                        u_clean = str(u).strip().lower()
                        # Только реальные отправки. DRY_RUN и ложные "already sent" (FAILED/skipped без
                        # подтверждения видимым текстом) НЕ считаются отправленными.
                        # Старые SENT_SUCCESS с пометкой "No send button / Active dialogue" — это
                        # артефакт ложного скипа, а не реальная отправка.
                        false_skip_artifact = (
                            "detected as already sent" in notes
                            and "visible" not in notes
                            and "deduplicated" not in notes
                        )
                        if st in ("SENT_SUCCESS", "SUCCESS") and not false_skip_artifact:
                            sent.add(u_clean)
                        elif st == "ALREADY_SENT" and ("visible" in notes or "deduplicated via inbox" in notes):
                            sent.add(u_clean)
            except Exception:
                pass

        # 3. Из базы данных SQLite: sending_history, inbox_conversations, requests, sending_queue
        try:
            with db_session() as conn:
                cur = conn.cursor()
                cur.execute("SELECT username FROM sending_history WHERE UPPER(status) IN ('SENT_SUCCESS', 'SUCCESS');")
                for row in cur.fetchall():
                    if row[0]:
                        sent.add(str(row[0]).strip().lower())

                # Активные и исторические диалоги из inbox_conversations
                cur.execute("SELECT host_username FROM inbox_conversations;")
                for row in cur.fetchall():
                    if row[0]:
                        sent.add(str(row[0]).strip().lower())

                # Запросы со статусом Sent/Declined/Completed. Для Sent требуем реальную дату
                # либо явное подтверждение — иначе ложный skip снова спрячет кандидата.
                cur.execute("""
                    SELECT h.username, sq.host_id, sq.sent_at, sq.notes
                    FROM sending_queue sq
                    LEFT JOIN hosts h ON h.id = sq.host_id
                    WHERE LOWER(sq.status) IN ('sent', 'declined', 'completed');
                """)
                for row in cur.fetchall():
                    username, host_id, sent_at, notes = row
                    notes_l = str(notes or "").lower()
                    confirmed = (
                        bool(sent_at)
                        or ("already sent" in notes_l and "visible" in notes_l)
                        or ("deduplicated" in notes_l and "inbox" in notes_l)
                        or str(notes or "").lower().startswith("sent (confirmed")
                    )
                    if not confirmed:
                        continue
                    if username:
                        sent.add(str(username).strip().lower())
                    if host_id:
                        sent.add(str(host_id).strip().lower())
        except Exception:
            pass

        return sent

    def deduplicate_queue_against_sent(self) -> int:
        """
        Сверяет очередь (Approved/Queued) со всеми отправленными хостами и диалогами.
        Переводит отправленных в статус 'Sent' с меткой 'Already Sent (Deduplicated)'.
        Возвращает количество исключенных хостов.
        """
        sent_set = self.get_sent_usernames()
        if not sent_set:
            return 0

        deduped_count = 0
        now_iso = datetime.now().isoformat()
        # Фейковая дата sent_at больше не подставляется; запись в sent_log помечается
        # меткой времени дедупликации (mode=sync_dedup исключается из счетчика "сегодня").
        historical_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # 1. Обновление SQLite (историческая дата, чтобы не искажать счетчик за сегодня)
        try:
            with db_session() as conn:
                cur = conn.cursor()
                for u in sent_set:
                    cur.execute(
                        """
                        UPDATE sending_queue 
                        SET status = 'Sent', notes = 'Already Sent (Deduplicated)', updated_at = ?
                        WHERE (LOWER(host_id) = ? OR host_id IN (SELECT id FROM hosts WHERE LOWER(username) = ?)) 
                          AND LOWER(status) IN ('approved', 'queued');
                        """,
                        (now_iso, u, u),
                    )
        except Exception as e:
            logger.warning(f"Ошибка обновления SQLite при дедупликации: {e}")

        # 2. Обновление requests_queue.json
        if QUEUE_FILE.exists():
            try:
                with open(QUEUE_FILE, "r", encoding="utf-8") as f:
                    queue = json.load(f)

                updated = False
                for item in queue:
                    u = str(item.get("username") or item.get("id") or "").strip().lower()
                    st = str(item.get("status") or "").lower()
                    if u in sent_set and st in ("approved", "queued"):
                        item["status"] = "Sent"
                        item["notes"] = "Already Sent (Deduplicated)"
                        item["updated_at"] = now_iso
                        deduped_count += 1
                        updated = True

                if updated:
                    with open(QUEUE_FILE, "w", encoding="utf-8") as f:
                        json.dump(queue, f, ensure_ascii=False, indent=2)
            except Exception as e:
                logger.warning(f"Ошибка сохранения requests_queue.json при дедупликации: {e}")

        # 3. Синхронизация sent_log.json
        if deduped_count > 0:
            logs = []
            existing_log_users = set()
            if SENT_LOG_FILE.exists():
                try:
                    with open(SENT_LOG_FILE, "r", encoding="utf-8") as f:
                        logs = json.load(f)
                    for l in logs:
                        lu = (l.get("username") or l.get("id") or "").lower()
                        if lu:
                            existing_log_users.add(lu)
                except Exception:
                    logs = []

            for u in sent_set:
                if u and u not in existing_log_users:
                    logs.append({
                        "id": u,
                        "username": u,
                        "name": u,
                        "timestamp": historical_str,
                        "status": "ALREADY_SENT",
                        "mode": "sync_dedup",
                        "notes": "Already Sent (Deduplicated via Inbox/History sync)",
                    })
                    existing_log_users.add(u)

            try:
                with open(SENT_LOG_FILE, "w", encoding="utf-8") as f:
                    json.dump(logs, f, ensure_ascii=False, indent=2)
            except Exception:
                pass

        if deduped_count > 0:
            logger.info(f"[DEDUP] Успешно исключено из очереди {deduped_count} уже отправленных хостов.")
        return deduped_count

    def sync_from_json_if_db_empty(self) -> None:
        """Если база данных пуста, импортирует существующую валидированную очередь из JSON."""
        try:
            with db_session() as conn:
                cur = conn.cursor()
                cur.execute("SELECT COUNT(*) FROM sending_queue;")
                count = cur.fetchone()[0]
                if count > 0:
                    return

            if not self.queue_file.exists():
                return

            with open(self.queue_file, "r", encoding="utf-8") as f:
                queue = json.load(f)

            if not queue:
                return

            logger.info(f"Миграция {len(queue)} анкет из JSON в базу данных SQLite...")
            analyzer = get_profile_analyzer()

            with db_session() as conn:
                cur = conn.cursor()
                now_iso = datetime.now().isoformat()
                for item in queue:
                    host_id = item.get("id") or item.get("username")
                    username = item.get("username") or host_id
                    name = item.get("name", "")
                    clean_name = item.get("clean_name", name)
                    loc = item.get("location", "Tokyo")
                    if isinstance(loc, dict):
                        loc = loc.get("locationDescription", {}).get("description", "Tokyo")

                    # 1. Запись в hosts
                    cur.execute(
                        """
                        INSERT INTO hosts (
                            id, username, name, clean_name, profile_url, stay_request_url,
                            location, about_me, about_me_clean, references_count,
                            response_rate_pct, instagram_handle, instagram_url,
                            first_scraped_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(id) DO UPDATE SET updated_at = excluded.updated_at;
                        """,
                        (
                            host_id,
                            username,
                            name,
                            clean_name,
                            item.get("profile_url") or f"https://www.couchsurfing.com/c/users/{username}",
                            item.get("stay_request_url") or f"https://www.couchsurfing.com/c/users/{username}/stay-request",
                            str(loc),
                            item.get("about_me", ""),
                            item.get("about_me_clean", ""),
                            item.get("references_count", 0),
                            float(item.get("response_rate_pct", 90.0)),
                            item.get("instagram_handle", ""),
                            item.get("instagram_url", ""),
                            now_iso,
                            now_iso,
                        ),
                    )

                    # 2. Анализ профиля в той же транзакции
                    analysis = analyzer.analyze_host(item, conn=conn)

                    # 3. Сохранение сообщения
                    msg_id = f"msg_{host_id}_{int(datetime.now().timestamp())}"
                    msg_text = item.get("generated_message", "")
                    cur.execute(
                        """
                        INSERT OR REPLACE INTO generated_messages (
                            id, host_id, check_in, check_out, message_text,
                            word_count, qc_passed, is_active, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, 1, 1, ?);
                        """,
                        (
                            msg_id,
                            host_id,
                            item.get("check_in", DEFAULT_CHECK_IN),
                            item.get("check_out", DEFAULT_CHECK_OUT),
                            msg_text,
                            len(msg_text.split()),
                            now_iso,
                        ),
                    )

                    # 4. Сохранение в очередь
                    cur.execute(
                        """
                        INSERT OR REPLACE INTO sending_queue (
                            id, host_id, current_message_id, status, priority,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, 10, ?, ?);
                        """,
                        (
                            f"q_{host_id}",
                            host_id,
                            msg_id,
                            item.get("status", "Draft"),
                            now_iso,
                            now_iso,
                        ),
                    )

            logger.info("Успешно импортировано в SQLite!")
        except Exception as e:
            logger.error(f"Ошибка миграции JSON в SQLite: {e}")

    def get_queue_stats(self, trip_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Возвращает согласованную статистику очереди через единый быстрый SQL-запрос.
        Категории строго не пересекаются, их сумма равна total.
        """
        try:
            with db_session() as conn:
                cur = conn.cursor()
                if trip_id:
                    query = """
                    SELECT 
                        COUNT(CASE WHEN LOWER(COALESCE(sq.status, '')) IN ('needs_regeneration', 'name_review_required', 'qc_review_required', 'draft', 'generated', 'approved', 'queued') THEN 1 END) as total,
                        COUNT(CASE WHEN LOWER(COALESCE(sq.status, '')) IN ('needs_regeneration', '') THEN 1 END) as needs_regeneration,
                        COUNT(CASE WHEN LOWER(COALESCE(sq.status, '')) = 'name_review_required' THEN 1 END) as name_review,
                        COUNT(CASE WHEN LOWER(COALESCE(sq.status, '')) = 'qc_review_required' THEN 1 END) as qc_review,
                        COUNT(CASE WHEN LOWER(COALESCE(sq.status, '')) IN ('draft', 'generated') THEN 1 END) as draft,
                        COUNT(CASE WHEN LOWER(COALESCE(sq.status, '')) IN ('approved', 'queued') THEN 1 END) as approved,
                        COUNT(CASE WHEN LOWER(COALESCE(sq.status, '')) LIKE 'sent%' THEN 1 END) as sent_lifetime,
                        COUNT(CASE WHEN LOWER(COALESCE(sq.status, '')) = 'skipped' THEN 1 END) as skipped
                    FROM sending_queue sq
                    JOIN trip_hosts th ON (sq.host_id = th.host_id OR LOWER(sq.host_id) = LOWER(th.host_id))
                    WHERE LOWER(th.trip_id) = LOWER(?);
                    """
                    known_aliases = {
                        "3lhdfema5epclndo82mclxklzfytp": "kyoto-trip-3lhdfema5epclndo82mclxklzfytp-3LhDfEmA5Epclndo82MclXkLzFy_tp",
                        "3lhdfema5epclndo82mclxk_tp": "kyoto-trip-3lhdfema5epclndo82mclxklzfytp-3LhDfEmA5Epclndo82MclXkLzFy_tp",
                        "kyoto-trip-3lhdfema5epclndo82mclxklzfytp-3lhdfema5epclndo82mclxklzfytp": "kyoto-trip-3lhdfema5epclndo82mclxklzfytp-3LhDfEmA5Epclndo82MclXkLzFy_tp",
                    }
                    t_str = str(trip_id).strip()
                    canon = known_aliases.get(t_str.lower(), t_str)
                    cur.execute(query, (canon,))
                else:
                    query = """
                    SELECT 
                        COUNT(CASE WHEN LOWER(COALESCE(sq.status, '')) IN ('needs_regeneration', 'name_review_required', 'qc_review_required', 'draft', 'generated', 'approved', 'queued') THEN 1 END) as total,
                        COUNT(CASE WHEN LOWER(COALESCE(sq.status, '')) IN ('needs_regeneration', '') THEN 1 END) as needs_regeneration,
                        COUNT(CASE WHEN LOWER(COALESCE(sq.status, '')) = 'name_review_required' THEN 1 END) as name_review,
                        COUNT(CASE WHEN LOWER(COALESCE(sq.status, '')) = 'qc_review_required' THEN 1 END) as qc_review,
                        COUNT(CASE WHEN LOWER(COALESCE(sq.status, '')) IN ('draft', 'generated') THEN 1 END) as draft,
                        COUNT(CASE WHEN LOWER(COALESCE(sq.status, '')) IN ('approved', 'queued') THEN 1 END) as approved,
                        COUNT(CASE WHEN LOWER(COALESCE(sq.status, '')) LIKE 'sent%' THEN 1 END) as sent_lifetime,
                        COUNT(CASE WHEN LOWER(COALESCE(sq.status, '')) = 'skipped' THEN 1 END) as skipped
                    FROM sending_queue sq;
                    """
                    cur.execute(query)

                row = cur.fetchone()
                total = int(row[0] or 0)
                needs_regen = int(row[1] or 0)
                name_review = int(row[2] or 0)
                qc_review = int(row[3] or 0)
                draft = int(row[4] or 0)
                approved = int(row[5] or 0)
                sent_lifetime = int(row[6] or 0)
                skipped = int(row[7] or 0)

                # Суточный показатель отправки (только успешные за сегодня)
                sent_today = 0
                sent_log_path = OUTPUT_DIR / "sent_log.json"
                if sent_log_path.exists():
                    try:
                        with open(sent_log_path, "r", encoding="utf-8") as f:
                            logs = json.load(f)
                        today_str = datetime.now().strftime("%Y-%m-%d")
                        sent_today = sum(
                            1 for x in logs
                            if str(x.get("timestamp", "")).startswith(today_str)
                            and x.get("status") in ("SENT_SUCCESS", "success", "dry_run_success", "DRY_RUN_SUCCESS")
                            and x.get("mode") in ("live", "dry_run")
                            and "Deduplicated" not in (x.get("notes") or "")
                            and "Detected as already sent" not in (x.get("notes") or "")
                            and not str(x.get("timestamp", "")).endswith("19:55:57")
                        )
                    except Exception:
                        pass

                return {
                    "total": total,
                    "total_active": total,
                    "needs_regeneration": needs_regen,
                    "name_review": name_review,
                    "qc_review": qc_review,
                    "draft": draft,
                    "approved": approved,
                    "sent_today": sent_today,
                    "daily_limit": 100,
                    "sent_lifetime": sent_lifetime,
                    "skipped": skipped,
                }
        except Exception as e:
            logger.error(f"Ошибка получения статистики очереди: {e}")
            return {
                "total": 0,
                "total_active": 0,
                "needs_regeneration": 0,
                "name_review": 0,
                "qc_review": 0,
                "draft": 0,
                "approved": 0,
                "sent_today": 0,
                "daily_limit": 100,
                "sent_lifetime": 0,
                "skipped": 0,
            }

    def load_queue(self, exclude_sent: bool = True, trip_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Загружает очередь с обогащением данными хоста, анализа и текущего сообщения.
        При указании trip_id фильтрует карточки по активной поездке.
        """
        try:
            with db_session() as conn:
                cur = conn.cursor()
                if trip_id:
                    known_aliases = {
                        "3lhdfema5epclndo82mclxklzfytp": "kyoto-trip-3lhdfema5epclndo82mclxklzfytp-3LhDfEmA5Epclndo82MclXkLzFy_tp",
                        "3lhdfema5epclndo82mclxk_tp": "kyoto-trip-3lhdfema5epclndo82mclxklzfytp-3LhDfEmA5Epclndo82MclXkLzFy_tp",
                        "kyoto-trip-3lhdfema5epclndo82mclxklzfytp-3lhdfema5epclndo82mclxklzfytp": "kyoto-trip-3lhdfema5epclndo82mclxklzfytp-3LhDfEmA5Epclndo82MclXkLzFy_tp",
                    }
                    t_str = str(trip_id).strip()
                    canon_trip = known_aliases.get(t_str.lower(), t_str)
                    query = """
                    SELECT 
                        h.id, h.username, h.name, h.clean_name, h.location,
                        h.profile_url, h.stay_request_url, h.references_count,
                        h.response_rate_pct, h.instagram_handle, h.instagram_url,
                        COALESCE(h.about_me_clean, h.about_me) as about_me_clean, h.about_me,
                        pa.estimated_gender, pa.host_type, pa.sleeping_arrangement,
                        pa.house_rules, pa.secret_code_detected, pa.secret_code_hint,
                        pa.conversation_hooks_json, pa.compatibility_points_json,
                        pa.conversation_strategy,
                        COALESCE(ch.message_draft, gm.message_text) as message_text,
                        COALESCE(gm.check_in, th.arrival_date) as check_in,
                        COALESCE(gm.check_out, th.departure_date) as check_out,
                        gm.anchor_used, gm.host_fact_used, gm.user_fact_used, gm.confidence, gm.fallback_used,
                        COALESCE(ch.state, sq.status) as status, sq.updated_at,
                        th.city as trip_city, th.arrival_date as trip_arrival, th.departure_date as trip_departure, th.trip_id as linked_trip_id,
                        COALESCE(ch.is_verified, 0) as is_verified,
                        COALESCE(ch.score_stars, 4) as score_stars,
                        ch.languages_json as languages_json,
                        ch.created_at as host_created_at,
                        COALESCE(sq.priority, 0) as priority
                    FROM sending_queue sq
                    JOIN hosts h ON sq.host_id = h.id
                    JOIN trip_hosts th ON (sq.host_id = th.host_id OR LOWER(sq.host_id) = LOWER(th.host_id))
                    LEFT JOIN core_hosts ch ON (LOWER(ch.host_id) = LOWER(h.id) OR LOWER(ch.username) = LOWER(h.username))
                    LEFT JOIN profile_analyses pa ON h.id = pa.host_id
                    LEFT JOIN generated_messages gm ON sq.current_message_id = gm.id
                    WHERE LOWER(th.trip_id) = LOWER(?)
                    ORDER BY COALESCE(sq.priority, 0) DESC, COALESCE(ch.score_stars, 4) DESC, ch.created_at ASC;
                    """
                    cur.execute(query, (canon_trip,))
                else:
                    query = """
                    SELECT 
                        h.id, h.username, h.name, h.clean_name, h.location,
                        h.profile_url, h.stay_request_url, h.references_count,
                        h.response_rate_pct, h.instagram_handle, h.instagram_url,
                        COALESCE(h.about_me_clean, h.about_me) as about_me_clean, h.about_me,
                        pa.estimated_gender, pa.host_type, pa.sleeping_arrangement,
                        pa.house_rules, pa.secret_code_detected, pa.secret_code_hint,
                        pa.conversation_hooks_json, pa.compatibility_points_json,
                        pa.conversation_strategy,
                        COALESCE(ch.message_draft, gm.message_text) as message_text,
                        gm.check_in, gm.check_out,
                        gm.anchor_used, gm.host_fact_used, gm.user_fact_used, gm.confidence, gm.fallback_used,
                        COALESCE(ch.state, sq.status) as status, sq.updated_at,
                        th.trip_city, th.trip_arrival, th.trip_departure, th.linked_trip_id,
                        COALESCE(ch.is_verified, 0) as is_verified,
                        COALESCE(ch.score_stars, 4) as score_stars,
                        ch.languages_json as languages_json,
                        ch.created_at as host_created_at,
                        COALESCE(sq.priority, 0) as priority
                    FROM sending_queue sq
                    JOIN hosts h ON sq.host_id = h.id
                    LEFT JOIN core_hosts ch ON (LOWER(ch.host_id) = LOWER(h.id) OR LOWER(ch.username) = LOWER(h.username))
                    LEFT JOIN (
                        SELECT LOWER(host_id) as h_key,
                               MAX(city) as trip_city,
                               MAX(arrival_date) as trip_arrival,
                               MAX(departure_date) as trip_departure,
                               MAX(trip_id) as linked_trip_id
                        FROM trip_hosts
                        GROUP BY LOWER(host_id)
                    ) th ON LOWER(sq.host_id) = th.h_key
                    LEFT JOIN profile_analyses pa ON h.id = pa.host_id
                    LEFT JOIN generated_messages gm ON sq.current_message_id = gm.id
                    ORDER BY sq.priority DESC, h.references_count DESC;
                    """
                    cur.execute(query)
                rows = cur.fetchall()

                all_sent_set = self.get_sent_usernames()
                result = []
                for r in rows:
                    u = r["username"]
                    is_sent = u.lower() in all_sent_set
                    if exclude_sent and is_sent:
                        continue

                    hooks = []
                    if r["conversation_hooks_json"]:
                        try:
                            hooks = json.loads(r["conversation_hooks_json"])
                        except Exception:
                            pass

                    compat = []
                    if r["compatibility_points_json"]:
                        try:
                            compat = json.loads(r["compatibility_points_json"])
                        except Exception:
                            pass

                    r_keys = r.keys()
                    t_city = r["trip_city"] if ("trip_city" in r_keys and r["trip_city"]) else None
                    t_arr = r["trip_arrival"] if ("trip_arrival" in r_keys and r["trip_arrival"]) else None
                    t_dep = r["trip_departure"] if ("trip_departure" in r_keys and r["trip_departure"]) else None
                    t_linked = r["linked_trip_id"] if ("linked_trip_id" in r_keys and r["linked_trip_id"]) else None
                    eff_loc = r["location"] or t_city or ""
                    eff_city = t_city or (str(eff_loc).split(",")[0].strip() if eff_loc else "")

                    langs = []
                    if "languages_json" in r_keys and r["languages_json"]:
                        try:
                            langs = json.loads(r["languages_json"])
                        except Exception:
                            pass
                    langs_str = ", ".join(langs[:2]) if langs else "Not specified"

                    raw_st = str(r["status"] or "Draft").strip().lower()
                    st_map = {
                        "approved": "Approved",
                        "sent": "Sent",
                        "was sent": "Sent",
                        "skipped": "Skipped",
                        "failed": "Skipped",
                        "rejected": "Rejected",
                        "selected": "Queued",
                        "queued": "Queued",
                        "drafted": "Draft",
                        "draft": "Draft"
                    }
                    mapped_status = "Sent" if is_sent else st_map.get(raw_st, r["status"] or "Draft")

                    item = {
                        "id": r["id"],
                        "username": u,
                        "name": r["name"],
                        "clean_name": r["clean_name"],
                        "estimated_gender": r["estimated_gender"] or "Unknown",
                        "host_type": r["host_type"] or "solo",
                        "location": eff_loc,
                        "city": eff_city,
                        "trip_id": t_linked,
                        "sleeping_arrangement": r["sleeping_arrangement"] or "Not specified",
                        "house_rules": r["house_rules"] or "Standard house rules",
                        "profile_url": r["profile_url"],
                        "stay_request_url": r["stay_request_url"],
                        "instagram_handle": r["instagram_handle"] or "",
                        "instagram_url": r["instagram_url"] or "",
                        "references_count": r["references_count"] or 0,
                        "response_rate_pct": r["response_rate_pct"] or 90.0,
                        "secret_code_detected": bool(r["secret_code_detected"]),
                        "secret_code_hint": r["secret_code_hint"] or "",
                        "about_me": r["about_me"] or r["about_me_clean"] or "",
                        "about_me_clean": r["about_me_clean"] or r["about_me"] or "",
                        "check_in": t_arr or r["check_in"] or DEFAULT_CHECK_IN,
                        "check_out": t_dep or r["check_out"] or DEFAULT_CHECK_OUT,
                        "guest_context": DEFAULT_CONTEXT,
                        "generated_message": r["message_text"] or "",
                        "word_count": len((r["message_text"] or "").split()),
                        "anchor": r["anchor_used"] if ("anchor_used" in r.keys() and r["anchor_used"]) else None,
                        "anchor_used": r["anchor_used"] if ("anchor_used" in r.keys() and r["anchor_used"]) else None,
                        "host_fact_used": r["host_fact_used"] if ("host_fact_used" in r.keys() and r["host_fact_used"]) else None,
                        "user_fact_used": r["user_fact_used"] if ("user_fact_used" in r.keys() and r["user_fact_used"]) else None,
                        "confidence": r["confidence"] if ("confidence" in r.keys() and r["confidence"] is not None) else None,
                        "fallback_used": bool(r["fallback_used"]) if ("fallback_used" in r.keys() and r["fallback_used"] is not None) else False,
                        "status": mapped_status,
                        "updated_at": r["updated_at"],
                        "score_stars": r["score_stars"] if ("score_stars" in r_keys and r["score_stars"]) else 4,
                        "is_verified": bool(r["is_verified"]) if ("is_verified" in r_keys and r["is_verified"]) else False,
                        "languages": langs_str,
                        "created_at": r["host_created_at"] if "host_created_at" in r_keys else (r["updated_at"] or ""),
                        "priority": int(r["priority"]) if ("priority" in r_keys and r["priority"] is not None) else 0,
                        # Интеллектуальный блок
                        "conversation_hooks": hooks,
                        "compatibility_points": compat,
                        "conversation_strategy": r["conversation_strategy"] or "Grounded CS connection",
                    }
                    result.append(item)

                return result
        except Exception as e:
            logger.warning(f"Ошибка загрузки очереди из SQLite: {e}, фолбэк на JSON")

        # Фолбэк на чтение JSON, если база недоступна
        if self.queue_file.exists():
            try:
                with open(self.queue_file, "r", encoding="utf-8") as f:
                    q = json.load(f)
                all_sent_set = self.get_sent_usernames()
                filtered = []
                for item in q:
                    u = (item.get("username") or item.get("id") or "").lower()
                    is_sent = u in all_sent_set
                    if exclude_sent and is_sent:
                        continue
                    if is_sent:
                        item["status"] = "Sent"
                    filtered.append(item)
                return filtered
            except Exception:
                pass
        return []

    def get_ready_dispatch_candidates(self, trip_id: Optional[str] = None, mutate_status: bool = False) -> List[Dict[str, Any]]:
        """
        Единый канонический список кандидатов, готовых к отправке автопилотом.
        Используется И в /api/autopilot/status (для отображения списка),
        И в auto_sender.py (для непосредственной отправки).
        При фоновом чтении статуса (mutate_status=False) НЕ перезаписывает пользовательские статусы в SQLite.
        """
        from llm_agent import validate_message_qc
        from message_generator import verify_host_name

        all_sent = self.get_sent_usernames()
        queue = self.load_queue(exclude_sent=False, trip_id=trip_id)
        candidates = []
        seen_first_sentences = set()

        for item in queue:
            u = (item.get("username") or item.get("id") or "").strip().lower()
            if not u or u in all_sent:
                continue
            st = str(item.get("status") or "").strip().lower()
            if st not in ("approved", "queued"):
                continue

            # 1. Проверка надежности отображаемого имени
            first_name, is_trusted = verify_host_name(item)
            if not is_trusted or not first_name:
                if mutate_status:
                    logger.warning(f"Хост @{u} исключен из отправки: сомнительное имя '{item.get('name')}'.")
                    self.update_status(u, "Name_Review_Required", notes="Untrusted display name; manual review required.")
                continue

            # 2. Проверка наличия и качества сгенерированного сообщения
            msg = item.get("generated_message") or ""
            if not msg.strip():
                if mutate_status:
                    logger.warning(f"Хост @{u} исключен из отправки: пустое сообщение.")
                    self.update_status(u, "Draft", notes="Missing message text.")
                continue

            bio_str = (item.get("about_me_clean") or item.get("about_me") or "").strip()
            is_minimal = len(bio_str) < 25 or bio_str.lower().startswith("friendly local resident")
            qc_ok, qc_issues = validate_message_qc(
                msg,
                is_minimal_bio=is_minimal,
                host_data=item,
                trip_city=item.get("city") or item.get("trip_city"),
            )
            if not qc_ok and st != "approved":
                if mutate_status:
                    logger.warning(f"Хост @{u} исключен из отправки: QC issues ({qc_issues}).")
                    self.update_status(u, "QC_Review_Required", notes=f"Pre-dispatch QC: {', '.join(qc_issues)}")
                continue

            # 3. Фиксация вступительной фразы для статистики (без отбрасывания одобренных кандидатов)
            msg_lines = [line.strip() for line in msg.split("\n") if line.strip()]
            first_sentence = ""
            for line in msg_lines:
                if not line.startswith("Hi ") and not line.startswith("Hello"):
                    first_sentence = line.split(".")[0].strip().lower()
                    break
            if first_sentence:
                seen_first_sentences.add(first_sentence)

            candidates.append(item)

        candidates.sort(
            key=lambda x: (
                int(x.get("attempts") or 0),
                -int(x.get("priority") or 0),
                -int(x.get("score_stars") or 4),
                x.get("created_at") or x.get("updated_at") or "",
                -int(x.get("references_count") or 0),
            )
        )
        return candidates

    def save_queue(self, queue: List[Dict[str, Any]]) -> None:
        """Сохраняет очередь на диск в JSON (для обратной совместимости) с файловым_lock и атомарной записью."""
        self.queue_file.parent.mkdir(parents=True, exist_ok=True)
        tmp_file = self.queue_file.with_suffix(".json.tmp")
        with open(tmp_file, "w", encoding="utf-8") as f:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            except (OSError, ImportError):
                pass
            try:
                json.dump(queue, f, ensure_ascii=False, indent=2)
            finally:
                try:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                except (OSError, ImportError):
                    pass
        os.replace(tmp_file, self.queue_file)

    def update_status(self, username: str, status: str, notes: Optional[str] = None) -> None:
        """Обновляет статус и заметки в SQLite sending_queue и синхронизирует в единый источник истины core_hosts."""
        now_iso = datetime.now().isoformat()
        u_clean = str(username or "").strip()
        try:
            with db_session() as conn:
                cur = conn.cursor()
                if notes is not None:
                    cur.execute(
                        """
                        UPDATE sending_queue 
                        SET status = ?, notes = ?, updated_at = ?
                        WHERE host_id IN (SELECT id FROM hosts WHERE username = ? COLLATE NOCASE OR id = ? COLLATE NOCASE) OR host_id = ? COLLATE NOCASE;
                        """,
                        (status, notes, now_iso, username, username, username),
                    )
                else:
                    cur.execute(
                        """
                        UPDATE sending_queue 
                        SET status = ?, updated_at = ?
                        WHERE host_id IN (SELECT id FROM hosts WHERE username = ? COLLATE NOCASE OR id = ? COLLATE NOCASE) OR host_id = ? COLLATE NOCASE;
                        """,
                        (status, now_iso, username, username, username),
                    )

                # Синхронизация в core_hosts (Единый источник истины по ТЗ)
                status_lower = str(status).strip().lower()
                st_map = {
                    "approved": "approved",
                    "sent": "sent",
                    "was sent": "sent",
                    "skipped": "skipped",
                    "failed": "skipped",
                    "rejected": "rejected",
                    "queued": "selected",
                    "draft": "drafted"
                }
                c_st = st_map.get(status_lower)
                if c_st:
                    cur.execute("""
                        UPDATE core_hosts
                        SET state = ?, updated_at = ?
                        WHERE (LOWER(username) = LOWER(?) OR LOWER(host_id) = LOWER(?))
                          AND state != 'sent';
                    """, (c_st, now_iso, u_clean, u_clean))
        except Exception as e:
            logger.error(f"Ошибка обновления статуса в SQLite: {e}")

    def update_item(
        self,
        username: str,
        message: Optional[str] = None,
        status: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Обновляет статус и/или текст сообщения в SQLite и JSON."""
        now_iso = datetime.now().isoformat()
        u_clean = str(username or "").strip()
        try:
            with db_session() as conn:
                cur = conn.cursor()
                if status is not None:
                    cur.execute(
                        """
                        UPDATE sending_queue 
                        SET status = ?, updated_at = ?
                        WHERE host_id IN (SELECT id FROM hosts WHERE LOWER(username) = LOWER(?) OR LOWER(id) = LOWER(?))
                           OR LOWER(host_id) = LOWER(?);
                        """,
                        (status, now_iso, u_clean, u_clean, u_clean),
                    )

                if message is not None:
                    words = len(message.split())
                    cur.execute(
                        """
                        UPDATE generated_messages 
                        SET message_text = ?, word_count = ?, qc_passed = 1
                        WHERE id IN (
                            SELECT current_message_id FROM sending_queue sq
                            LEFT JOIN hosts h ON sq.host_id = h.id
                            WHERE LOWER(h.username) = LOWER(?) OR LOWER(h.id) = LOWER(?) OR LOWER(sq.host_id) = LOWER(?)
                        );
                        """,
                        (message, words, u_clean, u_clean, u_clean),
                    )
                    if cur.rowcount == 0:
                        # Если current_message_id был NULL или отсутствовал в generated_messages — создаём новую запись и привязываем
                        cur.execute(
                            """
                            SELECT sq.host_id, COALESCE(th.arrival_date, ?), COALESCE(th.departure_date, ?)
                            FROM sending_queue sq
                            LEFT JOIN hosts h ON sq.host_id = h.id
                            LEFT JOIN trip_hosts th ON LOWER(sq.host_id) = LOWER(th.host_id)
                            WHERE LOWER(h.username) = LOWER(?) OR LOWER(h.id) = LOWER(?) OR LOWER(sq.host_id) = LOWER(?)
                            LIMIT 1;
                            """,
                            (DEFAULT_CHECK_IN, DEFAULT_CHECK_OUT, u_clean, u_clean, u_clean),
                        )
                        sq_row = cur.fetchone()
                        if sq_row:
                            actual_host_id, c_in, c_out = sq_row[0], sq_row[1], sq_row[2]
                            new_msg_id = f"msg_{actual_host_id}_{int(datetime.now().timestamp())}"
                            cur.execute(
                                """
                                INSERT OR REPLACE INTO generated_messages (
                                    id, host_id, check_in, check_out, message_text,
                                    word_count, qc_passed, is_active, created_at
                                ) VALUES (?, ?, ?, ?, ?, ?, 1, 1, ?);
                                """,
                                (new_msg_id, actual_host_id, c_in, c_out, message, words, now_iso),
                            )
                            cur.execute(
                                """
                                UPDATE sending_queue
                                SET current_message_id = ?, updated_at = ?
                                WHERE host_id = ?;
                                """,
                                (new_msg_id, now_iso, actual_host_id),
                            )
                    else:
                        cur.execute(
                            """
                            UPDATE sending_queue
                            SET updated_at = ?
                            WHERE host_id IN (SELECT id FROM hosts WHERE LOWER(username) = LOWER(?) OR LOWER(id) = LOWER(?))
                               OR LOWER(host_id) = LOWER(?);
                            """,
                            (now_iso, u_clean, u_clean, u_clean),
                        )
        except Exception as e:
            logger.error(f"Ошибка обновления карточки в SQLite: {e}")

        # Синхронизация с JSON
        items = self.load_queue(exclude_sent=False)
        target = None
        u_low = u_clean.lower()
        for item in items:
            if str(item.get("username") or "").strip().lower() == u_low or str(item.get("id") or "").strip().lower() == u_low:
                if message is not None:
                    item["generated_message"] = message
                    item["word_count"] = len(message.split())
                if status is not None:
                    item["status"] = status
                item["updated_at"] = now_iso
                target = item
                break

        self.save_queue(items)
        return target

    def regenerate_item(
        self,
        username: str,
        check_in: Optional[str] = None,
        check_out: Optional[str] = None,
        guest_context: Optional[str] = None,
        trip_id: Optional[str] = None,
        city: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Перегенерирует персонализированный текст запроса для конкретного хоста через LLM/Fallback
        и сохраняет изменения в SQLite и JSON.
        """
        now_iso = datetime.now().isoformat()

        # 1. Поиск хоста в текущей очереди
        items = self.load_queue(exclude_sent=False, trip_id=trip_id) if trip_id else self.load_queue(exclude_sent=False)
        target_item = None
        for item in items:
            if (
                str(item.get("username", "")).strip().lower() == username.strip().lower()
                or str(item.get("id", "")).strip().lower() == username.strip().lower()
            ):
                target_item = item
                break

        if not target_item and trip_id:
            all_items = self.load_queue(exclude_sent=False)
            for item in all_items:
                if (
                    str(item.get("username", "")).strip().lower() == username.strip().lower()
                    or str(item.get("id", "")).strip().lower() == username.strip().lower()
                ):
                    target_item = item
                    break

        # 2. Если в очереди нет, пробуем загрузить из curated или raw файлов
        if not target_item:
            for fpath in [CURATED_FILE, RAW_FILE]:
                if fpath.exists():
                    try:
                        with open(fpath, "r", encoding="utf-8") as f:
                            base_hosts = json.load(f)
                        for h in base_hosts:
                            if (
                                str(h.get("username", "")).strip().lower() == username.strip().lower()
                                or str(h.get("id", "")).strip().lower() == username.strip().lower()
                            ):
                                target_item = dict(h)
                                break
                    except Exception:
                        pass
                if target_item:
                    break

        if not target_item:
            # Поиск напрямую в SQLite таблице hosts
            try:
                with db_session() as conn:
                    cur = conn.cursor()
                    cur.execute("SELECT * FROM hosts WHERE LOWER(username) = LOWER(?) OR LOWER(id) = LOWER(?);", (username, username))
                    row = cur.fetchone()
                    if row:
                        target_item = dict(row)
            except Exception as e:
                logger.warning(f"Ошибка поиска хоста {username} в SQLite hosts: {e}")

        if not target_item:
            logger.error(f"Хост {username} не найден ни в очереди, ни в базе данных")
            return None

        # Подтягиваем город и даты из trip_hosts (по активному trip_id или по хосту)
        db_trip_city = None
        db_trip_arr = None
        db_trip_dep = None
        try:
            with db_session() as conn:
                cur = conn.cursor()
                if trip_id:
                    known_aliases = {
                        "3lhdfema5epclndo82mclxklzfytp": "kyoto-trip-3lhdfema5epclndo82mclxklzfytp-3LhDfEmA5Epclndo82MclXkLzFy_tp",
                        "3lhdfema5epclndo82mclxk_tp": "kyoto-trip-3lhdfema5epclndo82mclxklzfytp-3LhDfEmA5Epclndo82MclXkLzFy_tp",
                        "kyoto-trip-3lhdfema5epclndo82mclxklzfytp-3lhdfema5epclndo82mclxklzfytp": "kyoto-trip-3lhdfema5epclndo82mclxklzfytp-3LhDfEmA5Epclndo82MclXkLzFy_tp",
                    }
                    canon_t = known_aliases.get(str(trip_id).strip().lower(), str(trip_id).strip())
                    cur.execute(
                        "SELECT city, arrival_date, departure_date FROM user_trips WHERE LOWER(trip_id) = LOWER(?) LIMIT 1;",
                        (canon_t,),
                    )
                    t_row = cur.fetchone()
                    if not t_row:
                        cur.execute(
                            """
                            SELECT (SELECT city FROM trip_hosts WHERE LOWER(trip_id) = LOWER(?) AND city IS NOT NULL AND TRIM(city) != '' GROUP BY city ORDER BY count(*) DESC LIMIT 1),
                                   (SELECT arrival_date FROM trip_hosts WHERE LOWER(trip_id) = LOWER(?) AND arrival_date IS NOT NULL AND TRIM(arrival_date) != '' GROUP BY arrival_date ORDER BY count(*) DESC LIMIT 1),
                                   (SELECT departure_date FROM trip_hosts WHERE LOWER(trip_id) = LOWER(?) AND departure_date IS NOT NULL AND TRIM(departure_date) != '' GROUP BY departure_date ORDER BY count(*) DESC LIMIT 1);
                            """,
                            (canon_t, canon_t, canon_t),
                        )
                        t_row = cur.fetchone()
                    if t_row and (t_row[0] or t_row[1]):
                        db_trip_city, db_trip_arr, db_trip_dep = t_row[0], t_row[1], t_row[2]
                if not db_trip_city:
                    h_lookup = str(target_item.get("id") or username).strip()
                    cur.execute(
                        "SELECT NULLIF(TRIM(city), ''), NULLIF(TRIM(arrival_date), ''), NULLIF(TRIM(departure_date), '') FROM trip_hosts WHERE LOWER(host_id) = LOWER(?) OR LOWER(host_id) = LOWER(?) LIMIT 1;",
                        (h_lookup, username.strip()),
                    )
                    th_row = cur.fetchone()
                    if th_row:
                        db_trip_city, db_trip_arr, db_trip_dep = th_row[0], th_row[1], th_row[2]
        except Exception as e:
            logger.warning(f"Ошибка чтения trip_hosts при регенерации: {e}")

        # Разрешаем даты и город динамически: приоритет у переданных и Trip Card
        if check_in in (None, "", DEFAULT_CHECK_IN) and (db_trip_arr or target_item.get("arrival_date") or target_item.get("check_in")):
            check_in = db_trip_arr or target_item.get("arrival_date") or target_item.get("check_in") or DEFAULT_CHECK_IN
        else:
            check_in = check_in or db_trip_arr or target_item.get("check_in") or target_item.get("arrival_date") or DEFAULT_CHECK_IN

        if check_out in (None, "", DEFAULT_CHECK_OUT) and (db_trip_dep or target_item.get("departure_date") or target_item.get("check_out")):
            check_out = db_trip_dep or target_item.get("departure_date") or target_item.get("check_out") or DEFAULT_CHECK_OUT
        else:
            check_out = check_out or db_trip_dep or target_item.get("check_out") or target_item.get("departure_date") or DEFAULT_CHECK_OUT

        guest_context = guest_context or target_item.get("guest_context") or DEFAULT_CONTEXT
        effective_city = (city or db_trip_city or target_item.get("city") or str(target_item.get("location") or "Kyoto").split(",")[0].strip() or "Kyoto").strip()
        target_item["city"] = effective_city

        # Обогащаем биографией и актуальным id из базы hosts или файлов, если в очереди пусто
        if len(target_item.get("about_me_clean") or target_item.get("about_me") or "") < 20:
            try:
                with db_session() as conn:
                    cur = conn.cursor()
                    cur.execute("SELECT id, about_me, about_me_clean, location FROM hosts WHERE LOWER(username) = LOWER(?) OR LOWER(id) = LOWER(?);", (username, username))
                    h_row = cur.fetchone()
                    if h_row:
                        b_text = h_row["about_me_clean"] or h_row["about_me"] or ""
                        if b_text:
                            target_item["about_me_clean"] = b_text
                            target_item["about_me"] = b_text
                        if not target_item.get("location") and h_row["location"]:
                            target_item["location"] = h_row["location"]
                        if h_row["id"]:
                            target_item["id"] = h_row["id"]
            except Exception as e:
                logger.warning(f"Ошибка обогащения из hosts: {e}")

        if len(target_item.get("about_me_clean") or target_item.get("about_me") or "") < 20:
            for fpath in [CURATED_FILE, RAW_FILE]:
                if fpath.exists():
                    try:
                        with open(fpath, "r", encoding="utf-8") as f:
                            base_hosts = json.load(f)
                        for h in base_hosts:
                            if str(h.get("username", "")).strip().lower() == username.strip().lower():
                                b_text = h.get("about_me_clean") or h.get("about_me") or ""
                                if b_text:
                                    target_item["about_me_clean"] = b_text
                                    target_item["about_me"] = b_text
                                    break
                    except Exception:
                        pass
                if len(target_item.get("about_me_clean") or target_item.get("about_me") or "") >= 20:
                    break

        # Нормализуем поля хоста через data_cleaner
        from data_cleaner import sanitize_host_name, sanitize_host_clean_name, sanitize_host_location
        raw_name = target_item.get("name") or username
        target_item["name"] = sanitize_host_name(raw_name)
        target_item["clean_name"] = sanitize_host_clean_name(target_item.get("clean_name") or raw_name)
        target_item["location"] = sanitize_host_location(target_item.get("location"), effective_city)

        # Валидация достоверного имени хоста перед формированием промпта
        from message_generator import verify_host_name
        first_name, is_trusted_name = verify_host_name(target_item)
        if is_trusted_name and first_name:
            target_item["clean_name"] = first_name
        target_item["is_name_trusted"] = is_trusted_name

        # 3. Вызов генератора сообщений через LLM Agent (с fallback на rule-based)
        from llm_agent import get_llm_agent, validate_message_qc
        llm = get_llm_agent()
        msg_text = llm.generate_request(
            host=target_item,
            check_in=check_in,
            check_out=check_out,
            guest_context=guest_context,
            include_dates=False,
        )

        words = len(msg_text.strip().split()) if msg_text else 0
        existing_msg = target_item.get("generated_message") or ""
        if (not msg_text or not msg_text.strip()) and existing_msg.strip():
            logger.warning(f"Генерация вернула пустой ответ для @{username}, сохраняем существующий черновик.")
            msg_text = existing_msg
            words = len(msg_text.strip().split())

        bio_str = (target_item.get("about_me_clean") or target_item.get("about_me") or "").strip()
        is_minimal = len(bio_str) < 25 or bio_str.lower().startswith("friendly local resident")
        qc_passed, qc_issues = validate_message_qc(
            msg_text,
            is_minimal_bio=is_minimal,
            host_data=target_item,
            trip_city=target_item.get("city") or target_item.get("trip_city"),
        )

        if not is_trusted_name:
            item_status = "Name_Review_Required"
            item_notes = "Valid host display name not found in profile; manual name review required before sending."
        elif not qc_passed:
            item_status = "QC_Review_Required"
            item_notes = f"QC issues: {', '.join(qc_issues)}"
        else:
            item_status = "Draft"
            item_notes = "Regenerated successfully with Context Assembler & Host Dossier."

        # Извлекаем фактические метаданные Context Assembler для данного хоста
        from context_assembler import get_context_assembler
        assembler = get_context_assembler()
        loc_city = str(target_item.get("location") or "Kyoto").split(",")[0].strip() or "Kyoto"
        trip_ctx = {
            "city": target_item.get("city") or loc_city,
            "arrival_date": check_in,
            "departure_date": check_out,
        }
        assembled = assembler.assemble_context(target_item, trip_context=trip_ctx)
        fallback_used = assembled.get("fallback_used", False)
        intersections = assembled.get("intersections", [])

        from message_generator import select_primary_hook
        primary = select_primary_hook(target_item)
        if primary["type"] != "empty_bio":
            anchor_used = primary["type"].replace("_", " ").title()
            host_fact_used = primary["hook_detail"]
            user_fact_used = primary["andrey_fact"]
            conf = 0.92
            fallback_val = 0
        else:
            anchor_used = "Warm & Respectful Introduction"
            host_fact_used = "Considerate guest note for minimal bio"
            user_fact_used = "Respectful guest (quiet hours, self-sufficient)"
            conf = 0.6
            fallback_val = 1

        target_item["generated_message"] = msg_text
        target_item["check_in"] = check_in
        target_item["check_out"] = check_out
        target_item["word_count"] = words
        target_item["qc_passed"] = qc_passed
        target_item["status"] = item_status
        target_item["notes"] = item_notes
        target_item["is_name_trusted"] = is_trusted_name
        target_item["anchor"] = anchor_used
        target_item["anchor_used"] = anchor_used
        target_item["host_fact_used"] = host_fact_used
        target_item["user_fact_used"] = user_fact_used
        target_item["confidence"] = conf
        target_item["fallback_used"] = bool(fallback_val)
        target_item["updated_at"] = now_iso

        # 4. Обновление SQLite
        host_id = target_item.get("id") or target_item.get("username") or username
        try:
            with db_session() as conn:
                cur = conn.cursor()
                cur.execute("SELECT id FROM hosts WHERE id = ? OR username = ?;", (host_id, username))
                existing_host = cur.fetchone()
                if existing_host:
                    actual_host_id = existing_host[0]
                    cur.execute(
                        """
                        UPDATE hosts SET
                            name = COALESCE(?, name),
                            clean_name = COALESCE(?, clean_name),
                            profile_url = COALESCE(?, profile_url),
                            stay_request_url = COALESCE(?, stay_request_url),
                            location = COALESCE(?, location),
                            about_me = COALESCE(NULLIF(?, ''), about_me),
                            references_count = COALESCE(?, references_count),
                            response_rate_pct = COALESCE(?, response_rate_pct),
                            updated_at = ?
                        WHERE id = ?;
                        """,
                        (
                            target_item.get("name"),
                            target_item.get("clean_name"),
                            target_item.get("profile_url") or f"https://www.couchsurfing.com/c/users/{username}",
                            target_item.get("stay_request_url") or f"https://www.couchsurfing.com/c/users/{username}/stay-request",
                            target_item.get("location"),
                            target_item.get("about_me", ""),
                            target_item.get("references_count", 0),
                            float(target_item.get("response_rate_pct", 90.0)),
                            now_iso,
                            actual_host_id,
                        ),
                    )
                    host_id = actual_host_id
                else:
                    cur.execute(
                        """
                        INSERT INTO hosts (
                            id, username, name, clean_name, profile_url, stay_request_url,
                            location, about_me, references_count, response_rate_pct,
                            first_scraped_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                        """,
                        (
                            host_id,
                            target_item.get("username", username),
                            target_item["name"],
                            target_item["clean_name"],
                            target_item.get("profile_url") or f"https://www.couchsurfing.com/c/users/{username}",
                            target_item.get("stay_request_url") or f"https://www.couchsurfing.com/c/users/{username}/stay-request",
                            target_item["location"],
                            target_item.get("about_me", ""),
                            target_item.get("references_count", 0),
                            float(target_item.get("response_rate_pct", 90.0)),
                            now_iso,
                            now_iso,
                        ),
                    )

                msg_id = f"msg_{host_id}_{int(datetime.now().timestamp())}"

                # Деактивируем предыдущие сообщения для данного хоста
                cur.execute("UPDATE generated_messages SET is_active = 0 WHERE host_id = ?;", (host_id,))

                # Добавляем generated_messages
                cur.execute(
                    """
                    INSERT OR REPLACE INTO generated_messages (
                        id, host_id, check_in, check_out, message_text,
                        word_count, hook_used, context_facts_used_json,
                        anchor_used, host_fact_used, user_fact_used, confidence, fallback_used,
                        qc_passed, is_active, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?);
                    """,
                    (
                        msg_id,
                        host_id,
                        check_in,
                        check_out,
                        msg_text,
                        words,
                        anchor_used,
                        json.dumps([user_fact_used] if user_fact_used else []),
                        anchor_used,
                        host_fact_used,
                        user_fact_used,
                        conf,
                        fallback_val,
                        1 if qc_passed else 0,
                        now_iso,
                    ),
                )

                # Обновляем sending_queue
                cur.execute(
                    """
                    INSERT INTO sending_queue (id, host_id, current_message_id, status, notes, priority, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, 10, ?, ?)
                    ON CONFLICT(host_id) DO UPDATE SET
                        current_message_id = excluded.current_message_id,
                        status = excluded.status,
                        notes = excluded.notes,
                        updated_at = excluded.updated_at;
                    """,
                    (
                        f"q_{host_id}",
                        host_id,
                        msg_id,
                        item_status,
                        item_notes,
                        now_iso,
                        now_iso,
                    ),
                )
        except Exception as e:
            logger.error(f"Ошибка сохранения регенерации в SQLite: {e}")

        # 5. Сохранение в JSON очереди
        queue_replaced = False
        for i, q in enumerate(items):
            if (
                str(q.get("username", "")).strip().lower() == username.strip().lower()
                or str(q.get("id", "")).strip().lower() == username.strip().lower()
            ):
                items[i] = target_item
                queue_replaced = True
                break
        if not queue_replaced:
            items.insert(0, target_item)

        self.save_queue(items)
        return target_item

    def approve_all_drafts(self, trip_id: Optional[str] = None) -> int:
        """Одобряет все проверенные черновики в очереди (для указанного трипа или всех) и переводит их в статус Approved."""
        now_iso = datetime.now().isoformat()
        approved_count = 0

        queue = self.load_queue(exclude_sent=False, trip_id=trip_id)
        for item in queue:
            st = str(item.get("status") or "").strip().lower()
            if st != "draft":
                continue

            host_id = item.get("id") or item.get("username")
            u = item.get("username") or host_id
            if not u:
                continue

            self.update_status(u, "Approved", notes="Approved by user batch approval.")
            approved_count += 1

        self.save_queue(self.load_queue(exclude_sent=False, trip_id=trip_id))
        logger.info(f"[APPROVE_ALL] Одобрено {approved_count} черновиков (trip_id={trip_id}).")
        return approved_count

    def cleanup_blacklisted_from_queue(self) -> int:
        """Однократная очистка текущей очереди: Approved/Queued хосты с черносписочной bio → Skipped."""
        now_iso = datetime.now().isoformat()
        skipped_count = 0

        # 1. SQLite: sending_queue + hosts
        try:
            with db_session() as conn:
                cur = conn.cursor()
                cur.execute(
                    """
                    SELECT sq.host_id, h.username, h.about_me, h.about_me_clean
                    FROM sending_queue sq
                    LEFT JOIN hosts h ON h.id = sq.host_id
                    WHERE LOWER(sq.status) IN ('approved', 'queued');
                    """
                )
                candidates = cur.fetchall()
                for host_id, username, about_me, about_me_clean in candidates:
                    bio = about_me_clean or about_me or ""
                    blacklisted_phrase = is_blacklisted(bio)
                    if blacklisted_phrase:
                        cur.execute(
                            """
                            UPDATE sending_queue
                            SET status = 'Skipped', notes = ?, updated_at = ?
                            WHERE host_id = ? AND LOWER(status) IN ('approved', 'queued');
                            """,
                            (f"Not open to hosting: '{blacklisted_phrase}'", now_iso, host_id),
                        )
                        logger.info(
                            f"[CLEANUP] Хост @{username or host_id} переведён в Skipped: стоп-флаг '{blacklisted_phrase}'."
                        )
                        skipped_count += 1
        except Exception as e:
            logger.warning(f"Ошибка очистки черного списка в SQLite: {e}")

        # 2. JSON queue
        if QUEUE_FILE.exists():
            try:
                queue = json.loads(QUEUE_FILE.read_text(encoding="utf-8"))
                changed = False
                for item in queue:
                    st = str(item.get("status", "")).lower()
                    if st in ("approved", "queued"):
                        bio = str(item.get("about_me_clean") or item.get("about_me") or "")
                        blacklisted_phrase = is_blacklisted(bio)
                        if blacklisted_phrase:
                            item["status"] = "Skipped"
                            item["notes"] = f"Not open to hosting: '{blacklisted_phrase}'"
                            item["updated_at"] = now_iso
                            changed = True
                            logger.info(
                                f"[CLEANUP] Хост @{item.get('username')} переведён в Skipped: стоп-флаг '{blacklisted_phrase}'."
                            )
                if changed:
                    QUEUE_FILE.write_text(json.dumps(queue, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception as e:
                logger.warning(f"Ошибка очистки черного списка в JSON: {e}")

        if skipped_count:
            logger.info(f"[CLEANUP] Всего переведено в Skipped по черному списку: {skipped_count}.")
        return skipped_count

    def enqueue_hosts(
        self,
        hosts_list: List[Dict[str, Any]],
        trip_id: Optional[str] = None,
        return_details: bool = False,
    ) -> Union[int, Tuple[int, List[str]]]:
        """Массовое добавление хостов в очередь (SQLite + JSON)."""
        if not hosts_list:
            return (0, []) if return_details else 0

        existing = self.load_queue(exclude_sent=False, trip_id=trip_id)
        existing_users = {
            str(x.get("username", "")).strip().lower()
            for x in existing
            if str(x.get("status", "")).strip().lower() in ("draft", "approved", "queued", "name_review_required", "qc_review_required", "needs_regeneration", "sent")
        }
        sent_users = self.get_sent_usernames()

        added_items = []
        now_iso = datetime.now().isoformat()

        try:
            with db_session() as conn:
                cur = conn.cursor()
                db_trip_city = None
                db_trip_arr = None
                db_trip_dep = None
                if trip_id:
                    known_aliases = {
                        "3lhdfema5epclndo82mclxklzfytp": "kyoto-trip-3lhdfema5epclndo82mclxklzfytp-3LhDfEmA5Epclndo82MclXkLzFy_tp",
                        "3lhdfema5epclndo82mclxk_tp": "kyoto-trip-3lhdfema5epclndo82mclxklzfytp-3LhDfEmA5Epclndo82MclXkLzFy_tp",
                        "kyoto-trip-3lhdfema5epclndo82mclxklzfytp-3lhdfema5epclndo82mclxklzfytp": "kyoto-trip-3lhdfema5epclndo82mclxklzfytp-3LhDfEmA5Epclndo82MclXkLzFy_tp",
                    }
                    canon_t = known_aliases.get(str(trip_id).strip().lower(), str(trip_id).strip())
                    cur.execute(
                        "SELECT city, arrival_date, departure_date FROM user_trips WHERE LOWER(trip_id) = LOWER(?) LIMIT 1;",
                        (canon_t,),
                    )
                    t_row = cur.fetchone()
                    if not t_row:
                        cur.execute(
                            """
                            SELECT (SELECT city FROM trip_hosts WHERE LOWER(trip_id) = LOWER(?) AND city IS NOT NULL AND TRIM(city) != '' GROUP BY city ORDER BY count(*) DESC LIMIT 1),
                                   (SELECT arrival_date FROM trip_hosts WHERE LOWER(trip_id) = LOWER(?) AND arrival_date IS NOT NULL AND TRIM(arrival_date) != '' GROUP BY arrival_date ORDER BY count(*) DESC LIMIT 1),
                                   (SELECT departure_date FROM trip_hosts WHERE LOWER(trip_id) = LOWER(?) AND departure_date IS NOT NULL AND TRIM(departure_date) != '' GROUP BY departure_date ORDER BY count(*) DESC LIMIT 1);
                            """,
                            (canon_t, canon_t, canon_t),
                        )
                        t_row = cur.fetchone()
                    if t_row and (t_row[0] or t_row[1]):
                        db_trip_city, db_trip_arr, db_trip_dep = t_row[0], t_row[1], t_row[2]

                for h in hosts_list:
                    host_id = str(h.get("id") or h.get("username") or "").strip()
                    host_username = str(h.get("username") or h.get("id") or "").strip()
                    u = host_username or host_id
                    if not u or u.lower() in existing_users or host_id.lower() in existing_users or u.lower() in sent_users or host_id.lower() in sent_users:
                        continue

                    # Черный список: хост явно не принимает гостей — не добавляем в очередь.
                    blacklisted_phrase = is_blacklisted(h.get("about_me", "") or h.get("about_me_clean", ""))
                    if blacklisted_phrase:
                        logger.info(
                            f"[FILTER] Пропущен хост @{u}: обнаружен стоп-флаг '{blacklisted_phrase}' (not open to hosting)."
                        )
                        continue

                    clean_name = h.get("clean_name") or h.get("name") or host_username
                    first_name = clean_name.split()[0] if clean_name else host_username
                    loc = h.get("location") or db_trip_city or "Kyoto, Japan"
                    if isinstance(loc, dict):
                        loc = loc.get("locationDescription", {}).get("description", db_trip_city or "Kyoto")
                    eff_city = h.get("city") or db_trip_city or str(loc).split(",")[0].strip() or "Kyoto"
                    h["city"] = eff_city

                    msg_id = f"msg_{host_username}_{uuid.uuid4().hex[:6]}"
                    # Даты берём из трипа/хоста, чтобы автопилот отправлял на нужные даты
                    check_in = str(h.get("arrival_date") or h.get("check_in") or db_trip_arr or DEFAULT_CHECK_IN).strip()
                    check_out = str(h.get("departure_date") or h.get("check_out") or db_trip_dep or DEFAULT_CHECK_OUT).strip()

                    # Извлекаем персональный контекст через Context Assembler
                    from context_assembler import get_context_assembler
                    assembler = get_context_assembler()
                    trip_ctx = {
                        "city": eff_city,
                        "arrival_date": check_in,
                        "departure_date": check_out,
                    }
                    assembled = assembler.assemble_context(h, trip_context=trip_ctx)
                    intersections = assembled.get("intersections", [])
                    from message_generator import select_primary_hook
                    primary = select_primary_hook(h)
                    if primary["type"] != "empty_bio":
                        anchor_used = primary["type"].replace("_", " ").title()
                        host_fact_used = primary["hook_detail"]
                        user_fact_used = primary["andrey_fact"]
                        conf = 0.92
                        fb_val = 0
                    else:
                        anchor_used = "Warm & Respectful Introduction"
                        host_fact_used = "Considerate guest note for minimal bio"
                        user_fact_used = "Respectful guest (quiet hours, self-sufficient)"
                        conf = 0.6
                        fb_val = 1

                    # Генерация первого сообщения (даты не дублируются в теле письма)
                    default_msg = generate_stay_request(h, check_in=check_in, check_out=check_out, include_dates=False)

                    # 1. Запись в hosts
                    cur.execute(
                        """
                        INSERT OR IGNORE INTO hosts (
                            id, username, name, clean_name, profile_url, stay_request_url,
                            location, about_me, about_me_clean, references_count,
                            response_rate_pct, instagram_handle, instagram_url,
                            first_scraped_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                        """,
                        (
                            host_id, host_username, h.get("name") or host_username, first_name,
                            h.get("profile_url") or f"https://www.couchsurfing.com/c/users/{host_username}",
                            h.get("stay_request_url") or f"https://www.couchsurfing.com/c/users/{host_username}/stay-request",
                            str(loc), h.get("about_me", ""), h.get("about_me_clean", ""),
                            h.get("references_count", 0), float(h.get("response_rate_pct", 90.0) or 90.0),
                            h.get("instagram_handle", ""), h.get("instagram_url", ""),
                            now_iso, now_iso,
                        ),
                    )

                    # 1b. Связь с trip_hosts при наличии trip_id
                    if trip_id:
                        cur.execute(
                            """
                            INSERT INTO trip_hosts (trip_id, host_id, city, arrival_date, departure_date, created_at)
                            VALUES (?, ?, ?, ?, ?, ?)
                            ON CONFLICT(trip_id, host_id) DO NOTHING;
                            """,
                            (trip_id, host_id, str(loc), check_in, check_out, now_iso),
                        )

                    # 2. Сохранение сообщения в generated_messages
                    # Выявляем использованные факты из Host Dossier
                    used_dossier_facts = []
                    try:
                        from database import get_host_dossier
                        hd = get_host_dossier(host_username) or get_host_dossier(host_id)
                        if hd:
                            for itm in hd.get("interests", []):
                                if itm.get("confidence", 0) >= 0.8:
                                    used_dossier_facts.append(f"interest:{itm.get('name')}")
                            for vo in hd.get("visual_observations", [])[:3]:
                                used_dossier_facts.append(f"visual:{vo.get('observation')}")
                    except Exception:
                        pass
                    facts_json = json.dumps(used_dossier_facts) if used_dossier_facts else None

                    cur.execute(
                        """
                        INSERT OR REPLACE INTO generated_messages (
                            id, host_id, check_in, check_out, message_text,
                            word_count, hook_used, context_facts_used_json,
                            anchor_used, host_fact_used, user_fact_used, confidence, fallback_used,
                            qc_passed, is_active, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 1, ?);
                        """,
                        (
                            msg_id, host_id, check_in, check_out, default_msg,
                            len(default_msg.split()), anchor_used,
                            json.dumps([user_fact_used] if user_fact_used else []),
                            anchor_used, host_fact_used, user_fact_used, conf, fb_val,
                            now_iso,
                        ),
                    )

                    # 3. Сохранение в sending_queue
                    cur.execute(
                        """
                        INSERT INTO sending_queue (
                            id, host_id, current_message_id, status, priority,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, 'Draft', 10, ?, ?)
                        ON CONFLICT(host_id) DO UPDATE SET 
                            current_message_id = COALESCE(excluded.current_message_id, sending_queue.current_message_id),
                            status = CASE 
                                WHEN LOWER(sending_queue.status) IN ('sent', 'approved', 'qc_review_required', 'name_review_required') 
                                THEN sending_queue.status 
                                ELSE 'Draft' 
                            END,
                            updated_at = excluded.updated_at;
                        """,
                        (f"q_{host_id}", host_id, msg_id, now_iso, now_iso),
                    )

                    item = {
                        "id": host_id,
                        "username": host_username,
                        "name": h.get("name") or host_username,
                        "clean_name": first_name,
                        "profile_url": h.get("profile_url") or f"https://www.couchsurfing.com/c/users/{host_username}",
                        "stay_request_url": h.get("stay_request_url") or f"https://www.couchsurfing.com/c/users/{host_username}/stay-request",
                        "location": str(loc),
                        "references_count": h.get("references_count", 0),
                        "response_rate_pct": float(h.get("response_rate_pct", 90.0) or 90.0),
                        "estimated_gender": h.get("estimated_gender") or h.get("gender") or "unknown",
                        "host_type": h.get("host_type", "solo"),
                        "instagram_handle": h.get("instagram_handle"),
                        "instagram_url": h.get("instagram_url"),
                        "status": "Draft",
                        "check_in": check_in,
                        "check_out": check_out,
                        "message": default_msg,
                        "generated_message": default_msg,
                        "word_count": len(default_msg.split()),
                        "anchor": anchor_used,
                        "anchor_used": anchor_used,
                        "host_fact_used": host_fact_used,
                        "user_fact_used": user_fact_used,
                        "confidence": conf,
                        "fallback_used": bool(fb_val),
                        "qc_passed": True,
                        "created_at": now_iso,
                        "updated_at": now_iso,
                    }
                    added_items.append(item)
                    existing_users.add(host_username.lower())
                    existing_users.add(host_id.lower())
        except Exception as e:
            logger.error(f"Ошибка массового добавления хостов в БД: {e}")

        if added_items:
            full_queue = added_items + existing
            self.save_queue(full_queue)

        if return_details:
            return len(added_items), [x.get("username") or x.get("id") for x in added_items]
        return len(added_items)

    def set_status_for_selected(self, usernames: List[str], status: str) -> int:
        """Массовая установка статуса для выбранных хостов и синхронизация в core_hosts."""
        now_iso = datetime.now().isoformat()
        with db_session() as conn:
            cur = conn.cursor()
            cur.executemany(
                """
                UPDATE sending_queue 
                SET status = ?, updated_at = ?
                WHERE (host_id IN (SELECT id FROM hosts WHERE LOWER(username) = LOWER(?) OR LOWER(id) = LOWER(?)))
                   OR LOWER(host_id) = LOWER(?);
                """,
                [(status, now_iso, u, u, u) for u in usernames],
            )
            count = cur.rowcount

            status_lower = str(status).strip().lower()
            st_map = {
                "approved": "approved",
                "sent": "sent",
                "was sent": "sent",
                "skipped": "skipped",
                "failed": "skipped",
                "rejected": "rejected",
                "queued": "selected",
                "draft": "drafted"
            }
            c_st = st_map.get(status_lower)
            if c_st:
                cur.executemany(
                    """
                    UPDATE core_hosts
                    SET state = ?, updated_at = ?
                    WHERE (LOWER(username) = LOWER(?) OR LOWER(host_id) = LOWER(?))
                      AND state != 'sent';
                    """,
                    [(c_st, now_iso, u, u) for u in usernames]
                )
        self.save_queue(self.load_queue(exclude_sent=False))
        return count


_rqm_instance: Optional[RequestQueueManager] = None


def get_queue_manager() -> RequestQueueManager:
    global _rqm_instance
    if _rqm_instance is None:
        _rqm_instance = RequestQueueManager()
    return _rqm_instance
