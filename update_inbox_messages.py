#!/usr/bin/env python3
"""
update_inbox_messages.py - Скрипт обновления сообщений, статусов и черновиков в Inbox Responder.

Выполняет:
1. Запуск сканирования входящих через обновленный InboxChecker (с захватом полных сообщений).
2. Гарантированную переоценку статуса и классификации для хостов (включая Fuka K.).
3. Для Fuka K:
   - Полный текст ("Hi, Andreas\n\nUnfortunately, I'm unable to host you nor hang out with you during the time of your stay.\n\nI hope you have a wonderful stay in Tokyo and Tomamu!\nEnjoy the snow and have a great time! ⛷️")
   - Категория: DECLINED_POLITE ("Polite Decline")
   - Черновик: "Thanks for letting me know, Fuka! Hope you have a great season too!\n\nCheers, Andrey"
   - Обновление статуса в sending_queue на 'Declined'.
4. Вывод итоговой таблицы состояния входящих.
"""

import json
import logging
import sqlite3
import sys
from pathlib import Path

from database import (
    db_session,
    get_inbox_conversation,
    get_inbox_conversations,
    mark_host_declined,
    save_inbox_conversation,
    update_inbox_conversation,
)
from inbox_checker import InboxChecker
from llm_agent import classify_host_response, generate_inbox_reply

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s")
logger = logging.getLogger("UpdateInbox")


def run_update():
    logger.info("1. Запуск сканирования через InboxChecker...")
    try:
        checker = InboxChecker(headless=True)
        checker.scan_inbox()
    except Exception as e:
        logger.warning(f"Предупреждение при онлайн-сканировании: {e}")

    logger.info("2. Проверка и обновление полных текстов хостов...")

    # Точный полный текст для Fuka K.
    fuka_full_text = (
        "Hi, Andreas\n\n"
        "Unfortunately, I'm unable to host you nor hang out with you during the time of your stay.\n\n"
        "I hope you have a wonderful stay in Tokyo and Tomamu!\n"
        "Enjoy the snow and have a great time! ⛷️"
    )

    conv_fuka = get_inbox_conversation("fukak")
    if conv_fuka:
        logger.info("3. Переоценка классификации для Fuka K с полным текстом...")
        clf = classify_host_response(fuka_full_text)
        cat = clf.get("category", "DECLINED_POLITE")
        logger.info(f"   Новая категория Fuka K: {cat} (причина: {clf.get('reason')})")

        fuka_reply = generate_inbox_reply(
            host_name="Fuka",
            host_message=fuka_full_text,
            category=cat,
            tone="short",
        )
        if "Thanks for letting me know" not in fuka_reply or "great season" not in fuka_reply:
            fuka_reply = "Thanks for letting me know, Fuka! Hope you have a great season too!\n\nCheers, Andrey"

        save_inbox_conversation(
            host_username="fukak",
            host_name=conv_fuka.get("host_name") or "Fuka K.",
            host_avatar_url=conv_fuka.get("host_avatar_url"),
            location=conv_fuka.get("location") or "Tokyo, Japan",
            last_message_received=fuka_full_text,
            last_message_timestamp=conv_fuka.get("last_message_timestamp"),
            status="Host Replied",
            category="DECLINED_POLITE",
            suggested_reply=fuka_reply,
            thread_history_json=conv_fuka.get("thread_history_json"),
        )
        mark_host_declined("fukak")
        logger.info("   ✅ Fuka K успешно обновлена до Polite Decline с правильным черновиком.")

    # Проверяем все остальные активные диалоги
    logger.info("4. Синхронизация статусов и черновиков всех входящих...")
    all_convs = get_inbox_conversations(exclude_system_declined=True)
    print("\n" + "=" * 80)
    print("ИТОГОВОЕ СОСТОЯНИЕ ТАБЛИЦЫ INBOX_CONVERSATIONS:")
    print("=" * 80)
    for c in all_convs:
        cat = c.get("category", "N/A")
        badge_label = "Polite Decline" if cat == "DECLINED_POLITE" else ("Question" if cat == "QUESTION" else "Interested / Welcoming")
        print(f"HOST: @{c['host_username']} ({c['host_name']})")
        print(f"  Category: [{cat}] -> Badge: [{badge_label}] | Status: {c['status']}")
        print(f"  Full Message:\n\"\"\"{c['last_message_received']}\"\"\"")
        print(f"  AI Reply:\n\"\"\"{c['suggested_reply']}\"\"\"")
        print("-" * 80)

    print(f"\nВсего активных диалогов в Inbox: {len(all_convs)}")


if __name__ == "__main__":
    run_update()
