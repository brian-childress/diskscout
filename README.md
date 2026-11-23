# diskscout

[![CI](https://github.com/brian-childress/diskscout/actions/workflows/ci.yml/badge.svg)](https://github.com/brian-childress/diskscout/actions/workflows/ci.yml)
![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-3776ab)
![Dependencies: none](https://img.shields.io/badge/dependencies-none-2ea44f)
![License: MIT](https://img.shields.io/badge/license-MIT-black)

**Find the disk space your developer Mac can safely give back.**

diskscout walks your projects and caches and tells you, with evidence, what is safe to reclaim: dependency folders in projects you stopped touching, git worktrees you already merged, package-manager caches, simulator runtimes, duplicate folders and `*-bak` copies. Scans never change anything. When you choose to act, files go to the Trash first and every step can be undone.

```text
$ diskscout scan
Walking 8 folder(s): ~/Documents, ~/Downloads, …
Inventory: 188,046 folders, 817 dependency folders, 120 git repos (114s)
Protecting active agent run 20261123-1146 in ~/code/app (stage: build)
Scanning worktrees…  34 findings
Scanning deps…       56 findings
Scanning caches…     55 findings
Scanning dups…       110 findings
Recommended: 67.9 GB in 65 findings (nothing was changed)
Report: ~/.local/state/diskscout/reports/20261123-111004/report.md
```

On the author's 460 GB MacBook, one `scan` + `apply --category caches,simulators` took free space from **17.6 GB to 55 GB**.

---

## Why another disk cleaner?

General-purpose cleaners see a 2.5 GB `node_modules` and either ignore it or delete it blindly. diskscout knows what developer folders *are*:

| It asks… | …before it recommends |
|---|---|
| Has this project had a commit **or** a file change in the last 30 days? | deleting its `node_modules`, `.venv`, `.next`, `target`, `.terraform`, `Pods`, `bin/obj` |
| Is this worktree clean, fully pushed, and merged or idle? | removing a linked git worktree |
| Is an agent run using this checkout right now? | touching anything inside it (never) |
| Is every file in `site-bak` also in `site`, byte for byte? | calling the backup redundant |
| Are these duplicate files real copies, or APFS clones that already share blocks? | counting the space as reclaimable |
| Is Chrome running? | emptying Chrome's cache |

## Install

Requires Python 3.10+ (the one that ships with Xcode Command Line Tools works). No dependencies.

```sh
# Recommended: isolated install with pipx
pipx install git+https://github.com/brian-childress/diskscout

# Or with pip
python3 -m pip install --user git+https://github.com/brian-childress/diskscout

# Or run from a clone without installing
git clone https://github.com/brian-childress/diskscout && cd diskscout
python3 -m diskscout --help
```

For complete coverage, give your terminal **Full Disk Access** (System Settings → Privacy & Security). Without it, some Library folders are skipped and counted as unreadable.

## Quick start

```sh
diskscout scan                                  # read-only; writes report.md + report.json
diskscout report --print | less                 # read the latest report

diskscout apply --category caches,simulators    # dry run: shows exactly what would happen
diskscout apply --category caches,simulators --yes

diskscout purge <manifest> --yes                # permanently free the staged space
diskscout restore <manifest>                    # …or put everything back
```

`apply` prints the manifest path it wrote; `restore` and `purge` take that path.

## What it finds

| Category | Examples | Recommended when | How it's applied |
|---|---|---|---|
| `worktrees` | linked git worktrees, stale worktree records, finished agent-run folders | clean, everything pushed, and idle 30+ days, or branch merged / remote branch deleted | Move to Trash; `git worktree prune` runs at purge |
| `dependencies` | `node_modules`, venvs, `.next`, `.turbo`, `.terraform`, `target`, `Pods`, .NET `bin`/`obj`, `.gradle` (grouped per project) | no commits **and** no file changes for 30+ days | Move to Trash |
| `caches` | pnpm, npm, bun, Yarn, pip, uv, Homebrew, CocoaPods, NuGet, Gradle, Go, Cargo, Playwright, browser and app caches | always (they regenerate) | The tool's own command (`pnpm store prune`, `brew cleanup`…) or empty the folder in place |
| `simulators` | iOS Simulator runtimes | unused for 30+ days | `xcrun simctl runtime delete` |
| `duplicate-dirs` | identical folder trees anywhere you scan | content-verified, outside project repos, and any nested git repos identical | Move copies to Trash, keep the canonical one |
| `backups` | `x-bak`, `x.old`, `x copy`, `x (1)` next to `x` | every file in the backup exists unchanged in the original | Move to Trash; otherwise lists what differs |
| `duplicate-files` | identical files ≥ 1 MB | outside projects and Music/Photos libraries, not APFS clones | Move copies to Trash |
| `large-files` | installers (`.dmg`, `.pkg`, `.iso`), big archives, files ≥ 500 MB | old installers only | Move to Trash |
| `app-data` | `~/Library/Application Support` | never (listed for review) | Manual |

Everything not recommended is still listed under **Needs review** with the reason, for example *"58 uncommitted files and 0 unpushed commits; push or discard before removing"*.

## Safety model

**Scanning is read-only.** It writes the report and nothing else. Git runs with `GIT_OPTIONAL_LOCKS=0`, so even `git status` leaves your index alone.

**Applying is conservative:**

1. **Dry run by default.** Nothing changes without `--yes`.
2. **Re-checked when it runs, not trusted from the report.** Each finding is re-validated: the project is still inactive, the worktree is still clean and pushed, no agent run has started, and the owning app isn't running.
3. **Trash first.** Files are renamed (same volume, instant) into `~/.Trash/diskscout-<timestamp>/`. Nothing is deleted until you run `purge` or empty the Trash.
4. **A manifest for every run.** `diskscout restore <manifest>` moves everything back.
5. **Irreversible steps are explicit.** Tool commands (`pnpm store prune`, `xcrun simctl runtime delete`) can't be undone. They only clear data the tool can download again, and the dry run labels them.

**Protected, always:**

- Paths you list with `--protect` or in the config file.
- Checkouts that belong to an **active AI-agent run**: any repo with `.agent/v2c/runs/*/run.json` whose `stage` isn't `done`. Finished runs are reported as reclaimable.
- Worktrees with uncommitted files or commits that exist on no remote (always flagged *high risk* and never recommended).

## Accurate numbers

Folder sizes lie on modern filesystems. diskscout:

- counts **hardlinks once**, and only as reclaimable when every link is inside the folder, so pnpm `node_modules` hardlinked into a global store isn't counted as free space.
- detects **APFS clones** for duplicate files via `getattrlist(ATTR_CMNEXT_PRIVATESIZE)`. Cloned copies already share storage, so they are reported as zero reclaimable.
- de-duplicates **nested findings** in totals (a worktree and the `node_modules` inside it count once).
- skips **zero-filled preallocated files** (database journals) that look identical but aren't duplicates.

## Configuration

Optional. Put this at `~/.config/diskscout/config.json` (or `$XDG_CONFIG_HOME/diskscout/config.json`). CLI flags override it. See [`examples/config.json`](examples/config.json).

```json
{
  "project_roots": ["~/code"],
  "personal_roots": ["~/Documents", "~/Downloads", "~/Desktop", "~/Movies"],
  "inactive_days": 30,
  "protect": ["~/code/keep-this-forever"]
}
```

| Key | Default |
|---|---|
| `project_roots` | `~/Documents/Projects`, `~/CascadeProjects` |
| `personal_roots` | `~/Documents`, `~/Downloads`, `~/Desktop`, `~/Music`, `~/Movies`, `~/Pictures`, `~/Dropbox` |
| `inactive_days` | `30` |
| `protect` | `[]` |
| `dup_min_size` / `dir_dup_min_size` / `large_file_min` | `1M` / `10M` / `500M` |
| `reports_dir` | `~/.local/state/diskscout/reports` |
| `workers` | `8` |

## Command reference

```text
diskscout scan     [--only worktrees,deps,caches,dups,large] [--project-root P]… [--personal-root P]…
                   [--inactive-days N] [--protect P]… [--out DIR] [--workers N]
diskscout report   [--print]
diskscout apply    [REPORT|latest] --category C[,C…] | --id ID… [--yes] [--trash-dir DIR]
diskscout restore  MANIFEST
diskscout purge    MANIFEST [--yes]
```

- `--category` selects **recommended** findings only.
- `--id` (shown in the report next to each item) selects any single finding, including ones that need review, after you've looked at it yourself. The same live safety checks still apply.

## Platform support

Built and tested on **macOS** (APFS clone detection, `xcrun simctl`, `~/Library/Caches`). The scanners for dependencies, worktrees, duplicates and large files also run on **Linux**; macOS-only checks are skipped there. CI runs on both.

## Development

```sh
git clone https://github.com/brian-childress/diskscout && cd diskscout
python3 -m unittest discover -s tests -v
python3 -m diskscout scan --only deps --project-root ~/code
```

See [CONTRIBUTING.md](CONTRIBUTING.md). Adding a cache location is usually a one-line change in [`diskscout/scanners/caches.py`](diskscout/scanners/caches.py).

## License

[MIT](LICENSE) © Brian Childress
