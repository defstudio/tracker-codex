#!/usr/bin/env python3
"""Send bounded active-time heartbeats from Codex hooks to Tracker."""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


MAX_ACTIVE_SECONDS = 300
STATE_FILE = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "tracker-codex" / "sessions.json"


def tracker_configured() -> bool:
    return all(os.environ.get(name) for name in ("TRACKER_SERVER_URL", "TRACKER_TOKEN", "TRACKER_DEVICE_ID"))


def git_remote(cwd: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", cwd, "config", "--get", "remote.origin.url"],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None

    if result.returncode != 0:
        return None

    return result.stdout.strip() or None


def load_state(handle: Any) -> dict[str, float]:
    handle.seek(0)
    try:
        return json.load(handle)
    except json.JSONDecodeError:
        return {}


def save_state(handle: Any, state: dict[str, float]) -> None:
    handle.seek(0)
    handle.truncate()
    json.dump(state, handle)
    handle.flush()
    os.fsync(handle.fileno())


def record_heartbeat(event: dict[str, Any], active_seconds: int, remote: str) -> None:
    if active_seconds <= 0:
        return

    payload = json.dumps(
        {
            "device_id": int(os.environ["TRACKER_DEVICE_ID"]),
            "type": "codex_heartbeat",
            "path": event["cwd"],
            "git_remote": remote,
            "occurred_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "metadata": {
                "active_seconds": active_seconds,
                "event": event["hook_event_name"],
                "model": event.get("model"),
                "session_id": event["session_id"],
            },
        }
    ).encode()
    request = urllib.request.Request(
        f'{os.environ["TRACKER_SERVER_URL"].rstrip("/")}/api/events/ide',
        data=payload,
        headers={
            "Authorization": f'Bearer {os.environ["TRACKER_TOKEN"]}',
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=2):
            pass
    except (urllib.error.URLError, TimeoutError, ValueError):
        pass


def main() -> None:
    event = json.load(sys.stdin)
    hook_event = event.get("hook_event_name")

    if hook_event == "Stop":
        print(json.dumps({"continue": True}))

    if not tracker_configured() or not event.get("session_id") or not event.get("cwd"):
        return

    remote = git_remote(event["cwd"])
    if remote is None:
        return

    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()

    with STATE_FILE.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        state = load_state(handle)
        key = event["session_id"]
        previous = state.get(key)

        if hook_event == "SessionEnd":
            state.pop(key, None)
            save_state(handle, state)
            return

        if hook_event != "SessionStart":
            state[key] = now
            save_state(handle, state)

    if hook_event in {"UserPromptSubmit", "PostToolUse", "Stop", "Interrupt"} and previous is not None:
        record_heartbeat(event, min(int(now - previous), MAX_ACTIVE_SECONDS), remote)


if __name__ == "__main__":
    main()
