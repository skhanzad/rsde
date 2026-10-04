"""Terminal styling: ANSI colours and status glyphs with graceful fallbacks.

Colour is on for TTYs and off otherwise; ``NO_COLOR`` disables it and
``FORCE_COLOR`` forces it. Glyphs fall back to ASCII on non-UTF-8 streams.
"""

from __future__ import annotations

import os
from typing import TextIO

from rsde.planning.status import Status

_SGR = {
    "bold": "1",
    "dim": "2",
    "red": "31",
    "green": "32",
    "yellow": "33",
    "blue": "34",
    "magenta": "35",
    "cyan": "36",
    "gray": "90",
}

# status → (unicode glyph, ascii glyph, colour)
_STATUS = {
    Status.SATISFIED: ("✓", "+", "green"),
    Status.FAILED: ("✗", "x", "red"),
    Status.STALE: ("↻", "~", "yellow"),
    Status.PENDING: ("○", "o", "gray"),
    Status.BLOCKED: ("⊘", "-", "magenta"),
    Status.INCOMPLETE: ("◐", "*", "yellow"),
    Status.UNVERIFIABLE: ("?", "?", "red"),
}


class Style:
    def __init__(self, color: bool = False, unicode: bool = True) -> None:
        self.color = color
        self.unicode = unicode

    @classmethod
    def for_stream(cls, stream: TextIO, mode: str = "auto") -> "Style":
        if mode == "always":
            color = True
        elif mode == "never" or os.environ.get("NO_COLOR"):
            color = False
        elif os.environ.get("FORCE_COLOR"):
            color = True
        else:
            color = bool(getattr(stream, "isatty", lambda: False)())
        encoding = (getattr(stream, "encoding", None) or "").lower().replace("-", "")
        return cls(color, unicode="utf" in encoding)

    def paint(self, text: str, *styles: str) -> str:
        if not self.color or not text or not styles:
            return text
        codes = ";".join(_SGR[s] for s in styles)
        return f"\033[{codes}m{text}\033[0m"

    def bold(self, text: str) -> str:
        return self.paint(text, "bold")

    def dim(self, text: str) -> str:
        return self.paint(text, "dim")

    def red(self, text: str) -> str:
        return self.paint(text, "red")

    def green(self, text: str) -> str:
        return self.paint(text, "green")

    def yellow(self, text: str) -> str:
        return self.paint(text, "yellow")

    def blue(self, text: str) -> str:
        return self.paint(text, "blue")

    def cyan(self, text: str) -> str:
        return self.paint(text, "cyan")

    def magenta(self, text: str) -> str:
        return self.paint(text, "magenta")

    def gray(self, text: str) -> str:
        return self.paint(text, "gray")

    def glyph(self, status: Status) -> str:
        uni, ascii_, colour = _STATUS[status]
        return self.paint(uni if self.unicode else ascii_, colour, "bold")

    def status(self, status: Status) -> str:
        _, _, colour = _STATUS[status]
        return f"{self.glyph(status)} {self.paint(status.value, colour)}"

    def ok(self, passed: bool) -> str:
        if passed:
            return self.paint("✓" if self.unicode else "+", "green", "bold")
        return self.paint("✗" if self.unicode else "x", "red", "bold")

    @property
    def tee(self) -> str:
        return "├── " if self.unicode else "|-- "

    @property
    def elbow(self) -> str:
        return "└── " if self.unicode else "`-- "

    @property
    def pipe(self) -> str:
        return "│   " if self.unicode else "|   "

    @property
    def arrow(self) -> str:
        return "→" if self.unicode else "->"

    @property
    def dash(self) -> str:
        return "—" if self.unicode else "-"
