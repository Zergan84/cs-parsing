"""
core/enrich_kyoto_profiles.py - Скрипт интеллектуального обогащения существующих анкет хостов Киото.
Извлекает реальные показатели:
- языки (English, French, Japanese, etc.)
- интересы и увлечения (Лыжи, Горы, Фото, Кино, Музыка, Фортепиано)
- статус приема гостей (accepting vs not_accepting)
- домохозяйство (alone, family, flatmates)
- спецпослания в био (only 1 person, female only, etc.)
- точный скоринг: 1-5★ с прозрачными причинами, либо явное 'НЕТ ДАННЫХ'.
"""

import json
import logging
import re
import sqlite3
from pathlib import Path
from typing import Dict, List, Tuple

from core.collector import calculate_match_score
from core.database import get_connection

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("ProfileEnricher")

LANGUAGE_PATTERNS = [
    ("English", r"\b(english|англ)\b"),
    ("Japanese", r"\b(japanese|nihongo|японск)\b"),
    ("French", r"\b(french|français|francais|француз)\b"),
    ("Spanish", r"\b(spanish|español|espanol|испанск)\b"),
    ("German", r"\b(german|deutsch|немецк)\b"),
    ("Italian", r"\b(italian|italiano|итальянск)\b"),
    ("Chinese", r"\b(chinese|mandarin|китайск)\b"),
    ("Russian", r"\b(russian|русск)\b"),
    ("Korean", r"\b(korean|корейск)\b"),
    ("Portuguese", r"\b(portuguese|português|португальск)\b"),
]

INTEREST_KEYWORDS = {
    "Ski & Snowboard": ["ski", "skiing", "snowboard", "snowboarding", "slopes", "winter sports"],
    "Hiking & Mountains": ["hike", "hiking", "trekking", "mountains", "alps", "trail", "nature", "outdoor", "climbing"],
    "Cinema & Filmmaking": ["film", "cinema", "director", "filmmaking", "movie", "documentary", "screenplay"],
    "Photography": ["photo", "photography", "photographer", "camera", "analog", "35mm", "street photography"],
    "Music & Instruments": ["music", "piano", "pianist", "guitar", "jazz", "acoustic", "classical", "vinyl", "concert"],
    "Culinary & Food": ["cooking", "culinary", "foodie", "ramen", "sushi", "chef", "wine", "sake", "tasting"],
}


def extract_profile_indicators(about: str, current_interests: List[str], current_langs: List[str]) -> Dict:
    about_text = (about or "").strip()
    lower_about = about_text.lower()
    words = len(about_text.split())

    # 1. Языки
    langs = list(current_langs)
    for lang_name, pattern in LANGUAGE_PATTERNS:
        if lang_name not in langs and re.search(pattern, lower_about):
            langs.append(lang_name)

    # 2. Интересы
    interests = list(current_interests)
    for label, kws in INTEREST_KEYWORDS.items():
        if label not in interests and any(k in lower_about for k in kws):
            interests.append(label)

    # 3. Статус приема гостей
    is_hosting = "accepting"
    if any(k in lower_about for k in ["not hosting", "currently not hosting", "cannot host", "can't host", "unable to host", "taking a break from hosting"]):
        is_hosting = "not_accepting"

    # 4. Домохозяйство
    household = "unknown"
    if any(k in lower_about for k in ["live alone", "live by myself", "living alone", "my studio"]):
        household = "alone"
    elif any(k in lower_about for k in ["my wife", "my husband", "my partner", "my family", "with my family", "our daughter", "our son", "with my kids"]):
        household = "family"
    elif any(k in lower_about for k in ["roommate", "flatmate", "share house", "shared house", "flatmates"]):
        household = "flatmates"

    # 5. Спецпослания и ограничения
    notes = []
    if any(k in lower_about for k in ["only host 1", "only 1 person", "solo traveler only", "solo travelers only", "one person only"]):
        notes.append("Только 1 гость (соло)")
    if any(k in lower_about for k in ["female only", "women only", "only female"]):
        notes.append("Только для девушек")
    if any(k in lower_about for k in ["one night only", "max 2 nights", "maximum 2 nights", "1-2 nights"]):
        notes.append("Ограничение по сроку (1-2 ночи)")
    if is_hosting == "not_accepting":
        notes.append("В био: временно не принимает гостей")

    return {
        "words": words,
        "languages": langs,
        "interests": interests,
        "is_hosting": is_hosting,
        "household": household,
        "special_notes": "; ".join(notes) if notes else None,
    }


def run_enrichment(db_file: Path = Path("output/couchsurfing_platform.db")):
    logger.info("Запуск обогащения анкет хостов...")
    with get_connection(db_file) as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT host_id, trip_id, username, name, about, interests_json, languages_json, countries_json
            FROM core_hosts
        """)
        rows = cur.fetchall()

        updated_count = 0
        empty_count = 0
        scored_2_plus = 0

        for r in rows:
            host_id = r["host_id"]
            trip_id = r["trip_id"]
            about = r["about"] or ""
            current_interests = json.loads(r["interests_json"] or "[]")
            current_langs = json.loads(r["languages_json"] or "[]")
            countries = json.loads(r["countries_json"] or "[]")

            info = extract_profile_indicators(about, current_interests, current_langs)

            # Скоринг
            host_data = {
                "about": about,
                "interests": info["interests"],
                "languages": info["languages"],
                "countries_visited": countries,
            }
            score, reasons = calculate_match_score(host_data)

            if "НЕТ ДАННЫХ" in reasons[0]:
                empty_count += 1
            if score >= 2:
                scored_2_plus += 1

            cur.execute("""
                UPDATE core_hosts
                SET interests_json = ?,
                    languages_json = ?,
                    is_hosting = ?,
                    household = ?,
                    special_notes = ?,
                    score_stars = ?,
                    score_reasons_json = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE trip_id = ? AND host_id = ?
            """, (
                json.dumps(info["interests"], ensure_ascii=False),
                json.dumps(info["languages"], ensure_ascii=False),
                info["is_hosting"],
                info["household"],
                info["special_notes"],
                score,
                json.dumps(reasons, ensure_ascii=False),
                trip_id,
                host_id,
            ))
            updated_count += 1

    logger.info(f"Обогащение завершено! Обновлено: {updated_count} хостов.")
    logger.info(f"  - Хостов без данных (пустые анкеты, 1★ 'НЕТ ДАННЫХ'): {empty_count}")
    logger.info(f"  - Хостов с высокими совпадениями (2★-5★): {scored_2_plus}")


if __name__ == "__main__":
    run_enrichment()
