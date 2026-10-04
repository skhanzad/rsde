"""Tests for the sub-commands."""

import tempfile
import unittest
from pathlib import Path

from todo import commands
from todo.storage import TaskStore


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = TaskStore(Path(self.tmp.name) / "todo.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_add_returns_the_task(self):
        self.assertEqual(commands.add(self.store, "Buy milk").title, "Buy milk")

    def test_list_shows_open_tasks_by_default(self):
        first = commands.add(self.store, "a")
        commands.add(self.store, "b")
        commands.done(self.store, first.id)
        self.assertEqual([t.title for t in commands.list_tasks(self.store)], ["b"])
        self.assertEqual([t.title for t in commands.list_tasks(self.store, include_done=True)], ["a", "b"])

    def test_remove(self):
        task = commands.add(self.store, "a")
        commands.remove(self.store, task.id)
        self.assertEqual(commands.list_tasks(self.store, include_done=True), [])

    def test_unknown_ids_raise_task_not_found(self):
        with self.assertRaises(commands.TaskNotFound):
            commands.done(self.store, 7)
        with self.assertRaises(LookupError):
            commands.remove(self.store, 7)


if __name__ == "__main__":
    unittest.main()
