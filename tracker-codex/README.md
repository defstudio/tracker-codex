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

The hook sends data only when this configuration is valid and the current directory has an `origin` Git remote.
