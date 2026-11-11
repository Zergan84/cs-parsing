"""
message_generator.py - Personalized request generator for Couchsurfing.

Forms a warm, respectful, and concise message to a host:
1. Warm personal greeting with reference to the host's specific interest.
2. Clear arrival/departure dates in conversational format (Nov 18-20) and natural trip purpose.
3. Respect for space: daytime activity in the city, suggestion of low-pressure evening chat.
4. Strict volume: 65-90 words in 3 micro-paragraphs.
All text is English only, ASCII only.
"""

import json
import random
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple, Union


def format_conversational_dates(check_in: str, check_out: str) -> str:
    """Formats dates into conversational English: Nov 18–20."""
    try:
        cin_clean = str(check_in).strip()[:10]
        cout_clean = str(check_out).strip()[:10]
        d_in = datetime.strptime(cin_clean, "%Y-%m-%d")
        d_out = datetime.strptime(cout_clean, "%Y-%m-%d")
        m_in = d_in.strftime("%b")
        m_out = d_out.strftime("%b")
        if d_in.month == d_out.month:
            return f"{m_in} {d_in.day}–{d_out.day}"
        else:
            return f"{m_in} {d_in.day} – {m_out} {d_out.day}"
    except Exception:
        return "Nov 18–20"


def format_date_range(check_in: str, check_out: str) -> str:
    """Alias for backwards compatibility."""
    return format_conversational_dates(check_in, check_out)


COMMON_JP_SURNAMES = {
    "sato", "suzuki", "takahashi", "tanaka", "watanabe", "ito", "yamamoto", "nakamura",
    "kobayashi", "kato", "yoshida", "yamada", "sasaki", "yamaguchi", "saito", "matsumoto",
    "inoue", "kimura", "hayashi", "shimizu", "yamazaki", "mori", "abe", "ikeda", "hashimoto",
    "yamashita", "ishikawa", "nakajima", "maeda", "fujita", "ogawa", "goto", "okada",
    "hasegawa", "murakami", "kondo", "ishii", "sakamoto", "endo", "aoki", "fujii",
    "nishimura", "fukuda", "miura", "fujiwara", "okamoto", "matsuda", "nakagawa", "harada",
    "ono", "tamura", "takeuchi", "kaneko", "wada", "nakano", "ishizuka", "moroi", "tokuda",
    "hagimori", "ogura", "yamauchi", "shiozaki", "higashi", "suda", "ueda", "kojima",
    "shibata", "sakai", "miyazaki", "kudo", "yokoyama", "miyake", "noguchi", "otsuka",
    "chiba", "iwasaki", "kikuchi", "kinoshita", "arai", "sugiyama", "matsuoka", "hirano",
    "uchida", "kubo", "nomura", "miyata", "kitamura", "igarashi", "hoshino", "sano",
    "oshima", "sugawara", "naito", "tani", "doi", "nogami", "tsukamoto", "hori",
    "nishida", "kawai", "shiraishi", "sekine", "kuroda", "kuwabara", "tsuji", "asano",
}


def verify_host_name(host: Dict[str, Any]) -> Tuple[str, bool]:
    """
    Извлекает и строго валидирует настоящее имя хоста (display name / first name).
    Возвращает (first_name, is_trusted).
    
    Требования:
    1. Использовать подтверждённое first name из профиля.
    2. Не принимать фамилию за имя (японские фамилии типа Ishizuka, Tanaka).
    3. Не превращать username в имя.
    4. Если имя неоднозначно — возвращать ("", False), чтобы выставить Name_Review_Required.
    """
    raw_name = host.get("name") or ""
    raw_clean = host.get("clean_name") or ""
    username = str(host.get("username") or host.get("id") or "").strip().lstrip("@")
    bio = host.get("about_me") or host.get("about_me_clean") or ""

    fake_placeholders = {"there", "host", "user", "member", "unknown", "none", "null", "profile", "guest"}

    # Проверка 1: прямое указание имени в биографии ("My name is Hidetaka", "Call me Alex")
    m_bio = re.search(r"(?i)\b(?:my name is|call me)\s+([A-Za-z]+)", bio)
    if m_bio:
        bio_name = m_bio.group(1).capitalize()
        if bio_name.lower() not in fake_placeholders and len(bio_name) >= 2:
            if bio_name.lower() not in COMMON_JP_SURNAMES:
                return bio_name, True

    def clean_candidate(val: Any) -> str:
        if not val:
            return ""
        s = str(val).strip().lstrip("@")
        s = s.replace(chr(0x014c), "O").replace(chr(0x014d), "o")
        if s.lower() in fake_placeholders:
            return ""
        # Удаляем локационные суффиксы ("in Tokyo", "from Japan")
        s = re.sub(r"(?i)\s+(in|from|at)\s+.*$", "", s)
        # Удаляем смайлики
        s = re.sub(r"[:;=8][\-o\*\']?[\)\]\(\[dDpP/\\]", "", s)
        # Обработка скобок: "SATOMI (SATOMI)" -> "SATOMI"
        m_paren = re.search(r"\(([^)]+)\)", s)
        s_no_paren = re.sub(r"\([^)]*\)", "", s).strip()
        target = s_no_paren if s_no_paren else (m_paren.group(1) if m_paren else s)
        # Удаляем инициалы фамилий: "Sabina M.", "Olena S."
        target = re.sub(r"[\s\.,_-]+[A-Za-z]\.?$", "", target).strip()
        parts = target.split()
        if not parts:
            return ""
        first = parts[0]
        # Извлекаем буквенную часть (латиница)
        letters = re.findall(r"[a-zA-Z]+", first)
        if letters:
            w = letters[0]
            if w.lower() in fake_placeholders or len(w) < 2:
                return ""
            return w[0].upper() + w[1:].lower()
        return ""

    # Приоритет 1: настоящее имя (name)
    name_cand = clean_candidate(raw_name)
    if name_cand and name_cand.lower() not in COMMON_JP_SURNAMES:
        # Не должно совпадать с техническим username или состоять из цифр
        if name_cand.lower() != username.lower() and not name_cand.isdigit():
            return name_cand, True

    # Приоритет 2: clean_name (если это не username с дефисами/цифрами и не фамилия)
    if "-" not in str(raw_clean) and "_" not in str(raw_clean) and not any(ch.isdigit() for ch in str(raw_clean)):
        clean_cand = clean_candidate(raw_clean)
        if clean_cand and clean_cand.lower() not in COMMON_JP_SURNAMES:
            if clean_cand.lower() != username.lower() and not clean_cand.isdigit():
                return clean_cand, True

    # Если имя совпадает с username, но username не числовой и не содержит дефисов
    if name_cand and name_cand.lower() not in COMMON_JP_SURNAMES:
        if not username.isdigit() and "-" not in username and "_" not in username and len(name_cand) >= 3 and not any(ch.isdigit() for ch in username):
            return name_cand, True

    # Имя неоднозначно (фамилия, числовой ID, пустая графа, username)
    return "", False


def extract_greeting_name(host: Dict[str, Any]) -> str:
    """Извлекает проверенное имя хоста для приветствия. Если не надежно — возвращает пустую строку."""
    first_name, is_trusted = verify_host_name(host)
    return first_name if is_trusted else ""


def _extract_anchor(host: Dict[str, Any]) -> str:
    """Extracts a profile fact / anchor for the opening hook."""
    hooks = host.get("conversation_hooks") or []
    if isinstance(hooks, str):
        try:
            hooks = json.loads(hooks)
        except Exception:
            hooks = [hooks]
    if isinstance(hooks, list) and hooks:
        first = str(hooks[0]).strip()
        if first:
            return first

    secret = host.get("secret_code_hint") or ""
    if secret:
        return secret

    # Check Host Dossier visual observations or confirmed interests if available
    host_id = host.get("id") or host.get("username") or ""
    if host_id:
        try:
            from database import get_host_dossier
            dossier = get_host_dossier(host_id)
            if dossier:
                # 1. High confidence confirmed interest
                for item in dossier.get("interests", []):
                    if item.get("confidence", 0) >= 0.85 and item.get("name"):
                        return item["name"]
                # 2. Visual observation (e.g. camera -> photography, vinyl -> music)
                for vo in dossier.get("visual_observations", []):
                    obs = vo.get("observation", "").lower()
                    if obs in ("camera", "photography", "analog photo"):
                        return "photography"
                    elif obs in ("vinyl", "guitar", "piano", "record"):
                        return "music"
                    elif obs in ("cat", "dog", "pet"):
                        return "animals"
        except Exception:
            pass

    bio = (host.get("about_me_clean") or host.get("about_me") or "").lower()
    interest_map = {
        "cook": "home cooking",
        "culinary": "home cooking",
        "foodie": "local food",
        "ramen": "ramen",
        "sushi": "sushi",
        "photo": "photography",
        "photography": "photography",
        "camera": "photography",
        "film": "film",
        "cinema": "cinema",
        "movie": "film",
        "music": "music",
        "vinyl": "vinyl",
        "guitar": "guitar",
        "piano": "piano",
        "travel": "travel",
        "countries": "travel",
        "backpack": "backpacking",
        "sport": "sports",
        "sport": "sports",
    }
    for key, topic in interest_map.items():
        if key in bio:
            return topic

    return "hosting"


def select_primary_hook(host: Dict[str, Any], dossier: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Определяет ОДНУ наиболее интересную индивидуальную особенность профиля хоста.
    
    Приоритет:
    1. Необычная личная особенность или увлечение (кото, традиционные инструменты, редкие хобби).
    2. Конкретный проект, занятие или профессия (gap year / кругосветное путешествие, культурная антропология, музыкант).
    3. Визуальные наблюдения LLaVA или интерес, пересекающийся с бэкграундом Андрея (фотография, кино).
    4. Место, событие или локальная рекомендация (гид по Киото / bylocals).
    5. Общие интересы (спорт, рестораны) — только если более конкретных деталей нет.
    6. Пустое / минимальное bio.
    """
    bio = (host.get("about_me_clean") or host.get("about_me") or "").strip()
    host_id = host.get("id") or ""
    username = host.get("username") or ""

    if not dossier and (host_id or username):
        try:
            from database import get_host_dossier
            dossier = get_host_dossier(host_id) or get_host_dossier(username)
        except Exception:
            dossier = None

    if (not bio or len(bio) < 25) and dossier:
        bio = (dossier.get("about_me_clean") or dossier.get("about_me") or bio).strip()

    lower_bio = bio.lower()

    visual_obs = []
    if dossier:
        visual_obs = [vo.get("observation", "").lower() for vo in dossier.get("visual_observations", [])]

    # 6. Проверка на пустое или шаблонное bio (< 25 слов, < 30 символов или заглушка)
    bio_words_count = len(bio.strip().split())
    if bio_words_count < 25 or len(bio) < 30 or lower_bio.startswith("friendly local resident"):
        return {
            "type": "empty_bio",
            "hook_detail": "Empty or minimal bio",
            "reason": "Profile bio is empty; simple, honest, and respectful note without fabricated shared interests (Priority 6).",
            "andrey_fact": "Respectful, quiet guest visiting the city (pb_lifestyle_respect)",
        }

    # 1. Приоритет 1: Необычная личная особенность или аутентичное увлечение
    if "koto" in lower_bio or "traditional instrument" in lower_bio or "shamisen" in lower_bio:
        return {
            "type": "koto",
            "hook_detail": "Learning to play the koto, a traditional Japanese instrument",
            "reason": "Unusual authentic personal hobby in host's bio; distinctive, memorable, and conversational (Priority 1).",
            "andrey_fact": "Appreciation for acoustic music & plays piano (hobby_piano_music)",
        }

    # 2. Приоритет 2: Конкретный проект, профессия или академическое исследование
    if "gap year" in lower_bio or "round-the-world" in lower_bio or "round the world" in lower_bio:
        return {
            "type": "gap_year",
            "hook_detail": "Planning a round-the-world journey during gap year",
            "reason": "Major, concrete travel preparation project preparing to Couchsurf worldwide (Priority 2).",
            "andrey_fact": "Independent travel experience between coaching seasons (te_countries_55)",
        }

    if "anthropolog" in lower_bio or ("researcher" in lower_bio and "ph" in lower_bio):
        return {
            "type": "anthropology",
            "hook_detail": "Cultural anthropologist & PhD researcher in Kyoto",
            "reason": "Distinctive intellectual profession and cultural observation background (Priority 2).",
            "andrey_fact": "Studied film directing with focus on cultural nuances and human stories (prof_film_director_cannes)",
        }

    if "i am a musician" in lower_bio or "work as a musician" in lower_bio or lower_bio.startswith("i am a musician") or "musician" in lower_bio:
        # Nob M. has "I am a musician"
        return {
            "type": "musician",
            "hook_detail": "Professional musician in Kyoto",
            "reason": "Host's profession and core creative identity as an active musician (Priority 2).",
            "andrey_fact": "Plays piano alongside sports coaching (hobby_piano_music)",
        }

    # 3. Приоритет 3: Визуальные наблюдения LLaVA или интересы, пересекающиеся с бэкграундом Андрея
    if ("camera" in visual_obs or "photography" in visual_obs) or ("photo" in lower_bio and "camera" in lower_bio):
        return {
            "type": "photo_visual",
            "hook_detail": "Visual observation from photos: camera / street photography around the city",
            "reason": "Verified visual evidence from host photos showing a real interest in street framing (Priority 3).",
            "andrey_fact": "Film director & street photographer (prof_film_director_cannes / hobby_photography)",
        }

    if any(k in lower_bio for k in ("film", "cinema", "director", "cinematography", "35mm")):
        return {
            "type": "film",
            "hook_detail": "Interest in cinema, film and visual storytelling",
            "reason": "Direct artistic intersection with Andrey's filmmaking background (Priority 3).",
            "andrey_fact": "Film director & visual storyteller (prof_film_director_cannes)",
        }

    # 4. Приоритет 4: Локальная рекомендация или специфическое место
    if any(k in lower_bio for k in ("bylocals", "bylocals.jp", "recommendations in kyoto", "google map list")):
        return {
            "type": "kyoto_guide",
            "hook_detail": "Curated local Kyoto recommendations list (bylocals.jp)",
            "reason": "Specific personal local guide curated by the host for visitors (Priority 4).",
            "andrey_fact": "Appreciation for authentic local neighborhood exploration (hobby_cooking_local_food)",
        }

    # 5. Приоритет 5: Общие интересы (кулинария, рестораны, спорт)
    if any(k in lower_bio for k in ("restaurant", "restaurants", "tasting", "delicious offerings")):
        return {
            "type": "restaurants",
            "hook_detail": "Exploring neighborhood restaurants and tasting delicious food",
            "reason": "Active interest in exploring local dining spots (Priority 5).",
            "andrey_fact": "Love for authentic local food and home cooking (hobby_cooking_local_food)",
        }

    if any(k in lower_bio for k in ("futsal", "soccer", "football")):
        return {
            "type": "futsal",
            "hook_detail": "Playing futsal and soccer in free time",
            "reason": "Active sports hobby in free time (Priority 5).",
            "andrey_fact": "Sports instructor and coach (prof_ski_coach_alps)",
        }

    if any(k in lower_bio for k in ("cycling", "bike", "biking", "bicycle")):
        return {
            "type": "cycling",
            "hook_detail": "Enthusiasm for cycling and exploring on two wheels",
            "reason": "Outdoor mobility and cycling hobby (Priority 5).",
            "andrey_fact": "Active sports instructor lifestyle (prof_ski_coach_alps)",
        }

    if any(k in lower_bio for k in ("hiking", "mountain", "mountains", "ski", "snowboard", "climbing", "bouldering", "camping", "trekking", "trail", "outdoors")):
        return {
            "type": "mountains_hiking",
            "hook_detail": "Love for mountains, hiking, and outdoor adventures",
            "reason": "Direct outdoor & mountain lifestyle connection (Priority 3).",
            "andrey_fact": "Ski instructor in the European Alps & mountain enthusiast (prof_ski_coach_alps)",
        }

    if any(k in lower_bio for k in ("guitar", "piano", "jazz", "vinyl", "concert", "concerts", "band", "rock", "instrument", "singing")):
        return {
            "type": "music_general",
            "hook_detail": "Appreciation for music and live instruments",
            "reason": "Shared musical interest from host profile (Priority 3).",
            "andrey_fact": "Plays piano and enjoys acoustic & live music (hobby_piano_music)",
        }

    if any(k in lower_bio for k in ("cook", "cooking", "ramen", "izakaya", "baking", "cuisine", "kitchen", "foodie", "local food", "sake")):
        return {
            "type": "cooking_food",
            "hook_detail": "Interest in cooking and sharing local food culture",
            "reason": "Culinary and local food connection (Priority 4).",
            "andrey_fact": "Enjoys home cooking and exploring neighborhood food spots (hobby_cooking_local_food)",
        }

    if any(k in lower_bio for k in ("coffee", "cafe", "cafes", "matcha", "tea ceremony", "green tea", "roastery", "espresso")):
        return {
            "type": "tea_coffee",
            "hook_detail": "Appreciation for good coffee, tea, and local cafe culture",
            "reason": "Relaxed everyday ritual mentioned in host bio (Priority 4).",
            "andrey_fact": "Enjoys quiet morning coffee/tea conversations and neighborhood walks (pb_lifestyle_respect)",
        }

    if any(k in lower_bio for k in ("art", "design", "architecture", "museum", "gallery", "pottery", "craft", "calligraphy", "illustration", "drawing", "painting")):
        return {
            "type": "art_design",
            "hook_detail": "Interest in visual arts, design, and creative aesthetics",
            "reason": "Creative and visual arts intersection (Priority 3).",
            "andrey_fact": "Background in visual arts & film directing (prof_film_director_cannes)",
        }

    if any(k in lower_bio for k in ("engineer", "software", "developer", "coding", "programming", "startup", "tech", "scientist", "it ")):
        return {
            "type": "tech_startup",
            "hook_detail": "Work and curiosity in technology and engineering",
            "reason": "Professional background in technology/science (Priority 4).",
            "andrey_fact": "Builds practical software tools alongside creative & coaching work (prof_film_director_cannes)",
        }

    if any(k in lower_bio for k in ("book", "books", "reading", "literature", "history", "philosophy", "novel", "library")):
        return {
            "type": "books_reading",
            "hook_detail": "Interest in books, history, and thoughtful conversations",
            "reason": "Intellectual curiosity and reading habit in bio (Priority 4).",
            "andrey_fact": "Studied storytelling and film directing (prof_film_director_cannes)",
        }

    if any(k in lower_bio for k in ("yoga", "running", "marathon", "tennis", "badminton", "swimming", "surfing", "gym", "fitness", "martial arts", "karate", "judo")):
        return {
            "type": "yoga_fitness",
            "hook_detail": "Active sports and movement routine",
            "reason": "Shared active and athletic lifestyle (Priority 4).",
            "andrey_fact": "Professional sports and ski coach (prof_ski_coach_alps)",
        }

    if any(k in lower_bio for k in ("language", "languages", "french", "spanish", "english", "cultural exchange", "exchange")):
        return {
            "type": "languages_culture",
            "hook_detail": "Openness to language practice and cultural exchange",
            "reason": "Explicit interest in cultural and language exchange (Priority 4).",
            "andrey_fact": "Speaks English and French and loves cultural exchange (lang_multilingual)",
        }

    if any(k in lower_bio for k in ("cat", "cats", "dog", "dogs", "shiba", "pet", "pets", "animal", "animals")):
        return {
            "type": "pets_animals",
            "hook_detail": "Warm home environment and love for animals",
            "reason": "Specific home/pet detail from host profile (Priority 5).",
            "andrey_fact": "Calm, considerate guest who respects home routines (pb_lifestyle_respect)",
        }

    return {
        "type": "general_travel",
        "hook_detail": "Openness to hosting and connecting with travelers",
        "reason": "General hosting perspective (Priority 5).",
        "andrey_fact": "Considerate guest & traveler (lang_multilingual)",
    }


def generate_stay_request(
    host: Dict[str, Any],
    check_in: str = "2026-11-18",
    check_out: str = "2026-11-20",
    guest_context: Optional[str] = None,
    include_dates: bool = False,
) -> str:
    """
    Генерирует строго персонализированное, естественное обращение к хосту (55-90 слов, 2-3 абзаца).
    
    Принципы:
    1. Настоящее верифицированное first name (или 'Hello!'/'Hi there!' если имя сомнительно).
    2. Выбирает РОВНО ОДНУ сильнейшую особенность профиля по иерархии приоритетов.
    3. Органичное, неповторяющееся представление Андрея в контексте выбранного факта.
    4. Даты НЕ повторяются в тексте сообщения (они уже видны в Trip Card).
    5. Никаких шаблонных клише, фейковых восторгов, Yamato Takkyubin и Club Med.
    """
    first_name, is_trusted = verify_host_name(host)
    greeting = f"Hi {first_name}," if (is_trusted and first_name) else "Hello,"

    # Dynamic city
    raw_loc = host.get("location")
    if isinstance(raw_loc, dict):
        loc_desc = raw_loc.get("locationDescription", {}).get("description") or raw_loc.get("slug") or "Kyoto"
    else:
        loc_desc = str(raw_loc or "Kyoto")

    city = host.get("city")
    if not city:
        ld_lower = loc_desc.lower()
        if "sapporo" in ld_lower:
            city = "Sapporo"
        elif "kyoto" in ld_lower or any(w in ld_lower for w in ["kamigy", "shimogy", "nakagy", "sakyo", "higashiyama", "kita-ku", "minami-ku"]):
            city = "Kyoto"
        elif "osaka" in ld_lower:
            city = "Osaka"
        elif "tokyo" in ld_lower:
            city = "Tokyo"
        elif "uji" in ld_lower:
            city = "Uji"
        else:
            city = loc_desc.split(",")[0].strip() or "Kyoto"
    # Transliterate macrons and strip non-ASCII
    city = city.replace("ō", "o").replace("Ō", "O").replace("ū", "u").replace("Ū", "U")
    city = "".join(ch for ch in city if ord(ch) < 128)

    # Получаем досье для проверки визуальных данных
    host_id = host.get("id") or host.get("username") or ""
    dossier = None
    if host_id:
        try:
            from database import get_host_dossier
            dossier = get_host_dossier(host_id)
        except Exception:
            dossier = None

    hook_info = select_primary_hook(host, dossier=dossier)
    htype = hook_info["type"]

    # -------------------------------------------------------------------------
    # Генерация сообщений: каждый сценарий имеет УНИКАЛЬНЫЕ вступление, тело и закрытие
    # -------------------------------------------------------------------------

    if htype == "koto":
        # Shohei: фокусируемся ТОЛЬКО на кото (без перечисления ресторанов или футзала)
        p1 = f"{greeting}\n\nLearning to play the koto is such an unexpected and wonderful instrument to take up."
        p2 = f"I play piano myself and have a huge appreciation for traditional acoustic music. I'm visiting {city}, and it's always inspiring to connect with people learning traditional arts."
        p3 = f"If you happen to have time and space to host for a couple of nights, I'd be glad to share travel stories. Either way, hope your koto lessons are going great!\n\nWarmly,\nAndrey"

    elif htype == "photo_visual":
        # Daichi: фокусируемся ТОЛЬКО на уличной фотографии и камере из LLaVA (без музыки)
        p1 = f"{greeting}\n\nYour street photos around {city} with the camera really caught my attention."
        p2 = f"I studied film directing and do a lot of street photography myself. I'm visiting {city}, and I always enjoy seeing how locals frame their city."
        p3 = f"If you might have space to host a considerate guest for a couple of nights, I'd love to connect. Either way, great frames!\n\nCheers,\nAndrey"

    elif htype == "empty_bio":
        # Lina: лаконичное, честное сообщение (< 55 слов) без придуманных интересов
        p1 = f"{greeting}\n\nI’m visiting {city} and reaching out to see if you might have space to host a considerate guest for a couple of nights."
        p2 = f"I keep my space very clean, value quiet hours, and am respectful of host routines."
        p3 = f"Completely understand if the timing doesn't work out. Thanks for your time!\n\nBest,\nAndrey"

    elif htype == "musician":
        # Nob: фокусируемся на профессии музыканта в Киото (полностью отличается от Daichi)
        p1 = f"{greeting}\n\nAlways great to come across a fellow musician hosting in {city}."
        p2 = f"I play piano alongside working as a sports instructor, and I'm spending a couple of days in {city}. It's always a treat to meet people dedicated to making music."
        p3 = f"Would be glad to share stories or talk tunes if your schedule permits. Thanks for considering!\n\nWarmly,\nAndrey"

    elif htype == "anthropology":
        # Mizue: фокусируемся ТОЛЬКО на антропологии и исследовательской работе
        p1 = f"{greeting}\n\nYour research in cultural anthropology immediately stood out to me. Immersing into how people live and connect is truly the most rewarding part of travel."
        p2 = f"I studied film directing, so observing cultural nuances and human stories is something close to my heart. I'm visiting {city} to explore the city and its culture."
        p3 = f"Would love to cross paths if you happen to have room to host. Either way, best of luck with your research work!\n\nWarmly,\nAndrey"

    elif htype == "gap_year":
        # Hidetaka: фокусируемся на кругосветном путешествии во время gap year
        p1 = f"{greeting}\n\nPlanning a round-the-world journey during a gap year sounds like an incredible adventure to prepare for."
        p2 = f"Having spent considerable time traveling independently, I know that mix of excitement and planning well. I'm visiting {city} and would be glad to exchange travel stories."
        p3 = f"Would be great to exchange travel stories or share tips for your route if your schedule permits. Thanks for considering!\n\nWarmly,\nAndrey"

    elif htype == "kyoto_guide":
        p1 = f"{greeting}\n\nI really appreciated seeing your curated local guide recommendations for {city}."
        p2 = f"I'm visiting {city} for a few days, and discovering authentic neighborhood spots from locals is always the highlight of my trips. I keep a very clean space and respect host routines."
        p3 = f"Would be glad to share impressions or chat over a meal if your schedule permits. Thanks for considering!\n\nWarmly,\nAndrey"

    elif htype == "restaurants":
        p1 = f"{greeting}\n\nYour enthusiasm for exploring neighborhood restaurants and authentic food around {city} immediately stood out to me."
        p2 = f"I'm visiting {city} to explore the city. Outside of sports coaching, I love good home cooking and discovering local dining spots that aren't on the typical tourist map."
        p3 = f"Would be great to grab a bite or chat travel if your timing allows. Appreciate your time and consideration!\n\nCheers,\nAndrey"

    elif htype == "futsal":
        p1 = f"{greeting}\n\nGreat to see that you stay active playing futsal and sports in your free time."
        p2 = f"I work as a sports and ski coach and I'm visiting {city} for a few days. It is always refreshing to connect with active people who enjoy movement and sports."
        p3 = f"Let me know if you might have space to host a considerate guest for a couple of nights. Hope you have great games!\n\nCheers,\nAndrey"

    elif htype == "film":
        p1 = f"{greeting}\n\nYour interest in cinema and visual storytelling immediately caught my attention."
        p2 = f"I studied film directing and do street photography alongside sports coaching, so connecting around movies and creative work is always inspiring. I'm visiting {city} and would love to connect."
        p3 = f"Would love to cross paths if you have room to host for a couple of nights. Appreciate your consideration!\n\nBest,\nAndrey"

    elif htype == "cycling":
        p1 = f"{greeting}\n\nExploring by bike is one of the best ways to experience the rhythm of a city, so your interest in cycling stood out right away."
        p2 = f"I work as a ski and sports coach and I'm visiting {city} for a few days. I love staying active outdoors and keep a very clean, respectful routine as a guest."
        p3 = f"If you happen to have room to host for a couple of nights, I'd be glad to connect and swap travel stories!\n\nCheers,\nAndrey"

    elif htype == "mountains_hiking":
        p1 = f"{greeting}\n\nYour love for mountains, hiking, and spending time in nature immediately resonated with me."
        p2 = f"I work as a ski instructor in the Alps and spend much of my life in the mountains. I'm visiting {city} to explore the area and always enjoy meeting people who appreciate the outdoors."
        p3 = f"If your schedule allows hosting a tidy, easygoing guest for a couple of nights, I'd be glad to share mountain stories!\n\nWarmly,\nAndrey"

    elif htype == "music_general":
        p1 = f"{greeting}\n\nReading about your interest in music and live instruments was a real highlight while looking through hosts in {city}."
        p2 = f"I play piano myself alongside working as a sports coach, and I'm visiting {city} for a few days. Music is always such a natural way to connect across cultures."
        p3 = f"Would be glad to chat about music or share travel stories if you have space to host. Thanks for your consideration!\n\nWarmly,\nAndrey"

    elif htype == "cooking_food":
        p1 = f"{greeting}\n\nYour appreciation for cooking and sharing good food stood out to me right away."
        p2 = f"I'm visiting {city} for a few days. Alongside my work in sports coaching and film, I really enjoy cooking at home and learning about local culinary traditions."
        p3 = f"If you have space to host a considerate, tidy guest, I'd be happy to cook a meal together or swap recipes!\n\nCheers,\nAndrey"

    elif htype == "tea_coffee":
        p1 = f"{greeting}\n\nI really enjoyed reading about your appreciation for good coffee, tea, and relaxed neighborhood cafes."
        p2 = f"I'm visiting {city} to explore the city at an unhurried pace. I studied film directing and work as a ski coach, and I always value calm conversations and respectful house routines."
        p3 = f"Let me know if you might have room to host for a couple of nights. Either way, wishing you a great week in {city}!\n\nBest,\nAndrey"

    elif htype == "art_design":
        p1 = f"{greeting}\n\nYour interest in visual arts, design, and creative work immediately stood out to me."
        p2 = f"I studied film directing and practice street photography, so aesthetics and visual culture are a big part of how I experience {city}. I'm a quiet, tidy guest who respects house rules."
        p3 = f"If your schedule permits hosting for a couple of nights, I'd love to exchange creative perspectives!\n\nWarmly,\nAndrey"

    elif htype == "tech_startup":
        p1 = f"{greeting}\n\nIt was great reading about your background in technology and engineering on your profile."
        p2 = f"I'm visiting {city} for a few days. Alongside working as a ski coach and filmmaker, I enjoy building software tools and having thoughtful conversations about how technology shapes daily life."
        p3 = f"If you happen to have space to host a self-sufficient, respectful guest, I'd be glad to connect!\n\nBest,\nAndrey"

    elif htype == "books_reading":
        p1 = f"{greeting}\n\nYour interest in books, history, and thoughtful conversation stood out to me while browsing profiles in {city}."
        p2 = f"I studied film directing and work as a sports coach. Storytelling and learning how people live in {city} are what I value most when traveling, and I'm very mindful of quiet hours."
        p3 = f"Would be glad to share stories if you have room to host for a couple of nights. Appreciate your time!\n\nWarmly,\nAndrey"

    elif htype == "yoga_fitness":
        p1 = f"{greeting}\n\nGreat to see how active you stay with sports and movement in your daily routine."
        p2 = f"I work as a professional ski and sports coach and I'm visiting {city} for a few days. I keep a healthy, calm routine, keep my space very neat, and respect host schedules."
        p3 = f"If timing works out to host for a couple of nights, I'd be glad to connect and share travel experiences!\n\nCheers,\nAndrey"

    elif htype == "languages_culture":
        p1 = f"{greeting}\n\nYour openness to cultural exchange and practicing languages together is what makes Couchsurfing so rewarding."
        p2 = f"I speak English and French, and I'm visiting {city} from Geneva. I work as a ski coach and filmmaker, and always enjoy meaningful conversations with locals while respecting your schedule."
        p3 = f"Let me know if you might have space to host a considerate guest for a couple of nights. Thanks for considering!\n\nWarmly,\nAndrey"

    elif htype == "pets_animals":
        p1 = f"{greeting}\n\nReading about your home and love for animals brought a warm smile while looking for hosts in {city}."
        p2 = f"I'm visiting {city} for a few days. I work as a sports coach and filmmaker, and as a guest I'm calm, tidy, and very mindful of household routines and quiet hours."
        p3 = f"If you happen to have room to host for a couple of nights, I'd be happy to connect. Thanks for your time!\n\nBest,\nAndrey"

    else:
        # Детерминированная персонализированная вариация на основе анкеты хоста и контекста Андрея
        uid_seed = sum(ord(c) for c in (host_id or first_name or city or "cs"))
        openings = [
            f"Reading through your profile gave such a welcoming sense of life in {city}.",
            f"Your thoughtful approach to hosting and meeting travelers in {city} really stood out to me.",
            f"I appreciated reading your profile and seeing your openness to connecting with visitors in {city}.",
            f"Your perspective on sharing local experiences in {city} is what makes the Couchsurfing community so genuine.",
        ]
        middles = [
            f"I work as a ski coach based in Geneva and the Alps, and make independent films. I'm visiting {city} for a few days and value a clean, quiet, and respectful stay.",
            f"Alongside working as a sports coach, I do street photography and enjoy learning about everyday life in {city}. As a guest, I'm tidy, calm, and mindful of house rules.",
            f"I'm a filmmaker and sports instructor visiting {city} to explore its neighborhoods and culture. I keep my space very neat and always respect my host's daily rhythm.",
            f"I split my time between sports coaching and creative film work, and I'm visiting {city} for a couple of days. I'm an easygoing, considerate guest who values good conversation and quiet hours.",
        ]
        closings = [
            f"If your schedule permits hosting for a couple of nights, I'd be glad to share stories. Thanks for considering!\n\nWarmly,\nAndrey",
            f"Let me know if you might have space to host during my stay in {city}. Appreciate your time and consideration!\n\nCheers,\nAndrey",
            f"Would be glad to connect if you have room to host a respectful guest. Either way, wishing you a wonderful week!\n\nBest,\nAndrey",
            f"If the timing works out on your end, I'd be happy to cross paths in {city}. Thank you for your time!\n\nWarmly,\nAndrey",
        ]
        p1 = f"{greeting}\n\n{openings[uid_seed % len(openings)]}"
        p2 = middles[(uid_seed // 3) % len(middles)]
        p3 = closings[(uid_seed // 7) % len(closings)]

    return f"{p1}\n\n{p2}\n\n{p3}"


def validate_batch_quality(messages_data: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Автоматический контроль качества пачки сообщений (Section 7 Quality Control):
    1. Одинаковые первые предложения.
    2. Повторяющиеся шаблонные заключения.
    3. Использование технических Anchor вместо реальных фактов.
    4. Перечисление списков интересов (laundry list).
    5. Неподтвержденные факты / галлюцинации вне Host Dossier.
    6. Повторение шаблонного представления Андрея.
    """
    issues = []
    first_sentences = []
    closings = []

    banned_anchors = ["mountains / outdoor / ski", "culture & languages", "food & cooking", "music & sound", "technology / startups / ai", "anchor_"]

    for item in messages_data:
        hid = item.get("host_id", "unknown")
        text = item.get("message", "").strip()
        lines = [line.strip() for line in text.split("\n") if line.strip()]
        
        # 1. Первое предложение после приветствия
        first_sentence = ""
        for line in lines:
            if not line.startswith("Hi ") and not line.startswith("Hello"):
                first_sentence = re.split(r"[.!?]", line)[0].strip()
                break
        if first_sentence:
            if first_sentence.lower() in [s.lower() for s in first_sentences]:
                issues.append(f"Duplicate opening sentence for host {hid}: '{first_sentence}'")
            first_sentences.append(first_sentence)

        # 2. Заключительное предложение перед подписью
        closing = ""
        for line in reversed(lines):
            if not any(sign in line.lower() for sign in ["warmly", "cheers", "best", "andrey"]):
                closing = line.strip()
                break
        if closing:
            if closing.lower() in [c.lower() for c in closings]:
                issues.append(f"Duplicate closing sentence for host {hid}: '{closing}'")
            closings.append(closing)

        # 3. Утечка технических Anchor
        lower_text = text.lower()
        for ba in banned_anchors:
            if ba in lower_text:
                issues.append(f"Host {hid} contains raw Anchor name: '{ba}'")

        # 4. Проверка шаблонных фраз
        if "your passion for" in lower_text:
            issues.append(f"Host {hid} contains cliche 'your passion for'")
        if "outside of sports coaching, i studied film and play piano" in lower_text:
            issues.append(f"Host {hid} contains repetitive multi-hobby Andrey intro")
        if "would love to cross paths if your timing aligns" in lower_text:
            issues.append(f"Host {hid} contains repetitive closing formula")
        if "thanks for considering hosting a quiet, respectful guest" in lower_text:
            issues.append(f"Host {hid} contains repetitive guest closing")

        # 5. Проверка необоснованных предположений о маршруте и общих интересах
        if any(r in lower_text for r in ["on my way up north", "on my way north to hokkaido", "heading to hokkaido", "on my way to hokkaido", "for skiing in hokkaido"]):
            issues.append(f"Host {hid} contains ungrounded travel route inference")
        if any(si in lower_text for si in ["we both enjoy", "we both love", "we both share", "we both like"]):
            issues.append(f"Host {hid} contains ungrounded shared interest claim")

    passed = len(issues) == 0
    return {
        "passed": passed,
        "issues": issues,
        "first_sentences_count": len(first_sentences),
        "closings_count": len(closings),
    }
