r"""Line scanner for RSDE spec files.

A spec file is ordinary Markdown. The scanner walks it line by line and pulls
out *directives* (lines that start with ``@name``) while keeping everything
else as prose. It understands just enough Markdown to be safe:

* fenced code blocks (``` and ~~~) and HTML comments are opaque, so examples
  inside documentation are never mistaken for directives;
* a directive may sit inside a list item (``- @behavior ...``) and may be
  written with a trailing colon (``@goal: ...``);
* a directive continues onto following lines that are indented deeper than
  the directive itself;
* a directive with no inline value takes its value from an immediately
  following fenced code block (useful for multi-line ``@verify`` scripts);
* ``\@`` at the start of a line escapes a literal ``@``;
* YAML front matter is skipped.
"""

from __future__ import annotations

import re
import textwrap
from dataclasses import dataclass

DIRECTIVE_RE = re.compile(
    r"^(?P<indent>[ \t]*)"
    r"(?:(?P<marker>[-*+]|\d{1,9}[.)])[ \t]+)?"
    r"@(?P<name>[A-Za-z][A-Za-z0-9_-]*):?"
    r"(?=[ \t]|$)"
    r"[ \t]*(?P<value>.*?)[ \t]*$"
)
FENCE_RE = re.compile(r"^(?P<indent>[ \t]*)(?P<fence>`{3,}|~{3,})(?P<info>.*)$")
CLOSING_FENCE_RE = re.compile(r"^[ \t]*(?P<fence>`{3,}|~{3,})[ \t]*$")
HEADING_RE = re.compile(r"^ {0,3}(?P<hashes>#{1,6})(?:[ \t]+(?P<text>.*?))?(?:[ \t]+#+)?[ \t]*$")
COMMENT_START_RE = re.compile(r"^[ \t]*<!--")
ESCAPED_AT_RE = re.compile(r"^(?P<prefix>[ \t]*(?:(?:[-*+]|\d{1,9}[.)])[ \t]+)?)\\@")


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
    lines = text.splitlines()
    n = len(lines)
    directives: list[RawDirective] = []
    headings: list[Heading] = []
    prose: list[tuple[int, str]] = []
    issues: list[ScanIssue] = []

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

        if COMMENT_START_RE.match(line):
            end = _find_comment_end(lines, i)
            if end is None:
                issues.append(ScanIssue(i + 1, "this HTML comment is never closed, so the rest of the file is ignored"))
                break
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

    scanned = ScannedDocument(tuple(lines), tuple(directives), tuple(headings), "", tuple(issues))
    title = scanned.title
    title_index = title.line - 1 if title is not None else -1
    body = _tidy([text for index, text in prose if index != title_index])
    return ScannedDocument(tuple(lines), tuple(directives), tuple(headings), body, tuple(issues))


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
        if (
            DIRECTIVE_RE.match(nxt)
            or _open_fence(nxt)
            or HEADING_RE.match(nxt)
            or COMMENT_START_RE.match(nxt)
            or _indent_width(nxt) <= indent
        ):
            break
        parts.append(nxt.strip())
        j += 1
    return RawDirective(name, "\n".join(parts), i + 1, column, length, j), j


def _skip_front_matter(lines: list[str]) -> int:
    if lines and lines[0].strip() == "---":
        for j in range(1, len(lines)):
            if lines[j].strip() in ("---", "..."):
                return j + 1
    return 0


def _open_fence(line: str) -> _Fence | None:
    match = FENCE_RE.match(line)
    if match is None:
        return None
    fence, info = match.group("fence"), match.group("info").strip()
    if fence[0] == "`" and "`" in info:
        return None  # inline code such as ```x```, not a fence
    return _Fence(fence[0], len(fence), info)


def _find_fence_end(lines: list[str], start: int, fence: _Fence) -> int | None:
    for j in range(start, len(lines)):
        match = CLOSING_FENCE_RE.match(lines[j])
        if match and match.group("fence")[0] == fence.char and len(match.group("fence")) >= fence.length:
            return j
    return None


def _find_comment_end(lines: list[str], start: int) -> int | None:
    first = lines[start]
    if "-->" in first[first.index("<!--") + 4 :]:
        return start
    for j in range(start + 1, len(lines)):
        if "-->" in lines[j]:
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
