"""
core/autopilot.py - Модуль АВТОПИЛОТ.
Отвечает за:
1. Выборку утвержденных писем (state = 'approved') для активного трипа.
2. Контроль суточного лимита (100) и Human Jitter (5-10 минут).
3. Фиксацию фактического результата отправки в SQLite (`core_hosts`, `sending_queue`, `sending_history`, `core_audit_logs`).
4. Ротацию проблемных хостов в конец очереди (`move_host_to_end_of_queue`) и защиту от повторной отправки.
"""

import json
import logging
import random
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.database import get_connection, log_audit
from core.models import HostState

logger = logging.getLogger("Autopilot")


class AutopilotEngine:
    """Движок автопилота отправки stay-requests."""

    def __init__(
        self,
        trip_id: str,
        daily_limit: int = 100,
        min_interval_min: int = 5,
        max_interval_min: int = 10,
        db_file: Optional[Path] = None,
    ):
        self.trip_id = trip_id
        self.daily_limit = daily_limit
        self.min_interval_min = min_interval_min
        self.max_interval_min = max_interval_min
        self.db_file = db_file

    def get_sent_today_count(self) -> int:
        """Считает количество отправок за текущие сутки."""
        today_prefix = date.today().isoformat()
        with get_connection(self.db_file) as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT COUNT(*) FROM core_hosts
                WHERE trip_id = ? AND state = 'sent' AND sent_at LIKE ?;
                """,
                (self.trip_id, f"{today_prefix}%"),
            )
            return cur.fetchone()[0] or 0

    def get_ready_candidates(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Возвращает хостов с утвержденными письмами, готовых к отправке."""
        with get_connection(self.db_file) as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT host_id, username, name, location, message_draft, score_stars
                FROM core_hosts
                WHERE trip_id = ? AND state = 'approved'
                ORDER BY COALESCE(attempts, 0) ASC, COALESCE(priority, 10) DESC, score_stars DESC, updated_at ASC
                LIMIT ?;
                """,
                (self.trip_id, limit),
            )
            return [dict(r) for r in cur.fetchall()]

    def calculate_jitter_seconds(self) -> int:
        """Рассчитывает случайный интервал Human Jitter (в секундах)."""
        min_sec = self.min_interval_min * 60
        max_sec = self.max_interval_min * 60
        return random.randint(min_sec, max_sec)

    def record_sending_attempt(
        self,
        host_id_or_username: str,
        status: str,
        mode: str = "live",
        http_status: Optional[int] = None,
        proof_screenshot: Optional[str] = None,
        before_screenshot: Optional[str] = None,
        button_selector: Optional[str] = None,
        notes: Optional[str] = None,
        error_details: Optional[str] = None,
        timestamp: Optional[str] = None,
    ) -> int:
        """
        Атомарно записывает попытку отправки в таблицу `sending_history`
        (гарантируя соблюдение внешнего ключа к `hosts(id)`).
        """
        now = timestamp or datetime.now().isoformat()
        clean_key = str(host_id_or_username or "").strip()
        if not clean_key:
            return 0

        with get_connection(self.db_file) as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT id, username FROM hosts WHERE LOWER(id) = LOWER(?) OR LOWER(username) = LOWER(?) LIMIT 1;",
                (clean_key, clean_key),
            )
            h_row = cur.fetchone()
            if h_row:
                fk_host_id = h_row["id"]
                u_name = h_row["username"]
            else:
                cur.execute(
                    "SELECT host_id, username, name, location FROM core_hosts WHERE LOWER(host_id) = LOWER(?) OR LOWER(username) = LOWER(?) LIMIT 1;",
                    (clean_key, clean_key),
                )
                ch_row = cur.fetchone()
                u_name = ch_row["username"] if ch_row else clean_key
                fk_host_id = u_name
                disp = ch_row["name"] if ch_row else u_name
                loc = (ch_row["location"] if ch_row else None) or "Japan"
                cur.execute(
                    """
                    INSERT OR IGNORE INTO hosts (
                        id, username, name, clean_name, profile_url, stay_request_url,
                        location, first_scraped_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        fk_host_id,
                        u_name,
                        disp,
                        disp.split()[0] if disp else u_name,
                        f"https://www.couchsurfing.com/people/{u_name}",
                        f"https://www.couchsurfing.com/people/{u_name}",
                        loc,
                        now,
                        now,
                    ),
                )

            cur.execute(
                """
                INSERT INTO sending_history (
                    host_id, username, status, http_status, mode,
                    proof_screenshot, before_screenshot, button_selector,
                    notes, error_details, timestamp
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    fk_host_id,
                    u_name,
                    status,
                    http_status,
                    mode,
                    proof_screenshot,
                    before_screenshot,
                    button_selector,
                    notes,
                    error_details,
                    now,
                ),
            )
            return cur.lastrowid or 0

    def mark_as_sent(self, host_id: str, http_status: int, notes: Optional[str] = None) -> bool:
        """
        Атомарно и идемпотентно фиксирует успешную отправку запроса в `core_hosts`, `sending_history` и `core_audit_logs`.
        Все три таблицы обновляются в единой транзакции SQLite.
        Требует обязательного проверенного HTTP-кода (200-299).
        """
        if not (200 <= http_status <= 299):
            raise ValueError(f"mark_as_sent requires 2xx http_status, got {http_status}. Use mark_as_uncertain instead.")

        now_iso = datetime.now().isoformat()
        with get_connection(self.db_file) as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT state, host_id, username FROM core_hosts WHERE trip_id = ? AND (host_id = ? OR LOWER(username) = LOWER(?));",
                (self.trip_id, host_id, host_id),
            )
            row = cur.fetchone()
            if not row:
                return False
            if row["state"] == "sent":
                # Идемпотентность: хост уже отправлен, повторно не пишем в sending_history и audit log
                return True

            canon_host_id = row["host_id"]
            canon_username = row["username"]

            cur.execute(
                """
                UPDATE core_hosts
                SET state = 'sent', sent_at = COALESCE(sent_at, ?), updated_at = ?
                WHERE trip_id = ? AND (host_id = ? OR LOWER(username) = LOWER(?)) AND state != 'sent';
                """,
                (now_iso, now_iso, self.trip_id, host_id, host_id),
            )
            if cur.rowcount == 0:
                return True

            # Атомарная запись в sending_history внутри той же транзакции
            cur.execute(
                """
                INSERT INTO sending_history (
                    host_id, username, status, http_status, mode, notes, timestamp
                ) VALUES (?, ?, 'SENT_SUCCESS', ?, 'live', ?, ?);
                """,
                (canon_host_id, canon_username, http_status, notes or "Verified via Autopilot", now_iso),
            )

            # Атомарная запись в core_audit_logs внутри той же транзакции
            cur.execute(
                """
                INSERT INTO core_audit_logs (timestamp, trip_id, module, event, host_id, details_json)
                VALUES (?, ?, 'AUTOPILOT', 'REQUEST_SENT_VERIFIED', ?, ?);
                """,
                (now_iso, self.trip_id, canon_host_id, json.dumps({"http_status": http_status, "notes": notes}, ensure_ascii=False)),
            )
            return True

    def mark_as_uncertain(
        self,
        host_id: str,
        error_details: Optional[str] = None,
        http_status: Optional[int] = None,
        proof_screenshot: Optional[str] = None,
        before_screenshot: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> bool:
        """
        Атомарно фиксирует неизвестный/неподтвержденный исход отправки (таймаут, HTTP 500, сетевой сбой).
        Все изменения (core_hosts, sending_history, core_audit_logs) производятся в единой транзакции.
        Переводит host в state = 'uncertain', исключая его из автоматической очереди.
        Необратимость: если хост уже 'sent', его статус не может быть перезаписан.
        """
        now_iso = datetime.now().isoformat()
        clean_notes = notes or (f"HTTP {http_status}: {error_details}" if http_status else (error_details or "Uncertain outcome"))

        with get_connection(self.db_file) as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT state, host_id, username FROM core_hosts WHERE trip_id = ? AND (host_id = ? OR LOWER(username) = LOWER(?));",
                (self.trip_id, host_id, host_id),
            )
            row = cur.fetchone()
            if not row:
                return False
            if row["state"] == "sent":
                # Необратимость sent: уже отправленного хоста нельзя перевести в uncertain!
                return False

            canon_host_id = row["host_id"]
            canon_username = row["username"]

            cur.execute(
                """
                UPDATE core_hosts
                SET state = 'uncertain',
                    attempts = COALESCE(attempts, 0) + 1,
                    qc_notes = ?,
                    updated_at = ?
                WHERE trip_id = ? AND (host_id = ? OR LOWER(username) = LOWER(?)) AND state != 'sent';
                """,
                (f"UNCERTAIN: {clean_notes}", now_iso, self.trip_id, host_id, host_id),
            )
            if cur.rowcount == 0:
                return False

            cur.execute(
                """
                INSERT INTO sending_history (
                    host_id, username, status, http_status, mode,
                    proof_screenshot, before_screenshot, notes, error_details, timestamp
                ) VALUES (?, ?, 'UNCERTAIN', ?, 'live', ?, ?, ?, ?, ?);
                """,
                (
                    canon_host_id,
                    canon_username,
                    http_status,
                    proof_screenshot,
                    before_screenshot,
                    clean_notes,
                    error_details,
                    now_iso,
                ),
            )
            cur.execute(
                """
                INSERT INTO core_audit_logs (timestamp, trip_id, module, event, host_id, details_json)
                VALUES (?, ?, 'AUTOPILOT', 'REQUEST_SEND_UNCERTAIN', ?, ?);
                """,
                (now_iso, self.trip_id, canon_host_id, json.dumps({"http_status": http_status, "error_details": error_details, "notes": clean_notes}, ensure_ascii=False)),
            )
            return True

    def mark_as_skipped(self, host_id: str, reason: str) -> bool:
        """
        Фиксирует пропуск хоста при невозможности отправки с сохранением причины.
        Атомарно обновляет core_hosts и core_audit_logs в одной транзакции.
        Необратимость: если хост уже 'sent', статус 'sent' неприкосновенен.
        """
        now_iso = datetime.now().isoformat()
        with get_connection(self.db_file) as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT state, host_id, username FROM core_hosts WHERE trip_id = ? AND (host_id = ? OR LOWER(username) = LOWER(?));",
                (self.trip_id, host_id, host_id),
            )
            row = cur.fetchone()
            if not row:
                return False
            if row["state"] == "sent":
                # Необратимость sent: уже отправленного хоста нельзя пропустить!
                return False

            canon_host_id = row["host_id"]
            cur.execute(
                """
                UPDATE core_hosts
                SET state = 'skipped', qc_notes = ?, updated_at = ?
                WHERE trip_id = ? AND (host_id = ? OR username = ?) AND state != 'sent';
                """,
                (reason, now_iso, self.trip_id, host_id, host_id),
            )
            if cur.rowcount == 0:
                return False

            cur.execute(
                """
                INSERT INTO core_audit_logs (timestamp, trip_id, module, event, host_id, details_json)
                VALUES (?, ?, 'AUTOPILOT', 'REQUEST_SKIPPED', ?, ?);
                """,
                (now_iso, self.trip_id, canon_host_id, json.dumps({"reason": reason}, ensure_ascii=False)),
            )
            return True

    def move_host_to_end_of_queue(self, host_id: str, reason: str = "") -> bool:
        """Перемещает проблемного хоста в конец очереди (понижая priority на -100) без остановки автопилота."""
        now_iso = datetime.now().isoformat()
        with get_connection(self.db_file) as conn:
            cur = conn.cursor()
            cur.execute(
                """
                UPDATE core_hosts
                SET priority = COALESCE(priority, 10) - 100,
                    attempts = COALESCE(attempts, 0) + 1,
                    qc_notes = ?,
                    updated_at = ?
                WHERE (LOWER(host_id) = LOWER(?) OR LOWER(username) = LOWER(?))
                  AND state != 'sent';
                """,
                (f"Moved to bottom: {reason}", now_iso, host_id, host_id),
            )
            ch_ok = cur.rowcount > 0

        if ch_ok:
            log_audit(
                trip_id=self.trip_id,
                module="AUTOPILOT",
                event="HOST_DEMOTED_TO_BOTTOM",
                host_id=host_id,
                details={"reason": reason},
                db_file=self.db_file,
            )
        return ch_ok
