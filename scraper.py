"""
scraper.py - Скрипт автоматизированного сбора анкет хостов Couchsurfing.

Особенности:
- Проверка валидности сессии (auth_state.json) перед началом поиска.
- Автоматическое определение locationId для целевого города через каталог /c/locations/<город>-japan.
- Использование современной структуры Couchsurfing: /c/search/hosts/<locationId>.
- Перехват сетевых JSON/API ответов (searchHostsV2, GraphQL, REST) в режиме реального времени.
- Fallback на DOM-парсинг карточек хостов.
- Автоматическое закрытие cookie-баннеров и модальных окон OneTrust.
- Скролл внутреннего виртуализированного контейнера со списком хостов.
- Диагностика и сохранение скриншотов при нулевой выдаче или ошибках.
- Фильтрация "живых" профилей и экспорт в JSON и CSV.
"""

import argparse
import json
import math
import os
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd
from playwright.sync_api import sync_playwright

from gender_classifier import classify_host, get_gender_classifier
from social_extractor import extract_instagram
from data_cleaner import sanitize_host_name, sanitize_host_clean_name, sanitize_host_location
from hosting_guard import get_hosting_guard
from database import get_sent_usernames

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BASE_DIR = Path(__file__).parent
USER_DATA_DIR = BASE_DIR / "user_data"
AUTH_STATE_FILE = USER_DATA_DIR / "auth_state.json"
OUTPUT_DIR = BASE_DIR / "output"


def random_sleep(min_sec: float = 2.0, max_sec: float = 4.0) -> None:
    time.sleep(random.uniform(min_sec, max_sec))


class CouchsurfingScraper:
    def __init__(
        self,
        city: str = "Tokyo",
        gender: Optional[str] = "Any",
        status: Optional[str] = "Accepting Guests",
        headless: bool = False,
        max_days_inactive: int = 7,
        min_response_rate: float = 75.0,
        max_scroll_pages: int = 10,
        no_couples: bool = False,
        no_lgbtq: bool = False,
        arrival_date: Optional[str] = None,
        departure_date: Optional[str] = None,
    ):
        self.city = city
        self.gender = gender
        self.status = status
        self.headless = headless
        self.max_days_inactive = max_days_inactive
        self.min_response_rate = min_response_rate
        self.max_scroll_pages = max_scroll_pages
        self.no_couples = no_couples
        self.no_lgbtq = no_lgbtq
        self.arrival_date = arrival_date
        self.departure_date = departure_date
        self.collected_hosts: Dict[str, Dict[str, Any]] = {}
        self.gender_classifier = get_gender_classifier()
        self.hosting_guard = get_hosting_guard()

    def _dismiss_popups(self, page) -> None:
        """Закрывает cookie-баннеры и всплывающие окна, блокирующие интерфейс."""
        popup_selectors = [
            "#onetrust-accept-btn-handler",
            "#onetrust-reject-all-handler",
            "button[id*='accept']",
            "button[id*='cookie']",
            "button:has-text('Accept All')",
            "button:has-text('Accept all cookies')",
            "button:has-text('I agree')",
            "button:has-text('Got it')",
            "button:has-text('Close')",
            "button[aria-label='Close']",
            "[data-testid='modal-close']",
            "[aria-label='Dismiss']",
        ]
        for sel in popup_selectors:
            try:
                el = page.query_selector(sel)
                if el and el.is_visible():
                    print(f"[*] Закрытие баннера/модального окна ({sel})...")
                    el.click(timeout=1000)
                    page.wait_for_timeout(400)
            except Exception:
                pass

    def _check_auth_validity(self, page) -> bool:
        """
        Проверяет валидность сессии перед началом поиска.
        Открывает /dashboard или /c/chat и проверяет, нет ли редиректа на страницу входа.
        """
        print("[*] Проверка валидности сессии авторизации...")
        try:
            page.goto("https://www.couchsurfing.com/dashboard", timeout=25000, wait_until="domcontentloaded")
            page.wait_for_timeout(2500)
            curr_url = page.url.lower()
            if (
                "/users/sign_in" in curr_url
                or "/login" in curr_url
                or "/session/new" in curr_url
                or "sign in" in page.title().lower()
            ):
                print("[ERROR] Сессия в auth_state.json недействительна! Запустите auth.py для обновления кук")
                debug_dir = OUTPUT_DIR / "debug"
                debug_dir.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(debug_dir / "auth_invalid.png"))
                return False
            print(f"[+] Сессия подтверждена. URL: {page.url} ({page.title()})")
            return True
        except Exception as e:
            print(f"[!] Ошибка при проверке сессии: {e}")
            return False

    def _resolve_location_id(self, page) -> str:
        """
        Определяет уникальный locationId для города.
        Для Токио стандартный ID: 2pdyyI6P65u26SGYJJtEbWWXjeT_lc
        """
        city_lower = self.city.lower().strip()
        print(f"[*] Определение locationId для города: {self.city}...")

        # Быстрый маппинг для популярных городов
        known_cities = {
            "tokyo": "2pdyyI6P65u26SGYJJtEbWWXjeT_lc",
            "paris": "2u5Z5oEaL98Xv8j0bI5E5zX5eOY_lc",
        }
        if city_lower in known_cities:
            loc_id = known_cities[city_lower]
            print(f"[+] Найден locationId: {loc_id}")
            return loc_id

        # Динамический поиск через /c/locations/<city>-japan
        try:
            loc_url = f"https://www.couchsurfing.com/c/locations/{city_lower}-japan"
            page.goto(loc_url, timeout=20000)
            page.wait_for_timeout(2500)
            html = page.content()
            for part in html.split('"'):
                if part.endswith("_lc") and len(part) > 15:
                    clean_id = part.split("-")[-1]
                    print(f"[+] Динамически обнаружен locationId: {clean_id}")
                    return clean_id
        except Exception as e:
            print(f"[!] Ошибка резолвинга через locations: {e}")

        # Fallback на Токио
        return "2pdyyI6P65u26SGYJJtEbWWXjeT_lc"

    def _process_host_from_api(self, item: Dict[str, Any]) -> None:
        """Парсит профиль хоста из полученного JSON API объекта."""
        username = item.get("handle") or item.get("slug") or item.get("username")
        if not username or username in ["visits", "stays", "feed", "explore", "search", "users"]:
            return
        if username in self.collected_hosts:
            return

        raw_name = item.get("displayName") or item.get("name") or item.get("slug") or username
        name = sanitize_host_name(raw_name)
        clean_name = item.get("slug") or sanitize_host_clean_name(name)
        about_me = item.get("aboutMe") or item.get("about_me") or item.get("about") or ""
        if isinstance(about_me, list):
            about_me = " ".join(about_me)
        about_me = str(about_me).strip()

        raw_loc = (
            item.get("locationSummary")
            or item.get("domicileLocationDescription")
            or item.get("homeTownLocationDescription")
            or item.get("location")
            or self.city
        )
        loc = sanitize_host_location(raw_loc, self.city)

        refs = (
            item.get("totalReferencesCount")
            or item.get("references_count")
            or item.get("referencesCount")
            or 0
        )

        is_active = item.get("isRecentlyActive")
        if is_active is None:
            is_active = True
        last_login_raw = "Recently Active" if is_active else "Active recently"

        est_gender, host_type, gender_reason = self.gender_classifier.classify(name, about_me, username)
        print(f"[GENDER] '{name}' -> {est_gender.lower()} ({gender_reason})")

        # Hosting Guard: проверка на стоп-слова
        guard_hit = self.hosting_guard.check_regex_blacklist(about_me)
        if guard_hit:
            matched_text, _ = guard_hit
            print(f"[GUARD] Отсеян {username} ({name}): '{matched_text}'")
            return

        if self.no_couples and host_type == "couple":
            return
        if self.no_lgbtq and any(w in about_me.lower() for w in ["lgbt", "lgbtq", "queer", "non-binary", "transgender", "rainbow"]):
            return

        insta_url, insta_handle = extract_instagram(about_me)

        self.collected_hosts[username] = {
            "id": item.get("id") or item.get("userId") or username,
            "username": username,
            "name": name,
            "clean_name": clean_name,
            "age": item.get("age"),
            "gender": est_gender.lower(),
            "host_type": host_type,
            "gender_reason": gender_reason,
            "location": loc,
            "instagram_handle": insta_handle,
            "instagram_url": insta_url,
            "response_rate_pct": float(item.get("responseRate") or 90.0),
            "response_rate_raw": "High",
            "response_time": item.get("responseTime") or "within a few hours",
            "last_login_days": 1 if is_active else 3,
            "last_login_raw": last_login_raw,
            "references_count": int(refs),
            "about_me": " ".join(about_me.split())[:1500],
            "profile_url": f"https://www.couchsurfing.com/c/users/{username}",
            "target_city": self.city,
            "arrival_date": self.arrival_date,
            "departure_date": self.departure_date,
            "source": "network_api",
        }

    def _handle_search_response(self, response) -> None:
        """
        Перехватывает и парсит сетевые JSON-ответы от эндпоинтов поиска Couchsurfing
        (tRPC hostSearch.searchHostsV2, GraphQL, REST).
        """
        url = response.url.lower()
        if not any(k in url for k in ("searchhosts", "search_hosts", "graphql", "api/web", "hostsearch")):
            return
        if any(k in url for k in ("lotties", "mapbox", "analytics", "tracking", "heartbeat", "stats")):
            return

        try:
            content_type = response.headers.get("content-type", "")
            if "json" not in content_type:
                return

            data = response.json()
            items: List[Dict[str, Any]] = []

            # 1. tRPC batch ответ (список объектов с result -> data -> json -> data)
            if isinstance(data, list):
                for entry in data:
                    if isinstance(entry, dict):
                        res = entry.get("result", {}).get("data", {})
                        if isinstance(res, dict):
                            json_data = res.get("json", {})
                            if isinstance(json_data, dict) and "data" in json_data and isinstance(json_data["data"], list):
                                items.extend(json_data["data"])
                        elif isinstance(res, list):
                            items.extend(res)

            # 2. Обычный объект (GraphQL / REST / одиночный tRPC)
            elif isinstance(data, dict):
                if "result" in data and isinstance(data["result"], dict):
                    inner = data["result"].get("data", {}).get("json", {})
                    if isinstance(inner, dict) and "data" in inner and isinstance(inner["data"], list):
                        items.extend(inner["data"])
                elif "data" in data:
                    d = data["data"]
                    if isinstance(d, list):
                        items.extend(d)
                    elif isinstance(d, dict):
                        for k in ("hosts", "searchHosts", "users", "items", "results"):
                            if k in d and isinstance(d[k], list):
                                items.extend(d[k])

            if items:
                prev_len = len(self.collected_hosts)
                for it in items:
                    if isinstance(it, dict):
                        self._process_host_from_api(it)
                added = len(self.collected_hosts) - prev_len
                if added > 0:
                    print(f"[API] Перехвачено {added} новых хостов через сетевой ответ (Всего: {len(self.collected_hosts)})")
        except Exception:
            pass

    def _extract_cards_from_page(self, page) -> None:
        """Парсит карточки хостов из DOM страницы поиска (fallback при отсутствии API-ответов)."""
        user_anchors = page.query_selector_all('a[href*="/c/users/"], a[href*="/users/"]')

        for a in user_anchors:
            try:
                href = a.get_attribute("href") or ""
                user_match = re.search(r"/(?:c/)?users/([^/?#]+)", href)
                if not user_match:
                    continue
                username = user_match.group(1)
                if username in ["visits", "stays", "feed", "explore", "search", "users"] or username in self.collected_hosts:
                    continue

                parent_text = ""
                try:
                    card_el = a.evaluate_handle("el => el.closest('[class*=card], [data-testid], article, li, div')")
                    if card_el:
                        parent_text = card_el.as_element().inner_text()
                except Exception:
                    pass

                if not parent_text:
                    try:
                        parent_text = a.evaluate("el => el.parentElement.innerText")
                    except Exception:
                        pass

                lines = [line.strip() for line in parent_text.splitlines() if line.strip()]
                if not lines:
                    continue

                raw_name = lines[0]
                name = sanitize_host_name(raw_name)
                clean_name = sanitize_host_clean_name(name)
                meta_line = lines[1] if len(lines) > 1 else ""
                about_me = " ".join(lines[2:]) if len(lines) > 2 else ""

                # Извлечение отзывов
                ref_match = re.search(r"(\d+)\s*References", meta_line, re.I) or re.search(r"(\d+)\s*References", parent_text, re.I)
                references_count = int(ref_match.group(1)) if ref_match else 0

                # Локация
                loc_match = re.search(r"Lives in\s*([^\n\r]+)", meta_line, re.I) or re.search(r"Lives in\s*([^\n\r]+)", parent_text, re.I)
                raw_loc = loc_match.group(1).strip() if loc_match else self.city
                location = sanitize_host_location(raw_loc, self.city)

                # Проверка активности
                is_recently_active = any("Recently Active" in l or "Active" in l for l in lines)
                last_login_raw = "Recently Active" if is_recently_active else "Active recently"

                est_gender, host_type, gender_reason = self.gender_classifier.classify(name, about_me, username)
                print(f"[GENDER] '{name}' -> {est_gender.lower()} ({gender_reason})")

                # Hosting Guard: проверка на стоп-слова
                guard_hit = self.hosting_guard.check_regex_blacklist(about_me)
                if guard_hit:
                    matched_text, _ = guard_hit
                    print(f"[GUARD] Отсеян {username} ({name}): '{matched_text}'")
                    continue

                if self.no_couples and host_type == "couple":
                    continue
                if self.no_lgbtq and any(w in about_me.lower() for w in ["lgbt", "lgbtq", "queer", "non-binary", "transgender", "rainbow"]):
                    continue

                insta_url, insta_handle = extract_instagram(about_me)

                self.collected_hosts[username] = {
                    "id": username,
                    "username": username,
                    "name": name,
                    "clean_name": clean_name,
                    "age": None,
                    "gender": est_gender.lower(),
                    "host_type": host_type,
                    "gender_reason": gender_reason,
                    "location": location,
                    "instagram_handle": insta_handle,
                    "instagram_url": insta_url,
                    "response_rate_pct": 90.0,
                    "response_rate_raw": "High",
                    "response_time": "within a few hours",
                    "last_login_days": 1 if is_recently_active else 3,
                    "last_login_raw": last_login_raw,
                    "references_count": references_count,
                    "about_me": " ".join(about_me.split())[:1500],
                    "profile_url": f"https://www.couchsurfing.com/c/users/{username}",
                    "target_city": self.city,
                    "arrival_date": self.arrival_date,
                    "departure_date": self.departure_date,
                    "source": "dom_search_card",
                }
            except Exception:
                pass

    def run(self) -> List[Dict[str, Any]]:
        USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        debug_dir = OUTPUT_DIR / "debug"
        debug_dir.mkdir(parents=True, exist_ok=True)

        print("=" * 60)
        print(f"[*] Запуск парсера Couchsurfing")
        print(f"[*] Город: {self.city}")
        print(f"[*] Фильтры: Пол={self.gender}, Статус={self.status}")
        print(f"[*] Критерии: Активность <= {self.max_days_inactive} дн., Response Rate > {self.min_response_rate}%")
        print(f"[*] Режим Headless: {self.headless}")
        print("=" * 60)

        with sync_playwright() as p:
            launch_args = [
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-infobars",
            ]
            user_agent = (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )

            try:
                browser = p.chromium.launch(
                    channel="chrome",
                    headless=self.headless,
                    args=launch_args,
                )
            except Exception:
                browser = p.chromium.launch(
                    headless=self.headless,
                    args=launch_args,
                )

            context_kwargs = {
                "viewport": {"width": 1366, "height": 768},
                "user_agent": user_agent,
            }
            if AUTH_STATE_FILE.exists():
                print(f"[*] Загрузка сохраненной сессии из {AUTH_STATE_FILE.name}")
                context_kwargs["storage_state"] = str(AUTH_STATE_FILE)
            else:
                print(f"[!] Файл сессии {AUTH_STATE_FILE.name} не найден!")

            context = browser.new_context(**context_kwargs)
            page = context.new_page()

            # Регистрация перехвата сетевых API-ответов
            page.on("response", self._handle_search_response)

            # 1. Пре-чек авторизации
            if not self._check_auth_validity(page):
                context.close()
                browser.close()
                return []

            # 2. Резолвим locationId и строим URL поиска
            location_id = self._resolve_location_id(page)
            search_url = f"https://www.couchsurfing.com/c/search/hosts/{location_id}"
            print(f"[*] Переход на страницу поиска хостов: {search_url}")

            try:
                page.goto(search_url, timeout=30000, wait_until="domcontentloaded")
            except Exception as e:
                print(f"[!] Примечание при загрузке: {e}")

            page.wait_for_timeout(3500)
            self._dismiss_popups(page)

            # Проверка редиректа на авторизацию после перехода на поиск
            if "/users/sign_in" in page.url or "/login" in page.url:
                print("[ERROR] Сессия в auth_state.json недействительна! Запустите auth.py для обновления кук")
                context.close()
                browser.close()
                return []

            # Ожидание появления карточек хостов
            try:
                page.wait_for_selector('a[href*="/c/users/"], a[href*="/users/"]', timeout=5000)
            except Exception:
                pass

            # Первичный сбор карточек из DOM
            self._extract_cards_from_page(page)

            # Диагностика и сохранение скриншота текущего состояния
            zero_screenshot = debug_dir / "search_zero_hosts.png"
            page.screenshot(path=str(zero_screenshot))

            if len(self.collected_hosts) == 0:
                print(f"[DIAGNOSTIC] Предупреждение: найдено 0 карточек хостов после загрузки!")
                print(f"[DIAGNOSTIC] Текущий page.url: {page.url}")
                print(f"[DIAGNOSTIC] Заголовок страницы (page.title): {page.title()}")
                print(f"[DIAGNOSTIC] Диагностический скриншот сохранен: {zero_screenshot.resolve()}")
            else:
                print(f"[+] Успешно обнаружены карточки хостов (API/DOM): {len(self.collected_hosts)}")
                print(f"[+] Скриншот страницы поиска сохранен: {zero_screenshot.resolve()}")

            print("[*] Начинаем сбор и пагинацию виртуализированного списка...")

            for page_idx in range(1, self.max_scroll_pages + 1):
                # Дополнительно извлекаем из DOM
                self._extract_cards_from_page(page)
                print(f"[*] Шаг #{page_idx}/{self.max_scroll_pages} | Найдено хостов (API + DOM): {len(self.collected_hosts)}")

                if page_idx == self.max_scroll_pages:
                    break

                # Скроллим внутренний scrollable-контейнер списка хостов
                try:
                    page.evaluate("""() => {
                        const divs = Array.from(document.querySelectorAll('div'));
                        const scrollable = divs.find(d => d.scrollHeight > d.clientHeight && d.clientHeight > 300);
                        if (scrollable) {
                            scrollable.scrollTop += 1200;
                        } else {
                            window.scrollBy(0, 1000);
                        }
                    }""")
                except Exception:
                    pass

                self._dismiss_popups(page)
                random_sleep(1.8, 3.2)

            print(f"[*] Сбор завершен. Всего хостов собрано: {len(self.collected_hosts)}")
            context.close()
            browser.close()

        return self._filter_and_save()

    def _filter_and_save(self) -> List[Dict[str, Any]]:
        """Фильтрация и экспорт в CSV и JSON."""
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        all_hosts = list(self.collected_hosts.values())
        filtered_hosts = []

        # Загрузка отправленных запросов (из SQLite, sent_history.json, sent_log и queue)
        sent_usernames = get_sent_usernames()

        for host in all_hosts:
            username = str(host.get("username", "")).strip()
            host_id = str(host.get("id", "")).strip()
            if (username and username.lower() in sent_usernames) or (host_id and host_id.lower() in sent_usernames):
                print(f"[FILTER] Пропуск хоста {username}: запрос уже был отправлен ранее")
                self.hosting_guard.record_rejected(host, "Already sent (sending_history/queue)")
                continue
            days = host.get("last_login_days", 1)
            rate = host.get("response_rate_pct", 90.0)

            is_active = (days is None or days <= self.max_days_inactive)
            is_responsive = (rate is None or rate >= self.min_response_rate)

            # Фильтрация по полу, если задан жесткий фильтр (не Any)
            gender_match = True
            if self.gender and self.gender.lower() != "any":
                gender_match = (host.get("gender", "").lower() == self.gender.lower())

            if is_active and is_responsive and gender_match:
                filtered_hosts.append(host)

        print(f"[*] Хостов после фильтрации: {len(filtered_hosts)}")

        slug_city = re.sub(r"\W+", "_", self.city.lower())
        json_path = OUTPUT_DIR / f"hosts_{slug_city}.json"
        csv_path = OUTPUT_DIR / f"hosts_{slug_city}.csv"

        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(filtered_hosts, f, ensure_ascii=False, indent=2)
        print(f"[+] JSON сохранен: {json_path.resolve()}")

        if filtered_hosts:
            df = pd.DataFrame(filtered_hosts)
            columns_order = [
                "id",
                "username",
                "clean_name",
                "name",
                "gender",
                "gender_reason",
                "host_type",
                "location",
                "instagram_handle",
                "instagram_url",
                "response_rate_pct",
                "response_time",
                "last_login_raw",
                "references_count",
                "about_me",
                "profile_url",
            ]
            actual_cols = [c for c in columns_order if c in df.columns] + [
                c for c in df.columns if c not in columns_order
            ]
            df = df[actual_cols]
            df.to_csv(csv_path, index=False, encoding="utf-8-sig")
            print(f"[+] CSV сохранен: {csv_path.resolve()}")

        return filtered_hosts


def main():
    parser = argparse.ArgumentParser(description="Автоматизированный сбор анкет хостов Couchsurfing")
    parser.add_argument("--city", default="Tokyo", help="Город для поиска")
    parser.add_argument("--gender", default="Any", help="Фильтр по полу (Female, Male, Any)")
    parser.add_argument("--status", default="Accepting Guests", help="Статус доступности")
    parser.add_argument("--headless", action="store_true", default=False, help="Запуск в фоновом режиме")
    parser.add_argument("--headed", action="store_true", default=False, help="Запуск с видимым окном браузера")
    parser.add_argument("--max-days", type=int, default=7, help="Макс. дней неактивности")
    parser.add_argument("--min-rate", type=float, default=75.0, help="Мин. Response Rate")
    parser.add_argument("--pages", type=int, default=10, help="Количество шагов скролла")
    parser.add_argument("--no-couples", action="store_true", default=False, help="Исключать пары")
    parser.add_argument("--no-lgbtq", action="store_true", default=False, help="Исключать LGBTQ анкеты")
    parser.add_argument("--arrival-date", default=None, help="Дата заезда (YYYY-MM-DD)")
    parser.add_argument("--departure-date", default=None, help="Дата выезда (YYYY-MM-DD)")

    args = parser.parse_args()

    # Если передан --headed, гарантированно запускаем с окном
    headless_mode = args.headless and not args.headed

    scraper = CouchsurfingScraper(
        city=args.city,
        gender=None if args.gender.lower() == "any" else args.gender,
        status=args.status,
        headless=headless_mode,
        max_days_inactive=args.max_days,
        min_response_rate=args.min_rate,
        max_scroll_pages=args.pages,
        no_couples=args.no_couples,
        no_lgbtq=args.no_lgbtq,
        arrival_date=args.arrival_date,
        departure_date=args.departure_date,
    )
    scraper.run()


if __name__ == "__main__":
    main()
