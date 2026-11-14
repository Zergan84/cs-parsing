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
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.database import get_connection, log_audit
from core.dispatch_confirmation import (
    ConfirmationCapability,
    ConfirmationResult,
    DispatchOutcome,
    EvidenceType,
    get_confirmation_validator,
)
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
        """Count only reconciled, confirmed live sends across the account."""
        from core.queue import RequestQueueManager
        summary = RequestQueueManager(self.db_file).get_daily_send_summary(self.trip_id, account_wide=True)
        if not summary.get("available") or summary.get("confirmed_live") is None:
            raise RuntimeError("Daily send count is not reconciled across SQLite sources")
        return int(summary["confirmed_live"])

    def get_ready_candidates(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Возвращает хостов с утвержденными письмами, готовых к отправке."""
        with get_connection(self.db_file) as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT host_id, trip_id, username, name, location, message_draft, message_version, score_stars,
                       recently_active_status, last_login_days, activity_checked_at, activity_source
                FROM core_hosts
                WHERE trip_id = ? AND state = 'approved'
                ORDER BY CASE WHEN recently_active_status='ACTIVE' THEN 0
                              WHEN recently_active_status='RECENTLY_ACTIVE'
                               AND activity_checked_at >= datetime('now','-30 days') THEN 1 ELSE 2 END,
                         CASE WHEN last_login_days IS NULL THEN 2147483647 ELSE last_login_days END,
                         COALESCE(attempts, 0) ASC, COALESCE(priority, 10) DESC,
                         score_stars DESC, updated_at ASC
                LIMIT ?;
                """,
                (self.trip_id, limit),
            )
            rows = [dict(r) for r in cur.fetchall()]
        from core.approval_service import ApprovalService
        service = ApprovalService(self.db_file)
        ready = []
        for row in rows:
            result = service.validate_approval(
                trip_id=row["trip_id"], host_id=row["host_id"], username=row["username"],
                message_text=row["message_draft"] or "", message_version=row["message_version"],
                record_failure=True,
            )
            if result.valid:
                ready.append(row)
        return ready

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
        dispatch_outcome: Optional[str] = None,
        evidence_type: Optional[str] = None,
        attempt_id: Optional[str] = None,
        trip_id: Optional[str] = None,
        canonical_host_id: Optional[str] = None,
    ) -> int:
        """
        Записывает результат попытки независимо от legacy-таблицы профилей `hosts`.

        Для идентификации сначала использует саму запись sending_attempts, затем
        trip-scoped core_hosts. Если профиль уже удалён, сохраняет переданный ключ.
        """
        now = timestamp or datetime.now().isoformat()
        clean_key = str(host_id_or_username or "").strip()
        if not clean_key:
            return 0

        with get_connection(self.db_file) as conn:
            cur = conn.cursor()
            resolved_trip_id = trip_id or self.trip_id
            fk_host_id = str(canonical_host_id or clean_key).strip()
            u_name = clean_key
            attempt = None

            if attempt_id:
                cur.execute(
                    "SELECT trip_id, host_id, username FROM sending_attempts WHERE id = ? LIMIT 1;",
                    (attempt_id,),
                )
                attempt = cur.fetchone()
                if attempt:
                    attempt_trip_id = attempt["trip_id"]
                    if resolved_trip_id and attempt_trip_id and str(resolved_trip_id).lower() != str(attempt_trip_id).lower():
                        raise ValueError(
                            f"Sending attempt {attempt_id} belongs to trip {attempt_trip_id}, not {resolved_trip_id}"
                        )
                    resolved_trip_id = attempt_trip_id or resolved_trip_id
                    fk_host_id = attempt["host_id"] or fk_host_id
                    u_name = attempt["username"] or u_name

            if not attempt_id or not attempt:
                if resolved_trip_id:
                    cur.execute(
                        """SELECT host_id, username FROM core_hosts
                           WHERE trip_id = ? AND (LOWER(host_id) = LOWER(?) OR LOWER(username) = LOWER(?))
                           LIMIT 1;""",
                        (resolved_trip_id, clean_key, clean_key),
                    )
                    ch_row = cur.fetchone()
                    if ch_row:
                        fk_host_id = canonical_host_id or ch_row["host_id"] or fk_host_id
                        u_name = ch_row["username"] or u_name

            # Detailed attempt state belongs to sending_attempts; history stores a
            # compact, idempotent event linked by attempt_id.
            event_type = "ATTEMPT_RESULT" if attempt_id else None
            state_at_event = str(status or "UNKNOWN").strip().lower()
            columns = [
                "host_id", "username", "status", "http_status", "mode",
                "proof_screenshot", "before_screenshot", "button_selector",
                "notes", "error_details", "timestamp", "attempt_id", "trip_id",
                "event_type", "state_at_event", "evidence_type",
            ]
            values = (
                fk_host_id, u_name, status, http_status, mode,
                proof_screenshot, before_screenshot, button_selector,
                notes, error_details, now, attempt_id, resolved_trip_id,
                event_type, state_at_event, evidence_type,
            )
            insert_mode = "INSERT OR IGNORE" if attempt_id else "INSERT"
            cur.execute(
                f"""{insert_mode} INTO sending_history ({', '.join(columns)})
                    VALUES ({', '.join('?' for _ in columns)});""",
                values,
            )
            if cur.rowcount == 0 and attempt_id:
                cur.execute(
                    "SELECT id FROM sending_history WHERE attempt_id = ? AND event_type = 'ATTEMPT_RESULT' LIMIT 1;",
                    (attempt_id,),
                )
                existing = cur.fetchone()
                return int(existing["id"]) if existing else 0
            return cur.lastrowid or 0

    def mark_as_sent(
        self,
        host_id: str,
        http_status: int,
        capability: Optional[ConfirmationCapability] = None,
        confirmation_url: Optional[str] = None,
        evidence_reference: Optional[str] = None,
        notes: Optional[str] = None,
        attempt_id: Optional[str] = None,
    ) -> bool:
        """
        Атомарно и идемпотентно фиксирует успешную отправку Stay Request.

        ТРЕБУЕТ:
          * capability: неподделываемый ConfirmationCapability токен,
            выпущенный ConfirmationValidator.validate().
          * HTTP 200-299.

        Если capability отсутствует или не проходит проверку подлинности,
        метод возвращает False и НЕ переводит хост в состояние 'sent'.

        Все изменения (core_hosts, sending_history, core_audit_logs,
        sending_attempts.dispatch_outcome) производятся в одной транзакции.
        """
        # F1: Capability обязателен и должен быть валидным
        if capability is None:
            logger.warning(
                "mark_as_sent rejected: no ConfirmationCapability provided"
            )
            return False

        if not isinstance(capability, ConfirmationCapability):
            logger.warning(
                "mark_as_sent rejected: capability is not ConfirmationCapability instance"
            )
            return False

        # Проверяем подлинность capability через валидатор
        validator = get_confirmation_validator(db_file=self.db_file)
        if not validator.verify_capability(capability):
            logger.warning(
                "mark_as_sent rejected: capability verification failed "
                "for attempt_id=%s", capability.attempt_id
            )
            return False

        if not (200 <= http_status <= 299):
            logger.warning(
                "mark_as_sent rejected: http_status=%d is not 2xx", http_status
            )
            return False

        dispatch_attempt_id = attempt_id or capability.attempt_id
        if dispatch_attempt_id != capability.attempt_id:
            logger.error("mark_as_sent rejected: attempt_id differs from confirmation capability")
            return False

        now_iso = datetime.now().isoformat()
        with get_connection(self.db_file) as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT state, host_id, username FROM core_hosts WHERE trip_id=? AND (host_id=? OR LOWER(username)=LOWER(?))",
                (self.trip_id, host_id, host_id),
            )
            row = cur.fetchone()
            if not row:
                return False
            canon_host_id, canon_username = row["host_id"], row["username"]
            attempt = cur.execute(
                "SELECT trip_id,host_id,username FROM sending_attempts WHERE id=?",
                (dispatch_attempt_id,),
            ).fetchone()
            if (
                not attempt
                or str(attempt["trip_id"] or "").casefold() != str(self.trip_id).casefold()
                or str(attempt["username"] or "").casefold() != str(canon_username).casefold()
            ):
                logger.error("mark_as_sent rejected: dispatch attempt does not match host and Trip")
                return False

            # Idempotent recovery for an already-sent host: retain prior history
            # and audit rows, and only align the attempt and active lock.
            if str(row["state"]).lower() == "sent":
                cur.execute(
                    """UPDATE sending_attempts SET state='sent', dispatch_outcome=?,
                         send_intent_committed=1, send_initiated=1, http_status=?,
                         evidence_type='confirmed_acceptance', evidence_reference=COALESCE(?, evidence_reference),
                         sent_at=COALESCE(sent_at, ?), updated_at=? WHERE id=?""",
                    (DispatchOutcome.CONFIRMED_SENT.value, http_status,
                     evidence_reference or capability.evidence_type.value, now_iso, now_iso, dispatch_attempt_id),
                )
                cur.execute(
                    "UPDATE host_dispatch_locks SET lock_state='sent',updated_at=? WHERE attempt_id=? AND lock_state IN ('reserved','sending','uncertain','sent')",
                    (now_iso, dispatch_attempt_id),
                )
                cur.execute("UPDATE confirmation_capabilities SET used=1 WHERE attempt_id=?", (dispatch_attempt_id,))
                return True

            cur.execute(
                """UPDATE core_hosts SET state='sent',sent_at=COALESCE(sent_at,?),updated_at=?
                   WHERE trip_id=? AND (host_id=? OR LOWER(username)=LOWER(?)) AND state!='sent'""",
                (now_iso, now_iso, self.trip_id, host_id, host_id),
            )
            if cur.rowcount == 0:
                return True

            cur.execute(
                """INSERT INTO sending_history(host_id,username,status,http_status,mode,notes,timestamp,
                   attempt_id,trip_id,event_type,state_at_event,evidence_type)
                   VALUES(?,?,'SENT_SUCCESS',?,'live',?,?,?,?, 'SENT','sent',?)""",
                (canon_host_id, canon_username, http_status, notes or "Verified via strict confirmation",
                 now_iso, dispatch_attempt_id, self.trip_id, capability.evidence_type.value),
            )
            cur.execute(
                """INSERT INTO core_audit_logs(timestamp,trip_id,module,event,host_id,details_json)
                   VALUES(?,?,'AUTOPILOT','REQUEST_SENT_VERIFIED',?,?)""",
                (now_iso, self.trip_id, canon_host_id, json.dumps({
                    "http_status": http_status, "evidence_type": capability.evidence_type.value,
                    "confirmation_url": confirmation_url, "attempt_id": dispatch_attempt_id,
                    "canonical_host_key": capability.canonical_host_key, "notes": notes,
                }, ensure_ascii=False)),
            )
            cur.execute(
                """UPDATE sending_attempts SET state='sent',dispatch_outcome=?,
                   send_intent_committed=1,send_initiated=1,http_status=?,
                   evidence_type='confirmed_acceptance',evidence_reference=COALESCE(?,evidence_reference),
                   sent_at=?,updated_at=? WHERE id=?""",
                (DispatchOutcome.CONFIRMED_SENT.value, http_status,
                 evidence_reference or capability.evidence_type.value, now_iso, now_iso, dispatch_attempt_id),
            )
            if cur.rowcount != 1:
                raise RuntimeError(f"Confirmed dispatch attempt {dispatch_attempt_id} could not be updated")
            cur.execute(
                """UPDATE host_dispatch_locks SET lock_state='sent',updated_at=?
                   WHERE attempt_id=? AND lock_state IN ('reserved','sending','uncertain','sent')""",
                (now_iso, dispatch_attempt_id),
            )
            cur.execute("UPDATE confirmation_capabilities SET used=1 WHERE attempt_id=?", (dispatch_attempt_id,))

        return True

    def mark_as_rejected(
        self,
        host_id: str,
        http_status: Optional[int] = None,
        error_details: Optional[str] = None,
        notes: Optional[str] = None,
        attempt_id: Optional[str] = None,
    ) -> bool:
        """
        Фиксирует уверенный отказ: есть надежные доказательства, что Stay Request не принят.
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
                return False

            canon_host_id = row["host_id"]
            canon_username = row["username"]

            cur.execute(
                """
                UPDATE core_hosts
                SET state = 'failed', attempts = COALESCE(attempts, 0) + 1,
                    qc_notes = ?, updated_at = ?
                WHERE trip_id = ? AND (host_id = ? OR LOWER(username) = LOWER(?)) AND state != 'sent';
                """,
                (f"REJECTED: {notes or error_details or 'Explicit rejection'}", now_iso, self.trip_id, host_id, host_id),
            )
            if cur.rowcount == 0:
                return False

            cur.execute(
                """
                INSERT INTO sending_history (
                    host_id, username, status, http_status, mode, notes, error_details, timestamp
                ) VALUES (?, ?, 'FAILED', ?, 'live', ?, ?, ?);
                """,
                (canon_host_id, canon_username, http_status, notes or "Explicit rejection", error_details or "", now_iso),
            )

            cur.execute(
                """
                INSERT INTO core_audit_logs (timestamp, trip_id, module, event, host_id, details_json)
                VALUES (?, ?, 'AUTOPILOT', 'REQUEST_SEND_REJECTED', ?, ?);
                """,
                (
                    now_iso, self.trip_id, canon_host_id,
                    json.dumps({"http_status": http_status, "error_details": error_details, "notes": notes}, ensure_ascii=False),
                ),
            )

            if attempt_id:
                cur.execute(
                    """
                    UPDATE sending_attempts
                    SET state = 'failed', dispatch_outcome = ?, updated_at = ?
                    WHERE id = ?;
                    """,
                    (DispatchOutcome.CONFIRMED_REJECTED.value, now_iso, attempt_id),
                )

        return True

    def mark_as_unknown(
        self,
        host_id: str,
        http_status: Optional[int] = None,
        error_details: Optional[str] = None,
        notes: Optional[str] = None,
        attempt_id: Optional[str] = None,
    ) -> bool:
        """
        Фиксирует неизвестный исход: операция могла дойти до Couchsurfing, но принятие
        не установлено. Автоматический повтор ЗАПРЕЩЁН.
        """
        now_iso = datetime.now().isoformat()
        clean_notes = notes or (error_details or "Unknown outcome; no acceptance evidence")
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
                return False

            canon_host_id = row["host_id"]
            canon_username = row["username"]

            cur.execute(
                """
                UPDATE core_hosts
                SET state = 'uncertain', attempts = COALESCE(attempts, 0) + 1,
                    qc_notes = ?, updated_at = ?
                WHERE trip_id = ? AND (host_id = ? OR LOWER(username) = LOWER(?)) AND state != 'sent';
                """,
                (f"UNKNOWN: {clean_notes}", now_iso, self.trip_id, host_id, host_id),
            )
            if cur.rowcount == 0:
                return False

            cur.execute(
                """
                INSERT INTO sending_history (
                    host_id, username, status, http_status, mode, notes, error_details, timestamp
                ) VALUES (?, ?, 'UNCERTAIN', ?, 'live', ?, ?, ?);
                """,
                (canon_host_id, canon_username, http_status, clean_notes, error_details or "", now_iso),
            )

            cur.execute(
                """
                INSERT INTO core_audit_logs (timestamp, trip_id, module, event, host_id, details_json)
                VALUES (?, ?, 'AUTOPILOT', 'REQUEST_SEND_UNKNOWN', ?, ?);
                """,
                (
                    now_iso, self.trip_id, canon_host_id,
                    json.dumps({"http_status": http_status, "error_details": error_details, "notes": clean_notes}, ensure_ascii=False),
                ),
            )

            if attempt_id:
                cur.execute(
                    """
                    UPDATE sending_attempts
                    SET state = 'uncertain', dispatch_outcome = ?, updated_at = ?
                    WHERE id = ?;
                    """,
                    (DispatchOutcome.UNKNOWN_OUTCOME.value, now_iso, attempt_id),
                )

        return True

    def mark_as_requires_reconciliation(
        self,
        host_id: str,
        http_status: Optional[int] = None,
        error_details: Optional[str] = None,
        notes: Optional[str] = None,
        attempt_id: Optional[str] = None,
    ) -> bool:
        """
        Фиксирует необходимость ручной сверки перед следующей попыткой.
        """
        now_iso = datetime.now().isoformat()
        clean_notes = notes or (error_details or "Requires manual reconciliation")
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
                return False

            canon_host_id = row["host_id"]
            canon_username = row["username"]

            cur.execute(
                """
                UPDATE core_hosts
                SET state = 'uncertain', attempts = COALESCE(attempts, 0) + 1,
                    qc_notes = ?, updated_at = ?
                WHERE trip_id = ? AND (host_id = ? OR LOWER(username) = LOWER(?)) AND state != 'sent';
                """,
                (f"RECONCILIATION: {clean_notes}", now_iso, self.trip_id, host_id, host_id),
            )
            if cur.rowcount == 0:
                return False

            cur.execute(
                """
                INSERT INTO sending_history (
                    host_id, username, status, http_status, mode, notes, error_details, timestamp
                ) VALUES (?, ?, 'UNCERTAIN', ?, 'live', ?, ?, ?);
                """,
                (canon_host_id, canon_username, http_status, clean_notes, error_details or "", now_iso),
            )

            cur.execute(
                """
                INSERT INTO core_audit_logs (timestamp, trip_id, module, event, host_id, details_json)
                VALUES (?, ?, 'AUTOPILOT', 'REQUEST_SEND_RECONCILIATION', ?, ?);
                """,
                (
                    now_iso, self.trip_id, canon_host_id,
                    json.dumps({"http_status": http_status, "error_details": error_details, "notes": clean_notes}, ensure_ascii=False),
                ),
            )

            if attempt_id:
                cur.execute(
                    """
                    UPDATE sending_attempts
                    SET state = 'uncertain', dispatch_outcome = ?, updated_at = ?
                    WHERE id = ?;
                    """,
                    (DispatchOutcome.REQUIRES_RECONCILIATION.value, now_iso, attempt_id),
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
