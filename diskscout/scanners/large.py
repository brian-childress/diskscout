"""Old installers and very large files."""

from __future__ import annotations

import os

from diskscout.fsutil import fmt_ts
from diskscout.models import Action, Finding
from diskscout.scanners import Context

INSTALLERS = (".dmg", ".pkg", ".mpkg", ".iso", ".xip")
ARCHIVES = (".zip", ".tar", ".tgz", ".gz", ".7z", ".rar")


def scan(ctx: Context) -> list[Finding]:
    s, inv = ctx.settings, ctx.inv
    findings = []
    for d, n in inv.dirs.items():
        if n.linked:
            continue
        for f in n.files:
            if f.size < s.installer_min:
                continue
            path = os.path.join(d, f.name)
            lower = f.name.lower()
            age = int((s.now - f.mtime) / 86400)
            ev = {"modified": fmt_ts(f.mtime), "age_days": age, "size": f.size}
            protected = ctx.guard.check(path)
            if lower.endswith(INSTALLERS) and age >= s.inactive_days:
                risk, rec = ("protected", False) if protected else ("low", True)
                reason = protected or f"Installer image, {age}d old; re-download if needed"
                findings.append(Finding("large-files", f.name, path, f.blocks, f.blocks, risk, rec, reason, Action("trash", "", [path]), ev))
            elif lower.endswith(ARCHIVES) and age >= s.inactive_days and f.size >= 100 * 1024 * 1024:
                findings.append(Finding("large-files", f.name, path, f.blocks, f.blocks, "medium", False, f"Archive, {age}d old; delete if already extracted or backed up elsewhere", Action("review"), ev))
            elif f.size >= s.large_file_min:
                findings.append(Finding("large-files", f.name, path, f.blocks, f.blocks, "medium", False, f"Large file, last modified {age}d ago", Action("review"), ev))
    return findings
