"""SQLite persistence adapter for trip-scoped host queue operations."""

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.database import get_active_trip, get_connection, get_read_only_connection
from core.models import HostState


class HostQueueRepository:
    """Owns queue reads and state writes against the canonical core tables."""

    def __init__(self, db_file: Optional[Path | str] = None):
        self.db_file = Path(db_file) if db_file else None

    def resolve_trip(self, trip_id: Optional[str]) -> Optional[str]:
        if trip_id:
            return str(trip_id).strip()
        active = get_active_trip(self.db_file)
        return active.id if active else None

    def get_trip(self, trip_id: str) -> Optional[Dict[str, Any]]:
        with get_connection(self.db_file) as conn:
            row = conn.execute("SELECT * FROM core_trips WHERE id=?", (trip_id,)).fetchone()
            return dict(row) if row else None

    def list_hosts(self, trip_id: str, states: List[str]) -> List[Dict[str, Any]]:
        if not states:
            return []
        placeholders = ",".join("?" for _ in states)
        with get_connection(self.db_file) as conn:
            rows = conn.execute(
                f"""SELECT * FROM core_hosts WHERE trip_id=? AND state IN ({placeholders})
                    ORDER BY CASE state WHEN 'approved' THEN 1 WHEN 'drafted' THEN 2 ELSE 3 END,
                             score_stars DESC, updated_at DESC""",
                [trip_id, *states],
            ).fetchall()
            return [dict(row) for row in rows]

    def get_trip_hosts(
        self,
        trip_id: str,
        *,
        search: str = "",
        page: int = 1,
        page_size: int = 50,
    ) -> Dict[str, Any]:
        """Read the complete trip host collection, independent of dispatch authorization.

        Hosts are deduplicated by registered canonical identity, falling back to a
        normalized username only when no identity record exists. This is a read
        model: it never repairs state or creates missing records.
        """
        page = max(1, int(page))
        page_size = min(200, max(1, int(page_size)))
        offset = (page - 1) * page_size
        search = str(search or "").strip().lower()
        search_clause = ""
        search_params: List[Any] = []
        if search:
            search_clause = " AND (LOWER(h.username) LIKE ? OR LOWER(h.name) LIKE ? OR LOWER(h.location) LIKE ?)"
            needle = f"%{search}%"
            search_params = [needle, needle, needle]

        # For duplicate aliases keep the most conservative state and then the
        # freshest/fullest row. Dispatch still performs its own strict checks.
        dedup_cte = """
            WITH trip_rows AS (
                SELECT h.trip_id,h.host_id,h.username,h.name,h.location,h.state,h.message_draft,
                       h.message_version,h.updated_at,h.priority,h.recently_active_status,
                       h.last_login_days,h.score_stars,
                       CASE WHEN hi.canonical_key IS NOT NULL AND ci.canonical_key IS NOT NULL AND hi.canonical_key<>ci.canonical_key
                            THEN 'conflict:' || LOWER(TRIM(h.host_id))
                            ELSE COALESCE(hi.canonical_key,ci.canonical_key,'un:' || LOWER(TRIM(h.username))) END AS canonical_host_key,
                       ROW_NUMBER() OVER (
                           PARTITION BY CASE
                             WHEN hi.canonical_key IS NOT NULL AND ci.canonical_key IS NOT NULL AND hi.canonical_key<>ci.canonical_key
                               THEN 'conflict:' || LOWER(TRIM(h.host_id))
                             ELSE COALESCE(hi.canonical_key,ci.canonical_key,'un:' || LOWER(TRIM(h.username))) END
                           ORDER BY CASE LOWER(h.state)
                               WHEN 'uncertain' THEN 1 WHEN 'sending' THEN 2 WHEN 'sent' THEN 3
                               WHEN 'skipped' THEN 4 WHEN 'approved' THEN 5 WHEN 'drafted' THEN 6
                               WHEN 'selected' THEN 7 ELSE 8 END,
                               CASE WHEN COALESCE(TRIM(h.message_draft),'')<>'' THEN 0 ELSE 1 END,
                               COALESCE(h.updated_at,'' ) DESC, h.host_id ASC
                       ) AS identity_rank
                FROM core_hosts h
                LEFT JOIN host_identities hi
                  ON hi.identity_type='username'
                 AND LOWER(hi.identity_value)=LOWER(TRIM(h.username))
                LEFT JOIN host_identities ci
                  ON ci.identity_type='cs_id' AND ci.identity_value=TRIM(h.host_id)
                WHERE h.trip_id=?
            ), collection AS (
                SELECT * FROM trip_rows WHERE identity_rank=1
            )
        """
        with get_read_only_connection(self.db_file) as conn:
            trip = conn.execute(
                "SELECT id,city,arrival_date,departure_date FROM core_trips WHERE id=?", (trip_id,)
            ).fetchone()
            if not trip:
                return {"trip_id": trip_id, "hosts": [], "pagination": {"page": page, "page_size": page_size, "total_hosts": 0, "total_pages": 0}, "stats": {}}

            stats_rows = conn.execute(
                dedup_cte + """
                SELECT h.trip_id,h.host_id,h.state,h.message_draft,h.message_version
                FROM collection h
                """, (trip_id,)
            ).fetchall()
            approval_rows = conn.execute(
                """SELECT trip_id,host_id,approval_status,approved_message_hash,message_version,revoked_at
                   FROM message_approvals WHERE trip_id=? ORDER BY approved_at DESC,approval_id DESC""",
                (trip_id,),
            ).fetchall()
            approvals = {}
            for approval_row in approval_rows:
                approvals.setdefault((approval_row["trip_id"], approval_row["host_id"]), approval_row)
            from core.approval_service import hash_message
            stats = {key: 0 for key in ("total", "discovered", "draft_ready", "without_draft", "approval_required", "approved", "uncertain", "sent", "skipped")}
            for stat_row in stats_rows:
                state = str(stat_row["state"] or "").lower()
                has_draft = bool(str(stat_row["message_draft"] or "").strip())
                approval = approvals.get((stat_row["trip_id"], stat_row["host_id"]))
                is_approved = bool(
                    approval and approval["approval_status"] == "approved"
                    and not approval["revoked_at"]
                    and int(approval["message_version"] or 0) == int(stat_row["message_version"] or 1)
                    and approval["approved_message_hash"] == hash_message(stat_row["message_draft"] or "")
                    and state == "approved"
                    and state not in {"sent", "sending", "uncertain", "skipped"}
                )
                stats["total"] += 1
                stats["discovered"] += int(state == "discovered")
                stats["draft_ready"] += int(has_draft)
                stats["without_draft"] += int(not has_draft)
                stats["approved"] += int(is_approved)
                stats["approval_required"] += int(has_draft and not is_approved and state not in {"sent", "uncertain", "skipped"})
                stats["uncertain"] += int(state == "uncertain")
                stats["sent"] += int(state == "sent")
                stats["skipped"] += int(state == "skipped")
            total_filtered = stats["total"] if not search else conn.execute(
                dedup_cte + "SELECT COUNT(*) FROM collection h WHERE 1=1" + search_clause,
                [trip_id, *search_params],
            ).fetchone()[0]
            rows = conn.execute(
                dedup_cte + """
                SELECT h.*, c.canonical_host_key,
                       (SELECT ma.approval_status FROM message_approvals ma
                        WHERE ma.trip_id=h.trip_id AND ma.host_id=h.host_id
                        ORDER BY ma.approved_at DESC, ma.approval_id DESC LIMIT 1) AS approval_status,
                       (SELECT ma.approval_id FROM message_approvals ma
                        WHERE ma.trip_id=h.trip_id AND ma.host_id=h.host_id
                        ORDER BY ma.approved_at DESC, ma.approval_id DESC LIMIT 1) AS approval_id,
                       (SELECT ma.approved_message_hash FROM message_approvals ma
                        WHERE ma.trip_id=h.trip_id AND ma.host_id=h.host_id
                        ORDER BY ma.approved_at DESC, ma.approval_id DESC LIMIT 1) AS approved_message_hash,
                       (SELECT ma.message_version FROM message_approvals ma
                        WHERE ma.trip_id=h.trip_id AND ma.host_id=h.host_id
                        ORDER BY ma.approved_at DESC, ma.approval_id DESC LIMIT 1) AS approved_message_version,
                       (SELECT ma.revoked_at FROM message_approvals ma
                        WHERE ma.trip_id=h.trip_id AND ma.host_id=h.host_id
                        ORDER BY ma.approved_at DESC, ma.approval_id DESC LIMIT 1) AS approval_revoked_at,
                       CASE WHEN LOWER(h.state) IN ('uncertain','sending') THEN LOWER(h.state)
                            WHEN LOWER(h.state)='sent' THEN 'sent'
                            WHEN LOWER(h.state)='skipped' THEN 'skipped'
                            WHEN COALESCE(TRIM(h.message_draft),'')='' THEN 'draft_missing'
                            WHEN LOWER(h.state)='approved' THEN 'approval_required_or_invalid'
                            ELSE 'not_authorized' END AS dispatch_state
                FROM collection c JOIN core_hosts h ON h.trip_id=c.trip_id AND h.host_id=c.host_id
                WHERE 1=1
                """ + search_clause + """
                ORDER BY COALESCE(h.priority,0) DESC,
                         CASE WHEN h.recently_active_status='ACTIVE' THEN 0
                              WHEN h.recently_active_status='RECENTLY_ACTIVE' THEN 1 ELSE 2 END,
                         COALESCE(h.last_login_days,2147483647), COALESCE(h.score_stars,0) DESC,
                         LOWER(TRIM(h.username)), h.host_id
                LIMIT ? OFFSET ?
                """, [trip_id, *search_params, page_size, offset]
            ).fetchall()

            from core.approval_service import hash_message
            hosts = []
            for row in rows:
                host = dict(row)
                host["approval_valid"] = bool(
                    host.get("approval_status") == "approved"
                    and not host.get("approval_revoked_at")
                    and int(host.get("approved_message_version") or 0) == int(host.get("message_version") or 1)
                    and host.get("approved_message_hash") == hash_message(host.get("message_draft") or "")
                    and str(host.get("state") or "").lower() not in {"sent", "sending", "uncertain", "skipped"}
                )
                if host.get("approval_valid"):
                    host["dispatch_state"] = "authorization_valid_other_checks_required"
                elif str(host.get("state") or "").lower() == "approved":
                    host["dispatch_state"] = "approval_missing_or_invalid"
                host["trip_city"] = trip["city"]
                host["trip_arrival"] = trip["arrival_date"]
                host["trip_departure"] = trip["departure_date"]
                hosts.append(host)
            total = int(total_filtered or 0)
            return {
                "trip_id": trip_id,
                "trip": dict(trip),
                "hosts": hosts,
                "pagination": {"page": page, "page_size": page_size, "total_hosts": total, "total_pages": (total + page_size - 1) // page_size},
                "stats": stats,
            }

    def approve_host(self, trip_id: str, host_id: str, message: str, created_by: str) -> bool:
        from core.approval_service import ApprovalService
        with get_connection(self.db_file) as conn:
            row = conn.execute(
                "SELECT host_id,username FROM core_hosts WHERE trip_id=? AND (host_id=? OR LOWER(username)=LOWER(?))",
                (trip_id, host_id, host_id),
            ).fetchone()
        if not row:
            return False
        result = ApprovalService(self.db_file).approve_message(
            trip_id=trip_id, host_id=row["host_id"], username=row["username"],
            message_text=message, created_by=created_by,
        )
        return result.valid

    def get_trip_and_found_hosts(self, trip_id: str) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
        with get_connection(self.db_file) as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT id, city, arrival_date, departure_date FROM core_trips WHERE id = ?",
                (trip_id,),
            )
            trip_row = cur.fetchone()
            if not trip_row:
                return None, []
            trip = dict(trip_row)
            cur.execute(
                """SELECT host_id, trip_id, username, name, clean_name, location, occupation,
                          about, interests_json, score_reasons_json, message_draft, message_version, state
                   FROM core_hosts
                   WHERE trip_id = ? AND state IN ('discovered', 'drafted', 'selected', 'approved')
                   ORDER BY priority DESC, score_stars DESC, created_at ASC""",
                (trip_id,),
            )
            hosts = []
            for row in cur.fetchall():
                host = dict(row)
                snapshot = cur.execute(
                    "SELECT raw_data_json FROM core_host_snapshots WHERE trip_id=? AND host_id=? ORDER BY captured_at DESC LIMIT 1",
                    (trip_id, host["host_id"]),
                ).fetchone()
                if snapshot:
                    try:
                        raw_profile = json.loads(snapshot["raw_data_json"] or "{}")
                        if isinstance(raw_profile, dict):
                            host.update(raw_profile)
                    except (TypeError, ValueError):
                        pass
                host["host_id"] = host.get("host_id") or host.get("id")
                host["about"] = host.get("about") or host.get("about_me") or host.get("aboutMe")
                host["arrival_date"] = trip.get("arrival_date")
                host["departure_date"] = trip.get("departure_date")
                host["target_city"] = trip.get("city")
                host["about_me"] = host.get("about") or ""
                host["about_me_clean"] = host["about_me"]
                try:
                    host["score_reasons"] = json.loads(host.get("score_reasons_json") or "[]")
                except (TypeError, ValueError):
                    host["score_reasons"] = []
                try:
                    interests = json.loads(host.get("interests_json") or "[]")
                except (TypeError, ValueError):
                    interests = []
                host["interests"] = interests
                host["interests_text"] = ", ".join(interests) if isinstance(interests, list) else str(interests or "")
                hosts.append(host)
            return trip, hosts

    def reject_ineligible(self, trip_id: str, host_id: str, reason: str, evidence: Dict[str, Any]) -> bool:
        now = datetime.now().isoformat()
        with get_connection(self.db_file) as conn:
            cur = conn.execute(
                """UPDATE core_hosts SET state='skipped', eligibility_reason=?, eligibility_evidence_json=?,
                          eligibility_checked_at=?, qc_notes=?, updated_at=?
                   WHERE trip_id=? AND host_id=? AND state IN ('discovered','selected','drafted')""",
                (reason, json.dumps(evidence, ensure_ascii=False), now, f"Eligibility exclusion: {reason}", now, trip_id, host_id),
            )
            return cur.rowcount > 0

    def approve_prepared(
        self,
        trip_id: str,
        prepared: List[Dict[str, str]],
        considered_count: int,
        failed_count: int,
        neutral_greeting_count: int,
    ) -> int:
        """Atomically approves only still-eligible hosts from the requested trip."""
        approved = 0
        from core.approval_service import ApprovalService
        approval_service = ApprovalService(self.db_file)
        for entry in prepared:
            with get_connection(self.db_file) as conn:
                row = conn.execute(
                    "SELECT username FROM core_hosts WHERE trip_id=? AND host_id=?",
                    (trip_id, entry["host_id"]),
                ).fetchone()
            if not row:
                continue
            result = approval_service.approve_message(
                trip_id=trip_id, host_id=entry["host_id"], username=row["username"],
                message_text=entry["message"], created_by="user:approve_all_found",
            )
            if result.valid:
                approved += 1
                with get_connection(self.db_file) as conn:
                    conn.execute(
                        "UPDATE core_hosts SET qc_notes=? WHERE trip_id=? AND host_id=?",
                        (entry["audit_marker"], trip_id, entry["host_id"]),
                    )
        now = datetime.now().isoformat()
        with get_connection(self.db_file) as conn:
            cur = conn.cursor()
            cur.execute(
                """INSERT INTO core_audit_logs (timestamp, trip_id, module, event, host_id, details_json)
                   VALUES (?, ?, 'queue', 'bulk_approve_found', NULL, ?)""",
                (now, trip_id, json.dumps({
                    "approved": approved,
                    "neutral_greetings": neutral_greeting_count,
                    "failed_qc_or_safety": failed_count,
                    "considered": considered_count,
                }, ensure_ascii=False)),
            )
        return approved

    def update_state(
        self,
        trip_id: str,
        host_key: str,
        state: str,
        notes: Optional[str] = None,
        increment_attempts: bool = False,
        attempts: Optional[int] = None,
    ) -> bool:
        """Persist a non-sent transition for exactly one host in exactly one trip."""
        if state in {"sent", "approved"}:
            return False
        now = datetime.now().isoformat()
        assignments = ["state = ?", "qc_notes = COALESCE(?, qc_notes)", "updated_at = ?"]
        params: List[Any] = [state, notes, now]
        if increment_attempts:
            assignments.append("attempts = COALESCE(attempts, 0) + 1")
        elif attempts is not None:
            assignments.append("attempts = ?")
            params.append(int(attempts))
        params.extend([trip_id, host_key, host_key])
        with get_connection(self.db_file) as conn:
            cur = conn.cursor()
            current = cur.execute(
                "SELECT state FROM core_hosts WHERE trip_id=? AND (LOWER(username)=LOWER(?) OR LOWER(host_id)=LOWER(?)) LIMIT 1",
                (trip_id, host_key, host_key),
            ).fetchone()
            if not current or current["state"] == "sent":
                return False
            try:
                source_state = HostState(current["state"])
                target_state = HostState(state)
            except ValueError:
                return False
            if source_state != target_state and not source_state.can_transition_to(target_state):
                return False
            params.append(source_state.value)
            cur.execute(
                f"""UPDATE core_hosts SET {', '.join(assignments)}
                    WHERE trip_id=? AND (LOWER(username)=LOWER(?) OR LOWER(host_id)=LOWER(?))
                      AND state=?""",
                params,
            )
            return cur.rowcount > 0

    def move_to_end(self, trip_id: str, host_key: str, reason: str) -> bool:
        now = datetime.now().isoformat()
        with get_connection(self.db_file) as conn:
            cur = conn.cursor()
            cur.execute(
                """UPDATE core_hosts SET priority=COALESCE(priority,10)-100,
                          attempts=COALESCE(attempts,0)+1, qc_notes=?, updated_at=?
                   WHERE trip_id=? AND (LOWER(username)=LOWER(?) OR LOWER(host_id)=LOWER(?))
                     AND state NOT IN ('sent','rejected','skipped')""",
                (f"Moved to bottom: {reason}", now, trip_id, host_key, host_key),
            )
            return cur.rowcount > 0

    def get_host_context(self, trip_id: str, host_key: str) -> Optional[Dict[str, Any]]:
        with get_connection(self.db_file) as conn:
            cur = conn.cursor()
            cur.execute(
                """SELECT h.*, t.city AS trip_city, t.arrival_date AS trip_arrival,
                          t.departure_date AS trip_departure
                   FROM core_hosts h JOIN core_trips t ON t.id=h.trip_id
                   WHERE h.trip_id=? AND (LOWER(h.username)=LOWER(?) OR LOWER(h.host_id)=LOWER(?))
                   LIMIT 1""",
                (trip_id, host_key, host_key),
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def update_host_name(self, trip_id: str, host_key: str, name: str, clean_name: str) -> bool:
        """Persist a corrected name and invalidate any message approved for the old name."""
        now = datetime.now().isoformat()
        with get_connection(self.db_file) as conn:
            cur = conn.cursor()
            row = cur.execute(
                """SELECT host_id, state FROM core_hosts
                   WHERE trip_id=? AND (LOWER(username)=LOWER(?) OR LOWER(host_id)=LOWER(?))
                   LIMIT 1""",
                (trip_id, host_key, host_key),
            ).fetchone()
            if not row or row["state"] not in ("discovered", "selected", "drafted", "approved"):
                return False

            state = row["state"]
            target_state = "drafted" if state in ("selected", "drafted", "approved") else "discovered"
            cur.execute(
                """UPDATE core_hosts SET name=?, clean_name=?, state=?, message_draft='',
                          qc_passed=0, qc_notes='Host name corrected; message regeneration required', updated_at=?
                   WHERE trip_id=? AND host_id=? AND state=?""",
                (name, clean_name, target_state, now, trip_id, row["host_id"], state),
            )
            return cur.rowcount > 0

    def save_draft(
        self, trip_id: str, host_id: str, message: str, qc_passed: bool, notes: str,
        state: str = "drafted",
    ) -> bool:
        now = datetime.now().isoformat()
        with get_connection(self.db_file) as conn:
            cur = conn.cursor()
            current = cur.execute(
                "SELECT state FROM core_hosts WHERE trip_id=? AND host_id=?",
                (trip_id, host_id),
            ).fetchone()
            if not current:
                return False
            try:
                source_state = HostState(current["state"])
                target_state = HostState(state)
            except ValueError:
                return False
            if source_state != target_state and not source_state.can_transition_to(target_state):
                return False
            cur.execute(
                """UPDATE core_hosts SET state=?, message_draft=?, qc_passed=?,
                          qc_notes=?, updated_at=?
                   WHERE trip_id=? AND host_id=? AND state=?""",
                (state, message, int(qc_passed), notes, now, trip_id, host_id, source_state.value),
            )
            updated = cur.rowcount > 0
        if updated:
            from core.approval_service import ApprovalService
            ApprovalService(self.db_file).invalidate_stale_approvals(trip_id)
        return updated

    def skip_many(self, trip_id: str, host_keys: Optional[List[str]] = None) -> int:
        now = datetime.now().isoformat()
        with get_connection(self.db_file) as conn:
            cur = conn.cursor()
            if host_keys:
                keys = [str(key).strip().lower() for key in host_keys if str(key).strip()]
                if not keys:
                    return 0
                placeholders = ",".join("?" for _ in keys)
                cur.execute(
                    f"""UPDATE core_hosts SET state='skipped', qc_notes='Skipped by user', updated_at=?
                        WHERE trip_id=? AND state IN ('discovered','selected','drafted','approved')
                          AND (LOWER(username) IN ({placeholders}) OR LOWER(host_id) IN ({placeholders}))""",
                    [now, trip_id, *keys, *keys],
                )
            else:
                cur.execute(
                    """UPDATE core_hosts SET state='skipped', qc_notes='Queue cleared by user', updated_at=?
                       WHERE trip_id=? AND state IN ('selected','drafted','approved')""",
                    (now, trip_id),
                )
            return cur.rowcount
