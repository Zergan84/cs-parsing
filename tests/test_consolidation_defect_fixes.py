"""
tests/test_consolidation_defect_fixes.py - Тесты исправления конкретных дефектов коммита bf93b84.

1. Инициализация AutoSender с trip_id и без него (без AttributeError).
2. Атомарность и идемпотентность mark_as_sent, mark_as_uncertain и mark_as_skipped в AutopilotEngine.
3. Защита от деградации статуса sent: update_queue_status() и обработчики ошибок не могут изменить state = 'sent'.
"""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from auto_sender import AutoSender
from core.autopilot import AutopilotEngine
from core.database import get_connection, init_core_tables
from core.models import HostState

TEST_TRIP_ID = "test_defect_trip_sapporo"


class TestConsolidationDefectFixes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.db_path = Path(cls.temp_dir.name) / "test_defects.db"
        init_core_tables(cls.db_path)

        with get_connection(cls.db_path) as conn:
            conn.execute("""
                INSERT INTO core_trips (id, country, city, arrival_date, departure_date, url, is_active, created_at, updated_at)
                VALUES (?, 'Japan', 'Sapporo', '2026-12-01', '2026-12-05', 'https://cs.com/trips/defects', 1, '2026-11-11', '2026-11-11');
            """, (TEST_TRIP_ID,))

    @classmethod
    def tearDownClass(cls):
        cls.temp_dir.cleanup()

    def setUp(self):
        from unittest.mock import patch
        self.patcher_log_step = patch("auto_sender.log_step")
        self.mock_log_step = self.patcher_log_step.start()

        with get_connection(self.db_path) as conn:
            conn.execute("DELETE FROM core_audit_logs;")
            conn.execute("DELETE FROM sending_history;")
            conn.execute("DELETE FROM core_hosts;")

            # Базовый тестовый хост в core_hosts (единственный источник истины)
            conn.execute("""
                INSERT INTO core_hosts (trip_id, host_id, username, name, location, state, message_draft, qc_passed, created_at, updated_at)
                VALUES (?, 'h_defect_1', 'daiki_tester', 'Daiki Tester', 'Sapporo', 'approved', 'Test message', 1, '2026-11-11', '2026-11-11');
            """, (TEST_TRIP_ID,))

    def tearDown(self):
        if hasattr(self, "patcher_log_step"):
            self.patcher_log_step.stop()

    # 1. ТЕСТ: Конструктор AutoSender с trip_id и без него
    def test_autosender_init_with_and_without_trip_id(self):
        # А) Инициализация с явно переданным trip_id
        sender_custom = AutoSender(trip_id="custom_trip_manual", test_mode=True)
        self.assertEqual(sender_custom.trip_id, "custom_trip_manual")
        self.assertIsNotNone(sender_custom.autopilot_engine)
        self.assertEqual(sender_custom.autopilot_engine.trip_id, "custom_trip_manual")

        # Б) Инициализация без trip_id (разрешается через get_active_trip)
        sender_auto = AutoSender(trip_id=None, test_mode=True)
        # Должен подхватить активную поездку из базы данных без AttributeError
        self.assertTrue(hasattr(sender_auto, "trip_id"))

    # 2. ТЕСТ: Атомарность и идемпотентность mark_as_sent
    def test_mark_as_sent_atomic_and_idempotent(self):
        engine = AutopilotEngine(trip_id=TEST_TRIP_ID, db_file=self.db_path)

        # Первая фиксация отправки
        success = engine.mark_as_sent("daiki_tester", http_status=200, notes="Verified in test")
        self.assertTrue(success)

        with get_connection(self.db_path) as conn:
            cur = conn.cursor()
            # Проверяем core_hosts
            cur.execute("SELECT state, sent_at FROM core_hosts WHERE host_id = 'h_defect_1';")
            row = cur.fetchone()
            self.assertEqual(row["state"], "sent")
            self.assertIsNotNone(row["sent_at"])

            # Проверяем sending_history
            cur.execute("SELECT COUNT(*) FROM sending_history WHERE host_id = 'h_defect_1' AND status = 'SENT_SUCCESS';")
            self.assertEqual(cur.fetchone()[0], 1)

            # Проверяем core_audit_logs
            cur.execute("SELECT COUNT(*) FROM core_audit_logs WHERE host_id = 'h_defect_1' AND event = 'REQUEST_SENT_VERIFIED';")
            self.assertEqual(cur.fetchone()[0], 1)

        # Повторная фиксация (идемпотентность): должна вернуть True и не дублировать записи
        success_repeat = engine.mark_as_sent("daiki_tester", http_status=200, notes="Repeated dispatch")
        self.assertTrue(success_repeat)

        with get_connection(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM sending_history WHERE host_id = 'h_defect_1';")
            self.assertEqual(cur.fetchone()[0], 1)  # Никаких дубликатов!
            cur.execute("SELECT COUNT(*) FROM core_audit_logs WHERE host_id = 'h_defect_1';")
            self.assertEqual(cur.fetchone()[0], 1)

    # 3. ТЕСТ: Атомарность mark_as_uncertain
    def test_mark_as_uncertain_atomic(self):
        engine = AutopilotEngine(trip_id=TEST_TRIP_ID, db_file=self.db_path)

        success = engine.mark_as_uncertain("daiki_tester", error_details="Gateway Timeout 504", http_status=504)
        self.assertTrue(success)

        with get_connection(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute("SELECT state, attempts, qc_notes FROM core_hosts WHERE host_id = 'h_defect_1';")
            row = cur.fetchone()
            self.assertEqual(row["state"], "uncertain")
            self.assertEqual(row["attempts"], 1)
            self.assertIn("504", row["qc_notes"])

            cur.execute("SELECT COUNT(*) FROM sending_history WHERE host_id = 'h_defect_1' AND status = 'UNCERTAIN';")
            self.assertEqual(cur.fetchone()[0], 1)

            cur.execute("SELECT COUNT(*) FROM core_audit_logs WHERE host_id = 'h_defect_1' AND event = 'REQUEST_SEND_UNCERTAIN';")
            self.assertEqual(cur.fetchone()[0], 1)

    # 4. ТЕСТ: Необратимость статуса sent (защита от понижения в uncertain / skipped)
    def test_sent_status_cannot_be_downgraded_by_error_or_queue_update(self):
        engine = AutopilotEngine(trip_id=TEST_TRIP_ID, db_file=self.db_path)

        # Фиксируем успешную отправку
        self.assertTrue(engine.mark_as_sent("daiki_tester", http_status=200))

        # Попытка пометить как uncertain через AutopilotEngine
        res_uncertain = engine.mark_as_uncertain("daiki_tester", error_details="Late network error")
        self.assertFalse(res_uncertain)

        # Попытка пометить как skipped через AutopilotEngine
        res_skipped = engine.mark_as_skipped("daiki_tester", reason="Host unavailable")
        self.assertFalse(res_skipped)

        # Попытка обновления через AutoSender.update_queue_status()
        from unittest.mock import patch
        sender = AutoSender(trip_id=TEST_TRIP_ID, test_mode=True)
        with patch("database.db_session", side_effect=lambda: get_connection(self.db_path)):
            sender.update_queue_status("daiki_tester", "Uncertain", notes="Simulated timeout")
            sender.update_queue_status("daiki_tester", "Skipped", notes="User skip")

        # Проверяем, что в базе core_hosts статус остался СТРОГО 'sent'
        with get_connection(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute("SELECT state FROM core_hosts WHERE host_id = 'h_defect_1';")
            self.assertEqual(cur.fetchone()[0], HostState.SENT.value)

    # 5. ТЕСТ: Подтверждённая отправка: сбой mark_as_sent() переводит в UNCERTAIN и не ставит Sent
    def test_handle_submission_result_mark_as_sent_failure_transitions_to_uncertain(self):
        from unittest.mock import MagicMock, patch
        sender = AutoSender(trip_id=TEST_TRIP_ID, test_mode=True)
        sender.autopilot_engine = MagicMock()
        sender.autopilot_engine.mark_as_sent.return_value = False  # Сбой фиксации!

        with patch.object(sender, "update_queue_status") as mock_update_queue, \
             patch.object(sender, "append_sent_log") as mock_append_log:

            result = sender.handle_submission_result(
                username="daiki_tester",
                host_name="Daiki",
                target_city="Sapporo",
                arrival_date="2026-12-01",
                departure_date="2026-12-05",
                message="Hello Daiki",
                submission_confirmed=True,
                http_status=200,
            )

            # 1. Результат отправки не должен считаться успешным
            self.assertFalse(result)

            # 2. update_queue_status(..., "Sent") НЕ должен вызываться
            sent_calls = [call for call in mock_update_queue.call_args_list if call[0][1] == "Sent"]
            self.assertEqual(len(sent_calls), 0)

            # 3. Фиксация в UNCERTAIN во избежание дублирования отправки
            sender.autopilot_engine.mark_as_uncertain.assert_called_once()
            mock_update_queue.assert_called_with("daiki_tester", "Uncertain", notes=mock_update_queue.call_args[1].get("notes") or mock_update_queue.call_args[0][2], increment_attempts=True, sync_db=False)
            mock_append_log.assert_called_once()
            self.assertEqual(mock_append_log.call_args[0][0]["status"], "UNCERTAIN")

    # 6. ТЕСТ: Отсутствие AutopilotEngine не позволяет зафиксировать Sent и переводит в UNCERTAIN
    def test_handle_submission_result_missing_autopilot_engine(self):
        from unittest.mock import patch
        sender = AutoSender(trip_id=TEST_TRIP_ID, test_mode=True)
        sender.autopilot_engine = None  # Движок отсутствует!

        with patch.object(sender, "update_queue_status") as mock_update_queue, \
             patch.object(sender, "append_sent_log") as mock_append_log:

            result = sender.handle_submission_result(
                username="daiki_tester",
                host_name="Daiki",
                target_city="Sapporo",
                arrival_date="2026-12-01",
                departure_date="2026-12-05",
                message="Hello Daiki",
                submission_confirmed=True,
                http_status=200,
            )

            self.assertFalse(result)
            # update_queue_status(..., "Sent") НЕ вызывается
            sent_calls = [call for call in mock_update_queue.call_args_list if call[0][1] == "Sent"]
            self.assertEqual(len(sent_calls), 0)

            # Перевод в Uncertain
            uncertain_calls = [call for call in mock_update_queue.call_args_list if call[0][1] == "Uncertain"]
            self.assertEqual(len(uncertain_calls), 1)
            self.assertEqual(mock_append_log.call_args[0][0]["status"], "UNCERTAIN")

    # 7. ТЕСТ: Успешная фиксация обновляет JSON-очередь с sync_db=False
    def test_handle_submission_result_success_updates_queue_with_sync_db_false(self):
        from unittest.mock import MagicMock, patch
        sender = AutoSender(trip_id=TEST_TRIP_ID, test_mode=True)
        sender.autopilot_engine = MagicMock()
        sender.autopilot_engine.mark_as_sent.return_value = True  # Успешная фиксация

        with patch.object(sender, "update_queue_status") as mock_update_queue, \
             patch.object(sender, "append_sent_log") as mock_append_log:

            result = sender.handle_submission_result(
                username="daiki_tester",
                host_name="Daiki",
                target_city="Sapporo",
                arrival_date="2026-12-01",
                departure_date="2026-12-05",
                message="Hello Daiki",
                submission_confirmed=True,
                http_status=200,
            )

            self.assertTrue(result)
            # Обновление очереди со статусом Sent и sync_db=False (без дублирующих записей в БД)
            mock_update_queue.assert_called_once_with("daiki_tester", "Sent", notes=mock_update_queue.call_args[1].get("notes") or mock_update_queue.call_args[0][2], sync_db=False)
            mock_append_log.assert_called_once()
            self.assertEqual(mock_append_log.call_args[0][0]["status"], "SENT_SUCCESS")
            self.assertFalse(mock_append_log.call_args[1].get("sync_db", True))

    # 8. ТЕСТ: Кнопка исключения из очереди (крестик) /api/autopilot/skip/{username}
    def test_autopilot_skip_endpoint_marks_host_as_skipped(self):
        from unittest.mock import patch
        from fastapi.testclient import TestClient
        from app import app
        from core.models import Trip

        client = TestClient(app)
        mock_trip = Trip(id=TEST_TRIP_ID, url="", city="Sapporo", country="Japan", arrival_date="2026-12-01", departure_date="2026-12-05")

        with patch("app.db_session", side_effect=lambda: get_connection(self.db_path)), \
             patch("database.db_session", side_effect=lambda: get_connection(self.db_path)), \
             patch("core.queue.db_session", side_effect=lambda: get_connection(self.db_path)), \
             patch("core.database.get_active_trip", return_value=mock_trip):

            res = client.post("/api/autopilot/skip/daiki_tester", json={"reason": "Manually messaged by user"})
            self.assertEqual(res.status_code, 200)
            data = res.json()
            self.assertEqual(data["status"], "ok")
            self.assertEqual(data["state"], "skipped")

            # Проверяем, что в core_hosts статус изменился на 'skipped'
            with get_connection(self.db_path) as conn:
                cur = conn.cursor()
                cur.execute("SELECT state, qc_notes FROM core_hosts WHERE host_id = 'h_defect_1';")
                row = cur.fetchone()
                self.assertEqual(row["state"], "skipped")
                self.assertIn("Manually messaged by user", row["qc_notes"])

    # 9. ТЕСТ: Правый стакан строится по confirmed dispatches из sending_history, исключая FAILED и не подтверждённые записи
    def test_sent_history_requires_confirmed_dispatch_in_sending_history(self):
        from unittest.mock import patch
        from fastapi.testclient import TestClient
        from app import app
        from core.models import Trip

        client = TestClient(app)
        mock_trip = Trip(id=TEST_TRIP_ID, url="", city="Sapporo", country="Japan", arrival_date="2026-12-01", departure_date="2026-12-05")

        with get_connection(self.db_path) as conn:
            # Хост 1: state='sent' в core_hosts, но НЕТ записи в sending_history (фантом)
            conn.execute("""
                INSERT INTO core_hosts (trip_id, host_id, username, name, location, state, created_at, updated_at)
                VALUES (?, 'ghost_host_1', 'ghost_user', 'Ghost User', 'Sapporo', 'sent', '2026-11-10', '2026-11-10');
            """, (TEST_TRIP_ID,))

            # Хост 2: попытка отправки завершилась FAILED
            conn.execute("""
                INSERT INTO core_hosts (trip_id, host_id, username, name, location, state, created_at, updated_at)
                VALUES (?, 'failed_host_2', 'failed_user', 'Failed User', 'Sapporo', 'uncertain', '2026-11-10', '2026-11-10');
            """, (TEST_TRIP_ID,))
            conn.execute("""
                INSERT INTO sending_history (host_id, username, status, http_status, mode, notes, error_details, timestamp)
                VALUES ('failed_host_2', 'failed_user', 'FAILED', 500, 'error', 'Network error', 'DNS failed', '2026-11-10 14:00:00');
            """)

            # Хост 3: подтверждённая отправка SENT_SUCCESS
            conn.execute("""
                INSERT INTO core_hosts (trip_id, host_id, username, name, location, state, sent_at, message_draft, created_at, updated_at)
                VALUES (?, 'confirmed_host_3', 'confirmed_user', 'Confirmed User', 'Sapporo', 'sent', '2026-11-10 15:00:00', 'Hello from Sapporo!', '2026-11-10', '2026-11-10');
            """, (TEST_TRIP_ID,))
            conn.execute("""
                INSERT INTO sending_history (host_id, username, status, http_status, mode, notes, timestamp)
                VALUES ('confirmed_host_3', 'confirmed_user', 'SENT_SUCCESS', 200, 'live', 'Sent verified', '2026-11-10 15:00:00');
            """)

        with patch("app.db_session", side_effect=lambda: get_connection(self.db_path)), \
             patch("database.db_session", side_effect=lambda: get_connection(self.db_path)), \
             patch("core.queue.db_session", side_effect=lambda: get_connection(self.db_path)), \
             patch("core.database.get_active_trip", return_value=mock_trip):

            res = client.get(f"/api/autopilot/status?trip_id={TEST_TRIP_ID}")
            self.assertEqual(res.status_code, 200)
            data = res.json()

            sent_hist = data.get("sent_history", [])
            sent_usernames = [s["username"] for s in sent_hist]

            # Подтверждённый хост отображается в правом стакане
            self.assertIn("confirmed_user", sent_usernames)
            # Фантомный хост без sending_history НЕ отображается
            self.assertNotIn("ghost_user", sent_usernames)
            # Неудачная отправка (FAILED) НЕ отображается
            self.assertNotIn("failed_user", sent_usernames)

            # Сверка (reconciliation) выявляет расхождение для ghost_user
            rec = data.get("reconciliation", {})
            self.assertEqual(rec.get("status"), "DISCREPANCY")
            self.assertEqual(rec.get("unconfirmed_sent_count"), 1)
            unconfirmed_usernames = [u["username"] for u in rec.get("unconfirmed_hosts", [])]
            self.assertIn("ghost_user", unconfirmed_usernames)

    # 10. ТЕСТ: Верификатор миграции выявляет расхождение unconfirmed_sent_in_core
    def test_verify_migration_detects_unconfirmed_sent_hosts(self):
        from scripts.verify_migration import verify_migration

        with get_connection(self.db_path) as conn:
            conn.execute("""
                INSERT INTO core_hosts (trip_id, host_id, username, name, location, state, created_at, updated_at)
                VALUES (?, 'unconfirmed_h', 'unconfirmed_user_test', 'Unconfirmed', 'Sapporo', 'sent', '2026-11-10', '2026-11-10');
            """, (TEST_TRIP_ID,))

        v_rep = verify_migration(self.db_path)
        self.assertFalse(v_rep["checks_passed"])
        self.assertGreaterEqual(len(v_rep["per_record_checks"]["unconfirmed_sent_in_core"]["discrepancies"]), 1)
        disc_users = [d["username"] for d in v_rep["per_record_checks"]["unconfirmed_sent_in_core"]["discrepancies"]]
        self.assertIn("unconfirmed_user_test", disc_users)

    # 11. ТЕСТ: Единое сквозное логирование — новая задача НЕ стирает буфер логов
    def test_process_manager_preserves_logs_and_unified_operations_log(self):
        import asyncio
        from process_manager import ProcessManager

        pm = ProcessManager()
        pm.logs_buffer = ["Log entry from Autopilot 1", "Log entry from Autopilot 2"]

        # Запуск задачи через pm.start_job не должен стирать предыдущие логи
        async def run_test():
            res = await pm.start_job("Тестовая задача", ["python3", "-c", "print('Вывод тестовой задачи')"])
            self.assertTrue(res["success"])
            if pm.active_proc:
                await pm.active_proc.wait()
            await asyncio.sleep(0.2)

        asyncio.run(run_test())

        # Проверяем, что ранние логи автопилота сохранились в буфере
        self.assertIn("Log entry from Autopilot 1", pm.logs_buffer[0])
        self.assertIn("Log entry from Autopilot 2", pm.logs_buffer[1])

        # Проверяем, что вывод задачи добавился следом в общий буфер
        combined_text = " ".join(pm.logs_buffer)
        self.assertIn("Вывод тестовой задачи", combined_text)
        self.assertIn("Тестовая задача", combined_text)


if __name__ == "__main__":
    unittest.main()
