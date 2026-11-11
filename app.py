"""
app.py - Центральный веб-сервер и API платформы Couchsurfing Personalized Outreach Platform.

Предоставляет:
- Веб-интерфейс http://localhost:8000
- Поддержку PWA (manifest.json, Service Worker, адаптация под iPhone 15 Pro Max)
- Управление личным контекстом (Personal Context Knowledge Base)
- Интеллектуальный анализ профилей и генерацию сообщений
- Атомарное управление фоновыми процессами через ProcessManager
- Аварийную остановку (Emergency Stop) и проверку статусов
"""

import asyncio
import json
import logging
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("CouchsurfingApp")

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from database import (
    db_session,
    get_setting,
    init_db,
    set_setting,
)
from personal_context import (
    DEFAULT_GUEST_CONTEXT,
    get_personal_context_manager,
)
from process_manager import get_process_manager
from profile_analyzer import get_profile_analyzer
from request_manager import (
    DEFAULT_CHECK_IN,
    DEFAULT_CHECK_OUT,
    DEFAULT_CONTEXT,
    QUEUE_FILE,
    RequestQueueManager,
)
from data_cleaner import (
    sanitize_host_name,
    sanitize_host_clean_name,
    sanitize_host_location,
)

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BASE_DIR = Path(__file__).parent
TEMPLATES_DIR = BASE_DIR / "templates"
OUTPUT_DIR = Path(os.environ.get("CS_OUTPUT_DIR", str(BASE_DIR / "output"))).resolve()
USER_DATA_DIR = BASE_DIR / "user_data"
STATIC_DIR = BASE_DIR / "static"

# Инициализация сервисов платформы
init_db()
app = FastAPI(title="Couchsurfing Personalized Outreach Platform")
from api_v2 import router as v2_router
app.include_router(v2_router)
queue_manager = RequestQueueManager()
process_mgr = get_process_manager()
context_mgr = get_personal_context_manager()
profile_analyzer = get_profile_analyzer()

# Singleton-ссылка на asyncio-таск запуска диспетчера.
# Перед новым запуском незавершённый старый таск отменяется (два цикла ломают таймер).
dispatcher_task: Optional[asyncio.Task] = None

# Статические файлы для PWA и иконок
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def _date_or_default(value, default: str) -> str:
    """Возвращает ISO-дату из записи хоста/трипа либо дефолт."""
    if value:
        s = str(value).strip()
        if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
            return s
    return default

# Состояние фоновой перегенерации через LLM
regen_state = {
    "is_running": False,
    "current": 0,
    "total": 0,
    "status": "idle",
}


# =========================================================================
# Pydantic Модели запросов
# =========================================================================

class ScraperParams(BaseModel):
    city: str = "Tokyo"
    gender: str = "Any"
    status: str = "Accepting Guests"
    max_days: int = 7
    min_rate: float = 75.0
    pages: int = 10
    headless: bool = False
    no_couples: bool = False
    arrival_date: Optional[str] = "2026-11-16"
    departure_date: Optional[str] = "2026-11-19"


class BatchUpdateDatesPayload(BaseModel):
    request_ids: Optional[List[str]] = None
    old_pattern: Optional[str] = "dates"
    new_arrival: str = "16 Nov"
    new_departure: str = "19 Nov"
    city: Optional[str] = "Tokyo"


class FilterParams(BaseModel):
    gender: str = "any"
    allow_couples: bool = True
    enrich_home: bool = False
    hide_sent: bool = True
    city: Optional[str] = None


class CookieInput(BaseModel):
    cookie: str


class SaveRequestItem(BaseModel):
    username: str
    message: Optional[str] = None
    status: Optional[str] = None
    trip_id: Optional[str] = None
    city: Optional[str] = None


class RegenerateItem(BaseModel):
    host_id: Optional[str] = None
    username: Optional[str] = None
    check_in: Optional[str] = DEFAULT_CHECK_IN
    check_out: Optional[str] = DEFAULT_CHECK_OUT
    guest_context: Optional[str] = DEFAULT_CONTEXT
    trip_id: Optional[str] = None
    city: Optional[str] = None


class RegenerateAll(BaseModel):
    check_in: str = DEFAULT_CHECK_IN
    check_out: str = DEFAULT_CHECK_OUT
    guest_context: str = DEFAULT_CONTEXT
    overwrite_approved: bool = False


class RegenerateBatchPayload(BaseModel):
    request_ids: Optional[List[Any]] = None
    ids: Optional[List[Any]] = None
    usernames: Optional[List[Any]] = None
    check_in: Optional[str] = DEFAULT_CHECK_IN
    check_out: Optional[str] = DEFAULT_CHECK_OUT
    guest_context: Optional[str] = DEFAULT_CONTEXT


class AutopilotParams(BaseModel):
    daily_limit: int = 100
    delay_min: int = 15
    delay_max: int = 35
    period_minutes: Optional[int] = None
    interval_seconds: Optional[int] = None
    min_interval_minutes: int = 5
    max_interval_minutes: int = 10
    jitter_enabled: bool = True
    max_requests: Optional[int] = None
    headed: bool = False
    dry_run: bool = False
    test_mode: bool = False
    target: Optional[str] = None
    trip_id: Optional[str] = None


class ContextFactItem(BaseModel):
    fact_id: Optional[str] = None
    category: str = "Personal Background"
    title: str
    content: str
    priority: int = 1
    status: str = "confirmed"
    source: str = "user"
    confidence: float = 1.0
    is_active: bool = True
    valid_until: Optional[str] = None


class ToggleFactPayload(BaseModel):
    is_active: bool


class ContextAnchorItem(BaseModel):
    anchor_id: Optional[str] = None
    name: str
    category: str = "General"
    keywords: Union[List[str], str] = []
    description: Optional[str] = ""
    priority: int = 1
    is_active: bool = True


class ToggleAnchorPayload(BaseModel):
    is_active: bool


class ContextPreviewPayload(BaseModel):
    host_id: str
    trip_id: Optional[str] = None
    city: Optional[str] = None
    arrival_date: Optional[str] = None
    departure_date: Optional[str] = None


class TravelContextPayload(BaseModel):
    city: str
    arrival_date: Optional[str] = None
    departure_date: Optional[str] = None
    dates_text: Optional[str] = None
    purpose: Optional[str] = None
    trip_id: Optional[str] = None


class ReanalyzePayload(BaseModel):
    username: str


# =========================================================================
# PWA & Web App Маршруты
# =========================================================================

@app.get("/", response_class=HTMLResponse)
async def serve_index():
    index_file = TEMPLATES_DIR / "index.html"
    if not index_file.exists():
        raise HTTPException(status_code=404, detail="Template index.html not found")
    content = index_file.read_text(encoding="utf-8")
    return HTMLResponse(
        content=content,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0, post-check=0, pre-check=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@app.get("/manifest.json")
async def get_manifest():
    mf = STATIC_DIR / "manifest.json"
    if mf.exists():
        return FileResponse(
            mf,
            media_type="application/manifest+json",
            headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
        )
    raise HTTPException(status_code=404, detail="Manifest not found")


@app.get("/sw.js")
async def get_service_worker():
    sw = STATIC_DIR / "sw.js"
    if sw.exists():
        return FileResponse(
            sw,
            media_type="application/javascript",
            headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
        )
    raise HTTPException(status_code=404, detail="Service worker not found")


# =========================================================================
# Статус системы, Телеметрия и Emergency Stop
# =========================================================================

@app.get("/api/status")
async def get_status(city: Optional[str] = None):
    """Текущий статус платформы, последние логи и телеметрия по текущему городу/поездке."""
    # Получаем текущий выбранный город из параметра запроса или настроек
    target_city = (city or get_setting("default_city", "Kyoto") or "Kyoto").strip()
    slug = re.sub(r"\W+", "_", target_city.lower())

    total_hosts = 0
    curated_hosts = 0
    rejected_hosts = 0

    # Чтение файлов хостов для выбранного города (без искусственных минимумов)
    json_path = OUTPUT_DIR / f"hosts_{slug}.json"
    curated_path = OUTPUT_DIR / f"hosts_{slug}_curated.json"

    if json_path.exists():
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                total_hosts = len(json.load(f))
        except Exception:
            pass

    if curated_path.exists():
        try:
            with open(curated_path, "r", encoding="utf-8") as f:
                curated_hosts = len(json.load(f))
        except Exception:
            pass

    # Реальные данные из SQLite по текущему городу
    try:
        with db_session() as conn:
            cur = conn.cursor()
            cur.execute("SELECT count(*) FROM hosts WHERE LOWER(location) LIKE ?;", (f"%{target_city.lower()}%",))
            db_city_hosts = cur.fetchone()[0]
            if db_city_hosts > 0:
                total_hosts = max(total_hosts, db_city_hosts)
                if curated_hosts == 0:
                    curated_hosts = db_city_hosts
    except Exception:
        pass

    rejected_path = OUTPUT_DIR / "rejected_hosts.json"
    if rejected_path.exists():
        try:
            with open(rejected_path, "r", encoding="utf-8") as f:
                rejected_hosts = len(json.load(f))
        except Exception:
            pass

    scraped_total = max(curated_hosts + rejected_hosts, total_hosts)

    # Быстрый подсчет из SQLite (реальные данные очереди)
    approved_count = 0
    draft_count = 0
    queue_total = 0
    try:
        with db_session() as conn:
            cur = conn.cursor()
            cur.execute("SELECT state, count(*) FROM core_hosts GROUP BY state;")
            for row in cur.fetchall():
                st, cnt = row[0], row[1]
                if st in ("selected", "drafted", "approved"):
                    queue_total += cnt
                if st == "approved":
                    approved_count += cnt
                elif st == "drafted":
                    draft_count += cnt
    except Exception:
        pass

    emergency_stop = get_setting("emergency_stop", "0") == "1"
    is_proc_running = process_mgr.is_running
    current_task = process_mgr.active_task_name if is_proc_running else "None"
    logs = process_mgr.get_logs(80)
    if not logs:
        sent_log_file = OUTPUT_DIR / "sent_log.json"
        if sent_log_file.exists():
            try:
                with open(sent_log_file, "r", encoding="utf-8") as f:
                    s_logs = json.load(f)
                fallback_lines = []
                for entry in s_logs[-30:]:
                    ts = entry.get("timestamp") or entry.get("time") or ""
                    st = entry.get("status") or "INFO"
                    u = entry.get("username") or entry.get("id") or ""
                    msg = entry.get("notes") or entry.get("error") or ""
                    fallback_lines.append(f"[{ts}] [{st}] @{u}: {msg}")
                logs = fallback_lines
            except Exception:
                pass

    return {
        "is_running": is_proc_running,
        "current_task": current_task,
        "city": target_city,
        "logs": logs,
        "total_hosts": total_hosts,
        "curated_hosts": curated_hosts,
        "rejected_hosts": rejected_hosts,
        "scraped_hosts": scraped_total,
        "total_scraped": scraped_total,
        "queue_total": queue_total,
        "approved_hosts": approved_count,
        "draft_hosts": draft_count,
        "emergency_stop": emergency_stop,
    }


# =========================================================================
# Live System Health (Ollama & Tailscale)
# =========================================================================

@app.get("/api/system/health")
async def get_system_health():
    """Live health check для Ollama и Tailscale с учетом OLLAMA_HOST."""
    ollama_host = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
    ollama_online = False
    llm_online = False
    llava_online = False
    model_name = "qwen2.5:7b"
    
    try:
        import urllib.request
        req = urllib.request.Request(f"{ollama_host}/api/tags")
        loop = asyncio.get_running_loop()
        def _fetch_ollama():
            with urllib.request.urlopen(req, timeout=1.5) as resp:
                if resp.status == 200:
                    return json.loads(resp.read().decode("utf-8"))
            return None
        ollama_data = await loop.run_in_executor(None, _fetch_ollama)
        if ollama_data:
            models = [str(m.get("name", "")) for m in ollama_data.get("models", [])]
            ollama_online = True
            if any("qwen" in m.lower() for m in models):
                model_name = "qwen2.5:7b"
                llm_online = True
            elif models:
                model_name = models[0]
                llm_online = True
            
            # Легкая и быстрая проверка наличия LLaVA модели в списке без тяжелой инференции
            if any("llava" in m.lower() for m in models):
                llava_online = True
    except Exception:
        ollama_online = False
        llm_online = False
        llava_online = False
    
    ts_active = False
    ts_ip = None
    try:
        proc = await asyncio.create_subprocess_exec(
            "tailscale", "status", "--json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=1.5)
        if proc.returncode == 0:
            ts_data = json.loads(stdout.decode("utf-8"))
            ts_active = (ts_data.get("BackendState") == "Running")
            ips = ts_data.get("TailscaleIPs") or []
            ts_ip = ips[0] if ips else None
    except Exception:
        try:
            p2 = await asyncio.create_subprocess_shell(
                "ifconfig | grep '100.'",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            out2, _ = await asyncio.wait_for(p2.communicate(), timeout=1.0)
            if out2 and b"100." in out2:
                ts_active = True
        except Exception:
            ts_active = False

    return {
        "ollama": {
            "status": "online" if ollama_online else "offline",
            "model": model_name if ollama_online else "Offline",
            "is_active": ollama_online
        },
        "llm": {
            "status": "online" if llm_online else "offline",
            "is_active": llm_online
        },
        "llava": {
            "status": "online" if llava_online else "offline",
            "is_active": llava_online
        },
        "tailscale": {
            "status": "active" if ts_active else "off",
            "ip": ts_ip,
            "is_active": ts_active
        }
    }


# =========================================================================
# Emergency Stop & Resume Operations
# =========================================================================

@app.post("/api/dispatcher/emergency-stop")
@app.post("/api/emergency_stop")
async def dispatcher_emergency_stop():
    """Аварийная остановка диспетчера (пауза, статус STOPPED, сохранение очереди)."""
    set_setting("emergency_stop", "1")

    status_file = OUTPUT_DIR / "autopilot_status.json"
    d = {}
    if status_file.exists():
        try:
            with open(status_file, "r", encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            pass

    d["is_paused"] = True
    d["emergency_stop"] = True
    d["last_status"] = "STOPPED"
    d["state_label"] = "STOPPED"
    if d.get("seconds_remaining") is None or d.get("seconds_remaining") <= 0:
        d["seconds_remaining"] = 180
    d["next_send_time"] = None

    try:
        with open(status_file, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"Error saving autopilot_status on emergency stop: {e}")

    process_mgr.append_log("[EMERGENCY] Dispatcher stopped by user.")
    return {"status": "emergency_stop_triggered", "stopped": True, "is_paused": True, "emergency_stop": True}


@app.post("/api/dispatcher/resume")
@app.post("/api/emergency_resume")
async def dispatcher_resume():
    """Возобновление работы диспетчера (снятие паузы, обратный отсчет)."""
    set_setting("emergency_stop", "0")

    status_file = OUTPUT_DIR / "autopilot_status.json"
    d = {}
    if status_file.exists():
        try:
            with open(status_file, "r", encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            pass

    d["is_paused"] = False
    d["emergency_stop"] = False
    d["last_status"] = "Работает"
    d["state_label"] = "РАБОТАЕТ"

    import time
    rem_sec = d.get("seconds_remaining") or d.get("next_delay_seconds") or 180
    if rem_sec <= 0:
        rem_sec = 180
    d["seconds_remaining"] = rem_sec
    d["next_send_time"] = time.time() + rem_sec

    try:
        with open(status_file, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"Error saving autopilot_status on resume: {e}")

    # Если процесс автопилота не запущен в ProcessManager, запускаем его
    if not (process_mgr.active_task_name == "Автопилот" and process_mgr.is_running):
        cmd = [
            sys.executable,
            "-u",
            str(BASE_DIR / "auto_sender.py"),
            "--daily-limit", str(d.get("daily_limit", 100)),
            "--min-interval-minutes", str(d.get("min_interval_minutes", 5)),
            "--max-interval-minutes", str(d.get("max_interval_minutes", 10)),
        ]
        if d.get("jitter_enabled", True):
            cmd.append("--jitter-enabled")
        else:
            cmd.append("--no-jitter")
        if d.get("headed", False):
            cmd.append("--headed")
        if d.get("dry_run", False):
            cmd.append("--dry-run")
        res = await _start_dispatcher_job(cmd, d)
        if not res.get("success"):
            process_mgr.append_log(f"[RESUME] Не удалось запустить диспетчер: {res.get('error')}")

    process_mgr.append_log("[RESUME] Dispatcher resumed by user.")
    return {"status": "resumed", "is_paused": False, "emergency_stop": False}


@app.get("/api/emergency_status")
async def get_emergency_status():
    """Проверка флага Emergency Stop."""
    is_stopped = get_setting("emergency_stop", "0") == "1"
    return {"emergency_stop": is_stopped}


# =========================================================================
# Управление процессами (Scraper, Filter, Sync)
# =========================================================================

@app.post("/api/start_scraper")
async def start_scraper(params: ScraperParams):
    """Запуск scraper.py через атомарный ProcessManager."""
    if get_setting("emergency_stop", "0") == "1":
        return {"error": "Активирован Emergency Stop! Сначала снимите блокировку."}
    if process_mgr.is_running:
        return {"error": f"Процесс '{process_mgr.active_task_name}' уже выполняется!"}

    cmd = [
        sys.executable,
        "-u",
        str(BASE_DIR / "scraper.py"),
        "--city", params.city,
        "--gender", params.gender,
        "--status", params.status,
        "--max-days", str(params.max_days),
        "--min-rate", str(params.min_rate),
        "--pages", str(params.pages),
    ]
    if params.headless:
        cmd.append("--headless")
    if params.no_couples:
        cmd.append("--no-couples")
    if params.arrival_date:
        cmd.extend(["--arrival-date", params.arrival_date])
    if params.departure_date:
        cmd.extend(["--departure-date", params.departure_date])

    res = await process_mgr.start_job("Сбор анкет", cmd, params.model_dump())
    if not res.get("success"):
        return {"error": res.get("error")}
    return {"status": "started", "city": params.city, "job_id": res.get("job_id")}


@app.post("/api/scraper/stop")
async def stop_scraper():
    """Остановка активного процесса сбора анкет (scraper.py)."""
    if not process_mgr.is_running or not process_mgr.active_proc:
        return {"status": "not_running", "message": "Процесс сбора анкет не запущен."}
    task_name = process_mgr.active_task_name
    res = await process_mgr.stop_current_job()
    process_mgr.append_log(f"[STOP] Scraper '{task_name}' остановлен пользователем.")
    return {"status": "stopped", "task": task_name, "detail": res}


@app.get("/api/trips/list")
async def api_trips_list():
    """Возвращает список актуальных трипов пользователя из канонической таблицы core_trips."""
    from core.database import list_trips
    try:
        trips = list_trips()
        trips_list = [
            {
                "trip_id": t.id,
                "id": t.id,
                "city": t.city,
                "country": t.country,
                "arrival_date": t.arrival_date,
                "departure_date": t.departure_date,
                "dates_text": t.dates_text or f"{t.arrival_date} – {t.departure_date}",
                "url": t.url,
                "is_active": t.is_active,
            }
            for t in trips
        ]
        return {"status": "ok", "trips": trips_list, "count": len(trips_list)}
    except Exception as e:
        logger.error(f"Ошибка api_trips_list: {e}")
        return {"error": str(e)}


class SelectTripPayload(BaseModel):
    trip_id: str
    city: Optional[str] = None
    arrival: Optional[str] = None
    departure: Optional[str] = None
    datesText: Optional[str] = None
    url: Optional[str] = None


@app.get("/api/trips/active")
async def api_get_active_trip():
    """Возвращает текущую активную поездку (Единый корень системы) из core_trips."""
    from core.database import get_active_trip
    t = get_active_trip()
    if t:
        ctx = {
            "trip_id": t.id,
            "id": t.id,
            "city": t.city,
            "country": t.country,
            "arrival": t.arrival_date,
            "departure": t.departure_date,
            "datesText": t.dates_text or f"{t.arrival_date} – {t.departure_date}",
            "url": t.url,
            "is_active": t.is_active,
        }
        return {"status": "ok", "trip": ctx}
    return {"status": "ok", "trip": None}


@app.post("/api/trips/select")
async def api_select_trip(payload: SelectTripPayload):
    """Синхронизирует выбор активной поездки в каноническую таблицу core_trips."""
    from core.database import set_active_trip
    canon = resolve_trip_alias(payload.trip_id) or payload.trip_id
    updated_trip = set_active_trip(canon)
    if not updated_trip:
        raise HTTPException(status_code=404, detail=f"Trip {canon} not found")
    
    ctx = {
        "trip_id": updated_trip.id,
        "id": updated_trip.id,
        "city": updated_trip.city,
        "country": updated_trip.country,
        "arrival": updated_trip.arrival_date,
        "departure": updated_trip.departure_date,
        "datesText": updated_trip.dates_text,
        "url": updated_trip.url,
        "is_active": updated_trip.is_active,
    }
    logger.info(f"Активная поездка зафиксирована в core_trips: {updated_trip.id} ({updated_trip.city}, {updated_trip.country})")
    return {"status": "ok", "active_trip": ctx}


@app.post("/api/trips/import")
async def api_trips_import():
    """Импортирует реальные предстоящие поездки с Couchsurfing через Playwright и сохраняет в SQLite user_trips."""
    from scraper import CouchsurfingScraper, AUTH_STATE_FILE
    if not AUTH_STATE_FILE.exists():
        return JSONResponse(
            status_code=401,
            content={"status": "error", "error": "auth_required", "detail": "Файл сессии user_data/auth_state.json не найден. Требуется авторизация в Couchsurfing."}
        )

    try:
        def _fetch():
            scraper = CouchsurfingScraper(headless=True)
            return scraper.list_trips()

        discovered_trips = await asyncio.to_thread(_fetch)
        if not discovered_trips:
            return {"status": "ok", "trips": [], "count": 0, "message": "Предстоящие поездки не найдены на Couchsurfing."}

        saved_trips = []
        now = datetime.now().isoformat()
        with db_session() as conn:
            cur = conn.cursor()
            for t in discovered_trips:
                t_id = t.get("trip_id") or t.get("id") or ""
                if not t_id and t.get("url"):
                    parts = [p for p in t["url"].split("/") if p]
                    t_id = parts[-1] if parts else ""
                if not t_id:
                    continue
                city = t.get("city") or ""
                arr = t.get("arrival_date") or ""
                dep = t.get("departure_date") or ""
                d_text = t.get("dates_text") or (f"{arr} – {dep}" if arr and dep else "")
                url = t.get("url") or f"https://www.couchsurfing.com/c/trips/{t_id}"

                cur.execute("""
                    INSERT INTO user_trips (trip_id, city, arrival_date, departure_date, dates_text, url, is_active, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
                    ON CONFLICT(trip_id) DO UPDATE SET
                        city=excluded.city,
                        arrival_date=excluded.arrival_date,
                        departure_date=excluded.departure_date,
                        dates_text=excluded.dates_text,
                        url=excluded.url,
                        is_active=1,
                        updated_at=excluded.updated_at;
                """, (t_id, city, arr, dep, d_text, url, now, now))

                saved_trips.append({
                    "trip_id": t_id,
                    "id": t_id,
                    "city": city,
                    "arrival_date": arr,
                    "departure_date": dep,
                    "dates_text": d_text,
                    "url": url,
                    "host_count": 0
                })
            conn.commit()

        return {"status": "ok", "trips": saved_trips, "count": len(saved_trips)}
    except Exception as e:
        logger.error(f"Ошибка api_trips_import: {e}")
        return JSONResponse(
            status_code=500,
            content={"status": "error", "error": "import_failed", "detail": str(e)}
        )


class EraseSearchPayload(BaseModel):
    city: Optional[str] = None
    trip_id: Optional[str] = None


@app.post("/api/search/erase-all")
async def api_search_erase_all(payload: Optional[EraseSearchPayload] = None):
    """Очистка результатов поиска исключительно для текущего выбранного трипа/города."""
    try:
        import json as _json
        from pathlib import Path as _Path

        cleared = []
        target_city = (payload.city if payload and payload.city else None) or get_setting("default_city", "Kyoto") or "Kyoto"
        slug = re.sub(r"\W+", "_", target_city.strip().lower())
        trip_id = payload.trip_id if payload else None

        # 1. Очистка результатов поиска для выбранной поездки:
        # Удаляем привязку к поездке из trip_hosts и неотправленные карточки из core_hosts.
        # Исходная информация о профилях в глобальной таблице hosts сохраняется (ТЗ).
        reset_count = 0
        from core.database import get_active_trip, log_audit
        active_trip = get_active_trip()
        active_trip_id = trip_id or (active_trip.id if active_trip else None)
        if active_trip_id:
            active_trip_id = resolve_trip_alias(active_trip_id)

        try:
            with db_session() as conn:
                cur = conn.cursor()
                if active_trip_id:
                    cur.execute("""
                        DELETE FROM core_hosts 
                        WHERE LOWER(trip_id) = LOWER(?) AND state NOT IN ('sent', 'inbox');
                    """, (active_trip_id,))
                    reset_count = cur.rowcount
                    log_audit(
                        trip_id=active_trip_id,
                        module="COLLECTOR",
                        event="SEARCH_SELECTION_RESET",
                        details={"reset_count": reset_count}
                    )
        except Exception as e:
            logger.error(f"Ошибка очистки результатов поиска в базе: {e}")

        # 2. Очищаем ТОЛЬКО локальные кэш-файлы JSON текущего города
        for f in [f"hosts_{slug}.json", f"hosts_{slug}_curated.json", f"hosts_{slug}_live.json"]:
            p = OUTPUT_DIR / f
            if p.exists():
                p.write_text("[]", encoding="utf-8")
                cleared.append(f)

        process_mgr.append_log(f"[RESET] Выборка для {active_trip_id or target_city} сброшена (файлы: {cleared}, сброшено статусов: {reset_count}). Данные в БД сохранены.")

        return {
            "status": "ok",
            "message": f"Выборка для {active_trip_id or target_city} сброшена. Данные профилей сохранены в базе.",
            "cleared_files": cleared,
            "reset_count": reset_count,
        }
    except Exception as e:
        return {"error": str(e)}


class TripStartParams(BaseModel):
    trip_url: str
    trip_id: Optional[str] = None
    city: Optional[str] = None
    gender: str = "Any"
    status: str = "Accepting Guests"
    max_days: int = 7
    min_rate: float = 75.0
    pages: int = 10
    no_couples: bool = False
    headless: bool = False
    arrival_date: Optional[str] = None
    departure_date: Optional[str] = None


@app.post("/api/scraper/start")
@app.post("/api/scraper/start-from-trip")
async def start_scraper_from_trip(params: TripStartParams):
    """Запуск сбора хостов по конкретной ссылке на трип."""
    if get_setting("emergency_stop", "0") == "1":
        return {"error": "Активирован Emergency Stop! Сначала снимите блокировку."}
    if process_mgr.is_running:
        return {"error": f"Процесс '{process_mgr.active_task_name}' уже выполняется!"}

    cmd = [
        sys.executable,
        "-u",
        str(BASE_DIR / "scraper.py"),
        "--trip-url", params.trip_url,
        "--gender", params.gender or "Any",
        "--status", params.status,
        "--max-days", str(params.max_days),
        "--min-rate", str(params.min_rate),
        "--pages", str(params.pages),
    ]
    if params.trip_id:
        cmd.extend(["--trip-id", params.trip_id])
    if params.headless:
        cmd.append("--headless")
    if params.no_couples:
        cmd.append("--no-couples")
    if params.arrival_date:
        cmd.extend(["--arrival-date", params.arrival_date])
    if params.departure_date:
        cmd.extend(["--departure-date", params.departure_date])

    payload = params.model_dump()
    res = await process_mgr.start_job("Сбор анкет (трип)", cmd, payload)
    if not res.get("success"):
        return {"error": res.get("error")}
    return {
        "status": "started",
        "trip_url": params.trip_url,
        "trip_id": params.trip_id,
        "city": params.city,
        "arrival_date": params.arrival_date,
        "departure_date": params.departure_date,
        "job_id": res.get("job_id"),
    }


@app.post("/api/start_filter")
async def start_filter(params: FilterParams):
    """Запуск filter_hosts.py для очистки датасета от неактивных хостов."""
    if get_setting("emergency_stop", "0") == "1":
        return {"error": "Активирован Emergency Stop! Сначала снимите блокировку."}
    if process_mgr.is_running:
        return {"error": f"Процесс '{process_mgr.active_task_name}' уже выполняется!"}

    if params.city and params.city.strip():
        slug = re.sub(r"\W+", "_", params.city.strip().lower())
    else:
        slug = "tokyo"
    input_file = OUTPUT_DIR / f"hosts_{slug}.json"
    if not input_file.exists() and not (params.city and params.city.strip()):
        input_file = OUTPUT_DIR / "hosts_tokyo.json"
    if not input_file.exists():
        return {"error": f"Сначала соберите анкеты для города {params.city or 'Tokyo'} (нет исходного JSON файла)!"}

    cmd = [
        sys.executable,
        "-u",
        str(BASE_DIR / "filter_hosts.py"),
        "--input", str(input_file),
        "--gender", params.gender,
    ]
    if not params.allow_couples:
        cmd.append("--no-couples")
    if params.enrich_home:
        cmd.append("--enrich-home")
    if params.hide_sent:
        cmd.append("--hide-sent")
    else:
        cmd.append("--no-hide-sent")

    res = await process_mgr.start_job("Глубокая фильтрация", cmd, params.model_dump())
    if not res.get("success"):
        return {"error": res.get("error")}
    return {"status": "started", "job_id": res.get("job_id")}


@app.post("/api/sync_sent")
async def start_sync_sent():
    """Синхронизация отправленных запросов с Couchsurfing (sync_sent_requests.py)."""
    if process_mgr.is_running:
        return {"error": f"Уже выполняется процесс: {process_mgr.active_task_name}"}

    cmd = [
        sys.executable,
        "-u",
        str(BASE_DIR / "sync_sent_requests.py"),
    ]
    res = await process_mgr.start_job("Синхронизация отправленных", cmd)
    if not res.get("success"):
        return {"error": res.get("error")}
    return {"status": "started", "job_id": res.get("job_id")}


@app.post("/api/stop")
async def stop_process():
    """Остановка выполняющегося процесса."""
    res = await process_mgr.stop_current_job()
    return res


@app.post("/api/start_auth_console")
async def start_auth_console():
    """Запуск auth.py в новом окне консоли."""
    try:
        flags = subprocess.CREATE_NEW_CONSOLE if sys.platform == "win32" else 0
        subprocess.Popen(
            [sys.executable, str(BASE_DIR / "auth.py")],
            cwd=str(BASE_DIR),
            creationflags=flags,
        )
        return {"status": "launched"}
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/set_cookie")
async def set_cookie(data: CookieInput):
    """Сохранение cookie напрямую."""
    raw = data.cookie.strip()
    if not raw:
        return {"error": "Cookie is empty"}

    USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    auth_state_path = USER_DATA_DIR / "auth_state.json"

    cookies_list = []
    if "=" in raw:
        pairs = raw.split(";")
        for p in pairs:
            p = p.strip()
            if "=" in p:
                k, v = p.split("=", 1)
                cookies_list.append({
                    "name": k.strip(),
                    "value": v.strip(),
                    "domain": ".couchsurfing.com",
                    "path": "/",
                    "expires": -1,
                    "httpOnly": False,
                    "secure": True,
                    "sameSite": "Lax",
                })
    else:
        cookies_list.append({
            "name": "_couchsurfing_session",
            "value": raw,
            "domain": ".couchsurfing.com",
            "path": "/",
            "expires": -1,
            "httpOnly": True,
            "secure": True,
            "sameSite": "Lax",
        })

    storage_state_obj = {
        "cookies": cookies_list,
        "origins": [{"origin": "https://www.couchsurfing.com", "localStorage": []}],
    }

    with open(auth_state_path, "w", encoding="utf-8") as f:
        json.dump(storage_state_obj, f, indent=2)

    process_mgr.append_log(f"[+] Сохранен auth_state.json с {len(cookies_list)} cookies.")
    return {"status": "ok", "count": len(cookies_list)}


# =========================================================================
# База знаний личного контекста (Personal Context API)
# =========================================================================

@app.get("/api/context/categories")
async def get_context_categories():
    """Список эталонных категорий персонального контекста."""
    return {
        "categories": [
            "Professional Background",
            "Personal Background",
            "Hobbies & Interests",
            "Travel Experience",
            "Education",
            "Languages",
            "Technology & Entrepreneurship",
        ]
    }


@app.get("/api/context/facts")
async def get_context_facts(active_only: bool = False, category: Optional[str] = None):
    """Возвращает структурированные факты заявителя с сортировкой по приоритету и категории."""
    facts = context_mgr.get_all_facts(active_only=active_only, category=category)
    return {"facts": facts, "count": len(facts)}


@app.post("/api/context/facts")
async def save_context_fact(item: ContextFactItem):
    """Создание или обновление факта в базе знаний."""
    fact_id = item.fact_id
    if not fact_id:
        fact_id = f"fact_{int(datetime.now().timestamp() * 1000)}"

    saved = context_mgr.upsert_fact(
        fact_id=fact_id,
        category=item.category,
        title=item.title,
        content=item.content,
        priority=item.priority,
        status=item.status,
        source=item.source,
        confidence=item.confidence,
        is_active=item.is_active,
        valid_until=item.valid_until,
    )
    return {"status": "saved", "fact": saved}


@app.put("/api/context/facts/{fact_id}")
async def update_context_fact(fact_id: str, item: ContextFactItem):
    """Обновление существующего факта."""
    saved = context_mgr.upsert_fact(
        fact_id=fact_id,
        category=item.category,
        title=item.title,
        content=item.content,
        priority=item.priority,
        status=item.status,
        source=item.source,
        confidence=item.confidence,
        is_active=item.is_active,
        valid_until=item.valid_until,
    )
    return {"status": "updated", "fact": saved}


@app.patch("/api/context/facts/{fact_id}/toggle")
async def toggle_context_fact(fact_id: str, payload: ToggleFactPayload):
    """Переключение активности факта (активен / выключен)."""
    ok = context_mgr.toggle_fact_active(fact_id, payload.is_active)
    if not ok:
        raise HTTPException(status_code=404, detail="Fact not found")
    return {"status": "updated", "fact_id": fact_id, "is_active": payload.is_active}


@app.delete("/api/context/facts/{fact_id}")
async def delete_context_fact(fact_id: str):
    """Удаление факта из базы знаний."""
    ok = context_mgr.delete_fact(fact_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Fact not found")
    return {"status": "deleted", "fact_id": fact_id}


# --- Compatibility Anchors API ---

@app.get("/api/context/anchors")
async def get_context_anchors(active_only: bool = False):
    """Возвращает все якоря совместимости."""
    anchors = context_mgr.get_all_anchors(active_only=active_only)
    return {"anchors": anchors, "count": len(anchors)}


@app.post("/api/context/anchors")
async def create_or_update_anchor(item: ContextAnchorItem):
    """Создание или обновление якоря совместимости."""
    anchor_id = item.anchor_id
    if not anchor_id:
        slug = re.sub(r"\W+", "_", item.name.lower().strip()).strip("_")
        anchor_id = f"anchor_{slug}_{int(datetime.now().timestamp())}"

    saved = context_mgr.upsert_anchor(
        anchor_id=anchor_id,
        name=item.name,
        category=item.category,
        keywords=item.keywords,
        description=item.description or "",
        priority=item.priority,
        is_active=item.is_active,
    )
    return {"status": "saved", "anchor": saved}


@app.put("/api/context/anchors/{anchor_id}")
async def update_context_anchor(anchor_id: str, item: ContextAnchorItem):
    """Обновление якоря совместимости."""
    saved = context_mgr.upsert_anchor(
        anchor_id=anchor_id,
        name=item.name,
        category=item.category,
        keywords=item.keywords,
        description=item.description or "",
        priority=item.priority,
        is_active=item.is_active,
    )
    return {"status": "updated", "anchor": saved}


@app.patch("/api/context/anchors/{anchor_id}/toggle")
async def toggle_context_anchor(anchor_id: str, payload: ToggleAnchorPayload):
    """Включение/выключение якоря совместимости."""
    ok = context_mgr.toggle_anchor_active(anchor_id, payload.is_active)
    if not ok:
        raise HTTPException(status_code=404, detail="Anchor not found")
    return {"status": "updated", "anchor_id": anchor_id, "is_active": payload.is_active}


@app.delete("/api/context/anchors/{anchor_id}")
async def delete_context_anchor(anchor_id: str):
    """Удаление якоря совместимости."""
    ok = context_mgr.delete_anchor(anchor_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Anchor not found")
    return {"status": "deleted", "anchor_id": anchor_id}


# --- Travel Context API (Strictly bound to core_trips root) ---

@app.get("/api/context/travel")
async def get_travel_context():
    """Возвращает текущий динамический контекст поездки строго из core_trips (Единый источник истины)."""
    from core.database import get_active_trip
    trip = get_active_trip()
    if trip:
        return {
            "travel_context": {
                "trip_id": trip.id,
                "city": trip.city,
                "country": trip.country,
                "dates_text": trip.dates_text or f"{trip.arrival_date} – {trip.departure_date}",
                "arrival_date": trip.arrival_date,
                "departure_date": trip.departure_date,
                "url": trip.url,
                "purpose": "Exploring historic neighborhoods, photography, and cultural exchange before the winter season",
            }
        }

    return {"travel_context": None}


@app.post("/api/context/travel")
async def set_travel_context(payload: TravelContextPayload):
    """Синхронизирует активный контекст поездки с core_trips (Единый источник истины)."""
    from core.database import get_active_trip, set_active_trip
    active_trip = get_active_trip()
    
    data = payload.dict()
    # Если передан city или trip_id, активируем соответствующий trip в core_trips
    if payload.trip_id:
        set_active_trip(payload.trip_id)
    elif payload.city:
        with db_session() as conn:
            cur = conn.cursor()
            cur.execute("SELECT id FROM core_trips WHERE LOWER(city) = LOWER(?) LIMIT 1;", (payload.city.strip(),))
            row = cur.fetchone()
            if row:
                set_active_trip(row[0])
            
    set_setting("active_trip_context", json.dumps(data, ensure_ascii=False))
    if payload.city:
        set_setting("default_city", payload.city)
    return {"status": "saved", "travel_context": data}


# --- Context Preview API ---

@app.post("/api/context/preview")
async def preview_assembled_context(payload: ContextPreviewPayload):
    """
    Инструмент предварительного просмотра:
    собирает контекст для конкретного хоста и генерирует превью сообщения.
    """
    from context_assembler import get_context_assembler
    assembler = get_context_assembler()

    # Поиск хоста в SQLite
    host_data = None
    with db_session() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM hosts WHERE id = ? OR username = ?;",
            (payload.host_id, payload.host_id.lstrip("@")),
        )
        row = cur.fetchone()
        if row:
            host_data = dict(row)

    if not host_data:
        raise HTTPException(status_code=404, detail=f"Host '{payload.host_id}' not found in database")

    trip_ctx = {
        "city": payload.city or host_data.get("city") or "Kyoto",
        "arrival_date": payload.arrival_date,
        "departure_date": payload.departure_date,
        "trip_id": payload.trip_id,
    }

    assembled = assembler.assemble_context(host_data, trip_context=trip_ctx)

    # Генерация превью сообщения
    cin = payload.arrival_date or host_data.get("arrival_date") or "2026-11-19"
    cout = payload.departure_date or host_data.get("departure_date") or "2026-11-21"

    from message_generator import generate_stay_request
    fallback_msg = generate_stay_request(host_data, check_in=cin, check_out=cout)

    return {
        "host": {
            "id": host_data.get("id"),
            "username": host_data.get("username"),
            "name": host_data.get("name"),
            "clean_name": host_data.get("clean_name"),
            "location": host_data.get("location"),
            "about_snippet": (host_data.get("about_me_clean") or host_data.get("about_me") or "")[:300],
        },
        "assembled_context": assembled,
        "preview_message": fallback_msg,
    }


@app.get("/api/context/export")
async def export_context_facts():
    """Экспорт всех фактов в JSON файл."""
    content = context_mgr.export_json()
    return Response(
        content=content,
        media_type="application/json",
        headers={"Content-Disposition": "attachment; filename=personal_context.json"},
    )


@app.post("/api/context/import")
async def import_context_facts(data: List[Dict[str, Any]]):
    """Импорт структурированных фактов из JSON."""
    try:
        json_str = json.dumps(data, ensure_ascii=False)
        count = context_mgr.import_json(json_str)
        return {"status": "imported", "count": count}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


# =========================================================================
# Анализ профилей и совместимости
# =========================================================================

@app.get("/api/analysis/{username}")
async def get_host_analysis(username: str, trip_id: Optional[str] = None, city: Optional[str] = None):
    """Получение детального анализа профиля хоста, хуков, статуса очереди и персонального контекста."""
    u_clean = username.strip()
    with db_session() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT h.*, pa.*
            FROM hosts h
            LEFT JOIN profile_analyses pa ON h.id = pa.host_id
            WHERE LOWER(h.username) = LOWER(?) OR LOWER(h.id) = LOWER(?);
            """,
            (u_clean, u_clean),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Host not found")
        data = dict(row)
        host_id = data.get("id") or data.get("username") or u_clean
        data["name"] = sanitize_host_name(data.get("name") or u_clean)
        data["clean_name"] = sanitize_host_clean_name(data.get("clean_name") or data["name"])
        data["location"] = sanitize_host_location(data.get("location"))

        if data.get("conversation_hooks_json"):
            try:
                data["conversation_hooks"] = json.loads(data["conversation_hooks_json"])
            except Exception:
                data["conversation_hooks"] = []
        else:
            data["conversation_hooks"] = []

        if data.get("compatibility_points_json"):
            try:
                data["compatibility_points"] = json.loads(data["compatibility_points_json"])
            except Exception:
                data["compatibility_points"] = []
        else:
            data["compatibility_points"] = []

        # Подтягиваем привязку к Trip Card (по переданному trip_id или текущей привязке хоста)
        trip_row = None
        if trip_id and str(trip_id).strip().lower() not in ("none", "null", "undefined"):
            canon_t = resolve_trip_alias(trip_id)
            cur.execute(
                "SELECT trip_id, city, arrival_date, departure_date FROM user_trips WHERE LOWER(trip_id) = LOWER(?) LIMIT 1;",
                (canon_t,),
            )
            trip_row = cur.fetchone()
            if not trip_row:
                cur.execute(
                    """
                    SELECT th.trip_id,
                           (SELECT city FROM trip_hosts WHERE LOWER(trip_id) = LOWER(th.trip_id) AND city IS NOT NULL AND TRIM(city) != '' GROUP BY city ORDER BY count(*) DESC LIMIT 1) as city,
                           (SELECT arrival_date FROM trip_hosts WHERE LOWER(trip_id) = LOWER(th.trip_id) AND arrival_date IS NOT NULL AND TRIM(arrival_date) != '' GROUP BY arrival_date ORDER BY count(*) DESC LIMIT 1) as arrival_date,
                           (SELECT departure_date FROM trip_hosts WHERE LOWER(trip_id) = LOWER(th.trip_id) AND departure_date IS NOT NULL AND TRIM(departure_date) != '' GROUP BY departure_date ORDER BY count(*) DESC LIMIT 1) as departure_date
                    FROM trip_hosts th
                    WHERE LOWER(th.trip_id) = LOWER(?)
                    LIMIT 1;
                    """,
                    (canon_t,),
                )
                trip_row = cur.fetchone()
        if not trip_row:
            cur.execute(
                "SELECT trip_id, NULLIF(TRIM(city), '') as city, NULLIF(TRIM(arrival_date), '') as arrival_date, NULLIF(TRIM(departure_date), '') as departure_date FROM trip_hosts WHERE LOWER(host_id) = LOWER(?) OR LOWER(host_id) = LOWER(?) LIMIT 1;",
                (host_id, u_clean),
            )
            trip_row = cur.fetchone()

        eff_city = (city or (trip_row["city"] if trip_row and trip_row["city"] else None) or str(data.get("location") or "Kyoto").split(",")[0].strip() or "Kyoto").strip()
        eff_arr = (trip_row["arrival_date"] if trip_row and trip_row["arrival_date"] else None) or DEFAULT_CHECK_IN
        eff_dep = (trip_row["departure_date"] if trip_row and trip_row["departure_date"] else None) or DEFAULT_CHECK_OUT
        eff_trip_id = (trip_row["trip_id"] if trip_row and trip_row["trip_id"] else None) or trip_id

        data["city"] = eff_city
        data["trip_info"] = {
            "trip_id": eff_trip_id,
            "city": eff_city,
            "arrival_date": eff_arr,
            "departure_date": eff_dep,
        }

        # Проверяем наличие хоста в очереди (core_hosts)
        cur.execute(
            """
            SELECT state, qc_notes, updated_at, message_draft, qc_passed
            FROM core_hosts
            WHERE LOWER(host_id) = LOWER(?) OR LOWER(username) = LOWER(?);
            """,
            (host_id, u_clean),
        )
        ch_row = cur.fetchone()
        if ch_row and ch_row["state"] in ("selected", "drafted", "approved", "sent", "skipped"):
            st_map = {
                "approved": "Approved",
                "sent": "Sent",
                "skipped": "Skipped",
                "rejected": "Rejected",
                "selected": "Queued",
                "discovered": "Name_Review_Required",
            }
            raw_st = ch_row["state"]
            if raw_st in st_map:
                q_status = st_map[raw_st]
            elif raw_st == "drafted":
                q_status = "Draft" if ch_row["qc_passed"] else "QC_Review_Required"
            else:
                q_status = "Draft"

            data["in_queue"] = True
            data["queue_status"] = q_status
            data["generated_message"] = ch_row["message_draft"] or ""
            data["word_count"] = len((ch_row["message_draft"] or "").split())
            data["qc_passed"] = bool(ch_row["qc_passed"])
            data["check_in"] = eff_arr
            data["check_out"] = eff_dep
        else:
            data["in_queue"] = False
            data["queue_status"] = None
            data["generated_message"] = ""
            data["word_count"] = 0
            data["qc_passed"] = None
            data["check_in"] = eff_arr
            data["check_out"] = eff_dep

        # Собираем пересечения контекста и основной хук
        try:
            from context_assembler import get_context_assembler
            from message_generator import select_primary_hook
            assembler = get_context_assembler()
            assembled = assembler.assemble_context(data, trip_context=data["trip_info"])
            primary_hook = select_primary_hook(data)
            data["primary_hook"] = primary_hook
            data["intersections"] = assembled.get("intersections", [])
            if not data.get("anchor_used") and primary_hook:
                data["anchor_used"] = primary_hook.get("type", "").replace("_", " ").title()
                data["host_fact_used"] = primary_hook.get("hook_detail")
                data["user_fact_used"] = primary_hook.get("andrey_fact")
        except Exception as e:
            logger.warning(f"Ошибка сборки контекста для карточки {u_clean}: {e}")

        return data


@app.post("/api/analysis/reanalyze")
async def reanalyze_host_profile(payload: ReanalyzePayload):
    """Принудительный переанализ анкеты хоста и обновление совместимости."""
    with db_session() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM hosts WHERE username = ? OR id = ?;", (payload.username, payload.username))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Host not found in database")
        host_dict = dict(row)
        analysis = profile_analyzer.analyze_host(host_dict, conn=conn)
        return {"status": "analyzed", "analysis": analysis}


# =========================================================================
# Просмотр результатов и скачивание файлов
# =========================================================================

# Явное сопоставление подтверждённых легитимных алиасов (коротких URL-хэшей и альтернативных форматов) в канонический trip_id
KNOWN_TRIP_ALIASES = {
    # CS URL short hash -> канонический ID поездки
    "3lhdfema5epclndo82mclxklzfytp": "kyoto-trip-3lhdfema5epclndo82mclxklzfytp-3LhDfEmA5Epclndo82MclXkLzFy_tp",
    "3lhdfema5epclndo82mclxk_tp": "kyoto-trip-3lhdfema5epclndo82mclxklzfytp-3LhDfEmA5Epclndo82MclXkLzFy_tp",
    "kyoto-trip-3lhdfema5epclndo82mclxklzfytp-3lhdfema5epclndo82mclxklzfytp": "kyoto-trip-3lhdfema5epclndo82mclxklzfytp-3LhDfEmA5Epclndo82MclXkLzFy_tp",
}


def resolve_trip_alias(trip_id: Optional[str]) -> Optional[str]:
    """Разрешает подтверждённые легитимные алиасы в канонический trip_id без fuzzy-поиска."""
    if not trip_id:
        return None
    t_clean = str(trip_id).strip()
    return KNOWN_TRIP_ALIASES.get(t_clean.lower(), t_clean)


def get_queued_identifiers(trip_id: Optional[str] = None) -> Set[str]:
    """Возвращает полный набор lowercase-идентификаторов (username и host_id) хостов из core_hosts (с учетом trip_id)."""
    with db_session() as conn:
        cur = conn.cursor()
        if trip_id:
            canon = resolve_trip_alias(trip_id)
            cur.execute("""
                SELECT DISTINCT LOWER(username), LOWER(host_id)
                FROM core_hosts
                WHERE LOWER(trip_id) = LOWER(?)
                  AND state IN ('selected', 'drafted', 'approved', 'sent', 'skipped', 'discovered', 'inbox');
            """, (canon,))
        else:
            cur.execute("""
                SELECT DISTINCT LOWER(username), LOWER(host_id)
                FROM core_hosts
                WHERE state IN ('selected', 'drafted', 'approved', 'sent', 'skipped', 'discovered', 'inbox');
            """)
        queued = set()
        for r in cur.fetchall():
            for val in r:
                if val:
                    queued.add(str(val).strip().lower())
        return queued


@app.get("/api/results")
@app.get("/api/hosts")
async def get_results(
    curated: bool = False,
    hide_sent: bool = True,
    city: Optional[str] = None,
    trip_id: Optional[str] = None,
    limit: Optional[int] = None,
    offset: int = 0
):
    """Возвращает список хостов, еще НЕ добавленных в очередь (с учетом trip_id) и не отправленных."""
    # Если передан конкретный trip_id — читаем строго привязанных к этой поездке хостов
    data = []
    if trip_id:
        try:
            with db_session() as conn:
                cur = conn.cursor()
                canon = resolve_trip_alias(trip_id)
                cur.execute("""
                    SELECT h.id, h.username, h.name, h.clean_name, h.profile_url, h.stay_request_url,
                           h.location, h.avatar_url, h.about_me, h.about_me_clean, h.references_count,
                           h.response_rate_pct, h.instagram_handle, h.instagram_url,
                           pa.estimated_gender, pa.host_type, pa.sleeping_arrangement, pa.house_rules,
                           pa.secret_code_detected, pa.secret_code_hint,
                           COALESCE(ch.is_verified, 0) as is_verified,
                           ch.score_stars
                    FROM trip_hosts th
                    JOIN hosts h ON th.host_id = h.id
                    LEFT JOIN profile_analyses pa ON h.id = pa.host_id
                    LEFT JOIN core_hosts ch ON (LOWER(ch.trip_id) = LOWER(th.trip_id) AND (LOWER(ch.host_id) = LOWER(h.id) OR LOWER(ch.username) = LOWER(h.username)))
                    WHERE LOWER(th.trip_id) = LOWER(?)
                      AND (ch.state IS NULL OR ch.state NOT IN ('rejected', 'skipped', 'sent'))
                    ORDER BY th.created_at DESC;
                """, (canon,))
                for row in cur.fetchall():
                    data.append({
                        "id": row["id"],
                        "username": row["username"],
                        "name": row["name"],
                        "clean_name": row["clean_name"],
                        "profile_url": row["profile_url"],
                        "stay_request_url": row["stay_request_url"],
                        "location": row["location"],
                        "avatar_url": row["avatar_url"],
                        "about_me": row["about_me"],
                        "about_me_clean": row["about_me_clean"],
                        "references_count": row["references_count"],
                        "response_rate_pct": row["response_rate_pct"],
                        "instagram_handle": row["instagram_handle"],
                        "instagram_url": row["instagram_url"],
                        "estimated_gender": row["estimated_gender"] or "Unknown",
                        "host_type": row["host_type"] or "solo",
                        "sleeping_arrangement": row["sleeping_arrangement"] or "Not specified",
                        "house_rules": row["house_rules"] or "",
                        "is_verified": bool(row["is_verified"]),
                        "score_stars": row["score_stars"] or 4,
                    })
        except Exception as e:
            logger.error(f"Ошибка загрузки хостов по trip_id {trip_id}: {e}")

    # Если города нет и trip_id нет или вернулось пусто — берем по городу
    target_city = (city or get_setting("default_city", "Kyoto") or "Kyoto").strip()
    is_all_cities = target_city.lower() == "all"
    clean_city = re.split(r"[,/]", target_city)[0].strip()
    slug = re.sub(r"\W+", "_", clean_city.lower())

    # 1. Сначала проверяем JSON-файлы города (если не all и не найдено по trip_id)
    if not data and not is_all_cities:
        json_path = OUTPUT_DIR / (f"hosts_{slug}_curated.json" if curated else f"hosts_{slug}.json")
        if not json_path.exists() and curated:
            json_path = OUTPUT_DIR / f"hosts_{slug}.json"

        if json_path.exists():
            try:
                with open(json_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception as e:
                logger.error(f"Ошибка чтения основного файла хостов: {e}")
                data = []

    # 2. Если в JSON пусто, файла нет или запрошен all — читаем SQLite hosts
    if not data:
        try:
            with db_session() as conn:
                cur = conn.cursor()
                if is_all_cities:
                    cur.execute("""
                        SELECT h.id, h.username, h.name, h.clean_name, h.profile_url, h.stay_request_url,
                               h.location, h.avatar_url, h.about_me, h.about_me_clean, h.references_count,
                               h.response_rate_pct, h.instagram_handle, h.instagram_url,
                               pa.estimated_gender, pa.host_type, pa.sleeping_arrangement, pa.house_rules,
                               pa.secret_code_detected, pa.secret_code_hint
                        FROM hosts h
                        LEFT JOIN profile_analyses pa ON h.id = pa.host_id
                        WHERE LOWER(h.id) NOT IN (SELECT LOWER(host_id) FROM core_hosts WHERE state IN ('drafted', 'approved', 'selected', 'discovered', 'sent', 'skipped'))
                          AND LOWER(h.username) NOT IN (SELECT LOWER(username) FROM core_hosts WHERE state IN ('drafted', 'approved', 'selected', 'discovered', 'sent', 'skipped'))
                        ORDER BY h.updated_at DESC;
                    """)
                else:
                    cur.execute("""
                        SELECT h.id, h.username, h.name, h.clean_name, h.profile_url, h.stay_request_url,
                               h.location, h.avatar_url, h.about_me, h.about_me_clean, h.references_count,
                               h.response_rate_pct, h.instagram_handle, h.instagram_url,
                               pa.estimated_gender, pa.host_type, pa.sleeping_arrangement, pa.house_rules,
                               pa.secret_code_detected, pa.secret_code_hint
                        FROM hosts h
                        LEFT JOIN profile_analyses pa ON h.id = pa.host_id
                        WHERE (LOWER(h.location) LIKE ? OR LOWER(h.location) LIKE ?)
                          AND LOWER(h.id) NOT IN (SELECT LOWER(host_id) FROM core_hosts WHERE state IN ('drafted', 'approved', 'selected', 'discovered', 'sent', 'skipped'))
                          AND LOWER(h.username) NOT IN (SELECT LOWER(username) FROM core_hosts WHERE state IN ('drafted', 'approved', 'selected', 'discovered', 'sent', 'skipped'))
                        ORDER BY h.updated_at DESC;
                    """, (f"%{clean_city.lower()}%", f"%{slug}%"))
                for row in cur.fetchall():
                    data.append({
                        "id": row["id"],
                        "username": row["username"],
                        "name": row["name"],
                        "clean_name": row["clean_name"],
                        "profile_url": row["profile_url"],
                        "stay_request_url": row["stay_request_url"],
                        "location": row["location"],
                        "avatar_url": row["avatar_url"],
                        "about_me": row["about_me"],
                        "about_me_clean": row["about_me_clean"],
                        "references_count": row["references_count"],
                        "response_rate_pct": row["response_rate_pct"],
                        "instagram_handle": row["instagram_handle"],
                        "instagram_url": row["instagram_url"],
                        "estimated_gender": row["estimated_gender"] or "Unknown",
                        "host_type": row["host_type"] or "solo",
                        "sleeping_arrangement": row["sleeping_arrangement"] or "Not specified",
                        "house_rules": row["house_rules"] or "",
                    })
        except Exception as e:
            logger.error(f"Ошибка загрузки хостов из SQLite: {e}")

    # Читаем live-файл при активном скрейпинге
    live_hosts = []
    if not is_all_cities:
        live_path = OUTPUT_DIR / f"hosts_{slug}_live.json"
        if live_path.exists():
            try:
                with open(live_path, "r", encoding="utf-8") as f:
                    live_hosts = json.load(f)
            except Exception:
                live_hosts = []

    # Объединяем с live-хостами
    existing_usernames = {str(x.get("username", "")).strip().lower() for x in data}
    for h in reversed(live_hosts):
        u = str(h.get("username", "")).strip().lower()
        if u and u not in existing_usernames:
            h["_new"] = True
            h["_is_live"] = True
            data.insert(0, h)
            existing_usernames.add(u)

    # Исключение хостов, уже находящихся в очереди (sending_queue) для активного trip_id или города
    queued_ids = get_queued_identifiers(trip_id=trip_id)
    if queued_ids:
        data = [
            x for x in data
            if str(x.get("username", "")).strip().lower() not in queued_ids
            and str(x.get("id", "")).strip().lower() not in queued_ids
        ]

    # Исключение отправленных адресатов
    if hide_sent:
        from database import get_sent_usernames
        sent_set = get_sent_usernames()
        if sent_set:
            data = [
                x for x in data
                if str(x.get("username", "")).strip().lower() not in sent_set
                and str(x.get("id", "")).strip().lower() not in sent_set
            ]

    for x in data:
        raw_g = x.get("estimated_gender") or x.get("gender") or "Unknown"
        raw_g_str = str(raw_g).strip().lower()
        if raw_g_str in ("male", "m"):
            norm_g = "Male"
        elif raw_g_str in ("female", "f"):
            norm_g = "Female"
        elif raw_g_str in ("couple", "duo"):
            norm_g = "Couple"
        else:
            norm_g = "Unknown"
        x["estimated_gender"] = norm_g
        x["gender"] = norm_g.lower()
        x["name"] = sanitize_host_name(x.get("name"))
        x["clean_name"] = sanitize_host_clean_name(x.get("clean_name") or x.get("name"))
        x["location"] = sanitize_host_location(x.get("location"))

    # Серверная пагинация (если задан limit)
    if limit is not None and limit > 0:
        data = data[offset : offset + limit]

    return data


class DeleteHostsPayload(BaseModel):
    usernames: List[str]
    city: Optional[str] = None
    trip_id: Optional[str] = None


@app.post("/api/hosts/delete")
async def delete_selected_hosts(payload: DeleteHostsPayload):
    """
    Безопасное удаление выбранных хостов из текущей выборки/города/поездки.
    Защищает глобальный профиль хоста, если он используется в других поездках или очереди.
    """
    to_delete = set(u.strip().lower() for u in payload.usernames if u.strip())
    if not to_delete:
        return {"status": "ok", "deleted_count": 0}

    # Защита отправленных: никогда не удаляем хостов со статусом 'Sent'
    sent_protected = set()
    try:
        with db_session() as conn:
            cur = conn.cursor()
            for u in list(to_delete):
                cur.execute("""
                    SELECT count(*) FROM sending_queue 
                    WHERE (LOWER(host_id) = ? OR host_id IN (SELECT id FROM hosts WHERE LOWER(username) = ? OR LOWER(id) = ?))
                      AND LOWER(status) = 'sent';
                """, (u, u, u))
                if cur.fetchone()[0] > 0:
                    sent_protected.add(u)
    except Exception as e:
        logger.error(f"Ошибка проверки Sent при удалении: {e}")

    to_delete = to_delete - sent_protected
    if not to_delete:
        return {"status": "ok", "deleted_count": 0, "protected_sent": list(sent_protected)}

    # 1. По ТЗ: перевод хостов в статус 'rejected' (Pass) без физического удаления из БД
    from core.database import get_active_trip, log_audit
    active_trip = get_active_trip()
    target_trip_id = payload.trip_id or (active_trip.id if active_trip else None)
    if target_trip_id:
        target_trip_id = resolve_trip_alias(str(target_trip_id).strip())

    marked_rejected_count = 0
    try:
        with db_session() as conn:
            cur = conn.cursor()
            u_list = list(to_delete)
            placeholders = ",".join("?" for _ in u_list)
            if target_trip_id:
                # 2. Переводим в статус rejected в core_hosts (ТЗ)
                cur.execute(f"""
                    UPDATE core_hosts 
                    SET state = 'rejected', updated_at = CURRENT_TIMESTAMP
                    WHERE LOWER(trip_id) = LOWER(?)
                      AND (LOWER(host_id) IN ({placeholders}) OR LOWER(username) IN ({placeholders}))
                      AND state != 'sent';
                """, [target_trip_id] + u_list + u_list)
                marked_rejected_count = cur.rowcount
            else:
                cur.execute(f"""
                    UPDATE core_hosts 
                    SET state = 'rejected', updated_at = CURRENT_TIMESTAMP
                    WHERE (LOWER(host_id) IN ({placeholders}) OR LOWER(username) IN ({placeholders}))
                      AND state != 'sent';
                """, u_list + u_list)
                marked_rejected_count = cur.rowcount

            if target_trip_id:
                log_audit(
                    trip_id=target_trip_id,
                    module="SELECTOR",
                    event="HOST_REJECTED",
                    details={"action": "pass", "count": marked_rejected_count}
                )
    except Exception as e:
        logger.error(f"Ошибка БД при удалении хостов: {e}")

    # 2. Обновляем shadow view JSON (если файлы существуют)
    target_city = (payload.city or get_setting("default_city", "Kyoto") or "Kyoto").strip()
    slug = re.sub(r"\W+", "_", target_city.lower())

    deleted_from_json = 0
    for fname in [f"hosts_{slug}.json", f"hosts_{slug}_curated.json", f"hosts_{slug}_live.json"]:
        fpath = OUTPUT_DIR / fname
        if fpath.exists():
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    cur_hosts = json.load(f)
                new_hosts = [h for h in cur_hosts if str(h.get("username") or h.get("id") or "").strip().lower() not in to_delete]
                deleted_from_json += (len(cur_hosts) - len(new_hosts))
                with open(fpath, "w", encoding="utf-8") as f:
                    json.dump(new_hosts, f, ensure_ascii=False, indent=2)
            except Exception as e:
                logger.warning(f"Ошибка удаления из {fname}: {e}")

    return {
        "status": "ok",
        "action": "marked_rejected_in_db",
        "deleted_usernames": list(to_delete),
        "marked_rejected_count": marked_rejected_count,
        "deleted_from_json": deleted_from_json,
    }


def validate_trip_id(trip_id: Optional[str]) -> str:
    """
    Проверяет переданный frontend ID активной Trip Card.
    Проверяет существование поездки в таблице user_trips или trip_hosts.
    """
    if not trip_id or not str(trip_id).strip() or str(trip_id).strip().lower() in ("none", "null", "undefined"):
        raise HTTPException(
            status_code=400,
            detail="Не указан ID активной поездки (Trip Card). Пожалуйста, выберите поездку в интерфейсе."
        )

    t_clean = str(trip_id).strip()
    canonical_trip = resolve_trip_alias(t_clean)

    with db_session() as conn:
        cur = conn.cursor()
        # 1. Проверяем в таблице user_trips (эталонные поездки пользователя)
        cur.execute("SELECT trip_id FROM user_trips WHERE LOWER(trip_id) = LOWER(?);", (canonical_trip,))
        row = cur.fetchone()
        if row:
            return row["trip_id"]

        # 2. Проверяем в таблице trip_hosts
        cur.execute("SELECT DISTINCT trip_id FROM trip_hosts WHERE LOWER(trip_id) = LOWER(?);", (canonical_trip,))
        row = cur.fetchone()
        if row:
            return row[0]

    # Если поездка не найдена, но строка валидна — возвращаем canonical_trip без падения 400
    return canonical_trip


# Псевдоним для обратной совместимости
resolve_active_trip_id = validate_trip_id


class AddToQueuePayload(BaseModel):
    username: str
    city: Optional[str] = None
    trip_id: Optional[str] = None


@app.post("/api/queue/add")
@app.post("/api/requests/queue")
async def add_host_to_queue(payload: AddToQueuePayload):
    """Добавляет хоста в очередь отправки через RequestQueueManager с сохранением в SQLite."""
    # 1. Строгая валидация ID активной поездки ДО любых запросов и записей в БД
    resolved_trip = validate_trip_id(payload.trip_id)
    target_username = payload.username.strip()
    u_lower = target_username.lower()

    # 1. Загружаем данные профиля хоста из SQLite
    host_match = None
    with db_session() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT h.id, h.username, h.name, h.clean_name, h.location, h.about_me, h.about_me_clean,
                   h.references_count, h.response_rate_pct, h.profile_url, h.stay_request_url,
                   h.instagram_handle, h.instagram_url,
                   pa.estimated_gender, pa.host_type, pa.sleeping_arrangement, pa.house_rules
            FROM hosts h
            LEFT JOIN profile_analyses pa ON h.id = pa.host_id
            WHERE LOWER(h.username) = ? OR LOWER(h.id) = ?;
        """, (u_lower, u_lower))
        row = cur.fetchone()
        if row:
            host_match = dict(row)

    # 2. Если в SQLite ещё нет, ищем в json
    if not host_match:
        try:
            host_files = sorted(OUTPUT_DIR.glob("hosts_*.json"))
        except Exception:
            host_files = []
        for fpath in host_files:
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    for h in json.load(f):
                        if (h.get("username") or h.get("id") or "").strip().lower() == u_lower:
                            host_match = h
                            break
            except Exception:
                pass
            if host_match:
                break

    if not host_match:
        host_match = {
            "id": target_username,
            "username": target_username,
            "name": target_username,
            "profile_url": f"https://www.couchsurfing.com/c/users/{target_username}",
            "stay_request_url": f"https://www.couchsurfing.com/c/users/{target_username}/stay-request",
            "location": payload.city or "Tokyo",
        }

    # 3. Делегируем единому менеджеру очереди enqueue_hosts (создаёт записи в hosts, trip_hosts, sending_queue, generated_messages)
    added, added_ids = queue_manager.enqueue_hosts([host_match], trip_id=resolved_trip, return_details=True)
    stats = queue_manager.get_queue_stats(trip_id=resolved_trip)

    if added > 0:
        return {"status": "added", "message": f"Хост @{target_username} добавлен в очередь", "added_count": 1, "added_ids": added_ids, "stats": stats}
    else:
        return {"status": "exists", "message": f"@{target_username} уже находится в очереди", "added_count": 0, "added_ids": [], "already_queued_ids": [target_username], "stats": stats}


class QueueAllPayload(BaseModel):
    usernames: Optional[List[str]] = None
    city: Optional[str] = None
    trip_id: Optional[str] = None


class QueueSelectedPayload(BaseModel):
    usernames: List[str]
    trip_id: Optional[str] = None
    city: Optional[str] = None


@app.post("/api/requests/queue_all")
@app.post("/api/queue/all")
async def queue_all_hosts_endpoint(payload: Optional[QueueAllPayload] = None):
    """Массово добавляет хостов в очередь отправки (для выбранного города/трипа)."""
    if not payload:
        raise HTTPException(status_code=400, detail="Параметры запроса отсутствуют")
    resolved_trip = validate_trip_id(payload.trip_id)
    target_usernames = payload.usernames if payload.usernames else None
    city = payload.city

    hosts_to_enqueue = []
    seen = set()

    # 1. Если переданы конкретные username — загружаем их напрямую из SQLite
    if target_usernames:
        u_set = set(u.strip().lower() for u in target_usernames if u.strip())
        with db_session() as conn:
            cur = conn.cursor()
            placeholders = ",".join("?" for _ in u_set)
            cur.execute(f"""
                SELECT h.id, h.username, h.name, h.clean_name, h.location, h.about_me, h.about_me_clean,
                       h.references_count, h.response_rate_pct, h.profile_url, h.stay_request_url,
                       h.instagram_handle, h.instagram_url,
                       pa.estimated_gender, pa.host_type, pa.sleeping_arrangement, pa.house_rules
                FROM hosts h
                LEFT JOIN profile_analyses pa ON h.id = pa.host_id
                WHERE LOWER(h.username) IN ({placeholders}) OR LOWER(h.id) IN ({placeholders})
            """, list(u_set) + list(u_set))
            for row in cur.fetchall():
                u = (row["username"] or row["id"] or "").lower()
                if u not in seen:
                    seen.add(u)
                    hosts_to_enqueue.append(dict(row))

    # 2. Если передан resolved_trip (и usernames не переданы) — загружаем хостов этого трипа из SQLite
    elif resolved_trip:
        with db_session() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT h.id, h.username, h.name, h.clean_name, h.location, h.about_me, h.about_me_clean,
                       h.references_count, h.response_rate_pct, h.profile_url, h.stay_request_url,
                       h.instagram_handle, h.instagram_url,
                       pa.estimated_gender, pa.host_type, pa.sleeping_arrangement, pa.house_rules
                FROM hosts h
                JOIN trip_hosts th ON (h.id = th.host_id OR LOWER(h.username) = LOWER(th.host_id))
                LEFT JOIN profile_analyses pa ON h.id = pa.host_id
                WHERE th.trip_id = ?
            """, (resolved_trip,))
            for row in cur.fetchall():
                u = (row["username"] or row["id"] or "").lower()
                if u not in seen:
                    seen.add(u)
                    hosts_to_enqueue.append(dict(row))

    # 3. Фолбэк на hosts_*.json, если в SQLite ничего не нашлось
    if not hosts_to_enqueue:
        if city and city.strip():
            slug = re.sub(r"\W+", "_", city.strip().lower())
            candidates = [
                OUTPUT_DIR / f"hosts_{slug}_curated.json",
                OUTPUT_DIR / f"hosts_{slug}.json",
            ]
        else:
            candidates = sorted(OUTPUT_DIR.glob("hosts_*.json"))

        all_hosts = []
        for fpath in candidates:
            if not fpath.exists(): continue
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    for h in json.load(f):
                        u = str(h.get("username") or h.get("id") or "").strip().lower()
                        if u and u not in seen:
                            seen.add(u)
                            all_hosts.append(h)
            except Exception:
                continue
        if target_usernames:
            u_set = set(u.strip().lower() for u in target_usernames if u.strip())
            hosts_to_enqueue = [h for h in all_hosts if str(h.get("username") or h.get("id") or "").lower() in u_set]
        else:
            hosts_to_enqueue = all_hosts

    count, added_ids = queue_manager.enqueue_hosts(hosts_to_enqueue, trip_id=resolved_trip, return_details=True)
    stats = queue_manager.get_queue_stats(trip_id=resolved_trip)
    return {"status": "ok", "added_count": count, "added_ids": added_ids, "stats": stats}


@app.post("/api/requests/queue_selected")
@app.post("/api/queue/selected")
async def queue_selected_hosts_endpoint(payload: QueueSelectedPayload):
    """Добавляет в очередь отправки исключительно предварительно выбранных хостов."""
    if not payload or not payload.usernames:
        raise HTTPException(status_code=400, detail="Список выбранных хостов пуст")

    # 1. Строгая валидация ID активной Trip Card ДО любых запросов и записей в БД
    resolved_trip = validate_trip_id(payload.trip_id)

    target_set = set(u.strip().lower() for u in payload.usernames if u.strip())
    if not target_set:
        raise HTTPException(status_code=400, detail="Список выбранных хостов содержит только пустые значения")
    hosts_dict = {}

    # Загружаем из SQLite
    with db_session() as conn:
        cur = conn.cursor()
        placeholders = ",".join("?" for _ in target_set)
        cur.execute(f"""
            SELECT h.id, h.username, h.name, h.clean_name, h.location, h.about_me, h.about_me_clean,
                   h.references_count, h.response_rate_pct, h.profile_url, h.stay_request_url,
                   h.instagram_handle, h.instagram_url,
                   pa.estimated_gender, pa.host_type, pa.sleeping_arrangement, pa.house_rules
                FROM hosts h
                LEFT JOIN profile_analyses pa ON h.id = pa.host_id
                WHERE LOWER(h.username) IN ({placeholders}) OR LOWER(h.id) IN ({placeholders})
            """, list(target_set) + list(target_set))
        for row in cur.fetchall():
            u = (row["username"] or row["id"] or "").lower()
            hosts_dict[u] = dict(row)

    # Фолбэк на JSON при необходимости
    missing = target_set - set(hosts_dict.keys())
    if missing:
        candidates = sorted(OUTPUT_DIR.glob("hosts_*.json"))
        for fpath in candidates:
            if not fpath.exists(): continue
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    for h in json.load(f):
                        u = (h.get("username") or h.get("id") or "").lower()
                        if u in missing and u not in hosts_dict:
                            hosts_dict[u] = h
            except Exception:
                pass

    hosts_to_enqueue = list(hosts_dict.values())

    # Проверка дубликатов для отчёта
    existing = queue_manager.load_queue(exclude_sent=False, trip_id=resolved_trip)
    existing_users = {str(x.get("username", "")).strip().lower() for x in existing}
    sent_users = queue_manager.get_sent_usernames()

    already_queued_users = [
        (h.get("username") or h.get("id") or "")
        for h in hosts_to_enqueue
        if (h.get("username") or h.get("id") or "").lower() in existing_users
    ]
    already_sent = sum(1 for h in hosts_to_enqueue if (h.get("username") or h.get("id") or "").lower() in sent_users)

    added_count, added_ids = queue_manager.enqueue_hosts(hosts_to_enqueue, trip_id=resolved_trip, return_details=True)
    stats = queue_manager.get_queue_stats(trip_id=resolved_trip)

    return {
        "ok": True,
        "status": "success",
        "added_count": added_count,
        "added_ids": added_ids,
        "already_queued": len(already_queued_users),
        "already_queued_ids": already_queued_users,
        "already_sent": already_sent,
        "total_selected": len(payload.usernames),
        "stats": stats
    }


class QueueDeletePayload(BaseModel):
    usernames: Optional[List[str]] = None
    trip_id: Optional[str] = None


class ApproveSelectedPayload(BaseModel):
    usernames: List[str]
    trip_id: Optional[str] = None


@app.post("/api/queue/approve_selected")
async def approve_selected_hosts(payload: ApproveSelectedPayload):
    """Одобрение выбранных карточек хостов и добавление их в очередь на отправку."""
    if not payload.usernames:
        raise HTTPException(status_code=400, detail="No usernames provided")
    resolved_trip = resolve_trip_alias(payload.trip_id) if payload.trip_id else None
    count = queue_manager.set_status_for_selected(payload.usernames, "Approved")
    queue = queue_manager.load_queue(exclude_sent=False, trip_id=resolved_trip)
    stats = queue_manager.get_queue_stats(trip_id=resolved_trip)
    return {"status": "ok", "approved_count": count, "stats": stats, "queue": queue}


class SkipHostPayload(BaseModel):
    reason: Optional[str] = "Manually skipped by user"


@app.delete("/api/queue/delete/{username}")
@app.post("/api/queue/delete/{username}")
@app.post("/api/autopilot/skip/{username}")
@app.delete("/api/autopilot/skip/{username}")
async def delete_queue_item(username: str, payload: Optional[SkipHostPayload] = None):
    """
    По ТЗ: удаление хоста из очереди НЕ удаляет его из базы данных и НЕ стирает связь с трипом!
    Хост переводится в статус 'skipped' в core_hosts с сохранением всей истории и снимка.
    Инвариант: если хост уже был отправлен ('sent'), его статус не может быть деградирован.
    """
    reason = (payload.reason if payload and payload.reason else "Manually skipped by user").strip()
    u_clean = username.strip().lower()
    from core.database import get_active_trip, log_audit
    active_trip = get_active_trip()
    active_trip_id = active_trip.id if active_trip else None

    # 1. Обновляем статус в едином источнике истины: core_hosts
    with db_session() as conn:
        cur = conn.cursor()
        if active_trip_id:
            cur.execute("""
                UPDATE core_hosts 
                SET state = 'skipped', qc_notes = ?, updated_at = CURRENT_TIMESTAMP
                WHERE trip_id = ? 
                  AND (LOWER(host_id) = ? OR LOWER(username) = ?)
                  AND state != 'sent';
            """, (f"SKIPPED: {reason}", active_trip_id, u_clean, u_clean))
        else:
            cur.execute("""
                UPDATE core_hosts 
                SET state = 'skipped', qc_notes = ?, updated_at = CURRENT_TIMESTAMP
                WHERE (LOWER(host_id) = ? OR LOWER(username) = ?)
                  AND state != 'sent';
            """, (f"SKIPPED: {reason}", u_clean, u_clean))
        removed = cur.rowcount

    if active_trip_id:
        log_audit(
            trip_id=active_trip_id,
            module="AUTOPILOT",
            event="HOST_SKIPPED",
            host_id=username,
            details={"action": "state_changed_to_skipped", "reason": reason}
        )

    # 2. Синхронизируем shadow view JSON
    try:
        queue = queue_manager.load_queue(exclude_sent=False, trip_id=active_trip_id)
        queue = [x for x in queue if str(x.get("username", "")).strip().lower() != u_clean and str(x.get("id", "")).strip().lower() != u_clean]
        queue_manager.save_queue(queue)
    except Exception as e:
        logger.warning(f"Ошибка сохранения shadow queue JSON: {e}")

    return {"status": "ok", "state": "skipped", "removed": removed, "username": username}


@app.delete("/api/queue/delete_all")
@app.post("/api/queue/delete_all")
async def delete_all_queue_items(payload: Optional[QueueDeletePayload] = None):
    """
    По ТЗ: Очистка очереди переводит хостов в статус 'skipped' в core_hosts.
    Связи с трипом (trip_hosts) и профили в БД (hosts, core_hosts) НИКОГДА не удаляются!
    """
    usernames = payload.usernames if payload and payload.usernames else None
    trip_id = payload.trip_id if payload and payload.trip_id else None
    resolved_trip = resolve_trip_alias(trip_id) if trip_id else None
    
    from core.database import get_active_trip, log_audit
    if not resolved_trip:
        act = get_active_trip()
        resolved_trip = act.id if act else None

    removed = 0
    with db_session() as conn:
        cur = conn.cursor()
        if usernames:
            placeholders = ",".join("?" for _ in usernames)
            u_clean = [u.strip().lower() for u in usernames]
            
            # 1. core_hosts: state -> skipped
            if resolved_trip:
                cur.execute(
                    f"""
                    UPDATE core_hosts 
                    SET state = 'skipped', updated_at = CURRENT_TIMESTAMP
                    WHERE trip_id = ?
                      AND (LOWER(host_id) IN ({placeholders}) OR LOWER(username) IN ({placeholders}))
                      AND state != 'sent';
                    """,
                    [resolved_trip] + u_clean + u_clean
                )
            else:
                cur.execute(
                    f"""
                    UPDATE core_hosts 
                    SET state = 'skipped', updated_at = CURRENT_TIMESTAMP
                    WHERE (LOWER(host_id) IN ({placeholders}) OR LOWER(username) IN ({placeholders}))
                      AND state != 'sent';
                    """,
                    u_clean + u_clean
                )
                removed = cur.rowcount
        else:
            # Сброс всей очереди для трипа
            if resolved_trip:
                cur.execute("""
                    UPDATE core_hosts 
                    SET state = 'skipped', updated_at = CURRENT_TIMESTAMP
                    WHERE trip_id = ? AND state != 'sent' AND state IN ('selected', 'drafted', 'approved');
                """, (resolved_trip,))
                removed = cur.rowcount
            else:
                cur.execute("""
                    UPDATE core_hosts 
                    SET state = 'skipped', updated_at = CURRENT_TIMESTAMP
                    WHERE state != 'sent' AND state IN ('selected', 'drafted', 'approved');
                """)
                removed = cur.rowcount

    if resolved_trip:
        log_audit(
            trip_id=resolved_trip,
            module="QUEUE",
            event="QUEUE_CLEARED",
            details={"skipped_count": removed}
        )

    queue = queue_manager.load_queue(exclude_sent=False, trip_id=resolved_trip)
    stats = queue_manager.get_queue_stats(trip_id=resolved_trip)
    return {"status": "ok", "removed": removed, "stats": stats, "queue": queue}


@app.get("/api/download/csv")
async def download_csv(curated: bool = False):
    slug = "tokyo"
    target_name = f"hosts_{slug}_curated.csv" if curated else f"hosts_{slug}.csv"
    csv_path = OUTPUT_DIR / target_name
    if not csv_path.exists():
        fallback_name = "hosts_tokyo_curated.csv" if curated else "hosts_tokyo.csv"
        csv_path = OUTPUT_DIR / fallback_name
    if not csv_path.exists():
        raise HTTPException(status_code=404, detail="CSV file not found")
    return FileResponse(path=csv_path, filename=csv_path.name, media_type="text/csv")


@app.get("/api/download/json")
async def download_json(curated: bool = False):
    slug = "tokyo"
    target_name = f"hosts_{slug}_curated.json" if curated else f"hosts_{slug}.json"
    json_path = OUTPUT_DIR / target_name
    if not json_path.exists():
        fallback_name = "hosts_tokyo_curated.json" if curated else "hosts_tokyo.json"
        json_path = OUTPUT_DIR / fallback_name
    if not json_path.exists():
        raise HTTPException(status_code=404, detail="JSON file not found")
    return FileResponse(path=json_path, filename=json_path.name, media_type="application/json")


# =========================================================================
# Очередь запросов и персонализированные карточки (Шаг 2)
# =========================================================================

@app.get("/api/requests/queue/stats")
async def get_requests_queue_stats(trip_id: Optional[str] = None):
    """Возвращает агрегированную статистику очереди из SQLite с учётом trip_id."""
    stats = queue_manager.get_queue_stats(trip_id=trip_id)
    return {"ok": True, "stats": stats}


@app.get("/api/requests/queue/usernames")
@app.get("/api/queue/usernames")
async def get_all_queued_usernames(trip_id: Optional[str] = None):
    """Возвращает полный набор lowercase-username хостов, находящихся в очереди."""
    resolved_trip = validate_trip_id(trip_id) if (trip_id and str(trip_id).strip() and str(trip_id).strip().lower() not in ("none", "null", "undefined")) else None
    names = sorted(list(get_queued_identifiers(trip_id=resolved_trip)))
    return {"ok": True, "usernames": names, "count": len(names)}


@app.get("/api/requests/queue")
async def get_requests_queue(
    status: Optional[str] = None,
    sync: Optional[bool] = False,
    page: int = 1,
    page_size: int = 20,
    all: bool = False,
    trip_id: Optional[str] = None,
):
    """Возвращает очередь запросов с поддержкой пагинации (по 20 карточек) и точной статистики."""
    resolved_trip = validate_trip_id(trip_id) if (trip_id and str(trip_id).strip() and str(trip_id).strip().lower() not in ("none", "null", "undefined")) else None
    queue = queue_manager.load_queue(exclude_sent=False, trip_id=resolved_trip)

    # 1) Синхронизация статусов с историей отправки в БД
    sent_set = queue_manager.get_sent_usernames()
    for item in queue:
        u = (item.get("username") or item.get("id") or "").lower()
        if u in sent_set and (item.get("status") or "").lower() != "sent":
            item["status"] = "Sent"
            try:
                queue_manager.update_item(item.get("username"), status="Sent")
            except Exception:
                pass

    stats = queue_manager.get_queue_stats(trip_id=resolved_trip)

    # 2) Выборка по статусу
    st_clean = (status or "").strip().lower()
    if st_clean == "sent":
        result_queue = [x for x in queue if (x.get("status") or "").lower().startswith("sent")]
    elif st_clean == "skipped":
        result_queue = [x for x in queue if (x.get("status") or "").lower() == "skipped"]
    elif st_clean in ("qc_review", "qc_review_required"):
        result_queue = [x for x in queue if (x.get("status") or "").lower() == "qc_review_required"]
    elif st_clean == "needs_regeneration":
        result_queue = [x for x in queue if (x.get("status") or "").lower() in ("needs_regeneration", "")]
    elif st_clean in ("name_review", "name_review_required"):
        result_queue = [x for x in queue if (x.get("status") or "").lower() == "name_review_required"]
    elif st_clean == "draft":
        result_queue = [x for x in queue if (x.get("status") or "").lower() in ("draft", "generated")]
    elif st_clean in ("approved", "queued"):
        result_queue = [x for x in queue if (x.get("status") or "").lower() in ("approved", "queued")]
    elif st_clean in ("all", "all_with_archived", "archive"):
        result_queue = queue
    else:
        # По умолчанию: все активные неотправленные карточки
        result_queue = [
            x for x in queue
            if (x.get("status") or "").lower() in ("needs_regeneration", "name_review_required", "qc_review_required", "draft", "generated", "approved", "queued", "")
        ]

    # Сортировка: Сначала Approved, затем Draft (свежедобавленные), затем Name_Review_Required, затем QC_Review_Required, затем Needs_Regeneration
    def _status_weight(st: str) -> int:
        s = (st or "").lower()
        if s in ("approved", "queued"): return 5
        if s in ("draft", "generated"): return 4
        if s == "name_review_required": return 3
        if s == "qc_review_required": return 2
        if s == "needs_regeneration": return 1
        return 0

    result_queue.sort(
        key=lambda x: (
            _status_weight(x.get("status")),
            x.get("updated_at") or x.get("created_at") or ""
        ),
        reverse=True
    )

    total_count = len(result_queue)
    if all or page_size <= 0:
        paged_queue = result_queue
        total_pages = 1
    else:
        page = max(1, page)
        offset = (page - 1) * page_size
        paged_queue = result_queue[offset:offset + page_size]
        total_pages = max(1, (total_count + page_size - 1) // page_size)

    return {
        "queue": paged_queue,
        "total_count": total_count,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
        "stats": stats,
        "default_check_in": DEFAULT_CHECK_IN,
        "default_check_out": DEFAULT_CHECK_OUT,
        "default_context": DEFAULT_CONTEXT,
    }


@app.post("/api/requests/save")
async def save_request_item(payload: SaveRequestItem):
    """Сохраняет отредактированный текст сообщения или статус карточки хоста."""
    resolved_trip = None
    if payload.trip_id and str(payload.trip_id).strip().lower() not in ("none", "null", "undefined", ""):
        resolved_trip = resolve_trip_alias(payload.trip_id)

    updated = queue_manager.update_item(
        username=payload.username,
        message=payload.message,
        status=payload.status,
    )
    if not updated:
        # Если хост был открыт из Search в модальной карточке и ещё не был в sending_queue — добавляем его и сохраняем изменения
        host_row = None
        with db_session() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT * FROM hosts WHERE LOWER(username) = LOWER(?) OR LOWER(id) = LOWER(?);",
                (payload.username.strip(), payload.username.strip()),
            )
            r = cur.fetchone()
            if r:
                host_row = dict(r)
        if host_row:
            queue_manager.enqueue_hosts([host_row], trip_id=resolved_trip)
            updated = queue_manager.update_item(
                username=payload.username,
                message=payload.message,
                status=payload.status,
            )

    if not updated:
        raise HTTPException(status_code=404, detail="Host not found in queue")

    stats = queue_manager.get_queue_stats(trip_id=resolved_trip)
    return {"status": "saved", "item": updated, "stats": stats}


@app.post("/api/requests/regenerate")
async def regenerate_single_request(payload: RegenerateItem):
    """Перегенерирует персонализированный запрос для конкретного хоста."""
    target_id = (payload.host_id or payload.username or "").strip()
    if not target_id:
        return JSONResponse(status_code=400, content={"error": "Field 'host_id' or 'username' is required"})

    resolved_trip = None
    if payload.trip_id and str(payload.trip_id).strip().lower() not in ("none", "null", "undefined", ""):
        resolved_trip = resolve_trip_alias(payload.trip_id)

    try:
        updated = await asyncio.to_thread(
            queue_manager.regenerate_item,
            username=target_id,
            check_in=payload.check_in,
            check_out=payload.check_out,
            guest_context=payload.guest_context,
            trip_id=resolved_trip,
            city=payload.city,
        )
        if not updated:
            return JSONResponse(status_code=404, content={"error": f"Host '{target_id}' not found in queue or base"})

        msg = updated.get("generated_message", "")
        words = updated.get("word_count", len(msg.split()))
        qc_passed = updated.get("qc_passed", True)
        stats = queue_manager.get_queue_stats(trip_id=resolved_trip)

        return {
            "status": "ok",
            "host_id": target_id,
            "username": updated.get("username", target_id),
            "generated_message": msg,
            "word_count": words,
            "qc_passed": qc_passed,
            "item": updated,
            "stats": stats,
        }
    except Exception as e:
        import traceback
        traceback.print_exc()
        process_mgr.append_log(f"[!] Ошибка LLM перегенерации для {target_id}: {e}")
        return JSONResponse(status_code=500, content={"error": f"LLM generation failed: {str(e)}"})


class FixNamePayload(BaseModel):
    username: str
    name: str


@app.post("/api/requests/fix-name")
async def fix_host_name(payload: FixNamePayload):
    """Исправляет имя хоста в очереди и пересоздает сообщение с исправленным именем."""
    username = (payload.username or "").strip()
    new_name = (payload.name or "").strip()
    if not username:
        return JSONResponse(status_code=400, content={"error": "username is required"})
    if not new_name:
        return JSONResponse(status_code=400, content={"error": "name is required"})

    try:
        # 1. Находим хост в очереди
        queue = queue_manager.load_queue(exclude_sent=False)
        target_item = None
        for item in queue:
            if str(item.get("username", "")).strip().lower() == username.lower():
                target_item = item
                break

        if not target_item:
            return JSONResponse(status_code=404, content={"error": f"Host '{username}' not found in queue"})

        # 2. Обновляем имя через data_cleaner
        from data_cleaner import sanitize_host_name, sanitize_host_clean_name
        target_item["name"] = sanitize_host_name(new_name)
        target_item["clean_name"] = sanitize_host_clean_name(new_name)

        # 3. Сохраняем обновленную очередь
        queue_manager.save_queue(queue)

        # 4. Пересоздаем сообщение с исправленным именем
        updated = await asyncio.to_thread(
            queue_manager.regenerate_item,
            username=username,
            check_in=target_item.get("check_in"),
            check_out=target_item.get("check_out"),
            guest_context=target_item.get("guest_context"),
        )

        if not updated:
            return JSONResponse(status_code=500, content={"error": "Failed to regenerate message with fixed name"})

        msg = updated.get("generated_message", "")
        words = updated.get("word_count", len(msg.split()))
        qc_passed = updated.get("qc_passed", True)

        return {
            "status": "ok",
            "username": username,
            "name": new_name,
            "generated_message": msg,
            "word_count": words,
            "qc_passed": qc_passed,
            "item": updated,
        }
    except Exception as e:
        import traceback
        traceback.print_exc()
        return JSONResponse(status_code=500, content={"error": f"Failed to fix name: {str(e)}"})


class FixNamesBatchPayload(BaseModel):
    usernames: List[str] = []


@app.post("/api/requests/fix-names-batch")
async def fix_names_batch(payload: FixNamesBatchPayload):
    """Перегенерирует только имена хостов для выбранных карточек и пересоздает сообщения."""
    usernames = [str(u).strip() for u in (payload.usernames or []) if str(u).strip()]
    if not usernames:
        return JSONResponse(status_code=400, content={"error": "usernames is required"})

    try:
        from message_generator import extract_greeting_name
        from data_cleaner import sanitize_host_name, sanitize_host_clean_name

        results = []
        queue = queue_manager.load_queue(exclude_sent=False)

        for username in usernames:
            target_item = None
            for item in queue:
                if str(item.get("username", "")).strip().lower() == username.lower():
                    target_item = item
                    break

            if not target_item:
                results.append({"username": username, "error": "not found in queue"})
                continue

            # Извлекаем правильное имя
            greeting_name = extract_greeting_name(target_item)
            target_item["name"] = sanitize_host_name(greeting_name)
            target_item["clean_name"] = sanitize_host_clean_name(greeting_name)

            # Пересоздаем сообщение с исправленным именем
            updated = await asyncio.to_thread(
                queue_manager.regenerate_item,
                username=username,
                check_in=target_item.get("check_in"),
                check_out=target_item.get("check_out"),
                guest_context=target_item.get("guest_context"),
            )

            if updated:
                results.append({
                    "username": username,
                    "name": greeting_name,
                    "generated_message": updated.get("generated_message", ""),
                    "word_count": updated.get("word_count", 0),
                    "qc_passed": updated.get("qc_passed", True),
                })
            else:
                results.append({"username": username, "error": "regeneration failed"})

        queue_manager.save_queue(queue)

        return {
            "status": "ok",
            "processed": len(results),
            "results": results,
        }
    except Exception as e:
        import traceback
        traceback.print_exc()
        return JSONResponse(status_code=500, content={"error": f"Failed to fix names: {str(e)}"})


batch_regen_state: Dict[str, Any] = {
    "is_running": False,
    "stop_requested": False,
    "total": 0,
    "completed": 0,
    "failed": 0,
    "current_host": "",
    "active_ids": [],
    "completed_items": {},
    "failed_ids": [],
    "updated_at": None,
}

# Строго 1 запрос к Ollama одновременно для защиты Mac от зависаний
_regen_semaphore = asyncio.Semaphore(1)


async def _process_single_regeneration(target_id: str, check_in: str, check_out: str, guest_context: str):
    async with _regen_semaphore:
        try:
            batch_regen_state["current_host"] = target_id
            batch_regen_state["updated_at"] = datetime.now().isoformat()

            updated = await asyncio.to_thread(
                queue_manager.regenerate_item,
                username=target_id,
                check_in=check_in,
                check_out=check_out,
                guest_context=guest_context,
            )
            if updated:
                msg = updated.get("generated_message", "")
                words = updated.get("word_count", len(msg.split()))
                qc_passed = updated.get("qc_passed", True)
                status = updated.get("status", "Draft")
                item_info = {
                    "host_id": target_id,
                    "username": updated.get("username", target_id),
                    "name": updated.get("name", ""),
                    "clean_name": updated.get("clean_name", ""),
                    "generated_message": msg,
                    "word_count": words,
                    "qc_passed": qc_passed,
                    "status": status,
                    "is_name_trusted": updated.get("is_name_trusted", True),
                }
                batch_regen_state["completed_items"][target_id] = item_info
                batch_regen_state["completed"] += 1
                if target_id in batch_regen_state["active_ids"]:
                    batch_regen_state["active_ids"].remove(target_id)

                comp = batch_regen_state["completed"]
                tot = batch_regen_state["total"]
                process_mgr.append_log(f"[REGEN] ✓ Сгенерировано {comp}/{tot}: @{target_id} [{status}] ({words} слов, QC: {'PASS' if qc_passed else 'REVIEW'})")
                return item_info
            else:
                batch_regen_state["failed"] += 1
                batch_regen_state["failed_ids"].append(target_id)
                if target_id in batch_regen_state["active_ids"]:
                    batch_regen_state["active_ids"].remove(target_id)
                return None
        except Exception as e:
            process_mgr.append_log(f"[!] Ошибка LLM перегенерации для {target_id}: {e}")
            batch_regen_state["failed"] += 1
            batch_regen_state["failed_ids"].append(target_id)
            if target_id in batch_regen_state["active_ids"]:
                batch_regen_state["active_ids"].remove(target_id)
            return None


async def process_regeneration_queue(request_ids: List[str], check_in: Optional[str] = None, check_out: Optional[str] = None, guest_context: Optional[str] = None):
    check_in = check_in or DEFAULT_CHECK_IN
    check_out = check_out or DEFAULT_CHECK_OUT
    guest_context = guest_context or DEFAULT_CONTEXT

    # Ограничение размера пакета: максимум 10 сообщений
    if len(request_ids) > 10:
        request_ids = request_ids[:10]

    batch_regen_state["is_running"] = True
    batch_regen_state["stop_requested"] = False
    batch_regen_state["total"] = len(request_ids)
    batch_regen_state["completed"] = 0
    batch_regen_state["failed"] = 0
    batch_regen_state["current_host"] = ""
    batch_regen_state["active_ids"] = list(request_ids)
    batch_regen_state["completed_items"] = {}
    batch_regen_state["failed_ids"] = []
    batch_regen_state["updated_at"] = datetime.now().isoformat()

    process_mgr.append_log(f"[REGEN] 🚀 Запуск контролируемой последовательной генерации пакета из {len(request_ids)} сообщений (строго 1 Ollama поток)...")

    # Последовательная генерация строго по 1 запросу к Ollama (без лавины потоков)
    for rid in request_ids:
        if batch_regen_state["stop_requested"]:
            process_mgr.append_log(f"[REGEN] 🛑 Генерация остановлена по запросу пользователя. Завершено {batch_regen_state['completed']}/{batch_regen_state['total']}.")
            break
        await _process_single_regeneration(str(rid).strip(), check_in, check_out, guest_context)

    batch_regen_state["is_running"] = False
    batch_regen_state["active_ids"] = []
    batch_regen_state["current_host"] = ""
    batch_regen_state["updated_at"] = datetime.now().isoformat()
    process_mgr.append_log(f"[REGEN] 🏁 Пакетная генерация завершена: успешно {batch_regen_state['completed']}/{batch_regen_state['total']} (ошибок: {batch_regen_state['failed']})")


@app.post("/api/queue/stop-generation")
@app.post("/api/requests/stop-generation")
async def api_queue_stop_generation():
    """Немедленная остановка активной фоновой генерации сообщений."""
    if batch_regen_state["is_running"]:
        batch_regen_state["stop_requested"] = True
        return {"status": "stopping", "message": "Остановка генерации запрошена. Текущий запрос будет завершен, новые запускаться не будут."}
    return {"status": "idle", "message": "Генерация в данный момент не выполняется."}


@app.post("/api/queue/generate-batch")
async def api_queue_generate_batch_auto(count: int = 5):
    """
    Запускает контролируемую генерацию для следующих N хостов, требующих регенерации.
    По умолчанию 5 сообщений, максимум 10.
    """
    if batch_regen_state["is_running"]:
        return JSONResponse(status_code=409, content={"error": "Генерация уже выполняется в фоне!", "is_running": True})

    limit = min(max(1, count), 10)
    
    # Находим следующие записи со статусом Needs_Regeneration
    candidates = []
    with db_session() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT username, host_id FROM core_hosts
            WHERE state IN ('selected', 'drafted')
            ORDER BY COALESCE(priority, 10) DESC, updated_at ASC
            LIMIT ?;
            """,
            (limit,)
        )
        candidates = [r[0] or r[1] for r in cur.fetchall()]

    if not candidates:
        return {"status": "empty", "message": "Нет хостов, требующих генерации."}

    asyncio.create_task(process_regeneration_queue(candidates))
    return {"status": "started", "count": len(candidates), "hosts": candidates}


@app.post("/api/queue/regenerate-batch")
@app.post("/api/requests/regenerate-batch")
async def api_queue_regenerate_batch(payload: RegenerateBatchPayload):
    """Асинхронный эндпоинт фоновой пачечной регенерации выбранных сообщений (максимум 10)."""
    raw_ids = payload.request_ids or payload.ids or payload.usernames or []
    ids = [str(x).strip() for x in raw_ids if str(x).strip()]
    if not ids:
        return JSONResponse(status_code=400, content={"error": "Field 'request_ids' is empty or missing"})

    if batch_regen_state["is_running"]:
        return JSONResponse(status_code=409, content={"error": "Генерация уже выполняется в фоне!", "is_running": True, "active_count": len(batch_regen_state["active_ids"])})

    # Лимит пачки: максимум 10 хостов
    if len(ids) > 10:
        ids = ids[:10]

    asyncio.create_task(process_regeneration_queue(
        ids,
        check_in=payload.check_in,
        check_out=payload.check_out,
        guest_context=payload.guest_context,
    ))
    return {"status": "started", "total": len(ids), "request_ids": ids}


@app.get("/api/queue/regeneration-status")
@app.get("/api/requests/regeneration-status")
@app.get("/api/requests/regenerate_status")
async def api_queue_regeneration_status():
    """Возвращает текущий прогресс и статус пачечной генерации."""
    return batch_regen_state


def format_date_range(arrival_str: str, dep_str: str) -> tuple[str, str, str, str]:
    """
    Парсит ввод дат ('16 Nov', '2026-11-16', 'Nov 16', etc.)
    и возвращает (r_year, r_no_year, iso_check_in, iso_check_out).
    Например: ('Nov 16-19, 2026', 'Nov 16-19', '2026-11-16', '2026-11-19')
    """
    month_map = {
        'jan': ('Jan', '01'), 'feb': ('Feb', '02'), 'mar': ('Mar', '03'),
        'apr': ('Apr', '04'), 'may': ('May', '05'), 'jun': ('Jun', '06'),
        'jul': ('Jul', '07'), 'aug': ('Aug', '08'), 'sep': ('Sep', '09'),
        'oct': ('Oct', '10'), 'nov': ('Nov', '11'), 'dec': ('Dec', '12'),
    }
    month_lookup = {
        1: ('Jan', '01'), 2: ('Feb', '02'), 3: ('Mar', '03'), 4: ('Apr', '04'),
        5: ('May', '05'), 6: ('Jun', '06'), 7: ('Jul', '07'), 8: ('Aug', '08'),
        9: ('Sep', '09'), 10: ('Oct', '10'), 11: ('Nov', '11'), 12: ('Dec', '12'),
    }

    def parse_part(s: str, default_day: int, default_month: str):
        if not s or not isinstance(s, str):
            return default_day, default_month, f"2026-11-{default_day:02d}"
        s = s.strip()
        # ISO format: 2026-11-16
        iso_m = re.match(r'(\d{4})-(\d{1,2})-(\d{1,2})', s)
        if iso_m:
            y, m, d = iso_m.group(1), int(iso_m.group(2)), int(iso_m.group(3))
            m_name, m_num = month_lookup.get(m, ('Nov', '11'))
            return d, m_name, f"{y}-{m_num}-{d:02d}"

        # '16 Nov', 'Nov 16', '16 ноября'
        m_match = re.search(r'([a-zA-Z]{3,}|нояб|декаб|октяб)', s, re.IGNORECASE)
        d_match = re.search(r'(\d{1,2})', s)
        day = int(d_match.group(1)) if d_match else default_day

        m_str = 'nov'
        if m_match:
            raw_m = m_match.group(1).lower()
            if 'нояб' in raw_m: m_str = 'nov'
            elif 'декаб' in raw_m: m_str = 'dec'
            elif 'октяб' in raw_m: m_str = 'oct'
            else: m_str = raw_m[:3]
        m_name, m_num = month_map.get(m_str, ('Nov', '11'))
        return day, m_name, f"2026-{m_num}-{day:02d}"

    d1, m1, iso1 = parse_part(arrival_str, 16, 'Nov')
    d2, m2, iso2 = parse_part(dep_str, 19, m1)

    if m1 == m2:
        r_year = f"{m1} {d1}-{d2}, 2026"
        r_no_year = f"{m1} {d1}-{d2}"
    else:
        r_year = f"{m1} {d1} - {m2} {d2}, 2026"
        r_no_year = f"{m1} {d1} - {m2} {d2}"

    return r_year, r_no_year, iso1, iso2


def replace_message_dates_and_city(msg: str, r_year: str, r_no_year: str, city: Optional[str] = None) -> str:
    if not msg:
        return ""

    # 1. Замена конструкций с годом: 'from November 18-20, 2026', 'Nov 18-20, 2026'
    p1 = r'(from\s+)?(?:Nov(?:ember)?|Dec(?:ember)?|Oct(?:ober)?)\s+\d+[\s–\-]+\d+,\s*202\d'
    msg = re.sub(p1, lambda m: f"{m.group(1) or ''}{r_year}", msg, flags=re.IGNORECASE)

    # 2. Замена конструкций без года: 'from Nov 18-20', 'Nov 18–20', '18-20 Nov'
    p2 = r'(from\s+)?(?:Nov(?:ember)?|Dec(?:ember)?|Oct(?:ober)?)\s+\d+[\s–\-]+\d+'
    msg = re.sub(p2, lambda m: f"{m.group(1) or ''}{r_no_year}", msg, flags=re.IGNORECASE)

    # 3. Замена города, если передан: 'visiting Tokyo' -> 'visiting Kyoto'
    if city and city.strip():
        c_clean = city.strip()
        p_city = r'(visiting|exploring|trip to|heading to|staying in)\s+(?:Tokyo|Kyoto|Sapporo|Osaka)'
        msg = re.sub(p_city, lambda m: f"{m.group(1)} {c_clean}", msg, flags=re.IGNORECASE)

    return msg


@app.post("/api/queue/batch-update-dates")
@app.post("/api/requests/batch-update-dates")
async def api_queue_batch_update_dates(payload: BatchUpdateDatesPayload):
    """
    Массовое обновление дат и города в черновиках без обращения к Ollama LLM.
    Выполняет интеллектуальную замену дат в тексте сообщений и обновляет метаданные заявок.
    """
    r_yr, r_ny, iso_in, iso_out = format_date_range(payload.new_arrival, payload.new_departure)
    city = (payload.city or "Tokyo").strip()

    raw_ids = payload.request_ids or []
    target_usernames = {str(x).strip().lower() for x in raw_ids if str(x).strip()}

    queue = queue_manager.load_queue(exclude_sent=False)
    updated_items = []

    for item in queue:
        u = (item.get("username") or item.get("id") or "").lower()
        if target_usernames and u not in target_usernames:
            continue

        old_msg = item.get("generated_message") or item.get("message") or ""
        new_msg = replace_message_dates_and_city(old_msg, r_yr, r_ny, city=city)

        words = len(new_msg.strip().split()) if new_msg.strip() else 0
        qc_passed = 60 <= words <= 95

        item["generated_message"] = new_msg
        item["message"] = new_msg
        item["check_in"] = iso_in
        item["check_out"] = iso_out
        if city:
            item["location"] = city
        item["word_count"] = words
        item["qc_passed"] = qc_passed

        # Обновляем в SQLite базе
        try:
            with db_session() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    UPDATE core_hosts
                    SET message_draft = ?, qc_passed = ?, location = COALESCE(?, location), updated_at = CURRENT_TIMESTAMP
                    WHERE (LOWER(username) = LOWER(?) OR LOWER(host_id) = LOWER(?))
                      AND state != 'sent';
                """, (new_msg, 1 if qc_passed else 0, city if city else None, item.get("username") or item.get("id"), item.get("username") or item.get("id")))
                conn.commit()
        except Exception as e:
            logger.warning(f"Error updating SQLite dates for {u}: {e}")

        updated_items.append({
            "username": item.get("username") or item.get("id"),
            "clean_name": item.get("clean_name") or item.get("name"),
            "generated_message": new_msg,
            "message": new_msg,
            "check_in": iso_in,
            "check_out": iso_out,
            "location": item.get("location"),
            "words": words,
            "qc_passed": qc_passed,
        })

    # Сохраняем обновленную очередь в output/requests_queue.json
    queue_manager.save_queue(queue)
    process_mgr.append_log(f"[DATES] 📅 Обновлены даты ({iso_in} – {iso_out}, {city}) для {len(updated_items)} карточек.")

    return {
        "success": True,
        "updated_count": len(updated_items),
        "check_in": iso_in,
        "check_out": iso_out,
        "city": city,
        "updated_items": updated_items,
    }


@app.post("/api/requests/regenerate_all")
async def regenerate_all_requests(payload: Optional[RegenerateAll] = None):
    """Массовая перегенерация запросов для всех черновиков в фоне без таймаута."""
    if batch_regen_state["is_running"]:
        return JSONResponse(status_code=409, content={"error": "Генерация уже выполняется в фоне!", "is_running": True})

    queue = queue_manager.load_queue(exclude_sent=True)
    overwrite = payload.overwrite_approved if payload else False
    if overwrite:
        target_ids = [item.get("username") or item.get("id") for item in queue]
    else:
        target_ids = [item.get("username") or item.get("id") for item in queue if (item.get("status") or "").lower() == "draft"]

    target_ids = [str(x).strip() for x in target_ids if x and str(x).strip()]
    if not target_ids:
        return {"status": "ok", "message": "Нет черновиков для перегенерации", "total": 0}

    c_in = payload.check_in if payload else DEFAULT_CHECK_IN
    c_out = payload.check_out if payload else DEFAULT_CHECK_OUT
    g_ctx = payload.guest_context if payload else DEFAULT_CONTEXT

    asyncio.create_task(process_regeneration_queue(
        target_ids,
        check_in=c_in,
        check_out=c_out,
        guest_context=g_ctx,
    ))
    return {"status": "started", "total": len(target_ids), "request_ids": target_ids}


class ApproveAllPayload(BaseModel):
    trip_id: Optional[str] = None


@app.post("/api/requests/approve_all")
async def approve_all_drafts(payload: Optional[ApproveAllPayload] = None):
    """Массовое одобрение всех черновиков для подготовки к автопилоту."""
    t_id = payload.trip_id if payload else None
    resolved_trip = resolve_trip_alias(t_id) if t_id else None
    count = queue_manager.approve_all_drafts(trip_id=resolved_trip)
    queue = queue_manager.load_queue(exclude_sent=False, trip_id=resolved_trip)
    stats = queue_manager.get_queue_stats(trip_id=resolved_trip)
    return {"status": "ok", "approved_count": count, "stats": stats, "queue": queue}



@app.get("/api/llm/status")
async def get_llm_status():
    """Возвращает информацию о локальной LLM модели и статусе Ollama."""
    from llm_agent import get_llm_agent
    agent = get_llm_agent()
    online = agent.is_available()
    models = agent.get_available_models() if online else []
    active = agent.select_best_model() if online else "none"
    return {
        "online": online,
        "active_model": active,
        "installed_models": models,
    }


# =========================================================================
# Умный диспетчер автопилота отправки запросов
# =========================================================================

@app.get("/api/autopilot/status")
async def get_autopilot_status(trip_id: Optional[str] = None):
    """Возвращает статус автопилота, обратный отсчет, суточный прогресс, списки ready_candidates и sent_history с изоляцией по активному Trip."""
    status_file = OUTPUT_DIR / "autopilot_status.json"
    sent_log_file = OUTPUT_DIR / "sent_log.json"

    now_dt = datetime.now()
    today_date = now_dt.date()
    yesterday_date = today_date - timedelta(days=1)

    # 1. Определение активного Trip ID (контекстная константа)
    active_trip_id = trip_id if (trip_id and str(trip_id).strip() and str(trip_id).strip().lower() not in ("none", "null", "undefined")) else None
    if not active_trip_id:
        try:
            saved_trip_json = get_setting("active_trip_context", "")
            if saved_trip_json:
                trip_data = json.loads(saved_trip_json)
                active_trip_id = trip_data.get("trip_id") or trip_data.get("id")
        except Exception:
            pass
    if active_trip_id:
        active_trip_id = resolve_trip_alias(str(active_trip_id).strip())

    # 2. Суточный лимит (глобальный счетчик отправлений за сегодня для безопасности CS-аккаунта)
    sent_today = 0
    if sent_log_file.exists():
        try:
            with open(sent_log_file, "r", encoding="utf-8") as f:
                logs = json.load(f)
            today_str = today_date.isoformat()
            today_logs = [
                x for x in logs
                if x.get("timestamp", "").startswith(today_str)
                and x.get("status") in ("SENT_SUCCESS", "success", "dry_run_success", "DRY_RUN_SUCCESS")
                and x.get("mode") in ("live", "dry_run")
                and "Deduplicated" not in (x.get("notes") or "")
                and "Detected as already sent" not in (x.get("notes") or "")
                and not x.get("timestamp", "").endswith("19:55:57")
            ]
            sent_today = len(today_logs)
        except Exception as e:
            logger.warning(f"Error reading sent_today from sent_log: {e}")

    # 3. Чтение статуса процесса из файла
    import subprocess
    is_os_running = False
    try:
        res = subprocess.run(["pgrep", "-f", "auto_sender.py"], capture_output=True, text=True)
        is_os_running = (res.returncode == 0 and bool(res.stdout.strip()))
    except Exception:
        pass

    is_running = (process_mgr.active_task_name == "Автопилот" and process_mgr.is_running) or is_os_running

    status_data = {
        "is_running": is_running,
        "is_paused": False,
        "state_label": "РАБОТАЕТ" if is_running else "ОСТАНОВЛЕН",
        "sent_today": sent_today,
        "daily_limit": 100,
        "next_send_time": None,
        "seconds_remaining": 0,
        "last_status": "Остановлен" if not is_running else "Работает",
        "current_host": None,
        "next_candidate": None,
        "last_sent": None,
        "dry_run": False,
        "emergency_stop": get_setting("emergency_stop", "0") == "1",
    }

    if status_file.exists():
        try:
            with open(status_file, "r", encoding="utf-8") as f:
                saved = json.load(f)
            status_data.update(saved)
            status_data["is_running"] = is_running
            is_paused = bool(saved.get("is_paused"))
            status_data["is_paused"] = is_paused
            status_data["sent_today"] = sent_today
            status_data["daily_limit"] = int(saved.get("daily_limit") or 100)

            if not is_running:
                status_data["state_label"] = "ОСТАНОВЛЕН"
                status_data["current_host"] = None
                status_data["seconds_remaining"] = 0
            elif is_paused:
                status_data["state_label"] = "ПАУЗА"
            else:
                status_data["state_label"] = "РАБОТАЕТ"

            if status_data.get("next_send_time") and is_running:
                import time
                status_data["seconds_remaining"] = max(0, int(status_data["next_send_time"] - time.time()))
            elif not is_running:
                status_data["seconds_remaining"] = 0
        except Exception:
            pass

    current_sending = status_data.get("current_host")

    # 4. Формирование SENT_HISTORY strictly с привязкой к активному Trip (DDD)
    sent_history = []
    seen_sent = set()
    try:
        with db_session() as conn:
            cur = conn.cursor()
            if active_trip_id:
                cur.execute("""
                    SELECT host_id, username, name, location, gender, score_stars, languages_json, sent_at, message_draft, is_verified
                    FROM core_hosts
                    WHERE LOWER(trip_id) = LOWER(?) AND state = 'sent'
                    ORDER BY sent_at DESC;
                """, (active_trip_id,))
            else:
                cur.execute("""
                    SELECT host_id, username, name, location, gender, score_stars, languages_json, sent_at, message_draft, is_verified
                    FROM core_hosts
                    WHERE state = 'sent'
                    ORDER BY sent_at DESC;
                """)
            for r in cur.fetchall():
                u = r["username"] or r["host_id"]
                if not u:
                    continue
                seen_sent.add(u)
                seen_sent.add(u.strip().lower())

                ts_raw = r["sent_at"] or ""
                fmt_time = "Sent"
                if ts_raw:
                    try:
                        clean_ts = ts_raw.replace("T", " ").split(".")[0]
                        entry_dt = datetime.strptime(clean_ts, "%Y-%m-%d %H:%M:%S")
                        if entry_dt.date() == today_date:
                            fmt_time = f"Today, {entry_dt.strftime('%H:%M:%S')}"
                        elif entry_dt.date() == yesterday_date:
                            fmt_time = f"Yesterday, {entry_dt.strftime('%H:%M:%S')}"
                        else:
                            fmt_time = entry_dt.strftime("%b %d, %H:%M:%S")
                    except Exception:
                        fmt_time = ts_raw.split(" ")[-1] if " " in ts_raw else ts_raw

                langs = []
                try:
                    if r["languages_json"]:
                        langs = json.loads(r["languages_json"])
                except Exception:
                    pass
                langs_str = ", ".join(langs[:2]) if langs else "Not specified"

                msg = r["message_draft"] or ""
                sent_history.append({
                    "username": u,
                    "name": r["name"] or u,
                    "location": r["location"] or "Japan",
                    "gender": (r["gender"] or "unknown").capitalize(),
                    "score_stars": r["score_stars"] or 5,
                    "languages": langs_str,
                    "timestamp": fmt_time,
                    "raw_timestamp": ts_raw,
                    "status": "WAS SENT",
                    "mode": "live",
                    "message": msg,
                    "is_verified": bool(r["is_verified"]) if ("is_verified" in r.keys() and r["is_verified"]) else False,
                    "word_count": len(msg.split()) if msg else 0,
                })
    except Exception as e:
        logger.warning(f"Error loading sent_history from core_hosts: {e}")

    # Fallback на глобальный sent_log, если по трипу записей нет (например старые отправки до миграции)
    if not sent_history and not active_trip_id and sent_log_file.exists():
        try:
            with open(sent_log_file, "r", encoding="utf-8") as f:
                logs = json.load(f)
            for entry in reversed(logs):
                st = entry.get("status", "")
                if st in ("SENT_SUCCESS", "success", "dry_run_success", "DRY_RUN_SUCCESS"):
                    u = entry.get("username") or entry.get("id")
                    if not u or u in seen_sent:
                        continue
                    seen_sent.add(u)
                    seen_sent.add(u.strip().lower())
                    msg = entry.get("message") or ""
                    sent_history.append({
                        "username": u,
                        "name": entry.get("name") or u,
                        "location": entry.get("target_city") or "Japan",
                        "gender": "Unknown",
                        "score_stars": 5,
                        "languages": "EN",
                        "timestamp": entry.get("timestamp", "").split(" ")[-1] or "Sent",
                        "raw_timestamp": entry.get("timestamp", ""),
                        "status": "WAS SENT",
                        "mode": entry.get("mode", "live"),
                        "message": msg,
                        "is_verified": bool(entry.get("is_verified", False)),
                        "word_count": len(msg.split()) if msg else 0,
                    })
        except Exception:
            pass

    # Строгая сортировка отправленных по времени (самые свежие сверху)
    sent_history.sort(key=lambda x: str(x.get("raw_timestamp") or ""), reverse=True)
    if sent_history:
        top_sent = sent_history[0]
        status_data["last_sent"] = {
            "name": top_sent.get("name") or top_sent.get("username"),
            "username": top_sent.get("username"),
            "timestamp": top_sent.get("timestamp"),
            "status": "Sent / Verified",
            "is_success": True,
        }

    # 5. Формирование READY_CANDIDATES strictly для активного Trip через канонический RequestQueueManager
    ready_candidates = []
    try:
        from request_manager import RequestQueueManager
        qm = RequestQueueManager()
        raw_candidates = qm.get_ready_dispatch_candidates(trip_id=active_trip_id)
        for item in raw_candidates:
            u = item.get("username") or item.get("id")
            if not u or u.lower() in seen_sent:
                continue
            is_this_sending = bool(current_sending and current_sending.lower() == u.lower())
            msg = item.get("generated_message") or item.get("message_text") or ""
            ready_candidates.append({
                "username": u,
                "name": item.get("clean_name") or item.get("name") or u,
                "location": item.get("location") or item.get("city") or "Japan",
                "gender": (item.get("estimated_gender") or "Unknown").capitalize(),
                "score_stars": item.get("score_stars") or 4,
                "languages": item.get("languages") or "Not specified",
                "status": "SENDING" if is_this_sending else "QUEUED",
                "message": msg,
                "is_verified": bool(item.get("is_verified", False)),
                "word_count": len(msg.split()) if msg else 0,
            })
    except Exception as e:
        logger.warning(f"Error loading ready_candidates via RequestQueueManager: {e}")

    # Ensure sending host is placed at the top if present
    if current_sending:
        sending_items = [c for c in ready_candidates if c["username"].lower() == current_sending.lower()]
        other_items = [c for c in ready_candidates if c["username"].lower() != current_sending.lower()]
        if sending_items:
            ready_candidates = sending_items + other_items

    if ready_candidates:
        status_data["next_candidate"] = ready_candidates[0]

    status_data["ready_candidates"] = ready_candidates
    status_data["sent_history"] = sent_history
    status_data["active_trip_id"] = active_trip_id

    return status_data


@app.on_event("startup")
async def app_startup_cleanup():
    """Сброс зависших статусов sending и незавершенных фоновых задач при старте сервера."""
    try:
        from database import reset_stuck_sending_statuses, reset_stuck_background_jobs
        reset_stuck_sending_statuses()
        reset_stuck_background_jobs()
        if QUEUE_FILE.exists():
            with open(QUEUE_FILE, "r", encoding="utf-8") as f:
                q_data = json.load(f)
            q_changed = False
            for itm in q_data:
                if (itm.get("status") or "").lower() == "sending":
                    itm["status"] = "Verification_Required"
                    q_changed = True
            if q_changed:
                with open(QUEUE_FILE, "w", encoding="utf-8") as f:
                    json.dump(q_data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.warning(f"Ошибка при очистке статусов sending на старте: {e}")


async def _cancel_dispatcher_task() -> None:
    """Отменяет незавершённый таск запуска диспетчера (если он есть)."""
    global dispatcher_task
    task = dispatcher_task
    if task is not None and not task.done():
        logger.warning("Отмена предыдущего незавершённого dispatcher_task — запускаем только один цикл.")
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning(f"Ошибка при отмене старого dispatcher_task: {e}")
    dispatcher_task = None


async def _start_dispatcher_job(cmd: List[str], params: Dict[str, Any]) -> Dict[str, Any]:
    """Единственная точка запуска автопилота: сначала гарантированно убирает старый таск."""
    global dispatcher_task
    await _cancel_dispatcher_task()
    dispatcher_task = asyncio.create_task(process_mgr.start_job("Автопилот", cmd, params))
    try:
        return await dispatcher_task
    finally:
        if dispatcher_task is not None and dispatcher_task.done():
            dispatcher_task = None


@app.post("/api/autopilot/start")
@app.post("/api/dispatcher/start")
async def start_autopilot(params: AutopilotParams):
    """Запуск фонового автопилота отправки stay-requests."""
    if get_setting("emergency_stop", "0") == "1":
        return {"error": "Активирован Emergency Stop! Сначала снимите блокировку в интерфейсе."}
    if process_mgr.is_running:
        return {"error": f"Уже выполняется процесс: {process_mgr.active_task_name}"}

    # Сброс зависших записей 'sending' перед запуском
    try:
        from database import reset_stuck_sending_statuses
        reset_stuck_sending_statuses()
        if QUEUE_FILE.exists():
            with open(QUEUE_FILE, "r", encoding="utf-8") as f:
                q_data = json.load(f)
            q_changed = False
            for itm in q_data:
                if (itm.get("status") or "").lower() == "sending":
                    itm["status"] = "Approved"
                    q_changed = True
            if q_changed:
                with open(QUEUE_FILE, "w", encoding="utf-8") as f:
                    json.dump(q_data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.warning(f"Ошибка сброса зависших статусов перед запуском: {e}")

    cmd = [
        sys.executable,
        "-u",
        str(BASE_DIR / "auto_sender.py"),
        "--daily-limit", str(params.daily_limit),
        "--min-interval-minutes", str(params.min_interval_minutes),
        "--max-interval-minutes", str(params.max_interval_minutes),
    ]
    if params.jitter_enabled:
        cmd.append("--jitter-enabled")
    else:
        cmd.append("--no-jitter")

    # Исключаем устаревший period_minutes во избежание генерации коротких 35-сек интервалов
    # Паузы всегда строго определяются min_interval_minutes и max_interval_minutes
    if params.interval_seconds is not None:
        cmd.extend(["--interval-seconds", str(params.interval_seconds)])
    else:
        cmd.extend([
            "--delay-min", str(params.min_interval_minutes),
            "--delay-max", str(params.max_interval_minutes),
        ])

    if params.max_requests is not None:
        cmd.extend(["--max-requests", str(params.max_requests)])
    if params.headed:
        cmd.append("--headed")
    if params.dry_run:
        cmd.append("--dry-run")
    if params.test_mode:
        cmd.append("--test-mode")
    if params.target:
        cmd.extend(["--target", params.target])

    active_trip_id = params.trip_id
    if not active_trip_id:
        try:
            saved_trip_json = get_setting("active_trip_context", "")
            if saved_trip_json:
                trip_data = json.loads(saved_trip_json)
                active_trip_id = trip_data.get("trip_id") or trip_data.get("id")
        except Exception:
            pass
    resolved_active_trip = None
    if active_trip_id:
        resolved_active_trip = resolve_trip_alias(str(active_trip_id).strip())
        cmd.extend(["--trip-id", str(resolved_active_trip)])

    # Проверка наличия одобренных кандидатов перед запуском процесса
    try:
        from request_manager import RequestQueueManager
        qm = RequestQueueManager()
        ready_cands = qm.get_ready_dispatch_candidates(trip_id=resolved_active_trip)
        if params.target:
            t_norm = params.target.strip().lower()
            ready_cands = [c for c in ready_cands if str(c.get("username", "")).strip().lower() == t_norm]
        if not ready_cands:
            return {
                "error": "В очереди нет одобренных хостов к отправке. Имеющиеся черновики содержат устаревшие шаблоны и требуют регенерации по новым правилам QC."
            }
    except Exception as e:
        logger.warning(f"Ошибка предварительной проверки очереди: {e}")

    # Инициализируем статус активного запуска перед стартом подпроцесса
    status_file = OUTPUT_DIR / "autopilot_status.json"
    status_data = {}
    if status_file.exists():
        try:
            with open(status_file, "r", encoding="utf-8") as f:
                status_data = json.load(f)
        except Exception:
            status_data = {}
    status_data["is_running"] = True
    status_data["is_paused"] = False
    status_data["current_host"] = None
    status_data["last_status"] = "Запуск автопилота..."
    status_data["updated_at"] = datetime.now().isoformat()
    try:
        with open(status_file, "w", encoding="utf-8") as f:
            json.dump(status_data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"Ошибка сохранения autopilot_status: {e}")

    res = await _start_dispatcher_job(cmd, params.model_dump())
    if not res.get("success"):
        return {"error": res.get("error")}
    return {"status": "started", "dry_run": params.dry_run, "job_id": res.get("job_id")}


@app.post("/api/autopilot/pause")
async def pause_autopilot():
    """Приостановка автопилота без сброса подпроцесса."""
    status_file = OUTPUT_DIR / "autopilot_status.json"
    data = {}
    if status_file.exists():
        try:
            with open(status_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
    data["is_paused"] = True
    data["last_status"] = "Приостановлен (Пауза)"
    with open(status_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    process_mgr.append_log("⏸ Автопилот приостановлен пользователем.")
    return {"status": "paused", "is_paused": True}


@app.post("/api/autopilot/resume")
async def resume_autopilot():
    """Возобновление автопилота с паузы."""
    status_file = OUTPUT_DIR / "autopilot_status.json"
    data = {}
    if status_file.exists():
        try:
            with open(status_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
    data["is_paused"] = False
    data["last_status"] = "Возобновлен"
    with open(status_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    process_mgr.append_log("▶️ Автопилот возобновил работу.")
    return {"status": "resumed", "is_paused": False}


@app.post("/api/autopilot/stop")
@app.post("/api/dispatcher/stop")
async def stop_autopilot():
    """Остановка автопилота."""
    await _cancel_dispatcher_task()
    res = await process_mgr.stop_current_job()

    # Принудительная остановка любых фоновых процессов auto_sender в ОС
    try:
        import subprocess
        subprocess.run(["pkill", "-TERM", "-f", "auto_sender.py"], capture_output=True)
    except Exception:
        pass

    status_file = OUTPUT_DIR / "autopilot_status.json"
    if status_file.exists():
        try:
            with open(status_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            data["is_running"] = False
            data["is_paused"] = False
            data["current_host"] = None
            data["next_send_time"] = None
            data["seconds_remaining"] = 0
            data["last_status"] = "Остановлен пользователем"
            with open(status_file, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception:
            pass

    # Очистка зависших статусов 'sending'
    try:
        from database import reset_stuck_sending_statuses
        reset_stuck_sending_statuses()
        if QUEUE_FILE.exists():
            with open(QUEUE_FILE, "r", encoding="utf-8") as f:
                q_data = json.load(f)
            q_changed = False
            for itm in q_data:
                if (itm.get("status") or "").lower() == "sending":
                    itm["status"] = "Approved"
                    q_changed = True
            if q_changed:
                with open(QUEUE_FILE, "w", encoding="utf-8") as f:
                    json.dump(q_data, f, indent=2, ensure_ascii=False)
    except Exception:
        pass

    return {"status": "stopped", "details": res}


@app.post("/api/dispatcher/sync-queue")
@app.post("/api/autopilot/sync-queue")
async def api_sync_dispatcher_queue(trip_id: Optional[str] = None):
    """Синхронизация и очистка очереди автопилота от уже отправленных хостов."""
    rm = RequestQueueManager()
    removed = rm.deduplicate_queue_against_sent()

    # Синхронизация core_hosts со всеми подтвержденными отправками
    try:
        from core.database import db_session, get_sent_usernames
        sent_set = get_sent_usernames()
        if sent_set:
            now_iso = datetime.now().isoformat()
            with db_session() as conn:
                cur = conn.cursor()
                for u in sent_set:
                    cur.execute(
                        """
                        UPDATE core_hosts
                        SET state = 'sent', sent_at = COALESCE(sent_at, ?), updated_at = ?
                        WHERE (LOWER(username) = LOWER(?) OR LOWER(host_id) = LOWER(?))
                          AND state != 'sent';
                        """,
                        (now_iso, now_iso, u, u),
                    )
    except Exception as e:
        logger.warning(f"Ошибка синхронизации core_hosts при sync-queue: {e}")

    return {
        "ok": True,
        "status": "success",
        "removed_count": removed,
        "message": f"Очищено {removed} отправленных хостов из очереди"
    }


# =========================================================================
# Gender Enrichment (Background & Test)
# =========================================================================

@app.get("/api/enrichment/gender/status")
async def get_gender_enrichment_status(trip_id: Optional[str] = None):
    """Возвращает текущий статус фонового обогащения пола хостов."""
    from gender_enricher import enrichment_worker
    return enrichment_worker.get_progress(trip_id=trip_id)


@app.post("/api/enrichment/gender/test")
async def run_gender_enrichment_test(sample_size: int = 40, trip_id: Optional[str] = None):
    """Запускает разовый тест обогащения на выборке 30-50 хостов со статусом Unknown."""
    from gender_enricher import run_test_sample
    stats = run_test_sample(sample_size=sample_size, trip_id=trip_id)
    return {"ok": True, "stats": stats}


@app.post("/api/enrichment/gender/start")
async def start_gender_enrichment(trip_id: Optional[str] = None, delay_seconds: float = 1.3):
    """Запускает фоновый воркер обогащения пола для оставшихся Unknown хостов."""
    from gender_enricher import enrichment_worker
    started = enrichment_worker.start(trip_id=trip_id, delay_seconds=delay_seconds)
    if not started:
        return {"ok": False, "message": "Фоновый процесс уже запущен"}
    return {"ok": True, "message": "Фоновое обогащение успешно запущено"}


@app.post("/api/enrichment/gender/stop")
async def stop_gender_enrichment():
    """Останавливает фоновый воркер обогащения пола."""
    from gender_enricher import enrichment_worker
    stopped = enrichment_worker.stop()
    return {"ok": True, "stopped": stopped, "message": "Фоновое обогащение остановлено"}



# =========================================================================
# Потоковые логи и экспорт (SSE & Files)
# =========================================================================

@app.get("/api/logs/stream")
async def stream_logs():
    """Потоковая передача свежих записей журнала логов (SSE)."""
    async def event_generator():
        last_index = max(0, len(process_mgr.logs_buffer) - 60)
        while True:
            current_logs = process_mgr.logs_buffer
            if last_index < len(current_logs):
                new_entries = current_logs[last_index:]
                last_index = len(current_logs)
                for entry in new_entries:
                    yield f"data: {json.dumps({'log': entry})}\n\n"
            else:
                yield ": keep-alive\n\n"
            await asyncio.sleep(0.6)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"}
    )


@app.get("/api/logs/export")
async def export_logs(format: str = "txt"):
    """Экспорт полного журнала логов в формате .txt или .json."""
    logs = process_mgr.logs_buffer
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    if format.lower() == "json":
        content = json.dumps(logs, ensure_ascii=False, indent=2)
        return Response(
            content=content,
            media_type="application/json",
            headers={"Content-Disposition": f"attachment; filename=logs_{ts}.json"}
        )
    else:
        content = "\n".join(logs)
        return PlainTextResponse(
            content=content,
            headers={"Content-Disposition": f"attachment; filename=logs_{ts}.txt"}
        )


@app.post("/api/logs/clear")
async def clear_logs():
    """Очистка текущего буфера логов."""
    process_mgr.logs_buffer.clear()
    process_mgr.append_log("🧹 Журнал логов очищен пользователем.")
    return {"status": "cleared"}


@app.get("/api/debug/screenshot/{filename}")
async def get_debug_screenshot(filename: str):
    """Возвращает файл скриншота отладки из output/debug/."""
    file_path = OUTPUT_DIR / "debug" / filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Debug screenshot not found")
    return FileResponse(file_path)


@app.get("/api/requests/export")
async def export_requests_queue():
    """Скачивание файла очереди requests_queue.json."""
    if not QUEUE_FILE.exists():
        queue_manager.initialize_queue()
    return FileResponse(
        path=QUEUE_FILE,
        filename="requests_queue.json",
        media_type="application/json",
    )


# ----------------------------------------------------
# INBOX AI RESPONDER ENDPOINTS
# ----------------------------------------------------
class InboxRegeneratePayload(BaseModel):
    conversation_id: Optional[int] = None
    conv_id: Optional[int] = None
    tone: str = "casual"
    custom_instruction: Optional[str] = None


class InboxSendPayload(BaseModel):
    conv_id: int
    reply_text: str


class InboxUpdatePayload(BaseModel):
    conv_id: int
    status: Optional[str] = None
    suggested_reply: Optional[str] = None
    tone_used: Optional[str] = None


@app.get("/api/inbox/conversations")
async def api_get_inbox_conversations(status: Optional[str] = None):
    """Возвращает список всех входящих диалогов."""
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
        return {"conversations": [], "total_count": 0, "new_count": 0, "answered_count": 0, "error": str(e)}


@app.get("/api/inbox/status")
async def api_get_inbox_status():
    """Возвращает статус фонового сканирования входящих."""
    from database import get_inbox_conversations
    status_file = OUTPUT_DIR / "inbox_status.json"
    data = {
        "is_checking": False,
        "last_check_time": None,
        "new_replies_count": 0,
        "total_inbox_count": 0,
        "last_action": "Готов к работе",
    }
    if status_file.exists():
        try:
            with open(status_file, "r", encoding="utf-8") as f:
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
async def api_inbox_reply_regenerate(payload: InboxRegeneratePayload):
    """Генерация нового черновика ответа через Ollama."""
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
        return {"success": False, "error": str(e)}


@app.post("/api/inbox/reply/send")
async def api_inbox_reply_send(payload: InboxSendPayload):
    """Отправка ответа хосту через Playwright."""
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
async def api_inbox_update(payload: InboxUpdatePayload):
    """Обновление статуса диалога или черновика."""
    from database import update_inbox_conversation
    ok = update_inbox_conversation(
        conv_id=payload.conv_id,
        status=payload.status,
        suggested_reply=payload.suggested_reply,
        tone_used=payload.tone_used,
    )
    return {"success": ok}


@app.post("/api/inbox/check")
async def api_inbox_check():
    """Запуск внеочередной проверки входящих сообщений."""
    import threading
    from inbox_checker import InboxChecker

    def run_check():
        checker = InboxChecker(headless=True)
        checker.scan_inbox()

    t = threading.Thread(target=run_check, daemon=True)
    t.start()
    return {"status": "started", "message": "Фоновое сканирование входящих сообщений запущено"}


if __name__ == "__main__":
    print("[*] Запуск Couchsurfing Outreach Platform на http://0.0.0.0:8000")
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)
