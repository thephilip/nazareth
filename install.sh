#!/bin/sh
# Install the nazareth CLI on macOS, Linux or WSL.
#
#   curl -LsSf https://raw.githubusercontent.com/thephilip/nazareth/main/install.sh | sh
#   curl -LsSf .../install.sh | sh -s -- --extras search,graph
#
# Installs the latest release with uv (`uv tool install`), which provides Python 3.12+
# itself. Rerun to upgrade; `--uninstall` removes it. Run with --help for options.

set -eu

REPO="https://github.com/thephilip/nazareth"
UV_INSTALLER="https://astral.sh/uv/install.sh"

version="${NAZARETH_VERSION:-}"
extras="${NAZARETH_EXTRAS-search}"
source_path=""
assume_yes=0
modify_path=1
force=0
uninstall=0

usage() {
    cat <<EOF
Install nazareth, agent-native shared project memory.

Usage: install.sh [options]

  --version TAG       release to install, e.g. v0.1.0 (default: the latest)
  --extras LIST       comma-separated extras: search, graph (default: search)
  --core              no extras: keyword search only
  --from PATH         install from a local checkout instead of a release
  --yes, -y           don't ask before installing uv
  --no-modify-path    don't add uv's tool directory to your shell's PATH
  --force             replace a development (editable) install
  --uninstall         remove nazareth
  --help, -h          show this help

NAZARETH_VERSION and NAZARETH_EXTRAS set the same defaults.
EOF
}

say() { printf '%s\n' "nazareth: $*"; }
die() { printf '%s\n' "nazareth: $*" >&2; exit 1; }
need_arg() {
    if [ $# -lt 2 ] || [ -z "$2" ]; then
        die "$1 needs a value (see --help)"
    fi
}

while [ $# -gt 0 ]; do
    case "$1" in
        --version) need_arg "$@"; version="$2"; shift 2 ;;
        --extras) need_arg "$@"; extras="$2"; shift 2 ;;
        --core) extras=""; shift ;;
        --from) need_arg "$@"; source_path="$2"; shift 2 ;;
        --yes|-y) assume_yes=1; shift ;;
        --no-modify-path) modify_path=0; shift ;;
        --force) force=1; shift ;;
        --uninstall) uninstall=1; shift ;;
        --help|-h) usage; exit 0 ;;
        *) die "unknown option: $1 (see --help)" ;;
    esac
done

# -- platform -------------------------------------------------------------------

case "$(uname -s)" in
    Darwin|Linux) ;;
    MINGW*|MSYS*|CYGWIN*)
        die "native Windows isn't supported yet. Run this inside WSL (https://learn.microsoft.com/windows/wsl/install)." ;;
    *) die "unsupported system: $(uname -s). nazareth runs on macOS, Linux and WSL." ;;
esac

# -- what to install ---------------------------------------------------------------

extras="$(printf '%s' "$extras" | tr -d ' ')"
for extra in $(printf '%s' "$extras" | tr ',' ' '); do
    case "$extra" in
        search|graph) ;;
        *) die "unknown extra: $extra (choose from search, graph)" ;;
    esac
done
spec="nazareth"
[ -n "$extras" ] && spec="nazareth[$extras]"
if [ -n "$source_path" ] && [ ! -f "$source_path/pyproject.toml" ]; then
    die "$source_path isn't a nazareth checkout (no pyproject.toml)."
fi

# -- uv ---------------------------------------------------------------------------

find_uv() {
    if command -v uv >/dev/null 2>&1; then
        command -v uv
        return
    fi
    for candidate in "${XDG_BIN_HOME:-}/uv" "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do
        if [ -x "$candidate" ]; then
            printf '%s\n' "$candidate"
            return
        fi
    done
}

confirm() {
    # Piped from curl, stdin is the script itself, so ask on the terminal.
    [ "$assume_yes" -eq 1 ] && return 0
    if ! { true </dev/tty; } 2>/dev/null; then
        die "$1 Rerun with --yes to allow it, or install uv first: $UV_INSTALLER"
    fi
    printf '%s [y/N] ' "nazareth: $1" >/dev/tty
    read -r answer </dev/tty || answer=""
    case "$answer" in y|Y|yes|YES) return 0 ;; *) return 1 ;; esac
}

uv="$(find_uv)"

if [ "$uninstall" -eq 1 ]; then
    [ -n "$uv" ] || die "uv isn't installed, so there's no nazareth tool install to remove."
    "$uv" tool uninstall nazareth
    say "removed. Project memory in .nazareth/ directories is untouched."
    exit 0
fi

if [ -z "$uv" ]; then
    command -v curl >/dev/null 2>&1 || die "uv is needed and curl isn't available to install it; see https://docs.astral.sh/uv/"
    confirm "nazareth installs with uv (https://docs.astral.sh/uv/), which isn't installed. Install uv now?" \
        || die "uv is needed; install it and rerun."
    if [ "$modify_path" -eq 1 ]; then
        curl -LsSf "$UV_INSTALLER" | sh
    else
        curl -LsSf "$UV_INSTALLER" | env UV_NO_MODIFY_PATH=1 sh
    fi
    uv="$(find_uv)"
    [ -n "$uv" ] || die "uv was installed but can't be found; open a new shell and rerun."
fi

# -- where from --------------------------------------------------------------------

if [ -n "$source_path" ]; then
    source_path="$(cd "$source_path" && pwd)"
    requirement="$spec @ file://$source_path"
    what="from $source_path"
else
    command -v git >/dev/null 2>&1 || die "git is needed to fetch nazareth; install git and rerun."
    if [ -z "$version" ]; then
        version="$(git ls-remote --tags --refs --sort=-v:refname "$REPO" 'v*' 2>/dev/null \
            | head -n 1 | sed 's|.*refs/tags/||')" || true
        [ -n "$version" ] || die "couldn't find the latest release at $REPO; check your network, or pass --version."
    fi
    requirement="$spec @ git+$REPO@$version"
    what="$version"
fi

# A development install (`uv tool install -e`) is someone's working checkout.
receipt="$("$uv" tool dir 2>/dev/null)/nazareth/uv-receipt.toml"
if [ "$force" -eq 0 ] && [ -f "$receipt" ] && grep -q 'editable' "$receipt"; then
    die "nazareth is installed in development (editable) mode; rerun with --force to replace it."
fi

# -- install ---------------------------------------------------------------------

say "installing $spec $what"
"$uv" tool install --force --python '>=3.12' "$requirement"

bin_dir="$("$uv" tool dir --bin)"
"$bin_dir/nazareth" --version >/dev/null || die "the install finished but $bin_dir/nazareth doesn't run."

case ":$PATH:" in
    *":$bin_dir:"*) on_path=1 ;;
    *) on_path=0 ;;
esac
if [ "$on_path" -eq 0 ]; then
    if [ "$modify_path" -eq 1 ]; then
        "$uv" tool update-shell >/dev/null 2>&1 || true
        say "added $bin_dir to your shell's PATH; open a new shell to use nazareth."
    else
        say "$bin_dir isn't on your PATH; add it so agent hosts can run nazareth."
    fi
fi

say "$("$bin_dir/nazareth" --version) installed."
cat <<EOF

Next, in a project:
  nazareth init --claude   # memory, Claude Code lenses and hooks (or --docker)
  nazareth status
EOF
