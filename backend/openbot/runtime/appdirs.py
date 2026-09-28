"""The per-user directory OpenBot keeps its own generated state in, outside any project: isolated git
worktrees (runtime/worktrees.py) under `wt`, an isolated Claude Code profile (runtime/cli_agent.py) under
`claude-profile`. Nothing OpenBot manages lives directly under this directory, only named subfolders of it.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def openbot_data_dir(*, platform: str = sys.platform, env=os.environ, home: Path | None = None) -> Path:
    home = home or Path.home()
    if platform == "win32":
        base = env.get("LOCALAPPDATA")
        return Path(base) / "OpenBot" if base else home / "AppData" / "Local" / "OpenBot"
    data = env.get("XDG_DATA_HOME")
    return Path(data) / "openbot" if data else home / ".local" / "share" / "openbot"
