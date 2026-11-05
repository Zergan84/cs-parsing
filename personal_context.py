"""
personal_context.py - Модуль управления структурированной базой знаний заявителя (Personal Context).

Обеспечивает:
1. Хранение структурированных фактов в таблице personal_context_facts базы данных SQLite.
2. Категоризацию:
   - Personal Background
   - Travel Experience
   - Professional Background
   - Technology & Entrepreneurship
   - Hobbies & Interests
   - Current Travel Context (динамический контекст поездки)
3. Управление фактами: добавление, редактирование, удаление, переключение активности, импорт/экспорт JSON.
4. Интеллектуальный селектор: подбор только 1-2 наиболее релевантных фактов под конкретного хоста.
"""

import json
import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from database import db_session

logger = logging.getLogger("PersonalContext")

# Эталонная текстовая выжимка расширенного бэкграунда заявителя
DEFAULT_GUEST_CONTEXT = (
    "Andrey, professional ski instructor based in Courchevel (French Alps), moving to Hokkaido for winter season. "
    "10+ years CS veteran (overland Moscow-Paris, Morocco). High-altitude trekker (Thorong La Pass 5416m with son Mark in 2026, "
    "solo Everest Base Camp). Film director (VGIK, Cannes 2024), plays piano, rides BMW GS motorcycle. "
    "Independent, quiet, out exploring all day."
)

# Начальные эталонные факты Андрея
DEFAULT_FACTS = [
    # 1. Personal Background
    {
        "fact_id": "pb_lifestyle_respect",
        "category": "Personal Background",
        "title": "Уважение к личному пространству и чистоте",
        "content": "Внимательный и тихий гость. Понимает границы личного пространства, соблюдает режим тишины и чистоты в доме. Днем гуляет и исследует город самостоятельно.",
        "status": "confirmed",
        "source": "curated",
        "confidence": 1.0,
        "is_active": 1,
    },
    {
        "fact_id": "pb_languages",
        "category": "Personal Background",
        "title": "Языки общения",
        "content": "Свободный английский, базовый французский, родной русский. Легко общается на межкультурные темы.",
        "status": "confirmed",
        "source": "curated",
        "confidence": 1.0,
        "is_active": 1,
    },
    {
        "fact_id": "pb_cs_veteran",
        "category": "Personal Background",
        "title": "10+ лет на Couchsurfing",
        "content": "В сообществе Couchsurfing более 10 лет как активный путешественник и как хост во Французских Альпах. Знает культуру хостинга изнутри.",
        "status": "confirmed",
        "source": "curated",
        "confidence": 1.0,
        "is_active": 1,
    },

    # 2. Travel Experience
    {
        "fact_id": "te_countries_55",
        "category": "Travel Experience",
        "title": "55+ стран и независимые путешествия",
        "content": "Исследовал более 55 стран самостоятельно. Предпочитает глубокое погружение в аутентичную среду вместо туристических штампов.",
        "status": "confirmed",
        "source": "curated",
        "confidence": 1.0,
        "is_active": 1,
    },
    {
        "fact_id": "te_hong_kong_asia",
        "category": "Travel Experience",
        "title": "Жизнь в Гонконге и связь с Азией",
        "content": "Жил в Гонконге, имеет глубокую связь с культурой Азии, понимает местный этикет и городские ритмы.",
        "status": "confirmed",
        "source": "curated",
        "confidence": 1.0,
        "is_active": 1,
    },
    {
        "fact_id": "te_nepal_mountains",
        "category": "Travel Experience",
        "title": "Высокогорный треккинг в Непале (Торунг Ла, Эверест)",
        "content": "Опыт высокогорного треккинга в Непале: соло трек к Everest Base Camp и Kala Patthar (5642 м), прохождение кольца Аннапурны с сыном Марком через перевал Thorong La (5416 м в 2026 году).",
        "status": "confirmed",
        "source": "curated",
        "confidence": 1.0,
        "is_active": 1,
    },
    {
        "fact_id": "te_bmw_gs_moto",
        "category": "Travel Experience",
        "title": "Мотопутешествия на BMW GS (Альпы, Марокко)",
        "content": "Путешествует на мотоцикле BMW GS по горным серпантинам Европы, Альпам и Марокко. Увлекается мотомеханикой и автопутешествиями (overland Москва-Париж).",
        "status": "confirmed",
        "source": "curated",
        "confidence": 1.0,
        "is_active": 1,
    },

    # 3. Professional Background
    {
        "fact_id": "prof_ski_coach_alps",
        "category": "Professional Background",
        "title": "Горнолыжный инструктор (Куршевель, Франция)",
        "content": "Профессиональный инструктор по горным лыжам и сноуборду, базируется во Французских Альпах (Куршевель, Les 3 Vallées). Работал в России, Польше и Франции.",
        "status": "confirmed",
        "source": "curated",
        "confidence": 1.0,
        "is_active": 1,
    },
    {
        "fact_id": "prof_film_director_cannes",
        "category": "Professional Background",
        "title": "Режиссер кино (ВГИК, Канны 2024)",
        "content": "Высшее образование кинорежиссера (ВГИК), опыт работы в киноиндустрии. Участник Каннского кинофестиваля в 2024 году. Профессионально занимается фотографией и видео.",
        "status": "confirmed",
        "source": "curated",
        "confidence": 1.0,
        "is_active": 1,
    },

    # 4. Technology & Entrepreneurship
    {
        "fact_id": "tech_fitness_ml_tools",
        "category": "Technology & Entrepreneurship",
        "title": "IT-разработка, AI и Fitness Web Tools",
        "content": "Разрабатывает собственные веб-сервисы в сфере фитнеса и машинного обучения. Интересуется автоматизацией, агентными системами и LLM.",
        "status": "confirmed",
        "source": "curated",
        "confidence": 1.0,
        "is_active": 1,
    },

    # 5. Hobbies & Interests
    {
        "fact_id": "hobby_piano_music",
        "category": "Hobbies & Interests",
        "title": "Фортепиано, классика и эмбиент",
        "content": "Играет на фортепиано, ценит классическую музыку, эмбиент и акустическое звучание.",
        "status": "confirmed",
        "source": "curated",
        "confidence": 1.0,
        "is_active": 1,
    },
    {
        "fact_id": "hobby_cooking_local_food",
        "category": "Hobbies & Interests",
        "title": "Кулинария и аутентичные вкусы",
        "content": "Любит готовить вкусные домашние блюда, открывать для себя локальные гастрономические традиции, рынки и сезонную кухню.",
        "status": "confirmed",
        "source": "curated",
        "confidence": 1.0,
        "is_active": 1,
    },

    # 6. Current Travel Context (Динамическая категория)
    {
        "fact_id": "curr_tokyo_hokkaido_2026",
        "category": "Current Travel Context",
        "title": "Токио (ноябрь 2026) -> Club Med Tomamu (Хоккайдо)",
        "content": "Визит в Токио на даты 18-20 ноября 2026 года для акклиматизации и покупки зимнего снаряжения, после чего переезд на сезонную работу инструктором в Club Med Tomamu (Хоккайдо).",
        "status": "temporary",
        "source": "user_schedule",
        "confidence": 1.0,
        "is_active": 1,
    },
]


class PersonalContextManager:
    """Менеджер операций над базой знаний заявителя."""

    def __init__(self):
        self.ensure_default_facts()

    def ensure_default_facts(self) -> None:
        """Инициализирует базу данных начальным набором фактов при первом запуске."""
        with db_session() as conn:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM personal_context_facts;")
            count = cur.fetchone()[0]
            if count == 0:
                now = datetime.now().isoformat()
                for f in DEFAULT_FACTS:
                    cur.execute(
                        """
                        INSERT INTO personal_context_facts 
                        (fact_id, category, title, content, status, source, confidence, is_active, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                        """,
                        (
                            f["fact_id"],
                            f["category"],
                            f["title"],
                            f["content"],
                            f["status"],
                            f["source"],
                            f["confidence"],
                            f["is_active"],
                            now,
                            now,
                        ),
                    )
                logger.info("Таблица personal_context_facts инициализирована эталонными фактами.")

    def get_all_facts(self, active_only: bool = False) -> List[Dict[str, Any]]:
        """Возвращает список всех фактов, сгруппированных по категориям."""
        with db_session() as conn:
            cur = conn.cursor()
            if active_only:
                cur.execute("SELECT * FROM personal_context_facts WHERE is_active = 1 ORDER BY category, title;")
            else:
                cur.execute("SELECT * FROM personal_context_facts ORDER BY category, title;")
            rows = cur.fetchall()
            return [dict(r) for r in rows]

    def get_fact(self, fact_id: str) -> Optional[Dict[str, Any]]:
        """Возвращает факт по его ID."""
        with db_session() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM personal_context_facts WHERE fact_id = ?;", (fact_id,))
            row = cur.fetchone()
            return dict(row) if row else None

    def upsert_fact(
        self,
        fact_id: str,
        category: str,
        title: str,
        content: str,
        status: str = "confirmed",
        source: str = "user",
        confidence: float = 1.0,
        is_active: bool = True,
        valid_until: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Добавляет или обновляет факт."""
        now = datetime.now().isoformat()
        with db_session() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO personal_context_facts 
                (fact_id, category, title, content, status, source, confidence, is_active, valid_until, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(fact_id) DO UPDATE SET
                    category = excluded.category,
                    title = excluded.title,
                    content = excluded.content,
                    status = excluded.status,
                    source = excluded.source,
                    confidence = excluded.confidence,
                    is_active = excluded.is_active,
                    valid_until = excluded.valid_until,
                    updated_at = excluded.updated_at;
                """,
                (
                    fact_id,
                    category,
                    title,
                    content,
                    status,
                    source,
                    confidence,
                    1 if is_active else 0,
                    valid_until,
                    now,
                    now,
                ),
            )
            return self.get_fact(fact_id) or {}

    def toggle_fact_active(self, fact_id: str, is_active: bool) -> bool:
        """Включает или выключает факт."""
        now = datetime.now().isoformat()
        with db_session() as conn:
            cur = conn.cursor()
            cur.execute(
                "UPDATE personal_context_facts SET is_active = ?, updated_at = ? WHERE fact_id = ?;",
                (1 if is_active else 0, now, fact_id),
            )
            return cur.rowcount > 0

    def delete_fact(self, fact_id: str) -> bool:
        """Удаляет факт."""
        with db_session() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM personal_context_facts WHERE fact_id = ?;", (fact_id,))
            return cur.rowcount > 0

    def export_json(self) -> str:
        """Экспорт всех фактов в JSON строку."""
        facts = self.get_all_facts(active_only=False)
        return json.dumps(facts, ensure_ascii=False, indent=2)

    def import_json(self, json_str: str) -> int:
        """Импорт фактов из JSON строки."""
        data = json.loads(json_str)
        if not isinstance(data, list):
            raise ValueError("Expected a JSON array of facts")
        imported_count = 0
        for it in data:
            self.upsert_fact(
                fact_id=it["fact_id"],
                category=it.get("category", "Personal Background"),
                title=it.get("title", "Untitled Fact"),
                content=it.get("content", ""),
                status=it.get("status", "confirmed"),
                source=it.get("source", "import"),
                confidence=float(it.get("confidence", 1.0)),
                is_active=bool(it.get("is_active", True)),
                valid_until=it.get("valid_until"),
            )
            imported_count += 1
        return imported_count

    def select_relevant_facts(
        self,
        host_profile: Dict[str, Any],
        max_facts: int = 2,
    ) -> List[Dict[str, Any]]:
        """
        Интеллектуальный подбор 1-2 фактов обо мне, наиболее релевантных конкретному хосту.
        Не навязывает нерелевантные совпадения. Если точек пересечения нет, возвращает базовую надежность.
        """
        all_active = self.get_all_facts(active_only=True)
        about = (host_profile.get("about_me_clean") or host_profile.get("about_me") or "").lower()
        home = (host_profile.get("house_rules") or "").lower()
        full_text = f"{about} {home}"

        scored_facts: List[Tuple[float, Dict[str, Any]]] = []

        for fact in all_active:
            cat = fact["category"]
            fid = fact["fact_id"]
            score = 0.0

            # 1. Горы, лыжи, сноуборд, треккинг, Альпы, Непал
            if fid in ("prof_ski_coach_alps", "te_nepal_mountains"):
                if any(k in full_text for k in ("mountain", "hike", "hiking", "ski", "skiing", "snowboard", "alps", "trekking", "climb", "nature", "outdoor", "nepal")):
                    score += 5.0

            # 2. Кино, режиссура, Канны, фото, видео
            elif fid == "prof_film_director_cannes":
                if any(k in full_text for k in ("film", "cinema", "director", "photo", "photography", "camera", "video", "art", "creative", "cannes")):
                    score += 5.0

            # 3. Мотоциклы, BMW GS, механика, роудтрипы
            elif fid == "te_bmw_gs_moto":
                if any(k in full_text for k in ("moto", "motorcycle", "motorbike", "biker", "road trip", "mechanic", "bmw")):
                    score += 5.0

            # 4. IT, разработка, ML, стартапы
            elif fid == "tech_fitness_ml_tools":
                if any(k in full_text for k in ("software", "developer", "engineer", "machine learning", "coding", "tech", "python", "ai", "startup")):
                    score += 4.5

            # 5. Музыка, фортепиано
            elif fid == "hobby_piano_music":
                if any(k in full_text for k in ("music", "piano", "classical", "ambient", "guitar", "concert")):
                    score += 4.5

            # 6. Европа, Франция, экспаты
            elif fid in ("prof_ski_coach_alps", "te_bmw_gs_moto"):
                if any(k in full_text for k in ("europe", "france", "french", "courchevel", "italy", "switzerland")):
                    score += 4.0

            # 7. Гонконг, жизнь в Азии
            elif fid == "te_hong_kong_asia":
                if any(k in full_text for k in ("hong kong", "asia", "taiwan", "china", "expat")):
                    score += 4.0

            # 8. Кулинария, еда
            elif fid == "hobby_cooking_local_food":
                if any(k in full_text for k in ("food", "cook", "cooking", "ramen", "sushi", "wine", "tea", "baking", "foodie")):
                    score += 3.5

            # Базовый опыт на CS и надежность
            elif fid == "pb_cs_veteran":
                score += 1.0

            if score > 0:
                scored_facts.append((score, fact))

        scored_facts.sort(key=lambda x: x[0], reverse=True)

        if scored_facts and scored_facts[0][0] >= 3.0:
            # Найдено явное тематическое пересечение: берем топ-1 или топ-2 факта
            selected = [f for s, f in scored_facts[:max_facts]]
        else:
            # Если специфического совпадения нет, берем проверенный факт о 10+ годах на CS и уважении к границам
            cs_fact = next((f for f in all_active if f["fact_id"] == "pb_cs_veteran"), None)
            resp_fact = next((f for f in all_active if f["fact_id"] == "pb_lifestyle_respect"), None)
            selected = [f for f in (cs_fact, resp_fact) if f]

        return selected


_pcm_instance: Optional[PersonalContextManager] = None


def get_personal_context_manager() -> PersonalContextManager:
    global _pcm_instance
    if _pcm_instance is None:
        _pcm_instance = PersonalContextManager()
    return _pcm_instance
