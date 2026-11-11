"""
core/queue.py - Единый канонический модуль Очереди отправки, Генерации писем и QC-валидации.

Объединяет:
1. `QueueEngine`, `build_personalized_message`, `validate_message_qc` — доменный движок очереди и QC по ТЗ (60–90 слов, контроль дат/города и анти-галлюцинационный фильтр).
2. `RequestQueueManager` (`get_queue_manager`) — управление очередью (`sending_queue`, `core_hosts`, `generated_messages`, `hosts`, `trip_hosts`) с полной синхронизацией между таблицами и защитой инвариантов состояний.
"""

import fcntl
import json
import logging
import os
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union

from core.collector import get_profile_analyzer
from core.context import DEFAULT_GUEST_CONTEXT, GUEST, get_context_assembler, validate_text_purity
from core.database import (
    BASE_DIR,
    OUTPUT_DIR,
    db_session,
    get_connection,
    get_host_dossier,
    get_output_dir,
    get_sent_usernames as db_get_sent_usernames,
    init_db,
    log_audit,
)
from core.models import HostState, Trip

logger = logging.getLogger("CoreQueue")

QUEUE_FILE = OUTPUT_DIR / "requests_queue.json"
CURATED_FILE = OUTPUT_DIR / "hosts_tokyo_curated.json"
RAW_FILE = OUTPUT_DIR / "hosts_tokyo.json"
SENT_HISTORY_FILE = OUTPUT_DIR / "sent_history.json"
SENT_LOG_FILE = OUTPUT_DIR / "sent_log.json"

DEFAULT_CHECK_IN = "2026-11-18"
DEFAULT_CHECK_OUT = "2026-11-20"
DEFAULT_CONTEXT = DEFAULT_GUEST_CONTEXT

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
    """Возвращает сработавшую стоп-фразу или None, если хост подходит."""
    if not about_me:
        return None
    text = str(about_me).strip().lower()
    for phrase in BLACKLIST_PHRASES:
        if phrase in text:
            return phrase
    return None


# =====================================================================
# 1. STRICT MESSAGE GENERATION & QC VALIDATION (DDD ENGINE)
# =====================================================================

def extract_clean_first_name(raw_name: str, username: str) -> str:
    """Извлекает и очищает человеческое имя хоста."""
    clean = (raw_name or "").strip()
    if not clean or clean.lower() == username.lower() or "@" in clean or any(c.isdigit() for c in clean):
        parts = username.replace(".", " ").replace("_", " ").replace("-", " ").split()
        for p in parts:
            if p.isalpha() and len(p) >= 2:
                return p.capitalize()
        return "there"

    first = clean.split()[0].capitalize()
    return first if first.isalpha() else "there"


def build_personalized_message(host: Dict[str, Any], trip: Union[Dict[str, Any], Trip]) -> Tuple[str, bool, str]:
    """
    Формирует выверенное персонализированное сообщение на основе совпавших фактов.
    - Для минимального био (<25 слов): строго 30–70 слов.
    - Для обычного био (25+ слов): целевой диапазон 60–90 слов (допустимо 50–105).
    - Защита от роботизированных клише и запрещенных географических галлюцинаций.
    Возвращает (текст, qc_passed, qc_note).
    """
    first_name = extract_clean_first_name(
        host.get("name") or host.get("display_name") or host.get("clean_name") or "",
        host.get("username", "")
    )
    reasons = host.get("score_reasons") or []
    about_text = str(host.get("about") or host.get("about_me") or host.get("about_me_clean") or "").strip()
    reasons_str = (
        " ".join(reasons) + " " +
        about_text + " " +
        str(host.get("interests_text") or "") + " " +
        str(host.get("occupation") or "")
    ).lower()

    if isinstance(trip, dict):
        city = trip.get("city") or "the city"
        arr = trip.get("arrival_date") or ""
        dep = trip.get("departure_date") or ""
    else:
        city = trip.city or "the city"
        arr = trip.arrival_date or ""
        dep = trip.departure_date or ""

    if arr and dep:
        dates_str = f"({arr} – {dep})"
    elif arr:
        dates_str = f"(around {arr})"
    else:
        dates_str = "for a couple of nights"

    is_minimal = len(about_text.split()) < 25 or about_text.lower().startswith("friendly local resident")

    if is_minimal:
        hook_sentence = "I appreciate your open hospitality on Couchsurfing."
        guest_fact = f"I am visiting {city} to explore its architecture and street photography."
        lines = [
            f"Hi {first_name},",
            "",
            f"{hook_sentence} {guest_fact} Originally from Siberia, I work as an alpine ski instructor and have been on CS for years. I keep a quiet and tidy routine.",
            "",
            f"I will be visiting {city} {dates_str}. If your schedule allows hosting, I would be glad to connect and exchange stories!",
            "",
            "Warmly,",
            "Andrey",
        ]
    else:
        if any(k in reasons_str for k in ["горы", "лыжи", "ski", "mountain", "hiking", "trekking", "alps"]):
            hook_sentence = "Your shared love for the mountains and outdoor adventures really resonated with me."
            guest_fact = "I work as an alpine ski coach in Courchevel (French Alps) and grew up in Siberia, so mountain trails, snow, and outdoor life are second nature to me."
        elif any(k in reasons_str for k in ["кино", "фото", "арт", "photo", "cinema", "film", "art", "camera", "visual"]):
            hook_sentence = "Your creative interests in visual culture, film, and photography really caught my attention."
            guest_fact = "I studied film directing at VGIK and actively practice street photography; visual storytelling and capturing genuine moments are central to how I travel."
        elif any(k in reasons_str for k in ["музыка", "фортепиано", "music", "piano", "jazz", "concert", "sound"]):
            hook_sentence = "I was delighted to see your appreciation for acoustic music and piano."
            guest_fact = "I play the piano and deeply appreciate musical traditions, quiet evening conversations, sound design, and genuine cultural exchange."
        elif any(k in reasons_str for k in ["французский", "french", "france"]):
            hook_sentence = "It was wonderful to notice your connection with the French language and European culture."
            guest_fact = "I work as a ski coach in the French Alps (Courchevel), so sharing perspectives in French or English over local traditions would be a pleasure."
        else:
            hook_sentence = "Your welcoming approach to hosting and cultural exchange immediately stood out to me."
            guest_fact = f"Originally from Siberia, I work as an alpine ski instructor and have been on Couchsurfing for many years. Visiting {city} for photography, architecture, and local culture."

        lines = [
            f"Hi {first_name},",
            "",
            hook_sentence,
            "",
            f"{guest_fact} I'm a seasoned Couchsurfer, quiet, tidy, and respectful of house rules.",
            "",
            f"I will be visiting {city} {dates_str}. If your schedule allows hosting, I'd be glad to connect and exchange stories!",
            "",
            "Warmly,",
            "Andrey"
        ]

    message_text = "\n".join(lines)
    qc_ok, qc_issues = validate_message_qc(message_text, trip, is_minimal_bio=is_minimal)
    qc_note = "Strict QC Passed" if qc_ok else "; ".join(qc_issues)
    return message_text, qc_ok, qc_note


def validate_message_qc(
    text: str,
    trip: Union[Dict[str, Any], Trip],
    min_words: Optional[int] = None,
    max_words: Optional[int] = None,
    is_minimal_bio: bool = False,
) -> Tuple[bool, List[str]]:
    """
    Единый фильтр качества (QC):
    1. Для минимального био (<25 слов): строго 30–70 слов.
    2. Для обычного био: 50–105 слов (целевой диапазон 60–90).
    3. Присутствие города поездки.
    4. Отсутствие выдуманных фактов и роботизированных клише (validate_text_purity).
    """
    issues: List[str] = []
    if not text or not text.strip():
        return False, ["Empty message draft"]

    if min_words is None or max_words is None:
        if is_minimal_bio:
            min_w = 30 if min_words is None else min_words
            max_w = 70 if max_words is None else max_words
        else:
            min_w = 50 if min_words is None else min_words
            max_w = 105 if max_words is None else max_words
    else:
        min_w = min_words
        max_w = max_words

    words = text.strip().split()
    wc = len(words)
    if wc < min_w:
        issues.append(f"Too short ({wc} words, min {min_w})")
    if wc > max_w:
        issues.append(f"Too long ({wc} words, max {max_w})")

    city = trip.get("city") if isinstance(trip, dict) else (trip.city if trip else "")
    if city and city.lower() not in text.lower():
        issues.append(f"Missing target city '{city}' in message")

    pure, purity_issues = validate_text_purity(text)
    if not pure:
        issues.extend(purity_issues)

    return (len(issues) == 0, issues)


# =====================================================================
# 2. QUEUE ENGINE (DDD CORE API)
# =====================================================================

class QueueEngine:
    """Движок управления очередью, генерации писем и их утверждения для активной поездки."""

    def __init__(self, trip_id: str, db_file: Optional[Path | str] = None):
        self.trip_id = trip_id
        self.db_file = Path(db_file) if db_file else None

    def _get_trip(self) -> Optional[Trip]:
        with get_connection(self.db_file) as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM core_trips WHERE id = ?;", (self.trip_id,))
            row = cur.fetchone()
            return Trip(**dict(row)) if row else None

    def list_queue(self, state: Optional[str] = None) -> List[Dict[str, Any]]:
        """Возвращает хостов в очереди ('selected', 'drafted', 'approved')."""
        valid_states = [state] if state else [
            HostState.SELECTED.value,
            HostState.DRAFTED.value,
            HostState.APPROVED.value,
        ]
        placeholders = ",".join("?" for _ in valid_states)
        with get_connection(self.db_file) as conn:
            cur = conn.cursor()
            cur.execute(
                f"""
                SELECT * FROM core_hosts
                WHERE trip_id = ? AND state IN ({placeholders})
                ORDER BY
                    CASE state
                        WHEN 'approved' THEN 1
                        WHEN 'drafted' THEN 2
                        WHEN 'selected' THEN 3
                        ELSE 4
                    END,
                    score_stars DESC,
                    updated_at DESC;
                """,
                [self.trip_id, *valid_states],
            )
            rows = cur.fetchall()
            res = []
            for r in rows:
                d = dict(r)
                d["qc_passed"] = bool(d.get("qc_passed"))
                if "score_reasons_json" in d:
                    d["score_reasons"] = json.loads(d.get("score_reasons_json") or "[]")
                d["word_count"] = len((d.get("message_draft") or "").split())
                res.append(d)
            return res

    def generate_draft(self, host_id: str) -> Optional[Dict[str, Any]]:
        """Генерирует персонализированное письмо для хоста, проверяет QC и сохраняет в БД."""
        trip = self._get_trip()
        if not trip:
            return None

        with get_connection(self.db_file) as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT * FROM core_hosts WHERE trip_id = ? AND (host_id = ? OR LOWER(username) = LOWER(?));",
                (self.trip_id, host_id, host_id),
            )
            row = cur.fetchone()
            if not row:
                return None
            host_dict = dict(row)
            if host_dict.get("score_reasons_json"):
                host_dict["score_reasons"] = json.loads(host_dict.get("score_reasons_json") or "[]")
            actual_host_id = host_dict["host_id"]
            u_name = host_dict["username"]

            draft_text, qc_ok, notes = build_personalized_message(host_dict, trip)
            now = datetime.now().isoformat()

            cur.execute(
                """
                UPDATE core_hosts
                SET state = CASE WHEN state = 'sent' THEN 'sent' ELSE ? END,
                    message_draft = ?,
                    qc_passed = ?,
                    qc_notes = ?,
                    updated_at = ?
                WHERE trip_id = ? AND host_id = ?;
                """,
                (
                    HostState.DRAFTED.value,
                    draft_text,
                    1 if qc_ok else 0,
                    notes,
                    now,
                    self.trip_id,
                    actual_host_id,
                ),
            )

        log_audit(
            trip_id=self.trip_id,
            module="QUEUE",
            event="DRAFT_GENERATED",
            host_id=actual_host_id,
            details={"word_count": len(draft_text.split()), "qc_passed": qc_ok, "qc_notes": notes},
            db_file=self.db_file,
        )
        return {
            "host_id": actual_host_id,
            "text": draft_text,
            "message_draft": draft_text,
            "word_count": len(draft_text.split()),
            "qc_passed": qc_ok,
            "qc_notes": notes,
            "state": HostState.DRAFTED.value,
        }

    def approve(self, host_id: str, edited_text: Optional[str] = None) -> bool:
        """
        Утверждает черновик для отправки автопилотом (state -> 'approved').
        Инвариант: нельзя утвердить письмо, не прошедшее QC, или перезаписать уже отправленного хоста.
        """
        trip = self._get_trip()
        if not trip:
            return False

        now = datetime.now().isoformat()
        with get_connection(self.db_file) as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT host_id, username, state, message_draft, qc_passed FROM core_hosts WHERE trip_id = ? AND (host_id = ? OR LOWER(username) = LOWER(?));",
                (self.trip_id, host_id, host_id),
            )
            row = cur.fetchone()
            if not row:
                return False
            if row["state"] == HostState.SENT.value:
                return False

            actual_host_id = row["host_id"]
            u_name = row["username"]
            final_text = edited_text.strip() if edited_text else (row["message_draft"] or "")
            qc_ok, qc_issues = validate_message_qc(final_text, trip)
            if not qc_ok:
                raise ValueError(f"Cannot approve draft failing QC: {'; '.join(qc_issues)}")

            cur.execute(
                """
                UPDATE core_hosts
                SET state = ?, message_draft = ?, qc_passed = 1, qc_notes = 'Approved by user', updated_at = ?
                WHERE trip_id = ? AND host_id = ? AND state != 'sent';
                """,
                (HostState.APPROVED.value, final_text, now, self.trip_id, actual_host_id),
            )
            updated = cur.rowcount > 0

        if updated:
            log_audit(
                trip_id=self.trip_id,
                module="QUEUE",
                event="MESSAGE_APPROVED",
                host_id=actual_host_id,
                details={"word_count": len(final_text.split())},
                db_file=self.db_file,
            )
        return updated

    def batch_approve(self, host_ids: List[str]) -> int:
        count = 0
        for hid in host_ids:
            try:
                if self.approve(hid):
                    count += 1
            except ValueError:
                continue
        return count

    def skip(self, host_id: str) -> bool:
        """
        Снимает хоста из очереди (state = 'skipped') БЕЗ удаления из БД.
        Инвариант: уже отправленный хост ('sent') не может быть переведен в 'skipped'.
        """
        now = datetime.now().isoformat()
        with get_connection(self.db_file) as conn:
            cur = conn.cursor()
            cur.execute(
                """
                UPDATE core_hosts
                SET state = ?, updated_at = ?
                WHERE trip_id = ? AND (host_id = ? OR LOWER(username) = LOWER(?)) AND state != ?;
                """,
                (HostState.SKIPPED.value, now, self.trip_id, host_id, host_id, HostState.SENT.value),
            )
            updated = cur.rowcount > 0

        if updated:
            log_audit(
                trip_id=self.trip_id,
                module="QUEUE",
                event="HOST_SKIPPED",
                host_id=host_id,
                details={"new_state": HostState.SKIPPED.value},
                db_file=self.db_file,
            )
        return updated

    def clear_queue(self) -> int:
        """
        Переводит всех неотправленных хостов в очереди ('selected', 'drafted', 'approved')
        в статус 'skipped' БЕЗ физического удаления из БД.
        """
        now = datetime.now().isoformat()
        with get_connection(self.db_file) as conn:
            cur = conn.cursor()
            cur.execute(
                """
                UPDATE core_hosts
                SET state = ?, updated_at = ?
                WHERE trip_id = ? AND state IN (?, ?, ?);
                """,
                (
                    HostState.SKIPPED.value,
                    now,
                    self.trip_id,
                    HostState.SELECTED.value,
                    HostState.DRAFTED.value,
                    HostState.APPROVED.value,
                ),
            )
            count = cur.rowcount

        log_audit(
            trip_id=self.trip_id,
            module="QUEUE",
            event="QUEUE_CLEARED",
            details={"skipped_count": count},
            db_file=self.db_file,
        )
        return count


# =====================================================================
# 3. REQUEST QUEUE MANAGER (UNIFIED UI & AUTOPILOT QUEUE SERVICE)
# =====================================================================

class RequestQueueManager:
    """Канонический менеджер очереди запросов, синхронизирующий `core_hosts`, `sending_queue`, `generated_messages` и JSON."""

    def __init__(self, queue_file: Optional[Path] = None):
        out_dir = get_output_dir()
        self.queue_file = Path(queue_file) if queue_file is not None else (out_dir / "requests_queue.json")
        self.queue_file.parent.mkdir(parents=True, exist_ok=True)
        init_db()
        self.sync_from_json_if_db_empty()

    def get_sent_usernames(self) -> Set[str]:
        """Делегирует получение всех отправленных хостов канонической функции в `core.database`."""
        return db_get_sent_usernames()

    def deduplicate_queue_against_sent(self) -> int:
        """
        Сверяет очередь (Approved/Queued) со всеми отправленными хостами и диалогами.
        Переводит отправленных в статус 'Sent' с меткой 'Already Sent (Deduplicated)' в core_hosts.
        """
        sent_set = self.get_sent_usernames()
        if not sent_set:
            return 0

        deduped_count = 0
        now_iso = datetime.now().isoformat()

        try:
            with db_session() as conn:
                cur = conn.cursor()
                for u in sent_set:
                    cur.execute(
                        """
                        UPDATE core_hosts
                        SET state = 'sent', sent_at = COALESCE(sent_at, ?), qc_notes = 'Already Sent (Deduplicated)', updated_at = ?
                        WHERE (LOWER(username) = ? OR LOWER(host_id) = ?)
                          AND state IN ('selected', 'drafted', 'approved');
                        """,
                        (now_iso, now_iso, u, u),
                    )
                    if cur.rowcount > 0:
                        deduped_count += cur.rowcount
        except Exception as e:
            logger.warning(f"Ошибка обновления SQLite при дедупликации: {e}")

        if self.queue_file.exists():
            try:
                with open(self.queue_file, "r", encoding="utf-8") as f:
                    queue = json.load(f)

                updated = False
                for item in queue:
                    u = str(item.get("username") or item.get("id") or "").strip().lower()
                    st = str(item.get("status") or "").lower()
                    if u in sent_set and st in ("approved", "queued"):
                        item["status"] = "Sent"
                        item["notes"] = "Already Sent (Deduplicated)"
                        item["updated_at"] = now_iso
                        updated = True

                if updated:
                    self.save_queue(queue)
            except Exception as e:
                logger.warning(f"Ошибка сохранения requests_queue.json при дедупликации: {e}")

        if deduped_count > 0:
            logger.info(f"[DEDUP] Успешно исключено из очереди {deduped_count} уже отправленных хостов.")
        return deduped_count

    def sync_from_json_if_db_empty(self) -> None:
        """Если база данных пуста, импортирует существующую валидированную очередь из JSON в core_hosts."""
        try:
            with db_session() as conn:
                cur = conn.cursor()
                cur.execute("SELECT COUNT(*) FROM core_hosts;")
                if cur.fetchone()[0] > 0:
                    return

            if not self.queue_file.exists():
                return

            with open(self.queue_file, "r", encoding="utf-8") as f:
                queue = json.load(f)

            if not queue:
                return

            logger.info(f"Миграция {len(queue)} анкет из JSON в базу данных core_hosts...")
            from core.database import get_active_trip
            active_trip = get_active_trip()
            if not active_trip:
                logger.warning("sync_from_json_if_db_empty: NO_ACTIVE_TRIP, пропуск синхронизации без создания фиктивных поездок.")
                return
            target_trip_id = active_trip.id

            with db_session() as conn:
                cur = conn.cursor()

                now_iso = datetime.now().isoformat()
                for item in queue:
                    host_id = str(item.get("id") or item.get("username") or "")
                    username = str(item.get("username") or host_id)
                    name = item.get("name", "")
                    clean_name = item.get("clean_name", name)
                    loc = item.get("location", "Tokyo")
                    if isinstance(loc, dict):
                        loc = loc.get("locationDescription", {}).get("description", "Tokyo")

                    st_raw = str(item.get("status") or "Draft").lower()
                    st_map = {
                        "approved": "approved",
                        "sent": "sent",
                        "was sent": "sent",
                        "skipped": "skipped",
                        "rejected": "rejected",
                        "queued": "selected",
                        "draft": "drafted",
                        "qc_review_required": "drafted",
                        "name_review_required": "discovered",
                    }
                    c_state = st_map.get(st_raw, "selected")
                    msg_text = item.get("generated_message") or ""

                    cur.execute(
                        """
                        INSERT INTO core_hosts (
                            host_id, trip_id, username, name, clean_name, location,
                            about, references_count, response_rate_pct, state,
                            score_stars, priority, attempts, message_draft, qc_passed,
                            qc_notes, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 4, 10, ?, ?, 1, ?, ?, ?)
                        ON CONFLICT(trip_id, host_id) DO NOTHING;
                        """,
                        (
                            host_id,
                            target_trip_id,
                            username,
                            name,
                            clean_name,
                            str(loc),
                            item.get("about_me", ""),
                            item.get("references_count", 0),
                            float(item.get("response_rate_pct", 90.0) or 90.0),
                            c_state,
                            int(item.get("attempts", 0) or 0),
                            msg_text,
                            item.get("notes", ""),
                            now_iso,
                            now_iso,
                        ),
                    )
                    cur.execute(
                        """
                        INSERT OR IGNORE INTO core_host_snapshots (host_id, trip_id, raw_data_json, captured_at)
                        VALUES (?, ?, ?, ?);
                        """,
                        (host_id, target_trip_id, json.dumps(item, ensure_ascii=False), now_iso),
                    )
        except Exception as e:
            logger.error(f"Ошибка миграции JSON в SQLite core_hosts: {e}")

    @staticmethod
    def _resolve_canonical_trip_id(trip_id: str) -> str:
        known_aliases = {
            "3lhdfema5epclndo82mclxklzfytp": "kyoto-trip-3lhdfema5epclndo82mclxklzfytp-3LhDfEmA5Epclndo82MclXkLzFy_tp",
            "3lhdfema5epclndo82mclxk_tp": "kyoto-trip-3lhdfema5epclndo82mclxklzfytp-3LhDfEmA5Epclndo82MclXkLzFy_tp",
            "kyoto-trip-3lhdfema5epclndo82mclxklzfytp-3lhdfema5epclndo82mclxklzfytp": "kyoto-trip-3lhdfema5epclndo82mclxklzfytp-3LhDfEmA5Epclndo82MclXkLzFy_tp",
        }
        t_str = str(trip_id).strip()
        return known_aliases.get(t_str.lower(), t_str)

    def get_queue_stats(self, trip_id: Optional[str] = None) -> Dict[str, Any]:
        """Возвращает согласованную статистику очереди из канонической БД (core_hosts)."""
        try:
            with db_session() as conn:
                cur = conn.cursor()
                where_clauses = []
                params = []
                if trip_id:
                    canon = self._resolve_canonical_trip_id(trip_id)
                    where_clauses.append("LOWER(trip_id) = LOWER(?)")
                    params.append(canon)

                where_str = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
                query = f"""
                SELECT
                    COUNT(CASE WHEN state IN ('selected', 'drafted', 'approved') THEN 1 END) as total,
                    COUNT(CASE WHEN state = 'drafted' AND (message_draft IS NULL OR message_draft = '') THEN 1 END) as needs_regeneration,
                    COUNT(CASE WHEN state = 'discovered' THEN 1 END) as name_review,
                    COUNT(CASE WHEN state = 'drafted' AND qc_passed = 0 THEN 1 END) as qc_review,
                    COUNT(CASE WHEN state = 'drafted' AND qc_passed = 1 THEN 1 END) as draft,
                    COUNT(CASE WHEN state = 'approved' THEN 1 END) as approved,
                    COUNT(CASE WHEN state = 'sent' THEN 1 END) as sent_lifetime,
                    COUNT(CASE WHEN state = 'skipped' THEN 1 END) as skipped
                FROM core_hosts
                {where_str};
                """
                cur.execute(query, params)
                row = cur.fetchone()
                total = int(row[0] or 0)
                needs_regen = int(row[1] or 0)
                name_review = int(row[2] or 0)
                qc_review = int(row[3] or 0)
                draft = int(row[4] or 0)
                approved = int(row[5] or 0)
                sent_lifetime = int(row[6] or 0)
                skipped = int(row[7] or 0)

                today_str = datetime.now().strftime("%Y-%m-%d")
                today_users: Set[str] = set()
                cur.execute(
                    """
                    SELECT LOWER(username) FROM sending_history
                    WHERE timestamp LIKE ?
                      AND UPPER(status) IN ('SENT_SUCCESS', 'SUCCESS', 'DRY_RUN_SUCCESS')
                      AND LOWER(mode) IN ('live', 'dry_run')
                      AND COALESCE(notes, '') NOT LIKE '%Deduplicated%'
                      AND COALESCE(notes, '') NOT LIKE '%Detected as already sent%'
                      AND timestamp NOT LIKE '%19:55:57';
                    """,
                    (f"{today_str}%",),
                )
                for r in cur.fetchall():
                    if r[0]:
                        today_users.add(r[0])

                return {
                    "total": total,
                    "total_active": total,
                    "needs_regeneration": needs_regen,
                    "name_review": name_review,
                    "qc_review": qc_review,
                    "draft": draft,
                    "approved": approved,
                    "sent_today": len(today_users),
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
        """Загружает очередь исключительно из канонической таблицы core_hosts."""
        try:
            with db_session() as conn:
                cur = conn.cursor()
                where_clauses = []
                params = []
                if trip_id:
                    canon_trip = self._resolve_canonical_trip_id(trip_id)
                    where_clauses.append("LOWER(ch.trip_id) = LOWER(?)")
                    params.append(canon_trip)
                if exclude_sent:
                    where_clauses.append("ch.state != 'sent'")

                where_str = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

                query = f"""
                SELECT
                    ch.host_id as id,
                    ch.username,
                    ch.name,
                    ch.clean_name,
                    ch.location,
                    ch.about as about_me,
                    ch.about as about_me_clean,
                    ch.references_count,
                    ch.response_rate_pct,
                    ch.languages_json,
                    ch.state,
                    ch.score_stars,
                    ch.score_reasons_json,
                    ch.priority,
                    ch.attempts,
                    ch.message_draft as message_text,
                    ch.qc_passed,
                    ch.qc_notes,
                    ch.is_verified,
                    ch.created_at as host_created_at,
                    ch.updated_at,
                    ch.sent_at,
                    ch.trip_id as linked_trip_id,
                    ct.city as trip_city,
                    ct.arrival_date as trip_arrival,
                    ct.departure_date as trip_departure
                FROM core_hosts ch
                LEFT JOIN core_trips ct ON ch.trip_id = ct.id
                {where_str}
                ORDER BY COALESCE(ch.priority, 10) DESC, COALESCE(ch.score_stars, 4) DESC, ch.created_at ASC;
                """
                cur.execute(query, params)
                rows = cur.fetchall()

                all_sent_set = self.get_sent_usernames()
                result = []
                seen_usernames = set()
                for r in rows:
                    u = r["username"]
                    u_low = u.lower()
                    if u_low in seen_usernames:
                        continue
                    seen_usernames.add(u_low)

                    is_sent = u_low in all_sent_set or r["state"] == "sent"
                    if exclude_sent and is_sent:
                        continue

                    langs = []
                    if r["languages_json"]:
                        try:
                            langs = json.loads(r["languages_json"])
                        except Exception:
                            pass
                    langs_str = ", ".join(langs[:2]) if langs else "Not specified"

                    raw_st = str(r["state"] or "drafted").strip().lower()
                    st_map = {
                        "approved": "Approved",
                        "sent": "Sent",
                        "skipped": "Skipped",
                        "rejected": "Rejected",
                        "selected": "Queued",
                        "discovered": "Name_Review_Required",
                        "inbox": "Inbox",
                    }
                    if is_sent:
                        mapped_status = "Sent"
                    elif raw_st in st_map:
                        mapped_status = st_map[raw_st]
                    elif raw_st == "drafted":
                        mapped_status = "Draft" if r["qc_passed"] else "QC_Review_Required"
                    else:
                        mapped_status = "Draft"

                    t_city = r["trip_city"]
                    t_arr = r["trip_arrival"]
                    t_dep = r["trip_departure"]
                    eff_loc = r["location"] or t_city or ""
                    eff_city = t_city or (str(eff_loc).split(",")[0].strip() if eff_loc else "")

                    item = {
                        "id": r["id"],
                        "username": u,
                        "name": r["name"] or u,
                        "clean_name": r["clean_name"] or r["name"] or u,
                        "estimated_gender": "Unknown",
                        "host_type": "solo",
                        "location": eff_loc,
                        "city": eff_city,
                        "trip_id": r["linked_trip_id"],
                        "sleeping_arrangement": "Not specified",
                        "house_rules": "Standard house rules",
                        "profile_url": f"https://www.couchsurfing.com/people/{u}",
                        "stay_request_url": f"https://www.couchsurfing.com/people/{u}",
                        "instagram_handle": "",
                        "instagram_url": "",
                        "references_count": r["references_count"] or 0,
                        "response_rate_pct": r["response_rate_pct"] or 90.0,
                        "secret_code_detected": False,
                        "secret_code_hint": "",
                        "about_me": r["about_me"] or "",
                        "about_me_clean": r["about_me_clean"] or "",
                        "check_in": t_arr or DEFAULT_CHECK_IN,
                        "check_out": t_dep or DEFAULT_CHECK_OUT,
                        "guest_context": DEFAULT_CONTEXT,
                        "generated_message": r["message_text"] or "",
                        "word_count": len((r["message_text"] or "").split()),
                        "anchor": "Warm & Respectful Introduction",
                        "anchor_used": "Warm & Respectful Introduction",
                        "host_fact_used": "Considerate guest note",
                        "user_fact_used": "Respectful guest",
                        "confidence": 0.9,
                        "fallback_used": False,
                        "status": mapped_status,
                        "updated_at": r["updated_at"],
                        "score_stars": r["score_stars"] or 4,
                        "is_verified": bool(r["is_verified"]),
                        "languages": langs_str,
                        "created_at": r["host_created_at"] or r["updated_at"],
                        "priority": int(r["priority"] or 10),
                        "attempts": int(r["attempts"] or 0),
                        "qc_passed": bool(r["qc_passed"]),
                        "notes": r["qc_notes"] or "",
                        "conversation_hooks": [],
                        "compatibility_points": [],
                        "conversation_strategy": "Grounded CS connection",
                    }
                    result.append(item)

                return result
        except Exception as e:
            logger.warning(f"Ошибка загрузки очереди из SQLite core_hosts: {e}, фолбэк на JSON")

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
        """Единый канонический список кандидатов, готовых к отправке автопилотом."""
        from llm_agent import validate_message_qc as llm_validate_qc
        from message_generator import verify_host_name

        all_sent = self.get_sent_usernames()
        queue = self.load_queue(exclude_sent=False, trip_id=trip_id)
        candidates = []

        for item in queue:
            u = (item.get("username") or item.get("id") or "").strip().lower()
            if not u or u in all_sent:
                continue
            st = str(item.get("status") or "").strip().lower()
            if st not in ("approved", "queued"):
                continue

            first_name, is_trusted = verify_host_name(item)
            if not is_trusted or not first_name:
                if mutate_status:
                    logger.warning(f"Хост @{u} исключен из отправки: сомнительное имя '{item.get('name')}'.")
                    self.update_status(u, "Name_Review_Required", notes="Untrusted display name; manual review required.")
                continue

            msg = item.get("generated_message") or ""
            if not msg.strip():
                if mutate_status:
                    logger.warning(f"Хост @{u} исключен из отправки: пустое сообщение.")
                    self.update_status(u, "Draft", notes="Missing message text.")
                continue

            bio_str = (item.get("about_me_clean") or item.get("about_me") or "").strip()
            is_minimal = len(bio_str.split()) < 25 or bio_str.lower().startswith("friendly local resident")
            qc_ok, qc_issues = llm_validate_qc(
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
        """Сохраняет очередь на диск в JSON с файловым lock и атомарной записью."""
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
        """
        Обновляет статус и заметки в core_hosts.
        Инвариант: никогда не перезаписывает статус уже отправленного ('sent') хоста на неотправленный.
        """
        now_iso = datetime.now().isoformat()
        u_clean = str(username or "").strip()
        status_lower = str(status or "").strip().lower()
        is_target_sent = status_lower in ("sent", "was sent")

        st_map = {
            "approved": "approved",
            "sent": "sent",
            "was sent": "sent",
            "skipped": "skipped",
            "failed": "skipped",
            "rejected": "rejected",
            "queued": "selected",
            "draft": "drafted",
            "qc_review_required": "drafted",
            "name_review_required": "discovered",
        }
        c_st = st_map.get(status_lower, "selected")

        try:
            with db_session() as conn:
                cur = conn.cursor()
                if is_target_sent:
                    cur.execute(
                        """
                        UPDATE core_hosts
                        SET state = 'sent', sent_at = COALESCE(sent_at, ?), qc_notes = COALESCE(?, qc_notes), updated_at = ?
                        WHERE LOWER(username) = LOWER(?) OR LOWER(host_id) = LOWER(?);
                        """,
                        (now_iso, notes, now_iso, u_clean, u_clean),
                    )
                else:
                    cur.execute(
                        """
                        UPDATE core_hosts
                        SET state = ?, qc_notes = COALESCE(?, qc_notes), updated_at = ?
                        WHERE (LOWER(username) = LOWER(?) OR LOWER(host_id) = LOWER(?))
                          AND state != 'sent';
                        """,
                        (c_st, notes, now_iso, u_clean, u_clean),
                    )
        except Exception as e:
            logger.error(f"Ошибка обновления статуса в SQLite: {e}")

    def update_item(
        self,
        username: str,
        message: Optional[str] = None,
        status: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Обновляет статус и/или текст сообщения в core_hosts и JSON."""
        now_iso = datetime.now().isoformat()
        u_clean = str(username or "").strip()
        try:
            with db_session() as conn:
                cur = conn.cursor()
                if status is not None:
                    status_lower = str(status).strip().lower()
                    is_target_sent = status_lower in ("sent", "was sent")
                    st_map = {
                        "approved": "approved",
                        "sent": "sent",
                        "was sent": "sent",
                        "skipped": "skipped",
                        "failed": "skipped",
                        "rejected": "rejected",
                        "queued": "selected",
                        "draft": "drafted",
                    }
                    c_st = st_map.get(status_lower, "drafted")
                    if is_target_sent:
                        cur.execute(
                            """
                            UPDATE core_hosts
                            SET state = 'sent', sent_at = COALESCE(sent_at, ?), updated_at = ?
                            WHERE LOWER(username) = LOWER(?) OR LOWER(host_id) = LOWER(?);
                            """,
                            (now_iso, now_iso, u_clean, u_clean),
                        )
                    else:
                        cur.execute(
                            """
                            UPDATE core_hosts
                            SET state = ?, updated_at = ?
                            WHERE (LOWER(username) = LOWER(?) OR LOWER(host_id) = LOWER(?))
                              AND state != 'sent';
                            """,
                            (c_st, now_iso, u_clean, u_clean),
                        )

                if message is not None:
                    cur.execute(
                        """
                        UPDATE core_hosts
                        SET message_draft = ?, qc_passed = 1, updated_at = ?
                        WHERE LOWER(username) = LOWER(?) OR LOWER(host_id) = LOWER(?);
                        """,
                        (message, now_iso, u_clean, u_clean),
                    )
        except Exception as e:
            logger.error(f"Ошибка обновления карточки в SQLite: {e}")

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
        """Перегенерирует персонализированный текст запроса и синхронизирует `core_hosts`, `generated_messages`, `sending_queue`."""
        now_iso = datetime.now().isoformat()

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

        if not target_item:
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

        db_trip_city = None
        db_trip_arr = None
        db_trip_dep = None
        try:
            with db_session() as conn:
                cur = conn.cursor()
                if trip_id:
                    canon_t = self._resolve_canonical_trip_id(trip_id)
                    cur.execute(
                        "SELECT city, arrival_date, departure_date FROM core_trips WHERE LOWER(id) = LOWER(?) LIMIT 1;",
                        (canon_t,),
                    )
                    t_row = cur.fetchone()
                    if not t_row:
                        cur.execute(
                            "SELECT city, arrival_date, departure_date FROM user_trips WHERE LOWER(trip_id) = LOWER(?) LIMIT 1;",
                            (canon_t,),
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

        from data_cleaner import sanitize_host_clean_name, sanitize_host_location, sanitize_host_name
        raw_name = target_item.get("name") or username
        target_item["name"] = sanitize_host_name(raw_name)
        target_item["clean_name"] = sanitize_host_clean_name(target_item.get("clean_name") or raw_name)
        target_item["location"] = sanitize_host_location(target_item.get("location"), effective_city)

        from message_generator import select_primary_hook, verify_host_name
        first_name, is_trusted_name = verify_host_name(target_item)
        if is_trusted_name and first_name:
            target_item["clean_name"] = first_name
        target_item["is_name_trusted"] = is_trusted_name

        from llm_agent import get_llm_agent, validate_message_qc as llm_validate_qc
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
            msg_text = existing_msg
            words = len(msg_text.strip().split())

        bio_str = (target_item.get("about_me_clean") or target_item.get("about_me") or "").strip()
        is_minimal = len(bio_str.split()) < 25 or bio_str.lower().startswith("friendly local resident")
        qc_passed, qc_issues = llm_validate_qc(
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

        host_id = target_item.get("id") or target_item.get("username") or username
        try:
            with db_session() as conn:
                cur = conn.cursor()
                cur.execute(
                    """
                    UPDATE core_hosts
                    SET message_draft = ?,
                        qc_passed = ?,
                        qc_notes = ?,
                        updated_at = ?
                    WHERE (LOWER(username) = LOWER(?) OR LOWER(host_id) = LOWER(?))
                      AND state != 'sent';
                    """,
                    (msg_text, 1 if qc_passed else 0, item_notes, now_iso, username, host_id),
                )
        except Exception as e:
            logger.error(f"Ошибка сохранения регенерации в SQLite core_hosts: {e}")

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
        approved_count = 0
        queue = self.load_queue(exclude_sent=False, trip_id=trip_id)
        for item in queue:
            st = str(item.get("status") or "").strip().lower()
            if st != "draft":
                continue
            u = item.get("username") or item.get("id")
            if not u:
                continue
            self.update_status(u, "Approved", notes="Approved by user batch approval.")
            approved_count += 1

        self.save_queue(self.load_queue(exclude_sent=False, trip_id=trip_id))
        logger.info(f"[APPROVE_ALL] Одобрено {approved_count} черновиков (trip_id={trip_id}).")
        return approved_count

    def cleanup_blacklisted_from_queue(self) -> int:
        now_iso = datetime.now().isoformat()
        skipped_count = 0
        try:
            with db_session() as conn:
                cur = conn.cursor()
                cur.execute(
                    """
                    SELECT host_id, username, about
                    FROM core_hosts
                    WHERE state IN ('selected', 'drafted', 'approved');
                    """
                )
                for host_id, username, about in cur.fetchall():
                    blacklisted_phrase = is_blacklisted(about or "")
                    if blacklisted_phrase:
                        cur.execute(
                            """
                            UPDATE core_hosts
                            SET state = 'skipped', qc_notes = ?, updated_at = ?
                            WHERE (LOWER(host_id) = LOWER(?) OR LOWER(username) = LOWER(?))
                              AND state != 'sent';
                            """,
                            (f"Not open to hosting: '{blacklisted_phrase}'", now_iso, host_id, username or host_id),
                        )
                        skipped_count += 1
        except Exception as e:
            logger.warning(f"Ошибка очистки черного списка в SQLite: {e}")

        return skipped_count

    def enqueue_hosts(
        self,
        hosts_list: List[Dict[str, Any]],
        trip_id: Optional[str] = None,
        return_details: bool = False,
    ) -> Union[int, Tuple[int, List[str]]]:
        """Массовое добавление хостов в очередь (core_hosts, core_host_snapshots)."""
        if not hosts_list:
            return (0, []) if return_details else 0

        from core.database import get_active_trip
        active_trip = get_active_trip()
        target_trip_id = trip_id or (active_trip.id if active_trip else None)
        if not target_trip_id:
            logger.error("enqueue_hosts: NO_ACTIVE_TRIP, невозможно добавить хостов в очередь без активной поездки.")
            raise ValueError("NO_ACTIVE_TRIP")

        existing = self.load_queue(exclude_sent=False, trip_id=target_trip_id)
        existing_users = {
            str(x.get("username", "")).strip().lower()
            for x in existing
        }
        sent_users = self.get_sent_usernames()

        added_items = []
        now_iso = datetime.now().isoformat()

        try:
            with db_session() as conn:
                cur = conn.cursor()
                cur.execute("SELECT id, city, arrival_date, departure_date FROM core_trips WHERE id = ?;", (target_trip_id,))
                t_row = cur.fetchone()
                t_city = t_row["city"] if t_row else "City"
                t_arr = t_row["arrival_date"] if t_row else DEFAULT_CHECK_IN
                t_dep = t_row["departure_date"] if t_row else DEFAULT_CHECK_OUT

                for h in hosts_list:
                    host_id = str(h.get("id") or h.get("username") or "").strip()
                    host_username = str(h.get("username") or h.get("id") or "").strip()
                    u = host_username or host_id
                    if not u or u.lower() in existing_users or host_id.lower() in existing_users or u.lower() in sent_users or host_id.lower() in sent_users:
                        continue

                    blacklisted_phrase = is_blacklisted(h.get("about_me", "") or h.get("about", "") or h.get("about_me_clean", ""))
                    if blacklisted_phrase:
                        continue

                    clean_name = h.get("clean_name") or h.get("name") or host_username
                    first_name = clean_name.split()[0] if clean_name else host_username
                    loc = h.get("location") or t_city
                    if isinstance(loc, dict):
                        loc = loc.get("locationDescription", {}).get("description", t_city)
                    about_text = h.get("about_me") or h.get("about") or h.get("about_me_clean") or ""

                    draft_text, qc_ok, qc_note = build_personalized_message(h, {"city": t_city, "arrival_date": t_arr, "departure_date": t_dep})

                    cur.execute(
                        """
                        INSERT INTO core_hosts (
                            host_id, trip_id, username, name, clean_name, location,
                            about, references_count, response_rate_pct, languages_json,
                            state, score_stars, score_reasons_json, priority, attempts,
                            message_draft, qc_passed, qc_notes, is_verified, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 10, 0, ?, ?, ?, 0, ?, ?)
                        ON CONFLICT(trip_id, host_id) DO UPDATE SET
                            message_draft = COALESCE(excluded.message_draft, core_hosts.message_draft),
                            updated_at = excluded.updated_at;
                        """,
                        (
                            host_id,
                            target_trip_id,
                            host_username,
                            h.get("name") or host_username,
                            first_name,
                            str(loc),
                            about_text,
                            h.get("references_count", 0),
                            float(h.get("response_rate_pct", 90.0) or 90.0),
                            json.dumps(h.get("languages") or []),
                            "selected",
                            h.get("score_stars", 4),
                            json.dumps(h.get("score_reasons") or []),
                            draft_text,
                            1 if qc_ok else 0,
                            qc_note,
                            now_iso,
                            now_iso,
                        ),
                    )

                    cur.execute(
                        """
                        INSERT OR IGNORE INTO core_host_snapshots (host_id, trip_id, raw_data_json, captured_at)
                        VALUES (?, ?, ?, ?);
                        """,
                        (host_id, target_trip_id, json.dumps(h, ensure_ascii=False), now_iso),
                    )

                    added_items.append(host_username)
                    existing_users.add(host_username.lower())
                    existing_users.add(host_id.lower())
        except Exception as e:
            logger.error(f"Ошибка массового добавления хостов в core_hosts: {e}")

        self.save_queue(self.load_queue(exclude_sent=False, trip_id=target_trip_id))
        if return_details:
            return len(added_items), added_items
        return len(added_items)

    def set_status_for_selected(self, usernames: List[str], status: str) -> int:
        """Массовая установка статуса для выбранных хостов в core_hosts."""
        now_iso = datetime.now().isoformat()
        status_lower = str(status).strip().lower()
        is_target_sent = status_lower in ("sent", "was sent")

        st_map = {
            "approved": "approved",
            "sent": "sent",
            "was sent": "sent",
            "skipped": "skipped",
            "failed": "skipped",
            "rejected": "rejected",
            "queued": "selected",
            "draft": "drafted",
        }
        c_st = st_map.get(status_lower, "selected")

        with db_session() as conn:
            cur = conn.cursor()
            if is_target_sent:
                cur.executemany(
                    """
                    UPDATE core_hosts
                    SET state = 'sent', sent_at = COALESCE(sent_at, ?), updated_at = ?
                    WHERE LOWER(username) = LOWER(?) OR LOWER(host_id) = LOWER(?);
                    """,
                    [(now_iso, now_iso, u, u) for u in usernames],
                )
            else:
                cur.executemany(
                    """
                    UPDATE core_hosts
                    SET state = ?, updated_at = ?
                    WHERE (LOWER(username) = LOWER(?) OR LOWER(host_id) = LOWER(?))
                      AND state != 'sent';
                    """,
                    [(c_st, now_iso, u, u) for u in usernames],
                )
            count = cur.rowcount

        self.save_queue(self.load_queue(exclude_sent=False))
        return count

    def skip_item(self, username: str, reason: str = "User skipped") -> bool:
        """Переводит хоста в статус 'skipped' в core_hosts."""
        now_iso = datetime.now().isoformat()
        with db_session() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                UPDATE core_hosts
                SET state = 'skipped', qc_notes = ?, updated_at = ?
                WHERE (LOWER(username) = LOWER(?) OR LOWER(host_id) = LOWER(?))
                  AND state != 'sent';
                """,
                (reason, now_iso, username, username),
            )
            ok = cur.rowcount > 0
        if ok:
            self.save_queue(self.load_queue(exclude_sent=False))
        return ok

    def delete_item(self, username: str) -> bool:
        """Переводит хоста в статус 'skipped' (не удаляя физически)."""
        return self.skip_item(username)


_rqm_instance: Optional[RequestQueueManager] = None


def get_queue_manager() -> RequestQueueManager:
    global _rqm_instance
    if _rqm_instance is None:
        _rqm_instance = RequestQueueManager()
    return _rqm_instance
