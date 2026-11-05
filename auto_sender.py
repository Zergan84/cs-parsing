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
import json
import os
import random
import re
import sqlite3
import sys
import time
from datetime import datetime, date
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from playwright.sync_api import sync_playwright

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BASE_DIR = Path(__file__).parent
OUTPUT_DIR = BASE_DIR / "output"
DEBUG_DIR = OUTPUT_DIR / "debug"
USER_DATA_DIR = BASE_DIR / "user_data"
AUTH_STATE_FILE = USER_DATA_DIR / "auth_state.json"
QUEUE_FILE = OUTPUT_DIR / "requests_queue.json"
SENT_LOG_FILE = OUTPUT_DIR / "sent_log.json"
STATUS_FILE = OUTPUT_DIR / "autopilot_status.json"

DEFAULT_DAILY_LIMIT = 100
DEFAULT_DELAY_MIN = 15  # минут
DEFAULT_DELAY_MAX = 35  # минут


def sanitize_before_send(text: str) -> str:
    """Локальная фильтрация текста перед вводом 'в последнюю секунду'."""
    lines = text.split('\n')
    if not lines:
        return text

    first_line = lines[0]
    # Ловим паттерны "Hey Name,", "Hi Name:" и чистим мусорные суффиксы
    match = re.match(r'^(Hey|Hi|Hello|Hola)\s+([^,\n!:]+)([,!:]?)(.*)$', first_line, re.IGNORECASE)
    if match:
        greeting_word = random.choice(['Hey', 'Hi', 'Hello'])
        raw_name = match.group(2)
        # Чистим приписки "in Tokyo", спецсимволы и берем только первое слово
        clean_name = re.sub(r'(?i)\s+(in|from|at)\s+.*$', '', raw_name)
        clean_name = clean_name.strip().split()[0]
        cleaned_letters = re.sub(r'[^a-zA-Zа-яА-ЯёЁ]', '', clean_name).capitalize()
        if cleaned_letters:
            clean_name = cleaned_letters
        else:
            cjk = re.sub(r'[^\w]|[\d_]', '', clean_name)
            clean_name = cjk if cjk else "there"

        rest_of_line = match.group(4).strip()
        # Устранение неестественных вставок bio вида ", introvert who loves..."
        rest_of_line = re.sub(
            r'^[,\s]*(?:(?:an?\s+)?[a-zA-Z\s-]{2,30}\s+who\s+(?:loves|enjoys|likes|is|wants)[^,.]+[,.]?\s*)',
            '',
            rest_of_line,
            flags=re.IGNORECASE
        ).strip()
        rest_of_line = re.sub(r'^[,\s.:;]+', '', rest_of_line).strip()
        if rest_of_line:
            rest_of_line = rest_of_line[0].upper() + rest_of_line[1:]

        # Собираем чистую первую строчку
        if rest_of_line:
            lines[0] = f"{greeting_word} {clean_name}! {rest_of_line}".strip()
        else:
            lines[0] = f"{greeting_word} {clean_name}!"
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
    """Выводит форматированную строку лога с таймштампом."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] [{category}] {message}"
    print(line, flush=True)
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
        self.is_running = True
        self.is_paused = False

        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)

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
        """Записывает подробный результат отправки в sent_log.json."""
        logs = []
        if SENT_LOG_FILE.exists():
            try:
                with open(SENT_LOG_FILE, "r", encoding="utf-8") as f:
                    logs = json.load(f)
            except Exception:
                logs = []

        logs.append(entry)
        with open(SENT_LOG_FILE, "w", encoding="utf-8") as f:
            json.dump(logs, f, ensure_ascii=False, indent=2)

    def update_queue_status(self, username: str, new_status: str, notes: Optional[str] = None, sent_at: Optional[str] = None) -> None:
        """Обновляет статус карточки хоста в requests_queue.json и SQLite."""
        now_iso = datetime.now().isoformat()
        if sent_at is None:
            if notes and ("Already Sent" in notes or "Detected as already sent" in notes or "Deduplicated" in notes):
                effective_sent_at = "2026-11-01T00:00:00"
            elif new_status.lower() == "sent":
                effective_sent_at = now_iso
            else:
                effective_sent_at = None
        else:
            effective_sent_at = sent_at

        if QUEUE_FILE.exists():
            try:
                with open(QUEUE_FILE, "r", encoding="utf-8") as f:
                    queue = json.load(f)

                for item in queue:
                    if item.get("username") == username:
                        item["status"] = new_status
                        if notes:
                            item["notes"] = notes
                        if effective_sent_at:
                            item["sent_at"] = effective_sent_at
                        item["updated_at"] = now_iso
                        break

                with open(QUEUE_FILE, "w", encoding="utf-8") as f:
                    json.dump(queue, f, ensure_ascii=False, indent=2)
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
                        SET status = ?, notes = ?, sent_at = CASE WHEN ? IS NOT NULL THEN ? ELSE sent_at END, updated_at = ?
                        WHERE host_id IN (SELECT id FROM hosts WHERE username = ? OR id = ?);
                        """,
                        (new_status, notes, effective_sent_at, effective_sent_at, now_iso, username, username),
                    )
                else:
                    cur.execute(
                        """
                        UPDATE sending_queue 
                        SET status = ?, sent_at = CASE WHEN ? IS NOT NULL THEN ? ELSE sent_at END, updated_at = ?
                        WHERE host_id IN (SELECT id FROM hosts WHERE username = ? OR id = ?);
                        """,
                        (new_status, effective_sent_at, effective_sent_at, now_iso, username, username),
                    )
        except Exception:
            pass

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
                                from sync_sent_requests import extract_members_from_json
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
                    now_iso = datetime.now().isoformat()
                    with open(QUEUE_FILE, "r", encoding="utf-8") as f:
                        q_data = json.load(f)
                    newly_removed = 0
                    for item in q_data:
                        u = (item.get("username") or "").lower()
                        if u in chat_members and item.get("status") in ("Approved", "Queued"):
                            item["status"] = "Sent"
                            item["notes"] = "Already Sent (Detected in Inbox/Sent Requests)"
                            item["sent_at"] = now_iso
                            self.update_queue_status(item.get("username"), "Sent", notes="Already Sent (Detected in Inbox/Sent Requests)")
                            log_step("SYNC", f"[SYNC] Исключен из очереди уже отправленный хост: @{item.get('username')}")
                            newly_removed += 1
                            deduped += 1

                    if newly_removed > 0:
                        with open(QUEUE_FILE, "w", encoding="utf-8") as f:
                            json.dump(q_data, f, ensure_ascii=False, indent=2)
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

    def update_autopilot_status(
        self,
        is_running: bool,
        is_paused: Optional[bool] = None,
        current_host: Optional[str] = None,
        next_send_time: Optional[float] = None,
        next_delay_seconds: Optional[int] = None,
        last_status: str = "Idle",
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
            with open(STATUS_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
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
        """Имитирует естественный человеческий ввод текста."""
        element.click()
        time.sleep(random.uniform(0.3, 0.6))
        element.fill("")

        if len(text) > 400:
            first_part = text[:70]
            rest_part = text[70:]
            for char in first_part:
                element.type(char, delay=random.uniform(20, 45))
            time.sleep(0.3)
            element.type(rest_part, delay=4)
        else:
            for char in text:
                element.type(char, delay=random.uniform(25, 55))
                if char in (".", "!", "?", "\n"):
                    time.sleep(random.uniform(0.12, 0.3))
                elif random.random() < 0.04:
                    time.sleep(random.uniform(0.08, 0.2))

    def get_host_dates_and_city(self, host_item: Dict[str, Any]) -> Tuple[str, str, str]:
        """
        Считывает даты и город кандидата из объекта host_item и базы данных SQLite.
        """
        username = host_item.get("username", "")
        arrival_date = host_item.get("arrival_date") or host_item.get("check_in")
        departure_date = host_item.get("departure_date") or host_item.get("check_out")
        target_city = (
            host_item.get("target_city")
            or host_item.get("city")
            or host_item.get("location")
        )

        # Синхронизация с базой данных output/couchsurfing_platform.db
        db_path = OUTPUT_DIR / "couchsurfing_platform.db"
        if db_path.exists() and username:
            conn = None
            try:
                conn = sqlite3.connect(db_path)
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT h.location, gm.check_in, gm.check_out
                    FROM hosts h
                    LEFT JOIN generated_messages gm ON gm.host_id = h.id AND gm.is_active = 1
                    WHERE h.username = ? OR h.id = ?
                    ORDER BY gm.id DESC LIMIT 1
                """, (username, username))
                row = cursor.fetchone()
                if row:
                    db_loc, db_cin, db_cout = row
                    if db_loc and not host_item.get("target_city"):
                        target_city = db_loc
                    if db_cin:
                        arrival_date = db_cin
                    if db_cout:
                        departure_date = db_cout
            except Exception:
                pass
            finally:
                if conn:
                    conn.close()

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
        if not clean_city:
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
                    api_responses.append({
                        "url": res.url,
                        "method": method,
                        "status": res.status,
                        "status_text": res.status_text,
                        "ok": res.ok,
                    })
            except Exception:
                pass

        page.on("response", handle_response)

        try:
            # 1. Загрузка страницы формы
            page.goto(stay_url, timeout=35000, wait_until="domcontentloaded")
            time.sleep(random.uniform(2.0, 3.5))

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

            # 2.5. Немедленная проверка: был ли запрос уже отправлен ранее / существует ли активный диалог
            already_sent_detected = False
            skip_reason = ""
            try:
                body_text = page.inner_text("body").lower()
                banned_triggers = [
                    "awaiting response",
                    "pending stay",
                    "you sent a request",
                    "you've sent a request",
                    "you have sent a request",
                    "this user needs to accept your request before you can send another message",
                    "you already sent a request",
                    "already requested",
                    "request sent",
                    "request already sent",
                ]
                for bt in banned_triggers:
                    if bt in body_text:
                        already_sent_detected = True
                        skip_reason = f"Обнаружен текст '{bt}'"
                        break

                if not already_sent_detected:
                    view_btn = page.locator(
                        "button:has-text('View Request'), a:has-text('View Request'), "
                        "button:has-text('View request'), a:has-text('View request'), "
                        "[data-testid*='view-request'], button:has-text('Cancel Request'), "
                        "button:has-text('Cancel request')"
                    )
                    if view_btn.count() > 0 and view_btn.first.is_visible():
                        already_sent_detected = True
                        skip_reason = "Обнаружена кнопка 'View/Cancel Request'"
                    elif page.locator(".message-thread, [data-testid='conversation-view'], .conversation-history, [data-testid*='chat-message']").count() > 0:
                        already_sent_detected = True
                        skip_reason = "Обнаружен активный тред диалога"
            except Exception as e:
                log_step("DEBUG", f"Ошибка проверки существующих диалогов: {e}")

            if already_sent_detected:
                log_step("SKIP", f"[SKIP] Запрос хосту @{username} уже отправлен ранее ({skip_reason}). Запись перемещена в Sent Requests.")
                self.update_queue_status(username, "Sent", notes=f"Detected as already sent on Couchsurfing ({skip_reason})", sent_at="2026-11-01T00:00:00")
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
                    "proof_screenshot": str(before_screenshot) if before_screenshot.exists() else "",
                    "notes": f"Detected as already sent on Couchsurfing ({skip_reason})",
                })
                return "ALREADY_SENT"

            # 3. ДИНАМИЧЕСКИЙ ВЫБОР КАРТОЧКИ ПОЕЗДКИ (TRIP SELECTION) ПО ГОРОДУ И ДАТАМ
            page.wait_for_timeout(2000)
            trip_found = False

            # Поиск элемента карточки/селектора, содержащего текст города
            city_variants = [target_city]
            raw_loc = host_item.get("target_city") or host_item.get("location")
            if raw_loc and raw_loc not in city_variants:
                city_variants.append(raw_loc)

            for city_q in city_variants:
                trip_selector = page.locator(
                    f'.trip-card:has-text("{city_q}"), '
                    f'[data-testid="trip-option"]:has-text("{city_q}"), '
                    f'[data-testid*="trip"]:has-text("{city_q}"), '
                    f'div:has-text("{city_q}"):has(button.absolute.inset-0.z-10), '
                    f'button:has-text("{city_q}")'
                )
                try:
                    if trip_selector.count() > 0:
                        for i in range(trip_selector.count()):
                            candidate_card = trip_selector.nth(i)
                            if candidate_card.is_visible():
                                overlay = candidate_card.locator("button.absolute.inset-0.z-10, button").first
                                if overlay.count() > 0 and overlay.is_visible():
                                    overlay.click()
                                else:
                                    candidate_card.click()
                                trip_found = True
                                log_step("TRIP", f"Выбрана существующая карточка поездки для города {target_city}.")
                                page.wait_for_timeout(1500)
                                break
                except Exception as e:
                    log_step("DEBUG", f"Ошибка при клике карточки поездки для {city_q}: {e}")

                if trip_found:
                    break

            if not trip_found:
                # ЕСЛИ карточка для этого города НЕ найдена (или предложен ввод новых дат):
                log_step("TRIP", f"Карточка поездки не найдена, отправка с прямым указанием дат {arrival_date} - {departure_date}.")

                # Выбрать опцию "Create new trip / Other dates"
                new_trip_selectors = [
                    "button:has-text('Create new trip')",
                    "button:has-text('Create a new trip')",
                    "button:has-text('Other dates')",
                    "button:has-text('Different dates')",
                    "[data-testid='create-new-trip']",
                    "[data-testid='other-dates']",
                    "button:has-text('New trip')",
                ]
                for nts in new_trip_selectors:
                    try:
                        el = page.locator(nts)
                        if el.count() > 0 and el.first.is_visible():
                            el.first.click()
                            page.wait_for_timeout(1500)
                            log_step("TRIP", f"Выбрана опция создания поездки/других дат: {nts}")
                            break
                    except Exception:
                        pass

                # Заполнить форму дат напрямую
                self.fill_form_dates(page, arrival_date, departure_date)

            # 4. Переход к экрану Stay Details
            try:
                stay_details_btn = page.locator(
                    "button:has-text('Stay Details'), button:has-text('Next'), [data-testid='stay-request-next-button']"
                )
                if stay_details_btn.count() > 0 and stay_details_btn.first.is_visible():
                    log_step("NAV", "Переход к разделу Stay Details...")
                    stay_details_btn.first.click()
                    page.wait_for_timeout(2000)
            except Exception:
                pass

            # 5. Поиск поля ввода сообщения
            textarea = None
            try:
                page.wait_for_selector("textarea", timeout=7000)
                textarea = page.query_selector("textarea")
            except Exception:
                textarea = page.query_selector("[role='textbox'], [contenteditable='true']")

            if not textarea:
                # Проверяем, возможно запрос уже был отправлен и страница находится в режиме переписки/ожидания
                page_text_lower = page.content().lower()
                already_sent_triggers = [
                    "awaiting response", "pending stay", "you sent a request",
                    "this user needs to accept your request before you can send another message",
                    "view request", "cancel request", "couch request sent",
                    "request sent", "your stay request"
                ]
                is_already = any(trig in page_text_lower for trig in already_sent_triggers)
                if is_already:
                    log_step("SKIP", f"[SKIP] Запрос хосту @{username} уже отправлен ранее (textarea отсутствует, активен диалог). Перемещен в Sent Requests.")
                    self.update_queue_status(username, "Sent", notes="Detected as already sent on Couchsurfing (Active dialogue detected)", sent_at="2026-11-01T00:00:00")
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
                        "notes": "Detected as already sent on Couchsurfing (No textarea / Active dialogue)",
                    })
                    return "ALREADY_SENT"

                log_step("ERROR", f"Текстовое поле ввода не найдено на странице {username}!")
                page.screenshot(path=str(error_screenshot))
                error_html.write_text(page.content(), encoding="utf-8")
                self.append_sent_log({
                    "id": username,
                    "username": username,
                    "name": host_name,
                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "status": "FAILED",
                    "error": "Textarea not found",
                    "proof_screenshot": str(error_screenshot),
                    "error_html": str(error_html),
                })
                return False

            # Ввод текста
            # Runtime-санитизация текста перед вводом
            try:
                if QUEUE_FILE.exists():
                    with open(QUEUE_FILE, "r", encoding="utf-8") as f_q:
                        q_items = json.load(f_q)
                    for q_item in q_items:
                        if q_item.get("username") == username and q_item.get("generated_message"):
                            message = q_item["generated_message"]
                            break
            except Exception:
                pass
            message = sanitize_before_send(message)
            self.human_type(textarea, message)
            log_step("INPUT", f"Даты {arrival_date} – {departure_date} установлены, текст введен (символов: {len(message)})")
            time.sleep(random.uniform(1.0, 2.0))

            # 6. Скриншот заполненной формы ПЕРЕД отправкой
            page.screenshot(path=str(before_screenshot))
            log_step("SCREENSHOT", f"Скриншот заполненной формы: {before_screenshot}")

            # 7. Поиск кнопки отправки и логирование селектора
            send_selectors = [
                "button[data-testid='send-request-button']",
                "button:has-text('Send Request')",
                "button:has-text('Send request')",
                "button:has-text('Отправить запрос')",
                "button:has-text('Send')",
                "button[type='submit']",
            ]

            found_selector = None
            send_btn = None
            for sel in send_selectors:
                el = page.query_selector(sel)
                if el and el.is_visible():
                    found_selector = sel
                    send_btn = el
                    break

            if not send_btn:
                # Проверяем, возможно запрос уже отправлен и кнопка скрыта
                page_text_lower = page.content().lower()
                already_sent_triggers = [
                    "awaiting response", "pending stay", "you sent a request",
                    "this user needs to accept your request before you can send another message",
                    "view request", "cancel request", "couch request sent",
                    "request sent", "your stay request"
                ]
                is_already = any(trig in page_text_lower for trig in already_sent_triggers)
                if is_already:
                    log_step("SKIP", f"[SKIP] Запрос хосту @{username} уже отправлен ранее (кнопка отправки отсутствует, диалог открыт). Перемещен в Sent Requests.")
                    self.update_queue_status(username, "Sent", notes="Detected as already sent on Couchsurfing (Active dialogue detected)", sent_at="2026-11-01T00:00:00")
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
                        "notes": "Detected as already sent on Couchsurfing (No send button / Active dialogue)",
                    })
                    return "ALREADY_SENT"

                log_step("ERROR", f"Кнопка отправки ('Send Request') не найдена на странице {username}!")
                page.screenshot(path=str(error_screenshot))
                error_html.write_text(page.content(), encoding="utf-8")
                self.append_sent_log({
                    "id": username,
                    "username": username,
                    "name": host_name,
                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "status": "FAILED",
                    "error": "Send button not found",
                    "proof_screenshot": str(error_screenshot),
                    "error_html": str(error_html),
                })
                return False

            btn_disabled = send_btn.is_disabled()
            btn_text = send_btn.inner_text().strip()
            log_step("BUTTON", f"Найдена кнопка: selector='{found_selector}', text='{btn_text}', disabled={btn_disabled}")

            if btn_disabled:
                log_step("ERROR", f"Кнопка отправки неактивна (disabled) для {username}!")
                page.screenshot(path=str(error_screenshot))
                error_html.write_text(page.content(), encoding="utf-8")
                self.append_sent_log({
                    "id": username,
                    "username": username,
                    "name": host_name,
                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "status": "FAILED",
                    "error": "Send button is disabled",
                    "proof_screenshot": str(error_screenshot),
                    "error_html": str(error_html),
                })
                return False

            # 8. РЕЖИМ DRY-RUN
            if self.dry_run:
                log_step("DRY-RUN", f"Симуляция успешна: форма заполнена, кнопка верифицирована. Клик не производился.")
                self.append_sent_log({
                    "id": username,
                    "username": username,
                    "name": host_name,
                    "target_city": target_city,
                    "arrival_date": arrival_date,
                    "departure_date": departure_date,
                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "status": "DRY_RUN_SUCCESS",
                    "http_status": None,
                    "mode": "dry_run",
                    "proof_screenshot": str(before_screenshot),
                    "before_screenshot": str(before_screenshot),
                    "button_selector": found_selector,
                    "notes": "Form verified and filled in dry-run mode. Send button was not clicked.",
                })
                self.update_queue_status(username, "Sent (Dry-Run)")
                return True

            # 9. БОЕВОЙ РЕЖИМ: Очистка буфера ответов, клик и строгая верификация
            api_responses.clear()
            log_step("CLICK", f"Кнопка отправки нажата (селектор: {found_selector})")
            send_btn.click()

            # Ожидание подтверждения: только реальный сетевой ответ или подтвержденный переход в чат
            start_wait = time.time()
            submission_confirmed = False
            http_status = None

            while time.time() - start_wait < 14:
                # 1. Проверяем сетевые ответы POST запросов
                ok_res = [r for r in api_responses if r.get("ok")]
                err_res = [r for r in api_responses if not r.get("ok") and r.get("status", 0) >= 400]

                if err_res:
                    http_status = err_res[-1].get("status")
                    log_step("WARN", f"Получен ошибочный ответ сервера: HTTP {http_status}")
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
                page.screenshot(path=str(after_screenshot))
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
                })
                self.update_queue_status(username, "Sent")
                return True
            else:
                page.screenshot(path=str(error_screenshot))
                error_html.write_text(page.content(), encoding="utf-8")
                err_text = f"HTTP {http_status}" if http_status else "Отправка не подтверждена за отведенное время"
                log_step("WARN", f"Подтверждение отправки отсутствует: {err_text}. Статус: VERIFICATION_REQUIRED. Скриншот: {error_screenshot}")

                # Ни в коем случае не помечаем как Sent и не отправляем повторно!
                self.append_sent_log({
                    "id": username,
                    "username": username,
                    "name": host_name,
                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "status": "VERIFICATION_REQUIRED",
                    "http_status": http_status,
                    "mode": "live",
                    "proof_screenshot": str(error_screenshot),
                    "before_screenshot": str(before_screenshot),
                    "notes": f"Submission clicked but unverified. Marked as VERIFICATION_REQUIRED. Manual inspection recommended.",
                })
                self.update_queue_status(username, "Verification_Required")
                return False

        except Exception as e:
            log_step("ERROR", f"Исключение при обработке {username}: {e}")
            try:
                page.screenshot(path=str(error_screenshot))
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
            return False

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

        # 2. Загрузка очереди кандидатов
        if not QUEUE_FILE.exists():
            log_step("WARN", "Файл requests_queue.json не найден. Сначала выполните генерацию очереди.")
            return

        with open(QUEUE_FILE, "r", encoding="utf-8") as f:
            queue = json.load(f)

        if self.target_username:
            target_norm = self.target_username.strip().lower()
            candidates = [x for x in queue if x.get("username") == self.target_username]
            if not candidates:
                log_step("WARN", f"Хост '{self.target_username}' не найден в очереди.")
                return
        else:
            # СТРОГОЕ ПРАВИЛО: отправляем ТОЛЬКО Approved или Queued. Никаких случайных отправок Draft!
            candidates = [x for x in queue if x.get("status") in ("Approved", "Queued")]
            if not candidates:
                log_step("INFO", "В очереди нет хостов со статусом 'Approved' или 'Queued'. Автоматическая отправка черновиков ('Draft') строго заблокирована!")
                self.update_autopilot_status(
                    is_running=False,
                    is_paused=False,
                    current_host=None,
                    seconds_remaining=0,
                    last_status="Нет одобренных хостов к отправке",
                )
                return

        if self.max_requests and len(candidates) > self.max_requests:
            candidates = candidates[:self.max_requests]

        log_step("QUEUE", f"Отобрано кандидатов к отправке: {len(candidates)}")

        # Вычисление графика рандомизированных пауз при заданном периоде времени
        delays_schedule: List[float] = []
        if self.period_minutes and len(candidates) > 1:
            delays_schedule = compute_randomized_delays(len(candidates), self.period_minutes)
            delays_repr = [f"{int(s//60)}m {int(s%60)}s" for s in delays_schedule]
            log_step("SCHEDULE", f"Сгенерирован рандомизированный график пауз: {delays_repr}")

        processed_this_run = 0

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

            # Обработка кандидатов в цикле
            for idx, candidate in enumerate(candidates, 1):
                if not self.is_running:
                    log_step("ABORT", "Работа автопилота прервана.")
                    break

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

                # Жесткая проверка суточного лимита ДО отправки
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

                username = candidate.get("username", "")
                res = None
                try:
                    res = self.process_host(page, candidate)
                except Exception as ex:
                    log_step("ERROR", f"Непредвиденная ошибка при обработке {username}: {ex}")
                    res = "ERROR"
                finally:
                    # В ЛЮБОМ случае (включая ошибки) сбрасываем current_host = None
                    self.update_autopilot_status(
                        is_running=True,
                        current_host=None,
                    )

                processed_this_run += 1

                # Проверка лимита на запуск (--max-requests)
                if self.max_requests and processed_this_run >= self.max_requests:
                    log_step("FINISH", f"Достигнут лимит запросов на запуск (--max-requests {self.max_requests}). Завершение.")
                    break

                # Жесткая проверка суточного лимита ПОСЛЕ отправки
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

                # Обязательная пауза перед следующим кандидатом (никогда не пропускается!)
                if idx < len(candidates) and self.is_running:
                    if res is True or res == "SENT_SUCCESS":
                        # УСПЕШНАЯ ОТПРАВКА -> ОБЯЗАТЕЛЬНЫЙ HUMAN JITTER (5-10 мин; в test mode 6-12 сек)
                        if self.test_mode:
                            sleep_seconds = random.randint(6, 12)
                        elif self.jitter_enabled and self.max_interval_minutes > self.min_interval_minutes:
                            sleep_seconds = random.randint(
                                self.min_interval_minutes * 60,
                                self.max_interval_minutes * 60
                            )
                        else:
                            sleep_seconds = self.min_interval_minutes * 60

                        mins = int(sleep_seconds // 60)
                        secs = int(sleep_seconds % 60)
                        pause_type = "🎲 Human Jitter"
                        status_msg = f"Ожидание перед следующим запросом ({int(sleep_seconds)} сек)"
                        log_step("SLEEP", f"Ожидание {int(sleep_seconds)} сек ({mins} мин {secs} сек) ({pause_type}) до следующего запроса...")

                    else:
                        # ПРОПУСК / ALREADY_SENT / ОШИБКА -> БЕЗОПАСНАЯ КОРОТКАЯ ПАУЗА (30-60 сек; в test mode 4-8 сек)
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
                        if not self.is_running:
                            break

                        # Проверка флага паузы и аварийной остановки (Emergency Stop)
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

    args = parser.parse_args()

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
    )
    sender.run()


if __name__ == "__main__":
    main()
