#!/usr/bin/env bash
#
# install.sh — install or re-install the OpenCode usage tracker (`ocut`) from this repo.
#
# Idempotent: re-running is safe. It creates repo/.venv with the TUI dependency,
# then points two install locations at this repo with symlinks, so `git pull` in
# this directory updates the running install:
#
#   ~/.local/bin/ocut                        -> bin/ocut
#   ~/.config/opencode/plugin/ocut-hook.js   -> plugin/ocut-hook.js
#
#   ./install.sh            install (refuses to replace real files/dirs)
#   ./install.sh --force    also replace existing real files/dirs
#
# `ocut` itself needs only python3 for its headless subcommands (sync / stats /
# health / export / backup / …); the interactive TUI needs the venv. The hook is
# loaded by OpenCode itself and only appends plugin tool/command calls to a local
# sidecar — it never touches OpenCode's database.
#
# Environment overrides (mainly for testing a sandboxed install):
#   OCUT_CONFIG_DIR   default: ~/.config/opencode   (the OpenCode config home)
#   OCUT_BIN_DIR      default: ~/.local/bin
set -euo pipefail

FORCE=0
for arg in "$@"; do
  case "$arg" in
    --force) FORCE=1 ;;
    -h|--help)
      echo "usage: ./install.sh [--force]"
      echo "  --force   replace existing non-symlink files/dirs at the install locations"
      exit 0
      ;;
    *)
      echo "install.sh: unknown option '$arg' (try --help)" >&2
      exit 2
      ;;
  esac
done

# Resolve through symlinks so this works even if invoked via a symlink.
SELF="$(readlink -f "${BASH_SOURCE[0]}" 2>/dev/null || true)"
REPO_DIR="$(cd "$(dirname "${SELF:-${BASH_SOURCE[0]}}")" && pwd)"
cd "$REPO_DIR"

CONFIG_DIR="${OCUT_CONFIG_DIR:-$HOME/.config/opencode}"
BIN_DIR="${OCUT_BIN_DIR:-$HOME/.local/bin}"

# --- 1. TUI virtualenv -----------------------------------------------------
VENV="$REPO_DIR/.venv"
if [ -x "$VENV/bin/python" ]; then
  echo "==> venv already present: $VENV"
else
  echo "==> creating venv: $VENV"
  if command -v uv >/dev/null 2>&1; then
    uv venv --python 3.13 "$VENV" || uv venv "$VENV"
  else
    python3 -m venv "$VENV"
  fi
fi

echo "==> installing TUI dependencies"
if command -v uv >/dev/null 2>&1; then
  uv pip install --python "$VENV/bin/python" -r "$REPO_DIR/requirements.txt"
else
  "$VENV/bin/python" -m pip install -r "$REPO_DIR/requirements.txt"
fi

# --- 2. symlink the install locations back to this repo --------------------
# Only two links: `bin/ocut` resolves its own scripts/ from the repo it lives in
# (readlink -f through the symlink), so scripts/ is never copied to a user path.
LINK_SRC=(
  "$REPO_DIR/plugin/ocut-hook.js"
  "$REPO_DIR/bin/ocut"
)
LINK_DST=(
  "$CONFIG_DIR/plugin/ocut-hook.js"
  "$BIN_DIR/ocut"
)

# Pre-flight: check every target before touching any of them, so a blocked
# install leaves no half-linked state behind.
if [ "$FORCE" -ne 1 ]; then
  for i in "${!LINK_DST[@]}"; do
    dst="${LINK_DST[$i]}"
    if [ -e "$dst" ] && [ ! -L "$dst" ]; then
      echo "install.sh: refusing to replace '$dst' (exists and is not a symlink)" >&2
      echo "  move or delete it first, or re-run with --force" >&2
      echo "  nothing was changed" >&2
      exit 1
    fi
  done
fi

echo "==> linking install locations"
for i in "${!LINK_DST[@]}"; do
  src="${LINK_SRC[$i]}"; dst="${LINK_DST[$i]}"
  if [ -L "$dst" ]; then
    rm -f "$dst"
  elif [ -e "$dst" ]; then
    echo "  removing existing $dst (--force)"
    rm -rf "$dst"
  fi
  mkdir -p "$(dirname "$dst")"
  ln -s "$src" "$dst"
  echo "  $dst -> $src"
done

# --- 3. verify ------------------------------------------------------------
echo
echo "==> ocut health"
"$BIN_DIR/ocut" health || echo "  (health reported something — see the lines above)"

cat <<EOF

Installed.

  ocut            launch the interactive TUI (eight tabs)
  ocut sync       index what OpenCode wrote since the last run
  ocut stats      usage summary, no TUI required
  ocut health     check the tracker database is readable and current

Restart OpenCode so it loads the hook from $CONFIG_DIR/plugin/.
Plugin usage is recorded only after that restart; a plugin the hook has never
seen shows 0 uses, which is "nothing recorded", not "not used".
EOF
