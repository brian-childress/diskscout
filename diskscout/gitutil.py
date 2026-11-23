"""Read-only git queries. Optional locks are disabled so `git status` never rewrites the index."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass

_ENV = {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"}


def _run(cwd: str, *args: str, timeout: int = 120) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            ["git", "-C", cwd, "--no-optional-locks", *args],
            check=False,
            capture_output=True,
            text=True,
            env=_ENV,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def git(cwd: str, *args: str, timeout: int = 120) -> str | None:
    r = _run(cwd, *args, timeout=timeout)
    return r.stdout if r is not None and r.returncode == 0 else None


def last_commit_ts(path: str) -> int | None:
    out = git(path, "log", "-1", "--format=%ct", "HEAD")
    return int(out.strip()) if out and out.strip().isdigit() else None


@dataclass
class Worktree:
    path: str
    head: str | None = None
    branch: str | None = None
    detached: bool = False
    prunable: bool = False
    locked: bool = False


def worktree_list(repo: str) -> list[Worktree]:
    out = git(repo, "worktree", "list", "--porcelain")
    if not out:
        return []
    items: list[Worktree] = []
    cur: Worktree | None = None
    for line in out.splitlines():
        if line.startswith("worktree "):
            cur = Worktree(path=line[len("worktree ") :])
            items.append(cur)
        elif cur is None:
            continue
        elif line.startswith("HEAD "):
            cur.head = line[5:]
        elif line.startswith("branch "):
            cur.branch = line[7:].removeprefix("refs/heads/")
        elif line == "detached":
            cur.detached = True
        elif line.startswith("prunable"):
            cur.prunable = True
        elif line.startswith("locked"):
            cur.locked = True
    return items


def dirty_count(path: str) -> int | None:
    out = git(path, "status", "--porcelain=v1", "--untracked-files=normal")
    return None if out is None else len([line for line in out.splitlines() if line.strip()])


def unpushed_count(path: str) -> int | None:
    """Commits reachable from HEAD that exist on no remote branch."""
    out = git(path, "rev-list", "--count", "HEAD", "--not", "--remotes")
    return int(out.strip()) if out and out.strip().isdigit() else None


def default_base(repo: str) -> str | None:
    out = git(repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    if out and out.strip():
        return out.strip()
    for ref in ("origin/main", "origin/master", "main", "master"):
        if git(repo, "rev-parse", "--verify", "--quiet", ref) is not None:
            return ref
    return None


def is_merged(path: str, base: str) -> bool | None:
    r = _run(path, "merge-base", "--is-ancestor", "HEAD", base)
    if r is None or r.returncode not in (0, 1):
        return None
    return r.returncode == 0


def upstream_gone(path: str) -> bool:
    """True when the branch tracked a remote branch that has since been deleted (typical after PR merge)."""
    out = git(path, "for-each-ref", "--format=%(upstream:track)", "refs/heads/" + (current_branch(path) or "-"))
    return bool(out and "[gone]" in out)


def current_branch(path: str) -> str | None:
    out = git(path, "symbolic-ref", "--short", "-q", "HEAD")
    return out.strip() if out and out.strip() else None


def fingerprint(path: str) -> str | None:
    """Refs + HEAD + working-tree status; equal fingerprints mean equivalent repos."""
    refs = git(path, "for-each-ref", "--format=%(refname) %(objectname)")
    head = git(path, "rev-parse", "HEAD")
    status = git(path, "status", "--porcelain=v1")
    if refs is None or head is None or status is None:
        return None
    return f"{head.strip()}\n{refs}\n{status}"
