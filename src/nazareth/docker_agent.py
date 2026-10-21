"""Docker Agent integration: one team config with a coordinator and the three
lenses, each running its own memory server with a fixed identity.

Nothing here runs unless the human asks for it with `nazareth init --docker`.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from string import Template

from nazareth.claude_code import LENSES, _write, lens_body, template
from nazareth.memory.service import NazarethError

CONFIG_DIR = ".docker-agent"
CONFIG = "nazareth.yaml"
# Tried in order; docker-agent picks the first whose credentials are set, and the
# local Docker Model Runner needs none.
DEFAULT_MODELS = (
    "anthropic/claude-sonnet-5-5",
    "openai/gpt-5.6",
    "google/gemini-3.5-flash",
    "dmr/ai/qwen3",
)
_MODEL = re.compile(r"[A-Za-z0-9][\w.:-]*(/[\w.:-]+)*")


def _description(lens: str) -> str:
    """The lens's routing description, shared with its Claude Code subagent."""
    for line in template("claude", f"{lens}.frontmatter").splitlines():
        if line.startswith("description: "):
            return line.removeprefix("description: ")
    raise AssertionError(f"{lens}.frontmatter has no description")


def render(model: str | None = None) -> str:
    """The team config. `model` (provider/model, or a model name) replaces the
    default candidate list."""
    if model is not None and not _MODEL.fullmatch(model):
        raise NazarethError(f"--model {model!r} should look like provider/model, e.g. "
                            "anthropic/claude-sonnet-5-5 or dmr/ai/qwen3.")
    candidates = [model] if model else list(DEFAULT_MODELS)
    return Template(template("docker", CONFIG)).substitute(
        candidates=json.dumps(candidates),
        **{lens.removeprefix("lens-"): json.dumps(_description(lens)) for lens in LENSES},
    )


def files(model: str | None = None) -> dict[str, str]:
    """Every file `install` writes, by path relative to the project."""
    out = {f"{CONFIG_DIR}/{CONFIG}": render(model),
           f"{CONFIG_DIR}/coordinator.md": template("docker", "coordinator.md")}
    for lens in LENSES:
        out[f"{CONFIG_DIR}/lenses/{lens}.md"] = lens_body(lens)
    return out


def install(project: Path, model: str | None = None) -> list[tuple[str, str]]:
    """Add the Docker Agent team config and lens prompts to a project.
    Existing files are never overwritten. Returns (path, what happened) pairs."""
    report: list[tuple[str, str]] = []
    for rel, text in files(model).items():  # rendered first, so a bad --model writes nothing
        path = project / rel
        if path.exists():
            report.append((rel, "exists, left as is"))
        else:
            _write(path, text)
            report.append((rel, "created"))
    return report


def found() -> bool:
    """Whether Docker Agent is installed, standalone or as a docker CLI plugin."""
    plugin = Path.home() / ".docker" / "cli-plugins" / "docker-agent"
    return shutil.which("docker-agent") is not None or plugin.exists()
