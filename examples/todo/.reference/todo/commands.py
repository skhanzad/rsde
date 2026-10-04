"""Sub-commands: what each CLI command does, independent of argument parsing."""

from __future__ import annotations

from todo.model import Task
from todo.storage import TaskStore


class TaskNotFound(LookupError):
    """Raised when a command refers to a task id that does not exist."""

    def __init__(self, task_id: int) -> None:
        super().__init__(f"no task with id {task_id}")
        self.task_id = task_id


def add(store: TaskStore, title: str) -> Task:
    return store.add(title)


def list_tasks(store: TaskStore, include_done: bool = False) -> list[Task]:
    tasks = sorted(store.load(), key=lambda task: task.id)
    return tasks if include_done else [task for task in tasks if not task.done]


def done(store: TaskStore, task_id: int) -> Task:
    try:
        return store.complete(task_id)
    except KeyError:
        raise TaskNotFound(task_id) from None


def remove(store: TaskStore, task_id: int) -> Task:
    try:
        return store.remove(task_id)
    except KeyError:
        raise TaskNotFound(task_id) from None
