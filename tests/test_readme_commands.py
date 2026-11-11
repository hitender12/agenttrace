"""Every command in the README Quickstart runs verbatim.

Runs each Quickstart command exactly as written (with the ``<run_id>``
placeholder replaced by the id of a run recorded in the same test),
in a temporary ``--db`` store, and asserts exit code 0. If a README
command stops working, this test fails first.
"""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


class ReadmeCommandsTestCase(unittest.TestCase):
    def _run(self, *args, db_dir=None):
        cmd = [sys.executable, "-m", "agenttrace", *args]
        if db_dir is not None:
            cmd += ["--db", str(db_dir)]
        proc = subprocess.run(cmd, cwd=str(REPO), capture_output=True,
                              text=True, timeout=120)
        self.assertEqual(proc.returncode, 0,
                         f"{' '.join(cmd)}\n{proc.stdout}\n{proc.stderr}")
        return proc

    def test_quickstart_commands_run_verbatim(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "at"
            # Record one run of the demo agent.
            rec = self._run("record", db_dir=db)
            run_id = rec.stdout.split()[2]
            self.assertTrue(run_id.startswith("run_"), rec.stdout)
            # Record with an explicit query.
            self._run("record", "--query",
                      "Where is my delivery for order ORD-2002?", db_dir=db)
            # Record an agent script (options go before '--').
            self._run("record", "--db", str(db), "--", "python3",
                      "examples/customer_support_agent.py")
            # Inspect.
            listed = self._run("list", db_dir=db)
            self.assertIn(run_id, listed.stdout)
            self._run("timeline", run_id, db_dir=db)
            self._run("timeline", run_id, "--payloads", db_dir=db)
            # Replay exactly.
            replayed = self._run("replay", run_id, "--mode", "exact",
                                 db_dir=db)
            replay_id = replayed.stdout.split("-> new run ")[1].split()[0]
            # Compare two runs.
            self._run("diff", run_id, replay_id, db_dir=db)
            # Regression suite, JSON and YAML.
            self._run("eval", "examples/agent_cases.json", "--ci", db_dir=db)
            self._run("eval", "examples/agent_cases.yaml", "--ci", db_dir=db)
            # Export and import.
            out = Path(tmp) / "run.json"
            self._run("export", run_id, "--out", str(out), db_dir=db)
            self.assertTrue(out.is_file())
            imported = self._run("import", str(out), db_dir=db)
            self.assertIn("imported", imported.stdout)

    def test_console_script_installs(self):
        """`pip install -e .` provides the `agenttrace` binary."""
        venv = Path(tempfile.mkdtemp()) / "venv"
        created = subprocess.run(
            [sys.executable, "-m", "venv", str(venv)],
            capture_output=True, text=True, timeout=120)
        if created.returncode != 0:
            self.skipTest("python3 -m venv is not available on this machine")
        pip = venv / "bin" / "pip"
        installed = subprocess.run(
            [str(pip), "install", "-e", str(REPO)],
            capture_output=True, text=True, timeout=300)
        self.assertEqual(installed.returncode, 0, installed.stderr[-2000:])
        binary = venv / "bin" / "agenttrace"
        proc = subprocess.run([str(binary), "--help"],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("record", proc.stdout)


if __name__ == "__main__":
    unittest.main()
