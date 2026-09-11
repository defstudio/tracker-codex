# Tracker Codex

Records active Codex work as Tracker sessions, grouped by Git repository.

## Configure

After installing the plugin, run this command from this repository:

```bash
python3 tracker-codex/hooks/tracker_codex.py configure
```

It asks for the Tracker URL, personal token, and device ID. The token is hidden while typing and the values are saved only on your computer in `~/.config/tracker-codex/config.json`, with file permissions restricted to your user.

To verify the setup without revealing the token:

```bash
python3 tracker-codex/hooks/tracker_codex.py status
```

The hook takes a Git snapshot when a Codex session starts. On later hooks it detects changed, staged, untracked, deleted, and newly committed files, including repositories nested below the workspace such as `backend`, `laravel`, or `src`. It sends each changed repository as a separate heartbeat, with relative file paths in the event metadata.

Files that were already modified when the session started are excluded unless their content or index state changes during the session. The plugin does not capture file contents, prompts, or tool output.
