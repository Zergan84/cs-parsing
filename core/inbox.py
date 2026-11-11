"""
core/inbox.py - Модуль INBOX (Ответы хостов + Автоответ).
Отвечает за:
1. Фиксацию входящих ответов от хостов (state = 'inbox') и синхронизацию с `inbox_conversations`.
2. Привязку диалога к хосту и активному Trip.
3. Генерацию контекстных вариантов автоответа (время прибытия, благодарность, встреча).
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.context import GUEST
from core.database import (
    get_connection,
    get_inbox_conversation,
    get_inbox_conversations,
    log_audit,
    mark_host_declined,
    save_inbox_conversation,
    update_inbox_conversation,
)
from core.models import HostState


class InboxEngine:
    """Движок управления входящими ответами и перепиской."""

    def __init__(self, trip_id: str, db_file: Optional[Path] = None):
        self.trip_id = trip_id
        self.db_file = db_file

    def list_conversations(self) -> List[Dict[str, Any]]:
        """Возвращает всех хостов, с которыми есть входящий контакт (state = 'inbox')."""
        with get_connection(self.db_file) as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT host_id, username, name, location, profile_url, avatar_url, updated_at
                FROM core_hosts
                WHERE trip_id = ? AND state = 'inbox'
                ORDER BY updated_at DESC;
                """,
                (self.trip_id,),
            )
            return [dict(r) for r in cur.fetchall()]

    def record_incoming_reply(self, host_id: str, message_text: str) -> bool:
        """Регистрирует ответ хоста, переводит статус в 'inbox' и синхронизирует `inbox_conversations`."""
        now_iso = datetime.now().isoformat()
        with get_connection(self.db_file) as conn:
            cur = conn.cursor()
            cur.execute(
                """
                UPDATE core_hosts
                SET state = 'inbox', updated_at = ?
                WHERE trip_id = ? AND (host_id = ? OR username = ?);
                """,
                (now_iso, self.trip_id, host_id, host_id),
            )
            affected = cur.rowcount > 0
            cur.execute(
                "SELECT username, name, avatar_url, location FROM core_hosts WHERE trip_id = ? AND (host_id = ? OR username = ?) LIMIT 1;",
                (self.trip_id, host_id, host_id),
            )
            ch = cur.fetchone()

        if ch:
            save_inbox_conversation(
                host_username=ch["username"] or host_id,
                host_name=ch["name"] or host_id,
                last_message_received=message_text,
                last_message_timestamp=now_iso,
                host_avatar_url=ch["avatar_url"],
                location=ch["location"] or "Japan",
                db_file=self.db_file,
            )

        if affected:
            log_audit(
                trip_id=self.trip_id,
                module="INBOX",
                event="INCOMING_REPLY_RECEIVED",
                host_id=host_id,
                details={"preview": message_text[:100]},
                db_file=self.db_file,
            )
        return affected

    def generate_reply_suggestion(self, host_name: str, incoming_text: str) -> str:
        """Генерирует вежливый ответ на основе контекста гостя."""
        clean_name = host_name.split()[0].capitalize() if host_name else "there"
        inc_lower = incoming_text.lower()

        if "time" in inc_lower or "arrive" in inc_lower or "when" in inc_lower:
            return (
                f"Hi {clean_name},\n\n"
                f"Thank you so much for getting back to me! I expect to arrive in the late afternoon (around 5–6 PM). "
                f"I'm very flexible and happy to adapt to your evening schedule. Looking forward to meeting you!\n\n"
                f"Best,\nAndrey"
            )
        elif "sorry" in inc_lower or "cannot" in inc_lower or "busy" in inc_lower or "away" in inc_lower:
            return (
                f"Hi {clean_name},\n\n"
                f"Thank you for letting me know, and completely understand! Wish you wonderful travels and maybe our paths will cross another time.\n\n"
                f"Warmly,\nAndrey"
            )
        else:
            return (
                f"Hi {clean_name},\n\n"
                f"Thank you so much for the message! That sounds wonderful. I'd love to coordinate details closer to the dates.\n\n"
                f"Warmly,\nAndrey"
            )


__all__ = [
    "InboxEngine",
    "get_inbox_conversations",
    "get_inbox_conversation",
    "save_inbox_conversation",
    "update_inbox_conversation",
    "mark_host_declined",
]
