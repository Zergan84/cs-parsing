"""Regression checks for import-time and pytest SQLite isolation."""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from core.database import get_db_path, init_db
from core.queue import RequestQueueManager

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_DB = PROJECT_ROOT / "output" / "couchsurfing_platform.db"


def test_import_app_does_not_initialize_or_create_sqlite(tmp_path):
    db_path = tmp_path / "must-not-exist.sqlite3"
    env = os.environ.copy()
    env.update({
        "CS_ENV": "test",
        "CS_DB_PATH": str(db_path),
        "CS_OUTPUT_DIR": str(tmp_path / "output"),
        "PYTHONPATH": str(PROJECT_ROOT),
    })
    script = r'''
import database
def forbidden_init(*args, **kwargs):
    raise AssertionError("app import called init_db")
database.init_db = forbidden_init
import sqlite3
def forbidden_connect(*args, **kwargs):
    raise AssertionError("app import opened SQLite")
sqlite3.connect = forbidden_connect
import app
assert app.app is not None
'''
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert not db_path.exists()
    assert not list(tmp_path.rglob("*.sqlite*"))


def test_request_queue_manager_construction_does_not_run_migrations(tmp_path):
    db_path = tmp_path / "queue.sqlite3"
    with patch("core.database.init_db", side_effect=AssertionError("unexpected migration")) as init:
        manager = RequestQueueManager(db_path)
    init.assert_not_called()
    assert manager.db_file == db_path.resolve()
    assert not db_path.exists()


def test_test_runtime_rejects_production_path_relative_path_and_symlink_alias(tmp_path, monkeypatch):
    with pytest.raises(RuntimeError, match="production database"):
        get_db_path(PRODUCTION_DB)

    monkeypatch.chdir(PROJECT_ROOT)
    with pytest.raises(RuntimeError, match="production database"):
        get_db_path(Path("output/couchsurfing_platform.db"))

    alias = tmp_path / "production-link.sqlite3"
    try:
        alias.symlink_to(PRODUCTION_DB)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    with pytest.raises(RuntimeError, match="production database"):
        get_db_path(alias)


def test_direct_sqlite_connections_cannot_bypass_the_path_guard():
    with pytest.raises(RuntimeError, match="project output"):
        sqlite3.connect(str(PRODUCTION_DB))
    with pytest.raises(RuntimeError, match="project output"):
        sqlite3.connect(f"file:{PRODUCTION_DB}?mode=ro", uri=True)


def test_cs_db_path_cannot_override_an_explicit_isolated_database(tmp_path, monkeypatch):
    isolated = tmp_path / "isolated.sqlite3"
    monkeypatch.setenv("CS_DB_PATH", str(PRODUCTION_DB))
    with pytest.raises(RuntimeError, match="CS_DB_PATH"):
        get_db_path(isolated)
    assert not isolated.exists()


def test_database_initialization_requires_explicit_path():
    with pytest.raises(TypeError):
        init_db()


def test_test_application_initializes_only_its_explicit_database(tmp_path, monkeypatch):
    from app import create_app
    from fastapi.testclient import TestClient

    db_path = tmp_path / "application.sqlite3"
    monkeypatch.setenv("CS_DB_PATH", str(tmp_path / "different-default.sqlite3"))
    application = create_app(db_path)

    with TestClient(application) as client:
        response = client.get("/api/trips/list")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"
    assert db_path.exists()
    assert not (tmp_path / "different-default.sqlite3").exists()


def test_queue_and_migration_test_databases_live_outside_project_output():
    # Regression guard for tests that previously created and deleted SQLite files
    # below output/, where they could be confused with application data.
    for source in PROJECT_ROOT.joinpath("tests").glob("test_*.py"):
        if source == Path(__file__).resolve():
            continue
        text = source.read_text(encoding="utf-8")
        assert '"output" / "test_' not in text, source
        assert '"output/test_' not in text, source
