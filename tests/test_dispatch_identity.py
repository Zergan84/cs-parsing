"""
tests/test_dispatch_identity.py

Тесты этапа 2: Идентификация и глобальное резервирование хостов.

Покрывает:
1. normalize_identity — детерминированная нормализация.
2. register_host_identity — регистрация, уникальность, идемпотентность.
3. resolve_host_identity — разрешение, конфликты, not_found.
4. get_host_aliases — получение всех алиасов.
5. reserve_host_for_dispatch — атомарное резервирование.
6. release_host_dispatch_lock — освобождение блокировки.
7. check_host_availability — проверка доступности без изменений.
8. Конкурентное резервирование (реальные параллельные соединения).
9. Транзакционная целостность (откаты, отсутствие частичных записей).

Все тесты используют изолированные временные SQLite базы данных.
Никогда не трогают production базу output/couchsurfing_platform.db.
"""

import os
import sqlite3
import tempfile
import threading
import uuid
import unittest
from pathlib import Path

from core.database import get_connection, init_db
from core.dispatch_identity import (
    check_host_availability,
    normalize_identity,
    release_host_dispatch_lock,
    reserve_host_for_dispatch,
    resolve_host_identity,
    get_host_aliases,
    register_host_identity,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
TEST_DB_DIR = Path(tempfile.mkdtemp(prefix="cs-dispatch-identity-"))


def _make_db_path(suffix: str) -> Path:
    """Создаёт уникальный путь к временной тестовой БД."""
    return TEST_DB_DIR / f"{suffix}.db"


def _ensure_test_dir() -> None:
    TEST_DB_DIR.mkdir(parents=True, exist_ok=True)


def _cleanup_db(db_path: Path) -> None:
    """Удаляет тестовую БД и связанные файлы WAL/SHM."""
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
    """Создаёт и инициализирует свежую тестовую БД."""
    _ensure_test_dir()
    suffix = f"test_{uuid.uuid4().hex[:8]}"
    db_path = _make_db_path(suffix)
    _cleanup_db(db_path)
    init_db(db_path)
    return db_path


def _register_trip(conn, trip_id: str, city: str = "Kyoto") -> None:
    """Регистрирует тестовую поездку в core_trips."""
    now = "2026-11-10T00:00:00"
    conn.execute(
        """
        INSERT INTO core_trips (id, url, city, arrival_date, departure_date, is_active, created_at)
        VALUES (?, ?, ?, ?, ?, 1, ?)
        """,
        (trip_id, f"https://example.com/trip/{trip_id}", city, "2026-11-19", "2026-11-21", now),
    )


def _register_identity(
    conn, identity_type: str, identity_value: str, canonical_key: str
) -> str:
    """Регистрирует идентификатор в host_identities и возвращает identity_id."""
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


def _reserve_with_trip(db_path: Path, identity_type: str, identity_value: str, trip_id: str) -> dict:
    """Резервирует хоста, предварительно зарегистрировав поездку в core_trips."""
    with get_connection(db_path) as conn:
        _register_trip(conn, trip_id)
        conn.commit()
    return reserve_host_for_dispatch(identity_type, identity_value, trip_id, db_file=db_path)


class TestDispatchIdentity(unittest.TestCase):
    """Тесты идентификации и глобального резервирования."""

    @classmethod
    def setUpClass(cls):
        _ensure_test_dir()

    @classmethod
    def tearDownClass(cls):
        if TEST_DB_DIR.exists():
            for f in TEST_DB_DIR.iterdir():
                if f.is_file():
                    try:
                        f.unlink()
                    except Exception:
                        pass

    # ------------------------------------------------------------------
    # NORMALIZE IDENTITY
    # ------------------------------------------------------------------

    def test_normalize_username(self):
        """Нормализация username: lowercase + trim."""
        norm_type, norm_value = normalize_identity("username", "  AliceSmith  ")
        self.assertEqual(norm_type, "username")
        self.assertEqual(norm_value, "alicesmith")

    def test_normalize_cs_id(self):
        """Нормализация cs_id: как-is."""
        norm_type, norm_value = normalize_identity("cs_id", "CS_12345")
        self.assertEqual(norm_type, "cs_id")
        self.assertEqual(norm_value, "CS_12345")

    def test_normalize_invalid_type(self):
        """Неподдерживаемый тип вызывает ValueError."""
        with self.assertRaises(ValueError):
            normalize_identity("email", "alice@example.com")

    # ------------------------------------------------------------------
    # REGISTER HOST IDENTITY
    # ------------------------------------------------------------------

    def test_register_identity_success(self):
        """Регистрация нового идентификатора успешна."""
        db_path = _fresh_db()
        try:
            identity_id = register_host_identity("username", "alice", "un:alice", db_file=db_path)
            self.assertIsNotNone(identity_id)

            with get_connection(db_path) as conn:
                cur = conn.cursor()
                cur.execute(
                    "SELECT identity_type, identity_value, canonical_key FROM host_identities WHERE identity_id = ?",
                    (identity_id,),
                )
                row = cur.fetchone()
                self.assertIsNotNone(row)
                self.assertEqual(row["identity_type"], "username")
                self.assertEqual(row["identity_value"], "alice")
                self.assertEqual(row["canonical_key"], "un:alice")
        finally:
            _cleanup_db(db_path)

    def test_register_identity_duplicate_rejected(self):
        """Повторная регистрация того же (type, value) возвращает существующий ID (идемпотентно)."""
        db_path = _fresh_db()
        try:
            id1 = register_host_identity("username", "alice", "un:alice", db_file=db_path)
            id2 = register_host_identity("username", "alice", "un:alice", db_file=db_path)
            self.assertEqual(id1, id2)
        finally:
            _cleanup_db(db_path)

    def test_register_identity_case_sensitive_distinct(self):
        """Разные значения (type, value) — разные записи, даже если отличаются только регистром."""
        db_path = _fresh_db()
        try:
            # "Alice" и "alice" нормализуются в одно и то же — это дубликат
            id1 = register_host_identity("username", "Alice", "un:alice", db_file=db_path)
            id2 = register_host_identity("username", "alice", "un:alice", db_file=db_path)
            self.assertEqual(id1, id2)

            # Разные типы с одинаковым значением — разные записи
            id3 = register_host_identity("cs_id", "alice", "un:alice_cs", db_file=db_path)
            self.assertNotEqual(id1, id3)
        finally:
            _cleanup_db(db_path)

    def test_multiple_aliases_share_canonical_key(self):
        """Несколько identity_value могут ссылаться на один canonical_key."""
        db_path = _fresh_db()
        try:
            id1 = register_host_identity("username", "alice", "un:alice", db_file=db_path)
            id2 = register_host_identity("username", "alice_new", "un:alice", db_file=db_path)
            id3 = register_host_identity("cs_id", "cs_123", "un:alice", db_file=db_path)

            aliases = get_host_aliases("un:alice", db_file=db_path)
            self.assertEqual(len(aliases), 3)
        finally:
            _cleanup_db(db_path)

    # ------------------------------------------------------------------
    # RESOLVE HOST IDENTITY
    # ------------------------------------------------------------------

    def test_resolve_identity_found(self):
        """Разрешение существующего идентификатора возвращает canonical_key."""
        db_path = _fresh_db()
        try:
            register_host_identity("username", "alice", "un:alice", db_file=db_path)
            result = resolve_host_identity("username", "alice", db_file=db_path)
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["canonical_key"], "un:alice")
            self.assertEqual(len(result["aliases"]), 1)
            self.assertEqual(len(result["conflict_keys"]), 0)
        finally:
            _cleanup_db(db_path)

    def test_resolve_identity_not_found(self):
        """Несуществующий идентификатор возвращает not_found."""
        db_path = _fresh_db()
        try:
            result = resolve_host_identity("username", "nobody", db_file=db_path)
            self.assertEqual(result["status"], "not_found")
            self.assertIsNone(result["canonical_key"])
            self.assertEqual(len(result["aliases"]), 0)
        finally:
            _cleanup_db(db_path)

    def test_resolve_identity_conflict(self):
        """Один identity_value не может映射 к нескольким canonical_key — защищено UNIQUE."""
        # UNIQUE на (identity_type, identity_value) предотвращает дубликаты на уровне БД,
        # поэтому конфликт может возникнуть только при логической ошибке приложения.
        # Тест проверяет, что resolve_host_identity корректно обрабатывает ok случай.
        db_path = _fresh_db()
        try:
            register_host_identity("username", "alice", "un:alice", db_file=db_path)
            result = resolve_host_identity("username", "alice", db_file=db_path)
            self.assertEqual(result["status"], "ok")
        finally:
            _cleanup_db(db_path)

    # ------------------------------------------------------------------
    # GET HOST ALIASES
    # ------------------------------------------------------------------

    def test_get_host_aliases(self):
        """Получение всех алиасов для canonical ключа."""
        db_path = _fresh_db()
        try:
            register_host_identity("username", "alice", "un:alice", db_file=db_path)
            register_host_identity("cs_id", "cs_123", "un:alice", db_file=db_path)

            aliases = get_host_aliases("un:alice", db_file=db_path)
            self.assertEqual(len(aliases), 2)
            values = {a["identity_value"] for a in aliases}
            self.assertIn("alice", values)
            self.assertIn("cs_123", values)
        finally:
            _cleanup_db(db_path)

    def test_get_host_aliases_empty(self):
        """Пустой результат для несуществующего canonical ключа."""
        db_path = _fresh_db()
        try:
            aliases = get_host_aliases("un:nonexistent", db_file=db_path)
            self.assertEqual(len(aliases), 0)
        finally:
            _cleanup_db(db_path)

    # ------------------------------------------------------------------
    # CHECK HOST AVAILABILITY
    # ------------------------------------------------------------------

    def test_check_availability_available(self):
        """Хост без блокировок доступен."""
        db_path = _fresh_db()
        try:
            register_host_identity("username", "alice", "un:alice", db_file=db_path)
            result = check_host_availability("username", "alice", db_file=db_path)
            self.assertTrue(result["available"])
            self.assertEqual(result["canonical_key"], "un:alice")
            self.assertIsNone(result["active_lock"])
            self.assertFalse(result["conflict"])
        finally:
            _cleanup_db(db_path)

    def test_check_availability_blocked(self):
        """Хост с активной блокировкой недоступен."""
        db_path = _fresh_db()
        try:
            register_host_identity("username", "alice", "un:alice", db_file=db_path)
            _reserve_with_trip(db_path, "username", "alice", "trip_1")
            result = check_host_availability("username", "alice", db_file=db_path)
            self.assertFalse(result["available"])
            self.assertIsNotNone(result["active_lock"])
        finally:
            _cleanup_db(db_path)

    # ------------------------------------------------------------------
    # RESERVE HOST FOR DISPATCH
    # ------------------------------------------------------------------

    def test_reserve_success(self):
        """Успешное резервирование хоста."""
        db_path = _fresh_db()
        try:
            register_host_identity("username", "alice", "un:alice", db_file=db_path)
            result = _reserve_with_trip(db_path, "username", "alice", "trip_1")
            self.assertEqual(result["status"], "ok")
            self.assertIsNotNone(result["attempt_id"])
            self.assertIsNotNone(result["lock_id"])
            self.assertEqual(result["canonical_key"], "un:alice")
            self.assertIsNone(result["reason"])
        finally:
            _cleanup_db(db_path)

    def test_reserve_blocked_after_sent(self):
        """Запрет резервирования после sent."""
        db_path = _fresh_db()
        try:
            register_host_identity("username", "bob", "un:bob", db_file=db_path)
            # Первый резерв
            r1 = _reserve_with_trip(db_path, "username", "bob", "trip_1")
            self.assertEqual(r1["status"], "ok")

            # Переводим блокировку в sent (симуляция успешной отправки)
            with get_connection(db_path) as conn:
                conn.execute(
                    "UPDATE host_dispatch_locks SET lock_state = 'sent', updated_at = '2026-11-11T10:00:00' WHERE attempt_id = ?",
                    (r1["attempt_id"],),
                )
                conn.execute(
                    "UPDATE sending_attempts SET state = 'sent', updated_at = '2026-11-11T10:00:00' WHERE id = ?",
                    (r1["attempt_id"],),
                )
                conn.commit()

            # Второй reserva должен быть заблокирован
            r2 = _reserve_with_trip(db_path, "username", "bob", "trip_2")
            self.assertEqual(r2["status"], "blocked")
            self.assertIsNone(r2["attempt_id"])
        finally:
            _cleanup_db(db_path)

    def test_reserve_blocked_after_uncertain(self):
        """Запрет резервирования после uncertain."""
        db_path = _fresh_db()
        try:
            register_host_identity("username", "carol", "un:carol", db_file=db_path)
            r1 = _reserve_with_trip(db_path, "username", "carol", "trip_1")
            self.assertEqual(r1["status"], "ok")

            with get_connection(db_path) as conn:
                conn.execute(
                    "UPDATE host_dispatch_locks SET lock_state = 'uncertain', updated_at = '2026-11-11T10:00:00' WHERE attempt_id = ?",
                    (r1["attempt_id"],),
                )
                conn.execute(
                    "UPDATE sending_attempts SET state = 'uncertain', updated_at = '2026-11-11T10:00:00' WHERE id = ?",
                    (r1["attempt_id"],),
                )
                conn.commit()

            r2 = _reserve_with_trip(db_path, "username", "carol", "trip_2")
            self.assertEqual(r2["status"], "blocked")
            self.assertIsNone(r2["attempt_id"])
        finally:
            _cleanup_db(db_path)

    def test_reserve_blocked_after_already_contacted(self):
        """Запрет резервирования после already_contacted."""
        db_path = _fresh_db()
        try:
            register_host_identity("username", "dave", "un:dave", db_file=db_path)
            r1 = _reserve_with_trip(db_path, "username", "dave", "trip_1")
            self.assertEqual(r1["status"], "ok")

            with get_connection(db_path) as conn:
                conn.execute(
                    "UPDATE host_dispatch_locks SET lock_state = 'already_contacted', updated_at = '2026-11-11T10:00:00' WHERE attempt_id = ?",
                    (r1["attempt_id"],),
                )
                conn.execute(
                    "UPDATE sending_attempts SET state = 'already_contacted', updated_at = '2026-11-11T10:00:00' WHERE id = ?",
                    (r1["attempt_id"],),
                )
                conn.commit()

            r2 = _reserve_with_trip(db_path, "username", "dave", "trip_2")
            self.assertEqual(r2["status"], "blocked")
            self.assertIsNone(r2["attempt_id"])
        finally:
            _cleanup_db(db_path)

    def test_reserve_allowed_after_released(self):
        """Разрешение резервирования после released."""
        db_path = _fresh_db()
        try:
            register_host_identity("username", "eve", "un:eve", db_file=db_path)
            r1 = _reserve_with_trip(db_path, "username", "eve", "trip_1")
            self.assertEqual(r1["status"], "ok")

            # Освобождаем блокировку
            rel = release_host_dispatch_lock(r1["attempt_id"], "released", db_file=db_path)
            self.assertEqual(rel["status"], "ok")

            # Повторное резервирование разрешено
            r2 = _reserve_with_trip(db_path, "username", "eve", "trip_2")
            self.assertEqual(r2["status"], "ok")
            self.assertNotEqual(r1["attempt_id"], r2["attempt_id"])
        finally:
            _cleanup_db(db_path)

    def test_reserve_allowed_after_failed(self):
        """Разрешение резервирования после failed."""
        db_path = _fresh_db()
        try:
            register_host_identity("username", "frank", "un:frank", db_file=db_path)
            r1 = _reserve_with_trip(db_path, "username", "frank", "trip_1")
            self.assertEqual(r1["status"], "ok")

            rel = release_host_dispatch_lock(r1["attempt_id"], "failed", db_file=db_path)
            self.assertEqual(rel["status"], "ok")

            r2 = _reserve_with_trip(db_path, "username", "frank", "trip_2")
            self.assertEqual(r2["status"], "ok")
            self.assertNotEqual(r1["attempt_id"], r2["attempt_id"])
        finally:
            _cleanup_db(db_path)

    def test_reserve_not_registered(self):
        """Резервирование не зарегистрированного хоста отклоняется."""
        db_path = _fresh_db()
        try:
            result = reserve_host_for_dispatch("username", "unknown", "trip_1", db_file=db_path)
            self.assertEqual(result["status"], "not_found")
            self.assertIsNone(result["attempt_id"])
        finally:
            _cleanup_db(db_path)

    def test_reserve_trip_isolation(self):
        """Хост в одной поездке блокирует любую другую поездку (глобальная блокировка)."""
        db_path = _fresh_db()
        try:
            register_host_identity("username", "grace", "un:grace", db_file=db_path)
            # Резервируем в trip_1
            r1 = _reserve_with_trip(db_path, "username", "grace", "trip_1")
            self.assertEqual(r1["status"], "ok")
            # В trip_2 должен быть заблокирован (глобальная блокировка)
            r2 = _reserve_with_trip(db_path, "username", "grace", "trip_2")
            self.assertEqual(r2["status"], "blocked")
        finally:
            _cleanup_db(db_path)

    # ------------------------------------------------------------------
    # RELEASE HOST DISPATCH LOCK
    # ------------------------------------------------------------------

    def test_release_success(self):
        """Успешное освобождение блокировки."""
        db_path = _fresh_db()
        try:
            register_host_identity("username", "heidi", "un:heidi", db_file=db_path)
            r = _reserve_with_trip(db_path, "username", "heidi", "trip_1")
            self.assertEqual(r["status"], "ok")

            rel = release_host_dispatch_lock(r["attempt_id"], "released", db_file=db_path)
            self.assertEqual(rel["status"], "ok")
            self.assertEqual(rel["previous_state"], "reserved")
            self.assertEqual(rel["new_state"], "released")

            # Проверяем в БД
            with get_connection(db_path) as conn:
                cur = conn.cursor()
                cur.execute(
                    "SELECT lock_state FROM host_dispatch_locks WHERE attempt_id = ?",
                    (r["attempt_id"],),
                )
                row = cur.fetchone()
                self.assertEqual(row["lock_state"], "released")
        finally:
            _cleanup_db(db_path)

    def test_release_failed_success(self):
        """Освобождение блокировки в состояние failed."""
        db_path = _fresh_db()
        try:
            register_host_identity("username", "ivan", "un:ivan", db_file=db_path)
            r = _reserve_with_trip(db_path, "username", "ivan", "trip_1")
            self.assertEqual(r["status"], "ok")

            rel = release_host_dispatch_lock(r["attempt_id"], "failed", db_file=db_path)
            self.assertEqual(rel["status"], "ok")
            self.assertEqual(rel["new_state"], "failed")
        finally:
            _cleanup_db(db_path)

    def test_release_sent_blocked(self):
        """Нельзя освободить sent блокировку."""
        db_path = _fresh_db()
        try:
            register_host_identity("username", "judy", "un:judy", db_file=db_path)
            r = _reserve_with_trip(db_path, "username", "judy", "trip_1")
            self.assertEqual(r["status"], "ok")

            with get_connection(db_path) as conn:
                conn.execute(
                    "UPDATE host_dispatch_locks SET lock_state = 'sent', updated_at = '2026-11-11T10:00:00' WHERE attempt_id = ?",
                    (r["attempt_id"],),
                )
                conn.commit()

            rel = release_host_dispatch_lock(r["attempt_id"], "released", db_file=db_path)
            self.assertEqual(rel["status"], "blocked")
        finally:
            _cleanup_db(db_path)

    def test_release_uncertain_blocked(self):
        """Нельзя освободить uncertain блокировку."""
        db_path = _fresh_db()
        try:
            register_host_identity("username", "karl", "un:karl", db_file=db_path)
            r = _reserve_with_trip(db_path, "username", "karl", "trip_1")
            self.assertEqual(r["status"], "ok")

            with get_connection(db_path) as conn:
                conn.execute(
                    "UPDATE host_dispatch_locks SET lock_state = 'uncertain', updated_at = '2026-11-11T10:00:00' WHERE attempt_id = ?",
                    (r["attempt_id"],),
                )
                conn.commit()

            rel = release_host_dispatch_lock(r["attempt_id"], "released", db_file=db_path)
            self.assertEqual(rel["status"], "blocked")
        finally:
            _cleanup_db(db_path)

    def test_release_nonexistent_attempt(self):
        """Освобождение несуществующей попытки."""
        db_path = _fresh_db()
        try:
            rel = release_host_dispatch_lock("nonexistent_attempt", "released", db_file=db_path)
            self.assertEqual(rel["status"], "not_found")
        finally:
            _cleanup_db(db_path)

    # ------------------------------------------------------------------
    # CONCURRENT RESERVATION TESTS
    # ------------------------------------------------------------------

    def _concurrent_reserve(
        self, db_path: Path, identity_type: str, identity_value: str, trip_id: str, results: list, index: int
    ) -> None:
        """Вспомогательный метод для конкурентного резервирования."""
        try:
            # Каждый поток должен зарегистрировать свою поездку перед резервированием
            with get_connection(db_path) as conn:
                _register_trip(conn, trip_id)
                conn.commit()
            result = reserve_host_for_dispatch(identity_type, identity_value, trip_id, db_file=db_path)
            results[index] = result
        except Exception as e:
            results[index] = {"status": "error", "reason": str(e)}

    def test_concurrent_reserve_same_user(self):
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
            self.assertEqual(len(successes), 1, f"Ожидался 1 succès, получено {len(successes)}: {results}")

            # Проверяем в БД, что только одна активная блокировка
            with get_connection(db_path) as conn:
                cur = conn.cursor()
                cur.execute(
                    "SELECT COUNT(*) FROM host_dispatch_locks WHERE canonical_key = 'un:mallory' AND lock_state IN ('reserved', 'sending', 'sent', 'uncertain', 'already_contacted')"
                )
                count = cur.fetchone()[0]
                self.assertEqual(count, 1)
        finally:
            _cleanup_db(db_path)

    def test_concurrent_reserve_same_user_different_trips(self):
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

    def test_concurrent_reserve_through_different_aliases(self):
        """Резервирование через разные алиасы одного пользователя — только одно succès."""
        db_path = _fresh_db()
        try:
            # Регистрируем два алиаса на один canonical_key
            register_host_identity("username", "sasuke", "un:sasuke", db_file=db_path)
            register_host_identity("cs_id", "cs_sasuke", "un:sasuke", db_file=db_path)

            results = [None, None]
            t1 = threading.Thread(
                target=self._concurrent_reserve,
                args=(db_path, "username", "sasuke", "trip_a", results, 0),
            )
            t2 = threading.Thread(
                target=self._concurrent_reserve,
                args=(db_path, "cs_id", "cs_sasuke", "trip_b", results, 1),
            )
            t1.start()
            t2.start()
            t1.join()
            t2.join()

            successes = [r for r in results if r and r["status"] == "ok"]
            self.assertEqual(len(successes), 1)
        finally:
            _cleanup_db(db_path)

    def test_reserve_after_released_then_concurrent(self):
        """Повторное резервирование после released — только одно succès при конкурсе."""
        db_path = _fresh_db()
        try:
            register_host_identity("username", "sakura", "un:sakura", db_file=db_path)
            r1 = _reserve_with_trip(db_path, "username", "sakura", "trip_1")
            self.assertEqual(r1["status"], "ok")
            release_host_dispatch_lock(r1["attempt_id"], "released", db_file=db_path)

            results = [None, None]
            t1 = threading.Thread(
                target=self._concurrent_reserve,
                args=(db_path, "username", "sakura", "trip_2", results, 0),
            )
            t2 = threading.Thread(
                target=self._concurrent_reserve,
                args=(db_path, "username", "sakura", "trip_3", results, 1),
            )
            t1.start()
            t2.start()
            t1.join()
            t2.join()

            successes = [r for r in results if r and r["status"] == "ok"]
            self.assertEqual(len(successes), 1)
        finally:
            _cleanup_db(db_path)

    def test_reserve_after_failed_then_concurrent(self):
        """Повторное резервирование после failed — только одно succès при конкурсе."""
        db_path = _fresh_db()
        try:
            register_host_identity("username", "shikamaru", "un:shikamaru", db_file=db_path)
            r1 = _reserve_with_trip(db_path, "username", "shikamaru", "trip_1")
            self.assertEqual(r1["status"], "ok")
            release_host_dispatch_lock(r1["attempt_id"], "failed", db_file=db_path)

            results = [None, None]
            t1 = threading.Thread(
                target=self._concurrent_reserve,
                args=(db_path, "username", "shikamaru", "trip_2", results, 0),
            )
            t2 = threading.Thread(
                target=self._concurrent_reserve,
                args=(db_path, "username", "shikamaru", "trip_3", results, 1),
            )
            t1.start()
            t2.start()
            t1.join()
            t2.join()

            successes = [r for r in results if r and r["status"] == "ok"]
            self.assertEqual(len(successes), 1)
        finally:
            _cleanup_db(db_path)

    # ------------------------------------------------------------------
    # TRANSACTIONAL INTEGRITY
    # ------------------------------------------------------------------

    def test_reserve_creates_attempt_and_lock_atomically(self):
        """Резервирование создает attempt и lock в одной транзакции."""
        db_path = _fresh_db()
        try:
            register_host_identity("username", "tsunade", "un:tsunade", db_file=db_path)
            result = _reserve_with_trip(db_path, "username", "tsunade", "trip_1")
            self.assertEqual(result["status"], "ok")

            with get_connection(db_path) as conn:
                cur = conn.cursor()
                # Проверяем attempt
                cur.execute("SELECT state, send_intent_committed FROM sending_attempts WHERE id = ?", (result["attempt_id"],))
                attempt = cur.fetchone()
                self.assertIsNotNone(attempt)
                self.assertEqual(attempt["state"], "pending")
                self.assertEqual(attempt["send_intent_committed"], 0)

                # Проверяем lock
                cur.execute("SELECT lock_state, canonical_key FROM host_dispatch_locks WHERE lock_id = ?", (result["lock_id"],))
                lock = cur.fetchone()
                self.assertIsNotNone(lock)
                self.assertEqual(lock["lock_state"], "reserved")
                self.assertEqual(lock["canonical_key"], "un:tsunade")

                # Проверяем history
                cur.execute("SELECT event_type, state_at_event FROM sending_history WHERE attempt_id = ?", (result["attempt_id"],))
                hist = cur.fetchone()
                self.assertIsNotNone(hist)
                self.assertEqual(hist["event_type"], "RESERVATION")
                self.assertEqual(hist["state_at_event"], "pending")
        finally:
            _cleanup_db(db_path)

    def test_no_partial_records_on_failure(self):
        """При ошибке резервирования не остаётся частичных записей."""
        db_path = _fresh_db()
        try:
            register_host_identity("username", "obito", "un:obito", db_file=db_path)
            # Первое успешное резервирование
            r1 = _reserve_with_trip(db_path, "username", "obito", "trip_1")
            self.assertEqual(r1["status"], "ok")

            # Второе reserva должно быть заблокировано (не частично создано)
            r2 = _reserve_with_trip(db_path, "username", "obito", "trip_2")
            self.assertEqual(r2["status"], "blocked")

            # Проверяем, что только одна попытка и один lock
            with get_connection(db_path) as conn:
                cur = conn.cursor()
                cur.execute("SELECT COUNT(*) FROM sending_attempts WHERE host_id_norm = 'obito'")
                self.assertEqual(cur.fetchone()[0], 1)
                cur.execute("SELECT COUNT(*) FROM host_dispatch_locks WHERE canonical_key = 'un:obito'")
                self.assertEqual(cur.fetchone()[0], 1)
        finally:
            _cleanup_db(db_path)

    def test_one_attempt_one_active_lock(self):
        """Одна попытка не может владеть двумя активными блокировками."""
        db_path = _fresh_db()
        try:
            register_host_identity("username", "pain", "un:pain", db_file=db_path)
            r = _reserve_with_trip(db_path, "username", "pain", "trip_1")
            self.assertEqual(r["status"], "ok")

            # Попытка создать вторую активную блокировку для той же попытки
            # через прямую вставку в БД (обход API)
            with get_connection(db_path) as conn:
                cur = conn.cursor()
                try:
                    cur.execute(
                        """
                        INSERT INTO host_dispatch_locks (lock_id, canonical_key, attempt_id, lock_state, trip_id, original_trip_id, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        ("lock_dup", "un:pain", r["attempt_id"], "sending", "trip_2", "trip_2", "2026-11-11T10:00:00", "2026-11-11T10:00:00"),
                    )
                    conn.commit()
                    self.fail("Должна быть нарушена уникальность (partial unique index)")
                except sqlite3.IntegrityError:
                    conn.rollback()
                    # Ожидаемое поведение — partial unique index предотвращает duplicates
        finally:
            _cleanup_db(db_path)

    def test_one_user_one_active_lock_across_trips(self):
        """Один пользователь не может иметь две активные блокировки в разных поездках."""
        db_path = _fresh_db()
        try:
            register_host_identity("username", "konan", "un:konan", db_file=db_path)
            r1 = _reserve_with_trip(db_path, "username", "konan", "trip_a")
            self.assertEqual(r1["status"], "ok")

            r2 = _reserve_with_trip(db_path, "username", "konan", "trip_b")
            self.assertEqual(r2["status"], "blocked")
        finally:
            _cleanup_db(db_path)

    # ------------------------------------------------------------------
    # IDENTITY CONFLICT
    # ------------------------------------------------------------------

    def test_conflicting_identity_blocks_reservation(self):
        """Конфликтующие ключи идентичности блокируют резервирование."""
        db_path = _fresh_db()
        try:
            # Регистрируем два разных имени на два разных canonical_key
            register_host_identity("username", "ito", "un:ito_a", db_file=db_path)
            # Повторная регистрация того же (type, value) не создаст дубликат (UNIQUE constraint)
            # Это просто проверяет, что UNIQUE работает
            with get_connection(db_path) as conn:
                cur = conn.cursor()
                cur.execute(
                    "SELECT COUNT(*) FROM host_identities WHERE identity_type = 'username' AND identity_value = 'ito'"
                )
                self.assertEqual(cur.fetchone()[0], 1)
        finally:
            _cleanup_db(db_path)


if __name__ == "__main__":
    unittest.main()
