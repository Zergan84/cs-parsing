"""
personal_context.py - Модуль управления структурированной базой знаний заявителя (Personal Context)
и якорями совместимости (Compatibility Anchors).

Обеспечивает:
1. Хранение структурированных фактов в таблице personal_context_facts базы данных SQLite:
   - Professional Background
   - Personal Background
   - Hobbies & Interests
   - Travel Experience
   - Education
   - Languages
   - Technology & Entrepreneurship
2. Хранение и управление якорями совместимости в таблице compatibility_anchors:
   - Photography
   - Cinema & Filmmaking
   - Mountains / Outdoor / Ski
   - Music & Sound
   - Technology / Startups / AI
   - Food & Cooking
   - Culture & Languages
3. Полный CRUD: создание, редактирование, удаление, переключение активности, приоритеты.
4. Экспорт/импорт JSON.
"""

import json
import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple, Union

from database import db_session

logger = logging.getLogger("PersonalContext")

# Эталонная текстовая выжимка бэкграунда заявителя (обратная совместимость)
DEFAULT_GUEST_CONTEXT = (
    "Andrey, professional ski instructor based in Geneva (Switzerland) and Courchevel (French Alps), flying to Japan from Geneva. "
    "10+ years CS veteran (overland Geneva-Paris, Alps, Morocco). High-altitude trekker (Thorong La Pass 5416m with son Mark in 2026, "
    "solo Everest Base Camp). Film director (Cannes 2024), plays piano, rides BMW GS motorcycle. "
    "Independent, quiet, out exploring all day."
)

# Эталонные категории Personal Profile
PERSONAL_CATEGORIES = [
    "Professional Background",
    "Personal Background",
    "Hobbies & Interests",
    "Travel Experience",
    "Education",
    "Languages",
    "Technology & Entrepreneurship",
]

# Начальные структурированные факты заявителя (Андрей)
DEFAULT_FACTS = [
    # 1. Professional Background
    {
        "fact_id": "prof_ski_coach_alps",
        "category": "Professional Background",
        "title": "Горнолыжный инструктор (Куршевель, Франция)",
        "content": "Профессиональный инструктор по горным лыжам и сноуборду, базируется в Женеве (Швейцария) и Французских Альпах (Куршевель, Les 3 Vallées). Летит в Японию из Женевы.",
        "priority": 2,
        "status": "confirmed",
        "source": "curated",
        "confidence": 1.0,
        "is_active": 1,
    },
    {
        "fact_id": "prof_film_director_cannes",
        "category": "Professional Background",
        "title": "Режиссер кино (ВГИК, Канны 2024)",
        "content": "Высшее образование кинорежиссера (ВГИК), опыт работы в независимом кинопроизводстве. Участник Каннского кинофестиваля в 2024 году. Профессионально занимается визуальным сторителлингом.",
        "priority": 2,
        "status": "confirmed",
        "source": "curated",
        "confidence": 1.0,
        "is_active": 1,
    },

    # 2. Personal Background
    {
        "fact_id": "pb_cs_veteran",
        "category": "Personal Background",
        "title": "10+ лет на Couchsurfing",
        "content": "В сообществе Couchsurfing более 10 лет как активный путешественник и как хост во Французских Альпах. Прекрасно понимает этику хостинга изнутри.",
        "priority": 2,
        "status": "confirmed",
        "source": "curated",
        "confidence": 1.0,
        "is_active": 1,
    },
    {
        "fact_id": "pb_lifestyle_respect",
        "category": "Personal Background",
        "title": "Уважение к личному пространству и чистоте",
        "content": "Внимательный и тихий гость. Понимает границы личного пространства, соблюдает чистоту и режим в доме. Днем исследует город самостоятельно.",
        "priority": 1,
        "status": "confirmed",
        "source": "curated",
        "confidence": 1.0,
        "is_active": 1,
    },

    # 3. Hobbies & Interests
    {
        "fact_id": "hobby_photography",
        "category": "Hobbies & Interests",
        "title": "Стрит-фотография и визуальное искусство",
        "content": "Увлекается стрит-фотографией, композицией, аналоговой эстетикой и запечатлением аутентичных городских уголков и людей.",
        "priority": 2,
        "status": "confirmed",
        "source": "curated",
        "confidence": 1.0,
        "is_active": 1,
    },
    {
        "fact_id": "hobby_piano_music",
        "category": "Hobbies & Interests",
        "title": "Фортепиано, классика и эмбиент",
        "content": "Играет на фортепиано, ценит классическую музыку, эмбиент, винил и акустическое звучание.",
        "priority": 1,
        "status": "confirmed",
        "source": "curated",
        "confidence": 1.0,
        "is_active": 1,
    },
    {
        "fact_id": "hobby_cooking_local_food",
        "category": "Hobbies & Interests",
        "title": "Кулинария и аутентичные вкусы",
        "content": "Любит готовить вкусные домашние блюда, открывать локальные гастрономические традиции, рынки, фермерские продукты и сезонную кухню.",
        "priority": 1,
        "status": "confirmed",
        "source": "curated",
        "confidence": 1.0,
        "is_active": 1,
    },

    # 4. Travel Experience
    {
        "fact_id": "te_countries_55",
        "category": "Travel Experience",
        "title": "55+ стран и независимые путешествия",
        "content": "Исследовал более 55 стран самостоятельно. Предпочитает глубокое погружение в аутентичную среду вместо стандартных туристических маршрутов.",
        "priority": 2,
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
        "priority": 2,
        "status": "confirmed",
        "source": "curated",
        "confidence": 1.0,
        "is_active": 1,
    },
    {
        "fact_id": "te_bmw_gs_moto",
        "category": "Travel Experience",
        "title": "Мотопутешествия на BMW GS (Альпы, Марокко)",
        "content": "Путешествует на мотоцикле BMW GS по горным серпантинам Европы, Альпам и Марокко. Увлекается мотомеханикой и автопутешествиями (overland Женева-Париж, Альпы).",
        "priority": 1,
        "status": "confirmed",
        "source": "curated",
        "confidence": 1.0,
        "is_active": 1,
    },
    {
        "fact_id": "te_hong_kong_asia",
        "category": "Travel Experience",
        "title": "Жизнь в Гонконге и связь с Азией",
        "content": "Жил в Гонконге, имеет глубокую связь с культурой Азии, понимает местный этикет, ритмы мегаполисов и культуру чайных церемоний.",
        "priority": 1,
        "status": "confirmed",
        "source": "curated",
        "confidence": 1.0,
        "is_active": 1,
    },

    # 5. Education
    {
        "fact_id": "edu_vgik_cinema",
        "category": "Education",
        "title": "Высшее образование: Режиссура кино",
        "content": "Диплом кинорежиссера (киношкола визуальных искусств). Классическая школа киноязыка, драматургии и визуальной композиции.",
        "priority": 1,
        "status": "confirmed",
        "source": "curated",
        "confidence": 1.0,
        "is_active": 1,
    },

    # 6. Languages
    {
        "fact_id": "lang_multilingual",
        "category": "Languages",
        "title": "Языки общения: English, Français",
        "content": "Свободный английский (fluent), французский (French). Легко находит общий язык с людьми разных культур.",
        "priority": 1,
        "status": "confirmed",
        "source": "curated",
        "confidence": 1.0,
        "is_active": 1,
    },

    # 7. Technology & Entrepreneurship
    {
        "fact_id": "tech_fitness_ml_tools",
        "category": "Technology & Entrepreneurship",
        "title": "IT-разработка, AI и Fitness Web Tools",
        "content": "Разрабатывает веб-сервисы в сфере фитнеса и машинного обучения. Интересуется практическим применением LLM, автономными агентами и автоматизацией.",
        "priority": 2,
        "status": "confirmed",
        "source": "curated",
        "confidence": 1.0,
        "is_active": 1,
    },
]

# Начальные эталонные якоря совместимости
DEFAULT_ANCHORS = [
    {
        "anchor_id": "anchor_photo",
        "name": "Photography",
        "category": "Creative & Visual",
        "keywords": ["photo", "photography", "camera", "street photography", "portrait", "visual art", "photographer", "fujifilm", "leica"],
        "description": "Стрит-фотография, визуальное искусство, композиция, свет, камеры",
        "priority": 2,
        "is_active": 1,
    },
    {
        "anchor_id": "anchor_cinema",
        "name": "Cinema & Filmmaking",
        "category": "Creative & Visual",
        "keywords": ["film", "cinema", "movies", "director", "filmmaking", "screenwriting", "video", "documentary", "cannes", "arthouse", "editing"],
        "description": "Кинематограф, режиссура, авторское кино, Каннский фестиваль, видеомонтаж",
        "priority": 2,
        "is_active": 1,
    },
    {
        "anchor_id": "anchor_mountains",
        "name": "Mountains / Outdoor / Ski",
        "category": "Nature & Adventure",
        "keywords": ["mountain", "mountains", "hiking", "trek", "trekking", "climb", "ski", "skiing", "snowboard", "alps", "nature", "outdoor", "trail", "nepal", "everest", "annapurna", "sports", "sport", "soccer", "futsal", "football", "badminton", "tennis", "running", "cycling", "biking", "fitness", "coach", "coaching"],
        "description": "Горнолыжный спорт, активный спорт, треккинг в Непале, перевалы, горная природа",
        "priority": 2,
        "is_active": 1,
    },
    {
        "anchor_id": "anchor_music",
        "name": "Music & Sound",
        "category": "Culture & Arts",
        "keywords": ["music", "piano", "classical", "ambient", "guitar", "instrument", "instruments", "koto", "shamisen", "traditional instrument", "concert", "vinyl", "audio", "jazz", "acoustic", "synthesizer", "band", "musician"],
        "description": "Фортепиано, традиционные инструменты (кото), эмбиент, классическая музыка, акустика, джаз, живой звук",
        "priority": 1,
        "is_active": 1,
    },
    {
        "anchor_id": "anchor_tech",
        "name": "Technology / Startups / AI",
        "category": "Technology & Business",
        "keywords": ["tech", "software", "developer", "coding", "programming", "python", "ai", "machine learning", "startup", "engineering", "web", "data science"],
        "description": "IT-разработка, искусственный интеллект, стартапы, автоматизация, инженерия",
        "priority": 2,
        "is_active": 1,
    },
    {
        "anchor_id": "anchor_food",
        "name": "Food & Cooking",
        "category": "Lifestyle",
        "keywords": ["food", "cook", "cooking", "culinary", "cuisine", "gastronomy", "local food", "restaurant", "restaurants", "tasting", "delicious", "sweets", "tictac", "dining", "delicacies", "coffee", "tea", "wine", "ramen", "baking", "sushi"],
        "description": "Кулинарные традиции, рестораны, дегустация блюд, локальные вкусы, домашняя кухня, кофе/чай, сезонная еда",
        "priority": 1,
        "is_active": 1,
    },
    {
        "anchor_id": "anchor_culture_lang",
        "name": "Culture & Languages",
        "category": "Culture & Arts",
        "keywords": ["language", "languages", "culture", "cultural exchange", "expat", "linguistics", "travel stories", "multilingual", "asia", "hong kong", "japan", "english", "japanese", "french", "spanish", "practicing english", "learning english", "learning japanese", "language exchange", "bylocals", "recommendations", "local guide", "local spots", "cross-cultural"],
        "description": "Межкультурный диалог, языковая практика (EN/FR/RU), рекомендации местных мест, жизнь в Азии, глубокие путешествия",
        "priority": 1,
        "is_active": 1,
    },
]


class PersonalContextManager:
    """Менеджер управления персональной базой знаний и якорями совместимости."""

    def __init__(self):
        self.ensure_default_facts()
        self.ensure_default_anchors()

    def ensure_default_facts(self) -> None:
        """Инициализирует и синхронизирует эталонные факты Андрея по 7 категориям."""
        with db_session() as conn:
            cur = conn.cursor()
            now = datetime.now().isoformat()
            for f in DEFAULT_FACTS:
                cur.execute(
                    """
                    INSERT INTO personal_context_facts 
                    (fact_id, category, title, content, priority, status, source, confidence, is_active, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(fact_id) DO UPDATE SET
                        category = excluded.category,
                        priority = COALESCE(personal_context_facts.priority, excluded.priority);
                    """,
                    (
                        f["fact_id"],
                        f["category"],
                        f["title"],
                        f["content"],
                        f.get("priority", 1),
                        f["status"],
                        f["source"],
                        f["confidence"],
                        f["is_active"],
                        now,
                        now,
                    ),
                )

    def ensure_default_anchors(self) -> None:
        """Инициализирует и синхронизирует базовые якоря совместимости."""
        with db_session() as conn:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM compatibility_anchors;")
            if cur.fetchone()[0] == 0:
                now = datetime.now().isoformat()
                for a in DEFAULT_ANCHORS:
                    cur.execute(
                        """
                        INSERT OR IGNORE INTO compatibility_anchors
                        (anchor_id, name, category, keywords_json, description, priority, is_active, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
                        """,
                        (
                            a["anchor_id"],
                            a["name"],
                            a["category"],
                            json.dumps(a["keywords"]),
                            a.get("description", ""),
                            a.get("priority", 1),
                            a.get("is_active", 1),
                            now,
                            now,
                        ),
                    )

    # -------------------------------------------------------------
    # PERSONAL PROFILE FACTS CRUD
    # -------------------------------------------------------------

    def get_all_facts(self, active_only: bool = False, category: Optional[str] = None) -> List[Dict[str, Any]]:
        """Возвращает список фактов с сортировкой по приоритету (DESC) и категории."""
        with db_session() as conn:
            cur = conn.cursor()
            query = "SELECT * FROM personal_context_facts WHERE 1=1"
            params: List[Any] = []
            if active_only:
                query += " AND is_active = 1"
            if category:
                query += " AND category = ?"
                params.append(category)
            query += " ORDER BY category ASC, priority DESC, title ASC;"
            cur.execute(query, params)
            rows = cur.fetchall()
            return [dict(r) for r in rows]

    def get_fact(self, fact_id: str) -> Optional[Dict[str, Any]]:
        """Возвращает факт по ID."""
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
        priority: int = 1,
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
                (fact_id, category, title, content, priority, status, source, confidence, is_active, valid_until, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(fact_id) DO UPDATE SET
                    category = excluded.category,
                    title = excluded.title,
                    content = excluded.content,
                    priority = excluded.priority,
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
                    int(priority),
                    status,
                    source,
                    float(confidence),
                    1 if is_active else 0,
                    valid_until,
                    now,
                    now,
                ),
            )
            return self.get_fact(fact_id) or {}

    def toggle_fact_active(self, fact_id: str, is_active: bool) -> bool:
        """Переключает статус активности факта."""
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

    # -------------------------------------------------------------
    # COMPATIBILITY ANCHORS CRUD
    # -------------------------------------------------------------

    def get_all_anchors(self, active_only: bool = False) -> List[Dict[str, Any]]:
        """Возвращает список якорей совместимости."""
        with db_session() as conn:
            cur = conn.cursor()
            query = "SELECT * FROM compatibility_anchors"
            if active_only:
                query += " WHERE is_active = 1"
            query += " ORDER BY priority DESC, name ASC;"
            cur.execute(query)
            rows = cur.fetchall()
            res = []
            for r in rows:
                item = dict(r)
                if isinstance(item.get("keywords_json"), str):
                    try:
                        item["keywords"] = json.loads(item["keywords_json"])
                    except Exception:
                        item["keywords"] = []
                else:
                    item["keywords"] = []
                res.append(item)
            return res

    def get_anchor(self, anchor_id: str) -> Optional[Dict[str, Any]]:
        """Возвращает якорь по ID."""
        with db_session() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM compatibility_anchors WHERE anchor_id = ?;", (anchor_id,))
            row = cur.fetchone()
            if not row:
                return None
            item = dict(row)
            try:
                item["keywords"] = json.loads(item.get("keywords_json", "[]"))
            except Exception:
                item["keywords"] = []
            return item

    def upsert_anchor(
        self,
        anchor_id: str,
        name: str,
        category: str,
        keywords: Union[List[str], str],
        description: str = "",
        priority: int = 1,
        is_active: bool = True,
    ) -> Dict[str, Any]:
        """Создает или обновляет якорь совместимости."""
        now = datetime.now().isoformat()
        if isinstance(keywords, str):
            kw_list = [k.strip() for k in keywords.split(",") if k.strip()]
        else:
            kw_list = [str(k).strip() for k in keywords if str(k).strip()]
        kw_json = json.dumps(kw_list, ensure_ascii=False)

        with db_session() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO compatibility_anchors
                (anchor_id, name, category, keywords_json, description, priority, is_active, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(anchor_id) DO UPDATE SET
                    name = excluded.name,
                    category = excluded.category,
                    keywords_json = excluded.keywords_json,
                    description = excluded.description,
                    priority = excluded.priority,
                    is_active = excluded.is_active,
                    updated_at = excluded.updated_at;
                """,
                (
                    anchor_id,
                    name,
                    category,
                    kw_json,
                    description,
                    int(priority),
                    1 if is_active else 0,
                    now,
                    now,
                ),
            )
            return self.get_anchor(anchor_id) or {}

    def toggle_anchor_active(self, anchor_id: str, is_active: bool) -> bool:
        """Включает или выключает якорь совместимости."""
        now = datetime.now().isoformat()
        with db_session() as conn:
            cur = conn.cursor()
            cur.execute(
                "UPDATE compatibility_anchors SET is_active = ?, updated_at = ? WHERE anchor_id = ?;",
                (1 if is_active else 0, now, anchor_id),
            )
            return cur.rowcount > 0

    def delete_anchor(self, anchor_id: str) -> bool:
        """Удаляет якорь совместимости."""
        with db_session() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM compatibility_anchors WHERE anchor_id = ?;", (anchor_id,))
            return cur.rowcount > 0

    # -------------------------------------------------------------
    # IMPORT & EXPORT
    # -------------------------------------------------------------

    def export_json(self) -> str:
        """Экспорт фактов и якорей в структурированный JSON."""
        data = {
            "facts": self.get_all_facts(active_only=False),
            "anchors": self.get_all_anchors(active_only=False),
        }
        return json.dumps(data, ensure_ascii=False, indent=2)

    def import_json(self, json_str: str) -> Dict[str, int]:
        """Импорт фактов и якорей из JSON строки."""
        data = json.loads(json_str)
        facts_count = 0
        anchors_count = 0

        facts_list = data.get("facts", data) if isinstance(data, dict) else data
        if isinstance(facts_list, list):
            for it in facts_list:
                self.upsert_fact(
                    fact_id=it["fact_id"],
                    category=it.get("category", "Personal Background"),
                    title=it.get("title", "Untitled"),
                    content=it.get("content", ""),
                    priority=it.get("priority", 1),
                    status=it.get("status", "confirmed"),
                    source=it.get("source", "import"),
                    confidence=float(it.get("confidence", 1.0)),
                    is_active=bool(it.get("is_active", True)),
                    valid_until=it.get("valid_until"),
                )
                facts_count += 1

        if isinstance(data, dict) and "anchors" in data and isinstance(data["anchors"], list):
            for an in data["anchors"]:
                self.upsert_anchor(
                    anchor_id=an["anchor_id"],
                    name=an.get("name", "Untitled Anchor"),
                    category=an.get("category", "General"),
                    keywords=an.get("keywords", []),
                    description=an.get("description", ""),
                    priority=an.get("priority", 1),
                    is_active=bool(an.get("is_active", True)),
                )
                anchors_count += 1

        return {"imported_facts": facts_count, "imported_anchors": anchors_count}

    def select_relevant_facts(
        self,
        host_profile: Dict[str, Any],
        max_facts: int = 2,
    ) -> List[Dict[str, Any]]:
        """
        Сохранена для обратной совместимости.
        Делегирует подбор фактов в ContextAssembler.
        """
        try:
            from context_assembler import get_context_assembler
            assembler = get_context_assembler()
            assembled = assembler.assemble_context(host_profile)
            return assembled.get("selected_cards", [])
        except Exception as e:
            logger.warning(f"Fallback selection in select_relevant_facts: {e}")
            all_active = self.get_all_facts(active_only=True)
            return all_active[:max_facts]


_pcm_instance: Optional[PersonalContextManager] = None


def get_personal_context_manager() -> PersonalContextManager:
    global _pcm_instance
    if _pcm_instance is None:
        _pcm_instance = PersonalContextManager()
    return _pcm_instance
