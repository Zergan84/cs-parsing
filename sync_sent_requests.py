#!/usr/bin/env python3
"""
sync_sent_requests.py - Скрипт синхронизации отправленных запросов Couchsurfing.

Открывает страницу https://www.couchsurfing.com/c/chat/request/sent с авторизованной
сессией Playwright, собирает всех адресатов отправленных диалогов (через перехват
API communicationRequest и анализ DOM) и сохраняет их в output/sent_history.json.
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Set

from playwright.sync_api import sync_playwright

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BASE_DIR = Path(__file__).parent
OUTPUT_DIR = BASE_DIR / "output"
USER_DATA_DIR = BASE_DIR / "user_data"
AUTH_STATE_FILE = USER_DATA_DIR / "auth_state.json"
SENT_HISTORY_FILE = OUTPUT_DIR / "sent_history.json"
SENT_LOG_FILE = OUTPUT_DIR / "sent_log.json"
SENT_REQUESTS_URL = "https://www.couchsurfing.com/c/chat/request/sent"


def sync_sent_requests(headed: bool = False, max_scrolls: int = 15) -> Set[str]:
    """
    Выполняет сбор отправленных запросов через Playwright и сохраняет в sent_history.json.
    """
    print("=" * 65)
    print("🔄 СИНХРОНИЗАЦИЯ ОТПРАВЛЕННЫХ ЗАПРОСОВ COUCHSURFING")
    print(f"[*] URL: {SENT_REQUESTS_URL}")
    print(f"[*] Режим браузера: {'Headed (видимое окно)' if headed else 'Headless (фоновый)'}")
    print("=" * 65)

    if not AUTH_STATE_FILE.exists():
        print(f"[-] Файл авторизации {AUTH_STATE_FILE} не найден! Сначала выполните вход.")
        return set()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    sent_usernames: Set[str] = set()

    # 1. Загружаем уже известные отправленные из sent_log.json (если есть)
    if SENT_LOG_FILE.exists():
        try:
            with open(SENT_LOG_FILE, "r", encoding="utf-8") as f:
                logs = json.load(f)
            for item in logs:
                if item.get("status") in ("SENT_SUCCESS", "success"):
                    u = item.get("username")
                    if u:
                        sent_usernames.add(u.strip())
        except Exception:
            pass

    # 2. Загружаем ранее сохраненную историю
    if SENT_HISTORY_FILE.exists():
        try:
            with open(SENT_HISTORY_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
            for u in saved.get("sent_usernames", []):
                if u:
                    sent_usernames.add(u.strip())
        except Exception:
            pass

    # 3. Сбор из живого интерфейса Couchsurfing через Playwright
    with sync_playwright() as p:
        launch_args = [
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
        ]
        browser = p.chromium.launch(headless=not headed, args=launch_args)

        context_kwargs = {
            "viewport": {"width": 1280, "height": 800},
            "user_agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            "storage_state": str(AUTH_STATE_FILE),
        }
        context = browser.new_context(**context_kwargs)
        page = context.new_page()

        # Слушатель сетевых ответов API для сбора communicationRequest
        def on_response(res):
            u = res.url.lower()
            if "communicationrequest" in u or "chat." in u:
                try:
                    ct = res.headers.get("content-type", "")
                    if "json" in ct:
                        data = res.json()
                        extract_members_from_json(data, sent_usernames)
                except Exception:
                    pass

        page.on("response", on_response)

        print("[*] Загрузка страницы отправленных запросов...")
        try:
            page.goto(SENT_REQUESTS_URL, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(3500)
        except Exception as e:
            print(f"[!] Примечание при загрузке страницы: {e}")

        # Проверка редиректа на логин
        if "sign_in" in page.url.lower() or "login" in page.url.lower():
            print("[-] Сессия устарела или недействительна! Требуется повторный логин.")
            context.close()
            browser.close()
            return sent_usernames

        print(f"[+] Страница успешно открыта: {page.url}")

        # Скролл списка диалогов для подгрузки всех страниц
        print(f"[*] Автоскролл списка чатов (до {max_scrolls} итераций)...")
        prev_count = len(sent_usernames)

        for step in range(1, max_scrolls + 1):
            # Извлекаем ссылки из DOM
            dom_users = page.evaluate("""() => {
                const found = [];
                const links = document.querySelectorAll("a[href*='/users/'], a[href*='/c/users/'], a[href*='/people/']");
                for (const a of links) {
                    const m = a.href.match(/\\/(?:c\\/)?(?:users|people)\\/([a-zA-Z0-9_.-]+)/);
                    if (m && m[1]) found.push(m[1]);
                }
                return found;
            }""")
            for u in dom_users:
                if u and u.lower() != "vanagaz":
                    sent_usernames.add(u.strip())

            # Скроллим контейнер списка
            scrolled = page.evaluate("""() => {
                const el = document.querySelector("div.overflow-y-scroll, div[class*='overflow-y-scroll'], div[class*='overflow-y-auto']");
                if (el) {
                    const before = el.scrollTop;
                    el.scrollTop += 1400;
                    return el.scrollTop !== before;
                }
                window.scrollBy(0, 1000);
                return true;
            }""")

            page.wait_for_timeout(1500)
            curr_count = len(sent_usernames)

            if step % 3 == 0 or curr_count > prev_count:
                print(f"    [Шаг {step}/{max_scrolls}] Найдено отправленных: {curr_count}")
            prev_count = curr_count

        try:
            context.close()
            browser.close()
        except Exception:
            pass

    # 4. Сохранение результата
    sorted_usernames = sorted(list(sent_usernames), key=lambda x: x.lower())
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    output_data = {
        "sent_usernames": sorted_usernames,
        "total_count": len(sorted_usernames),
        "last_synced": now_iso,
    }

    with open(SENT_HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(output_data, f, ensure_ascii=False, indent=2)

    print("\n" + "=" * 65)
    print(f"[✅] Синхронизация завершена успешно!")
    print(f"[+] Всего уникальных отправленных хостов: {len(sorted_usernames)}")
    print(f"[+] Файл сохранен: {SENT_HISTORY_FILE.resolve()}")
    if sorted_usernames:
        sample_str = ", ".join(sorted_usernames[:12])
        print(f"[+] Примеры хостов: {sample_str}...")
    print("=" * 65 + "\n")

    return sent_usernames


def extract_members_from_json(data, target_set: Set[str]) -> None:
    """Извлекает handle получателей из структуры GraphQL/tRPC ответов."""
    if isinstance(data, list):
        for item in data:
            extract_members_from_json(item, target_set)
    elif isinstance(data, dict):
        # Проверяем resolvedMembers
        members = data.get("resolvedMembers")
        if isinstance(members, list):
            for m in members:
                if isinstance(m, dict):
                    handle = m.get("handle")
                    if handle and handle.lower() != "vanagaz":
                        target_set.add(handle.strip())

        # Рекурсивный обход вложенных данных
        for v in data.values():
            if isinstance(v, (dict, list)):
                extract_members_from_json(v, target_set)


def main():
    parser = argparse.ArgumentParser(description="Синхронизация отправленных запросов Couchsurfing")
    parser.add_argument("--headed", action="store_true", help="Показать окно браузера")
    parser.add_argument("--max-scrolls", type=int, default=15, help="Максимальное количество скроллов (по умолчанию 15)")
    args = parser.parse_args()

    sync_sent_requests(headed=args.headed, max_scrolls=args.max_scrolls)


if __name__ == "__main__":
    main()
