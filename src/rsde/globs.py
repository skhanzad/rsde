"""Workspace glob patterns, as used by ``@implement`` and ``[project].ignore``.

``**`` crosses directory boundaries, ``*`` and ``?`` stay inside one path
segment, ``[...]`` is a character class (``[!...]`` negates), and a pattern
also claims everything below a directory it matches, so ``src/auth/``,
``src/auth`` and ``src/*`` all cover ``src/auth/login.py``.
"""

from __future__ import annotations

import re


class GlobError(ValueError):
    """A pattern that cannot be compiled."""


def glob_to_regex(pattern: str) -> str:
    """Translate a workspace glob into a regular expression (without anchors)."""
    out: list[str] = []
    i, n = 0, len(pattern)
    while i < n:
        c = pattern[i]
        if c == "*":
            if pattern.startswith("**/", i):
                out.append("(?:.*/)?")
                i += 3
            elif pattern.startswith("**", i):
                out.append(".*")
                i += 2
            else:
                out.append("[^/]*")
                i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        elif c == "[":
            j = pattern.find("]", i + 1)
            if j < 0:
                out.append(re.escape(c))
                i += 1
            else:
                body = pattern[i + 1 : j]
                negate = body.startswith("!")
                if negate:
                    body = body[1:]
                # Inside a class only ranges (a-z) are special; escape everything else.
                body = "".join(ch if ch.isalnum() or ch == "-" else "\\" + ch for ch in body)
                out.append("[" + ("^" if negate else "") + body + "]")
                i = j + 1
        else:
            out.append(re.escape(c))
            i += 1
    return "".join(out)


def compile_pattern(pattern: str) -> re.Pattern[str]:
    """Compile an ownership pattern; raises :class:`GlobError` for malformed ones."""
    body = glob_to_regex(pattern.rstrip("/"))
    try:
        return re.compile(f"^{body}(?:/.*)?$")
    except re.error as exc:
        raise GlobError(f"invalid glob `{pattern}`: {exc.msg}") from None


def literal_prefix(pattern: str) -> str:
    """The leading directories of ``pattern`` that contain no glob characters."""
    parts = []
    for part in pattern.rstrip("/").split("/"):
        if any(ch in part for ch in "*?["):
            break
        parts.append(part)
    return "/".join(parts)
