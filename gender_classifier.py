"""
gender_classifier.py - Высокоточный многоуровневый классификатор пола хостов.

Архитектура классификации (GenderClassifier) со строгой приоритетной цепочкой:

УРОВЕНЬ 0: Детекция пар (Couple) по имени (&, and, +) или маркерам совместного проживания.
УРОВЕНЬ 1: Проверка жестких мужских/женских маркеров и никнеймов в Bio:
  - Западные мужские имена: Tom, Tommy, Ken, Dan, Nick, Mike, Alex, Ben
  - Отношения и маркеры себя: ex-girlfriend, my girlfriend, my wife, as a guy, as a man, he/him
  - Женские маркеры себя: ex-boyfriend, my husband, as a girl, as a woman, she/her
УРОВЕНЬ 2: Анализ отзывов (References) на местоимения третьего лица:
  - Мужские: he, him, his, guy, gentleman, brother
  - Женские: she, her, hers, girl, lady, sister
  - Преобладание одного пола (>= 2 и 0 противоположного, либо перевес 2:1)
УРОВЕНЬ 3: Лингвистический парсинг японских имен (БЕЗ ложных суффиксов):
  - Баг с -o: только если имя заканчивается СТРОГО на "ko" (Tomoko, Hanako) -> female (-hiko -> male)
  - Женские суффиксы: -mi, -ka, -na, -e (Rie, Tomoe) -> female
  - Мужские суффиксы: -o без k (Tomo, Taro, Ichiro, Kenzo), -to, -shi, -kazu, -suke, -ji, -ya, -hei, -pei -> male
  - Имя "Tomo" изолированно: унисекс/мужское, ни в коем случае не female.
УРОВЕНЬ 4: Локальная LLM (qwen2.5:7b через Ollama) с JSON-ответом:
  - ТОЛЬКО для оставшихся неоднозначных/амбивалентных имен (Yuki, Ren, Kaoru).
"""

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import requests

logger = logging.getLogger("GenderClassifier")

BASE_DIR = Path(__file__).parent
OUTPUT_DIR = BASE_DIR / "output"
CACHE_FILE = OUTPUT_DIR / "gender_cache.json"

DEFAULT_OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
DEFAULT_MODEL = "qwen2.5:7b"

# Западные и международные мужские имена / никнеймы
WESTERN_MALE_NAMES = {
    "tom", "tommy", "ken", "dan", "nick", "mike", "alex", "ben",
    "jose", "felipe", "hide", "shunsuke", "jorge", "andre", "andrey",
    "carlos", "pedro", "luis", "juan", "marco", "diego", "mateo",
    "maxence", "wade", "dec", "decdec"
}

# Проверенные японские мужские имена
JP_MALE_NAMES = {
    "tomo", "ryu", "hiro", "takumi", "hikaru", "naoya", "hayase", "koji", "yamamoto", "kenya",
    "yujiro", "taka", "rintaro", "ryuhei", "toshihiro", "ryuichi", "seiya", "taro",
    "daiki", "shota", "kenta", "kazuki", "yusuke", "kenji", "shinji", "masashi",
    "sho", "sota", "haruto", "yuto", "soma", "minato", "atsushi", "keisuke",
    "tomoya", "kosuke", "yuya", "daisuke", "jun", "taiki", "satoshi", "akira", "takashi",
    "ken", "shin", "hidenori", "takeshi", "yuji", "kohei", "kazuma", "kazuhiko",
    "masahiro", "yoshiki", "ryota", "sosuke", "kazuya", "shun", "yuuki", "yukihiro",
    "dec", "decdec", "mamad", "horacio", "karthik", "prag78m", "andreas", "arnaud",
    "alessandro", "hiroshi", "hirochan", "riota", "ryo", "rikushi", "tomofumi",
    "nobu", "ryuya", "yosh", "tsuyoshi", "essakhan", "hau", "song", "wei", "shitetsu",
    "masakazu", "ryosuke", "boyi", "homare", "marki", "essahunza", "kentaro", "hide",
    "jose", "felipe", "shunsuke"
}

MALE_NAME_SUFFIXES = ("suke", "taro", "kazu", "hiko", "hei", "pei", "ichi")

# Западные и международные женские имена
WESTERN_FEMALE_NAMES = {
    "lucy", "gloria", "sophia", "franca", "kumi", "naoko", "chihiro",
    "elena", "maria", "emma", "sarah", "charlotte", "laura", "anna",
    "tessa", "alice", "marion", "sophie", "julia", "robyn", "rita",
    "cintia", "alyssa", "leah", "jade", "fuka"
}

# Проверенные японские женские имена
JP_FEMALE_NAMES = {
    "ai", "nozomi", "sakura", "yui", "aoi", "hina", "rin", "mio", "mei", "yuna", "akari",
    "koharu", "hana", "ayaka", "asuka", "nanami", "rina", "yuka", "misaki",
    "erika", "kana", "sayaka", "mai", "chika", "mika", "tomomi", "ayumi", "megumi",
    "shiori", "haruka", "marion", "sophie", "julia", "elena", "maria", "emma", "sarah",
    "charlotte", "laura", "anna", "tessa", "alice", "chihiro", "reiko", "keiko",
    "yoko", "kumiko", "midori", "naoko", "mariko", "tomoko", "junko", "noriko",
    "ayami", "eimi", "yuko", "cha", "robyn", "ojo", "rie", "saki", "urara", "yara",
    "arina", "rita", "cintia", "kaori", "maki", "nana", "tomoe", "lucy", "gloria",
    "sophia", "franca", "kumi"
}

# Амбивалентные / унисекс имена, требующие контекста или LLM
AMBIGUOUS_NAMES = {
    "yuki", "ren", "kaoru", "makoto", "aoi", "shinobu", "tsubasa", "akira",
    "hikaru", "hinata", "nagisa", "sora", "ibuki", "mutsumi",
    "reona", "asahi", "mitsuki", "izumi", "shizuka", "haru", "ryo"
}

# Маркеры пар (Couple)
COUPLE_NAME_PATTERNS = [
    re.compile(r"(&|\band\b|\+|\/)", re.IGNORECASE),
]

COUPLE_TEXT_PATTERNS = [
    re.compile(r"\bmy\s+wife\s+and\s+i\b", re.IGNORECASE),
    re.compile(r"\bmy\s+husband\s+and\s+i\b", re.IGNORECASE),
    re.compile(r"\bmy\s+partner\s+and\s+i\b", re.IGNORECASE),
    re.compile(r"\bwith\s+my\s+boyfriend\b", re.IGNORECASE),
    re.compile(r"\bwith\s+my\s+girlfriend\b", re.IGNORECASE),
    re.compile(r"\bwith\s+my\s+wife\b", re.IGNORECASE),
    re.compile(r"\bwith\s+my\s+husband\b", re.IGNORECASE),
    re.compile(r"\bwith\s+my\s+partner\b", re.IGNORECASE),
    re.compile(r"\bwe\s+are\s+a\s+couple\b", re.IGNORECASE),
    re.compile(r"\bour\s+(?:flat|apartment|house|home|place)\b", re.IGNORECASE),
    re.compile(r"\bwe\s+live\s+together\b", re.IGNORECASE),
    re.compile(r"\bwe\s+both\b", re.IGNORECASE),
    re.compile(r"\bhusband\s+and\s+wife\b", re.IGNORECASE),
]

# УРОВЕНЬ 1: Мужские маркеры в Bio
RE_MALE_BIO_NAME_SELF = re.compile(
    r"\b(?:call\s+me|i['’]?m|i\s+am|my\s+name\s+is|name['’]?s|people\s+call\s+me|friends\s+call\s+me|just\s+call\s+me)\s+(?:tom|tommy|ken|dan|nick|mike|alex|ben|jose|felipe|hide)\b",
    re.IGNORECASE,
)

RE_MALE_BIO_KEYWORDS = re.compile(
    r"\b(?:ex-girlfriend|ex\s+girlfriend|my\s+girlfriend|my\s+wife|as\s+a\s+guy|as\s+a\s+man|i['’]?m\s+a\s+guy|i\s+am\s+a\s+guy|i['’]?m\s+a\s+man|i\s+am\s+a\s+man|i['’]?m\s+a\s+dude|i\s+am\s+a\s+dude|i['’]?m\s+a\s+boy|i\s+am\s+a\s+boy|he/him|him/he|solo\s+male|actor|mr\.)\b",
    re.IGNORECASE,
)

# УРОВЕНЬ 1: Женские маркеры в Bio (включая "I am japanese woman", "japanese girl", и т.д.)
RE_FEMALE_BIO_KEYWORDS = re.compile(
    r"\b(?:"
    r"i['’]?m\s+(?:a\s+)?(?:japanese\s+|asian\s+|solo\s+)?(?:woman|girl|lady|female)"
    r"|i\s+am\s+(?:a\s+)?(?:japanese\s+|asian\s+|solo\s+)?(?:woman|girl|lady|female)"
    r"|japanese\s+(?:woman|girl|lady|female)"
    r"|as\s+a\s+(?:girl|woman|lady|female|female\s+trave[l]+er)"
    r"|solo\s+female(?:\s+trave[l]+er)?"
    r"|ex-boyfriend|ex\s+boyfriend|my\s+husband"
    r"|she/her|her/she|actress|ms\.|mrs\."
    r")\b",
    re.IGNORECASE,
)

# УРОВЕНЬ 2: Местоимения в References
RE_MALE_PRONOUNS = re.compile(r"\b(he|him|his|guy|gentleman|brother)\b", re.IGNORECASE)
RE_FEMALE_PRONOUNS = re.compile(r"\b(she|her|hers|girl|lady|sister)\b", re.IGNORECASE)


class GenderClassifier:
    """
    Высокоточный многоуровневый классификатор пола хостов.
    """

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        host: str = DEFAULT_OLLAMA_HOST,
        timeout: int = 12,
        cache_path: Optional[Path] = None,
    ):
        self.model = model
        self.host = host.rstrip("/")
        self.timeout = timeout
        self.cache_path = Path(cache_path) if cache_path else CACHE_FILE
        self.cache: Dict[str, Dict[str, Any]] = self._load_cache()

    def _load_cache(self) -> Dict[str, Dict[str, Any]]:
        """Загружает кэш классификации из JSON."""
        if self.cache_path.exists():
            try:
                with open(self.cache_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"Ошибка чтения кэша gender_cache.json: {e}")
        return {}

    def _save_cache(self) -> None:
        """Сохраняет кэш на диск."""
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.cache_path, "w", encoding="utf-8") as f:
                json.dump(self.cache, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"Ошибка сохранения gender_cache.json: {e}")

    def _clean_name(self, name: str) -> str:
        """Очищает имя от инициалов фамилий ('Koji A.' -> 'Koji') и артефактов кодировки."""
        if not name:
            return ""
        # Удаляем unicode replacement char \ufffd и непечатаемые спецсимволы
        cleaned = str(name).replace("�", "").replace("", "")
        # Очищаем инициалы вроде ' A.' или ' S.'
        cleaned = re.sub(r"\s+[A-Za-z]\.?$", "", cleaned.strip()).strip()
        # Если есть точка на конце (например '彩 毛.' или 'y.'), убираем её
        cleaned = re.sub(r"[\.\s]+$", "", cleaned).strip()
        return cleaned or name.strip()

    def _normalize_references(self, references: Any) -> str:
        """Приводит отзывы любого формата (str, list[str], list[dict]) к единой строке."""
        if not references:
            return ""
        if isinstance(references, str):
            return references
        if isinstance(references, list):
            parts = []
            for r in references:
                if isinstance(r, str):
                    parts.append(r)
                elif isinstance(r, dict):
                    parts.append(str(r.get("text") or r.get("body") or r.get("content") or ""))
            return " ".join(parts)
        return ""

    def _check_couple(self, name: str, about_text: str, username: str) -> Tuple[bool, str]:
        """Проверяет признаки пары в имени или описании."""
        for pat in COUPLE_NAME_PATTERNS:
            if pat.search(name):
                return True, "couple symbol/word in name"

        about_lower = about_text.lower()
        for pat in COUPLE_TEXT_PATTERNS:
            m = pat.search(about_lower)
            if m:
                return True, f"couple pattern in bio ('{m.group(0)}')"

        return False, ""

    def _check_level1_bio(self, clean_name: str, about_text: str) -> Optional[Tuple[str, str, str]]:
        """
        УРОВЕНЬ 1: Проверка жестких мужских маркеров и никнеймов в Bio.
        Возвращает (gender, host_type, source) или None.
        """
        first_word = re.split(r"[\s\-_./]+", clean_name.lower())[0] if clean_name else ""

        # Мужские имена в имени профиля (западные и японские)
        if first_word in WESTERN_MALE_NAMES or first_word in JP_MALE_NAMES:
            return "male", "solo", "bio_keywords"

        for suff in MALE_NAME_SUFFIXES:
            if first_word.endswith(suff):
                return "male", "solo", f"japanese_male_suffix (-{suff})"

        about_lower = about_text.lower() if about_text else ""

        # Упоминания имени/никнейма в Bio ("Call me Tom", "I'm Mike")
        m_self = RE_MALE_BIO_NAME_SELF.search(about_lower)
        if m_self:
            return "male", "solo", "bio_keywords"

        # Мужские маркеры отношений и самоидентификации ("ex-girlfriend", "my girlfriend", "as a guy")
        m_kw = RE_MALE_BIO_KEYWORDS.search(about_lower)
        if m_kw:
            return "male", "solo", "bio_keywords"

        # Женские маркеры отношений и самоидентификации ("she/her", "as a girl", "ex-boyfriend")
        f_kw = RE_FEMALE_BIO_KEYWORDS.search(about_lower)
        if f_kw:
            return "female", "solo", "bio_keywords"

        return None

    def _check_level2_references(self, ref_text: str) -> Optional[Tuple[str, str, str]]:
        """
        УРОВЕНЬ 2: Анализ отзывов (References) на местоимения третьего лица.
        Возвращает (gender, host_type, source) или None.
        """
        if not ref_text:
            return None

        male_matches = RE_MALE_PRONOUNS.findall(ref_text)
        female_matches = RE_FEMALE_PRONOUNS.findall(ref_text)

        male_count = len(male_matches)
        female_count = len(female_matches)

        if male_count >= 2 and (female_count == 0 or male_count >= female_count * 2):
            return "male", "solo", "references_pronouns"
        elif female_count >= 2 and (male_count == 0 or female_count >= male_count * 2):
            return "female", "solo", "references_pronouns"

        return None

    def _check_level3_linguistics(self, clean_name: str) -> Optional[Tuple[str, str, str]]:
        """
        УРОВЕНЬ 3: Лингвистический парсинг японских имен (БЕЗ ложных суффиксов).
        Возвращает (gender, host_type, source) или None.
        """
        if not clean_name:
            return None

        first_name = re.split(r"[\s\-_./]+", clean_name.lower())[0]

        # 1. Проверка проверенных словарей (мужские и женские имена)
        if first_name in JP_MALE_NAMES or first_name in WESTERN_MALE_NAMES:
            return "male", "solo", f"male_name_dict ({first_name})"
        if first_name in JP_FEMALE_NAMES or first_name in WESTERN_FEMALE_NAMES:
            return "female", "solo", f"female_name_dict ({first_name})"

        # 2. Амбивалентные имена исключаются из суффиксного парсинга и передаются в LLM (Уровень 4)
        if first_name in AMBIGUOUS_NAMES:
            return None

        # 3. ИСПРАВЛЕНИЕ БАГА С ОКОНЧАНИЕМ НА -o:
        # Изолированное Tomo - унисекс/мужское, ни в коем случае не female!
        if first_name == "tomo":
            return "male", "solo", "japanese_name_tomo (male)"

        # СТРОГАЯ проверка женского окончания -ko:
        # Tomoko, Hanako, Keiko -> female ТОЛЬКО если заканчивается СТРОГО на "ko",
        # при этом -hiko (Kazuhiko, Masahiko) -> male!
        if first_name.endswith("hiko"):
            return "male", "solo", "japanese_male_suffix (-hiko)"

        if first_name.endswith("ko"):
            return "female", "solo", "japanese_female_suffix (-ko)"

        # Мужские суффиксы:
        # -suke, -taro, -kazu, -shi, -to, -ro, -ya, -ji, -hei, -pei, -ichi
        if first_name.endswith("suke"):
            return "male", "solo", "japanese_male_suffix (-suke)"
        if first_name.endswith("taro"):
            return "male", "solo", "japanese_male_suffix (-taro)"
        if first_name.endswith("kazu"):
            return "male", "solo", "japanese_male_suffix (-kazu)"
        if first_name.endswith("shi"):
            return "male", "solo", "japanese_male_suffix (-shi)"
        if first_name.endswith("to"):
            return "male", "solo", "japanese_male_suffix (-to)"
        if first_name.endswith("ro"):
            return "male", "solo", "japanese_male_suffix (-ro)"
        if first_name.endswith("ya"):
            return "male", "solo", "japanese_male_suffix (-ya)"
        if first_name.endswith("ji"):
            return "male", "solo", "japanese_male_suffix (-ji)"
        if first_name.endswith(("hei", "pei")):
            return "male", "solo", "japanese_male_suffix (-hei/-pei)"
        if first_name.endswith("ichi"):
            return "male", "solo", "japanese_male_suffix (-ichi)"

        # -o БЕЗ k (Tomo, Taro, Ichiro, Kenzo, Hideo, Kazuo) -> male
        if first_name.endswith("o") and not first_name.endswith("ko"):
            return "male", "solo", "japanese_male_suffix (-o without k)"

        # Женские суффиксы:
        # -mi (Eimi, Yumi, Ayami), -ka (Haruka, Asuka), -na (Rina, Kana), -e (Rie, Tomoe)
        if first_name.endswith("mi"):
            return "female", "solo", "japanese_female_suffix (-mi)"
        if first_name.endswith("ka"):
            return "female", "solo", "japanese_female_suffix (-ka)"
        if first_name.endswith("na"):
            return "female", "solo", "japanese_female_suffix (-na)"

        # Суффикс -e для японских женских имен (Rie, Tomoe, Sanae), исключая любые мужские имена
        if first_name not in WESTERN_MALE_NAMES and first_name not in JP_MALE_NAMES:
            if first_name.endswith(("rie", "moe", "nae", "tae", "sae", "kae", "toe")):
                return "female", "solo", "japanese_female_suffix (-e)"

        return None

    def _query_ollama(
        self,
        name: str,
        bio_snippet: str,
        pronoun_clues: str = "",
    ) -> Tuple[str, float, str]:
        """
        УРОВЕНЬ 4: Запрос в локальную Ollama (qwen2.5:7b) со строгим JSON ответом.
        """
        prompt = f"""Determine gender for profile:
Name: {name}
Bio: {bio_snippet}
References clues: {pronoun_clues}

Return ONLY JSON:
{{"gender": "male" | "female" | "couple" | "unknown", "confidence": 0.0-1.0, "reason": "short explanation"}}"""

        payload = {
            "model": self.model,
            "prompt": prompt,
            "format": "json",
            "stream": False,
            "system": (
                "You are a precise gender classifier for Couchsurfing profiles. "
                "Determine if the person is male, female, couple, or unknown based on their Japanese/international name and context. "
                "Rules:\n"
                "1. Heterosexual context: Mentions of 'ex-girlfriend', 'girlfriend', or 'wife' by the user indicate male gender.\n"
                "2. The name 'Tomo' is typically male/unisex, never classify as female unless bio explicitly says woman/girl.\n"
                "3. Return ONLY valid JSON."
            ),
            "options": {"temperature": 0.1, "num_predict": 150},
        }

        try:
            resp = requests.post(f"{self.host}/api/generate", json=payload, timeout=self.timeout)
            if resp.status_code == 200:
                raw_text = resp.json().get("response", "")
                data = json.loads(raw_text)
                gender = str(data.get("gender", "unknown")).strip().lower()
                confidence = float(data.get("confidence", 0.8))
                reason = str(data.get("reason", "Ollama LLM"))

                if gender in ("male", "man", "guy"):
                    norm_g = "male"
                elif gender in ("female", "woman", "girl"):
                    norm_g = "female"
                elif gender in ("couple", "pair"):
                    norm_g = "couple"
                else:
                    norm_g = "unknown"

                return norm_g, confidence, reason
        except Exception as e:
            logger.warning(f"Ошибка запроса в Ollama: {e}")

        return "unknown", 0.0, "Ollama unavailable/error"

    def classify(
        self,
        name: str,
        about_text: str = "",
        username: str = "",
        references: Any = None,
    ) -> Tuple[str, str, str]:
        """
        Основной метод классификации по 4-уровневой приоритетной цепочке:
        Возвращает:
          - estimated_gender: "Male" | "Female" | "Couple" | "Unknown"
          - host_type: "solo" | "couple"
          - reason: детальное описание источника/правила
        """
        clean_name = self._clean_name(name)
        ref_text = self._normalize_references(references)

        # УРОВЕНЬ 0: Проверка пары (Couple)
        is_couple, couple_reason = self._check_couple(name, about_text, username)
        if is_couple:
            return "Couple", "couple", couple_reason

        # УРОВЕНЬ 1: Жесткие мужские/женские маркеры и никнеймы в Bio
        l1_res = self._check_level1_bio(clean_name, about_text)
        if l1_res:
            gender, host_type, source = l1_res
            cap_gender = gender.capitalize()
            return cap_gender, host_type, source

        # УРОВЕНЬ 2: Местоимения в References (he/him vs she/her)
        l2_res = self._check_level2_references(ref_text)
        if l2_res:
            gender, host_type, source = l2_res
            cap_gender = gender.capitalize()
            return cap_gender, host_type, source

        # УРОВЕНЬ 3: Лингвистический парсинг японских и международных имен (БЕЗ ложных суффиксов)
        l3_res = self._check_level3_linguistics(clean_name)
        if l3_res:
            gender, host_type, source = l3_res
            cap_gender = gender.capitalize()
            return cap_gender, host_type, source

        # Проверка локального кэша перед тяжелым вызовом LLM (Уровень 4)
        clean_name_lower = clean_name.lower()
        if clean_name_lower in AMBIGUOUS_NAMES or not username:
            import hashlib
            bio_hash = hashlib.md5(((about_text or "") + ":" + (ref_text or "")).encode("utf-8")).hexdigest()[:8]
            cache_key = f"{clean_name_lower}:{username.lower()}:{bio_hash}"
        else:
            cache_key = f"{clean_name_lower}:{username.lower()}"

        if cache_key in self.cache:
            c = self.cache[cache_key]
            g = c.get("gender", "Unknown")
            ht = "couple" if g == "Couple" else "solo"
            return g, ht, f"cache ({c.get('reason', '')})"

        # УРОВЕНЬ 4: Локальная LLM (qwen2.5:7b) ТОЛЬКО для оставшихся неоднозначных случаев
        llm_gender, conf, llm_reason = self._query_ollama(
            name=clean_name,
            bio_snippet=about_text[:300] if about_text else "",
            pronoun_clues=ref_text[:200] if ref_text else "",
        )

        cap_gender = "Couple" if llm_gender == "couple" else llm_gender.capitalize()
        host_type = "couple" if cap_gender == "Couple" else "solo"
        full_reason = f"ollama_llm ({llm_reason})"

        self.cache[cache_key] = {"gender": cap_gender, "confidence": conf, "reason": full_reason}
        self._save_cache()
        return cap_gender, host_type, full_reason

    def classify_batch(self, hosts_list: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Пакетная классификация для списка хостов.
        """
        for h in hosts_list:
            name = h.get("name", "")
            bio = h.get("about_me", "") or h.get("about_me_clean", "")
            username = h.get("username", "")
            refs = h.get("references", "")
            gender, host_type, reason = self.classify(name, bio, username, refs)
            h["gender"] = gender.lower()
            h["estimated_gender"] = gender
            h["host_type"] = host_type
            h["gender_reason"] = reason

        return hosts_list


# Обратная совместимость для существующего кода
LLMGenderClassifier = GenderClassifier

_classifier_instance: Optional[GenderClassifier] = None


def get_gender_classifier() -> GenderClassifier:
    """Возвращает глобальный синглтон GenderClassifier."""
    global _classifier_instance
    if _classifier_instance is None:
        _classifier_instance = GenderClassifier()
    return _classifier_instance


def classify_host(
    name: str,
    about_text: str = "",
    username: str = "",
    references: Any = None,
) -> Tuple[str, str]:
    """
    Обратно-совместимая функция для вызова из других модулей.
    Возвращает (estimated_gender, host_type):
    - estimated_gender: "Male", "Female", "Couple", "Unknown"
    - host_type: "solo", "couple"
    """
    classifier = get_gender_classifier()
    gender, host_type, _ = classifier.classify(name, about_text, username, references)
    return gender, host_type
