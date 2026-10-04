"""Reconciliation behind ``rsde reconcile``: compare the repository with the specs, then converge.

::

    survey    structural drift that needs no commands: declared files that do
              not exist, files claimed by several specs, files no spec owns
    audit     run every spec's checks, ignoring cached evidence — the repository
              may have drifted in ways fingerprints cannot see (toolchain
              upgrades, environment changes, edits to files no spec owns)
    converge  unless --dry-run, execute the root spec so the agent fixes every
              non-conformant spec, then survey again

``execute`` is incremental and trusts fresh evidence, like ``make``.
``reconcile`` trusts nothing and reports drift first, like ``terraform plan``
followed by ``apply``. Reconcile never deletes code: files that no spec owns
are reported for a human to adopt into a spec or remove.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field, replace
from typing import Any, Sequence

from rsde.agents.base import AgentAdapter
from rsde.engine.executor import ExecuteOptions, ExecutionEvents, ExecutionReport, Executor, StepOutcome, new_run_id
from rsde.graph.analysis import execution_order
from rsde.graph.model import SpecGraph
from rsde.planning.planner import Action, PlanStep
from rsde.planning.status import SpecStatus, Status, has_own_checks
from rsde.repository.config import CONFIG_FILE, Workspace
from rsde.repository.files import GlobSet, matching_files
from rsde.repository.state import CheckResult, StateStore

#: Files that are documentation or repository metadata, never "unowned code".
NON_IMPLEMENTATION = (
    "**/*.md",
    CONFIG_FILE,
    ".gitignore",
    ".gitattributes",
    ".editorconfig",
    "LICENSE*",
    "COPYING*",
)


@dataclass(frozen=True)
class Drift:
    kind: str  # missing-files | failing-checks | unverifiable | shared-file | unowned-files
    message: str
    spec_id: str | None = None
    paths: tuple[str, ...] = ()

    @property
    def blocking(self) -> bool:
        """Blocking drift keeps the repository non-conformant; the rest is advisory."""
        return self.kind in ("missing-files", "failing-checks", "unverifiable")

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "spec": self.spec_id, "message": self.message, "paths": list(self.paths)}


@dataclass
class ReconcileReport:
    root_id: str
    before: list[Drift]
    after: list[Drift]
    audit: dict[str, list[CheckResult]]
    statuses: dict[str, SpecStatus]
    execution: ExecutionReport | None = None
    dry_run: bool = False
    conformant_specs: list[str] = field(default_factory=list)

    @property
    def conformant(self) -> bool:
        return self.statuses[self.root_id].satisfied and not any(d.blocking for d in self.after)

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": self.root_id,
            "dry_run": self.dry_run,
            "conformant": self.conformant,
            "drift_before": [d.to_dict() for d in self.before],
            "drift_after": [d.to_dict() for d in self.after],
            "conformant_specs": self.conformant_specs,
            "execution": self.execution.to_dict() if self.execution else None,
        }


def survey(graph: SpecGraph, files: Sequence[str]) -> list[Drift]:
    """Structural drift between the spec graph and the files on disk."""
    drift: list[Drift] = []
    owners: dict[str, list[str]] = defaultdict(list)
    for spec_id, doc in graph.specs.items():
        if not has_own_checks(doc) and graph.hierarchy.is_leaf(spec_id):
            drift.append(Drift("unverifiable", f"`{spec_id}` declares no checks and has no child specs", spec_id))
        if not doc.implement:
            continue
        missing = [t.pattern for t in doc.implement if not matching_files(files, [t.pattern])]
        if missing:
            drift.append(
                Drift("missing-files", f"`{spec_id}` declares files that do not exist", spec_id, tuple(missing))
            )
        for path in matching_files(files, doc.implement_patterns):
            owners[path].append(spec_id)
    for path, specs in sorted(owners.items()):
        if len(specs) > 1:
            drift.append(Drift("shared-file", f"`{path}` is claimed by " + ", ".join(f"`{s}`" for s in specs), None, (path,)))
    skip = GlobSet(NON_IMPLEMENTATION)
    spec_files = graph.spec_paths()
    unowned = tuple(f for f in files if f not in owners and f not in spec_files and not skip.matches(f))
    if unowned:
        drift.append(Drift("unowned-files", f"{len(unowned)} file(s) are not owned by any spec", None, unowned))
    return drift


def failing_drift(graph: SpecGraph, statuses: dict[str, SpecStatus]) -> list[Drift]:
    drift = []
    for spec_id in graph.specs:
        status = statuses[spec_id]
        if status.status is Status.FAILED and status.evidence is not None:
            commands = [c.command for c in status.evidence.failed_checks if not c.builtin]
            if commands:
                drift.append(
                    Drift("failing-checks", f"`{spec_id}`: " + "; ".join(f"`{c}` fails" for c in commands), spec_id)
                )
    return drift


class Reconciler:
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
        self.graph = graph
        self.events = events or ExecutionEvents()
        options = replace(options or ExecuteOptions(), force=False, trust_failures=True)
        self.executor = Executor(graph, workspace, adapter, options=options, events=self.events, state=state)

    def audit(self) -> dict[str, list[CheckResult]]:
        """Run every spec's own checks, bottom-up, regardless of cached evidence."""
        executor = self.executor
        run_id = new_run_id()
        run_dir = executor.state.run_dir(run_id)
        _, statuses = executor.assess()
        targets = [s for s in execution_order(self.graph) if has_own_checks(self.graph.specs[s])]
        results: dict[str, list[CheckResult]] = {}
        for index, spec_id in enumerate(targets, 1):
            step = PlanStep(spec_id, Action.CHECK, "audit", statuses[spec_id], ())
            self.events.step_started(step, index, len(targets))
            checks = executor.verify_spec(self.graph.specs[spec_id], run_dir, "audit")
            executor.record_evidence(self.graph.specs[spec_id], checks, run_id, attempts=0, violations=[])
            executor.state.save()
            passed = all(c.passed for c in checks)
            outcome = StepOutcome(
                spec_id,
                Action.CHECK,
                Status.SATISFIED if passed else Status.FAILED,
                "conforms" if passed else "drifted",
                checks,
            )
            self.events.step_finished(step, outcome)
            results[spec_id] = checks
        return results

    def run(self, *, dry_run: bool = False) -> ReconcileReport:
        executor = self.executor
        root = self.graph.root_id
        files = executor.fingerprinter.refresh_files()
        structural = survey(self.graph, files)
        audit = self.audit()
        _, statuses = executor.assess()
        before = structural + failing_drift(self.graph, statuses)

        execution = None
        if not dry_run and not statuses[root].satisfied:
            execution = executor.execute(root, command="reconcile")
            _, statuses = executor.assess()
        files = executor.fingerprinter.refresh_files()
        after = survey(self.graph, files) + failing_drift(self.graph, statuses)
        conformant = [s for s in self.graph.specs if statuses[s].satisfied]
        return ReconcileReport(root, before, after, audit, statuses, execution, dry_run, conformant)
