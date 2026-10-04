"""Intent inheritance: goals, constraints and invariants flow *down* the hierarchy.

A spec's effective intent is the chain of frames from the root to the spec
itself. Constraints and invariants accumulate: a child must honour its own
and every ancestor's. Goals are carried along as context so an agent knows
why the part it is building exists.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from rsde.graph.model import SpecGraph
from rsde.syntax.ast import SpecDocument, Text


@dataclass(frozen=True)
class IntentFrame:
    spec_id: str
    title: str
    goals: tuple[Text, ...]
    constraints: tuple[Text, ...]
    invariants: tuple[Text, ...]

    @classmethod
    def of(cls, doc: SpecDocument) -> "IntentFrame":
        return cls(doc.id, doc.title, doc.goals, doc.constraints, doc.invariants)

    @property
    def empty(self) -> bool:
        return not (self.goals or self.constraints or self.invariants)


@dataclass(frozen=True)
class Intent:
    """The effective intent of one spec: frames from the root (first) to the spec (last)."""

    spec_id: str
    frames: tuple[IntentFrame, ...]

    @property
    def own(self) -> IntentFrame:
        return self.frames[-1]

    @property
    def inherited(self) -> tuple[IntentFrame, ...]:
        return self.frames[:-1]

    def constraints(self) -> list[tuple[str, Text]]:
        """Every constraint that applies, as ``(origin spec id, text)``, outermost first."""
        return [(f.spec_id, t) for f in self.frames for t in f.constraints]

    def invariants(self) -> list[tuple[str, Text]]:
        return [(f.spec_id, t) for f in self.frames for t in f.invariants]

    def inherited_digest(self) -> str:
        """Changes whenever an ancestor changes what it passes down."""
        h = hashlib.sha256()
        for frame in self.inherited:
            h.update(frame.spec_id.encode())
            for kind, items in (("goal", frame.goals), ("constraint", frame.constraints), ("invariant", frame.invariants)):
                for item in items:
                    h.update(f"\0{kind}\0{item.text}".encode())
            h.update(b"\n")
        return h.hexdigest()


def resolve_intent(graph: SpecGraph, spec_id: str) -> Intent:
    frames = tuple(IntentFrame.of(graph.specs[s]) for s in graph.hierarchy.lineage(spec_id))
    return Intent(spec_id, frames)
