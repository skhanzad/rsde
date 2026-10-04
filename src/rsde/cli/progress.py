"""Live progress output for ``execute`` and ``reconcile``."""

from __future__ import annotations

import sys
from typing import TextIO

from rsde.cli.render import one_line, plural
from rsde.cli.style import Style
from rsde.engine.executor import AttemptRecord, ExecutionEvents, ExecutionReport, StepOutcome
from rsde.graph.model import SpecGraph
from rsde.planning.planner import Action, Plan, PlanStep
from rsde.repository.state import CheckResult

_COMPACT = (Action.SKIP, Action.ROLLUP, Action.UNVERIFIABLE)
INDENT = "      "


class CliEvents(ExecutionEvents):
    def __init__(self, graph: SpecGraph, style: Style, stream: TextIO | None = None, *, agent_output: bool = True) -> None:
        self.graph = graph
        self.style = style
        self.stream = stream or sys.stdout
        self.show_agent_output = agent_output
        self._headers: dict[str, str] = {}

    def write(self, text: str = "") -> None:
        print(text, file=self.stream, flush=True)

    def _title(self, spec_id: str) -> str:
        doc = self.graph.specs.get(spec_id)
        return self.style.dim(f"{self.style.dash} {doc.title}") if doc else ""

    def notice(self, message: str) -> None:
        self.write(self.style.yellow(message))

    def plan_ready(self, plan: Plan, agent: str) -> None:
        self.write(
            self.style.bold(f"Executing `{plan.target}`")
            + self.style.dim(
                f" with agent `{agent}` — {plural(len(plan.steps), 'spec')} in scope, {len(plan.work)} to verify"
            )
        )

    def step_started(self, step: PlanStep, index: int, total: int) -> None:
        header = "[settle]" if index == 0 else f"[{index}/{total}]"
        self._headers[step.spec_id] = header
        if step.action in _COMPACT:
            return
        self.write()
        self.write(f"{self.style.bold(header)} {self.style.bold(step.spec_id)} {self._title(step.spec_id)}"
                   + self.style.dim(f"  ({step.reason})"))

    def check_finished(self, spec_id: str, result: CheckResult) -> None:
        if result.builtin:
            meta = result.output
        elif result.timed_out:
            meta = "timed out"
        elif result.passed:
            meta = f"{result.duration:.2f}s"
        else:
            meta = f"exit {result.exit_code} · {result.duration:.2f}s"
        self.write(f"{INDENT}{self.style.ok(result.passed)} {one_line(result.command, 90)}  {self.style.dim(meta)}")

    def agent_started(self, spec_id: str, attempt: int, max_attempts: int, agent: str) -> None:
        self.write(f"{INDENT}{self.style.cyan(self.style.arrow + ' agent ' + agent)} {self.style.dim(f'attempt {attempt}/{max_attempts}')}")

    def agent_output(self, spec_id: str, line: str) -> None:
        if self.show_agent_output and line.strip():
            bar = "│" if self.style.unicode else "|"
            self.write(self.style.dim(f"{INDENT}  {bar} {line}"))

    def agent_finished(self, spec_id: str, record: AttemptRecord) -> None:
        changed = plural(len(record.changes), "file") + " changed"
        self.write(f"{INDENT}  {self.style.dim(record.summary + ' · ' + changed)}")
        if record.out_of_scope:
            self.write(f"{INDENT}  {self.style.yellow('outside scope: ' + ', '.join(record.out_of_scope))}")

    def step_finished(self, step: PlanStep, outcome: StepOutcome) -> None:
        glyph = self.style.glyph(outcome.status)
        if step.action in _COMPACT and not outcome.checks:
            header = self._headers.get(step.spec_id, "")
            self.write(
                f"{self.style.dim(header)} {glyph} {step.spec_id} {self._title(step.spec_id)}"
                + self.style.dim(f"  {outcome.message}")
            )
            return
        self.write(f"{INDENT}{glyph} {outcome.message}")

    def run_finished(self, report: ExecutionReport) -> None:
        pass
