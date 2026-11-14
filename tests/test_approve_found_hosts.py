"""Integration coverage for the trip-scoped ApproveFoundHosts use case."""

import tempfile
import unittest
from pathlib import Path

from core.application.approve_found_hosts import ApproveFoundHosts
from core.database import get_connection, init_db
from core.repositories.host_queue_repository import HostQueueRepository


class ApproveFoundHostsIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="approve-found-hosts-")
        self.db_path = Path(self.temp_dir.name) / "app.db"
        init_db(self.db_path)
        self.trip_a = "trip_approve_a"
        self.trip_b = "trip_approve_b"
        now = "2026-11-12T00:00:00"
        with get_connection(self.db_path) as conn:
            for trip_id, city in ((self.trip_a, "Kyoto"), (self.trip_b, "Osaka")):
                conn.execute(
                    """INSERT INTO core_trips
                       (id,url,country,city,arrival_date,departure_date,is_active,created_at)
                       VALUES (?, ?, 'Japan', ?, '2026-11-19', '2026-11-21', 0, ?)""",
                    (trip_id, f"https://example.test/{trip_id}", city, now),
                )
            self._insert_host(conn, self.trip_a, "found_a", "Aiko Mori", "discovered")
            self._insert_host(conn, self.trip_a, "selected_a", "Haruto Sato", "selected")
            self._insert_host(conn, self.trip_a, "sent_a", "Mei Tanaka", "sent")
            self._insert_host(conn, self.trip_b, "found_b", "Ren Ito", "discovered")

    def tearDown(self):
        self.temp_dir.cleanup()

    @staticmethod
    def _insert_host(conn, trip_id, username, name, state):
        about = (
            "I enjoy hiking and spending time in the mountains. I often explore trails near my home, "
            "take photographs of nature, and like meeting travelers from different places. I work as "
            "a teacher and can sometimes host guests when my schedule allows. I appreciate clear "
            "communication and a calm, respectful home."
        )
        conn.execute(
            """INSERT INTO core_hosts
               (host_id,trip_id,username,name,clean_name,location,about,state,score_stars,
                score_reasons_json,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?, ?,4,'["mountain hiking"]','2026-11-12','2026-11-12')""",
            (username, trip_id, username, name, name.split()[0], "Japan", about, state),
        )

    def test_approves_only_eligible_hosts_inside_requested_trip(self):
        service = ApproveFoundHosts(HostQueueRepository(self.db_path))

        result = service.execute(self.trip_a)

        self.assertEqual(result["considered_count"], 2)
        self.assertEqual(result["approved_count"], 2)
        with get_connection(self.db_path) as conn:
            states = {
                (row["trip_id"], row["username"]): row["state"]
                for row in conn.execute("SELECT trip_id,username,state FROM core_hosts")
            }
            messages = {
                row["username"]: row["message_draft"]
                for row in conn.execute("SELECT username,message_draft FROM core_hosts")
            }
            self.assertEqual(states[(self.trip_a, "found_a")], "approved")
            self.assertEqual(states[(self.trip_a, "selected_a")], "approved")
            self.assertEqual(states[(self.trip_a, "sent_a")], "sent")
            self.assertEqual(states[(self.trip_b, "found_b")], "discovered")
            self.assertTrue(messages["found_a"])
            self.assertIn("Kyoto", messages["found_a"])
            approvals = conn.execute(
                "SELECT COUNT(*) FROM message_approvals WHERE trip_id=? AND approval_status='approved' AND created_by='user:approve_all_found'",
                (self.trip_a,),
            ).fetchone()[0]
            self.assertEqual(approvals, 2)
            audit = conn.execute(
                "SELECT COUNT(*) FROM core_audit_logs WHERE trip_id=? AND event='bulk_approve_found'",
                (self.trip_a,),
            ).fetchone()[0]
            self.assertEqual(audit, 1)

    def test_second_run_is_idempotent_and_does_not_reapprove(self):
        service = ApproveFoundHosts(HostQueueRepository(self.db_path))
        service.execute(self.trip_a)

        result = service.execute(self.trip_a)

        self.assertEqual(result["considered_count"], 0)
        self.assertEqual(result["approved_count"], 0)
        self.assertEqual(result["already_approved_count"], 2)
        with get_connection(self.db_path) as conn:
            self.assertEqual(
                conn.execute("SELECT state FROM core_hosts WHERE trip_id=? AND username='sent_a'", (self.trip_a,)).fetchone()[0],
                "sent",
            )

    def test_bulk_approval_repairs_legacy_approved_state_without_message_approval(self):
        with get_connection(self.db_path) as conn:
            conn.execute(
                "UPDATE core_hosts SET state='approved' WHERE trip_id=? AND host_id='found_a'",
                (self.trip_a,),
            )

        result = ApproveFoundHosts(HostQueueRepository(self.db_path)).execute(self.trip_a)

        self.assertEqual(result["considered_count"], 2)
        self.assertEqual(result["approved_count"], 2)
        self.assertEqual(result["failed_count"], 0)
        with get_connection(self.db_path) as conn:
            approval = conn.execute(
                "SELECT approval_status,created_by FROM message_approvals WHERE trip_id=? AND host_id='found_a'",
                (self.trip_a,),
            ).fetchone()
        self.assertEqual(approval["approval_status"], "approved")
        self.assertEqual(approval["created_by"], "user:approve_all_found")

    def test_missing_trip_is_rejected(self):
        service = ApproveFoundHosts(HostQueueRepository(self.db_path))
        with self.assertRaises(ValueError):
            service.execute("does_not_exist")

    def test_bulk_approval_checks_saved_shared_information_and_keeps_evidence(self):
        with get_connection(self.db_path) as conn:
            conn.execute(
                "INSERT INTO core_host_snapshots(host_id,trip_id,raw_data_json,captured_at) VALUES(?,?,?,?)",
                ("found_a", self.trip_a, '{"sharedInformation":"I do not accept male guests."}', "2026-11-12"),
            )
        service = ApproveFoundHosts(HostQueueRepository(self.db_path))

        result = service.execute(self.trip_a)

        with get_connection(self.db_path) as conn:
            row = conn.execute(
                "SELECT state,eligibility_reason,eligibility_evidence_json FROM core_hosts WHERE trip_id=? AND host_id='found_a'",
                (self.trip_a,),
            ).fetchone()
        self.assertEqual(row["state"], "skipped")
        self.assertIn("gender restriction", row["eligibility_reason"].lower())
        self.assertIn("shared_information", row["eligibility_evidence_json"])
        self.assertEqual(result["approved_count"], 1)


if __name__ == "__main__":
    unittest.main()
