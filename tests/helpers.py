"""Shared helpers for the RSDE test-suite."""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path
from typing import Callable

from rsde.agents.base import AgentAdapter, AgentResult, AgentTask
from rsde.graph import SpecGraph, build_graph
from rsde.repository.config import Config, Workspace

PYTHON = sys.executable


def write_files(root: Path, files: dict[str, str]) -> Path:
    """Write ``{relative path: content}`` under ``root``; content is dedented."""
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(content).lstrip("\n"), encoding="utf-8")
    return root


def graph_of(root: Path, **kwargs) -> SpecGraph:
    kwargs.setdefault("probe_environment", False)
    return build_graph(root, **kwargs)


def codes(graph: SpecGraph) -> list[str]:
    return sorted(d.code for d in graph.diagnostics)


def workspace(root: Path, **config) -> Workspace:
    return Workspace(root.resolve(), Config(**config))


Action = Callable[[Path, AgentTask], None]


def writes(rel: str, text: str) -> Action:
    """An agent action that writes ``text`` to ``rel``."""

    def action(root: Path, task: AgentTask) -> None:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    return action


def chain(*actions: Action) -> Action:
    def action(root: Path, task: AgentTask) -> None:
        for step in actions:
            step(root, task)

    return action


class ScriptedAgent(AgentAdapter):
    """A deterministic test double: runs a scripted action per spec id (and per attempt)."""

    name = "scripted"

    def __init__(self, workspace: Path, actions: dict[str, Action | list[Action]] | None = None) -> None:
        super().__init__({}, workspace=workspace)
        self.actions = actions or {}
        self.tasks: list[AgentTask] = []

    def run(self, task: AgentTask) -> AgentResult:
        self.tasks.append(task)
        action = self.actions.get(task.spec_id)
        if isinstance(action, list):
            action = action[task.attempt - 1] if task.attempt <= len(action) else None
        if action is not None:
            action(task.workspace, task)
        return AgentResult(True, "scripted")

    @property
    def spec_ids(self) -> list[str]:
        return [t.spec_id for t in self.tasks]
