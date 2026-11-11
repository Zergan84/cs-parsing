"""
core/context.py - Единый источник истины о госте (Guest Context).
Предоставляет проверенные факты для:
1. Модуля COLLECTOR (для скоринга 1-5★).
2. Модуля QUEUE (для генерации персонализированных писем без галлюцинаций).
"""

from typing import List, Dict, Any, Set
from pydantic import BaseModel, Field


class GuestProfile(BaseModel):
    name: str = "Andrey"
    origin_city: str = "Geneva"
    origin_country: str = "Switzerland"
    departure_city: str = "Geneva"
    secondary_base: str = "Courchevel, French Alps"
    languages: List[str] = ["English", "Russian", "French"]
    
    # Ключевые сферы интересов гостя для матчинга
    interests: Dict[str, List[str]] = Field(default_factory=lambda: {
        "outdoors": ["skiing", "ski", "mountains", "alps", "hiking", "trekking", "everest", "nepal", "motorcycle", "bmw gs"],
        "arts_media": ["cinema", "film", "filmmaking", "directing", "cannes", "photography", "street photography", "visual arts"],
        "music": ["piano", "classical music", "jazz", "sound design"],
        "culture_travel": ["japan", "travel", "culture", "couchsurfing", "switzerland", "alps", "france"],
        "food": ["cooking", "coffee", "tea", "cuisine", "wine"],
        "tech": ["technology", "ai", "startups", "software"]
    })

    # Проверенные факты для генерации писем
    verified_facts: List[Dict[str, str]] = Field(default_factory=lambda: [
        {
            "category": "cinema_arts",
            "fact": "Studied film directing and practice street photography; deeply interested in visual culture.",
            "keywords": ["film", "cinema", "directing", "movies", "photography", "art", "visual", "design"]
        },
        {
            "category": "mountains_ski",
            "fact": "Professional ski instructor based in Geneva and Courchevel (French Alps); passionate about mountains.",
            "keywords": ["ski", "skiing", "mountains", "alps", "snowboard", "winter sports", "outdoor", "nature"]
        },
        {
            "category": "music",
            "fact": "Play the piano and appreciate acoustic music, sound design, and classical compositions.",
            "keywords": ["piano", "music", "classical", "jazz", "instruments", "sound"]
        },
        {
            "category": "travel_cs",
            "fact": "Couchsurfing member for over 10 years, quiet and tidy guest who respects house rules and local routines.",
            "keywords": ["couchsurfing", "hosting", "hospitality", "traveler", "cultures", "backpacking"]
        },
        {
            "category": "trekking",
            "fact": "High-altitude trekker having crossed Thorong La Pass (5,416m) and trekked to Everest Base Camp.",
            "keywords": ["trekking", "hiking", "nepal", "himalayas", "everest", "climbing", "trails"]
        }
    ])

    # Строгий стоп-лист (запрет галлюцинаций)
    forbidden_terms: List[str] = [
        "moscow", "russia", "vgik", "russian federation",
        "москва", "россия", "вгик"
    ]


# Синглтон профиля гостя
GUEST = GuestProfile()


def validate_text_purity(text: str) -> Tuple_Validation:
    """Проверяет текст на отсутствие запрещенных географических галлюцинаций."""
    text_lower = text.lower()
    violations = [term for term in GUEST.forbidden_terms if term in text_lower]
    return len(violations) == 0, violations


class Tuple_Validation:
    def __init__(self, is_valid: bool, violations: List[str]):
        self.is_valid = is_valid
        self.violations = violations
    def __bool__(self):
        return self.is_valid
