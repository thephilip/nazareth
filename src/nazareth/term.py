"""Terminal styling for the human CLI: colour roles, width and wrapping.

Colour is used only when the stream is a terminal and NO_COLOR is unset;
FORCE_COLOR turns it on regardless. Nothing here is used by the MCP server.
"""

from __future__ import annotations

import os
import shutil
import sys
import textwrap
from typing import TextIO

PIPED_WIDTH = 96
MIN_WIDTH, MAX_WIDTH = 60, 120

_CODES = {"bold": "1", "dim": "2", "red": "31", "green": "32", "yellow": "33",
          "blue": "34", "cyan": "36"}


class Term:
    def __init__(self, stream: TextIO | None = None) -> None:
        stream = stream or sys.stdout
        tty = hasattr(stream, "isatty") and stream.isatty()
        if os.environ.get("FORCE_COLOR"):
            self.color = True
        else:
            self.color = tty and not os.environ.get("NO_COLOR")
        if tty:
            cols = shutil.get_terminal_size((PIPED_WIDTH, 24)).columns
            self.width = max(MIN_WIDTH, min(MAX_WIDTH, cols))
        else:
            self.width = PIPED_WIDTH

    def style(self, text: str, *roles: str) -> str:
        if not self.color or not roles:
            return text
        return f"\033[{';'.join(_CODES[r] for r in roles)}m{text}\033[0m"

    # Roles, so commands say what a thing is rather than how it looks.
    def id(self, text: str) -> str:
        return self.style(text, "cyan")

    def heading(self, text: str) -> str:
        return self.style(text, "bold")

    def meta(self, text: str) -> str:
        return self.style(text, "dim")

    def warn(self, text: str) -> str:
        return self.style(text, "yellow")

    def alert(self, text: str) -> str:
        return self.style(text, "bold", "red")

    def ok(self, text: str) -> str:
        return self.style(text, "green")

    def actor(self, name: str, pad: int = 0) -> str:
        return self.style(name.ljust(pad), "green" if name == "human" else "blue")

    def status(self, status: str) -> str:
        if status == "pending":
            return self.warn(status)
        if status == "active":
            return self.ok(status)
        return self.meta(status)

    def clip(self, text: str, width: int) -> str:
        text = " ".join(text.split())
        return text if len(text) <= width else text[: width - 1] + "…"

    def wrap(self, text: str, indent: int, lines: int | None = None) -> list[str]:
        """`text` wrapped to the terminal at `indent`, at most `lines` lines
        (the last one ends in … when cut)."""
        width = max(20, self.width - indent)
        out: list[str] = []
        for para in text.splitlines() or [""]:
            out += textwrap.wrap(para, width) or [""]
        if lines is not None and len(out) > lines:
            out = out[:lines]
            out[-1] = out[-1][: width - 2].rstrip() + " …"
        return [" " * indent + line for line in out]
