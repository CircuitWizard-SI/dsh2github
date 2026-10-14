from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re


def is_private_task_repository(metadata):
    return (metadata.get("private") is True and metadata.get("is_template") is not True
            and metadata.get("name", "").casefold() not in {"dsh2github", "codex2github", "codex2github-private"})


def config_path():
    return Path(os.environ.get("DSH2GITHUB_CONFIG", "config.json")).expanduser().resolve()


def resolve_path(value, root):
    path = Path(value).expanduser()
    return (path if path.is_absolute() else root / path).resolve()


@dataclass(frozen=True)
class Settings:
    path: Path
    repo: str
    dsh_home: Path
    workspace: Path
    runtime_dir: Path
    poll_seconds: int = 15
    sync_seconds: int = 30
    approval_timeout_seconds: int = 900

    def require_configured(self):
        if not self.path.is_file():
            raise ValueError("Run dsh2github init --repo OWNER/PRIVATE_REPO first")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.repo) or self.repo.startswith("OWNER/"):
            raise ValueError("repo must be OWNER/PRIVATE_REPO")
        if not 1 <= self.poll_seconds <= 20:
            raise ValueError("poll_seconds must be 1..20 to keep the approval heartbeat alive")
        if self.sync_seconds < 10:
            raise ValueError("sync_seconds must be at least 10")


def load_settings(path=None):
    path = (path or config_path()).resolve()
    data = json.loads(path.read_text(encoding="utf-8-sig")) if path.is_file() else {}
    if not isinstance(data, dict):
        raise ValueError("Configuration must be an object")
    root = path.parent
    return Settings(path, data.get("repo", ""),
        resolve_path(data.get("dsh_home") or os.environ.get("DSH_HOME") or str(Path.home() / ".dsh"), root),
        resolve_path(data.get("workspace", "workspace"), root),
        resolve_path(data.get("runtime_dir", ".runtime"), root),
        int(data.get("poll_seconds", 15)), int(data.get("sync_seconds", 30)))
