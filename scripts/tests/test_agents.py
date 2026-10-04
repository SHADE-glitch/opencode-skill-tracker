"""Per-agent aggregation across the three usage streams (`skillt` Agents page).

Data-layer only — no Textual here on purpose: the aggregate has to be checked on a
machine without the TUI dependencies installed, which is exactly what the headless
subcommands run on.
"""

from __future__ import annotations

import sqlite3

import pytest

import skill_db as db


META_BUILD = '{"model":"p/m","agent":"build","branch":"main"}'
META_PLAN = '{"model":"p/m","agent":"plan","branch":"main"}'
META_NO_AGENT = '{"model":"p/m","branch":"main"}'


@pytest.fixture
def conn(tmp_path):
    path = str(tmp_path / "agents.db")
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    c.executescript(db.SCHEMA_SQL)
    yield c
    c.close()


def add_skill(c, name, session, status, ts, meta=META_BUILD):
    c.execute(
        "INSERT INTO skill_usage (skill_name, session_id, project_path, trigger_type,"
        " status, timestamp, duration_ms, call_id, metadata)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (name, session, "/p", "tool_call", status, ts, 10, f"c-{name}-{session}-{ts}", meta),
    )


def add_mcp(c, server, tool, session, status, ts, meta=META_BUILD):
    c.execute(
        "INSERT INTO mcp_usage (server_name, tool_name, session_id, project_path,"
        " trigger_type, status, timestamp, duration_ms, call_id, metadata)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (server, tool, session, "/p", "tool_call", status, ts, 10,
         f"c-{server}{tool}-{session}-{ts}", meta),
    )


def add_plugin(c, plugin, kind, item, session, status, ts, meta=META_BUILD):
    c.execute(
        "INSERT INTO plugin_usage (plugin_name, kind, item_name, session_id,"
        " project_path, trigger_type, status, timestamp, duration_ms, call_id, metadata)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (plugin, kind, item, session, "/p", "tool_call", status, ts, 10,
         f"c-{plugin}{item}-{session}-{ts}", meta),
    )


def _by_agent(rows):
    return {r["agent"]: r for r in rows}


# --- the aggregate ---------------------------------------------------------
def test_agents_rows_group_three_streams_by_agent(conn):
    add_skill(conn, "grow", "s1", "success", "2026-10-01T00:00:00.000Z")
    add_skill(conn, "grow", "s1", "error", "2026-10-02T00:00:00.000Z")
    add_skill(conn, "plan-skill", "s2", "success", "2026-10-01T12:00:00.000Z",
              meta=META_PLAN)
    add_mcp(conn, "srv", "tool", "s1", "denied", "2026-10-03T00:00:00.000Z")
    add_plugin(conn, "plug", "tool", "item", "s1", "success", "2026-10-01T06:00:00.000Z")
    conn.commit()

    rows = _by_agent(db.agent_usage_rows(conn))
    assert set(rows) == {"build", "plan"}, rows
    build = rows["build"]
    assert (build["skill"], build["mcp"], build["plugin"]) == (2, 1, 1), dict(build)
    assert build["total"] == 4
    assert build["success"] == 2 and build["errors"] == 1 and build["denied"] == 1
    assert build["last_used"] == "2026-10-03T00:00:00.000Z"
    assert rows["plan"]["total"] == 1
    # Biggest first, and the ordering is stable for equal totals.
    ordered = db.agent_usage_rows(conn)
    assert [r["agent"] for r in ordered] == ["build", "plan"], ordered


def test_agents_totals_equal_the_three_table_counts(conn):
    """Nothing may vanish between the tables and the group-by.

    A per-group query that silently drops rows still looks plausible — the totals
    are the only thing that says "this is the whole picture".
    """
    for i in range(5):
        add_skill(conn, "grow", f"s{i % 2}", "success", f"2026-10-0{i + 1}T00:00:00.000Z")
        add_mcp(conn, "srv", "tool", f"s{i % 2}", "error", f"2026-10-0{i + 1}T01:00:00.000Z",
                meta=META_PLAN)
    add_plugin(conn, "plug", "command", "x", "s0", "unknown", "2026-10-01T02:00:00.000Z")
    conn.commit()
    total = sum(r["total"] for r in db.agent_usage_rows(conn))
    expected = (
        conn.execute("SELECT COUNT(*) FROM skill_usage").fetchone()[0]
        + conn.execute("SELECT COUNT(*) FROM mcp_usage").fetchone()[0]
        + conn.execute("SELECT COUNT(*) FROM plugin_usage").fetchone()[0]
    )
    assert total == expected == 11, (total, expected)


# --- the bucket that must not be hidden ------------------------------------
def test_the_unknown_bucket_is_shown_not_hidden(conn):
    add_skill(conn, "grow", "s1", "success", "2026-10-01T00:00:00.000Z", meta=META_NO_AGENT)
    add_mcp(conn, "srv", "tool", "s2", "success", "2026-10-01T00:00:00.000Z", meta=None)
    add_skill(conn, "grow", "s3", "success", "2026-10-01T00:00:00.000Z", meta=META_BUILD)
    conn.commit()
    rows = db.agent_usage_rows(conn)
    assert len(rows) == 2, rows
    unknown = [r for r in rows if r["agent"] is None]
    assert len(unknown) == 1, rows
    assert unknown[0]["total"] == 2, unknown[0]
    assert sum(r["total"] for r in rows) == 3


def test_a_malformed_metadata_row_does_not_crash_the_aggregate(conn):
    """`json_extract` *raises* on invalid JSON, and old rows can hold anything.

    Without the `json_valid` guard one corrupt row makes the whole page fail to
    open — the failure is an exception out of a read-only screen.
    """
    add_skill(conn, "grow", "s1", "success", "2026-10-01T00:00:00.000Z", meta="not json")
    add_skill(conn, "grow", "s2", "success", "2026-10-01T01:00:00.000Z", meta=META_BUILD)
    conn.commit()
    rows = _by_agent(db.agent_usage_rows(conn))
    assert set(rows) == {None, "build"}, rows
    assert rows[None]["total"] == 1 and rows["build"]["total"] == 1


def test_a_non_string_agent_is_not_invented_as_a_label(conn):
    """A number where an agent name should be is unknown, not str()'d."""
    add_skill(conn, "grow", "s1", "success", "2026-10-01T00:00:00.000Z",
              meta='{"agent":7}')
    add_skill(conn, "grow", "s2", "success", "2026-10-01T00:00:00.000Z",
              meta='{"agent":"   "}')
    conn.commit()
    rows = db.agent_usage_rows(conn)
    assert [r["agent"] for r in rows] == [None], rows
    assert rows[0]["total"] == 2


# --- degrade like the other cross-stream readers ---------------------------
def test_a_legacy_db_without_mcp_or_plugin_tables_still_lists_agents(conn):
    conn.executescript(
        "DROP TABLE IF EXISTS mcp_usage; DROP TABLE IF EXISTS plugin_usage;"
    )
    add_skill(conn, "grow", "s1", "success", "2026-10-01T00:00:00.000Z")
    conn.commit()
    rows = db.agent_usage_rows(conn)
    assert len(rows) == 1, rows
    assert rows[0]["agent"] == "build" and rows[0]["skill"] == 1
    assert rows[0]["mcp"] == 0 and rows[0]["plugin"] == 0


def test_an_empty_database_lists_no_agents(conn):
    assert db.agent_usage_rows(conn) == []


def add_spawn(c, subagent, parent_session, call, status, ts, meta_agent="build"):
    """One recorded subagent run, written the way the plugin writes it."""
    c.execute(
        "INSERT INTO subagent_usage (parent_session_id, child_session_id, call_id,"
        " subagent, project_path, trigger_type, status, timestamp, duration_ms, metadata)"
        " VALUES (?,?,?,?,?,?,?,?,?,json_object('agent',?))",
        (parent_session, f"child-{call}", call, subagent, "/p", "event_detected",
         status, ts, 100, meta_agent),
    )


# --- subagents as first-class rows ----------------------------------------
def test_a_subagent_that_made_no_measured_call_still_gets_a_row(conn):
    """`explore` can spend a whole run on builtins and leave no usage row anywhere.

    Without a row of its own the Agents page cannot show it at all, which is the
    complaint this answers: the spawn record is the only evidence it ran.
    """
    add_skill(conn, "grow", "s0", "success", "2026-10-01T00:00:00.000Z")
    add_spawn(conn, "explore", "s0", "c1", "success", "2026-10-02T00:00:00.000Z")
    add_spawn(conn, "explore", "s0", "c2", "error", "2026-10-02T01:00:00.000Z")
    conn.commit()
    rows = _by_agent(db.agent_usage_rows(conn))
    assert "explore" in rows, sorted(rows, key=str)
    e = rows["explore"]
    assert (e["skill"], e["mcp"], e["plugin"], e["total"]) == (0, 0, 0, 0), e
    assert e["runs_as"] == 2 and e["runs_errors"] == 1, e
    assert e["subagent"] == 0, e
    assert e["is_subagent"] is True, e
    assert rows["build"]["is_subagent"] is False, rows["build"]


def test_a_name_that_is_both_parent_and_child_is_one_row_two_counts(conn):
    """`plan` can be a primary agent and a spawned subagent in the same window.

    Two rows for one name would read as two different agents; merging the two
    counts into one number would lose which role each was.
    """
    add_skill(conn, "grow", "s0", "success", "2026-10-01T00:00:00.000Z", meta=META_PLAN)
    add_spawn(conn, "plan", "s0", "c1", "success", "2026-10-02T00:00:00.000Z")
    add_spawn(conn, "auditor", "s1", "c2", "success", "2026-10-02T01:00:00.000Z",
              meta_agent="plan")
    conn.commit()
    rows = _by_agent(db.agent_usage_rows(conn))
    assert [r["agent"] for r in db.agent_usage_rows(conn)].count("plan") == 1
    p = rows["plan"]
    assert p["runs_as"] == 1 and p["subagent"] == 1, p
    assert p["is_subagent"] is True, p
    assert p["total"] == 1 and p["skill"] == 1, p


def test_spawns_and_runs_are_two_columns_and_never_inside_total(conn):
    """`Spawned` is a parent's count, `Ran as` a child's — one column cannot hold both.

    The invariant this protects: `Total` means skill + MCP + plugin calls on every
    row, whichever row that is. Folding an event in would let the same column mean
    two things depending on whether a session happened to delegate.
    """
    add_skill(conn, "grow", "s0", "success", "2026-10-01T00:00:00.000Z")
    add_spawn(conn, "explore", "s0", "c1", "success", "2026-10-02T00:00:00.000Z")
    add_spawn(conn, "explore", "s0", "c2", "success", "2026-10-02T00:00:01.000Z")
    conn.commit()
    rows = _by_agent(db.agent_usage_rows(conn))
    build, explore = rows["build"], rows["explore"]
    assert (build["subagent"], build["runs_as"]) == (2, 0), build
    assert (explore["subagent"], explore["runs_as"]) == (0, 2), explore
    for row in rows.values():
        assert row["total"] == row["skill"] + row["mcp"] + row["plugin"], row
    assert sum(r["total"] for r in rows.values()) == 1, rows
    assert sum(r["runs_as"] for r in rows.values()) == 2, rows


def test_a_spawn_with_no_parent_agent_lands_in_unknown_not_an_invented_name(conn):
    """The parent's agent is metadata; missing metadata has one home, `(unknown)`."""
    add_spawn(conn, "explore", "sx", "c1", "success", "2026-10-02T00:00:00.000Z",
              meta_agent=None)
    conn.commit()
    rows = _by_agent(db.agent_usage_rows(conn))
    assert None in rows, sorted(rows, key=str)
    assert rows[None]["subagent"] == 1, rows[None]
    assert rows["explore"]["runs_as"] == 1, rows["explore"]


def test_no_subagent_rows_means_no_extra_agent_names(conn):
    """The page must not invent names the spawn table never mentioned."""
    add_skill(conn, "grow", "s0", "success", "2026-10-01T00:00:00.000Z")
    conn.commit()
    rows = db.agent_usage_rows(conn)
    assert {r["agent"] for r in rows} == {"build"}, rows
    assert all(r["runs_as"] == 0 and r["is_subagent"] is False for r in rows), rows
