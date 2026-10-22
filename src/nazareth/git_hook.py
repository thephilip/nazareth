"""Commit logging: a git post-commit hook that records each commit in the ledger.

Agents forget to log what they build; commits are the one record of work that
always exists. The hook runs `nazareth record-commit`, which reads HEAD and
appends an event as actor `git`. No LLM or network is involved, and a failure
never fails the commit.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from nazareth.memory.service import Memory, NazarethError

GIT_ACTOR = "git"
COMMIT_ACTION = "commit"
HOOK_NAME = "post-commit"
HOOK_MARKER = "nazareth record-commit"
HOOK_SCRIPT = f"""#!/bin/sh
# Added by `nazareth init`: log each commit to project memory. Never fails the commit.
command -v nazareth >/dev/null 2>&1 && {HOOK_MARKER} >/dev/null 2>&1
exit 0
"""
FILES_SHOWN = 5


def _git(project: Path, *args: str) -> str | None:
    try:
        out = subprocess.run(["git", "-C", str(project), *args], check=False,
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def install(project: Path) -> list[tuple[str, str]]:
    """Add the post-commit hook. Never overwrites an existing hook, and leaves
    projects with a custom core.hooksPath (often tracked, e.g. husky) alone.
    Returns (path, what happened) pairs; nothing outside a git work tree."""
    if _git(project, "rev-parse", "--is-inside-work-tree") != "true":
        return []
    add_line = f"add `{HOOK_MARKER}` to it to log commits"
    if _git(project, "config", "core.hooksPath"):
        return [("core.hooksPath", f"is set, so no git hook was added; {add_line}")]
    hooks = _git(project, "rev-parse", "--git-path", "hooks")
    if hooks is None:
        return []
    path = (project / hooks / HOOK_NAME).resolve()
    rel = f"{hooks}/{HOOK_NAME}"
    if path.exists():
        if HOOK_MARKER in path.read_text(errors="replace"):
            return [(rel, "already logs commits, left as is")]
        return [(rel, f"exists, left as is; {add_line}")]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(HOOK_SCRIPT)
    path.chmod(0o755)
    return [(rel, "created: commits are logged to memory")]


def record(mem: Memory, project: Path) -> dict | None:
    """Log HEAD as a `commit` event, once per commit. None if there's nothing to log."""
    head = _git(project, "log", "-1", "--format=%H%n%s")
    if not head or "\n" not in head:
        return None
    sha, subject = head.split("\n", 1)
    short = sha[:10]
    if any(e.ref == short for e in mem.ledger.query(actor=GIT_ACTOR, action=COMMIT_ACTION,
                                                     limit=20)):
        return None
    files = (_git(project, "diff-tree", "--root", "--no-commit-id", "--name-only", "-r",
                  "HEAD") or "").splitlines()
    shown = ", ".join(files[:FILES_SHOWN]) + (f", … {len(files) - FILES_SHOWN} more"
                                               if len(files) > FILES_SHOWN else "")
    summary = f"{subject} ({shown})" if files else subject
    try:
        return mem.log(COMMIT_ACTION, summary, actor=GIT_ACTOR, ref=short)
    except NazarethError:
        return None
