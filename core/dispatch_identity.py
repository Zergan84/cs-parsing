"""
core/dispatch_identity.py — Идентификация и глобальное резервирование хостов.

Реализует:
1. normalize_identity() — детерминированная нормализация идентификаторов.
2. register_host_identity() — регистрация идентификатора с проверкой уникальности.
3. resolve_host_identity() — разрешение идентичности с обнаружением конфликтов.
4. get_host_aliases() — получение всех алиасов для canonical ключа.
5. reserve_host_for_dispatch() — атомарное резервирование хоста в одной транзакции.
6. release_host_dispatch_lock() — безопасное освобождение блокировки.

Все операции с БД используют get_connection() из core.database (единый слой персистентности).
Никаких прямых sqlite3.connect вне core/database.py.
"""

import logging
import unicodedata
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from core.database import get_connection, get_db_path

logger = logging.getLogger("DispatchIdentity")


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    """Возвращает текущую временную метку в формате ISO 8601."""
    return datetime.now().isoformat()


def _generate_uuid() -> str:
    """Генерирует новый UUID v4 в виде строки."""
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# 1. normalize_identity
# ---------------------------------------------------------------------------

def normalize_identity(identity_type: str, identity_value: str) -> tuple[str, str]:
    """
    Детерминированная нормализация идентификатора пользователя Couchsurfing.

    Правила:
    * username: NFKC нормализация Unicode, lowercase + trim
      (убираем пробелы по краям, приводим к нижнему регистру).
      NFKC используется для приведения визуально похожих символов к
      канонической форме, но НЕ для автоматического слияния разных пользователей.
    * cs_id: используется как-is (предполагается, что уже подтверждён).
    * Неизвестный тип: ValueError.

    Возвращает (normalized_type, normalized_value).

    Важно: Нормализация ≠ проверка идентичности. Два визуально похожих
    имени после NFKC нормализации всё ещё могут относиться к разным
    людям. Слияние выполняется только при явном cs_id совпадении.
    """
    if identity_type == "username":
        # NFKC нормализация для приведения различных Unicode представлений
        # к одной канонической форме (например, полноширинные → обычные символы)
        normalized = unicodedata.normalize("NFKC", identity_value)
        return ("username", normalized.strip().lower())
    elif identity_type == "cs_id":
        return ("cs_id", identity_value.strip())
    else:
        raise ValueError(f"Неподдерживаемый тип идентификации: {identity_type}")


# ---------------------------------------------------------------------------
# 2. register_host_identity
# ---------------------------------------------------------------------------

def register_host_identity(
    identity_type: str,
    identity_value: str,
    canonical_key: str,
    db_file: Optional[str] = None,
) -> str:
    """
    Регистрирует идентификатор пользователя в таблице host_identities.

    Правила:
    * UNIQUE ограничение на (identity_type, identity_value) гарантирует,
      что один и тот же идентификатор не зарегистрирован дважды.
    * Несколько identity_value могут ссылаться на один canonical_key ( алиасы ).
    * Не создаём UNIQUE на canonical_key: múltiples алиасы на одного пользователя допустимы.
    * Если (identity_type, identity_value) уже существует — возвращаем существующий identity_id
      без изменений (идемпотентность).
    * Конфликт: если identity_value уже связан с другим canonical_key, регистрация
      отклоняется (fail closed). Это предотвращает случайную связь разных пользователей.

    Возвращает identity_id.
    """
    norm_type, norm_value = normalize_identity(identity_type, identity_value)
    identity_id = _generate_uuid()
    now = _now_iso()

    with get_connection(db_file) as conn:
        cur = conn.cursor()
        # Проверяем, не существует ли уже такой идентификатор
        cur.execute(
            """
            SELECT identity_id, canonical_key FROM host_identities
            WHERE identity_type = ? AND identity_value = ?
            """,
            (norm_type, norm_value),
        )
        row = cur.fetchone()
        if row:
            # Уже зарегистрирован — возвращаем существующий ID (идемпотентно)
            return str(row["identity_id"])

        # F5: Проверка конфликта — не позволяет одному identity_value
        # относиться к разным canonical_key
        existing_canonical = str(row["canonical_key"]) if row else None
        # (row is None at this point since we returned above, but check anyway)
        if existing_canonical and existing_canonical != canonical_key:
            conn.rollback()
            raise ValueError(
                f"Конфликт идентичности: '{norm_value}' уже связан с "
                f"canonical_key '{existing_canonical}', не может быть связан с '{canonical_key}'"
            )

        cur.execute(
            """
            INSERT INTO host_identities
            (identity_id, identity_type, identity_value, canonical_key, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (identity_id, norm_type, norm_value, canonical_key, now),
        )
        return identity_id


# ---------------------------------------------------------------------------
# 3. resolve_host_identity
# ---------------------------------------------------------------------------

def resolve_host_identity(
    identity_type: str,
    identity_value: str,
    db_file: Optional[str] = None,
) -> dict:
    """
    Разрешает идентичность пользователя Couchsurfing.

    Поиск по всем известным алиасам. Возвращает словарь:

    {
        "status": "ok" | "conflict" | "not_found",
        "canonical_key": str | None,
        "aliases": list[dict],  # все найденные записи host_identities
        "conflict_keys": list[str],  # список различных canonical_key при конфликте
    }

    Правила:
    * Один identity_value → один canonical_key (гарантировано UNIQUE на (type, value)).
    * Если несколько различных canonical_key найдены для разных значений,
      которые могут относиться к одному пользователю — конфликт (fail closed).
    * Если запись не найдена — status = "not_found".
    """
    norm_type, norm_value = normalize_identity(identity_type, identity_value)

    with get_connection(db_file) as conn:
        cur = conn.cursor()
        # Находим прямую запись
        cur.execute(
            """
            SELECT identity_id, identity_type, identity_value, canonical_key, created_at
            FROM host_identities
            WHERE identity_type = ? AND identity_value = ?
            """,
            (norm_type, norm_value),
        )
        direct = cur.fetchone()

        if not direct:
            return {
                "status": "not_found",
                "canonical_key": None,
                "aliases": [],
                "conflict_keys": [],
            }

        canonical_key = str(direct["canonical_key"])

        # Находим все алиасы с тем же canonical_key
        cur.execute(
            """
            SELECT identity_id, identity_type, identity_value, canonical_key, created_at
            FROM host_identities
            WHERE canonical_key = ?
            ORDER BY identity_type, identity_value
            """,
            (canonical_key,),
        )
        aliases = [dict(row) for row in cur.fetchall()]

        # Проверяем, не есть ли какой-то из alias-значений связан с другим canonical_key
        # (это было бы конфликтом, хотя UNIQUE на (type, value) предотвращает дубликаты,
        #  но разные значения могут указывать на разные ключи)
        conflict_keys = {canonical_key}
        for alias in aliases:
            if alias["canonical_key"] != canonical_key:
                conflict_keys.add(alias["canonical_key"])

        if len(conflict_keys) > 1:
            return {
                "status": "conflict",
                "canonical_key": canonical_key,
                "aliases": aliases,
                "conflict_keys": sorted(conflict_keys),
            }

        return {
            "status": "ok",
            "canonical_key": canonical_key,
            "aliases": aliases,
            "conflict_keys": [],
        }


# ---------------------------------------------------------------------------
# 4. get_host_aliases
# ---------------------------------------------------------------------------

def get_host_aliases(
    canonical_key: str,
    db_file: Optional[str] = None,
) -> list[dict]:
    """
    Возвращает все известные алиасы для данного canonical ключа.

    Каждый элемент списка:
    {
        "identity_id": str,
        "identity_type": str,
        "identity_value": str,
        "canonical_key": str,
        "created_at": str,
    }
    """
    with get_connection(db_file) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT identity_id, identity_type, identity_value, canonical_key, created_at
            FROM host_identities
            WHERE canonical_key = ?
            ORDER BY identity_type, identity_value
            """,
            (canonical_key,),
        )
        return [dict(row) for row in cur.fetchall()]


# ---------------------------------------------------------------------------
# 5. reserve_host_for_dispatch
# ---------------------------------------------------------------------------

def reserve_host_for_dispatch(
    identity_type: str,
    identity_value: str,
    trip_id: Optional[str],
    db_file: Optional[str] = None,
) -> dict:
    """
    Атомарно резервирует хоста для диспетчеризации.

    Весь процесс выполняется в одной транзакции BEGIN IMMEDIATE:

    1. Разрешить идентичность хоста → canonical_key.
    2. Проверить все известные алиасы на наличие активных блокировок.
    3. При конфликте или активной блокировке — отказ.
    4. Создать новый sending_attempts (state='pending').
    5. Создать новый host_dispatch_locks (lock_state='reserved').
    6. Записать событие RESERVATION в sending_history.
    7. Зафиксировать транзакцию.

    Возвращает словарь с результатом:

    {
        "status": "ok" | "blocked" | "conflict" | "not_found",
        "attempt_id": str | None,
        "lock_id": str | None,
        "canonical_key": str | None,
        "reason": str | None,
    }

    Активные состояния блокировки (препятствуют резервированию):
    reserved, sending, sent, uncertain, already_contacted

    Неактивные состояния (не препятствуют):
    released, failed
    """
    norm_type, norm_value = normalize_identity(identity_type, identity_value)
    now = _now_iso()
    attempt_id = _generate_uuid()
    lock_id = _generate_uuid()

    # Шаг 1: Разрешить идентичность
    resolution = resolve_host_identity(norm_type, norm_value, db_file=db_file)
    if resolution["status"] == "conflict":
        return {
            "status": "conflict",
            "attempt_id": None,
            "lock_id": None,
            "canonical_key": None,
            "reason": f"Конфликт идентичности: ключи {resolution['conflict_keys']}",
        }
    if resolution["status"] == "not_found":
        return {
            "status": "not_found",
            "attempt_id": None,
            "lock_id": None,
            "canonical_key": None,
            "reason": "Идентификатор хоста не зарегистрирован. Сначала вызовите register_host_identity.",
        }

    canonical_key = resolution["canonical_key"]

    with get_connection(db_file) as conn:
        cur = conn.cursor()
        try:
            cur.execute("BEGIN IMMEDIATE")

            # Шаг 2: Проверить все известные алиасы на активные блокировки
            cur.execute(
                """
                SELECT lock_id, attempt_id, lock_state, trip_id
                FROM host_dispatch_locks
                WHERE canonical_key = ?
                  AND lock_state IN ('reserved', 'sending', 'sent', 'uncertain', 'already_contacted')
                """,
                (canonical_key,),
            )
            active_locks = cur.fetchall()
            if active_locks:
                conn.rollback()
                lock = active_locks[0]
                return {
                    "status": "blocked",
                    "attempt_id": None,
                    "lock_id": None,
                    "canonical_key": canonical_key,
                    "reason": (
                        f"Хост уже заблокирован (lock_id={lock['lock_id']}, "
                        f"attempt_id={lock['attempt_id']}, state={lock['lock_state']})"
                    ),
                }

            # Шаг 3: Создать новый sending_attempts
            cur.execute(
                """
                INSERT INTO sending_attempts
                (id, trip_id, original_trip_id, host_id, username, host_id_norm, username_norm,
                 state, send_intent_committed, send_initiated, http_status, evidence_type,
                 evidence_reference, confirmation_url, error_details, proof_screenshot,
                 created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    attempt_id,
                    trip_id,
                    trip_id if trip_id else "unknown",
                    norm_value,
                    norm_value,
                    norm_value,
                    norm_value,
                    "pending",
                    0,
                    0,
                    None,
                    "none",
                    None,
                    None,
                    None,
                    None,
                    now,
                    now,
                ),
            )
            if cur.rowcount != 1:
                conn.rollback()
                return {
                    "status": "blocked",
                    "attempt_id": None,
                    "lock_id": None,
                    "canonical_key": canonical_key,
                    "reason": "Не удалось создать sending_attempts (rowcount != 1)",
                }

            # Шаг 4: Создать новый host_dispatch_locks
            cur.execute(
                """
                INSERT INTO host_dispatch_locks
                (lock_id, canonical_key, attempt_id, lock_state, trip_id, original_trip_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    lock_id,
                    canonical_key,
                    attempt_id,
                    "reserved",
                    trip_id,
                    trip_id if trip_id else "unknown",
                    now,
                    now,
                ),
            )
            if cur.rowcount != 1:
                conn.rollback()
                return {
                    "status": "blocked",
                    "attempt_id": None,
                    "lock_id": None,
                    "canonical_key": canonical_key,
                    "reason": "Не удалось создать host_dispatch_locks (rowcount != 1)",
                }

            # Шаг 5: Записать событие в sending_history
            cur.execute(
                """
                INSERT INTO sending_history
                (host_id, username, status, http_status, mode, proof_screenshot,
                 before_screenshot, button_selector, notes, error_details, timestamp,
                 attempt_id, trip_id, event_type, state_at_event, evidence_type)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    norm_value,
                    norm_value,
                    "RESERVED",
                    None,
                    "live",
                    None,
                    None,
                    None,
                    f"Host reserved for dispatch (canonical_key={canonical_key})",
                    None,
                    now,
                    attempt_id,
                    trip_id,
                    "RESERVATION",
                    "pending",
                    "none",
                ),
            )
            if cur.rowcount != 1:
                conn.rollback()
                return {
                    "status": "blocked",
                    "attempt_id": None,
                    "lock_id": None,
                    "canonical_key": canonical_key,
                    "reason": "Не удалось записать событие в sending_history (rowcount != 1)",
                }

            conn.commit()

        except Exception:
            conn.rollback()
            raise

    return {
        "status": "ok",
        "attempt_id": attempt_id,
        "lock_id": lock_id,
        "canonical_key": canonical_key,
        "reason": None,
    }


# ---------------------------------------------------------------------------
# 6. release_host_dispatch_lock
# ---------------------------------------------------------------------------

def release_host_dispatch_lock(
    attempt_id: str,
    new_lock_state: str = "released",
    db_file: Optional[str] = None,
) -> dict:
    """
    Безопасно освобождает блокировку хоста.

    Допустимые новые состояния: 'released' или 'failed'.
    Текущее состояние блокировки должно быть в активном состоянии
    (reserved, sending, sent, uncertain, already_contacted) — иначе операция не выполняется.

    Правия переходов:
    * reserved + send_intent_committed=0 → released (безопасно, внешнего действия не было)
    * reserved + send_intent_committed=1 → uncertain (внешнее действие могло произойти)
    * sending → uncertain (внешнее действие могло произойти)
    * sent / uncertain / already_contacted → НЕ МОГУТ быть освобождены (глобальное подавление)

    Выполняется в одной транзакции.

    Возвращает словарь с результатом.
    """
    if new_lock_state not in ("released", "failed"):
        return {
            "status": "error",
            "reason": f"Недопустимое состояние для освобождения: {new_lock_state}",
        }

    now = _now_iso()

    with get_connection(db_file) as conn:
        cur = conn.cursor()
        try:
            cur.execute("BEGIN IMMEDIATE")

            # Проверить текущее состояние блокировки
            cur.execute(
                """
                SELECT lock_state, canonical_key
                FROM host_dispatch_locks
                WHERE attempt_id = ?
                """,
                (attempt_id,),
            )
            row = cur.fetchone()
            if not row:
                conn.rollback()
                return {
                    "status": "not_found",
                    "reason": f"Блокировка для attempt_id={attempt_id} не найдена",
                }

            current_state = str(row["lock_state"])
            canonical_key = str(row["canonical_key"])

            # Нельзя освобождать активные состояния (sent, uncertain, already_contacted)
            # через эту функцию — они являются глобальным подавлением
            if current_state in ("sent", "uncertain", "already_contacted"):
                conn.rollback()
                return {
                    "status": "blocked",
                    "reason": f"Блокировка в состоянии '{current_state}' не может быть освобождена через release_host_dispatch_lock",
                }

            if current_state not in ("reserved", "sending"):
                conn.rollback()
                return {
                    "status": "error",
                    "reason": f"Неожиданное состояние блокировки: {current_state}",
                }

            # Проверить send_intent_committed в sending_attempts
            cur.execute(
                """
                SELECT send_intent_committed
                FROM sending_attempts
                WHERE id = ?
                """,
                (attempt_id,),
            )
            attempt_row = cur.fetchone()
            send_intent_committed = bool(int(attempt_row["send_intent_committed"])) if attempt_row else False

            # F4: Безопасные переходы
            if current_state == "sending":
                # Send был нажат, исход неизвестен → uncertain
                new_lock_state = "uncertain"
                reason_suffix = " (sending state, outcome unknown)"
            elif current_state == "reserved" and send_intent_committed:
                # Intent был закоммичен, но клик мог или не мог произойти → uncertain
                new_lock_state = "uncertain"
                reason_suffix = " (send intent committed, outcome unknown)"
            elif current_state == "reserved" and not send_intent_committed:
                # Intent не был закоммичен, внешнего действия не было.
                # Уважаем запрошенное состояние (released или failed)
                reason_suffix = " (reserved, no send intent)"
            else:
                conn.rollback()
                return {
                    "status": "error",
                    "reason": f"Необратимый переход состояния: {current_state} → {new_lock_state}",
                }

            # Обновить состояние блокировки
            cur.execute(
                """
                UPDATE host_dispatch_locks
                SET lock_state = ?, updated_at = ?
                WHERE attempt_id = ?
                """,
                (new_lock_state, now, attempt_id),
            )
            if cur.rowcount != 1:
                conn.rollback()
                return {
                    "status": "error",
                    "reason": "Не удалось обновить состояние блокировки (rowcount != 1)",
                }

            # Также обновить sending_attempts.state для консистентности
            # 'released' не является валидным состоянием Attempt, только 'sent'/'failed'/'uncertain'
            if new_lock_state in ("sent", "failed", "uncertain"):
                cur.execute(
                    """
                    UPDATE sending_attempts
                    SET state = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (new_lock_state, now, attempt_id),
                )

            conn.commit()

        except Exception:
            conn.rollback()
            raise

    return {
        "status": "ok",
        "attempt_id": attempt_id,
        "canonical_key": canonical_key,
        "previous_state": current_state,
        "new_state": new_lock_state,
        "reason": f"{new_lock_state}{reason_suffix}",
    }


# ---------------------------------------------------------------------------
# 7. check_host_availability
# ---------------------------------------------------------------------------

def check_host_availability(
    identity_type: str,
    identity_value: str,
    db_file: Optional[str] = None,
) -> dict:
    """
    Проверяет, может ли хост быть зарезервирован для диспетчеризации.

    Не изменяет состояние БД (только чтение).

    Возвращает словарь:
    {
        "available": bool,
        "canonical_key": str | None,
        "active_lock": dict | None,
        "conflict": bool,
        "reason": str | None,
    }
    """
    resolution = resolve_host_identity(identity_type, identity_value, db_file=db_file)
    if resolution["status"] == "conflict":
        return {
            "available": False,
            "canonical_key": None,
            "active_lock": None,
            "conflict": True,
            "reason": f"Конфликт идентичности: {resolution['conflict_keys']}",
        }
    if resolution["status"] == "not_found":
        return {
            "available": False,
            "canonical_key": None,
            "active_lock": None,
            "conflict": False,
            "reason": "Идентификатор не зарегистрирован",
        }

    canonical_key = resolution["canonical_key"]

    with get_connection(db_file) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT lock_id, attempt_id, lock_state, trip_id, updated_at
            FROM host_dispatch_locks
            WHERE canonical_key = ?
              AND lock_state IN ('reserved', 'sending', 'sent', 'uncertain', 'already_contacted')
            """,
            (canonical_key,),
        )
        active_lock = cur.fetchone()
        if active_lock:
            return {
                "available": False,
                "canonical_key": canonical_key,
                "active_lock": dict(active_lock),
                "conflict": False,
                "reason": f"Хост заблокирован (state={active_lock['lock_state']})",
            }

        return {
            "available": True,
            "canonical_key": canonical_key,
            "active_lock": None,
            "conflict": False,
            "reason": None,
        }


# Для совместимости с типами, которые могут не импортироваться из typing в старых Python
from typing import Optional  # noqa: E402


# ---------------------------------------------------------------------------
# 8. Crash Recovery
# ---------------------------------------------------------------------------

def recover_crashed_dispatches(
    db_file: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Восстановление после краша приложения.

    Сканирует host_dispatch_locks и sending_attempts на наличие
    зависших блокировок и применяет правила восстановления:

    * reserved + send_intent_committed=0 → released
      (внешнего действия не было, безопасно освободить)

    * reserved + send_intent_committed=1 → uncertain
      (коммит намерения был совершен, внешнее действие могло произойти)

    * sending → uncertain
      (браузер взаимодействовал с сайтом, исход неизвестен)

    * sent / uncertain / already_contacted → без изменений
      (терминальные состояния, глобальное подавление сохраняется)

    Возвращает словарь со статистикой восстановления:
    {
        "recovered": int,           # количество восстановленных записей
        "released": int,            # количество released
        "uncertain": int,           # количество uncertain
        "details": list[dict],      # детали каждой восстановленной записи
    }
    """
    from datetime import datetime, timedelta, timezone

    now_iso = datetime.now(timezone.utc).isoformat()
    recovered = 0
    released_count = 0
    uncertain_count = 0
    details: List[Dict[str, Any]] = []

    with get_connection(db_file) as conn:
        cur = conn.cursor()

        # Найти все зависшие блокировки (не терминальные состояния)
        cur.execute(
            """
            SELECT l.lock_id, l.canonical_key, l.attempt_id, l.lock_state,
                   l.trip_id, a.send_intent_committed, a.state as attempt_state
            FROM host_dispatch_locks l
            LEFT JOIN sending_attempts a ON l.attempt_id = a.id
            WHERE l.lock_state IN ('reserved', 'sending')
            """
        )
        stuck_locks = cur.fetchall()

        for lock in stuck_locks:
            lock_id = str(lock["lock_id"])
            canonical_key = str(lock["canonical_key"])
            attempt_id = str(lock["attempt_id"])
            lock_state = str(lock["lock_state"])
            trip_id = str(lock["trip_id"]) if lock["trip_id"] else None
            send_intent = bool(int(lock["send_intent_committed"])) if lock["send_intent_committed"] is not None else False

            # Применяем правила восстановления
            if lock_state == "sending":
                # Браузер взаимодействовал, исход неизвестен → uncertain
                new_state = "uncertain"
                reason = "Crash recovery: sending state, external action may have occurred"
                uncertain_count += 1
            elif lock_state == "reserved" and send_intent:
                # Коммит намерения был совершен → uncertain
                new_state = "uncertain"
                reason = "Crash recovery: send intent committed, external action may have occurred"
                uncertain_count += 1
            elif lock_state == "reserved" and not send_intent:
                # Intent не закоммичен, внешнего действия не было → released
                new_state = "released"
                reason = "Crash recovery: reserved without send intent, safe to release"
                released_count += 1
            else:
                continue

            # Обновить host_dispatch_locks
            cur.execute(
                """
                UPDATE host_dispatch_locks
                SET lock_state = ?, updated_at = ?
                WHERE lock_id = ?
                """,
                (new_state, now_iso, lock_id),
            )

            # Обновить sending_attempts
            # 'released' не является валидным состоянием Attempt, только lock-состояние
            if new_state in ("sent", "failed", "uncertain"):
                cur.execute(
                    """
                    UPDATE sending_attempts
                    SET state = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (new_state, now_iso, attempt_id),
                )

            recovered += 1
            details.append({
                "lock_id": lock_id,
                "attempt_id": attempt_id,
                "canonical_key": canonical_key,
                "previous_state": lock_state,
                "new_state": new_state,
                "trip_id": trip_id,
                "reason": reason,
            })

        conn.commit()

    return {
        "recovered": recovered,
        "released": released_count,
        "uncertain": uncertain_count,
        "details": details,
    }