"""
personal_context.py - Канонический фасад персонального контекста, делегирующий все операции в `core.context`.
"""

from core.context import (
    DEFAULT_ANCHORS,
    DEFAULT_FACTS,
    DEFAULT_GUEST_CONTEXT,
    GUEST,
    PERSONAL_CATEGORIES,
    GuestProfile,
    PersonalContextManager,
    get_personal_context_manager,
    validate_text_purity,
)

__all__ = [
    "GuestProfile",
    "GUEST",
    "validate_text_purity",
    "DEFAULT_GUEST_CONTEXT",
    "PERSONAL_CATEGORIES",
    "DEFAULT_FACTS",
    "DEFAULT_ANCHORS",
    "PersonalContextManager",
    "get_personal_context_manager",
]
