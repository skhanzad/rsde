"""Tests for output formatting."""

import unittest

from todo.format import format_task, format_tasks
from todo.model import Task


class FormatTests(unittest.TestCase):
    def test_open_task(self):
        self.assertEqual(format_task(Task(3, "Buy milk")), "[ ] 3  Buy milk")

    def test_done_task(self):
        self.assertEqual(format_task(Task(3, "Buy milk", done=True)), "[x] 3  Buy milk")

    def test_empty_list(self):
        self.assertEqual(format_tasks([]), "No tasks.")

    def test_one_line_per_task_in_order(self):
        tasks = [Task(2, "b"), Task(1, "a", done=True)]
        self.assertEqual(format_tasks(tasks), "[ ] 2  b\n[x] 1  a")


if __name__ == "__main__":
    unittest.main()
