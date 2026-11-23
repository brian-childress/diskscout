"""Dependency and build folders, grouped per project and judged by project activity."""

from __future__ import annotations

import os
from collections import Counter

from diskscout import gitutil
from diskscout.fsutil import fmt_ts, measure
from diskscout.inventory import DepDir
from diskscout.models import Action, Finding
from diskscout.scanners import Context

LOCKFILES = ("package-lock.json", "pnpm-lock.yaml", "yarn.lock", "bun.lockb", "bun.lock", "poetry.lock", "uv.lock", "requirements.txt", "Cargo.lock", "Podfile.lock")


def scan(ctx: Context) -> list[Finding]:
    s, inv = ctx.settings, ctx.inv
    groups: dict[str, list[DepDir]] = {}
    for d in inv.dep_dirs:
        groups.setdefault(inv.owner_root(d.parent) or d.parent, []).append(d)

    all_paths = [d.path for d in inv.dep_dirs]
    sizes = dict(zip(all_paths, ctx.pool.map(measure, all_paths), strict=True))
    commits = dict(zip(groups, ctx.pool.map(lambda p: gitutil.last_commit_ts(p) if p in inv.git_roots else None, groups), strict=True))

    findings: list[Finding] = []
    for project, deps in groups.items():
        allocated = sum(sizes[d.path].allocated for d in deps)
        reclaimable = sum(sizes[d.path].reclaimable for d in deps)
        if allocated == 0:
            continue
        last_change = inv.subtree_newest.get(project, 0.0)
        last_commit = commits.get(project)
        last_activity = max(last_change, float(last_commit or 0))
        idle_days = int((s.now - last_activity) / 86400) if last_activity else None
        has_lock = any(os.path.exists(os.path.join(p, f)) for p in {project, *(d.parent for d in deps)} for f in LOCKFILES)
        protected = ctx.guard.check(project) or next((r for d in deps if (r := ctx.guard.check(d.path))), None)

        evidence = {
            "project": project,
            "git": inv.git_roots.get(project),
            "last_commit": fmt_ts(last_commit),
            "last_file_change": fmt_ts(last_change),
            "idle_days": idle_days,
            "kinds": dict(Counter(d.kind for d in deps)),
            "lockfile": has_lock,
            "folders": len(deps),
        }
        if protected:
            risk, rec, reason = "protected", False, protected
        elif last_activity >= s.inactive_cutoff:
            risk, rec = "low", False
            reason = f"Active project: last activity {idle_days}d ago (threshold {s.inactive_days}d)"
        else:
            risk, rec = ("low" if has_lock else "medium"), True
            when = "never" if idle_days is None else f"{idle_days}d"
            reason = f"Inactive {when}: no commits or file changes in {s.inactive_days}+ days; dependencies reinstall on demand"
            if not has_lock:
                reason += " (no lockfile found: reinstall may resolve newer versions)"
        findings.append(
            Finding(
                category="dependencies",
                title=os.path.basename(project) or project,
                path=project,
                allocated=allocated,
                reclaimable=reclaimable,
                risk=risk,
                recommended=rec,
                reason=reason,
                action=Action("trash", f"Move {len(deps)} dependency/build folder(s) to Trash", sorted(d.path for d in deps)),
                evidence=evidence,
            )
        )
    return findings
