"""
core/models.py - Единые канонические доменные модели (Pydantic) для всей системы cs-parsing.

Покрывает все доменные сущности:
1. Trip (Поездка — Корень Агрегата)
2. HostCard (Хост Couchsurfing + скоринг + состояние + черновик + ответы)
3. ProfileAnalysis (Глубокий анализ профиля хоста)
4. PersonalFact & CompatibilityAnchor (Персональный контекст заявителя)
5. MessageDraft (Версионированное сгенерированное письмо с QC)
6. SendingAttempt (Запись аудиторской истории отправки)
7. ConversationItem (Диалог в инбоксе)
8. AuditEvent (Событие сквозного аудита)
"""

from datetime import datetime
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


class QueueStatus(str, Enum):
    DRAFT = "Draft"
    NEEDS_REGENERATION = "Needs_Regeneration"
    NAME_REVIEW_REQUIRED = "Name_Review_Required"
    QC_REVIEW_REQUIRED = "QC_Review_Required"
    QUEUED = "Queued"
    APPROVED = "Approved"
    SENDING = "Sending"
    VERIFICATION_REQUIRED = "Verification_Required"
    SENT = "Sent"
    SKIPPED = "Skipped"
    FAILED = "Failed"
    DECLINED = "Declined"


class AcceptingState(str, Enum):
    ACCEPTING = "accepting"
    MAYBE = "maybe"
    NOT_ACCEPTING = "not_accepting"
    UNKNOWN = "unknown"


class ReplyStatus(str, Enum):
    NO_REPLY = "no_reply"
    ACCEPTED = "accepted"
    MAYBE = "maybe"
    DECLINED = "declined"
    REPLIED = "replied"


class Trip(BaseModel):
    id: str
    url: str = ""
    country: str = "Japan"
    city: str
    arrival_date: str
    departure_date: str
    dates_text: Optional[str] = None
    is_active: bool = True
    created_at: str = Field(default_factory=lambda: datetime.now().isoformat())
    updated_at: Optional[str] = None


class HostCard(BaseModel):
    host_id: str
    username: str
    trip_id: str
    name: str
    location: str = ""
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

    created_at: str = Field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now().isoformat())


class ProfileAnalysis(BaseModel):
    """Структурированный результат глубокого анализа профиля хоста."""
    host_id: str
    estimated_gender: str = "Unknown"
    host_type: str = "solo"
    can_host_overnight: bool = True
    hosting_verdict_reason: Optional[str] = None
    hosting_confidence: float = 1.0
    sleeping_arrangement: str = "Not specified"
    house_rules: str = "Standard rules"
    secret_code_detected: bool = False
    secret_code_hint: Optional[str] = None
    conversation_hooks: List[str] = Field(default_factory=list)
    compatibility_points: List[Dict[str, Any]] = Field(default_factory=list)
    conversation_strategy: str = "Grounded CS connection"
    compatibility_score: Optional[float] = None
    analyzed_at: str = Field(default_factory=lambda: datetime.now().isoformat())


class PersonalFact(BaseModel):
    """Структурированный факт персонального контекста заявителя."""
    fact_id: str
    category: str
    title: str
    content: str
    priority: int = 1
    status: str = "confirmed"
    source: str = "curated"
    confidence: float = 1.0
    is_active: bool = True
    valid_until: Optional[str] = None
    created_at: str = Field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now().isoformat())


class CompatibilityAnchor(BaseModel):
    """Якорь совместимости между интересами хоста и фактами заявителя."""
    anchor_id: str
    name: str
    category: str
    keywords: List[str] = Field(default_factory=list)
    description: str = ""
    priority: int = 1
    is_active: bool = True
    created_at: str = Field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now().isoformat())


class MessageDraft(BaseModel):
    """Сгенерированное персонализированное сообщение с метаданными QC и Context Assembler."""
    id: str
    host_id: str
    check_in: str
    check_out: str
    message_text: str
    word_count: int
    hook_used: Optional[str] = None
    context_facts_used: List[str] = Field(default_factory=list)
    anchor_used: Optional[str] = None
    host_fact_used: Optional[str] = None
    user_fact_used: Optional[str] = None
    confidence: Optional[float] = None
    fallback_used: bool = False
    qc_passed: bool = True
    qc_notes: Optional[str] = None
    is_active: bool = True
    created_at: str = Field(default_factory=lambda: datetime.now().isoformat())


class SendingAttempt(BaseModel):
    """Аудиторская запись о попытке или успешной отправке запроса."""
    id: Optional[int] = None
    host_id: str
    username: str
    status: str                  # SENT_SUCCESS | DRY_RUN_SUCCESS | FAILED | ALREADY_SENT | VERIFICATION_REQUIRED
    http_status: Optional[int] = None
    mode: str = "live"           # live | dry_run | sync_dedup
    proof_screenshot: Optional[str] = None
    before_screenshot: Optional[str] = None
    button_selector: Optional[str] = None
    notes: Optional[str] = None
    error_details: Optional[str] = None
    timestamp: str = Field(default_factory=lambda: datetime.now().isoformat())


class ConversationItem(BaseModel):
    """Входящий диалог с хостом в Инбоксе."""
    id: Optional[int] = None
    host_username: str
    host_name: str
    host_avatar_url: Optional[str] = None
    location: str = "Tokyo, Japan"
    last_message_received: str
    last_message_timestamp: str
    status: str = "Host Replied"
    category: str = "CHAT_INTERESTED"
    suggested_reply: Optional[str] = None
    thread_history: List[Dict[str, Any]] = Field(default_factory=list)
    tone_used: str = "normal"
    created_at: str = Field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now().isoformat())


class AuditEvent(BaseModel):
    timestamp: str
    trip_id: str
    module: str  # TRIP, SEARCH, COLLECTOR, SELECTOR, QUEUE, AUTOPILOT, INBOX
    event: str
    host_id: Optional[str] = None
    details: Optional[Dict[str, Any]] = None
