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
#   ./install.sh --with-timer   also install the user systemd units for the daily
#                        backup + log rotation. Off by default: writing into
#                        ~/.config/systemd/user is a change outside this project.
#
# The headless subcommands (skillt insight/health/doctor/...) need nothing but
# python3; only the interactive TUI needs the venv.
#
# Environment overrides (mainly for testing a sandboxed install):
#   SKILLT_CONFIG_DIR   default: ~/.config/opencode
#   SKILLT_BIN_DIR      default: ~/.local/bin
set -euo pipefail

FORCE=0
TIMER=0
for arg in "$@"; do
  case "$arg" in
    --force) FORCE=1 ;;
    --with-timer) TIMER=1 ;;
    -h|--help)
      echo "usage: ./install.sh [--force] [--with-timer]"
      echo "  --force        replace existing non-symlink files/dirs at the install locations"
      echo "  --with-timer   install and enable the user systemd units (daily backup + log rotation)"
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

SYSTEMD_DIR="${SKILLT_SYSTEMD_DIR:-$HOME/.config/systemd/user}"

if [ "$TIMER" -eq 1 ]; then
  # Everything that touches systemd is inside this branch: `--with-timer` is the
  # opt-in, and a default install must not leave a unit behind that runs code the
  # owner never asked for.
  echo "==> installing user systemd units (daily backup + log rotation)"
  mkdir -p "$SYSTEMD_DIR"
  cp -f "$REPO_DIR/skill-tracker/systemd/skillt-auto-backup.service" \
        "$REPO_DIR/skill-tracker/systemd/skillt-auto-backup.timer" "$SYSTEMD_DIR/"
  if command -v systemctl >/dev/null 2>&1; then
    systemctl --user daemon-reload
    systemctl --user enable --now skillt-auto-backup.timer
    echo "  enabled skillt-auto-backup.timer in $SYSTEMD_DIR"
    echo "  check: systemctl --user status skillt-auto-backup.timer"
  else
    echo "  units copied to $SYSTEMD_DIR, but systemctl is unavailable — enable by hand" >&2
  fi
fi

if [ "$TIMER" -eq 1 ]; then
  TIMER_NOTE="Daily backup + log rotation are scheduled (skillt-auto-backup.timer)."
else
  # Built with printf rather than a nested here-document: nesting one here-doc inside
  # a command substitution inside another leaves the outer delimiter unmatched, which
  # bash only warns about and then prints something nobody asked for.
  TIMER_NOTE="$(printf '%s\n' \
    '' \
    'Optional daily backups and log rotation (one timer runs both):' \
    '  ./install.sh --with-timer' \
    '  # or by hand:' \
    '  mkdir -p ~/.config/systemd/user' \
    "  cp $REPO_DIR/skill-tracker/systemd/skillt-auto-backup.* ~/.config/systemd/user/" \
    '  systemctl --user daemon-reload' \
    '  systemctl --user enable --now skillt-auto-backup.timer')"
fi

cat <<EOF

Installed.

  skillt           launch the interactive TUI
  skillt doctor    health check (PASS/WARN/FAIL)
  skillt insight   usage summary, no TUI required
  skillt config    the settings file, and where each value came from

Restart OpenCode so it loads the plugin from $CONFIG_DIR/plugin/.
$TIMER_NOTE
EOF
