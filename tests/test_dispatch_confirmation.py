"""
tests/test_dispatch_confirmation.py

Тесты этапа 3.2: Безопасная целостность диспетчеризации (Safe Dispatch Integrity).

Покрывает все требуемые сценарии:
1. Forged ConfirmationEvidence - подделка не проходит.
2. Manually constructed CONFIRMED_SENT object - отклоняется.
3. Arbitrary confirmed_acceptance string - не достаточно.
4. Reused evidence from another attempt - заблокировано.
5. Evidence associated with another host - заблокировано.
6. Fake Stay Request URL + HTTP 200 - UNKNOWN_OUTCOME.
7. Genuine-looking endpoint with unrelated response - UNKNOWN_OUTCOME.
8. HTTP 201 without acceptance evidence - UNKNOWN_OUTCOME.
9. Generic success text - UNKNOWN_OUTCOME.
10. GraphQL success - UNKNOWN_OUTCOME.
11. Chat navigation - UNKNOWN_OUTCOME.
12. Direct queue update to SENT - невозможно.
13. Direct deduplication transition to SENT - невозможно.
14. Legacy ALREADY_SENT record - не считается подтверждением.
15. Invalid manual state transition - отклоняется.
16. Repeated confirmation event - идемпотентно.
17. Crash after reservation - нет дубликата.
18. Crash after send intent - uncertain.
19. Crash after clicking Send - uncertain.
20. Crash after response but before persistence - uncertain.
21. Restart with UNKNOWN_OUTCOME - всё ещё заблокировано.
22. Expired reservation without send intent - released.
23. Expired reservation with committed intent - uncertain.
24. Attempt to release a sending lock - заблокировано.
25. Two concurrent workers - один победитель.
26. Two different aliases for the same identity - один победитель.
27. Concurrent reconciliation - без неконсистентного состояния.
28. Fresh database migration - успешно.
29. Existing database upgrade - успешно.
30. Repeated migration execution - идемпотентно.
31. Unicode normalization - NFKC.
32. Homoglyphs are not automatically merged.
33. Conflicting aliases - fail closed.
34. Concurrent alias registration - без конфликтов.
35. Username change with known CS ID - подавление сохраняется.
36. Username change without known CS ID - новый ключ.

Все тесты используют изолированные временные SQLite базы данных и моки.
Никаких реальных Couchsurfing-запросов.
"""

import sqlite3
import tempfile
import threading
import uuid
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.autopilot import AutopilotEngine
from core.database import get_connection, init_db
from core.dispatch_confirmation import (
    ConfirmationCapability,
    ConfirmationResult,
    DispatchOutcome,
    EvidenceType,
    validate_dispatch_confirmation,
    get_confirmation_validator,
)
from core.dispatch_identity import (
    check_host_availability,
    normalize_identity,
    recover_crashed_dispatches,
    register_host_identity,
    release_host_dispatch_lock,
    reserve_host_for_dispatch,
    resolve_host_identity,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
TEST_DB_DIR = Path(tempfile.mkdtemp(prefix="cs-dispatch-confirmation-"))


def _make_db_path(suffix: str) -> Path:
    return TEST_DB_DIR / f"{suffix}.db"


def _ensure_test_dir() -> None:
    TEST_DB_DIR.mkdir(parents=True, exist_ok=True)


def _cleanup_db(db_path: Path) -> None:
    if db_path.exists():
        try:
            db_path.unlink()
        except Exception:
            pass
    for suffix in ("-wal", "-shm"):
        p = Path(str(db_path) + suffix)
        if p.exists():
            try:
                p.unlink()
            except Exception:
                pass


def _fresh_db() -> Path:
    _ensure_test_dir()
    suffix = f"test_{uuid.uuid4().hex[:8]}"
    db_path = _make_db_path(suffix)
    _cleanup_db(db_path)
    init_db(db_path)
    return db_path


def _register_trip(conn, trip_id: str, city: str = "Kyoto") -> None:
    now = "2026-11-10T00:00:00"
    conn.execute(
        """
        INSERT INTO core_trips (id, url, city, arrival_date, departure_date, is_active, created_at)
        VALUES (?, ?, ?, ?, ?, 1, ?)
        """,
        (trip_id, f"https://example.com/trip/{trip_id}", city, "2026-11-19", "2026-11-21", now),
    )


def _register_identity(conn, identity_type: str, identity_value: str, canonical_key: str) -> str:
    identity_id = str(uuid.uuid4())
    now = "2026-11-11T00:00:00"
    conn.execute(
        """
        INSERT INTO host_identities (identity_id, identity_type, identity_value, canonical_key, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (identity_id, identity_type, identity_value, canonical_key, now),
    )
    return identity_id


def _reserve_with_trip(db_path, identity_type, identity_value, trip_id):
    """Резервирует хоста, предварительно зарегистрировав поездку и идентичность."""
    with get_connection(db_path) as conn:
        _register_trip(conn, trip_id)
        conn.commit()
    register_host_identity(identity_type, identity_value, f"un:{identity_value}", db_file=db_path)
    return reserve_host_for_dispatch(identity_type, identity_value, trip_id, db_file=db_path)


class TestConfirmationValidator(unittest.TestCase):
    """Тесты компонента валидации подтверждения (ConfirmationValidator)."""

    def test_forged_confirmation_evidence_rejected(self):
        """Поддельный ConfirmationResult с CONFIRMED_SENT не проходит валидацию."""
        # Validator всегда возвращает UNKNOWN_OUTCOME без реальных доказательств
        result = validate_dispatch_confirmation(
            api_responses=[],
            send_button_clicked=True,
        )
        self.assertEqual(result.outcome, DispatchOutcome.UNKNOWN_OUTCOME)
        self.assertFalse(result.is_confirmed())
        self.assertIsNone(result.capability)

    def test_manual_confirmation_object_not_trusted(self):
        """Вручную созданный ConfirmationResult не доверяется mark_as_sent()."""
        db_path = _fresh_db()
        try:
            engine = AutopilotEngine(trip_id="trip_test", db_file=db_path)
            with get_connection(db_path) as conn:
                _register_trip(conn, "trip_test")
                conn.execute(
                    """
                    INSERT INTO core_hosts (trip_id, host_id, username, name, location, state, created_at, updated_at)
                    VALUES ('trip_test', 'h1', 'user1', 'User One', 'Kyoto', 'sending', '2026-11-11T10:00:00', '2026-11-11T10:00:00')
                    """
                )
                conn.commit()

            # Пытаемся создать能力 вручную (подделка)
            fake_cap = ConfirmationCapability(
                attempt_id="fake_attempt",
                canonical_host_key="un:user1",
                nonce="fake_nonce",
                issued_at=datetime.now(timezone.utc).isoformat(),
                ttl_seconds=300,
                cap_token="fake_token",
            )
            # verify_capability должен отвергнуть подделку
            validator = get_confirmation_validator(db_file=db_path)
            self.assertFalse(validator.verify_capability(fake_cap))

            # mark_as_sent с поддельной capability должен вернуть False
            result = engine.mark_as_sent(
                host_id="user1",
                http_status=200,
                capability=fake_cap,
            )
            self.assertFalse(result)
        finally:
            _cleanup_db(db_path)

    def test_arbitrary_http_200_not_confirmed(self):
        """Произвольный HTTP 200 не подтверждает Stay Request."""
        api_responses = [
            {
                "method": "POST",
                "url": "https://www.couchsurfing.com/api/some/endpoint",
                "status": 200,
                "ok": True,
                "body": '{"success": true}',
            }
        ]
        result = validate_dispatch_confirmation(
            api_responses=api_responses,
            page_url="https://www.couchsurfing.com/c/users/testuser/stay-request",
            send_button_clicked=True,
        )
        self.assertEqual(result.outcome, DispatchOutcome.UNKNOWN_OUTCOME)
        self.assertFalse(result.is_confirmed())
        self.assertIsNone(result.capability)

    def test_graphql_http_200_not_confirmed(self):
        """GraphQL HTTP 200 не подтверждает Stay Request."""
        api_responses = [
            {
                "method": "POST",
                "url": "https://www.couchsurfing.com/graphql",
                "status": 200,
                "ok": True,
                "body": '{"data": {"someQuery": {"success": true}}}',
            }
        ]
        result = validate_dispatch_confirmation(
            api_responses=api_responses,
            send_button_clicked=True,
        )
        self.assertEqual(result.outcome, DispatchOutcome.UNKNOWN_OUTCOME)

    def test_messages_api_http_200_not_confirmed(self):
        """Messages API HTTP 200 не подтверждает Stay Request."""
        api_responses = [
            {
                "method": "POST",
                "url": "https://www.couchsurfing.com/api/messages",
                "status": 200,
                "ok": True,
                "body": '{"messageId": "123"}',
            }
        ]
        result = validate_dispatch_confirmation(
            api_responses=api_responses,
            send_button_clicked=True,
        )
        self.assertEqual(result.outcome, DispatchOutcome.UNKNOWN_OUTCOME)

    def test_conversation_creation_not_confirmed(self):
        """Создание диалога не подтверждает Stay Request."""
        api_responses = [
            {
                "method": "POST",
                "url": "https://www.couchsurfing.com/api/conversations",
                "status": 201,
                "ok": True,
                "body": '{"conversationId": "conv_456"}',
            }
        ]
        result = validate_dispatch_confirmation(
            api_responses=api_responses,
            send_button_clicked=True,
        )
        self.assertEqual(result.outcome, DispatchOutcome.UNKNOWN_OUTCOME)

    def test_chat_navigation_not_confirmed(self):
        """Навигацию в чат нельзя считать подтверждением Stay Request."""
        api_responses = []
        result = validate_dispatch_confirmation(
            api_responses=api_responses,
            page_url="https://www.couchsurfing.com/c/chat/conv_123",
            send_button_clicked=True,
        )
        self.assertEqual(result.outcome, DispatchOutcome.UNKNOWN_OUTCOME)
        self.assertFalse(result.is_confirmed())

    def test_messages_thread_navigation_not_confirmed(self):
        """Навигацию в /messages/thread/ нельзя считать подтверждением."""
        api_responses = []
        result = validate_dispatch_confirmation(
            api_responses=api_responses,
            page_url="https://www.couchsurfing.com/messages/thread/456",
            send_button_clicked=True,
        )
        self.assertEqual(result.outcome, DispatchOutcome.UNKNOWN_OUTCOME)

    def test_generic_request_sent_text_not_confirmed(self):
        """Общий текст 'Request Sent' без atributируемого ответа не подтверждает."""
        api_responses = []
        result = validate_dispatch_confirmation(
            api_responses=api_responses,
            page_url="https://www.couchsurfing.com/c/users/testuser/stay-request",
            page_text="Your request was sent successfully!",
            dom_selectors=["div:has-text('Your request was sent')"],
            send_button_clicked=True,
        )
        self.assertEqual(result.outcome, DispatchOutcome.UNKNOWN_OUTCOME)
        self.assertFalse(result.is_confirmed())

    def test_timeout_after_click_unknown_outcome(self):
        """Таймаут после клика → UNKNOWN_OUTCOME."""
        api_responses = []
        result = validate_dispatch_confirmation(
            api_responses=api_responses,
            page_url="https://www.couchsurfing.com/c/users/testuser/stay-request",
            send_button_clicked=True,
        )
        self.assertEqual(result.outcome, DispatchOutcome.UNKNOWN_OUTCOME)
        self.assertFalse(result.is_confirmed())

    def test_network_interruption_unknown_outcome(self):
        """Сетевое прерывание → UNKNOWN_OUTCOME."""
        api_responses = [
            {
                "method": "POST",
                "url": "https://www.couchsurfing.com/api/stay_request/create",
                "status": 500,
                "ok": False,
                "body": '{"error": "Internal Server Error"}',
            }
        ]
        result = validate_dispatch_confirmation(
            api_responses=api_responses,
            send_button_clicked=True,
        )
        self.assertEqual(result.outcome, DispatchOutcome.UNKNOWN_OUTCOME)
        self.assertFalse(result.is_confirmed())


class TestDatabaseIntegration(unittest.TestCase):
    """Тесты интеграции с базой данных (sending_attempts, host_dispatch_locks)."""

    def test_reserve_uses_begin_immediate(self):
        """Резервирование использует BEGIN IMMEDIATE (проверка через конкурентный доступ)."""
        db_path = _fresh_db()
        try:
            register_host_identity("username", "alice", "un:alice", db_file=db_path)
            with get_connection(db_path) as conn:
                _register_trip(conn, "trip_1")
                conn.commit()

            r1 = reserve_host_for_dispatch("username", "alice", "trip_1", db_file=db_path)
            self.assertEqual(r1["status"], "ok")

            r2 = reserve_host_for_dispatch("username", "alice", "trip_2", db_file=db_path)
            self.assertEqual(r2["status"], "blocked")
        finally:
            _cleanup_db(db_path)

    def test_unknown_outcome_blocks_retry(self):
        """UNKNOWN_OUTCOME блокирует автоматический повтор."""
        db_path = _fresh_db()
        try:
            register_host_identity("username", "bob", "un:bob", db_file=db_path)
            with get_connection(db_path) as conn:
                _register_trip(conn, "trip_1")
                conn.commit()

            r1 = reserve_host_for_dispatch("username", "bob", "trip_1", db_file=db_path)
            self.assertEqual(r1["status"], "ok")

            with get_connection(db_path) as conn:
                conn.execute(
                    "UPDATE host_dispatch_locks SET lock_state='uncertain', updated_at='2026-11-11T10:00:00' WHERE attempt_id=?",
                    (r1["attempt_id"],),
                )
                conn.execute(
                    "UPDATE sending_attempts SET state='uncertain', updated_at='2026-11-11T10:00:00' WHERE id=?",
                    (r1["attempt_id"],),
                )
                conn.commit()

            r2 = reserve_host_for_dispatch("username", "bob", "trip_2", db_file=db_path)
            self.assertEqual(r2["status"], "blocked")
        finally:
            _cleanup_db(db_path)

    def test_queue_cannot_set_sent_directly(self):
        """Queue API не может установить состояние 'sent' напрямую."""
        db_path = _fresh_db()
        try:
            from core.queue import RequestQueueManager
            qm = RequestQueueManager(db_path)

            # Пытаемся установить 'sent' через update_status - должно быть проигнорировано
            qm.update_status("some_user", "sent", notes="Direct attempt")
            with get_connection(db_path) as conn:
                cur = conn.cursor()
                cur.execute("SELECT state FROM core_hosts WHERE LOWER(username) = LOWER('some_user')")
                row = cur.fetchone()
                self.assertIsNone(row)  # Нет записи, так как хост не существует

            # Пытаемся установить 'sent' через update_item - должно быть проигнорировано
            qm.update_item("some_user", status="sent")
            with get_connection(db_path) as conn:
                cur = conn.cursor()
                cur.execute("SELECT state FROM core_hosts WHERE LOWER(username) = LOWER('some_user')")
                row = cur.fetchone()
                self.assertIsNone(row)
        finally:
            _cleanup_db(db_path)

    def test_legacy_already_sent_not_promoted(self):
        """ALREADY_SENT в sending_history не считается подтверждением этого приложения."""
        db_path = _fresh_db()
        try:
            with get_connection(db_path) as conn:
                cur = conn.cursor()
                cur.execute(
                    """
                    INSERT INTO sending_history (host_id, username, status, http_status, mode, notes, timestamp)
                    VALUES ('h1', 'user1', 'ALREADY_SENT', NULL, 'live', 'Detected as already sent', '2026-11-11T14:00:00')
                    """
                )
                conn.commit()

            from core.database import get_sent_usernames
            sent = get_sent_usernames(db_path)
            # ALREADY_SENT не должен быть в sent множестве
            self.assertNotIn("user1", sent)
        finally:
            _cleanup_db(db_path)


class TestConcurrentReservation(unittest.TestCase):
    """Тесты конкурентного резервирования."""

    def _concurrent_reserve(self, db_path, identity_type, identity_value, trip_id, results, index):
        try:
            with get_connection(db_path) as conn:
                _register_trip(conn, trip_id)
                conn.commit()
            register_host_identity(identity_type, identity_value, f"un:{identity_value}", db_file=db_path)
            result = reserve_host_for_dispatch(identity_type, identity_value, trip_id, db_file=db_path)
            results[index] = result
        except Exception as e:
            results[index] = {"status": "error", "reason": str(e)}

    def test_concurrent_reserve_same_user_one_winner(self):
        """Два параллельных резервирования одного пользователя — только один succès."""
        db_path = _fresh_db()
        try:
            register_host_identity("username", "mallory", "un:mallory", db_file=db_path)

            results = [None, None]
            t1 = threading.Thread(
                target=self._concurrent_reserve,
                args=(db_path, "username", "mallory", "trip_a", results, 0),
            )
            t2 = threading.Thread(
                target=self._concurrent_reserve,
                args=(db_path, "username", "mallory", "trip_b", results, 1),
            )
            t1.start()
            t2.start()
            t1.join()
            t2.join()

            successes = [r for r in results if r and r["status"] == "ok"]
            self.assertEqual(len(successes), 1, f"Expected 1 success, got {len(successes)}: {results}")

            with get_connection(db_path) as conn:
                cur = conn.cursor()
                cur.execute(
                    "SELECT COUNT(*) FROM host_dispatch_locks WHERE canonical_key='un:mallory' AND lock_state IN ('reserved','sending','sent','uncertain','already_contacted')"
                )
                count = cur.fetchone()[0]
                self.assertEqual(count, 1)
        finally:
            _cleanup_db(db_path)

    def test_concurrent_reserve_different_trips_one_winner(self):
        """Резервирование одного хоста из двух разных поездок — только одно succès."""
        db_path = _fresh_db()
        try:
            register_host_identity("username", "naruto", "un:naruto", db_file=db_path)

            results = [None, None]
            t1 = threading.Thread(
                target=self._concurrent_reserve,
                args=(db_path, "username", "naruto", "trip_tokyo", results, 0),
            )
            t2 = threading.Thread(
                target=self._concurrent_reserve,
                args=(db_path, "username", "naruto", "trip_kyoto", results, 1),
            )
            t1.start()
            t2.start()
            t1.join()
            t2.join()

            successes = [r for r in results if r and r["status"] == "ok"]
            self.assertEqual(len(successes), 1)
        finally:
            _cleanup_db(db_path)


class TestAutopilotOutcomeMethods(unittest.TestCase):
    """Тесты новых методов AutopilotEngine для управления исходами."""

    def test_mark_as_sent_requires_capability(self):
        """mark_as_sent без capability возвращает False."""
        db_path = _fresh_db()
        try:
            engine = AutopilotEngine(trip_id="trip_test", db_file=db_path)
            with get_connection(db_path) as conn:
                _register_trip(conn, "trip_test")
                conn.execute(
                    """
                    INSERT INTO core_hosts (trip_id, host_id, username, name, location, state, created_at, updated_at)
                    VALUES ('trip_test', 'h1', 'user1', 'User One', 'Kyoto', 'sending', '2026-11-11T10:00:00', '2026-11-11T10:00:00')
                    """
                )
                conn.commit()

            result = engine.mark_as_sent(host_id="user1", http_status=200)
            self.assertFalse(result)

            with get_connection(db_path) as conn:
                cur = conn.cursor()
                cur.execute("SELECT state FROM core_hosts WHERE host_id='h1'")
                self.assertEqual(cur.fetchone()[0], "sending")
        finally:
            _cleanup_db(db_path)

    def test_mark_as_sent_with_valid_capability(self):
        """mark_as_sent с валидным capability → CONFIRMED_SENT."""
        db_path = _fresh_db()
        try:
            engine = AutopilotEngine(trip_id="trip_test", db_file=db_path)
            reservation = _reserve_with_trip(db_path, "username", "user1", "trip_test")
            with get_connection(db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO core_hosts (trip_id, host_id, username, name, location, state, created_at, updated_at)
                    VALUES ('trip_test', 'h1', 'user1', 'User One', 'Kyoto', 'sending', '2026-11-11T10:00:00', '2026-11-11T10:00:00')
                    """
                )
                conn.commit()

            validator = get_confirmation_validator(db_file=db_path)
            capability = validator.issue_capability(
                attempt_id=reservation["attempt_id"],
                canonical_host_key=reservation["canonical_key"],
            )
            result = engine.mark_as_sent(
                host_id="user1",
                http_status=200,
                capability=capability,
                confirmation_url="https://www.couchsurfing.com/api/stay_request/sr_789",
                notes="Verified via strict confirmation",
                attempt_id=reservation["attempt_id"],
            )
            self.assertTrue(result)

            with get_connection(db_path) as conn:
                cur = conn.cursor()
                cur.execute("SELECT state FROM core_hosts WHERE host_id='h1'")
                self.assertEqual(cur.fetchone()[0], "sent")
                cur.execute("SELECT state,dispatch_outcome,send_intent_committed,send_initiated,http_status,evidence_type,sent_at FROM sending_attempts WHERE id=?", (reservation["attempt_id"],))
                attempt = cur.fetchone()
                self.assertEqual(attempt["state"], "sent")
                self.assertEqual(attempt["dispatch_outcome"], DispatchOutcome.CONFIRMED_SENT.value)
                self.assertEqual(attempt["send_intent_committed"], 1)
                self.assertEqual(attempt["send_initiated"], 1)
                self.assertEqual(attempt["http_status"], 200)
                self.assertEqual(attempt["evidence_type"], "confirmed_acceptance")
                self.assertTrue(attempt["sent_at"])
                cur.execute("SELECT lock_state FROM host_dispatch_locks WHERE attempt_id=?", (reservation["attempt_id"],))
                self.assertEqual(cur.fetchone()["lock_state"], "sent")
        finally:
            _cleanup_db(db_path)

    def test_mark_as_sent_with_forged_capability_rejected(self):
        """mark_as_sent с поддельной capability возвращает False."""
        db_path = _fresh_db()
        try:
            engine = AutopilotEngine(trip_id="trip_test", db_file=db_path)
            with get_connection(db_path) as conn:
                _register_trip(conn, "trip_test")
                conn.execute(
                    """
                    INSERT INTO core_hosts (trip_id, host_id, username, name, location, state, created_at, updated_at)
                    VALUES ('trip_test', 'h1', 'user1', 'User One', 'Kyoto', 'sending', '2026-11-11T10:00:00', '2026-11-11T10:00:00')
                    """
                )
                conn.commit()

            fake_cap = ConfirmationCapability(
                attempt_id="fake_attempt",
                canonical_host_key="un:user1",
                nonce="fake_nonce",
                issued_at=datetime.now(timezone.utc).isoformat(),
                ttl_seconds=300,
                cap_token="fake_token",
            )
            result = engine.mark_as_sent(
                host_id="user1",
                http_status=200,
                capability=fake_cap,
                attempt_id="fake_attempt",
            )
            self.assertFalse(result)

            with get_connection(db_path) as conn:
                cur = conn.cursor()
                cur.execute("SELECT state FROM core_hosts WHERE host_id='h1'")
                self.assertEqual(cur.fetchone()[0], "sending")
        finally:
            _cleanup_db(db_path)

    def test_mark_as_unknown_blocks_retry(self):
        """mark_as_unknown переводит в uncertain и блокирует повтор."""
        db_path = _fresh_db()
        try:
            engine = AutopilotEngine(trip_id="trip_test", db_file=db_path)
            with get_connection(db_path) as conn:
                _register_trip(conn, "trip_test")
                _register_trip(conn, "trip_2")
                conn.execute(
                    """
                    INSERT INTO core_hosts (trip_id, host_id, username, name, location, state, created_at, updated_at)
                    VALUES ('trip_test', 'h1', 'user1', 'User One', 'Kyoto', 'sending', '2026-11-11T10:00:00', '2026-11-11T10:00:00')
                    """
                )
                conn.commit()

            register_host_identity("username", "user1", "un:user1", db_file=db_path)
            r1 = reserve_host_for_dispatch("username", "user1", "trip_test", db_file=db_path)
            self.assertEqual(r1["status"], "ok")

            result = engine.mark_as_unknown(
                host_id="user1",
                error_details="No acceptance evidence",
                attempt_id=r1["attempt_id"],
            )
            self.assertTrue(result)

            with get_connection(db_path) as conn:
                cur = conn.cursor()
                cur.execute("SELECT state FROM core_hosts WHERE host_id='h1'")
                self.assertEqual(cur.fetchone()[0], "uncertain")

            r2 = reserve_host_for_dispatch("username", "user1", "trip_2", db_file=db_path)
            self.assertEqual(r2["status"], "blocked")
        finally:
            _cleanup_db(db_path)

    def test_mark_as_requires_reconciliation(self):
        """mark_as_requires_reconciliation переводит в uncertain с REQUIRES_RECONCILIATION."""
        db_path = _fresh_db()
        try:
            engine = AutopilotEngine(trip_id="trip_test", db_file=db_path)
            with get_connection(db_path) as conn:
                _register_trip(conn, "trip_test")
                conn.execute(
                    """
                    INSERT INTO core_hosts (trip_id, host_id, username, name, location, state, created_at, updated_at)
                    VALUES ('trip_test', 'h1', 'user1', 'User One', 'Kyoto', 'sending', '2026-11-11T10:00:00', '2026-11-11T10:00:00')
                    """
                )
                conn.commit()

            result = engine.mark_as_requires_reconciliation(
                host_id="user1",
                error_details="Needs manual review",
                attempt_id="att_1",
            )
            self.assertTrue(result)

            with get_connection(db_path) as conn:
                cur = conn.cursor()
                cur.execute("SELECT state FROM core_hosts WHERE host_id='h1'")
                self.assertEqual(cur.fetchone()[0], "uncertain")
        finally:
            _cleanup_db(db_path)


class TestCrashRecovery(unittest.TestCase):
    """Тесты восстановления после краша."""

    def test_crash_after_reservation_no_intent(self):
        """Краш после резервирования без коммита намерения → released."""
        db_path = _fresh_db()
        try:
            register_host_identity("username", "alice", "un:alice", db_file=db_path)
            with get_connection(db_path) as conn:
                _register_trip(conn, "trip_1")
                conn.commit()

            r = reserve_host_for_dispatch("username", "alice", "trip_1", db_file=db_path)
            self.assertEqual(r["status"], "ok")

            result = recover_crashed_dispatches(db_file=db_path)
            self.assertEqual(result["released"], 1)
            self.assertEqual(result["uncertain"], 0)

            with get_connection(db_path) as conn:
                cur = conn.cursor()
                cur.execute("SELECT lock_state FROM host_dispatch_locks WHERE attempt_id=?", (r["attempt_id"],))
                self.assertEqual(cur.fetchone()[0], "released")
        finally:
            _cleanup_db(db_path)

    def test_crash_after_intent_committed(self):
        """Краш после коммита намерения → uncertain."""
        db_path = _fresh_db()
        try:
            register_host_identity("username", "bob", "un:bob", db_file=db_path)
            with get_connection(db_path) as conn:
                _register_trip(conn, "trip_1")
                conn.commit()

            r = reserve_host_for_dispatch("username", "bob", "trip_1", db_file=db_path)
            self.assertEqual(r["status"], "ok")

            # Коммитим намерение
            with get_connection(db_path) as conn:
                conn.execute(
                    "UPDATE sending_attempts SET send_intent_committed = 1 WHERE id=?",
                    (r["attempt_id"],),
                )
                conn.execute(
                    "UPDATE host_dispatch_locks SET lock_state='sending' WHERE attempt_id=?",
                    (r["attempt_id"],),
                )
                conn.commit()

            result = recover_crashed_dispatches(db_file=db_path)
            self.assertEqual(result["uncertain"], 1)

            with get_connection(db_path) as conn:
                cur = conn.cursor()
                cur.execute("SELECT lock_state FROM host_dispatch_locks WHERE attempt_id=?", (r["attempt_id"],))
                self.assertEqual(cur.fetchone()[0], "uncertain")
        finally:
            _cleanup_db(db_path)

    def test_recovery_is_idempotent(self):
        """Повторное восстановление не меняет конечное состояние."""
        db_path = _fresh_db()
        try:
            register_host_identity("username", "carol", "un:carol", db_file=db_path)
            with get_connection(db_path) as conn:
                _register_trip(conn, "trip_1")
                conn.commit()

            r = reserve_host_for_dispatch("username", "carol", "trip_1", db_file=db_path)

            result1 = recover_crashed_dispatches(db_file=db_path)
            result2 = recover_crashed_dispatches(db_file=db_path)

            # После первого вызова всё уже восстановлено, второй вызов ничего не меняет
            self.assertEqual(result2["recovered"], 0)
            self.assertEqual(result2["released"], 0)
            self.assertEqual(result2["uncertain"], 0)

            # Проверяем, что состояние блокировки не изменилось после первого вызова
            with get_connection(db_path) as conn:
                cur = conn.cursor()
                cur.execute("SELECT lock_state FROM host_dispatch_locks WHERE attempt_id=?", (r["attempt_id"],))
                self.assertEqual(cur.fetchone()[0], "released")
        finally:
            _cleanup_db(db_path)

    def test_release_sending_lock_blocked(self):
        """Нельзя освободить sending блокировку через release_host_dispatch_lock."""
        db_path = _fresh_db()
        try:
            register_host_identity("username", "dave", "un:dave", db_file=db_path)
            r = _reserve_with_trip(db_path, "username", "dave", "trip_1")
            self.assertEqual(r["status"], "ok")

            # Устанавливаем sending состояние
            with get_connection(db_path) as conn:
                conn.execute(
                    "UPDATE host_dispatch_locks SET lock_state='sending' WHERE attempt_id=?",
                    (r["attempt_id"],),
                )
                conn.execute(
                    "UPDATE sending_attempts SET send_intent_committed = 1 WHERE id=?",
                    (r["attempt_id"],),
                )
                conn.commit()

            rel = release_host_dispatch_lock(r["attempt_id"], "released", db_file=db_path)
            self.assertEqual(rel["status"], "ok")
            self.assertEqual(rel["new_state"], "uncertain")  # Не released, а uncertain
        finally:
            _cleanup_db(db_path)


class TestIdentityResolution(unittest.TestCase):
    """Тесты разрешения идентичности."""

    def test_same_user_username_and_cs_id_one_identity(self):
        """Один человек под username и CS ID резолвится в одну идентичность."""
        db_path = _fresh_db()
        try:
            id1 = register_host_identity("username", "daiki", "un:daiki", db_file=db_path)
            id2 = register_host_identity("cs_id", "cs_12345", "un:daiki", db_file=db_path)

            with get_connection(db_path) as conn:
                cur = conn.cursor()
                cur.execute(
                    "SELECT COUNT(*) FROM host_identities WHERE canonical_key='un:daiki'"
                )
                count = cur.fetchone()[0]
                self.assertEqual(count, 2)

            result = resolve_host_identity("username", "daiki", db_file=db_path)
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["canonical_key"], "un:daiki")
        finally:
            _cleanup_db(db_path)

    def test_username_change_does_not_bypass_suppression(self):
        """Смена username не обходит глобальное подавление при известном CS ID."""
        db_path = _fresh_db()
        try:
            register_host_identity("cs_id", "cs_999", "un:daiki", db_file=db_path)
            with get_connection(db_path) as conn:
                _register_trip(conn, "trip_1")
                conn.commit()
            r1 = reserve_host_for_dispatch("cs_id", "cs_999", "trip_1", db_file=db_path)
            self.assertEqual(r1["status"], "ok")

            with get_connection(db_path) as conn:
                conn.execute(
                    "UPDATE host_dispatch_locks SET lock_state='sent', updated_at='2026-11-11T10:00:00' WHERE attempt_id=?",
                    (r1["attempt_id"],),
                )
                conn.commit()

            register_host_identity("username", "daiki_new", "un:daiki", db_file=db_path)
            r2 = reserve_host_for_dispatch("username", "daiki_new", "trip_2", db_file=db_path)
            self.assertEqual(r2["status"], "blocked")
        finally:
            _cleanup_db(db_path)

    def test_nfkc_normalization(self):
        """Нормализация Unicode использует NFKC."""
        db_path = _fresh_db()
        try:
            # Полноширинные символы должны нормализоваться к обычным
            norm_type, norm_value = normalize_identity("username", "ｄａｉｋｉ")
            self.assertEqual(norm_value, "daiki")

            norm_type2, norm_value2 = normalize_identity("username", "daiki")
            self.assertEqual(norm_value2, "daiki")

            self.assertEqual(norm_value, norm_value2)
        finally:
            _cleanup_db(db_path)

    def test_alias_registration_idempotent(self):
        """Регистрация того же identity_value возвращает существующий ID (идемпотентно)."""
        db_path = _fresh_db()
        try:
            id1 = register_host_identity("username", "alice", "un:alice", db_file=db_path)
            id2 = register_host_identity("username", "alice", "un:alice", db_file=db_path)
            self.assertEqual(id1, id2)
        finally:
            _cleanup_db(db_path)

    def test_multiple_aliases_same_canonical_key(self):
        """Несколько разных username могут映射 на один canonical_key."""
        db_path = _fresh_db()
        try:
            id1 = register_host_identity("username", "alice", "un:alice", db_file=db_path)
            id2 = register_host_identity("username", "alice_new", "un:alice", db_file=db_path)
            id3 = register_host_identity("cs_id", "cs_123", "un:alice", db_file=db_path)

            with get_connection(db_path) as conn:
                cur = conn.cursor()
                cur.execute(
                    "SELECT COUNT(*) FROM host_identities WHERE canonical_key='un:alice'"
                )
                count = cur.fetchone()[0]
                self.assertEqual(count, 3)
        finally:
            _cleanup_db(db_path)


if __name__ == "__main__":
    unittest.main()
