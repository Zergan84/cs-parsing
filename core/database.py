"""
core/database.py - Единый канонический слой персистентности SQLite (Single Source of Truth).

Обеспечивает:
1. Единую точку подключения к SQLite (`get_connection` / `db_session` / `get_db_connection`) с WAL,
   `PRAGMA foreign_keys=ON`, `PRAGMA busy_timeout=15000`.
2. Авторитетную схему всех доменных таблиц (`core_trips`, `core_hosts`, `core_host_snapshots`, `core_audit_logs`,
   `hosts`, `trip_hosts`, `user_trips`, `profile_analyses`, `personal_context_facts`, `compatibility_anchors`,
   `generated_messages`, `sending_queue`, `sending_history`, `inbox_conversations`, `background_jobs`, `app_settings`,
   визуальные таблицы Host Dossier и представление `requests`).
3. Каноническое управление активной поездкой (`get_active_trip`, `set_active_trip`, `list_trips`, `save_trip`).
"""

import json
import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional
from contextvars import ContextVar

from core.models import AuditEvent, HostCard, HostState, Trip

logger = logging.getLogger("CoreDatabase")

BASE_DIR = Path(__file__).resolve().parent.parent
OUTPUT_DIR = Path(os.environ.get("CS_OUTPUT_DIR", str(BASE_DIR / "output"))).resolve()
DEFAULT_DB_PATH = (OUTPUT_DIR / "couchsurfing_platform.db").resolve()
PROTECTED_PRODUCTION_DB_PATH = (BASE_DIR / "output" / "couchsurfing_platform.db").resolve()
DB_PATH = Path(os.environ.get("CS_DB_PATH", str(DEFAULT_DB_PATH))).resolve()
_DATABASE_PATH_OVERRIDE: ContextVar[Optional[Path]] = ContextVar("cs_database_path", default=None)


def _is_test_runtime() -> bool:
    return os.environ.get("CS_ENV", "").strip().lower() in {"test", "testing"} or bool(os.environ.get("PYTEST_CURRENT_TEST"))


def _assert_safe_test_path(path: Path) -> None:
    """Fail closed if test code resolves any alias of the production database."""
    if not _is_test_runtime():
        return
    resolved = path.expanduser().resolve()
    production = PROTECTED_PRODUCTION_DB_PATH
    env_path = os.environ.get("CS_DB_PATH", "").strip()
    if env_path:
        configured = Path(env_path).expanduser().resolve()
        if configured == production:
            raise RuntimeError(f"Test runtime cannot use production database via CS_DB_PATH: {configured}")
    if resolved == production:
        raise RuntimeError(f"Test runtime cannot use production database: {resolved}")
    try:
        resolved.relative_to((BASE_DIR / "output").resolve())
    except ValueError:
        pass
    else:
        raise RuntimeError(f"Test runtime requires a temporary database outside project output/: {resolved}")
    # Existing hard links resolve to different paths, so compare file identity too.
    try:
        if production.exists() and resolved.exists():
            prod_stat, candidate_stat = production.stat(), resolved.stat()
            if (prod_stat.st_dev, prod_stat.st_ino) == (candidate_stat.st_dev, candidate_stat.st_ino):
                raise RuntimeError(f"Test runtime cannot use a hard link to production database: {resolved}")
    except FileNotFoundError:
        pass


@contextmanager
def database_path_context(db_file: Path | str):
    """Set an explicit database for work performed within an app/request context."""
    target = Path(db_file).expanduser().resolve()
    _assert_safe_test_path(target)
    token = _DATABASE_PATH_OVERRIDE.set(target)
    try:
        yield target
    finally:
        _DATABASE_PATH_OVERRIDE.reset(token)


def get_output_dir() -> Path:
    """Возвращает актуальную директорию output с учетом переменной окружения CS_OUTPUT_DIR."""
    return Path(os.environ.get("CS_OUTPUT_DIR", str(OUTPUT_DIR))).resolve()


def get_production_db_path() -> Path:
    """Return the production location for an explicit production bootstrap only."""
    return PROTECTED_PRODUCTION_DB_PATH


def get_db_path(db_file: Optional[Path | str] = None) -> Path:
    """
    Возвращает канонический путь к файлу базы данных SQLite.
    Приоритет:
    1. Явно переданный аргумент `db_file`.
    2. Явный путь приложения в текущем async/request контексте.
    3. Переменная окружения, установленная bootstrap приложения или тестовой среды.
    4. Явный override DB_PATH (в основном для тестовых адаптеров).

    Универсальный database module намеренно не выбирает production default.
    """
    if db_file is not None:
        target = Path(db_file).expanduser().resolve()
        _assert_safe_test_path(target)
        return target
    scoped = _DATABASE_PATH_OVERRIDE.get()
    if scoped is not None:
        _assert_safe_test_path(scoped)
        return scoped
    env_db = os.environ.get("CS_DB_PATH")
    if env_db:
        target = Path(env_db).expanduser().resolve()
    elif DB_PATH != DEFAULT_DB_PATH:
        target = Path(DB_PATH).expanduser().resolve()
    else:
        raise RuntimeError("Database path is not configured; pass db_file or configure it in the application bootstrap")
    _assert_safe_test_path(target)
    return target


def get_db_connection(db_file: Optional[Path | str] = None) -> sqlite3.Connection:
    """Создает потокобезопасное подключение к SQLite с поддержкой WAL и внешних ключей."""
    target = get_db_path(db_file)
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target), timeout=30.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=15000;")
    return conn


@contextmanager
def get_read_only_connection(db_file: Optional[Path | str] = None) -> Generator[sqlite3.Connection, None, None]:
    """Open an existing SQLite database read-only and forbid writes at SQLite level."""
    target = get_db_path(db_file)
    _assert_safe_test_path(target)
    conn = sqlite3.connect(target.as_uri() + "?mode=ro", uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def get_connection(db_file: Optional[Path | str] = None) -> Generator[sqlite3.Connection, None, None]:
    """Единый контекстный менеджер транзакционной сессии SQLite."""
    conn = get_db_connection(db_file)
    try:
        yield conn
        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.error(f"Ошибка транзакции БД: {e}")
        raise
    finally:
        conn.close()


def verify_schema_read_only(db_file: Path | str, required_tables: set[str]) -> None:
    """Verify an existing SQLite schema without creating, migrating, or writing."""
    target = Path(db_file).expanduser().resolve()
    uri = target.as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=5) as conn:
        conn.execute("PRAGMA query_only=ON")
        present = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    missing = sorted(set(required_tables) - present)
    if missing:
        raise RuntimeError(f"Database schema is incomplete; refusing startup without migration: {', '.join(missing)}")


# Канонический алиас транзакционной сессии
db_session = get_connection


def _ensure_column(cursor: sqlite3.Cursor, table: str, column: str, col_def: str) -> None:
    """Безопасно добавляет колонку в таблицу, если она ещё не существует."""
    cursor.execute(f"PRAGMA table_info({table});")
    existing = {row[1] for row in cursor.fetchall()}
    if column not in existing:
        cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_def};")


def init_core_tables(db_file: Path | str) -> None:
    """
    Инициализирует полную каноническую схему базы данных со всеми таблицами,
    внешними ключами, индексами и дефолтными настройками.
    Операция идемпотентна и безопасна для существующих данных.
    """
    if db_file is None:
        raise ValueError("init_db requires an explicit database path")
    with get_connection(db_file) as conn:
        cur = conn.cursor()

        # ------------------------------------------------------------------
        # 1. ПОЕЗДКИ (TRIPS — КОРЕНЬ АГРЕГАТА)
        # ------------------------------------------------------------------
        cur.execute("""
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
        """)
        _ensure_column(cur, "core_trips", "country", "TEXT NOT NULL DEFAULT 'Japan'")
        _ensure_column(cur, "core_trips", "dates_text", "TEXT")
        _ensure_column(cur, "core_trips", "updated_at", "TEXT")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_core_trips_active ON core_trips(is_active);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_core_trips_city ON core_trips(city);")

        cur.execute("""
        CREATE TABLE IF NOT EXISTS user_trips (
            trip_id TEXT PRIMARY KEY,
            city TEXT NOT NULL,
            arrival_date TEXT,
            departure_date TEXT,
            dates_text TEXT,
            url TEXT,
            is_active INTEGER DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ut_city ON user_trips(city);")

        # ------------------------------------------------------------------
        # 2. ХОСТЫ И СНАПШОТЫ (HOSTS & SNAPSHOTS)
        # ------------------------------------------------------------------
        cur.execute("""
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
            verification_status TEXT NOT NULL DEFAULT 'UNKNOWN',
            recently_active_status TEXT NOT NULL DEFAULT 'UNKNOWN',
            last_active_raw TEXT,
            last_login_days INTEGER,
            last_active_at TEXT,
            birth_country TEXT,
            residence_country TEXT,
            is_hosting TEXT DEFAULT 'unknown',
            household TEXT,
            special_notes TEXT,
            countries_lived_json TEXT,
            score_stars INTEGER DEFAULT 1,
            score_reasons_json TEXT,
            state TEXT NOT NULL DEFAULT 'discovered',
            priority INTEGER NOT NULL DEFAULT 10,
            attempts INTEGER NOT NULL DEFAULT 0,
            message_draft TEXT,
            qc_passed INTEGER DEFAULT 0,
            qc_notes TEXT,
            sent_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (trip_id, host_id),
            FOREIGN KEY (trip_id) REFERENCES core_trips(id) ON DELETE CASCADE
        );
        """)
        _ensure_column(cur, "core_hosts", "is_verified", "INTEGER DEFAULT 0")
        _ensure_column(cur, "core_hosts", "verification_status", "TEXT NOT NULL DEFAULT 'UNKNOWN'")
        _ensure_column(cur, "core_hosts", "recently_active_status", "TEXT NOT NULL DEFAULT 'UNKNOWN'")
        _ensure_column(cur, "core_hosts", "last_active_raw", "TEXT")
        _ensure_column(cur, "core_hosts", "last_login_days", "INTEGER")
        _ensure_column(cur, "core_hosts", "last_active_at", "TEXT")
        _ensure_column(cur, "core_hosts", "activity_checked_at", "TEXT")
        _ensure_column(cur, "core_hosts", "activity_source", "TEXT")
        _ensure_column(cur, "core_hosts", "eligibility_reason", "TEXT")
        _ensure_column(cur, "core_hosts", "eligibility_evidence_json", "TEXT")
        _ensure_column(cur, "core_hosts", "eligibility_checked_at", "TEXT")
        _ensure_column(cur, "core_hosts", "birth_country", "TEXT")
        _ensure_column(cur, "core_hosts", "residence_country", "TEXT")
        _ensure_column(cur, "core_hosts", "is_hosting", "TEXT DEFAULT 'unknown'")
        _ensure_column(cur, "core_hosts", "household", "TEXT")
        _ensure_column(cur, "core_hosts", "special_notes", "TEXT")
        _ensure_column(cur, "core_hosts", "countries_lived_json", "TEXT")
        _ensure_column(cur, "core_hosts", "priority", "INTEGER NOT NULL DEFAULT 10")
        _ensure_column(cur, "core_hosts", "attempts", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(cur, "core_hosts", "clean_name", "TEXT")
        _ensure_column(cur, "core_hosts", "references_count", "INTEGER DEFAULT 0")
        _ensure_column(cur, "core_hosts", "response_rate_pct", "REAL DEFAULT 90.0")
        _ensure_column(cur, "core_hosts", "score_stars", "INTEGER DEFAULT 1")
        _ensure_column(cur, "core_hosts", "qc_notes", "TEXT")
        _ensure_column(cur, "core_hosts", "message_version", "INTEGER NOT NULL DEFAULT 1")

        # Any persisted message edit advances the version, including legacy API
        # writes that bypass application services. Approval hashes remain tied
        # to the exact version that the user reviewed.
        cur.execute("""
        CREATE TRIGGER IF NOT EXISTS trg_core_hosts_message_version
        AFTER UPDATE OF message_draft ON core_hosts
        WHEN OLD.message_draft IS NOT NEW.message_draft
        BEGIN
            UPDATE core_hosts SET message_version = COALESCE(OLD.message_version, 1) + 1
            WHERE trip_id = NEW.trip_id AND host_id = NEW.host_id;
        END;
        """)

        cur.execute("CREATE INDEX IF NOT EXISTS idx_core_hosts_state ON core_hosts(trip_id, state);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_core_hosts_stars ON core_hosts(trip_id, score_stars DESC);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_core_hosts_username ON core_hosts(username);")

        cur.execute("""
        CREATE TABLE IF NOT EXISTS core_host_snapshots (
            host_id TEXT NOT NULL,
            trip_id TEXT NOT NULL,
            raw_data_json TEXT NOT NULL,
            captured_at TEXT NOT NULL,
            PRIMARY KEY (trip_id, host_id)
        );
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_core_snapshots_host ON core_host_snapshots(host_id);")

        cur.execute("""
        CREATE TABLE IF NOT EXISTS hosts (
            id TEXT PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            clean_name TEXT NOT NULL,
            profile_url TEXT NOT NULL,
            stay_request_url TEXT NOT NULL,
            location TEXT DEFAULT 'Tokyo',
            avatar_url TEXT,
            about_me TEXT,
            about_me_clean TEXT,
            references_count INTEGER DEFAULT 0,
            response_rate_pct REAL DEFAULT 90.0,
            instagram_handle TEXT,
            instagram_url TEXT,
            raw_data_json TEXT,
            first_scraped_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_hosts_username ON hosts(username);")

        cur.execute("""
        CREATE TABLE IF NOT EXISTS trip_hosts (
            trip_id TEXT NOT NULL,
            host_id TEXT NOT NULL,
            city TEXT,
            arrival_date TEXT,
            departure_date TEXT,
            created_at TEXT NOT NULL,
            PRIMARY KEY (trip_id, host_id)
        );
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_th_trip ON trip_hosts(trip_id);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_th_host ON trip_hosts(host_id);")

        # ------------------------------------------------------------------
        # 3. ЛИЧНЫЙ КОНТЕКСТ И ЯКОРЯ СОВМЕСТИМОСТИ (PERSONAL CONTEXT)
        # ------------------------------------------------------------------
        cur.execute("""
        CREATE TABLE IF NOT EXISTS personal_context_facts (
            fact_id TEXT PRIMARY KEY,
            category TEXT NOT NULL,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'confirmed',
            source TEXT DEFAULT 'user_curated',
            confidence REAL DEFAULT 1.0,
            priority INTEGER DEFAULT 1,
            is_active INTEGER DEFAULT 1,
            valid_until TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """)
        _ensure_column(cur, "personal_context_facts", "priority", "INTEGER DEFAULT 1")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_pcf_category ON personal_context_facts(category);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_pcf_active ON personal_context_facts(is_active);")

        cur.execute("""
        CREATE TABLE IF NOT EXISTS compatibility_anchors (
            anchor_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            category TEXT NOT NULL,
            keywords_json TEXT NOT NULL,
            description TEXT,
            priority INTEGER DEFAULT 1,
            is_active INTEGER DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ca_active ON compatibility_anchors(is_active);")

        # ------------------------------------------------------------------
        # 4. ПРОФИЛЬНЫЙ АНАЛИЗ (PROFILE ANALYSES)
        # ------------------------------------------------------------------
        cur.execute("""
        CREATE TABLE IF NOT EXISTS profile_analyses (
            host_id TEXT PRIMARY KEY,
            estimated_gender TEXT NOT NULL DEFAULT 'Unknown',
            host_type TEXT NOT NULL DEFAULT 'solo',
            can_host_overnight INTEGER NOT NULL DEFAULT 1,
            hosting_verdict_reason TEXT,
            hosting_confidence REAL DEFAULT 1.0,
            sleeping_arrangement TEXT DEFAULT 'Not specified',
            house_rules TEXT DEFAULT 'Standard rules',
            secret_code_detected INTEGER DEFAULT 0,
            secret_code_hint TEXT,
            conversation_hooks_json TEXT,
            compatibility_points_json TEXT,
            conversation_strategy TEXT,
            compatibility_score REAL DEFAULT NULL,
            analyzed_at TEXT NOT NULL,
            FOREIGN KEY (host_id) REFERENCES hosts(id) ON DELETE CASCADE
        );
        """)
        _ensure_column(cur, "profile_analyses", "compatibility_score", "REAL DEFAULT NULL")

        # ------------------------------------------------------------------
        # 5. СГЕНЕРИРОВАННЫЕ СООБЩЕНИЯ И ОЧЕРЕДЬ (MESSAGES & SENDING QUEUE)
        # ------------------------------------------------------------------
        cur.execute("""
        CREATE TABLE IF NOT EXISTS generated_messages (
            id TEXT PRIMARY KEY,
            host_id TEXT NOT NULL,
            check_in TEXT NOT NULL,
            check_out TEXT NOT NULL,
            message_text TEXT NOT NULL,
            word_count INTEGER NOT NULL,
            hook_used TEXT,
            context_facts_used_json TEXT,
            anchor_used TEXT,
            host_fact_used TEXT,
            user_fact_used TEXT,
            confidence REAL,
            fallback_used INTEGER DEFAULT 0,
            qc_passed INTEGER DEFAULT 1,
            qc_notes TEXT,
            is_active INTEGER DEFAULT 1,
            created_at TEXT NOT NULL,
            FOREIGN KEY (host_id) REFERENCES hosts(id) ON DELETE CASCADE
        );
        """)
        _ensure_column(cur, "generated_messages", "anchor_used", "TEXT DEFAULT NULL")
        _ensure_column(cur, "generated_messages", "host_fact_used", "TEXT DEFAULT NULL")
        _ensure_column(cur, "generated_messages", "user_fact_used", "TEXT DEFAULT NULL")
        _ensure_column(cur, "generated_messages", "confidence", "REAL DEFAULT NULL")
        _ensure_column(cur, "generated_messages", "fallback_used", "INTEGER DEFAULT 0")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_gm_host ON generated_messages(host_id);")

        cur.execute("""
        CREATE TABLE IF NOT EXISTS sending_queue (
            id TEXT PRIMARY KEY,
            host_id TEXT NOT NULL UNIQUE,
            current_message_id TEXT,
            status TEXT NOT NULL DEFAULT 'Draft',
            priority INTEGER DEFAULT 10,
            notes TEXT,
            scheduled_for TEXT,
            queued_at TEXT,
            sent_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (host_id) REFERENCES hosts(id) ON DELETE CASCADE,
            FOREIGN KEY (current_message_id) REFERENCES generated_messages(id) ON DELETE SET NULL
        );
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_sq_status ON sending_queue(status);")

        # ------------------------------------------------------------------
        # 6. ИСТОРИЯ ОТПРАВОК И СКВОЗНОЙ АУДИТ (SENDING HISTORY & AUDIT LOGS)
        # ------------------------------------------------------------------
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
        # Миграция: отвязка sending_history от устаревшей legacy-таблицы hosts
        cur.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='sending_history';")
        sh_row = cur.fetchone()
        if sh_row and "REFERENCES hosts" in (sh_row[0] or ""):
            cur.execute("PRAGMA foreign_keys=OFF;")
            cur.execute("""
            CREATE TABLE IF NOT EXISTS sending_history_canonical (
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
            cur.execute("INSERT OR REPLACE INTO sending_history_canonical SELECT id, host_id, username, status, http_status, mode, proof_screenshot, before_screenshot, button_selector, notes, error_details, timestamp FROM sending_history;")
            cur.execute("DROP TABLE sending_history;")
            cur.execute("ALTER TABLE sending_history_canonical RENAME TO sending_history;")
            cur.execute("PRAGMA foreign_keys=ON;")

        cur.execute("CREATE INDEX IF NOT EXISTS idx_sh_username ON sending_history(username);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_sh_timestamp ON sending_history(timestamp);")

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
        cur.execute("CREATE INDEX IF NOT EXISTS idx_core_audit_trip ON core_audit_logs(trip_id, timestamp DESC);")

        # ------------------------------------------------------------------
        # 7. ИНБОКС, ЗАДАЧИ И НАСТРОЙКИ (INBOX, JOBS, SETTINGS)
        # ------------------------------------------------------------------
        cur.execute("""
        CREATE TABLE IF NOT EXISTS inbox_conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            host_username TEXT UNIQUE NOT NULL,
            host_name TEXT NOT NULL,
            host_avatar_url TEXT,
            location TEXT DEFAULT 'Tokyo, Japan',
            last_message_received TEXT NOT NULL,
            last_message_timestamp TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'Host Replied',
            category TEXT DEFAULT 'CHAT_INTERESTED',
            suggested_reply TEXT,
            thread_history_json TEXT,
            tone_used TEXT DEFAULT 'normal',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """)
        _ensure_column(cur, "inbox_conversations", "category", "TEXT DEFAULT 'CHAT_INTERESTED'")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ic_username ON inbox_conversations(host_username);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ic_status ON inbox_conversations(status);")

        cur.execute("""
        CREATE TABLE IF NOT EXISTS background_jobs (
            job_id TEXT PRIMARY KEY,
            job_type TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'running',
            parameters_json TEXT,
            progress_current INTEGER DEFAULT 0,
            progress_total INTEGER DEFAULT 0,
            message TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT
        );
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """)

        # ------------------------------------------------------------------
        # 8. ПОДСИСТЕМА HOST DOSSIER И ВИЗУАЛЬНЫХ НАБЛЮДЕНИЙ
        # ------------------------------------------------------------------
        cur.execute("""
        CREATE TABLE IF NOT EXISTS photo_assets (
            id TEXT PRIMARY KEY,
            image_hash TEXT UNIQUE NOT NULL,
            local_path TEXT NOT NULL,
            file_size INTEGER,
            mime_type TEXT,
            llava_raw_response TEXT,
            detected_objects_json TEXT,
            detected_topics_json TEXT,
            model TEXT NOT NULL DEFAULT 'llava:latest',
            analysis_status TEXT NOT NULL DEFAULT 'completed',
            analyzed_at TEXT,
            created_at TEXT NOT NULL,
            incomplete_source_metadata INTEGER DEFAULT 0
        );
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_pa_hash ON photo_assets(image_hash);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_pa_path ON photo_assets(local_path);")

        cur.execute("""
        CREATE TABLE IF NOT EXISTS host_photos (
            id TEXT PRIMARY KEY,
            host_id TEXT NOT NULL,
            photo_asset_id TEXT NOT NULL,
            original_url TEXT,
            photo_page_url TEXT,
            photo_order INTEGER,
            is_avatar INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            FOREIGN KEY (host_id) REFERENCES hosts(id) ON DELETE CASCADE,
            FOREIGN KEY (photo_asset_id) REFERENCES photo_assets(id) ON DELETE CASCADE,
            UNIQUE(host_id, photo_asset_id)
        );
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_hp_host ON host_photos(host_id);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_hp_asset ON host_photos(photo_asset_id);")

        cur.execute("""
        CREATE TABLE IF NOT EXISTS visual_observations (
            id TEXT PRIMARY KEY,
            host_id TEXT NOT NULL,
            photo_asset_id TEXT NOT NULL,
            observation TEXT NOT NULL,
            category TEXT NOT NULL,
            evidence_confidence REAL NOT NULL DEFAULT 0.95,
            raw_description TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (host_id) REFERENCES hosts(id) ON DELETE CASCADE,
            FOREIGN KEY (photo_asset_id) REFERENCES photo_assets(id) ON DELETE CASCADE
        );
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_vo_host ON visual_observations(host_id);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_vo_obs ON visual_observations(observation);")

        cur.execute("""
        CREATE TABLE IF NOT EXISTS host_interests (
            id TEXT PRIMARY KEY,
            host_id TEXT NOT NULL,
            interest_name TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'general',
            source TEXT NOT NULL,
            confirming_facts_json TEXT,
            source_refs_json TEXT,
            confidence REAL NOT NULL DEFAULT 0.5,
            status TEXT NOT NULL DEFAULT 'uncertain',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (host_id) REFERENCES hosts(id) ON DELETE CASCADE
        );
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_hi_host ON host_interests(host_id);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_hi_conf ON host_interests(confidence);")

        cur.execute("""
        CREATE TABLE IF NOT EXISTS host_places (
            id TEXT PRIMARY KEY,
            host_id TEXT NOT NULL,
            place_name TEXT NOT NULL,
            place_type TEXT NOT NULL,
            country TEXT,
            city TEXT,
            landmark TEXT,
            source TEXT NOT NULL,
            confidence REAL NOT NULL DEFAULT 0.5,
            created_at TEXT NOT NULL,
            FOREIGN KEY (host_id) REFERENCES hosts(id) ON DELETE CASCADE
        );
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_hpl_host ON host_places(host_id);")

        # ------------------------------------------------------------------
        # 9. ПРЕДСТАВЛЕНИЕ REQUESTS И ДЕФОЛТНЫЕ ДАННЫЕ
        # ------------------------------------------------------------------
        cur.execute("""
        CREATE VIEW IF NOT EXISTS requests AS
        SELECT
            sq.id AS request_id,
            sq.host_id AS host_id,
            h.username AS username,
            h.name AS host_name,
            sq.status AS status,
            gm.message_text AS message_text,
            gm.check_in AS check_in,
            gm.check_out AS check_out,
            sq.sent_at AS sent_at,
            sq.notes AS notes,
            sq.updated_at AS updated_at
        FROM sending_queue sq
        LEFT JOIN hosts h ON sq.host_id = h.id
        LEFT JOIN generated_messages gm ON sq.current_message_id = gm.id;
        """)

        now_iso = datetime.now().isoformat()
        default_settings = [
            ("daily_send_limit", "20"),
            ("delay_min_seconds", "20"),
            ("delay_max_seconds", "45"),
            ("default_check_in", "2026-11-18"),
            ("default_check_out", "2026-11-20"),
            ("default_city", "Tokyo"),
            ("auth_token", "couchsurfing_secure_default_token"),
            ("emergency_stop", "0"),
        ]
        for k, v in default_settings:
            cur.execute(
                "INSERT OR IGNORE INTO app_settings (key, value, updated_at) VALUES (?, ?, ?);",
                (k, v, now_iso),
            )

        cur.execute(
            "UPDATE core_hosts SET state = 'uncertain', qc_notes = 'Recovered from interrupted sending' WHERE LOWER(state) = 'sending';"
        )

        cur.execute("SELECT COUNT(*) FROM compatibility_anchors;")
        if cur.fetchone()[0] == 0:
            default_anchors = [
                ("anchor_photo", "Photography", "Creative & Visual", json.dumps(["photo", "photography", "camera", "street photography", "portrait", "visual art", "photographer"]), "Стрит-фотография, визуальное искусство, композиция, объективы", 2, 1),
                ("anchor_cinema", "Cinema & Filmmaking", "Creative & Visual", json.dumps(["film", "cinema", "movies", "director", "filmmaking", "screenwriting", "video", "documentary", "cannes", "arthouse"]), "Кинематограф, режиссура, авторское кино, Каннский фестиваль", 2, 1),
                ("anchor_mountains", "Mountains / Outdoor / Ski", "Nature & Adventure", json.dumps(["mountain", "mountains", "hiking", "trek", "trekking", "climb", "ski", "skiing", "snowboard", "alps", "nature", "outdoor", "trail", "nepal"]), "Горнолыжный спорт, треккинг в Непале, перевалы, горная природа", 2, 1),
                ("anchor_music", "Music & Sound", "Culture & Arts", json.dumps(["music", "piano", "classical", "ambient", "guitar", "instruments", "concert", "vinyl", "audio", "jazz", "acoustic"]), "Фортепиано, эмбиент, классическая музыка, акустика, джаз", 1, 1),
                ("anchor_tech", "Technology / Startups / AI", "Technology & Business", json.dumps(["tech", "software", "developer", "coding", "programming", "python", "ai", "machine learning", "startup", "engineering", "web"]), "IT-разработка, искусственный интеллект, стартапы, автоматизация", 2, 1),
                ("anchor_food", "Food & Cooking", "Lifestyle", json.dumps(["food", "cook", "cooking", "culinary", "cuisine", "gastronomy", "local food", "coffee", "tea", "wine", "ramen", "baking"]), "Кулинарные традиции, локальные вкусы, домашняя кухня, кофе/чай", 1, 1),
                ("anchor_culture_lang", "Culture & Languages", "Culture & Arts", json.dumps(["language", "languages", "culture", "cultural exchange", "expat", "linguistics", "travel stories", "multilingual", "asia", "hong kong"]), "Межкультурный обмен, языки (EN/FR/RU), жизнь в Азии, путешествия", 1, 1),
            ]
            for a_id, a_name, a_cat, a_kw, a_desc, a_prio, a_act in default_anchors:
                cur.execute(
                    """
                    INSERT OR IGNORE INTO compatibility_anchors
                    (anchor_id, name, category, keywords_json, description, priority, is_active, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (a_id, a_name, a_cat, a_kw, a_desc, a_prio, a_act, now_iso, now_iso),
                )

        # ------------------------------------------------------------------
        # 10. ДИСПЕТЧЕР ОТПРАВОК: ПОПЫТКИ, БЛОКИРОВКИ, ИДЕНТИЧНОСТИ
        # ------------------------------------------------------------------
        cur.execute("""
        CREATE TABLE IF NOT EXISTS sending_attempts (
            id TEXT PRIMARY KEY,
            trip_id TEXT REFERENCES core_trips(id) ON DELETE SET NULL,
            original_trip_id TEXT NOT NULL,
            host_id TEXT NOT NULL,
            username TEXT NOT NULL,
            host_id_norm TEXT NOT NULL,
            username_norm TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'pending'
                CHECK (state IN ('pending','sending','sent','failed','uncertain','already_contacted')),
            send_intent_committed INTEGER NOT NULL DEFAULT 0
                CHECK (send_intent_committed IN (0,1)),
            send_initiated INTEGER NOT NULL DEFAULT 0
                CHECK (send_initiated IN (0,1)),
            http_status INTEGER,
            evidence_type TEXT
                CHECK (evidence_type IN ('none','navigation','form_submission','confirmed_acceptance','legacy','outside_stay_request','existing_conversation','manual_reconciliation')),
            evidence_reference TEXT,
            confirmation_url TEXT,
            error_details TEXT,
            proof_screenshot TEXT,
            created_at TEXT NOT NULL,
            sent_at TEXT,
            updated_at TEXT NOT NULL,
            expires_at TEXT
        );
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_sa_active_per_user ON sending_attempts(host_id_norm, username_norm) WHERE state IN ('sending','uncertain','sent');")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_sa_trip_state ON sending_attempts(trip_id, state);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_sa_unresolved ON sending_attempts(state) WHERE state IN ('sending','uncertain');")

        cur.execute("""
        CREATE TABLE IF NOT EXISTS host_dispatch_locks (
            lock_id TEXT PRIMARY KEY,
            canonical_key TEXT NOT NULL,
            attempt_id TEXT NOT NULL REFERENCES sending_attempts(id) ON DELETE RESTRICT,
            lock_state TEXT NOT NULL CHECK (lock_state IN ('reserved','sending','sent','uncertain','already_contacted','released','failed')),
            trip_id TEXT REFERENCES core_trips(id) ON DELETE SET NULL,
            original_trip_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            expires_at TEXT
        );
        """)
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_hdl_one_active_per_user ON host_dispatch_locks(canonical_key) WHERE lock_state IN ('reserved','sending','sent','uncertain','already_contacted');")
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_hdl_one_active_per_attempt ON host_dispatch_locks(attempt_id) WHERE lock_state IN ('reserved','sending','sent','uncertain','already_contacted');")

        cur.execute("""
        CREATE TABLE IF NOT EXISTS host_identities (
            identity_id TEXT PRIMARY KEY,
            identity_type TEXT NOT NULL CHECK (identity_type IN ('cs_id','username')),
            identity_value TEXT NOT NULL,
            canonical_key TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        """)
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_hi_type_value ON host_identities(identity_type, identity_value);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_hi_canonical ON host_identities(canonical_key);")

        # --- sending_history: добавить новые колонки (идемпотентно) ---
        _ensure_column(cur, "sending_history", "attempt_id", "TEXT")
        _ensure_column(cur, "sending_history", "trip_id", "TEXT")
        _ensure_column(cur, "sending_history", "event_type", "TEXT")
        _ensure_column(cur, "sending_history", "state_at_event", "TEXT")
        _ensure_column(cur, "sending_history", "evidence_type", "TEXT")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_sh_attempt ON sending_history(attempt_id);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_sh_trip ON sending_history(trip_id);")
        cur.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_sh_idempotent_event
            ON sending_history(attempt_id, event_type)
            WHERE attempt_id IS NOT NULL
              AND event_type IN ('RESERVATION','SEND_INTENT','SENT','FAILED_PRE_SUBMIT','UNCERTAIN','RECOVERY_UNCERTAIN','MANUAL_SENT','MANUAL_NOT_SENT','EXISTING_CONVERSATION','OUTSIDE_STAY_REQUEST','LEGACY_CONTACTED');
        """)
        cur.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_sh_attempt_result
            ON sending_history(attempt_id)
            WHERE attempt_id IS NOT NULL AND event_type = 'ATTEMPT_RESULT';
        """)

        # --- sending_attempts: добавить dispatch_outcome (идемпотентно) ---
        _ensure_column(cur, "sending_attempts", "dispatch_outcome", "TEXT")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_sa_dispatch_outcome ON sending_attempts(dispatch_outcome);")

        # --- Таблица отслеживания версий схемы (schema_migrations) ---
        cur.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            migration_id TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL,
            description TEXT
        );
        """)

        # --- Таблица capability токенов подтверждения ---
        cur.execute("""
        CREATE TABLE IF NOT EXISTS confirmation_capabilities (
            attempt_id TEXT PRIMARY KEY,
            canonical_host_key TEXT NOT NULL,
            nonce TEXT NOT NULL,
            cap_token TEXT NOT NULL,
            issued_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            used INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );
        """)

        # Approval records bind a user action to one exact message revision,
        # host identity, and trip. Binding columns are immutable; only status
        # and revocation metadata can change.
        cur.execute("""
        CREATE TABLE IF NOT EXISTS message_approvals (
            approval_id TEXT PRIMARY KEY,
            canonical_host_key TEXT NOT NULL,
            trip_id TEXT NOT NULL,
            host_id TEXT NOT NULL,
            message_id TEXT NOT NULL,
            approved_message_hash TEXT NOT NULL,
            message_version INTEGER NOT NULL,
            approved_at TEXT NOT NULL,
            approval_status TEXT NOT NULL CHECK (approval_status IN ('approved','revoked','stale')),
            created_by TEXT NOT NULL,
            revoked_at TEXT,
            revoke_reason TEXT
        );
        """)
        cur.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_message_approvals_one_active
        ON message_approvals(trip_id, message_id) WHERE approval_status='approved';
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_message_approvals_host ON message_approvals(canonical_host_key, trip_id);")
        cur.execute("""
        CREATE TRIGGER IF NOT EXISTS trg_message_approvals_binding_immutable
        BEFORE UPDATE OF approval_id, canonical_host_key, trip_id, host_id, message_id,
                         approved_message_hash, message_version, approved_at, created_by
        ON message_approvals
        BEGIN
            SELECT RAISE(ABORT, 'message approval binding is immutable');
        END;
        """)
        cur.execute("""
        CREATE TRIGGER IF NOT EXISTS trg_message_approvals_no_delete
        BEFORE DELETE ON message_approvals
        BEGIN
            SELECT RAISE(ABORT, 'message approval records cannot be deleted');
        END;
        """)

        # Регистрация применённых миграций
        _ensure_column(cur, "sending_history", "attempt_id", "TEXT")
        _ensure_column(cur, "sending_history", "trip_id", "TEXT")
        _ensure_column(cur, "sending_history", "event_type", "TEXT")
        _ensure_column(cur, "sending_history", "state_at_event", "TEXT")
        _ensure_column(cur, "sending_history", "evidence_type", "TEXT")

        # Добавить expires_at колонки (идемпотентно)
        _ensure_column(cur, "host_dispatch_locks", "expires_at", "TEXT")
        _ensure_column(cur, "sending_attempts", "expires_at", "TEXT")

        # Записать миграции как применённые
        for mig_id in (
            "001_sending_attempts_host_dispatch_locks_host_identities",
            "002_sending_history_columns",
            "003_dispatch_outcome_column",
            "004_confirmation_capabilities_table",
            "005_expires_at_columns",
            "006_immutable_message_approvals",
        ):
            cur.execute(
                """
                INSERT OR IGNORE INTO schema_migrations (migration_id, applied_at, description)
                VALUES (?, ?, ?);
                """,
                (mig_id, datetime.now().isoformat(), f"Migration {mig_id}"),
            )


        # ------------------------------------------------------------------
        # 11. ПРОФИЛЬНАЯ ИНТЕЛЛЕКТУАЛЬНОСТЬ ХОСТОВ (HOST PROFILE INTELLIGENCE)
        # ------------------------------------------------------------------
        # Основная запись профиля хоста (каноническая, независимая от поездки)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS host_profiles (
            host_id TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            canonical_host_key TEXT,
            profile_url TEXT,
            avatar_url TEXT,
            name TEXT,
            location TEXT,

            verification_status TEXT NOT NULL DEFAULT 'UNKNOWN'
                CHECK (verification_status IN ('VERIFIED','NOT_VERIFIED','UNKNOWN')),
            verification_source TEXT,
            verification_evidence TEXT,
            verification_checked_at TEXT,

            recently_active_status TEXT NOT NULL DEFAULT 'UNKNOWN'
                CHECK (recently_active_status IN ('RECENTLY_ACTIVE','ACTIVE','NOT_RECENTLY_ACTIVE','UNKNOWN')),
            recently_active_raw TEXT,
            recently_active_normalized TEXT,
            last_active_url TEXT,
            recently_active_checked_at TEXT,
            last_seen_update TEXT,

            profile_completeness_pct REAL DEFAULT 0.0,
            sections_collected TEXT,

            first_collected_at TEXT NOT NULL,
            last_successful_collection TEXT,
            last_collection_attempt TEXT,
            collection_version INTEGER DEFAULT 1,

            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_hp_username ON host_profiles(username);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_hp_canonical_key ON host_profiles(canonical_host_key);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_hp_verification ON host_profiles(verification_status);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_hp_recently_active ON host_profiles(recently_active_status);")

        # Снимки профиля (версионирование)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS host_profile_snapshots (
            snapshot_id TEXT PRIMARY KEY,
            host_id TEXT NOT NULL,
            collection_timestamp TEXT NOT NULL,
            raw_profile_json TEXT,
            sections_json TEXT,
            FOREIGN KEY (host_id) REFERENCES host_profiles(host_id) ON DELETE CASCADE
        );
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_hps_host ON host_profile_snapshots(host_id);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_hps_timestamp ON host_profile_snapshots(collection_timestamp);")

        # Бейджи
        cur.execute("""
        CREATE TABLE IF NOT EXISTS host_badges (
            badge_id TEXT PRIMARY KEY,
            host_id TEXT NOT NULL,
            badge_name TEXT NOT NULL,
            badge_text TEXT,
            badge_category TEXT,
            badge_image_url TEXT,
            source_html_attribute TEXT,
            source_url TEXT,
            collected_at TEXT NOT NULL,
            FOREIGN KEY (host_id) REFERENCES host_profiles(host_id) ON DELETE CASCADE,
            UNIQUE(host_id, badge_name, badge_text)
        );
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_hb_host ON host_badges(host_id);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_hb_category ON host_badges(badge_category);")

        # Отзывы (References)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS host_references (
            reference_id TEXT PRIMARY KEY,
            host_id TEXT NOT NULL,
            reference_text TEXT NOT NULL,
            author_username TEXT,
            author_profile_url TEXT,
            reference_date TEXT,
            reference_type TEXT,
            relationship_direction TEXT,
            source_url TEXT,
            collected_at TEXT NOT NULL,
            FOREIGN KEY (host_id) REFERENCES host_profiles(host_id) ON DELETE CASCADE
        );
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_hr_host ON host_references(host_id);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_hr_author ON host_references(author_username);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_hr_date ON host_references(reference_date);")
        _ensure_column(cur, "host_references", "collection_source", "TEXT")

        # Статистика отзывов
        cur.execute("""
        CREATE TABLE IF NOT EXISTS host_reference_stats (
            host_id TEXT PRIMARY KEY,
            total_references INTEGER DEFAULT 0,
            positive_references INTEGER DEFAULT 0,
            neutral_references INTEGER DEFAULT 0,
            negative_references INTEGER DEFAULT 0,
            other_categories TEXT,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (host_id) REFERENCES host_profiles(host_id) ON DELETE CASCADE
        );
        """)

        # Доказательства для определения пола
        cur.execute("""
        CREATE TABLE IF NOT EXISTS host_gender_evidence (
            evidence_id TEXT PRIMARY KEY,
            host_id TEXT NOT NULL,
            evidence_type TEXT NOT NULL
                CHECK (evidence_type IN ('explicit_field','self_identification','pronoun','language_marker','reference')),
            evidence_source TEXT NOT NULL,
            evidence_fragment TEXT NOT NULL,
            inferred_gender TEXT NOT NULL
                CHECK (inferred_gender IN ('MALE','FEMALE','UNKNOWN')),
            confidence REAL NOT NULL DEFAULT 0.5,
            collected_at TEXT NOT NULL,
            FOREIGN KEY (host_id) REFERENCES host_profiles(host_id) ON DELETE CASCADE
        );
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_hge_host ON host_gender_evidence(host_id);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_hge_type ON host_gender_evidence(evidence_type);")

        # Индивидуальные разделы профиля
        cur.execute("""
        CREATE TABLE IF NOT EXISTS host_profile_sections (
            section_id TEXT PRIMARY KEY,
            host_id TEXT NOT NULL,
            section_name TEXT NOT NULL,
            section_text TEXT,
            language TEXT,
            extraction_status TEXT NOT NULL
                CHECK (extraction_status IN ('VALUE','NOT_PRESENT','NOT_AVAILABLE','PARSE_FAILED','NOT_CHECKED')),
            source_url TEXT,
            collected_at TEXT NOT NULL,
            FOREIGN KEY (host_id) REFERENCES host_profiles(host_id) ON DELETE CASCADE,
            UNIQUE(host_id, section_name)
        );
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_hps_section ON host_profile_sections(host_id, section_name);")
        _ensure_column(cur, "host_profile_sections", "extraction_source", "TEXT")
        _ensure_column(cur, "host_profile_sections", "value_collected_at", "TEXT")

        # Лог сбора профилей
        cur.execute("""
        CREATE TABLE IF NOT EXISTS host_profile_collection_log (
            log_id TEXT PRIMARY KEY,
            host_id TEXT NOT NULL,
            section_name TEXT,
            status TEXT NOT NULL
                CHECK (status IN ('SUCCESS','PARTIAL','FAILED')),
            error_details TEXT,
            collected_at TEXT NOT NULL,
            FOREIGN KEY (host_id) REFERENCES host_profiles(host_id) ON DELETE CASCADE
        );
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_hcl_host ON host_profile_collection_log(host_id);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_hcl_status ON host_profile_collection_log(status);")


# Алиас инициализации БД
init_db = init_core_tables


def log_audit(
    trip_id: str,
    module: str,
    event: str,
    host_id: Optional[str] = None,
    details: Optional[Dict[str, Any]] = None,
    db_file: Optional[Path | str] = None,
) -> None:
    """Сквозной аудит всех действий системы (core_audit_logs)."""
    now_iso = datetime.now().isoformat()
    det_str = json.dumps(details or {}, ensure_ascii=False)
    with get_connection(db_file) as conn:
        conn.execute(
            """
            INSERT INTO core_audit_logs (timestamp, trip_id, module, event, host_id, details_json)
            VALUES (?, ?, ?, ?, ?, ?);
            """,
            (now_iso, trip_id or "global", module, event, host_id, det_str),
        )


def get_active_trip(db_file: Optional[Path | str] = None) -> Optional[Trip]:
    """Возвращает текущую активную поездку (Единый корень системы). Возвращает None, если активная поездка не выбрана."""
    with get_connection(db_file) as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM core_trips WHERE is_active = 1 LIMIT 1;")
        row = cur.fetchone()
        if row:
            r_keys = row.keys()
            return Trip(
                id=row["id"],
                url=row["url"] if "url" in r_keys and row["url"] else "",
                country=row["country"] if "country" in r_keys and row["country"] else "Japan",
                city=row["city"],
                arrival_date=row["arrival_date"],
                departure_date=row["departure_date"],
                dates_text=row["dates_text"] if "dates_text" in r_keys else None,
                is_active=bool(row["is_active"]),
                created_at=row["created_at"],
                updated_at=row["updated_at"] if "updated_at" in r_keys else None,
            )
    return None


def set_active_trip(trip_id: str, db_file: Optional[Path | str] = None) -> Optional[Trip]:
    """Атомарно переключает рабочий контекст системы на указанный Trip и синхронизирует user_trips + app_settings."""
    now_iso = datetime.now().isoformat()
    with get_connection(db_file) as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM core_trips WHERE id = ?;", (trip_id,))
        row = cur.fetchone()
        if not row:
            return None

        conn.execute("UPDATE core_trips SET is_active = 0, updated_at = ?;", (now_iso,))
        conn.execute("UPDATE core_trips SET is_active = 1, updated_at = ? WHERE id = ?;", (now_iso, trip_id))
        conn.execute("UPDATE user_trips SET is_active = 0, updated_at = ?;", (now_iso,))
        conn.execute("UPDATE user_trips SET is_active = 1, updated_at = ? WHERE trip_id = ?;", (now_iso, trip_id))

        r_keys = row.keys()
        trip_dict = {
            "trip_id": row["id"],
            "url": row["url"] if "url" in r_keys else "",
            "city": row["city"],
            "arrival_date": row["arrival_date"],
            "departure_date": row["departure_date"],
            "dates_text": row["dates_text"] if "dates_text" in r_keys else None,
            "country": (row["country"] if "country" in r_keys else None) or "Japan",
        }
        conn.execute(
            """
            INSERT INTO app_settings (key, value, updated_at)
            VALUES ('active_trip_context', ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at;
            """,
            (json.dumps(trip_dict, ensure_ascii=False), now_iso),
        )

    log_audit(
        trip_id=trip_id,
        module="TRIP",
        event="ACTIVE_TRIP_SWITCHED",
        details={
            "city": row["city"],
            "country": (row["country"] if "country" in row.keys() else None) or "Japan",
            "dates": f"{row['arrival_date']} - {row['departure_date']}",
        },
        db_file=db_file,
    )
    return get_active_trip(db_file)


def list_trips(db_file: Optional[Path | str] = None) -> List[Trip]:
    """Возвращает все зарегистрированные поездки."""
    with get_connection(db_file) as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM core_trips ORDER BY arrival_date ASC;")
        rows = cur.fetchall()
        return [
            Trip(
                id=r["id"],
                url=r["url"] if "url" in r.keys() and r["url"] else "",
                country=r["country"] if "country" in r.keys() and r["country"] else "Japan",
                city=r["city"],
                arrival_date=r["arrival_date"],
                departure_date=r["departure_date"],
                dates_text=r["dates_text"] if "dates_text" in r.keys() else None,
                is_active=bool(r["is_active"]),
                created_at=r["created_at"],
                updated_at=r["updated_at"] if "updated_at" in r.keys() else None,
            )
            for r in rows
        ]


def save_trip(trip: Trip, db_file: Optional[Path | str] = None) -> Trip:
    """Сохраняет поездку в `core_trips` и синхронизирует `user_trips`."""
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
                trip.id,
                trip.url,
                trip.country,
                trip.city,
                trip.arrival_date,
                trip.departure_date,
                trip.dates_text,
                1 if trip.is_active else 0,
                trip.created_at or now_iso,
                trip.updated_at or now_iso,
            ),
        )
        conn.execute(
            """
            INSERT INTO user_trips (trip_id, city, arrival_date, departure_date, dates_text, url, is_active, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(trip_id) DO UPDATE SET
                city = excluded.city,
                arrival_date = excluded.arrival_date,
                departure_date = excluded.departure_date,
                dates_text = excluded.dates_text,
                url = excluded.url,
                is_active = excluded.is_active,
                updated_at = excluded.updated_at;
            """,
            (
                trip.id,
                trip.city,
                trip.arrival_date,
                trip.departure_date,
                trip.dates_text,
                trip.url,
                1 if trip.is_active else 0,
                trip.created_at or now_iso,
                trip.updated_at or now_iso,
            ),
        )
    return trip


def reset_stuck_sending_statuses(db_file: Optional[Path | str] = None) -> int:
    try:
        with get_connection(db_file) as conn:
            cur = conn.cursor()
            cur.execute(
                "UPDATE core_hosts SET state = 'uncertain', qc_notes = 'Stuck sending status recovered to verification' WHERE LOWER(state) = 'sending';"
            )
            return cur.rowcount
    except Exception as e:
        logger.warning(f"Ошибка сброса зависших статусов 'sending': {e}")
        return 0


def reset_stuck_background_jobs(db_file: Optional[Path | str] = None) -> int:
    try:
        with get_connection(db_file) as conn:
            cur = conn.cursor()
            cur.execute("""
                UPDATE background_jobs
                SET status = 'interrupted',
                    message = 'Server restarted while process was running',
                    finished_at = CURRENT_TIMESTAMP
                WHERE status = 'running';
            """)
            return cur.rowcount
    except Exception as e:
        logger.warning(f"Ошибка сброса зависших задач background_jobs: {e}")
        return 0


def get_setting(key: str, default: Optional[str] = None, db_file: Optional[Path | str] = None) -> Optional[str]:
    try:
        with get_connection(db_file) as conn:
            cur = conn.cursor()
            cur.execute("SELECT value FROM app_settings WHERE key = ?;", (key,))
            row = cur.fetchone()
            return row["value"] if row else default
    except Exception as e:
        logger.error(f"Ошибка чтения настройки {key}: {e}")
        return default


def set_setting(key: str, value: str, db_file: Optional[Path | str] = None) -> None:
    try:
        with get_connection(db_file) as conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at;
                """,
                (key, str(value), datetime.now().isoformat()),
            )
    except Exception as e:
        logger.error(f"Ошибка сохранения настройки {key}: {e}")


def get_sent_usernames(db_file: Optional[Path | str] = None) -> set:
    """
    Возвращает множество имён пользователей, которым этот вариант приложения
    уже отправил Stay Request с подтверждённым принятием.

    Исключает:
    * ALREADY_SENT — хост уже был contacted внешним источником (не этим приложением).
    * LEGACY записи без подтверждённого принятия.

    Это предотвращает ложное подавление на основании внешних контактов.
    """
    sent = set()
    try:
        with get_connection(db_file) as conn:
            cur = conn.cursor()
            # Только core_hosts с state='sent' (установлено через mark_as_sent с capability)
            cur.execute("SELECT username, host_id FROM core_hosts WHERE state = 'sent';")
            for row in cur.fetchall():
                if row["username"]:
                    sent.add(str(row["username"]).strip().lower())
                if row["host_id"]:
                    sent.add(str(row["host_id"]).strip().lower())

            # sending_history: только SENT_SUCCESS с confirmed_acceptance evidence
            # ALREADY_SENT означает внешний контакт, не подтверждение этого приложения
            cur.execute(
                """
                SELECT username, host_id, status, notes, evidence_type
                FROM sending_history
                WHERE UPPER(status) = 'SENT_SUCCESS'
                  AND (evidence_type IS NULL OR evidence_type != 'legacy')
                """
            )
            for row in cur.fetchall():
                st_notes = str(row["notes"] or "").lower()
                # Пропускаем записи, которые явно помечены как внешние контакты
                false_skip = (
                    "detected as already sent" in st_notes
                    and "visible" not in st_notes
                    and "deduplicated" not in st_notes
                )
                if false_skip:
                    continue
                if row["username"]:
                    sent.add(str(row["username"]).strip().lower())
                if row["host_id"]:
                    sent.add(str(row["host_id"]).strip().lower())

            # sending_queue: только записи с подтверждённым sent_at
            cur.execute("""
                SELECT sq.host_id, h.username, sq.sent_at, sq.notes
                FROM sending_queue sq
                LEFT JOIN hosts h ON sq.host_id = h.id
                WHERE LOWER(sq.status) IN ('sent', 'declined', 'completed');
            """)
            for row in cur.fetchall():
                notes = str(row["notes"] or "").lower()
                is_real = bool(row["sent_at"])
                is_visible_already = "already sent" in notes and "visible" in notes
                is_dedup_inbox = "deduplicated" in notes and "inbox" in notes
                is_confirmed = notes.startswith("sent (confirmed")
                # Только реально подтверждённые отправки
                if not (is_real or is_visible_already or is_dedup_inbox or is_confirmed):
                    continue
                if row["username"]:
                    sent.add(str(row["username"]).strip().lower())
                if row["host_id"]:
                    sent.add(str(row["host_id"]).strip().lower())

            cur.execute("SELECT host_username FROM inbox_conversations;")
            for row in cur.fetchall():
                if row["host_username"]:
                    sent.add(str(row["host_username"]).strip().lower())
    except Exception:
        # Queue mutations must not continue with an incomplete/stale sent set.
        # In particular, do not fall through to legacy JSON when SQLite fails.
        logger.exception("Ошибка чтения подтверждённых отправок из SQLite")
        raise

    out_dir = get_output_dir()
    sent_hist_path = out_dir / "sent_history.json"
    if sent_hist_path.exists():
        try:
            with open(sent_hist_path, "r", encoding="utf-8") as f:
                d = json.load(f)
                for u in d.get("sent_usernames", []):
                    if u:
                        sent.add(str(u).strip().lower())
        except Exception:
            pass

    sent_log_path = out_dir / "sent_log.json"
    if sent_log_path.exists():
        try:
            with open(sent_log_path, "r", encoding="utf-8") as f:
                d = json.load(f)
                if isinstance(d, list):
                    for item in d:
                        u = item.get("username") or item.get("host_id") or item.get("id")
                        if not u:
                            continue
                        st = str(item.get("status", "")).upper()
                        notes = str(item.get("notes", "")).lower()
                        false_skip = (
                            "detected as already sent" in notes
                            and "visible" not in notes
                            and "deduplicated" not in notes
                        )
                        if st in ("SENT_SUCCESS", "SUCCESS") and not false_skip:
                            sent.add(str(u).strip().lower())
                        elif st == "ALREADY_SENT" and ("visible" in notes or "deduplicated via inbox" in notes):
                            sent.add(str(u).strip().lower())
        except Exception:
            pass

    return sent


def is_host_already_sent(username_or_id: str, db_file: Optional[Path | str] = None) -> bool:
    if not username_or_id:
        return False
    return username_or_id.strip().lower() in get_sent_usernames(db_file)


def get_inbox_conversations(
    status: Optional[str] = None,
    exclude_system_declined: bool = True,
    db_file: Optional[Path | str] = None,
) -> List[Dict[str, Any]]:
    with get_connection(db_file) as conn:
        cur = conn.cursor()
        if status:
            cur.execute(
                "SELECT * FROM inbox_conversations WHERE status = ? ORDER BY last_message_timestamp DESC;",
                (status,),
            )
        elif exclude_system_declined:
            cur.execute(
                "SELECT * FROM inbox_conversations WHERE status NOT IN ('Declined (System)', 'ARCHIVED') ORDER BY last_message_timestamp DESC;"
            )
        else:
            cur.execute("SELECT * FROM inbox_conversations ORDER BY last_message_timestamp DESC;")
        return [dict(r) for r in cur.fetchall()]


def get_inbox_conversation(conv_id_or_username: Any, db_file: Optional[Path | str] = None) -> Optional[Dict[str, Any]]:
    with get_connection(db_file) as conn:
        cur = conn.cursor()
        if isinstance(conv_id_or_username, int) or (
            isinstance(conv_id_or_username, str) and conv_id_or_username.isdigit()
        ):
            cur.execute("SELECT * FROM inbox_conversations WHERE id = ?;", (int(conv_id_or_username),))
        else:
            cur.execute("SELECT * FROM inbox_conversations WHERE host_username = ?;", (str(conv_id_or_username),))
        row = cur.fetchone()
        return dict(row) if row else None


def save_inbox_conversation(
    host_username: str,
    host_name: str,
    last_message_received: str,
    last_message_timestamp: Optional[str] = None,
    host_avatar_url: Optional[str] = None,
    location: str = "Tokyo, Japan",
    status: str = "Host Replied",
    category: str = "CHAT_INTERESTED",
    suggested_reply: Optional[str] = None,
    thread_history_json: Optional[str] = None,
    db_file: Optional[Path | str] = None,
) -> int:
    now_str = datetime.now().isoformat()
    msg_ts = last_message_timestamp or now_str
    with get_connection(db_file) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO inbox_conversations (
                host_username, host_name, host_avatar_url, location,
                last_message_received, last_message_timestamp, status, category,
                suggested_reply, thread_history_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(host_username) DO UPDATE SET
                host_name = excluded.host_name,
                host_avatar_url = COALESCE(excluded.host_avatar_url, inbox_conversations.host_avatar_url),
                location = excluded.location,
                last_message_received = excluded.last_message_received,
                last_message_timestamp = excluded.last_message_timestamp,
                status = excluded.status,
                category = excluded.category,
                suggested_reply = COALESCE(excluded.suggested_reply, inbox_conversations.suggested_reply),
                thread_history_json = COALESCE(excluded.thread_history_json, inbox_conversations.thread_history_json),
                updated_at = excluded.updated_at;
            """,
            (
                host_username.strip(),
                host_name.strip(),
                host_avatar_url,
                location,
                last_message_received.strip(),
                msg_ts,
                status,
                category,
                suggested_reply,
                thread_history_json,
                now_str,
                now_str,
            ),
        )
        return cur.lastrowid or 0


def update_inbox_conversation(
    conv_id: int,
    status: Optional[str] = None,
    category: Optional[str] = None,
    suggested_reply: Optional[str] = None,
    tone_used: Optional[str] = None,
    db_file: Optional[Path | str] = None,
) -> bool:
    now_str = datetime.now().isoformat()
    fields = ["updated_at = ?"]
    params: List[Any] = [now_str]
    if status is not None:
        fields.append("status = ?")
        params.append(status)
    if category is not None:
        fields.append("category = ?")
        params.append(category)
    if suggested_reply is not None:
        fields.append("suggested_reply = ?")
        params.append(suggested_reply)
    if tone_used is not None:
        fields.append("tone_used = ?")
        params.append(tone_used)
    params.append(conv_id)
    with get_connection(db_file) as conn:
        cur = conn.cursor()
        cur.execute(f"UPDATE inbox_conversations SET {', '.join(fields)} WHERE id = ?;", tuple(params))
        return cur.rowcount > 0


def mark_host_declined(host_username: str, db_file: Optional[Path | str] = None) -> None:
    if not host_username:
        return
    u = host_username.strip()
    now_iso = datetime.now().isoformat()
    with get_connection(db_file) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE core_hosts
            SET state = 'skipped', qc_notes = 'Declined', updated_at = ?
            WHERE LOWER(username) = LOWER(?) OR LOWER(host_id) = LOWER(?);
            """,
            (now_iso, u, u),
        )


def get_host_dossier(host_id: str, db_file: Optional[Path | str] = None) -> Optional[Dict[str, Any]]:
    if not host_id:
        return None
    with get_connection(db_file) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM hosts WHERE LOWER(id) = LOWER(?) OR LOWER(username) = LOWER(?);",
            (host_id, host_id),
        )
        host_row = cur.fetchone()
        if host_row:
            h_data = dict(host_row)
            actual_id = h_data.get("id") or host_id
            u_name = h_data.get("username") or host_id
        else:
            cur.execute(
                "SELECT * FROM core_hosts WHERE LOWER(host_id) = LOWER(?) OR LOWER(username) = LOWER(?) LIMIT 1;",
                (host_id, host_id),
            )
            ch_row = cur.fetchone()
            if not ch_row:
                return None
            ch = dict(ch_row)
            actual_id = ch.get("host_id") or host_id
            u_name = ch.get("username") or host_id
            h_data = {
                "id": actual_id,
                "username": u_name,
                "name": ch.get("name") or u_name,
                "clean_name": (ch.get("name") or u_name).split()[0],
                "location": ch.get("location") or "",
                "about_me": ch.get("about") or "",
                "about_me_clean": ch.get("about") or "",
            }

        cur.execute("SELECT * FROM profile_analyses WHERE host_id = ? OR host_id = ?;", (actual_id, u_name))
        pa_row = cur.fetchone()
        analysis = dict(pa_row) if pa_row else {}

        cur.execute(
            """
            SELECT interest_name, category, source, confirming_facts_json, confidence, status
            FROM host_interests
            WHERE host_id = ? OR host_id = ?
            ORDER BY confidence DESC;
            """,
            (actual_id, u_name),
        )
        interests = []
        for r in cur.fetchall():
            facts = []
            if r["confirming_facts_json"]:
                try:
                    facts = json.loads(r["confirming_facts_json"])
                except Exception:
                    facts = []
            interests.append({
                "name": r["interest_name"],
                "category": r["category"],
                "source": r["source"],
                "facts": facts,
                "confidence": float(r["confidence"] or 0.5),
                "status": r["status"] or "uncertain",
            })

        cur.execute(
            """
            SELECT place_name, place_type, country, city, source, confidence
            FROM host_places
            WHERE host_id = ? OR host_id = ?
            ORDER BY confidence DESC;
            """,
            (actual_id, u_name),
        )
        places = [
            {
                "name": r["place_name"],
                "type": r["place_type"],
                "country": r["country"],
                "city": r["city"],
                "source": r["source"],
                "confidence": float(r["confidence"] or 0.5),
            }
            for r in cur.fetchall()
        ]

        cur.execute(
            """
            SELECT vo.observation, vo.category, vo.evidence_confidence, pa.local_path, pa.model
            FROM visual_observations vo
            LEFT JOIN photo_assets pa ON vo.photo_asset_id = pa.id
            WHERE vo.host_id = ? OR vo.host_id = ?
            ORDER BY vo.evidence_confidence DESC;
            """,
            (actual_id, u_name),
        )
        visual_obs = [
            {
                "observation": r["observation"],
                "category": r["category"],
                "confidence": float(r["evidence_confidence"] or 0.9),
                "photo_path": r["local_path"],
            }
            for r in cur.fetchall()
        ]

        return {
            "host_id": actual_id,
            "username": h_data.get("username"),
            "name": h_data.get("name"),
            "clean_name": h_data.get("clean_name"),
            "location": h_data.get("location"),
            "about_me": h_data.get("about_me"),
            "about_me_clean": h_data.get("about_me_clean"),
            "analysis": analysis,
            "interests": interests,
            "places": places,
            "visual_observations": visual_obs,
        }


def get_host_dossier_v2(host_id: str, db_file: Optional[Path | str] = None) -> Optional[Dict[str, Any]]:
    return get_host_dossier(host_id, db_file=db_file)


def backup_database(dest_path: Optional[Path | str] = None, source_db: Optional[Path | str] = None) -> Path:
    """Создает атомарный online-бэкап базы данных через sqlite3.Connection.backup()."""
    src = get_db_path(source_db) if source_db else get_db_path()
    if not dest_path:
        backup_dir = get_output_dir() / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        dst = get_db_path(backup_dir / f"db_backup_{ts}.db")
    else:
        dst = get_db_path(dest_path)
        dst.parent.mkdir(parents=True, exist_ok=True)

    with get_connection(src) as src_conn:
        with sqlite3.connect(dst) as dst_conn:
            src_conn.backup(dst_conn)
    return dst


backup_db = backup_database
