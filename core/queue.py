"""
core/queue.py - Единый канонический модуль Очереди отправки, Генерации писем и QC-валидации.

Объединяет:
1. `QueueEngine`, `build_personalized_message`, `validate_message_qc` — доменный движок очереди и QC по ТЗ (60–90 слов, контроль дат/города и анти-галлюцинационный фильтр).
2. `RequestQueueManager` (`get_queue_manager`) — чтение и управление очередью из канонической таблицы `core_hosts`.
"""

import json
import logging
import re
import uuid
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union

from core.collector import calculate_match_score, get_profile_analyzer, _activity_from_host
from core.context import DEFAULT_GUEST_CONTEXT, GUEST, get_context_assembler, validate_text_purity
from core.database import (
    OUTPUT_DIR,
    db_session,
    get_connection,
    get_read_only_connection,
    get_active_trip,
    get_sent_usernames as db_get_sent_usernames,
    log_audit,
)
from core.models import HostState, Trip
from core.messaging import build_personalized_message, validate_message_qc
from core.host_policy import BLACKLIST_PHRASES, is_blacklisted
from core.host_activity import parse_activity
from core.host_policy import evaluate_hosting_exclusion, evaluate_host_eligibility

logger = logging.getLogger("CoreQueue")


def _activity_is_fresh(checked_at: Optional[str], max_age_days: int = 30) -> bool:
    try:
        checked = datetime.fromisoformat(str(checked_at).replace("Z", "+00:00"))
        now = datetime.now(checked.tzinfo) if checked.tzinfo else datetime.now()
        age = (now - checked).total_seconds()
        return 0 <= age <= max_age_days * 86400
    except (TypeError, ValueError):
        return False

CURATED_FILE = OUTPUT_DIR / "hosts_tokyo_curated.json"
RAW_FILE = OUTPUT_DIR / "hosts_tokyo.json"
SENT_HISTORY_FILE = OUTPUT_DIR / "sent_history.json"
SENT_LOG_FILE = OUTPUT_DIR / "sent_log.json"

DEFAULT_CHECK_IN = "2026-11-18"
DEFAULT_CHECK_OUT = "2026-11-20"
DEFAULT_CONTEXT = DEFAULT_GUEST_CONTEXT

# =====================================================================
# 2. QUEUE ENGINE (DDD CORE API)
# =====================================================================

class QueueEngine:
    """Движок управления очередью, генерации писем и их утверждения для активной поездки."""

    def __init__(self, trip_id: str, db_file: Optional[Path | str] = None):
        self.trip_id = trip_id
        self.db_file = Path(db_file) if db_file else None
        from core.repositories.host_queue_repository import HostQueueRepository
        self.repository = HostQueueRepository(self.db_file)

    def _get_trip(self) -> Optional[Trip]:
        row = self.repository.get_trip(self.trip_id)
        return Trip(**row) if row else None

    def list_queue(self, state: Optional[str] = None) -> List[Dict[str, Any]]:
        """Возвращает хостов в очереди ('selected', 'drafted', 'approved')."""
        valid_states = [state] if state else [
            HostState.SELECTED.value,
            HostState.DRAFTED.value,
            HostState.APPROVED.value,
        ]
        rows = self.repository.list_hosts(self.trip_id, valid_states)
        for row in rows:
            row["qc_passed"] = bool(row.get("qc_passed"))
            try:
                row["score_reasons"] = json.loads(row.get("score_reasons_json") or "[]")
            except (TypeError, ValueError):
                row["score_reasons"] = []
            row["word_count"] = len((row.get("message_draft") or "").split())
        return rows

    def generate_draft(self, host_id: str) -> Optional[Dict[str, Any]]:
        """Генерирует персонализированное письмо для хоста, проверяет QC и сохраняет в БД."""
        trip = self._get_trip()
        if not trip:
            return None

        host_dict = self.repository.get_host_context(self.trip_id, host_id)
        if not host_dict or host_dict.get("state") == HostState.SENT.value:
            return None
        try:
            host_dict["score_reasons"] = json.loads(host_dict.get("score_reasons_json") or "[]")
        except (TypeError, ValueError):
            host_dict["score_reasons"] = []
        actual_host_id = host_dict["host_id"]
        draft_text, qc_ok, notes = build_personalized_message(host_dict, trip)
        updated = self.repository.save_draft(
            self.trip_id, actual_host_id, draft_text, qc_ok, notes,
        )
        if not updated:
            return None

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

    def approve(self, host_id: str, edited_text: Optional[str] = None, *, created_by: str) -> bool:
        """
        Утверждает черновик для отправки автопилотом (state -> 'approved').
        Инвариант: нельзя утвердить письмо, не прошедшее QC, или перезаписать уже отправленного хоста.
        """
        trip = self._get_trip()
        if not trip:
            return False

        host = self.repository.get_host_context(self.trip_id, host_id)
        if not host:
            return False
        if host["state"] == HostState.SENT.value:
            return False
        try:
            source_state = HostState(host["state"])
        except ValueError:
            return False
        if source_state != HostState.APPROVED and not source_state.can_transition_to(HostState.APPROVED):
            raise ValueError(f"Cannot approve host from state '{source_state.value}'")

        # Preserve the exact reviewed text, including leading/trailing whitespace.
        final_text = edited_text if edited_text is not None else (host.get("message_draft") or "")
        bio = host.get("about") or ""
        minimal = len(bio.split()) < 25 or bio.lower().startswith("friendly local resident")
        qc_ok, qc_issues = validate_message_qc(
            final_text,
            trip,
            is_minimal_bio=minimal,
            host_data={**host, "about_me": bio, "about_me_clean": bio},
            trip_city=trip.city,
        )
        if not qc_ok:
            raise ValueError(f"Cannot approve draft failing QC: {'; '.join(qc_issues)}")
        actual_host_id = host["host_id"]
        updated = self.repository.approve_host(self.trip_id, actual_host_id, final_text, created_by=created_by)

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

    def batch_approve(self, host_ids: List[str], *, created_by: str) -> int:
        count = 0
        for hid in host_ids:
            try:
                if self.approve(hid, created_by=created_by):
                    count += 1
            except ValueError:
                continue
        return count

    def skip(self, host_id: str) -> bool:
        """
        Снимает хоста из очереди (state = 'skipped') БЕЗ удаления из БД.
        Инвариант: уже отправленный хост ('sent') не может быть переведен в 'skipped'.
        """
        updated = self.repository.update_state(self.trip_id, host_id, HostState.SKIPPED.value)

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
        count = self.repository.skip_many(self.trip_id)

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
    """Менеджер очереди, использующий core_hosts как единственное хранилище состояния."""

    def __init__(self, db_file: Optional[Path | str] = None):
        self.db_file = Path(db_file).expanduser().resolve() if db_file is not None else None
        from core.repositories.host_queue_repository import HostQueueRepository
        self.host_repository = HostQueueRepository(self.db_file)

    def get_sent_usernames(self) -> Set[str]:
        """Делегирует получение всех отправленных хостов канонической функции в `core.database`."""
        return db_get_sent_usernames(self.db_file)

    def deduplicate_queue_against_sent(self) -> int:
        """
        Сверяет очередь (Approved/Queued) со всеми отправленными хостами и диалогами.

        Помечает уже отправленных хостов как 'skipped' (не 'sent') в core_hosts,
        так как только mark_as_sent() с валидным ConfirmationCapability может
        установить состояние 'sent'. Эта функция только читает существующие
        записи SENT и помечает соответствующие записи очереди как пропущенные.
        """
        sent_set = self.get_sent_usernames()
        if not sent_set:
            return 0

        deduped_count = 0
        now_iso = datetime.now().isoformat()

        try:
            connection = db_session(self.db_file) if self.db_file is not None else db_session()
            with connection as conn:
                cur = conn.cursor()
                for u in sent_set:
                    cur.execute(
                        """
                        UPDATE core_hosts
                        SET state = 'skipped', sent_at = COALESCE(sent_at, ?), qc_notes = 'Already Sent (Deduplicated)', updated_at = ?
                        WHERE (LOWER(username) = ? OR LOWER(host_id) = ?)
                          AND state IN ('selected', 'drafted', 'approved');
                        """,
                        (now_iso, now_iso, u, u),
                    )
                    if cur.rowcount > 0:
                        deduped_count += cur.rowcount
        except Exception:
            logger.exception("Ошибка обновления SQLite при дедупликации")
            raise

        if deduped_count > 0:
            logger.info(f"[DEDUP] Успешно исключено из очереди {deduped_count} уже отправленных хостов.")
        return deduped_count

    @staticmethod
    def _resolve_canonical_trip_id(trip_id: str) -> str:
        known_aliases = {
            "3lhdfema5epclndo82mclxklzfytp": "kyoto-trip-3lhdfema5epclndo82mclxklzfytp-3LhDfEmA5Epclndo82MclXkLzFy_tp",
            "3lhdfema5epclndo82mclxk_tp": "kyoto-trip-3lhdfema5epclndo82mclxklzfytp-3LhDfEmA5Epclndo82MclXkLzFy_tp",
            "kyoto-trip-3lhdfema5epclndo82mclxklzfytp-3lhdfema5epclndo82mclxklzfytp": "kyoto-trip-3lhdfema5epclndo82mclxklzfytp-3LhDfEmA5Epclndo82MclXkLzFy_tp",
        }
        t_str = str(trip_id).strip()
        return known_aliases.get(t_str.lower(), t_str)

    def get_daily_send_summary(
        self, trip_id: Optional[str] = None, *, account_wide: bool = False,
    ) -> Dict[str, Any]:
        """Reconcile today's live outcomes across the attempt, host, and history records.

        The saved sender status and sent_log.json are intentionally not used as
        sources of truth: they can be stale, global to multiple trips, or include
        dry runs. A numeric count is returned only when the three SQLite views
        agree for this trip.
        """
        from zoneinfo import ZoneInfo

        trip = self._resolve_canonical_trip_id(trip_id) if trip_id else None
        with get_read_only_connection(self.db_file) as read_conn:
            zone_row = read_conn.execute(
                "SELECT country,city FROM core_trips WHERE LOWER(id)=LOWER(?) LIMIT 1", (trip,)
            ).fetchone() if trip else read_conn.execute(
                "SELECT country,city FROM core_trips WHERE is_active=1 ORDER BY updated_at DESC,id LIMIT 1"
            ).fetchone()
        country = str(zone_row["country"] or "").casefold() if zone_row else ""
        city = str(zone_row["city"] or "").casefold() if zone_row else ""
        zone_name = "Asia/Tokyo" if country in {"japan", "日本"} or city in {"kyoto", "tokyo", "osaka", "sapporo"} else None
        local_now = datetime.now().astimezone()
        trip_zone = ZoneInfo(zone_name) if zone_name else local_now.tzinfo
        trip_now = local_now.astimezone(trip_zone)
        day_start = datetime.combine(trip_now.date(), time.min, tzinfo=trip_zone)
        day_end = day_start + timedelta(days=1)
        start_local = day_start.astimezone(local_now.tzinfo).replace(tzinfo=None).isoformat(timespec="seconds")
        end_local = day_end.astimezone(local_now.tzinfo).replace(tzinfo=None).isoformat(timespec="seconds")
        scope_trip = None if account_wide else trip
        trip_clause = " AND LOWER(trip_id)=LOWER(?)" if trip else ""
        if scope_trip is None:
            trip_clause = ""
            trip_args: tuple[Any, ...] = ()
        else:
            trip_clause = " AND LOWER(trip_id)=LOWER(?)"
            trip_args = (scope_trip,)
        connection = db_session(self.db_file) if self.db_file is not None else db_session()
        with connection as conn:
            cur = conn.cursor()
            host_count_sql = f"""SELECT COUNT(DISTINCT LOWER(username)) FROM core_hosts
                WHERE state='sent' AND sent_at LIKE ?{trip_clause}"""
            cur.execute(host_count_sql.replace("sent_at LIKE ?", "sent_at >= ? AND sent_at < ?"), (start_local, end_local, *trip_args))
            host_confirmed = int(cur.fetchone()[0] or 0)

            attempt_trip_clause = " AND LOWER(trip_id)=LOWER(?)" if scope_trip else ""
            cur.execute(
                f"""SELECT COUNT(DISTINCT id) FROM sending_attempts
                    WHERE updated_at >= ? AND updated_at < ? AND LOWER(state)='sent'
                      AND UPPER(dispatch_outcome)='CONFIRMED_SENT'{attempt_trip_clause}""",
                (start_local, end_local, *trip_args),
            )
            attempt_confirmed = int(cur.fetchone()[0] or 0)

            history_scope = ""
            history_args: tuple[Any, ...] = (start_local, end_local)
            confirmed_history_scope = ""
            confirmed_history_args = history_args
            if scope_trip:
                history_scope = """ AND (LOWER(sh.trip_id)=LOWER(?) OR
                    (sh.trip_id IS NULL AND EXISTS (
                        SELECT 1 FROM core_hosts ch
                        WHERE LOWER(ch.trip_id)=LOWER(?)
                          AND (LOWER(ch.username)=LOWER(sh.username)
                               OR LOWER(ch.host_id)=LOWER(sh.host_id)))))"""
                history_args = (start_local, end_local, scope_trip, scope_trip)
                confirmed_history_scope = """ AND (LOWER(sh.trip_id)=LOWER(?) OR
                    (sh.trip_id IS NULL AND EXISTS (
                        SELECT 1 FROM core_hosts ch
                        WHERE LOWER(ch.trip_id)=LOWER(?)
                          AND (LOWER(ch.username)=LOWER(sh.username)
                               OR LOWER(ch.host_id)=LOWER(sh.host_id))
                          AND ch.state='sent' AND ch.sent_at >= ? AND ch.sent_at < ?)))"""
                confirmed_history_args = (start_local, end_local, scope_trip, scope_trip, start_local, end_local)
            cur.execute(
                """SELECT COUNT(DISTINCT CASE WHEN NULLIF(sh.attempt_id,'') IS NOT NULL
                              THEN sh.attempt_id ELSE 'legacy:' || LOWER(COALESCE(sh.username,sh.host_id)) END)
                   FROM sending_history sh
                   WHERE sh.timestamp >= ? AND sh.timestamp < ? AND UPPER(sh.status) IN ('SENT_SUCCESS','SUCCESS','SENT')
                     AND LOWER(sh.mode)='live'""" + confirmed_history_scope,
                confirmed_history_args,
            )
            history_confirmed = int(cur.fetchone()[0] or 0)

            cur.execute(
                """SELECT COUNT(DISTINCT id) FROM sending_attempts
                   WHERE updated_at >= ? AND updated_at < ? AND LOWER(state)='uncertain'
                     AND UPPER(dispatch_outcome)='UNKNOWN_OUTCOME'""" + attempt_trip_clause,
                (start_local, end_local, *trip_args),
            )
            unknown = int(cur.fetchone()[0] or 0)
            dry_run_scope = history_scope
            cur.execute(
                """SELECT COUNT(DISTINCT CASE WHEN NULLIF(sh.attempt_id,'') IS NOT NULL
                              THEN sh.attempt_id ELSE 'legacy:' || LOWER(COALESCE(sh.username,sh.host_id)) END)
                   FROM sending_history sh
                   WHERE sh.timestamp >= ? AND sh.timestamp < ? AND UPPER(sh.status)='DRY_RUN_SUCCESS'
                     AND LOWER(sh.mode)='dry_run'""" + dry_run_scope,
                history_args,
            )
            dry_run = int(cur.fetchone()[0] or 0)
            cur.execute(
                """SELECT COUNT(DISTINCT CASE WHEN NULLIF(sh.attempt_id,'') IS NOT NULL
                              THEN sh.attempt_id ELSE 'legacy:' || LOWER(COALESCE(sh.username,sh.host_id)) END)
                   FROM sending_history sh
                   WHERE sh.timestamp >= ? AND sh.timestamp < ? AND UPPER(sh.status)='FAILED' AND LOWER(sh.mode)='error'""" + history_scope,
                history_args,
            )
            failed = int(cur.fetchone()[0] or 0)

        reconciled = host_confirmed == attempt_confirmed == history_confirmed
        return {
            "available": reconciled,
            "confirmed_live": attempt_confirmed if reconciled else None,
            "host_state_confirmed": host_confirmed,
            "history_confirmed": history_confirmed,
            "attempt_confirmed": attempt_confirmed,
            "dry_run_successes": dry_run,
            "unknown_outcomes": unknown,
            "failed_attempts": failed,
            "window_start": start_local,
            "window_end": end_local,
            "timezone": zone_name or str(local_now.tzinfo),
            "source": "sending_attempts + core_hosts + sending_history",
            "reason": None if reconciled else "Today's confirmed live-send records do not reconcile across SQLite sources.",
        }

    def get_queue_stats(self, trip_id: Optional[str] = None) -> Dict[str, Any]:
        """Возвращает согласованную статистику очереди из канонической БД (core_hosts)."""
        try:
            connection = db_session(self.db_file) if self.db_file is not None else db_session()
            with connection as conn:
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
                    COUNT(CASE WHEN state = 'skipped' THEN 1 END) as skipped,
                    COUNT(CASE WHEN state = 'uncertain' THEN 1 END) as uncertain
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
                host_approved = int(row[5] or 0)
                sent_lifetime = int(row[6] or 0)
                skipped = int(row[7] or 0)
                uncertain = int(row[8] or 0)

                from core.approval_service import ApprovalService
                approval_summary = ApprovalService(self.db_file).summarize_trip(trip_id)

                # Daily platform limits are account-wide, not trip-scoped.
                daily_send_summary = self.get_daily_send_summary(trip_id, account_wide=True)
                sent_today = daily_send_summary["confirmed_live"]

                return {
                    "total": total,
                    "total_active": total,
                    "needs_regeneration": needs_regen,
                    "name_review": name_review,
                    "qc_review": qc_review,
                    "draft": draft,
                    # `approved` means the exact current message has a valid,
                    # trip-scoped user authorization. Host state is reported
                    # separately so legacy rows cannot look dispatchable.
                    "approved": approval_summary["dispatch_ready"],
                    "dispatch_ready": approval_summary["dispatch_ready"],
                    "approval_required": approval_summary["approval_required"],
                    "host_approved": host_approved,
                    "sent_today": sent_today,
                    "daily_send_summary": daily_send_summary,
                    "daily_count_available": daily_send_summary["available"],
                    "daily_limit_source": "last_sender_run_configuration",
                    "daily_limit": 100,
                    "sent_lifetime": sent_lifetime,
                    "skipped": skipped,
                    "uncertain": uncertain,
                }
        except Exception:
            logger.exception("Ошибка получения статистики очереди из SQLite")
            raise

    def load_queue(self, exclude_sent: bool = True, trip_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Загружает очередь исключительно из канонической таблицы core_hosts."""
        try:
            with db_session(self.db_file) as conn:
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
                    ch.interests_json,
                    ch.references_count,
                    ch.response_rate_pct,
                    ch.languages_json,
                    ch.state,
                    ch.score_stars,
                    ch.score_reasons_json,
                    ch.priority,
                    ch.attempts,
                    ch.message_draft as message_text,
                    ch.message_version,
                    ch.qc_passed,
                    ch.qc_notes,
                    ch.is_verified,
                    ch.verification_status,
                    ch.recently_active_status,
                    ch.last_active_raw,
                    ch.last_login_days,
                    ch.last_active_at,
                    ch.activity_checked_at,
                    ch.activity_source,
                    (SELECT s.raw_data_json FROM core_host_snapshots s
                     WHERE s.trip_id=ch.trip_id AND s.host_id=ch.host_id
                     ORDER BY s.captured_at DESC LIMIT 1) AS profile_snapshot_json,
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
                ORDER BY CASE WHEN ch.recently_active_status='ACTIVE' THEN 0
                              WHEN ch.recently_active_status='RECENTLY_ACTIVE'
                               AND ch.activity_checked_at >= datetime('now','-30 days') THEN 1 ELSE 2 END,
                         CASE WHEN ch.last_login_days IS NULL THEN 2147483647 ELSE ch.last_login_days END,
                         COALESCE(ch.attempts,0) ASC, COALESCE(ch.priority,10) DESC,
                         COALESCE(ch.score_stars,4) DESC, ch.created_at ASC;
                """
                cur.execute(query, params)
                rows = cur.fetchall()
                interest_rows = cur.execute(
                    """SELECT title, content FROM personal_context_facts
                       WHERE is_active=1 AND status='confirmed'
                         AND category IN ('Hobbies & Interests','Professional Background','Education','Travel Experience')
                       ORDER BY priority DESC, fact_id"""
                ).fetchall()
                traveler_interests = [
                    text for fact in interest_rows for text in (fact[0], fact[1]) if text
                ]
                approved_message_keys = {
                    (str(r["trip_id"]), str(r["host_id"]))
                    for r in cur.execute(
                        "SELECT trip_id,host_id FROM message_approvals WHERE approval_status='approved'"
                    ).fetchall()
                }

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
                        "uncertain": "Uncertain",
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
                        "state": raw_st,
                        "username": u,
                        "name": r["name"] or u,
                        "clean_name": r["clean_name"] or r["name"] or u,
                        "estimated_gender": "Unknown",
                        "host_type": "solo",
                        "location": eff_loc,
                        "city": eff_city,
                        "trip_id": r["linked_trip_id"],
                        "message_version": int(r["message_version"] or 1),
                        "trip_city": t_city,
                        "trip_arrival": t_arr,
                        "trip_departure": t_dep,
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
                        "interests": r["interests_json"] or "[]",
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
                        "verification_status": r["verification_status"] or "UNKNOWN",
                        "recently_active_status": r["recently_active_status"] or "UNKNOWN",
                        "last_active_raw": r["last_active_raw"],
                        "last_login_days": r["last_login_days"],
                        "last_active_at": r["last_active_at"],
                        "activity_checked_at": r["activity_checked_at"],
                        "activity_source": r["activity_source"],
                        "profile_snapshot_json": r["profile_snapshot_json"],
                        "languages": langs_str,
                        "language_list": langs,
                        "created_at": r["host_created_at"] or r["updated_at"],
                        "priority": int(r["priority"] or 10),
                        "attempts": int(r["attempts"] or 0),
                        "qc_passed": bool(r["qc_passed"]),
                        "notes": r["qc_notes"] or "",
                        "conversation_hooks": [],
                        "compatibility_points": [],
                        "conversation_strategy": "Grounded CS connection",
                    }
                    item["approval_valid"] = False
                    item["approval_status"] = "NOT_APPROVED"
                    approval_key = (str(r["linked_trip_id"] or ""), str(r["id"] or ""))
                    if raw_st == "approved" and approval_key in approved_message_keys:
                        from core.approval_service import ApprovalService
                        approval_result = ApprovalService(self.db_file).get_approval_status(
                            trip_id=str(r["linked_trip_id"] or ""), host_id=str(r["id"] or ""),
                            username=str(u or ""), message_text=str(r["message_text"] or ""),
                            message_version=int(r["message_version"] or 1),
                        )
                        item["approval_valid"] = approval_result.valid
                        item["approval_status"] = approval_result.code
                        item["approval_id"] = approval_result.approval_id
                    elif raw_st == "approved":
                        item["approval_status"] = "APPROVAL_NOT_FOUND"
                    if raw_st == "approved" and not item["approval_valid"]:
                        # Host workflow state alone is not permission to send.
                        # Make the card reflect the missing exact-message approval.
                        item["status"] = "Approval_Required"
                    result.append(item)

                from core.queue_ranking import rank_hosts
                return rank_hosts(result, traveler_interests)
        except Exception:
            logger.exception("Ошибка чтения очереди из core_hosts; JSON fallback отключен")
            raise

    def get_ready_dispatch_candidates(self, trip_id: Optional[str] = None, mutate_status: bool = False) -> List[Dict[str, Any]]:
        """Единый канонический список кандидатов, готовых к отправке автопилотом."""
        from core.messaging import validate_message_qc
        from core.identity import verify_host_name

        # Approval controls dispatch only; eligibility exclusions are still
        # rechecked for every approved host before authorization validation.
        with get_connection(self.db_file) as conn:
            approval_sql = """SELECT trip_id,host_id FROM message_approvals
                             WHERE approval_status='approved'"""
            approval_params: Tuple[Any, ...] = ()
            if trip_id:
                approval_sql += " AND trip_id=?"
                approval_params = (trip_id,)
            active_approval_keys = {
                (str(row["trip_id"]), str(row["host_id"]))
                for row in conn.execute(approval_sql, approval_params).fetchall()
            }
            # Exclude global identities with unresolved outcomes or active
            # dispatch locks before ranking. The atomic reservation just
            # before the external action remains the final safety boundary.
            identity_map = {
                (str(row["identity_type"]), str(row["identity_value"]).casefold()):
                    str(row["canonical_key"]).casefold()
                for row in conn.execute(
                    "SELECT identity_type,identity_value,canonical_key FROM host_identities"
                ).fetchall()
            }
            blocked_canonical_keys = {
                str(row["canonical_key"]).casefold()
                for row in conn.execute(
                    """SELECT canonical_key FROM host_dispatch_locks
                       WHERE lock_state IN ('reserved','sending','sent','uncertain','already_contacted')"""
                ).fetchall()
            }
            blocked_identity_values: Set[str] = set()
            unknown_attempt_rows = conn.execute(
                """SELECT username,username_norm,host_id,host_id_norm FROM sending_attempts
                   WHERE LOWER(state)='uncertain' AND UPPER(dispatch_outcome)='UNKNOWN_OUTCOME'"""
            ).fetchall()
            for unknown in unknown_attempt_rows:
                for key in ("username", "username_norm", "host_id", "host_id_norm"):
                    value = str(unknown[key] or "").strip().casefold()
                    if not value:
                        continue
                    blocked_identity_values.add(value)
                    identity_type = "username" if key.startswith("username") else "cs_id"
                    canonical = identity_map.get((identity_type, value))
                    if canonical:
                        blocked_canonical_keys.add(canonical)
        all_sent = self.get_sent_usernames()
        queue = self.load_queue(exclude_sent=False, trip_id=trip_id)
        candidates = []

        for item in queue:
            u = (item.get("username") or item.get("id") or "").strip().lower()
            if not u or u in all_sent:
                continue
            from core.dispatch_identity import normalize_identity
            host_id_value = str(item.get("id") or "").strip()
            username_value = str(item.get("username") or "").strip()
            candidate_identity_values = {
                value.casefold() for value in (u, host_id_value, username_value) if value
            }
            candidate_canonical_keys = {
                canonical for identity_type, value in (
                    ("username", username_value.casefold()),
                    ("cs_id", host_id_value.casefold()),
                ) if value and (canonical := identity_map.get((identity_type, value)))
            }
            if username_value:
                _, normalized_username = normalize_identity("username", username_value)
                candidate_identity_values.add(normalized_username.casefold())
                candidate_canonical_keys.add(f"un:{normalized_username}".casefold())
            if (
                candidate_identity_values & blocked_identity_values
                or candidate_canonical_keys & blocked_canonical_keys
            ):
                continue
            st = str(item.get("state") or item.get("status") or "").strip().lower()
            if st != "approved":
                continue
            trip_key = str(item.get("trip_id") or item.get("linked_trip_id") or "")
            host_key = str(item.get("id") or "")
            try:
                profile_data = json.loads(item.get("profile_snapshot_json") or "{}")
            except (TypeError, ValueError):
                profile_data = {}
            if not isinstance(profile_data, dict):
                profile_data = {}
            policy_host = {**profile_data, **item}
            from core.host_policy import evaluate_host_eligibility
            eligibility = evaluate_host_eligibility(
                policy_host,
                arrival_date=item.get("trip_arrival"),
                departure_date=item.get("trip_departure"),
                guest_gender=policy_host.get("guest_gender") or "male",
                target_city=item.get("trip_city") or item.get("city"),
            )
            if not eligibility["eligible"]:
                if mutate_status:
                    now = datetime.now().isoformat()
                    with get_connection(self.db_file) as conn:
                        conn.execute(
                            """UPDATE core_hosts SET state='skipped', eligibility_reason=?,
                                      eligibility_evidence_json=?, eligibility_checked_at=?, qc_notes=?, updated_at=?
                               WHERE trip_id=? AND host_id=? AND state='approved'""",
                            (eligibility["reason"], json.dumps(eligibility, ensure_ascii=False), now,
                             f"Pre-dispatch eligibility exclusion: {eligibility['reason']}", now,
                             item.get("trip_id") or trip_id, item.get("id")),
                        )
                continue

            if (trip_key, host_key) not in active_approval_keys:
                continue

            from core.approval_service import ApprovalService
            approval = ApprovalService(self.db_file).validate_approval(
                trip_id=str(item.get("trip_id") or item.get("linked_trip_id") or ""),
                host_id=str(item.get("id") or ""), username=str(item.get("username") or ""),
                message_text=str(item.get("generated_message") or ""),
                message_version=item.get("message_version"),
                record_failure=False,
            )
            if not approval.valid:
                logger.warning("Dispatch candidate blocked for @%s: %s", u, approval.code)
                continue

            first_name, is_trusted = verify_host_name(item)
            approved_neutral = (
                st == "approved"
                and "BULK_APPROVED_NEUTRAL_GREETING" in str(item.get("notes") or "")
                and str(item.get("generated_message") or "").lstrip().startswith("Hello,")
            )
            if (not is_trusted or not first_name) and not approved_neutral:
                if mutate_status:
                    logger.warning(f"Хост @{u} исключен из отправки: сомнительное имя '{item.get('name')}'.")
                    self.update_status(u, "Name_Review_Required", notes="Untrusted display name; manual review required.", trip_id=item.get("trip_id") or trip_id)
                continue

            msg = item.get("generated_message") or ""
            if not msg.strip():
                if mutate_status:
                    logger.warning(f"Хост @{u} исключен из отправки: пустое сообщение.")
                    self.update_status(u, "Draft", notes="Missing message text.", trip_id=item.get("trip_id") or trip_id)
                continue

            bio_str = (item.get("about_me_clean") or item.get("about_me") or "").strip()
            is_minimal = len(bio_str.split()) < 25 or bio_str.lower().startswith("friendly local resident")
            qc_ok, qc_issues = validate_message_qc(
                msg,
                is_minimal_bio=is_minimal,
                host_data=item,
                trip_city=item.get("city") or item.get("trip_city"),
            )
            if not qc_ok:
                if mutate_status:
                    logger.warning(f"Хост @{u} исключен из отправки: QC issues ({qc_issues}).")
                    self.update_status(u, "QC_Review_Required", notes=f"Pre-dispatch QC: {', '.join(qc_issues)}", trip_id=item.get("trip_id") or trip_id)
                continue

            candidates.append(item)

        from core.queue_ranking import rank_hosts
        traveler_interests = self._get_traveler_interest_texts()
        return rank_hosts(candidates, traveler_interests)

    def _get_traveler_interest_texts(self) -> List[str]:
        """Load small, normalized traveler interest facts once per selection."""
        with get_connection(self.db_file) as conn:
            rows = conn.execute(
                """SELECT title, content FROM personal_context_facts
                   WHERE is_active=1 AND status='confirmed'
                     AND category IN ('Hobbies & Interests','Professional Background','Education','Travel Experience')
                   ORDER BY priority DESC, fact_id"""
            ).fetchall()
        return [text for row in rows for text in (row[0], row[1]) if text]

    def update_status(
        self,
        username: str,
        status: str,
        notes: Optional[str] = None,
        trip_id: Optional[str] = None,
        increment_attempts: bool = False,
        attempts: Optional[int] = None,
    ) -> bool:
        """
        Обновляет статус и заметки в core_hosts.

        Инвариант: никогда не устанавливает состояние 'sent' напрямую.
        Переход в 'sent' возможен только через AutopilotEngine.mark_as_sent()
        с валидным ConfirmationCapability токеном.

        Если запрошен статус 'sent' или 'was sent', запрос игнорируется
        с записью предупреждения в лог.
        """
        u_clean = str(username or "").strip()
        status_lower = str(status or "").strip().lower()
        is_target_sent = status_lower in ("sent", "was sent")

        if is_target_sent:
            logger.warning(
                f"update_status rejected: direct transition to 'sent' for {u_clean} is not allowed. "
                "Use AutopilotEngine.mark_as_sent() with ConfirmationCapability instead."
            )
            return False

        active_trip = get_active_trip(self.db_file) if not trip_id else None
        resolved_trip = self._resolve_canonical_trip_id(trip_id or (active_trip.id if active_trip else ""))
        if not resolved_trip:
            logger.warning("Queue state update rejected: no trip context for %s", u_clean)
            return False
        if status_lower == "approved":
            try:
                return QueueEngine(resolved_trip, db_file=self.db_file).approve(u_clean, created_by="user:legacy_queue_action")
            except ValueError as exc:
                logger.info("Approval rejected for %s: %s", u_clean, exc)
                return False

        st_map = {
            "approved": "approved",
            "skipped": "skipped",
            "failed": "skipped",
            "rejected": "rejected",
            "queued": "selected",
            "draft": "drafted",
            "qc_review_required": "drafted",
            "name_review_required": "discovered",
            "uncertain": "uncertain",
            "verification_required": "uncertain",
        }
        c_st = st_map.get(status_lower, "selected")
        if status_lower in {"draft", "queued", "selected", "skipped", "rejected"}:
            from core.approval_service import ApprovalService
            host = self.host_repository.get_host_context(resolved_trip, u_clean)
            if host:
                ApprovalService(self.db_file).revoke_approval(
                    trip_id=resolved_trip, host_id=host["host_id"], reason="user changed queue state"
                )
        return self.host_repository.update_state(
            resolved_trip, u_clean, c_st, notes, increment_attempts, attempts
        )

    def move_host_to_end(self, username: str, trip_id: str, reason: str = "") -> bool:
        """Trip-scoped queue priority update through the canonical repository."""
        resolved_trip = self._resolve_canonical_trip_id(trip_id)
        return self.host_repository.move_to_end(resolved_trip, username, reason)

    def update_item(
        self,
        username: str,
        message: Optional[str] = None,
        status: Optional[str] = None,
        trip_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Save a trip-scoped draft through canonical QC and persistence."""
        active_trip = get_active_trip(self.db_file) if not trip_id else None
        resolved_trip = self._resolve_canonical_trip_id(trip_id or (active_trip.id if active_trip else ""))
        if not resolved_trip:
            return None
        host = self.host_repository.get_host_context(resolved_trip, str(username).strip())
        if not host or host.get("state") == "sent":
            return None

        status_norm = str(status or "").strip().lower()
        if status_norm in ("sent", "was sent"):
            logger.warning("update_item rejected direct sent transition for %s", username)
            return None
        if message is not None:
            bio = host.get("about") or ""
            minimal = len(bio.split()) < 25 or bio.lower().startswith("friendly local resident")
            qc_ok, issues = validate_message_qc(
                message,
                {"city": host.get("trip_city"), "arrival_date": host.get("trip_arrival"), "departure_date": host.get("trip_departure")},
                is_minimal_bio=minimal,
                host_data={**host, "about_me": bio, "about_me_clean": bio},
                trip_city=host.get("trip_city"),
            )
            notes = "Manual edit passed canonical QC" if qc_ok else "; ".join(issues)
            if not self.host_repository.save_draft(resolved_trip, host["host_id"], message, qc_ok, notes):
                return None

        if status_norm == "approved":
            if not QueueEngine(resolved_trip, db_file=self.db_file).approve(
                host["host_id"], edited_text=message, created_by="user:queue_card"
            ):
                return None
        elif status_norm in {"draft", "selected", "queued", "skipped", "rejected"}:
            from core.approval_service import ApprovalService
            ApprovalService(self.db_file).revoke_approval(
                trip_id=resolved_trip, host_id=host["host_id"], reason="user changed queue state or edited message"
            )

        if status_norm and status_norm != "approved":
            self.update_status(username, status_norm, trip_id=resolved_trip)

        return next(
            (item for item in self.load_queue(exclude_sent=False, trip_id=resolved_trip)
             if item.get("username", "").lower() == str(username).lower()), None
        )

    def regenerate_item(
        self,
        username: str,
        check_in: Optional[str] = None,
        check_out: Optional[str] = None,
        guest_context: Optional[str] = None,
        trip_id: Optional[str] = None,
        city: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Regenerate one trip-scoped draft with the canonical message policy."""
        from core.identity import verify_host_name
        from core.messaging import build_personalized_message
        from data_cleaner import sanitize_host_clean_name, sanitize_host_location, sanitize_host_name

        active_trip = get_active_trip(self.db_file) if not trip_id else None
        resolved_trip_id = self._resolve_canonical_trip_id(trip_id or (active_trip.id if active_trip else ""))
        if not resolved_trip_id:
            logger.warning("Regeneration requires an explicit or active trip; refusing an unscoped host lookup.")
            return None

        host = self.host_repository.get_host_context(resolved_trip_id, username.strip())
        if not host or host.get("state") not in ("discovered", "selected", "drafted", "approved"):
            return None

        check_in = check_in or host.get("trip_arrival") or DEFAULT_CHECK_IN
        check_out = check_out or host.get("trip_departure") or DEFAULT_CHECK_OUT
        effective_city = (city or host.get("trip_city") or "").strip()
        host["city"] = effective_city
        host["about_me"] = host.get("about") or ""
        host["about_me_clean"] = host["about_me"]
        try:
            host["score_reasons"] = json.loads(host.get("score_reasons_json") or "[]")
        except (TypeError, ValueError):
            host["score_reasons"] = []
        try:
            host["interests"] = json.loads(host.get("interests_json") or "[]")
        except (TypeError, ValueError):
            host["interests"] = []
        host["interests_text"] = ", ".join(host["interests"])
        raw_name = host.get("name") or username
        host["name"] = sanitize_host_name(raw_name)
        host["clean_name"] = sanitize_host_clean_name(host.get("clean_name") or raw_name)
        host["location"] = sanitize_host_location(host.get("location"), effective_city)
        first_name, trusted_name = verify_host_name(host)
        if trusted_name and first_name:
            host["clean_name"] = first_name
        host["is_name_trusted"] = trusted_name

        trip = {"city": effective_city, "arrival_date": check_in, "departure_date": check_out}
        message, qc_passed, qc_notes = build_personalized_message(host, trip)
        if not trusted_name:
            item_status = "Name_Review_Required"
            qc_notes = "Valid host display name not found; neutral greeting used and manual review required."
        elif not qc_passed:
            item_status = "QC_Review_Required"
        else:
            item_status = "Draft"

        persisted_state = "drafted" if trusted_name else "discovered"
        updated = self.host_repository.save_draft(
            resolved_trip_id,
            host["host_id"],
            message,
            bool(qc_passed and trusted_name),
            qc_notes,
            state=persisted_state,
        )
        if not updated:
            return None
        now = datetime.now().isoformat()
        log_audit(
            trip_id=resolved_trip_id,
            module="QUEUE",
            event="DRAFT_REGENERATED",
            host_id=host["host_id"],
            details={"qc_passed": bool(qc_passed and trusted_name), "state": persisted_state},
            db_file=self.db_file,
        )

        host.update({
            "generated_message": message,
            "message": message,
            "word_count": len(message.split()),
            "qc_passed": bool(qc_passed and trusted_name),
            "status": item_status,
            "notes": qc_notes,
            "check_in": check_in,
            "check_out": check_out,
            "updated_at": now,
            "anchor": "Deterministic profile-based introduction",
            "anchor_used": "Deterministic profile-based introduction",
            "host_fact_used": (host.get("score_reasons") or ["Profile context"])[0],
            "user_fact_used": "Respectful guest and trip context",
            "confidence": 0.8,
            "fallback_used": len(host["about_me"].split()) < 25,
        })
        return host

    def cleanup_blacklisted_from_queue(self) -> int:
        now_iso = datetime.now().isoformat()
        skipped_count = 0
        with db_session(self.db_file) as conn:
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
        active_trip = get_active_trip(self.db_file)
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

        with db_session(self.db_file) as conn:
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

                    about_text = h.get("about_me") or h.get("about") or h.get("about_me_clean") or ""
                    eligibility = evaluate_host_eligibility(
                        h,
                        arrival_date=t_arr,
                        departure_date=t_dep,
                        target_city=t_city,
                        guest_gender="male",
                    )
                    exclusion = eligibility.get("reason") or ("Host marked as not accepting guests" if h.get("is_hosting") == "not_accepting" else None)

                    clean_name = h.get("clean_name") or h.get("name") or host_username
                    first_name = clean_name.split()[0] if clean_name else host_username
                    loc = h.get("location") or t_city
                    if isinstance(loc, dict):
                        loc = loc.get("locationDescription", {}).get("description", t_city)
                    activity = _activity_from_host(h)
                    score_stars, score_reasons = calculate_match_score(h)
                    verification_value = h.get("is_verified")
                    if verification_value is None:
                        verification_value = h.get("isIdentificationVerified")
                    verification_text = str(verification_value).strip().lower()
                    if verification_value is True or verification_text in {"true", "1", "yes", "verified"}:
                        verification_status = "VERIFIED"
                    elif verification_value is False or verification_text in {"false", "0", "no", "not verified", "not_verified", "unverified"}:
                        verification_status = "NOT_VERIFIED"
                    else:
                        verification_status = "UNKNOWN"

                    draft_text, qc_ok, qc_note = build_personalized_message(h, {"city": t_city, "arrival_date": t_arr, "departure_date": t_dep})

                    cur.execute(
                        """
                        INSERT INTO core_hosts (
                            host_id, trip_id, username, name, clean_name, location,
                            about, references_count, response_rate_pct, languages_json,
                            state, score_stars, score_reasons_json, priority, attempts,
                            message_draft, qc_passed, qc_notes, is_verified, verification_status,
                            recently_active_status, last_active_raw, last_login_days, last_active_at,
                            activity_checked_at, activity_source, eligibility_reason,
                            eligibility_evidence_json, eligibility_checked_at,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                            "skipped" if exclusion else "selected",
                            score_stars,
                            json.dumps(score_reasons),
                            1000 if activity["status"] == "RECENTLY_ACTIVE" else 10,
                            draft_text,
                            1 if qc_ok else 0,
                            f"Eligibility exclusion: {exclusion}" if exclusion else qc_note,
                            1 if verification_status == "VERIFIED" else 0,
                            verification_status,
                            activity["status"],
                            activity["raw"],
                            activity["days"],
                            activity["at"],
                            h.get("activity_checked_at") or now_iso,
                            h.get("activity_source") or ("couchsurfing_api" if "isRecentlyActive" in h else "search_card"),
                            exclusion,
                            json.dumps(h.get("eligibility_evidence") or ({"source": eligibility.get("source"), "excerpt": eligibility.get("excerpt")} if exclusion else {}), ensure_ascii=False),
                            now_iso,
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
        if return_details:
            return len(added_items), added_items
        return len(added_items)

    def set_status_for_selected(
        self, usernames: List[str], status: str, trip_id: Optional[str] = None,
    ) -> int:
        """Apply selected-host transitions through the domain queue engine."""
        status_lower = str(status or "").strip().lower()
        if status_lower in ("sent", "was sent"):
            logger.warning("Bulk queue update rejected direct sent transition")
            return 0
        active_trip = get_active_trip(self.db_file) if not trip_id else None
        resolved_trip = self._resolve_canonical_trip_id(trip_id or (active_trip.id if active_trip else ""))
        if not resolved_trip:
            logger.warning("Bulk queue update rejected: trip context is required")
            return 0

        if status_lower == "approved":
            engine = QueueEngine(resolved_trip, db_file=self.db_file)
            approved = 0
            for username in usernames:
                try:
                    approved += int(engine.approve(username, created_by="user:approve_selected"))
                except ValueError as exc:
                    logger.info("Selected host %s did not pass approval QC: %s", username, exc)
            return approved

        updated = 0
        for username in usernames:
            before = self.host_repository.get_host_context(resolved_trip, username)
            if before and before.get("state") != "sent":
                updated += int(bool(self.update_status(username, status_lower, trip_id=resolved_trip)))
        return updated

    def skip_item(self, username: str, reason: str = "User skipped", trip_id: Optional[str] = None) -> bool:
        """Mark a host skipped through the trip-scoped repository."""
        active_trip = get_active_trip(self.db_file) if not trip_id else None
        resolved_trip = self._resolve_canonical_trip_id(trip_id or (active_trip.id if active_trip else ""))
        if not resolved_trip:
            return False
        return self.host_repository.update_state(resolved_trip, username, "skipped", reason)

    def skip_many(self, trip_id: str, usernames: Optional[List[str]] = None) -> int:
        resolved_trip = self._resolve_canonical_trip_id(trip_id)
        return self.host_repository.skip_many(resolved_trip, usernames)

    def delete_item(self, username: str) -> bool:
        """Переводит хоста в статус 'skipped' (не удаляя физически)."""
        return self.skip_item(username)


_rqm_instance: Optional[RequestQueueManager] = None


def get_queue_manager() -> RequestQueueManager:
    global _rqm_instance
    if _rqm_instance is None:
        _rqm_instance = RequestQueueManager()
    return _rqm_instance
