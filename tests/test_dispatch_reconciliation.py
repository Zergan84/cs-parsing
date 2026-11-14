"""Narrow confirmed-dispatch reconciliation requires independent evidence."""

import json

import pytest

from core.database import get_connection, init_db
from core.dispatch_reconciliation import reconcile_confirmed_attempt


def _seed_confirmed_attempt(db_path, include_audit=True):
    trip = "kyoto-reconcile-trip"
    host = "81632723"
    attempt = "e147e945-5ac4-40c2-84c1-22cf4e71e826"
    stamp = "2026-11-13T22:19:57.624704"
    with get_connection(db_path) as conn:
        conn.execute(
            """INSERT INTO core_trips(id,url,city,arrival_date,departure_date,is_active,created_at)
               VALUES(?, 'https://example.test/kyoto','Kyoto','2026-11-19','2026-11-21',1,'now')""", (trip,)
        )
        conn.execute(
            """INSERT INTO core_hosts(host_id,trip_id,username,name,location,state,sent_at,created_at,updated_at)
               VALUES(?,?,'81632723','Host','Kyoto','sent',?,'created','updated')""", (host, trip, stamp)
        )
        conn.execute(
            """INSERT INTO sending_attempts(id,trip_id,original_trip_id,host_id,username,host_id_norm,
               username_norm,state,send_intent_committed,send_initiated,evidence_type,created_at,updated_at,
               dispatch_outcome) VALUES(?,?,?,?,?,?,?,'sent',0,0,'none','created',?,'CONFIRMED_SENT')""",
            (attempt, trip, trip, host, host, host, host, stamp),
        )
        conn.execute(
            """INSERT INTO host_dispatch_locks(lock_id,canonical_key,attempt_id,lock_state,trip_id,
               original_trip_id,created_at,updated_at) VALUES('lock-1','un:81632723',?,'reserved',?,?,'created','created')""",
            (attempt, trip, trip),
        )
        conn.execute(
            """INSERT INTO sending_history(host_id,username,status,http_status,mode,notes,timestamp,attempt_id,
               trip_id,event_type,state_at_event,evidence_type)
               VALUES(?,'81632723','SENT_SUCCESS',200,'live','confirmed',?, ?,?,'SENT','sent','view_stay_request_button')""",
            (host, stamp, attempt, trip),
        )
        if include_audit:
            conn.execute(
                """INSERT INTO core_audit_logs(timestamp,trip_id,module,event,host_id,details_json)
                   VALUES(? ,?,'AUTOPILOT','REQUEST_SENT_VERIFIED',?,?)""",
                (stamp, trip, host, json.dumps({"attempt_id": attempt, "evidence_type": "view_stay_request_button", "http_status": 200})),
            )
    return attempt, host, trip, stamp


def test_reconciliation_requires_independent_success_audit(tmp_path):
    db = tmp_path / "reconcile.sqlite"
    init_db(db)
    attempt, host, trip, _ = _seed_confirmed_attempt(db, include_audit=False)
    with pytest.raises(ValueError, match="REQUEST_SENT_VERIFIED"):
        reconcile_confirmed_attempt(attempt, host, trip, db)


def test_reconciliation_is_scoped_idempotent_and_preserves_evidence_timestamps(tmp_path):
    db = tmp_path / "reconcile.sqlite"
    init_db(db)
    attempt, host, trip, stamp = _seed_confirmed_attempt(db)
    first = reconcile_confirmed_attempt(attempt, host, trip, db)
    second = reconcile_confirmed_attempt(attempt, host, trip, db)
    assert first["status"] == "reconciled"
    assert second["status"] == "already_consistent"
    with get_connection(db) as conn:
        row = conn.execute("SELECT * FROM sending_attempts WHERE id=?", (attempt,)).fetchone()
        lock = conn.execute("SELECT * FROM host_dispatch_locks WHERE attempt_id=?", (attempt,)).fetchone()
        audits = conn.execute("SELECT COUNT(*) FROM core_audit_logs WHERE event='DISPATCH_STATE_RECONCILED'").fetchone()[0]
    assert row["created_at"] == "created"
    assert row["updated_at"] == stamp
    assert row["sent_at"] == stamp
    assert row["state"] == "sent"
    assert row["dispatch_outcome"] == "CONFIRMED_SENT"
    assert row["send_intent_committed"] == 1
    assert row["send_initiated"] == 1
    assert row["evidence_type"] == "confirmed_acceptance"
    assert lock["lock_state"] == "sent"
    assert audits == 1


def test_reconciliation_rejects_another_host_or_trip(tmp_path):
    db = tmp_path / "reconcile.sqlite"
    init_db(db)
    attempt, host, trip, _ = _seed_confirmed_attempt(db)
    with pytest.raises(ValueError, match="identity"):
        reconcile_confirmed_attempt(attempt, "other-host", trip, db)
    with pytest.raises(ValueError, match="identity"):
        reconcile_confirmed_attempt(attempt, host, "other-trip", db)


def test_reconciliation_uses_reserved_history_plus_exact_verified_audit_and_host_time(tmp_path):
    db = tmp_path / "reconcile-reserved.sqlite"
    init_db(db)
    attempt, host, trip, stamp = _seed_confirmed_attempt(db)
    with get_connection(db) as conn:
        # Legacy sender stored only the reservation, while host and audit prove
        # the request was visibly confirmed. The profile uses a CS ID, whereas
        # the attempt/history use the username; both are tied to the same user.
        conn.execute(
            "UPDATE core_hosts SET host_id='cs-profile-id',username='host@example' WHERE trip_id=? AND host_id=?",
            (trip, host),
        )
        conn.execute(
            "UPDATE sending_history SET status='RESERVED',http_status=NULL,evidence_type='none',timestamp='2026-11-13T22:19:50.000000' WHERE attempt_id=?",
            (attempt,),
        )
        conn.execute(
            "UPDATE core_audit_logs SET host_id='cs-profile-id',details_json=? WHERE event='REQUEST_SENT_VERIFIED'",
            (json.dumps({
                "attempt_id": attempt,
                "evidence_type": "view_stay_request_button",
                "http_status": 200,
                "canonical_host_key": "un:host@example",
            }),),
        )
        conn.execute(
            "UPDATE sending_attempts SET username='host@example',username_norm='host@example',host_id_norm='host@example' WHERE id=?",
            (attempt,),
        )

    result = reconcile_confirmed_attempt(attempt, host, trip, db)
    assert result["status"] == "reconciled"
    assert result["history_status_preserved"] == "RESERVED"
    with get_connection(db) as conn:
        row = conn.execute("SELECT * FROM sending_attempts WHERE id=?", (attempt,)).fetchone()
        lock = conn.execute("SELECT lock_state FROM host_dispatch_locks WHERE attempt_id=?", (attempt,)).fetchone()
        history = conn.execute("SELECT status,timestamp FROM sending_history WHERE attempt_id=?", (attempt,)).fetchone()
    assert row["sent_at"] == stamp
    assert row["updated_at"] == stamp
    assert row["evidence_type"] == "confirmed_acceptance"
    assert lock["lock_state"] == "sent"
    assert history["status"] == "RESERVED"
    assert history["timestamp"] == "2026-11-13T22:19:50.000000"
