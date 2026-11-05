"""
profile_analyzer.py - Модуль глубокого интеллектуального анализа профилей Couchsurfing.

Реализует:
1. Выявление персональных Conversation Hooks (детали биографии, стран, увлечений, цитат из отзывов).
2. Верификацию готовности принять гостей (Hosting Availability Guard).
3. Анализ совместимости с Personal Context (подбор 1-2 реальных точек пересечения).
4. Определение оптимальной стратегии общения (Conversation Strategy).
5. Персистентность результатов анализа в таблице profile_analyses базы данных SQLite.
"""

import json
import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from database import db_session
from gender_classifier import get_gender_classifier
from hosting_guard import get_hosting_guard
from personal_context import get_personal_context_manager

logger = logging.getLogger("ProfileAnalyzer")


class ProfileAnalyzer:
    """Интеллектуальный анализатор профилей Couchsurfing."""

    def __init__(self):
        self.guard = get_hosting_guard()
        self.gender_classifier = get_gender_classifier()
        self.context_mgr = get_personal_context_manager()

    def extract_host_profile_details(self, host: Dict[str, Any]) -> Dict[str, Any]:
        """
        Извлекает структурированные данные профиля хоста:
        - clean_bio: полный очищенный текст описания
        - host_interests: выявленные реальные темы и увлечения
        - host_occupation: профессия или род занятий
        - host_languages: языки общения
        """
        bio = (host.get("about_me_clean") or host.get("about_me") or "").strip()
        lower_about = bio.lower()

        detected_interests = []
        interest_patterns = [
            ("Board games & tabletop", ["board game", "boardgame", "catan", "chess", "tabletop"]),
            ("Education & science teaching", ["science teacher", "teacher", "education", "physics", "biology", "chemistry"]),
            ("Film, directing & cinema", ["film", "cinema", "director", "cinematography", "movie", "35mm", "screenplay"]),
            ("Photography & analog cameras", ["photo", "photography", "photographer", "camera", "analog", "35mm"]),
            ("Music, vinyl & audio", ["vinyl", "record", "piano", "guitar", "jazz", "band", "acoustic", "synthesizer"]),
            ("Culinary culture & cooking", ["cook", "culinary", "foodie", "ramen", "sushi", "baking", "wine", "sake", "chef"]),
            ("Reading & literature", ["reading", "books", "literature", "novels", "writing", "author"]),
            ("Cycling, badminton & sports", ["cycling", "badminton", "biking", "running", "marathon", "tennis", "sports"]),
            ("Architecture & design", ["architecture", "interior design", "sketching", "drawing", "illustration"]),
            ("Trail hiking & nature", ["hike", "hiking", "trekking", "camping", "alps", "mountaineering", "climb"]),
            ("Travel & cultural exchange", ["traveler", "backpacking", "cultural exchange", "countries", "nomad", "expats"]),
        ]

        for label, kws in interest_patterns:
            if any(k in lower_about for k in kws):
                detected_interests.append(label)

        if host.get("hobbies"):
            if isinstance(host["hobbies"], list):
                detected_interests.extend(host["hobbies"])
            elif isinstance(host["hobbies"], str) and host["hobbies"].strip():
                detected_interests.append(host["hobbies"].strip())

        # Определение профессии / рода занятий
        occupation = host.get("occupation") or host.get("job")
        if not occupation:
            occ_match = re.search(
                r"(?:work as (?:a |an )?|i(?: am|'m) (?:a |an )?)([a-zA-Z\s]{3,35}?)(?:\.|,|and|\n|who)",
                bio,
                re.IGNORECASE,
            )
            if occ_match:
                cand = occ_match.group(1).strip()
                if not any(w in cand.lower() for w in ["year old", "couchsurfer", "guy", "person", "friendly", "living", "staying"]):
                    occupation = cand

        # Определение языков
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
        """
        Извлекает конкретные детали для начала естественного разговора (Conversation Hooks).
        Исключает поверхностные банальности.
        """
        about = (host.get("about_me_clean") or host.get("about_me") or "").strip()
        hooks: List[str] = []

        # 1. Поиск конкретных стран или городов
        countries = re.findall(
            r"\b(?:France|Italy|Switzerland|Nepal|Norway|Japan|Spain|Germany|Iceland|Georgia|Morocco|Hong\s+Kong|Cuba|Afghanistan|Macedonia|Czech|Portugal|Brazil|Taiwan)\b",
            about,
            re.IGNORECASE,
        )
        if countries:
            unique_c = list(dict.fromkeys(c.capitalize() for c in countries))[:3]
            hooks.append(f"Travel experience in {', '.join(unique_c)}")

        # 2. Поиск увлечений и профессий
        lower_about = about.lower()
        if any(k in lower_about for k in ("board game", "boardgame", "catan", "chess")):
            hooks.append("Passion for board games and late-night strategy conversations")
        if any(k in lower_about for k in ("science teacher", "teaching", "education")):
            hooks.append("Experience as an educator and science teacher")
        if any(k in lower_about for k in ("badminton", "cycling", "bike", "biking")):
            hooks.append("Enthusiasm for cycling and badminton")
        if any(k in lower_about for k in ("film", "cinema", "director", "movies", "cinematography")):
            hooks.append("Interest in film, cinematography and visual storytelling")
        if any(k in lower_about for k in ("photo", "photography", "camera", "analog", "35mm")):
            hooks.append("Passion for photography and capturing moments")
        if any(k in lower_about for k in ("cook", "cooking", "ramen", "sushi", "chef", "wine", "sake", "foodie", "baking")):
            hooks.append("Love for authentic culinary culture and home cooking")
        if any(k in lower_about for k in ("piano", "music", "guitar", "ambient", "vinyl", "jazz")):
            hooks.append("Musical interests (piano / acoustic / vinyl)")
        if any(k in lower_about for k in ("developer", "software", "tech", "coding", "ai", "machine learning")):
            hooks.append("Tech background and passion for software/AI development")
        if any(k in lower_about for k in ("ski", "snowboard", "powder")):
            hooks.append("Passion for winter sports and skiing/snowboarding")
        if any(k in lower_about for k in ("hike", "hiking", "trail", "climb", "mountain", "alps", "trekking")):
            hooks.append("Enthusiasm for mountains, trail hiking and alpine nature")
        if any(k in lower_about for k in ("motorcycle", "motorbike", "moto", "riding", "biker")):
            hooks.append("Love for motorcycles and road trips")
        if any(k in lower_about for k in ("cultural exchange", "language exchange", "learn languages")):
            hooks.append("Focus on genuine peer-to-peer cultural and language exchange")

        # 3. Количество стран (если упомянуто число > 10)
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
        """
        Формирует разговорную стратегию (Conversation Strategy) на основе пересечения профилей.
        """
        fact_ids = {f["fact_id"] for f in matching_facts}

        if "prof_ski_coach_alps" in fact_ids and any("ski" in h.lower() or "mountain" in h.lower() for h in hooks):
            return "Alpine & Winter Sports: Connect via Courchevel ski instruction base and passion for mountains."
        elif "te_nepal_mountains" in fact_ids:
            return "High-Altitude Trekker: Bond over high-altitude Himalayan trekking (Thorong La 5416m, Everest Base Camp) and outdoor endurance."
        elif "prof_film_director_cannes" in fact_ids:
            return "Cinematic & Visual Storyteller: Relate through VGIK film directing, Cannes 2024 participation, and visual creativity."
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
        """
        Выполняет полный цикл анализа профиля и сохраняет результат в БД.
        """
        host_id = host.get("id") or host.get("username")
        username = host.get("username") or host_id
        name = host.get("clean_name") or host.get("name") or "Host"
        about_me = host.get("about_me_clean") or host.get("about_me") or ""
        house_rules = host.get("house_rules") or "Standard rules"
        sleeping = host.get("sleeping_arrangement") or "Not specified"
        refs = host.get("references_count", 0)

        # 1. Верификация доступности ночлега (Hosting Availability Guard)
        can_host, hosting_reason, hosting_conf = self.guard.verify_host(host, use_llm=False)

        # 2. Определение пола и типа хоста (Gender Classifier)
        est_gender, host_type, gender_reason = self.gender_classifier.classify(
            name=name,
            about_text=about_me,
            username=username,
            references=refs,
        )

        # 3. Выявление уникальных Conversation Hooks
        hooks = self.extract_conversation_hooks(host)

        # 4. Анализ совместимости с Personal Context
        matching_facts = self.context_mgr.select_relevant_facts(host, max_facts=2)

        # 5. Определение стратегии общения
        strategy = self.determine_strategy(host, matching_facts, hooks)

        # 6. Проверка кодового слова
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

        # Функция выполнения запросов к БД
        def _execute_save(active_conn):
            cur = active_conn.cursor()
            now_iso = datetime.now().isoformat()
            cur.execute(
                """
                INSERT INTO hosts (
                    id, username, name, clean_name, profile_url, stay_request_url,
                    location, about_me, about_me_clean, references_count,
                    response_rate_pct, first_scraped_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    clean_name = excluded.clean_name,
                    about_me_clean = excluded.about_me_clean,
                    updated_at = excluded.updated_at;
                """,
                (
                    host_id,
                    username,
                    name,
                    name,
                    host.get("profile_url") or f"https://www.couchsurfing.com/c/users/{username}",
                    host.get("stay_request_url") or f"https://www.couchsurfing.com/c/users/{username}/stay-request",
                    str(host.get("location", "Tokyo")),
                    host.get("about_me", ""),
                    about_me,
                    refs,
                    float(host.get("response_rate_pct", 90.0)),
                    now_iso,
                    now_iso,
                ),
            )

            cur.execute(
                """
                INSERT INTO profile_analyses (
                    host_id, estimated_gender, host_type, can_host_overnight,
                    hosting_verdict_reason, hosting_confidence, sleeping_arrangement,
                    house_rules, secret_code_detected, secret_code_hint,
                    conversation_hooks_json, compatibility_points_json,
                    conversation_strategy, analyzed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(host_id) DO UPDATE SET
                    estimated_gender = excluded.estimated_gender,
                    host_type = excluded.host_type,
                    can_host_overnight = excluded.can_host_overnight,
                    hosting_verdict_reason = excluded.hosting_verdict_reason,
                    hosting_confidence = excluded.hosting_confidence,
                    sleeping_arrangement = excluded.sleeping_arrangement,
                    house_rules = excluded.house_rules,
                    secret_code_detected = excluded.secret_code_detected,
                    secret_code_hint = excluded.secret_code_hint,
                    conversation_hooks_json = excluded.conversation_hooks_json,
                    compatibility_points_json = excluded.compatibility_points_json,
                    conversation_strategy = excluded.conversation_strategy,
                    analyzed_at = excluded.analyzed_at;
                """,
                (
                    host_id,
                    est_gender,
                    host_type,
                    1 if can_host else 0,
                    hosting_reason,
                    hosting_conf,
                    sleeping,
                    house_rules,
                    1 if has_secret else 0,
                    secret_code,
                    json.dumps(hooks, ensure_ascii=False),
                    json.dumps(analysis_result["compatibility_points"], ensure_ascii=False),
                    strategy,
                    analysis_result["analyzed_at"],
                ),
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
