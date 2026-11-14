"""Regression coverage for the queue's database-only source of truth."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from core.database import get_connection, init_db
from core.queue import RequestQueueManager
from core.repositories.host_queue_repository import HostQueueRepository


class QueueSingleSourceTests(unittest.TestCase):
    def test_manager_does_not_import_legacy_queue_during_initialization(self):
        db_path = Path(tempfile.mkdtemp(prefix="queue-manager-constructor-")) / "must-not-create.db"
        with patch("core.database.init_db", side_effect=AssertionError("constructor must not migrate")) as init, \
             patch("core.repositories.host_queue_repository.HostQueueRepository") as repository, \
             patch("builtins.open", side_effect=AssertionError("runtime must not read queue JSON")):
            manager = RequestQueueManager(db_path)

        repository.assert_called_once_with(db_path.resolve())
        init.assert_not_called()
        self.assertFalse(db_path.exists())
        self.assertFalse(hasattr(manager, "sync_from_json_if_db_empty"))
        self.assertFalse(hasattr(manager, "save_queue"))

    def test_database_read_failure_is_not_replaced_by_legacy_json(self):
        manager = object.__new__(RequestQueueManager)
        manager.db_file = None
        with patch("core.queue.db_session", side_effect=RuntimeError("database unavailable")), \
             patch("core.queue.logger.exception"):
            with self.assertRaisesRegex(RuntimeError, "database unavailable"):
                manager.load_queue()

    def test_queue_stats_database_failure_is_not_reported_as_an_empty_queue(self):
        manager = object.__new__(RequestQueueManager)
        manager.db_file = None
        with patch("core.queue.db_session", side_effect=RuntimeError("database unavailable")), \
             patch("core.queue.logger.exception"):
            with self.assertRaisesRegex(RuntimeError, "database unavailable"):
                manager.get_queue_stats()

    def test_sent_set_database_failure_is_not_replaced_by_legacy_files(self):
        from core.database import get_sent_usernames

        with patch("core.database.get_connection", side_effect=RuntimeError("database unavailable")), \
             patch("core.database.logger.exception"):
            with self.assertRaisesRegex(RuntimeError, "database unavailable"):
                get_sent_usernames()

    def test_queue_state_write_database_failure_is_propagated(self):
        manager = object.__new__(RequestQueueManager)
        manager.db_file = None
        manager.host_repository = Mock()
        manager.host_repository.get_host_context.return_value = None
        manager.host_repository.update_state.side_effect = RuntimeError("database unavailable")
        with patch("core.queue.get_active_trip", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "database unavailable"):
                manager.update_status("host-a", "skipped", trip_id="trip-a")

    def test_queue_export_is_a_snapshot_from_the_repository_backed_manager(self):
        from app import export_requests_queue, queue_manager

        expected = [{"username": "host-a", "status": "Approved"}]
        with patch.object(queue_manager.host_repository, "resolve_trip", return_value="trip-a"), \
             patch.object(queue_manager, "load_queue", return_value=expected) as load_queue:
            response = asyncio.run(export_requests_queue())

        self.assertEqual(json.loads(response.body), expected)
        self.assertEqual(response.media_type, "application/json")
        self.assertIn("requests_queue.json", response.headers["content-disposition"])
        load_queue.assert_called_once_with(exclude_sent=False, trip_id="trip-a")

    def test_correcting_name_invalidates_old_approved_message(self):
        with tempfile.TemporaryDirectory(prefix="queue-single-source-") as temp_dir:
            db_path = Path(temp_dir) / "queue.db"
            init_db(db_path)
            trip_id = "queue_name_trip"
            with get_connection(db_path) as conn:
                conn.execute(
                    """INSERT INTO core_trips
                       (id,url,country,city,arrival_date,departure_date,is_active,created_at)
                       VALUES (?, ?, 'Japan', 'Kyoto', '2026-12-01', '2026-12-03', 0, '2026-11-12')""",
                    (trip_id, "https://example.test/queue-name"),
                )
                conn.execute(
                    """INSERT INTO core_hosts
                       (host_id,trip_id,username,name,clean_name,location,state,message_draft,qc_passed,created_at,updated_at)
                       VALUES ('host-1',?,'user-1','Old Name','Old','Kyoto','approved','Old approved text',1,'now','now')""",
                    (trip_id,),
                )

            repository = HostQueueRepository(db_path)
            self.assertTrue(repository.update_host_name(trip_id, "user-1", "New Name", "New"))
            with get_connection(db_path) as conn:
                row = conn.execute(
                    "SELECT name,clean_name,state,message_draft,qc_passed FROM core_hosts WHERE trip_id=? AND host_id='host-1'",
                    (trip_id,),
                ).fetchone()

            self.assertEqual(row["name"], "New Name")
            self.assertEqual(row["clean_name"], "New")
            self.assertEqual(row["state"], "drafted")
            self.assertEqual(row["message_draft"], "")
            self.assertEqual(row["qc_passed"], 0)


if __name__ == "__main__":
    unittest.main()
