"""Package-manager caches, app caches, simulator runtimes, and app data (review only)."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
from datetime import datetime

from diskscout.fsutil import measure
from diskscout.models import Action, Finding
from diskscout.scanners import Context

# (title, path relative to home or absolute, action kind, action detail, risk, recommended)
KNOWN = [
    ("pnpm store", "Library/pnpm/store", "command", "pnpm store prune", "low", True),
    ("npm cache", ".npm/_cacache", "command", "npm cache clean --force", "low", True),
    ("bun install cache", ".bun/install/cache", "command", "bun pm cache rm", "low", True),
    ("Yarn cache", "Library/Caches/Yarn", "command", "yarn cache clean", "low", True),
    ("pnpm metadata cache", "Library/Caches/pnpm", "trash", "Trash folder; pnpm rebuilds it", "low", True),
    ("pip cache", "Library/Caches/pip", "command", "pip cache purge", "low", True),
    ("uv cache", ".cache/uv", "command", "uv cache clean", "low", True),
    ("Homebrew downloads", "Library/Caches/Homebrew", "command", "brew cleanup --prune=all", "low", True),
    ("CocoaPods cache", "Library/Caches/CocoaPods", "command", "pod cache clean --all", "low", True),
    ("SwiftPM cache", "Library/Caches/org.swift.swiftpm", "trash", "Trash folder; SwiftPM re-downloads", "low", True),
    ("Playwright browsers", "Library/Caches/ms-playwright", "trash", "Trash folder; `npx playwright install` restores", "low", True),
    ("Puppeteer browsers", ".cache/puppeteer", "trash", "Trash folder; re-downloads on next install", "low", True),
    ("Prisma engines", ".cache/prisma", "trash", "Trash folder; re-downloads on next generate", "low", True),
    ("NuGet packages", ".nuget/packages", "command", "dotnet nuget locals all --clear", "low", True),
    ("Gradle caches", ".gradle/caches", "trash", "Trash folder (stop Gradle daemons first)", "low", True),
    ("Maven repository", ".m2/repository", "trash", "Trash folder; re-downloads", "medium", True),
    ("Go module cache", "go/pkg/mod", "command", "go clean -modcache", "low", True),
    ("Cargo registry", ".cargo/registry", "trash", "Trash folder; re-downloads", "low", True),
    ("Chrome cache", "Library/Caches/Google", "trash", "Quit Chrome, then trash folder contents", "low", True),
    ("Firefox cache", "Library/Caches/Firefox", "trash", "Quit Firefox, then trash folder contents", "low", True),
    ("VS Code update cache", "Library/Caches/com.microsoft.VSCode.ShipIt", "trash", "Trash folder contents", "low", True),
    ("Xcode DerivedData", "Library/Developer/Xcode/DerivedData", "trash", "Trash folder contents", "low", True),
    ("iOS DeviceSupport", "Library/Developer/Xcode/iOS DeviceSupport", "trash", "Trash versions you no longer debug", "low", True),
    ("Xcode Archives", "Library/Developer/Xcode/Archives", "review", "Keep archives for shipped builds you may symbolicate", "medium", False),
    ("Simulator devices", "Library/Developer/CoreSimulator/Devices", "command", "xcrun simctl delete unavailable", "medium", False),
    ("CoreSimulator caches", "/Library/Developer/CoreSimulator/Caches", "review", "Rebuilt by Xcode as needed", "medium", False),
    ("Hugging Face models", ".cache/huggingface", "review", "Downloaded ML models; delete ones you no longer use", "medium", False),
    ("Trash", ".Trash", "command", "Empty Trash in Finder", "low", True),
]


def _action(kind: str, detail: str, path: str) -> Action:
    """Caches are emptied in place (contents moved out) so apps keep their folder."""
    if path.endswith(os.sep + ".Trash"):
        return Action("manual", detail)
    if kind == "command":
        return Action("command", detail, [path], command=shlex.split(detail), contents=True)
    if kind == "trash":
        return Action("trash", detail, [path], contents=True)
    return Action(kind, detail)


def _abs(home: str, p: str) -> str:
    return p if os.path.isabs(p) else os.path.join(home, p)


def _children(path: str) -> list[str]:
    try:
        return [e.path for e in os.scandir(path) if e.is_dir(follow_symlinks=False)]
    except OSError:
        return []


def _simulator_runtimes(ctx: Context) -> list[Finding]:
    if not shutil.which("xcrun"):
        return []
    try:
        out = subprocess.run(["xcrun", "simctl", "runtime", "list", "-j"], check=False, capture_output=True, text=True, timeout=60)
        data = json.loads(out.stdout or "{}")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return []
    findings = []
    s = ctx.settings
    for rid, rt in data.items():
        size = int(rt.get("sizeBytes") or 0)
        last = rt.get("lastUsedAt")
        last_ts = datetime.fromisoformat(last.replace("Z", "+00:00")).timestamp() if last else None
        name = f"{rt.get('runtimeIdentifier', '').split('.')[-1]} ({rt.get('build')})"
        if not rt.get("deletable", True):
            risk, rec, reason = "high", False, "Runtime is not deletable"
        elif last_ts and last_ts >= s.inactive_cutoff:
            risk, rec, reason = "medium", False, f"Used {last[:10]}"
        else:
            risk, rec = "medium", True
            reason = f"Not used since {last[:10]}" if last else "Never used by a simulator (keep it if you target this iOS version)"
        findings.append(
            Finding(
                "simulators", f"iOS Simulator runtime {name}", rt.get("path", rid), size, size, risk, rec, reason,
                Action("command", f"xcrun simctl runtime delete {rid}", command=["xcrun", "simctl", "runtime", "delete", rid]),
                {"version": rt.get("version"), "last_used": last, "state": rt.get("state")},
            )
        )
    return findings


def scan(ctx: Context) -> list[Finding]:
    s = ctx.settings
    home = s.home
    covered: set[str] = set()
    jobs = []
    for title, rel, kind, detail, risk, rec in KNOWN:
        path = _abs(home, rel)
        covered.add(path)
        if os.path.isdir(path):
            jobs.append((title, path, kind, detail, risk, rec, ctx.pool.submit(measure, path)))

    # Generic ~/Library/Caches and ~/.cache children not covered above.
    generic = []
    for parent, risk, rec, detail in (
        (os.path.join(home, "Library", "Caches"), "low", True, "Quit the app, then trash folder contents; apps rebuild caches"),
        (os.path.join(home, ".cache"), "medium", False, "Tool data under ~/.cache; check what owns it before deleting"),
    ):
        for child in _children(parent):
            if child in covered or any(c.startswith(child + os.sep) for c in covered):
                continue
            generic.append((os.path.basename(child), child, "trash" if rec else "review", detail, risk, rec, ctx.pool.submit(measure, child)))

    app_support = [
        (os.path.basename(c), c, "review", "App data, not cache. Remove via the app or uninstall it", "high", False, ctx.pool.submit(measure, c))
        for c in _children(os.path.join(home, "Library", "Application Support"))
    ]

    findings: list[Finding] = []
    for title, path, kind, detail, risk, rec, fut in jobs:
        size = fut.result()
        if size.allocated < 1024 * 1024:
            continue
        findings.append(Finding("caches", title, path, size.allocated, size.reclaimable, risk, rec, detail, _action(kind, detail, path), {"errors": size.errors}))
    for title, path, kind, detail, risk, rec, fut in generic:
        size = fut.result()
        if size.allocated >= s.cache_min:
            findings.append(Finding("caches", title, path, size.allocated, size.reclaimable, risk, rec, detail, _action(kind, detail, path), {}))
    for title, path, kind, detail, risk, rec, fut in app_support:
        size = fut.result()
        if size.allocated >= 5 * s.cache_min:
            findings.append(Finding("app-data", title, path, size.allocated, size.reclaimable, risk, rec, detail, Action(kind, detail), {}))
    findings.extend(_simulator_runtimes(ctx))
    return findings
