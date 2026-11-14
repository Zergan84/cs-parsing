"""User approvals bound to an exact, trip-scoped message revision."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from core.database import get_connection
from core.dispatch_identity import normalize_identity


@dataclass(frozen=True)
class ApprovalValidation:
    code: str
    valid: bool
    approval_id: Optional[str] = None
    canonical_host_key: Optional[str] = None
    message_hash: Optional[str] = None
    message_version: Optional[int] = None
    detail: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code, "valid": self.valid, "approval_id": self.approval_id,
            "canonical_host_key": self.canonical_host_key,
            "message_hash": self.message_hash, "message_version": self.message_version,
            "detail": self.detail,
        }


def hash_message(text: str) -> str:
    """SHA-256 over the exact UTF-8 text; no trimming or Unicode normalization."""
    if not isinstance(text, str):
        raise TypeError("Approved message must be a string")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical_host_key(conn, username: str, host_id: str) -> tuple[Optional[str], str]:
    norm_type, norm_username = normalize_identity("username", username)
    username_row = conn.execute(
        "SELECT canonical_key FROM host_identities WHERE identity_type=? AND identity_value=?",
        (norm_type, norm_username),
    ).fetchone()
    cs_row = conn.execute(
        "SELECT canonical_key FROM host_identities WHERE identity_type='cs_id' AND identity_value=?",
        (str(host_id).strip(),),
    ).fetchone()
    if username_row and cs_row and username_row["canonical_key"] != cs_row["canonical_key"]:
        return None, "Host username and host id resolve to different canonical identities"
    canonical = (username_row["canonical_key"] if username_row else None) or (
        cs_row["canonical_key"] if cs_row else None
    ) or f"un:{norm_username}"
    if not username_row:
        conn.execute(
            "INSERT INTO host_identities(identity_id,identity_type,identity_value,canonical_key,created_at) VALUES(?,?,?,?,?)",
            (str(uuid.uuid4()), "username", norm_username, canonical, datetime.now().isoformat()),
        )
    return canonical, ""


def _audit(conn, trip_id: str, host_id: str, event: str, details: dict[str, Any]) -> None:
    conn.execute(
        """INSERT INTO core_audit_logs(timestamp,trip_id,module,event,host_id,details_json)
           VALUES(?,?,'APPROVAL',?,?,?)""",
        (datetime.now().isoformat(), trip_id, event, host_id, json.dumps(details, ensure_ascii=False)),
    )


class ApprovalService:
    """The sole interpreter and writer of message approval records."""

    def __init__(self, db_file=None):
        self.db_file = db_file

    def approve_message(
        self, *, trip_id: str, host_id: str, username: str, message_text: str,
        created_by: str,
    ) -> ApprovalValidation:
        if not str(created_by).startswith("user:") or not str(created_by)[5:].strip():
            return ApprovalValidation("INVALID_APPROVAL", False, detail="Approval must originate from an explicit user action")
        if not trip_id or not host_id or not username or not isinstance(message_text, str) or not message_text:
            return ApprovalValidation("INVALID_APPROVAL", False, detail="Trip, host identity, and non-empty exact message are required")
        message_hash = hash_message(message_text)
        now = datetime.now().isoformat()
        with get_connection(self.db_file) as conn:
            rows = conn.execute(
                "SELECT * FROM core_hosts WHERE trip_id=? AND (host_id=? OR LOWER(username)=LOWER(?))",
                (trip_id, host_id, username),
            ).fetchall()
            if len(rows) != 1:
                _audit(conn, trip_id, host_id, "APPROVAL_DENIED", {"reason": "host lookup ambiguous or missing"})
                return ApprovalValidation("WRONG_HOST", False, detail="Host is missing or ambiguous in this trip")
            host = rows[0]
            if str(host["username"]).strip().lower() != str(username).strip().lower():
                _audit(conn, trip_id, host_id, "APPROVAL_DENIED", {"reason": "username mismatch"})
                return ApprovalValidation("WRONG_HOST", False, detail="Host username does not match the trip record")
            if str(host["state"]).lower() in {"sent", "sending", "uncertain"}:
                return ApprovalValidation("INVALID_APPROVAL", False, detail=f"Cannot approve host in state {host['state']}")
            canonical, identity_error = _canonical_host_key(conn, username, host["host_id"])
            if not canonical:
                _audit(conn, trip_id, host["host_id"], "APPROVAL_DENIED", {"reason": identity_error})
                return ApprovalValidation("WRONG_HOST", False, detail=identity_error)
            trip = conn.execute("SELECT city,arrival_date,departure_date FROM core_trips WHERE id=?", (trip_id,)).fetchone()
            if not trip:
                _audit(conn, trip_id, host["host_id"], "APPROVAL_DENIED", {"reason": "trip not found"})
                return ApprovalValidation("WRONG_TRIP", False, detail="Trip does not exist")
            from core.messaging import validate_message_qc
            about = host["about"] or ""
            minimal_bio = len(about.split()) < 25 or about.lower().startswith("friendly local resident")
            qc_ok, qc_issues = validate_message_qc(
                message_text, dict(trip), is_minimal_bio=minimal_bio,
                host_data={**dict(host), "about_me": about, "about_me_clean": about}, trip_city=trip["city"],
            )
            if not qc_ok:
                detail = "; ".join(qc_issues)
                _audit(conn, trip_id, host["host_id"], "APPROVAL_DENIED", {"reason": "message failed QC", "issues": qc_issues})
                return ApprovalValidation("INVALID_APPROVAL", False, canonical_host_key=canonical, message_hash=message_hash, detail=detail)
            cur = conn.execute(
                """UPDATE core_hosts SET state='approved',message_draft=?,qc_passed=1,
                          qc_notes='Approved by user',updated_at=?
                   WHERE trip_id=? AND host_id=? AND state IN ('discovered','selected','drafted','approved')""",
                (message_text, now, trip_id, host["host_id"]),
            )
            if not cur.rowcount:
                return ApprovalValidation("INVALID_APPROVAL", False, detail="Host state does not allow approval")
            version_row = conn.execute(
                "SELECT message_version FROM core_hosts WHERE trip_id=? AND host_id=?",
                (trip_id, host["host_id"]),
            ).fetchone()
            version = int(version_row["message_version"] or 1)
            message_id = f"{trip_id}:{host['host_id']}"
            conn.execute(
                """UPDATE message_approvals SET approval_status='revoked',revoked_at=?,revoke_reason='superseded by a new explicit approval'
                   WHERE trip_id=? AND message_id=? AND approval_status='approved'""",
                (now, trip_id, message_id),
            )
            approval_id = str(uuid.uuid4())
            conn.execute(
                """INSERT INTO message_approvals
                   (approval_id,canonical_host_key,trip_id,host_id,message_id,approved_message_hash,
                    message_version,approved_at,approval_status,created_by)
                   VALUES(?,?,?,?,?,?,?,?, 'approved',?)""",
                (approval_id, canonical, trip_id, host["host_id"], message_id, message_hash, version, now, created_by),
            )
            _audit(conn, trip_id, host["host_id"], "MESSAGE_APPROVED", {
                "approval_id": approval_id, "canonical_host_key": canonical,
                "message_hash": message_hash, "message_version": version, "created_by": created_by,
            })
            return ApprovalValidation("APPROVED_VALID", True, approval_id, canonical, message_hash, version)

    def validate_approval(
        self, *, trip_id: str, host_id: str, username: str, message_text: str,
        message_version: Optional[int] = None, record_failure: bool = False,
    ) -> ApprovalValidation:
        exact_hash = hash_message(message_text)
        with get_connection(self.db_file) as conn:
            host_rows = conn.execute(
                "SELECT * FROM core_hosts WHERE trip_id=? AND (host_id=? OR LOWER(username)=LOWER(?))",
                (trip_id, host_id, username),
            ).fetchall()
            if len(host_rows) != 1:
                elsewhere = conn.execute(
                    """SELECT 1 FROM message_approvals WHERE host_id=? AND trip_id<>? LIMIT 1""",
                    (host_id, trip_id),
                ).fetchone()
                same_identity_elsewhere = conn.execute(
                    "SELECT 1 FROM core_hosts WHERE LOWER(username)=LOWER(?) AND trip_id<>? LIMIT 1",
                    (username, trip_id),
                ).fetchone()
                code = "WRONG_TRIP" if elsewhere or same_identity_elsewhere else "WRONG_HOST"
                result = ApprovalValidation(code, False, detail="Host is not bound to the specified trip")
                return self._record_validation_failure(conn, result, trip_id, host_id) if record_failure else result
            host = host_rows[0]
            if str(host["username"]).strip().lower() != str(username).strip().lower():
                result = ApprovalValidation("WRONG_HOST", False, detail="Host username differs from the trip-scoped record")
                return self._record_validation_failure(conn, result, trip_id, host["host_id"]) if record_failure else result
            if str(host["host_id"]) != str(host_id):
                # Alternate IDs are accepted only when both IDs are explicitly
                # registered aliases of the same canonical identity.
                supplied = conn.execute("SELECT canonical_key FROM host_identities WHERE identity_type='cs_id' AND identity_value=?", (str(host_id),)).fetchone()
                actual = conn.execute("SELECT canonical_key FROM host_identities WHERE identity_type='cs_id' AND identity_value=?", (str(host["host_id"]),)).fetchone()
                if not supplied or not actual or supplied["canonical_key"] != actual["canonical_key"]:
                    result = ApprovalValidation("WRONG_HOST", False, detail="Host identity alias is not verified")
                    return self._record_validation_failure(conn, result, trip_id, host["host_id"]) if record_failure else result
            canonical, identity_error = _canonical_host_key(conn, username, host["host_id"])
            if not canonical:
                result = ApprovalValidation("WRONG_HOST", False, detail=identity_error)
                return self._record_validation_failure(conn, result, trip_id, host["host_id"]) if record_failure else result
            message_id = f"{trip_id}:{host['host_id']}"
            approval = conn.execute(
                "SELECT * FROM message_approvals WHERE trip_id=? AND message_id=? ORDER BY approved_at DESC LIMIT 1",
                (trip_id, message_id),
            ).fetchone()
            if not approval:
                result = ApprovalValidation("APPROVAL_NOT_FOUND", False, canonical_host_key=canonical, message_hash=exact_hash, detail="No user approval exists for this message")
            elif approval["approval_status"] == "revoked":
                result = ApprovalValidation("APPROVAL_REVOKED", False, approval["approval_id"], canonical, exact_hash, detail="Approval was revoked")
            elif approval["approval_status"] != "approved":
                result = ApprovalValidation("INVALID_APPROVAL", False, approval["approval_id"], canonical, exact_hash, detail=f"Approval status is {approval['approval_status']}")
            elif approval["canonical_host_key"] != canonical:
                result = ApprovalValidation("WRONG_HOST", False, approval["approval_id"], canonical, exact_hash, detail="Canonical identity differs from the approved host")
            elif approval["trip_id"] != trip_id:
                result = ApprovalValidation("WRONG_TRIP", False, approval["approval_id"], canonical, exact_hash, detail="Approval belongs to another trip")
            elif approval["host_id"] != host["host_id"]:
                result = ApprovalValidation("WRONG_HOST", False, approval["approval_id"], canonical, exact_hash, detail="Approval belongs to another host record")
            elif approval["message_version"] != int(host["message_version"] or 1) or (message_version is not None and int(message_version) != int(host["message_version"] or 1)):
                result = ApprovalValidation("MESSAGE_VERSION_MISMATCH", False, approval["approval_id"], canonical, exact_hash, int(host["message_version"] or 1), "Message version changed after approval")
            elif approval["approved_message_hash"] != exact_hash or host["message_draft"] != message_text:
                result = ApprovalValidation("MESSAGE_CHANGED", False, approval["approval_id"], canonical, exact_hash, int(host["message_version"] or 1), "Exact message text differs from approved text")
            elif str(host["state"]).lower() != "approved":
                result = ApprovalValidation("INVALID_APPROVAL", False, approval["approval_id"], canonical, exact_hash, int(host["message_version"] or 1), f"Host state is {host['state']}")
            else:
                result = ApprovalValidation("APPROVED_VALID", True, approval["approval_id"], canonical, exact_hash, int(host["message_version"] or 1))
            return self._record_validation_failure(conn, result, trip_id, host["host_id"]) if record_failure and not result.valid else result

    @staticmethod
    def _record_validation_failure(conn, result: ApprovalValidation, trip_id: str, host_id: str) -> ApprovalValidation:
        _audit(conn, trip_id or "", host_id or "", "DISPATCH_BLOCKED_APPROVAL", result.as_dict())
        return result

    def revoke_approval(self, *, trip_id: str, host_id: str, reason: str = "revoked by user") -> ApprovalValidation:
        now = datetime.now().isoformat()
        with get_connection(self.db_file) as conn:
            row = conn.execute(
                "SELECT * FROM message_approvals WHERE trip_id=? AND host_id=? AND approval_status='approved' ORDER BY approved_at DESC LIMIT 1",
                (trip_id, host_id),
            ).fetchone()
            if not row:
                return ApprovalValidation("APPROVAL_NOT_FOUND", False, detail="No active approval to revoke")
            conn.execute(
                "UPDATE message_approvals SET approval_status='revoked',revoked_at=?,revoke_reason=? WHERE approval_id=?",
                (now, reason, row["approval_id"]),
            )
            _audit(conn, trip_id, host_id, "MESSAGE_APPROVAL_REVOKED", {"approval_id": row["approval_id"], "reason": reason})
            return ApprovalValidation("APPROVAL_REVOKED", False, row["approval_id"], row["canonical_host_key"], row["approved_message_hash"], row["message_version"], reason)

    def record_dispatch_authorization(
        self, *, validation: ApprovalValidation, attempt_id: str, trip_id: str, host_id: str,
    ) -> bool:
        """Persist the exact authorization snapshot immediately before the external click."""
        if not validation.valid or not validation.approval_id or not attempt_id:
            return False
        with get_connection(self.db_file) as conn:
            _audit(conn, trip_id, host_id, "DISPATCH_AUTHORIZED", {
                "approval_id": validation.approval_id,
                "attempt_id": attempt_id,
                "canonical_host_key": validation.canonical_host_key,
                "approved_message_hash": validation.message_hash,
                "message_version": validation.message_version,
            })
        return True

    def record_dispatch_denial(self, *, trip_id: str, host_id: str, code: str, detail: str) -> None:
        with get_connection(self.db_file) as conn:
            _audit(conn, trip_id or "<missing-trip>", host_id or "<missing-host>", "DISPATCH_BLOCKED_APPROVAL", {
                "code": code, "detail": detail,
            })

    def get_approval_status(self, *, trip_id: str, host_id: str, username: str, message_text: str, message_version: Optional[int] = None) -> ApprovalValidation:
        return self.validate_approval(trip_id=trip_id, host_id=host_id, username=username, message_text=message_text, message_version=message_version)

    def summarize_trip(self, trip_id: Optional[str] = None) -> dict[str, int]:
        """Count host-state approvals separately from valid exact-message permissions.

        This is a read-only domain query. Legacy ``state='approved'`` rows are not
        promoted to message approvals; only an active record for the current text,
        message version, trip, host, and canonical identity is dispatchable.
        """
        with get_connection(self.db_file) as conn:
            where = "WHERE ch.state='approved'"
            params: tuple[Any, ...] = ()
            if trip_id:
                where += " AND ch.trip_id=?"
                params = (trip_id,)
            rows = conn.execute(
                f"""SELECT ch.trip_id,ch.host_id,ch.username,ch.message_draft,ch.message_version,
                           ma.canonical_host_key,ma.approved_message_hash,ma.message_version AS approved_version,
                           ma.approval_id
                    FROM core_hosts ch
                    LEFT JOIN message_approvals ma
                      ON ma.trip_id=ch.trip_id
                     AND ma.message_id=(ch.trip_id || ':' || ch.host_id)
                     AND ma.approval_status='approved'
                    {where}""",
                params,
            ).fetchall()
            if not rows or not any(row["approval_id"] for row in rows):
                approved_host_count = len(rows)
                return {
                    "host_approved": approved_host_count,
                    "dispatch_ready": 0,
                    "approval_required": approved_host_count,
                }
            identities = {
                (row["identity_type"], row["identity_value"]): row["canonical_key"]
                for row in conn.execute(
                    "SELECT identity_type,identity_value,canonical_key FROM host_identities"
                ).fetchall()
            }

        ready = 0
        for row in rows:
            if not row["approval_id"]:
                continue
            _, normalized_username = normalize_identity("username", row["username"])
            username_key = identities.get(("username", normalized_username))
            host_key = identities.get(("cs_id", str(row["host_id"]).strip()))
            if username_key and host_key and username_key != host_key:
                continue
            canonical = username_key or host_key or f"un:{normalized_username}"
            if row["canonical_host_key"] != canonical:
                continue
            if int(row["approved_version"] or 0) != int(row["message_version"] or 1):
                continue
            if row["approved_message_hash"] != hash_message(row["message_draft"] or ""):
                continue
            ready += 1
        return {
            "host_approved": len(rows),
            "dispatch_ready": ready,
            "approval_required": len(rows) - ready,
        }

    def invalidate_stale_approvals(self, trip_id: Optional[str] = None) -> int:
        where, params = ("WHERE trip_id=? AND approval_status='approved'", (trip_id,)) if trip_id else ("WHERE approval_status='approved'", ())
        now = datetime.now().isoformat()
        changed = 0
        with get_connection(self.db_file) as conn:
            rows = conn.execute(f"SELECT * FROM message_approvals {where}", params).fetchall()
            for approval in rows:
                host = conn.execute("SELECT * FROM core_hosts WHERE trip_id=? AND host_id=?", (approval["trip_id"], approval["host_id"])).fetchone()
                stale = (not host or host["state"] != "approved" or int(host["message_version"] or 1) != approval["message_version"] or hash_message(host["message_draft"] or "") != approval["approved_message_hash"])
                if stale:
                    conn.execute("UPDATE message_approvals SET approval_status='stale',revoked_at=?,revoke_reason='bound host or message changed' WHERE approval_id=?", (now, approval["approval_id"]))
                    _audit(conn, approval["trip_id"], approval["host_id"], "MESSAGE_APPROVAL_STALE", {"approval_id": approval["approval_id"]})
                    changed += 1
        return changed
