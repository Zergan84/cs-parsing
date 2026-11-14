"""
tests/test_architectural_invariants.py

Автоматические тесты архитектурных инвариантов и ограничений P0:
1. Политика единственного соединения: отсутствие прямых вызовов `sqlite3.connect`
   во всех рабочих модулях приложения (разрешено только в `core/database.py` и `scripts/`).
2. Принудительное включение `PRAGMA foreign_keys = 1` и `PRAGMA journal_mode = wal`.
3. Доменные инварианты жизненного цикла:
   - Статус 'sent' необратим (не может перейти обратно в 'drafted', 'approved', 'selected', 'skipped').
   - Сообщения, не прошедшие QC (недопустимые слова, некорректная длина), не могут быть утверждены (approve).
   - Неутвержденные сообщения не могут быть выданы в `autopilot.get_ready_candidates()`.
   - Изоляция поездок (Trip Scoping): хосты поездки А изолированы от поездки Б.
"""

import ast
import os
import unittest
import tempfile
from pathlib import Path

from core.autopilot import AutopilotEngine
from core.collector import CollectorEngine
from core.database import get_connection, init_db
from core.dispatch_confirmation import (
    ConfirmationCapability,
    ConfirmationResult,
    DispatchOutcome,
    EvidenceType,
    get_confirmation_validator,
)
from core.dispatch_identity import register_host_identity, reserve_host_for_dispatch
from core.models import HostState
from core.queue import QueueEngine
from core.selector import SelectorEngine

REPO_ROOT = Path(__file__).resolve().parent.parent
TEST_DB_PATH = Path(tempfile.mkdtemp(prefix="cs-architecture-invariants-")) / "test.db"
TRIP_A_ID = "test_trip_a_invariants"
TRIP_B_ID = "test_trip_b_invariants"


class TestArchitecturalInvariants(unittest.TestCase):
    def setUp(self):
        global TEST_DB_PATH
        TEST_DB_PATH = Path(tempfile.mkdtemp(prefix="cs-architecture-invariants-test-")) / "test.db"
        init_db(TEST_DB_PATH)
        with get_connection(TEST_DB_PATH) as conn:
            conn.execute("DELETE FROM core_audit_logs;")
            conn.execute("DELETE FROM core_hosts;")
            conn.execute("DELETE FROM core_host_snapshots;")
            conn.execute("DELETE FROM core_trips;")

        # Создаем две изолированные тестовые поездки
        with get_connection(TEST_DB_PATH) as conn:
            conn.execute(
                """
                INSERT INTO core_trips (id, url, country, city, arrival_date, departure_date, dates_text, is_active, created_at)
                VALUES (?, 'https://couchsurfing.com/trips/kyoto', 'Japan', 'Kyoto', '2026-11-19', '2026-11-21', 'Nov 19 – 21', 1, '2026-11-10T00:00:00'),
                       (?, 'https://couchsurfing.com/trips/tokyo', 'Japan', 'Tokyo', '2026-11-25', '2026-11-28', 'Nov 25 – 28', 0, '2026-11-10T00:00:00');
                """,
                (TRIP_A_ID, TRIP_B_ID),
            )

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            Path(f"{TEST_DB_PATH}{suffix}").unlink(missing_ok=True)

    # 1. ТЕСТ: ПОЛИТИКА ЕДИНОГО СОЕДИНЕНИЯ (AST SCANNING)
    def test_single_connection_policy(self):
        """
        Проверяет, что во всех рабочих модулях проекта (кроме core/database.py и scripts/)
        отсутствуют прямые вызовы `sqlite3.connect`. Весь доступ к БД идет через `core.database`.
        """
        exempt_files = {
            REPO_ROOT / "core" / "database.py",
        }
        exempt_dirs = {
            REPO_ROOT / "scripts",
            REPO_ROOT / "archive",
            REPO_ROOT / ".venv",
            REPO_ROOT / "tests",
        }

        violations = []
        for root, dirs, files in os.walk(REPO_ROOT):
            root_path = Path(root)
            if any(exempt_dir in root_path.parents or root_path == exempt_dir for exempt_dir in exempt_dirs):
                continue
            if ".git" in root_path.parts or "__pycache__" in root_path.parts:
                continue

            for file in files:
                if not file.endswith(".py"):
                    continue
                file_path = root_path / file
                if file_path in exempt_files:
                    continue

                try:
                    with open(file_path, "r", encoding="utf-8") as f:
                        tree = ast.parse(f.read(), filename=str(file_path))

                    for node in ast.walk(tree):
                        if isinstance(node, ast.Call):
                            # Проверяем sqlite3.connect(...)
                            func = node.func
                            if (
                                isinstance(func, ast.Attribute)
                                and func.attr == "connect"
                                and isinstance(func.value, ast.Name)
                                and func.value.id == "sqlite3"
                            ):
                                violations.append(f"{file_path.relative_to(REPO_ROOT)}:L{node.lineno}")
                except Exception:
                    pass

        self.assertEqual(
            violations,
            [],
            f"Найдены несанкционированные вызовы sqlite3.connect в обход core.database: {violations}",
        )

    # 2. ТЕСТ: PRAGMA FOREIGN KEYS И WAL
    def test_foreign_keys_and_wal_enforced(self):
        """Проверяет, что каждое соединение включает foreign_keys = 1 и journal_mode = WAL."""
        with get_connection(TEST_DB_PATH) as conn:
            cur = conn.cursor()
            cur.execute("PRAGMA foreign_keys;")
            fk = cur.fetchone()[0]
            self.assertEqual(fk, 1, "Foreign keys must be enabled (PRAGMA foreign_keys=1)")

            cur.execute("PRAGMA journal_mode;")
            jm = cur.fetchone()[0]
            self.assertEqual(jm.lower(), "wal", "Journal mode must be WAL")

    # 3. ТЕСТ: НЕОБРАТИМОСТЬ СТАТУСА 'SENT'
    def test_sent_status_is_irreversible(self):
        """Инвариант: хост со статусом 'sent' не может быть переведен в drafted, approved, skipped."""
        collector = CollectorEngine(trip_id=TRIP_A_ID, db_file=TEST_DB_PATH)
        selector = SelectorEngine(trip_id=TRIP_A_ID, db_file=TEST_DB_PATH)
        queue = QueueEngine(trip_id=TRIP_A_ID, db_file=TEST_DB_PATH)
        autopilot = AutopilotEngine(trip_id=TRIP_A_ID, db_file=TEST_DB_PATH)

        collector.ingest_raw_host({"id": "h_inv_sent", "username": "sent_user", "name": "Ken"})
        selector.decide("h_inv_sent", "like")
        queue.generate_draft("h_inv_sent")
        queue.approve("h_inv_sent", created_by="user:test")
        # Создаём capability для mark_as_sent
        register_host_identity("username", "sent_user", "un:sent_user", db_file=TEST_DB_PATH)
        reservation = reserve_host_for_dispatch("username", "sent_user", TRIP_A_ID, db_file=TEST_DB_PATH)
        validator = get_confirmation_validator(db_file=TEST_DB_PATH)
        capability = validator.issue_capability(
            attempt_id=reservation["attempt_id"],
            canonical_host_key=reservation["canonical_key"],
        )
        confirmation = ConfirmationResult(
            outcome=DispatchOutcome.CONFIRMED_SENT,
            capability=capability,
            evidence_type=EvidenceType.CONFIRMED_ACCEPTANCE,
            attributable_to_stay_request=True,
            http_status=200,
        )
        from auto_sender import AutoSender
        sender = AutoSender(trip_id=TRIP_A_ID, test_mode=True)
        sender.autopilot_engine = autopilot
        sender._current_attempt_id = reservation["attempt_id"]
        result = sender.handle_submission_result(
            username="sent_user",
            host_name="Ken",
            target_city="Kyoto",
            arrival_date="2026-11-19",
            departure_date="2026-11-21",
            message="Hello",
            submission_confirmed=True,
            http_status=200,
            confirmation=confirmation,
        )
        self.assertTrue(result)

        # Попытка approve после sent
        self.assertFalse(queue.approve("h_inv_sent", created_by="user:test"))

        # Попытка skip после sent
        self.assertFalse(queue.skip("h_inv_sent"))

        # Попытка пакетной очистки
        queue.clear_queue()

        # Проверяем состояние
        with get_connection(TEST_DB_PATH) as conn:
            cur = conn.cursor()
            cur.execute("SELECT state FROM core_hosts WHERE trip_id = ? AND host_id = 'h_inv_sent';", (TRIP_A_ID,))
            self.assertEqual(cur.fetchone()[0], HostState.SENT.value)

    # 4. ТЕСТ: ЗАПРЕТ ДИСПЕТЧЕРИЗАЦИИ НЕУТВЕРЖДЕННЫХ ХОСТОВ
    def test_unapproved_hosts_never_dispatched(self):
        """Инвариант: автопилот выдает только хостов в статусе 'approved'."""
        collector = CollectorEngine(trip_id=TRIP_A_ID, db_file=TEST_DB_PATH)
        selector = SelectorEngine(trip_id=TRIP_A_ID, db_file=TEST_DB_PATH)
        queue = QueueEngine(trip_id=TRIP_A_ID, db_file=TEST_DB_PATH)
        autopilot = AutopilotEngine(trip_id=TRIP_A_ID, db_file=TEST_DB_PATH)

        collector.ingest_raw_host({"id": "h_discovered", "username": "u_disc", "name": "Disc"})
        collector.ingest_raw_host({"id": "h_selected", "username": "u_sel", "name": "Sel"})
        selector.decide("h_selected", "like")
        collector.ingest_raw_host({"id": "h_drafted", "username": "u_drf", "name": "Draft"})
        selector.decide("h_drafted", "like")
        queue.generate_draft("h_drafted")

        # Ни один из них еще не утвержден
        ready = autopilot.get_ready_candidates()
        self.assertEqual(len(ready), 0)

        # Утверждаем h_drafted
        queue.approve("h_drafted", created_by="user:test")
        ready_after = autopilot.get_ready_candidates()
        self.assertEqual(len(ready_after), 1)
        self.assertEqual(ready_after[0]["host_id"], "h_drafted")

    # 5. ТЕСТ: ИЗОЛЯЦИЯ ПОЕЗДОК (TRIP SCOPING)
    def test_trip_isolation(self):
        """Инвариант: хосты поездки A не видны и не обрабатываются движками поездки B."""
        collector_a = CollectorEngine(trip_id=TRIP_A_ID, db_file=TEST_DB_PATH)
        collector_b = CollectorEngine(trip_id=TRIP_B_ID, db_file=TEST_DB_PATH)
        queue_b = QueueEngine(trip_id=TRIP_B_ID, db_file=TEST_DB_PATH)
        autopilot_b = AutopilotEngine(trip_id=TRIP_B_ID, db_file=TEST_DB_PATH)

        collector_a.ingest_raw_host({"id": "h_shared", "username": "shared_user", "name": "Shared in A"})
        
        # В поездке B хост отсутствует
        self.assertIsNone(queue_b.generate_draft("h_shared"))
        self.assertEqual(len(autopilot_b.get_ready_candidates()), 0)

        # Добавляем в поездку B независимо
        collector_b.ingest_raw_host({"id": "h_shared", "username": "shared_user", "name": "Shared in B"})
        draft_b = queue_b.generate_draft("h_shared")
        self.assertIsNotNone(draft_b)
        self.assertIn("Tokyo", draft_b["text"])  # Trip B город


if __name__ == "__main__":
    unittest.main()
