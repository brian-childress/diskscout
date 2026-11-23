"""Apply selected report findings.

Safety model:
- Dry run unless --yes.
- Every finding is re-checked against the live system before acting (protected runs,
  project activity, git cleanliness, running apps).
- Files are moved (renamed) into a Trash staging folder, never deleted. Space is freed only
  by `purge`, which also runs deferred cleanup commands (e.g. `git worktree prune`).
- Every action is recorded in a manifest; `restore` moves items back.
- Tool commands (pnpm store prune, xcrun simctl runtime delete, …) are not reversible;
  they only clear data the tool can re-download.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from datetime import datetime

from diskscout import gitutil
from diskscout.fsutil import human
from diskscout.inventory import newest_mtime
from diskscout.protect import Guard, find_v2c_runs
from diskscout.report import disk_usage

MANUAL_KINDS = {"review", "manual", "none"}

# Cache folder name -> process names that must not be running while it is emptied ([] = safe while running).
APP_PROCS = {
    "Google": ["Google Chrome"],
    "Firefox": ["firefox"],
    "Mozilla": ["firefox"],
    "BraveSoftware": ["Brave Browser"],
    "ru.keepcoder.Telegram": ["Telegram"],
    "us.zoom.xos": ["zoom.us"],
    "Comet": ["Comet"],
    "ollama": ["ollama", "Ollama"],
    "claude-cli-nodejs": ["claude"],
    "node-gyp": [],
    "ms-playwright": [],
    "ms-playwright-go": [],
    "SiriTTS": [],
    "GeoServices": [],
    "com.apple.python": [],
    "aws": [],
    "dotslash": [],
    "electron": [],
}
GENERIC_TOKENS = {"com", "org", "net", "io", "us", "ru", "app", "apple", "updater", "shipit", "cache", "caches", "desktop", "helper"}


# --- live checks ------------------------------------------------------------------


_procs_cache: list[str] | None = None


def _processes() -> list[str]:
    global _procs_cache
    if _procs_cache is None:
        try:
            out = subprocess.run(["ps", "-axo", "comm="], check=False, capture_output=True, text=True, timeout=30).stdout
        except (OSError, subprocess.TimeoutExpired):
            out = ""
        _procs_cache = [os.path.basename(line.strip()) for line in out.splitlines() if line.strip()]
    return _procs_cache


def running_app(cache_path: str, procs: list[str] | None = None) -> str | None:
    name = os.path.basename(cache_path.rstrip(os.sep))
    needles = APP_PROCS.get(name)
    if needles is None:
        tokens = [t for t in re.split(r"[.\-_@ ]+", name) if len(t) >= 4 and t.lower() not in GENERIC_TOKENS]
        reverse_dns = bool(re.match(r"^(com|org|net|io|us|ru)\.", name))
        needles = (tokens[-1:] if reverse_dns else tokens[:1]) if tokens else []
    for proc in procs if procs is not None else _processes():
        for n in needles:
            if n.lower() in proc.lower():
                return proc
    return None


_guards: dict[str, Guard] = {}


def live_guard(path: str) -> str | None:
    """Re-read v2c run state for every repo above path (runs may have started since the scan)."""
    p = os.path.dirname(path.rstrip(os.sep))
    while p and p != os.path.dirname(p):
        if os.path.isdir(os.path.join(p, ".agent", "v2c", "runs")):
            if p not in _guards:
                _guards[p] = Guard(find_v2c_runs(p), [])
            if reason := _guards[p].check(path):
                return reason
        p = os.path.dirname(p)
    return None


def recheck(f: dict, inactive_days: int, now: float, procs: list[str] | None = None) -> str | None:
    """Reason to skip this finding now, or None if it is still safe to apply."""
    a = f["action"]
    if f["risk"] == "protected":
        return "protected"
    if a["kind"] in MANUAL_KINDS:
        return f"manual step: {a.get('detail') or 'review'}"
    if a["kind"] == "command" and not a.get("command"):
        return "no runnable command in this report (re-scan with the current version)"
    paths = a.get("paths") or [f["path"]]
    for p in paths:
        if reason := live_guard(p):
            return reason
    if a["kind"] == "trash" and not any(os.path.lexists(p) for p in paths):
        return "already gone"
    cat = f["category"]
    cutoff = now - inactive_days * 86400
    if cat == "dependencies":
        project = f["evidence"].get("project") or f["path"]
        last = max(newest_mtime(project), float(gitutil.last_commit_ts(project) or 0) if os.path.exists(os.path.join(project, ".git")) else 0.0)
        if last >= cutoff:
            return f"project became active ({datetime.fromtimestamp(last):%Y-%m-%d})"
    if cat in ("worktrees", "duplicate-dirs", "backups"):
        for p in paths:
            if os.path.exists(os.path.join(p, ".git")):
                dirty, unpushed = gitutil.dirty_count(p), gitutil.unpushed_count(p)
                if dirty is None or unpushed is None or dirty or unpushed:
                    return f"{p} now has {dirty} uncommitted file(s) / {unpushed} unpushed commit(s)"
    if cat == "caches" and a["kind"] == "trash" and a.get("contents") and (app := running_app(f["path"], procs)):
        return f"{app} is running; quit it and re-run"
    return None


# --- execution --------------------------------------------------------------------


def _move(src: str, dst: str) -> None:
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    os.rename(src, dst)  # same-volume rename: instant, no copy; raises on cross-device


def _trash(f: dict, stage: str, moves: list[dict]) -> None:
    a = f["action"]
    for i, p in enumerate(a.get("paths") or [f["path"]]):
        if not os.path.lexists(p):
            continue
        base = os.path.join(stage, f["id"], str(i))
        if a.get("contents"):
            for child in sorted(os.listdir(p)):
                src = os.path.join(p, child)
                dst = os.path.join(base, os.path.basename(p), child)
                _move(src, dst)
                moves.append({"from": src, "to": dst})
        else:
            dst = os.path.join(base, os.path.basename(p))
            _move(p, dst)
            moves.append({"from": p, "to": dst})


def _execute(f: dict, stage: str) -> dict:
    a = f["action"]
    rec: dict = {"moves": []}
    if a["kind"] == "trash":
        _trash(f, stage, rec["moves"])
        rec["status"] = "staged"
        return rec
    argv = a["command"]
    if shutil.which(argv[0]) is None:
        if a.get("contents") and a.get("paths"):
            _trash(f, stage, rec["moves"])
            rec["status"] = "staged"
            rec["note"] = f"{argv[0]} not installed; moved cache contents to Trash instead"
            return rec
        rec["status"] = "failed"
        rec["note"] = f"{argv[0]} not installed"
        return rec
    try:
        r = subprocess.run(argv, check=False, capture_output=True, text=True, timeout=1800)
        rec["returncode"] = r.returncode
        rec["output"] = (r.stdout + r.stderr)[-2000:]
        rec["status"] = "done" if r.returncode == 0 else "failed"
    except (OSError, subprocess.TimeoutExpired) as e:
        rec["status"] = "failed"
        rec["note"] = str(e)
    return rec


def select(report: dict, categories: list[str], ids: list[str]) -> list[dict]:
    out = []
    for f in report["findings"]:
        if ids:
            if f.get("id") in ids:
                out.append(f)
        elif f["category"] in categories and f["recommended"]:
            out.append(f)
    return out


def apply(report_path: str, categories: list[str], ids: list[str], yes: bool, trash_dir: str | None, now: float | None = None) -> tuple[dict, str | None]:
    with open(report_path) as fh:
        report = json.load(fh)
    now = now or time.time()
    inactive_days = int(report.get("settings", {}).get("inactive_days", 30))
    chosen = select(report, categories, ids)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    stage = os.path.join(trash_dir or os.path.expanduser("~/.Trash"), f"diskscout-{stamp}")
    free_before = disk_usage()["free"]
    steps = []
    for f in sorted(chosen, key=lambda f: -f["reclaimable"]):
        step = {
            "id": f["id"],
            "category": f["category"],
            "title": f["title"],
            "path": f["path"],
            "estimated_bytes": f["reclaimable"],
            "action": f["action"],
        }
        reason = recheck(f, inactive_days, now)
        if reason:
            step["status"] = "skipped"
            step["note"] = reason
        elif not yes:
            step["status"] = "planned"
        else:
            try:
                step.update(_execute(f, stage))
            except OSError as e:
                step["status"] = "failed"
                step["note"] = str(e)
        steps.append(step)
    manifest = {
        "created": datetime.now().isoformat(timespec="seconds"),
        "report": os.path.abspath(report_path),
        "dry_run": not yes,
        "stage": stage,
        "free_before": free_before,
        "free_after": disk_usage()["free"],
        "steps": steps,
    }
    path = None
    if yes:
        path = os.path.join(os.path.dirname(os.path.abspath(report_path)), f"apply-{stamp}.json")
        with open(path, "w") as fh:
            json.dump(manifest, fh, indent=2)
    return manifest, path


def restore(manifest_path: str) -> list[str]:
    with open(manifest_path) as fh:
        m = json.load(fh)
    problems = []
    for step in m["steps"]:
        for mv in reversed(step.get("moves", [])):
            if mv.get("restored"):
                continue
            if os.path.lexists(mv["from"]):
                problems.append(f"exists, not overwritten: {mv['from']}")
                continue
            if not os.path.lexists(mv["to"]):
                problems.append(f"missing from Trash (purged?): {mv['to']}")
                continue
            _move(mv["to"], mv["from"])
            mv["restored"] = True
        if step.get("status") == "done":
            problems.append(f"not reversible (tool command): {step['title']}")
    with open(manifest_path, "w") as fh:
        json.dump(m, fh, indent=2)
    return problems


def _force_writable(func, path, exc) -> None:
    """rmtree error hook: staged caches can contain read-only folders (e.g. extracted .app bundles)."""
    if isinstance(exc, tuple):  # Python < 3.12 onerror passes exc_info
        exc = exc[1]
    if not isinstance(exc, PermissionError):
        raise exc
    for p in (os.path.dirname(path), path):
        try:
            os.chmod(p, stat.S_IRWXU)
        except OSError:
            pass
    if os.path.isdir(path) and not os.path.islink(path):
        _rmtree(path)
    else:
        os.unlink(path)


def _rmtree(path: str) -> None:
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_force_writable)
    else:
        shutil.rmtree(path, onerror=_force_writable)


def purge(manifest_path: str, yes: bool) -> tuple[int, list[str]]:
    """Permanently delete the staged items and run deferred cleanup commands. Returns (freed bytes, notes)."""
    with open(manifest_path) as fh:
        m = json.load(fh)
    stage = m["stage"]
    notes = []
    if not os.path.isdir(stage):
        return 0, [f"nothing staged at {stage}"]
    posts = []
    for step in m["steps"]:
        if step.get("moves") and not all(mv.get("restored") for mv in step["moves"]):
            posts.extend(step["action"].get("post") or [])
    if not yes:
        return 0, [f"would delete {stage}"] + [f"would run: {' '.join(p)}" for p in posts]
    before = disk_usage()["free"]
    _rmtree(stage)
    for argv in posts:
        r = subprocess.run(argv, check=False, capture_output=True, text=True)
        notes.append(f"{' '.join(argv)} -> {r.returncode}")
    m["purged"] = datetime.now().isoformat(timespec="seconds")
    with open(manifest_path, "w") as fh:
        json.dump(m, fh, indent=2)
    return disk_usage()["free"] - before, notes


def print_manifest(m: dict, home: str) -> None:
    def short(p: str) -> str:
        return "~" + p[len(home):] if p.startswith(home) else p

    total = 0
    for s in m["steps"]:
        a = s["action"]
        if s["status"] in ("planned", "staged", "done"):
            total += s["estimated_bytes"]
        what = " ".join(a["command"]) if a.get("command") and s.get("status") != "staged" else ("empty contents of" if a.get("contents") else "move to Trash")
        line = f"{s['status']:>8}  {human(s['estimated_bytes']):>9}  [{s['id']}] {s['category']}: {s['title']}  ({what})"
        print(line)
        if s.get("note"):
            print(f"{'':>20}↳ {s['note']}")
    verb = "Would free" if m["dry_run"] else "Freed/staged"
    print(f"\n{verb} ~{human(total)}. Free space now {human(m['free_after'])}.")
    if not m["dry_run"]:
        print(f"Staged items: {short(m['stage'])} (space returns after `diskscout purge` or emptying the Trash)")
