"""
core/models.py - Pydantic и SQLite схемы для модульного пайплайна.
Каждая сущность строго привязана к trip_id (Корень Агрегата).
"""

from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class HostState(str, Enum):
    DISCOVERED = "discovered"  # Найден в поиске, сохранен в SQLite
    REJECTED = "rejected"      # Отклонен в Селекторе (Pass)
    SELECTED = "selected"      # Отобран в Селекторе (Like / VIP)
    DRAFTED = "drafted"        # Сгенерировано персонализированное письмо
    APPROVED = "approved"      # Утверждено к отправке
    SENDING = "sending"        # В процессе отправки
    SENT = "sent"              # Успешно доставлено в CS API
    INBOX = "inbox"            # Хост ответил (активный диалог)
    SKIPPED = "skipped"        # Пропущен


class Trip(BaseModel):
    id: str
    url: str
    country: str = "Japan"
    city: str
    arrival_date: str
    departure_date: str
    dates_text: Optional[str] = None
    is_active: bool = True
    created_at: str
    updated_at: Optional[str] = None


class HostCard(BaseModel):
    host_id: str
    username: str
    trip_id: str
    name: str
    location: str
    gender: Optional[str] = None
    age: Optional[int] = None
    occupation: Optional[str] = None
    about: Optional[str] = None
    interests: List[str] = Field(default_factory=list)
    languages: List[str] = Field(default_factory=list)
    countries_visited: List[str] = Field(default_factory=list)
    countries_lived: List[str] = Field(default_factory=list)
    profile_url: Optional[str] = None
    avatar_url: Optional[str] = None
    
    # Расширенные показатели профиля хоста (ТЗ)
    is_verified: bool = False
    birth_country: Optional[str] = None
    residence_country: Optional[str] = None
    is_hosting: Optional[str] = "unknown"  # "accepting", "maybe", "not_accepting", "unknown"
    household: Optional[str] = None        # "alone", "family", "flatmates", "partner"
    special_notes: Optional[str] = None    # Спецпослания из био: "accepting 1 person", "no couples", etc.
    
    # Скоринг (1-5 звезд)
    score_stars: int = 1
    score_reasons: List[str] = Field(default_factory=list)
    
    # Состояние в пайплайне
    state: HostState = HostState.DISCOVERED
    
    # Письмо и отправка
    message_draft: Optional[str] = None
    qc_passed: bool = False
    qc_notes: Optional[str] = None
    sent_at: Optional[str] = None
    
    created_at: str
    updated_at: str


class AuditEvent(BaseModel):
    timestamp: str
    trip_id: str
    module: str  # TRIP, SEARCH, COLLECTOR, SELECTOR, QUEUE, AUTOPILOT, INBOX
    event: str
    host_id: Optional[str] = None
    details: Optional[Dict[str, Any]] = None
