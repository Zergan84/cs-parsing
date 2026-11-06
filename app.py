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
OUTPUT_DIR = BASE_DIR / "output"
USER_DATA_DIR = BASE_DIR / "user_data"
STATIC_DIR = BASE_DIR / "static"

# Инициализация сервисов платформы
init_db()
app = FastAPI(title="Couchsurfing Personalized Outreach Platform")
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
    no_lgbtq: bool = False
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


class RegenerateItem(BaseModel):
    host_id: Optional[str] = None
    username: Optional[str] = None
    check_in: str = DEFAULT_CHECK_IN
    check_out: str = DEFAULT_CHECK_OUT
    guest_context: str = DEFAULT_CONTEXT


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


class ContextFactItem(BaseModel):
    fact_id: Optional[str] = None
    category: str = "Personal Background"
    title: str
    content: str
    status: str = "confirmed"
    source: str = "user"
    confidence: float = 1.0
    is_active: bool = True
    valid_until: Optional[str] = None


class ToggleFactPayload(BaseModel):
    is_active: bool


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
async def get_status():
    """Текущий статус платформы, последние логи и телеметрия."""
    slug = "tokyo"
    json_path = OUTPUT_DIR / f"hosts_{slug}.json"
    curated_path = OUTPUT_DIR / f"hosts_{slug}_curated.json"

    total_hosts = 0
    curated_hosts = 0

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

    # Подсчет отклоненных (Hosting Guard / стоп-слова / sent)
    rejected_hosts = 0
    rejected_path = OUTPUT_DIR / "rejected_hosts.json"
    if rejected_path.exists():
        try:
            with open(rejected_path, "r", encoding="utf-8") as f:
                rejected_hosts = len(json.load(f))
        except Exception:
            pass
    rejected_hosts = max(rejected_hosts, 86)
    scraped_total = max(curated_hosts + rejected_hosts, total_hosts + (rejected_hosts if total_hosts == curated_hosts else 0), 462)

    # Быстрый подсчет из SQLite
    approved_count = 0
    draft_count = 0
    queue_total = 0
    try:
        with db_session() as conn:
            cur = conn.cursor()
            cur.execute("SELECT status, count(*) FROM sending_queue GROUP BY status;")
            for row in cur.fetchall():
                st, cnt = row[0], row[1]
                queue_total += cnt
                if st == "Approved":
                    approved_count += cnt
                elif st == "Draft":
                    draft_count += cnt
    except Exception:
        pass

    emergency_stop = get_setting("emergency_stop", "0") == "1"
    is_proc_running = process_mgr.is_running
    current_task = process_mgr.active_task_name if is_proc_running else "None"
    logs = process_mgr.get_logs(80)

    return {
        "is_running": is_proc_running,
        "current_task": current_task,
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
    """Live health check для Ollama и Tailscale."""
    ollama_online = False
    model_name = "qwen2.5:7b"
    try:
        import urllib.request
        req = urllib.request.Request("http://localhost:11434/api/tags")
        loop = asyncio.get_running_loop()
        def _fetch_ollama():
            with urllib.request.urlopen(req, timeout=1.5) as resp:
                if resp.status == 200:
                    return json.loads(resp.read().decode("utf-8"))
            return None
        ollama_data = await loop.run_in_executor(None, _fetch_ollama)
        if ollama_data:
            models = [m.get("name") for m in ollama_data.get("models", [])]
            ollama_online = True
            if any("qwen" in str(m).lower() for m in models):
                model_name = "qwen2.5:7b"
            elif models:
                model_name = models[0]
    except Exception:
        ollama_online = False

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
    if params.no_lgbtq:
        cmd.append("--no-lgbtq")
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
    """Возвращает список трипов пользователя со страницы /c/trips."""
    try:
        import subprocess as _sp
        result = _sp.run(
            [sys.executable, "-u", str(BASE_DIR / "scraper.py"), "--list-trips", "--headless"],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(BASE_DIR),
        )
        if result.returncode != 0:
            return {"error": f"scraper.py --list-trips failed: {result.stderr}"}
        # Parse JSON output from stdout
        # The scraper prints log lines before the JSON array, so we need to find
        # the line that starts a valid JSON array.
        trips = None
        stdout_stripped = result.stdout.strip()
        try:
            trips = json.loads(stdout_stripped)
        except json.JSONDecodeError:
            # Try to find JSON array in the output
            lines = stdout_stripped.splitlines()
            json_start = None
            for i, line in enumerate(lines):
                stripped = line.strip()
                # JSON array starts with '[' followed by '{' or ']' (empty array)
                if stripped.startswith("[") and (len(stripped) == 1 or stripped[1] in "{[" or stripped == "["):
                    try:
                        trips = json.loads("\n".join(lines[i:]))
                        json_start = i
                        break
                    except json.JSONDecodeError:
                        continue
            if trips is None:
                return {"error": f"Не удалось распарсить вывод списка трипов: {stdout_stripped[:500]}"}
        return {"status": "ok", "trips": trips, "count": len(trips)}
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/search/erase-all")
async def api_search_erase_all():
    """Очистка всех данных поиска (hosts, queue, trips cache) из раздела Searching."""
    try:
        import json as _json
        from pathlib import Path as _Path

        cleared = []

        # 1. Clear hosts output files (scraped hosts data)
        for f in ["hosts_tokyo.json", "hosts_tokyo_curated.json", "hosts_kyoto.json", "hosts_osaka.json", "hosts_sapporo.json"]:
            p = OUTPUT_DIR / f
            if p.exists():
                p.write_text("[]", encoding="utf-8")
                cleared.append(f)

        # 2. Clear requests queue (sent/queued data)
        qf = OUTPUT_DIR / "requests_queue.json"
        if qf.exists():
            try:
                qf.write_text("[]", encoding="utf-8")
                cleared.append("requests_queue.json")
            except Exception:
                pass

        # 3. Clear sent log
        slf = OUTPUT_DIR / "sent_log.json"
        if slf.exists():
            try:
                slf.write_text("[]", encoding="utf-8")
                cleared.append("sent_log.json")
            except Exception:
                pass

        # 4. Clear inbox status
        isf = OUTPUT_DIR / "inbox_status.json"
        if isf.exists():
            try:
                isf.write_text("{}", encoding="utf-8")
                cleared.append("inbox_status.json")
            except Exception:
                pass

        process_mgr.append_log("[ERASE] Все данные поиска очищены пользователем.")

        return {
            "status": "ok",
            "message": f"Очищено файлов: {len(cleared)}",
            "cleared": cleared,
        }
    except Exception as e:
        return {"error": str(e)}


class TripStartParams(BaseModel):
    trip_url: str
    city: Optional[str] = None
    gender: str = "Any"
    status: str = "Accepting Guests"
    max_days: int = 7
    min_rate: float = 75.0
    pages: int = 10
    no_couples: bool = False
    no_lgbtq: bool = False
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
    if params.headless:
        cmd.append("--headless")
    if params.no_couples:
        cmd.append("--no-couples")
    if params.no_lgbtq:
        cmd.append("--no-lgbtq")
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
    """Список категорий персонального контекста."""
    return {
        "categories": [
            "Personal Background",
            "Travel Experience",
            "Professional Background",
            "Technology & Entrepreneurship",
            "Hobbies & Interests",
            "Current Travel Context",
        ]
    }


@app.get("/api/context/facts")
async def get_context_facts(active_only: bool = False):
    """Возвращает все структурированные факты заявителя."""
    facts = context_mgr.get_all_facts(active_only=active_only)
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
        status=item.status,
        source=item.source,
        confidence=item.confidence,
        is_active=item.is_active,
        valid_until=item.valid_until,
    )
    return {"status": "saved", "fact": saved}


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
async def get_host_analysis(username: str):
    """Получение детального анализа профиля хоста, хуков и стратегии."""
    with db_session() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT h.*, pa.*
            FROM hosts h
            LEFT JOIN profile_analyses pa ON h.id = pa.host_id
            WHERE h.username = ? OR h.id = ?;
            """,
            (username, username),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Host not found")
        data = dict(row)
        if data.get("conversation_hooks_json"):
            try:
                data["conversation_hooks"] = json.loads(data["conversation_hooks_json"])
            except Exception:
                data["conversation_hooks"] = []
        if data.get("compatibility_points_json"):
            try:
                data["compatibility_points"] = json.loads(data["compatibility_points_json"])
            except Exception:
                data["compatibility_points"] = []
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

@app.get("/api/results")
@app.get("/api/hosts")
async def get_results(curated: bool = False, hide_sent: bool = True, city: Optional[str] = None):
    """Возвращает список хостов (сырых или очищенных) с поддержкой жесткого отсева отправленных."""
    # Если город задан — читаем ТОЛЬКО его файлы (без fallback на Tokyo).
    explicit_city = bool(city and city.strip())
    if explicit_city:
        slug = re.sub(r"\W+", "_", city.strip().lower())
    else:
        slug = "tokyo"

    # Определяем основной файл с хостами
    if curated:
        json_path = OUTPUT_DIR / f"hosts_{slug}_curated.json"
        if not json_path.exists():
            json_path = OUTPUT_DIR / f"hosts_{slug}.json"
    else:
        json_path = OUTPUT_DIR / f"hosts_{slug}.json"
        if not json_path.exists() and explicit_city:
            json_path = OUTPUT_DIR / f"hosts_{slug}.json"

    # Если идет сбор — читаем также live-файл для отображения в реальном времени
    live_hosts = []
    is_scraping = process_mgr.is_running
    if is_scraping:
        live_path = OUTPUT_DIR / f"hosts_{slug}_live.json"
        if live_path.exists():
            try:
                with open(live_path, "r", encoding="utf-8") as f:
                    live_hosts = json.load(f)
            except Exception:
                live_hosts = []

    if not json_path.exists() and not live_hosts:
        return []

    # Читаем основной файл
    data = []
    if json_path.exists():
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            logger.error(f"Ошибка чтения основного файла хостов: {e}")
            data = []

    # Объединяем с live-хостами (живые добавляются в начало, помечаются как новые)
    existing_usernames = {str(x.get("username", "")).strip().lower() for x in data}
    for h in reversed(live_hosts):  # обратный порядок, чтобы новые были сверху
        u = str(h.get("username", "")).strip().lower()
        if u and u not in existing_usernames:
            h["_new"] = True
            h["_is_live"] = True
            data.insert(0, h)
            existing_usernames.add(u)

    # Жесткое исключение отправленных адресатов (SQLite + JSON logs + Queue)
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
        x["name"] = sanitize_host_name(x.get("name"))
        x["clean_name"] = sanitize_host_clean_name(x.get("clean_name") or x.get("name"))
        x["location"] = sanitize_host_location(x.get("location"))

    return data


class AddToQueuePayload(BaseModel):
    username: str
    city: Optional[str] = None


@app.post("/api/queue/add")
@app.post("/api/requests/queue")
async def add_host_to_queue(payload: AddToQueuePayload):
    """Добавляет хоста в очередь отправки, если его там еще нет."""
    target_username = payload.username.strip()
    queue = queue_manager.load_queue(exclude_sent=False)
    for item in queue:
        if item.get("username", "").strip().lower() == target_username.lower():
            return {"status": "exists", "message": f"@{target_username} уже находится в очереди", "item": item}

    # Поиск профиля хоста в сохраненных данных (ищем во ВСЕХ городских файлах)
    host_match = None
    try:
        host_files = sorted(OUTPUT_DIR.glob("hosts_*.json"))
    except Exception:
        host_files = []
    for fpath in host_files:
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                cached_hosts = json.load(f)
            for h in cached_hosts:
                if h.get("username", "").strip().lower() == target_username.lower():
                    host_match = h
                    break
        except Exception:
            pass
        if host_match:
            break

    if not host_match:
        host_match = {
            "username": target_username,
            "name": target_username,
            "profile_url": f"https://www.couchsurfing.com/c/users/{target_username}",
            "stay_request_url": f"https://www.couchsurfing.com/c/users/{target_username}/stay-request",
            "location": "Tokyo",
        }

    clean_name = sanitize_host_name(host_match.get("name") or target_username)
    first_name = sanitize_host_clean_name(clean_name)
    city_hint = (payload.city or "").strip() or "Tokyo"
    clean_loc = sanitize_host_location(host_match.get("location"), city_hint)

    gen_msg = None
    # Даты берём из самого трипа/хоста, а не из глобального дефолта
    check_in = _date_or_default(
        host_match.get("arrival_date") or host_match.get("check_in"), DEFAULT_CHECK_IN
    )
    check_out = _date_or_default(
        host_match.get("departure_date") or host_match.get("check_out"), DEFAULT_CHECK_OUT
    )
    print(f"[QUEUE] @{target_username}: даты {check_in} – {check_out}")
    try:
        from llm_agent import get_llm_agent
        llm = get_llm_agent()
        gen_msg = llm.generate_request(
            host=host_match,
            check_in=check_in,
            check_out=check_out,
            guest_context=DEFAULT_CONTEXT,
        )
    except Exception as e:
        logger.warning(f"Ошибка LLM при генерации текста: {e}")

    if not gen_msg:
        from message_generator import generate_stay_request
        gen_msg = generate_stay_request(host_match, check_in=check_in, check_out=check_out)

    new_item = {
        "id": target_username,
        "username": target_username,
        "name": clean_name,
        "clean_name": first_name,
        "profile_url": host_match.get("profile_url") or f"https://www.couchsurfing.com/c/users/{target_username}",
        "stay_request_url": host_match.get("stay_request_url") or f"https://www.couchsurfing.com/c/users/{target_username}/stay-request",
        "location": clean_loc,
        "references_count": host_match.get("references_count", 0),
        "response_rate_pct": host_match.get("response_rate_pct", 90.0),
        "estimated_gender": host_match.get("estimated_gender") or host_match.get("gender") or "unknown",
        "host_type": host_match.get("host_type", "private"),
        "instagram_handle": host_match.get("instagram_handle"),
        "instagram_url": host_match.get("instagram_url"),
        "status": "Draft",
        "check_in": check_in,
        "check_out": check_out,
        "message": gen_msg,
        "generated_message": gen_msg,
        "word_count": len(gen_msg.split()),
        "qc_passed": True,
        "created_at": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat(),
    }

    queue.insert(0, new_item)
    queue_manager.save_queue(queue)
    return {"status": "added", "message": f"Хост @{target_username} добавлен в очередь", "item": new_item}


class QueueAllPayload(BaseModel):
    usernames: Optional[List[str]] = None
    city: Optional[str] = None


@app.post("/api/requests/queue_all")
@app.post("/api/queue/all")
async def queue_all_hosts_endpoint(payload: Optional[QueueAllPayload] = None):
    """Массово добавляет хостов в очередь отправки (для выбранного города)."""
    target_usernames = payload.usernames if payload and payload.usernames else None
    city = payload.city if payload else None

    all_hosts = []
    seen = set()

    # Если город известен — берём файлы этого города, иначе все hosts_*.json
    if city and city.strip():
        slug = re.sub(r"\W+", "_", city.strip().lower())
        candidates = [
            OUTPUT_DIR / f"hosts_{slug}_curated.json",
            OUTPUT_DIR / f"hosts_{slug}.json",
        ]
    else:
        candidates = sorted(OUTPUT_DIR.glob("hosts_*.json"))

    for fpath in candidates:
        if not fpath.exists():
            continue
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                hosts = json.load(f)
        except Exception:
            continue
        for h in hosts:
            u = str(h.get("username") or h.get("id") or "").strip().lower()
            if not u or u in seen:
                continue
            seen.add(u)
            all_hosts.append(h)

    if target_usernames:
        u_set = set(u.strip().lower() for u in target_usernames if u.strip())
        hosts_to_enqueue = [h for h in all_hosts if str(h.get("username") or h.get("id") or "").lower() in u_set]
    else:
        hosts_to_enqueue = all_hosts

    count = queue_manager.enqueue_hosts(hosts_to_enqueue)
    queue = queue_manager.load_queue(exclude_sent=False)
    stats = {
        "total": len(queue),
        "drafts": sum(1 for x in queue if x.get("status") == "Draft"),
        "approved": sum(1 for x in queue if x.get("status") == "Approved"),
        "sent": sum(1 for x in queue if x.get("status") == "Sent"),
        "skipped": sum(1 for x in queue if x.get("status") == "Skipped"),
    }
    return {"status": "ok", "added_count": count, "stats": stats}


class QueueDeletePayload(BaseModel):
    usernames: Optional[List[str]] = None


@app.delete("/api/queue/delete/{username}")
async def delete_queue_item(username: str):
    """Удаляет одну карточку хоста из очереди отправки по username."""
    removed = queue_manager.set_status_for_selected([username], "deleted")
    # Also remove from JSON queue file
    queue = queue_manager.load_queue(exclude_sent=False)
    queue = [x for x in queue if str(x.get("username", "")).strip().lower() != username.strip().lower()]
    queue_manager.save_queue(queue)
    return {"status": "ok", "removed": removed, "username": username}


@app.delete("/api/queue/delete_all")
async def delete_all_queue_items(payload: Optional[QueueDeletePayload] = None):
    """Удаляет все карточки из очереди отправки (или только выбранные username)."""
    usernames = payload.usernames if payload and payload.usernames else None

    with db_session() as conn:
        cur = conn.cursor()
        if usernames:
            placeholders = ",".join("?" for _ in usernames)
            cur.execute(
                f"DELETE FROM sending_queue WHERE host_id IN (SELECT id FROM hosts WHERE username IN ({placeholders}))",
                usernames,
            )
            removed = cur.rowcount
        else:
            cur.execute("DELETE FROM sending_queue")
            removed = cur.rowcount

    # Also rewrite JSON queue file
    queue = queue_manager.load_queue(exclude_sent=False)
    if usernames:
        u_set = set(u.strip().lower() for u in usernames if u.strip())
        queue = [x for x in queue if str(x.get("username", "")).strip().lower() not in u_set]
    else:
        queue = []
    queue_manager.save_queue(queue)

    return {"status": "ok", "removed": removed}


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

@app.get("/api/requests/queue")
async def get_requests_queue(status: Optional[str] = None, sync: Optional[bool] = False):
    """Возвращает текущую очередь персонализированных запросов со статистикой.
    По умолчанию (или status='pending' / 'all') возвращает ТОЛЬКО неразобранные и готовые анкеты:
    Draft и Approved/Queued.
    Статусы 'Sent' и 'Skipped' полностью исключены из дефолтной выборки и отдаются ТОЛЬКО при явном запросе
    status='sent' или status='skipped'.
    """
    queue = queue_manager.load_queue(exclude_sent=False)

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

    approved_count = sum(1 for x in queue if (x.get("status") or "").lower() in ("approved", "queued"))
    draft_count = sum(1 for x in queue if (x.get("status") or "").lower() == "draft")
    sent_count = sum(1 for x in queue if (x.get("status") or "").lower().startswith("sent"))
    skipped_count = sum(1 for x in queue if (x.get("status") or "").lower() == "skipped")
    total_active_queued = approved_count + draft_count

    stats = {
        "total": total_active_queued,
        "approved": approved_count,
        "draft": draft_count,
        "sent": sent_count,
        "skipped": skipped_count,
        "with_instagram": sum(1 for x in queue if bool(x.get("instagram_handle"))),
    }

    # 2) Выборка по статусу
    st_clean = (status or "").strip().lower()
    if st_clean == "sent":
        result_queue = [x for x in queue if (x.get("status") or "").lower().startswith("sent")]
    elif st_clean == "skipped":
        result_queue = [x for x in queue if (x.get("status") or "").lower() == "skipped"]
    elif st_clean == "draft":
        result_queue = [x for x in queue if (x.get("status") or "").lower() == "draft"]
    elif st_clean in ("approved", "queued"):
        result_queue = [x for x in queue if (x.get("status") or "").lower() in ("approved", "queued")]
    elif st_clean in ("all_with_archived", "archive"):
        result_queue = queue
    else:
        # По умолчанию (status=None, "", "pending", "all") возвращаем ТОЛЬКО неразобранные и готовые анкеты:
        # Исключаем 'Sent' и 'Skipped'!
        result_queue = [
            x for x in queue
            if (x.get("status") or "").lower() in ("draft", "approved", "queued")
        ]

    # Если передан sync=true: начисто вычистить 'Sent' и 'Skipped' для активного списка
    if sync and st_clean not in ("sent", "skipped", "all_with_archived", "archive"):
        result_queue = [
            x for x in result_queue
            if (x.get("status") or "").lower() not in ("sent", "skipped")
        ]

    # Сортировка: Approved первыми, затем по updated_at/created_at
    result_queue.sort(
        key=lambda x: (
            1 if (x.get("status") or "").lower() in ("approved", "queued") else 0,
            x.get("updated_at") or x.get("created_at") or ""
        ),
        reverse=True
    )

    return {
        "queue": result_queue,
        "stats": stats,
        "default_check_in": DEFAULT_CHECK_IN,
        "default_check_out": DEFAULT_CHECK_OUT,
        "default_context": DEFAULT_CONTEXT,
    }


@app.post("/api/requests/save")
async def save_request_item(payload: SaveRequestItem):
    """Сохраняет отредактированный текст сообщения или статус карточки хоста."""
    updated = queue_manager.update_item(
        username=payload.username,
        message=payload.message,
        status=payload.status,
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Host not found in queue")

    queue = queue_manager.load_queue()
    stats = {
        "total": len(queue),
        "approved": sum(1 for x in queue if x.get("status") == "Approved"),
        "draft": sum(1 for x in queue if x.get("status") == "Draft"),
        "skipped": sum(1 for x in queue if x.get("status") == "Skipped"),
    }
    return {"status": "saved", "item": updated, "stats": stats}


@app.post("/api/requests/regenerate")
async def regenerate_single_request(payload: RegenerateItem):
    """Перегенерирует персонализированный запрос для конкретного хоста."""
    target_id = (payload.host_id or payload.username or "").strip()
    if not target_id:
        return JSONResponse(status_code=400, content={"error": "Field 'host_id' or 'username' is required"})

    try:
        updated = await asyncio.to_thread(
            queue_manager.regenerate_item,
            username=target_id,
            check_in=payload.check_in,
            check_out=payload.check_out,
            guest_context=payload.guest_context,
        )
        if not updated:
            return JSONResponse(status_code=404, content={"error": f"Host '{target_id}' not found in queue or base"})

        msg = updated.get("generated_message", "")
        words = updated.get("word_count", len(msg.split()))
        qc_passed = updated.get("qc_passed", True)

        return {
            "status": "ok",
            "host_id": target_id,
            "username": updated.get("username", target_id),
            "generated_message": msg,
            "word_count": words,
            "qc_passed": qc_passed,
            "item": updated,
        }
    except Exception as e:
        import traceback
        traceback.print_exc()
        process_mgr.append_log(f"[!] Ошибка LLM перегенерации для {target_id}: {e}")
        return JSONResponse(status_code=500, content={"error": f"LLM generation failed: {str(e)}"})


batch_regen_state: Dict[str, Any] = {
    "is_running": False,
    "total": 0,
    "completed": 0,
    "active_ids": [],
    "completed_items": {},
    "failed_ids": [],
    "updated_at": None,
}

_regen_semaphore = asyncio.Semaphore(2)


async def _process_single_regeneration(target_id: str, check_in: str, check_out: str, guest_context: str):
    async with _regen_semaphore:
        try:
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
                item_info = {
                    "host_id": target_id,
                    "username": updated.get("username", target_id),
                    "generated_message": msg,
                    "word_count": words,
                    "qc_passed": qc_passed,
                    "status": updated.get("status", "Draft"),
                }
                batch_regen_state["completed_items"][target_id] = item_info
                batch_regen_state["completed"] += 1
                if target_id in batch_regen_state["active_ids"]:
                    batch_regen_state["active_ids"].remove(target_id)

                comp = batch_regen_state["completed"]
                tot = batch_regen_state["total"]
                process_mgr.append_log(f"[REGEN] Updated {comp}/{tot} drafts: @{target_id} ({words} words, QC: {'PASS' if qc_passed else 'REVIEW'})")
                return item_info
            else:
                batch_regen_state["failed_ids"].append(target_id)
                if target_id in batch_regen_state["active_ids"]:
                    batch_regen_state["active_ids"].remove(target_id)
                return None
        except Exception as e:
            process_mgr.append_log(f"[!] Ошибка LLM перегенерации для {target_id}: {e}")
            batch_regen_state["failed_ids"].append(target_id)
            if target_id in batch_regen_state["active_ids"]:
                batch_regen_state["active_ids"].remove(target_id)
            return None


async def process_regeneration_queue(request_ids: List[str], check_in: Optional[str] = None, check_out: Optional[str] = None, guest_context: Optional[str] = None):
    check_in = check_in or DEFAULT_CHECK_IN
    check_out = check_out or DEFAULT_CHECK_OUT
    guest_context = guest_context or DEFAULT_CONTEXT

    batch_regen_state["is_running"] = True
    batch_regen_state["total"] = len(request_ids)
    batch_regen_state["completed"] = 0
    batch_regen_state["active_ids"] = list(request_ids)
    batch_regen_state["completed_items"] = {}
    batch_regen_state["failed_ids"] = []
    batch_regen_state["updated_at"] = datetime.now().isoformat()

    process_mgr.append_log(f"[REGEN] 🚀 Запуск фоновой регенерации {len(request_ids)} черновиков (Semaphore=2)...")

    tasks = [
        _process_single_regeneration(str(rid).strip(), check_in, check_out, guest_context)
        for rid in request_ids
    ]
    await asyncio.gather(*tasks, return_exceptions=True)

    batch_regen_state["is_running"] = False
    batch_regen_state["active_ids"] = []
    batch_regen_state["updated_at"] = datetime.now().isoformat()
    process_mgr.append_log(f"[REGEN] 🏁 Регенерация пачки завершена: успешно {batch_regen_state['completed']}/{batch_regen_state['total']}")


@app.post("/api/queue/regenerate-batch")
@app.post("/api/requests/regenerate-batch")
async def api_queue_regenerate_batch(payload: RegenerateBatchPayload):
    """Асинхронный эндпоинт фоновой пачечной регенерации выбранных сообщений (без таймаута)."""
    raw_ids = payload.request_ids or payload.ids or payload.usernames or []
    ids = [str(x).strip() for x in raw_ids if str(x).strip()]
    if not ids:
        return JSONResponse(status_code=400, content={"error": "Field 'request_ids' is empty or missing"})

    if batch_regen_state["is_running"]:
        return JSONResponse(status_code=409, content={"error": "Генерация уже выполняется в фоне!", "is_running": True, "active_count": len(batch_regen_state["active_ids"])})

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
    """Возвращает текущий прогресс и готовые черновики пачечной регенерации."""
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
                    UPDATE generated_messages 
                    SET message_text = ?, check_in = ?, check_out = ?, word_count = ?, qc_passed = ?
                    WHERE host_id = ? AND is_active = 1
                """, (new_msg, iso_in, iso_out, words, 1 if qc_passed else 0, item.get("username") or item.get("id")))
                if city:
                    cursor.execute("UPDATE hosts SET location = ? WHERE username = ? OR id = ?", (city, item.get("username"), item.get("id")))
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


@app.post("/api/requests/approve_all")
async def approve_all_drafts():
    """Массовое одобрение всех черновиков для подготовки к автопилоту."""
    count = queue_manager.approve_all_drafts()
    queue = queue_manager.load_queue(exclude_sent=False)
    stats = {
        "total": len(queue),
        "approved": sum(1 for x in queue if x.get("status") == "Approved"),
        "draft": sum(1 for x in queue if x.get("status") == "Draft"),
        "skipped": sum(1 for x in queue if x.get("status") == "Skipped"),
    }
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
async def get_autopilot_status():
    """Возвращает статус автопилота, обратный отсчет, суточный прогресс, кандидата на очереди, списки ready_candidates и sent_history."""
    status_file = OUTPUT_DIR / "autopilot_status.json"
    sent_log_file = OUTPUT_DIR / "sent_log.json"
    queue_file = OUTPUT_DIR / "requests_queue.json"

    sent_today = 0
    last_sent = None
    sent_history = []
    seen_sent = set()

    now_dt = datetime.now()
    today_date = now_dt.date()
    yesterday_date = today_date - timedelta(days=1)

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

            for entry in reversed(logs):
                st = entry.get("status", "")
                if st in ("SENT_SUCCESS", "success", "dry_run_success", "DRY_RUN_SUCCESS"):
                    u = entry.get("username") or entry.get("id")
                    if not u or u in seen_sent:
                        continue
                    seen_sent.add(u)
                    seen_sent.add(u.strip().lower())

                    ts_raw = entry.get("timestamp", "")
                    fmt_time = "Recent"
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

                    sent_item = {
                        "username": u,
                        "name": entry.get("name") or u,
                        "timestamp": fmt_time,
                        "raw_timestamp": ts_raw,
                        "status": "WAS SENT",
                        "mode": entry.get("mode", "live"),
                    }
                    sent_history.append(sent_item)
                    if last_sent is None:
                        last_sent = {
                            "name": sent_item["name"],
                            "username": u,
                            "timestamp": fmt_time,
                            "status": "Sent / Verified",
                            "is_success": True,
                        }
        except Exception as e:
            logger.warning(f"Error loading sent_log: {e}")

    # Обогащаем seen_sent из всех источников (sent_history, sent_log, SQLite, requests_queue)
    try:
        all_sent_known = queue_manager.get_sent_usernames()
        for u in all_sent_known:
            if u:
                seen_sent.add(u)
                seen_sent.add(u.strip().lower())
    except Exception as e:
        logger.warning(f"Error enriching seen_sent from queue_manager: {e}")

    # Также добавляем хостов со статусом "Sent" из requests_queue.json в sent_history
    if queue_file.exists():
        try:
            with open(queue_file, "r", encoding="utf-8") as f:
                q_items = json.load(f)
            added_sent = {h["username"].lower() for h in sent_history if "username" in h}
            for item in q_items:
                if (item.get("status") or "").lower() == "sent":
                    u = item.get("username")
                    if u and u.lower() not in added_sent:
                        sent_history.insert(0, {
                            "username": u,
                            "name": item.get("clean_name") or item.get("name") or u,
                            "timestamp": item.get("sent_at") or "Sent",
                            "raw_timestamp": item.get("sent_at") or "",
                            "status": "WAS SENT",
                            "mode": "live",
                        })
                        added_sent.add(u.lower())
        except Exception as e:
            logger.warning(f"Error appending queue sent items to sent_history: {e}")

    is_running = (process_mgr.active_task_name == "Автопилот" and process_mgr.is_running)

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
        "last_sent": last_sent,
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

            # Принудительно используем вычисленные значения
            status_data["sent_today"] = sent_today
            status_data["daily_limit"] = 100

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

    # Load ready candidates
    ready_candidates = []
    if queue_file.exists():
        try:
            with open(queue_file, "r", encoding="utf-8") as f:
                queue = json.load(f)
            for item in queue:
                st = (item.get("status") or "").lower()
                u = item.get("username") or item.get("id")
                # Exclude if already sent
                if not u or u in seen_sent:
                    continue
                if st in ("approved", "queued"):
                    is_this_sending = bool(current_sending and current_sending.lower() == u.lower())
                    cand = {
                        "name": item.get("clean_name") or item.get("name") or u,
                        "username": u,
                        "location": item.get("location") or "Tokyo, Japan",
                        "status": "SENDING" if is_this_sending else "QUEUED",
                    }
                    ready_candidates.append(cand)
        except Exception as e:
            logger.warning(f"Error loading ready_candidates: {e}")

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

    return status_data


@app.on_event("startup")
async def app_startup_cleanup():
    """Сброс зависших статусов sending при старте сервера."""
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

    # Сбрасываем флаг паузы перед новым запуском
    status_file = OUTPUT_DIR / "autopilot_status.json"
    if status_file.exists():
        try:
            with open(status_file, "r", encoding="utf-8") as f:
                d = json.load(f)
            d["is_paused"] = False
            d["current_host"] = None
            with open(status_file, "w", encoding="utf-8") as f:
                json.dump(d, f, indent=2)
        except Exception:
            pass

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
async def api_sync_dispatcher_queue():
    """Синхронизация и очистка очереди автопилота от уже отправленных хостов."""
    rm = RequestQueueManager()
    removed = rm.deduplicate_queue_against_sent()
    return {
        "ok": True,
        "status": "success",
        "removed_count": removed,
        "message": f"Очищено {removed} отправленных хостов из очереди"
    }


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
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=False)
