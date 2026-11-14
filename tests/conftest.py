"""Fail-closed SQLite isolation for every pytest process and test case."""

import os
import sqlite3
import tempfile
from urllib.parse import unquote
from pathlib import Path


# Set this before test modules import project modules. Database path resolution
# enforces the mode centrally and refuses repository output/ paths.
_repository_root = Path(__file__).resolve().parents[1]
_production_db = (_repository_root / "output" / "couchsurfing_platform.db").resolve()
_inherited_db = os.environ.get("CS_DB_PATH", "").strip()
if _inherited_db:
    _inherited_path = Path(_inherited_db).expanduser().resolve()
    target = "production" if _inherited_path == _production_db else "a preconfigured database"
    raise RuntimeError(
        f"Refusing to start pytest with CS_DB_PATH pointing at {target}: {_inherited_path}. "
        "The suite assigns isolated temporary databases itself."
    )

os.environ["CS_ENV"] = "test"
_pytest_root = Path(tempfile.mkdtemp(prefix="couchsurfing-pytest-"))
os.environ["CS_OUTPUT_DIR"] = str(_pytest_root / "output")
os.environ["CS_DB_PATH"] = str(_pytest_root / "session-bootstrap.db")

_real_sqlite_connect = sqlite3.connect


def _guarded_sqlite_connect(database, *args, **kwargs):
    """Block direct SQLite calls that bypass core.database path validation."""
    try:
        raw_path = os.fspath(database)
    except TypeError:
        raw_path = str(database)
    if raw_path not in {":memory:", "file::memory:"}:
        if raw_path.startswith("file:"):
            raw_path = unquote(raw_path[5:].split("?", 1)[0])
        candidate = Path(raw_path).expanduser().resolve()
        try:
            candidate.relative_to((_repository_root / "output").resolve())
        except ValueError:
            pass
        else:
            raise RuntimeError(f"pytest is forbidden from opening SQLite under project output/: {candidate}")
        try:
            if _production_db.exists() and candidate.exists():
                expected, actual = _production_db.stat(), candidate.stat()
                if (expected.st_dev, expected.st_ino) == (actual.st_dev, actual.st_ino):
                    raise RuntimeError(f"pytest is forbidden from opening a hard link to production SQLite: {candidate}")
        except FileNotFoundError:
            pass
    return _real_sqlite_connect(database, *args, **kwargs)


sqlite3.connect = _guarded_sqlite_connect


def pytest_runtest_setup(item):
    """Give implicit database access a unique path for each test item."""
    per_test = _pytest_root / "cases" / item.nodeid.replace("/", "_").replace("::", "_")
    per_test.mkdir(parents=True, exist_ok=True)
    os.environ["CS_DB_PATH"] = str(per_test / "database.sqlite3")
    os.environ["CS_OUTPUT_DIR"] = str(per_test / "output")
