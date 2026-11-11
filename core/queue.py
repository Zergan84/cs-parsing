"""
core/queue.py - Модуль QUEUE + GENERATE.
Отвечает за:
1. Генерацию персонализированных писем для отобранных хостов (state = 'selected').
2. Использование проверенных фактов из CONTEXT (без галлюцинаций).
3. Strict QC (60-90 слов, вежливый тон, проверка чистоты текста).
4. Утверждение (Approve) гостем для передачи в АВТОПИЛОТ.
"""

import json
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from pathlib import Path
from core.context import GUEST, validate_text_purity
from core.database import get_connection, log_audit
from core.models import HostState


def extract_clean_first_name(raw_name: str, username: str) -> str:
    """Извлекает и очищает человеческое имя хоста."""
    clean = (raw_name or "").strip()
    if not clean or clean.lower() == username.lower() or "@" in clean or any(c.isdigit() for c in clean):
        parts = username.replace(".", " ").replace("_", " ").replace("-", " ").split()
        for p in parts:
            if p.isalpha() and len(p) >= 2:
                return p.capitalize()
        return "there"
    
    # Берем первое слово имени
    first = clean.split()[0].capitalize()
    return first if first.isalpha() else "there"


def build_personalized_message(host: Dict[str, Any], trip: Dict[str, Any]) -> Tuple[str, bool, str]:
    """
    Формирует выверенное персонализированное сообщение на основе совпавших фактов.
    Возвращает (текст, qc_passed, qc_note).
    """
    first_name = extract_clean_first_name(host.get("name", ""), host.get("username", ""))
    reasons = host.get("score_reasons") or []
    
    # Выбор подходящего крючка (hook) на основе реальных совпадений
    reasons_str = " ".join(reasons).lower()
    
    city = trip.get("city") or "the city"
    arr = trip.get("arrival_date") or ""
    dep = trip.get("departure_date") or ""
    if arr and dep:
        dates_str = f"({arr} – {dep})"
    elif arr:
        dates_str = f"(around {arr})"
    else:
        dates_str = "for a couple of nights"

    if "горы" in reasons_str or "лыжи" in reasons_str:
        hook_sentence = "Your passion for the mountains and outdoor adventures immediately caught my eye."
        guest_fact = "I'm a professional ski instructor based in Geneva and the French Alps, so spending time in nature is close to my heart."
    elif "кино" in reasons_str or "фото" in reasons_str or "арт" in reasons_str:
        hook_sentence = "Your creative interests in visual culture, film, and photography really resonated with me."
        guest_fact = "I'm traveling from Geneva (Switzerland) where I studied film directing and practice street photography; visual culture is central to my journey."
    elif "музыка" in reasons_str or "фортепиано" in reasons_str:
        hook_sentence = "I was delighted to see your appreciation for music and acoustic arts."
        guest_fact = "I play the piano and deeply appreciate musical traditions, quiet conversations, and cultural exchange."
    elif "французский" in reasons_str:
        hook_sentence = "It was wonderful to notice your connection with the French language and European culture."
        guest_fact = "I'm based in Geneva (Switzerland) and Courchevel, so sharing cultural perspectives would be a genuine pleasure."
    else:
        hook_sentence = "Your welcoming approach to hosting and cultural exchange immediately stood out to me."
        guest_fact = f"I'm traveling from Geneva (Switzerland) to experience {city}'s local atmosphere, photography, and cultural traditions."

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

    # Strict QC проверка
    words = len(message_text.split())
    is_pure, violations = validate_text_purity(message_text)
    
    if not is_pure:
        return message_text, False, f"Violation: forbidden terms found ({violations})"
    
    if words < 50:
        return message_text, False, f"Too short ({words} words, minimum 50)"
    if words > 105:
        return message_text, False, f"Too long ({words} words, maximum 105)"

    return message_text, True, f"QC OK ({words} words)"


class QueueEngine:
    """Движок подготовки сообщений и утверждения очереди."""

    def __init__(self, trip_id: str, db_file: Optional[Path] = None):
        self.trip_id = trip_id
        self.db_file = db_file

    def list_queue(self, state: Optional[str] = None) -> List[Dict[str, Any]]:
        """Возвращает хостов в очереди (selected, drafted, approved)."""
        with get_connection(self.db_file) as conn:
            cur = conn.cursor()
            if state:
                cur.execute(
                    """
                    SELECT * FROM core_hosts 
                    WHERE trip_id = ? AND state = ?
                    ORDER BY score_stars DESC, updated_at DESC;
                    """,
                    (self.trip_id, state)
                )
            else:
                cur.execute(
                    """
                    SELECT * FROM core_hosts 
                    WHERE trip_id = ? AND state IN ('selected', 'drafted', 'approved')
                    ORDER BY score_stars DESC, updated_at DESC;
                    """,
                    (self.trip_id,)
                )
            rows = cur.fetchall()
            return [dict(r) for r in rows]

    def generate_draft(self, host_id: str) -> Optional[Dict[str, Any]]:
        """Генерирует выверенное письмо для одного отобранного хоста."""
        with get_connection(self.db_file) as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM core_trips WHERE id = ?;", (self.trip_id,))
            trip_row = cur.fetchone()
            if not trip_row:
                return None
            trip_dict = dict(trip_row)

            cur.execute("SELECT * FROM core_hosts WHERE trip_id = ? AND host_id = ?;", (self.trip_id, host_id))
            host_row = cur.fetchone()
            if not host_row:
                return None
            host_dict = dict(host_row)
            host_dict["score_reasons"] = json.loads(host_dict.get("score_reasons_json") or "[]")

        text, qc_ok, qc_note = build_personalized_message(host_dict, trip_dict)
        now_iso = datetime.now().isoformat()

        with get_connection(self.db_file) as conn:
            conn.execute(
                """
                UPDATE core_hosts
                SET message_draft = ?, qc_passed = ?, qc_notes = ?, state = 'drafted', updated_at = ?
                WHERE trip_id = ? AND host_id = ?;
                """,
                (text, 1 if qc_ok else 0, qc_note, now_iso, self.trip_id, host_id)
            )

        log_audit(
            trip_id=self.trip_id,
            module="QUEUE",
            event="DRAFT_GENERATED",
            host_id=host_id,
            details={"qc_passed": qc_ok, "qc_notes": qc_note},
            db_file=self.db_file
        )
        return {"host_id": host_id, "text": text, "qc_passed": qc_ok, "qc_notes": qc_note}

    def approve(self, host_id: str, edited_text: Optional[str] = None) -> bool:
        """Утверждает письмо (ручной Approve) и переводит в статус 'approved' для Автопилота."""
        now_iso = datetime.now().isoformat()
        with get_connection(self.db_file) as conn:
            cur = conn.cursor()
            if edited_text is not None:
                is_pure, violations = validate_text_purity(edited_text)
                if not is_pure:
                    raise ValueError(f"Forbidden terms detected in edited text: {violations}")
                cur.execute(
                    """
                    UPDATE core_hosts
                    SET message_draft = ?, state = 'approved', updated_at = ?
                    WHERE trip_id = ? AND host_id = ?;
                    """,
                    (edited_text, now_iso, self.trip_id, host_id)
                )
            else:
                cur.execute(
                    """
                    UPDATE core_hosts
                    SET state = 'approved', updated_at = ?
                    WHERE trip_id = ? AND host_id = ?;
                    """,
                    (now_iso, self.trip_id, host_id)
                )
            affected = cur.rowcount > 0

        if affected:
            log_audit(
                trip_id=self.trip_id,
                module="QUEUE",
                event="MESSAGE_APPROVED",
                host_id=host_id,
                db_file=self.db_file
            )
        return affected

    def batch_approve(self, host_ids: List[str]) -> int:
        """Пакетное утверждение писем."""
        count = 0
        for hid in host_ids:
            if self.approve(hid):
                count += 1
        return count

    def skip(self, host_id: str) -> bool:
        """Снимает хоста из очереди отправки, переводя в статус 'skipped' (БЕЗ УДАЛЕНИЯ ИЗ БД)."""
        now_iso = datetime.now().isoformat()
        with get_connection(self.db_file) as conn:
            cur = conn.cursor()
            cur.execute("""
                UPDATE core_hosts
                SET state = 'skipped', updated_at = ?
                WHERE trip_id = ? AND (host_id = ? OR username = ?) AND state != 'sent';
            """, (now_iso, self.trip_id, host_id, host_id))
            affected = cur.rowcount > 0

        if affected:
            log_audit(
                trip_id=self.trip_id,
                module="QUEUE",
                event="HOST_SKIPPED",
                host_id=host_id,
                db_file=self.db_file
            )
        return affected

    def clear_queue(self) -> int:
        """Переводит всех неотправленных хостов очереди в статус 'skipped' (БЕЗ УДАЛЕНИЯ ИЗ БД)."""
        now_iso = datetime.now().isoformat()
        with get_connection(self.db_file) as conn:
            cur = conn.cursor()
            cur.execute("""
                UPDATE core_hosts
                SET state = 'skipped', updated_at = ?
                WHERE trip_id = ? AND state IN ('selected', 'drafted', 'approved') AND state != 'sent';
            """, (now_iso, self.trip_id))
            count = cur.rowcount

        if count > 0:
            log_audit(
                trip_id=self.trip_id,
                module="QUEUE",
                event="QUEUE_CLEARED",
                details={"skipped_count": count},
                db_file=self.db_file
            )
        return count
