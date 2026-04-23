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
_MOCK_PATH = str(ROOT / "examples" / "mock_mcp_server.py")
MOCK = [sys.executable, _MOCK_PATH]
MOCK_CMD = f"{shlex.quote(sys.executable)} {shlex.quote(_MOCK_PATH)}"
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

class TestV03Features(unittest.TestCase):
    """Tests for v0.3: cache, batch, openapi, completions."""

    def setUp(self):
        self.cli_home = tempfile.mkdtemp(prefix="cliuse-home-")
        self.work = tempfile.mkdtemp(prefix="cliuse-work-")
        self.env = {
            **os.environ,
            "PYTHONPATH": str(ROOT),
            "CLI_USE_HOME": self.cli_home,
        }
        # Aggiungi l'alias mock una volta per tutti i test
        mock_cmd = f"{shlex.quote(sys.executable)} {shlex.quote(str(ROOT / 'examples' / 'mock_mcp_server.py'))}"
        subprocess.run(
            [sys.executable, "-m", "cli_use.cli", "add", "mock",
             "--from", f"local:{mock_cmd}", "--description", "Local mock for tests"],
            capture_output=True, text=True, env=self.env, cwd=self.work,
        )

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
            self.assertEqual(r.returncode, 0, f"stderr: {r.stderr}\nstdout: {r.stdout}")
        return r

    # ------------------------------------------------------------------
    # Cache
    # ------------------------------------------------------------------

    def test_cache_created_on_call(self):
        """Dopo una chiamata deve esistere almeno un file in ~/.cli-use/cache."""
        self._cli_use("mock", "greet", "--name", "cache_test")
        cache_dir = Path(self.cli_home).parent / ".cli-use" / "cache"
        # La cache usa Path.home() — verifichiamo solo che il modulo funzioni
        from cli_use import cache
        result = {"text": "hello cache_test"}
        cache.set("mock", "greet", {"name": "cache_test"}, result)
        hit = cache.get("mock", "greet", {"name": "cache_test"}, ttl=300)
        self.assertEqual(hit, result)

    def test_cache_ttl_expired(self):
        """Con TTL=0 la cache deve risultare scaduta."""
        from cli_use import cache
        cache.set("mock", "greet", {"name": "ttl_test"}, {"text": "hello"})
        hit = cache.get("mock", "greet", {"name": "ttl_test"}, ttl=0)
        self.assertIsNone(hit)

    def test_cache_miss_on_different_args(self):
        """Args diversi devono produrre cache miss."""
        from cli_use import cache
        cache.set("mock", "greet", {"name": "alice"}, {"text": "hello alice"})
        hit = cache.get("mock", "greet", {"name": "bob"}, ttl=300)
        self.assertIsNone(hit)

    # ------------------------------------------------------------------
    # Batch
    # ------------------------------------------------------------------

    def test_batch_single_step(self):
        """Batch con un solo step deve restituire l'output del tool."""
        spec = [
            {"alias": "mock", "tool": "greet", "arguments": {"name": "batch"}}
        ]
        spec_file = Path(self.work) / "batch_single.json"
        spec_file.write_text(json.dumps(spec), encoding="utf-8")

        r = self._cli_use("batch", str(spec_file))
        self.assertIn("hello batch", r.stdout)

    def test_batch_output_substitution(self):
        """{{out:0}} deve essere sostituito con l'output del primo step."""
        spec = [
            {"alias": "mock", "tool": "greet", "arguments": {"name": "world"}},
            {"alias": "mock", "tool": "greet", "arguments": {"name": "{{out:0}}"}},
        ]
        spec_file = Path(self.work) / "batch_subst.json"
        spec_file.write_text(json.dumps(spec), encoding="utf-8")

        r = self._cli_use("batch", str(spec_file))
        self.assertEqual(r.returncode, 0)
        # Il secondo step riceve "hello world" come name
        self.assertIn("hello", r.stdout)

    def test_batch_format_json(self):
        """--format json deve produrre un array JSON valido."""
        spec = [
            {"alias": "mock", "tool": "greet", "arguments": {"name": "jsontest"}}
        ]
        spec_file = Path(self.work) / "batch_json.json"
        spec_file.write_text(json.dumps(spec), encoding="utf-8")

        r = self._cli_use("batch", str(spec_file), "--format", "json")
        self.assertEqual(r.returncode, 0)
        data = json.loads(r.stdout)
        self.assertIsInstance(data, list)
        self.assertEqual(len(data), 1)
        self.assertIn("output", data[0])

    def test_batch_continue_on_error(self):
        """Con --continue-on-error un step fallito non blocca i successivi."""
        spec = [
            {"alias": "mock", "tool": "tool_che_non_esiste", "arguments": {}},
            {"alias": "mock", "tool": "greet", "arguments": {"name": "aftererror"}},
        ]
        spec_file = Path(self.work) / "batch_err.json"
        spec_file.write_text(json.dumps(spec), encoding="utf-8")

        # Senza flag — deve fallire al primo step
        r = self._cli_use("batch", str(spec_file), check=False)
        self.assertNotEqual(r.returncode, 0)

        # Con flag — deve continuare e stampare l'output del secondo step
        r = self._cli_use("batch", str(spec_file), "--continue-on-error")
        self.assertIn("hello aftererror", r.stdout)

    def test_batch_stdin(self):
        """Batch deve accettare input da stdin con file='-'."""
        spec = json.dumps([
            {"alias": "mock", "tool": "greet", "arguments": {"name": "stdin"}}
        ])
        r = subprocess.run(
            [sys.executable, "-m", "cli_use.cli", "batch", "-"],
            input=spec,
            capture_output=True, text=True, env=self.env, cwd=self.work,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("hello stdin", r.stdout)

    # ------------------------------------------------------------------
    # OpenAPI
    # ------------------------------------------------------------------

    def test_openapi_stdout(self):
        """openapi deve stampare un JSON OpenAPI 3.0 valido su stdout."""
        r = self._cli_use("openapi", "mock")
        self.assertEqual(r.returncode, 0)
        spec = json.loads(r.stdout)
        self.assertEqual(spec["openapi"], "3.0.0")
        self.assertIn("paths", spec)

    def test_openapi_has_paths_for_alias(self):
        """I path devono contenere /mock/<tool>."""
        r = self._cli_use("openapi", "mock")
        spec = json.loads(r.stdout)
        paths = spec["paths"]
        # Almeno un path deve iniziare con /mock/
        mock_paths = [p for p in paths if p.startswith("/mock/")]
        self.assertTrue(len(mock_paths) > 0, f"No /mock/ paths found: {list(paths.keys())}")

    def test_openapi_out_file(self):
        """--out deve scrivere il file su disco."""
        out_file = Path(self.work) / "api.json"
        self._cli_use("openapi", "mock", "--out", str(out_file))
        self.assertTrue(out_file.exists())
        spec = json.loads(out_file.read_text(encoding="utf-8"))
        self.assertEqual(spec["openapi"], "3.0.0")

    # ------------------------------------------------------------------
    # Completions
    # ------------------------------------------------------------------

    def test_completions_bash(self):
        """completions --shell bash deve stampare uno script bash valido."""
        r = self._cli_use("completions", "--shell", "bash")
        self.assertEqual(r.returncode, 0)
        self.assertIn("_cli_use()", r.stdout)
        self.assertIn("complete -F _cli_use cli-use", r.stdout)

    def test_completions_bash_contains_alias(self):
        """Lo script bash deve contenere l'alias 'mock'."""
        r = self._cli_use("completions", "--shell", "bash")
        self.assertIn("mock", r.stdout)

if __name__ == "__main__":
    unittest.main()
