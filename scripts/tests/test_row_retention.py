"""Row-level retention for the usage tables and `skill_versions`.

Three of this project's tables grow one row per event forever and `skill_versions`
grows one row per content change, so a database that is a reasonable size today is
bigger every week with nothing to say about it. The promise under test is the
opposite of a cleanup feature: **with the shipped defaults not one row is ever
deleted**, and the first row that does go, went because the owner named a number and
typed `--yes`.

Each test is a property the delete path has to keep:

* default = off, and provably no write (a row-by-row count, not "we believe the flag");
* what `--dry-run` counted is exactly what `--yes` deletes — the count is not a second
  query that merely looks like the delete;
* a row that cannot be dated is never deleted, because "older than N days" is a claim
  about a date we do not have;
* the newest N versions survive, so retention cannot eat the current content;
* `subagent_usage` is not in the delete set at all: those rows are events, not calls,
  and the spawn record is the only witness that a subagent ran;
* `--yes` always takes a backup first, from the one command line with no other net.
"""

from __future__ import annotations

import json
import os
import sqlite3

import pytest

from conftest import load_module

import skill_db as db
import settings as cfg

st = load_module("skill-tui.py", "skill_tui_retention")

STREAMS = ("skill_usage", "mcp_usage", "plugin_usage")
ALL_TABLES = STREAMS + ("subagent_usage", "skills", "skill_versions", "plugin_inventory")


def counts(conn) -> dict:
    """Rows in every table this feature could conceivably touch."""
    out = {}
    for t in ALL_TABLES:
        try:
            out[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        except sqlite3.Error:
            out[t] = None
    return out


@pytest.fixture
def aged_db(tmp_path):
    """A migrated DB with rows at known ages, including undatable ones.

    Built here rather than reused from `seeded_db`: those counts are asserted exactly
    by other files, and retention needs rows at 29/31/400 days plus two timestamps
    that will not parse.
    """
    path = str(tmp_path / "aged.db")
    conn = db.open_db(path, readonly=False)
    db.ensure_schema(conn)
    conn.execute("INSERT INTO skills (name, category, path, description) "
                 "VALUES ('aged','personal-skills','/s/aged','d')")

    def insert(table, cols, ago_days):
        keys = ", ".join(cols)
        marks = ", ".join("?" for _ in cols)
        conn.execute(
            f"INSERT INTO {table} ({keys}, timestamp) VALUES ({marks},"
            " strftime('%Y-%m-%dT%H:%M:%fZ','now', ?))",
            (*cols.values(), f"-{ago_days} days"),
        )

    for d in (1, 5, 29, 31, 45, 400):
        insert("skill_usage", {
            "skill_name": f"skill-{d}", "session_id": f"s-{d}", "project_path": "/proj",
            "trigger_type": "tool_call", "status": "success", "call_id": f"c-{d}"}, d)
    for d in (2, 60):
        insert("mcp_usage", {
            "server_name": "srv", "tool_name": f"tool-{d}", "session_id": f"m-{d}",
            "project_path": "/proj", "trigger_type": "tool_call", "status": "success",
            "call_id": f"mc-{d}"}, d)
    for d in (3, 90):
        insert("plugin_usage", {
            "plugin_name": "plug", "kind": "tool", "item_name": f"item-{d}",
            "session_id": f"p-{d}", "project_path": "/proj", "trigger_type": "tool_call",
            "status": "success", "call_id": f"pc-{d}"}, d)
    # Events, not calls — and the point of these rows is that they stay.
    for d in (4, 120):
        insert("subagent_usage", {
            "subagent": "worker", "parent_session_id": f"par-{d}",
            "child_session_id": f"kid-{d}", "call_id": f"sc-{d}",
            "project_path": "/proj", "trigger_type": "tool_call", "status": "success"}, d)

    # An empty timestamp and a prose one: neither can be compared against "30 days
    # ago", so neither may be deleted.
    conn.execute("INSERT INTO skill_usage (skill_name, session_id, project_path,"
                 " trigger_type, status, call_id, timestamp) VALUES"
                 " ('undated','s-u','/proj','manual','success','c-u','')")
    conn.execute("INSERT INTO skill_usage (skill_name, session_id, project_path,"
                 " trigger_type, status, call_id, timestamp) VALUES"
                 " ('prose','s-p','/proj','manual','success','c-p','not a timestamp')")

    # Versions: five for `aged`, h1 the most recent (10 days ago) and h5 the oldest
    # (50 days ago), plus two for `other`. Keeping 2 per skill deletes h3/h4/h5 —
    # the oldest three — and none of `other`, which only has two.
    for n in range(1, 6):
        conn.execute(
            "INSERT INTO skill_versions (skill_id, skill_name, content_hash, size_bytes,"
            " recorded_at) VALUES ((SELECT id FROM skills WHERE name='aged'), 'aged',"
            " ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ','now', ?))",
            (f"h{n}", 100 + n, f"-{n * 10} days"),
        )
    conn.execute("INSERT INTO skills (name, category, path, description) "
                 "VALUES ('other','personal-skills','/s/other','d')")
    for n in (1, 2):
        conn.execute(
            "INSERT INTO skill_versions (skill_id, skill_name, content_hash, size_bytes,"
            " recorded_at) VALUES ((SELECT id FROM skills WHERE name='other'), 'other',"
            " ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ','now', ?))",
            (f"o{n}", 10, f"-{n} days"),
        )
    conn.commit()
    conn.close()
    return path


# --- the defaults must be provably inert ------------------------------------
def test_the_knobs_ship_off():
    assert cfg.spec("retention.usage_days")["default"] == 0
    assert cfg.spec("retention.max_skill_versions")["default"] == 0
    assert cfg.spec("retention.usage_days")["minimum"] == 0, "0 is a value, not an absence"
    assert cfg.value("retention.usage_days") == 0
    assert cfg.value("retention.max_skill_versions") == 0


def test_off_means_the_plan_says_nothing_and_no_write_happens(aged_db):
    conn = db.open_db(aged_db, readonly=False)
    try:
        before = counts(conn)
        plan = db.plan_usage_retention(conn, usage_days=0, max_versions=0)
        assert plan["enabled"] is False, plan
        assert plan["usage_rows"] == 0 and plan["version_rows"] == 0, plan
        res = db.prune_usage(conn, usage_days=0, max_versions=0, dry_run=False)
        assert res["deleted"]["total"] == 0, res
        assert counts(conn) == before, "a disabled retention still wrote"
    finally:
        conn.close()


def test_a_negative_number_is_refused_before_it_becomes_a_full_delete(aged_db):
    """`-1` in the cutoff expression means "everything older than tomorrow".

    0 means off; a negative would mean the whole table. The parser refuses it and so
    does the data layer, because the layer is the last place that still knows what it
    is about to delete.
    """
    conn = db.open_db(aged_db, readonly=False)
    try:
        before = counts(conn)
        with pytest.raises(ValueError):
            db.prune_usage(conn, usage_days=-1, max_versions=0, dry_run=True)
        with pytest.raises(ValueError):
            db.plan_usage_retention(conn, usage_days=0, max_versions=-1)
        assert counts(conn) == before, "a refused call still wrote"
    finally:
        conn.close()


# --- dry-run must count what --yes deletes ----------------------------------
def test_dry_run_counts_exactly_what_the_delete_removes(aged_db):
    conn = db.open_db(aged_db, readonly=False)
    try:
        before = counts(conn)
        plan = db.plan_usage_retention(conn, usage_days=30, max_versions=2)
        # older than 30 days: skill 31/45/400 = 3, mcp 60 = 1, plugin 90 = 1, and the
        # three oldest of `aged`'s five versions. Every table considered is listed,
        # including the ones whose count is 0, so "nothing was considered" cannot be
        # read as "nothing was there".
        assert plan["by_table"] == {"skill_usage": 3, "mcp_usage": 1, "plugin_usage": 1,
                                    "skill_versions": 3}, plan["by_table"]
        assert plan["usage_rows"] == 5 and plan["version_rows"] == 3, plan
        assert plan["total"] == 8, plan
        assert plan["undated"] == 2, plan

        dry = db.prune_usage(conn, usage_days=30, max_versions=2, dry_run=True)
        assert dry["deleted"]["total"] == 0, "a dry run deleted rows"
        assert dry["plan"]["total"] == plan["total"] == 8, dry
        assert counts(conn) == before, "a dry run wrote"

        applied = db.prune_usage(conn, usage_days=30, max_versions=2, dry_run=False)
        assert applied["deleted"]["total"] == dry["plan"]["total"] == 8, (
            "the dry run counted something other than what the delete removed")
    finally:
        conn.close()


def test_the_delete_removes_the_oldest_and_keeps_the_rest(aged_db):
    conn = db.open_db(aged_db, readonly=False)
    try:
        before = counts(conn)
        plan = db.plan_usage_retention(conn, usage_days=30, max_versions=2)
        res = db.prune_usage(conn, usage_days=30, max_versions=2, dry_run=False)
        after = counts(conn)

        assert res["deleted"]["usage"] == plan["usage_rows"] == 5, res
        assert res["deleted"]["versions"] == plan["version_rows"] == 3, res
        assert res["dry_run"] is False
        for t in STREAMS:
            assert after[t] == before[t] - plan["by_table"][t], (t, before, after)
        assert after["skill_versions"] == before["skill_versions"] - 3, after
        # The tables this feature must never touch:
        assert after["skills"] == before["skills"], "retention deleted a skill row"
        assert after["plugin_inventory"] == before["plugin_inventory"]

        survivors = {r[0] for r in conn.execute("SELECT skill_name FROM skill_usage")}
        assert {"undated", "prose"} <= survivors, survivors
        assert not {"skill-31", "skill-45", "skill-400"} & survivors, survivors
        assert {"skill-1", "skill-5", "skill-29"} <= survivors, survivors

        kept = [r[0] for r in conn.execute(
            "SELECT content_hash FROM skill_versions WHERE skill_name='aged'"
            " ORDER BY recorded_at DESC")]
        # The two most recent, not the two that happen to sort first by name: the
        # failure this guards is retention eating the current content and keeping
        # the ancient copies.
        assert kept == ["h1", "h2"], f"retention kept the oldest versions: {kept}"
        assert conn.execute("SELECT COUNT(*) FROM skill_versions"
                            " WHERE skill_name='other'").fetchone()[0] == 2
    finally:
        conn.close()


def test_subagent_events_are_not_a_usage_stream_and_are_never_deleted(aged_db):
    """Even a one-day window leaves `subagent_usage` alone.

    These rows are not calls: they are the only witness that a subagent ran at all
    (README M24), and they grow by task spawns rather than by tool calls, so the
    volume that makes retention necessary elsewhere does not exist here.
    """
    conn = db.open_db(aged_db, readonly=False)
    try:
        before = counts(conn)
        plan = db.plan_usage_retention(conn, usage_days=1, max_versions=0)
        assert "subagent_usage" not in plan["by_table"], plan["by_table"]
        res = db.prune_usage(conn, usage_days=1, max_versions=0, dry_run=False)
        assert counts(conn)["subagent_usage"] == before["subagent_usage"] == 2, res
    finally:
        conn.close()


# --- a row we cannot date is a row we cannot call old -----------------------
def test_undatable_rows_are_reported_and_never_deleted(aged_db):
    conn = db.open_db(aged_db, readonly=False)
    try:
        plan = db.plan_usage_retention(conn, usage_days=1, max_versions=0)
        assert plan["undated"] == 2, plan
        res = db.prune_usage(conn, usage_days=1, max_versions=0, dry_run=False)
        assert res["deleted"]["usage"] == plan["usage_rows"], res
        rows = {r[0] for r in conn.execute("SELECT skill_name FROM skill_usage")}
        assert {"undated", "prose"} <= rows, rows
    finally:
        conn.close()


def test_a_pre_migration_database_prunes_only_what_exists(tmp_path):
    """An unmigrated DB has skill_usage and nothing else.

    The missing tables are reported as absent, not as empty, and nothing crashes:
    this is the same degrade contract every other reader already keeps.
    """
    legacy = str(tmp_path / "legacy.db")
    conn = db.open_db(legacy, readonly=False)
    conn.executescript(db.SCHEMA_SQL)
    conn.executescript(
        "DROP VIEW IF EXISTS v_mcp_totals; DROP VIEW IF EXISTS v_mcp_last30;"
        " DROP VIEW IF EXISTS v_mcp_history; DROP TABLE IF EXISTS mcp_usage;"
        " DROP TABLE IF EXISTS plugin_usage; DROP TABLE IF EXISTS plugin_inventory;"
        " DROP TABLE IF EXISTS subagent_usage;"
    )
    conn.execute("INSERT INTO skill_usage (skill_name, session_id, project_path,"
                 " trigger_type, status, call_id, timestamp) VALUES ('old','s','/p',"
                 " 'manual','success','c', strftime('%Y-%m-%dT%H:%M:%fZ','now','-60 days'))")
    conn.commit()
    try:
        plan = db.plan_usage_retention(conn, usage_days=30, max_versions=0)
        assert plan["tables_present"] == ["skill_usage"], plan
        assert plan["by_table"] == {"skill_usage": 1}, plan
        res = db.prune_usage(conn, usage_days=30, max_versions=0, dry_run=False)
        assert res["deleted"]["usage"] == 1, res
        assert conn.execute("SELECT COUNT(*) FROM skill_usage").fetchone()[0] == 0
    finally:
        conn.close()


def test_the_delete_is_atomic_and_reports_which_tables_it_touched(aged_db):
    """One transaction, so a crash mid-delete cannot empty half of the timeline."""
    conn = db.open_db(aged_db, readonly=False)
    try:
        res = db.prune_usage(conn, usage_days=30, max_versions=2, dry_run=False)
        assert set(res["by_table"]) == set(ALL_TABLES[:3]) | {"skill_versions"}, res
        assert conn.in_transaction is False, "the write left an open transaction"
    finally:
        conn.close()


# --- the command line -------------------------------------------------------
def _args(db_path, *extra):
    return st.parse_args(["--cli", "prune-usage", "--db", db_path, *extra])


def test_the_cli_dry_runs_by_default_and_deletes_only_with_yes(aged_db, tmp_path,
                                                               monkeypatch, capsys):
    monkeypatch.setattr(db, "BACKUP_DIR", str(tmp_path / "backups"))
    conn = db.open_db(aged_db, readonly=False)
    before = counts(conn)
    conn.close()

    # Armed by a flag, still dry: the default is the plan, not the delete.
    assert st.cli_main(_args(aged_db, "--keep-days", "30", "--keep-versions", "2")) == 0
    out = capsys.readouterr().out
    assert "[dry-run]" in out, out
    assert "Re-run with --yes" in out, out
    conn = db.open_db(aged_db, readonly=False)
    assert counts(conn) == before, "the default wrote"
    conn.close()

    # Knobs off: --yes has nothing to do, and does not do it.
    assert st.cli_main(_args(aged_db, "--yes")) == 0
    assert "off" in capsys.readouterr().out
    made = os.listdir(tmp_path / "backups") if (tmp_path / "backups").is_dir() else []
    assert not made, f"an off switch took a backup anyway: {made}"
    conn = db.open_db(aged_db, readonly=False)
    assert counts(conn) == before, "--yes with the knobs off deleted rows"
    conn.close()


def test_yes_takes_a_backup_before_it_deletes(aged_db, tmp_path, monkeypatch, capsys):
    """The delete path has no undo, so it makes one first, from the command line."""
    backups = tmp_path / "backups"
    monkeypatch.setattr(db, "BACKUP_DIR", str(backups))
    conn = db.open_db(aged_db, readonly=False)
    before = counts(conn)
    conn.close()

    rc = st.cli_main(_args(aged_db, "--keep-days", "30", "--keep-versions", "2", "--yes"))
    assert rc == 0
    assert "deleted 8 row" in capsys.readouterr().out

    files = [n for n in os.listdir(backups) if n.endswith(".db")] if backups.is_dir() else []
    assert len(files) == 1, f"expected exactly one pre-delete backup, saw {files}"

    conn = db.open_db(aged_db, readonly=False)
    after = counts(conn)
    assert after["skill_usage"] == before["skill_usage"] - 3, (before, after)
    assert after["skills"] == before["skills"], "the delete reached the skills table"
    # The backup is the undo: it must still hold the rows that were just deleted.
    snap = db.open_db(os.path.join(str(backups), files[0]), readonly=True)
    assert counts(snap) == before, "the backup was taken after the delete"
    snap.close()
    conn.close()


def test_the_knob_can_come_from_the_settings_file(aged_db, tmp_path, monkeypatch, capsys):
    """No flag on the command line, and the file still arms it — same precedence."""
    conf = tmp_path / "skillt-config.json"
    conf.write_text('{"retention.usage_days": 30}', encoding="utf-8")
    monkeypatch.setenv(cfg.CONFIG_PATH_ENV, str(conf))
    monkeypatch.setattr(db, "BACKUP_DIR", str(tmp_path / "backups"))

    assert st.cli_main(_args(aged_db)) == 0
    out = capsys.readouterr().out
    assert "[dry-run]" in out and "skill_usage=3" in out, out


def test_a_flag_refuses_an_impossible_number(aged_db):
    with pytest.raises(SystemExit) as ei:
        st.parse_args(["--cli", "prune-usage", "--keep-days", "-1"])
    assert ei.value.code == 2
    assert st.cli_main(_args(aged_db, "--keep-days", "30")) == 0


def test_json_output_carries_the_plan_not_just_a_count(aged_db, tmp_path, monkeypatch,
                                                       capsys):
    monkeypatch.setattr(db, "BACKUP_DIR", str(tmp_path / "backups"))
    assert st.cli_main(_args(aged_db, "--keep-days", "30", "--json")) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["dry_run"] is True
    assert doc["usage_days"] == 30 and doc["max_skill_versions"] == 0
    assert doc["by_table"]["skill_usage"] == 3
    assert doc["undated"] == 2
    assert doc["excluded"] == ["subagent_usage"], doc
    assert "vacuum" in doc["note"], doc["note"]
