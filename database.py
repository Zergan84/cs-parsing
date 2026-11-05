"""
database.py - Высоконадежный слой персистентности на SQLite для платформы Couchsurfing Outreach.

Обеспечивает:
1. WAL-режим (Write-Ahead Logging) для конкурентного чтения и бесконфликтной записи.
2. Транзакционную целостность и защиту от race conditions при параллельных процессах.
3. Полную схему сущностей:
   - personal_context_facts: структурированная база знаний пользователя.
   - hosts: собранные хосты Couchsurfing.
   - profile_analyses: результаты глубокого анализа, хуки, совместимость и стратегии.
   - generated_messages: сгенерированные тексты с версионированием и QC.
   - sending_queue: очередь с конечным автоматом статусов (Draft -> Approved -> Queued -> Sending -> Sent).
   - sending_history: строгий аудиторский лог отправок с доказательствами (пруфами).
   - parser_jobs: история и статус задач сбора данных.
   - app_settings: хранилище параметров приложения и лимитов.
"""

import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional, Tuple

logger = logging.getLogger("Database")

BASE_DIR = Path(__file__).parent
OUTPUT_DIR = BASE_DIR / "output"
DB_PATH = OUTPUT_DIR / "couchsurfing_platform.db"


def get_db_connection() -> sqlite3.Connection:
    """Создает потокобезопасное подключение к SQLite с поддержкой WAL и foreign keys."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=30.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=15000;")
    return conn


@contextmanager
def db_session() -> Generator[sqlite3.Connection, None, None]:
    """Контекстный менеджер транзакции базы данных."""
    conn = get_db_connection()
    try:
        yield conn
        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.error(f"Ошибка транзакции базы данных: {e}")
        raise
    finally:
        conn.close()


def init_db() -> None:
    """Инициализация схемы базы данных со всеми индексами и начальными таблицами."""
    with db_session() as conn:
        cursor = conn.cursor()

        # 1. Личный контекст пользователя (Personal Context)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS personal_context_facts (
            fact_id TEXT PRIMARY KEY,
            category TEXT NOT NULL,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'confirmed', -- confirmed | assumption | temporary | outdated
            source TEXT DEFAULT 'user_curated',
            confidence REAL DEFAULT 1.0,
            is_active INTEGER DEFAULT 1,
            valid_until TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_pcf_category ON personal_context_facts(category);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_pcf_active ON personal_context_facts(is_active);")

        # 2. Хосты Couchsurfing
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS hosts (
            id TEXT PRIMARY KEY, -- username or id
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
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_hosts_username ON hosts(username);")

        # 3. Интеллектуальный анализ профиля хоста
        cursor.execute("""
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
            conversation_hooks_json TEXT, -- List[str]
            compatibility_points_json TEXT, -- List[Dict]
            conversation_strategy TEXT,
            analyzed_at TEXT NOT NULL,
            FOREIGN KEY (host_id) REFERENCES hosts(id) ON DELETE CASCADE
        );
        """)

        # 4. Сгенерированные персонализированные сообщения
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS generated_messages (
            id TEXT PRIMARY KEY,
            host_id TEXT NOT NULL,
            check_in TEXT NOT NULL,
            check_out TEXT NOT NULL,
            message_text TEXT NOT NULL,
            word_count INTEGER NOT NULL,
            hook_used TEXT,
            context_facts_used_json TEXT, -- List of fact_ids
            qc_passed INTEGER DEFAULT 1,
            qc_notes TEXT,
            is_active INTEGER DEFAULT 1,
            created_at TEXT NOT NULL,
            FOREIGN KEY (host_id) REFERENCES hosts(id) ON DELETE CASCADE
        );
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_gm_host ON generated_messages(host_id);")

        # 5. Очередь отправки (Sending Queue) с конечным автоматом
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS sending_queue (
            id TEXT PRIMARY KEY,
            host_id TEXT NOT NULL UNIQUE,
            current_message_id TEXT,
            status TEXT NOT NULL DEFAULT 'Draft', -- Draft | Approved | Queued | Sending | Sent | Verification_Required | Failed | Skipped
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
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_sq_status ON sending_queue(status);")

        # 6. Аудиторский лог отправок (Sending History)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS sending_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            host_id TEXT NOT NULL,
            username TEXT NOT NULL,
            status TEXT NOT NULL, -- SENT_SUCCESS | DRY_RUN_SUCCESS | FAILED | VERIFICATION_REQUIRED
            http_status INTEGER,
            mode TEXT NOT NULL, -- live | dry_run
            proof_screenshot TEXT,
            before_screenshot TEXT,
            button_selector TEXT,
            notes TEXT,
            error_details TEXT,
            timestamp TEXT NOT NULL,
            FOREIGN KEY (host_id) REFERENCES hosts(id) ON DELETE CASCADE
        );
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_sh_username ON sending_history(username);")

        # 7. Задачи фоновых процессов (Parser / Sender Jobs)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS background_jobs (
            job_id TEXT PRIMARY KEY,
            job_type TEXT NOT NULL, -- scraper | filter | auto_sender | queue_regen
            status TEXT NOT NULL DEFAULT 'running', -- running | paused | completed | aborted | failed
            parameters_json TEXT,
            progress_current INTEGER DEFAULT 0,
            progress_total INTEGER DEFAULT 0,
            message TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT
        );
        """)

        # 8. Настройки приложения (Application Settings)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """)

        # 9. Входящие диалоги и ответы хостов (Inbox Conversations)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS inbox_conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            host_username TEXT UNIQUE NOT NULL,
            host_name TEXT NOT NULL,
            host_avatar_url TEXT,
            location TEXT DEFAULT 'Tokyo, Japan',
            last_message_received TEXT NOT NULL,
            last_message_timestamp TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'Host Replied', -- 'Host Replied', 'Declined (System)', 'DRAFT_GENERATED', 'ANSWERED', 'ARCHIVED'
            category TEXT DEFAULT 'CHAT_INTERESTED', -- ACCEPTED | CHAT_INTERESTED | QUESTION | DECLINED_POLITE | DECLINED_SYSTEM
            suggested_reply TEXT,
            thread_history_json TEXT, -- JSON array of messages
            tone_used TEXT DEFAULT 'normal',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_ic_username ON inbox_conversations(host_username);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_ic_status ON inbox_conversations(status);")

        # Миграция для поля category при необходимости
        cursor.execute("PRAGMA table_info(inbox_conversations);")
        ic_cols = [r[1] for r in cursor.fetchall()]
        if "category" not in ic_cols:
            cursor.execute("ALTER TABLE inbox_conversations ADD COLUMN category TEXT DEFAULT 'CHAT_INTERESTED';")

        # Начальные настройки по умолчанию
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
            cursor.execute(
                "INSERT OR IGNORE INTO app_settings (key, value, updated_at) VALUES (?, ?, ?);",
                (k, v, datetime.now().isoformat()),
            )

        # Очистка зависших статусов 'sending' при инициализации
        cursor.execute("UPDATE sending_queue SET status = 'Approved' WHERE LOWER(status) = 'sending';")

    logger.info("База данных SQLite успешно инициализирована с таблицами и индексами.")


def reset_stuck_sending_statuses() -> int:
    """Сбрасывает зависшие статусы 'sending' обратно в 'Approved'."""
    try:
        with db_session() as conn:
            cur = conn.cursor()
            cur.execute("UPDATE sending_queue SET status = 'Approved' WHERE LOWER(status) = 'sending';")
            count = cur.rowcount
            return count
    except Exception as e:
        logger.warning(f"Ошибка сброса зависших статусов 'sending': {e}")
        return 0



def get_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    """Получает значение настройки из таблицы app_settings."""
    try:
        with db_session() as conn:
            cur = conn.cursor()
            cur.execute("SELECT value FROM app_settings WHERE key = ?;", (key,))
            row = cur.fetchone()
            return row["value"] if row else default
    except Exception as e:
        logger.error(f"Ошибка чтения настройки {key}: {e}")
        return default


def set_setting(key: str, value: str) -> None:
    """Устанавливает значение настройки в таблице app_settings."""
    try:
        with db_session() as conn:
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


def get_sent_usernames() -> set:
    """
    Возвращает множество username и host_id всех хостов,
    которым уже было отправлено сообщение (или статус 'Sent').
    Сверяется с:
    1. Таблицей sending_history в SQLite.
    2. Таблицей sending_queue со статусом LIKE 'Sent%'.
    3. Файлом output/sent_history.json.
    4. Файлом output/sent_log.json.
    5. Файлом output/requests_queue.json (статус Sent).
    """
    sent = set()

    # 1. Из SQLite
    try:
        with db_session() as conn:
            cur = conn.cursor()
            cur.execute("SELECT username, host_id FROM sending_history;")
            for row in cur.fetchall():
                if row["username"]:
                    sent.add(str(row["username"]).strip().lower())
                if row["host_id"]:
                    sent.add(str(row["host_id"]).strip().lower())
            cur.execute("""
                SELECT sq.host_id, h.username 
                FROM sending_queue sq 
                LEFT JOIN hosts h ON sq.host_id = h.id 
                WHERE sq.status LIKE 'Sent%';
            """)
            for row in cur.fetchall():
                if row["username"]:
                    sent.add(str(row["username"]).strip().lower())
                if row["host_id"]:
                    sent.add(str(row["host_id"]).strip().lower())
    except Exception as e:
        logger.debug(f"Ошибка чтения отправленных из SQLite: {e}")

    # 2. Из sent_history.json
    sent_hist_path = OUTPUT_DIR / "sent_history.json"
    if sent_hist_path.exists():
        try:
            with open(sent_hist_path, "r", encoding="utf-8") as f:
                d = json.load(f)
                for u in d.get("sent_usernames", []):
                    if u:
                        sent.add(str(u).strip().lower())
        except Exception:
            pass

    # 3. Из sent_log.json
    sent_log_path = OUTPUT_DIR / "sent_log.json"
    if sent_log_path.exists():
        try:
            with open(sent_log_path, "r", encoding="utf-8") as f:
                d = json.load(f)
                if isinstance(d, list):
                    for item in d:
                        u = item.get("username") or item.get("host_id")
                        if u:
                            sent.add(str(u).strip().lower())
        except Exception:
            pass

    # 4. Из requests_queue.json
    queue_path = OUTPUT_DIR / "requests_queue.json"
    if queue_path.exists():
        try:
            with open(queue_path, "r", encoding="utf-8") as f:
                q = json.load(f)
                if isinstance(q, list):
                    for item in q:
                        if str(item.get("status", "")).startswith("Sent"):
                            u = item.get("username") or item.get("id")
                            if u:
                                sent.add(str(u).strip().lower())
        except Exception:
            pass

    return sent


def is_host_already_sent(username_or_id: str) -> bool:
    """Проверяет, было ли уже отправлено сообщение хосту."""
    if not username_or_id:
        return False
    return username_or_id.strip().lower() in get_sent_usernames()


def get_inbox_conversations(status: Optional[str] = None, exclude_system_declined: bool = True) -> List[Dict[str, Any]]:
    """Возвращает список всех входящих диалогов."""
    with db_session() as conn:
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
        rows = cur.fetchall()
        return [dict(r) for r in rows]


def get_inbox_conversation(conv_id_or_username: Any) -> Optional[Dict[str, Any]]:
    """Получает диалог по id или username."""
    with db_session() as conn:
        cur = conn.cursor()
        if isinstance(conv_id_or_username, int) or (isinstance(conv_id_or_username, str) and conv_id_or_username.isdigit()):
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
) -> int:
    """Создает или обновляет диалог в таблице inbox_conversations."""
    now_str = datetime.now().isoformat()
    msg_ts = last_message_timestamp or now_str
    with db_session() as conn:
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
) -> bool:
    """Обновляет статус, категорию и/или драфт ответа в диалоге."""
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
    with db_session() as conn:
        cur = conn.cursor()
        cur.execute(f"UPDATE inbox_conversations SET {', '.join(fields)} WHERE id = ?;", tuple(params))
        return cur.rowcount > 0


def mark_host_declined(host_username: str) -> None:
    """Обновляет статус хоста в sending_queue на Declined при системном или вежливом отказе."""
    if not host_username:
        return
    with db_session() as conn:
        cur = conn.cursor()
        cur.execute("""
            UPDATE sending_queue 
            SET status = 'Declined'
            WHERE host_id IN (
                SELECT id FROM hosts WHERE LOWER(username) = LOWER(?) OR LOWER(id) = LOWER(?)
            ) OR LOWER(host_id) = LOWER(?);
        """, (host_username.strip(), host_username.strip(), host_username.strip()))


if __name__ == "__main__":
    init_db()
    print("✅ База данных couchsurfing_platform.db успешно создана.")
