#!/usr/bin/env python3
"""
inbox_service.py - Независимый микросервис API для управления входящими диалогами Inbox AI Responder.
Запускается автономно на порту 8001 с включенным CORS для параллельной работы без перезапуска основного app.py.
"""

import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from starlette.requests import Request

BASE_DIR = Path(__file__).parent
OUTPUT_DIR = BASE_DIR / "output"
TEMPLATES_DIR = BASE_DIR / "templates"
INBOX_STATUS_FILE = OUTPUT_DIR / "inbox_status.json"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("InboxService")

app = FastAPI(title="Couchsurfing Inbox AI Responder API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


class RegenerateReplyPayload(BaseModel):
    conversation_id: Optional[int] = None
    conv_id: Optional[int] = None
    tone: str = "casual"  # casual / short / detailed / thanks
    custom_instruction: Optional[str] = None


class SendReplyPayload(BaseModel):
    conv_id: int
    reply_text: str


class UpdateConversationPayload(BaseModel):
    conv_id: int
    status: Optional[str] = None
    suggested_reply: Optional[str] = None
    tone_used: Optional[str] = None


@app.get("/", response_class=HTMLResponse)
async def serve_index(request: Request):
    """Синхронное зеркало веб-интерфейса платформы на порту 8001."""
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "version": "2.4.0",
            "curated_count": 0,
            "timestamp": datetime.now().isoformat(),
        },
    )


@app.get("/api/inbox/conversations")
async def get_conversations(status: Optional[str] = None):
    """Возвращает список всех входящих диалогов и агрегированные счетчики."""
    from database import get_inbox_conversations
    try:
        convs = get_inbox_conversations(status=status, exclude_system_declined=True)
        new_count = sum(1 for c in convs if c.get("status") in ("NEW_REPLY", "DRAFT_GENERATED", "Host Replied"))
        answered_count = sum(1 for c in convs if c.get("status") == "ANSWERED")
        return {
            "conversations": convs,
            "total_count": len(convs),
            "new_count": new_count,
            "answered_count": answered_count,
        }
    except Exception as e:
        logger.error(f"Error fetching inbox conversations: {e}")
        return {"conversations": [], "total_count": 0, "new_count": 0, "answered_count": 0, "error": str(e)}


@app.get("/api/inbox/status")
async def get_status():
    """Возвращает статус фонового воркера сканирования входящих."""
    from database import get_inbox_conversations
    data = {
        "is_checking": False,
        "last_check_time": None,
        "new_replies_count": 0,
        "total_inbox_count": 0,
        "last_action": "Готов к работе",
    }
    if INBOX_STATUS_FILE.exists():
        try:
            with open(INBOX_STATUS_FILE, "r", encoding="utf-8") as f:
                data.update(json.load(f))
        except Exception:
            pass
    try:
        all_convs = get_inbox_conversations(exclude_system_declined=True)
        data["total_inbox_count"] = len(all_convs)
        data["new_replies_count"] = sum(1 for c in all_convs if c.get("status") in ("NEW_REPLY", "DRAFT_GENERATED", "Host Replied"))
    except Exception:
        pass
    return data


@app.post("/api/inbox/generate-reply")
@app.post("/api/inbox/reply/regenerate")
async def regenerate_reply(payload: RegenerateReplyPayload):
    """Генерирует или перегенерирует AI-черновик ответа с заданной тональностью."""
    from database import get_inbox_conversation, update_inbox_conversation
    from llm_agent import generate_inbox_reply, classify_host_response

    target_id = payload.conversation_id or payload.conv_id
    if not target_id:
        raise HTTPException(status_code=400, detail="Missing conversation_id or conv_id")

    conv = get_inbox_conversation(target_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")

    host_name = conv.get("host_name") or conv.get("host_username") or "there"
    incoming_msg = conv.get("last_message_received") or ""
    
    # Автоматически обновляем категорию по новому правилу
    clf = classify_host_response(incoming_msg)
    cat = clf.get("category") or conv.get("category")

    try:
        new_draft = generate_inbox_reply(
            host_name=host_name,
            host_message=incoming_msg,
            category=cat,
            tone=payload.tone,
        )
        new_status = conv.get("status")
        if new_status != "ANSWERED":
            new_status = "Host Replied"

        update_inbox_conversation(
            conv_id=target_id,
            suggested_reply=new_draft,
            category=cat,
            tone_used=payload.tone,
            status=new_status,
        )
        return {
            "success": True,
            "conversation_id": target_id,
            "conv_id": target_id,
            "suggested_reply": new_draft,
            "tone": payload.tone,
        }
    except Exception as e:
        logger.error(f"Error regenerating inbox reply: {e}")
        return {"success": False, "error": str(e)}


@app.post("/api/inbox/reply/send")
async def send_reply(payload: SendReplyPayload):
    """Отправляет ответ хосту через Playwright или фиксирует отправку."""
    from database import get_inbox_conversation
    from inbox_checker import InboxChecker

    conv = get_inbox_conversation(payload.conv_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")

    host_username = conv.get("host_username")
    checker = InboxChecker(headless=True)
    res = checker.send_reply(host_username=host_username, reply_text=payload.reply_text)
    return res


@app.post("/api/inbox/update")
async def update_conversation(payload: UpdateConversationPayload):
    """Обновляет драфт или статус диалога."""
    from database import update_inbox_conversation
    ok = update_inbox_conversation(
        conv_id=payload.conv_id,
        status=payload.status,
        suggested_reply=payload.suggested_reply,
        tone_used=payload.tone_used,
    )
    return {"success": ok}


@app.post("/api/inbox/check")
async def trigger_check():
    """Запускает внеочередное сканирование входящих в фоне."""
    import threading
    from inbox_checker import InboxChecker

    def run_check():
        checker = InboxChecker(headless=True)
        checker.scan_inbox()

    t = threading.Thread(target=run_check, daemon=True)
    t.start()
    return {"status": "started", "message": "Фоновое сканирование входящих сообщений запущено"}


if __name__ == "__main__":
    port = 8001
    print(f"[*] Запуск Inbox AI Responder Service на http://0.0.0.0:{port}")
    uvicorn.run("inbox_service:app", host="0.0.0.0", port=port, reload=False)
