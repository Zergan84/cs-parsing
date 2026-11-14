"""Evidence-based repair for an already confirmed dispatch record."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from core.database import get_connection


def reconcile_confirmed_attempt(
    attempt_id: str,
    expected_host_id: str,
    expected_trip_id: str,
    db_file: str | None = None,
) -> dict[str, Any]:
    """Reconcile one attempt only when its persisted evidence agrees.

    Supported evidence shapes are:
    * a matching live SENT_SUCCESS history row plus its verified audit; or
    * the original live RESERVED history row, a REQUEST_SENT_VERIFIED audit
      containing the visible View Stay Request confirmation, and a sent host
      whose timestamp exactly matches the audit and attempt update timestamp.

    The second shape repairs the historical write gap without rewriting the
    original reservation history or creating another dispatch attempt.
    """
    with get_connection(db_file) as conn:
        conn.execute("BEGIN IMMEDIATE")
        attempt = conn.execute(
            "SELECT * FROM sending_attempts WHERE id=?", (attempt_id,)
        ).fetchone()
        if not attempt:
            raise ValueError("Attempt not found")
        if (
            str(attempt["host_id"]).casefold() != str(expected_host_id).casefold()
            or str(attempt["trip_id"]).casefold() != str(expected_trip_id).casefold()
        ):
            raise ValueError("Attempt identity does not match the requested host and Trip")
        if (
            str(attempt["state"]).casefold() != "sent"
            or str(attempt["dispatch_outcome"]).upper() != "CONFIRMED_SENT"
        ):
            raise ValueError("Attempt does not already have a confirmed sent outcome")

        host_rows = conn.execute(
            """SELECT * FROM core_hosts
               WHERE LOWER(trip_id)=LOWER(?)
                 AND (LOWER(host_id)=LOWER(?) OR LOWER(username)=LOWER(?))""",
            (expected_trip_id, expected_host_id, attempt["username"]),
        ).fetchall()
        if len(host_rows) != 1:
            raise ValueError("The matching trip host is missing or ambiguous")
        host = host_rows[0]
        if str(host["state"]).casefold() != "sent" or not host["sent_at"]:
            raise ValueError("The matching core host is not already in the confirmed sent state")

        history_rows = conn.execute(
            """SELECT * FROM sending_history
               WHERE attempt_id=? AND LOWER(trip_id)=LOWER(?)
               ORDER BY timestamp,id""",
            (attempt_id, expected_trip_id),
        ).fetchall()
        allowed_history_hosts = {
            str(value).casefold()
            for value in (attempt["host_id"], attempt["username"], host["host_id"], host["username"])
            if value
        }
        history_rows = [
            row for row in history_rows
            if str(row["host_id"]).casefold() in allowed_history_hosts
        ]
        if not history_rows:
            raise ValueError("No sending history is linked to this attempt, host, and Trip")
        if any(str(row["status"]).upper() in {"FAILED", "UNKNOWN_OUTCOME", "UNCERTAIN"} for row in history_rows):
            raise ValueError("History contains a conflicting failure or uncertain outcome")

        audit = None
        for row in conn.execute(
            """SELECT id,timestamp,host_id,details_json FROM core_audit_logs
               WHERE LOWER(trip_id)=LOWER(?) AND event='REQUEST_SENT_VERIFIED'
               ORDER BY id""",
            (expected_trip_id,),
        ):
            try:
                details = json.loads(row["details_json"] or "{}")
            except (TypeError, ValueError):
                continue
            audit_host_ids = {str(value).casefold() for value in (
                attempt["host_id"], attempt["username"], host["host_id"], host["username"]
            ) if value}
            try:
                http_status = int(details.get("http_status") or 0)
            except (TypeError, ValueError):
                continue
            if (
                details.get("attempt_id") == attempt_id
                and details.get("evidence_type") == "view_stay_request_button"
                and 200 <= http_status < 300
                and str(row["host_id"]).casefold() in audit_host_ids
            ):
                audit = (row, details, http_status)
                break
        if not audit:
            raise ValueError("No matching REQUEST_SENT_VERIFIED audit event with confirmation evidence")

        audit_row, audit_details, http_status = audit
        evidence_timestamp = str(audit_row["timestamp"])
        if str(host["sent_at"]) != evidence_timestamp:
            raise ValueError("Host sent_at does not match the verified confirmation timestamp")
        if str(attempt["updated_at"]) != evidence_timestamp:
            raise ValueError("Attempt update timestamp does not match the verified confirmation timestamp")
        if attempt["sent_at"] and str(attempt["sent_at"]) != evidence_timestamp:
            raise ValueError("Attempt sent_at conflicts with the verified confirmation timestamp")
        if attempt["http_status"] is not None and int(attempt["http_status"]) != http_status:
            raise ValueError("Attempt HTTP status conflicts with the verified confirmation")
        if str(attempt["evidence_type"] or "none") not in {"none", "confirmed_acceptance", "view_stay_request_button"}:
            raise ValueError("Attempt evidence type conflicts with the verified confirmation")

        successful_history = [
            row for row in history_rows
            if str(row["status"]).upper() in {"SENT_SUCCESS", "SUCCESS", "SENT"}
            and str(row["mode"]).casefold() == "live"
            and row["evidence_type"] == "view_stay_request_button"
            and row["http_status"] is not None
            and 200 <= int(row["http_status"]) < 300
            and str(row["timestamp"]) == evidence_timestamp
        ]
        if successful_history:
            supporting_history = successful_history[0]
        else:
            reservations = [
                row for row in history_rows
                if str(row["status"]).upper() == "RESERVED"
                and str(row["mode"]).casefold() == "live"
                and str(row["timestamp"]) < evidence_timestamp
                and 0 <= (
                    datetime.fromisoformat(evidence_timestamp)
                    - datetime.fromisoformat(str(row["timestamp"]))
                ).total_seconds() <= 300
            ]
            conflicting_terminal = [row for row in history_rows if str(row["status"]).upper() in {"SENT_SUCCESS", "SUCCESS", "SENT"}]
            if not reservations or conflicting_terminal:
                raise ValueError("History has no safe reservation-to-confirmation evidence chain")
            supporting_history = reservations[0]

        lock = conn.execute(
            "SELECT lock_state FROM host_dispatch_locks WHERE attempt_id=?", (attempt_id,)
        ).fetchone()
        if not lock or str(lock["lock_state"]).casefold() not in {"reserved", "sent"}:
            raise ValueError("The attempt does not have a reconcilable reserved/sent dispatch lock")

        consistent = (
            str(attempt["send_intent_committed"] or 0) == "1"
            and str(attempt["send_initiated"] or 0) == "1"
            and str(attempt["evidence_type"]) == "confirmed_acceptance"
            and attempt["sent_at"] == evidence_timestamp
            and int(attempt["http_status"] or 0) == http_status
            and str(lock["lock_state"]).casefold() == "sent"
        )
        if consistent:
            return {"status": "already_consistent", "attempt_id": attempt_id}

        history_ref = f"sending_history:{supporting_history['id']}"
        audit_ref = f"audit:{audit_row['id']}"
        conn.execute(
            """UPDATE sending_attempts
               SET state='sent', dispatch_outcome='CONFIRMED_SENT',
                   send_intent_committed=1, send_initiated=1,
                   http_status=?, evidence_type='confirmed_acceptance',
                   sent_at=?, evidence_reference=COALESCE(evidence_reference, ?)
               WHERE id=?""",
            (http_status, evidence_timestamp, f"{history_ref};{audit_ref}", attempt_id),
        )
        conn.execute(
            """UPDATE host_dispatch_locks SET lock_state='sent', updated_at=?
               WHERE attempt_id=? AND lock_state IN ('reserved','sent')""",
            (datetime.now().isoformat(), attempt_id),
        )
        already_audited = False
        for row in conn.execute(
            """SELECT details_json FROM core_audit_logs
               WHERE event='DISPATCH_STATE_RECONCILED' AND trip_id=? AND host_id=?""",
            (expected_trip_id, host["host_id"]),
        ):
            try:
                if json.loads(row["details_json"] or "{}").get("attempt_id") == attempt_id:
                    already_audited = True
                    break
            except (TypeError, ValueError):
                continue
        if not already_audited:
            conn.execute(
                """INSERT INTO core_audit_logs(timestamp,trip_id,module,event,host_id,details_json)
                   VALUES(?,?,'AUTOPILOT','DISPATCH_STATE_RECONCILED',?,?)""",
                (datetime.now().isoformat(), expected_trip_id, host["host_id"],
                 json.dumps({
                     "attempt_id": attempt_id,
                     "history_id": supporting_history["id"],
                     "verified_audit_id": audit_row["id"],
                     "evidence_type": audit_details["evidence_type"],
                     "sent_at_preserved": evidence_timestamp,
                     "history_status_preserved": supporting_history["status"],
                 }, ensure_ascii=False)),
            )
        return {
            "status": "reconciled",
            "attempt_id": attempt_id,
            "sent_at": evidence_timestamp,
            "lock_state": "sent",
            "history_id": supporting_history["id"],
            "history_status_preserved": supporting_history["status"],
        }
