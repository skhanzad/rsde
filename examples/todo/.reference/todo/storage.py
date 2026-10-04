"""JSON task storage: keeps every task in one JSON file, written atomically."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from todo.model import Task

DEFAULT_FILE = "todo.json"


def default_path() -> Path:
    return Path(os.environ.get("TODO_FILE", DEFAULT_FILE))


class TaskStore:
    """A repository of tasks backed by a single JSON file."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else default_path()

    def _read(self) -> tuple[int, list[Task]]:
        if not self.path.exists():
            return 0, []
        with self.path.open(encoding="utf-8") as fh:
            data: dict[str, Any] = json.load(fh)
        tasks = [Task.from_dict(item) for item in data.get("tasks", [])]
        last_id = max([int(data.get("last_id", 0)), *(t.id for t in tasks)])
        return last_id, tasks

    def _write(self, last_id: int, tasks: list[Task]) -> None:
        payload = {"last_id": last_id, "tasks": [t.to_dict() for t in tasks]}
        directory = self.path.parent
        directory.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".todo-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2)
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def load(self) -> list[Task]:
        return self._read()[1]

    def add(self, title: str) -> Task:
        last_id, tasks = self._read()
        task = Task(last_id + 1, title)
        self._write(task.id, [*tasks, task])
        return task

    def complete(self, task_id: int) -> Task:
        last_id, tasks = self._read()
        for index, task in enumerate(tasks):
            if task.id == task_id:
                tasks[index] = task.complete()
                self._write(last_id, tasks)
                return tasks[index]
        raise KeyError(task_id)

    def remove(self, task_id: int) -> Task:
        last_id, tasks = self._read()
        for index, task in enumerate(tasks):
            if task.id == task_id:
                del tasks[index]
                self._write(last_id, tasks)
                return task
        raise KeyError(task_id)
