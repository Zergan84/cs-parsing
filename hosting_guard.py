"""
hosting_guard.py - Жесткая фильтрация доступности хостов (Hosting Availability Guard).

Исключает анкеты пользователей, которые:
1. Прямо пишут о невозможности принять гостей ("not hosting", "cannot host", "impossible to host").
2. Живут с родителями ("live with my parents", "staying with parents", "live my parents house").
3. Ищут только встречи/кофе/прогулки ("only coffee", "only meet", "just want to meet", "show you around only").
4. Не имеют места ("no space to host", "not enough space", "don't have a place").

Работает в два этапа:
- ЭТАП А: Детерминированный Regex/Blacklist фильтр (мгновенный отсев за < 1 мс).
- ЭТАП Б: Семантическая валидация через локальную LLM (qwen2.5:7b в Ollama) для спорных случаев.
"""

import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger("HostingGuard")

BASE_DIR = Path(__file__).parent
OUTPUT_DIR = BASE_DIR / "output"
REJECTED_FILE = OUTPUT_DIR / "rejected_hosts.json"
GUARD_CACHE_FILE = OUTPUT_DIR / "hosting_guard_cache.json"

DEFAULT_OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
DEFAULT_MODEL = "qwen2.5:7b"

# Детерминированные стоп-паттерны (Case-Insensitive)
RE_BLACKLIST_PATTERNS = [
    # 1. Прямой отказ от хостинга
    r"\bdo\s+not\s+send\s+(?:me\s+)?(?:home\s+|stay\s+)?requests?\b",
    r"\bnot\s+hosting\b",
    r"\bno\s+hosting\b",
    r"\bhosting\s+not\s+available\b",
    r"\bnot\s+available\s+hosting\b",
    r"\b(?:cannot|can't|cant|unable\s+to|impossible\s+to)\s+host\b",
    r"\bnot\s+able\s+to\s+host\b",
    r"\bnot\s+accepting\s+guests?\b",
    r"\bcan['’]?t\s+take\s+guests\b",
    r"\bcannot\s+accept\s+guests\b",
    r"\bstop(?:ped)?\s+hosting\b",
    r"\bpause(?:d)?\s+hosting\b",
    r"\bnot\s+offering\s+(?:a\s+)?couch\b",
    r"\bno\s+space\s+to\s+(?:welcome|host)\b",
    r"\bnot\s+hosting\s+(?:people|guests|anyone)\b",
    r"\bdo\s+not\s+host\b",
    r"\bdon['’]?t\s+host\b",

    # 2. Только гости женского пола (дисквалификация для заявителя-мужчины)
    r"\bfemale\s+guests?\s+only\b",
    r"\bonly\s+female\b",
    r"\bwomen\s+only\b",
    r"\bonly\s+women\b",
    r"\bgirls?\s+only\b",
    r"\bonly\s+girls?\b",
    r"\bhost(?:ing)?\s+(?:only\s+)?female(?:\s+travelers?)?\s+only\b",
    r"\bhost(?:ing)?\s+female\s+travelers?\b",
    r"\bfemale\s+(?:travelers?|couchsurfers?|guests?)\s+only\b",
    r"\bonly\s+(?:host|accept)\s+(?:female|women|girls?)\b",

    # 3. Путешествия / отсутствие в городе / за пределами Японии
    r"\btrave[l]+ing(?:\s+solo)?\s+(?:through|in|around|across|europe|asia|south\s+america|latin\s+america|south\s+korea|america|world|abroad)\b",
    r"\bcurrently\s+trave[l]+ing\b",
    r"\bcurrently\s+(?:in|on|exploring|traveling(?:\s+solo)?\s+in|traveling(?:\s+solo)?\s+through)\s+(?!tokyo|japan|my\s+apartment|home\b)[a-z\s]+",
    r"\bwho\s+is\s+trave[l]+ing\b",
    r"\bexploring\s+(?:south\s+korea|europe|asia|vietnam|thailand|world|[a-z\s]+for\s+next)\b",
    r"\bnot\s+in\s+japan\b",
    r"\bcurrently\s+not\s+in\s+japan\b",
    r"\bliving\s+in\s+(?!tokyo|japan)[a-z\s]+\s+now\b",

    # 4. Проживание с родителями / семьей / нет собственного жилья
    r"\b(?:live|living|staying)\s+(?:with|at)\s+(?:my\s+)?(?:parents|family)(?:\s+house)?\b",
    r"\blive\s+(?:in\s+)?(?:my\s+)?parents\s+house\b",
    r"\bparents['’]?\s+house\b",
    r"\bwith\s+my\s+family\s+and\s+(?:can['’]?t\s+host|no\s+space)\b",

    # 5. Только встречи / кофе / бары
    r"\b(?:only|just)\s+(?:meet|coffee|hang|hangout|hang\s+out|drinks?|drink|walk|chat)\b",
    r"\b(?:only|just)\s+want\s+to\s+meet\b",
    r"\b(?:only|just)\s+available\s+(?:for|to)\s+(?:meet|coffee|drinks?)\b",
    r"\bcan\s+only\s+(?:meet|hang|have\s+coffee)\b",
    r"\bshow\s+you\s+around\s+only\b",
    r"\bonly\s+(?:to\s+)?show\s+you\s+around\b",
    r"\bmeet\s+up\s+only\b",
    r"\bavailable\s+for\s+hangout\s+only\b",

    # 6. Отсутствие спального места или пространства
    r"\bnot\s+enough\s+space\b",
    r"\bno\s+(?:spare\s+)?(?:bed|room|couch|space)\b",
    r"\bdon['’]?t\s+have\s+(?:a\s+)?(?:place|room|space|bed|couch)\b",
    r"\bmy\s+room\s+is\s+too\s+small\s+to\s+host\b",
    r"\bmy\s+apartment\s+is\s+too\s+small\b",
]

COMPILED_PATTERNS = [re.compile(p, re.IGNORECASE) for p in RE_BLACKLIST_PATTERNS]


class HostingAvailabilityGuard:
    """
    Двухэтапный верификатор реальной доступности ночлега у хоста.
    """

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        host: str = DEFAULT_OLLAMA_HOST,
        timeout: float = 20.0,
    ):
        self.model = model
        self.host = host.rstrip("/")
        self.timeout = timeout
        self.cache: Dict[str, Dict[str, Any]] = self._load_cache()

    def _load_cache(self) -> Dict[str, Dict[str, Any]]:
        if GUARD_CACHE_FILE.exists():
            try:
                with open(GUARD_CACHE_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"Ошибка загрузки кэша hosting_guard: {e}")
        return {}

    def _save_cache(self) -> None:
        GUARD_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(GUARD_CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(self.cache, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"Ошибка сохранения кэша hosting_guard: {e}")

    def check_regex_blacklist(self, text: str) -> Optional[Tuple[str, str]]:
        """
        ЭТАП А: Проверка по детерминированным регулярным выражениям.
        Возвращает (matched_text, pattern_str) или None.
        """
        if not text:
            return None

        clean = " ".join(text.split())
        for pat in COMPILED_PATTERNS:
            m = pat.search(clean)
            if m:
                return m.group(0), pat.pattern
        return None

    def _query_ollama_semantic(self, about_me: str, my_home: str = "") -> Dict[str, Any]:
        """
        ЭТАП Б: Семантическая валидация через Ollama (qwen2.5:7b).
        """
        prompt = f"""Analyze if this Couchsurfing host ACTUALLY offers overnight accommodation/hosting, or if they are only looking for meetups/hangouts/coffee.

Profile text:
{about_me[:600]}

My Home info:
{my_home[:400]}

Return strict JSON:
{{
  "can_host_overnight": true | false,
  "reason": "explicit explanation why they can or cannot host overnight",
  "confidence": 0.0-1.0
}}"""

        payload = {
            "model": self.model,
            "prompt": prompt,
            "format": "json",
            "stream": False,
            "options": {
                "temperature": 0.1,
                "num_predict": 120,
            },
        }

        try:
            resp = requests.post(f"{self.host}/api/generate", json=payload, timeout=self.timeout)
            if resp.status_code == 200:
                raw = resp.json().get("response", "")
                data = json.loads(raw)
                if isinstance(data, dict):
                    can_host = bool(data.get("can_host_overnight", True))
                    reason = str(data.get("reason", "LLM validated"))
                    confidence = float(data.get("confidence", 0.9))
                    return {
                        "can_host_overnight": can_host,
                        "reason": f"LLM: {reason}",
                        "confidence": confidence,
                    }
        except Exception as e:
            logger.debug(f"Ollama semantic check error: {e}")

        # Fallback при сбое LLM: считаем валидным, если прошел regex
        return {
            "can_host_overnight": True,
            "reason": "Ollama fallback (passed Stage A)",
            "confidence": 0.5,
        }

    def verify_host(
        self,
        host: Dict[str, Any],
        use_llm: bool = True,
    ) -> Tuple[bool, str, float]:
        """
        Проверяет анкету хоста на возможность предоставления ночлега.
        Возвращает: (can_host_overnight, reason, confidence)
        """
        username = host.get("username", "") or host.get("id", "unknown")
        about_me = (
            host.get("about_me_clean")
            or host.get("about_me")
            or host.get("about")
            or ""
        )
        my_home = (
            host.get("my_home")
            or host.get("sleeping_arrangement")
            or host.get("house_rules")
            or ""
        )
        full_text = f"{about_me} {my_home}".strip()

        # 1. Проверка в кэше
        if username in self.cache:
            entry = self.cache[username]
            return (
                entry["can_host_overnight"],
                entry["reason"],
                entry.get("confidence", 0.95),
            )

        # 2. ЭТАП А: Детерминированный Regex/Blacklist
        regex_hit = self.check_regex_blacklist(full_text)
        if regex_hit:
            matched_phrase, pat_pattern = regex_hit
            reason = f"Blacklist match: '{matched_phrase}'"
            self.cache[username] = {
                "can_host_overnight": False,
                "reason": reason,
                "confidence": 1.0,
            }
            self._save_cache()
            return False, reason, 1.0

        # 3. ЭТАП Б: Семантическая проверка через LLM (при наличии текста био)
        if use_llm and len(about_me.strip()) > 40:
            llm_result = self._query_ollama_semantic(about_me=about_me, my_home=my_home)
            can_host = llm_result["can_host_overnight"]
            reason = llm_result["reason"]
            conf = llm_result["confidence"]

            self.cache[username] = {
                "can_host_overnight": can_host,
                "reason": reason,
                "confidence": conf,
            }
            self._save_cache()
            return can_host, reason, conf

        # По умолчанию (прошел Regex, текста мало)
        reason = "Passed Stage A (no blacklist patterns)"
        self.cache[username] = {
            "can_host_overnight": True,
            "reason": reason,
            "confidence": 0.8,
        }
        self._save_cache()
        return True, reason, 0.8

    def record_rejected(self, host: Dict[str, Any], reason: str) -> None:
        """Сохраняет отсеянного хоста в output/rejected_hosts.json для аудита."""
        username = host.get("username", "") or host.get("id", "")
        name = host.get("name") or host.get("clean_name", "")
        about = (
            host.get("about_me_clean")
            or host.get("about_me")
            or host.get("about")
            or ""
        )

        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        rejected_list: List[Dict[str, Any]] = []

        if REJECTED_FILE.exists():
            try:
                with open(REJECTED_FILE, "r", encoding="utf-8") as f:
                    rejected_list = json.load(f)
            except Exception:
                rejected_list = []

        # Исключаем дубликаты
        existing_usernames = {x.get("username") for x in rejected_list if x.get("username")}
        if username not in existing_usernames:
            rejected_list.append({
                "username": username,
                "name": name,
                "reason": reason,
                "about_snippet": " ".join(about.split())[:300],
                "timestamp": datetime.now().isoformat(),
            })
            try:
                with open(REJECTED_FILE, "w", encoding="utf-8") as f:
                    json.dump(rejected_list, f, ensure_ascii=False, indent=2)
            except Exception as e:
                logger.warning(f"Ошибка сохранения rejected_hosts.json: {e}")


# Глобальный синглтон
_guard_instance: Optional[HostingAvailabilityGuard] = None


def get_hosting_guard() -> HostingAvailabilityGuard:
    """Возвращает глобальный экземпляр HostingAvailabilityGuard."""
    global _guard_instance
    if _guard_instance is None:
        _guard_instance = HostingAvailabilityGuard()
    return _guard_instance


def check_host_can_overnight(
    host: Dict[str, Any],
    use_llm: bool = True,
) -> Tuple[bool, str]:
    """
    Быстрая функция-помощник для внешних модулей.
    Возвращает (can_host_overnight: bool, reason: str).
    """
    guard = get_hosting_guard()
    can_host, reason, _ = guard.verify_host(host, use_llm=use_llm)
    return can_host, reason
