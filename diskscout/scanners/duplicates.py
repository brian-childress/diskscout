"""Duplicate directories, backup copies (*-bak etc.), and duplicate files.

Content scans skip dependency folders, .git internals, and linked git worktrees
(worktrees are handled by the worktrees scanner).
"""

from __future__ import annotations

import hashlib
import os
import re
from collections import defaultdict

from diskscout import gitutil
from diskscout.fsutil import file_hash, fmt_ts, is_within, measure, partial_hash, private_size
from diskscout.inventory import FileRec
from diskscout.models import Action, Finding
from diskscout.scanners import Context

BACKUP_RE = re.compile(
    r"^(?P<base>.+?)(?:[-_. ](?:bak|backup|old|orig|copy)\d*| copy(?: \d+)?| \(\d+\))$",
    re.IGNORECASE,
)
APP_MANAGED = ("Music/Music", "Pictures/Photos Library", "Movies/TV")


class _Hashes:
    def __init__(self, ctx: Context):
        self.ctx = ctx
        self.full: dict[tuple[int, int], str | None] = {}

    def many(self, items: list[tuple[str, FileRec]]) -> dict[str, str | None]:
        todo = {}
        for path, f in items:
            key = (f.dev, f.ino)
            if key not in self.full and key not in todo:
                todo[key] = path
        for key, h in zip(todo, self.ctx.pool.map(file_hash, todo.values()), strict=True):
            self.full[key] = h
        return {path: self.full[(f.dev, f.ino)] for path, f in items}


def _zero_filled(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            head = f.read(65536)
    except OSError:
        return False
    return bool(head) and not any(head)


def _keep_score(path: str, home: str) -> tuple:
    """Lower is better: prefer canonical locations over backups, Downloads, Desktop, copies."""
    rel = path.removeprefix(home)
    parts = rel.split(os.sep)
    backupish = sum(1 for p in parts if BACKUP_RE.match(p) or "backup" in p.lower() or "archive" in p.lower())
    transient = any(p in ("Downloads", "Desktop") for p in parts[:2])
    return (backupish, transient, len(parts), len(path), path)


def _walk_files(inv, root: str) -> list[tuple[str, FileRec]]:
    out = []
    stack = [root]
    while stack:
        d = stack.pop()
        n = inv.dirs.get(d)
        if n is None or n.linked:
            continue
        rel = os.path.relpath(d, root)
        for f in n.files:
            out.append((os.path.normpath(os.path.join(rel, f.name)), f))
        stack.extend(os.path.join(d, s) for s in n.subdirs)
    return out


def _git_equal(inv, members: list[str]) -> bool | None:
    """None if no git repos inside; True if every repo inside matches across members."""
    rels = None
    for m in members:
        r = sorted(os.path.relpath(g, m) for g in inv.git_roots if is_within(g, m))
        if rels is None:
            rels = r
        elif r != rels:
            return False
    if not rels:
        return None
    for rel in rels:
        fps = {gitutil.fingerprint(os.path.normpath(os.path.join(m, rel))) for m in members}
        if None in fps or len(fps) != 1:
            return False
    return True


def _dir_dups(ctx: Context, hashes: _Hashes) -> tuple[list[Finding], list[str]]:
    inv, s = ctx.inv, ctx.settings
    nodes = {p: n for p, n in inv.dirs.items() if not n.linked}
    sig: dict[str, str] = {}
    tsize: dict[str, int] = {}
    tcount: dict[str, int] = {}
    for p in sorted(nodes, key=lambda x: x.count(os.sep), reverse=True):
        n = nodes[p]
        items = [("f", f.name, f.size) for f in n.files]
        size = sum(f.size for f in n.files)
        count = len(n.files)
        for sd in n.subdirs:
            c = os.path.join(p, sd)
            if c in sig:
                items.append(("d", sd, sig[c]))
                size += tsize[c]
                count += tcount[c]
        h = hashlib.blake2b(digest_size=16)
        for it in sorted(items):
            h.update(repr(it).encode())
        sig[p], tsize[p], tcount[p] = h.hexdigest(), size, count

    by_sig: dict[str, list[str]] = defaultdict(list)
    for p, sg in sig.items():
        if tsize[p] >= s.dir_dup_min_size and tcount[p] > 0:
            by_sig[sg].append(p)
    # Largest first; on ties the shallowest folder wins so nested copies collapse into their parent group.
    candidates = sorted((g for g in by_sig.values() if len(g) > 1), key=lambda g: (-tsize[g[0]], g[0].count(os.sep)))

    chosen: list[str] = []
    findings: list[Finding] = []
    removals: list[str] = []
    for group in candidates:
        if all(any(is_within(m, c) for c in chosen) for m in group):
            continue
        # verify content
        by_content: dict[str, list[str]] = defaultdict(list)
        for m in group:
            files = _walk_files(inv, m)
            hs = hashes.many([(os.path.join(m, rel), f) for rel, f in files])
            content = hashlib.blake2b(digest_size=16)
            for rel, _ in sorted(files, key=lambda x: x[0]):
                content.update(f"{rel}\0{hs[os.path.join(m, rel)]}\n".encode())
            by_content[content.hexdigest()].append(m)
        for members in by_content.values():
            if len(members) < 2:
                continue
            chosen.extend(members)
            members = sorted(members, key=lambda m: _keep_score(m, s.home))
            keep, remove = members[0], members[1:]
            sizes = [measure(r) for r in remove]
            git_eq = _git_equal(inv, members)
            protected = next((r for m in remove if (r := ctx.guard.check(m))), None)
            removals.extend(remove)
            inside_repo = [m for m in remove if inv.in_project(os.path.dirname(m))]
            app_managed = [m for m in remove if any(f"{os.sep}{a}" in m for a in APP_MANAGED)]
            if protected:
                risk, rec, reason = "protected", False, protected
            elif app_managed:
                risk, rec, reason = "medium", False, "Managed by Music/Photos/TV; remove duplicates from inside the app"
            elif inside_repo:
                risk, rec, reason = "medium", False, "Copy is a folder inside a project repository; consolidate in the project rather than deleting"
            elif git_eq is False:
                risk, rec, reason = "medium", False, "Same files, but the git repos inside differ (history, branches or status); compare before removing"
            else:
                risk, rec = "low", True
                reason = f"{len(members)} identical copies ({tcount[keep]} files, content-verified)"
                if git_eq:
                    reason += "; git history also identical"
            findings.append(
                Finding(
                    "duplicate-dirs",
                    os.path.basename(keep),
                    remove[0],
                    sum(z.allocated for z in sizes),
                    sum(z.reclaimable for z in sizes),
                    risk,
                    rec,
                    reason,
                    Action("trash", f"Keep {keep}", remove),
                    {"keep": keep, "copies": members, "files": tcount[keep], "content_bytes": tsize[keep]},
                )
            )
    return findings, removals


def _backups(ctx: Context, hashes: _Hashes) -> list[Finding]:
    inv = ctx.inv
    findings = []
    for p, n in inv.dirs.items():
        if n.linked:
            continue
        name = os.path.basename(p)
        m = BACKUP_RE.match(name)
        if not m:
            continue
        original = os.path.join(os.path.dirname(p), m.group("base"))
        size = measure(p)
        if size.allocated < ctx.settings.dir_dup_min_size:
            continue
        if original not in inv.dirs:
            findings.append(
                Finding(
                    "backups", name, p, size.allocated, size.reclaimable, "medium", False,
                    "Backup-named folder with no original next to it; check whether it is still needed",
                    Action("review", "Open and decide"), {"last_file_change": fmt_ts(inv.subtree_newest.get(p))},
                )
            )
            continue
        bfiles = dict(_walk_files(inv, p))
        ofiles = dict(_walk_files(inv, original))
        common = [r for r in bfiles if r in ofiles and bfiles[r].size == ofiles[r].size]
        bh = hashes.many([(os.path.join(p, r), bfiles[r]) for r in common])
        oh = hashes.many([(os.path.join(original, r), ofiles[r]) for r in common])
        same = {r for r in common if bh[os.path.join(p, r)] and bh[os.path.join(p, r)] == oh[os.path.join(original, r)]}
        only_backup = sorted(r for r in bfiles if r not in ofiles)
        changed = sorted(r for r in bfiles if r in ofiles and r not in same)
        unique_bytes = sum(bfiles[r].size for r in only_backup + changed)
        evidence = {
            "original": original,
            "identical_files": len(same),
            "changed_files": len(changed),
            "only_in_backup": len(only_backup),
            "only_in_original": len([r for r in ofiles if r not in bfiles]),
            "unique_bytes_in_backup": unique_bytes,
            "examples_changed": changed[:15],
            "examples_only_in_backup": only_backup[:15],
            "backup_last_change": fmt_ts(inv.subtree_newest.get(p)),
            "original_last_change": fmt_ts(inv.subtree_newest.get(original)),
        }
        protected = ctx.guard.check(p)
        if protected:
            risk, rec, reason = "protected", False, protected
        elif not only_backup and not changed:
            risk, rec, reason = "low", True, f"Every file in the backup also exists unchanged in {os.path.basename(original)}"
        else:
            risk, rec = "medium", False
            reason = f"Backup has {len(changed)} changed and {len(only_backup)} extra file(s) not in the original; merge what matters, then remove"
        findings.append(Finding("backups", name, p, size.allocated, size.reclaimable, risk, rec, reason, Action("trash" if rec else "review", f"Compare with {original}", [p]), evidence))
    return findings


def _file_dups(ctx: Context, hashes: _Hashes, removed_dirs: list[str]) -> list[Finding]:
    inv, s = ctx.inv, ctx.settings
    removed_dirs = sorted(removed_dirs)
    by_size: dict[int, list[tuple[str, FileRec]]] = defaultdict(list)
    for d, n in inv.dirs.items():
        if n.linked or any(is_within(d, r) for r in removed_dirs):
            continue
        for f in n.files:
            if f.size >= s.dup_min_size:
                by_size[f.size].append((os.path.join(d, f.name), f))

    partial_groups: dict[str, list[tuple[str, FileRec]]] = defaultdict(list)
    sized = []
    for items in by_size.values():
        uniq = {}
        for path, f in items:
            uniq.setdefault((f.dev, f.ino), (path, f))  # hardlinks are one file, not duplicates
        if len(uniq) > 1:
            sized.extend(uniq.values())
    for (path, f), ph in zip(sized, ctx.pool.map(lambda x: partial_hash(x[0], x[1].size), sized), strict=True):
        if ph:
            partial_groups[ph].append((path, f))

    full_groups: dict[str, list[tuple[str, FileRec]]] = defaultdict(list)
    cands = [x for g in partial_groups.values() if len(g) > 1 for x in g]
    hs = hashes.many(cands)
    for path, f in cands:
        if hs[path]:
            full_groups[hs[path]].append((path, f))

    findings = []
    for digest, items in full_groups.items():
        if len(items) < 2 or _zero_filled(items[0][0]):
            continue  # preallocated/zero-filled files (DB journals, sparse images) are not real duplicates
        items.sort(key=lambda x: _keep_score(x[0], s.home))
        keep, remove = items[0], items[1:]
        reclaim = 0
        for path, f in remove:
            ps = private_size(path)
            reclaim += f.blocks if ps is None else min(ps, f.blocks)
        in_repo = [p for p, _ in remove if inv.in_project(os.path.dirname(p))]
        app_managed = [p for p, _ in remove if any(f"{os.sep}{a}" in p for a in APP_MANAGED)]
        protected = next((r for p, _ in remove if (r := ctx.guard.check(p))), None)
        if protected:
            risk, rec, reason = "protected", False, protected
        elif reclaim == 0:
            risk, rec, reason = "low", False, "Copies are APFS clones that already share storage; nothing to reclaim"
        elif app_managed:
            risk, rec, reason = "medium", False, "Managed by Music/Photos/TV; remove duplicates from inside the app"
        elif in_repo:
            risk, rec, reason = "medium", False, "Copies live inside project repositories; consolidate in the projects"
        else:
            risk, rec, reason = "low", True, f"{len(items)} identical copies (content hash verified)"
        findings.append(
            Finding(
                "duplicate-files",
                os.path.basename(keep[0]),
                remove[0][0],
                sum(f.blocks for _, f in remove),
                reclaim,
                risk,
                rec,
                reason,
                Action("trash", f"Keep {keep[0]}", [p for p, _ in remove]),
                {"keep": keep[0], "copies": [p for p, _ in items], "size_each": keep[1].size, "hash": digest},
            )
        )
    return findings


def scan(ctx: Context) -> list[Finding]:
    hashes = _Hashes(ctx)
    dir_findings, removed = _dir_dups(ctx, hashes)
    ctx.log(f"  duplicate folders: {len(dir_findings)} groups")
    backup_findings = _backups(ctx, hashes)
    ctx.log(f"  backups: {len(backup_findings)}")
    file_findings = _file_dups(ctx, hashes, removed)
    ctx.log(f"  duplicate files: {len(file_findings)} groups")
    return dir_findings + backup_findings + file_findings
