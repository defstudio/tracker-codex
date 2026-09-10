import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).parents[1] / "hooks" / "tracker_codex.py"
SPEC = importlib.util.spec_from_file_location("tracker_codex", SCRIPT)
tracker_codex = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(tracker_codex)


class TrackerCodexTest(unittest.TestCase):
    def test_heartbeat_uses_the_tracker_codex_user_agent(self) -> None:
        config = {"server_url": "https://tracker.test", "token": "secret", "device_id": 1}
        event = {"hook_event_name": "PostToolUse", "session_id": "session-1", "cwd": "/code/tracker"}

        with patch.object(tracker_codex.urllib.request, "urlopen") as urlopen:
            tracker_codex.record_heartbeat(config, event, 45, "git@example.test:acme/tracker.git")

        request = urlopen.call_args.args[0]
        self.assertEqual(tracker_codex.USER_AGENT, request.get_header("User-agent"))

    def test_detects_files_changed_in_a_nested_repository(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = Path(temporary_directory)
            repository = workspace / "backend"
            repository.mkdir()
            subprocess.run(["git", "init", "-q", str(repository)], check=True)
            subprocess.run(["git", "-C", str(repository), "config", "user.email", "tests@example.com"], check=True)
            subprocess.run(["git", "-C", str(repository), "config", "user.name", "Tests"], check=True)
            (repository / "app.php").write_text("<?php\n")
            subprocess.run(["git", "-C", str(repository), "add", "app.php"], check=True)
            subprocess.run(["git", "-C", str(repository), "-c", "commit.gpgSign=false", "commit", "-qm", "Initial"], check=True)

            previous = tracker_codex.snapshot_repositories(str(workspace))
            (repository / "app.php").write_text("<?php\n// changed\n")
            current = tracker_codex.snapshot_repositories(str(workspace))

            self.assertEqual([str(repository)], list(current))
            self.assertEqual(["app.php"], tracker_codex.changed_paths(str(repository), previous[str(repository)], current[str(repository)]))

            event = {"hook_event_name": "PostToolUse", "session_id": "session-1", "cwd": str(workspace)}
            config = {"server_url": "https://tracker.test", "token": "secret", "device_id": 1}
            with patch.object(tracker_codex, "git_remote", return_value="git@example.test:acme/tracker.git"), patch.object(tracker_codex, "record_heartbeat") as record_heartbeat:
                tracker_codex.record_changed_repositories(config, event, 45, previous, current)

            record_heartbeat.assert_called_once_with(config, event, 45, "git@example.test:acme/tracker.git", str(repository), ["app.php"])

    def test_session_start_saves_the_initial_repository_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            state_file = Path(temporary_directory) / "sessions.json"
            event = {"hook_event_name": "SessionStart", "session_id": "session-1", "cwd": temporary_directory}

            with patch.object(tracker_codex, "STATE_FILE", state_file), patch.object(tracker_codex, "load_config", return_value={"server_url": "https://tracker.test", "token": "secret", "device_id": 1}), patch.object(tracker_codex, "snapshot_repositories", return_value={}):
                with patch.object(sys, "argv", [str(SCRIPT)]), patch.object(sys, "stdin", io.StringIO(json.dumps(event))):
                    tracker_codex.main()

            state = json.loads(state_file.read_text())
            self.assertEqual({}, state["session-1"]["repositories"])
            self.assertIsInstance(state["session-1"]["timestamp"], float)

    def test_detects_files_committed_during_a_session(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = Path(temporary_directory)
            subprocess.run(["git", "init", "-q", str(repository)], check=True)
            subprocess.run(["git", "-C", str(repository), "config", "user.email", "tests@example.com"], check=True)
            subprocess.run(["git", "-C", str(repository), "config", "user.name", "Tests"], check=True)
            (repository / "initial.php").write_text("<?php\n")
            subprocess.run(["git", "-C", str(repository), "add", "initial.php"], check=True)
            subprocess.run(["git", "-C", str(repository), "-c", "commit.gpgSign=false", "commit", "-qm", "Initial"], check=True)

            previous = tracker_codex.snapshot_repositories(str(repository))
            (repository / "committed.php").write_text("<?php\n")
            subprocess.run(["git", "-C", str(repository), "add", "committed.php"], check=True)
            subprocess.run(["git", "-C", str(repository), "-c", "commit.gpgSign=false", "commit", "-qm", "Add committed file"], check=True)
            current = tracker_codex.snapshot_repositories(str(repository))

            self.assertEqual(["committed.php"], tracker_codex.changed_paths(str(repository), previous[str(repository)], current[str(repository)]))


if __name__ == "__main__":
    unittest.main()
