"""Output formatting: renders tasks as stable, human-readable text."""

from __future__ import annotations

from collections.abc import Iterable

from todo.model import Task


def format_task(task: Task) -> str:
    return f"[{'x' if task.done else ' '}] {task.id}  {task.title}"


def format_tasks(tasks: Iterable[Task]) -> str:
    lines = [format_task(task) for task in tasks]
    return "\n".join(lines) if lines else "No tasks."
