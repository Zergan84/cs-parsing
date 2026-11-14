"""Launch paths fail closed and singleton locking never replaces a live sender."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException


def test_start_route_refuses_when_os_process_snapshot_is_unavailable(monkeypatch):
    import app

    monkeypatch.setattr(app, "get_setting", lambda *args, **kwargs: "0")
    monkeypatch.setattr(app, "read_process_table", lambda: (False, []))
    spawn = AsyncMock(return_value={"success": True})
    monkeypatch.setattr(app, "_start_dispatcher_job", spawn)
    with pytest.raises(HTTPException) as error:
        asyncio.run(app.start_autopilot(app.AutopilotParams(trip_id="kyoto-trip")))
    assert error.value.status_code == 503
    spawn.assert_not_awaited()


def test_singleton_lock_does_not_terminate_existing_sender(tmp_path, monkeypatch):
    import fcntl
    import auto_sender

    guard_path = tmp_path / "auto_sender.lock"
    held_guard = guard_path.open("a+")
    fcntl.flock(held_guard.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    monkeypatch.setattr(auto_sender, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(auto_sender, "_SINGLETON_LOCK_HANDLE", None)
    monkeypatch.setattr(auto_sender.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(auto_sender.os, "kill", lambda *_args: (_ for _ in ()).throw(AssertionError("must not kill sender")))
    try:
        with pytest.raises(RuntimeError, match="singleton-блокировку"):
            auto_sender.acquire_singleton_lock()
        assert not (tmp_path / "auto_sender.pid").exists()
    finally:
        fcntl.flock(held_guard.fileno(), fcntl.LOCK_UN)
        held_guard.close()
