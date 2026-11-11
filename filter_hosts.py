"""
filter_hosts.py - Модуль глубокой постобработки и фильтрации датасета хостов Couchsurfing.

Возможности:
1. Отсеивание нерелевантных/недоступных хостов по стоп-словам:
   - "not accepting guests", "cannot host", "can't host", "not available hosting",
     "not in japan", "currently in", "working holiday", "traveling around the world",
     "indefinite", "airbnb", "only for one night", "only host 1 person from friday to sunday" и др.
2. Определение пола и типа размещения (Male / Female / Couple / Unknown) на основе
   словарей имен, маркеров текста и анализа совместного размещения.
3. Извлечение параметров "My Home" / "House Rules":
   - sleeping_arrangement (private room, shared room, common area / couch)
   - house_rules
   - secret_code_detected и secret_code_hint (поиск кодовых слов вроде 'holyfalcon', 'include the word', 'keyword', 'read profile')
4. Опциональное live-обогащение через Playwright: посещение страницы /c/users/<username>/home
   с сохраненной сессией auth_state.json для получения официальных правил дома и спального места.
5. Экспорт очищенного датасета в output/hosts_tokyo_curated.json и output/hosts_tokyo_curated.csv.
"""

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from gender_classifier import classify_host
from social_extractor import extract_instagram
from data_cleaner import sanitize_host_name, sanitize_host_clean_name, sanitize_host_location
from database import get_sent_usernames
from hosting_guard import HostingAvailabilityGuard

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BASE_DIR = Path(__file__).parent
OUTPUT_DIR = Path(os.environ.get("CS_OUTPUT_DIR", str(BASE_DIR / "output"))).resolve()
USER_DATA_DIR = BASE_DIR / "user_data"
AUTH_STATE_FILE = USER_DATA_DIR / "auth_state.json"

# Стоп-слова и негативные паттерны недоступности хостов (Hosting Availability Guard)
STOP_PATTERNS = [
    (r"\bdo\s+not\s+send\s+(?:me\s+)?(?:home\s+|stay\s+)?requests?\b", "do not send stay requests"),
    (r"\bnot\s+hosting\b", "not hosting"),
    (r"\bno\s+hosting\b", "no hosting"),
    (r"\bhosting\s+not\s+available\b", "hosting not available"),
    (r"\bnot\s+available\s+hosting\b", "not available hosting"),
    (r"(?:cannot|can't|cant|unable\s+to|won't\s+be\s+able\s+to|impossible\s+to)\s+host", "cannot host"),
    (r"\bnot\s+able\s+to\s+host\b", "not able to host"),
    (r"\bnot\s+accepting\s+guests?\b", "not accepting guests"),
    (r"\bnot\s+accepting\b", "not accepting"),
    (r"\bcan['’]?t\s+take\s+guests\b", "can't take guests"),
    (r"\bcannot\s+accept\s+guests\b", "cannot accept guests"),
    (r"\bstop(?:ped)?\s+hosting\b", "stopped hosting"),
    (r"\bpause(?:d)?\s+hosting\b", "paused hosting"),
    (r"\bnot\s+offering\s+(?:a\s+)?couch\b", "not offering couch"),
    (r"\bno\s+space\s+to\s+(?:welcome|host)\b", "no space to host"),
    (r"\bnot\s+hosting\s+(?:people|guests|anyone)\b", "not hosting people"),
    (r"\bdo\s+not\s+host\b", "do not host"),
    (r"\bdon['’]?t\s+host\b", "don't host"),

    # Ограничения только для гостей-девушек
    (r"\bfemale\s+guests?\s+only\b", "female guests only"),
    (r"\bonly\s+female\b", "only female"),
    (r"\bwomen\s+only\b", "women only"),
    (r"\bonly\s+women\b", "only women"),
    (r"\bgirls?\s+only\b", "girls only"),
    (r"\bonly\s+girls?\b", "only girls"),
    (r"\bhost(?:ing)?\s+(?:only\s+)?female(?:\s+travelers?)?\s+only\b", "female only"),
    (r"\bhost(?:ing)?\s+female\s+travelers?\b", "female travelers only"),
    (r"\bfemale\s+(?:travelers?|couchsurfers?|guests?)\s+only\b", "female guests only"),
    (r"\bonly\s+(?:host|accept)\s+(?:female|women|girls?)\b", "only host female"),

    # Путешествия / отсутствие в городе
    (r"\btrave[l]+ing\s+(?:through|in|around|europe|asia|south\s+korea|america|world|abroad)\b", "traveling abroad"),
    (r"\bwho\s+is\s+trave[l]+ing\b", "who is traveling"),
    (r"\bexploring\s+(?:south\s+korea|europe|asia|vietnam|thailand|world|[a-z\s]+for\s+next)\b", "exploring abroad"),
    (r"not\s+in\s+japan", "not in japan"),
    (r"currently\s+not\s+in\s+japan", "currently not in japan"),
    (r"\bcurrently\s+(?:traveling|travelling|abroad|away|out\s+of\s+town|out\s+of\s+the\s+country)\b", "currently traveling/abroad"),
    (r"\bcurrently\s+on\s+(?:a\s+)?(?:trip|vacation|holiday|tour|sabbatical|journey|the\s+road)\b", "currently on trip/vacation"),
    (r"\bcurrently\s+(?:in|exploring)\s+(?:europe|asia|america|africa|australia|korea|taiwan|thailand|vietnam|bali|singapore|london|paris|usa|canada)\b", "currently traveling abroad"),
    (r"working\s+holiday", "working holiday"),
    (r"trave[l]+ing\s+around\s+the\s+world", "traveling around the world"),
    (r"\bindefinite\b", "indefinite travel"),
    (r"\bairbnb\b", "airbnb"),
    (r"only\s+(?:for\s+)?one\s+night", "only for one night"),
    (r"only\s+host\s+1\s+person\s+from\s+friday\s+to\s+sunday", "restricted days"),
    (r"only\s+host.*weekend", "weekends only"),
    (r"refrain\s+from\s+sending\s+(?:stay\s+)?requests?", "refrain from sending requests"),
    (r"don't\s+offer\s+accommodation", "no accommodation"),
    (r"do\s+not\s+offer\s+accommodation", "no accommodation"),
    (r"living\s+in\s+(?!tokyo|japan|kyoto|osaka)[a-z\s]+\s+now", "living outside tokyo/japan"),

    # Проживание с родителями / семьей / нет места
    (r"(?:live|living|staying)\s+(?:with|at)\s+(?:my\s+)?(?:parents|family)(?:\s+house)?", "living with parents/family"),
    (r"parents['’]?\s+house", "parents house"),
    (r"with\s+my\s+family\s+and\s+(?:can['’]?t\s+host|no\s+space)", "family no space"),
    (r"no\s+space\s+to\s+(?:host|welcome)", "no space to host"),
    (r"not\s+enough\s+space", "not enough space"),
    (r"don['’]?t\s+have\s+(?:a\s+)?(?:place|room|space|bed|couch)", "no space"),
]

# Паттерны поиска кодовых слов / секретных фраз
SECRET_CODE_PATTERNS = [
    r"(?:secret|code|keyword|pass\s*phrase|phrase|magic\s*word)[\s\w]*(?:is|:|\bwrite\b|\binclude\b|\bput\b)\s*['\"]?([A-Za-z0-9_\-]+)['\"]?",
    r"(?:write|include|put|start\s*with)\s+['\"]([A-Za-z0-9_\-\s]{2,30})['\"]",
    r"code\s*word\s*(?:is|:)\s*['\"]?([A-Za-z0-9_\-]+)['\"]?",
    r"keyword\s*(?:is|:)\s*['\"]?([A-Za-z0-9_\-]+)['\"]?",
]


def clean_text(text: Optional[str]) -> str:
    """Очищает текст от лишних пробелов, типографических спецсимволов и переносов."""
    if not text:
        return ""
    normalized = text.replace("’", "'").replace("`", "'").replace("“", '"').replace("”", '"').replace("\xa0", " ")
    return " ".join(normalized.split())


def detect_secret_code(text: str) -> Tuple[bool, Optional[str]]:
    """
    Ищет секретные слова и маркеры внимательного чтения профиля.
    Возвращает (detected: bool, hint: str).
    """
    if not text:
        return False, None
    text_clean = clean_text(text)

    # Поиск конкретного кодового слова
    for pat in SECRET_CODE_PATTERNS:
        m = re.search(pat, text_clean, re.I)
        if m:
            code = m.group(1).strip()
            # Исключаем ложные короткие слова вроде "a", "the", "hello"
            if len(code) >= 2 and code.lower() not in {"the", "a", "my", "your", "an"}:
                return True, code

    # Поиск общего требования о прочтении профиля
    if re.search(r"read\s+(?:my\s+entire|the\s+entire|my\s+whole|all\s+of\s+my)\s+profile", text_clean, re.I):
        return True, "Profile reading required (check full bio)"

    if re.search(r"(?:code\s*word|secret\s*phrase|secret\s*word)", text_clean, re.I):
        return True, "Secret code requested in bio"

    return False, None


def extract_sleeping_arrangement(text: str) -> str:
    """Определяет тип спального места из текста описания или вкладки My Home."""
    t_lower = text.lower()
    if "private room" in t_lower:
        return "Private Room"
    if "shared room" in t_lower:
        return "Shared Room"
    if any(k in t_lower for k in ("common area", "public space", "couch", "futon", "living room", "shared space")):
        return "Common Area / Couch"
    return "Not specified"


def extract_house_rules(text: str) -> str:
    """Извлекает ключевые правила дома (курение, тишина, гости и т.д.)."""
    rules = []
    t_lower = text.lower()

    if "no smoking" in t_lower or "non-smoking" in t_lower:
        rules.append("No smoking")
    if "no pets" in t_lower:
        rules.append("No pets")
    if "quiet" in t_lower or "no party" in t_lower or "respect" in t_lower:
        rules.append("Quiet environment / No parties")
    if "late nights okay" in t_lower:
        rules.append("Late nights okay")
    if "shoes off" in t_lower:
        rules.append("Shoes off inside")

    # Поиск маркеров списка (⚪︎, -, *)
    bullet_rules = re.findall(r"[⚪︎•\-\*]\s*([^\n\r]+)", text)
    if bullet_rules:
        for b in bullet_rules[:3]:
            b_clean = clean_text(b)
            if b_clean and b_clean not in rules and len(b_clean) < 120:
                rules.append(b_clean)

    return "; ".join(rules) if rules else "Standard house rules"


class HostFilter:
    def __init__(
        self,
        input_file: Path = OUTPUT_DIR / "hosts_tokyo.json",
        output_json: Path = OUTPUT_DIR / "hosts_tokyo_curated.json",
        output_csv: Path = OUTPUT_DIR / "hosts_tokyo_curated.csv",
        gender_filter: str = "any",
        allow_couples: bool = True,
        enrich_home: bool = False,
        headless: bool = True,
        max_enrich_hosts: int = 50,
        hide_sent: bool = True,
    ):
        self.input_file = Path(input_file)
        self.output_json = Path(output_json)
        self.output_csv = Path(output_csv)
        self.gender_filter = (gender_filter or "any").strip().lower()
        self.allow_couples = allow_couples
        self.enrich_home = enrich_home
        self.headless = headless
        self.max_enrich_hosts = max_enrich_hosts
        self.hide_sent = hide_sent
        self.hosting_guard = HostingAvailabilityGuard()

    def check_stop_triggers(self, text: str) -> List[str]:
        """Возвращает список сработавших стоп-паттернов."""
        norm_text = clean_text(text).lower()
        triggers = []
        for pat, label in STOP_PATTERNS:
            if re.search(pat, norm_text, re.I):
                triggers.append(label)
        return triggers

    def enrich_via_playwright(self, hosts: List[Dict[str, Any]]) -> None:
        """
        Посещает страницы /c/users/<username>/home для извлечения официального раздела My Home,
        точного типа размещения, правил дома и скрытых кодовых слов.
        """
        if not hosts:
            return

        from playwright.sync_api import sync_playwright

        print(f"[*] Запуск Playwright для детального обогащения {min(len(hosts), self.max_enrich_hosts)} анкет...")
        with sync_playwright() as p:
            launch_args = ["--disable-blink-features=AutomationControlled", "--no-sandbox"]
            try:
                browser = p.chromium.launch(headless=self.headless, args=launch_args)
            except Exception:
                browser = p.chromium.launch(headless=self.headless)

            context_kwargs = {
                "viewport": {"width": 1280, "height": 720},
                "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            }
            if AUTH_STATE_FILE.exists():
                context_kwargs["storage_state"] = str(AUTH_STATE_FILE)

            context = browser.new_context(**context_kwargs)
            page = context.new_page()

            for idx, h in enumerate(hosts[:self.max_enrich_hosts], 1):
                username = h["username"]
                home_url = f"https://www.couchsurfing.com/c/users/{username}/home"
                print(f"[{idx}/{min(len(hosts), self.max_enrich_hosts)}] Обогащение данных хоста: {username} -> /home")

                try:
                    page.goto(home_url, timeout=20000, wait_until="domcontentloaded")
                    page.wait_for_timeout(1800)
                    body_text = page.evaluate("() => document.body.innerText")

                    # Если страница вернула 404 или нет информации о доме
                    if "Page Not Found" in body_text or "Oops" in body_text:
                        continue

                    # Проверка стоп-триггеров в My Home
                    home_triggers = self.check_stop_triggers(body_text)
                    if home_triggers:
                        h["_home_stop_triggers"] = home_triggers

                    # Извлечение спального места
                    sleeping = extract_sleeping_arrangement(body_text)
                    if sleeping != "Not specified":
                        h["sleeping_arrangement"] = sleeping

                    # Извлечение правил дома
                    rules = extract_house_rules(body_text)
                    if rules:
                        h["house_rules"] = rules

                    # Поиск кодового слова в полном тексте My Home
                    has_code, code_hint = detect_secret_code(body_text)
                    if has_code:
                        h["secret_code_detected"] = True
                        h["secret_code_hint"] = code_hint

                    # Поиск Instagram в полном тексте My Home (если не был найден в about_me)
                    if not h.get("instagram_handle"):
                        i_url, i_handle = extract_instagram(body_text)
                        if i_handle:
                            h["instagram_url"] = i_url
                            h["instagram_handle"] = i_handle

                    # Краткое резюме My Home
                    summary_parts = []
                    if h.get("sleeping_arrangement"):
                        summary_parts.append(f"Bed: {h['sleeping_arrangement']}")
                    if h.get("house_rules"):
                        summary_parts.append(f"Rules: {h['house_rules']}")
                    h["my_home_summary"] = " | ".join(summary_parts)

                except Exception as e:
                    print(f"    [!] Ошибка при обращении к /home для {username}: {e}")

                time.sleep(1.0)

            context.close()
            browser.close()

    def run(self) -> List[Dict[str, Any]]:
        if not self.input_file.exists():
            print(f"[!] Файл с сырыми данными не найден: {self.input_file}")
            return []

        print("=" * 60)
        print(f"[*] Глубокая фильтрация датасета хостов")
        print(f"[*] Входной файл: {self.input_file}")
        print(f"[*] Фильтр пола: {self.gender_filter}")
        print(f"[*] Разрешать пары: {self.allow_couples}")
        print(f"[*] Live-обогащение через Playwright: {self.enrich_home}")
        print("=" * 60)

        with open(self.input_file, "r", encoding="utf-8") as f:
            raw_hosts = json.load(f)

        print(f"[*] Всего загружено сырых анкет: {len(raw_hosts)}")

        # Загрузка отправленных запросов для жесткого исключения
        sent_usernames = get_sent_usernames() if self.hide_sent else set()
        if sent_usernames:
            print(f"[*] Загружено отправленных адресатов для фильтрации: {len(sent_usernames)}")

        valid_hosts = []
        excluded_count = 0

        for h in raw_hosts:
            username = str(h.get("username", "")).strip()
            host_id = str(h.get("id", "")).strip()
            name = h.get("name", "")
            about_me = h.get("about_me", "")

            # 0. Исключение ранее отправленных хостов
            if self.hide_sent and ((username and username.lower() in sent_usernames) or (host_id and host_id.lower() in sent_usernames)):
                print(f"[FILTER] Пропуск хоста {username}: запрос уже был отправлен ранее")
                self.hosting_guard.record_rejected(h, "Already sent (sending_history/queue)")
                excluded_count += 1
                continue

            # 1. Проверка стоп-триггеров в about_me
            triggers = self.check_stop_triggers(about_me)
            if triggers:
                excluded_count += 1
                self.hosting_guard.record_rejected(h, f"Stop triggers: {', '.join(triggers)}")
                continue

            # 2. Определение пола и типа размещения
            name = sanitize_host_name(name)
            clean_name = sanitize_host_clean_name(name)
            est_gender, host_type = classify_host(name, about_me, username)

            # 3. Фильтрация по полу
            if self.gender_filter != "any":
                if est_gender.lower() != self.gender_filter:
                    # Если разрешены пары и хост - пара, можем оставить
                    if not (self.allow_couples and host_type == "couple"):
                        continue

            # 4. Фильтрация по типу пары
            if not self.allow_couples and host_type == "couple":
                continue

            # 5. Извлечение информации о жилье, соцсетях и секретных кодах
            sleeping = extract_sleeping_arrangement(about_me)
            rules = extract_house_rules(about_me)
            has_secret, secret_hint = detect_secret_code(about_me)
            insta_url, insta_handle = extract_instagram(about_me)
            clean_loc = sanitize_host_location(h.get("location"), "Tokyo")

            curated_item = {
                "id": username,
                "username": username,
                "profile_url": f"https://www.couchsurfing.com/c/users/{username}",
                "stay_request_url": f"https://www.couchsurfing.com/c/users/{username}/stay-request",
                "clean_name": clean_name or name,
                "name": name,
                "estimated_gender": est_gender,
                "host_type": host_type,
                "location": clean_loc,
                "instagram_handle": insta_handle,
                "instagram_url": insta_url,
                "references_count": h.get("references_count", 0),
                "response_rate_pct": h.get("response_rate_pct", 90.0),
                "about_me_clean": clean_text(about_me),
                "sleeping_arrangement": sleeping,
                "house_rules": rules,
                "my_home_summary": f"Bed: {sleeping} | Rules: {rules}",
                "secret_code_detected": has_secret,
                "secret_code_hint": secret_hint or "",
            }
            valid_hosts.append(curated_item)

        print(f"[*] Отсеяно по стоп-словам: {excluded_count}")
        print(f"[*] Прошло валидацию анкет: {len(valid_hosts)}")

        # Опциональное live-обогащение через Playwright
        if self.enrich_home and valid_hosts:
            self.enrich_via_playwright(valid_hosts)
            # Отсеиваем тех, у кого стоп-триггеры обнаружены в My Home
            valid_hosts = [h for h in valid_hosts if "_home_stop_triggers" not in h]

        # Экспорт в JSON
        self.output_json.parent.mkdir(parents=True, exist_ok=True)
        with open(self.output_json, "w", encoding="utf-8") as f:
            json.dump(valid_hosts, f, ensure_ascii=False, indent=2)
        print(f"[+] Очищенный JSON сохранен: {self.output_json.resolve()}")

        # Экспорт в CSV
        if valid_hosts:
            df = pd.DataFrame(valid_hosts)
            columns_order = [
                "id",
                "username",
                "profile_url",
                "stay_request_url",
                "clean_name",
                "estimated_gender",
                "host_type",
                "location",
                "instagram_handle",
                "instagram_url",
                "references_count",
                "response_rate_pct",
                "about_me_clean",
                "sleeping_arrangement",
                "house_rules",
                "my_home_summary",
                "secret_code_detected",
                "secret_code_hint",
            ]
            actual_cols = [c for c in columns_order if c in df.columns] + [
                c for c in df.columns if c not in columns_order
            ]
            df = df[actual_cols]
            df.to_csv(self.output_csv, index=False, encoding="utf-8-sig")
            print(f"[+] Очищенный CSV сохранен: {self.output_csv.resolve()}")

        return valid_hosts


def main():
    parser = argparse.ArgumentParser(description="Глубокая фильтрация и обогащение датасета хостов Couchsurfing")
    parser.add_argument("--input", default=str(OUTPUT_DIR / "hosts_tokyo.json"), help="Входной JSON файл")
    parser.add_argument("--output-json", default=str(OUTPUT_DIR / "hosts_tokyo_curated.json"), help="Выходной JSON")
    parser.add_argument("--output-csv", default=str(OUTPUT_DIR / "hosts_tokyo_curated.csv"), help="Выходной CSV")
    parser.add_argument("--gender", default="any", help="Фильтр по полу: female, male, any")
    parser.add_argument("--allow-couples", action="store_true", default=True, help="Разрешать пары")
    parser.add_argument("--no-couples", dest="allow_couples", action="store_false", help="Исключать пары")
    parser.add_argument("--enrich-home", action="store_true", help="Обогатить данными My Home через Playwright")
    parser.add_argument("--headless", action="store_true", default=True, help="Headless режим для Playwright")
    parser.add_argument("--hide-sent", action="store_true", default=True, help="Исключать тех, кому уже отправлен запрос")
    parser.add_argument("--no-hide-sent", dest="hide_sent", action="store_false", help="Не исключать отправленных")
    parser.add_argument("--limit", type=int, default=50, help="Лимит хостов для live-обогащения")

    args = parser.parse_args()

    filter_module = HostFilter(
        input_file=Path(args.input),
        output_json=Path(args.output_json),
        output_csv=Path(args.output_csv),
        gender_filter=args.gender,
        allow_couples=args.allow_couples,
        enrich_home=args.enrich_home,
        headless=args.headless,
        max_enrich_hosts=args.limit,
        hide_sent=args.hide_sent,
    )
    filter_module.run()


if __name__ == "__main__":
    main()
