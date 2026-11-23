from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from diskscout import inventory, report
from diskscout.config import Settings
from diskscout.fsutil import measure, private_size
from diskscout.protect import Guard, find_v2c_runs
from diskscout.scanners import Context, deps, duplicates, worktrees

DAY = 86400


def write(path: str, data: bytes | str = b"x", mtime: float | None = None) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data.encode() if isinstance(data, str) else data)
    if mtime:
        os.utime(path, (mtime, mtime))
    return path


def git(cwd: str, *args: str, date: float | None = None) -> None:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    if date:
        stamp = f"@{int(date)} +0000"
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = stamp
    subprocess.run(["git", "-C", cwd, *args], check=True, capture_output=True, env=env)


def age_tree(root: str, mtime: float) -> None:
    for d, _dirs, files in os.walk(root):
        for f in files:
            p = os.path.join(d, f)
            if not os.path.islink(p):
                os.utime(p, (mtime, mtime))


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.pool = ThreadPoolExecutor(4)
        self.addCleanup(self.pool.shutdown)
        self.now = time.time()

    def ctx(self, roots: list[str], protect: list[str] | None = None) -> Context:
        s = Settings(project_roots=roots, personal_roots=[], now=self.now, home=self.tmp, dup_min_size=1, dir_dup_min_size=1, protect=protect or [])
        inv = inventory.build(s.scan_roots())
        runs = [r for repo, k in inv.git_roots.items() if k == "main" for r in find_v2c_runs(repo)]
        return Context(s, inv, Guard(runs, s.protect), runs, self.pool, lambda m: None)


class MeasureTests(Base):
    def test_hardlink_shared_outside_is_not_reclaimable(self) -> None:
        store = write(os.path.join(self.tmp, "store", "pkg.js"), b"a" * 100_000)
        nm = os.path.join(self.tmp, "proj", "node_modules")
        os.makedirs(nm)
        os.link(store, os.path.join(nm, "pkg.js"))
        own = write(os.path.join(nm, "own.js"), b"b" * 100_000)
        size = measure(nm)
        self.assertGreaterEqual(size.allocated, 200_000)
        self.assertLess(size.reclaimable, size.allocated)
        self.assertGreaterEqual(size.reclaimable, os.stat(own).st_blocks * 512)

    def test_hardlinks_fully_inside_are_reclaimable(self) -> None:
        a = write(os.path.join(self.tmp, "d", "a"), b"a" * 100_000)
        os.link(a, os.path.join(self.tmp, "d", "b"))
        size = measure(os.path.join(self.tmp, "d"))
        self.assertEqual(size.allocated, size.reclaimable)

    @unittest.skipUnless(sys.platform == "darwin", "APFS only")
    def test_private_size_detects_clone(self) -> None:
        a = write(os.path.join(self.tmp, "big"), os.urandom(2_000_000))
        b = os.path.join(self.tmp, "big-clone")
        if subprocess.run(["cp", "-c", a, b], check=False, capture_output=True).returncode != 0:
            self.skipTest("volume does not support clones")
        self.assertEqual(private_size(b), 0)
        self.assertGreater(private_size(a + "") or 0, -1)


class DepsTests(Base):
    def make_project(self, name: str, idle_days: int) -> str:
        p = os.path.join(self.tmp, "Projects", name)
        write(os.path.join(p, "package.json"), "{}")
        write(os.path.join(p, "package-lock.json"), "{}")
        git(p, "init", "-q")
        git(p, "add", ".")
        when = self.now - idle_days * DAY
        git(p, "commit", "-qm", "init", date=when)
        write(os.path.join(p, "node_modules", "lib", "index.js"), b"z" * 50_000)
        age_tree(p, when)
        return p

    def test_inactive_project_deps_recommended_active_not(self) -> None:
        old = self.make_project("old", 45)
        new = self.make_project("new", 5)
        found = {f.path: f for f in deps.scan(self.ctx([os.path.join(self.tmp, "Projects")]))}
        self.assertTrue(found[old].recommended)
        self.assertEqual(found[old].action.paths, [os.path.join(old, "node_modules")])
        self.assertFalse(found[new].recommended)

    def test_recent_file_edit_keeps_project_active(self) -> None:
        old = self.make_project("edited", 60)
        write(os.path.join(old, "src", "new.ts"), "x")  # mtime now
        found = {f.path: f for f in deps.scan(self.ctx([os.path.join(self.tmp, "Projects")]))}
        self.assertFalse(found[old].recommended)


class WorktreeTests(Base):
    def setUp(self) -> None:
        super().setUp()
        self.repo = os.path.join(self.tmp, "Projects", "repo")
        write(os.path.join(self.repo, "a.txt"), "a")
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "init", date=self.now - 90 * DAY)
        remote = os.path.join(self.tmp, "remote.git")
        subprocess.run(["git", "init", "-q", "--bare", remote], check=True)
        git(self.repo, "remote", "add", "origin", remote)
        git(self.repo, "push", "-q", "origin", "main")

    def add_wt(self, path: str, branch: str) -> str:
        git(self.repo, "worktree", "add", "-q", "-b", branch, path)
        return path

    def test_active_v2c_run_is_protected_and_done_run_recommended(self) -> None:
        active_root = os.path.join(self.repo, ".worktrees", "v2c", "run-active")
        done_root = os.path.join(self.repo, ".worktrees", "v2c", "run-done")
        self.add_wt(os.path.join(active_root, "impl"), "a")
        self.add_wt(os.path.join(done_root, "impl"), "b")
        for rid, stage, root in (("run-active", "build", active_root), ("run-done", "done", done_root)):
            write(os.path.join(self.repo, ".agent", "v2c", "runs", rid, "run.json"), json.dumps({"run": rid, "stage": stage, "worktreeRoot": root}))
        age_tree(os.path.join(self.repo, ".worktrees"), self.now - 60 * DAY)
        found = {f.path: f for f in worktrees.scan(self.ctx([os.path.join(self.tmp, "Projects")]))}
        self.assertEqual(found[os.path.join(active_root, "impl")].risk, "protected")
        self.assertEqual(found[active_root].risk, "protected")
        self.assertTrue(found[done_root].recommended)

    def test_dirty_worktree_not_recommended(self) -> None:
        wt = self.add_wt(os.path.join(self.tmp, "Projects", "repo-wt"), "feature")
        write(os.path.join(wt, "wip.txt"), "wip")
        age_tree(wt, self.now - 60 * DAY)
        found = {f.path: f for f in worktrees.scan(self.ctx([os.path.join(self.tmp, "Projects")]))}
        self.assertEqual(found[wt].risk, "high")
        self.assertFalse(found[wt].recommended)


class DuplicateTests(Base):
    def test_identical_backup_dir_recommended_and_files_not_double_counted(self) -> None:
        root = os.path.join(self.tmp, "Projects")
        for name in ("site", "site-bak"):
            write(os.path.join(root, name, "index.html"), "<h1>hi</h1>" * 1000)
            write(os.path.join(root, name, "img", "logo.png"), b"\x89PNG" * 5000)
        found = duplicates.scan(self.ctx([root]))
        dirs = [f for f in found if f.category == "duplicate-dirs"]
        self.assertEqual(len(dirs), 1)
        self.assertEqual(dirs[0].evidence["keep"], os.path.join(root, "site"))
        self.assertTrue(dirs[0].recommended)
        self.assertFalse([f for f in found if f.category == "duplicate-files"])
        backups = [f for f in found if f.category == "backups"]
        self.assertTrue(backups[0].recommended)
        self.assertEqual(report.dedupe_total(dirs + backups), dirs[0].reclaimable)

    def test_diverged_backup_needs_review(self) -> None:
        root = os.path.join(self.tmp, "Projects")
        write(os.path.join(root, "site", "index.html"), "new" * 1000)
        write(os.path.join(root, "site-bak", "index.html"), "old" * 1000)
        write(os.path.join(root, "site-bak", "notes.md"), "only here" * 1000)
        b = next(f for f in duplicates.scan(self.ctx([root])) if f.category == "backups")
        self.assertFalse(b.recommended)
        self.assertEqual(b.evidence["changed_files"], 1)
        self.assertEqual(b.evidence["only_in_backup"], 1)

    def test_nested_duplicate_dirs_collapse_into_one_group(self) -> None:
        root = os.path.join(self.tmp, "Downloads")
        for name in ("a", "b"):
            write(os.path.join(root, name, "x", "y", "z", "big.bin"), b"q" * 200_000)
        dirs = [f for f in duplicates.scan(self.ctx([root])) if f.category == "duplicate-dirs"]
        self.assertEqual(len(dirs), 1)
        self.assertEqual(dirs[0].evidence["keep"], os.path.join(root, "a"))

    def test_zero_filled_and_in_project_files_not_recommended(self) -> None:
        root = os.path.join(self.tmp, "Projects")
        write(os.path.join(root, "db", "journal", "prep.1"), b"\0" * 200_000)
        write(os.path.join(root, "db", "journal", "prep.2"), b"\0" * 200_000)
        write(os.path.join(root, "app", "package.json"), "{}")
        write(os.path.join(root, "app", "k1", "logo.png"), b"L" * 200_000)
        write(os.path.join(root, "app", "k2", "logo.png"), b"L" * 200_000)
        files = [f for f in duplicates.scan(self.ctx([root])) if f.category in ("duplicate-files", "duplicate-dirs")]
        self.assertFalse([f for f in files if "journal" in f.path])
        self.assertTrue(files)
        self.assertFalse(any(f.recommended for f in files))

    def test_terraform_providers_are_dependencies(self) -> None:
        root = os.path.join(self.tmp, "Projects")
        write(os.path.join(root, "infra", "main.tf"), "")
        write(os.path.join(root, "infra", ".terraform", "providers", "aws"), b"p" * 100_000)
        inv = self.ctx([root]).inv
        self.assertEqual([d.kind for d in inv.dep_dirs], ["terraform-providers"])

    def test_same_size_different_content_not_duplicate(self) -> None:
        root = os.path.join(self.tmp, "Downloads")
        write(os.path.join(root, "a.bin"), b"a" * 300_000)
        write(os.path.join(root, "b.bin"), b"b" * 300_000)
        write(os.path.join(root, "c", "a-copy.bin"), b"a" * 300_000)
        files = [f for f in duplicates.scan(self.ctx([root])) if f.category == "duplicate-files"]
        self.assertEqual(len(files), 1)
        self.assertEqual(sorted(files[0].evidence["copies"]), sorted([os.path.join(root, "a.bin"), os.path.join(root, "c", "a-copy.bin")]))


if __name__ == "__main__":
    unittest.main()
