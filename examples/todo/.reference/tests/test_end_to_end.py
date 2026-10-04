"""End-to-end test: drive the real CLI, as a subprocess, through add → list → done."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name) / "todo.json"
        self.env = {**os.environ, "TODO_FILE": str(self.data)}

    def tearDown(self):
        self.tmp.cleanup()

    def todo(self, *args):
        return subprocess.run(
            [sys.executable, "-m", "todo", *args], cwd=ROOT, env=self.env, capture_output=True, text=True
        )

    def test_full_cycle(self):
        result = self.todo("add", "Buy milk")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("[ ] 1  Buy milk", self.todo("list").stdout)

        self.assertEqual(self.todo("done", "1").returncode, 0)
        self.assertIn("[x] 1  Buy milk", self.todo("list", "--all").stdout)
        json.loads(self.data.read_text(encoding="utf-8"))

    def test_unknown_id(self):
        result = self.todo("done", "42")
        self.assertEqual(result.returncode, 1)
        self.assertIn("error:", result.stderr)


if __name__ == "__main__":
    unittest.main()
