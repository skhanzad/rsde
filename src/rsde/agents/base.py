"""The coding-agent adapter contract.

RSDE never trusts an agent's opinion of its own work. An adapter receives an
:class:`AgentTask` (a compiled, deterministic prompt plus structured context),
changes files in the workspace, and returns an :class:`AgentResult` that only
says whether the agent *ran*. Whether the spec is satisfied is decided
afterwards by re-running the spec's checks.

To plug in a new backend, subclass :class:`AgentAdapter` and implement
:meth:`AgentAdapter.run` — or, for any CLI agent, configure the generic
``command`` adapter in ``rsde.toml`` without writing code.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, ClassVar, Mapping

from rsde.repository.state import CheckResult


class AgentConfigError(Exception):
    """An agent is unknown, misconfigured or unavailable."""


@dataclass(frozen=True)
class AgentTask:
    """Everything an agent needs to make one spec pass."""

    spec_id: str
    spec_path: str
    title: str
    prompt: str
    attempt: int
    max_attempts: int
    workspace: Path
    scope: tuple[str, ...]
    verify_commands: tuple[str, ...]
    prompt_file: Path
    log_file: Path
    timeout: float
    failures: tuple[CheckResult, ...] = ()
    on_output: Callable[[str], None] | None = field(default=None, compare=False, repr=False)


@dataclass(frozen=True)
class AgentResult:
    """How an agent invocation went. Says nothing about whether the spec is satisfied."""

    completed: bool
    summary: str = ""
    output: str = ""
    exit_code: int | None = None
    duration: float = 0.0
    deferred: bool = False  # the task was handed to a human; stop the run


class AgentAdapter(abc.ABC):
    """Base class for coding-agent backends."""

    #: Name used on the command line (``--agent NAME``) and in ``rsde.toml``.
    #: Instances carry the name they were configured under, e.g. ``[agents.mine]``.
    name: str = ""
    #: One line shown by ``rsde agents``.
    description: ClassVar[str] = ""
    #: False for adapters that never change code (verify-only mode).
    implements: ClassVar[bool] = True

    def __init__(self, options: Mapping[str, Any] | None = None, *, workspace: Path, name: str | None = None) -> None:
        self.name = name or type(self).name
        self.options = dict(options or {})
        self.workspace = workspace

    def unavailable_reason(self) -> str | None:
        """Why this adapter cannot run in this environment, or None if it can."""
        return None

    @abc.abstractmethod
    def run(self, task: AgentTask) -> AgentResult:
        """Change the workspace so that ``task`` is satisfied."""
