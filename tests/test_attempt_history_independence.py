"""Dispatch history must not depend on the legacy ``hosts`` profile table."""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from core.autopilot import AutopilotEngine
from core.database import get_connection, init_db


class TestAttemptHistoryIndependence(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test.db"
        init_db(self.db_path)
        with get_connection(self.db_path) as conn:
            conn.execute(
                "INSERT INTO core_trips (id, url, city, arrival_date, departure_date, created_at, updated_at) "
                "VALUES ('trip-a', 'https://example.test/a', 'Kyoto', '2026-11-10', '2026-11-12', 'now', 'now')"
            )
            conn.execute(
                "INSERT INTO core_trips (id, url, city, arrival_date, departure_date, created_at, updated_at) "
                "VALUES ('trip-b', 'https://example.test/b', 'Osaka', '2026-11-20', '2026-11-22', 'now', 'now')"
            )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_history_is_written_when_legacy_hosts_table_is_absent(self):
        with get_connection(self.db_path) as conn:
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute("DROP TABLE hosts")

        history_id = AutopilotEngine("trip-a", db_file=self.db_path).record_sending_attempt(
            "host-42", "FAILED", notes="profile was removed"
        )
        self.assertGreater(history_id, 0)
        with get_connection(self.db_path) as conn:
            row = conn.execute("SELECT host_id, username, trip_id FROM sending_history WHERE id = ?", (history_id,)).fetchone()
            self.assertEqual(tuple(row), ("host-42", "host-42", "trip-a"))
            self.assertEqual(conn.execute("PRAGMA foreign_key_list(sending_history)").fetchall(), [])

    def test_host_in_another_trip_is_not_used_to_resolve_identity(self):
        with get_connection(self.db_path) as conn:
            conn.execute(
                "INSERT INTO core_hosts (host_id, trip_id, username, name, location, created_at, updated_at) "
                "VALUES ('shared-id', 'trip-a', 'alice', 'Alice', 'Kyoto', 'now', 'now')"
            )

        history_id = AutopilotEngine("trip-b", db_file=self.db_path).record_sending_attempt(
            "alice", "FAILED"
        )
        with get_connection(self.db_path) as conn:
            row = conn.execute("SELECT host_id, username, trip_id FROM sending_history WHERE id = ?", (history_id,)).fetchone()
            self.assertEqual(tuple(row), ("alice", "alice", "trip-b"))

    def test_attempt_snapshot_survives_profile_deletion_and_duplicate_result(self):
        with get_connection(self.db_path) as conn:
            conn.execute(
                "INSERT INTO sending_attempts (id, trip_id, original_trip_id, host_id, username, host_id_norm, username_norm, created_at, updated_at) "
                "VALUES ('attempt-1', 'trip-a', 'trip-a', 'host-7', 'bob', 'host-7', 'bob', 'now', 'now')"
            )
        engine = AutopilotEngine("trip-a", db_file=self.db_path)
        first = engine.record_sending_attempt("unknown-key", "FAILED", attempt_id="attempt-1")
        second = engine.record_sending_attempt("unknown-key", "FAILED", attempt_id="attempt-1")
        self.assertEqual(first, second)
        with get_connection(self.db_path) as conn:
            conn.execute("DELETE FROM core_hosts WHERE trip_id = 'trip-a'")
            rows = conn.execute("SELECT host_id, username, trip_id FROM sending_history WHERE attempt_id = 'attempt-1'").fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual(tuple(rows[0]), ("host-7", "bob", "trip-a"))

    def test_failed_history_insert_can_be_retried(self):
        with get_connection(self.db_path) as conn:
            conn.execute("CREATE TRIGGER fail_history BEFORE INSERT ON sending_history BEGIN SELECT RAISE(ABORT, 'simulated db failure'); END")
        engine = AutopilotEngine("trip-a", db_file=self.db_path)
        with self.assertRaises(sqlite3.IntegrityError):
            engine.record_sending_attempt("retry-host", "FAILED", attempt_id="attempt-retry")
        with get_connection(self.db_path) as conn:
            conn.execute("DROP TRIGGER fail_history")
        history_id = engine.record_sending_attempt("retry-host", "FAILED", attempt_id="attempt-retry")
        self.assertGreater(history_id, 0)
        with get_connection(self.db_path) as conn:
            count = conn.execute("SELECT COUNT(*) FROM sending_history WHERE attempt_id = 'attempt-retry'").fetchone()[0]
            self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()
