"""Tests for JSON task storage."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from todo.storage import TaskStore


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "tasks.json"
        self.store = TaskStore(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_file_means_no_tasks(self):
        self.assertEqual(self.store.load(), [])

    def test_add_assigns_sequential_ids_and_persists(self):
        first = self.store.add("one")
        second = self.store.add("two")
        self.assertEqual((first.id, second.id), (1, 2))
        self.assertEqual([t.title for t in TaskStore(self.path).load()], ["one", "two"])

    def test_ids_are_not_reused_after_removal(self):
        self.store.add("one")
        second = self.store.add("two")
        self.store.remove(second.id)
        self.assertEqual(self.store.add("three").id, 3)

    def test_complete_marks_the_task_done(self):
        task = self.store.add("one")
        self.assertTrue(self.store.complete(task.id).done)
        self.assertTrue(self.store.load()[0].done)

    def test_unknown_ids_raise_key_error(self):
        with self.assertRaises(KeyError):
            self.store.complete(99)
        with self.assertRaises(KeyError):
            self.store.remove(99)

    def test_file_is_always_valid_json(self):
        self.store.add("one")
        json.loads(self.path.read_text(encoding="utf-8"))

    def test_saving_replaces_the_file_atomically(self):
        with mock.patch("todo.storage.os.replace", wraps=os.replace) as replace:
            self.store.add("one")
        replace.assert_called_once()
        self.assertEqual(Path(replace.call_args.args[1]), self.path)

    def test_location_defaults_to_the_todo_file_variable(self):
        with mock.patch.dict(os.environ, {"TODO_FILE": str(self.path)}):
            self.assertEqual(TaskStore().path, self.path)


if __name__ == "__main__":
    unittest.main()
