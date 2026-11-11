"""
core/autopilot.py - Модуль АВТОПИЛОТ.
Отвечает за:
1. Выборку утвержденных писем (state = 'approved') для активного трипа.
2. Контроль суточного лимита (100) и Human Jitter (5-10 минут).
3. Фиксацию фактического результата отправки в SQLite (state = 'sent' при 200 OK).
4. Защиту от повторной отправки.
"""

import logging
import random
from datetime import datetime, date
from typing import Any, Dict, List, Optional

from pathlib import Path
from core.database import get_connection, log_audit
from core.models import HostState

logger = logging.getLogger("Autopilot")


class AutopilotEngine:
    """Движок автопилота отправки stay-requests."""

    def __init__(self, trip_id: str, daily_limit: int = 100, min_interval_min: int = 5, max_interval_min: int = 10, db_file: Optional[Path] = None):
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
                (self.trip_id, f"{today_prefix}%")
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
                ORDER BY score_stars DESC, updated_at ASC
                LIMIT ?;
                """,
                (self.trip_id, limit)
            )
            return [dict(r) for r in cur.fetchall()]

    def calculate_jitter_seconds(self) -> int:
        """Рассчитывает случайный интервал Human Jitter (в секундах)."""
        min_sec = self.min_interval_min * 60
        max_sec = self.max_interval_min * 60
        return random.randint(min_sec, max_sec)

    def mark_as_sent(self, host_id: str, http_status: int = 200, notes: Optional[str] = None) -> bool:
        """Фиксирует успешную отправку запроса."""
        now_iso = datetime.now().isoformat()
        with get_connection(self.db_file) as conn:
            cur = conn.cursor()
            cur.execute(
                """
                UPDATE core_hosts
                SET state = 'sent', sent_at = ?, updated_at = ?
                WHERE trip_id = ? AND (host_id = ? OR username = ?);
                """,
                (now_iso, now_iso, self.trip_id, host_id, host_id)
            )
            affected = cur.rowcount > 0

        if affected:
            log_audit(
                trip_id=self.trip_id,
                module="AUTOPILOT",
                event="REQUEST_SENT_VERIFIED",
                host_id=host_id,
                details={"http_status": http_status, "notes": notes},
                db_file=self.db_file
            )
        return affected

    def mark_as_skipped(self, host_id: str, reason: str) -> bool:
        """Фиксирует пропуск хоста при невозможности отправки с сохранением причины."""
        now_iso = datetime.now().isoformat()
        with get_connection(self.db_file) as conn:
            cur = conn.cursor()
            cur.execute(
                """
                UPDATE core_hosts
                SET state = 'skipped', qc_notes = ?, updated_at = ?
                WHERE trip_id = ? AND (host_id = ? OR username = ?);
                """,
                (reason, now_iso, self.trip_id, host_id, host_id)
            )
            affected = cur.rowcount > 0

        if affected:
            log_audit(
                trip_id=self.trip_id,
                module="AUTOPILOT",
                event="REQUEST_SKIPPED",
                host_id=host_id,
                details={"reason": reason},
                db_file=self.db_file
            )
        return affected
