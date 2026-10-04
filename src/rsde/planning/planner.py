"""Execution planning: which specs to work on, in which order, and why.

A plan is a pure function of the graph, the target and the current statuses,
so ``rsde execute --plan`` shows exactly what a real run would attempt.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Mapping

from rsde.graph.analysis import scope
from rsde.graph.model import SpecGraph
from rsde.planning.status import OwnState, SpecStatus, Status, has_own_checks


class Action(str, enum.Enum):
    SKIP = "skip"  #                   fresh passing evidence; nothing to do
    VERIFY = "verify"  #               run checks; on failure invoke the agent, then re-verify
    CHECK = "check"  #                 run checks only (verify-only mode)
    ROLLUP = "rollup"  #               no own checks: satisfied when its children are
    UNVERIFIABLE = "unverifiable"  #   cannot be satisfied until it declares checks


_REASONS = {
    OwnState.UNVERIFIED: "never verified",
    OwnState.STALE: "inputs changed since last verification",
    OwnState.FAIL: "last verification failed",
    OwnState.PASS: "re-verification forced",
}


@dataclass(frozen=True)
class PlanStep:
    spec_id: str
    action: Action
    reason: str
    before: SpecStatus
    prerequisites: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "spec": self.spec_id,
            "action": self.action.value,
            "reason": self.reason,
            "status": self.before.status.value,
            "prerequisites": list(self.prerequisites),
        }


@dataclass(frozen=True)
class Plan:
    target: str
    steps: tuple[PlanStep, ...]

    @property
    def work(self) -> list[PlanStep]:
        return [s for s in self.steps if s.action in (Action.VERIFY, Action.CHECK)]

    def to_dict(self) -> dict[str, object]:
        return {"target": self.target, "steps": [s.to_dict() for s in self.steps]}


def build_plan(
    graph: SpecGraph,
    target: str,
    statuses: Mapping[str, SpecStatus],
    *,
    force: bool = False,
    verify_only: bool = False,
) -> Plan:
    ids = scope(graph, target)
    members = set(ids)
    steps = []
    for spec_id in ids:
        doc = graph.specs[spec_id]
        before = statuses[spec_id]
        prerequisites = tuple(
            p
            for p in dict.fromkeys([*graph.dependencies.dependencies(spec_id), *graph.hierarchy.children(spec_id)])
            if p in members
        )
        if before.status is Status.UNVERIFIABLE:
            action, reason = Action.UNVERIFIABLE, "declares no checks and has no child specs"
        elif not has_own_checks(doc):
            action, reason = Action.ROLLUP, "no own checks; satisfied when its child specs are"
        elif before.own is OwnState.PASS and not force:
            action, reason = Action.SKIP, "fresh passing evidence"
        else:
            action = Action.CHECK if verify_only else Action.VERIFY
            reason = _REASONS[before.own]
        steps.append(PlanStep(spec_id, action, reason, before, prerequisites))
    return Plan(target, tuple(steps))
