"""
core/database.py - Управление чистой базой данных SQLite для нового пайплайна.
Работает в режиме WAL (Write-Ahead Logging) для быстрой параллельной работы.
Единый источник истины: TRIP (Карточка поездки: страна, город, даты, статус).
"""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.models import HostCard, HostState, Trip, AuditEvent

DB_PATH = Path("output/couchsurfing_platform.db")


@contextmanager
def get_connection(db_file: Optional[Path] = None):
    target = db_file or DB_PATH
    conn = sqlite3.connect(str(target), timeout=30.0)
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_core_tables(db_file: Optional[Path] = None):
    """Создает таблицы нового модульного пайплайна."""
    with get_connection(db_file) as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS core_trips (
            id TEXT PRIMARY KEY,
            url TEXT NOT NULL UNIQUE,
            country TEXT NOT NULL DEFAULT 'Japan',
            city TEXT NOT NULL,
            arrival_date TEXT NOT NULL,
            departure_date TEXT NOT NULL,
            dates_text TEXT,
            is_active INTEGER DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS core_hosts (
            host_id TEXT NOT NULL,
            trip_id TEXT NOT NULL,
            username TEXT NOT NULL,
            name TEXT NOT NULL,
            location TEXT NOT NULL,
            gender TEXT,
            age INTEGER,
            occupation TEXT,
            about TEXT,
            interests_json TEXT,
            languages_json TEXT,
            countries_json TEXT,
            profile_url TEXT,
            avatar_url TEXT,
            is_verified INTEGER DEFAULT 0,
            birth_country TEXT,
            residence_country TEXT,
            is_hosting TEXT DEFAULT 'unknown',
            household TEXT,
            special_notes TEXT,
            countries_lived_json TEXT,
            score_stars INTEGER DEFAULT 1,
            score_reasons_json TEXT,
            state TEXT NOT NULL DEFAULT 'discovered',
            message_draft TEXT,
            qc_passed INTEGER DEFAULT 0,
            qc_notes TEXT,
            sent_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (trip_id, host_id),
            FOREIGN KEY (trip_id) REFERENCES core_trips(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_core_hosts_state ON core_hosts(trip_id, state);
        CREATE INDEX IF NOT EXISTS idx_core_hosts_stars ON core_hosts(trip_id, score_stars DESC);

        CREATE TABLE IF NOT EXISTS core_host_snapshots (
            host_id TEXT NOT NULL,
            trip_id TEXT NOT NULL,
            raw_data_json TEXT NOT NULL,
            captured_at TEXT NOT NULL,
            PRIMARY KEY (trip_id, host_id)
        );

        CREATE TABLE IF NOT EXISTS core_audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            trip_id TEXT NOT NULL,
            module TEXT NOT NULL,
            event TEXT NOT NULL,
            host_id TEXT,
            details_json TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_core_audit_trip ON core_audit_logs(trip_id, timestamp DESC);
        """)

        # Миграция колонок для существующих таблиц при необходимости
        cur = conn.cursor()
        cur.execute("PRAGMA table_info(core_trips);")
        cols = {row["name"] for row in cur.fetchall()}
        if "country" not in cols:
            conn.execute("ALTER TABLE core_trips ADD COLUMN country TEXT NOT NULL DEFAULT 'Japan';")
        if "dates_text" not in cols:
            conn.execute("ALTER TABLE core_trips ADD COLUMN dates_text TEXT;")
        if "updated_at" not in cols:
            conn.execute("ALTER TABLE core_trips ADD COLUMN updated_at TEXT;")


def log_audit(trip_id: str, module: str, event: str, host_id: Optional[str] = None, details: Optional[Dict[str, Any]] = None, db_file: Optional[Path] = None):
    """Сквозной аудит всех действий системы."""
    now_iso = datetime.now().isoformat()
    det_str = json.dumps(details or {}, ensure_ascii=False)
    with get_connection(db_file) as conn:
        conn.execute(
            """
            INSERT INTO core_audit_logs (timestamp, trip_id, module, event, host_id, details_json)
            VALUES (?, ?, ?, ?, ?, ?);
            """,
            (now_iso, trip_id, module, event, host_id, det_str)
        )


def get_active_trip(db_file: Optional[Path] = None) -> Optional[Trip]:
    """Возвращает текущую активную поездку (Единый корень системы)."""
    with get_connection(db_file) as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM core_trips WHERE is_active = 1 LIMIT 1;")
        row = cur.fetchone()
        if not row:
            # Если активный не помечен, берем первый попавшийся
            cur.execute("SELECT * FROM core_trips ORDER BY created_at DESC LIMIT 1;")
            row = cur.fetchone()
        if row:
            return Trip(
                id=row["id"],
                url=row["url"],
                country=row["country"] or "Japan",
                city=row["city"],
                arrival_date=row["arrival_date"],
                departure_date=row["departure_date"],
                dates_text=row["dates_text"],
                is_active=bool(row["is_active"]),
                created_at=row["created_at"],
                updated_at=row["updated_at"]
            )
    return None


def set_active_trip(trip_id: str, db_file: Optional[Path] = None) -> Optional[Trip]:
    """
    Атомарно переключает рабочий контекст системы на указанный Trip.
    С этого момента все модули работают исключительно с этой поездкой.
    """
    now_iso = datetime.now().isoformat()
    with get_connection(db_file) as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM core_trips WHERE id = ?;", (trip_id,))
        row = cur.fetchone()
        if not row:
            return None

        # Сбрасываем активность со всех поездок
        conn.execute("UPDATE core_trips SET is_active = 0, updated_at = ?;", (now_iso,))
        # Активируем выбранную поездку
        conn.execute("UPDATE core_trips SET is_active = 1, updated_at = ? WHERE id = ?;", (now_iso, trip_id))

        # Синхронизируем app_settings (для обратной совместимости старых модулей)
        trip_dict = {
            "trip_id": row["id"],
            "url": row["url"],
            "city": row["city"],
            "arrival_date": row["arrival_date"],
            "departure_date": row["departure_date"],
            "dates_text": row["dates_text"],
            "country": row["country"] or "Japan"
        }
        conn.execute(
            """
            INSERT INTO app_settings (key, value, updated_at) 
            VALUES ('active_trip_context', ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at;
            """,
            (json.dumps(trip_dict, ensure_ascii=False), now_iso)
        )

    log_audit(
        trip_id=trip_id,
        module="TRIP",
        event="ACTIVE_TRIP_SWITCHED",
        details={"city": row["city"], "country": row["country"] or "Japan", "dates": f"{row['arrival_date']} - {row['departure_date']}"},
        db_file=db_file
    )
    return get_active_trip(db_file)


def list_trips(db_file: Optional[Path] = None) -> List[Trip]:
    """Возвращает все зарегистрированные поездки."""
    with get_connection(db_file) as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM core_trips ORDER BY arrival_date ASC;")
        rows = cur.fetchall()
        return [
            Trip(
                id=r["id"],
                url=r["url"],
                country=r["country"] or "Japan",
                city=r["city"],
                arrival_date=r["arrival_date"],
                departure_date=r["departure_date"],
                dates_text=r["dates_text"],
                is_active=bool(r["is_active"]),
                created_at=r["created_at"],
                updated_at=r["updated_at"]
            )
            for r in rows
        ]


def save_trip(trip: Trip, db_file: Optional[Path] = None) -> Trip:
    """Сохраняет поездку в базу данных."""
    now_iso = datetime.now().isoformat()
    with get_connection(db_file) as conn:
        conn.execute(
            """
            INSERT INTO core_trips (id, url, country, city, arrival_date, departure_date, dates_text, is_active, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                url = excluded.url,
                country = excluded.country,
                city = excluded.city,
                arrival_date = excluded.arrival_date,
                departure_date = excluded.departure_date,
                dates_text = excluded.dates_text,
                is_active = excluded.is_active,
                updated_at = excluded.updated_at;
            """,
            (
                trip.id, trip.url, trip.country, trip.city, trip.arrival_date,
                trip.departure_date, trip.dates_text, 1 if trip.is_active else 0,
                trip.created_at or now_iso, trip.updated_at or now_iso
            )
        )
    return trip
