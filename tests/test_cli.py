from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from diskscout import __version__
from diskscout.cli import main
from diskscout.config import latest_report


def write(path: str, data: bytes = b"x" * 1000) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        env = {"XDG_STATE_HOME": os.path.join(self.tmp, "state"), "XDG_CONFIG_HOME": os.path.join(self.tmp, "config")}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.proj = os.path.join(self.tmp, "proj")
        write(os.path.join(self.proj, "app", "package.json"))
        write(os.path.join(self.proj, "app", "node_modules", "x", "index.js"))

    def run_cli(self, *argv: str) -> str:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = main(list(argv))
        self.assertEqual(code, 0)
        return out.getvalue()

    def test_version(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            main(["--version"])
        self.assertIn(__version__, out.getvalue())

    def test_scan_writes_to_state_dir_and_latest_resolves(self) -> None:
        out = self.run_cli("scan", "--only", "deps,dups,large", "--project-root", self.proj, "--personal-root", self.proj)
        self.assertIn("nothing was changed", out)
        latest = latest_report()
        self.assertIsNotNone(latest)
        self.assertTrue(latest.startswith(os.path.join(self.tmp, "state", "diskscout", "reports")))
        with open(latest) as fh:
            data = json.load(fh)
        self.assertEqual(data["findings"][0]["category"], "dependencies")
        self.assertIn("report.md", self.run_cli("report"))
        applied = self.run_cli("apply", "--id", data["findings"][0]["id"])  # fresh project: live re-check refuses it
        self.assertIn("project became active", applied)
        self.assertIn("Dry run", applied)

    def test_config_file_supplies_defaults_and_protect(self) -> None:
        write(
            os.path.join(self.tmp, "config", "diskscout", "config.json"),
            json.dumps({"project_roots": [self.proj], "personal_roots": [self.proj], "protect": [os.path.join(self.proj, "app")]}).encode(),
        )
        self.run_cli("scan", "--only", "deps")
        with open(latest_report()) as fh:
            f = json.load(fh)["findings"][0]
        self.assertEqual(f["risk"], "protected")


if __name__ == "__main__":
    unittest.main()
