"""Read-only runtime identity checks for the standalone legacy sender."""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path
from typing import Any, Dict, Iterable, Optional


def read_process_table() -> tuple[bool, list[dict[str, Any]]]:
    """Return a process snapshot using portable macOS/Linux `ps` output."""
    try:
        result = subprocess.run(
            ["ps", "-axo", "pid=,lstart=,command="],
            capture_output=True, text=True, timeout=3, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False, []
    if result.returncode != 0:
        return False, []
    processes: list[dict[str, Any]] = []
    for raw in result.stdout.splitlines():
        fields = raw.strip().split(None, 6)
        if len(fields) < 7:
            continue
        try:
            pid = int(fields[0])
        except ValueError:
            continue
        started_at = " ".join(fields[1:6])
        command = fields[6]
        processes.append({"pid": pid, "started_at": started_at, "command": command})
    return True, processes


def inspect_sender_runtime(
    saved: Optional[Dict[str, Any]],
    *,
    snapshot_ok: bool,
    processes: Iterable[Dict[str, Any]],
    expected_script: Optional[str] = None,
) -> Dict[str, Any]:
    """Classify sender status without trusting a stale status-file boolean."""
    saved = saved or {}
    expected = str(Path(expected_script).resolve()) if expected_script else None
    snapshot = list(processes)
    matched = []
    for process in snapshot:
        command = str(process.get("command") or "")
        try:
            argv = shlex.split(command)
        except ValueError:
            argv = command.split()
        script_args = [str(Path(arg).resolve()) for arg in argv[1:] if Path(arg).name == "auto_sender.py"]
        if script_args and (expected is None or expected in script_args):
            matched.append({**process, "identity_verified": True})

    saved_pid = saved.get("sender_pid")
    saved_start = saved.get("sender_started_at")
    try:
        saved_pid_int = int(saved_pid) if saved_pid is not None else None
    except (TypeError, ValueError):
        saved_pid_int = None
    saved_pid_process = next(
        (p for p in snapshot if saved_pid_int is not None and int(p.get("pid", -1)) == saved_pid_int),
        None,
    )
    if not snapshot_ok:
        process_state = "unknown"
        verified = False
    elif matched:
        process_state = "running"
        # The absolute script path and OS-reported PID/start time/command are
        # observed together. Stored identity, when present, must also agree.
        matching_saved = next((p for p in matched if saved_pid_int is not None and saved_pid_int == int(p["pid"])), None)
        if saved_pid_int is not None and matching_saved:
            verified = bool(
                saved_start == matching_saved.get("started_at")
                and (not saved.get("sender_command") or saved.get("sender_command") == matching_saved.get("command"))
            )
        elif saved_pid_int is not None:
            verified = False
        else:
            verified = True
    else:
        process_state = "stopped"
        verified = snapshot_ok

    stale = bool(saved.get("is_running") and process_state == "stopped")
    pid_reused = bool(
        saved_pid_process
        and not any(
            int(p["pid"]) == saved_pid_int and p.get("started_at") == saved_start
            for p in matched
        )
    )
    if process_state == "unknown":
        label = "UNKNOWN"
    elif process_state == "running":
        label = "RUNNING" if verified else "RUNNING — IDENTITY MISMATCH"
    else:
        label = "STOPPED — PID REUSED; STALE STATUS" if pid_reused else ("STOPPED — STALE STATUS" if stale else "STOPPED")
    return {
        "process_status": process_state,
        "process_identity_verified": verified,
        "stale_status_file": stale,
        "pid_reused": pid_reused,
        "state_label": label,
        "is_running": process_state == "running",
        "sender_processes": matched,
    }
