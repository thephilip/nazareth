"""Locate a project's nazareth state directory."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

STATE_DIRNAME = ".nazareth"

# Derived files are rebuildable from the ledger, so they never belong in git.
# Whether to commit the ledger itself is left to the project.
_STATE_GITIGNORE = "index.db*\n*.lock\n"


def find_project_dir(start: Path | None = None) -> Path:
    """NAZARETH_PROJECT_DIR wins; otherwise the nearest ancestor that already
    has a state dir, then the nearest git root, then the start dir itself."""
    env = os.environ.get("NAZARETH_PROJECT_DIR")
    if env:
        return Path(env).expanduser().resolve()
    start = (start or Path.cwd()).resolve()
    candidates = [start, *start.parents]
    for marker in (STATE_DIRNAME, ".git"):
        for d in candidates:
            if (d / marker).exists():
                return d
    return start


@dataclass(frozen=True)
class StatePaths:
    project: Path

    @property
    def state(self) -> Path:
        return self.project / STATE_DIRNAME

    @property
    def ledger(self) -> Path:
        return self.state / "ledger.jsonl"

    @property
    def index(self) -> Path:
        return self.state / "index.db"

    @property
    def initialized(self) -> bool:
        return self.state.is_dir()

    def init(self) -> None:
        self.state.mkdir(parents=True, exist_ok=True)
        gitignore = self.state / ".gitignore"
        if not gitignore.exists():
            gitignore.write_text(_STATE_GITIGNORE)
