"""The `bin/ocut` dispatcher's own contract, tested against a stub scripts dir.

The dispatcher is bash on purpose — it must still speak usefully when the venv is
missing or broken — so these cases run the real script and assert on its exit code
and message, never on what Python does next. `OCUT_SCRIPTS` always points at a
temporary directory holding a recorder, so no case here can reach this machine's
database, its OpenCode directory, or the real `scripts/`.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
DISPATCHER = REPO / "bin" / "ocut"

# Every headless route the dispatcher knows about. A name added to `bin/ocut`'s
# case list without landing here is a route no test has ever walked.
ROUTES = ("tui", "sync", "stats", "tools", "skills", "agents", "plugins", "mcp",
          "models", "show", "recent", "inventory", "export", "health", "format",
          "backup", "restore")

VALUE_FLAGS = ("--db", "--kind", "--limit", "--host-db", "--host-config",
               "--plugin-events")


@pytest.fixture
def scripts(tmp_path):
    """A stub scripts/ whose three entry points record how they were called."""
    d = tmp_path / "scripts"
    d.mkdir()
    for name in ("ocut-tui.py", "ocut-sync.py", "ocut-stats.py"):
        (d / name).write_text(
            "import json, os, sys\n"
            "print(json.dumps({'target': os.path.basename(__file__), 'argv': sys.argv[1:]}))\n",
            encoding="utf-8",
        )
    return d


def run(*args, scripts=None, env_extra=None, timeout=30):
    env = dict(os.environ)
    env["OCUT_SCRIPTS"] = str(scripts if scripts is not None else REPO / "scripts")
    # An empty value means "use the interpreter on PATH"; the repo venv would also
    # work, but the stub needs no dependency, so the test must not depend on one.
    env.pop("OCUT_VENV", None)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(["bash", str(DISPATCHER), *args],
                          capture_output=True, text=True, timeout=timeout, env=env)


def target_of(result):
    import json
    payload = json.loads(result.stdout)
    return payload["target"], payload["argv"]


# --- help answers before anything is resolved ------------------------------
def test_help_works_with_a_broken_install(tmp_path):
    """`--help` is the one thing that must work when nothing else can.

    It is answered before the scripts directory and the interpreter are resolved,
    so a machine with no venv and no checkout still gets the command list. Both
    spellings are checked because the flag and the word are typed by different
    people in different habits.
    """
    for flag in ("--help", "-h", "help"):
        r = run(flag, scripts=tmp_path / "nowhere",
                env_extra={"OCUT_VENV": str(tmp_path / "no-such-venv")})
        assert r.returncode == 0, (flag, r.stderr)
        assert "ocut — entry point" in r.stdout, (flag, r.stdout[:120])
        assert "is neither a venv dir nor an executable" not in r.stderr, \
            "the venv was resolved, so --help did not answer first"


def test_help_is_read_off_the_script_not_a_line_range():
    """A hard-coded range is how help text drifts out of step with the script.

    The comment block is the documentation, so the last documented command must
    still be in the output after a line is added above it — which a range cannot
    promise.
    """
    r = run("--help")
    assert "format" in r.stdout, r.stdout[:200]
    head = DISPATCHER.read_text(encoding="utf-8").splitlines()
    documented = [l for l in head[:24] if l.startswith("#   ocut")]
    assert documented, "the header block no longer documents any command"
    for line in documented:
        word = line.split()[1]
        assert word in r.stdout, f"{line!r} is in the header but not in --help"


# --- routing ---------------------------------------------------------------
@pytest.mark.parametrize("cmd", ROUTES)
def test_every_documented_command_routes(cmd, scripts):
    r = run(cmd, scripts=scripts)
    assert r.returncode == 0, r.stderr
    which, argv = target_of(r)
    expected = {"tui": "ocut-tui.py", "sync": "ocut-sync.py"}
    assert which == expected.get(cmd, "ocut-stats.py"), (cmd, which)
    if cmd not in expected:
        assert argv[0] == cmd, f"{cmd} must be passed through to the report CLI: {argv}"


def test_no_arguments_launches_the_tui(scripts):
    r = run(scripts=scripts)
    assert r.returncode == 0, r.stderr
    assert target_of(r)[0] == "ocut-tui.py"


def test_global_flag_before_the_subcommand_routes_to_the_subcommand(scripts):
    """The C13 regression, re-pinned for this launcher.

    `ocut --json health` used to be read as a command called `--json`, and the old
    dispatcher then lost the real command. Both orderings must reach the same
    target with the same payload.

    `--quiet` is a boolean the *sync* script accepts and the stats one does not;
    that is deliberate. This test is about position handling in the shell, and a
    token the launcher does not know may only ever be forwarded untouched.
    """
    before = run("--quiet", "health", scripts=scripts)
    after = run("health", "--quiet", scripts=scripts)
    assert before.returncode == after.returncode == 0
    assert target_of(before) == target_of(after), (before.stdout, after.stdout)


@pytest.mark.parametrize("flag", VALUE_FLAGS)
def test_a_flags_value_is_never_mistaken_for_the_command(flag, scripts):
    """`--db X health` must not route to a command called `X`.

    Each value-taking flag is listed here; a flag added to `bin/ocut` without
    joining this list is a flag whose value becomes the command, which is the bug
    this whole file exists to keep from coming back.
    """
    r = run(flag, "something", "health", scripts=scripts)
    assert r.returncode == 0, r.stderr
    which, argv = target_of(r)
    assert which == "ocut-stats.py", (flag, which)
    assert argv[0] == "health", f"{flag} swallowed the subcommand: {argv}"
    assert "something" in argv, f"{flag} lost its value: {argv}"


def test_the_value_flag_list_matches_the_dispatchers_own():
    """This file's list cannot be the only copy of the flag names.

    The dispatcher's `VALUES_FOR` and this test's `VALUE_FLAGS` are read as text
    and compared, so adding a flag to one side fails here rather than silently
    changing what counts as a subcommand.
    """
    declared = dispatcher_value_flags()
    assert declared == VALUE_FLAGS, (declared, VALUE_FLAGS)


def dispatcher_value_flags():
    text = DISPATCHER.read_text(encoding="utf-8")
    line = next(l for l in text.splitlines() if l.startswith("VALUES_FOR="))
    return tuple(re.findall(r'"([^"]+)"', line))


def parser_value_flags():
    """Every `--flag` the two routed scripts accept that is not a switch.

    Read out of the parsers themselves: `add_argument("--x", ...)` without
    `action="store_true"`. A flag listed here and missing from the dispatcher is a
    flag whose value gets mistaken for the subcommand; a flag in the dispatcher and
    missing here is a route that advertises something nothing accepts — which is
    what the four entries removed from this file on 2026-10-12 were.
    """
    out = set()
    for name in ("ocut-stats.py", "ocut-sync.py"):
        text = (Path(__file__).resolve().parents[1] / name).read_text(
            encoding="utf-8")
        for call in re.findall(r'add_argument\(\s*("(?:--|")[^)]*?)\)', text, re.S):
            args = [a.strip('"') for a in re.findall(r'"([^"]+)"', call)]
            flags = [a for a in args if a.startswith("--")]
            if not flags:
                continue
            if "store_true" in call or "count" in call:
                continue
            out.update(flags)
    return tuple(sorted(out))


def test_the_launcher_cannot_advertise_a_flag_no_script_accepts():
    """Order is not a claim; membership is.

    Compared as sets, so re-ordering the launcher's list is not a failure, and
    duplicates are checked separately because a duplicated entry hides a missing
    one.
    """
    declared = dispatcher_value_flags()
    parsed = parser_value_flags()
    assert set(declared) == set(parsed), (
        f"bin/ocut advertises {sorted(set(declared) - set(parsed))}, "
        f"the scripts accept {sorted(set(parsed) - set(declared))}")
    assert len(declared) == len(set(declared)), f"duplicate in VALUES_FOR"


# --- failure paths must name what they mean --------------------------------
def test_unknown_command_is_rejected_without_running_anything(scripts):
    r = run("nonsense", scripts=scripts)
    assert r.returncode == 2, (r.returncode, r.stdout, r.stderr)
    assert "unknown command 'nonsense'" in r.stderr, r.stderr


def test_broken_venv_override_says_so_by_name_and_value(tmp_path, scripts):
    """Silently falling back to system python3 is the failure this guards.

    `OCUT_VENV` may name a venv directory or a python binary; anything else is a
    typo, and running a different interpreter than the owner asked for is
    invisible until the TUI dies on a missing dependency.
    """
    r = run("health", scripts=scripts, env_extra={"OCUT_VENV": str(tmp_path / "nope")})
    assert r.returncode == 1, (r.returncode, r.stdout, r.stderr)
    assert "OCUT_VENV=" in r.stderr, r.stderr


def test_missing_scripts_directory_is_not_searched_around_for(tmp_path):
    """The installer only ever symlinks `bin/ocut`, so there is no second place.

    Run from a copy in a throwaway `bin/`: the real launcher always finds the
    repository's own `scripts/` next to it, which would make this case pass by
    accident instead of by the error path being reached. Saying "set OCUT_SCRIPTS"
    beats exec'ing a path that cannot exist, because a half-installed host would
    otherwise report a Python traceback where it means an installation problem.
    """
    lonely = tmp_path / "bin"
    lonely.mkdir()
    copy = lonely / "ocut"
    copy.write_text(DISPATCHER.read_text(encoding="utf-8"), encoding="utf-8")
    env = dict(os.environ)
    env.pop("OCUT_SCRIPTS", None)
    env["HOME"] = str(tmp_path)
    r = subprocess.run(["bash", str(copy), "health"], capture_output=True,
                       text=True, timeout=30, env=env)
    assert r.returncode == 1, (r.returncode, r.stdout, r.stderr)
    assert "no scripts directory" in r.stderr, r.stderr


def test_bash_is_not_strict_about_unset_array_elements(scripts):
    """`set -u` with `"${rest[@]}"` on an empty array is a runtime landmine.

    The empty-array expansion form is what this dispatcher runs for `ocut health`
    (no trailing arguments), so a case with zero extra tokens is required — the
    routing tests all pass at least one argument, and would not have caught it.
    """
    r = run("health", scripts=scripts)
    assert r.returncode == 0, r.stderr
    assert "unbound variable" not in r.stderr, r.stderr
