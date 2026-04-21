"""Smoke tests — run with: PYTHONPATH=. python -m unittest tests/test_smoke.py"""
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MOCK = [sys.executable, str(ROOT / "examples" / "mock_mcp_server.py")]
ENV = {**os.environ, "PYTHONPATH": str(ROOT)}


def run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, env=ENV)


class TestCreateFramework(unittest.TestCase):
    def test_hello_cli_greet(self):
        r = run([sys.executable, str(ROOT / "examples" / "hello_cli.py"), "greet", "--name", "x"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "hello x")

    def test_hello_cli_add(self):
        r = run([sys.executable, str(ROOT / "examples" / "hello_cli.py"), "add", "--a", "1", "--b", "2"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "3.0")

    def test_hello_cli_boolean_optional_action(self):
        r = run([
            sys.executable,
            str(ROOT / "examples" / "hello_cli.py"),
            "feature-status",
            "--no-enabled",
        ])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "disabled")


class TestMCPClient(unittest.TestCase):
    def test_list_tools(self):
        r = run([sys.executable, "-m", "cli_use.cli", "mcp-list", " ".join(MOCK), "--format", "json"])
        self.assertEqual(r.returncode, 0, r.stderr)
        tools = json.loads(r.stdout)
        names = {t["name"] for t in tools}
        self.assertEqual(names, {"greet", "add", "search_notes", "feature_status"})

    def test_run_tool(self):
        r = run([
            sys.executable, "-m", "cli_use.cli", "run", " ".join(MOCK),
            "greet", "--arguments", '{"name":"bob"}'
        ])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "hello bob")


class TestConverter(unittest.TestCase):
    def test_convert_and_invoke(self):
        with tempfile.NamedTemporaryFile(suffix=".py", delete=False) as f:
            out = f.name
        try:
            r = run([sys.executable, "-m", "cli_use.cli", "convert", " ".join(MOCK), "--out", out])
            self.assertEqual(r.returncode, 0, r.stderr)
            r2 = run([sys.executable, out, "add", "--a", "40", "--b", "2"])
            self.assertEqual(r2.returncode, 0, r2.stderr)
            self.assertEqual(r2.stdout.strip(), "42.0")
            r_bool = run([sys.executable, out, "feature_status", "--no-enabled"])
            self.assertEqual(r_bool.returncode, 0, r_bool.stderr)
            self.assertEqual(r_bool.stdout.strip(), "disabled")
            r3 = run([sys.executable, out, "--list-tools"])
            self.assertEqual(r3.returncode, 0, r3.stderr)
            tools = json.loads(r3.stdout)
            self.assertEqual(len(tools), 4)
        finally:
            os.unlink(out)


class TestHighLevelUX(unittest.TestCase):
    """Tests for `cli-use add <alias>` + `cli-use <alias> <tool>` dispatch."""

    def setUp(self):
        self.cli_home = tempfile.mkdtemp(prefix="cliuse-home-")
        self.work = tempfile.mkdtemp(prefix="cliuse-work-")
        self.env = {
            **os.environ,
            "PYTHONPATH": str(ROOT),
            "CLI_USE_HOME": self.cli_home,
        }

    def tearDown(self):
        import shutil
        shutil.rmtree(self.cli_home, ignore_errors=True)
        shutil.rmtree(self.work, ignore_errors=True)

    def _cli_use(self, *argv, check=True):
        r = subprocess.run(
            [sys.executable, "-m", "cli_use.cli", *argv],
            capture_output=True, text=True, env=self.env, cwd=self.work,
        )
        if check:
            self.assertEqual(r.returncode, 0, f"stderr: {r.stderr}")
        return r

    def test_add_dispatch_and_skill(self):
        mock_cmd = f"{shlex.quote(sys.executable)} {shlex.quote(str(ROOT / 'examples' / 'mock_mcp_server.py'))}"
        self._cli_use("add", "mock", "--from", f"local:{mock_cmd}",
                      "--description", "Local mock for tests")

        # aliases.json persisted
        self.assertTrue(Path(self.cli_home, "aliases.json").exists())

        # list shows the alias
        r = self._cli_use("list")
        self.assertIn("mock", r.stdout)

        # alias dispatch invokes the tool
        r = self._cli_use("mock", "greet", "--name", "x")
        self.assertEqual(r.stdout.strip(), "hello x")

        r = self._cli_use("mock", "feature_status", "--no-enabled")
        self.assertEqual(r.stdout.strip(), "disabled")

        # compact help prints tool roster
        r = self._cli_use("mock")
        self.assertIn("greet", r.stdout)
        self.assertIn("search_notes", r.stdout)

        # SKILL.md + AGENTS.md were emitted
        self.assertTrue(Path(self.work, "skills", "mock", "SKILL.md").exists())
        self.assertTrue(Path(self.work, "AGENTS.md").exists())
        agents = Path(self.work, "AGENTS.md").read_text()
        self.assertIn("<!-- cli-use:mock:start -->", agents)

    def test_remove_alias(self):
        mock_cmd = f"{shlex.quote(sys.executable)} {shlex.quote(str(ROOT / 'examples' / 'mock_mcp_server.py'))}"
        self._cli_use("add", "tmpmock", "--from", f"local:{mock_cmd}")
        self._cli_use("remove", "tmpmock")
        r = self._cli_use("list")
        self.assertNotIn("tmpmock", r.stdout)


if __name__ == "__main__":
    unittest.main()
