"""Single read-only walk of the scan roots shared by all scanners."""

from __future__ import annotations

import os
import stat
import sys
import time
from dataclasses import dataclass, field

ALWAYS_DEP = {
    "node_modules": "node_modules",
    ".next": "build-cache",
    ".nuxt": "build-cache",
    ".turbo": "build-cache",
    ".parcel-cache": "build-cache",
    ".svelte-kit": "build-cache",
    ".angular": "build-cache",
    ".expo": "build-cache",
    ".dart_tool": "build-cache",
    ".gradle": "build-cache",
    ".terraform": "terraform-providers",
    ".tox": "tool-cache",
    ".mypy_cache": "tool-cache",
    ".pytest_cache": "tool-cache",
    ".ruff_cache": "tool-cache",
}
SKIP_DIRS = {".git", ".Trash", ".Spotlight-V100", ".fseventsd"}
OPAQUE_SUFFIXES = (".photoslibrary", ".musiclibrary", ".tvlibrary", ".app", ".fcpbundle", ".imovielibrary", ".xcarchive")
IGNORED_FILES = {".DS_Store"}
PROJECT_MARKERS = {
    "package.json", "pyproject.toml", "requirements.txt", "setup.py", "Cargo.toml", "go.mod", "Gemfile",
    "pom.xml", "build.gradle", "build.gradle.kts", "composer.json", "docker-compose.yml", "compose.yaml",
    "Makefile", "pubspec.yaml", "Podfile", "Package.swift", "mix.exs", "deno.json",
}
PROJECT_SUFFIXES = (".csproj", ".sln", ".fsproj", ".xcodeproj", ".tf")


def dep_kind(name: str, path: str, siblings: set[str]) -> str | None:
    """Classify a directory as regenerable dependencies/build output, or None."""
    if name in ALWAYS_DEP:
        return ALWAYS_DEP[name]
    if name in (".venv", "venv", "env") and os.path.exists(os.path.join(path, "pyvenv.cfg")):
        return "python-venv"
    if name == "target" and {"Cargo.toml", "pom.xml"} & siblings:
        return "build-output"
    if name == "Pods" and "Podfile" in siblings:
        return "cocoapods"
    if name == "build" and {"build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts", "pubspec.yaml"} & siblings:
        return "build-output"
    if name in ("bin", "obj") and any(s.endswith((".csproj", ".fsproj", ".vbproj")) for s in siblings):
        return "build-output"
    return None


@dataclass(slots=True)
class FileRec:
    name: str
    size: int
    blocks: int
    mtime: float
    dev: int
    ino: int
    nlink: int


@dataclass
class DirNode:
    files: list[FileRec] = field(default_factory=list)
    subdirs: list[str] = field(default_factory=list)
    own_newest: float = 0.0
    git_kind: str | None = None  # "main" (.git dir) | "linked" (.git file) | None
    linked: bool = False  # inside a linked git worktree: walked for activity, excluded from content scans


@dataclass
class DepDir:
    path: str
    kind: str
    parent: str


@dataclass
class Inventory:
    roots: list[str]
    dirs: dict[str, DirNode] = field(default_factory=dict)
    dep_dirs: list[DepDir] = field(default_factory=list)
    git_roots: dict[str, str] = field(default_factory=dict)
    subtree_newest: dict[str, float] = field(default_factory=dict)
    errors: int = 0
    _owner_cache: dict[str, str | None] = field(default_factory=dict)
    _project_cache: dict[str, bool] = field(default_factory=dict)

    def in_project(self, path: str) -> bool:
        """True if path is inside a git repo or a folder with a project marker (package.json, *.tf, …)."""
        chain = []
        p = path
        result = False
        while any(p == r or p.startswith(r + os.sep) for r in self.roots):
            if p in self._project_cache:
                result = self._project_cache[p]
                break
            chain.append(p)
            n = self.dirs.get(p)
            if p in self.git_roots or (
                n and any(f.name in PROJECT_MARKERS or f.name.endswith(PROJECT_SUFFIXES) for f in n.files)
            ):
                result = True
                break
            parent = os.path.dirname(p)
            if parent == p:
                break
            p = parent
        for c in chain:
            self._project_cache[c] = result
        return result

    def owner_root(self, path: str) -> str | None:
        """Nearest enclosing git root (including path itself)."""
        chain = []
        p = path
        result: str | None = None
        while True:
            if p in self._owner_cache:
                result = self._owner_cache[p]
                break
            chain.append(p)
            if p in self.git_roots:
                result = p
                break
            parent = os.path.dirname(p)
            if parent == p or not any(p.startswith(r) for r in self.roots):
                break
            p = parent
        for c in chain:
            self._owner_cache[c] = result
        return result


def build(roots: list[str], log=None) -> Inventory:
    inv = Inventory(roots=roots)
    stack: list[tuple[str, bool]] = [(r, False) for r in roots]
    started = time.time()
    while stack:
        d, in_linked = stack.pop()
        try:
            with os.scandir(d) as it:
                entries = list(it)
        except OSError:
            inv.errors += 1
            continue
        names = {e.name for e in entries}
        node = DirNode()
        if ".git" in names:
            gpath = os.path.join(d, ".git")
            try:
                node.git_kind = "main" if stat.S_ISDIR(os.lstat(gpath).st_mode) else "linked"
            except OSError:
                node.git_kind = None
            if node.git_kind:
                inv.git_roots[d] = node.git_kind
            if node.git_kind == "linked":
                in_linked = True
        node.linked = in_linked
        newest = 0.0
        for e in entries:
            try:
                if e.is_dir(follow_symlinks=False):
                    if e.name in SKIP_DIRS or e.name.endswith(OPAQUE_SUFFIXES):
                        continue
                    kind = dep_kind(e.name, e.path, names)
                    if kind:
                        inv.dep_dirs.append(DepDir(e.path, kind, d))
                        continue
                    node.subdirs.append(e.name)
                    stack.append((e.path, in_linked))
                elif e.is_file(follow_symlinks=False):
                    if e.name in IGNORED_FILES:
                        continue
                    st = e.stat(follow_symlinks=False)
                    newest = max(newest, st.st_mtime)
                    if not in_linked:
                        node.files.append(
                            FileRec(e.name, st.st_size, st.st_blocks * 512, st.st_mtime, st.st_dev, st.st_ino, st.st_nlink)
                        )
            except OSError:
                inv.errors += 1
        node.own_newest = newest
        inv.dirs[d] = node
        if log and len(inv.dirs) % 50000 == 0:
            log(f"  walked {len(inv.dirs):,} folders ({time.time() - started:.0f}s)")

    for p in sorted(inv.dirs, key=lambda x: x.count(os.sep), reverse=True):
        n = inv.dirs[p]
        v = n.own_newest
        for s in n.subdirs:
            c = os.path.join(p, s)
            cn = inv.dirs.get(c)
            if cn is None or cn.git_kind:
                continue
            v = max(v, inv.subtree_newest.get(c, 0.0))
        inv.subtree_newest[p] = v
    return inv


def newest_mtime(path: str) -> float:
    """Newest file mtime under path, ignoring dependency folders and .git (for paths outside the inventory)."""
    newest = 0.0
    stack = [path]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                entries = list(it)
        except OSError:
            continue
        names = {e.name for e in entries}
        for e in entries:
            try:
                if e.is_dir(follow_symlinks=False):
                    if e.name in SKIP_DIRS or dep_kind(e.name, e.path, names):
                        continue
                    stack.append(e.path)
                elif e.is_file(follow_symlinks=False) and e.name not in IGNORED_FILES:
                    newest = max(newest, e.stat(follow_symlinks=False).st_mtime)
            except OSError:
                pass
    return newest


def stderr_log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)
