"""Decides which paths must never be proposed for removal."""

from __future__ import annotations

import glob
import json
import os
from dataclasses import dataclass

from diskscout.fsutil import is_within

DONE_STAGES = {"done", "dropped", "aborted", "failed", "closed", "cancelled", "canceled", "merged"}


@dataclass
class V2cRun:
    repo: str
    run_id: str
    stage: str
    worktree_root: str | None

    @property
    def active(self) -> bool:
        return self.stage.lower() not in DONE_STAGES


def find_v2c_runs(repo: str) -> list[V2cRun]:
    runs: list[V2cRun] = []
    for f in sorted(glob.glob(os.path.join(repo, ".agent", "v2c", "runs", "*", "run.json"))):
        try:
            with open(f) as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        run_id = data.get("run") or os.path.basename(os.path.dirname(f))
        runs.append(
            V2cRun(
                repo=repo,
                run_id=run_id,
                stage=str(data.get("stage", "unknown")),
                worktree_root=data.get("worktreeRoot") or os.path.join(repo, ".worktrees", "v2c", run_id),
            )
        )
    return runs


class Guard:
    def __init__(self, runs: list[V2cRun], protect: list[str]):
        self.active_roots = [(r.worktree_root, r) for r in runs if r.active and r.worktree_root]
        self.protect = [os.path.realpath(os.path.expanduser(p)) for p in protect]

    def check(self, path: str) -> str | None:
        """Reason the path is protected, or None."""
        for root, run in self.active_roots:
            if is_within(path, root) or is_within(root, path):
                return f"belongs to active v2c run {run.run_id} (stage: {run.stage})"
        for p in self.protect:
            if is_within(path, p) or is_within(p, path):
                return f"protected by --protect {p}"
        return None
