r"""Line scanner for RSDE spec files.

A spec file is ordinary Markdown. The scanner walks it line by line and pulls
out *directives* (lines that start with ``@name``) while keeping everything
else as prose. It understands just enough Markdown to be safe:

* fenced code blocks (``` and ~~~, also opened on a list-item line) are
  opaque, so examples inside documentation are never mistaken for directives;
* HTML comments are blanked out wherever they start, so nothing hidden from a
  rendered page can act as a directive (``<!--`` inside inline code is text);
* a directive may sit inside a list item (``- @behavior ...``) and may be
  written with a colon (``@goal: ...`` or ``@goal:...``);
* a directive continues onto following lines that are indented deeper than
  the directive itself;
* a directive with no inline value takes its value from an immediately
  following fenced code block (useful for multi-line ``@verify`` scripts);
* ``\@`` at the start of a line escapes a literal ``@``;
* YAML front matter and a UTF-8 byte-order mark are skipped.
"""

from __future__ import annotations

import re
import textwrap
from dataclasses import dataclass

_MARKER = r"(?:[-*+]|\d{1,9}[.)])"  # a list-item marker: -, *, + or 1. / 1)
DIRECTIVE_RE = re.compile(
    r"^(?P<indent>[ \t]*)"
    rf"(?:(?P<marker>{_MARKER})[ \t]+)?"
    r"@(?P<name>[A-Za-z][A-Za-z0-9_-]*)"
    r"(?::|(?=[ \t]|$))"
    r"[ \t]*(?P<value>.*?)[ \t]*$"
)
FENCE_RE = re.compile(rf"^(?P<indent>[ \t]*)(?:{_MARKER}[ \t]+)?(?P<fence>`{{3,}}|~{{3,}})(?P<info>.*)$")
CLOSING_FENCE_RE = re.compile(r"^[ \t]*(?P<fence>`{3,}|~{3,})[ \t]*$")
HEADING_RE = re.compile(r"^ {0,3}(?P<hashes>#{1,6})(?:[ \t]+(?P<text>.*?))?(?:[ \t]+#+)?[ \t]*$")
ESCAPED_AT_RE = re.compile(rf"^(?P<prefix>[ \t]*(?:{_MARKER}[ \t]+)?)\\@")
_YAML_LINE = re.compile(r"^(?:[ \t].*|[-?][ \t].*|-|#.*|[A-Za-z0-9_\"'][^:]*:.*|[ \t]*)$")


@dataclass(frozen=True)
class RawDirective:
    """A directive as found in the source, before it is interpreted."""

    name: str
    value: str
    line: int
    column: int
    length: int
    end_line: int
    block: bool = False
    block_info: str = ""


@dataclass(frozen=True)
class Heading:
    level: int
    text: str
    line: int


@dataclass(frozen=True)
class ScanIssue:
    line: int
    message: str


@dataclass(frozen=True)
class ScannedDocument:
    lines: tuple[str, ...]
    directives: tuple[RawDirective, ...]
    headings: tuple[Heading, ...]
    prose: str
    issues: tuple[ScanIssue, ...]

    @property
    def title(self) -> Heading | None:
        """The first level-1 heading, falling back to the first heading of any level."""
        for heading in self.headings:
            if heading.level == 1:
                return heading
        return self.headings[0] if self.headings else None


@dataclass(frozen=True)
class _Fence:
    char: str
    length: int
    info: str


def scan(text: str) -> ScannedDocument:
    """Split a spec file into directives, headings and prose."""
    text = text.removeprefix("﻿")
    original = text.splitlines()
    issues: list[ScanIssue] = []
    lines, unclosed_comment = _mask_comments(original)
    if unclosed_comment is not None:
        issues.append(ScanIssue(unclosed_comment, "this HTML comment is never closed, so the rest of the file is ignored"))
    n = len(lines)
    directives: list[RawDirective] = []
    headings: list[Heading] = []
    prose: list[tuple[int, str]] = []

    i = _skip_front_matter(lines)
    while i < n:
        line = lines[i]

        fence = _open_fence(line)
        if fence is not None:
            end = _find_fence_end(lines, i + 1, fence)
            if end is None:
                issues.append(ScanIssue(i + 1, "this code fence is never closed, so the rest of the file is treated as code"))
                end = n - 1
            prose.extend((k, lines[k]) for k in range(i, end + 1))
            i = end + 1
            continue

        heading = HEADING_RE.match(line)
        if heading is not None:
            headings.append(Heading(len(heading.group("hashes")), (heading.group("text") or "").strip(), i + 1))
            prose.append((i, line))
            i += 1
            continue

        match = DIRECTIVE_RE.match(line)
        if match is not None:
            directive, i = _read_directive(lines, i, match, issues)
            directives.append(directive)
            continue

        prose.append((i, _unescape(line)))
        i += 1

    scanned = ScannedDocument(tuple(original), tuple(directives), tuple(headings), "", tuple(issues))
    title = scanned.title
    title_index = title.line - 1 if title is not None else -1
    body = _tidy([text for index, text in prose if index != title_index])
    return ScannedDocument(tuple(original), tuple(directives), tuple(headings), body, tuple(issues))


def _read_directive(
    lines: list[str], i: int, match: re.Match[str], issues: list[ScanIssue]
) -> tuple[RawDirective, int]:
    line = lines[i]
    name = match.group("name")
    value = match.group("value")
    column = match.start("name")  # the '@' sits just before the name; columns are 1-based
    length = max(len(line.rstrip()) - (column - 1), 1)
    indent = _indent_width(line)

    if not value:
        k = i + 1
        while k < len(lines) and not lines[k].strip():
            k += 1
        fence = _open_fence(lines[k]) if k < len(lines) else None
        if fence is not None:
            end = _find_fence_end(lines, k + 1, fence)
            if end is None:
                issues.append(ScanIssue(k + 1, "this code fence is never closed, so the rest of the file is treated as code"))
                body = lines[k + 1 :]
                end = len(lines) - 1
            else:
                body = lines[k + 1 : end]
            content = textwrap.dedent("\n".join(body)).strip("\n")
            directive = RawDirective(name, content, i + 1, column, length, end + 1, True, fence.info)
            return directive, end + 1

    parts = [value] if value else []
    j = i + 1
    while j < len(lines):
        nxt = lines[j]
        if not nxt.strip():
            break
        if DIRECTIVE_RE.match(nxt) or _open_fence(nxt) or HEADING_RE.match(nxt) or _indent_width(nxt) <= indent:
            break
        parts.append(nxt.strip())
        j += 1
    return RawDirective(name, "\n".join(parts), i + 1, column, length, j), j


def _mask_comments(lines: list[str]) -> tuple[list[str], int | None]:
    """Blank out HTML comments outside code fences, keeping every column in place.

    Returns the masked lines and the line where an unclosed comment starts.
    """
    out: list[str] = []
    fence: _Fence | None = None
    open_line: int | None = None
    for index, line in enumerate(lines):
        if fence is not None:
            out.append(line)
            if _closes(line, fence):
                fence = None
            continue
        if open_line is None:
            opened = _open_fence(line)
            if opened is not None:
                fence = opened
                out.append(line)
                continue
        chars = list(line)
        pos = 0
        while pos < len(line):
            if open_line is None:
                start = _comment_start(line, pos)
                if start < 0:
                    break
                open_line = index + 1
                mask_from, search_from = start, start + 4
            else:
                mask_from, search_from = pos, pos
            end = line.find("-->", search_from)
            stop = len(line) if end < 0 else end + 3
            chars[mask_from:stop] = " " * (stop - mask_from)
            pos = stop
            if end >= 0:
                open_line = None
        out.append("".join(chars))
    return out, open_line


def _comment_start(line: str, pos: int) -> int:
    """Index of the next ``<!--`` at or after ``pos`` that is not inside inline code."""
    i = pos
    while i < len(line):
        if line.startswith("<!--", i):
            return i
        if line[i] == "`":
            j = i
            while j < len(line) and line[j] == "`":
                j += 1
            closing = re.compile(rf"(?<!`){line[i:j]}(?!`)").search(line, j)
            i = closing.end() if closing else j
            continue
        i += 1
    return -1


def _skip_front_matter(lines: list[str]) -> int:
    """Skip a leading YAML block; a ``---`` rule followed by Markdown is not front matter."""
    if lines and lines[0].strip() == "---":
        for j in range(1, len(lines)):
            if lines[j].strip() in ("---", "..."):
                return j + 1 if all(_YAML_LINE.match(line) for line in lines[1:j]) else 0
    return 0


def _open_fence(line: str) -> _Fence | None:
    match = FENCE_RE.match(line)
    if match is None:
        return None
    fence, info = match.group("fence"), match.group("info").strip()
    if fence[0] == "`" and "`" in info:
        return None  # inline code such as ```x```, not a fence
    return _Fence(fence[0], len(fence), info)


def _closes(line: str, fence: _Fence) -> bool:
    match = CLOSING_FENCE_RE.match(line)
    return bool(match and match.group("fence")[0] == fence.char and len(match.group("fence")) >= fence.length)


def _find_fence_end(lines: list[str], start: int, fence: _Fence) -> int | None:
    for j in range(start, len(lines)):
        if _closes(lines[j], fence):
            return j
    return None


def _indent_width(line: str) -> int:
    expanded = line.expandtabs(4)
    return len(expanded) - len(expanded.lstrip())


def _unescape(line: str) -> str:
    return ESCAPED_AT_RE.sub(lambda m: m.group("prefix") + "@", line)


def _tidy(lines: list[str]) -> str:
    """Trim blank lines at both ends and collapse runs of blank lines."""
    out: list[str] = []
    blank = False
    for line in lines:
        if not line.strip():
            if out and not blank:
                out.append("")
            blank = True
            continue
        out.append(line.rstrip())
        blank = False
    while out and not out[-1]:
        out.pop()
    return "\n".join(out)
