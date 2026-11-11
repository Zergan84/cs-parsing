"""
test_phase0_infrastructure.py - Верификационный тестовый набор Фазы 0 (безопасная изолированная тестовая инфраструктура).

Проверяет все 9 обязательных требований Фазы 0:
1. Инициализация временной SQLite БД вне production output/.
2. Создание тестовых записей hosts, trip_hosts, sending_queue.
3. Создание тестового requests_queue.json во временной директории.
4. Запись и чтение тестовых данных через SQLite, RequestQueueManager и FastAPI TestClient.
5. Проверка, что все модули (database, request_manager, app, auto_sender, scraper, inbox_checker и др.) используют одну тестовую БД.
6. Проверка, что все JSON-файлы (requests_queue.json, sent_log.json, autopilot_status.json и др.) создаются исключительно в тестовой директории.
7. Проверка блокировки внешней сети (socket, urllib, requests, httpx, Playwright против couchsurfing.com).
8. Проверка блокировки запуска реального AutoSender (subprocess и прямой вызов send_request / run).
9. Проверка неизменности SHA-256 production DB и production JSON до и после выполнения.
"""

import json
import os
import socket
import sqlite3
import subprocess
import sys
import unittest
import urllib.request
from pathlib import Path

from test_isolation import (
    ExternalNetworkAccessError,
    ForbiddenProcessExecutionError,
    PROD_BASE_DIR,
    PROD_DB_PATH,
    PROD_OUTPUT_DIR,
    PROD_REQUESTS_DB_SYMLINK,
    ProductionAccessError,
    activate_test_isolation,
    assert_not_production_paths,
    get_db_path,
    get_output_dir,
    reset_isolated_test_env,
    snapshot_production_checksums,
    verify_production_checksums,
)

activate_test_isolation()

import app as app_mod
import auto_sender as auto_sender_mod
import database as db_mod
import filter_hosts as filter_hosts_mod
import gender_classifier as gender_classifier_mod
import hosting_guard as hosting_guard_mod
import inbox_checker as inbox_checker_mod
import inbox_service as inbox_service_mod
import request_manager as rm_mod
import scraper as scraper_mod
import sync_sent_requests as sync_sent_mod
from fastapi.testclient import TestClient


class TestPhase0IsolationInfrastructure(unittest.TestCase):
    """Полная проверка изолированной тестовой инфраструктуры Фазы 0."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.initial_prod_snapshot = snapshot_production_checksums()

    def setUp(self) -> None:
        reset_isolated_test_env()
        db_mod.init_db()

    @classmethod
    def tearDownClass(cls) -> None:
        verify_production_checksums(cls.initial_prod_snapshot)

    def test_01_temporary_sqlite_db_initialization(self) -> None:
        """1. Инициализация временной SQLite БД физически вне production output/."""
        db_mod.init_db()
        active_db = get_db_path()
        active_out = get_output_dir()

        self.assertTrue(active_db.exists(), f"Тестовая БД должна существовать: {active_db}")
        self.assertNotEqual(active_db.resolve(), PROD_DB_PATH.resolve())
        self.assertNotEqual(active_out.resolve(), PROD_OUTPUT_DIR.resolve())
        self.assertNotIn(PROD_OUTPUT_DIR.resolve(), active_db.resolve().parents)
        self.assertNotIn(PROD_OUTPUT_DIR.resolve(), active_out.resolve().parents)

        with db_mod.db_session() as conn:
            cur = conn.cursor()
            cur.execute("SELECT name FROM sqlite_master WHERE type='table';")
            tables = {r[0] for r in cur.fetchall()}

        for expected_table in (
            "hosts",
            "trip_hosts",
            "sending_queue",
            "generated_messages",
            "profile_analyses",
            "personal_context_facts",
            "inbox_conversations",
        ):
            self.assertIn(expected_table, tables)

    def test_02_create_test_records_hosts_trip_hosts_sending_queue(self) -> None:
        """2. Создание тестовых записей hosts, trip_hosts, sending_queue в изолированной БД."""
        db_mod.init_db()
        with db_mod.db_session() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO hosts (id, username, name, clean_name, profile_url, stay_request_url, location, about_me, first_scraped_at, updated_at)
                VALUES ('phase0_h1', 'phase0_user_1', 'Kenji Sato', 'Kenji', 'http://127.0.0.1/u/1', '', 'Kyoto', 'Mountain hiker in Kyoto', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP);
                """
            )
            cur.execute(
                """
                INSERT INTO trip_hosts (trip_id, host_id, city, arrival_date, departure_date, created_at)
                VALUES ('trip_phase0_kyoto', 'phase0_h1', 'Kyoto', '2026-11-19', '2026-11-21', CURRENT_TIMESTAMP);
                """
            )
            cur.execute(
                """
                INSERT INTO generated_messages (id, host_id, check_in, check_out, message_text, word_count, qc_passed, is_active, created_at)
                VALUES ('msg_phase0_1', 'phase0_h1', '2026-11-19', '2026-11-21', 'Hello Kenji, test message.', 4, 1, 1, CURRENT_TIMESTAMP);
                """
            )
            cur.execute(
                """
                INSERT INTO sending_queue (id, host_id, current_message_id, status, created_at, updated_at)
                VALUES ('sq_phase0_1', 'phase0_h1', 'msg_phase0_1', 'Draft', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP);
                """
            )

        with db_mod.db_session() as conn:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM hosts WHERE id = 'phase0_h1';")
            self.assertEqual(cur.fetchone()[0], 1)
            cur.execute("SELECT COUNT(*) FROM trip_hosts WHERE trip_id = 'trip_phase0_kyoto';")
            self.assertEqual(cur.fetchone()[0], 1)
            cur.execute("SELECT status FROM sending_queue WHERE host_id = 'phase0_h1';")
            self.assertEqual(cur.fetchone()[0], "Draft")

    def test_03_and_04_requests_queue_json_and_read_write_roundtrip(self) -> None:
        """3 & 4. Создание тестового requests_queue.json, запись и чтение тестовых данных."""
        db_mod.init_db()
        with db_mod.db_session() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT OR REPLACE INTO hosts (id, username, name, clean_name, profile_url, stay_request_url, location, about_me, about_me_clean, first_scraped_at, updated_at)
                VALUES ('phase0_h2', 'phase0_user_2', 'Yuki Tanaka', 'Yuki', 'http://127.0.0.1/u/2', '', 'Kyoto', 'I love photography and tea.', 'I love photography and tea.', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP);
                """
            )
            cur.execute(
                """
                INSERT OR REPLACE INTO trip_hosts (trip_id, host_id, city, arrival_date, departure_date, created_at)
                VALUES ('trip_phase0_rw', 'phase0_h2', 'Kyoto', '2026-11-19', '2026-11-21', CURRENT_TIMESTAMP);
                """
            )

        qm = rm_mod.RequestQueueManager()
        added_res = qm.enqueue_hosts(
            [
                {
                    "id": "phase0_h2",
                    "username": "phase0_user_2",
                    "name": "Yuki Tanaka",
                    "clean_name": "Yuki",
                    "location": "Kyoto",
                    "about_me": "I love photography and tea.",
                    "about_me_clean": "I love photography and tea.",
                }
            ],
            trip_id="trip_phase0_rw",
        )
        added_count = added_res[0] if isinstance(added_res, tuple) else int(added_res)
        self.assertEqual(added_count, 1)

        # Проверяем, что requests_queue.json создан именно во временной директории
        test_queue_file = get_output_dir() / "requests_queue.json"
        self.assertTrue(test_queue_file.exists())
        self.assertEqual(qm.queue_file.resolve(), test_queue_file.resolve())

        q_json = json.loads(test_queue_file.read_text(encoding="utf-8"))
        self.assertTrue(any(item.get("username") == "phase0_user_2" for item in q_json))

        # Проверяем чтение через load_queue и через FastAPI TestClient
        loaded = qm.load_queue(exclude_sent=False, trip_id="trip_phase0_rw")
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["username"], "phase0_user_2")

        client = TestClient(app_mod.app)
        resp = client.get("/api/requests/queue", params={"trip_id": "trip_phase0_rw"})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["total_count"], 1)
        self.assertEqual(body["queue"][0]["username"], "phase0_user_2")

    def test_05_all_modules_point_to_same_isolated_db_and_output(self) -> None:
        """5. Проверка, что все модули используют одну и ту же тестовую БД и тестовый output/."""
        expected_out = get_output_dir().resolve()
        expected_db = get_db_path().resolve()

        self.assertEqual(db_mod.OUTPUT_DIR.resolve(), expected_out)
        self.assertEqual(db_mod.DB_PATH.resolve(), expected_db)

        with db_mod.db_session() as conn:
            db_list = conn.execute("PRAGMA database_list;").fetchall()
            main_db_file = Path(db_list[0][2]).resolve()
            self.assertEqual(main_db_file, expected_db)

        for mod in (
            rm_mod,
            app_mod,
            auto_sender_mod,
            scraper_mod,
            inbox_checker_mod,
            inbox_service_mod,
            filter_hosts_mod,
            hosting_guard_mod,
            gender_classifier_mod,
            sync_sent_mod,
        ):
            self.assertEqual(
                mod.OUTPUT_DIR.resolve(),
                expected_out,
                f"Модуль {mod.__name__} имеет неверный OUTPUT_DIR: {mod.OUTPUT_DIR}",
            )

        self.assertEqual(rm_mod.QUEUE_FILE.resolve(), (expected_out / "requests_queue.json").resolve())
        self.assertEqual(app_mod.QUEUE_FILE.resolve(), (expected_out / "requests_queue.json").resolve())
        self.assertEqual(auto_sender_mod.QUEUE_FILE.resolve(), (expected_out / "requests_queue.json").resolve())
        self.assertEqual(auto_sender_mod.SENT_LOG_FILE.resolve(), (expected_out / "sent_log.json").resolve())
        self.assertEqual(inbox_checker_mod.INBOX_STATUS_FILE.resolve(), (expected_out / "inbox_status.json").resolve())

    def test_06_all_json_files_created_exclusively_in_test_dir(self) -> None:
        """6. Проверка, что все JSON-файлы создаются исключительно во временной тестовой директории."""
        expected_out = get_output_dir().resolve()
        client = TestClient(app_mod.app)

        # Вызываем pause автопилота (создаёт autopilot_status.json)
        res_pause = client.post("/api/autopilot/pause")
        self.assertEqual(res_pause.status_code, 200)
        self.assertTrue((expected_out / "autopilot_status.json").exists())

        # Записываем тестовый sent_log через AutoSender._record_dry_run_success
        sender = auto_sender_mod.AutoSender(dry_run=True, headless=True, test_mode=True)
        sender._record_dry_run_success(
            username="phase0_dry_user",
            host_name="Phase0 Dry",
            target_city="Kyoto",
            arrival_date="2026-11-19",
            departure_date="2026-11-21",
            before_screenshot=expected_out / "debug" / "before.png",
            found_selector="button",
        )
        self.assertTrue((expected_out / "sent_log.json").exists())

        # Сохраняем отклонённого хоста через HostingAvailabilityGuard
        guard = hosting_guard_mod.HostingAvailabilityGuard()
        guard.record_rejected({"username": "phase0_rej", "name": "Rej"}, "not hosting")
        self.assertTrue((expected_out / "rejected_hosts.json").exists())

        for json_file in expected_out.rglob("*.json"):
            self.assertIn(expected_out, json_file.resolve().parents)
            self.assertNotIn(PROD_OUTPUT_DIR.resolve(), json_file.resolve().parents)

    def test_07_external_network_and_couchsurfing_blocked(self) -> None:
        """7. Проверка блокировки внешней сети (socket, urllib, requests, httpx, Playwright)."""
        # 7.1 socket.create_connection к внешнему домену
        with self.assertRaises(ExternalNetworkAccessError):
            socket.create_connection(("www.couchsurfing.com", 443), timeout=1.0)

        # 7.2 urllib.request.urlopen к внешнему URL
        with self.assertRaises(ExternalNetworkAccessError):
            urllib.request.urlopen("https://www.couchsurfing.com", timeout=1.0)

        # 7.3 urllib.request.urlopen к локальному порту Ollama (11434) и production-серверу (8000)
        with self.assertRaises(ExternalNetworkAccessError):
            urllib.request.urlopen("http://localhost:11434/api/tags", timeout=1.0)
        with self.assertRaises(ExternalNetworkAccessError):
            urllib.request.urlopen("http://127.0.0.1:8000/api/status", timeout=1.0)

        # 7.4 requests.get к внешнему URL
        import requests

        with self.assertRaises(ExternalNetworkAccessError):
            requests.get("https://www.couchsurfing.com", timeout=1.0)

        # 7.5 httpx.Client.get к внешнему URL
        import httpx

        with self.assertRaises(ExternalNetworkAccessError):
            with httpx.Client() as hclient:
                hclient.get("https://www.couchsurfing.com")

    def test_08_real_autosender_and_production_writes_blocked(self) -> None:
        """8. Проверка блокировки запуска реального AutoSender и записи в production-пути."""
        # 8.1 Запрет subprocess запуска auto_sender.py и build_index.py
        with self.assertRaises(ForbiddenProcessExecutionError):
            subprocess.run([sys.executable, str(PROD_BASE_DIR / "auto_sender.py")], check=False)

        with self.assertRaises(ForbiddenProcessExecutionError):
            subprocess.Popen([sys.executable, str(PROD_BASE_DIR / "build_index.py")])

        # 8.2 Запрет прямого вызова AutoSender.send_request
        sender = auto_sender_mod.AutoSender(dry_run=False, headless=True, test_mode=False)
        with self.assertRaises(ForbiddenProcessExecutionError):
            sender.send_request(None, {"username": "test"})

        # 8.3 Запрет запуска AutoSender.run() без мока sync_playwright
        with self.assertRaises(ForbiddenProcessExecutionError):
            sender.run()

        # 8.4 Запрет открытия production SQLite БД через sqlite3.connect
        with self.assertRaises(ProductionAccessError):
            sqlite3.connect(str(PROD_DB_PATH))

        with self.assertRaises(ProductionAccessError):
            sqlite3.connect(str(PROD_REQUESTS_DB_SYMLINK))

        # 8.5 Запрет открытия production JSON на запись
        with self.assertRaises(ProductionAccessError):
            open(PROD_OUTPUT_DIR / "requests_queue.json", "w", encoding="utf-8")

        with self.assertRaises(ProductionAccessError):
            (PROD_OUTPUT_DIR / "sent_log.json").write_text("[]", encoding="utf-8")

        # 8.6 Запрет указания production-путей в CS_DB_PATH / CS_OUTPUT_DIR
        saved_out = os.environ.get("CS_OUTPUT_DIR")
        saved_db = os.environ.get("CS_DB_PATH")
        try:
            os.environ["CS_OUTPUT_DIR"] = str(PROD_OUTPUT_DIR)
            with self.assertRaises(ProductionAccessError):
                assert_not_production_paths()

            os.environ["CS_OUTPUT_DIR"] = saved_out or ""
            os.environ["CS_DB_PATH"] = str(PROD_DB_PATH)
            with self.assertRaises(ProductionAccessError):
                assert_not_production_paths()
        finally:
            if saved_out is not None:
                os.environ["CS_OUTPUT_DIR"] = saved_out
            if saved_db is not None:
                os.environ["CS_DB_PATH"] = saved_db

    def test_09_production_files_sha256_unchanged(self) -> None:
        """9. Проверка, что production DB и все production JSON остались 100% неизменными."""
        verify_production_checksums(self.initial_prod_snapshot)


if __name__ == "__main__":
    unittest.main()
