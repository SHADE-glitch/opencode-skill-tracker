"""Tests for `skillt doctor` — PASS/WARN/FAIL checks and exit codes."""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from conftest import load_module

import skill_db as db
import skill_db_claude_mem as cm
import settings as cfg

st = load_module("skill-tui.py", "skill_tui")


class Args(st.Args):
    """A stand-in for one parsed `skillt doctor` command line.

    Subclass of the real `Args` so the log bounds come from the production class:
    `doctor` reads the plugin log at `log_max_bytes`, and a stand-in that carried its
    own copy of that number could quietly disagree with the writer it is meant to be
    compared against. A test that wants a different bound assigns it on the instance.
    """

    def __init__(self, db_path, json=False):
        self.db = db_path
        self.json = json
        self.limit = 10
        self.freshness_days = 7


def checks_by_name(conn, args):
    return {name: (status, detail) for name, status, detail in st._doctor_checks(conn, args)}


def _healthy_setup(tmp_path, monkeypatch):
    """A DB + skills dir + plugin + backup that should produce zero FAILs."""
    skills = tmp_path / "skills"
    for name in ("alpha", "beta"):
        d = skills / "personal-skills" / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: d\n---\nbody\n", encoding="utf-8"
        )
    monkeypatch.setattr(db, "SKILLS_DIR", str(skills))

    db_path = str(tmp_path / "ok.db")
    conn = db.open_db(db_path, readonly=False)
    db.ensure_schema(conn)
    for name in ("alpha", "beta"):
        conn.execute(
            "INSERT INTO skills (name, category, path, description) VALUES (?,?,?,?)",
            (name, "personal-skills", str(skills / "personal-skills" / name), "d"),
        )
    conn.commit()
    os.chmod(db_path, 0o600)

    plugin = tmp_path / "skill-tracker.js"
    plugin.write_text(
        # A stand-in for a plugin of the current generation: the marker strings
        # doctor greps for are the hook names, which the real file now spells once
        # inside its CONTRACT block. Kept as literals here on purpose — this is a
        # fingerprint fixture, not a copy of the writer.
        'HOOK_TOOL_BEFORE: "tool.execute.before",\n'
        'HOOK_TOOL_AFTER: "tool.execute.after",\n'
        'HOOK_PERMISSION_ASK: "permission.ask",\n'
        'HOOK_CHAT_MESSAGE: "chat.message",\n'
        'HOOK_DISPOSE: "dispose",\n'
        "mcp_usage function classify( recordMcpUsage\n"
        "plugin_usage recordPluginUsage command.execute.before\n"
        "// Builtin tool ids, verified against OpenCode 1.18.33 with\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(st, "PLUGIN_PATH", str(plugin))

    # Never read the developer's real tracker log, and never shell out for a
    # version comparison that has nothing to compare against.
    log = tmp_path / "skill-tracker.log"
    log.write_text("2026-01-01T00:00:00.000Z [info] initialized\n", encoding="utf-8")
    monkeypatch.setattr(st, "TRACKER_LOG_PATH", str(log))
    monkeypatch.setattr(st, "_opencode_version", lambda: "1.18.33\n")

    bdir = tmp_path / "backups"
    bdir.mkdir()
    (bdir / "skill-usage-backup-20260615-120000.db").write_bytes(b"x")
    monkeypatch.setattr(db, "BACKUP_DIR", str(bdir))
    return conn, db_path


def test_doctor_reports_no_failures_on_healthy_setup(tmp_path, monkeypatch, capsys):
    conn, db_path = _healthy_setup(tmp_path, monkeypatch)
    rc = st._cli_doctor(conn, Args(db_path))
    out = capsys.readouterr().out
    assert "[FAIL]" not in out, out
    assert "0 FAIL" in out
    assert rc == 0
    conn.close()


def test_doctor_json_is_structured(tmp_path, monkeypatch, capsys):
    import json

    conn, db_path = _healthy_setup(tmp_path, monkeypatch)
    rc = st._cli_doctor(conn, Args(db_path, json=True))
    doc = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert doc["summary"]["fail"] == 0
    names = {c["name"] for c in doc["checks"]}
    assert {"db.quick_check", "plugin.hooks", "plugin.mcp_hooks", "plugin.plugin_hooks",
            "skills.frontmatter_names", "capture.freshness", "log.errors",
            "env.opencode_version"} <= names
    for c in doc["checks"]:
        assert c["status"] in ("PASS", "WARN", "FAIL"), c
    conn.close()


def test_doctor_fails_on_corrupt_database(tmp_path, capsys):
    bad = tmp_path / "bad.db"
    bad.write_text("this is definitely not a sqlite database\n", encoding="utf-8")
    conn = db.open_db(str(bad), readonly=False)   # lazy connect: no error yet
    rc = st._cli_doctor(conn, Args(str(bad)))
    out = capsys.readouterr().out
    assert rc == 1, "a FAIL must produce exit code 1"
    assert "[FAIL]" in out
    assert "db.quick_check" in out
    conn.close()


def test_doctor_fails_when_plugin_missing(tmp_path, monkeypatch, capsys):
    conn, db_path = _healthy_setup(tmp_path, monkeypatch)
    monkeypatch.setattr(st, "PLUGIN_PATH", str(tmp_path / "nope.js"))
    rc = st._cli_doctor(conn, Args(db_path))
    out = capsys.readouterr().out
    assert "[FAIL]" in out
    assert rc == 1
    conn.close()


def test_doctor_warns_on_unparsed_frontmatter(tmp_path, monkeypatch, capsys):
    conn, db_path = _healthy_setup(tmp_path, monkeypatch)
    # a SKILL.md with no frontmatter name -> WARN, not FAIL
    d = tmp_path / "skills" / "personal-skills" / "no-name"
    d.mkdir()
    (d / "SKILL.md").write_text("---\ndescription: no name here\n---\nbody\n", encoding="utf-8")
    rc = st._cli_doctor(conn, Args(db_path))
    out = capsys.readouterr().out
    assert "skills.frontmatter_names" in out
    assert "[WARN]" in out
    assert rc == 0, "WARN must not fail the command"
    conn.close()


def test_doctor_reports_missing_skills_dir(tmp_path, monkeypatch, capsys):
    """A missing SKILLS_DIR must WARN, not vacuously PASS the scan."""
    conn, db_path = _healthy_setup(tmp_path, monkeypatch)
    monkeypatch.setattr(db, "SKILLS_DIR", str(tmp_path / "gone"))
    rc = st._cli_doctor(conn, Args(db_path))
    out = capsys.readouterr().out
    assert "skills.dir" in out
    assert "[WARN]" in out
    assert "missing" in out
    assert "skills.frontmatter_names" not in out, "must not vacuously pass"
    assert rc == 0
    conn.close()


# --- capture pipeline checks ----------------------------------------------
# The tracker's whole purpose is to keep recording, and a silent stop is
# indistinguishable from an idle machine unless someone asks.
def test_doctor_passes_on_fresh_capture(tmp_path, monkeypatch):
    conn, db_path = _healthy_setup(tmp_path, monkeypatch)
    conn.execute(
        "INSERT INTO skill_usage (skill_name, session_id, trigger_type, status,"
        " timestamp, call_id) VALUES ('alpha','s1','tool_call','success',"
        " strftime('%Y-%m-%dT%H:%M:%fZ','now'), 'c1')"
    )
    conn.commit()
    status, detail = checks_by_name(conn, Args(db_path))["capture.freshness"]
    assert status == "PASS", detail
    assert "skill_usage" in detail, "must name which stream is newest"
    conn.close()


def test_doctor_warns_when_nothing_has_been_recorded(tmp_path, monkeypatch):
    conn, db_path = _healthy_setup(tmp_path, monkeypatch)
    status, detail = checks_by_name(conn, Args(db_path))["capture.freshness"]
    assert status == "WARN", detail
    assert "no usage rows" in detail
    conn.close()


def test_doctor_warns_when_capture_has_stalled(tmp_path, monkeypatch):
    conn, db_path = _healthy_setup(tmp_path, monkeypatch)
    conn.execute(
        "INSERT INTO mcp_usage (server_name, tool_name, session_id, trigger_type,"
        " status, timestamp, call_id) VALUES ('s','t','s1','tool_call','success',"
        " strftime('%Y-%m-%dT%H:%M:%fZ','now','-30 days'), 'c1')"
    )
    conn.commit()
    status, detail = checks_by_name(conn, Args(db_path))["capture.freshness"]
    assert status == "WARN", detail
    assert "mcp_usage" in detail and "30" in detail
    # The limit is a knob because a genuinely idle machine is not a fault.
    status, _ = checks_by_name(conn, Args(db_path))["capture.freshness"]
    a = Args(db_path)
    a.freshness_days = 60
    assert checks_by_name(conn, a)["capture.freshness"][0] == "PASS"
    conn.close()


# --- per-stream freshness --------------------------------------------------
# Taking the newest row across the three tables means one live stream hides two
# dead ones: the check cannot go red, so it stops meaning anything.
def _insert(conn, table, when, call_id):
    if table == "skill_usage":
        conn.execute(
            "INSERT INTO skill_usage (skill_name, session_id, trigger_type, status,"
            f" timestamp, call_id) VALUES ('alpha','s1','tool_call','success',{when},?)",
            (call_id,),
        )
    elif table == "plugin_usage":
        conn.execute(
            "INSERT INTO plugin_usage (plugin_name, kind, item_name, session_id,"
            " trigger_type, status, timestamp, call_id)"
            f" VALUES ('p','tool','t','s1','tool_call','success',{when},?)",
            (call_id,),
        )
    else:
        conn.execute(
            "INSERT INTO mcp_usage (server_name, tool_name, session_id, trigger_type,"
            " status, timestamp, call_id)"
            f" VALUES ('s','t','s1','tool_call','success',{when},?)",
            (call_id,),
        )


def test_doctor_warns_about_a_stalled_stream_that_is_not_the_newest(tmp_path, monkeypatch):
    """plugin_usage recorded today, mcp_usage stopped a month ago.

    The old check reported the newest row and passed. The stalled stream is the
    only news here, so it must be named.
    """
    conn, db_path = _healthy_setup(tmp_path, monkeypatch)
    _insert(conn, "plugin_usage", "strftime('%Y-%m-%dT%H:%M:%fZ','now')", "p1")
    _insert(conn, "mcp_usage", "strftime('%Y-%m-%dT%H:%M:%fZ','now','-30 days')", "m1")
    conn.commit()
    status, detail = checks_by_name(conn, Args(db_path))["capture.freshness"]
    assert status == "WARN", detail
    assert "mcp_usage" in detail, detail
    assert "stalled" in detail, detail
    # the healthy stream is still reported, so the reader sees the whole picture
    assert "plugin_usage" in detail, detail
    conn.close()


def test_doctor_does_not_nag_about_a_stream_that_never_recorded(tmp_path, monkeypatch):
    """Zero rows is 'never used', which is not a stalled pipeline."""
    conn, db_path = _healthy_setup(tmp_path, monkeypatch)
    _insert(conn, "skill_usage", "strftime('%Y-%m-%dT%H:%M:%fZ','now')", "s1")
    _insert(conn, "plugin_usage", "strftime('%Y-%m-%dT%H:%M:%fZ','now')", "p1")
    conn.commit()
    status, detail = checks_by_name(conn, Args(db_path))["capture.freshness"]
    assert status == "PASS", detail
    assert "mcp_usage: no rows" in detail, detail
    conn.close()


def test_doctor_stream_allowlist_silences_a_stream_on_purpose(tmp_path, monkeypatch):
    """The owner removed MCP servers; a month-old mcp_usage row is then expected.

    Declaring which streams to judge is honest only if the excluded one is still
    printed — a silent exclusion is the same false silence this check exists to
    remove.
    """
    conn, db_path = _healthy_setup(tmp_path, monkeypatch)
    _insert(conn, "plugin_usage", "strftime('%Y-%m-%dT%H:%M:%fZ','now')", "p1")
    _insert(conn, "mcp_usage", "strftime('%Y-%m-%dT%H:%M:%fZ','now','-30 days')", "m1")
    conn.commit()
    monkeypatch.setenv("OPENCODE_SKILL_TRACKER_STREAMS", "skill,plugin")
    status, detail = checks_by_name(conn, Args(db_path))["capture.freshness"]
    assert status == "PASS", detail
    assert "mcp_usage" in detail, "an excluded stream must still be shown"
    assert "excluded" in detail, detail
    conn.close()


def test_doctor_survives_a_database_without_the_new_tables(tmp_path, monkeypatch):
    """A pre-migration DB must WARN, not raise out of doctor."""
    conn, db_path = _healthy_setup(tmp_path, monkeypatch)
    conn.execute("DROP TABLE plugin_usage")
    conn.commit()
    row = checks_by_name(conn, Args(db_path))["capture.freshness"]
    assert row[0] in ("PASS", "WARN"), row
    conn.close()


# --- tracker log ----------------------------------------------------------
def test_doctor_counts_plugin_errors_in_the_log(tmp_path, monkeypatch):
    conn, db_path = _healthy_setup(tmp_path, monkeypatch)
    log = tmp_path / "noisy.log"
    log.write_text(
        "2026-01-01T00:00:00.000Z [info] initialized\n"
        "2026-01-01T00:00:01.000Z [err] recordMcpUsage: database is locked\n"
        "2026-01-01T00:00:02.000Z [err] init: no such table\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(st, "TRACKER_LOG_PATH", str(log))
    status, detail = checks_by_name(conn, Args(db_path))["log.errors"]
    assert status == "WARN", detail
    assert "2 error line(s)" in detail
    assert "no such table" in detail, "the last error is the one shown"
    conn.close()


def test_doctor_warns_when_the_log_is_absent(tmp_path, monkeypatch):
    """No log has ever been written: the plugin has not initialised."""
    conn, db_path = _healthy_setup(tmp_path, monkeypatch)
    monkeypatch.setattr(st, "TRACKER_LOG_PATH", str(tmp_path / "gone.log"))
    status, detail = checks_by_name(conn, Args(db_path))["log.errors"]
    assert status == "WARN", detail
    assert "not initialised" in detail
    conn.close()


# --- OpenCode version vs the pinned allowlist (M13) -----------------------
def test_doctor_warns_when_opencode_outgrew_the_allowlist(tmp_path, monkeypatch):
    conn, db_path = _healthy_setup(tmp_path, monkeypatch)
    monkeypatch.setattr(st, "_opencode_version", lambda: "1.19.2\n")
    status, detail = checks_by_name(conn, Args(db_path))["env.opencode_version"]
    assert status == "WARN", detail
    assert "1.19.2" in detail and "1.18.33" in detail
    assert "DEFAULT_BUILTIN_TOOLS" in detail, "must say what to do about it"
    conn.close()


def test_doctor_warns_when_opencode_is_not_on_path(tmp_path, monkeypatch):
    conn, db_path = _healthy_setup(tmp_path, monkeypatch)
    monkeypatch.setattr(st, "_opencode_version", lambda: None)
    status, detail = checks_by_name(conn, Args(db_path))["env.opencode_version"]
    assert status == "WARN", detail
    assert "not on PATH" in detail
    conn.close()


def test_the_new_checks_can_never_fail_doctor(tmp_path, monkeypatch):
    """They are advisory: capture can legitimately be idle for a week."""
    conn, db_path = _healthy_setup(tmp_path, monkeypatch)
    monkeypatch.setattr(st, "TRACKER_LOG_PATH", str(tmp_path / "gone.log"))
    monkeypatch.setattr(st, "_opencode_version", lambda: None)
    names = ("capture.freshness", "log.errors", "env.opencode_version")
    found = checks_by_name(conn, Args(db_path))
    for n in names:
        assert n in found, f"{n} missing"
        assert found[n][0] != "FAIL", found[n]
    assert st._cli_doctor(conn, Args(db_path)) == 0
    conn.close()


def test_the_log_read_is_bounded_like_every_other_log_we_open(tmp_path, monkeypatch):
    """doctor read the whole plugin log with a plain `open()`; nothing else does.

    The claude-mem reader stops at `CLAUDE_MEM_LOG_BYTES_CAP` because a foreign log
    is not ours to size. The tracker's own log grows one line per recorded error
    forever (M17: no rotation), so an unbounded read here is a stall that gets
    worse with age on the cheapest diagnostic the tool has. The cap must be named
    in the module, not inline, or the message cannot say what it covered.
    """
    assert st.TRACKER_LOG_BYTES_CAP == cm.CLAUDE_MEM_LOG_BYTES_CAP


def test_a_bounded_log_read_reports_a_floor_never_a_clean_bill(tmp_path, monkeypatch):
    """A bound is allowed; a bound that hides itself is not.

    Two errors, one at the top of the file and one past the cap: the count is then
    1 and the line must say the counts cover only the first N bytes. Reading the
    tail instead would have been worse — the real log's failures were at the top,
    and a 64 KB tail reported 0 of its 57 ERROR lines.
    """
    conn, db_path = _healthy_setup(tmp_path, monkeypatch)
    cap = 64 * 1024                       # shrink the knob; the shape is what matters
    filler = "# " + ("x" * 4096) + "\n"   # no "[err]" — filler must never look like a fault
    log = tmp_path / "big.log"
    with log.open("w", encoding="utf-8") as f:
        f.write("[2026-10-09T00:00:00.000Z] [err] [skill] early failure\n")
        while f.tell() < cap + 4096:
            f.write(filler)
        f.write("[2026-10-09T00:00:01.000Z] [err] [skill] failure past the cap\n")
    assert log.stat().st_size > cap
    monkeypatch.setattr(st, "TRACKER_LOG_PATH", str(log))

    args = Args(db_path)
    args.log_max_bytes = cap
    status, detail = checks_by_name(conn, args)["log.errors"]
    assert status == "WARN", detail
    assert "1 error line(s)" in detail, f"the past-the-cap error leaked into the count: {detail}"
    assert "early failure" in detail, detail
    assert "first" in detail and f"{cap} B" in detail.replace(",", ""), \
        f"a bounded read must name its bound: {detail}"
    conn.close()


def test_the_reader_bound_moves_with_the_writer_bound(tmp_path, monkeypatch):
    """One knob means both sides, not two defaults that happen to agree.

    `log.max_bytes` decides when `skillt rotate-log` moves the file, and it decides
    how far `doctor` reads. The reader was pinned to `TRACKER_LOG_BYTES_CAP` — the
    registry's *default* — so an owner who raised the knob to 8 MiB got rotation at
    8 MiB and a reader still stopping at 4, which is the one case the "a line
    `doctor` cannot see is structurally impossible" claim cannot survive. The bound
    this line prints is therefore the test: it must be the resolved number.
    """
    conn, db_path = _healthy_setup(tmp_path, monkeypatch)
    monkeypatch.setenv("OPENCODE_SKILL_TRACKER_LOG_MAX_BYTES", "8388608")
    args = st.parse_args(["--cli", "doctor", "--db", db_path])
    assert args.log_max_bytes == 8388608, "the knob did not resolve; this proves nothing"
    assert st.TRACKER_LOG_BYTES_CAP != args.log_max_bytes, (
        "the default and the override are the same number, so the assertion below "
        "would pass for the wrong reason")

    seen = []
    real = db._read_bounded_text

    def spy(path, max_bytes):
        seen.append((str(path), max_bytes))
        return real(path, max_bytes)

    monkeypatch.setattr(db, "_read_bounded_text", spy)
    checks_by_name(conn, args)
    caps = [c for (p, c) in seen if p == str(st.TRACKER_LOG_PATH)]
    assert caps == [8388608], f"the reader used {caps}, not the owner's 8388608: {seen}"
    conn.close()


# --- config.file: the settings layer is only honest if doctor reads it out -----
def test_doctor_passes_a_clean_or_absent_config_file(tmp_path, monkeypatch):
    """No file is the normal case, and a readable one is the good case.

    A silent absence would be indistinguishable from "the tool ignored my file",
    so the check prints the path it looked at either way.
    """
    conn, db_path = _healthy_setup(tmp_path, monkeypatch)
    absent = tmp_path / "skillt-config.json"
    monkeypatch.setenv(cfg.CONFIG_PATH_ENV, str(absent))
    status, detail = checks_by_name(conn, Args(db_path))["config.file"]
    assert status == "PASS", detail
    assert str(absent) in detail and "not created" in detail

    absent.write_text('{"view.days": 45}', encoding="utf-8")
    status, detail = checks_by_name(conn, Args(db_path))["config.file"]
    assert status == "PASS", detail
    assert "1 key" in detail, detail
    conn.close()


def test_doctor_warns_loudly_about_a_setting_it_refused(tmp_path, monkeypatch):
    """The failure this check exists for: a value typed that never took effect.

    Owner types `view.days: "thirty"`, the screen still says 30, and without a
    line in `doctor` there is no way to find out why short of reading the source.
    """
    conn, db_path = _healthy_setup(tmp_path, monkeypatch)
    p = tmp_path / "skillt-config.json"
    p.write_text('{"view.days": "thirty"}', encoding="utf-8")
    monkeypatch.setenv(cfg.CONFIG_PATH_ENV, str(p))
    status, detail = checks_by_name(conn, Args(db_path))["config.file"]
    assert status == "WARN", detail
    assert "is not a number" in detail, detail
    conn.close()


def test_the_suite_isolates_the_settings_file_the_checks_read(tmp_path):
    """The isolation fixture is the only thing keeping this check hermetic.

    `config.file` reads whatever path the environment resolves to, so a suite
    that forgot to isolate it would pass or fail depending on what the developer
    last typed into `skillt config set`. Pinned here, because that is a failure
    that looks like a code change.
    """
    assert cfg.config_path() == str(tmp_path / "isolated-skillt-config.json")


ROOT = Path(__file__).resolve().parents[2]


def test_the_python_floor_is_the_same_number_everywhere():
    """`requires-python`, the README badge and doctor must state one floor.

    The READMEs said 3.11+ while doctor warned below 3.10, so a user on 3.10 read
    "supported" in one place and "you are behind" in the other. The number is
    pinned in three prints and nothing says which one is right except this test.
    """
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'requires-python\s*=\s*">=\s*(\d+)\.(\d+)"', pyproject)
    assert m, "pyproject.toml states no requires-python"
    assert (int(m.group(1)), int(m.group(2))) == st.MIN_PYTHON, (
        f"requires-python and doctor disagree: {m.group(0)} vs {st.MIN_PYTHON}")
    plain = f"{st.MIN_PYTHON[0]}.{st.MIN_PYTHON[1]}+"
    for name in ("README.md", "README.zh-CN.md"):
        text = (ROOT / name).read_text(encoding="utf-8")
        # `+` is URL-encoded inside a shields.io badge, so the same claim is
        # spelled two ways in one file. Either counts; a third number does not.
        assert plain in text or plain.replace("+", "%2B") in text, (
            f"{name} states no Python floor of {plain}")
