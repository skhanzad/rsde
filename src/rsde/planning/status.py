"""Satisfaction semantics: evidence flows *up* the hierarchy.

A spec S is **satisfied** iff

1. S has at least one check (``@verify`` or ``@implement``) or at least one child,
2. S's own checks passed against S's *current* fingerprint,
3. every child of S is satisfied, and
4. every spec S depends on is satisfied.

Nothing else can satisfy a spec: not ``@done`` text, not an agent claiming
success, not evidence recorded for an older version of the inputs.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Mapping

from rsde.graph.analysis import execution_order
from rsde.graph.model import SpecGraph
from rsde.planning.fingerprint import Fingerprint
from rsde.repository.state import Evidence
from rsde.syntax.ast import SpecDocument


class Status(str, enum.Enum):
    SATISFIED = "satisfied"
    FAILED = "failed"
    STALE = "stale"
    PENDING = "pending"
    BLOCKED = "blocked"
    INCOMPLETE = "incomplete"
    UNVERIFIABLE = "unverifiable"


class OwnState(str, enum.Enum):
    """The state of a spec's own checks, ignoring children and dependencies."""

    PASS = "pass"
    FAIL = "fail"
    STALE = "stale"
    UNVERIFIED = "unverified"
    NO_CHECKS = "no-checks"


@dataclass(frozen=True)
class SpecStatus:
    spec_id: str
    status: Status
    own: OwnState
    reason: str
    evidence: Evidence | None = None
    blockers: tuple[str, ...] = ()

    @property
    def satisfied(self) -> bool:
        return self.status is Status.SATISFIED


def has_own_checks(doc: SpecDocument) -> bool:
    return bool(doc.verify or doc.implement)


def own_state(doc: SpecDocument, fingerprint: Fingerprint, evidence: Evidence | None) -> OwnState:
    if not has_own_checks(doc):
        return OwnState.NO_CHECKS
    if evidence is None:
        return OwnState.UNVERIFIED
    if evidence.fingerprint != fingerprint.value:
        return OwnState.STALE
    return OwnState.PASS if evidence.passed else OwnState.FAIL


def evaluate(
    graph: SpecGraph, fingerprints: Mapping[str, Fingerprint], evidence: Mapping[str, Evidence]
) -> dict[str, SpecStatus]:
    """Compute every spec's status bottom-up from fingerprints and recorded evidence."""
    result: dict[str, SpecStatus] = {}
    for spec_id in execution_order(graph):
        doc = graph.specs[spec_id]
        ev = evidence.get(spec_id)
        own = own_state(doc, fingerprints[spec_id], ev)
        children = graph.hierarchy.children(spec_id)
        result[spec_id] = _status(spec_id, own, ev, children, graph.dependencies.dependencies(spec_id), result)
    return {spec_id: result[spec_id] for spec_id in graph.specs}


def _status(
    spec_id: str,
    own: OwnState,
    ev: Evidence | None,
    children: tuple[str, ...],
    deps: list[str],
    known: dict[str, SpecStatus],
) -> SpecStatus:
    if own is OwnState.NO_CHECKS and not children:
        return SpecStatus(spec_id, Status.UNVERIFIABLE, own, "no @verify, no @implement and no child specs")
    if own is OwnState.FAIL:
        assert ev is not None
        missing = [c for c in ev.failed_checks if c.builtin]
        commands = [c for c in ev.failed_checks if not c.builtin]
        parts = ["declared implementation missing"] if missing else []
        if commands:
            more = f" (+{len(commands) - 1} more)" if len(commands) > 1 else ""
            parts.append(f"`{commands[0].command}` failed{more}")
        what = "; ".join(parts)
        if not what:
            what = "agent edited files outside its scope" if ev.scope_violations else "verification failed"
        return SpecStatus(spec_id, Status.FAILED, own, what, ev)
    waiting = tuple(d for d in deps if not known[d].satisfied)
    if waiting:
        return SpecStatus(spec_id, Status.BLOCKED, own, "waiting for " + ", ".join(waiting), ev, waiting)
    if own is OwnState.STALE:
        return SpecStatus(spec_id, Status.STALE, own, "inputs changed since it was last verified", ev)
    if own is OwnState.UNVERIFIED:
        return SpecStatus(spec_id, Status.PENDING, own, "never verified", ev)
    gaps = tuple(c for c in children if not known[c].satisfied)
    if gaps:
        return SpecStatus(
            spec_id, Status.INCOMPLETE, own, f"{len(gaps)} of {len(children)} child specs not satisfied", ev, gaps
        )
    if own is OwnState.PASS:
        assert ev is not None
        return SpecStatus(spec_id, Status.SATISFIED, own, f"verified {ev.verified_at}", ev)
    return SpecStatus(spec_id, Status.SATISFIED, own, "all child specs satisfied", ev)
