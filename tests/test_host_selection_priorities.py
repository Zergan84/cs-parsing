"""Regression tests for evidence-based host freshness and eligibility."""

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from core.collector import CollectorEngine, calculate_match_score
from core.database import get_connection, init_db
from core.host_activity import NOT_RECENT, RECENT, UNKNOWN, parse_activity
from core.host_policy import evaluate_host_eligibility, evaluate_hosting_exclusion
from core.autopilot import AutopilotEngine
from core.selector import SelectorEngine
from core.queue import _activity_is_fresh


class HostSelectionPriorityTests(unittest.TestCase):
    def test_missing_activity_is_unknown_and_not_fabricated_as_recent(self):
        self.assertEqual(parse_activity()["status"], UNKNOWN)
        self.assertEqual(parse_activity(is_recently_active=False)["status"], NOT_RECENT)
        self.assertEqual(parse_activity(is_recently_active=True)["status"], RECENT)
        self.assertEqual(parse_activity(raw_value="last active 2 months ago")["status"], NOT_RECENT)
        vague = parse_activity(is_recently_active=True)
        self.assertEqual(vague["days"], None)
        self.assertIsNone(vague["at"])
        self.assertFalse(_activity_is_fresh("2020-01-01T00:00:00"))
        self.assertTrue(_activity_is_fresh(datetime.now().isoformat()))
        self.assertFalse(_activity_is_fresh((datetime.now() - timedelta(days=31)).isoformat()))

    def test_explicit_recent_activity_always_gets_five_stars(self):
        score, reasons = calculate_match_score({"isRecentlyActive": True})
        self.assertEqual(score, 5)
        self.assertIn("RECENTLY ACTIVE", reasons[0])

        unknown_score, _ = calculate_match_score({"about_me": "A short profile description."})
        self.assertEqual(unknown_score, 1)

    def test_trip_specific_hosting_refusals_are_detected(self):
        reason = evaluate_hosting_exclusion(
            "I am unavailable to host in November.",
            arrival_date="2026-11-10",
            departure_date="2026-11-14",
        )
        self.assertIn("November", reason)

        gender_reason = evaluate_hosting_exclusion("I don't accept male guests.")
        self.assertIn("gender restriction", gender_reason)

    def test_full_profile_sections_are_checked_for_trip_eligibility(self):
        cases = [
            ({"aboutMe": "I am not accepting guests."}, "not accepting guests"),
            ({"sharedInformation": "I don't accept male guests."}, "gender restriction"),
            ({"houseRules": "I cannot host in November."}, "cannot host"),
            ({"whyOnCouchsurfing": "I moved to Osaka and no longer live here."}, "no longer live here"),
        ]
        for profile, expected in cases:
            with self.subTest(profile=profile):
                result = evaluate_host_eligibility(
                    profile, arrival_date="2026-11-10", departure_date="2026-11-14", target_city="Kyoto",
                )
                self.assertFalse(result["eligible"])
                self.assertIn(expected.casefold(), result["reason"].casefold())
                self.assertTrue(result["source"])
                self.assertTrue(result["excerpt"])

    def test_missing_profile_sections_remain_unverified_and_do_not_claim_acceptance(self):
        result = evaluate_host_eligibility({"aboutMe": "A short bio."})
        self.assertTrue(result["eligible"])
        self.assertEqual(result["status"], "NOT_EXCLUDED_FROM_AVAILABLE_EVIDENCE")
        self.assertIn("shared_information", result["unavailable_sections"])

    def test_collector_persists_activity_priority_and_explicit_verification(self):
        with tempfile.TemporaryDirectory(prefix="host-selection-") as temp_dir:
            db_path = Path(temp_dir) / "selection.db"
            init_db(db_path)
            trip_id = "host_selection_trip"
            with get_connection(db_path) as conn:
                conn.execute(
                    """INSERT INTO core_trips
                       (id,url,country,city,arrival_date,departure_date,is_active,created_at)
                       VALUES (?, ?, 'Japan', 'Kyoto', '2026-11-10', '2026-11-14', 0, 'now')""",
                    (trip_id, "https://example.test/host-selection"),
                )

            CollectorEngine(trip_id, db_path).ingest_raw_host({
                "id": "active-host",
                "username": "active-host",
                "name": "Host",
                "about_me": "A thoughtful profile with enough words to describe travel, film, music, and hiking interests.",
                "isRecentlyActive": True,
                "isIdentificationVerified": False,
                "arrival_date": "2026-11-10",
                "departure_date": "2026-11-14",
            })

            with get_connection(db_path) as conn:
                row = conn.execute(
                    """SELECT score_stars, priority, recently_active_status,
                              verification_status, is_verified
                       FROM core_hosts WHERE trip_id=? AND host_id='active-host'""",
                    (trip_id,),
                ).fetchone()

            self.assertEqual(row["score_stars"], 5)
            self.assertGreater(row["priority"], 10)
            self.assertEqual(row["recently_active_status"], RECENT)
            self.assertEqual(row["verification_status"], "NOT_VERIFIED")
            self.assertEqual(row["is_verified"], 0)

    def test_real_api_activity_shape_survives_collector_without_losing_exact_age(self):
        with tempfile.TemporaryDirectory(prefix="host-api-activity-") as temp_dir:
            db_path = Path(temp_dir) / "selection.db"
            init_db(db_path)
            trip_id = "real_api_activity_trip"
            with get_connection(db_path) as conn:
                conn.execute(
                    """INSERT INTO core_trips
                       (id,url,country,city,arrival_date,departure_date,is_active,created_at)
                       VALUES (?, ?, 'Japan', 'Kyoto', '2026-11-10', '2026-11-14', 0, 'now')""",
                    (trip_id, "https://example.test/real-api-activity"),
                )

            CollectorEngine(trip_id, db_path).ingest_raw_host({
                "id": "recent-api-host", "username": "recent-api-host", "displayName": "Host",
                "aboutMe": "A detailed profile about travel, hiking, music, photography, and welcoming guests.",
                "isRecentlyActive": True, "lastLogin": "2026-11-12T10:00:00Z",
                "activity_source": "couchsurfing_api", "activity_checked_at": "2026-11-13T10:00:00",
            })
            with get_connection(db_path) as conn:
                row = conn.execute(
                    """SELECT score_stars, priority, recently_active_status, last_login_days,
                              last_active_at, activity_checked_at, activity_source
                       FROM core_hosts WHERE trip_id=? AND username='recent-api-host'""",
                    (trip_id,),
                ).fetchone()
            self.assertEqual(row["score_stars"], 5)
            self.assertEqual(row["recently_active_status"], RECENT)
            self.assertEqual(row["last_login_days"], 1)
            self.assertIn("2026-11-12", row["last_active_at"])
            self.assertEqual(row["activity_checked_at"], "2026-11-13T10:00:00")
            self.assertEqual(row["activity_source"], "couchsurfing_api")

    def test_recent_host_ranks_first_over_attempt_count_in_selector_and_autopilot(self):
        with tempfile.TemporaryDirectory(prefix="activity-order-") as temp_dir:
            db_path = Path(temp_dir) / "selection.db"
            init_db(db_path)
            trip_id = "activity_order_trip"
            with get_connection(db_path) as conn:
                conn.execute(
                    """INSERT INTO core_trips
                       (id,url,country,city,arrival_date,departure_date,is_active,created_at)
                       VALUES (?, ?, 'Japan', 'Kyoto', '2026-11-10', '2026-11-14', 0, 'now')""",
                    (trip_id, "https://example.test/activity-order"),
                )
                for username, state, activity, attempts in (
                    ("active-first", "discovered", RECENT, 99),
                    ("inactive-next", "discovered", NOT_RECENT, 0),
                ):
                    conn.execute(
                        """INSERT INTO core_hosts
                           (host_id,trip_id,username,name,location,state,score_stars,priority,attempts,
                            recently_active_status,last_login_days,activity_checked_at,created_at,updated_at)
                           VALUES(?,?,?,?,? ,?,5,1000,?,?,0,'2026-11-13T10:00:00','now','now')""",
                        (username, trip_id, username, username, "Kyoto", state, attempts, activity),
                    )
            selected = SelectorEngine(trip_id, db_path).list_candidates(min_stars=1)
            message = (
                "Hello, I would love to stay with you in Kyoto. I enjoy learning how people make a home in places they "
                "know well. I work in technology and enjoy photography, films, walks, and local food. Your profile "
                "suggests you enjoy sharing stories with visitors. I am a respectful, tidy guest and happy to adapt to "
                "your schedule and house rules. Thank you for considering my request."
            )
            with get_connection(db_path) as conn:
                conn.execute("UPDATE core_hosts SET state='drafted',message_draft=? WHERE username IN ('active-first','inactive-next')", (message,))
            from core.approval_service import ApprovalService
            for username in ('active-first', 'inactive-next'):
                ApprovalService(db_path).approve_message(
                    trip_id=trip_id, host_id=username, username=username,
                    message_text=message, created_by='user:test_fixture',
                )
            dispatch = AutopilotEngine(trip_id, db_file=db_path).get_ready_candidates()
            self.assertEqual(selected[0]["username"], "active-first")
            self.assertEqual(dispatch[0]["username"], "active-first")


if __name__ == "__main__":
    unittest.main()
