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
from typing import Any, TypedDict
from urllib.parse import urlparse


MAX_ACTIVE_SECONDS = 300
STATE_FILE = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "tracker-codex" / "sessions.json"
CONFIG_FILE = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "tracker-codex" / "config.json"
MAX_REPOSITORY_DEPTH = 4
IGNORED_DIRECTORIES = {".git", ".idea", ".venv", "node_modules", "vendor"}


class RepositorySnapshot(TypedDict):
    head: str | None
    paths: list[str]


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


def git_output(cwd: str, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", cwd, *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None

    if result.returncode != 0:
        return None

    return result.stdout or None


def git_remote(cwd: str) -> str | None:
    remote = git_output(cwd, "config", "--get", "remote.origin.url")

    return remote.strip() if remote is not None else None


def git_root(cwd: str) -> str | None:
    root = git_output(cwd, "rev-parse", "--show-toplevel")

    return root.strip() if root is not None else None


def discover_repositories(cwd: str) -> list[str]:
    workspace = Path(cwd).resolve()
    repositories: set[str] = set()

    root = git_root(str(workspace))
    if root is not None:
        repositories.add(root)

    for directory, children, _ in os.walk(workspace):
        path = Path(directory)
        depth = len(path.relative_to(workspace).parts)
        children[:] = [child for child in children if child not in IGNORED_DIRECTORIES]
        if depth >= MAX_REPOSITORY_DEPTH:
            children.clear()

        if (path / ".git").exists():
            root = git_root(str(path))
            if root is not None:
                repositories.add(root)

    return sorted(repositories)


def dirty_paths(repository: str) -> set[str]:
    status = git_output(repository, "status", "--porcelain=v1", "--untracked-files=all")
    if status is None:
        return set()

    return {line[3:] for line in status.splitlines() if len(line) > 3}


def snapshot_repositories(cwd: str) -> dict[str, RepositorySnapshot]:
    return {
        repository: {
            "head": (git_output(repository, "rev-parse", "HEAD") or "").strip() or None,
            "paths": sorted(dirty_paths(repository)),
        }
        for repository in discover_repositories(cwd)
    }


def changed_paths(repository: str, previous: RepositorySnapshot | None, current: RepositorySnapshot) -> list[str]:
    paths = set(current["paths"])
    if previous is None:
        return sorted(paths)

    paths.symmetric_difference_update(previous["paths"])
    if previous["head"] != current["head"] and previous["head"] is not None and current["head"] is not None:
        commits = git_output(repository, "diff", "--name-only", f'{previous["head"]}..{current["head"]}')
        if commits is not None:
            paths.update(path for path in commits.splitlines() if path)

    return sorted(paths)


def load_state(handle: Any) -> dict[str, Any]:
    handle.seek(0)
    try:
        return json.load(handle)
    except json.JSONDecodeError:
        return {}


def save_state(handle: Any, state: dict[str, Any]) -> None:
    handle.seek(0)
    handle.truncate()
    json.dump(state, handle)
    handle.flush()
    os.fsync(handle.fileno())


def record_heartbeat(config: dict[str, Any], event: dict[str, Any], active_seconds: int, remote: str | None, repository_root: str | None = None, changed_files: list[str] | None = None) -> None:
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
                "repository_root": repository_root,
                "changed_files": changed_files or [],
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


def record_changed_repositories(config: dict[str, Any], event: dict[str, Any], active_seconds: int, previous: dict[str, RepositorySnapshot], current: dict[str, RepositorySnapshot]) -> None:
    changed = [
        (repository, changed_paths(repository, previous.get(repository), snapshot))
        for repository, snapshot in current.items()
    ]
    changed = [(repository, paths) for repository, paths in changed if paths]

    if changed:
        for repository, paths in changed:
            record_heartbeat(config, event, active_seconds, git_remote(repository), repository, paths)

        return

    repository = git_root(event["cwd"])
    record_heartbeat(config, event, active_seconds, git_remote(repository) if repository is not None else None, repository)


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

        current = {"timestamp": now, "repositories": snapshot_repositories(event["cwd"])}
        state[key] = current
        save_state(handle, state)

    if hook_event in {"UserPromptSubmit", "PostToolUse", "Stop", "Interrupt"} and previous is not None:
        previous_timestamp = previous.get("timestamp", previous) if isinstance(previous, dict) else previous
        previous_repositories = previous.get("repositories", {}) if isinstance(previous, dict) else {}
        if isinstance(previous_timestamp, (float, int)):
            record_changed_repositories(config, event, min(int(now - previous_timestamp), MAX_ACTIVE_SECONDS), previous_repositories, current["repositories"])


if __name__ == "__main__":
    main()
