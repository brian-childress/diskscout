from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from typing import Any

RISKS = ("low", "medium", "high", "protected")


def finding_id(category: str, path: str) -> str:
    """Stable short id so a finding can be selected across scans."""
    return hashlib.blake2b(f"{category}\0{path}".encode(), digest_size=4).hexdigest()


@dataclass
class Action:
    """What applying this finding would do. Never executed by the scanner."""

    kind: str  # "trash" | "command" | "review" | "manual" | "none"
    detail: str = ""
    paths: list[str] = field(default_factory=list)
    command: list[str] | None = None  # argv for kind == "command"
    post: list[list[str]] | None = None  # argv run after a successful trash (e.g. git worktree prune)
    contents: bool = False  # trash the folder's contents, keep the folder itself


@dataclass
class Finding:
    category: str
    title: str
    path: str
    allocated: int  # bytes the item occupies (hardlinks counted once)
    reclaimable: int  # bytes freed if applied (hardlinks shared outside excluded)
    risk: str
    recommended: bool
    reason: str
    action: Action
    evidence: dict[str, Any] = field(default_factory=dict)
    id: str = ""

    def __post_init__(self) -> None:
        if not self.id:
            self.id = finding_id(self.category, self.path)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
