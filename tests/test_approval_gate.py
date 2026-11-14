"""Security-focused tests for exact, user-approved Stay Request messages."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from core.approval_service import ApprovalService, hash_message
from core.database import get_connection, init_db
from core.queue import RequestQueueManager


class ApprovalGateTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="approval-gate-")
        self.db_path = Path(self.temp_dir.name) / "approval.db"
        init_db(self.db_path)
        self.trip = "approval-trip-a"
        self.other_trip = "approval-trip-b"
        self.host = "host-identity-1"
        self.username = "guest-host-1"
        self.message = (
            "Hello Aiko, I would love to stay with you in Kyoto. I enjoy learning how people make a home in places they "
            "know well. I work in technology and enjoy photography, films, walks, and local food. Your profile suggests "
            "you enjoy sharing stories with visitors. I am a respectful, tidy guest and happy to adapt to your schedule "
            "and house rules. Thank you for considering my request."
        )
        with get_connection(self.db_path) as conn:
            for trip in (self.trip, self.other_trip):
                conn.execute(
                    """INSERT INTO core_trips(id,url,city,arrival_date,departure_date,is_active,created_at)
                       VALUES(?,?, 'Kyoto','2026-12-01','2026-12-03',?,'now')""",
                    (trip, f"https://example.test/{trip}", int(trip == self.trip)),
                )
            conn.execute(
                """INSERT INTO core_hosts(host_id,trip_id,username,name,location,state,message_draft,
                           qc_passed,created_at,updated_at)
                   VALUES(?,?,?,'Aiko','Kyoto','drafted',?,1,'now','now')""",
                (self.host, self.trip, self.username, self.message),
            )
        self.service = ApprovalService(self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def approve(self, *, trip=None, host=None, username=None, text=None):
        return self.service.approve_message(
            trip_id=trip or self.trip, host_id=host or self.host,
            username=username or self.username, message_text=self.message if text is None else text,
            created_by="user:test_approval",
        )

    def test_hash_is_sha256_of_exact_utf8_without_normalization(self):
        self.assertEqual(hash_message("á\nHi "), hash_message("á\nHi "))
        self.assertNotEqual(hash_message("Hi!"), hash_message("Hi."))
        self.assertNotEqual(hash_message("Hi\nthere"), hash_message("Hi there"))
        self.assertNotEqual(hash_message("Hi"), hash_message("Hi "))

    def test_explicit_user_approval_binds_trip_host_version_and_exact_message(self):
        approved = self.approve()
        self.assertTrue(approved.valid)
        with get_connection(self.db_path) as conn:
            record = conn.execute("SELECT * FROM message_approvals WHERE approval_id=?", (approved.approval_id,)).fetchone()
        self.assertEqual(record["approved_message_hash"], hash_message(self.message))
        self.assertEqual(record["message_version"], 1)
        self.assertEqual(record["created_by"], "user:test_approval")
        valid = self.service.validate_approval(
            trip_id=self.trip, host_id=self.host, username=self.username, message_text=self.message, message_version=1,
        )
        self.assertEqual(valid.code, "APPROVED_VALID")
        self.assertTrue(valid.valid)

    def test_queue_approval_creates_dispatch_candidate_bound_to_reviewed_text(self):
        from core.queue import QueueEngine
        engine = QueueEngine(self.trip, db_file=self.db_path)
        self.assertTrue(engine.approve(self.host, edited_text=self.message, created_by="user:queue_card"))
        candidates = RequestQueueManager(self.db_path).get_ready_dispatch_candidates(self.trip)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["generated_message"], self.message)
        self.assertTrue(candidates[0]["approval_valid"])

    def test_v2_api_approval_route_records_the_explicit_user_action(self):
        import asyncio
        import api_v2
        from core.database import database_path_context
        with database_path_context(self.db_path):
            response = asyncio.run(api_v2.post_approve_draft(
                api_v2.ApproveDraftPayload(host_id=self.host, edited_text=self.message),
            ))
        self.assertEqual(response["state"], "approved")
        with get_connection(self.db_path) as conn:
            row = conn.execute("SELECT created_by FROM message_approvals WHERE trip_id=?", (self.trip,)).fetchone()
        self.assertEqual(row["created_by"], "user:api_v2_queue_approve")

    def test_verified_identity_alias_does_not_change_the_approved_host(self):
        self.assertTrue(self.approve().valid)
        with get_connection(self.db_path) as conn:
            canonical = conn.execute("SELECT canonical_host_key FROM message_approvals WHERE trip_id=?", (self.trip,)).fetchone()[0]
            for alias in (self.host, "host-alias-2"):
                conn.execute(
                    "INSERT INTO host_identities(identity_id,identity_type,identity_value,canonical_key,created_at) VALUES(?,?,?,?, 'now')",
                    (f"identity-{alias}", "cs_id", alias, canonical),
                )
        result = self.service.validate_approval(
            trip_id=self.trip, host_id="host-alias-2", username=self.username, message_text=self.message,
        )
        self.assertTrue(result.valid)

    def test_approval_from_one_trip_does_not_authorize_same_host_in_another_trip(self):
        self.assertTrue(self.approve().valid)
        with get_connection(self.db_path) as conn:
            conn.execute(
                """INSERT INTO core_hosts(host_id,trip_id,username,name,location,state,message_draft,qc_passed,created_at,updated_at)
                   VALUES('other-trip-host',? ,?,'Aiko','Kyoto','approved',?,1,'now','now')""",
                (self.other_trip, self.username, self.message),
            )
        result = self.service.validate_approval(
            trip_id=self.other_trip, host_id="other-trip-host", username=self.username, message_text=self.message,
        )
        self.assertIn(result.code, {"APPROVAL_NOT_FOUND", "WRONG_TRIP"})
        self.assertFalse(result.valid)

    def test_non_user_source_cannot_issue_approval(self):
        for source in ("ai:generator", "collector:search", "background_worker:regenerate"):
            result = self.service.approve_message(
                trip_id=self.trip, host_id=self.host, username=self.username,
                message_text=self.message, created_by=source,
            )
            self.assertEqual(result.code, "INVALID_APPROVAL")
            self.assertFalse(result.valid)

    def test_queued_or_approved_state_without_approval_is_not_a_dispatch_candidate(self):
        with get_connection(self.db_path) as conn:
            conn.execute("UPDATE core_hosts SET state='approved' WHERE trip_id=? AND host_id=?", (self.trip, self.host))
        manager = RequestQueueManager(self.db_path)
        self.assertEqual(manager.get_ready_dispatch_candidates(self.trip), [])
        from core.autopilot import AutopilotEngine
        self.assertEqual(AutopilotEngine(self.trip, db_file=self.db_path).get_ready_candidates(), [])

    def test_message_edit_invalidates_approval_and_increments_version(self):
        self.assertTrue(self.approve().valid)
        changed = self.message.replace("Kyoto", "Osaka")
        with get_connection(self.db_path) as conn:
            conn.execute("UPDATE core_hosts SET message_draft=? WHERE trip_id=? AND host_id=?", (changed, self.trip, self.host))
        result = self.service.validate_approval(
            trip_id=self.trip, host_id=self.host, username=self.username,
            message_text=changed, message_version=2,
        )
        self.assertEqual(result.code, "MESSAGE_VERSION_MISMATCH")
        self.assertFalse(result.valid)

    def test_punctuation_and_whitespace_mutations_fail_exact_hash_validation(self):
        self.assertTrue(self.approve().valid)
        for mutated in (self.message.replace(",", "."), self.message + " ", self.message.replace("I enjoy", "I enjoy\n")):
            result = self.service.validate_approval(
                trip_id=self.trip, host_id=self.host, username=self.username, message_text=mutated,
            )
            self.assertEqual(result.code, "MESSAGE_CHANGED")
            self.assertFalse(result.valid)

    def test_version_change_revocation_and_trip_or_host_mismatch_are_blocked(self):
        approval = self.approve()
        wrong_trip = self.service.validate_approval(
            trip_id=self.other_trip, host_id=self.host, username=self.username, message_text=self.message,
        )
        self.assertFalse(wrong_trip.valid)
        wrong_host = self.service.validate_approval(
            trip_id=self.trip, host_id="someone-else", username="someone-else", message_text=self.message,
        )
        self.assertFalse(wrong_host.valid)
        revoked = self.service.revoke_approval(trip_id=self.trip, host_id=self.host)
        self.assertEqual(revoked.code, "APPROVAL_REVOKED")
        result = self.service.validate_approval(
            trip_id=self.trip, host_id=self.host, username=self.username, message_text=self.message,
        )
        self.assertEqual(result.code, "APPROVAL_REVOKED")
        self.assertNotEqual(approval.approval_id, revoked.approval_id if revoked.valid else None)

    def test_binding_columns_are_immutable_and_migration_is_registered(self):
        approval = self.approve()
        with get_connection(self.db_path) as conn:
            migration = conn.execute("SELECT 1 FROM schema_migrations WHERE migration_id='006_immutable_message_approvals'").fetchone()
            self.assertIsNotNone(migration)
            with self.assertRaisesRegex(Exception, "binding is immutable"):
                conn.execute("UPDATE message_approvals SET approved_message_hash='forged' WHERE approval_id=?", (approval.approval_id,))

    def test_direct_autosender_call_without_approval_is_blocked_before_browser_use(self):
        with get_connection(self.db_path) as conn:
            conn.execute("UPDATE core_hosts SET state='approved' WHERE trip_id=? AND host_id=?", (self.trip, self.host))
        from auto_sender import AutoSender
        sender = AutoSender(trip_id=self.trip, db_file=self.db_path, dry_run=True)
        page = Mock()
        item = {
            "id": self.host, "trip_id": self.trip, "username": self.username,
            "generated_message": self.message, "message_version": 1,
            "status": "Approved", "trip_city": "Kyoto",
            "trip_arrival": "2026-12-01", "trip_departure": "2026-12-03",
        }
        self.assertFalse(sender.process_host(page, item))
        page.goto.assert_not_called()

    def test_candidate_mutated_after_approval_is_not_returned(self):
        self.assertTrue(self.approve().valid)
        with get_connection(self.db_path) as conn:
            conn.execute("UPDATE core_hosts SET state='approved', message_draft=? WHERE trip_id=? AND host_id=?", (self.message + "!", self.trip, self.host))
        self.assertEqual(RequestQueueManager(self.db_path).get_ready_dispatch_candidates(self.trip), [])

    def test_candidate_text_changed_before_direct_dispatch_is_blocked(self):
        self.assertTrue(self.approve().valid)
        with get_connection(self.db_path) as conn:
            conn.execute("UPDATE core_hosts SET state='approved',message_draft=? WHERE trip_id=? AND host_id=?", (self.message + "!", self.trip, self.host))
        from auto_sender import AutoSender
        sender = AutoSender(trip_id=self.trip, db_file=self.db_path, dry_run=True)
        page = Mock()
        item = {
            "id": self.host, "trip_id": self.trip, "username": self.username,
            "generated_message": self.message, "message_version": 1,
            "status": "Approved", "trip_city": "Kyoto",
            "trip_arrival": "2026-12-01", "trip_departure": "2026-12-03",
        }
        self.assertFalse(sender.process_host(page, item))
        page.goto.assert_not_called()

    def test_repository_cannot_directly_write_approved_state(self):
        from core.repositories.host_queue_repository import HostQueueRepository
        self.assertFalse(HostQueueRepository(self.db_path).update_state(self.trip, self.host, "approved"))
        with get_connection(self.db_path) as conn:
            self.assertEqual(conn.execute("SELECT state FROM core_hosts WHERE trip_id=? AND host_id=?", (self.trip, self.host)).fetchone()[0], "drafted")


if __name__ == "__main__":
    unittest.main()
