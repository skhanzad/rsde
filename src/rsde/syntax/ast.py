"""Typed abstract syntax tree for RSDE spec files.

The parser turns one Markdown file into one :class:`SpecDocument`. Every node
is immutable and carries the :class:`~rsde.diagnostics.SourceSpan` it came
from, so later stages (graph validation, planning, execution) can point back
at the exact line that caused a decision.
"""

from __future__ import annotations

import enum
import hashlib
from dataclasses import dataclass

from rsde.diagnostics import SourceSpan


class DirectiveKind(str, enum.Enum):
    """Every directive the language understands."""

    SPEC = "spec"
    DEPENDS = "depends"
    GOAL = "goal"
    REQUIRES = "requires"
    PROVIDES = "provides"
    CONSTRAINT = "constraint"
    BEHAVIOR = "behavior"
    INVARIANT = "invariant"
    VERIFY = "verify"
    IMPLEMENT = "implement"
    DONE = "done"


#: Directives whose meaning flows down the hierarchy to every descendant.
INHERITED_KINDS = frozenset({DirectiveKind.GOAL, DirectiveKind.CONSTRAINT, DirectiveKind.INVARIANT})


class RefStyle(str, enum.Enum):
    """How a spec reference was written."""

    PATH = "path"  #         ./storage.spec.md, specs/storage
    MARKDOWN = "markdown"  # [Storage](specs/storage.spec.md)
    WIKILINK = "wikilink"  # [[storage]]
    NAME = "name"  #         storage


#: External dependency kinds. ``tool`` and ``env`` are probed; the rest are declarative.
EXTERNAL_KINDS = ("tool", "env", "pkg", "service")
PROBED_EXTERNAL_KINDS = frozenset({"tool", "env"})


@dataclass(frozen=True)
class Directive:
    """A directive exactly as written, before semantic interpretation."""

    kind: DirectiveKind
    value: str
    span: SourceSpan
    block: bool = False


@dataclass(frozen=True)
class Text:
    """Free text carried by @goal, @constraint, @behavior, @invariant and @done."""

    text: str
    span: SourceSpan

    @property
    def one_line(self) -> str:
        """The text with continuation lines joined; spacing inside a line is preserved."""
        return " ".join(line.strip() for line in self.text.splitlines() if line.strip())


@dataclass(frozen=True)
class Capability:
    """A named capability, as used by @provides and @requires."""

    name: str
    span: SourceSpan
    description: str = ""


@dataclass(frozen=True)
class SpecRef:
    """A reference to another spec file (@spec or @depends)."""

    target: str
    style: RefStyle
    span: SourceSpan
    label: str = ""
    raw: str = ""


@dataclass(frozen=True)
class ExternalDep:
    """A dependency on something outside the spec graph, e.g. ``tool:git``."""

    kind: str
    name: str
    span: SourceSpan
    version: str = ""

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.name}"

    @property
    def probed(self) -> bool:
        return self.kind in PROBED_EXTERNAL_KINDS

    def __str__(self) -> str:
        return f"{self.key}@{self.version}" if self.version else self.key


@dataclass(frozen=True)
class VerifyCommand:
    """A shell command whose exit status is verification evidence."""

    command: str
    span: SourceSpan


@dataclass(frozen=True)
class ImplementTarget:
    """A workspace-relative path or glob owned by a spec."""

    pattern: str
    span: SourceSpan


@dataclass(frozen=True)
class SpecDocument:
    """The typed AST of one spec file."""

    id: str
    path: str
    title: str
    title_span: SourceSpan
    goals: tuple[Text, ...] = ()
    children: tuple[SpecRef, ...] = ()
    depends: tuple[SpecRef, ...] = ()
    externals: tuple[ExternalDep, ...] = ()
    requires: tuple[Capability, ...] = ()
    provides: tuple[Capability, ...] = ()
    constraints: tuple[Text, ...] = ()
    behaviors: tuple[Text, ...] = ()
    invariants: tuple[Text, ...] = ()
    verify: tuple[VerifyCommand, ...] = ()
    implement: tuple[ImplementTarget, ...] = ()
    done: tuple[Text, ...] = ()
    notes: str = ""
    directives: tuple[Directive, ...] = ()
    content_hash: str = ""

    @property
    def goal(self) -> str:
        return " ".join(g.one_line for g in self.goals)

    @property
    def implement_patterns(self) -> tuple[str, ...]:
        return tuple(t.pattern for t in self.implement)

    @property
    def verify_commands(self) -> tuple[str, ...]:
        return tuple(v.command for v in self.verify)

    def intent_digest(self) -> str:
        """Digest of the directives this spec passes down to its descendants."""
        h = hashlib.sha256()
        for d in self.directives:
            if d.kind in INHERITED_KINDS:
                h.update(f"{d.kind.value}\0{d.value}\0".encode())
        return h.hexdigest()
