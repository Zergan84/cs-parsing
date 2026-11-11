"""
test_isolation.py - Единый контур изоляции путей и защиты production-данных для тестов (Фаза 0).

Гарантирует:
1. Перенаправление CS_DB_PATH и CS_OUTPUT_DIR во временную директорию вне production output/.
2. Немедленное аварийное завершение при попытке указать production-пути в тестовом режиме.
3. Блокировку записи/удаления в production SQLite, production output/ и user_data/.
4. Блокировку запуска реального AutoSender и опасных подпроцессов.
5. Блокировку реального Playwright против couchsurfing.com и любых внешних доменов.
6. Блокировку внешних сетевых запросов на уровне socket, urllib, requests, httpx.
7. Контроль неизменности SHA-256 всех production-файлов до и после тестов.
"""

import asyncio
import atexit
import builtins
import hashlib
import io
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Set, Tuple

PROD_BASE_DIR: Path = Path(__file__).resolve().parent
PROD_OUTPUT_DIR: Path = (PROD_BASE_DIR / "output").resolve()
PROD_DB_PATH: Path = (PROD_OUTPUT_DIR / "couchsurfing_platform.db").resolve()
PROD_REQUESTS_DB_SYMLINK: Path = PROD_BASE_DIR / "requests.db"
PROD_USER_DATA_DIR: Path = (PROD_BASE_DIR / "user_data").resolve()
PROD_INDEX_HTML: Path = (PROD_BASE_DIR / "templates" / "index.html").resolve()


class ProductionAccessError(RuntimeError):
    """Исключение при попытке теста обратиться к рабочим production-файлам или БД."""


class ExternalNetworkAccessError(RuntimeError):
    """Исключение при попытке теста выполнить внешний сетевой запрос или открыть внешний URL."""


class ForbiddenProcessExecutionError(RuntimeError):
    """Исключение при попытке теста запустить реальный AutoSender или запрещённый подпроцесс."""


_REAL_SQLITE_CONNECT = sqlite3.connect
_REAL_BUILTINS_OPEN = builtins.open
_REAL_IO_OPEN = io.open
_REAL_PATH_OPEN = Path.open
_REAL_PATH_WRITE_TEXT = Path.write_text
_REAL_PATH_WRITE_BYTES = Path.write_bytes
_REAL_PATH_UNLINK = Path.unlink
_REAL_OS_REMOVE = os.remove
_REAL_OS_UNLINK = os.unlink
_REAL_OS_REPLACE = os.replace
_REAL_OS_RENAME = os.rename
_REAL_SHUTIL_RMTREE = shutil.rmtree
_REAL_SHUTIL_COPY = shutil.copy
_REAL_SHUTIL_COPY2 = shutil.copy2
_REAL_SHUTIL_COPYFILE = shutil.copyfile
_REAL_SHUTIL_MOVE = shutil.move
_REAL_SUBPROCESS_POPEN = subprocess.Popen
_REAL_SUBPROCESS_RUN = subprocess.run
_REAL_SUBPROCESS_CALL = subprocess.call
_REAL_ASYNCIO_SUBPROCESS_EXEC = asyncio.create_subprocess_exec
_REAL_ASYNCIO_SUBPROCESS_SHELL = asyncio.create_subprocess_shell
_REAL_SOCKET_CONNECT = socket.socket.connect
_REAL_SOCKET_CONNECT_EX = socket.socket.connect_ex
_REAL_SOCKET_CREATE_CONNECTION = socket.create_connection
_REAL_URLLIB_URLOPEN = urllib.request.urlopen

_GUARDS_INSTALLED: bool = False
_SESSION_TEMP_DIR: Optional[Path] = None

# По умолчанию в тестах блокируются даже локальные порты production-сервера (8000, 8001) и Ollama (11434),
# чтобы тесты случайно не задели живой сервер или локальную LLM.
BLOCKED_LOCAL_PORTS: Set[int] = {8000, 8001, 11434}
ALLOWED_LOCAL_PORTS: Set[int] = set()

FORBIDDEN_SUBPROCESS_SCRIPTS: Tuple[str, ...] = (
    "auto_sender.py",
    "scraper.py",
    "sync_sent_requests.py",
    "auth.py",
    "build_index.py",
    "fix_false_sent.py",
    "hotfix_approved_texts.py",
    "reprocess_queue.py",
)


def get_output_dir() -> Path:
    """Возвращает текущую директорию output с учётом CS_OUTPUT_DIR."""
    env_val = os.environ.get("CS_OUTPUT_DIR", "").strip()
    if env_val:
        return Path(env_val).resolve()
    return PROD_OUTPUT_DIR


def get_db_path() -> Path:
    """Возвращает текущий путь к SQLite БД с учётом CS_DB_PATH и CS_OUTPUT_DIR."""
    env_db = os.environ.get("CS_DB_PATH", "").strip()
    if env_db:
        return Path(env_db).resolve()
    env_out = os.environ.get("CS_OUTPUT_DIR", "").strip()
    if env_out:
        return (Path(env_out).resolve() / "couchsurfing_platform.db").resolve()
    return PROD_DB_PATH


def _normalize_candidate_path(raw_path: Any) -> Optional[Path]:
    """Преобразует аргумент пути (включая SQLite URI file:...) в абсолютный Path."""
    if raw_path is None or isinstance(raw_path, int):
        return None
    if isinstance(raw_path, bytes):
        try:
            raw_path = raw_path.decode("utf-8", errors="replace")
        except Exception:
            return None
    if isinstance(raw_path, Path):
        s = str(raw_path)
    elif isinstance(raw_path, str):
        s = raw_path.strip()
    else:
        try:
            s = os.fspath(raw_path)
            if isinstance(s, bytes):
                s = s.decode("utf-8", errors="replace")
        except Exception:
            return None

    if not s or s == ":memory:" or s.startswith("file::memory:"):
        return None

    if s.startswith("file:"):
        parsed = urllib.parse.urlparse(s)
        s = urllib.parse.unquote(parsed.path or s[5:].split("?", 1)[0])

    try:
        p = Path(s)
        if not p.is_absolute():
            p = (Path.cwd() / p)
        return p.resolve()
    except Exception:
        return None


def is_protected_production_path(raw_path: Any) -> bool:
    """Возвращает True, если путь указывает на рабочую БД, output/, user_data/ или index.html."""
    if raw_path is None:
        return False
    # Проверка самого симлинка requests.db до resolve()
    try:
        raw_str = os.fspath(raw_path)
        if isinstance(raw_str, bytes):
            raw_str = raw_str.decode("utf-8", errors="replace")
        cand_abs = Path(raw_str)
        if not cand_abs.is_absolute():
            cand_abs = Path.cwd() / cand_abs
        if cand_abs.absolute() == PROD_REQUESTS_DB_SYMLINK.absolute():
            return True
    except Exception:
        pass

    resolved = _normalize_candidate_path(raw_path)
    if resolved is None:
        return False

    if resolved == PROD_DB_PATH:
        return True
    if resolved == PROD_OUTPUT_DIR or PROD_OUTPUT_DIR in resolved.parents:
        return True
    if resolved == PROD_USER_DATA_DIR or PROD_USER_DATA_DIR in resolved.parents:
        return True
    if resolved == PROD_INDEX_HTML:
        return True
    return False


def assert_not_production_paths(
    db_path: Optional[Path] = None,
    output_dir: Optional[Path] = None,
) -> Tuple[Path, Path]:
    """
    Проверяет, что CS_DB_PATH и CS_OUTPUT_DIR заданы и физически изолированы от production.
    При нарушении немедленно выбрасывает ProductionAccessError.
    """
    env_out = os.environ.get("CS_OUTPUT_DIR", "").strip()
    env_db = os.environ.get("CS_DB_PATH", "").strip()

    if not env_out or not env_db:
        raise ProductionAccessError(
            "CRITICAL: CS_OUTPUT_DIR и CS_DB_PATH обязаны быть явно заданы в тестовом режиме! "
            f"Текущие значения: CS_OUTPUT_DIR={env_out!r}, CS_DB_PATH={env_db!r}"
        )

    out_resolved = (Path(output_dir) if output_dir else Path(env_out)).resolve()
    db_resolved = (Path(db_path) if db_path else Path(env_db)).resolve()

    if (
        is_protected_production_path(out_resolved)
        or out_resolved == PROD_BASE_DIR
        or out_resolved in PROD_OUTPUT_DIR.parents
    ):
        raise ProductionAccessError(
            f"CRITICAL: CS_OUTPUT_DIR указывает на защищённую production-директорию: {out_resolved}"
        )

    if (
        is_protected_production_path(db_resolved)
        or is_protected_production_path(env_db)
        or db_resolved == PROD_BASE_DIR
    ):
        raise ProductionAccessError(
            f"CRITICAL: CS_DB_PATH указывает на защищённую production-БД: {db_resolved}"
        )

    return out_resolved, db_resolved


def rebind_all_modules(
    output_dir: Optional[Path] = None,
    db_path: Optional[Path] = None,
) -> Tuple[Path, Path]:
    """
    Перенастраивает CS_OUTPUT_DIR и CS_DB_PATH и обновляет атрибуты во всех уже загруженных модулях проекта.
    """
    if output_dir is not None:
        os.environ["CS_OUTPUT_DIR"] = str(Path(output_dir).resolve())
    if db_path is not None:
        os.environ["CS_DB_PATH"] = str(Path(db_path).resolve())

    out_dir, db_p = assert_not_production_paths()
    out_dir.mkdir(parents=True, exist_ok=True)
    db_p.parent.mkdir(parents=True, exist_ok=True)

    # 1. database
    if "database" in sys.modules:
        mod = sys.modules["database"]
        mod.OUTPUT_DIR = out_dir
        mod.DB_PATH = db_p

    # 2. request_manager
    if "request_manager" in sys.modules:
        mod = sys.modules["request_manager"]
        mod.OUTPUT_DIR = out_dir
        mod.QUEUE_FILE = out_dir / "requests_queue.json"
        mod.CURATED_FILE = out_dir / "hosts_tokyo_curated.json"
        mod.RAW_FILE = out_dir / "hosts_tokyo.json"
        mod.SENT_HISTORY_FILE = out_dir / "sent_history.json"
        mod.SENT_LOG_FILE = out_dir / "sent_log.json"

    # 3. app
    if "app" in sys.modules:
        mod = sys.modules["app"]
        mod.OUTPUT_DIR = out_dir
        mod.QUEUE_FILE = out_dir / "requests_queue.json"
        if hasattr(mod, "queue_manager") and mod.queue_manager is not None:
            mod.queue_manager.queue_file = out_dir / "requests_queue.json"

    # 4. auto_sender
    if "auto_sender" in sys.modules:
        mod = sys.modules["auto_sender"]
        mod.OUTPUT_DIR = out_dir
        mod.DEBUG_DIR = out_dir / "debug"
        mod.QUEUE_FILE = out_dir / "requests_queue.json"
        mod.SENT_LOG_FILE = out_dir / "sent_log.json"
        mod.STATUS_FILE = out_dir / "autopilot_status.json"

    # 5. scraper
    if "scraper" in sys.modules:
        mod = sys.modules["scraper"]
        mod.OUTPUT_DIR = out_dir

    # 6. inbox_checker & inbox_service
    if "inbox_checker" in sys.modules:
        mod = sys.modules["inbox_checker"]
        mod.OUTPUT_DIR = out_dir
        mod.SENT_LOG_FILE = out_dir / "sent_log.json"
        mod.INBOX_STATUS_FILE = out_dir / "inbox_status.json"

    if "inbox_service" in sys.modules:
        mod = sys.modules["inbox_service"]
        mod.OUTPUT_DIR = out_dir
        mod.INBOX_STATUS_FILE = out_dir / "inbox_status.json"

    # 7. filter_hosts, hosting_guard, gender_classifier, sync_sent_requests
    if "filter_hosts" in sys.modules:
        mod = sys.modules["filter_hosts"]
        mod.OUTPUT_DIR = out_dir

    if "hosting_guard" in sys.modules:
        mod = sys.modules["hosting_guard"]
        mod.OUTPUT_DIR = out_dir
        mod.REJECTED_FILE = out_dir / "rejected_hosts.json"
        mod.GUARD_CACHE_FILE = out_dir / "hosting_guard_cache.json"

    if "gender_classifier" in sys.modules:
        mod = sys.modules["gender_classifier"]
        mod.OUTPUT_DIR = out_dir
        mod.CACHE_FILE = out_dir / "gender_cache.json"
        if getattr(mod, "_classifier_instance", None) is not None:
            mod._classifier_instance.cache_path = out_dir / "gender_cache.json"

    if "sync_sent_requests" in sys.modules:
        mod = sys.modules["sync_sent_requests"]
        mod.OUTPUT_DIR = out_dir
        mod.SENT_HISTORY_FILE = out_dir / "sent_history.json"
        mod.SENT_LOG_FILE = out_dir / "sent_log.json"

    # 8. Миграционные и сервисные скрипты
    for script_name in (
        "migrate_profile_system",
        "migrate_profile_system_v2",
        "fix_queue_names",
        "fix_false_sent",
        "hotfix_approved_texts",
        "reprocess_queue",
        "requeue_missing",
        "final_report",
    ):
        if script_name in sys.modules:
            mod = sys.modules[script_name]
            if hasattr(mod, "OUTPUT_DIR"):
                mod.OUTPUT_DIR = out_dir
            if hasattr(mod, "DB_PATH"):
                mod.DB_PATH = db_p
            if hasattr(mod, "PLATFORM_DB"):
                mod.PLATFORM_DB = db_p
            if hasattr(mod, "DB"):
                mod.DB = db_p
            if hasattr(mod, "QUEUE_FILE"):
                mod.QUEUE_FILE = out_dir / "requests_queue.json"
            if hasattr(mod, "QUEUE"):
                mod.QUEUE = out_dir / "requests_queue.json"
            if hasattr(mod, "SENT_LOG_FILE"):
                mod.SENT_LOG_FILE = out_dir / "sent_log.json"
            if hasattr(mod, "SENT_LOG"):
                mod.SENT_LOG = out_dir / "sent_log.json"
            if hasattr(mod, "LOG_FILE"):
                mod.LOG_FILE = out_dir / "migration_v2_log.json"

    return out_dir, db_p


def _is_write_mode(mode: str) -> bool:
    m = str(mode or "r")
    return any(ch in m for ch in ("w", "a", "x", "+"))


def _check_host_and_port_allowed(host: Optional[str], port: Optional[int], context: str) -> None:
    """Разрешает только localhost/127.0.0.1 на явно разрешённых тестовых портах."""
    h = (host or "").strip().lower()
    if h.startswith("[") and h.endswith("]"):
        h = h[1:-1]

    allowed_hosts = {"127.0.0.1", "localhost", "::1", "testserver"}
    if h not in allowed_hosts:
        raise ExternalNetworkAccessError(
            f"BLOCKED [{context}]: Запрещено внешнее сетевое обращение к хосту {host!r} (port={port})"
        )

    if port is not None:
        try:
            p_int = int(port)
        except (ValueError, TypeError):
            p_int = None
        if p_int is not None and p_int in BLOCKED_LOCAL_PORTS and p_int not in ALLOWED_LOCAL_PORTS:
            raise ExternalNetworkAccessError(
                f"BLOCKED [{context}]: Запрещено обращение к защищённому локальному порту {h}:{p_int} "
                "(production server / live Ollama)"
            )


@contextmanager
def allow_local_test_port(port: int):
    """Контекстный менеджер для временного разрешения локального порта тестового сервера."""
    ALLOWED_LOCAL_PORTS.add(int(port))
    try:
        yield
    finally:
        ALLOWED_LOCAL_PORTS.discard(int(port))


def _check_subprocess_cmd_allowed(cmd: Any) -> None:
    """Блокирует запуск реального AutoSender и других опасных скриптов в подпроцессах."""
    if isinstance(cmd, (str, bytes)):
        cmd_str = cmd.decode("utf-8", errors="replace") if isinstance(cmd, bytes) else cmd
        tokens = [cmd_str]
    elif isinstance(cmd, Iterable):
        tokens = [str(x) for x in cmd]
        cmd_str = " ".join(tokens)
    else:
        cmd_str = str(cmd)
        tokens = [cmd_str]

    cmd_lower = cmd_str.lower()
    for forbidden in FORBIDDEN_SUBPROCESS_SCRIPTS:
        if forbidden.lower() in cmd_lower:
            raise ForbiddenProcessExecutionError(
                f"BLOCKED: Запуск реального скрипта {forbidden!r} в тестовом режиме запрещён! Команда: {cmd_str}"
            )


def _wrap_playwright_page(page: Any) -> Any:
    """Оборачивает page.goto для запрета навигации на couchsurfing.com и любые внешние домены."""
    orig_goto = getattr(page, "goto", None)
    if orig_goto is None or getattr(orig_goto, "_cs_guarded", False):
        return page

    def _guarded_goto(url: str, *args: Any, **kwargs: Any) -> Any:
        parsed = urllib.parse.urlparse(str(url))
        scheme = (parsed.scheme or "").lower()
        if scheme in ("about", "data", "blob"):
            return orig_goto(url, *args, **kwargs)
        host = parsed.hostname
        port = parsed.port
        if "couchsurfing.com" in str(url).lower():
            raise ExternalNetworkAccessError(
                f"BLOCKED [Playwright]: Навигация на реальный Couchsurfing запрещена в тестах: {url}"
            )
        _check_host_and_port_allowed(host, port, "Playwright page.goto")
        return orig_goto(url, *args, **kwargs)

    _guarded_goto._cs_guarded = True  # type: ignore[attr-defined]
    page.goto = _guarded_goto
    return page


def _wrap_playwright_context(ctx: Any) -> Any:
    orig_new_page = getattr(ctx, "new_page", None)
    if orig_new_page is not None and not getattr(orig_new_page, "_cs_guarded", False):
        def _guarded_new_page(*args: Any, **kwargs: Any) -> Any:
            page = orig_new_page(*args, **kwargs)
            return _wrap_playwright_page(page)

        _guarded_new_page._cs_guarded = True  # type: ignore[attr-defined]
        ctx.new_page = _guarded_new_page
    return ctx


def _wrap_playwright_browser(browser: Any) -> Any:
    orig_new_context = getattr(browser, "new_context", None)
    if orig_new_context is not None and not getattr(orig_new_context, "_cs_guarded", False):
        def _guarded_new_context(*args: Any, **kwargs: Any) -> Any:
            storage_state = kwargs.get("storage_state")
            if storage_state and is_protected_production_path(storage_state):
                raise ProductionAccessError(
                    f"BLOCKED [Playwright]: Запрещено использовать production storage_state в тестах: {storage_state}"
                )
            ctx = orig_new_context(*args, **kwargs)
            return _wrap_playwright_context(ctx)

        _guarded_new_context._cs_guarded = True  # type: ignore[attr-defined]
        browser.new_context = _guarded_new_context

    orig_new_page = getattr(browser, "new_page", None)
    if orig_new_page is not None and not getattr(orig_new_page, "_cs_guarded", False):
        def _guarded_browser_new_page(*args: Any, **kwargs: Any) -> Any:
            page = orig_new_page(*args, **kwargs)
            return _wrap_playwright_page(page)

        _guarded_browser_new_page._cs_guarded = True  # type: ignore[attr-defined]
        browser.new_page = _guarded_browser_new_page

    return browser


def install_runtime_guards() -> None:
    """Устанавливает многоуровневые перехватчики для защиты production-данных, сети и подпроцессов."""
    global _GUARDS_INSTALLED
    if _GUARDS_INSTALLED:
        return

    # 1. Защита SQLite (полный запрет подключения к production БД из тестов)
    def _guarded_sqlite_connect(database: Any, *args: Any, **kwargs: Any) -> sqlite3.Connection:
        assert_not_production_paths()
        if is_protected_production_path(database):
            raise ProductionAccessError(
                f"BLOCKED [sqlite3.connect]: Попытка теста подключиться к рабочей production SQLite БД: {database}"
            )
        return _REAL_SQLITE_CONNECT(database, *args, **kwargs)

    sqlite3.connect = _guarded_sqlite_connect  # type: ignore[assignment]

    # 2. Защита файловой системы от записи/удаления в production output/ и user_data/
    def _guarded_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        if is_protected_production_path(file):
            if _is_write_mode(mode):
                raise ProductionAccessError(
                    f"BLOCKED [open]: Попытка теста открыть production-файл на запись (mode={mode!r}): {file}"
                )
        return _REAL_BUILTINS_OPEN(file, mode, *args, **kwargs)

    def _guarded_io_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        if is_protected_production_path(file):
            if _is_write_mode(mode):
                raise ProductionAccessError(
                    f"BLOCKED [io.open]: Попытка теста открыть production-файл на запись (mode={mode!r}): {file}"
                )
        return _REAL_IO_OPEN(file, mode, *args, **kwargs)

    def _guarded_path_open(self_path: Path, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        if is_protected_production_path(self_path) and _is_write_mode(mode):
            raise ProductionAccessError(
                f"BLOCKED [Path.open]: Попытка теста открыть production-файл на запись (mode={mode!r}): {self_path}"
            )
        return _REAL_PATH_OPEN(self_path, mode, *args, **kwargs)

    def _guarded_write_text(self_path: Path, *args: Any, **kwargs: Any) -> int:
        if is_protected_production_path(self_path):
            raise ProductionAccessError(
                f"BLOCKED [Path.write_text]: Попытка теста перезаписать production-файл: {self_path}"
            )
        return _REAL_PATH_WRITE_TEXT(self_path, *args, **kwargs)

    def _guarded_write_bytes(self_path: Path, *args: Any, **kwargs: Any) -> int:
        if is_protected_production_path(self_path):
            raise ProductionAccessError(
                f"BLOCKED [Path.write_bytes]: Попытка теста перезаписать production-файл: {self_path}"
            )
        return _REAL_PATH_WRITE_BYTES(self_path, *args, **kwargs)

    def _guarded_path_unlink(self_path: Path, *args: Any, **kwargs: Any) -> None:
        if is_protected_production_path(self_path):
            raise ProductionAccessError(
                f"BLOCKED [Path.unlink]: Попытка теста удалить production-файл: {self_path}"
            )
        return _REAL_PATH_UNLINK(self_path, *args, **kwargs)

    def _guarded_os_remove(path: Any, *args: Any, **kwargs: Any) -> None:
        if kwargs.get("dir_fd") is None and is_protected_production_path(path):
            raise ProductionAccessError(
                f"BLOCKED [os.remove]: Попытка теста удалить production-файл: {path}"
            )
        return _REAL_OS_REMOVE(path, *args, **kwargs)

    def _guarded_os_replace(src: Any, dst: Any, *args: Any, **kwargs: Any) -> None:
        if is_protected_production_path(src) or is_protected_production_path(dst):
            raise ProductionAccessError(
                f"BLOCKED [os.replace]: Попытка теста переместить/перезаписать production-файл: {src} -> {dst}"
            )
        return _REAL_OS_REPLACE(src, dst, *args, **kwargs)

    def _guarded_os_rename(src: Any, dst: Any, *args: Any, **kwargs: Any) -> None:
        if is_protected_production_path(src) or is_protected_production_path(dst):
            raise ProductionAccessError(
                f"BLOCKED [os.rename]: Попытка теста переименовать production-файл: {src} -> {dst}"
            )
        return _REAL_OS_RENAME(src, dst, *args, **kwargs)

    def _guarded_rmtree(path: Any, *args: Any, **kwargs: Any) -> Any:
        if is_protected_production_path(path):
            raise ProductionAccessError(
                f"BLOCKED [shutil.rmtree]: Попытка теста удалить production-директорию: {path}"
            )
        return _REAL_SHUTIL_RMTREE(path, *args, **kwargs)

    def _guarded_copy(src: Any, dst: Any, *args: Any, **kwargs: Any) -> Any:
        if is_protected_production_path(dst):
            raise ProductionAccessError(
                f"BLOCKED [shutil.copy]: Попытка теста перезаписать production-файл: {dst}"
            )
        return _REAL_SHUTIL_COPY(src, dst, *args, **kwargs)

    def _guarded_copy2(src: Any, dst: Any, *args: Any, **kwargs: Any) -> Any:
        if is_protected_production_path(dst):
            raise ProductionAccessError(
                f"BLOCKED [shutil.copy2]: Попытка теста перезаписать production-файл: {dst}"
            )
        return _REAL_SHUTIL_COPY2(src, dst, *args, **kwargs)

    def _guarded_copyfile(src: Any, dst: Any, *args: Any, **kwargs: Any) -> Any:
        if is_protected_production_path(dst):
            raise ProductionAccessError(
                f"BLOCKED [shutil.copyfile]: Попытка теста перезаписать production-файл: {dst}"
            )
        return _REAL_SHUTIL_COPYFILE(src, dst, *args, **kwargs)

    def _guarded_move(src: Any, dst: Any, *args: Any, **kwargs: Any) -> Any:
        if is_protected_production_path(src) or is_protected_production_path(dst):
            raise ProductionAccessError(
                f"BLOCKED [shutil.move]: Попытка теста переместить production-файл: {src} -> {dst}"
            )
        return _REAL_SHUTIL_MOVE(src, dst, *args, **kwargs)

    builtins.open = _guarded_open  # type: ignore[assignment]
    io.open = _guarded_io_open  # type: ignore[assignment]
    Path.open = _guarded_path_open  # type: ignore[assignment]
    Path.write_text = _guarded_write_text  # type: ignore[assignment]
    Path.write_bytes = _guarded_write_bytes  # type: ignore[assignment]
    Path.unlink = _guarded_path_unlink  # type: ignore[assignment]
    os.remove = _guarded_os_remove  # type: ignore[assignment]
    os.unlink = _guarded_os_remove  # type: ignore[assignment]
    os.replace = _guarded_os_replace  # type: ignore[assignment]
    os.rename = _guarded_os_rename  # type: ignore[assignment]
    shutil.rmtree = _guarded_rmtree  # type: ignore[assignment]
    shutil.copy = _guarded_copy  # type: ignore[assignment]
    shutil.copy2 = _guarded_copy2  # type: ignore[assignment]
    shutil.copyfile = _guarded_copyfile  # type: ignore[assignment]
    shutil.move = _guarded_move  # type: ignore[assignment]

    # 3. Защита от запуска AutoSender и опасных скриптов через subprocess / asyncio
    class _GuardedPopen(_REAL_SUBPROCESS_POPEN):  # type: ignore[misc]
        def __init__(self, args: Any, *popen_args: Any, **popen_kwargs: Any) -> None:
            _check_subprocess_cmd_allowed(args)
            super().__init__(args, *popen_args, **popen_kwargs)

    def _guarded_subprocess_run(popenargs: Any, *args: Any, **kwargs: Any) -> Any:
        _check_subprocess_cmd_allowed(popenargs)
        return _REAL_SUBPROCESS_RUN(popenargs, *args, **kwargs)

    def _guarded_subprocess_call(popenargs: Any, *args: Any, **kwargs: Any) -> Any:
        _check_subprocess_cmd_allowed(popenargs)
        return _REAL_SUBPROCESS_CALL(popenargs, *args, **kwargs)

    async def _guarded_create_subprocess_exec(program: Any, *args: Any, **kwargs: Any) -> Any:
        _check_subprocess_cmd_allowed([program, *args])
        return await _REAL_ASYNCIO_SUBPROCESS_EXEC(program, *args, **kwargs)

    async def _guarded_create_subprocess_shell(cmd: Any, *args: Any, **kwargs: Any) -> Any:
        _check_subprocess_cmd_allowed(cmd)
        return await _REAL_ASYNCIO_SUBPROCESS_SHELL(cmd, *args, **kwargs)

    subprocess.Popen = _GuardedPopen  # type: ignore[misc,assignment]
    subprocess.run = _guarded_subprocess_run  # type: ignore[assignment]
    subprocess.call = _guarded_subprocess_call  # type: ignore[assignment]
    asyncio.create_subprocess_exec = _guarded_create_subprocess_exec  # type: ignore[assignment]
    asyncio.create_subprocess_shell = _guarded_create_subprocess_shell  # type: ignore[assignment]

    # 4. Защита сети на уровне socket, urllib, requests, httpx
    def _guarded_socket_connect(sock_self: socket.socket, address: Any) -> Any:
        if isinstance(address, tuple) and len(address) >= 2:
            host, port = address[0], address[1]
            _check_host_and_port_allowed(str(host), int(port), "socket.connect")
        return _REAL_SOCKET_CONNECT(sock_self, address)

    def _guarded_socket_connect_ex(sock_self: socket.socket, address: Any) -> int:
        if isinstance(address, tuple) and len(address) >= 2:
            host, port = address[0], address[1]
            _check_host_and_port_allowed(str(host), int(port), "socket.connect_ex")
        return _REAL_SOCKET_CONNECT_EX(sock_self, address)

    def _guarded_create_connection(address: Tuple[str, int], *args: Any, **kwargs: Any) -> socket.socket:
        if isinstance(address, tuple) and len(address) >= 2:
            _check_host_and_port_allowed(str(address[0]), int(address[1]), "socket.create_connection")
        return _REAL_SOCKET_CREATE_CONNECTION(address, *args, **kwargs)

    def _guarded_urlopen(url: Any, *args: Any, **kwargs: Any) -> Any:
        target_url = url.full_url if hasattr(url, "full_url") else str(url)
        parsed = urllib.parse.urlparse(target_url)
        _check_host_and_port_allowed(parsed.hostname, parsed.port, "urllib.request.urlopen")
        return _REAL_URLLIB_URLOPEN(url, *args, **kwargs)

    socket.socket.connect = _guarded_socket_connect  # type: ignore[assignment]
    socket.socket.connect_ex = _guarded_socket_connect_ex  # type: ignore[assignment]
    socket.create_connection = _guarded_create_connection  # type: ignore[assignment]
    urllib.request.urlopen = _guarded_urlopen  # type: ignore[assignment]

    try:
        import requests.adapters

        _orig_requests_send = requests.adapters.HTTPAdapter.send

        def _guarded_requests_send(self_adapter: Any, request: Any, *args: Any, **kwargs: Any) -> Any:
            parsed = urllib.parse.urlparse(str(request.url))
            _check_host_and_port_allowed(parsed.hostname, parsed.port, "requests.HTTPAdapter.send")
            return _orig_requests_send(self_adapter, request, *args, **kwargs)

        requests.adapters.HTTPAdapter.send = _guarded_requests_send  # type: ignore[assignment]
    except ImportError:
        pass

    try:
        import httpx

        _orig_httpx_send = httpx.Client.send
        _orig_httpx_async_send = httpx.AsyncClient.send

        def _guarded_httpx_send(self_client: Any, request: Any, *args: Any, **kwargs: Any) -> Any:
            host = request.url.host
            port = request.url.port
            _check_host_and_port_allowed(host, port, "httpx.Client.send")
            return _orig_httpx_send(self_client, request, *args, **kwargs)

        async def _guarded_httpx_async_send(self_client: Any, request: Any, *args: Any, **kwargs: Any) -> Any:
            host = request.url.host
            port = request.url.port
            _check_host_and_port_allowed(host, port, "httpx.AsyncClient.send")
            return await _orig_httpx_async_send(self_client, request, *args, **kwargs)

        httpx.Client.send = _guarded_httpx_send  # type: ignore[assignment]
        httpx.AsyncClient.send = _guarded_httpx_async_send  # type: ignore[assignment]
    except ImportError:
        pass

    # 5. Защита Playwright (блокировка навигации на внешние URL и couchsurfing.com)
    try:
        import playwright.sync_api as pw_sync

        _orig_sync_pw = pw_sync.sync_playwright

        class _GuardedSyncPlaywrightContextManager:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                self._cm = _orig_sync_pw(*args, **kwargs)

            def __enter__(self) -> Any:
                p = self._cm.__enter__()
                for browser_type_name in ("chromium", "firefox", "webkit"):
                    bt = getattr(p, browser_type_name, None)
                    if bt is None:
                        continue
                    orig_launch = getattr(bt, "launch", None)
                    if orig_launch and not getattr(orig_launch, "_cs_guarded", False):
                        def _make_guarded_launch(_ol: Any) -> Any:
                            def _guarded_launch(*l_args: Any, **l_kwargs: Any) -> Any:
                                browser = _ol(*l_args, **l_kwargs)
                                return _wrap_playwright_browser(browser)
                            _guarded_launch._cs_guarded = True  # type: ignore[attr-defined]
                            return _guarded_launch

                        bt.launch = _make_guarded_launch(orig_launch)

                    orig_persistent = getattr(bt, "launch_persistent_context", None)
                    if orig_persistent and not getattr(orig_persistent, "_cs_guarded", False):
                        def _make_guarded_persistent(_op: Any) -> Any:
                            def _guarded_persistent(user_data_dir: Any, *p_args: Any, **p_kwargs: Any) -> Any:
                                if is_protected_production_path(user_data_dir):
                                    raise ProductionAccessError(
                                        f"BLOCKED [Playwright]: Запрещён запуск persistent context на рабочей user_data: {user_data_dir}"
                                    )
                                ctx = _op(user_data_dir, *p_args, **p_kwargs)
                                return _wrap_playwright_context(ctx)
                            _guarded_persistent._cs_guarded = True  # type: ignore[attr-defined]
                            return _guarded_persistent

                        bt.launch_persistent_context = _make_guarded_persistent(orig_persistent)
                return p

            def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> Any:
                return self._cm.__exit__(exc_type, exc_val, exc_tb)

        pw_sync.sync_playwright = _GuardedSyncPlaywrightContextManager  # type: ignore[assignment]
    except ImportError:
        pass

    # 6. Защита AutoSender от реальной браузерной отправки
    try:
        import auto_sender as as_mod

        def _blocked_send_request(self_sender: Any, *args: Any, **kwargs: Any) -> Any:
            raise ForbiddenProcessExecutionError(
                "BLOCKED [AutoSender.send_request]: Вызов реальной отправки сообщения в Couchsurfing запрещён в тестах!"
            )

        as_mod.AutoSender.send_request = _blocked_send_request  # type: ignore[assignment]

        _orig_autosender_run = as_mod.AutoSender.run

        def _guarded_autosender_run(self_sender: Any, *args: Any, **kwargs: Any) -> Any:
            assert_not_production_paths()
            if is_protected_production_path(as_mod.QUEUE_FILE) or is_protected_production_path(as_mod.SENT_LOG_FILE):
                raise ProductionAccessError(
                    f"BLOCKED [AutoSender.run]: QUEUE_FILE или SENT_LOG_FILE указывает на production: {as_mod.QUEUE_FILE}"
                )
            # Если sync_playwright в auto_sender не замокан (т.е. указывает на реальный Playwright),
            # запрещаем запуск, если в очереди есть Approved кандидаты
            sp_attr = getattr(as_mod, "sync_playwright", None)
            is_mocked_sp = hasattr(sp_attr, "assert_not_called") or "MagicMock" in type(sp_attr).__name__
            if not is_mocked_sp:
                raise ForbiddenProcessExecutionError(
                    "BLOCKED [AutoSender.run]: Запуск AutoSender.run() без мока sync_playwright запрещён в тестах!"
                )
            return _orig_autosender_run(self_sender, *args, **kwargs)

        as_mod.AutoSender.run = _guarded_autosender_run  # type: ignore[assignment]
    except ImportError:
        pass

    _GUARDS_INSTALLED = True


def activate_test_isolation(custom_temp_dir: Optional[Path] = None) -> Tuple[Path, Path]:
    """
    Инициализирует безопасное изолированное окружение для тестов.
    Должна вызываться ДО импорта модулей проекта в каждом тестовом файле и в conftest.py.
    """
    global _SESSION_TEMP_DIR

    os.environ["CS_TEST_MODE"] = "1"

    if custom_temp_dir is not None:
        base_tmp = Path(custom_temp_dir).resolve()
        base_tmp.mkdir(parents=True, exist_ok=True)
        out_dir = base_tmp / "output"
        db_path = out_dir / "couchsurfing_platform.db"
        os.environ["CS_OUTPUT_DIR"] = str(out_dir)
        os.environ["CS_DB_PATH"] = str(db_path)
    else:
        env_out = os.environ.get("CS_OUTPUT_DIR", "").strip()
        env_db = os.environ.get("CS_DB_PATH", "").strip()
        if not env_out or not env_db:
            if _SESSION_TEMP_DIR is None or not _SESSION_TEMP_DIR.exists():
                _SESSION_TEMP_DIR = Path(tempfile.mkdtemp(prefix="cs_isolated_test_")).resolve()
                atexit.register(lambda: shutil.rmtree(_SESSION_TEMP_DIR, ignore_errors=True))
            out_dir = _SESSION_TEMP_DIR / "output"
            db_path = out_dir / "couchsurfing_platform.db"
            os.environ["CS_OUTPUT_DIR"] = str(out_dir)
            os.environ["CS_DB_PATH"] = str(db_path)

    # Проверяем, что пути гарантированно не указывают на production
    out_resolved, db_resolved = assert_not_production_paths()
    rebind_all_modules(out_resolved, db_resolved)
    install_runtime_guards()
    return out_resolved, db_resolved


def reset_isolated_test_env() -> Tuple[Path, Path]:
    """
    Создаёт новую чистую временную директорию и новую пустую БД SQLite для текущего теста/класса,
    полностью исключая протекание тестовых данных между модулями при запуске через unittest.
    """
    new_tmp = Path(tempfile.mkdtemp(prefix="cs_isolated_case_")).resolve()
    atexit.register(lambda p=new_tmp: shutil.rmtree(p, ignore_errors=True))
    return activate_test_isolation(custom_temp_dir=new_tmp)


def get_tracked_production_files() -> Tuple[Path, ...]:
    """Возвращает полный список отслеживаемых production-файлов (БД + все JSON)."""
    fixed = [
        PROD_DB_PATH,
        PROD_REQUESTS_DB_SYMLINK,
        PROD_OUTPUT_DIR / "requests_queue.json",
        PROD_OUTPUT_DIR / "sent_log.json",
        PROD_OUTPUT_DIR / "sent_history.json",
        PROD_OUTPUT_DIR / "autopilot_status.json",
        PROD_OUTPUT_DIR / "inbox_status.json",
        PROD_OUTPUT_DIR / "rejected_hosts.json",
    ]
    for p in sorted(PROD_OUTPUT_DIR.glob("hosts_*.json")):
        if p not in fixed:
            fixed.append(p)
    return tuple(fixed)


def snapshot_production_checksums() -> Dict[str, Dict[str, Any]]:
    """Снимает SHA-256 и размер всех production-файлов в режиме строгого чтения."""
    snapshot: Dict[str, Dict[str, Any]] = {}
    for path in get_tracked_production_files():
        key = str(path)
        if path.exists():
            with _REAL_BUILTINS_OPEN(path, "rb") as f:
                data = f.read()
            snapshot[key] = {
                "exists": True,
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        else:
            snapshot[key] = {
                "exists": False,
                "size": 0,
                "sha256": None,
            }
    return snapshot


def verify_production_checksums(before: Dict[str, Dict[str, Any]]) -> None:
    """Сверяет текущие SHA-256 production-файлов со снимком до запуска тестов."""
    after = snapshot_production_checksums()
    diffs = []
    for path_str, before_meta in before.items():
        after_meta = after.get(path_str)
        if before_meta != after_meta:
            diffs.append(f"{path_str}: BEFORE={before_meta} != AFTER={after_meta}")
    if diffs:
        raise ProductionAccessError(
            "CRITICAL: Контрольные суммы production-файлов изменились во время выполнения тестов!\n"
            + "\n".join(diffs)
        )
