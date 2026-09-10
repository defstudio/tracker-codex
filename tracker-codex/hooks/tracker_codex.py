#!/usr/bin/env python3
"""Send bounded active-time heartbeats from Codex hooks to Tracker."""

from __future__ import annotations

import fcntl
import getpass
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


MAX_ACTIVE_SECONDS = 300
STATE_FILE = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "tracker-codex" / "sessions.json"
CONFIG_FILE = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "tracker-codex" / "config.json"


def load_config() -> dict[str, Any] | None:
    try:
        config = json.loads(CONFIG_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return None

    server_url = config.get("server_url")
    token = config.get("token")
    device_id = config.get("device_id")

    if not isinstance(server_url, str) or not isinstance(token, str) or not isinstance(device_id, int):
        return None

    parsed_url = urlparse(server_url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc or not token or device_id <= 0:
        return None

    return {"server_url": server_url.rstrip("/"), "token": token, "device_id": device_id}


def save_config(config: dict[str, Any]) -> None:
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    CONFIG_FILE.parent.chmod(0o700)
    CONFIG_FILE.write_text(json.dumps(config, indent=2) + "\n")
    CONFIG_FILE.chmod(0o600)


def configure() -> None:
    current = load_config() or {}
    server_url = input(f"Tracker URL [{current.get('server_url', '')}]: ").strip() or current.get("server_url", "")
    token = getpass.getpass("Tracker token (hidden; leave empty to keep the current token): ") or current.get("token", "")
    device_id = input(f"Tracker device ID [{current.get('device_id', '')}]: ").strip() or current.get("device_id")

    try:
        config = {"server_url": server_url, "token": token, "device_id": int(device_id)}
    except (TypeError, ValueError):
        raise SystemExit("The device ID must be a positive integer.")

    if not load_config_from(config):
        raise SystemExit("Enter a valid http(s) Tracker URL, a token, and a positive device ID.")

    save_config(config)
    print(f"Tracker Codex configured. Configuration saved to {CONFIG_FILE}.")


def load_config_from(config: dict[str, Any]) -> dict[str, Any] | None:
    server_url = config.get("server_url")
    token = config.get("token")
    device_id = config.get("device_id")

    if not isinstance(server_url, str) or not isinstance(token, str) or not isinstance(device_id, int):
        return None

    parsed_url = urlparse(server_url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc or not token or device_id <= 0:
        return None

    return {"server_url": server_url.rstrip("/"), "token": token, "device_id": device_id}


def print_status() -> None:
    config = load_config()
    if config is None:
        print(f"Tracker Codex is not configured. Run: {Path(__file__).resolve()} configure")
        return

    print(f"Tracker Codex is configured for {config['server_url']} (device {config['device_id']}).")


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


def record_heartbeat(config: dict[str, Any], event: dict[str, Any], active_seconds: int, remote: str) -> None:
    if active_seconds <= 0:
        return

    payload = json.dumps(
        {
            "device_id": config["device_id"],
            "type": "heartbeat",
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
        f'{config["server_url"]}/api/events/codex',
        data=payload,
        headers={
            "Authorization": f'Bearer {config["token"]}',
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
    if len(sys.argv) == 2:
        if sys.argv[1] == "configure":
            configure()
            return
        if sys.argv[1] == "status":
            print_status()
            return
        raise SystemExit("Usage: tracker_codex.py [configure|status]")

    event = json.load(sys.stdin)
    hook_event = event.get("hook_event_name")

    if hook_event == "Stop":
        print(json.dumps({"continue": True}))

    config = load_config()
    if config is None or not event.get("session_id") or not event.get("cwd"):
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
        record_heartbeat(config, event, min(int(now - previous), MAX_ACTIVE_SECONDS), remote)


if __name__ == "__main__":
    main()
