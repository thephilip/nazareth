"""Tests for the Docker Agent integration (team config, prompts, install)."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import jsonschema
import pytest
import yaml

from nazareth.claude_code import HUMAN_COMMANDS, LENSES, _agent_file, lens_body
from nazareth.cli import main
from nazareth.docker_agent import CONFIG, CONFIG_DIR, DEFAULT_MODELS, files, install, render
from nazareth.memory.service import NazarethError

SCHEMA = Path(__file__).parent / "data" / "docker-agent-schema.json"
AGENTS = ("root", *LENSES)
WRITE_TOOLS = {"decide", "constrain", "ask", "log", "phase"}


def _config(model: str | None = None) -> dict:
    return yaml.safe_load(render(model))


def _toolsets(agent: dict, kind: str) -> list[dict]:
    return [t for t in agent.get("toolsets", []) if t["type"] == kind]


def test_config_matches_docker_agent_schema() -> None:
    jsonschema.Draft7Validator(json.loads(SCHEMA.read_text())).validate(_config())


def test_every_agent_has_its_own_memory_server() -> None:
    agents = _config()["agents"]
    assert set(agents) == set(AGENTS)
    actors = []
    for name in AGENTS:
        [memory] = _toolsets(agents[name], "mcp")
        assert memory["name"] == "nazareth"
        assert memory["command"] == "nazareth" and memory["args"] == ["serve"]
        actors.append(memory["env"]["NAZARETH_ACTOR"])
    assert actors == ["agent:main", "lens:architect", "lens:code", "lens:reviewer"]


def test_read_only_agents_cannot_change_files() -> None:
    agents = _config()["agents"]
    for name in ("root", "lens-architect", "lens-reviewer"):
        assert all(t["readonly"] for t in _toolsets(agents[name], "filesystem")), name
        assert all(t["readonly"] for t in _toolsets(agents[name], "git")), name
    assert not _toolsets(agents["root"], "shell")
    assert not _toolsets(agents["lens-architect"], "shell")
    [fs] = _toolsets(agents["lens-code"], "filesystem")
    assert not fs.get("readonly")


def test_root_delegates_to_the_lenses() -> None:
    root = _config()["agents"]["root"]
    assert root["sub_agents"] == list(LENSES)
    assert root["hooks"]["session_start"][0]["command"] == "nazareth context"


def test_lens_descriptions_match_claude_code() -> None:
    agents = _config()["agents"]
    for lens in LENSES:
        _, fm, _ = _agent_file(lens).split("---\n", 2)
        assert agents[lens]["description"] == yaml.safe_load(fm)["description"]


def test_models_resolve_and_default_to_first_available() -> None:
    config = _config()
    assert config["models"]["lens"] == {"first_available": list(DEFAULT_MODELS)}
    assert {a["model"] for a in config["agents"].values()} == {"lens"}
    assert "providers" not in config  # the gateway example stays commented out


def test_model_option_replaces_the_candidates() -> None:
    assert _config("dmr/ai/qwen3")["models"]["lens"] == {"first_available": ["dmr/ai/qwen3"]}


@pytest.mark.parametrize("model", ["", "a b", "x/", "/x", "evil\nkey: 1"])
def test_bad_model_is_rejected(model: str) -> None:
    with pytest.raises(NazarethError, match="provider/model"):
        render(model)


def test_write_tools_are_auto_approved() -> None:
    allow = _config()["permissions"]["allow"]
    assert set(allow) == {f"nazareth_{tool}" for tool in WRITE_TOOLS}


def _denied(config: dict, cmd: str) -> bool:
    """docker-agent's matching: an anchored, case-insensitive glob on the argument."""
    for rule in config["permissions"]["deny"]:
        tool, _, glob = rule.partition(":cmd=")
        pattern = ".*".join(map(re.escape, glob.split("*")))
        if tool == "shell" and re.fullmatch(pattern, cmd, re.IGNORECASE | re.DOTALL):
            return True
    return False


def test_shell_cannot_run_the_human_cli() -> None:
    config = _config()
    assert len(config["permissions"]["deny"]) == len(HUMAN_COMMANDS)
    for c in HUMAN_COMMANDS:
        assert _denied(config, f"nazareth {c} x"), c
    assert _denied(config, "cd src && nazareth answer q-1 yes")
    for ok in ("nazareth status", "nazareth phase", "nazareth query decide",
               'grep -n "nazareth log" README.md', "pytest"):
        assert not _denied(config, ok), ok


def test_instruction_files_exist_inside_the_config_dir(tmp_path: Path) -> None:
    install(tmp_path)
    config_dir = tmp_path / CONFIG_DIR
    for name, agent in _config()["agents"].items():
        path = (config_dir / agent["instruction_file"]).resolve()
        assert path.is_relative_to(config_dir.resolve()) and path.is_file(), name


@pytest.mark.parametrize("lens", LENSES)
def test_lens_prompt_is_the_same_on_both_hosts(tmp_path: Path, lens: str) -> None:
    install(tmp_path)
    docker = (tmp_path / CONFIG_DIR / "lenses" / f"{lens}.md").read_text()
    assert docker == lens_body(lens)
    assert _agent_file(lens).endswith("---\n" + docker)
    assert "Bash" not in docker and "lens-memory" not in docker


def test_install_is_idempotent_and_never_overwrites(tmp_path: Path) -> None:
    first = dict(install(tmp_path))
    assert set(first) == set(files()) and set(first.values()) == {"created"}
    mine = tmp_path / CONFIG_DIR / CONFIG
    mine.write_text("my own config")
    again = dict(install(tmp_path, "dmr/ai/qwen3"))
    assert set(again.values()) == {"exists, left as is"}
    assert mine.read_text() == "my own config"


def test_init_docker_via_cli(tmp_path: Path, capsys) -> None:
    main(["--dir", str(tmp_path), "init", "--docker", "--model", "dmr/ai/qwen3"])
    out = capsys.readouterr().out
    assert f"{CONFIG_DIR}/{CONFIG}: created" in out
    assert "docker agent run" in out
    assert (tmp_path / ".nazareth").is_dir()
    assert "dmr/ai/qwen3" in (tmp_path / CONFIG_DIR / CONFIG).read_text()


def test_init_claude_and_docker_together(tmp_path: Path) -> None:
    main(["--dir", str(tmp_path), "init", "--claude", "--docker"])
    assert (tmp_path / ".claude" / "agents" / "lens-code.md").exists()
    assert (tmp_path / CONFIG_DIR / "lenses" / "lens-code.md").exists()


@pytest.mark.parametrize("argv", [["--model", "dmr/ai/qwen3"], ["--docker", "--model", "a b"]])
def test_init_rejects_bad_options_before_writing(tmp_path: Path, capsys, argv) -> None:
    with pytest.raises(SystemExit):
        main(["--dir", str(tmp_path), "init", "--claude", *argv])
    assert "--model" in capsys.readouterr().err
    assert not list(tmp_path.iterdir())


@pytest.mark.skipif(shutil.which("docker-agent") is None, reason="docker-agent not installed")
def test_docker_agent_accepts_the_config(tmp_path: Path) -> None:
    install(tmp_path)
    subprocess.run(["docker-agent", "doctor", str(tmp_path / CONFIG_DIR / CONFIG)], check=True)
