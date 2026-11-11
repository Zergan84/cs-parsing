"""
gender_enricher.py - Модуль точечного обогащения данных о поле (Gender) из блока Overview полного профиля Couchsurfing.

Ключевые принципы:
1. Источник - только явное поле Gender в блоке Overview страницы профиля (data-testid="gender-list-item-title" или Overview card).
2. Нормализация строго: 'Female' / 'Male' / 'Couple' / 'Unknown'.
3. Никаких догадок по имени, фото, внешности или AI-предположений.
4. Существующие подтверждённые значения (Female, Male, Couple) не перезаписываются.
5. Прогресс сохраняется в таблице gender_enrichment_state в SQLite:
   - status: 'success' (обработано, найдено или подтверждено отсутствие) / 'error' (сбой сети/таймаут)
   - возможность остановить и продолжить в любой момент
   - повторно успешно обработанные профили не запрашиваются
"""

import json
import logging
import os
import re
import sqlite3
import threading
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from database import DB_PATH, get_db_connection

logger = logging.getLogger("gender_enricher")
logger.setLevel(logging.INFO)
if not logger.handlers:
    ch = logging.StreamHandler()
    ch.setFormatter(logging.Formatter("[%(asctime)s][%(levelname)s][gender_enricher] %(message)s"))
    logger.addHandler(ch)

USER_DATA_DIR = Path(__file__).resolve().parent / "user_data"
AUTH_STATE_FILE = USER_DATA_DIR / "auth_state.json"


def init_enrichment_schema(conn: sqlite3.Connection) -> None:
    """Создает таблицу gender_enrichment_state, если она не существует."""
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS gender_enrichment_state (
            host_id TEXT PRIMARY KEY,
            username TEXT,
            status TEXT NOT NULL,          -- 'success' или 'error'
            gender_found TEXT,              -- 'Female', 'Male', 'Couple', 'Unknown'
            raw_overview_text TEXT,         -- Исходный текст из блока Overview (для верификации)
            error_message TEXT,
            checked_at DATETIME NOT NULL
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_gender_enrichment_status ON gender_enrichment_state (status)")
    conn.commit()


def get_cookie_header() -> str:
    """Извлекает cookies сессии Couchsurfing из auth_state.json."""
    if not AUTH_STATE_FILE.exists():
        logger.warning(f"Файл сессии {AUTH_STATE_FILE} не найден")
        return ""
    try:
        with open(AUTH_STATE_FILE, "r", encoding="utf-8") as f:
            auth_data = json.load(f)
        cookies = auth_data.get("cookies", [])
        cs_cookies = [
            f"{c['name']}={c['value']}"
            for c in cookies
            if "couchsurfing" in c.get("domain", "")
        ]
        return "; ".join(cs_cookies)
    except Exception as e:
        logger.error(f"Ошибка чтения cookies: {e}")
        return ""


def extract_gender_from_overview(html: str) -> Tuple[str, Optional[str]]:
    """
    Извлекает Gender строго из блока Overview полного профиля Couchsurfing.
    Возвращает (нормализованный_пол, исходный_текст_overview).
    Нормализованный пол: 'Female' | 'Male' | 'Couple' | 'Unknown'
    """
    if not html:
        return "Unknown", None

    # 1. Основной селектор Next.js: data-testid="gender-list-item-title"
    m_title = re.search(
        r'data-testid=[\"\']gender-list-item-title[\"\'][^>]*>([^<]+)<',
        html,
        re.IGNORECASE
    )
    if m_title:
        raw_text = m_title.group(1).strip()
        raw_lower = raw_text.lower()
        if raw_lower in ("woman", "female"):
            return "Female", raw_text
        if raw_lower in ("man", "male"):
            return "Male", raw_text
        if raw_lower in ("couple", "duo"):
            return "Couple", raw_text
        return "Unknown", raw_text

    # 2. Альтернативный контейнер Overview: data-testid="gender-list-item"
    m_item = re.search(
        r'data-testid=[\"\']gender-list-item[\"\'][^>]*>(.*?)</div>',
        html,
        re.IGNORECASE | re.DOTALL
    )
    if m_item:
        inner = re.sub(r'<[^>]+>', ' ', m_item.group(1))
        tokens = [t.strip() for t in inner.split() if t.strip() and t.lower() not in ("gender",)]
        if tokens:
            raw_text = tokens[0]
            raw_lower = raw_text.lower()
            if raw_lower in ("woman", "female"):
                return "Female", raw_text
            if raw_lower in ("man", "male"):
                return "Male", raw_text
            if raw_lower in ("couple", "duo"):
                return "Couple", raw_text
            return "Unknown", raw_text

    return "Unknown", None


def fetch_profile_html(username: str, cookie_header: str, timeout: int = 12) -> Tuple[int, str]:
    """Выполняет защищённый HTTP-запрос к профилю Couchsurfing."""
    url = f"https://www.couchsurfing.com/c/users/{username}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if cookie_header:
        headers["Cookie"] = cookie_header

    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
            return resp.status, html
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception as e:
        logger.warning(f"Ошибка загрузки профиля {username}: {e}")
        return 0, ""


def process_single_host(
    host_id: str,
    username: str,
    cookie_header: str,
    conn: sqlite3.Connection,
    dry_run: bool = False
) -> Dict[str, Any]:
    """
    Обрабатывает один профиль:
    - Извлекает Gender из Overview
    - Сохраняет результат в SQLite (gender_enrichment_state и profile_analyses)
    - Не перезаписывает подтверждённые значения
    """
    c = conn.cursor()

    # Проверяем существующее значение в profile_analyses
    c.execute("SELECT estimated_gender, host_type FROM profile_analyses WHERE host_id = ?", (host_id,))
    row = c.fetchone()
    current_gender = (row[0] if row else "Unknown") or "Unknown"
    host_type = (row[1] if row else "") or ""

    # Если уже точно известно Female, Male или Couple — не трогаем
    if current_gender in ("Female", "Male", "Couple") and not dry_run:
        return {
            "host_id": host_id,
            "username": username,
            "status": "skipped",
            "gender": current_gender,
            "reason": "already_determined"
        }

    status_code, html = fetch_profile_html(username, cookie_header)
    now = datetime.now().isoformat()

    if status_code != 200 or not html:
        err_msg = f"HTTP {status_code}" if status_code else "Connection failed"
        if not dry_run:
            c.execute("""
                INSERT INTO gender_enrichment_state (host_id, username, status, gender_found, raw_overview_text, error_message, checked_at)
                VALUES (?, ?, 'error', NULL, NULL, ?, ?)
                ON CONFLICT(host_id) DO UPDATE SET
                    status = 'error',
                    error_message = excluded.error_message,
                    checked_at = excluded.checked_at
            """, (host_id, username, err_msg, now))
            conn.commit()
        return {
            "host_id": host_id,
            "username": username,
            "status": "error",
            "error": err_msg
        }

    # Извлекаем Gender из блока Overview
    extracted_gender, raw_overview = extract_gender_from_overview(html)

    # Если профиль помечен как пара в host_type и Gender не указан отдельно
    if extracted_gender == "Unknown" and host_type.lower() in ("couple", "duo"):
        extracted_gender = "Couple"
        raw_overview = raw_overview or "Account Host Type: Couple"

    if not dry_run:
        # 1. Сохраняем состояние обработки
        c.execute("""
            INSERT INTO gender_enrichment_state (host_id, username, status, gender_found, raw_overview_text, error_message, checked_at)
            VALUES (?, ?, 'success', ?, ?, NULL, ?)
            ON CONFLICT(host_id) DO UPDATE SET
                status = 'success',
                gender_found = excluded.gender_found,
                raw_overview_text = excluded.raw_overview_text,
                error_message = NULL,
                checked_at = excluded.checked_at
        """, (host_id, username, extracted_gender, raw_overview, now))

        # 2. Если пол явно установлен (Female, Male, Couple) — обновляем profile_analyses
        if extracted_gender in ("Female", "Male", "Couple"):
            c.execute("""
                UPDATE profile_analyses
                SET estimated_gender = ?
                WHERE host_id = ?
            """, (extracted_gender, host_id))

        conn.commit()

    return {
        "host_id": host_id,
        "username": username,
        "status": "success",
        "gender": extracted_gender,
        "raw_overview": raw_overview,
        "contained_gender": extracted_gender in ("Female", "Male", "Couple")
    }


def run_test_sample(sample_size: int = 40, trip_id: Optional[str] = None) -> Dict[str, Any]:
    """
    Запускает тестовое обогащение на 30–50 хостах со значением Unknown.
    Возвращает полную статистику и детальные образцы верификации.
    """
    cookie_header = get_cookie_header()
    conn = get_db_connection()
    init_enrichment_schema(conn)

    c = conn.cursor()
    query = """
        SELECT h.id, h.username, h.name
        FROM hosts h
        JOIN trip_hosts th ON h.id = th.host_id
        JOIN profile_analyses pa ON h.id = pa.host_id
        LEFT JOIN gender_enrichment_state ges ON h.id = ges.host_id
        WHERE pa.estimated_gender = 'Unknown'
          AND (ges.status IS NULL OR ges.status = 'error')
    """
    params = []
    if trip_id:
        query += " AND th.trip_id = ?"
        params.append(trip_id)

    query += " ORDER BY h.id ASC LIMIT ?"
    params.append(sample_size)

    c.execute(query, params)
    targets = c.fetchall()

    logger.info(f"Начало тестового обогащения: {len(targets)} хостов со значением Unknown")

    stats = {
        "total_attempted": len(targets),
        "opened_successfully": 0,
        "contained_gender": 0,
        "female_count": 0,
        "male_count": 0,
        "couple_count": 0,
        "remained_unknown": 0,
        "error_count": 0,
        "sample_verifications": []
    }

    for idx, (hid, username, name) in enumerate(targets, 1):
        res = process_single_host(hid, username, cookie_header, conn)
        status = res.get("status")

        if status == "success":
            stats["opened_successfully"] += 1
            gender = res.get("gender")
            raw_overview = res.get("raw_overview")

            if gender == "Female":
                stats["contained_gender"] += 1
                stats["female_count"] += 1
            elif gender == "Male":
                stats["contained_gender"] += 1
                stats["male_count"] += 1
            elif gender == "Couple":
                stats["contained_gender"] += 1
                stats["couple_count"] += 1
            else:
                stats["remained_unknown"] += 1

            if len(stats["sample_verifications"]) < 12 or gender != "Unknown":
                stats["sample_verifications"].append({
                    "username": username,
                    "name": name,
                    "gender_in_overview": raw_overview if raw_overview else "Not displayed",
                    "final_gender": gender,
                    "status": "OK"
                })
        else:
            stats["error_count"] += 1
            stats["sample_verifications"].append({
                "username": username,
                "name": name,
                "gender_in_overview": None,
                "final_gender": "Error",
                "status": res.get("error", "Unknown error")
            })

        time.sleep(1.2)  # Вежливая задержка между запросами

    conn.close()
    return stats


class BackgroundGenderWorker:
    """
    Управляемый фоновый воркер для постепенного обогащения всех оставшихся хостов.
    Поддерживает старт, остановку, возобновление и запрос прогресса.
    """
    def __init__(self):
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._is_running = False
        self._lock = threading.Lock()
        self._trip_id: Optional[str] = None
        self._delay_seconds = 1.3

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._is_running

    def get_progress(self, trip_id: Optional[str] = None) -> Dict[str, Any]:
        """Возвращает текущий прогресс из SQLite."""
        conn = get_db_connection()
        init_enrichment_schema(conn)
        c = conn.cursor()

        # Всего хостов в трипе со статусом Unknown (или в целом)
        trip_filter = "AND th.trip_id = ?" if trip_id else ""
        params = [trip_id] if trip_id else []

        c.execute(f"""
            SELECT COUNT(*)
            FROM hosts h
            JOIN trip_hosts th ON h.id = th.host_id
            JOIN profile_analyses pa ON h.id = pa.host_id
            WHERE 1=1 {trip_filter}
        """, params)
        total_hosts = c.fetchone()[0]

        c.execute(f"""
            SELECT COUNT(*)
            FROM hosts h
            JOIN trip_hosts th ON h.id = th.host_id
            JOIN gender_enrichment_state ges ON h.id = ges.host_id
            WHERE ges.status = 'success' {trip_filter}
        """, params)
        processed_count = c.fetchone()[0]

        c.execute(f"""
            SELECT COUNT(*)
            FROM hosts h
            JOIN trip_hosts th ON h.id = th.host_id
            JOIN gender_enrichment_state ges ON h.id = ges.host_id
            WHERE ges.status = 'error' {trip_filter}
        """, params)
        error_count = c.fetchone()[0]

        # Текущее распределение полов
        c.execute(f"""
            SELECT pa.estimated_gender, COUNT(*)
            FROM hosts h
            JOIN trip_hosts th ON h.id = th.host_id
            JOIN profile_analyses pa ON h.id = pa.host_id
            WHERE 1=1 {trip_filter}
            GROUP BY pa.estimated_gender
        """, params)
        dist = {r[0]: r[1] for r in c.fetchall()}

        conn.close()
        return {
            "is_running": self.is_running,
            "total_hosts": total_hosts,
            "processed": processed_count,
            "errors": error_count,
            "remaining": max(0, total_hosts - processed_count),
            "distribution": dist
        }

    def start(self, trip_id: Optional[str] = None, delay_seconds: float = 1.3) -> bool:
        """Запускает фоновую обработку."""
        with self._lock:
            if self._is_running:
                return False
            self._is_running = True
            self._stop_event.clear()
            self._trip_id = trip_id
            self._delay_seconds = delay_seconds
            self._thread = threading.Thread(target=self._run_loop, daemon=True)
            self._thread.start()
            return True

    def stop(self) -> bool:
        """Останавливает фоновую обработку."""
        with self._lock:
            if not self._is_running:
                return False
            self._stop_event.set()
            self._is_running = False
            return True

    def _run_loop(self):
        logger.info(f"Фоновый воркер GenderEnrichment запущен для трипа: {self._trip_id}")
        cookie_header = get_cookie_header()
        conn = get_db_connection()
        init_enrichment_schema(conn)

        try:
            while not self._stop_event.is_set():
                c = conn.cursor()
                query = """
                    SELECT h.id, h.username, h.name
                    FROM hosts h
                    JOIN trip_hosts th ON h.id = th.host_id
                    JOIN profile_analyses pa ON h.id = pa.host_id
                    LEFT JOIN gender_enrichment_state ges ON h.id = ges.host_id
                    WHERE pa.estimated_gender = 'Unknown'
                      AND (ges.status IS NULL OR ges.status = 'error')
                """
                params = []
                if self._trip_id:
                    query += " AND th.trip_id = ?"
                    params.append(self._trip_id)

                query += " ORDER BY h.id ASC LIMIT 10"
                c.execute(query, params)
                batch = c.fetchall()

                if not batch:
                    logger.info("Все доступные хосты обработаны.")
                    break

                for hid, username, name in batch:
                    if self._stop_event.is_set():
                        break
                    process_single_host(hid, username, cookie_header, conn)
                    time.sleep(self._delay_seconds)

        except Exception as e:
            logger.error(f"Ошибка в фоновом воркере GenderEnrichment: {e}")
        finally:
            with self._lock:
                self._is_running = False
            conn.close()
            logger.info("Фоновый воркер GenderEnrichment завершил работу.")


# Глобальный экземпляр воркера
enrichment_worker = BackgroundGenderWorker()
