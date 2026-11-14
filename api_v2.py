"""
api_v2.py - Чистый REST API для новой DDD-архитектуры платформы.
Подключается к FastAPI приложению с префиксом /api/v2.
Все операции строго привязаны к активному Trip (Корень агрегата).
"""

from typing import Any, Dict, List, Optional
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from core.database import get_connection
from core.selector import SelectorEngine
from core.queue import QueueEngine
from core.autopilot import AutopilotEngine
from core.inbox import InboxEngine

router = APIRouter(prefix="/api/v2", tags=["V2 DDD Architecture"])

def get_active_trip_id() -> str:
    """Возвращает идентификатор активной поездки. При отсутствии выбрасывает NO_ACTIVE_TRIP."""
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id FROM core_trips WHERE is_active = 1 LIMIT 1;")
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=400, detail="NO_ACTIVE_TRIP")
        return row[0]


# =========================================================================
# TRIP (КОРЕНЬ АГРЕГАТА)
# =========================================================================
@router.get("/trip/active")
async def get_active_trip():
    """Возвращает текущую зафиксированную поездку."""
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM core_trips WHERE is_active = 1 LIMIT 1;")
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="NO_ACTIVE_TRIP")
        return dict(row)


# =========================================================================
# SELECTOR (СЕЛЕКТОР: Like / Pass / VIP)
# =========================================================================
class DecisionPayload(BaseModel):
    host_id: str
    decision: str  # like | pass | vip


class AutoSelectPayload(BaseModel):
    min_stars: int = 4


@router.get("/selector/candidates")
async def get_selector_candidates(
    min_stars: int = Query(1, ge=1, le=5),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """Возвращает ранжированных кандидатов со скорингом 1-5 звезд для экспресс-отбора."""
    trip_id = get_active_trip_id()
    engine = SelectorEngine(trip_id=trip_id)
    candidates = engine.list_candidates(min_stars=min_stars, limit=limit, offset=offset)
    return {"trip_id": trip_id, "count": len(candidates), "candidates": candidates}


@router.post("/selector/decide")
async def post_selector_decision(payload: DecisionPayload):
    """Устанавливает решение по хосту: like (в очередь), pass (отклонить), vip."""
    trip_id = get_active_trip_id()
    engine = SelectorEngine(trip_id=trip_id)
    try:
        ok = engine.decide(payload.host_id, payload.decision)
        if not ok:
            raise HTTPException(status_code=404, detail="Host not found or already triaged")
        return {"status": "ok", "host_id": payload.host_id, "decision": payload.decision}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/selector/auto-select")
async def post_auto_select_high_match(payload: AutoSelectPayload):
    """Автоматически переводит всех хостов с рейтингом >= min_stars в статус Like."""
    trip_id = get_active_trip_id()
    engine = SelectorEngine(trip_id=trip_id)
    selected_count = engine.auto_select_high_match(min_stars=payload.min_stars)
    return {"status": "ok", "auto_selected_count": selected_count, "min_stars": payload.min_stars}


# =========================================================================
# QUEUE (ОЧЕРЕДЬ + ГЕНЕРАЦИЯ ПИСЕМ)
# =========================================================================
class GenerateDraftPayload(BaseModel):
    host_id: str


class ApproveDraftPayload(BaseModel):
    host_id: str
    edited_text: Optional[str] = None


class BatchApprovePayload(BaseModel):
    host_ids: List[str]


class RevokeApprovalPayload(BaseModel):
    host_id: str


@router.get("/queue")
async def get_queue_items(state: Optional[str] = Query(None)):
    """Возвращает список хостов в очереди (selected, drafted, approved)."""
    trip_id = get_active_trip_id()
    engine = QueueEngine(trip_id=trip_id)
    items = engine.list_queue(state=state)
    return {"trip_id": trip_id, "count": len(items), "queue": items}


@router.post("/queue/generate")
async def post_generate_draft(payload: GenerateDraftPayload):
    """Генерирует строгое персонализированное письмо (60-90 слов) без галлюцинаций."""
    trip_id = get_active_trip_id()
    engine = QueueEngine(trip_id=trip_id)
    res = engine.generate_draft(payload.host_id)
    if not res:
        raise HTTPException(status_code=404, detail="Host or active trip not found")
    return {"status": "ok", "draft": res}


@router.post("/queue/approve")
async def post_approve_draft(payload: ApproveDraftPayload):
    """Утверждает письмо для отправки автопилотом."""
    trip_id = get_active_trip_id()
    engine = QueueEngine(trip_id=trip_id)
    try:
        ok = engine.approve(payload.host_id, edited_text=payload.edited_text, created_by="user:api_v2_queue_approve")
        if not ok:
            raise HTTPException(status_code=404, detail="Host not found")
        host = engine.repository.get_host_context(trip_id, payload.host_id)
        from core.approval_service import ApprovalService
        approval = ApprovalService().get_approval_status(
            trip_id=trip_id, host_id=host["host_id"], username=host["username"],
            message_text=host.get("message_draft") or "", message_version=host.get("message_version"),
        )
        return {"status": "ok", "host_id": payload.host_id, "state": "approved", "approval": approval.as_dict()}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/queue/batch-approve")
async def post_batch_approve(payload: BatchApprovePayload):
    """Пакетное утверждение писем."""
    trip_id = get_active_trip_id()
    engine = QueueEngine(trip_id=trip_id)
    approved_count = engine.batch_approve(payload.host_ids, created_by="user:api_v2_batch_approve")
    return {"status": "ok", "approved_count": approved_count}


@router.post("/queue/revoke")
async def post_revoke_approval(payload: RevokeApprovalPayload):
    """Revoke the explicit user approval for one host in the active trip."""
    from core.approval_service import ApprovalService
    trip_id = get_active_trip_id()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT host_id FROM core_hosts WHERE trip_id=? AND (host_id=? OR LOWER(username)=LOWER(?))",
            (trip_id, payload.host_id, payload.host_id),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Host not found in active trip")
    result = ApprovalService().revoke_approval(
        trip_id=trip_id, host_id=row["host_id"], reason="revoked by user through API",
    )
    if result.code == "APPROVAL_NOT_FOUND":
        raise HTTPException(status_code=404, detail=result.detail)
    return {"status": result.code, "approval": result.as_dict()}


class SkipQueuePayload(BaseModel):
    host_id: str


@router.post("/queue/skip")
async def post_skip_queue_item(payload: SkipQueuePayload):
    """По ТЗ: Снимает хоста из очереди (state='skipped') БЕЗ удаления из БД."""
    trip_id = get_active_trip_id()
    engine = QueueEngine(trip_id=trip_id)
    ok = engine.skip(payload.host_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Host not found or already sent")
    return {"status": "ok", "host_id": payload.host_id, "state": "skipped"}


@router.post("/queue/clear")
async def post_clear_queue():
    """По ТЗ: Переводит всех неотправленных хостов очереди в 'skipped' БЕЗ удаления из БД."""
    trip_id = get_active_trip_id()
    engine = QueueEngine(trip_id=trip_id)
    skipped_count = engine.clear_queue()
    return {"status": "ok", "skipped_count": skipped_count}


# =========================================================================
# AUTOPILOT (АВТОПИЛОТ ОТПРАВКИ)
# =========================================================================
@router.get("/autopilot/ready-candidates")
async def get_autopilot_ready_candidates(limit: int = Query(50)):
    """Возвращает список утвержденных хостов, готовых к отправке."""
    trip_id = get_active_trip_id()
    engine = AutopilotEngine(trip_id=trip_id)
    cands = engine.get_ready_candidates(limit=limit)
    sent_today = engine.get_sent_today_count()
    return {"trip_id": trip_id, "sent_today": sent_today, "ready_count": len(cands), "candidates": cands}


# =========================================================================
# INBOX (ИНБОКС)
# =========================================================================
class SuggestReplyPayload(BaseModel):
    host_name: str
    incoming_text: str


@router.get("/inbox/conversations")
async def get_inbox_conversations():
    """Возвращает все активные переписки."""
    trip_id = get_active_trip_id()
    engine = InboxEngine(trip_id=trip_id)
    return {"trip_id": trip_id, "conversations": engine.list_conversations()}


@router.post("/inbox/suggest-reply")
async def post_suggest_reply(payload: SuggestReplyPayload):
    """Генерирует контекстный автоответ."""
    trip_id = get_active_trip_id()
    engine = InboxEngine(trip_id=trip_id)
    suggestion = engine.generate_reply_suggestion(payload.host_name, payload.incoming_text)
    return {"suggestion": suggestion}


# =========================================================================
# LOGS (СКВОЗНОЙ АУДИТ)
# =========================================================================
@router.get("/logs")
async def get_audit_logs(limit: int = Query(100)):
    """Возвращает историю сквозного аудита системы."""
    trip_id = get_active_trip_id()
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, timestamp, module, event, host_id, details_json
            FROM core_audit_logs
            WHERE trip_id = ?
            ORDER BY id DESC
            LIMIT ?;
            """,
            (trip_id, limit)
        )
        rows = [dict(r) for r in cur.fetchall()]
        return {"trip_id": trip_id, "count": len(rows), "logs": rows}
