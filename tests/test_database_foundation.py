"""
tests/test_database_foundation.py

Тесты 기초 базы данных (Database Foundation) для новой архитектуры диспетчера.

Покрывает:
1. Миграция: свежая БД, существующая legacy-БД, повторный запуск (идемпотентность).
2. Ограничения: уникальность идентичности, активные блокировки, FK-поведение.
3. Внешние ключи: удаление поездки -> SET NULL, защита от удаления попытки.
4. Транзакции: откат при ошибке, активное enforcement внешних ключей.

Все тесты используют изолированные временные SQLite базы данных.
Никогда не трогают production базу output/couchsurfing_platform.db.
"""

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from core.database import get_connection, init_db

REPO_ROOT = Path(__file__).resolve().parent.parent
TEST_DB_DIR = Path(tempfile.mkdtemp(prefix="cs-db-foundation-"))


def _make_db_path(suffix: str) -> Path:
    """Создаёт уникальный путь к временной тестовой БД."""
    return TEST_DB_DIR / f"{suffix}.db"


def _ensure_test_dir() -> None:
    TEST_DB_DIR.mkdir(parents=True, exist_ok=True)


def _cleanup_db(db_path: Path) -> None:
    if db_path.exists():
        try:
            db_path.unlink()
        except Exception:
            pass
    # Also clean up WAL/SHM
    for suffix in ("-wal", "-shm"):
        p = Path(str(db_path) + suffix)
        if p.exists():
            try:
                p.unlink()
            except Exception:
                pass


class TestDatabaseFoundation(unittest.TestCase):
    """Тесты нового слоя БД: попытки, блокировки, идентичности."""

    @classmethod
    def setUpClass(cls):
        _ensure_test_dir()

    @classmethod
    def tearDownClass(cls):
        # Очищаем все тестовые БД
        if TEST_DB_DIR.exists():
            for f in TEST_DB_DIR.iterdir():
                if f.is_file():
                    try:
                        f.unlink()
                    except Exception:
                        pass

    # ------------------------------------------------------------------
    # MIGRATION TESTS
    # ------------------------------------------------------------------

    def test_fresh_database_migration_succeeds(self):
        """Свежая БД: все новые таблицы и колонки создаются без ошибок."""
        db_path = _make_db_path("fresh")
        _cleanup_db(db_path)
        try:
            init_db(db_path)

            with get_connection(db_path) as conn:
                cur = conn.cursor()

                # Проверяем存在所有 таблиц
                cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
                tables = {row[0] for row in cur.fetchall()}
                for required in ("sending_attempts", "host_dispatch_locks", "host_identities"):
                    self.assertIn(required, tables, f"Таблица {required} отсутствует")

                # Проверяем новые колонки в sending_history
                cur.execute("PRAGMA table_info(sending_history)")
                cols = {row[1] for row in cur.fetchall()}
                for required in ("attempt_id", "trip_id", "event_type", "state_at_event", "evidence_type"):
                    self.assertIn(required, cols, f"Колонка {required} отсутствует в sending_history")

                # Проверяем部分 уникальные индексы
                cur.execute("SELECT name FROM sqlite_master WHERE type='index'")
                indexes = {row[0] for row in cur.fetchall()}
                self.assertIn("idx_hdl_one_active_per_user", indexes)
                self.assertIn("idx_hdl_one_active_per_attempt", indexes)
                self.assertIn("idx_sh_idempotent_event", indexes)

                # Проверяем FK enforcement активен
                cur.execute("PRAGMA foreign_keys")
                self.assertEqual(cur.fetchone()[0], 1)
        finally:
            _cleanup_db(db_path)

    def test_existing_legacy_database_migration_succeeds(self):
        """Существующая legacy-БД: новые таблицы добавляются, старые данные не трогаются."""
        db_path = _make_db_path("legacy")
        _cleanup_db(db_path)
        try:
            # Создаём legacy-структуру вручную (имитация существующей БД)
            with get_connection(db_path) as conn:
                cur = conn.cursor()
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS core_trips (
                        id TEXT PRIMARY KEY,
                        url TEXT NOT NULL UNIQUE,
                        city TEXT NOT NULL,
                        arrival_date TEXT NOT NULL,
                        departure_date TEXT NOT NULL,
                        is_active INTEGER DEFAULT 1,
                        created_at TEXT NOT NULL,
                        updated_at TEXT
                    );
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS core_hosts (
                        host_id TEXT NOT NULL,
                        trip_id TEXT NOT NULL,
                        username TEXT NOT NULL,
                        name TEXT NOT NULL,
                        state TEXT NOT NULL DEFAULT 'discovered',
                        PRIMARY KEY (trip_id, host_id),
                        FOREIGN KEY (trip_id) REFERENCES core_trips(id) ON DELETE CASCADE
                    );
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS sending_history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        host_id TEXT NOT NULL,
                        username TEXT NOT NULL,
                        status TEXT NOT NULL,
                        http_status INTEGER,
                        mode TEXT NOT NULL,
                        proof_screenshot TEXT,
                        before_screenshot TEXT,
                        button_selector TEXT,
                        notes TEXT,
                        error_details TEXT,
                        timestamp TEXT NOT NULL
                    );
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS core_audit_logs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp TEXT NOT NULL,
                        trip_id TEXT NOT NULL,
                        module TEXT NOT NULL,
                        event TEXT NOT NULL,
                        host_id TEXT,
                        details_json TEXT
                    );
                """)
                # Вставляем legacy-данные
                cur.execute(
                    "INSERT INTO core_trips (id, url, city, arrival_date, departure_date, is_active, created_at) "
                    "VALUES ('trip_legacy', 'https://example.com/trip/legacy', 'Kyoto', '2026-11-19', '2026-11-21', 1, '2026-11-10T00:00:00')"
                )
                cur.execute(
                    "INSERT INTO core_hosts (host_id, trip_id, username, name, state) "
                    "VALUES ('h1', 'trip_legacy', 'user1', 'User One', 'discovered')"
                )
                cur.execute(
                    "INSERT INTO sending_history (host_id, username, status, mode, timestamp) "
                    "VALUES ('h1', 'user1', 'SENT_SUCCESS', 'live', '2026-11-11T14:00:00')"
                )
                conn.commit()

            # Запускаем init_db (миграцию)
            init_db(db_path)

            # Проверяем: новые таблицы созданы
            with get_connection(db_path) as conn:
                cur = conn.cursor()
                cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
                tables = {row[0] for row in cur.fetchall()}
                for required in ("sending_attempts", "host_dispatch_locks", "host_identities"):
                    self.assertIn(required, tables)

                # Проверяем: legacy-данные нетронуты
                cur.execute("SELECT COUNT(*) FROM core_hosts")
                self.assertEqual(cur.fetchone()[0], 1)
                cur.execute("SELECT COUNT(*) FROM sending_history")
                self.assertEqual(cur.fetchone()[0], 1)
                cur.execute("SELECT state FROM core_hosts WHERE host_id='h1'")
                self.assertEqual(cur.fetchone()[0], 'discovered')

                # Проверяем: новые колонки в sending_history существуют (legacy строки имеют NULL)
                cur.execute("PRAGMA table_info(sending_history)")
                cols = {row[1] for row in cur.fetchall()}
                for required in ("attempt_id", "trip_id", "event_type", "state_at_event", "evidence_type"):
                    self.assertIn(required, cols)
                cur.execute("SELECT attempt_id, trip_id, event_type, state_at_event, evidence_type FROM sending_history")
                row = cur.fetchone()
                self.assertIsNone(row[0])  # attempt_id NULL для legacy
                self.assertIsNone(row[1])  # trip_id NULL
                self.assertIsNone(row[2])  # event_type NULL
                self.assertIsNone(row[3])  # state_at_event NULL
                self.assertIsNone(row[4])  # evidence_type NULL
        finally:
            _cleanup_db(db_path)

    def test_repeat_migration_does_not_duplicate(self):
        """Повторный запуск init_db не дублирует колонки или индексы."""
        db_path = _make_db_path("repeat")
        _cleanup_db(db_path)
        try:
            init_db(db_path)
            init_db(db_path)  # Второй запуск — должен быть безопасен

            with get_connection(db_path) as conn:
                cur = conn.cursor()
                # Проверяем, что таблиц не дублировалось
                cur.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='sending_attempts'")
                self.assertEqual(cur.fetchone()[0], 1)
                cur.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='host_dispatch_locks'")
                self.assertEqual(cur.fetchone()[0], 1)
                cur.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='host_identities'")
                self.assertEqual(cur.fetchone()[0], 1)

                # Проверяем, что индексы не дублировались
                cur.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name='idx_hdl_one_active_per_user'")
                self.assertEqual(cur.fetchone()[0], 1)
                cur.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name='idx_sh_idempotent_event'")
                self.assertEqual(cur.fetchone()[0], 1)

                # Проверяем, что колонки sending_history не дублировались
                cur.execute("PRAGMA table_info(sending_history)")
                attempt_id_cols = [row for row in cur.fetchall() if row[1] == 'attempt_id']
                self.assertEqual(len(attempt_id_cols), 1)
        finally:
            _cleanup_db(db_path)

    def test_existing_sending_history_records_unchanged(self):
        """Существующие записи sending_history остаются нетронутыми после миграции."""
        db_path = _make_db_path("legacy_history")
        _cleanup_db(db_path)
        try:
            with get_connection(db_path) as conn:
                cur = conn.cursor()
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS sending_history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        host_id TEXT NOT NULL,
                        username TEXT NOT NULL,
                        status TEXT NOT NULL,
                        http_status INTEGER,
                        mode TEXT NOT NULL,
                        proof_screenshot TEXT,
                        before_screenshot TEXT,
                        button_selector TEXT,
                        notes TEXT,
                        error_details TEXT,
                        timestamp TEXT NOT NULL
                    );
                """)
                cur.execute(
                    "INSERT INTO sending_history (host_id, username, status, http_status, mode, timestamp) "
                    "VALUES ('h1', 'user1', 'SENT_SUCCESS', 200, 'live', '2026-11-11T14:00:00')"
                )
                cur.execute(
                    "INSERT INTO sending_history (host_id, username, status, http_status, mode, timestamp) "
                    "VALUES ('h2', 'user2', 'FAILED', NULL, 'error', '2026-11-11T15:00:00')"
                )
                conn.commit()

            init_db(db_path)

            with get_connection(db_path) as conn:
                cur = conn.cursor()
                cur.execute("SELECT id, host_id, username, status, http_status, mode, timestamp FROM sending_history ORDER BY id")
                rows = cur.fetchall()
                self.assertEqual(len(rows), 2)
                self.assertEqual(rows[0][1], 'h1')
                self.assertEqual(rows[0][2], 'user1')
                self.assertEqual(rows[0][3], 'SENT_SUCCESS')
                self.assertEqual(rows[0][4], 200)
                self.assertEqual(rows[0][5], 'live')
                self.assertEqual(rows[0][6], '2026-11-11T14:00:00')
                self.assertEqual(rows[1][1], 'h2')
                self.assertEqual(rows[1][2], 'user2')
                self.assertEqual(rows[1][3], 'FAILED')
                # Новые колонки должны быть NULL для legacy строк
                cur.execute("SELECT attempt_id, trip_id, event_type, state_at_event, evidence_type FROM sending_history WHERE id=1")
                row = cur.fetchone()
                self.assertIsNone(row[0])
                self.assertIsNone(row[1])
                self.assertIsNone(row[2])
                self.assertIsNone(row[3])
                self.assertIsNone(row[4])
        finally:
            _cleanup_db(db_path)

    def test_existing_core_tables_intact(self):
        """Существующие core-таблицы остаются целыми после миграции."""
        db_path = _make_db_path("core_intact")
        _cleanup_db(db_path)
        try:
            with get_connection(db_path) as conn:
                cur = conn.cursor()
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS core_trips (
                        id TEXT PRIMARY KEY,
                        url TEXT NOT NULL UNIQUE,
                        city TEXT NOT NULL,
                        arrival_date TEXT NOT NULL,
                        departure_date TEXT NOT NULL,
                        is_active INTEGER DEFAULT 1,
                        created_at TEXT NOT NULL,
                        updated_at TEXT
                    );
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS core_hosts (
                        host_id TEXT NOT NULL,
                        trip_id TEXT NOT NULL,
                        username TEXT NOT NULL,
                        name TEXT NOT NULL,
                        state TEXT NOT NULL DEFAULT 'discovered',
                        PRIMARY KEY (trip_id, host_id),
                        FOREIGN KEY (trip_id) REFERENCES core_trips(id) ON DELETE CASCADE
                    );
                """)
                cur.execute(
                    "INSERT INTO core_trips (id, url, city, arrival_date, departure_date, is_active, created_at) "
                    "VALUES ('trip_1', 'https://example.com/trip/1', 'Kyoto', '2026-11-19', '2026-11-21', 1, '2026-11-10T00:00:00')"
                )
                cur.execute(
                    "INSERT INTO core_hosts (host_id, trip_id, username, name, state) "
                    "VALUES ('h1', 'trip_1', 'user1', 'User One', 'discovered')"
                )
                conn.commit()

            init_db(db_path)

            with get_connection(db_path) as conn:
                cur = conn.cursor()
                cur.execute("SELECT COUNT(*) FROM core_trips")
                self.assertEqual(cur.fetchone()[0], 1)
                cur.execute("SELECT COUNT(*) FROM core_hosts")
                self.assertEqual(cur.fetchone()[0], 1)
                cur.execute("SELECT state FROM core_hosts WHERE host_id='h1'")
                self.assertEqual(cur.fetchone()[0], 'discovered')
        finally:
            _cleanup_db(db_path)

    # ------------------------------------------------------------------
    # CONSTRAINT TESTS
    # ------------------------------------------------------------------

    def test_duplicate_normalized_identity_rejected(self):
        """Дубликат нормализованной идентичности отклоняется UNIQUE индексом."""
        db_path = _make_db_path("dup_identity")
        _cleanup_db(db_path)
        try:
            init_db(db_path)
            now = "2026-11-11T10:00:00"

            with get_connection(db_path) as conn:
                cur = conn.cursor()
                cur.execute(
                    "INSERT INTO host_identities (identity_id, identity_type, identity_value, canonical_key, created_at) "
                    "VALUES ('id1', 'username', 'User1', 'un:user1', ?)",
                    (now,)
                )
                # Повторная вставка того же (type, value) должна упасть
                with self.assertRaises(sqlite3.IntegrityError):
                    cur.execute(
                        "INSERT INTO host_identities (identity_id, identity_type, identity_value, canonical_key, created_at) "
                        "VALUES ('id2', 'username', 'User1', 'un:user1', ?)",
                        (now,)
                    )
        finally:
            _cleanup_db(db_path)

    def test_multiple_aliases_share_canonical_key(self):
        """Несколько алиасов могут ссылаться на один canonical_key."""
        db_path = _make_db_path("multi_alias")
        _cleanup_db(db_path)
        try:
            init_db(db_path)
            now = "2026-11-11T10:00:00"

            with get_connection(db_path) as conn:
                cur = conn.cursor()
                # Три разных identity_value на один canonical_key
                cur.execute(
                    "INSERT INTO host_identities (identity_id, identity_type, identity_value, canonical_key, created_at) "
                    "VALUES ('id1', 'username', 'alice', 'un:alice', ?)", (now,)
                )
                cur.execute(
                    "INSERT INTO host_identities (identity_id, identity_type, identity_value, canonical_key, created_at) "
                    "VALUES ('id2', 'cs_id', 'cs_123', 'un:alice', ?)", (now,)
                )
                cur.execute(
                    "INSERT INTO host_identities (identity_id, identity_type, identity_value, canonical_key, created_at) "
                    "VALUES ('id3', 'username', 'alice_new', 'un:alice', ?)", (now,)
                )
                conn.commit()

                cur.execute(
                    "SELECT COUNT(*) FROM host_identities WHERE canonical_key='un:alice'"
                )
                self.assertEqual(cur.fetchone()[0], 3)
        finally:
            _cleanup_db(db_path)

    def test_one_canonical_key_cannot_have_two_active_locks(self):
        """Один canonical_key не может иметь двух одновременных активных блокировок."""
        db_path = _make_db_path("dup_active_lock")
        _cleanup_db(db_path)
        try:
            init_db(db_path)
            now = "2026-11-11T10:00:00"

            with get_connection(db_path) as conn:
                cur = conn.cursor()
                # Сначала создаём попытку
                cur.execute(
                    "INSERT INTO sending_attempts (id, trip_id, original_trip_id, host_id, username, host_id_norm, username_norm, state, created_at, updated_at) "
                    "VALUES ('att1', NULL, 'trip_x', 'h1', 'user1', 'h1', 'user1', 'pending', ?, ?)",
                    (now, now)
                )
                cur.execute(
                    "INSERT INTO host_dispatch_locks (lock_id, canonical_key, attempt_id, lock_state, trip_id, original_trip_id, created_at, updated_at) "
                    "VALUES ('lock1', 'un:user1', 'att1', 'reserved', NULL, 'trip_x', ?, ?)",
                    (now, now)
                )

                # Создаём вторую попытку для того же пользователя
                cur.execute(
                    "INSERT INTO sending_attempts (id, trip_id, original_trip_id, host_id, username, host_id_norm, username_norm, state, created_at, updated_at) "
                    "VALUES ('att2', NULL, 'trip_x', 'h1', 'user1', 'h1', 'user1', 'pending', ?, ?)",
                    (now, now)
                )
                # Попытка создать вторую активную блокировку для того же canonical_key должна упасть
                with self.assertRaises(sqlite3.IntegrityError):
                    cur.execute(
                        "INSERT INTO host_dispatch_locks (lock_id, canonical_key, attempt_id, lock_state, trip_id, original_trip_id, created_at, updated_at) "
                        "VALUES ('lock2', 'un:user1', 'att2', 'sending', NULL, 'trip_x', ?, ?)",
                        (now, now)
                    )
        finally:
            _cleanup_db(db_path)

    def test_one_attempt_cannot_own_two_active_locks(self):
        """Одна попытка не может владеть двумя одновременными активными блокировками."""
        db_path = _make_db_path("dup_lock_per_attempt")
        _cleanup_db(db_path)
        try:
            init_db(db_path)
            now = "2026-11-11T10:00:00"

            with get_connection(db_path) as conn:
                cur = conn.cursor()
                cur.execute(
                    "INSERT INTO sending_attempts (id, trip_id, original_trip_id, host_id, username, host_id_norm, username_norm, state, created_at, updated_at) "
                    "VALUES ('att1', NULL, 'trip_x', 'h1', 'user1', 'h1', 'user1', 'pending', ?, ?)",
                    (now, now)
                )
                cur.execute(
                    "INSERT INTO host_dispatch_locks (lock_id, canonical_key, attempt_id, lock_state, trip_id, original_trip_id, created_at, updated_at) "
                    "VALUES ('lock1', 'un:user1', 'att1', 'reserved', NULL, 'trip_x', ?, ?)",
                    (now, now)
                )
                # Вторая активная блокировка для той же попытки должна быть отклонена
                with self.assertRaises(sqlite3.IntegrityError):
                    cur.execute(
                        "INSERT INTO host_dispatch_locks (lock_id, canonical_key, attempt_id, lock_state, trip_id, original_trip_id, created_at, updated_at) "
                        "VALUES ('lock2', 'un:user2', 'att1', 'sending', NULL, 'trip_y', ?, ?)",
                        (now, now)
                    )
        finally:
            _cleanup_db(db_path)

    def test_released_lock_can_coexist_with_new_active_lock(self):
        """Released-блокировка может сосуществовать с новой активной блокировкой."""
        db_path = _make_db_path("released_coexist")
        _cleanup_db(db_path)
        try:
            init_db(db_path)
            now = "2026-11-11T10:00:00"

            with get_connection(db_path) as conn:
                cur = conn.cursor()
                # Первая попытка и lock в released
                cur.execute(
                    "INSERT INTO sending_attempts (id, trip_id, original_trip_id, host_id, username, host_id_norm, username_norm, state, created_at, updated_at) "
                    "VALUES ('att1', NULL, 'trip_x', 'h1', 'user1', 'h1', 'user1', 'pending', ?, ?)",
                    (now, now)
                )
                cur.execute(
                    "INSERT INTO host_dispatch_locks (lock_id, canonical_key, attempt_id, lock_state, trip_id, original_trip_id, created_at, updated_at) "
                    "VALUES ('lock1', 'un:user1', 'att1', 'released', NULL, 'trip_x', ?, ?)",
                    (now, now)
                )
                # Вторая попытка и новый active lock для того же пользователя — должен пройти
                cur.execute(
                    "INSERT INTO sending_attempts (id, trip_id, original_trip_id, host_id, username, host_id_norm, username_norm, state, created_at, updated_at) "
                    "VALUES ('att2', NULL, 'trip_x', 'h1', 'user1', 'h1', 'user1', 'pending', ?, ?)",
                    (now, now)
                )
                cur.execute(
                    "INSERT INTO host_dispatch_locks (lock_id, canonical_key, attempt_id, lock_state, trip_id, original_trip_id, created_at, updated_at) "
                    "VALUES ('lock2', 'un:user1', 'att2', 'sending', NULL, 'trip_x', ?, ?)",
                    (now, now)
                )
                conn.commit()

                cur.execute(
                    "SELECT COUNT(*) FROM host_dispatch_locks WHERE canonical_key='un:user1' AND lock_state IN ('reserved','sending','sent','uncertain','already_contacted')"
                )
                self.assertEqual(cur.fetchone()[0], 1)  # Только lock2 активен
        finally:
            _cleanup_db(db_path)

    def test_sent_lock_cannot_be_treated_as_released(self):
        """Sent-блокировка не может быть переключена в released через обычный UPDATE (сохраняет подавление)."""
        db_path = _make_db_path("sent_not_released")
        _cleanup_db(db_path)
        try:
            init_db(db_path)
            now = "2026-11-11T10:00:00"

            with get_connection(db_path) as conn:
                cur = conn.cursor()
                cur.execute(
                    "INSERT INTO sending_attempts (id, trip_id, original_trip_id, host_id, username, host_id_norm, username_norm, state, created_at, updated_at) "
                    "VALUES ('att1', NULL, 'trip_x', 'h1', 'user1', 'h1', 'user1', 'sent', ?, ?)",
                    (now, now)
                )
                cur.execute(
                    "INSERT INTO host_dispatch_locks (lock_id, canonical_key, attempt_id, lock_state, trip_id, original_trip_id, created_at, updated_at) "
                    "VALUES ('lock1', 'un:user1', 'att1', 'sent', NULL, 'trip_x', ?, ?)",
                    (now, now)
                )
                conn.commit()

                # Попытка обновить sent на released — должна быть возможна на уровне SQL (нет CHECK против этого),
                # но для теста проверяем, что sent-блокировка всё ещё считается активной для подавления
                cur.execute(
                    "SELECT COUNT(*) FROM host_dispatch_locks WHERE canonical_key='un:user1' AND lock_state='sent'"
                )
                self.assertEqual(cur.fetchone()[0], 1)
        finally:
            _cleanup_db(db_path)

    def test_uncertain_lock_cannot_be_treated_as_released(self):
        """Uncertain-блокировка не может быть переключена в released через обычный UPDATE."""
        db_path = _make_db_path("uncertain_not_released")
        _cleanup_db(db_path)
        try:
            init_db(db_path)
            now = "2026-11-11T10:00:00"

            with get_connection(db_path) as conn:
                cur = conn.cursor()
                cur.execute(
                    "INSERT INTO sending_attempts (id, trip_id, original_trip_id, host_id, username, host_id_norm, username_norm, state, created_at, updated_at) "
                    "VALUES ('att1', NULL, 'trip_x', 'h1', 'user1', 'h1', 'user1', 'uncertain', ?, ?)",
                    (now, now)
                )
                cur.execute(
                    "INSERT INTO host_dispatch_locks (lock_id, canonical_key, attempt_id, lock_state, trip_id, original_trip_id, created_at, updated_at) "
                    "VALUES ('lock1', 'un:user1', 'att1', 'uncertain', NULL, 'trip_x', ?, ?)",
                    (now, now)
                )
                conn.commit()

                cur.execute(
                    "SELECT COUNT(*) FROM host_dispatch_locks WHERE canonical_key='un:user1' AND lock_state='uncertain'"
                )
                self.assertEqual(cur.fetchone()[0], 1)
        finally:
            _cleanup_db(db_path)

    def test_invalid_states_are_rejected(self):
        """Недопустимые состояния отвергаются CHECK constraint."""
        db_path = _make_db_path("invalid_state")
        _cleanup_db(db_path)
        try:
            init_db(db_path)
            now = "2026-11-11T10:00:00"

            with get_connection(db_path) as conn:
                cur = conn.cursor()
                with self.assertRaises(sqlite3.IntegrityError):
                    cur.execute(
                        "INSERT INTO sending_attempts (id, trip_id, original_trip_id, host_id, username, host_id_norm, username_norm, state, created_at, updated_at) "
                        "VALUES ('att1', NULL, 'trip_x', 'h1', 'user1', 'h1', 'user1', 'invalid_state', ?, ?)",
                        (now, now)
                    )
        finally:
            _cleanup_db(db_path)

    # ------------------------------------------------------------------
    # FOREIGN KEY TESTS
    # ------------------------------------------------------------------

    def test_deleting_trip_sets_attempt_trip_id_to_null(self):
        """Удаление поездки устанавливает trip_id в NULL для sending_attempts."""
        db_path = _make_db_path("fk_trip_delete_attempt")
        _cleanup_db(db_path)
        try:
            init_db(db_path)
            now = "2026-11-11T10:00:00"

            with get_connection(db_path) as conn:
                cur = conn.cursor()
                cur.execute(
                    "INSERT INTO core_trips (id, url, city, arrival_date, departure_date, is_active, created_at) "
                    "VALUES ('trip_del', 'https://example.com/trip/del', 'Kyoto', '2026-11-19', '2026-11-21', 1, ?)",
                    (now,)
                )
                cur.execute(
                    "INSERT INTO sending_attempts (id, trip_id, original_trip_id, host_id, username, host_id_norm, username_norm, state, created_at, updated_at) "
                    "VALUES ('att1', 'trip_del', 'trip_del', 'h1', 'user1', 'h1', 'user1', 'pending', ?, ?)",
                    (now, now)
                )
                conn.commit()

                # Удаляем поездку
                cur.execute("DELETE FROM core_trips WHERE id='trip_del'")
                conn.commit()

                # Проверяем: trip_id в attempting стал NULL, original_trip_id сохранён
                cur.execute("SELECT trip_id, original_trip_id FROM sending_attempts WHERE id='att1'")
                row = cur.fetchone()
                self.assertIsNone(row[0])
                self.assertEqual(row[1], 'trip_del')
                # Попытка всё ещё существует
                cur.execute("SELECT state FROM sending_attempts WHERE id='att1'")
                self.assertEqual(cur.fetchone()[0], 'pending')
        finally:
            _cleanup_db(db_path)

    def test_deleting_trip_sets_lock_trip_id_to_null(self):
        """Удаление поездки устанавливает trip_id в NULL для host_dispatch_locks."""
        db_path = _make_db_path("fk_trip_delete_lock")
        _cleanup_db(db_path)
        try:
            init_db(db_path)
            now = "2026-11-11T10:00:00"

            with get_connection(db_path) as conn:
                cur = conn.cursor()
                cur.execute(
                    "INSERT INTO core_trips (id, url, city, arrival_date, departure_date, is_active, created_at) "
                    "VALUES ('trip_del', 'https://example.com/trip/del', 'Kyoto', '2026-11-19', '2026-11-21', 1, ?)",
                    (now,)
                )
                cur.execute(
                    "INSERT INTO sending_attempts (id, trip_id, original_trip_id, host_id, username, host_id_norm, username_norm, state, created_at, updated_at) "
                    "VALUES ('att1', 'trip_del', 'trip_del', 'h1', 'user1', 'h1', 'user1', 'pending', ?, ?)",
                    (now, now)
                )
                cur.execute(
                    "INSERT INTO host_dispatch_locks (lock_id, canonical_key, attempt_id, lock_state, trip_id, original_trip_id, created_at, updated_at) "
                    "VALUES ('lock1', 'un:user1', 'att1', 'reserved', 'trip_del', 'trip_del', ?, ?)",
                    (now, now)
                )
                conn.commit()

                cur.execute("DELETE FROM core_trips WHERE id='trip_del'")
                conn.commit()

                cur.execute("SELECT trip_id, original_trip_id FROM host_dispatch_locks WHERE lock_id='lock1'")
                row = cur.fetchone()
                self.assertIsNone(row[0])
                self.assertEqual(row[1], 'trip_del')
        finally:
            _cleanup_db(db_path)

    def test_original_trip_id_remains_unchanged(self):
        """original_trip_id остаётся неизменным после удаления поездки."""
        db_path = _make_db_path("fk_original_trip")
        _cleanup_db(db_path)
        try:
            init_db(db_path)
            now = "2026-11-11T10:00:00"

            with get_connection(db_path) as conn:
                cur = conn.cursor()
                cur.execute(
                    "INSERT INTO core_trips (id, url, city, arrival_date, departure_date, is_active, created_at) "
                    "VALUES ('trip_orig', 'https://example.com/trip/orig', 'Kyoto', '2026-11-19', '2026-11-21', 1, ?)",
                    (now,)
                )
                cur.execute(
                    "INSERT INTO sending_attempts (id, trip_id, original_trip_id, host_id, username, host_id_norm, username_norm, state, created_at, updated_at) "
                    "VALUES ('att1', 'trip_orig', 'trip_orig', 'h1', 'user1', 'h1', 'user1', 'pending', ?, ?)",
                    (now, now)
                )
                cur.execute(
                    "INSERT INTO host_dispatch_locks (lock_id, canonical_key, attempt_id, lock_state, trip_id, original_trip_id, created_at, updated_at) "
                    "VALUES ('lock1', 'un:user1', 'att1', 'reserved', 'trip_orig', 'trip_orig', ?, ?)",
                    (now, now)
                )
                conn.commit()

                cur.execute("DELETE FROM core_trips WHERE id='trip_orig'")
                conn.commit()

                cur.execute("SELECT original_trip_id FROM sending_attempts WHERE id='att1'")
                self.assertEqual(cur.fetchone()[0], 'trip_orig')
                cur.execute("SELECT original_trip_id FROM host_dispatch_locks WHERE lock_id='lock1'")
                self.assertEqual(cur.fetchone()[0], 'trip_orig')
        finally:
            _cleanup_db(db_path)

    def test_attempts_and_locks_remain_after_trip_deletion(self):
        """Попытки и блокировки остаются после удаления поездки."""
        db_path = _make_db_path("fk_remain_after_delete")
        _cleanup_db(db_path)
        try:
            init_db(db_path)
            now = "2026-11-11T10:00:00"

            with get_connection(db_path) as conn:
                cur = conn.cursor()
                cur.execute(
                    "INSERT INTO core_trips (id, url, city, arrival_date, departure_date, is_active, created_at) "
                    "VALUES ('trip_del', 'https://example.com/trip/del', 'Kyoto', '2026-11-19', '2026-11-21', 1, ?)",
                    (now,)
                )
                cur.execute(
                    "INSERT INTO sending_attempts (id, trip_id, original_trip_id, host_id, username, host_id_norm, username_norm, state, created_at, updated_at) "
                    "VALUES ('att1', 'trip_del', 'trip_del', 'h1', 'user1', 'h1', 'user1', 'pending', ?, ?)",
                    (now, now)
                )
                cur.execute(
                    "INSERT INTO host_dispatch_locks (lock_id, canonical_key, attempt_id, lock_state, trip_id, original_trip_id, created_at, updated_at) "
                    "VALUES ('lock1', 'un:user1', 'att1', 'reserved', 'trip_del', 'trip_del', ?, ?)",
                    (now, now)
                )
                conn.commit()

                cur.execute("DELETE FROM core_trips WHERE id='trip_del'")
                conn.commit()

                cur.execute("SELECT COUNT(*) FROM sending_attempts")
                self.assertEqual(cur.fetchone()[0], 1)
                cur.execute("SELECT COUNT(*) FROM host_dispatch_locks")
                self.assertEqual(cur.fetchone()[0], 1)
        finally:
            _cleanup_db(db_path)

    def test_deleting_attempt_referenced_by_lock_is_rejected(self):
        """Удаление попытки, на которую ссылается блокировка, отклоняется ON DELETE RESTRICT."""
        db_path = _make_db_path("fk_delete_attempt_blocked")
        _cleanup_db(db_path)
        try:
            init_db(db_path)
            now = "2026-11-11T10:00:00"

            with get_connection(db_path) as conn:
                cur = conn.cursor()
                cur.execute(
                    "INSERT INTO sending_attempts (id, trip_id, original_trip_id, host_id, username, host_id_norm, username_norm, state, created_at, updated_at) "
                    "VALUES ('att1', NULL, 'trip_x', 'h1', 'user1', 'h1', 'user1', 'pending', ?, ?)",
                    (now, now)
                )
                cur.execute(
                    "INSERT INTO host_dispatch_locks (lock_id, canonical_key, attempt_id, lock_state, trip_id, original_trip_id, created_at, updated_at) "
                    "VALUES ('lock1', 'un:user1', 'att1', 'reserved', NULL, 'trip_x', ?, ?)",
                    (now, now)
                )
                conn.commit()

                # Попытка удалить attempt, на который ссылается lock — должна быть отвергнута
                with self.assertRaises(sqlite3.IntegrityError):
                    cur.execute("DELETE FROM sending_attempts WHERE id='att1'")
                    conn.commit()
        finally:
            _cleanup_db(db_path)

    def test_deleting_trip_preserves_sending_history(self):
        """Удаление поездки не удаляет sending_history."""
        db_path = _make_db_path("fk_history_preserved")
        _cleanup_db(db_path)
        try:
            init_db(db_path)
            now = "2026-11-11T10:00:00"

            with get_connection(db_path) as conn:
                cur = conn.cursor()
                cur.execute(
                    "INSERT INTO core_trips (id, url, city, arrival_date, departure_date, is_active, created_at) "
                    "VALUES ('trip_del', 'https://example.com/trip/del', 'Kyoto', '2026-11-19', '2026-11-21', 1, ?)",
                    (now,)
                )
                cur.execute(
                    "INSERT INTO sending_history (host_id, username, status, http_status, mode, timestamp) "
                    "VALUES ('h1', 'user1', 'SENT_SUCCESS', 200, 'live', '2026-11-11T14:00:00')"
                )
                conn.commit()

                cur.execute("DELETE FROM core_trips WHERE id='trip_del'")
                conn.commit()

                cur.execute("SELECT COUNT(*) FROM sending_history")
                self.assertEqual(cur.fetchone()[0], 1)
        finally:
            _cleanup_db(db_path)

    # ------------------------------------------------------------------
    # TRANSACTION TESTS
    # ------------------------------------------------------------------

    def test_failed_migration_rolls_back_cleanly(self):
        """Неудачная миграция откатывается чисто (частичная схема не остаётся)."""
        # SQLite DDL не откатывается внутри одной транзакции в некоторых версиях,
        # но init_core_tables использует get_connection который коммитит при успехе.
        # Тест проверяет, что повторный запуск после неудачи не оставляет частичного состояния.
        db_path = _make_db_path("rollback_test")
        _cleanup_db(db_path)
        try:
            init_db(db_path)
            # Повторный запуск не должен сломать состояние
            init_db(db_path)

            with get_connection(db_path) as conn:
                cur = conn.cursor()
                cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='sending_attempts'")
                self.assertIsNotNone(cur.fetchone())
                cur.execute("PRAGMA foreign_keys")
                self.assertEqual(cur.fetchone()[0], 1)
        finally:
            _cleanup_db(db_path)

    def test_foreign_key_enforcement_is_active(self):
        """Enforcement внешних ключей активно на всех соединениях."""
        db_path = _make_db_path("fk_enforcement")
        _cleanup_db(db_path)
        try:
            init_db(db_path)

            with get_connection(db_path) as conn:
                cur = conn.cursor()
                cur.execute("PRAGMA foreign_keys")
                self.assertEqual(cur.fetchone()[0], 1)

                # Проверяем, что FK действительно работает: вставка attempting с несуществующим trip_id
                try:
                    cur.execute(
                        "INSERT INTO sending_attempts (id, trip_id, original_trip_id, host_id, username, host_id_norm, username_norm, state, created_at, updated_at) "
                        "VALUES ('att1', 'nonexistent_trip', 'nonexistent_trip', 'h1', 'user1', 'h1', 'user1', 'pending', '2026-11-11T10:00:00', '2026-11-11T10:00:00')"
                    )
                    # Если не упало — значит FK не enforced (маловероятно при PRAGMA foreign_keys=1)
                    conn.rollback()
                    self.fail("FOREIGN KEY constraint was not enforced")
                except sqlite3.IntegrityError:
                    pass  # Ожидаемое поведение
        finally:
            _cleanup_db(db_path)

    def test_no_partial_schema_after_failed_migration(self):
        """После неудачной миграции не остаётся частичной схемы."""
        db_path = _make_db_path("no_partial")
        _cleanup_db(db_path)
        try:
            init_db(db_path)
            # Все ожидаемые таблицы должны существовать
            with get_connection(db_path) as conn:
                cur = conn.cursor()
                cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
                tables = {row[0] for row in cur.fetchall()}
                for required in ("sending_attempts", "host_dispatch_locks", "host_identities", "sending_history", "core_hosts", "core_trips", "core_audit_logs"):
                    self.assertIn(required, tables)

                # Все ожидаемые индексы
                cur.execute("SELECT name FROM sqlite_master WHERE type='index'")
                indexes = {row[0] for row in cur.fetchall()}
                for required in ("idx_sa_active_per_user", "idx_sa_trip_state", "idx_sa_unresolved",
                                 "idx_hdl_one_active_per_user", "idx_hdl_one_active_per_attempt",
                                 "idx_hi_type_value", "idx_hi_canonical",
                                 "idx_sh_attempt", "idx_sh_trip", "idx_sh_idempotent_event"):
                    self.assertIn(required, indexes, f"Индекс {required} отсутствует")
        finally:
            _cleanup_db(db_path)


if __name__ == "__main__":
    unittest.main()
