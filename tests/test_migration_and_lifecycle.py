"""
tests/test_migration_and_lifecycle.py

Автоматические тесты миграции и полного жизненного цикла (DDD E2E Lifecycle):
1. Тест идемпотентности миграции: повторный запуск `run_migration` на тестовой БД
   не меняет количество записей и завершается с 0 ошибок.
2. Полный сквозной цикл доменной сущности:
   Trip -> Collector (Ingest) -> Selector (Like) -> Queue (Draft + Strict QC) ->
   Queue (Approve) -> Autopilot (Dispatch + Jitter + Sent) -> Inbox (Reply + AI Suggestion).
3. Проверка неразрывности цепочки сквозного аудита (core_audit_logs).
"""

import json
import unittest
from pathlib import Path
import tempfile

from core.autopilot import AutopilotEngine
from core.collector import CollectorEngine
from core.database import get_connection, init_db
from core.dispatch_confirmation import get_confirmation_validator
from core.dispatch_identity import register_host_identity, reserve_host_for_dispatch
from core.inbox import InboxEngine
from core.models import HostState
from core.queue import QueueEngine
from core.selector import SelectorEngine
from scripts.migrate_legacy_database import run_migration
from scripts.verify_migration import verify_migration

REPO_ROOT = Path(__file__).resolve().parent.parent
TEST_DB_PATH = Path(tempfile.mkdtemp(prefix="cs-migration-lifecycle-")) / "test.db"
LIFECYCLE_TRIP_ID = "trip_lifecycle_sapporo"


class TestMigrationAndLifecycle(unittest.TestCase):
    def setUp(self):
        global TEST_DB_PATH
        TEST_DB_PATH = Path(tempfile.mkdtemp(prefix="cs-migration-lifecycle-test-")) / "test.db"
        init_db(TEST_DB_PATH)
        with get_connection(TEST_DB_PATH) as conn:
            conn.execute("DELETE FROM core_audit_logs;")
            conn.execute("DELETE FROM core_hosts;")
            conn.execute("DELETE FROM core_host_snapshots;")
            conn.execute("DELETE FROM sending_history;")
            conn.execute("DELETE FROM sending_queue;")
            conn.execute("DELETE FROM generated_messages;")
            conn.execute("DELETE FROM hosts;")
            conn.execute("DELETE FROM core_trips;")
            conn.execute("DELETE FROM user_trips;")

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            Path(f"{TEST_DB_PATH}{suffix}").unlink(missing_ok=True)

    # 1. ТЕСТ: ИДЕМПОТЕНТНОСТЬ МИГРАЦИИ НА ТЕСТОВОЙ БД
    def test_migration_idempotency(self):
        """Проверяет, что запуск миграции 1 раз и повторно 2 раз дает одинаковый чистый результат."""
        with get_connection(TEST_DB_PATH) as conn:
            # Создаем тестовую user_trip
            conn.execute(
                """
                INSERT INTO user_trips (trip_id, city, arrival_date, departure_date, dates_text, url, is_active, created_at, updated_at)
                VALUES ('trip_test_idemp', 'Osaka', '2026-12-01', '2026-12-04', 'Dec 1–4', 'https://cs.com/trips/osaka', 1, '2026-11-10', '2026-11-10');
                """
            )
            # Создаем тестового host
            conn.execute(
                """
                INSERT INTO hosts (id, username, name, clean_name, location, about_me, profile_url, stay_request_url, first_scraped_at, updated_at)
                VALUES ('h_idemp_1', 'osaka_host', 'Takeshi', 'Takeshi', 'Osaka', 'Ski instructor in Hokkaido', 'https://couchsurfing.com/people/osaka_host', 'https://couchsurfing.com/requests/osaka_host', '2026-11-10', '2026-11-10');
                """
            )

        # Прогон 1
        res1 = run_migration(TEST_DB_PATH, dry_run=False)
        self.assertEqual(len(res1["errors"]), 0)
        self.assertEqual(res1["trips_reconciled"], 1)
        self.assertGreaterEqual(res1["hosts_backfilled_to_core"], 1)

        # Верификация после 1 прогона
        v_rep1 = verify_migration(TEST_DB_PATH)
        self.assertTrue(v_rep1["checks_passed"])

        # Прогон 2 (должен ничего не менять и завершиться без ошибок)
        res2 = run_migration(TEST_DB_PATH, dry_run=False)
        self.assertEqual(len(res2["errors"]), 0)
        self.assertEqual(res2["trips_reconciled"], 0)
        self.assertEqual(res2["hosts_backfilled_to_core"], 0)

        # Верификация после 2 прогона
        v_rep2 = verify_migration(TEST_DB_PATH)
        self.assertTrue(v_rep2["checks_passed"])

    # 2. ТЕСТ: ПОЛНЫЙ СКВОЗНОЙ ЖИЗНЕННЫЙ ЦИКЛ (E2E LIFECYCLE)
    def test_full_domain_lifecycle(self):
        """
        Проверяет полный цикл обработки кандидата:
        1. Создание поездки
        2. Ingest сырого профиля Коллектором
        3. Отбор Селектором (Like)
        4. Генерация выверенного черновика и прохождение Strict QC
        5. Ручной Approve пользователем
        6. Получение автопилотом, проверка Jitter и отметка 'sent'
        7. Получение входящего ответа в Inbox и генерация автоответа
        8. Проверка сквозной аудиторской дорожки в core_audit_logs
        """
        # 1. Создание поездки
        with get_connection(TEST_DB_PATH) as conn:
            conn.execute(
                """
                INSERT INTO core_trips (id, url, country, city, arrival_date, departure_date, dates_text, is_active, created_at)
                VALUES (?, 'https://couchsurfing.com/trips/sapporo', 'Japan', 'Sapporo', '2026-12-10', '2026-12-15', 'Dec 10–15', 1, '2026-11-10T00:00:00');
                """,
                (LIFECYCLE_TRIP_ID,),
            )

        collector = CollectorEngine(trip_id=LIFECYCLE_TRIP_ID, db_file=TEST_DB_PATH)
        selector = SelectorEngine(trip_id=LIFECYCLE_TRIP_ID, db_file=TEST_DB_PATH)
        queue = QueueEngine(trip_id=LIFECYCLE_TRIP_ID, db_file=TEST_DB_PATH)
        autopilot = AutopilotEngine(trip_id=LIFECYCLE_TRIP_ID, db_file=TEST_DB_PATH)
        inbox = InboxEngine(trip_id=LIFECYCLE_TRIP_ID, db_file=TEST_DB_PATH)

        # 2. Ingest хоста
        raw_host = {
            "id": "h_sapporo_snow",
            "username": "snow_guide_daiki",
            "name": "Daiki Sato",
            "age": 31,
            "is_verified": True,
            "location": "Sapporo, Japan",
            "about": "Ski coach in Niseko and mountain photography enthusiast. Love drinking tea and sharing local secrets.",
        }
        card = collector.ingest_raw_host(raw_host)
        self.assertIsNotNone(card)
        self.assertEqual(card.state, HostState.DISCOVERED)
        self.assertGreaterEqual(card.score_stars, 3)

        # 3. Селектор: Like
        self.assertTrue(selector.decide("h_sapporo_snow", "like"))
        with get_connection(TEST_DB_PATH) as conn:
            cur = conn.cursor()
            cur.execute("SELECT state FROM core_hosts WHERE trip_id = ? AND host_id = 'h_sapporo_snow';", (LIFECYCLE_TRIP_ID,))
            self.assertEqual(cur.fetchone()[0], HostState.SELECTED.value)

        # 4. Очередь: Генерация черновика и Strict QC
        draft_res = queue.generate_draft("h_sapporo_snow")
        self.assertIsNotNone(draft_res)
        self.assertTrue(draft_res["qc_passed"])
        text = draft_res["text"]
        self.assertIn("Daiki", text)
        self.assertIn("Sapporo", text)
        self.assertIn("Siberia", text)
        wc = len(text.split())
        self.assertTrue(50 <= wc <= 95, f"Word count was {wc}")

        # 5. Ручной Approve
        self.assertTrue(queue.approve("h_sapporo_snow", created_by="user:test"))
        with get_connection(TEST_DB_PATH) as conn:
            cur = conn.cursor()
            cur.execute("SELECT state FROM core_hosts WHERE trip_id = ? AND host_id = 'h_sapporo_snow';", (LIFECYCLE_TRIP_ID,))
            self.assertEqual(cur.fetchone()[0], HostState.APPROVED.value)

        # 6. Автопилот: Выбор и отправка
        ready = autopilot.get_ready_candidates()
        self.assertEqual(len(ready), 1)
        self.assertEqual(ready[0]["host_id"], "h_sapporo_snow")

        jitter = autopilot.calculate_jitter_seconds()
        self.assertTrue(300 <= jitter <= 600)

        # Создаём capability для mark_as_sent
        register_host_identity("username", "snow_guide_daiki", "un:snow_guide_daiki", db_file=TEST_DB_PATH)
        reservation = reserve_host_for_dispatch("username", "snow_guide_daiki", LIFECYCLE_TRIP_ID, db_file=TEST_DB_PATH)
        validator = get_confirmation_validator(db_file=TEST_DB_PATH)
        capability = validator.issue_capability(
            attempt_id=reservation["attempt_id"],
            canonical_host_key=reservation["canonical_key"],
        )
        self.assertTrue(autopilot.mark_as_sent(
            "h_sapporo_snow",
            http_status=200,
            capability=capability,
            attempt_id=reservation["attempt_id"],
        ))
        self.assertEqual(autopilot.get_sent_today_count(), 1)
        self.assertEqual(len(autopilot.get_ready_candidates()), 0)

        with get_connection(TEST_DB_PATH) as conn:
            cur = conn.cursor()
            cur.execute("SELECT state FROM core_hosts WHERE trip_id = ? AND host_id = 'h_sapporo_snow';", (LIFECYCLE_TRIP_ID,))
            self.assertEqual(cur.fetchone()[0], HostState.SENT.value)

        # 7. Inbox: Входящее сообщение от хоста
        self.assertTrue(inbox.record_incoming_reply("h_sapporo_snow", "Sounds awesome Andrey! What time do you land in Sapporo?"))
        with get_connection(TEST_DB_PATH) as conn:
            cur = conn.cursor()
            cur.execute("SELECT state FROM core_hosts WHERE trip_id = ? AND host_id = 'h_sapporo_snow';", (LIFECYCLE_TRIP_ID,))
            self.assertEqual(cur.fetchone()[0], HostState.INBOX.value)

        suggestion = inbox.generate_reply_suggestion("Daiki", "What time do you land in Sapporo?")
        self.assertIn("Hi Daiki,", suggestion)
        self.assertIn("late afternoon", suggestion)

        # 8. Проверка неразрывности цепочки аудита в core_audit_logs
        with get_connection(TEST_DB_PATH) as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT module, event FROM core_audit_logs WHERE trip_id = ? AND host_id = 'h_sapporo_snow' ORDER BY id ASC;",
                (LIFECYCLE_TRIP_ID,),
            )
            events = [r["event"] for r in cur.fetchall()]

            self.assertIn("HOST_INGESTED", events)
            self.assertIn("DECISION_LIKE", events)
            self.assertIn("DRAFT_GENERATED", events)
            self.assertIn("MESSAGE_APPROVED", events)
            self.assertIn("REQUEST_SENT_VERIFIED", events)
            self.assertIn("INCOMING_REPLY_RECEIVED", events)


if __name__ == "__main__":
    unittest.main()
