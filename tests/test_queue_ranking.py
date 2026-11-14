"""Ranking order and explanation are deterministic and data-evidence based."""

from core.queue_ranking import rank_hosts


def test_ranking_respects_priority_hierarchy_and_exact_review_count():
    hosts = [
        {"username": "name-only", "name": "Andrey Ivanov", "verification_status": "UNKNOWN"},
        {"username": "reviews-11", "name": "Host", "verification_status": "NOT_VERIFIED",
         "profile_snapshot_json": '{"review_count":11}'},
        {"username": "reviews-101", "name": "Host", "verification_status": "NOT_VERIFIED",
         "profile_snapshot_json": '{"reviewsCount":101}'},
        {"username": "recent", "name": "Host", "verification_status": "NOT_VERIFIED",
         "recently_active_status": "RECENTLY_ACTIVE", "activity_checked_at": "2026-11-13T10:00:00"},
        {"username": "verified", "name": "Host", "verification_status": "VERIFIED"},
        {"username": "refs-not-reviews", "name": "Host", "verification_status": "NOT_VERIFIED",
         "references_count": 999},
    ]
    ranked = rank_hosts(hosts)
    assert [host["username"] for host in ranked][:5] == ["verified", "recent", "reviews-101", "reviews-11", "name-only"]
    by_name = {host["username"]: host for host in ranked}
    assert by_name["refs-not-reviews"]["review_count"] is None
    assert by_name["reviews-101"]["ranking_reasons"] == ["101 REVIEWS"]


def test_interest_language_and_name_are_separate_signals():
    ranked = rank_hosts([
        {"username": "russian-name", "name": "Андрей Иванов", "verification_status": "NOT_VERIFIED"},
        {"username": "russian-language", "name": "Host", "verification_status": "NOT_VERIFIED",
         "language_list": ["English", "Russian"]},
        {"username": "matching", "name": "Host", "verification_status": "NOT_VERIFIED",
         "interests": '["photography"]'},
        {"username": "translit", "name": "Andrey Smith", "verification_status": "NOT_VERIFIED"},
        {"username": "weak-surname", "name": "Petrov", "verification_status": "NOT_VERIFIED"},
    ], traveler_interests=["street photography"])
    by_name = {host["username"]: host for host in ranked}
    assert by_name["russian-language"]["language_match"] is True
    assert by_name["russian-name"]["russian_name_match"] is True
    assert by_name["translit"]["russian_name_match"] is True
    assert "PROBABLE TRANSLITERATION" in by_name["translit"]["ranking_reasons"][-1]
    assert "WEAK SURNAME SIGNAL" in by_name["weak-surname"]["ranking_reasons"][-1]
    assert by_name["russian-name"]["language_match"] is False
    assert by_name["matching"]["interest_match"] is True
    assert by_name["matching"]["ranking_score"] > by_name["russian-language"]["ranking_score"]


def test_stale_recent_activity_is_downgraded_and_ties_are_stable():
    hosts = [
        {"username": "b", "name": "Host", "verification_status": "UNKNOWN",
         "recently_active_status": "RECENTLY_ACTIVE", "activity_checked_at": "2020-01-01T00:00:00"},
        {"username": "a", "name": "Host", "verification_status": "UNKNOWN",
         "recently_active_status": "UNKNOWN"},
    ]
    first = rank_hosts(hosts)
    second = rank_hosts(list(reversed(hosts)))
    assert [h["username"] for h in first] == [h["username"] for h in second]
    assert first[0]["activity_status"] == "UNKNOWN"
    assert all(item["ranking_position"] == i for i, item in enumerate(first, 1))
    full = rank_hosts(hosts)
    pages = [full[:1], full[1:]]
    assert [item["username"] for page in pages for item in page] == [item["username"] for item in full]


def test_dispatch_candidate_selection_uses_rank_and_requires_exact_approval(tmp_path):
    from core.approval_service import ApprovalService
    from core.database import get_connection, init_db
    from core.queue import RequestQueueManager

    db = tmp_path / "ranked-dispatch.sqlite"
    init_db(db)
    trip = "ranked-kyoto-trip"
    message = (
        "Hello Aiko, I would love to stay with you in Kyoto. I enjoy learning how people make a home in places they "
        "know well. I work in technology and enjoy photography, films, walks, and local food. Your profile suggests "
        "you enjoy sharing stories with visitors. I am a respectful, tidy guest and happy to adapt to your schedule "
        "and house rules. Thank you for considering my request."
    )
    with get_connection(db) as conn:
        conn.execute(
            """INSERT INTO core_trips(id,url,city,arrival_date,departure_date,is_active,created_at)
               VALUES(?, 'https://example.test/trip','Kyoto','2026-12-01','2026-12-03',1,'now')""", (trip,)
        )
        for host_id, username, verified in (("host-a", "Anna Smith", 0), ("host-b", "Aiko Tanaka", 1)):
            conn.execute(
                """INSERT INTO core_hosts(host_id,trip_id,username,name,location,state,message_draft,qc_passed,
                       verification_status,is_verified,created_at,updated_at)
                   VALUES(?,?,?,?,'Kyoto','approved',?,1,?,?, 'now','now')""",
                (host_id, trip, username, username, message,
                 "VERIFIED" if verified else "NOT_VERIFIED", verified),
            )
    for host_id, username in (("host-a", "Anna Smith"), ("host-b", "Aiko Tanaka")):
        ApprovalService(db).approve_message(
            trip_id=trip, host_id=host_id, username=username,
            message_text=message, created_by="user:test",
        )
    # A globally unresolved attempt must keep an otherwise approved identity
    # out of the actual dispatch-candidate list.
    with get_connection(db) as conn:
        canonical = conn.execute(
            "SELECT canonical_key FROM host_identities WHERE identity_type='username' AND identity_value='anna smith'"
        ).fetchone()[0]
        conn.execute(
            """INSERT INTO sending_attempts(id,trip_id,original_trip_id,host_id,username,
               host_id_norm,username_norm,state,send_intent_committed,send_initiated,
               created_at,updated_at,dispatch_outcome)
               VALUES('unknown-a',?,?,?,?,?,?,'uncertain',0,0,'now','now','UNKNOWN_OUTCOME')""",
            (trip, trip, 'host-a', 'Anna Smith', 'host-a', 'anna smith'),
        )
        conn.execute(
            """INSERT INTO host_dispatch_locks(lock_id,canonical_key,attempt_id,lock_state,
               trip_id,original_trip_id,created_at,updated_at)
               VALUES('unknown-lock',?,'unknown-a','reserved',?,?,'now','now')""",
            (canonical, trip, trip),
        )

    candidates = RequestQueueManager(db).get_ready_dispatch_candidates(trip)
    assert [candidate["id"] for candidate in candidates] == ["host-b"]
    assert candidates[0]["verification_status"] == "VERIFIED"
    assert candidates[0]["ranking_position"] == 1
