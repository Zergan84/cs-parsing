from core.approval_service import hash_message
from core.database import get_connection, init_db, save_trip
from core.models import Trip
from core.repositories.host_queue_repository import HostQueueRepository
from core.runtime_status import inspect_sender_runtime


def _seed_host(db_path, trip_id, username, state, draft="", host_id=None):
    host_id = host_id or username
    with get_connection(db_path) as conn:
        conn.execute(
            """INSERT INTO core_hosts
               (host_id,trip_id,username,name,location,state,score_stars,priority,
                message_draft,message_version,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (host_id, trip_id, username, username.title(), "Kyoto", state, 4, 10,
             draft, 1, "2026-11-01T00:00:00", "2026-11-01T00:00:00"),
        )


def test_trip_host_collection_includes_all_states_and_paginates_without_mutating(tmp_path):
    db_path = tmp_path / "collection.sqlite3"
    init_db(db_path)
    trip_id = "kyoto"
    save_trip(Trip(id=trip_id, url="https://example.test/trips/kyoto", city="Kyoto",
                   arrival_date="2026-11-19", departure_date="2026-11-21"), db_path)
    other_trip = "tokyo"
    save_trip(Trip(id=other_trip, url="https://example.test/trips/tokyo", city="Tokyo",
                   arrival_date="2026-11-16", departure_date="2026-11-19", is_active=False), db_path)

    for username, state, draft in [
        ("found", "discovered", ""),
        ("drafted", "drafted", "unapproved draft"),
        ("approved", "approved", "approved message"),
        ("uncertain", "uncertain", "uncertain draft"),
        ("sent", "sent", "sent draft"),
        ("skipped", "skipped", "skipped draft"),
    ]:
        _seed_host(db_path, trip_id, username, state, draft)
    _seed_host(db_path, trip_id, "ALIAS", "skipped", "duplicate alias", "alias-a")
    _seed_host(db_path, trip_id, "alias", "skipped", "duplicate alias", "alias-b")
    _seed_host(db_path, other_trip, "othertrip", "discovered", "")
    with get_connection(db_path) as conn:
        conn.execute(
            "INSERT INTO host_identities(identity_id,identity_type,identity_value,canonical_key,created_at) VALUES(?,?,?,?,?)",
            ("identity-a", "username", "alias", "canonical:alias", "2026-11-01"),
        )
        conn.execute(
            "INSERT INTO host_identities(identity_id,identity_type,identity_value,canonical_key,created_at) VALUES(?,?,?,?,?)",
            ("identity-b", "username", "alias-a", "canonical:alias", "2026-11-01"),
        )

    repo = HostQueueRepository(db_path)
    first = repo.get_trip_hosts(trip_id, page=1, page_size=3)
    second = repo.get_trip_hosts(trip_id, page=2, page_size=3)
    third = repo.get_trip_hosts(trip_id, page=3, page_size=3)
    all_usernames = [h["username"].lower() for h in first["hosts"] + second["hosts"] + third["hosts"]]
    assert first["pagination"]["total_hosts"] == 7
    assert first["pagination"]["total_pages"] == 3
    assert len(all_usernames) == len(set(all_usernames)) == 7
    assert set(all_usernames) == {"found", "drafted", "approved", "uncertain", "sent", "skipped", "alias"}
    assert repo.get_trip_hosts(trip_id, search="found")["pagination"]["total_hosts"] == 1
    with get_connection(db_path) as conn:
        states = dict(conn.execute("SELECT username,state FROM core_hosts WHERE trip_id=?", (trip_id,)).fetchall())
        approvals = conn.execute("SELECT COUNT(*) FROM message_approvals").fetchone()[0]
    assert states["uncertain"] == "uncertain"
    assert states["sent"] == "sent"
    assert approvals == 0


def test_trip_collection_marks_only_exact_current_message_approval(tmp_path):
    db_path = tmp_path / "approval.sqlite3"
    init_db(db_path)
    trip_id = "trip"
    save_trip(Trip(id=trip_id, url="https://example.test/trips/trip", city="Kyoto",
                   arrival_date="2026-11-19", departure_date="2026-11-21"), db_path)
    draft = "exact approved draft"
    _seed_host(db_path, trip_id, "validhost", "approved", draft)
    with get_connection(db_path) as conn:
        conn.execute(
            """INSERT INTO message_approvals
               (approval_id,canonical_host_key,trip_id,host_id,message_id,approved_message_hash,
                message_version,approved_at,approval_status,created_by)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            ("approval-1", "un:validhost", trip_id, "validhost", f"{trip_id}:validhost",
             hash_message(draft), 1, "2026-11-01", "approved", "test"),
        )
    host = HostQueueRepository(db_path).get_trip_hosts(trip_id)["hosts"][0]
    assert host["approval_valid"] is True
    assert host["dispatch_state"] == "authorization_valid_other_checks_required"
    with get_connection(db_path) as conn:
        conn.execute("UPDATE core_hosts SET message_draft='changed' WHERE trip_id=?", (trip_id,))
    changed = HostQueueRepository(db_path).get_trip_hosts(trip_id)["hosts"][0]
    assert changed["approval_valid"] is False


def test_autopilot_api_keeps_visible_collection_separate_from_dispatch_candidates(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app import create_app

    db_path = tmp_path / "autopilot-api.sqlite3"
    init_db(db_path)
    trip_id = "api-trip"
    save_trip(Trip(id=trip_id, url="https://example.test/trips/api-trip", city="Kyoto",
                   arrival_date="2026-11-19", departure_date="2026-11-21"), db_path)
    save_trip(Trip(id="other-trip", url="https://example.test/trips/other-trip", city="Tokyo",
                   arrival_date="2026-11-16", departure_date="2026-11-19", is_active=False), db_path)
    _seed_host(db_path, trip_id, "visible-unapproved", "approved", "draft without authorization")
    _seed_host(db_path, "other-trip", "must-not-leak", "discovered", "")
    monkeypatch.setenv("CS_ENV", "test")
    client = TestClient(create_app(db_path))
    with client:
        page_response = client.get("/")
        host_response = client.get("/api/autopilot/hosts", params={"trip_id": trip_id, "page_size": 10})
        status_response = client.get("/api/autopilot/status", params={"trip_id": trip_id})
    assert page_response.status_code == 200
    page_html = page_response.text
    assert "TRIP HOSTS" in page_html
    assert "AUTHORIZED CANDIDATES READY FOR DISPATCH" not in page_html
    assert "fetch('/api/autopilot/hosts?'" in page_html
    assert "autopilotHostCollection.hosts" in page_html
    assert "UNKNOWN OUTCOME" in page_html and "RECONCILIATION REQUIRED" in page_html
    assert "Daily send count unavailable" in page_html
    assert host_response.status_code == 200
    assert [host["username"] for host in host_response.json()["hosts"]] == ["visible-unapproved"]
    assert status_response.status_code == 200
    assert status_response.json()["ready_candidates"] == []
    assert status_response.json()["active_trip_id"] == trip_id
    assert status_response.json()["daily_count_available"] is True
    assert status_response.json()["sent_today"] == 0


def test_daily_send_summary_reconciles_live_success_and_separates_other_outcomes(tmp_path):
    from datetime import datetime

    from core.queue import RequestQueueManager

    db_path = tmp_path / "daily-send-summary.sqlite3"
    init_db(db_path)
    trip_id = "daily-trip"
    save_trip(Trip(id=trip_id, url="https://example.test/trips/daily-trip", city="Kyoto",
                   arrival_date="2026-11-19", departure_date="2026-11-21"), db_path)
    save_trip(Trip(id="other-daily-trip", url="https://example.test/trips/other", city="Osaka",
                   arrival_date="2026-11-21", departure_date="2026-11-22", is_active=False), db_path)
    today = datetime.now().isoformat(timespec="seconds")
    with get_connection(db_path) as conn:
        conn.execute(
            """INSERT INTO core_hosts(host_id,trip_id,username,name,location,state,sent_at,created_at,updated_at)
               VALUES('live-host',?,'live-host','Live Host','Kyoto','sent',?,?,?)""",
            (trip_id, today, today, today),
        )
        conn.execute(
            """INSERT INTO core_hosts(host_id,trip_id,username,name,location,state,created_at,updated_at)
               VALUES('failed-host',?,'failed-host','Failed Host','Kyoto','skipped',?,?)""",
            (trip_id, today, today),
        )
        conn.execute(
            """INSERT INTO sending_attempts
               (id,trip_id,original_trip_id,host_id,username,host_id_norm,username_norm,state,
                dispatch_outcome,created_at,sent_at,updated_at)
               VALUES('attempt-live',?,?, 'live-host','live-host','live-host','live-host','sent',
                      'CONFIRMED_SENT',?,?,?)""",
            (trip_id, trip_id, today, today, today),
        )
        conn.execute(
            """INSERT INTO sending_history
               (host_id,username,status,http_status,mode,timestamp,attempt_id,trip_id,event_type,evidence_type)
               VALUES('live-host','live-host','SENT_SUCCESS',200,'live',?,'attempt-live',?,'SENT','view_stay_request_button')""",
            (today, trip_id),
        )
        conn.execute(
            """INSERT INTO sending_attempts
               (id,trip_id,original_trip_id,host_id,username,host_id_norm,username_norm,state,
                dispatch_outcome,created_at,updated_at)
               VALUES('attempt-unknown',?,?, 'unknown-host','unknown-host','unknown-host','unknown-host',
                      'uncertain','UNKNOWN_OUTCOME',?,?)""",
            (trip_id, trip_id, today, today),
        )
        conn.execute(
            """INSERT INTO sending_history(host_id,username,status,mode,timestamp,attempt_id,trip_id,event_type)
               VALUES('dry-host','dry-host','DRY_RUN_SUCCESS','dry_run',?,'attempt-dry',?,'SENT')""",
            (today, trip_id),
        )
        conn.execute(
            """INSERT INTO sending_history(host_id,username,status,mode,timestamp,trip_id,error_details)
               VALUES('failed-host','failed-host','FAILED','error',?,NULL,'form was not available')""",
            (today,),
        )
        conn.execute(
            """INSERT INTO core_hosts(host_id,trip_id,username,name,location,state,sent_at,created_at,updated_at)
               VALUES('other-live','other-daily-trip','other-live','Other Live','Osaka','sent',?,?,?)""",
            (today, today, today),
        )
        conn.execute(
            """INSERT INTO sending_attempts
               (id,trip_id,original_trip_id,host_id,username,host_id_norm,username_norm,state,
                dispatch_outcome,created_at,sent_at,updated_at)
               VALUES('attempt-other-live','other-daily-trip','other-daily-trip','other-live','other-live',
                      'other-live','other-live','sent','CONFIRMED_SENT',?,?,?)""",
            (today, today, today),
        )
        conn.execute(
            """INSERT INTO sending_history(host_id,username,status,http_status,mode,timestamp,attempt_id,
               trip_id,event_type,evidence_type)
               VALUES('other-live','other-live','SENT_SUCCESS',200,'live',?,'attempt-other-live',
                      'other-daily-trip','SENT','view_stay_request_button')""",
            (today,),
        )

    summary = RequestQueueManager(db_path).get_daily_send_summary(trip_id)

    assert summary["available"] is True
    assert summary["confirmed_live"] == 1
    assert summary["unknown_outcomes"] == 1
    assert summary["dry_run_successes"] == 1
    assert summary["failed_attempts"] == 1
    assert summary["timezone"] == "Asia/Tokyo"
    account_summary = RequestQueueManager(db_path).get_daily_send_summary(trip_id, account_wide=True)
    assert account_summary["confirmed_live"] == 2
    assert account_summary["available"] is True


def test_daily_send_count_is_unavailable_when_sources_disagree(tmp_path):
    from datetime import datetime

    from core.queue import RequestQueueManager

    db_path = tmp_path / "daily-send-mismatch.sqlite3"
    init_db(db_path)
    trip_id = "daily-mismatch"
    save_trip(Trip(id=trip_id, url="https://example.test/trips/daily-mismatch", city="Kyoto",
                   arrival_date="2026-11-19", departure_date="2026-11-21"), db_path)
    today = datetime.now().isoformat(timespec="seconds")
    with get_connection(db_path) as conn:
        conn.execute(
            """INSERT INTO sending_attempts
               (id,trip_id,original_trip_id,host_id,username,host_id_norm,username_norm,state,
                dispatch_outcome,created_at,updated_at)
               VALUES('unmatched-attempt',?,?, 'host','host','host','host','sent','CONFIRMED_SENT',?,?)""",
            (trip_id, trip_id, today, today),
        )

    summary = RequestQueueManager(db_path).get_daily_send_summary(trip_id)

    assert summary["available"] is False
    assert summary["confirmed_live"] is None
    assert summary["attempt_confirmed"] == 1
    assert summary["host_state_confirmed"] == 0


def test_runtime_status_ignores_stale_boolean_and_detects_pid_reuse():
    script = "/project/auto_sender.py"
    saved = {"is_running": True, "sender_pid": 123, "sender_started_at": "old", "sender_command": "old command"}
    stopped = inspect_sender_runtime(saved, snapshot_ok=True, processes=[], expected_script=script)
    assert stopped["process_status"] == "stopped"
    assert stopped["is_running"] is False
    assert stopped["stale_status_file"] is True

    command = f"/python -u {script} --trip-id kyoto"
    reused = inspect_sender_runtime(
        saved, snapshot_ok=True,
        processes=[{"pid": 123, "started_at": "new", "command": command}],
        expected_script=script,
    )
    assert reused["process_status"] == "running"
    assert reused["process_identity_verified"] is False
    assert reused["pid_reused"] is True

    unrelated_pid = inspect_sender_runtime(
        saved, snapshot_ok=True,
        processes=[{"pid": 123, "started_at": "new", "command": "/usr/bin/python unrelated.py"}],
        expected_script=script,
    )
    assert unrelated_pid["process_status"] == "stopped"
    assert unrelated_pid["pid_reused"] is True

    unknown = inspect_sender_runtime(saved, snapshot_ok=False, processes=[], expected_script=script)
    assert unknown["process_status"] == "unknown"
    assert unknown["is_running"] is False
