"""In-process tests for the legacy CLI `skill-stats.py` (the `skillt <cmd>` path).

`test_dispatcher.py` already drives this file through the real bash dispatcher,
so `top`/`show`/`delete`/`clear` are exercised as a *subprocess* — which
in-process coverage cannot see, and which is why this file reported 0% under a
plain `coverage run`. Loading it directly here does two things:

  1. measures it, so a future regression shows up as a coverage drop;
  2. covers the commands the dispatcher never runs — `stats`, `recent`,
     `backup`, `vacuum`, `--self-test` — plus the non-interactive `confirm`
     gate, the `parse_args` error branches and the `main` dispatch.

The subprocess tests are not duplicated: the write semantics they pin
(delete/clear against real rows) are asserted there; here the point is the
surface they never touch.
"""

from __future__ import annotations

import json
import os
import sqlite3
import types

import pytest

import skill_db as db
from conftest import load_module

ss = load_module("skill-stats.py", "skill_stats")


def _args(**kw):
    """An Args object shaped exactly like `parse_args` leaves one."""
    a = ss.Args()
    a.json = kw.get("json", False)
    a.db = kw.get("db", ss.DEFAULT_DB)
    a.self_test = kw.get("self_test", False)
    a.command = kw.get("command")
    a.n = kw.get("n")
    a.skill = kw.get("skill")
    a.limit = kw.get("limit")
    a.yes = kw.get("yes", False)
    a.all = kw.get("all", False)
    a.path = kw.get("path")
    return a


@pytest.fixture
def rw_conn(seeded_db):
    """A read-write connection to the seeded fixture (write commands need one)."""
    conn = db.open_db(seeded_db, readonly=False)
    yield conn
    conn.close()


@pytest.fixture
def bare_db(tmp_path):
    """A migrated but empty database (for the empty-result branches)."""
    path = str(tmp_path / "bare.db")
    conn = db.open_db(path, readonly=False)
    db.ensure_schema(conn)
    conn.commit()
    conn.close()
    return path


# ---------------------------------------------------------------------------
# parse_args
# ---------------------------------------------------------------------------
def test_parse_args_global_flags():
    a = ss.parse_args(["--json", "--self-test"])
    assert a.json is True
    assert a.self_test is True
    assert a.command is None


def test_parse_args_db_requires_a_path():
    with pytest.raises(SystemExit) as ei:
        ss.parse_args(["--db"])
    assert "--db requires a path" in str(ei.value)


def test_parse_args_help_exits_zero():
    with pytest.raises(SystemExit) as ei:
        ss.parse_args(["--help"])
    assert ei.value.code == 0


def test_parse_args_top_positional_and_limit_alias():
    assert ss.parse_args(["top", "3"]).n == 3
    assert ss.parse_args(["top", "--limit", "3"]).limit == 3


def test_parse_args_recent_positional_and_limit_alias():
    assert ss.parse_args(["recent", "5"]).n == 5
    assert ss.parse_args(["recent", "--limit", "5"]).limit == 5


def test_parse_args_top_with_a_word_is_a_mistake_not_a_count():
    with pytest.raises(SystemExit) as ei:
        ss.parse_args(["top", "foo"])
    assert "unexpected argument" in str(ei.value)


def test_parse_args_show_and_delete_require_a_name():
    with pytest.raises(SystemExit) as ei:
        ss.parse_args(["show"])
    assert "requires a skill name" in str(ei.value)
    with pytest.raises(SystemExit) as ei:
        ss.parse_args(["delete"])
    assert "requires a skill name" in str(ei.value)


def test_parse_args_backup_takes_an_optional_path():
    assert ss.parse_args(["backup"]).path is None
    assert ss.parse_args(["backup", "/tmp/out.db"]).path == "/tmp/out.db"


def test_parse_args_show_flags_before_name():
    """`show --limit 5 NAME` must parse the name, not "--limit" (regression)."""
    a = ss.parse_args(["show", "--limit", "5", "myskill"])
    assert a.skill == "myskill"
    assert a.limit == 5


def test_parse_args_limit_errors():
    with pytest.raises(SystemExit) as ei:
        ss.parse_args(["top", "--limit"])
    assert "--limit requires a number" in str(ei.value)
    with pytest.raises(SystemExit) as ei:
        ss.parse_args(["top", "--limit", "abc"])
    assert "got: abc" in str(ei.value)
    with pytest.raises(SystemExit) as ei:
        ss.parse_args(["top", "--limit", "0"])
    assert "--limit must be >= 1" in str(ei.value)


def test_parse_args_unknown_flag_reports_itself():
    with pytest.raises(SystemExit) as ei:
        ss.parse_args(["top", "--bogus"])
    assert "unknown argument: --bogus" in str(ei.value)


def test_parse_args_unknown_flag_beats_the_stray_value():
    """The flag check runs before the extra-positional check (regression)."""
    with pytest.raises(SystemExit) as ei:
        ss.parse_args(["show", "NAME", "--bogus"])
    assert "unknown argument: --bogus" in str(ei.value)


def test_parse_args_yes_and_all_flags():
    a = ss.parse_args(["delete", "x", "--yes"])
    assert a.yes is True and a.all is False
    a = ss.parse_args(["clear", "--all", "--yes"])
    assert a.yes is True and a.all is True


def test_parse_args_unexpected_extra_positional():
    with pytest.raises(SystemExit) as ei:
        ss.parse_args(["show", "one", "two"])
    assert "unexpected argument: two" in str(ei.value)


# ---------------------------------------------------------------------------
# missing_db
# ---------------------------------------------------------------------------
def test_missing_db_json(capsys):
    assert ss.missing_db("/nope.db", json_mode=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"error": "no_database", "path": "/nope.db"}


def test_missing_db_human(capsys):
    assert ss.missing_db("/nope.db") == 0
    out = capsys.readouterr().out
    assert "No database yet at /nope.db" in out


# ---------------------------------------------------------------------------
# read commands
# ---------------------------------------------------------------------------
def test_cmd_stats_human(rw_conn, capsys):
    assert ss.cmd_stats(rw_conn, _args()) == 0
    out = capsys.readouterr().out
    assert "Skill usage statistics" in out
    assert "grow" in out
    # only *used* skills get a per-skill block; the never-used ones are counted
    # in the summary line instead
    assert "Used 5 of 6 skills (1 never used)" in out


def test_cmd_stats_json(rw_conn, capsys):
    assert ss.cmd_stats(rw_conn, _args(json=True)) == 0
    rows = json.loads(capsys.readouterr().out)
    names = {r["skill_name"] for r in rows}
    assert {"grow", "unused"} <= names


def test_cmd_stats_includes_usage_only_skills(rw_conn, capsys):
    """A usage row whose skill has no `skills` entry still shows up (UNION arm)."""
    rw_conn.execute(
        "INSERT INTO skill_usage (skill_name, session_id, trigger_type, status, call_id)"
        " VALUES ('ghost', 's', 'tool_call', 'success', 'g1')"
    )
    rw_conn.commit()
    assert ss.cmd_stats(rw_conn, _args(json=True)) == 0
    names = {r["skill_name"] for r in json.loads(capsys.readouterr().out)}
    assert "ghost" in names


def test_cmd_top_human(rw_conn, capsys):
    assert ss.cmd_top(rw_conn, _args(n=3)) == 0
    out = capsys.readouterr().out
    assert "Top skills (top 3)" in out
    assert "grow" in out


def test_cmd_top_json(rw_conn, capsys):
    assert ss.cmd_top(rw_conn, _args(json=True)) == 0
    rows = json.loads(capsys.readouterr().out)
    # ordered by total DESC: `decline` has 6 uses, `grow` 5
    totals = [r["total"] for r in rows]
    assert totals == sorted(totals, reverse=True)
    assert rows[0]["skill_name"] == "decline"


def test_cmd_top_empty(bare_db, capsys):
    conn = db.open_db(bare_db)
    assert ss.cmd_top(conn, _args()) == 0
    assert "(no data)" in capsys.readouterr().out
    conn.close()


def test_cmd_show_human(rw_conn, capsys):
    assert ss.cmd_show(rw_conn, _args(skill="grow", limit=2)) == 0
    out = capsys.readouterr().out
    assert "Skill usage history: grow" in out
    assert "Shown" in out


def test_cmd_show_json(rw_conn, capsys):
    assert ss.cmd_show(rw_conn, _args(skill="grow", json=True)) == 0
    rows = json.loads(capsys.readouterr().out)
    assert all(r["status"] for r in rows)


def test_cmd_show_empty(rw_conn, capsys):
    assert ss.cmd_show(rw_conn, _args(skill="unused")) == 0
    assert "(no records)" in capsys.readouterr().out


def test_cmd_recent_human(rw_conn, capsys):
    assert ss.cmd_recent(rw_conn, _args(n=3)) == 0
    assert "Recent 3 record(s)" in capsys.readouterr().out


def test_cmd_recent_json(rw_conn, capsys):
    assert ss.cmd_recent(rw_conn, _args(json=True)) == 0
    assert isinstance(json.loads(capsys.readouterr().out), list)


def test_cmd_recent_empty(bare_db, capsys):
    conn = db.open_db(bare_db)
    assert ss.cmd_recent(conn, _args()) == 0
    assert "(no records)" in capsys.readouterr().out
    conn.close()


# ---------------------------------------------------------------------------
# confirm (the write gate)
# ---------------------------------------------------------------------------
def test_confirm_assume_yes():
    assert ss.confirm("really? ", True) is True


def test_confirm_non_interactive_refuses(monkeypatch, capsys):
    monkeypatch.setattr(ss.sys, "stdin", types.SimpleNamespace(isatty=lambda: False))
    assert ss.confirm("really? ", False) is False
    assert "Refusing to modify without --yes" in capsys.readouterr().err


def test_confirm_reads_an_interactive_yes(monkeypatch):
    monkeypatch.setattr(ss.sys, "stdin", types.SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr("builtins.input", lambda _prompt: "  y ")
    assert ss.confirm("really? ", False) is True


def test_confirm_interactive_no(monkeypatch):
    monkeypatch.setattr(ss.sys, "stdin", types.SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")
    assert ss.confirm("really? ", False) is False


def test_confirm_eof_is_false(monkeypatch):
    monkeypatch.setattr(ss.sys, "stdin", types.SimpleNamespace(isatty=lambda: True))

    def _eof(_prompt):
        raise EOFError

    monkeypatch.setattr("builtins.input", _eof)
    assert ss.confirm("really? ", False) is False


# ---------------------------------------------------------------------------
# write commands (semantics asserted in test_dispatcher; here the branches)
# ---------------------------------------------------------------------------
def test_cmd_delete_nothing_to_delete(rw_conn, capsys):
    assert ss.cmd_delete(rw_conn, _args(skill="does-not-exist", yes=True)) == 0
    assert "Nothing to delete" in capsys.readouterr().out


def test_cmd_delete_aborts_when_not_confirmed(rw_conn, monkeypatch, capsys):
    """Non-interactive without --yes must abort, not delete."""
    monkeypatch.setattr(ss.sys, "stdin", types.SimpleNamespace(isatty=lambda: False))
    assert ss.cmd_delete(rw_conn, _args(skill="grow")) == 1
    assert "Aborted." in capsys.readouterr().out


def test_cmd_delete_survives_a_missing_versions_table(rw_conn, monkeypatch, capsys):
    """A legacy DB without `skill_versions` reports 0 versions, not a crash."""
    rw_conn.execute("DROP TABLE skill_versions")
    rw_conn.commit()
    monkeypatch.setattr(ss.sys, "stdin", types.SimpleNamespace(isatty=lambda: False))
    assert ss.cmd_delete(rw_conn, _args(skill="grow")) == 1  # aborts at confirm
    assert "Aborted." in capsys.readouterr().out


def test_cmd_clear_aborts_when_not_confirmed(rw_conn, monkeypatch, capsys):
    monkeypatch.setattr(ss.sys, "stdin", types.SimpleNamespace(isatty=lambda: False))
    assert ss.cmd_clear(rw_conn, _args()) == 1
    assert "Aborted." in capsys.readouterr().out


def test_cmd_backup_writes_the_target(rw_conn, tmp_path, capsys):
    target = str(tmp_path / "backup.db")
    assert ss.cmd_backup(rw_conn, _args(path=target), "unused") == 0
    assert "Backed up to" in capsys.readouterr().out
    assert os.path.exists(target) and os.path.getsize(target) > 0


def test_cmd_backup_refuses_to_overwrite(rw_conn, tmp_path, capsys):
    target = tmp_path / "taken.db"
    target.write_text("x")
    assert ss.cmd_backup(rw_conn, _args(path=str(target)), "unused") == 1
    assert "Refusing to overwrite" in capsys.readouterr().err


def test_cmd_backup_reports_sqlite_errors(rw_conn, monkeypatch, capsys):
    def _boom(conn, target=None):
        raise sqlite3.Error("disk full")

    monkeypatch.setattr(ss.db, "backup_db", _boom)
    assert ss.cmd_backup(rw_conn, _args(path="/tmp/x.db"), "unused") == 1
    assert "Backup failed: disk full" in capsys.readouterr().err


def test_cmd_delete_removes_the_skill(rw_conn, capsys):
    assert ss.cmd_delete(rw_conn, _args(skill="grow", yes=True)) == 0
    assert "Deleted 'grow'" in capsys.readouterr().out


def test_cmd_clear_removes_usage(rw_conn, capsys):
    assert ss.cmd_clear(rw_conn, _args(yes=True)) == 0
    assert "Deleted" in capsys.readouterr().out


def test_cmd_clear_all_also_clears_skills(rw_conn, capsys):
    assert ss.cmd_clear(rw_conn, _args(yes=True, all=True)) == 0
    assert "Also cleared" in capsys.readouterr().out


def test_cmd_vacuum(rw_conn, capsys):
    assert ss.cmd_vacuum(rw_conn, _args()) == 0
    assert "VACUUM done." in capsys.readouterr().out


# ---------------------------------------------------------------------------
# self_test + main dispatch
# ---------------------------------------------------------------------------
def test_self_test_is_green(capsys):
    assert ss.self_test() == 0
    out = capsys.readouterr().out
    assert "FAIL" not in out
    assert "passed" in out


def test_main_self_test_flag(capsys):
    assert ss.main(["--self-test"]) == 0
    assert "passed" in capsys.readouterr().out


def test_main_unknown_command_is_exit_2(capsys):
    assert ss.main(["bogus"]) == 2
    assert "Unknown command: bogus" in capsys.readouterr().err


def test_main_readonly_missing_db_is_exit_0(tmp_path, capsys):
    assert ss.main(["--db", str(tmp_path / "nope.db"), "stats"]) == 0
    assert "No database yet" in capsys.readouterr().out


def test_main_write_missing_db_is_exit_1(tmp_path, capsys):
    assert ss.main(["--db", str(tmp_path / "nope.db"), "vacuum"]) == 1
    assert "nothing to modify" in capsys.readouterr().err


def test_main_default_command_is_stats(seeded_db, capsys):
    assert ss.main(["--db", seeded_db]) == 0
    assert "Skill usage statistics" in capsys.readouterr().out


def test_main_dispatches_recent(seeded_db, capsys):
    assert ss.main(["--db", seeded_db, "recent", "2"]) == 0
    assert "Recent 2 record(s)" in capsys.readouterr().out


def test_main_dispatches_vacuum(seeded_db, capsys):
    assert ss.main(["--db", seeded_db, "vacuum"]) == 0
    assert "VACUUM done." in capsys.readouterr().out


@pytest.mark.parametrize(
    "argv",
    [
        ["top"],
        ["show", "grow"],
        ["delete", "grow", "--yes"],
        ["clear", "--yes"],
        ["backup"],
    ],
)
def test_main_dispatches_every_write_and_read_command(seeded_db, argv, capsys):
    """Every command in the dispatch chain returns 0 (exercises main()'s arms)."""
    assert ss.main(["--db", seeded_db, *argv]) == 0
    capsys.readouterr()


def test_main_reports_an_unopenable_database(tmp_path, monkeypatch, capsys):
    # The path must *exist* or main() short-circuits to missing_db first.
    not_a_db = tmp_path / "not-a-db"
    not_a_db.write_text("this is not sqlite")

    def _boom(_path, _readonly=True):
        raise sqlite3.Error("not a database")

    monkeypatch.setattr(ss, "open_db", _boom)
    assert ss.main(["--db", str(not_a_db), "stats"]) == 1
    assert "Cannot open database" in capsys.readouterr().err


def test_main_reports_a_command_sqlite_error(seeded_db, monkeypatch, capsys):
    def _boom(_conn, _args):
        raise sqlite3.Error("boom")

    monkeypatch.setattr(ss, "cmd_stats", _boom)
    assert ss.main(["--db", seeded_db, "stats"]) == 1
    assert "Database error: boom" in capsys.readouterr().err
