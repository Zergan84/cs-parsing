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
OUTPUT_DIR = BASE_DIR / "output"
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

    def __init__(self, queue_file: Path = QUEUE_FILE):
        self.queue_file = Path(queue_file)
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

    def load_queue(self, exclude_sent: bool = True) -> List[Dict[str, Any]]:
        """
        Загружает очередь с обогащением данными хоста, анализа и текущего сообщения.
        """
        try:
            with db_session() as conn:
                cur = conn.cursor()
                query = """
                SELECT 
                    h.id, h.username, h.name, h.clean_name, h.location,
                    h.profile_url, h.stay_request_url, h.references_count,
                    h.response_rate_pct, h.instagram_handle, h.instagram_url,
                    h.about_me_clean,
                    pa.estimated_gender, pa.host_type, pa.sleeping_arrangement,
                    pa.house_rules, pa.secret_code_detected, pa.secret_code_hint,
                    pa.conversation_hooks_json, pa.compatibility_points_json,
                    pa.conversation_strategy,
                    gm.message_text, gm.check_in, gm.check_out,
                    sq.status, sq.updated_at
                FROM sending_queue sq
                JOIN hosts h ON sq.host_id = h.id
                LEFT JOIN profile_analyses pa ON h.id = pa.host_id
                LEFT JOIN generated_messages gm ON sq.current_message_id = gm.id
                ORDER BY sq.priority DESC, h.references_count DESC;
                """
                cur.execute(query)
                rows = cur.fetchall()

                if rows:
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

                        item = {
                            "id": r["id"],
                            "username": u,
                            "name": r["name"],
                            "clean_name": r["clean_name"],
                            "estimated_gender": r["estimated_gender"] or "Unknown",
                            "host_type": r["host_type"] or "solo",
                            "location": r["location"] or "Tokyo",
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
                            "about_me_clean": r["about_me_clean"] or "",
                            "check_in": r["check_in"] or DEFAULT_CHECK_IN,
                            "check_out": r["check_out"] or DEFAULT_CHECK_OUT,
                            "guest_context": DEFAULT_CONTEXT,
                            "generated_message": r["message_text"] or "",
                            "status": "Sent" if is_sent else (r["status"] or "Draft"),
                            "updated_at": r["updated_at"],
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

    def get_ready_dispatch_candidates(self) -> List[Dict[str, Any]]:
        """
        Единый канонический список кандидатов, готовых к отправке автопилотом.
        Используется И в /api/autopilot/status (для отображения списка),
        И в auto_sender.py (для непосредственной отправки).

        1. Загружает очередь через load_queue(exclude_sent=False).
        2. Оставляет только кандидатов со статусом 'Approved' или 'Queued'.
        3. Исключает любых кандидатов, чей username содержится в get_sent_usernames().
        4. Сортирует строго единообразно:
           - attempts ASC (0 попыток первыми; хосты с ошибками уходят в конец)
           - references_count DESC (активные и проверенные хосты выше)
           - updated_at ASC
        """
        all_sent = self.get_sent_usernames()
        queue = self.load_queue(exclude_sent=False)
        candidates = []
        for item in queue:
            u = (item.get("username") or item.get("id") or "").strip().lower()
            if not u or u in all_sent:
                continue
            st = str(item.get("status") or "").strip().lower()
            if st in ("approved", "queued"):
                candidates.append(item)

        candidates.sort(
            key=lambda x: (
                int(x.get("attempts") or 0),
                -int(x.get("references_count") or 0),
                x.get("updated_at") or "",
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

    def update_item(
        self,
        username: str,
        message: Optional[str] = None,
        status: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Обновляет статус и/или текст сообщения в SQLite и JSON."""
        now_iso = datetime.now().isoformat()
        try:
            with db_session() as conn:
                cur = conn.cursor()
                if status is not None:
                    cur.execute(
                        """
                        UPDATE sending_queue 
                        SET status = ?, updated_at = ?
                        WHERE host_id IN (SELECT id FROM hosts WHERE username = ? OR id = ?);
                        """,
                        (status, now_iso, username, username),
                    )

                if message is not None:
                    cur.execute(
                        """
                        UPDATE generated_messages 
                        SET message_text = ?, word_count = ?
                        WHERE id IN (
                            SELECT current_message_id FROM sending_queue sq
                            JOIN hosts h ON sq.host_id = h.id
                            WHERE h.username = ? OR h.id = ?
                        );
                        """,
                        (message, len(message.split()), username, username),
                    )
        except Exception as e:
            logger.error(f"Ошибка обновления карточки в SQLite: {e}")

        # Синхронизация с JSON
        items = self.load_queue(exclude_sent=False)
        target = None
        for item in items:
            if item.get("username") == username:
                if message is not None:
                    item["generated_message"] = message
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
    ) -> Optional[Dict[str, Any]]:
        """
        Перегенерирует персонализированный текст запроса для конкретного хоста через LLM/Fallback
        и сохраняет изменения в SQLite и JSON.
        """
        now_iso = datetime.now().isoformat()

        # 1. Поиск хоста в текущей очереди
        items = self.load_queue(exclude_sent=False)
        target_item = None
        for item in items:
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
                    cur.execute("SELECT * FROM hosts WHERE username = ? OR id = ?;", (username, username))
                    row = cur.fetchone()
                    if row:
                        target_item = dict(row)
            except Exception as e:
                logger.warning(f"Ошибка поиска хоста {username} в SQLite hosts: {e}")

        if not target_item:
            logger.error(f"Хост {username} не найден ни в очереди, ни в базе данных")
            return None

        # Разрешаем даты динамически: приоритет у переданных, затем у сохраненных в карточке хоста
        check_in = check_in or target_item.get("check_in") or target_item.get("arrival_date") or DEFAULT_CHECK_IN
        check_out = check_out or target_item.get("check_out") or target_item.get("departure_date") or DEFAULT_CHECK_OUT
        guest_context = guest_context or target_item.get("guest_context") or DEFAULT_CONTEXT

        # Обогащаем биографией из curated/raw файлов, если она отсутствует или коротка
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
        target_item["location"] = sanitize_host_location(target_item.get("location"), "Tokyo")

        # 3. Вызов генератора сообщений через LLM Agent (с fallback на rule-based)
        from llm_agent import get_llm_agent, validate_message_qc
        llm = get_llm_agent()
        msg_text = llm.generate_request(
            host=target_item,
            check_in=check_in,
            check_out=check_out,
            guest_context=guest_context,
        )

        words = len(msg_text.strip().split())
        qc_passed, qc_issues = validate_message_qc(msg_text)

        target_item["generated_message"] = msg_text
        target_item["check_in"] = check_in
        target_item["check_out"] = check_out
        target_item["word_count"] = words
        target_item["qc_passed"] = qc_passed
        target_item["status"] = target_item.get("status") or "Draft"
        target_item["updated_at"] = now_iso

        # 4. Обновление SQLite
        host_id = target_item.get("id") or target_item.get("username") or username
        msg_id = f"msg_{host_id}_{int(datetime.now().timestamp())}"
        try:
            with db_session() as conn:
                cur = conn.cursor()
                # Обновляем hosts
                cur.execute(
                    """
                    INSERT INTO hosts (
                        id, username, name, clean_name, profile_url, stay_request_url,
                        location, about_me, references_count, response_rate_pct,
                        first_scraped_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET 
                        name = excluded.name,
                        clean_name = excluded.clean_name,
                        location = excluded.location,
                        updated_at = excluded.updated_at;
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

                # Добавляем generated_messages
                cur.execute(
                    """
                    INSERT OR REPLACE INTO generated_messages (
                        id, host_id, check_in, check_out, message_text,
                        word_count, qc_passed, is_active, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?);
                    """,
                    (
                        msg_id,
                        host_id,
                        check_in,
                        check_out,
                        msg_text,
                        words,
                        1 if qc_passed else 0,
                        now_iso,
                    ),
                )

                # Обновляем sending_queue
                cur.execute(
                    """
                    INSERT INTO sending_queue (id, host_id, current_message_id, status, priority, created_at, updated_at)
                    VALUES (?, ?, ?, ?, 10, ?, ?)
                    ON CONFLICT(host_id) DO UPDATE SET
                        current_message_id = excluded.current_message_id,
                        updated_at = excluded.updated_at;
                    """,
                    (
                        f"q_{host_id}",
                        host_id,
                        msg_id,
                        target_item.get("status", "Draft"),
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

    def approve_all_drafts(self) -> int:
        """Одобряет все черновики в очереди, пропуская хосты из черного списка."""
        now_iso = datetime.now().isoformat()
        approved_count = 0
        skipped_count = 0
        with db_session() as conn:
            cur = conn.cursor()
            # Берём черновики вместе с about_me хоста, чтобы проверить черный список.
            cur.execute(
                """
                SELECT sq.host_id, h.about_me, h.about_me_clean
                FROM sending_queue sq
                LEFT JOIN hosts h ON h.id = sq.host_id
                WHERE LOWER(sq.status) = 'draft';
                """
            )
            drafts = cur.fetchall()
            for host_id, about_me, about_me_clean in drafts:
                bio = about_me_clean or about_me or ""
                blacklisted_phrase = is_blacklisted(bio)
                if blacklisted_phrase:
                    cur.execute(
                        """
                        UPDATE sending_queue
                        SET status = 'Skipped', notes = ?, updated_at = ?
                        WHERE host_id = ? AND LOWER(status) = 'draft';
                        """,
                        (f"Not open to hosting: '{blacklisted_phrase}'", now_iso, host_id),
                    )
                    logger.info(
                        f"[FILTER] Пропущен хост @{host_id}: обнаружен стоп-флаг '{blacklisted_phrase}' (not open to hosting)."
                    )
                    skipped_count += 1
                else:
                    cur.execute(
                        "UPDATE sending_queue SET status = 'Approved', updated_at = ? WHERE host_id = ? AND LOWER(status) = 'draft';",
                        (now_iso, host_id),
                    )
                    approved_count += 1
        self.save_queue(self.load_queue(exclude_sent=False))
        logger.info(f"[FILTER] Одобрено: {approved_count}, пропущено по черному списку: {skipped_count}.")
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

    def enqueue_hosts(self, hosts_list: List[Dict[str, Any]]) -> int:
        """Массовое добавление хостов в очередь (SQLite + JSON)."""
        if not hosts_list:
            return 0

        existing = self.load_queue(exclude_sent=False)
        existing_users = {str(x.get("username", "")).strip().lower() for x in existing}
        sent_users = self.get_sent_usernames()

        added_items = []
        now_iso = datetime.now().isoformat()

        try:
            with db_session() as conn:
                cur = conn.cursor()
                for h in hosts_list:
                    u = str(h.get("username") or h.get("id") or "").strip()
                    if not u or u.lower() in existing_users or u.lower() in sent_users:
                        continue

                    # Черный список: хост явно не принимает гостей — не добавляем в очередь.
                    blacklisted_phrase = is_blacklisted(h.get("about_me", "") or h.get("about_me_clean", ""))
                    if blacklisted_phrase:
                        logger.info(
                            f"[FILTER] Пропущен хост @{u}: обнаружен стоп-флаг '{blacklisted_phrase}' (not open to hosting)."
                        )
                        continue

                    clean_name = h.get("clean_name") or h.get("name") or u
                    first_name = clean_name.split()[0] if clean_name else u
                    loc = h.get("location") or "Tokyo, Japan"
                    if isinstance(loc, dict):
                        loc = loc.get("locationDescription", {}).get("description", "Tokyo")

                    msg_id = f"msg_{u}_{uuid.uuid4().hex[:6]}"
                    # Даты берём из трипа/хоста, чтобы автопилот отправлял на нужные даты
                    check_in = str(h.get("arrival_date") or h.get("check_in") or DEFAULT_CHECK_IN).strip()
                    check_out = str(h.get("departure_date") or h.get("check_out") or DEFAULT_CHECK_OUT).strip()
                    default_msg = generate_stay_request(h, check_in=check_in, check_out=check_out)

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
                            u, u, h.get("name") or u, first_name,
                            h.get("profile_url") or f"https://www.couchsurfing.com/c/users/{u}",
                            h.get("stay_request_url") or f"https://www.couchsurfing.com/c/users/{u}/stay-request",
                            str(loc), h.get("about_me", ""), h.get("about_me_clean", ""),
                            h.get("references_count", 0), float(h.get("response_rate_pct", 90.0) or 90.0),
                            h.get("instagram_handle", ""), h.get("instagram_url", ""),
                            now_iso, now_iso,
                        ),
                    )

                    # 2. Сохранение сообщения в generated_messages
                    cur.execute(
                        """
                        INSERT OR REPLACE INTO generated_messages (
                            id, host_id, check_in, check_out, message_text,
                            word_count, qc_passed, is_active, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, 1, 1, ?);
                        """,
                        (msg_id, u, check_in, check_out, default_msg, len(default_msg.split()), now_iso),
                    )

                    # 3. Сохранение в sending_queue
                    cur.execute(
                        """
                        INSERT INTO sending_queue (
                            id, host_id, current_message_id, status, priority,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, 'Draft', 10, ?, ?)
                        ON CONFLICT(host_id) DO UPDATE SET updated_at = excluded.updated_at;
                        """,
                        (f"q_{u}", u, msg_id, now_iso, now_iso),
                    )

                    item = {
                        "id": u,
                        "username": u,
                        "name": h.get("name") or u,
                        "clean_name": first_name,
                        "profile_url": h.get("profile_url") or f"https://www.couchsurfing.com/c/users/{u}",
                        "stay_request_url": h.get("stay_request_url") or f"https://www.couchsurfing.com/c/users/{u}/stay-request",
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
                        "qc_passed": True,
                        "created_at": now_iso,
                        "updated_at": now_iso,
                    }
                    added_items.append(item)
                    existing_users.add(u.lower())
        except Exception as e:
            logger.error(f"Ошибка массового добавления хостов в БД: {e}")

        if added_items:
            full_queue = added_items + existing
            self.save_queue(full_queue)

        return len(added_items)

    def set_status_for_selected(self, usernames: List[str], status: str) -> int:
        """Массовая установка статуса для выбранных хостов."""
        now_iso = datetime.now().isoformat()
        with db_session() as conn:
            cur = conn.cursor()
            cur.executemany(
                """
                UPDATE sending_queue 
                SET status = ?, updated_at = ?
                WHERE host_id IN (SELECT id FROM hosts WHERE username = ? OR id = ?);
                """,
                [(status, now_iso, u, u) for u in usernames],
            )
            count = cur.rowcount
        self.save_queue(self.load_queue(exclude_sent=False))
        return count


_rqm_instance: Optional[RequestQueueManager] = None


def get_queue_manager() -> RequestQueueManager:
    global _rqm_instance
    if _rqm_instance is None:
        _rqm_instance = RequestQueueManager()
    return _rqm_instance
