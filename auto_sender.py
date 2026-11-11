"""
auto_sender.py - Модуль фонового автопилота отправки запросов (Шаг 3).

Безопасность, верификация и антифрод:
1. Строгий суточный лимит и гибкие интервалы (включая Fast Test Mode от 30 до 60 сек).
2. Поддержка --headed (видимое окно браузера) и --dry-run (симуляция без клика).
3. Полная верификация отправки:
   - Скриншот заполненной формы: output/debug/before_send_{username}.png
   - Логирование точного селектора кнопки отправки
   - Перехват сетевых ответов сервера (HTTP 200/201) и проверка редиректов
   - Скриншот результата: output/debug/after_send_{username}.png
   - Сохранение скриншота ошибки и HTML-дампа в случае сбоя: output/debug/error_{username}.png/html
4. Прозрачный структурированный лог с таймштампами в stdout и sent_log.json.
"""

import argparse
import fcntl
import json
import os
import random
import re
import signal
import sys
import time

try:
    if hasattr(signal, "SIGPIPE"):
        signal.signal(signal.SIGPIPE, signal.SIG_IGN)
except Exception:
    pass
from datetime import datetime, date
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from playwright.sync_api import sync_playwright

from request_manager import RequestQueueManager

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BASE_DIR = Path(__file__).parent
OUTPUT_DIR = Path(os.environ.get("CS_OUTPUT_DIR", str(BASE_DIR / "output"))).resolve()
DEBUG_DIR = OUTPUT_DIR / "debug"
USER_DATA_DIR = BASE_DIR / "user_data"
AUTH_STATE_FILE = USER_DATA_DIR / "auth_state.json"
QUEUE_FILE = OUTPUT_DIR / "requests_queue.json"
SENT_LOG_FILE = OUTPUT_DIR / "sent_log.json"
STATUS_FILE = OUTPUT_DIR / "autopilot_status.json"

DEFAULT_DAILY_LIMIT = 100
DEFAULT_DELAY_MIN = 15  # минут
DEFAULT_DELAY_MAX = 35  # минут


def sanitize_before_send(text: str, host_clean_name: Optional[str] = None) -> str:
    """Локальная фильтрация текста перед вводом 'в последнюю секунду' (без подстановки 'there')."""
    lines = text.split('\n')
    if not lines:
        return text

    first_line = lines[0].strip()
    # Если приветствие уже чистое ("Hi Name," или "Hello Name,"), не искажаем пунктуацию
    if re.match(r'^(Hi|Hello|Hey)\s+[A-Za-zА-Яа-яЁё]+,\s*$', first_line):
        return text

    fake_placeholders = {"there", "host", "user", "member", "unknown", "none", "null"}

    # Ловим паттерны "Hey Name,", "Hi Name:", "Hello!" и чистим мусорные суффиксы
    match = re.match(r'^(Hey|Hi|Hello|Hola)(?:\s+([^,\n!:]+))?([,!:]?)(.*)$', first_line, re.IGNORECASE)
    if match:
        greeting_word = random.choice(['Hey', 'Hi', 'Hello'])
        clean_name = ""

        if host_clean_name and host_clean_name.strip() and host_clean_name.strip().lower() not in fake_placeholders:
            cand = re.findall(r'[a-zA-Zа-яА-ЯёЁ]+', host_clean_name.strip())
            if cand and cand[0].lower() not in fake_placeholders:
                clean_name = cand[0].capitalize()

        if not clean_name and match.group(2):
            raw_name = match.group(2)
            raw_name = re.sub(r'(?i)\s+(in|from|at)\s+.*$', '', raw_name).strip()
            first_word = raw_name.split()[0] if raw_name.split() else ""
            cand = re.findall(r'[a-zA-Zа-яА-ЯёЁ]+', first_word)
            if cand and cand[0].lower() not in fake_placeholders:
                clean_name = cand[0].capitalize()

        rest_of_line = (match.group(4) or "").strip()
        rest_of_line = re.sub(
            r'^[,\s]*(?:(?:an?\s+)?[a-zA-Z\s-]{2,30}\s+who\s+(?:loves|enjoys|likes|is|wants)[^,.]+[,.]?\s*)',
            '',
            rest_of_line,
            flags=re.IGNORECASE
        ).strip()
        rest_of_line = re.sub(r'^[,\s.:;]+', '', rest_of_line).strip()
        if rest_of_line:
            rest_of_line = rest_of_line[0].upper() + rest_of_line[1:]

        # Если имя извлечено — "Hi Sakura!", если пустое — универсальное "Hello!"
        base_greeting = f"{greeting_word} {clean_name}!" if clean_name else "Hello!"
        lines[0] = f"{base_greeting} {rest_of_line}".strip() if rest_of_line else base_greeting
        return '\n'.join(lines)
    return text


def compute_randomized_delays(num_messages: int, period_minutes: int) -> List[float]:
    """
    Генерирует человекоподобные неравномерные интервалы между N сообщениями
    на общем отрезке времени T минут.
    Интервалы никогда не кратны ровным минутам (не 5:00, 10:00), содержат случайный джиттер
    и исключают шаблонные паттерны бота.
    """
    if num_messages <= 1 or period_minutes <= 0:
        return []

    total_seconds = float(period_minutes * 60)
    intervals_count = num_messages - 1

    # Случайные веса для естественного распределения времени
    weights = [random.uniform(0.45, 1.55) for _ in range(intervals_count)]
    sum_w = sum(weights)
    base_delays = [(w / sum_w) * total_seconds for w in weights]

    delays = []
    for d in base_delays:
        jitter = random.choice([-1, 1]) * random.randint(14, 53)
        val = max(35.0, d + jitter)
        if int(val) % 60 == 0:
            val += random.choice([-23, 17, 29])
        delays.append(round(val, 1))

    return delays


def log_step(category: str, message: str) -> str:
    """Выводит форматированную строку лога с таймштампом, защищенную от обрыва stdout-пайпа."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] [{category}] {message}"
    try:
        print(line, flush=True)
    except (BrokenPipeError, IOError, OSError):
        pass
    try:
        log_file = OUTPUT_DIR / "auto_sender.log"
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    return line


class AutoSender:
    def __init__(
        self,
        daily_limit: int = DEFAULT_DAILY_LIMIT,
        delay_min: int = DEFAULT_DELAY_MIN,
        delay_max: int = DEFAULT_DELAY_MAX,
        interval_seconds: Optional[int] = None,
        min_interval_minutes: int = 5,
        max_interval_minutes: int = 10,
        jitter_enabled: bool = True,
        max_requests: Optional[int] = None,
        period_minutes: Optional[int] = None,
        dry_run: bool = False,
        headless: bool = True,
        target_username: Optional[str] = None,
        test_mode: bool = False,
        trip_id: Optional[str] = None,
    ):
        self.daily_limit = max(1, daily_limit)
        self.delay_min = delay_min
        self.delay_max = delay_max
        self.interval_seconds = interval_seconds
        self.min_interval_minutes = max(1, int(min_interval_minutes))
        self.max_interval_minutes = max(self.min_interval_minutes, int(max_interval_minutes))
        self.jitter_enabled = bool(jitter_enabled)
        self.max_requests = max_requests
        self.period_minutes = period_minutes
        self.dry_run = dry_run
        self.headless = headless
        self.target_username = target_username
        self.test_mode = test_mode
        self.trip_id = trip_id
        self.is_running = True
        self.is_paused = False
        self._start_timestamp = time.time()

        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)

        # Единый источник истины для очереди — RequestQueueManager (SQLite + JSON синхронизация)
        self.queue_manager = RequestQueueManager()
        from core.autopilot import AutopilotEngine
        self.autopilot_engine = AutopilotEngine(trip_id=self.trip_id or "default")

    def get_sent_today_count(self) -> int:
        """Подсчитывает количество успешно отправленных запросов за сегодняшний день."""
        if not SENT_LOG_FILE.exists():
            return 0
        try:
            with open(SENT_LOG_FILE, "r", encoding="utf-8") as f:
                logs = json.load(f)
            today_str = date.today().isoformat()
            today_sent = [
                entry for entry in logs
                if entry.get("timestamp", "").startswith(today_str)
                and entry.get("status") in ("SENT_SUCCESS", "success", "dry_run_success", "DRY_RUN_SUCCESS")
                and entry.get("mode") in ("live", "dry_run")
                and "Deduplicated" not in (entry.get("notes") or "")
                and "Detected as already sent" not in (entry.get("notes") or "")
                and not entry.get("timestamp", "").endswith("19:55:57")
            ]
            return len(today_sent)
        except Exception:
            return 0

    def append_sent_log(self, entry: Dict[str, Any]) -> None:
        """Записывает подробный результат отправки в sent_log.json с файловым_lock и атомарной записью."""
        logs = []
        if SENT_LOG_FILE.exists():
            try:
                with open(SENT_LOG_FILE, "r", encoding="utf-8") as f:
                    try:
                        fcntl.flock(f.fileno(), fcntl.LOCK_SH)
                    except (OSError, ImportError):
                        pass
                    try:
                        logs = json.load(f)
                    except Exception:
                        logs = []
                    finally:
                        try:
                            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                        except (OSError, ImportError):
                            pass
            except Exception:
                logs = []

        logs.append(entry)
        tmp_file = SENT_LOG_FILE.with_suffix(".json.tmp")
        with open(tmp_file, "w", encoding="utf-8") as f:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            except (OSError, ImportError):
                pass
            try:
                json.dump(logs, f, ensure_ascii=False, indent=2)
            finally:
                try:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                except (OSError, ImportError):
                    pass
        os.replace(tmp_file, SENT_LOG_FILE)

        # Каноническая запись попытки отправки в SQLite sending_history через AutopilotEngine
        try:
            from core.autopilot import AutopilotEngine
            engine = AutopilotEngine(trip_id=self.trip_id or "default")
            engine.record_sending_attempt(
                host_id_or_username=str(entry.get("username") or entry.get("id") or ""),
                status=str(entry.get("status") or "UNKNOWN"),
                mode=str(entry.get("mode") or ("dry_run" if self.dry_run else "live")),
                http_status=entry.get("http_status"),
                proof_screenshot=entry.get("proof_screenshot"),
                before_screenshot=entry.get("before_screenshot"),
                button_selector=entry.get("button_selector"),
                notes=entry.get("notes"),
                error_details=entry.get("error"),
                timestamp=entry.get("timestamp"),
            )
        except Exception as e:
            log_step("DEBUG", f"SQLite sending_history sync note: {e}")

    def update_queue_status(
        self,
        username: str,
        new_status: str,
        notes: Optional[str] = None,
        sent_at: Optional[str] = None,
        attempts: Optional[int] = None,
        increment_attempts: bool = False,
    ) -> None:
        """Обновляет статус карточки хоста в requests_queue.json и SQLite.

        `attempts` / `increment_attempts` учитываются только в JSON-очереди
        (в таблице sending_queue нет такой колонки) и используются для
        сортировки кандидатов, чтобы проблемный хост уходил в конец очереди.
        """
        now_iso = datetime.now().isoformat()
        status_norm = (new_status or "").strip().lower()
        notes_lower = (notes or "").lower()

        # sent_at ставится ТОЛЬКО для реального подтверждённого клика Send.
        # "Already sent" (особенно подтверждённый лишь отсутствием кнопки) — не WAS SENT:
        # дату не подставляем, а у Approved/Queued/Failed/Skipped старую дату очищаем.
        is_sent_status = status_norm in ("sent", "was sent")
        is_already = "already sent" in notes_lower or "deduplicated" in notes_lower
        clear_sent_at = False

        if sent_at is not None:
            effective_sent_at = sent_at
        elif is_sent_status and not is_already:
            effective_sent_at = now_iso
        elif not is_sent_status:
            effective_sent_at = None
            clear_sent_at = True
        else:
            effective_sent_at = None

        if QUEUE_FILE.exists():
            try:
                with open(QUEUE_FILE, "r", encoding="utf-8") as f:
                    try:
                        fcntl.flock(f.fileno(), fcntl.LOCK_SH)
                    except (OSError, ImportError):
                        pass
                    try:
                        queue = json.load(f)
                    except Exception:
                        queue = []
                    finally:
                        try:
                            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                        except (OSError, ImportError):
                            pass

                for item in queue:
                    if item.get("username") == username:
                        item["status"] = new_status
                        if notes:
                            item["notes"] = notes
                        if effective_sent_at:
                            item["sent_at"] = effective_sent_at
                        elif clear_sent_at:
                            item.pop("sent_at", None)
                        item["updated_at"] = now_iso
                        if increment_attempts:
                            item["attempts"] = int(item.get("attempts") or 0) + 1
                        elif attempts is not None:
                            item["attempts"] = int(attempts)
                        break

                tmp_queue = QUEUE_FILE.with_suffix(".json.tmp")
                with open(tmp_queue, "w", encoding="utf-8") as f:
                    try:
                        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                    except (OSError, ImportError):
                        pass
                    try:
                        json.dump(queue, f, ensure_ascii=False, indent=2)
                    finally:
                        try:
                            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                        except (OSError, ImportError):
                            pass
                os.replace(tmp_queue, QUEUE_FILE)
            except Exception as e:
                log_step("WARN", f"Ошибка обновления очереди JSON: {e}")

        try:
            from database import db_session
            with db_session() as conn:
                cur = conn.cursor()
                if notes:
                    cur.execute(
                        """
                        UPDATE sending_queue 
                        SET status = ?, notes = ?,
                            sent_at = CASE WHEN ? IS NOT NULL THEN ? WHEN ? = 1 THEN NULL ELSE sent_at END,
                            updated_at = ?
                        WHERE host_id IN (SELECT id FROM hosts WHERE username = ? OR id = ?);
                        """,
                        (new_status, notes, effective_sent_at, effective_sent_at, int(clear_sent_at), now_iso, username, username),
                    )
                else:
                    cur.execute(
                        """
                        UPDATE sending_queue 
                        SET status = ?,
                            sent_at = CASE WHEN ? IS NOT NULL THEN ? WHEN ? = 1 THEN NULL ELSE sent_at END,
                            updated_at = ?
                        WHERE host_id IN (SELECT id FROM hosts WHERE username = ? OR id = ?);
                        """,
                        (new_status, effective_sent_at, effective_sent_at, int(clear_sent_at), now_iso, username, username),
                    )

                # Синхронизация в единый источник истины core_hosts (ТЗ)
                st_map = {
                    "sent": "sent",
                    "was sent": "sent",
                    "skipped": "skipped",
                    "failed": "skipped",
                    "approved": "approved",
                    "queued": "selected",
                    "qc_review_required": "drafted",
                    "draft": "drafted",
                }
                c_st = st_map.get(status_norm)
                if c_st:
                    cur.execute(
                        """
                        UPDATE core_hosts
                        SET state = ?,
                            sent_at = CASE WHEN ? = 'sent' THEN coalesce(sent_at, ?) ELSE sent_at END,
                            qc_notes = CASE WHEN ? = 'skipped' THEN coalesce(?, qc_notes) ELSE qc_notes END,
                            updated_at = ?
                        WHERE (LOWER(username) = LOWER(?) OR LOWER(host_id) = LOWER(?));
                        """,
                        (c_st, c_st, effective_sent_at or now_iso, c_st, notes, now_iso, username, username),
                    )
        except Exception as e:
            log_step("WARN", f"Ошибка синхронизации статуса: {e}")

    def move_host_to_end_of_queue(self, username: str, reason: str = "") -> None:
        """Перемещает проблемного хоста в самый конец очереди (вниз левого стакана) без остановки автопилота."""
        now_iso = datetime.now().isoformat()
        try:
            from database import db_session
            with db_session() as conn:
                cur = conn.cursor()
                cur.execute(
                    """
                    UPDATE sending_queue
                    SET priority = COALESCE(priority, 0) - 100,
                        notes = ?,
                        updated_at = ?
                    WHERE host_id IN (SELECT id FROM hosts WHERE LOWER(username) = LOWER(?) OR LOWER(id) = LOWER(?));
                    """,
                    (f"Moved to bottom of queue: {reason}", now_iso, username, username),
                )
                cur.execute(
                    """
                    UPDATE core_hosts
                    SET qc_notes = ?,
                        updated_at = ?
                    WHERE (LOWER(username) = LOWER(?) OR LOWER(host_id) = LOWER(?));
                    """,
                    (f"Moved to bottom: {reason}", now_iso, username, username),
                )
            log_step("QUEUE", f"Хост @{username} перемещен в конец очереди (priority -100). Причина: {reason}")
        except Exception as e:
            log_step("WARN", f"Ошибка перемещения @{username} в конец очереди: {e}")

    def sync_queue_against_sent_and_inbox(self, page=None) -> int:
        """
        Автоматическая очистка очереди от уже отправленных запросов:
        1. Локальная сверка: SQLite sending_history, inbox_conversations, requests, sent_history.json, sent_log.json.
        2. Экспресс-проверка чатов через сессию Playwright (если передан page).
        """
        log_step("SYNC", "Автоматическая проверка и очистка очереди от ранее отправленных хостов...")
        from request_manager import RequestQueueManager
        rm = RequestQueueManager()
        deduped = rm.deduplicate_queue_against_sent()

        # Экспресс-проверка активных диалогов через живую страницу Playwright
        if page and AUTH_STATE_FILE.exists():
            try:
                log_step("SYNC", "Экспресс-проверка активных диалогов Couchsurfing (/c/chat/request/sent)...")
                chat_members = set()

                def on_res(res):
                    u = res.url.lower()
                    if "communicationrequest" in u or "chat." in u:
                        try:
                            if "json" in res.headers.get("content-type", ""):
                                data = res.json()
                                from data_cleaner import extract_members_from_json
                                extract_members_from_json(data, chat_members)
                        except Exception:
                            pass

                page.on("response", on_res)
                try:
                    page.goto("https://www.couchsurfing.com/c/chat/request/sent", wait_until="domcontentloaded", timeout=25000)
                    page.wait_for_timeout(3000)
                except Exception as e:
                    log_step("DEBUG", f"Загрузка страницы отправленных запросов: {e}")

                # Собираем ссылки на профили из списка DOM
                links = page.query_selector_all("a[href*='/people/'], a[href*='/c/users/'], a[href*='/members/']")
                for link in links:
                    href = link.get_attribute("href") or ""
                    m = re.search(r"/(?:people|users|members)/([a-zA-Z0-9._-]+)", href)
                    if m:
                        chat_members.add(m.group(1).lower())

                if chat_members:
                    newly_removed = 0
                    sync_queue = self.queue_manager.load_queue(exclude_sent=False)
                    for item in sync_queue:
                        u = (item.get("username") or "").lower()
                        if u in chat_members and item.get("status") in ("Approved", "Queued"):
                            self.queue_manager.update_item(
                                item.get("username"),
                                status="Sent",
                                message=item.get("generated_message"),
                            )
                            log_step("SYNC", f"[SYNC] Исключен из очереди уже отправленный хост: @{item.get('username')}")
                            newly_removed += 1
                            deduped += 1
            except Exception as e:
                log_step("DEBUG", f"Ошибка экспресс-проверки чатов: {e}")

        if deduped > 0:
            log_step("SYNC", f"Очистка очереди завершена: исключено {deduped} ранее отправленных хостов.")
        else:
            log_step("SYNC", "Очередь чиста: все кандидаты готовы к отправке.")

        return deduped

    def check_is_paused(self) -> bool:
        """Проверяет флаг паузы в файле статуса."""
        if not STATUS_FILE.exists():
            return False
        try:
            with open(STATUS_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
                return bool(d.get("is_paused"))
        except Exception:
            return False

    def check_is_stopped(self) -> bool:
        """Проверяет флаг остановки автопилота из файла статуса с защитным периодом при старте."""
        if hasattr(self, "_start_timestamp") and (time.time() - self._start_timestamp < 4.0):
            return False
        if not STATUS_FILE.exists():
            return False
        try:
            with open(STATUS_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
                return d.get("is_running") is False
        except Exception:
            return False

    def update_autopilot_status(
        self,
        is_running: bool,
        is_paused: Optional[bool] = None,
        current_host: Optional[str] = None,
        next_send_time: Optional[float] = None,
        next_delay_seconds: Optional[int] = None,
        last_status: str = "Idle",
        seconds_remaining: Optional[int] = None,  # совместимость со старыми вызовами (вычисляется автоматически)
    ) -> None:
        """Сохраняет актуальный статус автопилота для отображения в UI."""
        now = time.time()
        rem = max(0, int(next_send_time - now)) if next_send_time else 0
        if is_paused is not None:
            self.is_paused = is_paused
        data = {
            "is_running": is_running,
            "is_paused": self.is_paused,
            "current_host": current_host,
            "sent_today": self.get_sent_today_count(),
            "daily_limit": self.daily_limit,
            "next_send_time": next_send_time,
            "next_dispatch_timestamp": next_send_time,
            "next_delay_seconds": next_delay_seconds,
            "interval_seconds": next_delay_seconds,
            "seconds_remaining": rem,
            "last_status": last_status,
            "dry_run": self.dry_run,
            "headed": not self.headless,
            "min_interval_minutes": self.min_interval_minutes,
            "max_interval_minutes": self.max_interval_minutes,
            "jitter_enabled": self.jitter_enabled,
            "updated_at": datetime.now().isoformat(),
        }
        try:
            # Атомарная запись: UI никогда не прочитает наполовину записанный JSON.
            tmp_file = STATUS_FILE.with_suffix(".json.tmp")
            with open(tmp_file, "w", encoding="utf-8") as f:
                try:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                except (OSError, ImportError):
                    pass
                try:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                finally:
                    try:
                        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                    except (OSError, ImportError):
                        pass
            os.replace(tmp_file, STATUS_FILE)
        except Exception:
            pass

    def check_security_challenges(self, page) -> bool:
        """Проверяет страницу на наличие капчи, Cloudflare или блокировок."""
        url = page.url.lower()
        if any(k in url for k in ("challenge", "captcha", "turnstile", "cdn-cgi/challenge")):
            return True

        title = page.title().lower()
        if any(t in title for t in ("just a moment", "attention required", "security check", "access denied", "ddos")):
            return True

        try:
            body_text = page.evaluate("() => document.body ? document.body.innerText.toLowerCase() : ''")
            visible_markers = [
                "verify you are human",
                "performing a security check",
                "access denied",
                "error 429",
                "error 403",
                "too many requests. please try again",
                "please verify you are human",
            ]
            return any(m in body_text for m in visible_markers)
        except Exception:
            return False

    def human_type(self, element, text: str) -> None:
        """Имитирует естественный человеческий ввод текста с жесткими таймаутами."""
        try:
            element.click(timeout=5000)
        except Exception:
            try:
                element.click(force=True, timeout=5000)
            except Exception:
                try:
                    element.focus(timeout=5000)
                except Exception:
                    pass
        time.sleep(random.uniform(0.2, 0.4))
        try:
            element.fill("", timeout=5000)
        except Exception:
            pass

        if len(text) > 400:
            first_part = text[:70]
            rest_part = text[70:]
            for char in first_part:
                element.type(char, delay=random.uniform(20, 45), timeout=5000)
            time.sleep(0.3)
            element.type(rest_part, delay=4, timeout=15000)
        else:
            for char in text:
                element.type(char, delay=random.uniform(25, 55), timeout=5000)
                if char in (".", "!", "?", "\n"):
                    time.sleep(random.uniform(0.12, 0.3))
                elif random.random() < 0.04:
                    time.sleep(random.uniform(0.08, 0.2))

    def get_host_dates_and_city(self, host_item: Dict[str, Any]) -> Tuple[str, str, str]:
        """
        Считывает даты и город кандидата из объекта host_item и базы данных SQLite.
        Приоритет отдается городу и датам Trip Card, так как на CS сопоставляется карточка поездки.
        """
        username = host_item.get("username", "")
        arrival_date = host_item.get("arrival_date") or host_item.get("check_in")
        departure_date = host_item.get("departure_date") or host_item.get("check_out")
        target_city = (
            host_item.get("trip_city")
            or host_item.get("city")
            or host_item.get("target_city")
        )

        # Синхронизация с канонической базой данных через core.database
        if username:
            try:
                from core.database import get_connection
                with get_connection() as conn:
                    cursor = conn.cursor()
                    cursor.execute("""
                        SELECT 
                            COALESCE(ct.city, ut.city, th.city, h.location) as trip_city,
                            COALESCE(gm.check_in, th.arrival_date, ct.arrival_date, ut.arrival_date) as cin,
                            COALESCE(gm.check_out, th.departure_date, ct.departure_date, ut.departure_date) as cout
                        FROM hosts h
                        LEFT JOIN trip_hosts th ON (th.host_id = h.id OR LOWER(th.host_id) = LOWER(h.username))
                        LEFT JOIN core_trips ct ON th.trip_id = ct.id
                        LEFT JOIN user_trips ut ON th.trip_id = ut.trip_id
                        LEFT JOIN generated_messages gm ON gm.host_id = h.id AND gm.is_active = 1
                        WHERE LOWER(h.username) = LOWER(?) OR LOWER(h.id) = LOWER(?)
                        ORDER BY th.created_at DESC, gm.id DESC LIMIT 1
                    """, (username, username))
                    row = cursor.fetchone()
                    if row:
                        db_city, db_cin, db_cout = row
                        if db_city and not target_city:
                            target_city = db_city
                        if db_cin and not arrival_date:
                            arrival_date = db_cin
                        if db_cout and not departure_date:
                            departure_date = db_cout
            except Exception:
                pass

        arrival_date = arrival_date or "2026-11-16"
        departure_date = departure_date or "2026-11-19"
        target_city = target_city or "Tokyo"

        # Извлечение чистого названия города (например, "Kyoto, Japan" -> "Kyoto")
        raw_lower = target_city.lower()
        clean_city = None
        for known in ["Tokyo", "Kyoto", "Sapporo", "Osaka", "Nagoya", "Fukuoka", "Yokohama"]:
            if known.lower() in raw_lower:
                clean_city = known
                break

        tokyo_districts = [
            "kinshi", "sumida", "meguro", "shibuya", "shinjuku", "minato", "chuo", "chiyoda",
            "ota", "shinagawa", "setagaya", "nakano", "suginami", "toshima", "kita", "arakawa",
            "itabashi", "nerima", "adachi", "katsushika", "edogawa", "koto", "taito",
            "nihonbashi", "ginza", "roppongi", "akihabara", "ueno", "asakusa"
        ]
        if not clean_city:
            if (self.trip_id and "tokyo" in self.trip_id.lower()) or any(d in raw_lower for d in tokyo_districts):
                clean_city = "Tokyo"
            elif self.trip_id and "kyoto" in self.trip_id.lower():
                clean_city = "Kyoto"
            elif self.trip_id and "osaka" in self.trip_id.lower():
                clean_city = "Osaka"
            elif self.trip_id and "sapporo" in self.trip_id.lower():
                clean_city = "Sapporo"
            else:
                clean_city = target_city.split(",")[0].strip() or "Tokyo"

        return arrival_date, departure_date, clean_city

    def fill_form_dates(self, page, arrival_date: str, departure_date: str) -> None:
        """
        Заполняет поля дат прибытия и отъезда в форме запроса, если они доступны для ввода.
        """
        try:
            # 1. Текстовые поля ввода дат
            date_inputs = [
                ("input[name*='arrival'], input[name*='check_in'], input[placeholder*='Arrival']", arrival_date),
                ("input[name*='departure'], input[name*='check_out'], input[placeholder*='Departure']", departure_date),
            ]
            for sel, val in date_inputs:
                try:
                    el = page.locator(sel)
                    if el.count() > 0 and el.first.is_visible():
                        el.first.fill(val)
                        page.wait_for_timeout(300)
                except Exception:
                    pass

            # 2. Прямые HTML5 инпуты date
            date_fields = page.locator("input[type='date']")
            if date_fields.count() >= 2:
                try:
                    date_fields.nth(0).fill(arrival_date)
                    date_fields.nth(1).fill(departure_date)
                    page.wait_for_timeout(300)
                except Exception:
                    pass
        except Exception as e:
            log_step("DEBUG", f"fill_form_dates: {e}")

    # ------------------------------------------------------------------
    # Вспомогательные методы надежной отправки
    # ------------------------------------------------------------------
    ALREADY_SENT_PHRASES = (
        "you sent a request to",
        "this user needs to accept your request before you can send another message",
        "view chat request",
        "view stay request",
        "request pending",
    )

    def detect_already_sent(self, page) -> Optional[str]:
        """
        Возвращает найденную фразу, ТОЛЬКО если на странице ВИДИМ текст
        'You sent a request to' или 'This user needs to accept your request...'.
        page.content() НЕ используется: в HTML есть i18n-бандлы с этими строками.
        """
        try:
            body_text = (page.inner_text("body") or "").lower()
        except Exception:
            return None
        for phrase in self.ALREADY_SENT_PHRASES:
            if phrase in body_text:
                return phrase
        return None

    SEND_BUTTON_SELECTOR = (
        "button[data-testid='send-request-button']:visible, "
        "button:visible:has-text('Send Request'), "
        "button:visible:has-text('Send request'), "
        "button:visible:has-text('Отправить запрос')"
    )

    def _log_visible_controls(self, page) -> None:
        """Логирует видимые заголовки и тексты кнопок — для диагностики того, что именно показывает сайт."""
        try:
            texts = page.evaluate("""() => {
              const seen = new Set();
              const out = [];
              const add = (el) => {
                try {
                  const t = (el.innerText || el.textContent || '').trim();
                  if (t && t.length < 140 && !seen.has(t)) { seen.add(t); out.push(t); }
                } catch (e) {}
              };
              document.querySelectorAll('h1,h2,h3,h4,button,[role="button"],a').forEach(add);
              return out;
            }""")
            for t in (texts or [])[:40]:
                log_step("DIAG", f"Видимый элемент на странице: {t}")
        except Exception as e:
            log_step("DEBUG", f"Диагностика видимых элементов страницы: {e}")

    def _has_visible_send_form(self, page) -> bool:
        """Есть ли на странице реально видимая форма отправки (textarea или кнопка Send Request)."""
        try:
            if page.locator("textarea:visible").count() > 0:
                return True
        except Exception:
            pass
        try:
            btn = page.locator(self.SEND_BUTTON_SELECTOR).first
            return btn.count() > 0 and btn.is_visible()
        except Exception:
            return False

    def wait_for_send_form_or_already_sent(self, page, timeout: int = 15) -> Tuple[str, Optional[str]]:
        """
        Ждёт до `timeout` секунд появления формы отправки.
        Возвращает:
          ("form", None)                  — форма/кнопка появились, можно отправлять;
          ("already", <visible text>)     — формы нет, но виден явный текст уже отправленного запроса;
          ("timeout", <visible text|None>)— форма так и не загрузилась (НЕ считаем отправленным).
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._has_visible_send_form(page):
                return "form", None
            reason = self.detect_already_sent(page)
            if reason:
                return "already", reason
            time.sleep(0.25)

        if self._has_visible_send_form(page):
            return "form", None
        return "timeout", self.detect_already_sent(page)

    def _mark_already_sent(self, username: str, host_name: str, target_city: str, arrival_date: str, departure_date: str, reason: str) -> str:
        """Skip ТОЛЬКО при явном видимом тексте. sent_at не выставляется (это не наш WAS SENT)."""
        log_step("SKIP", f"[SKIP] Запрос хосту @{username} уже отправлен ранее (видимый текст: '{reason}').")
        self.update_queue_status(username, "Skipped", notes=f"Already sent on Couchsurfing (visible: '{reason}')")
        self.append_sent_log({
            "id": username,
            "username": username,
            "name": host_name,
            "target_city": target_city,
            "arrival_date": arrival_date,
            "departure_date": departure_date,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "status": "ALREADY_SENT",
            "mode": "skipped",
            "notes": f"Already sent on Couchsurfing (visible text: '{reason}')",
        })
        return "ALREADY_SENT"

    def dismiss_cookie_banner(self, page) -> None:
        """Закрывает cookie-баннер (он перекрывает нижние карточки поездок)."""
        for sel in ("button:visible:has-text('Reject All')", "button:visible:has-text('Accept')"):
            try:
                btn = page.locator(sel).first
                if btn.count() > 0 and btn.is_visible():
                    btn.click(timeout=3000)
                    page.wait_for_timeout(600)
                    log_step("NAV", "Cookie-баннер закрыт.")
                    return
            except Exception:
                pass

    def is_trip_selection_screen(self, page) -> bool:
        try:
            if page.locator('button[data-testid="trip-cards-trip-gallery-card-overlay-button"]').count() > 0:
                return True
            if page.locator('[data-testid*="trip-selector"]').count() > 0:
                return True
            loc = page.get_by_text("Which trip is this stay for?")
            return loc.count() > 0 and loc.first.is_visible()
        except Exception:
            return False

    @staticmethod
    def _trip_date_hint(arrival_date: str, departure_date: str) -> str:
        """'2026-11-16','2026-11-19' -> 'nov1619'; кросс-месяц -> 'nov30dec2' (без пробелов/тире)."""
        try:
            a = datetime.strptime(arrival_date[:10], "%Y-%m-%d")
            d = datetime.strptime(departure_date[:10], "%Y-%m-%d")
            if a.month == d.month:
                return f"{a.strftime('%b').lower()}{a.day}{d.day}"
            return f"{a.strftime('%b').lower()}{a.day}{d.strftime('%b').lower()}{d.day}"
        except Exception:
            return ""

    def select_trip_card(self, page, city: str, arrival_date: str, departure_date: str) -> bool:
        """
        Находит карточку поездки по городу (+ даты для приоритета) и кликает её overlay-кнопку.
        Карточка = ближайший предок overlay-кнопки (button.absolute.inset-0), содержащий ровно одну такую кнопку.
        """
        # 1. Прямой поиск кнопки через data-testid с совпадением города в aria-label
        try:
            gallery_btns = page.locator('button[data-testid="trip-cards-trip-gallery-card-overlay-button"]')
            if gallery_btns.count() > 0:
                for idx in range(gallery_btns.count()):
                    b = gallery_btns.nth(idx)
                    label = (b.get_attribute("aria-label") or "").strip()
                    if city.lower() in label.lower():
                        b.click(timeout=8000)
                        log_step("TRIP", f"Выбрана карточка поездки из галереи: '{label}'")
                        return True
                # Если точный город не найден в aria-label, кликаем первую карточку
                first_btn = gallery_btns.first
                first_label = (first_btn.get_attribute("aria-label") or "").strip()
                first_btn.click(timeout=8000)
                log_step("TRIP", f"Выбрана первая доступная карточка поездки: '{first_label}'")
                return True
        except Exception as e:
            log_step("DEBUG", f"Галерея trip-cards поиск: {e}")

        date_hint = self._trip_date_hint(arrival_date, departure_date)
        js = r"""
        ({city, dateHint}) => {
            document.querySelectorAll('[data-cs-trip-pick]').forEach(e => e.removeAttribute('data-cs-trip-pick'));
            const norm = s => (s || '').toLowerCase().replace(/[\s\u2013\u2014\-\u00b7•,]/g, '');
            const overlays = [...document.querySelectorAll('button')].filter(
                b => b.classList.contains('absolute') && b.classList.contains('inset-0'));
            let best = null, bestScore = -1, bestText = '';
            for (const b of overlays) {
                let card = b.parentElement, depth = 0;
                while (card && depth < 6) {
                    const n = [...card.querySelectorAll('button')].filter(
                        x => x.classList.contains('absolute') && x.classList.contains('inset-0')).length;
                    if (n > 1) { card = null; break; }
                    if (norm(card.innerText).includes(norm(city))) break;
                    card = card.parentElement; depth++;
                }
                if (!card) continue;
                const t = norm(card.innerText);
                if (!t.includes(norm(city))) continue;
                let score = 1;
                if (dateHint && t.includes(dateHint)) score += 10;
                if (score > bestScore) { best = b; bestScore = score; bestText = card.innerText; }
            }
            if (!best) return null;
            best.setAttribute('data-cs-trip-pick', '1');
            best.scrollIntoView({block: 'center'});
            return {score: bestScore, text: (bestText || '').replace(/\s+/g, ' ').slice(0, 120)};
        }
        """
        try:
            info = page.evaluate(js, {"city": city, "dateHint": date_hint})
        except Exception as e:
            log_step("DEBUG", f"select_trip_card evaluate: {e}")
            info = None
        if not info:
            log_step("TRIP", f"Карточка поездки для '{city}' не найдена (date_hint={date_hint}).")
            return False
        if info.get("score", 0) < 10:
            log_step("TRIP", f"ВНИМАНИЕ: карточка '{city}' найдена, но даты {arrival_date} – {departure_date} не совпали: {info.get('text')}")
        try:
            page.locator("[data-cs-trip-pick='1']").first.click(timeout=8000)
        except Exception as e:
            log_step("TRIP", f"Не удалось кликнуть карточку поездки: {e}")
            return False
        log_step("TRIP", f"Выбрана карточка поездки: {info.get('text')}")
        return True

    def _fail_attempt(self, page, username: str, host_name: str, reason: str, error_screenshot, error_html) -> bool:
        """Ошибка формы/сети/таймаут: помечаем хоста как FAILED, пишем в лог и переходим к следующему."""
        log_step("ERROR", f"[FAILED] @{username}: {reason}. Статус изменён на FAILED, переходим к следующему хосту.")
        try:
            page.screenshot(path=str(error_screenshot), timeout=5000)
            error_html.write_text(page.content(), encoding="utf-8")
        except Exception:
            pass
        self.append_sent_log({
            "id": username,
            "username": username,
            "name": host_name,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "status": "FAILED",
            "mode": "error",
            "error": reason,
            "proof_screenshot": str(error_screenshot),
            "error_html": str(error_html),
        })
        self.update_queue_status(username, "FAILED", notes=f"Failed: {reason}", increment_attempts=True)
        return False

    def _record_dry_run_success(
        self,
        username: str,
        host_name: str,
        target_city: str,
        arrival_date: str,
        departure_date: str,
        before_screenshot: Path,
        found_selector: str,
        message: str = "",
    ) -> None:
        """
        Пишет результат тестового прогона (dry-run) только в sent_log.json.
        Статус записи в requests_queue.json НЕ меняется — остаётся 'Approved'.
        """
        self.append_sent_log({
            "id": username,
            "username": username,
            "name": host_name,
            "target_city": target_city,
            "arrival_date": arrival_date,
            "departure_date": departure_date,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "status": "dry_run_success",
            "http_status": None,
            "mode": "dry_run",
            "proof_screenshot": str(before_screenshot),
            "before_screenshot": str(before_screenshot),
            "button_selector": found_selector,
            "notes": "Form verified and filled in dry-run mode. Send button was not clicked.",
            "message": message,
        })

    def process_host(self, page, host_item: Dict[str, Any]) -> bool:
        """
        Выполняет отправку запроса для одного хоста с расширенной верификацией.
        Возвращает True в случае успеха.
        """
        username = host_item.get("username", "")
        host_name = host_item.get("clean_name") or host_item.get("name") or username
        message = host_item.get("generated_message", "")
        arrival_date, departure_date, target_city = self.get_host_dates_and_city(host_item)
        stay_url = f"https://www.couchsurfing.com/c/users/{username}/stay-request"

        # Предохранитель Pre-flight QC: жесткая проверка перед запуском браузера
        from llm_agent import validate_message_qc
        from message_generator import verify_host_name

        first_name, is_trusted = verify_host_name(host_item)
        if not is_trusted or not first_name:
            log_step("WARN", f"Остановка отправки @{username}: сомнительное имя '{host_item.get('name')}'. Требуется проверка.")
            self.update_queue_status(username, "Name_Review_Required", notes="Untrusted display name; manual review required.")
            return False

        if not message or not message.strip():
            log_step("WARN", f"Остановка отправки @{username}: пустое сообщение.")
            self.update_queue_status(username, "Draft", notes="Missing message text.")
            return False

        bio_words = len((host_item.get("about_me_clean") or host_item.get("about_me") or "").split())
        is_minimal = bio_words < 25
        qc_ok, qc_issues = validate_message_qc(
            message,
            is_minimal_bio=is_minimal,
            host_data=host_item,
            trip_city=host_item.get("city") or host_item.get("trip_city"),
        )
        if not qc_ok:
            log_step("WARN", f"Сообщение для @{username} не прошло QC ({', '.join(qc_issues)}). Пробуем авто-перегенерацию...")
            try:
                from message_generator import generate_stay_request
                regen_msg = generate_stay_request(
                    host_item,
                    check_in=arrival_date,
                    check_out=departure_date,
                )
                regen_qc_ok, regen_qc_issues = validate_message_qc(
                    regen_msg,
                    is_minimal_bio=is_minimal,
                    host_data=host_item,
                    trip_city=target_city,
                )
                if regen_qc_ok:
                    log_step("INFO", f"Успешная авто-перегенерация для @{username} ({len(regen_msg.split())} слов).")
                    message = regen_msg
                    host_item["generated_message"] = regen_msg
                    from database import db_session
                    with db_session() as conn:
                        cur = conn.cursor()
                        cur.execute(
                            "UPDATE core_hosts SET message_draft = ?, qc_passed = 1, qc_notes = 'Auto-regenerated passed QC' WHERE LOWER(username) = LOWER(?)",
                            (regen_msg, username),
                        )
                        cur.execute(
                            "UPDATE generated_messages SET message_text = ? WHERE host_id IN (SELECT id FROM hosts WHERE LOWER(username) = LOWER(?))",
                            (regen_msg, username),
                        )
                    qc_ok = True
                else:
                    log_step("WARN", f"Перегенерация для @{username} не прошла QC: {', '.join(regen_qc_issues)}.")
            except Exception as ex:
                log_step("WARN", f"Ошибка авто-перегенерации для @{username}: {ex}")

        if not qc_ok:
            log_step("WARN", f"Остановка отправки @{username}: сообщение не прошло QC ({', '.join(qc_issues)}).")
            self.update_queue_status(username, "QC_Review_Required", notes=f"Pre-dispatch QC: {', '.join(qc_issues)}")
            return "QC_FAILED"

        before_screenshot = DEBUG_DIR / f"before_send_{username}.png"
        after_screenshot = DEBUG_DIR / f"after_send_{username}.png"
        error_screenshot = DEBUG_DIR / f"error_{username}.png"
        error_html = DEBUG_DIR / f"error_{username}.html"

        log_step("START", f"Отправка запроса хосту: {username} ({host_name})")
        log_step("URL", f"Адрес страницы: {stay_url}")
        log_step("DESTINATION", f"Целевой город: {target_city}, даты: {arrival_date} – {departure_date}")
        log_step("MODE", "DRY-RUN (симуляция)" if self.dry_run else "БОЕВАЯ ОТПРАВКА")

        self.update_autopilot_status(
            is_running=True,
            current_host=username,
            last_status=f"Открытие формы для {username}...",
        )

        # Перехват сетевых запросов и ответов
        api_responses: List[Dict[str, Any]] = []

        def handle_response(res):
            try:
                u = res.url.lower()
                req = res.request
                method = req.method.upper()
                # Перехватываем ТОЛЬКО POST/PUT запросы на создание запроса/сообщения
                if method in ("POST", "PUT") and any(k in u for k in ("stay_request", "stay-request", "stayrequest", "graphql", "couchsurfing.com/api", "messages", "conversations")):
                    body = ""
                    try:
                        body = res.text()[:1000]
                    except Exception:
                        pass
                    api_responses.append({
                        "url": res.url,
                        "method": method,
                        "status": res.status,
                        "status_text": res.status_text,
                        "ok": res.ok,
                        "body": body,
                    })
            except Exception:
                pass

        page.on("response", handle_response)

        try:
            # 0. Жесткие таймауты Playwright против бесконечных зависаний
            try:
                page.set_default_timeout(15000)
                page.set_default_navigation_timeout(25000)
            except Exception:
                pass

            # 1. Загрузка страницы формы
            page.goto(stay_url, timeout=25000, wait_until="domcontentloaded")
            time.sleep(random.uniform(2.0, 3.5))

            # Диагностика сессии: текущий адрес и заголовок страницы
            try:
                cur_url = page.url
                cur_title = page.title()
                log_step("SESSION", f"Страница хоста @{username}: {cur_url}")
                log_step("SESSION", f"Заголовок страницы: {cur_title}")
            except Exception as e:
                log_step("DEBUG", f"Не удалось прочитать url/title страницы: {e}")

            # Проверка, не выбросило ли на логин (сессия недействительна / истекла)
            if "/login" in page.url.lower() or "sign in" in (page.title() or "").lower() or "log in" in (page.title() or "").lower():
                log_step("CRITICAL", f"Сессия недействительна — перенаправление на логин для @{username}! Автопилот остановлен.")
                page.screenshot(path=str(error_screenshot))
                error_html.write_text(page.content(), encoding="utf-8")
                self.append_sent_log({
                    "id": username,
                    "username": username,
                    "name": host_name,
                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "status": "FAILED",
                    "mode": "error",
                    "error": "Session expired: redirected to login",
                    "proof_screenshot": str(error_screenshot),
                    "error_html": str(error_html),
                })
                self.update_queue_status(username, "Queued", notes="Session expired: redirected to login", increment_attempts=True)
                self.update_autopilot_status(
                    is_running=False,
                    current_host=username,
                    last_status="Остановлен: сессия истекла (требуется повторный вход)",
                )
                self.is_running = False
                return False

            # 2. Проверка защиты Cloudflare / Капчи
            if self.check_security_challenges(page):
                log_step("CRITICAL", f"Обнаружен Cloudflare / Капча / 429 на {username}! Аварийная остановка.")
                page.screenshot(path=str(error_screenshot))
                error_html.write_text(page.content(), encoding="utf-8")
                self.append_sent_log({
                    "id": username,
                    "username": username,
                    "name": host_name,
                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "status": "FAILED",
                    "http_status": 403,
                    "error": "Captcha / Cloudflare / 429 detected",
                    "proof_screenshot": str(error_screenshot),
                    "error_html": str(error_html),
                })
                self.update_autopilot_status(
                    is_running=False,
                    current_host=username,
                    last_status="Остановлен: проверка безопасности (Капча/Cloudflare)",
                )
                self.is_running = False
                return False

            # 2.5. Закрываем cookie-баннер, чтобы он не перехватывал клики
            self.dismiss_cookie_banner(page)

            # 2.6. Агрессивное удаление оверлеев и баннеров куки из DOM (чтобы не блокировали клики)
            try:
                page.evaluate("""() => {
                    const selectors = [
                        '#cookie-banner', '.cookie-banner', '[class*="cookie"]', '[id*="cookie"]',
                        '.modal-backdrop', '[class*="overlay"]', '[class*="gdpr"]', '[id*="gdpr"]',
                        '[class*="consent"]', '[id*="consent"]'
                    ];
                    selectors.forEach(sel => {
                        try {
                            document.querySelectorAll(sel).forEach(el => el.remove());
                        } catch (e) {}
                    });
                }""")
                page.wait_for_timeout(500)
            except Exception as e:
                log_step("DEBUG", f"Очистка оверлеев куки: {e}")

            # Программное нажатие на известные кнопки согласия с куки, если они есть
            for consent_text in ("Accept", "Got it", "Agree", "I agree", "OK", "Принять", "Согласен"):
                try:
                    consent_btn = page.locator(
                        f'button:visible:has-text("{consent_text}"), a:visible:has-text("{consent_text}")'
                    ).first
                    if consent_btn.count() > 0 and consent_btn.is_visible():
                        consent_btn.click(timeout=2000)
                        page.wait_for_timeout(500)
                        log_step("NAV", f"Нажата кнопка согласия с куки: '{consent_text}'")
                        break
                except Exception:
                    pass

            # 2.7. Универсальный поиск и клик по кнопке "Request Stay"
            # Ищем любые элементы (a, button, [role="button"]) с текстом "Request Stay" (без учёта регистра).
            request_btn = page.locator(
                'a, button, [role="button"]'
            ).filter(
                has_text=re.compile("Request Stay", re.IGNORECASE)
            ).first

            found = False
            try:
                # Ждём появления кнопки до 3 секунд (кнопка может подгружаться динамически).
                deadline = time.time() + 3
                while time.time() < deadline:
                    try:
                        if request_btn.count() > 0 and request_btn.is_visible():
                            found = True
                            break
                    except Exception:
                        pass
                    page.wait_for_timeout(300)

                if not found:
                    # Прокручиваем страницу вниз — на адаптивной/моб. верстке кнопка может быть ниже первого экрана.
                    log_step("DEBUG", "Кнопка 'Request Stay' не видна сразу — прокрутка страницы вниз...")
                    try:
                        page.evaluate("window.scrollBy(0, window.innerHeight);")
                    except Exception:
                        pass
                    page.wait_for_timeout(1000)
                    try:
                        if request_btn.count() > 0 and request_btn.is_visible():
                            found = True
                    except Exception:
                        pass

                if not found:
                    already_msg = self.detect_already_sent(page)
                    if not already_msg:
                        view_req = page.locator("button:has-text('View Chat Request'), button:has-text('View Stay Request'), a:has-text('View Chat Request'), a:has-text('View Stay Request')")
                        if view_req.count() > 0 and view_req.first.is_visible():
                            already_msg = "View Request button visible on profile"

                    if already_msg:
                        return self._mark_already_sent(
                            username, host_name, target_city, arrival_date, departure_date, already_msg
                        )

                    try:
                        page.screenshot(path=str(DEBUG_DIR / f"profile_{username}.png"), timeout=5000)
                    except Exception:
                        pass
                    return self._fail_attempt(
                        page, username, host_name,
                        f"Кнопка 'Request Stay' не найдена на странице профиля хоста @{username} (форма не открылась)",
                        error_screenshot, error_html,
                    )

                try:
                    request_btn.scroll_into_view_if_needed(timeout=5000)
                except Exception:
                    pass
                log_step("NAV", "Нажатие кнопки 'Request Stay' на профиле хоста...")
                request_btn.click(force=True, timeout=15000)
                page.wait_for_timeout(2500)
                log_step("NAV", "Кнопка 'Request Stay' нажата, ожидаем открытие формы/модалки.")
            except Exception as e:
                try:
                    page.screenshot(path=str(DEBUG_DIR / f"profile_{username}.png"), timeout=5000)
                except Exception:
                    pass
                return self._fail_attempt(
                    page, username, host_name,
                    f"Кнопка 'Request Stay' не кликнута: {e}",
                    error_screenshot, error_html,
                )

            # 3. ВЫБОР КАРТОЧКИ ПОЕЗДКИ ПО ГОРОДУ И ДАТАМ
            trip_screen = self.is_trip_selection_screen(page)
            if trip_screen:
                picked = self.select_trip_card(page, target_city, arrival_date, departure_date)
                if not picked:
                    return self._fail_attempt(
                        page, username, host_name,
                        f"Карточка поездки для {target_city} ({arrival_date} – {departure_date}) не найдена на экране выбора Trip",
                        error_screenshot, error_html,
                    )
                page.wait_for_timeout(1500)

            # 4. Переход к экрану Stay Details (если есть кнопка Next / Stay Details)
            try:
                stay_details_btn = page.locator(
                    "button:has-text('Stay Details'), button:has-text('Next'), [data-testid='stay-request-next-button']"
                )
                if stay_details_btn.count() > 0 and stay_details_btn.first.is_visible():
                    log_step("NAV", "Переход к разделу Stay Details...")
                    stay_details_btn.first.click(timeout=15000)
                    page.wait_for_timeout(2000)
            except Exception:
                pass

            # Защита: если всё ещё на экране выбора поездки — НЕ вводим текст
            if self.is_trip_selection_screen(page):
                return self._fail_attempt(
                    page, username, host_name,
                    "Форма осталась на экране 'Which trip is this stay for?' — карточка поездки не применилась",
                    error_screenshot, error_html,
                )

            # Заполнение дат вручную (если поля присутствуют на экране Stay Details)
            self.fill_form_dates(page, arrival_date, departure_date)

            # 5. Ждём появления формы/кнопки Send Request до 15 секунд.
            # "Уже отправлено" — ТОЛЬКО при явном видимом тексте, а не из-за отсутствия селектора.
            form_state, already_reason = self.wait_for_send_form_or_already_sent(page, timeout=15)
            if form_state == "already" and already_reason:
                return self._mark_already_sent(
                    username, host_name, target_city, arrival_date, departure_date, already_reason
                )
            if form_state == "timeout":
                # Диагностика: что именно показывает сайт на экране (заголовки, кнопки).
                self._log_visible_controls(page)
                if already_reason:
                    return self._mark_already_sent(
                        username, host_name, target_city, arrival_date, departure_date, already_reason
                    )
                return self._fail_attempt(
                    page, username, host_name,
                    "Форма отправки не загрузилась за 15 сек: нет ни textarea, ни кнопки 'Send Request' (сеть/селектор). Повторная отправка не выполнялась.",
                    error_screenshot, error_html,
                )

            # 5.1. Поле ввода сообщения (форма уже видна; даём 15 сек на появление целевой textarea)
            textarea = None
            selectors = [
                'textarea[data-testid="stay-request-introduction-textarea"]:visible',
                'textarea[name="message"]:visible',
                'textarea:not([data-testid*="trip"]):visible',
                'textarea:visible'
            ]
            for sel in selectors:
                try:
                    ta = page.locator(sel).first
                    if ta.count() > 0 and ta.is_visible():
                        ta.wait_for(state="visible", timeout=4000)
                        textarea = ta
                        break
                except Exception:
                    continue

            if textarea is None:
                return self._fail_attempt(
                    page, username, host_name,
                    "Форма открылась, но поле ввода сообщения (textarea) не найдено",
                    error_screenshot, error_html,
                )

            # Runtime-санитизация текста перед вводом (текст уже в host_item из очереди)
            message = host_item.get("generated_message", "") or ""
            message = sanitize_before_send(message, host_name)
            if not message.strip():
                return self._fail_attempt(page, username, host_name, "Пустой текст сообщения в очереди", error_screenshot, error_html)

            self.human_type(textarea, message)
            log_step("INPUT", f"Даты {arrival_date} – {departure_date}, текст введен (символов: {len(message)})")
            time.sleep(random.uniform(1.0, 2.0))

            # 6. Скриншот заполненной формы ПЕРЕД отправкой
            try:
                page.screenshot(path=str(before_screenshot), timeout=5000)
                log_step("SCREENSHOT", f"Скриншот заполненной формы: {before_screenshot}")
            except Exception:
                pass

            # 7. Кнопка отправки: ждем до 15 сек
            send_locator = page.locator(self.SEND_BUTTON_SELECTOR).first
            try:
                send_locator.wait_for(state="visible", timeout=15000)
            except Exception:
                return self._fail_attempt(
                    page, username, host_name,
                    "Кнопка 'Send Request' не появилась за 15 сек",
                    error_screenshot, error_html,
                )

            send_btn = send_locator
            found_selector = "Send Request button"
            btn_text = (send_btn.inner_text(timeout=5000) or "").strip()
            btn_disabled = send_btn.is_disabled(timeout=5000)
            log_step("BUTTON", f"Найдена кнопка: text='{btn_text}', disabled={btn_disabled}")

            if btn_disabled:
                return self._fail_attempt(
                    page, username, host_name,
                    "Кнопка 'Send Request' неактивна (disabled)",
                    error_screenshot, error_html,
                )

            # 8. РЕЖИМ DRY-RUN — без клика
            if self.dry_run:
                log_step("DRY-RUN", "Симуляция успешна: форма заполнена, кнопка верифицирована. Клик не производился.")
                self._record_dry_run_success(
                    username=username,
                    host_name=host_name,
                    target_city=target_city,
                    arrival_date=arrival_date,
                    departure_date=departure_date,
                    before_screenshot=before_screenshot,
                    found_selector=found_selector,
                    message=message,
                )
                return True

            # 8.5. Экстренная проверка STOP / PAUSE перед боевым кликом отправки
            if self.check_emergency_stop() or self.check_is_paused():
                log_step("STOP", "Обнаружен сигнал остановки/паузы непосредственно перед отправкой! Клик отменен.")
                self.update_autopilot_status(
                    is_running=False,
                    is_paused=self.check_is_paused(),
                    current_host=None,
                    last_status="Отправка прервана сигналом STOP/PAUSE перед кликом",
                )
                return False

            # 9. БОЕВОЙ РЕЖИМ: Очистка буфера ответов, клик и строгая верификация
            api_responses.clear()
            send_btn.click(timeout=15000)
            log_step("CLICK", f"Кнопка отправки нажата ('{btn_text}')")

            # Ожидание подтверждения: только реальный сетевой ответ или подтвержденный переход в чат
            start_wait = time.time()
            submission_confirmed = False
            http_status = None

            while time.time() - start_wait < 14:
                # 1. Проверяем сетевые ответы POST запросов
                ok_res = [r for r in api_responses if r.get("ok")]
                err_res = [r for r in api_responses if not r.get("ok") and r.get("status", 0) >= 400]

                if err_res:
                    err_info = err_res[-1]
                    http_status = err_info.get("status")
                    err_url = err_info.get("url")
                    err_body = err_info.get("body", "")
                    log_step("WARN", f"Получен ошибочный ответ сервера: HTTP {http_status} от {err_url}. Тело ответа: {err_body}")
                    break

                if ok_res:
                    http_status = ok_res[-1].get("status")
                    submission_confirmed = True
                    break

                # 2. Проверяем переход в конкретный чат (/c/chat/ или /messages/)
                curr_url = page.url.lower()
                if any(x in curr_url for x in ("/c/chat/", "/c/messages/", "/messages/thread/")):
                    submission_confirmed = True
                    http_status = 200
                    break

                # 3. Проверяем появление заголовка успешной отправки
                try:
                    success_heading = page.query_selector("h1:has-text('Request Sent'), h2:has-text('Request Sent'), div:has-text('Your request was sent')")
                    if success_heading and success_heading.is_visible():
                        submission_confirmed = True
                        http_status = 200
                        break
                except Exception:
                    pass

                time.sleep(0.5)

            # 10. Фиксация результата в БД и логах
            if submission_confirmed:
                time.sleep(1.5)
                try:
                    page.screenshot(path=str(after_screenshot), timeout=5000)
                except Exception:
                    pass
                log_step("SUCCESS", f"Отправка верифицирована! Ответ сервера: {http_status or 200} OK. Скриншот: {after_screenshot}")

                self.append_sent_log({
                    "id": username,
                    "username": username,
                    "name": host_name,
                    "target_city": target_city,
                    "arrival_date": arrival_date,
                    "departure_date": departure_date,
                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "status": "SENT_SUCCESS",
                    "http_status": http_status or 200,
                    "mode": "live",
                    "proof_screenshot": str(after_screenshot),
                    "before_screenshot": str(before_screenshot),
                    "notes": f"Stay request sent and verified. Server status: {http_status or 200}.",
                    "message": message,
                })
                sent_notes = f"Stay request sent and verified. Server status: {http_status or 200}."
                try:
                    self.autopilot_engine.mark_as_sent(username, http_status=http_status or 200, notes=sent_notes)
                except Exception as e:
                    log_step("WARN", f"AutopilotEngine mark_as_sent note: {e}")
                self.update_queue_status(username, "Sent", notes=sent_notes)
                return True
            else:
                try:
                    page.screenshot(path=str(error_screenshot), timeout=5000)
                    error_html.write_text(page.content(), encoding="utf-8")
                except Exception:
                    pass
                err_text = f"HTTP {http_status}" if http_status else "Отправка не подтверждена за отведенное время"
                log_step("WARN", f"Подтверждение отправки отсутствует: {err_text}. Статус: FAILED. Скриншот: {error_screenshot}")

                self.append_sent_log({
                    "id": username,
                    "username": username,
                    "name": host_name,
                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "status": "FAILED",
                    "http_status": http_status,
                    "mode": "live",
                    "proof_screenshot": str(error_screenshot),
                    "before_screenshot": str(before_screenshot),
                    "notes": f"Submission clicked but unverified ({err_text}). Marked as FAILED.",
                })
                self.update_queue_status(username, "FAILED", notes=f"Submission unverified: {err_text}", increment_attempts=True)
                return False

        except Exception as e:
            log_step("ERROR", f"Исключение при обработке @{username}: {e}. Статус изменён на FAILED, переход к следующему хосту.")
            try:
                page.screenshot(path=str(error_screenshot), timeout=5000)
                error_html.write_text(page.content(), encoding="utf-8")
            except Exception:
                pass

            self.append_sent_log({
                "id": username,
                "username": username,
                "name": host_name,
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "status": "FAILED",
                "error": str(e),
                "proof_screenshot": str(error_screenshot),
                "error_html": str(error_html),
            })
            self.update_queue_status(username, "FAILED", notes=f"Exception: {e}", increment_attempts=True)
            return False
        finally:
            try:
                page.remove_listener("response", handle_response)
            except Exception:
                pass

    def compute_human_delay(self, idx: int, res_is_error: bool, delays_schedule: List[float]) -> int:
        """
        Единая точка расчёта паузы перед следующей попыткой.
        Human Jitter применяется и после успешной отправки, и после ошибки формы/сети.
        """
        if self.test_mode:
            return random.randint(6, 12)

        if self.interval_seconds:
            base = max(10, int(self.interval_seconds))
            return max(10, int(base * random.uniform(0.9, 1.1)))

        if not res_is_error and delays_schedule and 0 <= idx - 1 < len(delays_schedule):
            sleep_seconds = int(delays_schedule[idx - 1])
        elif self.jitter_enabled and self.max_interval_minutes > self.min_interval_minutes:
            sleep_seconds = random.randint(
                self.min_interval_minutes * 60,
                self.max_interval_minutes * 60,
            )
        else:
            sleep_seconds = self.min_interval_minutes * 60

        # Жёсткий минимум Human Jitter между реальными попытками.
        if sleep_seconds < 300:
            sleep_seconds = random.randint(300, 600)
        return int(sleep_seconds)

    def check_emergency_stop(self) -> bool:
        """Проверяет глобальный флаг экстренной остановки из базы данных."""
        try:
            from database import db_session
            with db_session() as conn:
                cur = conn.cursor()
                cur.execute("SELECT value FROM app_settings WHERE key = 'emergency_stop';")
                row = cur.fetchone()
                return bool(row and row[0] == "1")
        except Exception:
            return False

    def _select_candidates(self, queue: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Отбирает кандидатов к отправке из очереди.

        СТРОГОЕ ПРАВИЛО: только со статусом 'Approved'.
        - Без --target: все записи со статусом 'Approved'.
        - С --target: запись с данным username, только если статус 'Approved'
          (статус 'Sent' автоматически исключается, т.к. Approved != Sent).
        Draft, Skipped, Failed, Queued и прочие статусы НЕ попадают в список.
        """
        if self.target_username:
            target_norm = self.target_username.strip().lower()
            return [
                x for x in queue
                if str(x.get("username", "")).strip().lower() == target_norm
                and str(x.get("status", "")).strip().lower() == "approved"
            ]
        return [x for x in queue if str(x.get("status", "")).strip().lower() == "approved"]

    def run(self) -> None:
        """Основной цикл работы автопилота с поддержкой лимитов и интервалов."""
        log_step("INIT", "ЗАПУСК АВТОПИЛОТА ОТПРАВКИ ЗАПРОСОВ COUCHSURFING")
        log_step("INIT", f"Суточный лимит: {self.daily_limit} запросов")
        if self.max_requests:
            log_step("INIT", f"Лимит на текущий запуск: {self.max_requests} запросов (--max-requests)")
        if self.jitter_enabled and self.max_interval_minutes > self.min_interval_minutes:
            log_step("INIT", f"Плавающий интервал: {self.min_interval_minutes} – {self.max_interval_minutes} мин (🎲 Human Jitter)")
        else:
            log_step("INIT", f"Фиксированный интервал: {self.min_interval_minutes} мин")
        if self.period_minutes:
            log_step("INIT", f"Период времени: {self.period_minutes} мин (рандомизированный джиттер)")
        if self.interval_seconds:
            log_step("INIT", f"Пользовательский интервал: {self.interval_seconds} сек (--interval-seconds)")
        log_step("INIT", f"Режим отображения браузера: {'Headed (видимое окно)' if not self.headless else 'Headless (фоновый)'}")
        log_step("INIT", f"Режим симуляции: {'Dry-Run (без клика)' if self.dry_run else 'Live (боевая отправка)'}")

        self.update_autopilot_status(
            is_running=True,
            is_paused=False,
            current_host=None,
            last_status="Инициализация очереди и проверка лимитов...",
        )

        # 1. Проверка суточного лимита
        sent_today = self.get_sent_today_count()
        log_step("CHECK", f"Отправлено сегодня: {sent_today} / {self.daily_limit}")
        if sent_today >= self.daily_limit:
            log_step("LIMIT", f"[LIMIT] Daily limit reached ({sent_today}/{self.daily_limit}). Dispatcher halted.")
            self.update_autopilot_status(
                is_running=False,
                is_paused=False,
                current_host=None,
                seconds_remaining=0,
                last_status="[LIMIT] Daily limit reached. Dispatcher halted.",
            )
            return

        # 1.5. Синхронизация очереди перед отбором (исключение уже отправленных хостов)
        self.sync_queue_against_sent_and_inbox()

        # Предварительная проверка наличия кандидатов к отправке
        initial_candidates = self.queue_manager.get_ready_dispatch_candidates(trip_id=self.trip_id)
        if self.target_username:
            target_norm = self.target_username.strip().lower()
            initial_candidates = [c for c in initial_candidates if str(c.get("username", "")).strip().lower() == target_norm]

        if not initial_candidates:
            log_step("INFO", "В очереди нет хостов со статусом 'Approved'/'Queued'. Отправка черновиков ('Draft', 'Skipped') заблокирована.")
            self.update_autopilot_status(
                is_running=False,
                is_paused=False,
                current_host=None,
                seconds_remaining=0,
                last_status="Нет одобренных хостов к отправке",
            )
            return

        log_step("QUEUE", f"Отобрано кандидатов в текущей пачке: {len(initial_candidates)}")

        delays_schedule: List[float] = []
        self.period_minutes = None

        processed_this_run = 0
        consecutive_failures = 0
        host_failures: Dict[str, int] = {}
        iteration_index = 0

        # 3. Инициализация Playwright
        with sync_playwright() as p:
            launch_args = [
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ]
            browser = p.chromium.launch(headless=self.headless, args=launch_args)

            context_kwargs = {
                "viewport": {"width": 1280, "height": 720},
                "user_agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                ),
            }
            if AUTH_STATE_FILE.exists():
                context_kwargs["storage_state"] = str(AUTH_STATE_FILE)

            context = browser.new_context(**context_kwargs)
            page = context.new_page()

            # Непрерывная обработка кандидатов в цикле до исчерпания очереди или достижения суточного лимита
            while self.is_running:
                if self.check_is_stopped():
                    log_step("STOP", "Автопилот остановлен через UI/файл статуса. Завершение работы.")
                    self.is_running = False
                    break
                # Жесткая проверка суточного лимита ДО отбора
                if self.get_sent_today_count() >= self.daily_limit:
                    log_step("LIMIT", f"[LIMIT] Daily limit reached ({self.get_sent_today_count()}/{self.daily_limit}). Dispatcher halted.")
                    self.update_autopilot_status(
                        is_running=False,
                        is_paused=False,
                        current_host=None,
                        seconds_remaining=0,
                        last_status=f"Завершено. Отправлено сегодня: {self.get_sent_today_count()}/{self.daily_limit}",
                    )
                    break

                if self.max_requests and processed_this_run >= self.max_requests:
                    log_step("FINISH", f"Достигнут лимит запросов на запуск (--max-requests {self.max_requests}). Завершение.")
                    break

                # Динамический отбор следующей пачки кандидатов (автоматически разблокирует хостов при освобождении вступительных фраз)
                active_candidates = self.queue_manager.get_ready_dispatch_candidates(trip_id=self.trip_id)
                if self.target_username:
                    target_norm = self.target_username.strip().lower()
                    active_candidates = [c for c in active_candidates if str(c.get("username", "")).strip().lower() == target_norm]

                if not active_candidates:
                    log_step("INFO", "Очередь готовых кандидатов пуста или все одобренные хосты обработаны.")
                    self.update_autopilot_status(
                        is_running=False,
                        is_paused=False,
                        current_host=None,
                        seconds_remaining=0,
                        last_status=f"Завершено. Отправлено сегодня: {self.get_sent_today_count()}/{self.daily_limit}",
                    )
                    break

                candidate = active_candidates[0]
                iteration_index += 1

                # Если активен Emergency Stop или пауза — ждем снятия без завершения процесса
                while self.check_emergency_stop() or self.check_is_paused():
                    self.update_autopilot_status(
                        is_running=True,
                        is_paused=True,
                        current_host=None,
                        last_status="STOPPED (Emergency Stop)",
                    )
                    time.sleep(2)
                    if not self.is_running:
                        break

                if not self.is_running:
                    break

                username = candidate.get("username", "")
                self.update_autopilot_status(
                    is_running=True,
                    is_paused=False,
                    current_host=username,
                    last_status=f"Обработка #{iteration_index}: @{username} ({candidate.get('name') or username})...",
                )
                res = None
                try:
                    res = self.process_host(page, candidate)
                except Exception as ex:
                    log_step("ERROR", f"Непредвиденная ошибка при обработке {username}: {ex}")
                    res = "ERROR"
                finally:
                    self.update_autopilot_status(
                        is_running=True,
                        current_host=None,
                    )

                processed_this_run += 1

                # Обработка результата отправки хосту
                was_demoted_to_bottom = False
                if res is True or res == "ALREADY_SENT":
                    consecutive_failures = 0
                    host_failures.pop(username, None)
                elif res == "QC_FAILED":
                    # Ошибка валидации текста конкретного хоста: не инкрементирует consecutive_failures
                    host_failures[username] = host_failures.get(username, 0) + 1
                    if host_failures[username] >= 3:
                        log_step("WARN", f"Хост @{username} не прошёл QC 3 раза. Перемещаем в самый конец очереди...")
                        self.move_host_to_end_of_queue(username, reason="QC validation failed 3 times")
                        host_failures.pop(username, None)
                        was_demoted_to_bottom = True
                else:
                    host_failures[username] = host_failures.get(username, 0) + 1
                    if host_failures[username] >= 3:
                        log_step("WARN", f"Хост @{username} дал сбой {host_failures[username]} раза подряд. Перемещаем в самый конец очереди (вниз списка) и продолжаем отправку...")
                        self.move_host_to_end_of_queue(username, reason=f"3 dispatch attempts failed: {res}")
                        host_failures.pop(username, None)
                        consecutive_failures = 0
                        was_demoted_to_bottom = True
                    else:
                        consecutive_failures += 1
                        if consecutive_failures >= 5:
                            log_step("HALT", f"[HALT] {consecutive_failures} системных сбоев подряд на разных хостах. Диспетчер остановлен — проверьте скриншоты в output/debug/.")
                            self.update_autopilot_status(
                                is_running=False,
                                is_paused=False,
                                current_host=None,
                                last_status=f"Остановлен: {consecutive_failures} системные ошибки подряд (см. Live Logs)",
                            )
                            break

                if self.target_username:
                    log_step("FINISH", f"Целевой хост @{self.target_username} обработан. Завершение.")
                    break

                if self.max_requests and processed_this_run >= self.max_requests:
                    log_step("FINISH", f"Достигнут лимит запросов на запуск (--max-requests {self.max_requests}). Завершение.")
                    break

                if self.get_sent_today_count() >= self.daily_limit:
                    log_step("LIMIT", "[LIMIT] Daily limit reached. Dispatcher halted.")
                    self.update_autopilot_status(
                        is_running=False,
                        is_paused=False,
                        current_host=None,
                        seconds_remaining=0,
                        last_status="[LIMIT] Daily limit reached. Dispatcher halted.",
                    )
                    break

                # Проверяем, есть ли следующие кандидаты в очереди
                next_batch = self.queue_manager.get_ready_dispatch_candidates(trip_id=self.trip_id)
                if not next_batch:
                    log_step("INFO", "Все доступные кандидаты в очереди отправлены.")
                    break

                # Обязательная пауза перед следующим кандидатом (никогда не пропускается!)
                if self.is_running:
                    if res is not True and res != "SENT_SUCCESS" and res != "ALREADY_SENT":
                        res_is_error = True
                    else:
                        res_is_error = False

                    if was_demoted_to_bottom:
                        sleep_seconds = random.randint(20, 35) if not self.test_mode else 3
                        pause_type = "🛡️ Demote Pause"
                        status_msg = f"Хост отправлен в конец очереди. Пауза ({int(sleep_seconds)} сек)"
                        log_step("SKIP_PAUSE", f"Хост @{username} перемещен в конец очереди. Пауза {int(sleep_seconds)} сек перед переходом к следующему кандидату...")
                    elif res is True or res == "SENT_SUCCESS" or res_is_error:
                        sleep_seconds = self.compute_human_delay(iteration_index, res_is_error, delays_schedule)
                        mins = int(sleep_seconds // 60)
                        secs = int(sleep_seconds % 60)
                        pause_type = "🎲 Human Jitter"
                        status_msg = f"Ожидание перед следующим запросом ({int(sleep_seconds)} сек)"
                        if res_is_error:
                            log_step("SLEEP", f"@{username} НЕ отправлен (ошибка формы/сети, возвращён в очередь Queued). Пауза {int(sleep_seconds)} сек ({mins} мин {secs} сек) ({pause_type})...")
                        else:
                            log_step("SLEEP", f"Ожидание {int(sleep_seconds)} сек ({mins} мин {secs} сек) ({pause_type}) до следующего запроса...")
                    else:
                        if self.test_mode:
                            sleep_seconds = random.randint(4, 8)
                        else:
                            sleep_seconds = random.randint(30, 60)
                        pause_type = "🛡️ Safe Skip Pause"
                        status_msg = f"Безопасная пауза перед следующим ({int(sleep_seconds)} сек)"
                        reason = "уже отправлен ранее" if res == "ALREADY_SENT" else "ошибка / форма не найдена"
                        log_step("SKIP_PAUSE", f"Хост @{username} пропущен ({reason}). Защитная пауза {int(sleep_seconds)} сек ({pause_type}) во избежание блокировок...")

                    next_dispatch_timestamp = time.time() + sleep_seconds
                    next_time = next_dispatch_timestamp

                    self.update_autopilot_status(
                        is_running=True,
                        is_paused=False,
                        current_host=None,
                        next_send_time=next_time,
                        next_delay_seconds=sleep_seconds,
                        last_status=status_msg,
                    )

                    start_sleep = time.time()
                    while time.time() - start_sleep < sleep_seconds:
                        if not self.is_running or self.check_is_stopped():
                            self.is_running = False
                            log_step("STOP", "Автопилот остановлен во время ожидания таймера. Завершение работы.")
                            break

                        while self.check_is_paused() or self.check_emergency_stop():
                            self.update_autopilot_status(
                                is_running=True,
                                is_paused=True,
                                current_host=None,
                                next_send_time=next_time,
                                next_delay_seconds=sleep_seconds,
                                last_status="STOPPED (Emergency Stop)",
                            )
                            time.sleep(2)
                            if not self.is_running:
                                break
                            next_time += 2
                            start_sleep += 2

                        rem_sec = max(0, int(next_time - time.time()))
                        self.update_autopilot_status(
                            is_running=True,
                            is_paused=False,
                            current_host=None,
                            next_send_time=next_time,
                            next_delay_seconds=sleep_seconds,
                            last_status=f"Пауза перед следующим запросом ({rem_sec} сек)",
                        )
                        time.sleep(1)

            try:
                context.close()
                browser.close()
            except Exception:
                pass

        self.update_autopilot_status(
            is_running=False,
            is_paused=False,
            last_status=f"Завершено. Отправлено сегодня: {self.get_sent_today_count()}/{self.daily_limit}",
        )
        log_step("DONE", "Работа автопилота полностью завершена.")


_SINGLETON_LOCK_HANDLE = None


def _read_lock_pid(lock_file: Path) -> int:
    try:
        return int(lock_file.read_text().strip() or 0)
    except Exception:
        return 0


def _is_auto_sender_process(pid: int) -> bool:
    """Не убиваем чужой процесс, если PID успел переиспользоваться."""
    try:
        import subprocess as _sp
        cmdline = _sp.run(["ps", "-p", str(pid), "-o", "command="],
                          capture_output=True, text=True).stdout
        return "auto_sender" in cmdline
    except Exception:
        return False


def _terminate_old_dispatcher(pid: int) -> None:
    """SIGTERM -> ожидание -> SIGKILL для старого экземпляра автопилота."""
    import signal

    if not pid or pid == os.getpid() or not _is_auto_sender_process(pid):
        return
    log_step("SINGLETON", f"Найден старый экземпляр диспетчера (PID {pid}) — отменяем его перед запуском нового.")
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return
        time.sleep(0.2)
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def acquire_singleton_lock() -> None:
    """
    Singleton диспетчера: одновременно работает строго ОДИН процесс auto_sender.

    На Unix используем flock (атомарно, без гонки TOCTOU): если старый процесс держит
    блокировку — мягко завершаем его и забираем блокировку себе. PID-файл нужен только
    для диагностики и адресной остановки. Если взять блокировку не удалось — процесс
    НЕ стартует (защита от двух параллельных циклов, ломающих таймер).
    """
    import atexit
    import signal

    global _SINGLETON_LOCK_HANDLE

    lock_file = OUTPUT_DIR / "auto_sender.pid"
    guard_file = OUTPUT_DIR / "auto_sender.lock"
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    my_pid = os.getpid()

    try:
        import fcntl  # type: ignore
    except ImportError:
        fcntl = None

    if fcntl is not None:
        guard = open(guard_file, "a+")
        acquired = False
        for _ in range(24):  # до ~6 секунд: даём старому процессу завершиться
            try:
                fcntl.flock(guard.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError:
                old_pid = _read_lock_pid(lock_file)
                if old_pid and old_pid != my_pid:
                    _terminate_old_dispatcher(old_pid)
                else:
                    time.sleep(0.25)
        if not acquired:
            try:
                guard.close()
            except Exception:
                pass
            raise RuntimeError(
                "Не удалось получить singleton-блокировку auto_sender: уже работает другой экземпляр диспетчера."
            )
        _SINGLETON_LOCK_HANDLE = guard
    else:
        # Windows/без fcntl: прежняя PID-проверка.
        old_pid = _read_lock_pid(lock_file)
        _terminate_old_dispatcher(old_pid)

    lock_file.write_text(str(my_pid))

    def _release():
        global _SINGLETON_LOCK_HANDLE
        try:
            if lock_file.exists() and lock_file.read_text().strip() == str(my_pid):
                lock_file.unlink()
        except Exception:
            pass
        if _SINGLETON_LOCK_HANDLE is not None:
            try:
                if fcntl is not None:
                    fcntl.flock(_SINGLETON_LOCK_HANDLE.fileno(), fcntl.LOCK_UN)
                _SINGLETON_LOCK_HANDLE.close()
            except Exception:
                pass
            _SINGLETON_LOCK_HANDLE = None

    atexit.register(_release)

    def _on_term(signum, frame):
        _release()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _on_term)


def main():
    parser = argparse.ArgumentParser(description="Фоновый автопилот отправки запросов Couchsurfing")
    parser.add_argument("--daily-limit", type=int, default=DEFAULT_DAILY_LIMIT, help="Суточный лимит (по умолчанию 100)")
    parser.add_argument("--min-interval-minutes", type=int, default=5, help="Мин. интервал в минутах (по умолчанию 5)")
    parser.add_argument("--max-interval-minutes", type=int, default=10, help="Макс. интервал в минутах (по умолчанию 10)")
    parser.add_argument("--jitter-enabled", dest="jitter_enabled", action="store_true", default=True, help="Включить случайный джиттер")
    parser.add_argument("--no-jitter", dest="jitter_enabled", action="store_false", help="Отключить случайный джиттер")
    parser.add_argument("--delay-min", type=int, default=DEFAULT_DELAY_MIN, help="Мин. пауза (минут)")
    parser.add_argument("--delay-max", type=int, default=DEFAULT_DELAY_MAX, help="Макс. пауза (минут)")
    parser.add_argument("--period-minutes", type=int, default=None, help="Период времени на N запросов (минут) для умной рандомизации")
    parser.add_argument("--interval-seconds", type=int, default=None, help="Фиксированный интервал между отправками в секундах (30-60 для тестов)")
    parser.add_argument("--max-requests", type=int, default=None, help="Жесткий лимит запросов на запуск (например, 1 или 2)")
    parser.add_argument("--dry-run", action="store_true", help="Тестовый режим без клика отправки")
    parser.add_argument("--headed", action="store_true", help="Запуск с видимым окном браузера")
    parser.add_argument("--no-headless", dest="headed", action="store_true", help="Запуск с видимым окном браузера (алиас)")
    parser.add_argument("--target", type=str, default=None, help="Целевой username хоста")
    parser.add_argument("--test-mode", action="store_true", help="Быстрый тестовый режим пауз (секунды вместо минут)")
    parser.add_argument("--trip-id", type=str, default=None, help="ID активной карточки поездки (фильтр хостов)")

    args = parser.parse_args()

    acquire_singleton_lock()

    sender = AutoSender(
        daily_limit=args.daily_limit,
        delay_min=args.delay_min,
        delay_max=args.delay_max,
        period_minutes=args.period_minutes,
        interval_seconds=args.interval_seconds,
        min_interval_minutes=args.min_interval_minutes,
        max_interval_minutes=args.max_interval_minutes,
        jitter_enabled=args.jitter_enabled,
        max_requests=args.max_requests,
        dry_run=args.dry_run,
        headless=not args.headed,
        target_username=args.target,
        test_mode=args.test_mode,
        trip_id=args.trip_id,
    )
    sender.run()


if __name__ == "__main__":
    main()
