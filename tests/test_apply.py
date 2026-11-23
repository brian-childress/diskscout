from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from diskscout import apply as ap
from diskscout import report
from diskscout.config import Settings
from diskscout.models import Action, Finding

DAY = 86400


def write(path: str, data: bytes = b"x" * 1000, mtime: float | None = None) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    if mtime:
        os.utime(path, (mtime, mtime))
    return path


class ApplyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.trash = os.path.join(self.tmp, "trash")
        ap._guards.clear()

    def make_report(self, findings: list[Finding]) -> str:
        _, md = report.write(findings, Settings(home=self.tmp), os.path.join(self.tmp, "rep"), {})
        return os.path.join(os.path.dirname(md), "report.json")

    def cache_finding(self, path: str, kind: str = "trash", command: list[str] | None = None) -> Finding:
        return Finding("caches", os.path.basename(path), path, 1000, 1000, "low", True, "cache",
                       Action(kind, "", [path], command=command, contents=True))

    def test_dry_run_changes_nothing(self) -> None:
        cache = os.path.join(self.tmp, "Caches", "SiriTTS")
        f = write(os.path.join(cache, "a.bin"))
        m, path = ap.apply(self.make_report([self.cache_finding(cache)]), ["caches"], [], False, self.trash)
        self.assertEqual(m["steps"][0]["status"], "planned")
        self.assertIsNone(path)
        self.assertTrue(os.path.exists(f))
        self.assertFalse(os.path.exists(self.trash))

    def test_apply_empties_cache_in_place_then_restore_and_purge(self) -> None:
        cache = os.path.join(self.tmp, "Caches", "SiriTTS")
        f = write(os.path.join(cache, "sub", "a.bin"))
        rep = self.make_report([self.cache_finding(cache)])
        m, manifest = ap.apply(rep, ["caches"], [], True, self.trash)
        self.assertEqual(m["steps"][0]["status"], "staged")
        self.assertTrue(os.path.isdir(cache))
        self.assertEqual(os.listdir(cache), [])
        self.assertEqual(ap.restore(manifest), [])
        self.assertTrue(os.path.exists(f))

        m, manifest = ap.apply(rep, ["caches"], [], True, self.trash)
        ap.purge(manifest, yes=True)
        self.assertFalse(os.path.exists(m["stage"]))
        self.assertFalse(os.path.exists(f))

    def test_purge_handles_read_only_folders(self) -> None:
        cache = os.path.join(self.tmp, "Caches", "dotslash")
        locked = os.path.join(cache, "x", "Tool.app", "Contents")
        write(os.path.join(locked, "bin"))
        os.chmod(locked, 0o555)
        os.chmod(os.path.dirname(locked), 0o555)
        m, manifest = ap.apply(self.make_report([self.cache_finding(cache)]), ["caches"], [], True, self.trash)
        ap.purge(manifest, yes=True)
        self.assertFalse(os.path.exists(m["stage"]))

    def test_missing_tool_falls_back_to_trashing_contents(self) -> None:
        cache = os.path.join(self.tmp, "nuget")
        write(os.path.join(cache, "pkg.nupkg"))
        fnd = self.cache_finding(cache, "command", ["definitely-not-a-real-tool-xyz", "clean"])
        m, _ = ap.apply(self.make_report([fnd]), ["caches"], [], True, self.trash)
        self.assertEqual(m["steps"][0]["status"], "staged")
        self.assertIn("not installed", m["steps"][0]["note"])
        self.assertEqual(os.listdir(cache), [])

    def test_active_v2c_run_is_skipped_live(self) -> None:
        repo = os.path.join(self.tmp, "repo")
        root = os.path.join(repo, ".worktrees", "v2c", "r1")
        nm = os.path.join(root, "impl", "node_modules")
        write(os.path.join(nm, "x.js"))
        write(os.path.join(repo, ".agent", "v2c", "runs", "r1", "run.json"),
              json.dumps({"run": "r1", "stage": "build", "worktreeRoot": root}).encode())
        fnd = Finding("dependencies", "impl", os.path.join(root, "impl"), 1, 1, "low", True, "", Action("trash", "", [nm]),
                      {"project": os.path.join(root, "impl")})
        m, _ = ap.apply(self.make_report([fnd]), ["dependencies"], [], True, self.trash)
        self.assertEqual(m["steps"][0]["status"], "skipped")
        self.assertIn("active v2c run", m["steps"][0]["note"])
        self.assertTrue(os.path.exists(nm))

    def test_project_that_became_active_is_skipped(self) -> None:
        proj = os.path.join(self.tmp, "proj")
        nm = os.path.join(proj, "node_modules")
        write(os.path.join(nm, "x.js"), mtime=time.time() - 90 * DAY)
        write(os.path.join(proj, "src", "edited.ts"))  # modified now
        fnd = Finding("dependencies", "proj", proj, 1, 1, "low", True, "", Action("trash", "", [nm]), {"project": proj})
        m, _ = ap.apply(self.make_report([fnd]), ["dependencies"], [], True, self.trash)
        self.assertEqual(m["steps"][0]["status"], "skipped")
        self.assertIn("became active", m["steps"][0]["note"])

    def test_running_app_blocks_cache_clear(self) -> None:
        write(os.path.join(self.tmp, "Google", "c.bin"))
        f = self.cache_finding(os.path.join(self.tmp, "Google")).to_dict()
        self.assertIn("running", ap.recheck(f, 30, time.time(), procs=["Google Chrome Helper"]))
        self.assertIsNone(ap.recheck(f, 30, time.time(), procs=["Finder"]))
        cmd = self.cache_finding(os.path.join(self.tmp, "store"), "command", ["pnpm", "store", "prune"]).to_dict()
        self.assertIsNone(ap.recheck(cmd, 30, time.time(), procs=["storedownloadd"]))

    def test_only_recommended_selected_by_category(self) -> None:
        a = self.cache_finding(os.path.join(self.tmp, "A"))
        b = self.cache_finding(os.path.join(self.tmp, "B"))
        b.recommended = False
        with open(self.make_report([a, b])) as fh:
            rep = json.load(fh)
        self.assertEqual([f["id"] for f in ap.select(rep, ["caches"], [])], [a.id])
        self.assertEqual([f["id"] for f in ap.select(rep, [], [b.id])], [b.id])


if __name__ == "__main__":
    unittest.main()
