"""Task model: an immutable, validated todo task."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any


@dataclass(frozen=True)
class Task:
    """A todo task. Instances never change; :meth:`complete` returns a done copy."""

    id: int
    title: str
    done: bool = False

    def __post_init__(self) -> None:
        if isinstance(self.id, bool) or not isinstance(self.id, int) or self.id < 1:
            raise ValueError(f"task id must be a positive integer, got {self.id!r}")
        if not isinstance(self.title, str) or not self.title.strip():
            raise ValueError("task title must not be empty")
        object.__setattr__(self, "title", self.title.strip())

    def complete(self) -> Task:
        return replace(self, done=True)

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "title": self.title, "done": self.done}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Task:
        return cls(id=data["id"], title=data["title"], done=bool(data.get("done", False)))
