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
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote

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


# =========================================================================
# Разбор карточек трипов: город, даты, секция "Upcoming Trips"
# =========================================================================

MONTH_ABBR = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
TRIP_SECTION_TITLES = {"Upcoming Trips", "Recently Viewed", "Friends Traveling Soon", "Past Trips"}


def _month_num(name: str) -> Optional[int]:
    if not name:
        return None
    return MONTH_ABBR.get(name.strip().rstrip(".").lower()[:3])


def _safe_iso(year: int, month: int, day: int) -> Optional[str]:
    try:
        return date(year, month, day).isoformat()
    except Exception:
        return None


def normalize_trip_date(value: Optional[str], default_year: Optional[int] = None) -> Optional[str]:
    """Приводит дату из карточки/страницы трипа к ISO YYYY-MM-DD."""
    if not value:
        return None
    s = str(value).strip()
    if not s:
        return None
    if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        return s
    # 19.11.2026 / 19/11/2026
    m = re.match(r"^(\d{1,2})[./-](\d{1,2})[./-](\d{4})$", s)
    if m:
        return _safe_iso(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    # Nov 19, 2026 / Nov 19
    m = re.match(r"^([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:,\s*(\d{4}))?$", s)
    if m:
        mon = _month_num(m.group(1))
        year = int(m.group(3)) if m.group(3) else (default_year or datetime.now().year)
        if mon:
            return _safe_iso(year, mon, int(m.group(2)))
    # 19 Nov 2026 / 19 Nov
    m = re.match(r"^(\d{1,2})\s+([A-Za-z]{3,9})\.?(?:,?\s*(\d{4}))?$", s)
    if m:
        mon = _month_num(m.group(2))
        year = int(m.group(3)) if m.group(3) else (default_year or datetime.now().year)
        if mon:
            return _safe_iso(year, mon, int(m.group(1)))
    return None


def parse_trip_date_range(text: str, default_year: Optional[int] = None):
    """
    Извлекает (arrival_iso, departure_iso) из текста карточки трипа.
    Поддерживает форматы:
      "Nov 16 – 19, 2026"
      "Nov 30 – Dec 5, 2026"
      "Dec 30, 2026 – Jan 2, 2027"
      "2026-11-16 – 2026-11-19"
    """
    if not text:
        return None, None
    default_year = default_year or datetime.now().year

    # 1) Dec 30, 2026 – Jan 2, 2027
    m = re.search(
        r"([A-Za-z]{3,9})\.?\s+(\d{1,2}),\s*(\d{4})\s*[–—\-]\s*([A-Za-z]{3,9})\.?\s+(\d{1,2}),\s*(\d{4})",
        text,
    )
    if m:
        m1, m2 = _month_num(m.group(1)), _month_num(m.group(4))
        if m1 and m2:
            return (
                _safe_iso(int(m.group(3)), m1, int(m.group(2))),
                _safe_iso(int(m.group(6)), m2, int(m.group(5))),
            )

    # 2) Nov 30 – Dec 5, 2026
    m = re.search(
        r"([A-Za-z]{3,9})\.?\s+(\d{1,2})\s*[–—\-]\s*([A-Za-z]{3,9})\.?\s+(\d{1,2}),\s*(\d{4})",
        text,
    )
    if m:
        m1, m2 = _month_num(m.group(1)), _month_num(m.group(3))
        year = int(m.group(5))
        if m1 and m2:
            end_year = year + 1 if m2 < m1 else year
            return (
                _safe_iso(year, m1, int(m.group(2))),
                _safe_iso(end_year, m2, int(m.group(4))),
            )

    # 3) Nov 16 – 19, 2026
    m = re.search(
        r"([A-Za-z]{3,9})\.?\s+(\d{1,2})\s*[–—\-]\s*(\d{1,2}),\s*(\d{4})",
        text,
    )
    if m:
        mon = _month_num(m.group(1))
        if mon:
            year = int(m.group(4))
            return (
                _safe_iso(year, mon, int(m.group(2))),
                _safe_iso(year, mon, int(m.group(3))),
            )

    # 4) ISO диапазон
    m = re.search(
        r"(\d{4}-\d{2}-\d{2})\s*[–—\-]\s*(\d{4}-\d{2}-\d{2})",
        text,
    )
    if m:
        return m.group(1), m.group(2)

    return None, None


def extract_iso_dates_from_html(html: str):
    """Ищет точные даты трипа в HTML: startDate=..., endDate=..., <time datetime>."""
    if not html:
        return None, None

    def find(key: str) -> Optional[str]:
        for pat in (
            rf"{key}=(\d{{4}}-\d{{2}}-\d{{2}})",
            rf'"{key}"\s*:\s*"(\d{{4}}-\d{{2}}-\d{{2}})',
            rf"{key}\s*:\s*'(\d{{4}}-\d{{2}}-\d{{2}})'",
        ):
            m = re.search(pat, html)
            if m:
                return m.group(1)
        return None

    start = find("startDate")
    end = find("endDate")
    if start and end:
        return start, end

    times = re.findall(r'<time[^>]+datetime="(\d{4}-\d{2}-\d{2})', html)
    if len(times) >= 2:
        return times[0], times[1]
    if len(times) == 1:
        return times[0], end or start
    return start, end


def city_from_trip_url(url: str) -> Optional[str]:
    """Извлекает город из слага /c/trips/<city>-trip-<hash>."""
    if not url:
        return None
    m = re.search(r"/c/trips/([^/?#]+)", url)
    if not m:
        return None
    slug = m.group(1)
    if "-trip-" in slug:
        base = slug.split("-trip-")[0]
    elif re.match(r"^[0-9a-f]{8}-", slug):
        return None
    else:
        base = slug.split("-")[0]
    base = base.replace("-", " ").strip()
    if not base or base.lower() in ("trip", "my", "the", "c", "untitled"):
        return None
    return base.title()


def city_from_card_text(text: str) -> Optional[str]:
    """'Kyoto Trip\nNov 19 – 21, 2026' -> 'Kyoto'."""
    if not text:
        return None
    line = text.strip().splitlines()[0].strip()
    if not line:
        return None
    if "," in line:
        line = line.split(",")[0].strip()
    line = re.sub(r"\s+[Tt]rip$", "", line).strip()
    line = re.sub(r"\s+Japan$", "", line, flags=re.I).strip()
    if not line:
        return None
    return line


def city_from_location_text(text: str) -> Optional[str]:
    """'Kyoto, Kyoto Prefecture, Japan' -> 'Kyoto'."""
    if not text:
        return None
    m = re.search(
        r"([A-Z][A-Za-z'’\-]+(?:\s+[A-Z][A-Za-z'’\-]+)*)\s*,\s*[^\n,]{1,60},\s*(?:Japan|JAPAN)",
        text,
    )
    if m:
        return m.group(1).strip()
    return None


def collect_upcoming_trip_cards(page) -> List[Dict[str, str]]:
    """
    Возвращает карточки ТОЛЬКО из секции 'Upcoming Trips'.
    Каждая карточка: href, cardText (город + даты), cardHtml.
    Если секцию найти не удалось — возвращает все ссылки на трипы (fallback).
    """
    try:
        cards = page.evaluate(
            """(sectionTitles) => {
                const titles = sectionTitles || [];
                const headings = [...document.querySelectorAll('h1,h2,h3,h4,h5,h6')]
                    .filter(e => titles.includes((e.innerText || '').trim()));
                let scope = null;
                for (const heading of headings) {
                    let el = heading;
                    for (let i = 0; i < 10 && el; i++) {
                        const anchors = el.querySelectorAll('a[href*="/c/trips/"]');
                        if (anchors.length > 0) {
                            scope = el;
                            break;
                        }
                        el = el.parentElement;
                    }
                    if (scope) break;
                }
                const anchors = scope
                    ? [...scope.querySelectorAll('a[href*="/c/trips/"]')]
                    : [...document.querySelectorAll('a[href*="/c/trips/"]')];
                const seen = new Set();
                const out = [];
                for (const a of anchors) {
                    const href = a.getAttribute('href') || '';
                    if (!href || seen.has(href)) continue;
                    seen.add(href);
                    let card = a;
                    for (let i = 0; i < 10 && card; i++) {
                        if ((card.innerText || '').trim().length > 3) break;
                        card = card.parentElement;
                    }
                    out.push({
                        href: href,
                        cardText: (card ? card.innerText : '').trim(),
                        cardHtml: (card ? card.outerHTML : '').slice(0, 5000),
                    });
                }
                return out;
            }""",
            sorted(TRIP_SECTION_TITLES),
        )
        return cards or []
    except Exception as e:
        print(f"[!] Не удалось собрать карточки трипов: {e}")
        return []


def build_trip_record(href: str, card_text: str, card_html: str = "") -> Dict[str, Any]:
    """Собирает запись трипа: URL, город, ISO-даты, текст дат."""
    if href.startswith("/"):
        href = "https://www.couchsurfing.com" + href

    city = city_from_trip_url(href) or city_from_card_text(card_text)

    arrival, departure = parse_trip_date_range(card_text)
    if not arrival or not departure:
        a2, d2 = extract_iso_dates_from_html(card_html)
        arrival = arrival or a2
        departure = departure or d2

    dates_text = ""
    m = re.search(r"[A-Za-z]{3,9}\.?\s+\d{1,2}\s*[–—\-]\s*[^\n]{1,20}\d{4}", card_text or "")
    if m:
        dates_text = m.group(0).strip()

    return {
        "url": href,
        "city": city or "Unknown",
        "arrival_date": arrival,
        "departure_date": departure,
        "dates_text": dates_text,
        "raw_text": (card_text or "")[:200],
    }


def trip_is_upcoming(trip: Dict[str, Any]) -> bool:
    dep = trip.get("departure_date")
    if not dep:
        return True
    try:
        return dep >= date.today().isoformat()
    except Exception:
        return True


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
        Открывает /dashboard и проверяет, нет ли редиректа на страницу входа.
        """
        print("[*] Проверка валидности сессии авторизации...")
        try:
            page.goto("https://www.couchsurfing.com/dashboard", timeout=25000, wait_until="domcontentloaded")
            page.wait_for_timeout(2500)
            curr_url = page.url.lower()
            page_title = (page.title() or "").lower()

            is_redirect_to_login = (
                "/users/sign_in" in curr_url
                or "/login" in curr_url
                or "/session/new" in curr_url
                or "sign in" in page_title
                or "log in" in page_title
            )

            if is_redirect_to_login:
                print("[ERROR] Сессия в auth_state.json недействительна или истекла!")
                print("        Запустите: python3 auth.py (ручной вход в браузере)")
                print("        Или обновите куки через веб-интерфейс: POST /api/set_cookie")
                debug_dir = OUTPUT_DIR / "debug"
                debug_dir.mkdir(parents=True, exist_ok=True)
                try:
                    page.screenshot(path=str(debug_dir / "auth_invalid.png"))
                    print(f"[+] Диагностический скриншот: {debug_dir / 'auth_invalid.png'}")
                except Exception:
                    pass
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

        # Быстрый маппинг для популярных городов (известные location-хэши Couchsurfing)
        # Оставляем только верифицированный ID Токио. Остальные города resolvingятся
        # динамически через /c/locations/<город> и /c/explore, чтобы не подменять
        # чужие города токийским хэшем и не возвращать 0 результатов.
        known_cities = {
            "tokyo": "2pdyyI6P65u26SGYJJtEbWWXjeT_lc",
        }
        if city_lower in known_cities:
            loc_id = known_cities[city_lower]
            print(f"[+] Найден locationId: {loc_id} (город: {self.city})")
            return loc_id

        # Динамический поиск через /c/locations/<city>-japan
        try:
            loc_url = f"https://www.couchsurfing.com/c/locations/{city_lower}-japan"
            page.goto(loc_url, timeout=20000)
            page.wait_for_timeout(2500)
            html = page.content()
            # Ищем location-хэш в ссылках на поиск хостов и в JSON-данных страницы
            found_hashes = []
            for part in html.split('"'):
                if part.endswith("_lc") and len(part) > 15:
                    found_hashes.append(part)
            # Также ищем в шаблонах /c/search/hosts/<hash>
            for m in re.finditer(r'/c/search/hosts/([A-Za-z0-9_\-]+_lc)', html):
                found_hashes.append(m.group(1))
            # Убираем дубли, берем первый не-Токийский хэш (чтобы не попасться на Токио при поиске Киото)
            seen = set()
            for h in found_hashes:
                if h not in seen:
                    seen.add(h)
                    if h != "2pdyyI6P65u26SGYJJtEbWWXjeT_lc":
                        print(f"[+] Динамически обнаружен locationId: {h} (город: {self.city})")
                        return h
            # Если нашли только Токийский хэш и город не Токио — не используем его
            if seen and city_lower != "tokyo":
                print(f"[!] На странице города {self.city} найден только хэш Токио, пропускаем.")
        except Exception as e:
            print(f"[!] Ошибка резолвинга через locations: {e}")

        # Динамический поиск через страницу /c/explore с полем ввода города
        try:
            print(f"[*] Пробуем динамический поиск через /c/explore для города: {self.city}...")
            page.goto("https://www.couchsurfing.com/c/explore", timeout=20000)
            page.wait_for_timeout(3000)
            # Пытаемся найти и заполнить поле поиска города
            search_input = page.locator("input[placeholder*='city' i], input[placeholder*='город' i], input[name*='city' i], input[aria-label*='city' i]").first
            if search_input.count() > 0 and search_input.is_visible():
                search_input.click()
                search_input.fill(self.city)
                page.wait_for_timeout(2000)
                # Ищем хэш в сетевых ответах или обновившейся странице
                html = page.content()
                for m in re.finditer(r'/c/search/hosts/([A-Za-z0-9_\-]+_lc)', html):
                    h = m.group(1)
                    if h != "2pdyyI6P65u26SGYJJtEbWWXjeT_lc":
                        print(f"[+] Динамически обнаружен locationId через /c/explore: {h} (город: {self.city})")
                        return h
        except Exception as e:
            print(f"[!] Ошибка поиска через /c/explore: {e}")

        # Fallback: пробуем поиск по имени города без locationId (если CS поддерживает query-поиск)
        try:
            search_url = f"https://www.couchsurfing.com/c/search/hosts?query={city_lower}"
            print(f"[*] Fallback: поиск по имени города через URL: {search_url}")
            # Возвращаем маркер, что используется поиск по имени (заменит locationId в URL)
            return f"query:{city_lower}"
        except Exception as e:
            print(f"[!] Ошибка формирования fallback-URL: {e}")

        # Последняя instance: если город не Токио, не подменяем на Токийский хэш — возвращаем сам город как идентификатор
        print(f"[!] Не удалось определить locationId для '{self.city}'. Используем имя города как идентификатор.")
        return city_lower

    def _extract_city_from_trip_page(self, page, page_text: str, trip_url: str) -> Optional[str]:
        """Извлекает чистый город из заголовка трипа ('Kyoto, Kyoto Prefecture, Japan' -> 'Kyoto')."""
        for sel in ("h1", "h2", "h3", "h4"):
            try:
                elements = page.locator(sel).all()
            except Exception:
                continue
            for el in elements:
                try:
                    text = (el.inner_text() or "").strip()
                except Exception:
                    continue
                if not text or len(text) > 140:
                    continue
                if "," in text and ("Japan" in text or "Prefecture" in text):
                    city = city_from_location_text(text) or city_from_card_text(text)
                    if city:
                        print(f"[+] Город из {sel} трипа: {city}")
                        return city

        city = city_from_location_text(page_text)
        if city:
            print(f"[+] Город из текста трипа: {city}")
            return city

        city = city_from_trip_url(trip_url)
        if city:
            print(f"[+] Город из слага трипа: {city}")
        return city

    def _click_find_hosts(self, page) -> Optional[str]:
        """Кликает 'Find Hosts' на странице трипа и возвращает URL списка хостов."""
        find_hosts_selectors = [
            "button:has-text('Find Hosts')",
            "button:has-text('Search Hosts')",
            "a:has-text('Find Hosts')",
            "a:has-text('Search Hosts')",
            "button:has-text('Найти хостов')",
            "a:has-text('Найти хостов')",
            "[data-testid*='find-hosts']",
            "[data-testid*='search-hosts']",
        ]
        for sel in find_hosts_selectors:
            try:
                btn = page.locator(sel).first
                if btn.count() == 0 or not btn.is_visible():
                    continue
                print(f"[+] Найдена кнопка 'Find Hosts': {sel}")
                btn.click()
                for _ in range(8):
                    page.wait_for_timeout(1000)
                    url = page.url
                    if "/c/search/hosts/" in url or "/c/search/hosts?" in url:
                        print(f"[*] URL хостов после клика: {url}")
                        return url
                print(f"[!] Клик по '{sel}' не привёл к /c/search/hosts (URL: {page.url})")
            except Exception as e:
                print(f"[!] Не удалось кликнуть {sel}: {e}")
        return None

    def _open_trip_url(self, page, trip_url: str) -> Optional[Dict[str, Any]]:
        """
        Открывает прямую ссылку на трип, извлекает город и ТОЧНЫЕ даты
        (startDate/endDate из HTML страницы трипа), затем нажимает 'Find Hosts'.

        Важно: здесь НЕ используются захардкоженные locationId.
        """
        print(f"[*] Открытие трипа: {trip_url}")
        arrival_date = normalize_trip_date(self.arrival_date)
        departure_date = normalize_trip_date(self.departure_date)

        try:
            page.goto(trip_url, timeout=30000, wait_until="domcontentloaded")
            page.wait_for_timeout(3500)
            self._dismiss_popups(page)

            try:
                html = page.content()
            except Exception:
                html = ""
            try:
                page_text = page.evaluate("() => document.body.innerText") or ""
            except Exception:
                page_text = ""

            city_name = self._extract_city_from_trip_page(page, page_text, trip_url)

            # 1) Самый надёжный источник дат — startDate/endDate в HTML трипа
            iso_in, iso_out = extract_iso_dates_from_html(html)
            # 2) Fallback — текст страницы ("Nov 19 – 21")
            text_in, text_out = parse_trip_date_range(page_text or "")
            found_in = iso_in or text_in
            found_out = iso_out or text_out
            if found_in:
                arrival_date = found_in
            if found_out:
                departure_date = found_out
            if arrival_date and departure_date:
                print(f"[+] Реальные даты трипа: {arrival_date} – {departure_date}")

            hosts_url = self._click_find_hosts(page)
            if not hosts_url and city_name:
                print(
                    f"[!] 'Find Hosts' не сработала — fallback на поиск по имени города "
                    f"'{city_name}' (без locationId)"
                )
                hosts_url = f"https://www.couchsurfing.com/c/search/hosts?query={quote(city_name)}"

            if not hosts_url:
                print("[!] Не удалось получить URL хостов из трипа")
                return None

            return {
                "hosts_url": hosts_url,
                "city_name": city_name,
                "arrival_date": arrival_date,
                "departure_date": departure_date,
            }
        except Exception as e:
            print(f"[!] Ошибка при открытии трипа: {e}")
            return None

    def _resolve_trip_link(self, page) -> Optional[Dict[str, Any]]:
        """Прямая ссылка на трип -> hosts_url + чистый город + реальные даты."""
        return self._open_trip_url(page, self.city.strip())

    def _navigate_to_trips_and_select_city(
        self, page, city_name: str
    ) -> Optional[Dict[str, Any]]:
        """
        Открывает /c/trips и находит карточку города ТОЛЬКО в секции
        'Upcoming Trips', затем переходит по её прямой ссылке.
        """
        target = (city_name or "").strip()
        print(f"[*] Поиск upcoming-трипа для города '{target}' на /c/trips...")

        try:
            page.goto(
                "https://www.couchsurfing.com/c/trips",
                timeout=30000,
                wait_until="domcontentloaded",
            )
            page.wait_for_timeout(4000)
            self._dismiss_popups(page)

            cards = collect_upcoming_trip_cards(page)
            print(f"[*] Карточек в 'Upcoming Trips': {len(cards)}")

            chosen = None
            for card in cards:
                trip = build_trip_record(
                    card.get("href", ""),
                    card.get("cardText", ""),
                    card.get("cardHtml", ""),
                )
                if not trip_is_upcoming(trip):
                    continue
                card_city = (trip.get("city") or "").lower()
                if card_city == target.lower() or target.lower() in (trip.get("raw_text") or "").lower():
                    chosen = trip
                    print(
                        f"[+] Выбрана карточка: {trip.get('city')} | "
                        f"{trip.get('arrival_date')} – {trip.get('departure_date')} | {trip.get('url')}"
                    )
                    break

            if not chosen:
                print(f"[!] Upcoming-трип для '{target}' не найден. Доступные карточки:")
                for card in cards:
                    trip = build_trip_record(
                        card.get("href", ""),
                        card.get("cardText", ""),
                        card.get("cardHtml", ""),
                    )
                    print(
                        f"    [DIAG] {trip.get('city')} | "
                        f"{trip.get('arrival_date')} – {trip.get('departure_date')}"
                    )
                return None

            # Передаём даты из карточки, чтобы они не потерялись
            if chosen.get("arrival_date"):
                self.arrival_date = chosen["arrival_date"]
            if chosen.get("departure_date"):
                self.departure_date = chosen["departure_date"]

            return self._open_trip_url(page, chosen["url"])
        except Exception as e:
            print(f"[!] Ошибка при навигации по трипам: {e}")
            return None

    def _validate_api_host(self, item: Dict[str, Any]) -> bool:
        """Проверяет, что запись из сетевого ответа выглядит как валидный профиль хоста."""
        username = item.get("handle") or item.get("slug") or item.get("username")
        if not username:
            return False
        if username in ["visits", "stays", "feed", "explore", "search", "users"]:
            return False
        # Технические объекты без имени — не хосты
        if not any(k in item for k in ("displayName", "name", "aboutMe", "about_me", "locationSummary", "domicileLocationDescription", "responseRate")):
            return False
        return True

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

        Устойчивость:
        - Не полагается на content-type (некоторые CDN/прокси отдают JSON без верного заголовка).
        - Пытается распарсить тело как JSON независимо от content-type.
        - Расширен список ключевых слов в URL.
        - Логирует найденные хосты и ошибки парсинга вместо бес silenzio проглатывания.
        """
        url = response.url.lower()

        # Ключевые слова эндпоинтов поиска/хостов
        search_keywords = (
            "searchhosts", "search_hosts", "graphql", "api/web", "hostsearch",
            "search-hosts", "searchhost", "host-search", "users/search",
        )
        if not any(k in url for k in search_keywords):
            return

        # Исключаем явный мусор
        skip_keywords = ("lotties", "mapbox", "analytics", "tracking", "heartbeat", "stats", "static", "assets")
        if any(k in url for k in skip_keywords):
            return

        print(f"[API] Перехвачен ответ: {response.url} (status={response.status})")

        try:
            # Пытаемся распарсить как JSON независимо от content-type
            try:
                data = response.json()
            except Exception:
                # Попробуем прочитать текст и распарсить вручную
                try:
                    body = response.body()
                    import json as _json
                    data = _json.loads(body)
                except Exception:
                    print(f"[API] Не удалось распарсить JSON из {response.url}")
                    return

            items: List[Dict[str, Any]] = []

            def _extract_list_from_value(val: Any) -> List[Dict[str, Any]]:
                """Рекурсивно ищет списки объектов хостов в произвольной JSON-структуре."""
                found: List[Dict[str, Any]] = []
                if isinstance(val, list):
                    for item in val:
                        if isinstance(item, dict):
                            # Вероятно, это уже хост (есть handle/username/slug)
                            if any(k in item for k in ("handle", "username", "slug", "displayName", "id")):
                                found.append(item)
                            found.extend(_extract_list_from_value(item))
                        elif isinstance(item, list):
                            found.extend(_extract_list_from_value(item))
                elif isinstance(val, dict):
                    for k, v in val.items():
                        if k in ("hosts", "searchHosts", "users", "items", "results", "data", "edges", "nodes"):
                            if isinstance(v, list):
                                for item in v:
                                    if isinstance(item, dict):
                                        found.append(item)
                            found.extend(_extract_list_from_value(v))
                        else:
                            found.extend(_extract_list_from_value(v))
                return found

            # tRPC batch ответ (список объектов с result -> data -> json -> data)
            if isinstance(data, list):
                for entry in data:
                    items.extend(_extract_list_from_value(entry))

            # Одиночный объект (GraphQL / REST / tRPC)
            elif isinstance(data, dict):
                items.extend(_extract_list_from_value(data))

            if items:
                prev_len = len(self.collected_hosts)
                valid_count = 0
                invalid_count = 0
                for it in items:
                    if not isinstance(it, dict):
                        invalid_count += 1
                        continue
                    if not self._validate_api_host(it):
                        invalid_count += 1
                        continue
                    self._process_host_from_api(it)
                    valid_count += 1
                added = len(self.collected_hosts) - prev_len
                if added > 0:
                    print(f"[API] Перехвачено {added} новых хостов через сетевой ответ (Всего: {len(self.collected_hosts)})")
                elif len(items) > 0:
                    print(f"[API] Сетевой ответ содержит {len(items)} записей: валидных={valid_count}, невалидных={invalid_count}, новых={added}")
            else:
                print(f"[API] В ответе {response.url} не найдено записей хостов.")

        except Exception as e:
            print(f"[!] Ошибка обработки сетевого ответа {response.url}: {e}")

    def _extract_cards_from_page(self, page) -> None:
        """Парсит карточки хостов из DOM страницы поиска (fallback при отсутствии API-antwortов)."""
        user_selectors = [
            'a[href*="/c/users/"]',
            'a[href*="/users/"]',
            'a[href*="/people/"]',
            'a[href*="/member/"]',
        ]
        user_anchors = []
        for sel in user_selectors:
            try:
                found = page.query_selector_all(sel)
                for a in found:
                    if a not in user_anchors:
                        user_anchors.append(a)
            except Exception:
                continue

        for a in user_anchors:
            try:
                href = a.get_attribute("href") or ""
                user_match = re.search(r"/(?:c/)?(?:users|people|members)/([^/?#]+)", href)
                if not user_match:
                    continue
                username = user_match.group(1)
                if username in ["visits", "stays", "feed", "explore", "search", "users", "members", "people"] or username in self.collected_hosts:
                    continue

                parent_text = ""
                # Пробуем несколько стратегий поиска контейнера карточки
                card_strategies = [
                    "el => el.closest('[class*=card], [data-testid], article, li, div[class*=\"host\"], div[class*=\"user\"], div[class*=\"profile\"]')",
                    "el => el.closest('div')",
                    "el => el.parentElement",
                ]
                for strategy in card_strategies:
                    try:
                        card_el = a.evaluate_handle(strategy)
                        if card_el:
                            el = card_el.as_element()
                            if el:
                                t = el.inner_text()
                                if t and len(t.strip()) > len(parent_text.strip()):
                                    parent_text = t
                    except Exception:
                        continue

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

                # Извлечение отзывов — ищем по всему тексту карточки
                ref_match = re.search(r"(\d+)\s*References", meta_line, re.I) or re.search(r"(\d+)\s*References", parent_text, re.I)
                references_count = int(ref_match.group(1)) if ref_match else 0

                # Локация — ищем "Lives in" или "Based in"
                loc_match = (
                    re.search(r"Lives in\s*([^\n\r]+)", meta_line, re.I)
                    or re.search(r"Lives in\s*([^\n\r]+)", parent_text, re.I)
                    or re.search(r"Based in\s*([^\n\r]+)", parent_text, re.I)
                    or re.search(r"Home:\s*([^\n\r]+)", parent_text, re.I)
                )
                raw_loc = loc_match.group(1).strip() if loc_match else self.city
                location = sanitize_host_location(raw_loc, self.city)

                # Проверка активности — ищем маркеры активности в любом месте текста
                is_recently_active = any("Recently Active" in l or "Active" in l for l in lines) or any(
                    k in parent_text.lower() for k in ("recently active", "active recently", "active")
                )
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
        if self.arrival_date or self.departure_date:
            print(f"[*] Даты поездки: {self.arrival_date} – {self.departure_date}")
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
                print(f"[ERROR] Файл сессии {AUTH_STATE_FILE.name} не найден!")
                print(f"       Сначала выполните вход: python3 auth.py")
                print(f"       Или сохраните куки вручную через веб-интерфейс (/api/set_cookie).")
                context.close()
                browser.close()
                return []

            context = browser.new_context(**context_kwargs)
            page = context.new_page()

            # Проверка наличия сессионных cookies в контексте
            context_cookies = context.cookies()
            has_session_cookie = any(
                "session" in c.get("name", "").lower() or "couchsurfing" in c.get("domain", "").lower()
                for c in context_cookies
            )
            if not has_session_cookie:
                print(f"[ERROR] В auth_state.json нет сессионных cookies Couchsurfing!")
                print(f"       Файл существует, но сессия пуста или истекла.")
                print(f"       Сначала выполните вход: python3 auth.py")
                context.close()
                browser.close()
                return []
            print(f"[*] Найдено сессионных cookies: {len(context_cookies)}")

            # Регистрация перехвата сетевых API-ответов
            page.on("response", self._handle_search_response)

            # 1. Пре-чек авторизации
            if not self._check_auth_validity(page):
                context.close()
                browser.close()
                return []

            # 1.5. Определяем URL страницы хостов: либо по прямой ссылке на трип,
            # либо через выбор города на странице /c/trips, либо через locationId.
            trip_hosts_url = None
            city_lower = self.city.strip().lower()

            is_direct_trip_url = (
                self.city.strip().startswith("http")
                or "/c/trips/" in self.city
                or "couchsurfing.com/c/trips" in self.city
            )

            if is_direct_trip_url:
                # Прямая ссылка на трип: переходим по ней и ищем кнопку "Find Hosts"
                trip_result = self._resolve_trip_link(page)
                if trip_result:
                    trip_hosts_url = trip_result.get("hosts_url")
                    if trip_result.get("city_name"):
                        self.city = trip_result["city_name"]
                    if trip_result.get("arrival_date"):
                        self.arrival_date = trip_result["arrival_date"]
                    if trip_result.get("departure_date"):
                        self.departure_date = trip_result["departure_date"]
                    print(
                        f"[*] Обновлённые параметры из трипа: город={self.city}, даты={self.arrival_date} – {self.departure_date}"
                    )
                else:
                    print(
                        "[!] Не удалось обработать ссылку на трип, продолжаем с исходными параметрами."
                    )
            else:
                # Обычное название города (Kyoto, Tokyo, Sapporo...) — открываем /c/trips
                # и выбираем карточку этого города, т.к. захардкоженные locationId_hash
                # ведут на 0 результатов.
                print(
                    f"[*] Город '{self.city}' — открываем /c/trips для выбора карточки города..."
                )
                trip_result = self._navigate_to_trips_and_select_city(page, self.city.strip())
                if trip_result:
                    trip_hosts_url = trip_result.get("hosts_url")
                    if trip_result.get("city_name"):
                        self.city = trip_result["city_name"]
                    if trip_result.get("arrival_date"):
                        self.arrival_date = trip_result["arrival_date"]
                    if trip_result.get("departure_date"):
                        self.departure_date = trip_result["departure_date"]
                    print(
                        f"[*] Параметры из трипа: город={self.city}, даты={self.arrival_date} – {self.departure_date}"
                    )
                else:
                    print(
                        "[!] Не удалось выбрать трип через /c/trips. "
                        "Используем поиск по имени города (без locationId)."
                    )

            # 2. Строим URL поиска хостов.
            # Захардкоженные locationId больше НЕ используем: для трипов берём
            # URL, полученный после клика 'Find Hosts', а в крайнем случае —
            # поиск по имени города.
            if trip_hosts_url:
                search_url = trip_hosts_url
                print(f"[*] Переход на страницу хостов из трипа: {search_url}")
            else:
                fallback_city = city_from_trip_url(self.city) or self.city.strip()
                search_url = f"https://www.couchsurfing.com/c/search/hosts?query={quote(fallback_city)}"
                print(f"[*] Fallback-поиск хостов по городу: {search_url}")

            try:
                page.goto(search_url, timeout=30000, wait_until="domcontentloaded")
            except Exception as e:
                print(f"[!] Примечание при загрузке: {e}")

            page.wait_for_timeout(3500)
            self._dismiss_popups(page)

            # Проверка редиректа на авторизацию после перехода на поиск
            if "/users/sign_in" in page.url or "/login" in page.url:
                print("[ERROR] Сессия в auth_state.json недействительна или истекла!")
                print("        Запустите: python3 auth.py (ручной вход в браузере)")
                print("        Или обновите куки через веб-интерфейс: POST /api/set_cookie")
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
            try:
                page.screenshot(path=str(zero_screenshot))
                screenshot_ok = True
            except Exception:
                screenshot_ok = False

            if len(self.collected_hosts) == 0:
                print(f"[DIAGNOSTIC] Предупреждение: найдено 0 карточек хостов после загрузки!")
                try:
                    print(f"[DIAGNOSTIC] Текущий page.url: {page.url}")
                except Exception:
                    print(f"[DIAGNOSTIC] Текущий page.url: <страница закрыта>")
                try:
                    print(f"[DIAGNOSTIC] Заголовок страницы (page.title): {page.title()}")
                except Exception:
                    print(f"[DIAGNOSTIC] Заголовок страницы: <страница закрыта>")
                if screenshot_ok:
                    print(f"[DIAGNOSTIC] Диагностический скриншот сохранен: {zero_screenshot.resolve()}")
                else:
                    print(f"[DIAGNOSTIC] Не удалось сделать скриншот (страница закрыта)")
                print(f"[!] Парсинг остановлен: на странице поиска хостов нет карточек (вероятно, неверный город или ссылка на трип).")
                context.close()
                browser.close()
                return self._filter_and_save()
            else:
                print(f"[+] Успешно обнаружены карточки хостов (API/DOM): {len(self.collected_hosts)}")
                print(f"[+] Скриншот страницы поиска сохранен: {zero_screenshot.resolve()}")

            print("[*] Начинаем сбор и пагинацию виртуализированного списка...")

            for page_idx in range(1, self.max_scroll_pages + 1):
                # Дополнительно извлекаем из DOM
                self._extract_cards_from_page(page)
                print(f"[*] Шаг #{page_idx}/{self.max_scroll_pages} | Найдено хостов (API + DOM): {len(self.collected_hosts)}")
                # Сохраняем live-промежуток для отображения в UI в реальном времени
                self._save_live_hosts()

                if page_idx == self.max_scroll_pages:
                    break

                # Скроллим внутренний scrollable-контейнер списка хостов
                try:
                    page.evaluate("""() => {
                        const strategies = [
                            () => {
                                const divs = Array.from(document.querySelectorAll('div'));
                                return divs.find(d => d.scrollHeight > d.clientHeight && d.clientHeight > 300);
                            },
                            () => document.querySelector('[class*="scroll"], [class*="virtual"], [class*="list"], [class*="results"], [data-testid*="list"]'),
                            () => document.querySelector('main') || document.querySelector('[role="main"]') || document.body,
                        ];
                        let scrollable = null;
                        for (const strat of strategies) {
                            try { scrollable = strat(); } catch (e) {}
                            if (scrollable) break;
                        }
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

                # Проверка сессии в процессе скроллинга (сессия может истечь по ходу работы)
                curr_url = page.url.lower()
                if "/users/sign_in" in curr_url or "/login" in curr_url:
                    print("[!] Сессия истекла в процессе сбора — перенаправление на логин. Остановка.")
                    context.close()
                    browser.close()
                    return self._filter_and_save()

            print(f"[*] Сбор завершен. Всего хостов собрано: {len(self.collected_hosts)}")
            context.close()
            browser.close()

        return self._filter_and_save()

    def _save_live_hosts(self) -> None:
        """Сохраняет текущих собранных хостов в live-файл для отображения в UI в реальном времени."""
        try:
            OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
            all_hosts = list(self.collected_hosts.values())
            slug_city = re.sub(r"\W+", "_", self.city.lower())
            live_path = OUTPUT_DIR / f"hosts_{slug_city}_live.json"
            with open(live_path, "w", encoding="utf-8") as f:
                json.dump(all_hosts, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[!] Не удалось сохранить live-хосты: {e}")

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


    def list_trips(self) -> List[Dict[str, Any]]:
        """
        Открывает https://www.couchsurfing.com/c/trips и возвращает список
        всех карточек трипов пользователя с городом, датами и URL.
        """
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        debug_dir = OUTPUT_DIR / "debug"
        debug_dir.mkdir(parents=True, exist_ok=True)

        trips: List[Dict[str, Any]] = []

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
                context_kwargs["storage_state"] = str(AUTH_STATE_FILE)

            context = browser.new_context(**context_kwargs)
            page = context.new_page()

            try:
                print("[*] Открытие страницы трипов: https://www.couchsurfing.com/c/trips")
                page.goto("https://www.couchsurfing.com/c/trips", timeout=30000, wait_until="domcontentloaded")
                page.wait_for_timeout(4000)
                self._dismiss_popups(page)

                # Сохраняем скриншот для диагностики
                try:
                    page.screenshot(path=str(debug_dir / "trips_list.png"))
                except Exception:
                    pass

                # Берём карточки ТОЛЬКО из секции "Upcoming Trips"
                cards = collect_upcoming_trip_cards(page)
                print(f"[*] Карточек в 'Upcoming Trips': {len(cards)}")

                for card in cards:
                    try:
                        trip = build_trip_record(
                            card.get("href", ""),
                            card.get("cardText", ""),
                            card.get("cardHtml", ""),
                        )
                        if not trip_is_upcoming(trip):
                            print(f"[SKIP] Прошедший трип: {trip['city']} | {trip['departure_date']}")
                            continue
                        trips.append(trip)
                        print(
                            f"[+] Трип: {trip['city']} | {trip['arrival_date']} – {trip['departure_date']} "
                            f"| {trip['url']} | {trip['raw_text'][:70]!r}"
                        )
                    except Exception as e:
                        print(f"[!] Ошибка обработки карточки трипа: {e}")
                        continue

            except Exception as e:
                print(f"[!] Ошибка при получении списка трипов: {e}")

            try:
                context.close()
                browser.close()
            except Exception:
                pass

        print(f"[*] Всего трипов найдено: {len(trips)}")
        return trips


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
    parser.add_argument("--list-trips", action="store_true", default=False, help="Вывести список трипов из /c/trips и выйти")
    parser.add_argument("--trip-url", default=None, help="Прямая URL ссылки на трип для парсинга хостов")

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

    if args.list_trips:
        trips = scraper.list_trips()
        # Выводим JSON для использования фронтендом
        print(json.dumps(trips, ensure_ascii=False, indent=2))
        return

    if args.trip_url:
        # Режим парсинга хостов из конкретного трипа
        print(f"[*] Режим парсинга хостов из трипа: {args.trip_url}")
        scraper.city = args.trip_url
        if args.arrival_date:
            scraper.arrival_date = args.arrival_date
        if args.departure_date:
            scraper.departure_date = args.departure_date
        print(f"[*] Даты поездки: {scraper.arrival_date} – {scraper.departure_date}")
        # Вызываем run() - он обнаружит trip URL и перейдет по нему
        scraper.run()
        return

    scraper.run()


if __name__ == "__main__":
    main()
