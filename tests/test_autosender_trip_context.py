"""Dispatch must use complete trip context from the canonical queue DTO."""

import unittest
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

from auto_sender import AutoSender
from core.database import get_connection, init_db
from core.queue import RequestQueueManager


class AutoSenderTripContextTests(unittest.TestCase):
    def setUp(self):
        self.sender = object.__new__(AutoSender)
        self.sender.trip_id = "trip-a"

    def test_uses_only_trip_scoped_fields_from_core_queue_dto(self):
        candidate = {
            "username": "host-a",
            "trip_id": "trip-a",
            "trip_city": "Kyoto, Japan",
            "trip_arrival": "2026-12-01",
            "trip_departure": "2026-12-04",
            # Deliberately conflicting legacy/display values must not win.
            "city": "Tokyo",
            "arrival_date": "2026-11-16",
            "departure_date": "2026-11-19",
            "check_in": "2026-11-16",
            "check_out": "2026-11-19",
        }

        with patch("core.database.get_connection", side_effect=AssertionError("sender must not query legacy tables")):
            result = self.sender.get_host_dates_and_city(candidate)

        self.assertEqual(result, ("2026-12-01", "2026-12-04", "Kyoto"))

    def test_missing_canonical_context_fails_without_default_dates_or_city(self):
        candidate = {
            "username": "host-a",
            "trip_id": "trip-a",
            "city": "Tokyo",
            "check_in": "2026-11-16",
            "check_out": "2026-11-19",
        }

        with self.assertRaisesRegex(ValueError, "core_trips.city.*arrival_date.*departure_date"):
            self.sender.get_host_dates_and_city(candidate)

    def test_preflight_reports_incomplete_candidate_before_browser_dispatch(self):
        candidates = [
            {
                "username": "complete-host",
                "trip_id": "trip-a",
                "trip_city": "Sapporo",
                "trip_arrival": "2026-12-01",
                "trip_departure": "2026-12-03",
            },
            {"username": "incomplete-host", "trip_id": "trip-a", "trip_city": "Sapporo"},
        ]

        errors = self.sender.validate_dispatch_context(candidates)

        self.assertEqual(len(errors), 1)
        self.assertIn("@incomplete-host", errors[0])
        self.assertIn("core_trips.arrival_date", errors[0])

    def test_candidate_from_another_trip_is_rejected(self):
        candidate = {
            "username": "host-b",
            "trip_id": "trip-b",
            "trip_city": "Tokyo",
            "trip_arrival": "2026-12-01",
            "trip_departure": "2026-12-03",
        }

        with self.assertRaisesRegex(ValueError, "Trip context mismatch"):
            self.sender.get_host_dates_and_city(candidate)

    def test_run_stops_before_playwright_when_candidate_context_is_incomplete(self):
        self.sender.daily_limit = 20
        self.sender.max_requests = None
        self.sender.jitter_enabled = False
        self.sender.max_interval_minutes = 5
        self.sender.min_interval_minutes = 5
        self.sender.period_minutes = None
        self.sender.interval_seconds = None
        self.sender.headless = True
        self.sender.dry_run = False
        self.sender.target_username = None
        self.sender.is_running = True
        self.sender.update_autopilot_status = Mock()
        self.sender.get_sent_today_count = Mock(return_value=0)
        self.sender.sync_queue_against_sent_and_inbox = Mock(return_value=0)
        self.sender.queue_manager = Mock()
        self.sender.queue_manager.get_ready_dispatch_candidates.return_value = [{
            "username": "incomplete-host",
            "trip_id": "trip-a",
            "status": "Approved",
            "trip_city": "Kyoto",
        }]

        with patch("auto_sender.sync_playwright") as playwright, patch("auto_sender.log_step"):
            self.sender.run()

        playwright.assert_not_called()
        self.assertFalse(self.sender.is_running)
        self.sender.update_autopilot_status.assert_any_call(
            is_running=False,
            is_paused=False,
            current_host=None,
            seconds_remaining=0,
            last_status="ERROR: неполный контекст поездки в SQLite; отправка не запускалась",
        )

    def test_queue_dispatch_dto_carries_trip_city_and_dates_from_core_trips(self):
        with tempfile.TemporaryDirectory(prefix="dispatch-trip-context-") as temp_dir:
            db_path = Path(temp_dir) / "app.db"
            init_db(db_path)
            with get_connection(db_path) as conn:
                conn.execute(
                    """INSERT INTO core_trips
                       (id,url,country,city,arrival_date,departure_date,is_active,created_at)
                       VALUES ('trip-a','https://example.test/trip-a','Japan','Kyoto',
                               '2026-12-01','2026-12-04',1,'2026-11-12')"""
                )
                conn.execute(
                    """INSERT INTO core_hosts
                       (host_id,trip_id,username,name,location,state,message_draft,qc_passed,
                        created_at,updated_at)
                       VALUES ('host-a','trip-a','host-a','Host A','Tokyo','approved',
                               'Approved message',1,'2026-11-12','2026-11-12')"""
                )

            manager = object.__new__(RequestQueueManager)
            manager.db_file = db_path
            self.sender.db_file = db_path
            with patch("core.queue.db_session", side_effect=lambda *_args, **_kwargs: get_connection(db_path)), \
                 patch("core.queue.db_get_sent_usernames", return_value=set()):
                queue_item = manager.load_queue(exclude_sent=False, trip_id="trip-a")[0]

            self.assertEqual(queue_item["trip_city"], "Kyoto")
            self.assertEqual(queue_item["trip_arrival"], "2026-12-01")
            self.assertEqual(queue_item["trip_departure"], "2026-12-04")
            self.assertEqual(self.sender.get_host_dates_and_city(queue_item), (
                "2026-12-01", "2026-12-04", "Kyoto",
            ))

    def test_pre_dispatch_rechecks_full_saved_profile_and_persists_exclusion(self):
        with tempfile.TemporaryDirectory(prefix="dispatch-eligibility-") as temp_dir:
            db_path = Path(temp_dir) / "app.db"
            init_db(db_path)
            with get_connection(db_path) as conn:
                conn.execute(
                    """INSERT INTO core_trips
                       (id,url,country,city,arrival_date,departure_date,is_active,created_at)
                       VALUES ('trip-a','https://example.test/trip-a','Japan','Kyoto',
                               '2026-11-19','2026-11-21',1,'2026-11-12')"""
                )
                conn.execute(
                    """INSERT INTO core_hosts
                       (host_id,trip_id,username,name,clean_name,location,state,message_draft,qc_passed,
                        created_at,updated_at)
                       VALUES ('host-a','trip-a','host-a','Aiko Mori','Aiko','Kyoto','approved',
                               'Hello, I would love to stay.',1,'2026-11-12','2026-11-12')"""
                )
                conn.execute(
                    "INSERT INTO core_host_snapshots(host_id,trip_id,raw_data_json,captured_at) VALUES(?,?,?,?)",
                    ('host-a','trip-a','{"sharedInformation":"I do not accept male guests."}','2026-11-12'),
                )

            manager = object.__new__(RequestQueueManager)
            real_get_connection = get_connection
            manager.db_file = db_path
            with patch("core.queue.db_session", side_effect=lambda *_args, **_kwargs: real_get_connection(db_path)), \
                 patch("core.queue.db_get_sent_usernames", return_value=set()), \
                 patch("core.database.get_connection", side_effect=lambda *args, **kwargs: real_get_connection(db_path)):
                candidates = manager.get_ready_dispatch_candidates(trip_id="trip-a", mutate_status=True)

            with get_connection(db_path) as conn:
                row = conn.execute(
                    "SELECT state,eligibility_reason,eligibility_evidence_json FROM core_hosts WHERE trip_id='trip-a' AND host_id='host-a'"
                ).fetchone()
            self.assertEqual(candidates, [])
            self.assertEqual(row["state"], "skipped")
            self.assertIn("gender restriction", row["eligibility_reason"].lower())
            self.assertIn("shared_information", row["eligibility_evidence_json"])


if __name__ == "__main__":
    unittest.main()
