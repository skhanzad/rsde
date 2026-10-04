"""The execution engine behind ``rsde execute``.

Lifecycle for a target spec::

    assess    fingerprints + recorded evidence → status of every spec
    plan      the target's scope (descendants + transitive dependencies),
              in an order where dependencies and children come first
    run       for each step:
                blocked?   a dependency or external tool is missing → skip
                pre-check  run the spec's checks; passing → satisfied, no agent
                implement  invoke the agent with a compiled task prompt
                re-verify  run the checks again; retry up to max_attempts
    settle    re-verify specs whose inputs changed during the run
    propagate re-evaluate every status from evidence; the target is satisfied
              only if its whole subtree and dependency closure is

Two guards keep agents honest: changes to spec files abort the run (specs are
the source of truth), and changes outside a spec's ``@implement`` scope are
recorded as violations (failing the spec under ``--strict-scope``).
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from rsde.agents.base import AgentAdapter, AgentResult, AgentTask
from rsde.agents.prompt import render_task_prompt
from rsde.graph.builder import missing_externals
from rsde.graph.model import SpecGraph
from rsde.planning.fingerprint import Fingerprint, Fingerprinter
from rsde.planning.planner import Action, Plan, PlanStep, build_plan
from rsde.planning.status import OwnState, SpecStatus, Status, evaluate
from rsde.repository.config import CONFIG_FILE, Workspace
from rsde.repository.files import Changes, GlobSet, diff_snapshots, take_snapshot
from rsde.repository.state import CheckResult, Evidence, RunRecord, StateStore, utc_now
from rsde.syntax.ast import SpecDocument
from rsde.verification.runner import Verifier

MAX_SETTLE_PASSES = 2


def plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


@dataclass
class ExecuteOptions:
    max_attempts: int = 3
    verify_timeout: float = 600.0
    agent_timeout: float = 1800.0
    force: bool = False
    verify_only: bool = False
    strict_scope: bool = False
    trust_failures: bool = False  # skip the pre-check when fresh evidence already shows failure


@dataclass
class AttemptRecord:
    attempt: int
    agent: str
    completed: bool
    summary: str
    duration: float
    changes: Changes
    out_of_scope: list[str]
    prompt_file: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempt": self.attempt,
            "agent": self.agent,
            "completed": self.completed,
            "summary": self.summary,
            "duration": round(self.duration, 3),
            "changes": self.changes.to_dict(),
            "out_of_scope": self.out_of_scope,
            "prompt_file": self.prompt_file,
        }


@dataclass
class StepOutcome:
    spec_id: str
    action: Action
    status: Status
    message: str
    checks: list[CheckResult] = field(default_factory=list)
    attempts: list[AttemptRecord] = field(default_factory=list)
    checks_run: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "spec": self.spec_id,
            "action": self.action.value,
            "status": self.status.value,
            "message": self.message,
            "checks_run": self.checks_run,
            "checks": [c.to_dict() for c in self.checks],
            "attempts": [a.to_dict() for a in self.attempts],
        }


@dataclass
class ExecutionReport:
    run_id: str
    target: str
    agent: str
    plan: Plan
    outcomes: dict[str, StepOutcome]
    statuses: dict[str, SpecStatus]
    scope: list[str]
    aborted: str | None = None
    run_dir: Path | None = None

    @property
    def satisfied(self) -> bool:
        return self.aborted is None and self.statuses[self.target].satisfied

    @property
    def agent_runs(self) -> int:
        return sum(len(o.attempts) for o in self.outcomes.values())

    @property
    def checks_run(self) -> int:
        return sum(o.checks_run for o in self.outcomes.values())

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "target": self.target,
            "agent": self.agent,
            "satisfied": self.satisfied,
            "aborted": self.aborted,
            "plan": self.plan.to_dict(),
            "outcomes": {k: v.to_dict() for k, v in self.outcomes.items()},
            "statuses": {
                k: {"status": self.statuses[k].status.value, "reason": self.statuses[k].reason} for k in self.scope
            },
        }


class ExecutionEvents:
    """Progress callbacks. The CLI renders them; tests record them. All optional."""

    def plan_ready(self, plan: Plan, agent: str) -> None: ...
    def step_started(self, step: PlanStep, index: int, total: int) -> None: ...
    def check_started(self, spec_id: str, command: str) -> None: ...
    def check_finished(self, spec_id: str, result: CheckResult) -> None: ...
    def agent_started(self, spec_id: str, attempt: int, max_attempts: int, agent: str) -> None: ...
    def agent_output(self, spec_id: str, line: str) -> None: ...
    def agent_finished(self, spec_id: str, record: AttemptRecord) -> None: ...
    def step_finished(self, step: PlanStep, outcome: StepOutcome) -> None: ...
    def run_finished(self, report: ExecutionReport) -> None: ...


class ExecutionAborted(Exception):
    """Stop the run immediately (integrity violation, deferred manual task, interrupt)."""


def new_run_id() -> str:
    return f"{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"


class Executor:
    def __init__(
        self,
        graph: SpecGraph,
        workspace: Workspace,
        adapter: AgentAdapter,
        *,
        options: ExecuteOptions | None = None,
        events: ExecutionEvents | None = None,
        state: StateStore | None = None,
    ) -> None:
        if not graph.ok:
            raise ValueError("cannot execute an invalid spec graph; run `rsde check`")
        self.graph = graph
        self.workspace = workspace
        self.adapter = adapter
        self.options = options or ExecuteOptions()
        self.events = events or ExecutionEvents()
        self.state = state or StateStore(workspace.state_dir)
        self.fingerprinter = Fingerprinter(graph, workspace.config.ignore)
        self.verifier = Verifier(graph.root, timeout=self.options.verify_timeout)
        self._protected = graph.spec_paths() | {CONFIG_FILE}

    @property
    def verify_only(self) -> bool:
        return self.options.verify_only or not self.adapter.implements

    def assess(self) -> tuple[dict[str, Fingerprint], dict[str, SpecStatus]]:
        fingerprints = self.fingerprinter.compute()
        return fingerprints, evaluate(self.graph, fingerprints, self.state.evidence)

    def plan(self, target: str) -> Plan:
        _, statuses = self.assess()
        return build_plan(self.graph, target, statuses, force=self.options.force, verify_only=self.verify_only)

    # ------------------------------------------------------------------ run

    def execute(self, target: str, *, command: str = "execute") -> ExecutionReport:
        run_id = new_run_id()
        run_dir = self.state.run_dir(run_id)
        plan = self.plan(target)
        (run_dir / "plan.json").write_text(json.dumps(plan.to_dict(), indent=2) + "\n", encoding="utf-8")
        self.events.plan_ready(plan, self.adapter.name)

        record = RunRecord(run_id, command, target, self.adapter.name, utc_now())
        outcomes: dict[str, StepOutcome] = {}
        aborted: str | None = None
        try:
            for index, step in enumerate(plan.steps, 1):
                self.events.step_started(step, index, len(plan.steps))
                outcome = self._run_step(step, run_id, run_dir)
                outcomes[step.spec_id] = outcome
                self.state.save()
                self.events.step_finished(step, outcome)
            self._settle(plan, outcomes, run_id, run_dir)
        except ExecutionAborted as exc:
            aborted = str(exc)
        except KeyboardInterrupt:
            aborted = "interrupted"
        finally:
            _, statuses = self.assess()
            record.finished_at = utc_now()
            record.satisfied = aborted is None and statuses[target].satisfied
            record.outcome = aborted or statuses[target].status.value
            self.state.record_run(record)
            self.state.save()

        scope_ids = [s.spec_id for s in plan.steps]
        report = ExecutionReport(run_id, target, self.adapter.name, plan, outcomes, statuses, scope_ids, aborted, run_dir)
        (run_dir / "report.json").write_text(json.dumps(report.to_dict(), indent=2) + "\n", encoding="utf-8")
        self.events.run_finished(report)
        return report

    def _run_step(self, step: PlanStep, run_id: str, run_dir: Path) -> StepOutcome:
        spec_id = step.spec_id
        doc = self.graph.specs[spec_id]
        fingerprints, statuses = self.assess()

        if step.action is Action.UNVERIFIABLE:
            return StepOutcome(spec_id, step.action, Status.UNVERIFIABLE, "declares no checks: add @verify")
        waiting = [d for d in self.graph.dependencies.dependencies(spec_id) if not statuses[d].satisfied]
        if waiting:
            return StepOutcome(spec_id, step.action, Status.BLOCKED, "waiting for " + ", ".join(waiting))
        missing = missing_externals(doc)
        if missing:
            return StepOutcome(spec_id, step.action, Status.BLOCKED, "; ".join(missing))
        gaps = [c for c in self.graph.hierarchy.children(spec_id) if not statuses[c].satisfied]
        if gaps:
            return StepOutcome(spec_id, step.action, Status.INCOMPLETE, "child specs not satisfied: " + ", ".join(gaps))
        current = statuses[spec_id]
        if step.action is Action.ROLLUP:
            return StepOutcome(spec_id, step.action, current.status, "all child specs satisfied")
        if step.action is Action.SKIP and current.own is OwnState.PASS:
            return StepOutcome(spec_id, step.action, Status.SATISFIED, "fresh evidence; nothing to do")

        ran = 0
        if self.options.trust_failures and current.own is OwnState.FAIL and current.evidence is not None:
            results = list(current.evidence.checks)
        else:
            results = self.verify_spec(doc, run_dir, "pre-check")
            ran += len(results)
            if all(r.passed for r in results):
                self.record_evidence(doc, results, run_id, attempts=0, violations=[])
                message = "checks pass; no implementation needed"
                return StepOutcome(spec_id, step.action, Status.SATISFIED, message, results, [], ran)
        if self.verify_only:
            self.record_evidence(doc, results, run_id, attempts=0, violations=[])
            failed = sum(1 for r in results if not r.passed)
            return StepOutcome(spec_id, step.action, Status.FAILED, f"{failed} check(s) failed", results, [], ran)

        attempts: list[AttemptRecord] = []
        violations: list[str] = []
        passed = False
        for attempt in range(1, self.options.max_attempts + 1):
            record = self._attempt(doc, attempt, results, violations, run_dir)
            attempts.append(record)
            violations = sorted({*violations, *record.out_of_scope})
            results = self.verify_spec(doc, run_dir, f"attempt-{attempt}")
            ran += len(results)
            passed = all(r.passed for r in results)
            if passed and not (self.options.strict_scope and record.out_of_scope):
                break
        ok = passed and not (self.options.strict_scope and violations)
        self.record_evidence(doc, results, run_id, attempts=len(attempts), violations=violations, passed=ok)
        if ok:
            message = f"satisfied after {plural(len(attempts), 'agent attempt')}"
            if violations:
                message += f"; {plural(len(violations), 'out-of-scope change')} recorded"
            return StepOutcome(spec_id, step.action, Status.SATISFIED, message, results, attempts, ran)
        if passed:
            message = "checks pass, but the agent changed files outside the spec's scope (--strict-scope)"
        else:
            message = f"still failing after {plural(len(attempts), 'agent attempt')}"
        return StepOutcome(spec_id, step.action, Status.FAILED, message, results, attempts, ran)

    def _settle(self, plan: Plan, outcomes: dict[str, StepOutcome], run_id: str, run_dir: Path) -> None:
        """Re-verify specs whose inputs were changed by later steps (no agent involved)."""
        in_scope = [s.spec_id for s in plan.steps]
        for _ in range(MAX_SETTLE_PASSES):
            _, statuses = self.assess()
            stale = [
                s
                for s in in_scope
                if statuses[s].own is OwnState.STALE
                and outcomes.get(s) is not None
                and outcomes[s].status is Status.SATISFIED
            ]
            if not stale:
                return
            for spec_id in stale:
                step = PlanStep(spec_id, Action.CHECK, "inputs changed during this run", statuses[spec_id], ())
                self.events.step_started(step, 0, 0)
                doc = self.graph.specs[spec_id]
                results = self.verify_spec(doc, run_dir, "settle")
                self.record_evidence(doc, results, run_id, attempts=0, violations=[])
                status = Status.SATISFIED if all(r.passed for r in results) else Status.FAILED
                message = "re-verified after later changes" if status is Status.SATISFIED else (
                    "broken by changes made later in this run"
                )
                previous = outcomes[spec_id]
                outcome = StepOutcome(
                    spec_id, Action.CHECK, status, message, results, previous.attempts, previous.checks_run + len(results)
                )
                outcomes[spec_id] = outcome
                self.state.save()
                self.events.step_finished(step, outcome)

    # ------------------------------------------------------------ helpers

    def verify_spec(self, doc: SpecDocument, run_dir: Path, label: str) -> list[CheckResult]:
        files = self.fingerprinter.refresh_files()
        return self.verifier.verify(
            doc,
            files,
            on_start=lambda command: self.events.check_started(doc.id, command),
            on_result=lambda result: self.events.check_finished(doc.id, result),
            log_dir=run_dir / "logs",
            label=label,
        )

    def record_evidence(
        self,
        doc: SpecDocument,
        results: list[CheckResult],
        run_id: str,
        *,
        attempts: int,
        violations: list[str],
        passed: bool | None = None,
    ) -> Evidence:
        # The fingerprint is taken after the checks ran: evidence describes the
        # workspace exactly as it was when the outcome was observed.
        fingerprints = self.fingerprinter.compute()
        evidence = Evidence(
            spec_id=doc.id,
            fingerprint=fingerprints[doc.id].value,
            passed=all(r.passed for r in results) if passed is None else passed,
            checks=results,
            verified_at=utc_now(),
            run_id=run_id,
            agent=self.adapter.name if attempts else None,
            attempts=attempts,
            scope_violations=violations,
        )
        self.state.put(evidence)
        return evidence

    def _attempt(
        self,
        doc: SpecDocument,
        attempt: int,
        results: list[CheckResult],
        violations: list[str],
        run_dir: Path,
    ) -> AttemptRecord:
        slug = doc.id.replace("/", "__")
        prompt = render_task_prompt(
            self.graph,
            doc.id,
            attempt=attempt,
            max_attempts=self.options.max_attempts,
            results=results,
            files=self.fingerprinter.files,
            scope_violations=violations,
        )
        prompt_file = run_dir / f"{slug}.attempt-{attempt}.prompt.md"
        prompt_file.write_text(prompt, encoding="utf-8")
        task = AgentTask(
            spec_id=doc.id,
            spec_path=doc.path,
            title=doc.title,
            prompt=prompt,
            attempt=attempt,
            max_attempts=self.options.max_attempts,
            workspace=self.graph.root,
            scope=doc.implement_patterns,
            verify_commands=doc.verify_commands,
            prompt_file=prompt_file,
            log_file=run_dir / f"{slug}.attempt-{attempt}.agent.log",
            timeout=self.options.agent_timeout,
            failures=tuple(r for r in results if not r.passed),
            on_output=lambda line: self.events.agent_output(doc.id, line),
        )

        ignore = self.workspace.config.ignore
        before = take_snapshot(self.graph.root, self.fingerprinter.hasher, ignore)
        self.events.agent_started(doc.id, attempt, self.options.max_attempts, self.adapter.name)
        try:
            result = self.adapter.run(task)
        except KeyboardInterrupt:
            raise
        except Exception as exc:  # a broken adapter must not take the run down
            result = AgentResult(False, f"adapter `{self.adapter.name}` raised {type(exc).__name__}: {exc}")
        after = take_snapshot(self.graph.root, self.fingerprinter.hasher, ignore)
        changes = diff_snapshots(before, after)
        owned = GlobSet(doc.implement_patterns)
        out_of_scope = [p for p in changes.paths if not owned.matches(p)]
        record = AttemptRecord(
            attempt, self.adapter.name, result.completed, result.summary, result.duration, changes, out_of_scope,
            str(prompt_file),
        )
        self.events.agent_finished(doc.id, record)

        tampered = sorted(set(changes.paths) & self._protected)
        if tampered:
            raise ExecutionAborted(
                f"the agent modified protected file(s) {', '.join(tampered)} while implementing `{doc.id}`. "
                "Specs and rsde.toml are the source of truth and only humans may change them; "
                "review the change (e.g. `git diff`) and run again."
            )
        if result.deferred:
            raise ExecutionAborted(result.summary)
        return record
