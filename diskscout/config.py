from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field

HOME = os.path.expanduser("~")


def config_path() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(HOME, ".config")
    return os.path.join(base, "diskscout", "config.json")


def reports_dir() -> str:
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(HOME, ".local", "state")
    return os.path.join(base, "diskscout", "reports")


def load_config(path: str | None = None) -> dict:
    path = path or config_path()
    if not os.path.exists(path):
        return {}
    with open(path) as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise SystemExit(f"{path}: expected a JSON object")
    return data


def latest_report(base: str | None = None) -> str | None:
    base = base or load_config().get("reports_dir") or reports_dir()
    base = os.path.expanduser(base)
    if not os.path.isdir(base):
        return None
    runs = sorted(d for d in os.listdir(base) if os.path.exists(os.path.join(base, d, "report.json")))
    return os.path.join(base, runs[-1], "report.json") if runs else None

MIB = 1024 * 1024
GIB = 1024 * MIB

DEFAULT_PROJECT_ROOTS = [
    os.path.join(HOME, "Documents", "Projects"),
    os.path.join(HOME, "CascadeProjects"),
]

DEFAULT_PERSONAL_ROOTS = [
    os.path.join(HOME, "Documents"),
    os.path.join(HOME, "Downloads"),
    os.path.join(HOME, "Desktop"),
    os.path.join(HOME, "Music"),
    os.path.join(HOME, "Movies"),
    os.path.join(HOME, "Pictures"),
    os.path.join(HOME, "Dropbox"),
]


@dataclass
class Settings:
    project_roots: list[str] = field(default_factory=lambda: list(DEFAULT_PROJECT_ROOTS))
    personal_roots: list[str] = field(default_factory=lambda: list(DEFAULT_PERSONAL_ROOTS))
    inactive_days: int = 30
    recent_hours: int = 24
    dup_min_size: int = 1 * MIB
    dir_dup_min_size: int = 10 * MIB
    large_file_min: int = 500 * MIB
    installer_min: int = 20 * MIB
    cache_min: int = 100 * MIB
    workers: int = 8
    protect: list[str] = field(default_factory=list)
    home: str = HOME
    now: float = field(default_factory=time.time)

    @property
    def inactive_cutoff(self) -> float:
        return self.now - self.inactive_days * 86400

    @property
    def recent_cutoff(self) -> float:
        return self.now - self.recent_hours * 3600

    def scan_roots(self) -> list[str]:
        """Existing roots with nested duplicates removed (parents win)."""
        roots = sorted({os.path.realpath(r) for r in self.project_roots + self.personal_roots if os.path.isdir(r)})
        out: list[str] = []
        for r in roots:
            if not any(r == o or r.startswith(o + os.sep) for o in out):
                out.append(r)
        return out
