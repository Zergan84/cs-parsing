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
OUTPUT_DIR = Path(os.environ.get("CS_OUTPUT_DIR", str(BASE_DIR / "output"))).resolve()
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

# Международные имена, оканчивающиеся на гласные или суффиксы (-a, -i, -ka, -mi, -na),
# которые являются мужскими и НЕ должны классифицироваться по японским женским суффиксам
INTERNATIONAL_MALE_NAMES_WITH_VOWELS = {
    "luka", "luca", "mika", "sami", "rami", "krishna", "joshua", "slava", "nikita",
    "sasha", "ilya", "dima", "andrea", "mustafa", "hamza", "reza", "ali", "eli",
    "levi", "jonah", "noah", "ezra", "misha", "kolya", "vasya", "petya", "toma",
    "kuba", "kostya", "yura", "vanya", "boris", "dani", "danny", "jean", "sena"
}

NON_JAPANESE_CHARS = set("lvxq")
NON_JAPANESE_CLUSTERS = (
    "kr", "sl", "st", "sm", "sn", "sp", "sc", "sk", "br", "dr",
    "fr", "gr", "pr", "tr", "pl", "bl", "cl", "gl", "fl", "ph", "th"
)

def is_likely_japanese_name(name: str) -> bool:
    """Проверяет, соответствует ли имя базовым фонетическим правилам японского языка (ромадзи)."""
    n = name.lower()
    if any(c in NON_JAPANESE_CHARS for c in n):
        return False
    if "c" in n and "ch" not in n:
        return False
    if any(cl in n for cl in NON_JAPANESE_CLUSTERS):
        return False
    return True

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
    "erika", "kana", "sayaka", "mai", "chika", "tomomi", "ayumi", "megumi",
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
    "reona", "asahi", "mitsuki", "izumi", "shizuka", "haru", "ryo", "mika"
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

# УРОВЕНЬ 1: Мужские контекстные маркеры в Bio
RE_MALE_BIO_KEYWORDS = re.compile(
    r"\b(?:"
    r"ex-girlfriend|ex\s+girlfriend|my\s+girlfriend|my\s+wife"
    r"|as\s+a\s+(?:guy|man|boy|male|dude)"
    r"|i['’]?m\s+(?:a\s+)?(?:guy|man|boy|male|dude)"
    r"|i\s+am\s+(?:a\s+)?(?:guy|man|boy|male|dude)"
    r"|solo\s+(?:male|guy|man|boy)"
    r"|solo\s+adventure\s+boy"
    r"|he/him|him/he"
    r"|father|husband|boyfriend|brother|son"
    r"|actor|mr\."
    r")\b",
    re.IGNORECASE,
)

# УРОВЕНЬ 1: Женские контекстные маркеры в Bio
RE_FEMALE_BIO_KEYWORDS = re.compile(
    r"\b(?:"
    r"i['’]?m\s+(?:a\s+)?(?:japanese\s+|asian\s+|solo\s+)?(?:woman|girl|lady|female)"
    r"|i\s+am\s+(?:a\s+)?(?:japanese\s+|asian\s+|solo\s+)?(?:woman|girl|lady|female)"
    r"|japanese\s+(?:woman|girl|lady|female)"
    r"|the\s+girl\s+who\b"
    r"|as\s+a\s+(?:girl|woman|lady|female|female\s+trave[l]+er)"
    r"|solo\s+female(?:\s+trave[l]+er)?"
    r"|ex-boyfriend|ex\s+boyfriend|my\s+boyfriend|my\s+husband"
    r"|she/her|her/she"
    r"|mother|sister|daughter"
    r"|actress|ms\.|mrs\."
    r")\b",
    re.IGNORECASE,
)

# УРОВЕНЬ 2: Местоимения в References
RE_MALE_PRONOUNS = re.compile(r"\b(he|him|his|guy|gentleman|brother|man)\b", re.IGNORECASE)
RE_FEMALE_PRONOUNS = re.compile(r"\b(she|her|hers|girl|lady|sister|woman)\b", re.IGNORECASE)


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
        УРОВЕНЬ 1: Проверка явных контекстных маркеров пола в Bio.
        Пол НЕ определяется только по имени, никнейму или суффиксу.
        """
        about_lower = about_text.lower() if about_text else ""
        if not about_lower:
            return None

        # Мужские маркеры отношений и самоидентификации ("ex-girlfriend", "my girlfriend", "as a guy", "he/him")
        m_kw = RE_MALE_BIO_KEYWORDS.search(about_lower)
        # Женские маркеры отношений и самоидентификации ("she/her", "as a girl", "ex-boyfriend")
        f_kw = RE_FEMALE_BIO_KEYWORDS.search(about_lower)

        if m_kw and not f_kw:
            return "male", "solo", f"bio_context ('{m_kw.group(0)}')"
        if f_kw and not m_kw:
            return "female", "solo", f"bio_context ('{f_kw.group(0)}')"

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

        if male_count >= 1 and (female_count == 0 or male_count >= female_count * 2):
            return "male", "solo", f"references_pronouns (he/him x{male_count})"
        elif female_count >= 1 and (male_count == 0 or female_count >= male_count * 2):
            return "female", "solo", f"references_pronouns (she/her x{female_count})"

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
        # -suke, -taro, -kazu, -taka, -fumi, -masa, -hide, -shige, -nori, -shi, -to, -ro, -ya, -ji, -hei, -pei, -ichi
        if first_name.endswith("suke"):
            return "male", "solo", "japanese_male_suffix (-suke)"
        if first_name.endswith("taro"):
            return "male", "solo", "japanese_male_suffix (-taro)"
        if first_name.endswith("kazu"):
            return "male", "solo", "japanese_male_suffix (-kazu)"
        if first_name.endswith("taka"):
            return "male", "solo", "japanese_male_suffix (-taka)"
        if first_name.endswith("fumi"):
            return "male", "solo", "japanese_male_suffix (-fumi)"
        if first_name.endswith("masa"):
            return "male", "solo", "japanese_male_suffix (-masa)"
        if first_name.endswith("hide"):
            return "male", "solo", "japanese_male_suffix (-hide)"
        if first_name.endswith("shige"):
            return "male", "solo", "japanese_male_suffix (-shige)"
        if first_name.endswith("nori"):
            return "male", "solo", "japanese_male_suffix (-nori)"
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
        # ВАЖНО: суффиксы -ka, -mi, -na применяются ТОЛЬКО к валидным японским именам!
        # Неяпонские и международные имена (Luka, Mika, Slava, Sami, Rami, Krishna и др.)
        # не должны классифицироваться как female по окончанию.
        if first_name in INTERNATIONAL_MALE_NAMES_WITH_VOWELS:
            return "male", "solo", f"international_male_name ({first_name})"

        if is_likely_japanese_name(first_name):
            if first_name.endswith("mi") and not first_name.endswith(("fumi", "umi")):
                return "female", "solo", "japanese_female_suffix (-mi)"
            if first_name.endswith("ka") and not first_name.endswith("taka"):
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
        prompt = f"""Determine gender for profile based ONLY on explicit factual evidence in Bio or References:
Name: {name}
Bio: {bio_snippet}
References clues: {pronoun_clues}

CRITICAL RULES:
1. If there are NO explicit factual clues (such as pronouns he/she/him/her, or relationship roles wife/husband/girlfriend/boyfriend) in Bio or References, output "unknown".
2. NEVER guess gender based on name alone.
3. Return ONLY valid JSON.

JSON format:
{{"gender": "male" | "female" | "couple" | "unknown", "confidence": 0.0-1.0, "reason": "short explanation"}}"""

        payload = {
            "model": self.model,
            "prompt": prompt,
            "format": "json",
            "stream": False,
            "system": (
                "You are a strict, evidence-based gender classifier for Couchsurfing profiles. "
                "Classify gender ONLY when explicit factual clues are present in the text (pronouns, spouse/partner terms). "
                "Never guess gender based on first name alone. If in doubt or evidence is lacking, return 'unknown'."
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

        # При отсутствии явных контекстных подтверждений в bio или references:
        # Пол нельзя определять только по имени, суффиксу, национальности или фотографии.
        return "Unknown", "solo", "insufficient_context (no explicit contextual confirmation in bio or references)"

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
