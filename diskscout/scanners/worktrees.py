"""Linked git worktrees and leftover v2c run folders."""

from __future__ import annotations

import os

from diskscout import gitutil
from diskscout.fsutil import fmt_ts, is_within, measure
from diskscout.inventory import newest_mtime
from diskscout.models import Action, Finding
from diskscout.scanners import Context


def _facts(ctx: Context, repo: str, base: str | None, wt: gitutil.Worktree) -> dict:
    inv = ctx.inv
    last_change = inv.subtree_newest.get(wt.path)
    if last_change is None:
        last_change = newest_mtime(wt.path)
    size = measure(wt.path)
    return {
        "size": size,
        "last_commit": gitutil.last_commit_ts(wt.path),
        "last_change": last_change,
        "dirty": gitutil.dirty_count(wt.path),
        "unpushed": gitutil.unpushed_count(wt.path),
        "merged": gitutil.is_merged(wt.path, base) if base else None,
        "upstream_gone": gitutil.upstream_gone(wt.path),
    }


def scan(ctx: Context) -> list[Finding]:
    s = ctx.settings
    findings: list[Finding] = []
    jobs = []
    for repo, kind in ctx.inv.git_roots.items():
        if kind != "main":
            continue
        entries = gitutil.worktree_list(repo)
        if len(entries) <= 1:
            continue
        base = gitutil.default_base(repo)
        for wt in entries[1:]:
            if wt.prunable or not os.path.isdir(wt.path):
                findings.append(
                    Finding(
                        "worktrees", f"stale worktree record: {os.path.basename(wt.path)}", wt.path, 0, 0, "low", True,
                        "Worktree folder no longer exists; git still tracks it",
                        Action("command", f"git -C '{repo}' worktree prune", command=["git", "-C", repo, "worktree", "prune"]),
                        {"repo": repo, "branch": wt.branch},
                    )
                )
                continue
            jobs.append((repo, base, wt, ctx.pool.submit(_facts, ctx, repo, base, wt)))

    evaluated: dict[str, Finding] = {}
    for repo, base, wt, fut in jobs:
        f = fut.result()
        size = f["size"]
        last_activity = max(f["last_change"] or 0.0, float(f["last_commit"] or 0))
        idle = int((s.now - last_activity) / 86400) if last_activity else None
        dirty, unpushed = f["dirty"], f["unpushed"]
        evidence = {
            "repo": repo,
            "branch": wt.branch or ("(detached)" if wt.detached else None),
            "base": base,
            "last_commit": fmt_ts(f["last_commit"]),
            "last_file_change": fmt_ts(f["last_change"]),
            "idle_days": idle,
            "uncommitted_files": dirty,
            "unpushed_commits": unpushed,
            "merged_into_base": f["merged"],
            "upstream_deleted": f["upstream_gone"],
            "locked": wt.locked,
        }
        protected = ctx.guard.check(wt.path)
        if protected:
            risk, rec, reason = "protected", False, protected
        elif wt.locked:
            risk, rec, reason = "high", False, "Worktree is locked"
        elif dirty is None or unpushed is None:
            risk, rec, reason = "high", False, "Could not read git state; review manually"
        elif dirty or unpushed:
            risk, rec = "high", False
            reason = f"Has {dirty} uncommitted file(s) and {unpushed} commit(s) on no remote; push or discard before removing"
        elif last_activity < s.inactive_cutoff:
            risk, rec = "low", True
            reason = f"Inactive {idle}d, clean, everything pushed"
        elif (f["merged"] or f["upstream_gone"]) and last_activity < s.recent_cutoff:
            risk, rec = "medium", True
            why = f"merged into {base}" if f["merged"] else "remote branch deleted (PR likely merged)"
            reason = f"Branch {why}; clean and pushed (last activity {idle}d ago)"
        else:
            risk, rec = "medium", False
            reason = f"In use: last activity {idle}d ago"
        finding = Finding(
            category="worktrees",
            title=f"{os.path.basename(repo)} ▸ {wt.branch or os.path.basename(wt.path)}",
            path=wt.path,
            allocated=size.allocated,
            reclaimable=size.reclaimable,
            risk=risk,
            recommended=rec,
            reason=reason,
            action=Action(
                "trash",
                f"Move worktree to Trash, then `git worktree prune` (branch {wt.branch} kept)",
                [wt.path],
                post=[["git", "-C", repo, "worktree", "prune"]],
            ),
            evidence=evidence,
        )
        evaluated[wt.path] = finding
        findings.append(finding)

    for run in ctx.runs:
        root = run.worktree_root
        if not root or not os.path.isdir(root):
            continue
        size = measure(root)
        inner = [f for p, f in evaluated.items() if is_within(p, root)]
        evidence = {"repo": run.repo, "run": run.run_id, "stage": run.stage, "registered_worktrees": len(inner)}
        if run.active:
            risk, rec, reason = "protected", False, f"Active v2c run {run.run_id} (stage: {run.stage})"
        elif any(f.risk == "high" for f in inner):
            risk, rec, reason = "high", False, f"v2c run {run.run_id} is {run.stage}, but some worktrees have unpushed or uncommitted work"
        else:
            risk, rec = "low", True
            reason = f"v2c run {run.run_id} finished (stage: {run.stage}); leftover worktrees and scratch copies"
        findings.append(
            Finding(
                "worktrees", f"v2c run folder {run.run_id}", root, size.allocated, size.reclaimable, risk, rec, reason,
                Action("trash", "Move run folder to Trash, then `git worktree prune`", [root], post=[["git", "-C", run.repo, "worktree", "prune"]]),
                evidence,
            )
        )
    return findings
