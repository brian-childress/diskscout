from __future__ import annotations

import argparse
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from diskscout import __version__, inventory, report
from diskscout.config import Settings, config_path, latest_report, load_config, reports_dir
from diskscout.fsutil import human, parse_size
from diskscout.protect import Guard, find_v2c_runs
from diskscout.scanners import Context, caches, deps, duplicates, large, worktrees

SCANNERS = {
    "worktrees": worktrees.scan,
    "deps": deps.scan,
    "caches": caches.scan,
    "dups": duplicates.scan,
    "large": large.scan,
}
CATEGORIES = "worktrees, dependencies, caches, simulators, duplicate-dirs, backups, duplicate-files, large-files"


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="diskscout",
        description="Find disk space a developer machine can safely give back. Scans are read-only; "
        "`apply` is a dry run unless --yes, and stages files in the Trash so they can be restored.",
    )
    p.add_argument("--version", action="version", version=f"diskscout {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="command")

    s = sub.add_parser("scan", help="Scan and write report.md + report.json (read-only)")
    s.add_argument("--out", help=f"Reports folder (default {reports_dir()})")
    s.add_argument("--only", default=",".join(SCANNERS), help=f"Comma list of scanners: {','.join(SCANNERS)}")
    s.add_argument("--project-root", action="append", help="Project root to scan (repeatable; replaces defaults)")
    s.add_argument("--personal-root", action="append", help="Personal folder for duplicate/large-file scans (repeatable; replaces defaults)")
    s.add_argument("--inactive-days", type=int, help="Days without commits or file changes before a project counts as inactive (default 30)")
    s.add_argument("--dup-min-size", help="Minimum file size for duplicate files (default 1M)")
    s.add_argument("--dir-dup-min-size", help="Minimum folder size for duplicate folders (default 10M)")
    s.add_argument("--workers", type=int, help="Parallel workers for sizing and hashing (default 8)")
    s.add_argument("--protect", action="append", default=[], help="Path that must never be proposed (repeatable)")

    r = sub.add_parser("report", help="Print the path (or contents) of the latest report")
    r.add_argument("--print", action="store_true", help="Print the Markdown report instead of its path")

    a = sub.add_parser("apply", help="Act on recommended findings (dry run unless --yes)")
    a.add_argument("report", nargs="?", default="latest", help="report.json path (default: latest report)")
    a.add_argument("--category", default="", help=f"Comma list of categories; only recommended findings are selected. One of: {CATEGORIES}")
    a.add_argument("--id", action="append", default=[], help="Finding id from the report (repeatable; may select findings that need review)")
    a.add_argument("--yes", action="store_true", help="Actually do it")
    a.add_argument("--trash-dir", help="Parent folder for staged items (default ~/.Trash)")

    rs = sub.add_parser("restore", help="Move staged items from an apply manifest back where they were")
    rs.add_argument("manifest")

    g = sub.add_parser("purge", help="Permanently delete staged items and run deferred cleanup (dry run unless --yes)")
    g.add_argument("manifest")
    g.add_argument("--yes", action="store_true")
    return p


def _resolve_report(arg: str) -> str:
    if arg != "latest":
        return arg
    found = latest_report()
    if not found:
        raise SystemExit("No reports yet. Run `diskscout scan` first.")
    return found


def _apply_cmd(args) -> int:
    from diskscout import apply as ap

    cats = [c.strip() for c in args.category.split(",") if c.strip()]
    if not cats and not args.id:
        raise SystemExit("Choose what to apply with --category and/or --id (see the report).")
    path = _resolve_report(args.report)
    m, manifest = ap.apply(path, cats, args.id, args.yes, args.trash_dir)
    if not m["steps"]:
        print("No matching findings.")
        return 0
    ap.print_manifest(m, os.path.expanduser("~"))
    if manifest:
        print(f"Manifest: {manifest}")
        print(f"Undo:     diskscout restore '{manifest}'")
        print(f"Free it:  diskscout purge '{manifest}' --yes")
    else:
        print("Dry run: nothing changed. Re-run with --yes to apply.")
    return 1 if any(s["status"] == "failed" for s in m["steps"]) else 0


def _scan_cmd(args) -> int:
    cfg = load_config()
    settings = Settings()
    for key in ("project_roots", "personal_roots", "protect"):
        if key in cfg:
            setattr(settings, key, [os.path.realpath(os.path.expanduser(p)) for p in cfg[key]])
    for key in ("inactive_days", "workers"):
        if key in cfg:
            setattr(settings, key, int(cfg[key]))
    for key in ("dup_min_size", "dir_dup_min_size", "large_file_min"):
        if key in cfg:
            setattr(settings, key, parse_size(str(cfg[key])))

    if args.project_root:
        settings.project_roots = [os.path.realpath(os.path.expanduser(p)) for p in args.project_root]
    if args.personal_root:
        settings.personal_roots = [os.path.realpath(os.path.expanduser(p)) for p in args.personal_root]
    if args.inactive_days is not None:
        settings.inactive_days = args.inactive_days
    if args.workers is not None:
        settings.workers = args.workers
    if args.dup_min_size:
        settings.dup_min_size = parse_size(args.dup_min_size)
    if args.dir_dup_min_size:
        settings.dir_dup_min_size = parse_size(args.dir_dup_min_size)
    settings.protect = settings.protect + [os.path.realpath(os.path.expanduser(p)) for p in args.protect]

    only = [x.strip() for x in args.only.split(",") if x.strip()]
    unknown = set(only) - set(SCANNERS)
    if unknown:
        raise SystemExit(f"Unknown scanner(s): {', '.join(sorted(unknown))}. Choose from {', '.join(SCANNERS)}.")

    log = inventory.stderr_log
    started = time.time()
    if os.path.exists(config_path()):
        log(f"Config: {config_path()}")
    roots = settings.scan_roots() if set(only) - {"caches"} else []
    if roots:
        log(f"Walking {len(roots)} folder(s): {', '.join(roots)}")
    inv = inventory.build(roots, log)
    if roots:
        log(f"Inventory: {len(inv.dirs):,} folders, {len(inv.dep_dirs):,} dependency folders, {len(inv.git_roots):,} git repos ({time.time() - started:.0f}s)")

    runs = [r for repo, kind in inv.git_roots.items() if kind == "main" for r in find_v2c_runs(repo)]
    for r in runs:
        if r.active:
            log(f"Protecting active agent run {r.run_id} in {r.repo} (stage: {r.stage})")
    guard = Guard(runs, settings.protect)

    findings = []
    with ThreadPoolExecutor(max_workers=settings.workers) as pool:
        ctx = Context(settings, inv, guard, runs, pool, log)
        for name in only:
            t = time.time()
            log(f"Scanning {name}…")
            got = SCANNERS[name](ctx)
            findings.extend(got)
            log(f"  {name}: {len(got)} findings ({time.time() - t:.0f}s)")

    out_dir = os.path.join(os.path.expanduser(args.out or cfg.get("reports_dir") or reports_dir()), datetime.now().strftime("%Y%m%d-%H%M%S"))
    meta = {"seconds": time.time() - started, "errors": inv.errors, "roots": roots, "scanners": only}
    json_path, md_path = report.write(findings, settings, out_dir, meta)
    rec = [f for f in findings if f.recommended]
    print(f"Recommended: {human(report.dedupe_total(rec))} in {len(rec)} findings (nothing was changed)")
    print(f"Report: {md_path}")
    print(f"Data:   {json_path}")
    print("Next:   diskscout apply --category caches   # dry run")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.cmd == "scan":
        return _scan_cmd(args)
    if args.cmd == "report":
        path = _resolve_report("latest")
        md = os.path.join(os.path.dirname(path), "report.md")
        if args.print:
            with open(md) as fh:
                sys.stdout.write(fh.read())
        else:
            print(md)
        return 0
    if args.cmd == "apply":
        return _apply_cmd(args)

    from diskscout import apply as ap

    if args.cmd == "restore":
        problems = ap.restore(args.manifest)
        print("Restored." if not problems else "Restored, with notes:")
        for p in problems:
            print(f"  - {p}")
        return 0
    freed, notes = ap.purge(args.manifest, args.yes)
    for n in notes:
        print(n)
    print(f"Freed {human(freed)}." if args.yes else "Dry run: re-run with --yes to purge.")
    return 0
