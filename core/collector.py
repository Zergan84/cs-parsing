"""
core/collector.py - Центральный модуль КОЛЛЕКТОР и Профильный Анализатор.

Объединяет:
1. `CollectorEngine` и `calculate_match_score` — сохранение сырых профилей в SQLite (`core_hosts`, `core_host_snapshots`, `hosts`, `trip_hosts`), дедупликация по `(trip_id, host_id / username)`, скоринг 1-5★.
2. `ProfileAnalyzer` (`get_profile_analyzer`) — глубокий анализ биографии, разговорных хуков и сохранение в `profile_analyses`.
"""

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.context import GUEST, get_personal_context_manager
from core.database import db_session, get_connection, log_audit
from core.host_activity import RECENT, parse_activity
from core.host_policy import evaluate_host_eligibility
from core.models import HostCard, HostState

logger = logging.getLogger("Collector")


def _activity_from_host(host_data: Dict[str, Any]) -> Dict[str, Any]:
    """Prefer normalized scraper evidence; only parse source API fields when needed."""
    status = str(host_data.get("recently_active_status") or "").upper()
    if status in {"RECENTLY_ACTIVE", "ACTIVE", "NOT_RECENTLY_ACTIVE", "UNKNOWN"}:
        return {
            "status": status,
            "raw": host_data.get("last_login_raw") or host_data.get("last_active_raw") or host_data.get("activity_raw"),
            "days": host_data.get("last_login_days"),
            "at": host_data.get("last_active_at"),
        }
    return parse_activity(
        is_recently_active=host_data.get("isRecentlyActive"),
        raw_value=host_data.get("last_login_raw") or host_data.get("last_active_raw") or host_data.get("lastLogin") or host_data.get("last_login"),
    )


# =====================================================================
# 1. MATCH SCORING (1–5 STARS)
# =====================================================================

def calculate_match_score(host_data: Dict[str, Any]) -> Tuple[int, List[str]]:
    """
    Рассчитывает скоринг соответствия профиля хоста контексту гостя (1-5 звезд)
    и формирует список причин оценки.
    """
    if isinstance(host_data, HostCard):
        host_data = host_data.model_dump()

    score = 1
    reasons = []

    about = str(host_data.get("about") or host_data.get("about_me") or "").lower()
    interests = [str(x).lower() for x in (host_data.get("interests") or [])]
    languages = [str(x).lower() for x in (host_data.get("languages") or [])]
    countries = [str(x).lower() for x in (host_data.get("countries_visited") or [])]

    reference_data = host_data.get("reference_text") or host_data.get("references") or host_data.get("reviews") or []
    if isinstance(reference_data, list):
        reference_text = " ".join(
            str(item.get("text") or item.get("body") or item.get("content") or "")
            if isinstance(item, dict) else str(item)
            for item in reference_data
        )
    else:
        reference_text = str(reference_data or "")
    supplemental = " ".join(str(host_data.get(key) or "") for key in (
        "shared_information", "why_on_couchsurfing", "personal_description",
        "additional_information", "reference_text",
    )).lower() + " " + reference_text.lower()
    full_text = " ".join([about, supplemental] + interests + countries)

    # Горы / Лыжи / Outdoor
    outdoor_matches = [k for k in GUEST.interests["outdoors"] if k in full_text]
    if outdoor_matches:
        score += 1
        reasons.append(f"Горы / Лыжи / Экстрим ({', '.join(outdoor_matches[:2])})")

    # Кино / Фотография / Искусство
    art_matches = [k for k in GUEST.interests["arts_media"] if k in full_text]
    if art_matches:
        score += 1
        reasons.append(f"Кино / Фото / Арт ({', '.join(art_matches[:2])})")

    # Музыка / Пианино
    music_matches = [k for k in GUEST.interests["music"] if k in full_text]
    if music_matches:
        score += 1
        reasons.append(f"Музыка / Классика / Фортепиано ({', '.join(music_matches[:2])})")

    # Языковая совместимость (Французский = особый плюс для Женевы)
    french_found = any("french" in l or "français" in l for l in languages) or "french" in full_text
    if french_found:
        if "Французский язык" not in reasons:
            score += 1
            reasons.append("Владеет французским языком")

    # Проверка наполненности анкеты
    activity = _activity_from_host(host_data)
    if activity["status"] == RECENT:
        return 5, ["RECENTLY ACTIVE — top priority (5★)"] + reasons

    has_meaningful_data = bool(interests or languages or (about and len(about.split()) >= 5))
    if not has_meaningful_data:
        return 1, ["НЕТ ДАННЫХ (анкета не заполнена или отсутствует информация)"]

    score = min(5, max(1, score))
    if not reasons:
        reasons.append("Общий интерес к путешествиям (нет специфических совпадений)")

    return score, reasons


# =====================================================================
# 2. COLLECTOR ENGINE
# =====================================================================

class CollectorEngine:
    """Движок коллектора данных."""

    def __init__(self, trip_id: str, db_file: Optional[Path] = None):
        self.trip_id = trip_id
        self.db_file = db_file

    def ingest_raw_host(self, raw_host: Dict[str, Any]) -> Optional[HostCard]:
        """
        Принимает сырой профиль из поиска:
        1. Проверяет дубликаты.
        2. Считает скоринг (1-5★).
        3. Сохраняет снимок и карточку в SQLite (`core_hosts`, `core_host_snapshots`, `hosts`, `trip_hosts`).
        """
        host_id = str(raw_host.get("id") or raw_host.get("public_address") or raw_host.get("username") or "").strip()
        username = str(raw_host.get("username") or raw_host.get("public_address") or host_id).strip()
        if not host_id or not username:
            return None

        now_iso = datetime.now().isoformat()

        with get_connection(self.db_file) as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT state, score_stars FROM core_hosts WHERE trip_id = ? AND (host_id = ? OR username = ?)",
                (self.trip_id, host_id, username),
            )
            existing = cur.fetchone()
            if existing:
                refreshed = _activity_from_host(raw_host)
                if refreshed["status"] != "UNKNOWN":
                    checked_at = raw_host.get("activity_checked_at") or now_iso
                    source = raw_host.get("activity_source") or "couchsurfing_api"
                    cur.execute(
                        """UPDATE core_hosts SET recently_active_status=?, last_active_raw=?, last_login_days=?,
                                  last_active_at=?, activity_checked_at=?, activity_source=?,
                                  priority=CASE WHEN ?='RECENTLY_ACTIVE' THEN MAX(COALESCE(priority,10),1000) ELSE priority END,
                                  score_stars=CASE WHEN ?='RECENTLY_ACTIVE' THEN 5 ELSE score_stars END,
                                  updated_at=? WHERE trip_id=? AND (host_id=? OR username=?) AND state!='sent'""",
                        (refreshed["status"], refreshed["raw"], refreshed["days"], refreshed["at"],
                         checked_at, source, refreshed["status"], refreshed["status"], now_iso,
                         self.trip_id, host_id, username),
                    )
                    conn.execute(
                        """INSERT INTO core_host_snapshots(host_id,trip_id,raw_data_json,captured_at) VALUES(?,?,?,?)
                           ON CONFLICT(trip_id,host_id) DO UPDATE SET raw_data_json=excluded.raw_data_json,
                             captured_at=excluded.captured_at""",
                        (host_id, self.trip_id, json.dumps(raw_host, ensure_ascii=False), now_iso),
                    )
                return None

        activity = _activity_from_host(raw_host)
        score_stars, score_reasons = calculate_match_score(raw_host)

        name = (raw_host.get("name") or raw_host.get("first_name") or username).strip()
        location = (raw_host.get("location") or raw_host.get("city") or "").strip()

        profile_api = raw_host.get("profile_api_data") if isinstance(raw_host.get("profile_api_data"), dict) else {}
        policy_host = {**profile_api, **raw_host}
        eligibility = evaluate_host_eligibility(
            policy_host,
            arrival_date=raw_host.get("arrival_date") or raw_host.get("trip_arrival"),
            departure_date=raw_host.get("departure_date") or raw_host.get("trip_departure"),
            target_city=raw_host.get("target_city") or raw_host.get("city"),
            guest_gender=raw_host.get("guest_gender") or "male",
        )
        exclusion_reason = raw_host.get("eligibility_reason") or eligibility.get("reason")
        init_state = HostState.DISCOVERED
        if raw_host.get("is_hosting") == "not_accepting" or exclusion_reason:
            init_state = HostState.SKIPPED
            score_reasons.append(exclusion_reason or "Не принимает гостей / в поездке")

        verified_value = raw_host.get("is_verified")
        if verified_value is None:
            verified_value = raw_host.get("isIdentificationVerified")
        verified_text = str(verified_value).strip().lower()
        if verified_value is True or verified_text in {"true", "1", "yes", "verified"}:
            verification_status = "VERIFIED"
        elif verified_value is False or verified_text in {"false", "0", "no", "not verified", "not_verified", "unverified"}:
            verification_status = "NOT_VERIFIED"
        else:
            verification_status = "UNKNOWN"
        is_verified = verification_status == "VERIFIED"

        card = HostCard(
            host_id=host_id,
            username=username,
            trip_id=self.trip_id,
            name=name,
            location=location,
            gender=raw_host.get("gender"),
            age=raw_host.get("age"),
            occupation=raw_host.get("occupation"),
            about=raw_host.get("about") or raw_host.get("about_me"),
            interests=raw_host.get("interests") or [],
            languages=raw_host.get("languages") or [],
            countries_visited=raw_host.get("countries_visited") or [],
            countries_lived=raw_host.get("countries_lived") or [],
            profile_url=raw_host.get("profile_url") or f"https://www.couchsurfing.com/c/users/{username}",
            avatar_url=raw_host.get("avatar_url") or raw_host.get("profile_photo_url"),
            is_verified=is_verified,
            verification_status=verification_status,
            recently_active_status=activity["status"],
            last_active_raw=activity["raw"],
            last_login_days=activity["days"],
            last_active_at=activity["at"],
            birth_country=raw_host.get("birth_country"),
            residence_country=raw_host.get("residence_country"),
            is_hosting=raw_host.get("is_hosting", "unknown"),
            household=raw_host.get("household"),
            special_notes=raw_host.get("special_notes"),
            score_stars=score_stars,
            score_reasons=score_reasons,
            state=init_state,
            created_at=now_iso,
            updated_at=now_iso,
        )

        with get_connection(self.db_file) as conn:
            cur = conn.cursor()
            # 1. Сохранение сырого снимка
            conn.execute(
                """
                INSERT OR REPLACE INTO core_host_snapshots (host_id, trip_id, raw_data_json, captured_at)
                VALUES (?, ?, ?, ?);
                """,
                (host_id, self.trip_id, json.dumps(raw_host, ensure_ascii=False), now_iso),
            )
            # 2. Сохранение карточки в core_hosts
            conn.execute(
                """
                INSERT INTO core_hosts (
                    host_id, trip_id, username, name, location, gender, age, occupation,
                    about, interests_json, languages_json, countries_json, profile_url,
                    avatar_url, is_verified, verification_status, recently_active_status, last_active_raw,
                    last_login_days, last_active_at, activity_checked_at, activity_source,
                    eligibility_reason, eligibility_evidence_json, eligibility_checked_at,
                    birth_country, residence_country, is_hosting,
                    household, special_notes, countries_lived_json,
                    score_stars, score_reasons_json, state, priority, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    card.host_id, card.trip_id, card.username, card.name, card.location,
                    card.gender, card.age, card.occupation, card.about,
                    json.dumps(card.interests, ensure_ascii=False),
                    json.dumps(card.languages, ensure_ascii=False),
                    json.dumps(card.countries_visited, ensure_ascii=False),
                    card.profile_url, card.avatar_url, 1 if card.is_verified else 0,
                    card.verification_status,
                    card.recently_active_status, card.last_active_raw,
                    card.last_login_days, card.last_active_at,
                    raw_host.get("activity_checked_at") or now_iso,
                    raw_host.get("activity_source") or ("couchsurfing_api" if "isRecentlyActive" in raw_host or "lastLogin" in raw_host else "normalized_profile"),
                    exclusion_reason,
                    json.dumps(raw_host.get("eligibility_evidence") or ({"source": eligibility.get("source"), "excerpt": eligibility.get("excerpt")} if exclusion_reason else {}), ensure_ascii=False),
                    now_iso,
                    card.birth_country, card.residence_country, card.is_hosting,
                    card.household, card.special_notes,
                    json.dumps(card.countries_lived, ensure_ascii=False),
                    card.score_stars, json.dumps(card.score_reasons, ensure_ascii=False),
                    card.state.value, 1000 if activity["status"] == RECENT else 10,
                    card.created_at, card.updated_at,
                ),
            )

        log_audit(
            trip_id=self.trip_id,
            module="COLLECTOR",
            event="HOST_INGESTED",
            host_id=host_id,
            details={"username": username, "score_stars": score_stars, "reasons": score_reasons},
            db_file=self.db_file,
        )
        return card

    def ingest_batch(self, raw_hosts: List[Dict[str, Any]]) -> Dict[str, int]:
        """Пакетный прием списка хостов."""
        added = 0
        skipped = 0
        for h in raw_hosts:
            card = self.ingest_raw_host(h)
            if card:
                added += 1
            else:
                skipped += 1
        return {"added": added, "skipped": skipped, "total": len(raw_hosts)}

    # Алиас для обратной совместимости
    ingest_raw_hosts = ingest_batch


# =====================================================================
# 3. PROFILE ANALYZER
# =====================================================================

class ProfileAnalyzer:
    """Интеллектуальный анализатор профилей Couchsurfing."""

    def __init__(self):
        from gender_classifier import get_gender_classifier
        from hosting_guard import get_hosting_guard
        self.guard = get_hosting_guard()
        self.gender_classifier = get_gender_classifier()
        self.context_mgr = get_personal_context_manager()

    def extract_host_profile_details(self, host: Dict[str, Any]) -> Dict[str, Any]:
        bio = (host.get("about_me_clean") or host.get("about_me") or host.get("about") or "").strip()
        lower_about = bio.lower()

        detected_interests = []
        interest_patterns = [
            ("Board games & tabletop", ["board game", "boardgame", "catan", "chess", "tabletop"]),
            ("Education & science teaching", ["science teacher", "teacher", "education", "physics", "biology", "chemistry", "researcher", "phd", "academic", "anthropolog", "student"]),
            ("Film, directing & cinema", ["film", "cinema", "director", "cinematography", "movie", "35mm", "screenplay", "documentary"]),
            ("Photography & analog cameras", ["photo", "photography", "photographer", "camera", "analog", "35mm", "street photography", "portrait"]),
            ("Music, vinyl & instruments", ["vinyl", "record", "piano", "guitar", "jazz", "band", "acoustic", "synthesizer", "koto", "instrument", "instruments", "music", "musician", "shamisen", "singing", "drums"]),
            ("Culinary culture & restaurants", ["cook", "cooking", "culinary", "foodie", "ramen", "sushi", "baking", "wine", "sake", "chef", "restaurant", "restaurants", "tasting", "delicious", "sweets", "tictac", "dining", "delicacies", "coffee", "tea"]),
            ("Reading & literature", ["reading", "books", "literature", "novels", "writing", "author"]),
            ("Sports, futsal & cycling", ["cycling", "badminton", "biking", "running", "marathon", "tennis", "sports", "sport", "soccer", "futsal", "football", "gym", "fitness", "climbing", "bouldering"]),
            ("Architecture & design", ["architecture", "interior design", "sketching", "drawing", "illustration", "art", "museum", "gallery"]),
            ("Trail hiking & nature", ["hike", "hiking", "trekking", "camping", "alps", "mountaineering", "climb", "mountain", "nature"]),
            ("Travel & cultural exchange", ["traveler", "backpacking", "cultural exchange", "countries", "nomad", "expats", "practicing english", "learning english", "learning japanese", "language exchange", "languages", "bylocals", "recommendations", "guide"]),
        ]

        for label, kws in interest_patterns:
            if any(k in lower_about for k in kws):
                detected_interests.append(label)

        if host.get("hobbies"):
            if isinstance(host["hobbies"], list):
                detected_interests.extend(host["hobbies"])
            elif isinstance(host["hobbies"], str) and host["hobbies"].strip():
                detected_interests.append(host["hobbies"].strip())

        occupation = host.get("occupation") or host.get("job")
        if not occupation:
            occ_match = re.search(
                r"(?:work as (?:a |an )?|i(?: am|'m) (?:a |an )?)([a-zA-Z\s]{3,35}?)(?:\.|,|and|\n|who)",
                bio,
                re.IGNORECASE,
            )
            if occ_match:
                cand = occ_match.group(1).strip()
                if not any(w in cand.lower() for w in ["year old", "couchsurfer", "guy", "person", "friendly", "living", "staying", "learning", "practicing", "playing", "planning", "trying"]):
                    occupation = cand

        languages = host.get("languages")
        if not languages:
            lang_matches = re.findall(
                r"\b(English|Japanese|French|Spanish|German|Italian|Chinese|Russian|Korean|Portuguese)\b",
                bio,
                re.IGNORECASE,
            )
            if lang_matches:
                languages = list(dict.fromkeys(l.capitalize() for l in lang_matches))

        return {
            "clean_bio": bio,
            "host_interests": detected_interests or ["International travel and cultural exchange"],
            "host_occupation": occupation or "Not specified",
            "host_languages": languages or ["English"],
        }

    def extract_conversation_hooks(self, host: Dict[str, Any]) -> List[str]:
        about = (host.get("about_me_clean") or host.get("about_me") or host.get("about") or "").strip()
        hooks: List[str] = []

        countries = re.findall(
            r"\b(?:France|Italy|Switzerland|Nepal|Norway|Japan|Spain|Germany|Iceland|Georgia|Morocco|Hong\s+Kong|Cuba|Afghanistan|Macedonia|Czech|Portugal|Brazil|Taiwan)\b",
            about,
            re.IGNORECASE,
        )
        if countries:
            unique_c = list(dict.fromkeys(c.capitalize() for c in countries))[:3]
            hooks.append(f"Travel experience in {', '.join(unique_c)}")

        lower_about = about.lower()
        if any(k in lower_about for k in ("board game", "boardgame", "catan", "chess")):
            hooks.append("Passion for board games and late-night strategy conversations")
        if any(k in lower_about for k in ("science teacher", "teaching", "education")):
            hooks.append("Experience as an educator and science teacher")
        if any(k in lower_about for k in ("anthropolog", "researcher (holder of a ph", "phd")):
            hooks.append("Cultural anthropology research and academic studies")
        if any(k in lower_about for k in ("gap year", "round-the-world")):
            hooks.append("Planning a round-the-world journey during gap year")
        if any(k in lower_about for k in ("badminton", "cycling", "bike", "biking")):
            hooks.append("Enthusiasm for cycling and badminton")
        if any(k in lower_about for k in ("futsal", "soccer", "football")):
            hooks.append("Playing futsal and soccer in free time")
        if any(k in lower_about for k in ("film", "cinema", "director", "movies", "cinematography")):
            hooks.append("Interest in film, cinematography and visual storytelling")
        if any(k in lower_about for k in ("photo", "photography", "camera", "analog", "35mm")):
            hooks.append("Passion for photography and capturing moments")
        if any(k in lower_about for k in ("koto", "shamisen", "traditional instrument")):
            hooks.append("Learning to play the koto, a traditional Japanese instrument")
        elif any(k in lower_about for k in ("piano", "music", "guitar", "ambient", "vinyl", "jazz", "musician")):
            hooks.append("Musical interests (piano / acoustic / vinyl)")
        if any(k in lower_about for k in ("restaurant", "restaurants", "tasting", "delicious offerings", "culinary")):
            hooks.append("Exploring local restaurants and tasting delicious food")
        elif any(k in lower_about for k in ("cook", "cooking", "ramen", "sushi", "chef", "wine", "sake", "foodie", "baking")):
            hooks.append("Love for authentic culinary culture and home cooking")
        if any(k in lower_about for k in ("tictac", "tic tac")):
            hooks.append("Affection for Tic Tac sweets")
        if any(k in lower_about for k in ("bylocals.jp", "bylocals", "recommendations in kyoto", "google map list")):
            hooks.append("Curated local Kyoto recommendations and favorite spots (bylocals.jp)")
        if any(k in lower_about for k in ("developer", "software", "tech", "coding", "ai", "machine learning")):
            hooks.append("Tech background and passion for software/AI development")
        if any(k in lower_about for k in ("ski", "snowboard", "powder")):
            hooks.append("Passion for winter sports and skiing/snowboarding")
        if any(k in lower_about for k in ("hike", "hiking", "trail", "climb", "mountain", "alps", "trekking")):
            hooks.append("Enthusiasm for mountains, trail hiking and alpine nature")
        if any(k in lower_about for k in ("motorcycle", "motorbike", "moto", "riding", "biker")):
            hooks.append("Love for motorcycles and road trips")
        if any(k in lower_about for k in ("practicing my english", "practicing english", "learning english")):
            hooks.append("Practicing English and open cross-cultural communication")
        elif any(k in lower_about for k in ("cultural exchange", "language exchange", "learn languages")):
            hooks.append("Focus on genuine peer-to-peer cultural and language exchange")

        countries_num = re.search(r"(\d{2,3})\s+(?:countries|prefectures)", lower_about)
        if countries_num:
            hooks.append(f"Explored {countries_num.group(1)} countries/prefectures")

        if not hooks:
            hooks.append("Thoughtful perspective on travel and hospitable hosting approach")

        return hooks

    def determine_strategy(
        self,
        host: Dict[str, Any],
        matching_facts: List[Dict[str, Any]],
        hooks: List[str],
    ) -> str:
        fact_ids = {f["fact_id"] for f in matching_facts}

        if "prof_ski_coach_alps" in fact_ids and any("ski" in h.lower() or "mountain" in h.lower() for h in hooks):
            return "Alpine & Winter Sports: Connect via Courchevel ski instruction base and passion for mountains."
        elif "te_nepal_mountains" in fact_ids:
            return "High-Altitude Trekker: Bond over high-altitude Himalayan trekking (Thorong La 5416m, Everest Base Camp) and outdoor endurance."
        elif "prof_film_director_cannes" in fact_ids:
            return "Cinematic & Visual Storyteller: Relate through film directing, Cannes 2024 participation, and visual creativity."
        elif "te_bmw_gs_moto" in fact_ids:
            return "Motorcycle & Alpine Roamer: Connect through BMW GS motorcycle journeys across the Alps and Morocco."
        elif "tech_fitness_ml_tools" in fact_ids:
            return "Tech Creator: Casual peer-to-peer connection over fitness/ML development and web tools."
        elif "hobby_cooking_local_food" in fact_ids:
            return "Culinary Exchange: Offer to cook a tasty meal together, sharing food culture and quiet travel stories."
        elif "te_hong_kong_asia" in fact_ids:
            return "Asian Connection: Connect over living in Hong Kong and appreciating Japanese cultural nuances."
        else:
            return "Grounded CS Veteran: Respectful 10+ year CS peer connection, quiet hours, neatness, zero-pressure evening catch-up."

    def analyze_host(
        self,
        host: Dict[str, Any],
        conn: Optional[Any] = None,
    ) -> Dict[str, Any]:
        host_id = host.get("id") or host.get("host_id") or host.get("username")
        username = host.get("username") or host_id
        name = host.get("clean_name") or host.get("name") or "Host"
        about_me = host.get("about_me_clean") or host.get("about_me") or host.get("about") or ""
        house_rules = host.get("house_rules") or "Standard rules"
        sleeping = host.get("sleeping_arrangement") or "Not specified"
        refs = host.get("references_count", 0)

        can_host, hosting_reason, hosting_conf = self.guard.verify_host(host, use_llm=False)
        est_gender, host_type, _ = self.gender_classifier.classify(
            name=name,
            about_text=about_me,
            username=username,
            references=refs,
        )
        hooks = self.extract_conversation_hooks(host)
        matching_facts = self.context_mgr.select_relevant_facts(host, max_facts=2)
        strategy = self.determine_strategy(host, matching_facts, hooks)

        has_secret = host.get("secret_code_detected", False)
        secret_code = host.get("secret_code_hint", "")

        analysis_result = {
            "host_id": host_id,
            "estimated_gender": est_gender,
            "host_type": host_type,
            "can_host_overnight": 1 if can_host else 0,
            "hosting_verdict_reason": hosting_reason,
            "hosting_confidence": hosting_conf,
            "sleeping_arrangement": sleeping,
            "house_rules": house_rules,
            "secret_code_detected": 1 if has_secret else 0,
            "secret_code_hint": secret_code,
            "conversation_hooks": hooks,
            "compatibility_points": [
                {
                    "fact_id": f["fact_id"],
                    "title": f["title"],
                    "content": f["content"],
                    "category": f["category"],
                }
                for f in matching_facts
            ],
            "conversation_strategy": strategy,
            "analyzed_at": datetime.now().isoformat(),
        }

        def _execute_save(active_conn):
            cur = active_conn.cursor()
            now_iso = datetime.now().isoformat()
            cur.execute(
                """
                UPDATE core_hosts
                SET name = COALESCE(?, name),
                    about = CASE WHEN length(?) > length(COALESCE(about, '')) THEN ? ELSE about END,
                    gender = COALESCE(?, gender),
                    house_rules = COALESCE(?, house_rules),
                    updated_at = ?
                WHERE (LOWER(username) = LOWER(?) OR LOWER(host_id) = LOWER(?));
                """,
                (name, about_me, about_me, est_gender, house_rules, now_iso, username, host_id),
            )

        if conn is not None:
            _execute_save(conn)
        else:
            with db_session() as new_conn:
                _execute_save(new_conn)

        return analysis_result


_analyzer_instance: Optional[ProfileAnalyzer] = None


def get_profile_analyzer() -> ProfileAnalyzer:
    global _analyzer_instance
    if _analyzer_instance is None:
        _analyzer_instance = ProfileAnalyzer()
    return _analyzer_instance
