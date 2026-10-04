"""Parser: one Markdown spec file → one typed :class:`SpecDocument`.

Parsing never touches other files. Cross-file questions (does a referenced
spec exist? who provides a capability?) are answered later by
:mod:`rsde.graph`, which keeps this stage pure, fast and deterministic.
"""

from __future__ import annotations

import difflib
import hashlib
import posixpath
import re

from rsde.diagnostics import Diagnostic, Diagnostics, Related, SourceSpan
from rsde.globs import GlobError, compile_pattern
from rsde.syntax.ast import (
    Capability,
    Directive,
    DirectiveKind,
    ExternalDep,
    ImplementTarget,
    SpecDocument,
    SpecRef,
    Text,
    VerifyCommand,
)
from rsde.syntax.lexer import RawDirective, scan
from rsde.syntax.references import ReferenceSyntaxError, parse_item, split_description, split_items, strip_code

CAPABILITY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:[.:/-][A-Za-z0-9_]+)*$")
_CLAIMS_EVERYTHING = frozenset({".", "*", "**", "**/*"})

#: One canonical example per directive, used in help messages and docs.
EXAMPLES: dict[DirectiveKind, str] = {
    DirectiveKind.SPEC: "@spec specs/storage.spec.md — persistence",
    DirectiveKind.DEPENDS: "@depends [[storage]], tool:python3",
    DirectiveKind.GOAL: "@goal Users can add, list and complete tasks from the terminal.",
    DirectiveKind.REQUIRES: "@requires todo.storage",
    DirectiveKind.PROVIDES: "@provides todo.storage — JSON-backed task repository",
    DirectiveKind.CONSTRAINT: "@constraint Use only the Python standard library.",
    DirectiveKind.BEHAVIOR: "@behavior `todo add \"Buy milk\"` prints the new task id.",
    DirectiveKind.INVARIANT: "@invariant Task ids are never reused.",
    DirectiveKind.VERIFY: "@verify python3 -m unittest tests.test_storage",
    DirectiveKind.IMPLEMENT: "@implement todo/storage.py, tests/test_storage.py",
    DirectiveKind.DONE: "@done All storage tests pass, including the error paths.",
}

_TEXT_KINDS = {
    DirectiveKind.GOAL: "goals",
    DirectiveKind.CONSTRAINT: "constraints",
    DirectiveKind.BEHAVIOR: "behaviors",
    DirectiveKind.INVARIANT: "invariants",
    DirectiveKind.DONE: "done",
}


def list_items(value: str) -> list[str]:
    """Split a comma-separated list that may wrap across continuation lines.

    A line break next to a comma (``a,⏎b`` or ``a⏎, b``) does not create an
    empty item; ``a,,b`` on one line still does, and is reported.
    """
    items: list[str] = []
    rows = value.split("\n")
    for r, row in enumerate(rows):
        parts = row.split(",")
        for k, part in enumerate(parts):
            part = part.strip()
            at_line_break = (k == len(parts) - 1 and r < len(rows) - 1) or (k == 0 and r > 0)
            if part or not at_line_break:
                items.append(part)
    return items


def spec_id_for(path: str) -> str:
    """Spec ids are workspace-relative paths without ``.spec.md`` / ``.md``."""
    for suffix in (".spec.md", ".md"):
        if path.endswith(suffix):
            return path[: -len(suffix)]
    return path


def content_hash(text: str) -> str:
    """Hash of a spec file, insensitive to trailing whitespace and line endings."""
    normalized = "\n".join(line.rstrip() for line in text.splitlines()).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def parse_document(text: str, path: str, spec_id: str | None = None) -> tuple[SpecDocument, list[Diagnostic]]:
    """Parse the text of one spec file. ``path`` is workspace-relative."""
    diags = Diagnostics()
    scanned = scan(text)
    for issue in scanned.issues:
        diags.warning("W102", issue.message, SourceSpan(path, issue.line, 1, 3))

    builder = _DocumentBuilder(path, diags)
    for raw in scanned.directives:
        builder.add(raw)

    heading = scanned.title
    if heading is not None and heading.text:
        title = heading.text
        title_span = SourceSpan(path, heading.line, 1, len(scanned.lines[heading.line - 1].rstrip()))
    else:
        stem = posixpath.basename(spec_id_for(path))
        title = stem.replace("-", " ").replace("_", " ").strip().capitalize() or path
        title_span = SourceSpan(path, 1, 1, 0)

    document = SpecDocument(
        id=spec_id if spec_id is not None else spec_id_for(path),
        path=path,
        title=title,
        title_span=title_span,
        goals=tuple(builder.texts["goals"]),
        children=tuple(builder.children),
        depends=tuple(builder.depends),
        externals=tuple(builder.externals),
        requires=tuple(builder.requires),
        provides=tuple(builder.provides),
        constraints=tuple(builder.texts["constraints"]),
        behaviors=tuple(builder.texts["behaviors"]),
        invariants=tuple(builder.texts["invariants"]),
        verify=tuple(builder.verify),
        implement=tuple(builder.implement),
        done=tuple(builder.texts["done"]),
        notes=scanned.prose,
        directives=tuple(builder.directives),
        content_hash=content_hash(text),
    )
    return document, list(diags)


class _DocumentBuilder:
    def __init__(self, path: str, diags: Diagnostics) -> None:
        self.path = path
        self.diags = diags
        self.directives: list[Directive] = []
        self.texts: dict[str, list[Text]] = {name: [] for name in _TEXT_KINDS.values()}
        self.children: list[SpecRef] = []
        self.depends: list[SpecRef] = []
        self.externals: list[ExternalDep] = []
        self.requires: list[Capability] = []
        self.provides: list[Capability] = []
        self.verify: list[VerifyCommand] = []
        self.implement: list[ImplementTarget] = []
        self._seen: dict[tuple[DirectiveKind, str], SourceSpan] = {}

    def add(self, raw: RawDirective) -> None:
        span = SourceSpan(self.path, raw.line, raw.column, raw.length)
        try:
            kind = DirectiveKind(raw.name.lower())
        except ValueError:
            self._unknown(raw, span)
            return

        value = raw.value.strip()
        if not value:
            self.diags.error(
                "E102",
                f"`@{kind.value}` needs a value",
                span,
                label="empty directive",
                help=f"for example: {EXAMPLES[kind]}",
            )
            return

        key = (kind, " ".join(value.split()))
        first = self._seen.get(key)
        if first is not None:
            self.diags.warning(
                "W101",
                f"`@{kind.value}` repeats an earlier declaration in this spec",
                span,
                label="duplicate",
                help="remove one of the two lines",
                related=(Related(first, "first declared here"),),
            )
            return
        self._seen[key] = span
        self.directives.append(Directive(kind, value, span, raw.block))

        if kind in _TEXT_KINDS:
            self.texts[_TEXT_KINDS[kind]].append(Text(value, span))
        elif kind is DirectiveKind.VERIFY:
            self._verify(value, span, raw.block)
        elif kind is DirectiveKind.IMPLEMENT:
            self._implement(value, span)
        elif kind in (DirectiveKind.REQUIRES, DirectiveKind.PROVIDES):
            target = self.requires if kind is DirectiveKind.REQUIRES else self.provides
            target.extend(self._capabilities(value, span, kind))
        elif kind is DirectiveKind.SPEC:
            self._references(value, span, kind, allow_external=False)
        elif kind is DirectiveKind.DEPENDS:
            self._references(value, span, kind, allow_external=True)

    def _unknown(self, raw: RawDirective, span: SourceSpan) -> None:
        known = [k.value for k in DirectiveKind]
        close = difflib.get_close_matches(raw.name.lower(), known, n=1, cutoff=0.6)
        hint = f"did you mean `@{close[0]}`?" if close else "known directives: " + ", ".join(f"@{k}" for k in known)
        self.diags.error(
            "E101",
            f"unknown directive `@{raw.name}`",
            span,
            label="not a directive",
            help=f"{hint} To start a line with a literal '@', escape it as '\\@'.",
        )

    def _verify(self, value: str, span: SourceSpan, block: bool) -> None:
        if block:
            command = value
        else:
            # Continuation lines fold into one command; a trailing '\' is optional.
            command = " ".join(part.strip().rstrip("\\").strip() for part in value.split("\n"))
            command = strip_code(command)
        self.verify.append(VerifyCommand(command, span))

    def _implement(self, value: str, span: SourceSpan) -> None:
        head, _description = split_description(value)
        for raw_pattern in list_items(head):
            pattern = strip_code(raw_pattern).replace("\\", "/")
            if not pattern:
                self.diags.error(
                    "E106",
                    "empty path in `@implement` list",
                    span,
                    help=f"for example: {EXAMPLES[DirectiveKind.IMPLEMENT]}",
                )
                continue
            if pattern.startswith("/") or re.match(r"^[A-Za-z]:/", pattern):
                self.diags.error(
                    "E106",
                    f"`{pattern}` is an absolute path",
                    span,
                    help="implementation paths are relative to the workspace root, e.g. `src/storage.py`",
                )
                continue
            normalized = posixpath.normpath(pattern)
            if normalized == ".." or normalized.startswith("../"):
                self.diags.error("E106", f"`{pattern}` points outside the workspace", span)
                continue
            if normalized in _CLAIMS_EVERYTHING:
                self.diags.error(
                    "E106",
                    f"`@implement {pattern}` would claim the entire workspace",
                    span,
                    help="list the specific files or directories this spec owns",
                )
                continue
            try:
                compile_pattern(normalized)
            except GlobError as exc:
                self.diags.error(
                    "E106",
                    str(exc),
                    span,
                    help="globs support *, **, ? and character classes such as [a-z] or [!_]",
                )
                continue
            if pattern.endswith("/"):
                normalized += "/"
            self.implement.append(ImplementTarget(normalized, span))

    def _capabilities(self, value: str, span: SourceSpan, kind: DirectiveKind) -> list[Capability]:
        head, description = split_description(value)
        names = [strip_code(name) for name in list_items(head)]
        out: list[Capability] = []
        for name in names:
            if not name:
                self.diags.error(
                    "E103", f"empty capability name in `@{kind.value}`", span, help=f"for example: {EXAMPLES[kind]}"
                )
            elif not CAPABILITY_RE.match(name):
                if any(ch.isspace() for ch in name):
                    help = (
                        "capability names are identifiers such as `todo.storage`; "
                        f"put prose after a dash: {EXAMPLES[kind]}"
                    )
                else:
                    help = "use letters, digits and '_', separated by '.', '-', '/' or ':', e.g. `todo.storage`"
                self.diags.error("E103", f"invalid capability name `{name}`", span, label="not an identifier", help=help)
            else:
                out.append(Capability(name, span, description if len(names) == 1 else ""))
        return out

    def _references(self, value: str, span: SourceSpan, kind: DirectiveKind, *, allow_external: bool) -> None:
        try:
            items, description = split_items(value)
        except ReferenceSyntaxError as exc:
            self.diags.error(exc.code, exc.message, span, help=exc.help or f"for example: {EXAMPLES[kind]}")
            return
        for item in items:
            try:
                parsed = parse_item(item, span, allow_external=allow_external)
            except ReferenceSyntaxError as exc:
                self.diags.error(exc.code, exc.message, span, help=exc.help)
                continue
            if isinstance(parsed, ExternalDep):
                self.externals.append(parsed)
            else:
                if description and not parsed.label and len(items) == 1:
                    parsed = SpecRef(parsed.target, parsed.style, parsed.span, description, parsed.raw)
                (self.children if kind is DirectiveKind.SPEC else self.depends).append(parsed)
