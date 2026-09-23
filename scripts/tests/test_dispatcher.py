"""Tests for the bash dispatcher `~/.local/bin/skillt` (C13 regression).

The old dispatcher did `cmd="$1"; shift`, so a global flag placed *before* the
subcommand (`skillt --json insight`) was mistaken for the command. These tests
run the real script as a subprocess to prove both orderings work and that exit
codes are stable.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import skill_db as db

DISPATCHER = Path.home() / ".local" / "bin" / "skillt"
pytestmark = pytest.mark.skipif(
    not DISPATCHER.is_file(), reason="dispatcher not installed"
)


@pytest.fixture
def db_path(tmp_path):
    """A migrated, empty database (schema + views, no rows)."""
    p = str(tmp_path / "d.db")
    conn = db.open_db(p, readonly=False)
    db.ensure_schema(conn)
    conn.commit()
    conn.close()
    return p


def run(*args, timeout=30):
    """Invoke the dispatcher with an explicit python for the headless paths."""
    env = dict(os.environ)
    # Make sure the script finds a python3 for the legacy CLI path.
    env.setdefault("PATH", os.environ.get("PATH", ""))
    return subprocess.run(
        ["bash", str(DISPATCHER), *args],
        capture_output=True, text=True, timeout=timeout, env=env,
    )


# --- flag ordering (the actual bug) ---------------------------------------
def test_global_flag_before_subcommand(db_path):
    r = run("--json", "insight", "--db", db_path)
    assert r.returncode == 0, r.stderr
    assert r.stdout.lstrip().startswith("{")


def test_global_flag_after_subcommand(db_path):
    r = run("insight", "--db", db_path, "--json")
    assert r.returncode == 0, r.stderr
    assert r.stdout.lstrip().startswith("{")


def test_legacy_flag_before_subcommand(db_path):
    r = run("--db", db_path, "top")
    assert r.returncode == 0, r.stderr
    assert "Top skills" in r.stdout


# --- exit codes ------------------------------------------------------------
def test_unknown_command_exit_2():
    r = run("bogus")
    assert r.returncode == 2
    assert "unknown command" in r.stderr


def test_help_exit_0():
    r = run("help")
    assert r.returncode == 0


def test_cli_without_subcommand_exit_2():
    r = run("--cli")
    assert r.returncode == 2
    assert "--cli requires a subcommand" in r.stderr


def test_missing_db_exit_1(tmp_path):
    r = run("insight", "--db", str(tmp_path / "nope.db"))
    assert r.returncode == 1


def test_directory_db_exit_2(tmp_path):
    d = tmp_path / "adir"
    d.mkdir()
    r = run("insight", "--db", str(d))
    assert r.returncode == 2


# --- headless routes all reachable ----------------------------------------
@pytest.mark.parametrize("cmd", ["insight", "health", "doctor", "auto-backup"])
def test_headless_commands_run(db_path, cmd):
    args = [cmd, "--db", db_path]
    if cmd == "auto-backup":
        args.append("--dry-run")
    r = run(*args)
    # doctor may exit 1 when checks FAIL; insight/health/auto-backup must be 0.
    assert r.returncode in (0, 1), f"{cmd}: rc={r.returncode} err={r.stderr}"
    assert "Traceback" not in r.stderr, f"{cmd} crashed:\n{r.stderr}"


def test_legacy_top_limit_alias(db_path):
    """`top --limit N` must equal `top N` (silent no-op fixed)."""
    a = run("top", "3", "--db", db_path)
    b = run("top", "--limit", "3", "--db", db_path)
    assert a.returncode == b.returncode == 0
    assert "top 3" in a.stdout and "top 3" in b.stdout


def test_legacy_top_limit_invalid_exit_1(db_path):
    r = run("top", "--limit", "abc", "--db", db_path)
    assert r.returncode == 1
    assert "Traceback" not in r.stderr
    assert "--limit requires a number" in (r.stdout + r.stderr)
