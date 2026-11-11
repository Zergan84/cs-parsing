"""
core/context.py - Единый источник истины о госте (Guest Context), Персональном Контексте и Якорях Совместимости.

Объединяет:
1. `GuestProfile` (`GUEST`) и `validate_text_purity` — константы гостя и анти-галлюцинационный фильтр.
2. `PersonalContextManager` (`get_personal_context_manager`) — CRUD над `personal_context_facts` и `compatibility_anchors`.
3. `ContextAssembler` (`get_context_assembler`) — сопоставление досье хоста с фактами и якорями заявителя.
"""

import json
import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple, Union
from pydantic import BaseModel, Field

from core.database import db_session, get_host_dossier, get_setting

logger = logging.getLogger("CoreContext")


# =====================================================================
# 1. GUEST PROFILE & ANTI-HALLUCINATION PURITY GUARD
# =====================================================================

class GuestProfile(BaseModel):
    name: str = "Andrey"
    origin_city: str = "Geneva"
    origin_country: str = "Switzerland"
    departure_city: str = "Geneva"
    secondary_base: str = "Courchevel, French Alps"
    languages: List[str] = ["English", "Russian", "French"]

    interests: Dict[str, List[str]] = Field(default_factory=lambda: {
        "outdoors": ["skiing", "ski", "mountains", "alps", "hiking", "trekking", "everest", "nepal", "motorcycle", "bmw gs"],
        "arts_media": ["cinema", "film", "filmmaking", "directing", "cannes", "photography", "street photography", "visual arts"],
        "music": ["piano", "classical music", "jazz", "sound design"],
        "culture_travel": ["japan", "travel", "culture", "couchsurfing", "switzerland", "alps", "france"],
        "food": ["cooking", "coffee", "tea", "cuisine", "wine"],
        "tech": ["technology", "ai", "startups", "software"],
    })

    verified_facts: List[Dict[str, Any]] = Field(default_factory=lambda: [
        {
            "category": "cinema_arts",
            "fact": "Studied film directing and practice street photography; deeply interested in visual culture.",
            "keywords": ["film", "cinema", "directing", "movies", "photography", "art", "visual", "design"],
        },
        {
            "category": "mountains_ski",
            "fact": "Professional ski instructor based in Geneva and Courchevel (French Alps); passionate about mountains.",
            "keywords": ["ski", "skiing", "mountains", "alps", "snowboard", "winter sports", "outdoor", "nature"],
        },
        {
            "category": "music",
            "fact": "Play the piano and appreciate acoustic music, sound design, and classical compositions.",
            "keywords": ["piano", "music", "classical", "jazz", "instruments", "sound"],
        },
        {
            "category": "travel_cs",
            "fact": "Couchsurfing member for over 10 years, quiet and tidy guest who respects house rules and local routines.",
            "keywords": ["couchsurfing", "hosting", "hospitality", "traveler", "cultures", "backpacking"],
        },
        {
            "category": "trekking",
            "fact": "High-altitude trekker having crossed Thorong La Pass (5,416m) and trekked to Everest Base Camp.",
            "keywords": ["trekking", "hiking", "nepal", "himalayas", "everest", "climbing", "trails"],
        },
    ])

    forbidden_terms: List[str] = [
        "moscow", "russia", "vgik", "russian federation",
        "москва", "россия", "вгик",
    ]


GUEST = GuestProfile()


class Tuple_Validation:
    def __init__(self, is_valid: bool, violations: List[str]):
        self.is_valid = is_valid
        self.violations = violations

    def __bool__(self):
        return self.is_valid


def validate_text_purity(text: str) -> Tuple[bool, List[str]]:
    """Проверяет текст на отсутствие запрещенных географических галлюцинаций."""
    text_lower = text.lower()
    violations = [term for term in GUEST.forbidden_terms if term in text_lower]
    return len(violations) == 0, violations


# =====================================================================
# 2. DEFAULT PERSONAL FACTS & COMPATIBILITY ANCHORS
# =====================================================================

DEFAULT_GUEST_CONTEXT = (
    "Andrey, professional ski instructor based in Geneva (Switzerland) and Courchevel (French Alps), flying to Japan from Geneva. "
    "10+ years CS veteran (overland Geneva-Paris, Alps, Morocco). High-altitude trekker (Thorong La Pass 5416m with son Mark in 2026, "
    "solo Everest Base Camp). Film director (Cannes 2024), plays piano, rides BMW GS motorcycle. "
    "Independent, quiet, out exploring all day."
)

PERSONAL_CATEGORIES = [
    "Professional Background",
    "Personal Background",
    "Hobbies & Interests",
    "Travel Experience",
    "Education",
    "Languages",
    "Technology & Entrepreneurship",
]

DEFAULT_FACTS: List[Dict[str, Any]] = [
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

DEFAULT_ANCHORS: List[Dict[str, Any]] = [
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


# =====================================================================
# 3. PERSONAL CONTEXT MANAGER (CRUD FOR FACTS & ANCHORS)
# =====================================================================

class PersonalContextManager:
    """Канонический менеджер управления персональной базой знаний и якорями совместимости."""

    def __init__(self):
        self.ensure_default_facts()
        self.ensure_default_anchors()

    def ensure_default_facts(self) -> None:
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

    def get_all_facts(self, active_only: bool = False, category: Optional[str] = None) -> List[Dict[str, Any]]:
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
            return [dict(r) for r in cur.fetchall()]

    def get_fact(self, fact_id: str) -> Optional[Dict[str, Any]]:
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
        now = datetime.now().isoformat()
        with db_session() as conn:
            cur = conn.cursor()
            cur.execute(
                "UPDATE personal_context_facts SET is_active = ?, updated_at = ? WHERE fact_id = ?;",
                (1 if is_active else 0, now, fact_id),
            )
            return cur.rowcount > 0

    def delete_fact(self, fact_id: str) -> bool:
        with db_session() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM personal_context_facts WHERE fact_id = ?;", (fact_id,))
            return cur.rowcount > 0

    def get_all_anchors(self, active_only: bool = False) -> List[Dict[str, Any]]:
        with db_session() as conn:
            cur = conn.cursor()
            query = "SELECT * FROM compatibility_anchors"
            if active_only:
                query += " WHERE is_active = 1"
            query += " ORDER BY priority DESC, name ASC;"
            cur.execute(query)
            res = []
            for r in cur.fetchall():
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
        now = datetime.now().isoformat()
        with db_session() as conn:
            cur = conn.cursor()
            cur.execute(
                "UPDATE compatibility_anchors SET is_active = ?, updated_at = ? WHERE anchor_id = ?;",
                (1 if is_active else 0, now, anchor_id),
            )
            return cur.rowcount > 0

    def delete_anchor(self, anchor_id: str) -> bool:
        with db_session() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM compatibility_anchors WHERE anchor_id = ?;", (anchor_id,))
            return cur.rowcount > 0

    def export_json(self) -> str:
        data = {
            "facts": self.get_all_facts(active_only=False),
            "anchors": self.get_all_anchors(active_only=False),
        }
        return json.dumps(data, ensure_ascii=False, indent=2)

    def import_json(self, json_str: str) -> Dict[str, int]:
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
        try:
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


# =====================================================================
# 4. CONTEXT ASSEMBLER
# =====================================================================

ANCHOR_TO_FACT_MAPPINGS: Dict[str, List[str]] = {
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
        host_id = host.get("id") or host.get("host_id") or host.get("username") or ""
        dossier = get_host_dossier(host_id) if host_id else None

        verified_interests = []
        visual_observations = []
        places = []

        if dossier:
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

        if not verified_interests:
            raw_interests = host.get("interests") or host.get("interests_text")
            if isinstance(raw_interests, list):
                for ri in raw_interests:
                    if ri:
                        verified_interests.append({"name": str(ri).strip(), "source": "interests", "confidence": 0.80})
            elif isinstance(raw_interests, str) and raw_interests.strip():
                for ri in re.split(r"[,;\n]", raw_interests):
                    if ri.strip():
                        verified_interests.append({"name": ri.strip(), "source": "interests", "confidence": 0.80})

        about_text = (host.get("about_me_clean") or host.get("about_me") or host.get("about") or "").strip()
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

        if not city:
            raw_loc = host.get("location")
            if isinstance(raw_loc, dict):
                city = raw_loc.get("city") or raw_loc.get("slug") or "Kyoto"
            elif isinstance(raw_loc, str) and raw_loc.strip():
                city = raw_loc.split(",")[0].strip()
            else:
                city = host.get("city") or get_setting("default_city", "Kyoto") or "Kyoto"

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
        signals = self._extract_host_signals(host)
        travel = self.assemble_travel_context(host, trip_context)

        active_facts = self.pcm.get_all_facts(active_only=True)
        active_anchors = self.pcm.get_all_anchors(active_only=True)
        facts_by_id = {f["fact_id"]: f for f in active_facts}

        candidate_intersections: List[Dict[str, Any]] = []

        for anchor in active_anchors:
            anchor_name = anchor["name"]
            keywords = [k.lower().strip() for k in anchor.get("keywords", []) if k.strip()]
            anchor_prio = int(anchor.get("priority", 1))

            matched_signal = None
            evidence_text = ""
            source = ""
            conf = 0.0

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

            if not matched_signal and signals["about_text"]:
                bio_lower = signals["about_text"].lower()
                occ_lower = signals["occupation"].lower()
                combined_text = f"{bio_lower} {occ_lower}"

                for kw in keywords:
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

            if matched_signal and evidence_text:
                preferred_fact_ids = ANCHOR_TO_FACT_MAPPINGS.get(anchor["anchor_id"], [])
                chosen_fact = None
                for fid in preferred_fact_ids:
                    if fid in facts_by_id:
                        chosen_fact = facts_by_id[fid]
                        break
                if not chosen_fact:
                    for f in active_facts:
                        content_lower = f["content"].lower() + " " + f["title"].lower()
                        if any(kw in content_lower for kw in keywords):
                            chosen_fact = f
                            break

                if chosen_fact:
                    score = (anchor_prio * 2.0) + (int(chosen_fact.get("priority", 1)) * 1.5) + (conf * 3.0)
                    if source in ("photo/LLaVA", "interests"):
                        score += 2.0

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

        candidate_intersections.sort(key=lambda x: x["_score"], reverse=True)

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
            from core.collector import get_profile_analyzer
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
                fallback_used = True
                respect_fact = facts_by_id.get("pb_lifestyle_respect")
                lang_fact = facts_by_id.get("lang_multilingual")
                fallback_candidates = [f for f in (respect_fact, lang_fact) if f]
                selected_cards = fallback_candidates[:2]

        llm_instructions = self._format_llm_instructions(
            host_name=host.get("clean_name") or host.get("name") or "Host",
            travel=travel,
            intersections=unique_intersections,
            selected_cards=selected_cards,
            fallback_used=fallback_used,
        )

        return {
            "host_name": host.get("clean_name") or host.get("name") or "Host",
            "host_id": host.get("id") or host.get("host_id") or host.get("username") or "",
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
        city = travel["city"]
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
