"""Argument-validation tests for `skillt --cli` (A6 regression).

Every numeric flag must fail with a friendly message and exit code 2 instead of
a raw traceback, and `--cli` without a subcommand must not silently do nothing.
"""

from __future__ import annotations

import pytest

from conftest import load_module

st = load_module("skill-tui.py", "skill_tui")


# --- _int_arg --------------------------------------------------------------
def test_int_arg_accepts_valid():
    assert st._int_arg("--days", "7") == 7
    assert st._int_arg("--min-uses", "0", minimum=0) == 0


def test_int_arg_rejects_non_numeric():
    with pytest.raises(SystemExit) as ei:
        st._int_arg("--days", "abc")
    assert ei.value.code == 2


def test_int_arg_rejects_below_minimum():
    with pytest.raises(SystemExit) as ei:
        st._int_arg("--days", "0", minimum=1)
    assert ei.value.code == 2


def test_int_arg_rejects_none():
    with pytest.raises(SystemExit) as ei:
        st._int_arg("--limit", None, minimum=1)
    assert ei.value.code == 2


# --- parse_args: numeric flags --------------------------------------------
@pytest.mark.parametrize(
    "argv",
    [
        ["--cli", "insight", "--days", "0"],
        ["--cli", "insight", "--days", "-3"],
        ["--cli", "insight", "--days", "x"],
        ["--cli", "insight", "--min-uses", "-1"],
        ["--cli", "insight", "--limit", "0"],
        ["--cli", "health", "--limit", "0"],
    ],
)
def test_parse_args_rejects_bad_numbers(argv):
    with pytest.raises(SystemExit) as ei:
        st.parse_args(argv)
    assert ei.value.code == 2


def test_parse_args_accepts_boundary_values():
    a = st.parse_args(["--cli", "insight", "--days", "1", "--min-uses", "0", "--limit", "1"])
    assert (a.days, a.min_uses, a.limit) == (1, 0, 1)


def test_parse_args_missing_value():
    with pytest.raises(SystemExit):
        st.parse_args(["--cli", "insight", "--days"])


def test_parse_args_unknown_command():
    # SystemExit(<str>) keeps the message in .code; the shell turns it into 1.
    with pytest.raises(SystemExit) as ei:
        st.parse_args(["--cli", "bogus"])
    assert "unknown command" in str(ei.value)


def test_parse_args_flag_after_command():
    """`skillt insight --days 5` and `skillt --days 5 insight` are equivalent."""
    a = st.parse_args(["--cli", "insight", "--days", "5"])
    b = st.parse_args(["--cli", "--days", "5", "insight"])
    assert a.command == b.command == "insight"
    assert a.days == b.days == 5


# --- main(): --cli needs a subcommand -------------------------------------
def test_cli_without_subcommand_exits():
    with pytest.raises(SystemExit) as ei:
        st.main(["--cli"])
    assert "--cli requires a subcommand" in str(ei.value)


# --- cli_main(): db path guards -------------------------------------------
def test_cli_main_missing_db_exits_1(tmp_path):
    a = st.parse_args(["--cli", "insight", "--db", str(tmp_path / "nope.db")])
    assert st.cli_main(a) == 1


def test_cli_main_directory_db_exits_2(tmp_path):
    """A directory is not a database; must be a clean exit 2, not a crash."""
    d = tmp_path / "adir"
    d.mkdir()
    a = st.parse_args(["--cli", "insight", "--db", str(d)])
    assert st.cli_main(a) == 2
