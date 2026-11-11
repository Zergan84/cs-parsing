"""
context_assembler.py - Модуль интеллектуальной сборки персонализированного контекста (Context Assembler).

Отвечает за:
1. Сопоставление активных карточек Personal Profile, динамического Travel Context и Compatibility Anchors
   с полным досье хоста (интересы, bio, визуальные наблюдения LLaVA, места).
2. Поиск достоверных точек пересечения (shared interests, experiences, activities) с привязкой к источнику:
   - topic (тема пересечения);
   - user fact (какой факт пользователя используется);
   - confirming host fact (что подтверждает интерес хоста);
   - source (interests, photo/LLaVA, places, bio);
   - confidence (уверенность);
   - priority (приоритет).
3. Исключение неподтвержденных/фейковых совпадений: при отсутствии реальных совпадений
   формируется нейтральный вежливый контекст (Couchsurfing veteran, respectful guest, independent explorer).
4. Ограничение: выдает только 1-2 сильнейших совпадения для компактного внедрения в LLM.
"""

import json
import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple, Union

from database import db_session, get_host_dossier, get_setting
from personal_context import get_personal_context_manager

logger = logging.getLogger("ContextAssembler")

# Карта привязки якорей совместимости к предпочтительным ID карточек Personal Profile
ANCHOR_TO_FACT_MAPPINGS = {
    "anchor_photo": ["hobby_photography", "prof_film_director_cannes"],
    "anchor_cinema": ["prof_film_director_cannes", "edu_vgik_cinema", "hobby_photography"],
    "anchor_mountains": ["prof_ski_coach_alps", "te_nepal_mountains"],
    "anchor_music": ["hobby_piano_music"],
    "anchor_tech": ["tech_fitness_ml_tools"],
    "anchor_food": ["hobby_cooking_local_food"],
    "anchor_culture_lang": ["lang_multilingual", "te_hong_kong_asia", "te_countries_55"],
}


class ContextAssembler:
    """Интеллектуальный сборщик персонализированного контекста для генерации Stay Requests."""

    def __init__(self):
        self.pcm = get_personal_context_manager()

    def _extract_host_signals(self, host: Dict[str, Any]) -> Dict[str, Any]:
        """
        Собирает все верифицированные сигналы хоста:
        - подтвержденные интересы из базы/досье;
        - визуальные наблюдения из LLaVA (confidence >= 0.80);
        - посещенные/упомянутые места;
        - очищенный bio и параметры профиля.
        """
        host_id = host.get("id") or host.get("username") or ""
        dossier = get_host_dossier(host_id) if host_id else None

        verified_interests = []
        visual_observations = []
        places = []

        if dossier:
            # 1. Подтвержденные интересы
            for item in dossier.get("interests", []):
                name = item.get("name", "").strip()
                conf = float(item.get("confidence", 0.5))
                status = item.get("status", "uncertain")
                source = item.get("source", "interests")
                if (status == "confirmed" or conf >= 0.75) and name:
                    verified_interests.append({
                        "name": name,
                        "source": source,
                        "confidence": conf,
                    })

            # 2. Визуальные наблюдения (LLaVA)
            for vo in dossier.get("visual_observations", []):
                obs = vo.get("observation", "").strip()
                cat = vo.get("category", "")
                conf = float(vo.get("confidence", 0.85))
                if obs and conf >= 0.75:
                    visual_observations.append({
                        "observation": obs,
                        "category": cat,
                        "source": "photo/LLaVA",
                        "confidence": conf,
                    })

            # 3. Места
            for pl in dossier.get("places", []):
                pname = pl.get("name", "").strip()
                ptype = pl.get("type", "visited")
                if pname:
                    places.append({
                        "name": pname,
                        "type": ptype,
                        "source": "places",
                        "confidence": 0.85,
                    })

        # Если в досье нет интересов, проверяем поле raw_interests или about_me хоста
        if not verified_interests:
            raw_interests = host.get("interests")
            if isinstance(raw_interests, list):
                for ri in raw_interests:
                    if ri:
                        verified_interests.append({"name": str(ri).strip(), "source": "interests", "confidence": 0.80})
            elif isinstance(raw_interests, str) and raw_interests.strip():
                for ri in re.split(r"[,;\n]", raw_interests):
                    if ri.strip():
                        verified_interests.append({"name": ri.strip(), "source": "interests", "confidence": 0.80})

        about_text = (host.get("about_me_clean") or host.get("about_me") or "").strip()
        occupation = host.get("occupation") or ""

        return {
            "verified_interests": verified_interests,
            "visual_observations": visual_observations,
            "places": places,
            "about_text": about_text,
            "occupation": occupation,
        }

    def assemble_travel_context(
        self,
        host: Dict[str, Any],
        trip_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Формирует динамический контекст поездки на основе активной Trip Card.
        Исключает устаревшие статичные даты.
        """
        city = None
        dates_text = None
        arrival = None
        departure = None
        purpose = None
        trip_id = None

        if trip_context and isinstance(trip_context, dict):
            city = trip_context.get("city")
            dates_text = trip_context.get("dates_text")
            arrival = trip_context.get("arrival_date") or trip_context.get("arrival")
            departure = trip_context.get("departure_date") or trip_context.get("departure")
            purpose = trip_context.get("purpose")
            trip_id = trip_context.get("trip_id")

        # Если город не передан в trip_context, берем город хоста или настройку
        if not city:
            raw_loc = host.get("location")
            if isinstance(raw_loc, dict):
                city = raw_loc.get("city") or raw_loc.get("slug") or "Kyoto"
            elif isinstance(raw_loc, str) and raw_loc.strip():
                city = raw_loc.split(",")[0].strip()
            else:
                city = host.get("city") or get_setting("default_city", "Kyoto") or "Kyoto"

        # Нормализация дат
        if not dates_text:
            if arrival and departure:
                try:
                    d_in = datetime.strptime(str(arrival).strip()[:10], "%Y-%m-%d")
                    d_out = datetime.strptime(str(departure).strip()[:10], "%Y-%m-%d")
                    m_in = d_in.strftime("%b")
                    m_out = d_out.strftime("%b")
                    if d_in.month == d_out.month:
                        dates_text = f"{m_in} {d_in.day}–{d_out.day}"
                    else:
                        dates_text = f"{m_in} {d_in.day} – {m_out} {d_out.day}"
                except Exception:
                    dates_text = f"{arrival} – {departure}"
            else:
                # Динамический дефолт для текущего сезона
                if str(city).lower() in ("kyoto", "osaka"):
                    dates_text = "Nov 19–21"
                elif str(city).lower() in ("tokyo",):
                    dates_text = "Nov 16–19"
                elif str(city).lower() in ("sapporo", "hokkaido"):
                    dates_text = "Nov 22–26"
                else:
                    dates_text = "Nov 19–21"

        if not purpose:
            if str(city).lower() in ("kyoto", "osaka"):
                purpose = "Exploring the city, photography, and cultural immersion"
            elif str(city).lower() in ("tokyo",):
                purpose = "Acclimatization, urban exploration, and connecting with local creative community"
            elif str(city).lower() in ("sapporo", "hokkaido"):
                purpose = "Winter season preparation and alpine exploration"
            else:
                purpose = f"Visiting {city} to experience local life and connect with creative minds"

        return {
            "city": city.strip(),
            "country": "Japan",
            "dates_text": dates_text,
            "arrival_date": arrival,
            "departure_date": departure,
            "purpose": purpose,
            "trip_id": trip_id,
        }

    def assemble_context(
        self,
        host: Dict[str, Any],
        trip_context: Optional[Dict[str, Any]] = None,
        max_intersections: int = 2,
    ) -> Dict[str, Any]:
        """
        Основной метод сборки:
        1. Извлекает сигналы хоста.
        2. Формирует динамический контекст поездки.
        3. Ищет точки пересечения между хостом, активными якорями и карточками профиля пользователя.
        4. Отбирает 1-2 самых сильных пересечения с доказательствами.
        5. Возвращает структурированный словарь для инъекции в промпт.
        """
        signals = self._extract_host_signals(host)
        travel = self.assemble_travel_context(host, trip_context)

        active_facts = self.pcm.get_all_facts(active_only=True)
        active_anchors = self.pcm.get_all_anchors(active_only=True)

        facts_by_id = {f["fact_id"]: f for f in active_facts}

        candidate_intersections: List[Dict[str, Any]] = []

        # Поиск пересечений по каждому активному якорю
        for anchor in active_anchors:
            anchor_name = anchor["name"]
            keywords = [k.lower().strip() for k in anchor.get("keywords", []) if k.strip()]
            anchor_prio = int(anchor.get("priority", 1))

            matched_signal = None
            evidence_text = ""
            source = ""
            conf = 0.0

            # 1. Проверка визуальных наблюдений LLaVA (наиболее надежный зрительный пруф)
            for vo in signals["visual_observations"]:
                obs_lower = vo["observation"].lower()
                for kw in keywords:
                    if re.search(rf"\b{re.escape(kw)}\b", obs_lower):
                        matched_signal = vo
                        evidence_text = f"Visual observation: {vo['observation']}"
                        source = vo["source"]
                        conf = float(vo["confidence"])
                        break
                if matched_signal:
                    break

            # 2. Проверка подтвержденных интересов хоста
            if not matched_signal:
                for item in signals["verified_interests"]:
                    name_lower = item["name"].lower()
                    for kw in keywords:
                        if kw in name_lower or re.search(rf"\b{re.escape(kw)}\b", name_lower):
                            matched_signal = item
                            evidence_text = f"Host interest: {item['name']}"
                            source = item["source"]
                            conf = float(item["confidence"])
                            break
                    if matched_signal:
                        break

            # 3. Проверка мест хоста (особенно для гор/Непала/Азии)
            if not matched_signal:
                for pl in signals["places"]:
                    pname_lower = pl["name"].lower()
                    for kw in keywords:
                        if kw in pname_lower:
                            matched_signal = pl
                            evidence_text = f"Host place association: {pl['name']} ({pl.get('type')})"
                            source = pl["source"]
                            conf = float(pl["confidence"])
                            break
                    if matched_signal:
                        break

            # 4. Проверка bio и профессии хоста
            if not matched_signal and signals["about_text"]:
                bio_lower = signals["about_text"].lower()
                occ_lower = signals["occupation"].lower()
                combined_text = f"{bio_lower} {occ_lower}"

                for kw in keywords:
                    # Ищем целые слова для защиты от ложных совпадений
                    match = re.search(rf"\b{re.escape(kw)}\b", combined_text)
                    if match:
                        start = max(0, match.start() - 40)
                        end = min(len(combined_text), match.end() + 40)
                        snippet = combined_text[start:end].replace("\n", " ").strip()
                        evidence_text = f"Mentioned in bio: \"...{snippet}...\""
                        source = "bio"
                        conf = 0.78
                        matched_signal = {"name": kw}
                        break

            # Если совпадение для якоря найдено — сопоставляем с карточкой профиля пользователя
            if matched_signal and evidence_text:
                preferred_fact_ids = ANCHOR_TO_FACT_MAPPINGS.get(anchor["anchor_id"], [])
                chosen_fact = None

                # Ищем лучшую активную карточку пользователя
                for fid in preferred_fact_ids:
                    if fid in facts_by_id:
                        chosen_fact = facts_by_id[fid]
                        break

                # Если нет в маппинге, проверяем совпадение ключевых слов с контентом активных карточек
                if not chosen_fact:
                    for f in active_facts:
                        content_lower = f["content"].lower() + " " + f["title"].lower()
                        if any(kw in content_lower for kw in keywords):
                            chosen_fact = f
                            break

                if chosen_fact:
                    score = (anchor_prio * 2.0) + (int(chosen_fact.get("priority", 1)) * 1.5) + (conf * 3.0)
                    if source in ("photo/LLaVA", "interests"):
                        score += 2.0  # Бонус за объективно подтвержденные факты

                    candidate_intersections.append({
                        "topic": anchor_name,
                        "anchor_id": anchor["anchor_id"],
                        "user_fact": {
                            "fact_id": chosen_fact["fact_id"],
                            "category": chosen_fact["category"],
                            "title": chosen_fact["title"],
                            "content": chosen_fact["content"],
                            "priority": chosen_fact.get("priority", 1),
                        },
                        "confirming_host_fact": evidence_text,
                        "source": source,
                        "confidence": round(conf, 2),
                        "priority": chosen_fact.get("priority", 1),
                        "_score": score,
                    })

        # Сортируем кандидатов по общему баллу достоверности и приоритета
        candidate_intersections.sort(key=lambda x: x["_score"], reverse=True)

        # Убираем дубликаты карточек пользователя (одна карточка не должна дублироваться в разных точках)
        unique_intersections: List[Dict[str, Any]] = []
        seen_fact_ids = set()
        seen_topics = set()

        for cand in candidate_intersections:
            fid = cand["user_fact"]["fact_id"]
            topic = cand["topic"]
            if fid not in seen_fact_ids and topic not in seen_topics:
                seen_fact_ids.add(fid)
                seen_topics.add(topic)
                cand_copy = dict(cand)
                cand_copy.pop("_score", None)
                unique_intersections.append(cand_copy)
                if len(unique_intersections) >= max_intersections:
                    break

        fallback_used = False
        selected_cards: List[Dict[str, Any]] = []

        if unique_intersections:
            selected_cards = [item["user_fact"] for item in unique_intersections]
        else:
            # Проверяем наличие специфических деталей в биографии хоста через ProfileAnalyzer
            from profile_analyzer import get_profile_analyzer
            analyzer = get_profile_analyzer()
            bio_hooks = analyzer.extract_conversation_hooks(host)
            specific_hooks = [h for h in bio_hooks if "Thoughtful perspective" not in h]

            if specific_hooks:
                top_hook = specific_hooks[0]
                user_f = facts_by_id.get("pb_lifestyle_respect") or facts_by_id.get("lang_multilingual") or (active_facts[0] if active_facts else None)
                fallback_used = False
                if user_f:
                    hook_intersection = {
                        "topic": f"Profile detail: {top_hook}",
                        "anchor_id": "profile_bio_detail",
                        "user_fact": {
                            "fact_id": user_f["fact_id"],
                            "category": user_f.get("category", "General"),
                            "title": user_f["title"],
                            "content": user_f["content"],
                            "priority": user_f.get("priority", 1),
                        },
                        "confirming_host_fact": f"Mentioned in bio: \"{top_hook}\"",
                        "source": "bio",
                        "confidence": 0.85,
                        "priority": 1,
                    }
                    unique_intersections.append(hook_intersection)
                    selected_cards = [user_f]
            else:
                # Исключение фейковых совпадений: если в bio действительно нет хобби,
                # формируем вежливое обращение уважительного и чистоплотного гостя
                fallback_used = True
                respect_fact = facts_by_id.get("pb_lifestyle_respect")
                lang_fact = facts_by_id.get("lang_multilingual")
                fallback_candidates = [f for f in (respect_fact, lang_fact) if f]
                selected_cards = fallback_candidates[:2]

        # Формируем компактные инструкции для LLM промпта
        llm_instructions = self._format_llm_instructions(
            host_name=host.get("clean_name") or host.get("name") or "Host",
            travel=travel,
            intersections=unique_intersections,
            selected_cards=selected_cards,
            fallback_used=fallback_used,
        )

        return {
            "host_name": host.get("clean_name") or host.get("name") or "Host",
            "host_id": host.get("id") or host.get("username") or "",
            "travel_context": travel,
            "intersections": unique_intersections,
            "selected_cards": selected_cards,
            "fallback_used": fallback_used,
            "llm_instructions": llm_instructions,
        }

    def _format_llm_instructions(
        self,
        host_name: str,
        travel: Dict[str, Any],
        intersections: List[Dict[str, Any]],
        selected_cards: List[Dict[str, Any]],
        fallback_used: bool,
    ) -> str:
        """Генерирует компактную секцию персонального контекста для промпта LLM."""
        city = travel["city"]
        dates = travel["dates_text"]
        purpose = travel["purpose"]

        if not fallback_used and intersections:
            hooks_lines = []
            for idx, inter in enumerate(intersections, 1):
                topic = inter["topic"]
                my_fact = inter["user_fact"]["title"]
                host_fact = inter["confirming_host_fact"]
                src = inter["source"]
                hooks_lines.append(
                    f"  Hook {idx} [{topic}]: Host interest confirmed by '{host_fact}' (source: {src}). "
                    f"Match with Andrey's background: '{my_fact}'."
                )
            hooks_str = "\n".join(hooks_lines)

            instructions = f"""PERSONALIZED COMPATIBILITY CONTEXT (GROUNDED & VERIFIED):
- Trip Context: Visiting {city} ({purpose}).
  (IMPORTANT: Do NOT repeat explicit calendar dates in the message text — they are already prominently displayed in the Couchsurfing Stay Request card header!).
- Verified Common Ground (1-2 Strongest Points):
{hooks_str}
- Rule for Paragraph 1: Address {host_name} warmly by first name. Open with a genuine, natural connection to the host's confirmed interest ({intersections[0]['topic']}). Avoid cliches like "your passion for X".
- Rule for Paragraph 2: Naturally connect Andrey's relevant background ({selected_cards[0]['title']}) with visiting {city}. Keep it modest, relatable, and human — never list achievements. Do NOT repeat calendar dates. Do NOT insert formulaic fillers like "traveling light with a daypack" or "historic neighborhoods" unless directly relevant.
- FACTUAL GROUNDING RULES:
  1. DESTINATION ONLY: Andrey is visiting {city} as established by the Trip Card. NEVER infer or invent travel routes beyond {city} (do NOT say 'heading to Hokkaido', 'on my way north for skiing', or 'winter season relocation').
  2. STRICT SEPARATION OF FACTS: Keep host facts and Andrey's background strictly separate. Do NOT claim ungrounded shared interests — NEVER write 'We both enjoy [X]' or 'We both love [X]' unless the host explicitly states that exact interest in their bio.
  3. NO LANGUAGE CONFLATION: A host's interest in travel, local culture, or chatting with locals DOES NOT imply interest in language learning. Only reference language learning if the host explicitly states they study or practice languages."""
        else:
            instructions = f"""PERSONAL CONTEXT (RESPECTFUL GUEST):
- Trip Context: Visiting {city} ({purpose}).
  (IMPORTANT: Do NOT repeat explicit calendar dates in the message text — they are already prominently displayed in the Couchsurfing Stay Request card header!).
- No niche overlapping hobbies confirmed; do NOT fabricate fake common interests or claim the host loves hosting/cultural exchange if their bio is empty.
- Keep the message simple, personal, and polite: introduce yourself briefly as a visitor to {city}, and ask if they might have space to host a considerate guest.
- Rule for Paragraph 2: Keep it natural and grounded without cliches like "traveling light with a daypack" or "independent during the day". Do NOT repeat calendar dates.
- DESTINATION ONLY: Do NOT infer or invent travel routes beyond {city} (do NOT say 'on my way to Hokkaido' or 'heading north for skiing')."""

        return instructions


_assembler_instance: Optional[ContextAssembler] = None


def get_context_assembler() -> ContextAssembler:
    global _assembler_instance
    if _assembler_instance is None:
        _assembler_instance = ContextAssembler()
    return _assembler_instance
