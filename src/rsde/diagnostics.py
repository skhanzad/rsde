"""Structured, source-located diagnostics.

Every problem RSDE finds in a spec graph is a :class:`Diagnostic`: a stable
code such as ``E301``, a severity, a primary :class:`SourceSpan`, and optional
help text and related locations. Codes never change meaning, so scripts and
tests can rely on them. Rendering for humans lives in :mod:`rsde.cli.render`.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Iterable, Iterator


class Severity(enum.Enum):
    ERROR = "error"
    WARNING = "warning"


@dataclass(frozen=True, order=True)
class SourceSpan:
    """A position in a workspace file. Lines and columns are 1-based."""

    path: str
    line: int = 1
    column: int = 1
    length: int = 0

    def __str__(self) -> str:
        return f"{self.path}:{self.line}:{self.column}"

    def to_dict(self) -> dict[str, object]:
        return {"path": self.path, "line": self.line, "column": self.column, "length": self.length}


@dataclass(frozen=True)
class Related:
    """A secondary location that helps explain a diagnostic."""

    span: SourceSpan
    message: str


@dataclass(frozen=True)
class Diagnostic:
    code: str
    severity: Severity
    message: str
    span: SourceSpan | None = None
    label: str = ""
    help: str | None = None
    notes: tuple[str, ...] = ()
    related: tuple[Related, ...] = ()

    @property
    def is_error(self) -> bool:
        return self.severity is Severity.ERROR

    @property
    def title(self) -> str:
        return CODES.get(self.code, "")

    def sort_key(self) -> tuple[str, int, int, str]:
        if self.span is None:
            return ("", 0, 0, self.code)
        return (self.span.path, self.span.line, self.span.column, self.code)

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "severity": self.severity.value,
            "title": self.title,
            "message": self.message,
            "span": self.span.to_dict() if self.span else None,
            "label": self.label or None,
            "help": self.help,
            "notes": list(self.notes),
            "related": [{"span": r.span.to_dict(), "message": r.message} for r in self.related],
        }


class Diagnostics:
    """An ordered, append-only collection of diagnostics."""

    def __init__(self, items: Iterable[Diagnostic] = ()) -> None:
        self._items: list[Diagnostic] = list(items)

    def add(self, diagnostic: Diagnostic) -> Diagnostic:
        self._items.append(diagnostic)
        return diagnostic

    def error(
        self,
        code: str,
        message: str,
        span: SourceSpan | None = None,
        *,
        label: str = "",
        help: str | None = None,
        notes: Iterable[str] = (),
        related: Iterable[Related] = (),
    ) -> Diagnostic:
        return self.add(
            Diagnostic(code, Severity.ERROR, message, span, label, help, tuple(notes), tuple(related))
        )

    def warning(
        self,
        code: str,
        message: str,
        span: SourceSpan | None = None,
        *,
        label: str = "",
        help: str | None = None,
        notes: Iterable[str] = (),
        related: Iterable[Related] = (),
    ) -> Diagnostic:
        return self.add(
            Diagnostic(code, Severity.WARNING, message, span, label, help, tuple(notes), tuple(related))
        )

    def extend(self, items: Iterable[Diagnostic]) -> None:
        self._items.extend(items)

    @property
    def errors(self) -> list[Diagnostic]:
        return [d for d in self._items if d.is_error]

    @property
    def warnings(self) -> list[Diagnostic]:
        return [d for d in self._items if not d.is_error]

    def has_errors(self) -> bool:
        return any(d.is_error for d in self._items)

    def sorted(self) -> list[Diagnostic]:
        return sorted(self._items, key=Diagnostic.sort_key)

    def __iter__(self) -> Iterator[Diagnostic]:
        return iter(self._items)

    def __len__(self) -> int:
        return len(self._items)


#: Catalog of every diagnostic code RSDE can emit. ``E`` codes are errors that
#: make the graph invalid; ``W`` codes are warnings (errors under ``--strict``).
CODES: dict[str, str] = {
    # Syntax
    "E101": "unknown directive",
    "E102": "directive without a value",
    "E103": "invalid capability name",
    "E104": "malformed spec reference",
    "E105": "malformed external dependency",
    "E106": "invalid implementation path",
    "W101": "duplicate directive",
    "W102": "unterminated block",
    # References and hierarchy
    "E200": "root spec not found",
    "E201": "referenced spec not found",
    "E202": "ambiguous spec reference",
    "E203": "spec included by more than one parent",
    "E204": "hierarchy cycle",
    "E205": "detached spec",
    "E206": "unreadable spec file",
    "E207": "conflicting spec ids",
    "W201": "orphan spec file",
    # Dependency graph
    "E301": "missing provider",
    "E302": "duplicate provider",
    "E303": "dependency cycle",
    "W301": "spec requires its own capability",
    "W302": "external dependency unavailable",
    # Completeness
    "W401": "spec has no goal",
    "W402": "spec cannot be verified",
    "W403": "verified spec owns no files",
    "W404": "file owned by more than one spec",
}
