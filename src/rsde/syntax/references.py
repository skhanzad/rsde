"""Grammar for list-valued directives: spec references, externals, capabilities.

::

    list        = item { "," item } [ description ]
    description = ( "—" | "–" | "--" | "-" ) text
    ref         = wikilink | md-link | path | name
    wikilink    = "[[" target [ "#" anchor ] [ "|" alias ] "]]"
    md-link     = "[" label "](" target ")"
    external    = kind ":" name [ "@" version ]      kind = tool | env | pkg | service

A *path* contains a ``/`` or ends in ``.md``; anything else is a *name* that is
resolved against the workspace like an Obsidian wikilink.
"""

from __future__ import annotations

import difflib
import re

from rsde.diagnostics import SourceSpan
from rsde.syntax.ast import EXTERNAL_KINDS, ExternalDep, RefStyle, SpecRef

_DESC_AT = re.compile(r"(?:—|–|--|-)(?:[ \t]+|$)")
_DESC_SEP = re.compile(r"[ \t]*[—–][ \t]*|[ \t]+--?[ \t]+")
_BARE = re.compile(r"[^\s,]+")
_WIKILINK = re.compile(r"\[\[(?P<target>[^\]|#]*)(?:#[^\]|]*)?(?:\|(?P<alias>[^\]]*))?\]\]")
_MDLINK = re.compile(r"\[(?P<label>[^\]]*)\]\((?P<target><[^>]*>|[^)\s]+)(?:[ \t]+\"[^\"]*\")?\)")
_EXTERNAL = re.compile(r"^(?P<kind>[a-z][a-z0-9+.-]+):(?P<name>[^\s@]+)(?:@(?P<version>\S+))?$")


class ReferenceSyntaxError(ValueError):
    """A malformed reference list. ``help`` suggests a fix."""

    def __init__(self, message: str, help: str | None = None, code: str = "E104") -> None:
        super().__init__(message)
        self.message = message
        self.help = help
        self.code = code


def strip_code(text: str) -> str:
    """Remove a single pair of surrounding backticks: ```foo``` → ``foo``."""
    text = text.strip()
    if len(text) >= 2 and text[0] == "`" and text[-1] == "`" and "`" not in text[1:-1]:
        return text[1:-1].strip()
    return text


def split_description(value: str) -> tuple[str, str]:
    """Split ``"head — description"`` into its two halves."""
    match = _DESC_SEP.search(value)
    if match is None:
        return value.strip(), ""
    return value[: match.start()].strip(), value[match.end() :].strip()


def split_items(value: str) -> tuple[list[str], str]:
    """Tokenize ``"a, [[b]], [c](d.md) — text"`` into items and a description."""
    items: list[str] = []
    description = ""
    pos, n = 0, len(value)
    while True:
        while pos < n and value[pos].isspace():
            pos += 1
        if pos >= n:
            if items:
                raise ReferenceSyntaxError("trailing ',' with no item after it")
            raise ReferenceSyntaxError("expected at least one item")
        if value.startswith("[[", pos):
            end = value.find("]]", pos)
            if end < 0:
                raise ReferenceSyntaxError(f"unterminated wikilink {value[pos:]!r}", "close it with ']]'")
            items.append(value[pos : end + 2])
            pos = end + 2
        elif value[pos] == "[":
            match = _MDLINK.match(value, pos)
            if match is None:
                raise ReferenceSyntaxError(
                    f"malformed Markdown link {value[pos:]!r}", "write links as [label](path/to/spec.md)"
                )
            items.append(match.group(0))
            pos = match.end()
        else:
            match = _BARE.match(value, pos)
            if match is None:
                raise ReferenceSyntaxError("empty item before ','")
            items.append(match.group(0))
            pos = match.end()
        while pos < n and value[pos] in " \t":
            pos += 1
        if pos >= n:
            break
        if value[pos] == ",":
            pos += 1
            continue
        sep = _DESC_AT.match(value, pos)
        if sep is not None:
            description = value[sep.end() :].strip()
            break
        raise ReferenceSyntaxError(
            f"unexpected {value[pos:]!r} after {items[-1]!r}",
            "separate items with ',' and put a description after ' — ', "
            "e.g. `@spec specs/storage.spec.md — persistence`",
        )
    return items, description


def parse_item(item: str, span: SourceSpan, *, allow_external: bool) -> SpecRef | ExternalDep:
    """Classify one list item as a spec reference or an external dependency."""
    if item.startswith("`") and item.endswith("`"):
        item = strip_code(item)
    if item.startswith("[["):
        match = _WIKILINK.fullmatch(item)
        target = match.group("target").strip() if match else ""
        if not target:
            raise ReferenceSyntaxError(f"empty wikilink {item!r}", "write wikilinks as [[spec-name]]")
        return SpecRef(target, RefStyle.WIKILINK, span, (match.group("alias") or "").strip(), item)
    if item.startswith("["):
        match = _MDLINK.fullmatch(item)
        if match is None:
            raise ReferenceSyntaxError(f"malformed Markdown link {item!r}")
        target = match.group("target").strip("<>").split("#", 1)[0].strip()
        if re.match(r"^[a-z][a-z0-9+.-]*://", target):
            raise ReferenceSyntaxError(
                f"remote reference {target!r} is not supported", "spec references must point at files in the workspace"
            )
        if not target:
            raise ReferenceSyntaxError(f"Markdown link {item!r} has no target")
        return SpecRef(target, RefStyle.MARKDOWN, span, match.group("label").strip(), item)

    external = _EXTERNAL.match(item)
    if external is not None:
        kind = external.group("kind")
        if kind not in EXTERNAL_KINDS:
            if "//" in item:
                raise ReferenceSyntaxError(
                    f"remote reference {item!r} is not supported", "spec references must point at files in the workspace"
                )
            close = difflib.get_close_matches(kind, EXTERNAL_KINDS, n=1)
            hint = f"did you mean `{close[0]}:`? " if close else ""
            raise ReferenceSyntaxError(
                f"unknown external dependency kind {kind!r}",
                f"{hint}external dependencies are written tool:<name>, env:<NAME>, pkg:<name> or service:<name>",
                code="E105",
            )
        if not allow_external:
            raise ReferenceSyntaxError(
                f"{item!r} is an external dependency, but @spec only links child specs",
                f"declare it with `@depends {item}`",
                code="E105",
            )
        return ExternalDep(kind, external.group("name"), span, external.group("version") or "")

    if "/" in item or item.endswith(".md") or item.startswith("."):
        return SpecRef(item, RefStyle.PATH, span, "", item)
    return SpecRef(item, RefStyle.NAME, span, "", item)
