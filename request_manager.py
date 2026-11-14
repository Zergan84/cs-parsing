"""
request_manager.py - Канонический фасад управления очередью, делегирующий все операции в `core.queue`.
"""

from core.queue import (
    BLACKLIST_PHRASES,
    CURATED_FILE,
    DEFAULT_CHECK_IN,
    DEFAULT_CHECK_OUT,
    DEFAULT_CONTEXT,
    RAW_FILE,
    SENT_HISTORY_FILE,
    SENT_LOG_FILE,
    QueueEngine,
    RequestQueueManager,
    build_personalized_message,
    get_queue_manager,
    is_blacklisted,
    validate_message_qc,
)

__all__ = [
    "BLACKLIST_PHRASES",
    "CURATED_FILE",
    "RAW_FILE",
    "SENT_HISTORY_FILE",
    "SENT_LOG_FILE",
    "DEFAULT_CHECK_IN",
    "DEFAULT_CHECK_OUT",
    "DEFAULT_CONTEXT",
    "is_blacklisted",
    "build_personalized_message",
    "validate_message_qc",
    "QueueEngine",
    "RequestQueueManager",
    "get_queue_manager",
]
