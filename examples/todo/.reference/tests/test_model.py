"""Tests for the task model."""

import unittest
from dataclasses import FrozenInstanceError

from todo.model import Task


class TaskTests(unittest.TestCase):
    def test_title_is_stripped(self):
        self.assertEqual(Task(1, "  Buy milk  ").title, "Buy milk")

    def test_empty_title_is_rejected(self):
        for title in ("", "   "):
            with self.assertRaises(ValueError):
                Task(1, title)

    def test_id_must_be_a_positive_integer(self):
        for bad in (0, -1, "1", 1.5, True):
            with self.assertRaises(ValueError):
                Task(bad, "x")

    def test_complete_returns_a_new_task(self):
        task = Task(1, "x")
        done = task.complete()
        self.assertTrue(done.done)
        self.assertFalse(task.done)

    def test_tasks_are_frozen(self):
        with self.assertRaises(FrozenInstanceError):
            Task(1, "x").title = "y"

    def test_dict_round_trip(self):
        task = Task(3, "Write specs", done=True)
        self.assertEqual(Task.from_dict(task.to_dict()), task)


if __name__ == "__main__":
    unittest.main()
