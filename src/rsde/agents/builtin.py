"""Adapters that need no external agent: ``none`` and ``manual``."""

from __future__ import annotations

import sys

from rsde.agents.base import AgentAdapter, AgentResult, AgentTask


class NoneAdapter(AgentAdapter):
    """Verify-only mode: never changes code."""

    name = "none"
    description = "verify only; never changes code (useful in CI)"
    implements = False

    def run(self, task: AgentTask) -> AgentResult:
        return AgentResult(False, "no coding agent configured (use --agent or [execute].agent in rsde.toml)")


class ManualAdapter(AgentAdapter):
    """A human is the agent: RSDE writes the task, waits, then verifies."""

    name = "manual"
    description = "a human implements each task; RSDE waits for Enter, then verifies"

    def run(self, task: AgentTask) -> AgentResult:
        interactive = bool(self.options.get("interactive", sys.stdin.isatty()))
        message = (
            f"Task for `{task.spec_id}` (attempt {task.attempt}/{task.max_attempts}) written to {task.prompt_file}"
        )
        if not interactive:
            return AgentResult(
                False,
                f"{message}. Implement it, then run `rsde execute` again.",
                deferred=True,
            )
        print(f"\n{message}", file=sys.stderr)
        print("Implement it, then press Enter to verify (or type q + Enter to stop): ", end="", file=sys.stderr, flush=True)
        try:
            answer = input()
        except EOFError:
            answer = "q"
        if answer.strip().lower() in ("q", "quit", "stop"):
            return AgentResult(False, "stopped by the user", deferred=True)
        return AgentResult(True, "implemented manually")
