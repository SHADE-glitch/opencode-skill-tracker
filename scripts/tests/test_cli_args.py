"""Argument-validation tests for `skillt --cli` (A6 regression).

Every numeric flag must fail with a friendly message and exit code 2 instead of
a raw traceback, and `--cli` without a subcommand must not silently do nothing.

The second half is the settings layer: a flag is the top of the precedence
chain, not the whole of it, so a value typed once into the config file has to
reach the same attributes without any flag on the command line.
"""

from __future__ import annotations

import pytest

from conftest import load_module

import settings as cfg

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


def test_parse_args_freshness_days():
    a = st.parse_args(["--cli", "doctor", "--freshness-days", "14"])
    assert a.freshness_days == 14
    with pytest.raises(SystemExit) as ei:
        st.parse_args(["--cli", "doctor", "--freshness-days", "0"])
    assert ei.value.code == 2
    with pytest.raises(SystemExit) as ei:
        st.parse_args(["--cli", "doctor", "--freshness-days"])
    assert "requires a value" in str(ei.value)


def test_parse_args_accepts_scrub_metadata():
    """A new subcommand must be in CLI_COMMANDS or the dispatcher rejects it."""
    a = st.parse_args(["--cli", "scrub-metadata"])
    assert a.command == "scrub-metadata"
    assert "scrub-metadata" in st.CLI_COMMANDS


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


# --- settings resolution (S5) ----------------------------------------------
@pytest.fixture
def cfg_file(tmp_path, monkeypatch):
    """A config path in the sandbox, so no test sees this machine's settings."""
    path = tmp_path / "skillt-config.json"
    monkeypatch.setenv(cfg.CONFIG_PATH_ENV, str(path))
    for spec in cfg.REGISTRY:
        monkeypatch.delenv(cfg.env_name(spec["key"]), raising=False)
    return path


def test_parse_args_takes_its_defaults_from_the_settings_file(cfg_file):
    """A key typed once must reach every command without a flag.

    This is the whole point of the layer: before it, `--days/--limit/--min-uses`
    had a literal on the parser line and nothing else could set them.
    """
    cfg_file.write_text('{"view.days": 45, "view.limit": 4, "view.min_uses": 9}',
                        encoding="utf-8")
    a = st.parse_args(["--cli", "insight"])
    assert (a.days, a.limit, a.min_uses) == (45, 4, 9)


def test_a_flag_beats_the_settings_file(cfg_file):
    cfg_file.write_text('{"view.days": 45}', encoding="utf-8")
    a = st.parse_args(["--cli", "insight", "--days", "7"])
    assert a.days == 7


def test_the_environment_beats_the_default_but_not_the_file(cfg_file, monkeypatch):
    monkeypatch.setenv(cfg.env_name("view.days"), "60")
    assert st.parse_args(["--cli", "insight"]).days == 60
    cfg_file.write_text('{"view.days": 45}', encoding="utf-8")
    assert st.parse_args(["--cli", "insight"]).days == 45


def test_parse_args_reads_the_recent_rows_knob(cfg_file):
    a = st.parse_args(["--cli", "doctor", "--recent-rows", "25"])
    assert a.recent_rows == 25
    cfg_file.write_text('{"view.recent_rows": 33}', encoding="utf-8")
    assert st.parse_args(["--cli", "doctor"]).recent_rows == 33
    with pytest.raises(SystemExit) as ei:
        st.parse_args(["--cli", "doctor", "--recent-rows", "0"])
    assert ei.value.code == 2


def test_parse_args_keeps_the_words_after_the_command():
    """`config set view.days 45` is three words the parser used to throw away."""
    a = st.parse_args(["--cli", "config", "set", "view.days", "45"])
    assert (a.command, a.positional) == ("config", ["set", "view.days", "45"])
    assert st.parse_args(["--cli", "config"]).positional == []


def test_config_is_a_known_command():
    assert "config" in st.CLI_COMMANDS


def test_config_never_touches_the_database(tmp_path, capsys):
    """`skillt config list` on a fresh machine must work with no DB at all.

    A settings reader that required the database to exist would be unusable in
    exactly the situation it is for — before the first run.
    """
    missing = str(tmp_path / "nope.db")
    a = st.parse_args(["--cli", "config", "list", "--db", missing])
    assert st.cli_main(a) == 0
    out = capsys.readouterr().out
    assert "view.days" in out and "precedence" in out
    assert "No database" not in out
