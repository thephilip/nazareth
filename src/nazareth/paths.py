"""Locate a project's nazareth state directory."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

STATE_DIRNAME = ".nazareth"

# Derived files are rebuildable from the ledger, so they never belong in git.
# Whether to commit the ledger itself is left to the project.
_STATE_GITIGNORE = "index.db*\nvectors.db*\ngraph.db*\n*.lock\n"


def find_project_dir(start: Path | None = None) -> Path:
    """NAZARETH_PROJECT_DIR wins; otherwise search upward from the start dir
    (default: CLAUDE_PROJECT_DIR when Claude Code launched us, else cwd) for
    an existing state dir, then a git root, falling back to the start dir."""
    env = os.environ.get("NAZARETH_PROJECT_DIR")
    if env:
        return Path(env).expanduser().resolve()
    claude_dir = os.environ.get("CLAUDE_PROJECT_DIR")
    start = (start or (Path(claude_dir) if claude_dir else Path.cwd())).resolve()
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
    def vectors(self) -> Path:
        return self.state / "vectors.db"

    @property
    def graph(self) -> Path:
        return self.state / "graph.db"

    @property
    def graph_lock(self) -> Path:
        return self.state / "graph.lock"

    @property
    def initialized(self) -> bool:
        return self.state.is_dir()

    def init(self) -> None:
        self.state.mkdir(parents=True, exist_ok=True)
        self.ensure_gitignore()

    def ensure_gitignore(self) -> None:
        """Make sure .nazareth/.gitignore covers every derived file, appending
        only the missing patterns. Never creates the state dir itself."""
        if not self.initialized:
            return
        gitignore = self.state / ".gitignore"
        existing = gitignore.read_text() if gitignore.exists() else ""
        lines = {line.strip() for line in existing.splitlines()}
        missing = [p for p in _STATE_GITIGNORE.splitlines() if p not in lines]
        if missing:
            sep = "\n" if existing and not existing.endswith("\n") else ""
            with gitignore.open("a") as f:
                f.write(sep + "".join(f"{p}\n" for p in missing))
