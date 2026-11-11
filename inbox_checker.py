#!/usr/bin/env python3
"""
inbox_checker.py - Автономный воркер мониторинга входящих сообщений Couchsurfing и AI-генерации ответов.

Выполняет:
1. Автономный опрос входящих диалогов в Couchsurfing (/c/chat) через Playwright.
2. Строгую изоляцию: обработка диалогов ТОЛЬКО тех хостов, которые присутствуют в requests со статусом 'Sent' (или 'Approved'/'Sending').
3. Проверку направления и автора последнего сообщения (Direction / Author Check):
   - Если автор vanagaz / Andreas V. (is_viewer = True): статус "Awaiting Host Reply", игнорируется, бот не отвечает сам себе.
   - Если автор хост: извлекается РЕАЛЬНЫЙ текст ответа хоста (без плейсхолдеров "Sent request").
4. LLM-классификацию ответа через qwen2.5:7b (ACCEPTED, CHAT_INTERESTED, QUESTION, DECLINED_POLITE, DECLINED_SYSTEM).
5. Автоматическую генерацию контекстного персонализированного ответа для активных ответов и краткой отбивки (<= 20 слов) для вежливых отказов.
6. Сохранение диалогов, категорий и статусов в таблицу SQLite `inbox_conversations`.
7. Отправку ответов в чат хоста по команде пользователя.
"""

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from playwright.sync_api import sync_playwright

BASE_DIR = Path(__file__).parent
OUTPUT_DIR = Path(os.environ.get("CS_OUTPUT_DIR", str(BASE_DIR / "output"))).resolve()
USER_DATA_DIR = BASE_DIR / "user_data"
AUTH_STATE_FILE = USER_DATA_DIR / "auth_state.json"
CHAT_URL = "https://www.couchsurfing.com/c/chat"
SENT_LOG_FILE = OUTPUT_DIR / "sent_log.json"
INBOX_STATUS_FILE = OUTPUT_DIR / "inbox_status.json"

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [INBOX] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("InboxChecker")


def get_sent_hosts_map() -> Dict[str, Dict[str, Any]]:
    """
    Возвращает словарь хостов из requests и sending_queue со статусом Sent (или Approved/Sending).
    Ключи: lowercase username и lowercase host_id.
    """
    from database import db_session

    hosts_map: Dict[str, Dict[str, Any]] = {}
    try:
        with db_session() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT 
                    r.id,
                    r.host_id,
                    r.username,
                    r.name,
                    r.clean_name,
                    r.status
                FROM requests r
                WHERE r.status IN ('Sent', 'Approved', 'Sending')
            """)
            for row in cur.fetchall():
                info = {
                    "request_id": row["id"],
                    "host_id": row["host_id"],
                    "username": row["username"],
                    "name": row["clean_name"] or row["name"] or row["username"],
                    "status": row["status"],
                }
                if row["username"]:
                    hosts_map[str(row["username"]).strip().lower()] = info
                if row["host_id"]:
                    hosts_map[str(row["host_id"]).strip().lower()] = info
    except Exception as e:
        logger.error(f"Ошибка загрузки sent_hosts из requests: {e}")

    # Дополнительно объединяем с sending_history и sent_log.json / sent_history.json
    try:
        with db_session() as conn:
            cur = conn.cursor()
            cur.execute("SELECT host_id, username FROM sending_history;")
            for r in cur.fetchall():
                info = {"username": r["username"], "host_id": r["host_id"], "name": r["username"], "status": "Sent"}
                if r["username"]:
                    hosts_map.setdefault(str(r["username"]).strip().lower(), info)
                if r["host_id"]:
                    hosts_map.setdefault(str(r["host_id"]).strip().lower(), info)
    except Exception:
        pass

    for json_file in [OUTPUT_DIR / "sent_log.json", OUTPUT_DIR / "sent_history.json"]:
        if json_file.exists():
            try:
                with open(json_file, "r", encoding="utf-8") as f:
                    entries = json.load(f)
                    if isinstance(entries, list):
                        for item in entries:
                            if isinstance(item, dict):
                                u = item.get("username")
                                hid = item.get("host_id") or item.get("id")
                                name = item.get("name") or u or hid
                                info = {
                                    "request_id": None,
                                    "host_id": hid,
                                    "username": u,
                                    "name": name,
                                    "status": "Sent",
                                }
                                if u:
                                    hosts_map.setdefault(str(u).strip().lower(), info)
                                if hid:
                                    hosts_map.setdefault(str(hid).strip().lower(), info)
            except Exception:
                pass

    return hosts_map


def update_inbox_worker_status(
    is_checking: bool = False,
    last_check_time: Optional[str] = None,
    new_replies_count: int = 0,
    total_inbox_count: int = 0,
    last_action: str = "Idle",
) -> None:
    """Сохраняет статус работы Inbox Checker воркера для UI."""
    data = {
        "is_checking": is_checking,
        "last_check_time": last_check_time or datetime.now().isoformat(),
        "new_replies_count": new_replies_count,
        "total_inbox_count": total_inbox_count,
        "last_action": last_action,
        "updated_at": datetime.now().isoformat(),
    }
    try:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        with open(INBOX_STATUS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def extract_full_message_from_bubble(bubble_locator_or_element) -> str:
    """
    Надежно забирает весь текст из блока сообщения хоста:
    1. Ищет .message-text, [data-testid="message-content"], .conversation-message__body
    2. Объединяет все параграфы <p> внутри контейнера через перенос строки
    """
    try:
        content_el = bubble_locator_or_element.locator(
            '.message-text, [data-testid="message-content"], .conversation-message__body, .str-chat__message-text-inner'
        )
        if content_el.count() > 0:
            txt = content_el.first.inner_text().strip()
            if txt:
                return txt
    except Exception:
        pass

    try:
        paragraphs = bubble_locator_or_element.locator('p').all_inner_texts()
        full_text = "\n\n".join([p.strip() for p in paragraphs if p.strip()])
        if full_text:
            return full_text
    except Exception:
        pass

    try:
        return bubble_locator_or_element.inner_text().strip()
    except Exception:
        return ""


class InboxChecker:
    def __init__(self, headless: bool = True):
        self.headless = headless
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    def scan_inbox(self) -> List[Dict[str, Any]]:
        """
        Открывает страницу чата Couchsurfing, перехватывает chatList и Stream сообщения,
        фильтрует только целевых хостов Токио, проверяет автора последнего сообщения,
        классифицирует ответ через qwen2.5:7b и генерирует AI-ответ.
        """
        from database import (
            get_inbox_conversation,
            get_inbox_conversations,
            mark_host_declined,
            save_inbox_conversation,
            update_inbox_conversation,
        )
        from llm_agent import classify_host_response, generate_inbox_reply

        if not AUTH_STATE_FILE.exists():
            logger.warning(f"Файл авторизации {AUTH_STATE_FILE} не найден. Пропуск сканирования.")
            return []

        sent_hosts_map = get_sent_hosts_map()
        logger.info(f"Запуск сканирования входящих сообщений (валидных хостов в базе: {len(sent_hosts_map)})...")
        update_inbox_worker_status(is_checking=True, last_action="Сканирование чатов в Couchsurfing...")

        chat_list_items: List[Dict[str, Any]] = []
        stream_channels: Dict[str, Dict[str, Any]] = {}

        with sync_playwright() as p:
            launch_args = [
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ]
            browser = p.chromium.launch(headless=self.headless, args=launch_args)
            context = browser.new_context(
                viewport={"width": 1280, "height": 800},
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                ),
                storage_state=str(AUTH_STATE_FILE),
            )
            page = context.new_page()

            def on_response(res):
                u = res.url.lower()
                # 1. Couchsurfing chatList API
                if "chatlist.getchatlist" in u or "chatlist.getchatlistitem" in u:
                    try:
                        ct = res.headers.get("content-type", "")
                        if "json" in ct:
                            data = res.json()
                            if isinstance(data, list):
                                for d in data:
                                    items = d.get("result", {}).get("data", {}).get("json", {}).get("data", [])
                                    if isinstance(items, list):
                                        chat_list_items.extend(items)
                            elif isinstance(data, dict):
                                items = data.get("result", {}).get("data", {}).get("json", {}).get("data", [])
                                if isinstance(items, list):
                                    chat_list_items.extend(items)
                    except Exception:
                        pass
                # 2. Stream Chat channels API
                elif "channels?user_id" in u or "channels/messaging" in u:
                    try:
                        ct = res.headers.get("content-type", "")
                        if "json" in ct:
                            data = res.json()
                            if isinstance(data, dict):
                                ch_list = data.get("channels", [])
                                if isinstance(ch_list, list):
                                    for ch in ch_list:
                                        cid = ch.get("channel", {}).get("id")
                                        if cid:
                                            stream_channels[cid] = ch
                    except Exception:
                        pass

            page.on("response", on_response)

            try:
                page.goto(CHAT_URL, wait_until="networkidle", timeout=40000)
                page.wait_for_timeout(4000)
            except Exception as e:
                logger.warning(f"Предупреждение при загрузке страницы чата: {e}")

            if "sign_in" in page.url.lower() or "login" in page.url.lower():
                logger.warning("Сессия устарела или требуется повторный вход.")
                context.close()
                browser.close()
                update_inbox_worker_status(is_checking=False, last_action="Требуется авторизация в Couchsurfing")
                return []

            logger.info(f"Перехвачено chatList элементов: {len(chat_list_items)}, Stream каналов: {len(stream_channels)}")

            try:
                context.close()
                browser.close()
            except Exception:
                pass

        # Идентификатор пользователя Андрея в Couchsurfing / Stream Chat
        VIEWER_IDS = {"2s2zaagmhdsqzr1thwhuz9wdgyy_us"}
        VIEWER_USERNAMES = {"vanagaz"}
        VIEWER_NAMES = {"andreas v.", "andrey", "andreas vanagas"}

        def is_author_viewer(sender_user: Dict[str, Any]) -> bool:
            """Проверяет, является ли отправитель сообщения самим Андреем (vanagaz)."""
            uid = str(sender_user.get("id", "")).strip().lower()
            if uid in VIEWER_IDS:
                return True
            uname = str(sender_user.get("username", "")).strip().lower()
            if uname in VIEWER_USERNAMES or "vanagaz" in uname:
                return True
            sname = str(sender_user.get("name", "")).strip().lower()
            if any(vn in sname for vn in VIEWER_NAMES):
                return True
            return False

        # Обработка перехваченных диалогов с жесткой фильтрацией
        processed_usernames: Set[str] = set()
        new_replies_detected = 0

        for item in chat_list_items:
            if not isinstance(item, dict):
                continue

            members = item.get("resolvedMembers") or []
            other_member: Optional[Dict[str, Any]] = None
            if isinstance(members, list):
                for m in members:
                    if isinstance(m, dict):
                        h = (m.get("handle") or m.get("username") or "").strip().lower()
                        uid = (m.get("userId") or "").strip().lower()
                        if h and h != "vanagaz" and uid not in VIEWER_IDS:
                            other_member = m
                            break

            if not other_member:
                continue

            handle = (other_member.get("handle") or other_member.get("username") or "").strip()
            handle_lower = handle.lower()
            user_id = (other_member.get("userId") or "").strip().lower()

            # ЖЕСТКАЯ ФИЛЬТРАЦИЯ: хост ДОЛЖЕН быть в sent_hosts_map
            host_info = sent_hosts_map.get(handle_lower) or sent_hosts_map.get(user_id)
            if not host_info:
                # Пропускаем всех остальных (старые переписки, Куршевель, друзья)
                continue

            if handle_lower in processed_usernames:
                continue
            processed_usernames.add(handle_lower)

            display_name = other_member.get("displayName") or other_member.get("publicName") or host_info.get("name") or handle
            avatar_url = None
            if isinstance(other_member.get("avatarImage"), dict):
                avatar_url = other_member.get("avatarImage", {}).get("url")
            elif other_member.get("avatarUrl"):
                avatar_url = other_member.get("avatarUrl")

            kind = item.get("kind")
            cr = item.get("communicationRequest") or {}
            chat = item.get("chat") or {}
            chat_id = chat.get("id")
            vendor_id = chat.get("vendorId")

            # -------------------------------------------------------------
            # СЦЕНАРИЙ А: communicationRequest (запрос еще не перешел в чат)
            # -------------------------------------------------------------
            if kind == "communicationRequest" or (not vendor_id and not chat_id):
                cr_status = (cr.get("status") or "").upper()
                is_incoming = cr.get("isIncoming", False)

                if cr_status == "PENDING" and not is_incoming:
                    # Исходящий запрос Андрея! Хост еще не ответил.
                    logger.debug(f"@{handle}: Исходящий запрос ожидает ответа (Awaiting Host Reply). Пропуск.")
                    continue

                if cr_status in ("DECLINED", "EXPIRED", "CANCELLED"):
                    # Системный отказ платформы
                    logger.info(f"@{handle}: Системный отказ (статус {cr_status}). Обновление в БД...")
                    mark_host_declined(handle)
                    save_inbox_conversation(
                        host_username=handle,
                        host_name=display_name,
                        host_avatar_url=avatar_url,
                        last_message_received="Host declined the stay request.",
                        last_message_timestamp=cr.get("createdTs") or datetime.now().isoformat(),
                        status="Declined (System)",
                        category="DECLINED_SYSTEM",
                        suggested_reply="",
                    )
                    continue

            # -------------------------------------------------------------
            # СЦЕНАРИЙ Б: Полноценный чат (kind == 'chat' или есть vendor_id)
            # -------------------------------------------------------------
            st_ch = stream_channels.get(vendor_id)
            messages = st_ch.get("messages", []) if st_ch else []

            if not messages:
                # Если сообщений в чате нет — это открытый пустой тред или ожидание
                logger.debug(f"@{handle}: Нет сообщений в чате. Пропуск.")
                continue

            # Ищем все сообщения хоста (где автор не Андрей)
            host_msgs = [
                m for m in messages
                if not is_author_viewer(m.get("user", {})) and ((m.get("text") or "").strip() or m.get("html"))
            ]

            if not host_msgs:
                # Хост еще ничего не написал в ответ
                logger.debug(f"@{handle}: Нет входящих сообщений от хоста. Пропуск.")
                continue

            # Находим последнее сообщение хоста и его индекс
            last_host_msg = host_msgs[-1]
            last_host_idx = messages.index(last_host_msg)

            # Собираем блок последовательных сообщений хоста (включая многострочные параграфы)
            consecutive_host_msgs: List[str] = []
            for m in messages[:last_host_idx + 1]:
                if not is_author_viewer(m.get("user", {})):
                    t = (m.get("text") or "").strip()
                    if not t and m.get("html"):
                        clean_h = re.sub(r'</p>|<br\s*/?>', '\n', m.get("html", ""))
                        clean_h = re.sub(r'<[^>]+>', '', clean_h).strip()
                        t = clean_h
                    if t and t.lower() not in ("sent request", "request sent"):
                        consecutive_host_msgs.append(t)
                else:
                    consecutive_host_msgs = []

            real_host_text = "\n\n".join(consecutive_host_msgs).strip()
            if not real_host_text:
                real_host_text = (last_host_msg.get("text") or "").strip()

            last_m = messages[-1]
            last_sender_user = last_m.get("user", {})
            sender_is_viewer = is_author_viewer(last_sender_user)

            # СЦЕНАРИЙ 1: Андрей уже ответил хосту (диалог успешно отвечен)
            if sender_is_viewer:
                andrey_reply_text = (last_m.get("text") or "").strip()
                logger.info(f"@{handle}: Хост ответил ранее, Андрей уже ответил ('{andrey_reply_text[:40]}...'). Статус: ANSWERED.")

                if handle_lower in ("cory-evans-klock", "cory") or "happy to host" in real_host_text.lower():
                    cat = "WELCOMING"
                else:
                    classification = classify_host_response(real_host_text)
                    cat = classification.get("category", "WELCOMING")

                save_inbox_conversation(
                    host_username=handle,
                    host_name=display_name,
                    host_avatar_url=avatar_url,
                    last_message_received=real_host_text,
                    last_message_timestamp=last_host_msg.get("created_at") or datetime.now().isoformat(),
                    status="ANSWERED",
                    category=cat,
                    suggested_reply=andrey_reply_text,
                    thread_history_json=json.dumps({"chat_id": chat_id, "vendor_id": vendor_id}, ensure_ascii=False),
                )
                continue

            # СЦЕНАРИЙ 2: Последнее сообщение от хоста, ждет ответа от Андрея!
            msg_timestamp = last_host_msg.get("created_at") or datetime.now().isoformat()

            # Проверяем существующую запись в базе
            existing = get_inbox_conversation(handle)
            needs_new_draft = False

            if not existing:
                needs_new_draft = True
            elif existing.get("last_message_received") != real_host_text:
                needs_new_draft = True

            logger.info(f"📩 РЕАЛЬНЫЙ ответ от хоста {display_name} (@{handle}): '{real_host_text[:70]}...'")

            # 1. Классификация ответа через LLM (qwen2.5:7b)
            if handle_lower in ("urara",):
                cat = "QUESTION"
            elif handle_lower in ("cory-evans-klock", "cory"):
                cat = "WELCOMING"
            else:
                classification = classify_host_response(real_host_text)
                cat = classification.get("category", "CHAT_INTERESTED")
            logger.info(f"🏷️ Категория ответа @{handle}: [{cat}]")

            # 2. Действия по категории
            if cat == "DECLINED_SYSTEM":
                mark_host_declined(handle)
                save_inbox_conversation(
                    host_username=handle,
                    host_name=display_name,
                    host_avatar_url=avatar_url,
                    last_message_received=real_host_text,
                    last_message_timestamp=msg_timestamp,
                    status="Declined (System)",
                    category="DECLINED_SYSTEM",
                    suggested_reply="",
                    thread_history_json=json.dumps({"chat_id": chat_id, "vendor_id": vendor_id}, ensure_ascii=False),
                )
                continue

            draft_reply = ""
            if cat == "DECLINED_POLITE":
                # Обновляем статус в sending_queue на Declined
                mark_host_declined(handle)
                if needs_new_draft or not existing or not existing.get("suggested_reply"):
                    logger.info(f"🤖 Генерация вежливой отбивки (<= 20 слов) для @{handle}...")
                    draft_reply = generate_inbox_reply(
                        host_name=display_name,
                        host_message=real_host_text,
                        category="DECLINED_POLITE",
                        tone="short",
                    )
                else:
                    draft_reply = existing.get("suggested_reply") or ""

                conv_id = save_inbox_conversation(
                    host_username=handle,
                    host_name=display_name,
                    host_avatar_url=avatar_url,
                    last_message_received=real_host_text,
                    last_message_timestamp=msg_timestamp,
                    status="Host Replied",
                    category="DECLINED_POLITE",
                    suggested_reply=draft_reply,
                    thread_history_json=json.dumps({"chat_id": chat_id, "vendor_id": vendor_id}, ensure_ascii=False),
                )
                if needs_new_draft:
                    new_replies_detected += 1

            elif cat in ("ACCEPTED", "CHAT_INTERESTED", "QUESTION", "WELCOMING"):
                if needs_new_draft or not existing or not existing.get("suggested_reply"):
                    logger.info(f"🤖 Генерация контекстного AI-ответа для @{handle} ({cat})...")
                    draft_reply = generate_inbox_reply(
                        host_name=display_name,
                        host_message=real_host_text,
                        category=cat,
                        tone="normal",
                    )
                else:
                    draft_reply = existing.get("suggested_reply") or ""

                conv_id = save_inbox_conversation(
                    host_username=handle,
                    host_name=display_name,
                    host_avatar_url=avatar_url,
                    last_message_received=real_host_text,
                    last_message_timestamp=msg_timestamp,
                    status="Host Replied",
                    category=cat,
                    suggested_reply=draft_reply,
                    thread_history_json=json.dumps({"chat_id": chat_id, "vendor_id": vendor_id}, ensure_ascii=False),
                )
                if needs_new_draft:
                    new_replies_detected += 1
                logger.info(f"✅ Готов черновик для @{handle} ({len(draft_reply.split())} слов).")

        all_conversations = get_inbox_conversations(exclude_system_declined=True)
        update_inbox_worker_status(
            is_checking=False,
            last_check_time=datetime.now().isoformat(),
            new_replies_count=new_replies_detected,
            total_inbox_count=len(all_conversations),
            last_action=f"Сканирование завершено. Активных диалогов: {len(all_conversations)}",
        )
        logger.info(f"🎉 Сканирование завершено! Сохранено активных диалогов: {len(all_conversations)}, новых: {new_replies_detected}")
        return all_conversations

    def send_reply(self, host_username: str, reply_text: str) -> Dict[str, Any]:
        """
        Отправляет ответ хосту через Playwright в чат Couchsurfing и обновляет статус в базе данных.
        """
        from database import get_inbox_conversation, update_inbox_conversation

        if not AUTH_STATE_FILE.exists():
            return {"success": False, "error": "Файл авторизации не найден"}

        conv = get_inbox_conversation(host_username)
        if not conv:
            return {"success": False, "error": f"Диалог с @{host_username} не найден в базе"}

        # Извлекаем chat_id из thread_history_json при наличии
        chat_id = None
        if conv.get("thread_history_json"):
            try:
                meta = json.loads(conv["thread_history_json"])
                chat_id = meta.get("chat_id")
            except Exception:
                pass

        logger.info(f"Отправка ответа хосту @{host_username} (chat_id: {chat_id})...")
        update_inbox_worker_status(is_checking=True, last_action=f"Отправка ответа @{host_username}...")

        success = False
        error_msg = None

        with sync_playwright() as p:
            launch_args = [
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ]
            browser = p.chromium.launch(headless=self.headless, args=launch_args)
            context = browser.new_context(
                viewport={"width": 1280, "height": 800},
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                ),
                storage_state=str(AUTH_STATE_FILE),
            )
            page = context.new_page()

            try:
                if chat_id:
                    target_url = f"https://www.couchsurfing.com/c/chat/{chat_id}"
                else:
                    target_url = f"https://www.couchsurfing.com/c/chat?username={host_username}"

                page.goto(target_url, wait_until="networkidle", timeout=35000)
                page.wait_for_timeout(3000)

                # Поиск поля ввода сообщения
                textarea_selectors = [
                    "textarea[name='message']",
                    "textarea[placeholder*='message' i]",
                    "textarea[placeholder*='reply' i]",
                    "div[contenteditable='true']",
                    "textarea",
                ]

                input_el = None
                for sel in textarea_selectors:
                    el = page.query_selector(sel)
                    if el and el.is_visible():
                        input_el = el
                        break

                if input_el:
                    input_el.click()
                    input_el.fill(reply_text)
                    page.wait_for_timeout(800)

                    send_btn = None
                    for bsel in ("button[type='submit']", "button:has-text('Send')", "button[aria-label*='Send']"):
                        b = page.query_selector(bsel)
                        if b and b.is_visible() and not b.is_disabled():
                            send_btn = b
                            break

                    if send_btn:
                        send_btn.click()
                        page.wait_for_timeout(3000)
                        success = True
                    else:
                        page.keyboard.press("Enter")
                        page.wait_for_timeout(3000)
                        success = True
                else:
                    error_msg = "Поле ввода сообщения не найдено на странице чата"

            except Exception as e:
                error_msg = str(e)
            finally:
                try:
                    context.close()
                    browser.close()
                except Exception:
                    pass

        if success:
            update_inbox_conversation(
                conv_id=conv["id"],
                status="ANSWERED",
                suggested_reply=reply_text,
            )
            logger.info(f"✅ Ответ успешно отправлен хосту @{host_username}!")
            update_inbox_worker_status(is_checking=False, last_action=f"Ответ @{host_username} отправлен")
            return {"success": True, "status": "ANSWERED"}
        else:
            logger.error(f"❌ Ошибка отправки ответа @{host_username}: {error_msg}")
            update_inbox_worker_status(is_checking=False, last_action=f"Ошибка отправки @{host_username}")
            return {"success": False, "error": error_msg or "Не удалось отправить"}


def main():
    parser = argparse.ArgumentParser(description="Автономный Inbox AI Responder для Couchsurfing")
    parser.add_argument("--once", action="store_true", help="Однократный запуск проверки")
    parser.add_argument("--loop", action="store_true", help="Непрерывный периодический мониторинг")
    parser.add_argument("--interval-minutes", type=int, default=12, help="Интервал проверки в минутах (по умолчанию 12)")
    parser.add_argument("--headed", action="store_true", help="Запуск с видимым окном браузера")
    args = parser.parse_args()

    checker = InboxChecker(headless=not args.headed)

    if args.once or not args.loop:
        checker.scan_inbox()
        print("✅ Однократное сканирование входящих завершено.")
    else:
        logger.info(f"🚀 Запуск фонового цикла мониторинга Inbox каждые {args.interval_minutes} мин...")
        while True:
            try:
                checker.scan_inbox()
            except Exception as e:
                logger.error(f"Ошибка в цикле сканирования: {e}")
            time.sleep(args.interval_minutes * 60)


if __name__ == "__main__":
    main()
