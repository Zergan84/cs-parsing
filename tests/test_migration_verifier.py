"""
tests/test_migration_verifier.py - Тесты верификатора миграции с намеренно поврежденными fixture-данными.

Проверяет детекцию всех 5 категорий несоответствий:
1. MISSING
2. EXTRA
3. DUPLICATE
4. FIELD_MISMATCH
5. UNRESOLVED_RELATIONSHIP
А также гарантирует строго read-only режим работы верификатора.
"""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts.verify_migration import verify_migration


def _init_mock_db(db_path: Path):
    """Инициализирует тестовую схему базы данных с legacy и core таблицами."""
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("""
            CREATE TABLE user_trips (
                trip_id TEXT PRIMARY KEY,
                title TEXT,
                city TEXT,
                arrival_date TEXT,
                departure_date TEXT,
                url TEXT,
                is_active INTEGER DEFAULT 0
            );
        """)
        conn.execute("""
            CREATE TABLE core_trips (
                id TEXT PRIMARY KEY,
                title TEXT,
                city TEXT,
                arrival_date TEXT,
                departure_date TEXT,
                url TEXT,
                is_active INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
        """)
        conn.execute("""
            CREATE TABLE hosts (
                id TEXT PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                name TEXT,
                clean_name TEXT,
                location TEXT,
                about_me TEXT,
                profile_url TEXT,
                stay_request_url TEXT,
                first_scraped_at TEXT,
                updated_at TEXT
            );
        """)
        conn.execute("""
            CREATE TABLE core_hosts (
                trip_id TEXT NOT NULL,
                host_id TEXT NOT NULL,
                username TEXT NOT NULL,
                name TEXT NOT NULL,
                location TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'discovered',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (trip_id) REFERENCES core_trips(id) ON DELETE CASCADE
            );
        """)
        conn.execute("""
            CREATE TABLE core_host_snapshots (
                trip_id TEXT NOT NULL,
                host_id TEXT NOT NULL,
                raw_data_json TEXT NOT NULL,
                captured_at TEXT NOT NULL,
                PRIMARY KEY (trip_id, host_id)
            );
        """)
        conn.execute("""
            CREATE TABLE sending_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                host_id TEXT,
                username TEXT,
                status TEXT,
                timestamp TEXT
            );
        """)
        conn.execute("""
            CREATE TABLE inbox_conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                host_username TEXT UNIQUE,
                host_name TEXT,
                status TEXT
            );
        """)

        # Базовые консистентные данные
        conn.execute("""
            INSERT INTO user_trips (trip_id, title, city, arrival_date, departure_date, url, is_active)
            VALUES ('trip_1', 'Sapporo Trip', 'Sapporo', '2026-12-01', '2026-12-05', 'https://cs.com/trips/1', 1);
        """)
        conn.execute("""
            INSERT INTO core_trips (id, title, city, arrival_date, departure_date, url, is_active, created_at, updated_at)
            VALUES ('trip_1', 'Sapporo Trip', 'Sapporo', '2026-12-01', '2026-12-05', 'https://cs.com/trips/1', 1, '2026-11-10', '2026-11-10');
        """)
        conn.execute("""
            INSERT INTO hosts (id, username, name, clean_name, location, first_scraped_at, updated_at)
            VALUES ('h_1', 'kenji_s', 'Kenji Sato', 'Kenji', 'Sapporo', '2026-11-10', '2026-11-10');
        """)
        conn.execute("""
            INSERT INTO core_hosts (trip_id, host_id, username, name, location, state, created_at, updated_at)
            VALUES ('trip_1', 'h_1', 'kenji_s', 'Kenji Sato', 'Sapporo', 'discovered', '2026-11-10', '2026-11-10');
        """)
        conn.execute("""
            INSERT INTO core_host_snapshots (trip_id, host_id, raw_data_json, captured_at)
            VALUES ('trip_1', 'h_1', '{"name": "Kenji Sato"}', '2026-11-10');
        """)


class TestMigrationVerifier(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_verify.db"
        _init_mock_db(self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_clean_database_passes(self):
        report = verify_migration(self.db_path)
        self.assertTrue(report["checks_passed"])
        self.assertEqual(report["integrity_check"], "ok")
        for count in report["category_counts"].values():
            self.assertEqual(count, 0)

    def test_detects_missing_host_and_trip(self):
        # Намеренно добавляем legacy хоста и поездку без переноса в core
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO user_trips (trip_id, title, city, arrival_date, departure_date, url, is_active)
                VALUES ('trip_missing', 'Okinawa Trip', 'Naha', '2026-12-10', '2026-12-15', 'https://cs.com/trips/2', 0);
            """)
            conn.execute("""
                INSERT INTO hosts (id, username, name, clean_name, location, first_scraped_at, updated_at)
                VALUES ('h_missing', 'taro_o', 'Taro Okinawa', 'Taro', 'Naha', '2026-11-10', '2026-11-10');
            """)

        report = verify_migration(self.db_path)
        self.assertFalse(report["checks_passed"])
        self.assertGreaterEqual(report["category_counts"]["MISSING"], 2)

    def test_detects_extra_unregistered_entities(self):
        # Намеренно добавляем core_trip и core_host, которых нет в legacy источниках
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO core_trips (id, title, city, arrival_date, departure_date, url, is_active, created_at, updated_at)
                VALUES ('trip_extra_phantom', 'Phantom Trip', 'Osaka', '2026-12-10', '2026-12-15', 'https://cs.com/trips/phantom', 0, '2026-11-10', '2026-11-10');
            """)
            conn.execute("""
                INSERT INTO core_hosts (trip_id, host_id, username, name, location, state, created_at, updated_at)
                VALUES ('trip_extra_phantom', 'h_phantom', 'ghost_user', 'Ghost User', 'Osaka', 'discovered', '2026-11-10', '2026-11-10');
            """)

        report = verify_migration(self.db_path)
        self.assertFalse(report["checks_passed"])
        self.assertGreaterEqual(report["category_counts"]["EXTRA"], 1)

    def test_detects_duplicate_records_and_multiple_active_trips(self):
        # Намеренно вставляем вторую активную поездку и дубликат primary key
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO user_trips (trip_id, title, city, arrival_date, departure_date, url, is_active)
                VALUES ('trip_active_2', 'Second Active Trip', 'Tokyo', '2026-12-20', '2026-12-25', 'https://cs.com/trips/3', 1);
            """)
            conn.execute("""
                INSERT INTO core_trips (id, title, city, arrival_date, departure_date, url, is_active, created_at, updated_at)
                VALUES ('trip_active_2', 'Second Active Trip', 'Tokyo', '2026-12-20', '2026-12-25', 'https://cs.com/trips/3', 1, '2026-11-10', '2026-11-10');
            """)
            # Дубликат (trip_id, host_id) в core_hosts
            conn.execute("""
                INSERT INTO core_hosts (trip_id, host_id, username, name, location, state, created_at, updated_at)
                VALUES ('trip_1', 'h_1', 'kenji_s', 'Kenji Sato', 'Sapporo', 'discovered', '2026-11-10', '2026-11-10');
            """)

        report = verify_migration(self.db_path)
        self.assertFalse(report["checks_passed"])
        self.assertGreaterEqual(report["category_counts"]["DUPLICATE"], 2)

    def test_detects_field_mismatch(self):
        # Намеренно искажаем поле city в core_trips по сравнению с user_trips
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("UPDATE core_trips SET city = 'Nagoya' WHERE id = 'trip_1';")

        report = verify_migration(self.db_path)
        self.assertFalse(report["checks_passed"])
        self.assertGreaterEqual(report["category_counts"]["FIELD_MISMATCH"], 1)

    def test_detects_unresolved_relationship_orphan_host(self):
        # Отключаем FK проверку при вставке сироты
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA foreign_keys = OFF;")
            conn.execute("""
                INSERT INTO core_hosts (trip_id, host_id, username, name, location, state, created_at, updated_at)
                VALUES ('non_existent_trip_xyz', 'h_orphan', 'orphan_user', 'Orphan', 'Kyoto', 'discovered', '2026-11-10', '2026-11-10');
            """)

        report = verify_migration(self.db_path)
        self.assertFalse(report["checks_passed"])
        self.assertGreaterEqual(report["category_counts"]["UNRESOLVED_RELATIONSHIP"], 1)

    def test_strictly_read_only(self):
        # Проверяем, что запуск verify_migration не изменяет размер базы и не модифицирует данные
        size_before = self.db_path.stat().st_size
        report = verify_migration(self.db_path)
        self.assertTrue(report["checks_passed"])
        size_after = self.db_path.stat().st_size
        self.assertEqual(size_before, size_after)


if __name__ == "__main__":
    unittest.main()
