#!/usr/bin/env bash
#
# install.sh — install or re-install the OpenCode skill tracker from this repo.
#
# Idempotent: re-running is safe. It creates repo/.venv with the TUI dependency,
# then points the four OpenCode install locations at this repo with symlinks, so
# `git pull` in this directory updates the running install.
#
#   ./install.sh            install (refuses to replace real files/dirs)
#   ./install.sh --force    also replace existing real files/dirs
#
# The headless subcommands (skillt insight/health/doctor/...) need nothing but
# python3; only the interactive TUI needs the venv.
#
# Environment overrides (mainly for testing a sandboxed install):
#   SKILLT_CONFIG_DIR   default: ~/.config/opencode
#   SKILLT_BIN_DIR      default: ~/.local/bin
set -euo pipefail

FORCE=0
for arg in "$@"; do
  case "$arg" in
    --force) FORCE=1 ;;
    -h|--help)
      echo "usage: ./install.sh [--force]"
      echo "  --force  replace existing non-symlink files/dirs at the install locations"
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

CONFIG_DIR="${SKILLT_CONFIG_DIR:-$HOME/.config/opencode}"
BIN_DIR="${SKILLT_BIN_DIR:-$HOME/.local/bin}"

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
LINK_SRC=(
  "$REPO_DIR/plugin/skill-tracker.js"
  "$REPO_DIR/scripts"
  "$REPO_DIR/skill-tracker"
  "$REPO_DIR/bin/skillt"
)
LINK_DST=(
  "$CONFIG_DIR/plugin/skill-tracker.js"
  "$CONFIG_DIR/scripts"
  "$CONFIG_DIR/skill-tracker"
  "$BIN_DIR/skillt"
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
echo "==> skillt doctor"
"$BIN_DIR/skillt" doctor || echo "  (doctor reported FAIL/WARN — see the lines above)"

cat <<EOF

Installed.

  skillt           launch the interactive TUI
  skillt doctor    health check (PASS/WARN/FAIL)
  skillt insight   usage summary, no TUI required

Restart OpenCode so it loads the plugin from $CONFIG_DIR/plugin/.

Optional daily backups:
  mkdir -p ~/.config/systemd/user
  cp "$REPO_DIR/skill-tracker/systemd/skillt-auto-backup."* ~/.config/systemd/user/
  systemctl --user daemon-reload
  systemctl --user enable --now skillt-auto-backup.timer
EOF
