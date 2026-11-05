"""
process_manager.py - Атомарный менеджер фоновых процессов и задач (Thread-safe & Transactional).

Предотвращает:
1. Одновременный запуск конкурирующих подпроцессов (Scraper, Auto-sender, Sync).
2. Потерю статусов и зависание процессов при аварийном завершении.
3. Сохраняет историю и логи каждой задачи в SQLite (таблица background_jobs).
"""

import asyncio
import logging
import os
import signal
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from database import db_session

logger = logging.getLogger("ProcessManager")
BASE_DIR = Path(__file__).parent


class ProcessManager:
    """Централизованный контроллер фоновых задач с поддержкой блокировок и отслеживания в SQLite."""

    def __init__(self):
        self.lock = asyncio.Lock()
        self.active_proc: Optional[asyncio.subprocess.Process] = None
        self.active_task_name: str = "None"
        self.active_job_id: Optional[str] = None
        self.logs_buffer: List[str] = []
        self._is_running: bool = False

    @property
    def is_running(self) -> bool:
        return self._is_running

    def get_logs(self, limit: int = 250) -> List[str]:
        return self.logs_buffer[-limit:]

    def append_log(self, message: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        entry = f"[{ts}] {message}"
        print(entry)
        self.logs_buffer.append(entry)
        if len(self.logs_buffer) > 1000:
            self.logs_buffer.pop(0)

    async def start_job(
        self,
        task_name: str,
        cmd: List[str],
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Атомарный запуск фоновой задачи с проверкой блокировки."""
        async with self.lock:
            if self._is_running and self.active_proc and self.active_proc.returncode is None:
                return {
                    "success": False,
                    "error": f"Процесс '{self.active_task_name}' уже выполняется! Дождитесь его завершения или остановите.",
                }

            job_id = f"{task_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            self.active_job_id = job_id
            self.active_task_name = task_name
            self._is_running = True
            self.logs_buffer.clear()

            # Запись задачи в SQLite
            with db_session() as conn:
                cur = conn.cursor()
                cur.execute(
                    """
                    INSERT INTO background_jobs (job_id, job_type, status, parameters_json, started_at)
                    VALUES (?, ?, 'running', ?, ?);
                    """,
                    (job_id, task_name, json_dumps(params or {}), datetime.now().isoformat()),
                )

            self.append_log(f"🚀 Запуск задачи '{task_name}' [Job ID: {job_id}]")
            self.append_log(f"Команда: {' '.join(cmd)}")

            env = os.environ.copy()
            env["PYTHONIOENCODING"] = "utf-8"
            env["PYTHONUTF8"] = "1"
            env["PYTHONUNBUFFERED"] = "1"

            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    cwd=str(BASE_DIR),
                    env=env,
                )
                self.active_proc = proc
            except Exception as e:
                self._is_running = False
                self.append_log(f"❌ Ошибка запуска процесса: {e}")
                self._update_job_in_db(job_id, "failed", f"Failed to start: {e}")
                return {"success": False, "error": str(e)}

            # Фоновое потоковое чтение логов
            asyncio.create_task(self._stream_output(proc, job_id, task_name))

            return {"success": True, "job_id": job_id, "task_name": task_name}

    async def _stream_output(self, proc: asyncio.subprocess.Process, job_id: str, task_name: str) -> None:
        """Потоковое чтение вывода процесса без блокировки событийного цикла."""
        try:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                try:
                    text = line.decode("utf-8", errors="replace").strip()
                except Exception:
                    text = line.decode("cp1251", errors="replace").strip()
                if text:
                    self.append_log(text)

            await proc.wait()
            ret_code = proc.returncode
            status = "completed" if ret_code == 0 else "failed"
            self.append_log(f"🏁 Задача '{task_name}' завершена (код возврата: {ret_code})")
            self._update_job_in_db(job_id, status, f"Exit code {ret_code}")
        except Exception as e:
            self.append_log(f"⚠️ Ошибка чтения вывода процесса: {e}")
            self._update_job_in_db(job_id, "failed", str(e))
        finally:
            self._is_running = False
            self.active_proc = None

    def _update_job_in_db(self, job_id: str, status: str, message: str = "") -> None:
        try:
            with db_session() as conn:
                cur = conn.cursor()
                cur.execute(
                    """
                    UPDATE background_jobs 
                    SET status = ?, message = ?, finished_at = ?
                    WHERE job_id = ?;
                    """,
                    (status, message, datetime.now().isoformat(), job_id),
                )
        except Exception as e:
            logger.error(f"Ошибка обновления статуса задачи в БД: {e}")

    async def stop_current_job(self) -> Dict[str, Any]:
        """Аварийная остановка текущей выполняемой задачи (Graceful -> Force)."""
        async with self.lock:
            if not self._is_running or not self.active_proc:
                return {"success": False, "message": "Нет активных выполняемых процессов."}

            job_id = self.active_job_id
            task_name = self.active_task_name
            self.append_log(f"🛑 Запрос остановки процесса '{task_name}'...")

            try:
                # 1. Попытка корректного завершения SIGTERM
                self.active_proc.terminate()
                try:
                    await asyncio.wait_for(self.active_proc.wait(), timeout=4.0)
                    self.append_log("✅ Процесс корректно завершился (SIGTERM).")
                except asyncio.TimeoutError:
                    # 2. Принудительное завершение SIGKILL
                    self.append_log("⚠️ Процесс не ответил на SIGTERM, отправка SIGKILL...")
                    self.active_proc.kill()
                    await self.active_proc.wait()
                    self.append_log("🛑 Процесс принудительно убит (SIGKILL).")

                if job_id:
                    self._update_job_in_db(job_id, "aborted", "Terminated by user")

                self._is_running = False
                self.active_proc = None
                return {"success": True, "message": f"Задача '{task_name}' успешно остановлена."}
            except Exception as e:
                self.append_log(f"❌ Ошибка остановки процесса: {e}")
                return {"success": False, "error": str(e)}


def json_dumps(data: Any) -> str:
    import json
    return json.dumps(data, ensure_ascii=False)


_process_mgr_instance: Optional[ProcessManager] = None


def get_process_manager() -> ProcessManager:
    global _process_mgr_instance
    if _process_mgr_instance is None:
        _process_mgr_instance = ProcessManager()
    return _process_mgr_instance
