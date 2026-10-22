"""Fast checks of install.sh's argument handling. Real installs run in CI's install job."""

import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "install.sh"


def run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["sh", str(SCRIPT), *args], check=False, capture_output=True,
                          text=True, timeout=30, stdin=subprocess.DEVNULL)


def test_script_parses() -> None:
    subprocess.run(["sh", "-n", str(SCRIPT)], check=True)


def test_help_lists_every_option() -> None:
    out = run("--help")
    assert out.returncode == 0
    for option in ("--version", "--extras", "--core", "--from", "--yes", "--no-modify-path",
                   "--force", "--uninstall"):
        assert option in out.stdout


@pytest.mark.parametrize(("args", "message"), [
    (("--extras", "search,bogus"), "unknown extra: bogus"),
    (("--version",), "--version needs a value"),
    (("--frobnicate",), "unknown option"),
    (("--from", "/nonexistent", "--core"), "isn't a nazareth checkout"),
])
def test_bad_arguments_fail_before_installing(args: tuple[str, ...], message: str) -> None:
    out = run(*args)
    assert out.returncode == 1
    assert message in out.stderr
