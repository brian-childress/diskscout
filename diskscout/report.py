"""JSON + Markdown report writers, with overlap-aware totals."""

from __future__ import annotations

import json
import os
import shutil
from collections import defaultdict
from datetime import datetime

from diskscout.config import Settings
from diskscout.fsutil import human, is_within
from diskscout.models import Finding

CATEGORY_ORDER = ["worktrees", "dependencies", "caches", "simulators", "duplicate-dirs", "backups", "duplicate-files", "large-files", "app-data"]


def dedupe_total(findings: list[Finding]) -> int:
    """Reclaimable bytes for a set of findings, not double-counting nested paths."""
    items = []
    for f in findings:
        for p in f.action.paths or [f.path]:
            items.append((p, f))
    items.sort(key=lambda x: len(x[0]))
    kept: list[str] = []
    total = 0
    counted: set[int] = set()
    for p, f in items:
        if any(is_within(p, k) for k in kept):
            continue
        kept.append(p)
        if id(f) not in counted:
            counted.add(id(f))
            total += f.reclaimable
    return total


def disk_usage() -> dict:
    target = "/System/Volumes/Data" if os.path.isdir("/System/Volumes/Data") else "/"
    u = shutil.disk_usage(target)
    return {"volume": target, "total": u.total, "used": u.used, "free": u.free}


def _short(path: str, home: str) -> str:
    return "~" + path[len(home):] if path.startswith(home) else path


def _cell(text: str) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def write(findings: list[Finding], settings: Settings, out_dir: str, meta: dict) -> tuple[str, str]:
    os.makedirs(out_dir, exist_ok=True)
    disk = disk_usage()
    rec = [f for f in findings if f.recommended]
    review = [f for f in findings if not f.recommended and f.risk != "protected"]
    protected = [f for f in findings if f.risk == "protected"]
    summary = {
        "recommended_reclaimable": dedupe_total(rec),
        "review_reclaimable": dedupe_total(review),
        "protected_bytes": dedupe_total(protected),
        "counts": {"recommended": len(rec), "review": len(review), "protected": len(protected)},
    }

    json_path = os.path.join(out_dir, "report.json")
    with open(json_path, "w") as fh:
        json.dump(
            {
                "generated": datetime.now().isoformat(timespec="seconds"),
                "disk": disk,
                "settings": {k: v for k, v in vars(settings).items() if k != "now"},
                "meta": meta,
                "summary": summary,
                "findings": [f.to_dict() for f in sorted(findings, key=lambda f: -f.reclaimable)],
            },
            fh,
            indent=2,
        )

    home = settings.home
    L: list[str] = []
    L.append(f"# diskscout report, {datetime.now():%Y-%m-%d %H:%M}")
    L.append("")
    pct = disk["used"] / disk["total"] * 100
    L.append(f"**Disk:** {human(disk['used'])} used of {human(disk['total'])} ({pct:.0f}%), **{human(disk['free'])} free**.  ")
    L.append(f"**Recommended (deduplicated):** {human(summary['recommended_reclaimable'])} across {len(rec)} findings.  ")
    L.append(f"**Needs your review:** {human(summary['review_reclaimable'])} across {len(review)} findings.  ")
    L.append(f"**Protected (never touched):** {human(summary['protected_bytes'])} across {len(protected)} findings.")
    L.append("")
    L.append(f"Inactive = no commits and no file changes for {settings.inactive_days}+ days. Scan took {meta.get('seconds', 0):.0f}s. Nothing was modified.")
    L.append("")

    L.append("## Summary by category")
    L.append("")
    L.append("| Category | Recommended | Reclaimable | Review | Review size |")
    L.append("|---|---:|---:|---:|---:|")
    by_cat = defaultdict(list)
    for f in findings:
        by_cat[f.category].append(f)
    for cat in CATEGORY_ORDER + sorted(set(by_cat) - set(CATEGORY_ORDER)):
        fs = by_cat.get(cat, [])
        if not fs:
            continue
        r = [f for f in fs if f.recommended]
        v = [f for f in fs if not f.recommended and f.risk != "protected"]
        L.append(f"| {cat} | {len(r)} | {human(dedupe_total(r))} | {len(v)} | {human(dedupe_total(v))} |")
    L.append("")

    def table(title: str, items: list[Finding], limit: int) -> None:
        L.append(f"## {title}")
        L.append("")
        if not items:
            L.append("_None._")
            L.append("")
            return
        L.append("| Size | Risk | Category | ID / Item | Path | Why | Action |")
        L.append("|---:|---|---|---|---|---|---|")
        for f in sorted(items, key=lambda f: -f.reclaimable)[:limit]:
            act = f.action.detail or f.action.kind
            if f.action.paths and len(f.action.paths) > 1:
                act += f" ({len(f.action.paths)} paths)"
            L.append(
                f"| {human(f.reclaimable)} | {f.risk} | {f.category} | `{f.id}` {_cell(f.title)} | `{_cell(_short(f.path, home))}` | {_cell(f.reason)} | {_cell(_short(act, home))} |"
            )
        if len(items) > limit:
            L.append(f"| | | | _…{len(items) - limit} more in report.json_ | | | |")
        L.append("")

    table("Recommended", rec, 60)
    table("Needs review", review, 60)
    table("Protected", protected, 30)

    dups = [f for f in findings if f.category in ("duplicate-dirs", "backups") and f.reclaimable > 0]
    if dups:
        L.append("## Duplicate folders and backups (detail)")
        L.append("")
        for f in sorted(dups, key=lambda f: -f.reclaimable)[:25]:
            L.append(f"### {f.title}: {human(f.reclaimable)} ({f.risk}{', recommended' if f.recommended else ''})")
            L.append(f"- {f.reason}")
            ev = f.evidence
            if "keep" in ev:
                L.append(f"- Keep: `{_short(ev['keep'], home)}`")
                for c in ev["copies"]:
                    if c != ev["keep"]:
                        L.append(f"- Copy: `{_short(c, home)}`")
            if "original" in ev:
                L.append(f"- Original: `{_short(ev['original'], home)}` (last change {ev['original_last_change']}); backup last change {ev['backup_last_change']}")
                L.append(f"- Identical: {ev['identical_files']}, changed: {ev['changed_files']}, only in backup: {ev['only_in_backup']} ({human(ev['unique_bytes_in_backup'])}), only in original: {ev['only_in_original']}")
                for r in ev["examples_changed"][:5]:
                    L.append(f"  - changed: `{r}`")
                for r in ev["examples_only_in_backup"][:5]:
                    L.append(f"  - only in backup: `{r}`")
            L.append("")

    L.append("## Notes")
    L.append("")
    L.append("- Sizes count hardlinked files once; `node_modules` that hardlink into a pnpm store free little on their own.")
    L.append("- Folder sizes do not detect APFS clones, so they can overstate what is freed. Duplicate-file sizes do account for clones.")
    L.append("- Linked git worktrees are excluded from duplicate scans (they are copies by design); see the worktrees category.")
    L.append(f"- Unreadable paths: {meta.get('errors', 0)}. Grant Full Disk Access to the terminal for complete coverage.")
    md_path = os.path.join(out_dir, "report.md")
    with open(md_path, "w") as fh:
        fh.write("\n".join(L) + "\n")
    return json_path, md_path
