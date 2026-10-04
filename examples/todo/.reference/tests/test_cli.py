"""Tests for the command-line interface."""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from todo.cli import main
from todo.storage import TaskStore


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = TaskStore(Path(self.tmp.name) / "todo.json")

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(err):
            code = main(list(argv), store=self.store, out=out, err=err)
        return code, out.getvalue(), err.getvalue()

    def test_add_and_list(self):
        self.assertEqual(self.run_cli("add", "Buy milk")[0], 0)
        code, out, _ = self.run_cli("list")
        self.assertEqual(code, 0)
        self.assertIn("[ ] 1  Buy milk", out)

    def test_done_and_list_all(self):
        self.run_cli("add", "Buy milk")
        self.assertEqual(self.run_cli("done", "1")[0], 0)
        self.assertEqual(self.run_cli("list")[1].strip(), "No tasks.")
        self.assertIn("[x] 1  Buy milk", self.run_cli("list", "--all")[1])

    def test_remove(self):
        self.run_cli("add", "Buy milk")
        self.assertEqual(self.run_cli("remove", "1")[0], 0)
        self.assertEqual(self.run_cli("list", "--all")[1].strip(), "No tasks.")

    def test_unknown_id_exits_with_1(self):
        code, _, err = self.run_cli("done", "42")
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith("error: "))

    def test_usage_error_exits_with_2(self):
        self.assertEqual(self.run_cli("frobnicate")[0], 2)
        self.assertEqual(self.run_cli("done", "not-a-number")[0], 2)


if __name__ == "__main__":
    unittest.main()
