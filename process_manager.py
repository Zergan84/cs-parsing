"""
process_manager.py - Атомарный менеджер фоновых процессов и задач (Thread-safe & Transactional).

Предотвращает:
1. Одновременный запуск конкурирующих подпроцессов (Scraper, Auto-sender, Sync).
2. Потерю статусов и зависание процессов при аварийном завершении.
3. Сохраняет историю и логи каждой задачи в единый файл operations.log и в SQLite (таблица background_jobs).
4. Обеспечивает единое сквозное окно логирования: новая задача не стирает историю, логи автопилота и задач идут непрерывным потоком.
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
OUTPUT_DIR = BASE_DIR / "output"
OPERATIONS_LOG_FILE = OUTPUT_DIR / "operations.log"
AUTO_SENDER_LOG_FILE = OUTPUT_DIR / "auto_sender.log"


class ProcessManager:
    """Централизованный контроллер фоновых задач с поддержкой единого сквозного логирования."""

    def __init__(self):
        self.lock = asyncio.Lock()
        self.active_proc: Optional[asyncio.subprocess.Process] = None
        self.active_task_name: str = "None"
        self.active_job_id: Optional[str] = None
        self.logs_buffer: List[str] = []
        self._is_running: bool = False
        self._tailer_task: Optional[asyncio.Task] = None
        self._auto_sender_offset: int = 0
        self._init_logs_from_disk()

    def _init_logs_from_disk(self) -> None:
        """Инициализирует буфер логов последними строками из operations.log или auto_sender.log."""
        target_file = OPERATIONS_LOG_FILE if OPERATIONS_LOG_FILE.exists() else AUTO_SENDER_LOG_FILE
        if target_file.exists():
            try:
                with open(target_file, "r", encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()
                    for line in lines[-250:]:
                        clean = line.strip()
                        if clean:
                            self.logs_buffer.append(clean)
            except Exception as e:
                logger.warning(f"Ошибка загрузки истории логов с диска: {e}")

        if AUTO_SENDER_LOG_FILE.exists():
            try:
                self._auto_sender_offset = AUTO_SENDER_LOG_FILE.stat().st_size
            except Exception:
                self._auto_sender_offset = 0

    def ensure_tailer(self) -> None:
        """Гарантирует, что фоновый таск отслеживания auto_sender.log запущен в текущем event loop."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._tailer_task is None or self._tailer_task.done():
            self._tailer_task = loop.create_task(self._tail_external_logs())

    async def _tail_external_logs(self) -> None:
        """Непрерывное отслеживание auto_sender.log для объединения потока автопилота с текущими операциями."""
        while True:
            try:
                # Если автопилот запущен напрямую через start_job, его вывод уже читает _stream_output
                is_direct_autopilot = (self.is_running and self.active_task_name == "Автопилот")
                if not is_direct_autopilot and AUTO_SENDER_LOG_FILE.exists():
                    current_size = AUTO_SENDER_LOG_FILE.stat().st_size
                    if current_size < self._auto_sender_offset:
                        self._auto_sender_offset = 0
                    if current_size > self._auto_sender_offset:
                        with open(AUTO_SENDER_LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
                            f.seek(self._auto_sender_offset)
                            new_text = f.read()
                            self._auto_sender_offset = f.tell()
                        for line in new_text.splitlines():
                            clean = line.strip()
                            if clean:
                                self.append_log(clean, is_external=True)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"Tailer note: {e}")
            await asyncio.sleep(0.5)

    @property
    def is_running(self) -> bool:
        return self._is_running

    def get_logs(self, limit: int = 250) -> List[str]:
        self.ensure_tailer()
        return self.logs_buffer[-limit:]

    def append_log(self, message: str, is_external: bool = False) -> None:
        msg = message.strip()
        if not msg:
            return

        # Если сообщение уже содержит таймштамп вида [2026-11-11 20:46:17] или [20:46:17]
        if msg.startswith("[") and ("]" in msg[:25]):
            entry = msg
        else:
            ts = datetime.now().strftime("%H:%M:%S")
            entry = f"[{ts}] {msg}"

        print(entry)
        self.logs_buffer.append(entry)
        if len(self.logs_buffer) > 2000:
            self.logs_buffer.pop(0)

        # Синхронная дозапись в единый operations.log
        try:
            OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
            with open(OPERATIONS_LOG_FILE, "a", encoding="utf-8") as f:
                f.write(entry + "\n")
                f.flush()
        except Exception:
            pass

    async def start_job(
        self,
        task_name: str,
        cmd: List[str],
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Атомарный запуск фоновой задачи БЕЗ очистки предыдущего окна логов."""
        self.ensure_tailer()
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

            self.append_log("────────────────────────────────────────────────────────────")
            self.append_log(f"🚀 Запуск задачи '{task_name}' [Job ID: {job_id}]")
            self.append_log(f"Команда: {' '.join(cmd)}")
            self.append_log("────────────────────────────────────────────────────────────")

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
            self.active_task_name = "None"

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
                self.active_task_name = "None"
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
