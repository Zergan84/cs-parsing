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


from core.identity import verify_host_name


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
    """Compatibility entry point delegating to the canonical core generator."""
    from core.messaging import build_personalized_message

    trip = {
        "city": host.get("city") or str(host.get("location") or "Kyoto").split(",")[0].strip(),
        "arrival_date": check_in if include_dates else "",
        "departure_date": check_out if include_dates else "",
    }
    message, _, _ = build_personalized_message(host, trip)
    return message

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
