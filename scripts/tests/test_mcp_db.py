"""MCP read paths, write helpers, and tolerance of a pre-MCP database.

The `mcp_usage` table is created by the plugin and mirrored into
`SCHEMA_SQL`; `ensure_schema` only adds it to a database that already exists.
Migration is triggered by the TUI, `sync`, `doctor` and `cleanup-selftest` —
NOT by `health`, `insight` or `export` — so every MCP read has to survive
meeting a database that predates the table.
"""

from __future__ import annotations

import pytest

import skill_db as db


# ---------------------------------------------------------------------------
# Aggregates
# ---------------------------------------------------------------------------
def test_mcp_stats_rows_aggregates_by_server_and_tool(seeded_mcp_db):
    conn = db.open_db(seeded_mcp_db)
    rows = db.mcp_stats_rows(conn)
    by_key = {(r["server_name"], r["tool_name"]): r for r in rows}

    assert len(rows) == 5, [(r["server_name"], r["tool_name"]) for r in rows]

    read = by_key[("basic-memory", "read_note")]
    assert read["total"] == 3
    assert read["success"] == 3
    assert read["errors"] == 0
    assert read["denied"] == 0
    assert read["uses_30d"] == 3
    assert read["sessions"] == 3
    assert read["tool_id"] == "basic-memory_read_note"

    search = by_key[("basic-memory", "search_notes")]
    assert (search["total"], search["success"], search["errors"]) == (2, 1, 1)

    # The permission path only knows the server, so it lands in the '*' bucket.
    ctx = by_key[("context7", "*")]
    assert ctx["denied"] == 1
    assert ctx["tool_id"] == "context7_*"
    conn.close()


def test_mcp_stats_rows_orders_by_total_then_name(seeded_mcp_db):
    conn = db.open_db(seeded_mcp_db)
    rows = db.mcp_stats_rows(conn)
    assert [(r["server_name"], r["tool_name"]) for r in rows] == [
        ("basic-memory", "read_note"),      # 3
        ("basic-memory", "search_notes"),   # 2
        ("basic-memory", "write_note"),     # 1
        ("context7", "*"),                  # 1
        ("playwright", "browser_click"),    # 1
    ]
    conn.close()


def test_mcp_uses_30d_excludes_older_rows(seeded_mcp_db):
    conn = db.open_db(seeded_mcp_db)
    rows = {(r["server_name"], r["tool_name"]): r for r in db.mcp_stats_rows(conn)}
    # playwright/browser_click is 45 days old: counted overall, not in the window.
    click = rows[("playwright", "browser_click")]
    assert click["total"] == 1
    assert click["uses_30d"] == 0
    conn.close()


def test_mcp_server_rows_rolls_up_per_server(seeded_mcp_db):
    conn = db.open_db(seeded_mcp_db)
    rows = db.mcp_server_rows(conn)
    assert [(r["server_name"], r["total"], r["tools"]) for r in rows] == [
        ("basic-memory", 6, 3),
        ("context7", 1, 1),
        ("playwright", 1, 1),
    ]
    bm = rows[0]
    assert (bm["success"], bm["errors"], bm["denied"]) == (4, 1, 1)
    conn.close()


def test_mcp_recent_rows_are_newest_first(seeded_mcp_db):
    conn = db.open_db(seeded_mcp_db)
    rows = db.mcp_recent_rows(conn, limit=3)
    assert len(rows) == 3
    stamps = [r["timestamp"] for r in rows]
    assert stamps == sorted(stamps, reverse=True)
    conn.close()


def test_mcp_tool_detail_includes_history(seeded_mcp_db):
    conn = db.open_db(seeded_mcp_db)
    detail = db.mcp_tool_detail(conn, "basic-memory", "search_notes")
    assert detail["total"] == 2
    assert detail["errors"] == 1
    assert len(detail["history"]) == 2
    assert detail["avg_ms"] == pytest.approx(700.0)  # (500 + 900) / 2

    assert db.mcp_tool_detail(conn, "basic-memory", "nope") is None
    conn.close()


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------
def test_dashboard_reports_mcp_counts_separately(seeded_mcp_db):
    conn = db.open_db(seeded_mcp_db)
    s = db.dashboard_summary(conn)
    assert s["total_mcp"] == 8
    assert s["today_mcp"] == 0, "every fixture row is dated in the past"
    # The skill counters must keep meaning skills only.
    assert s["total_usage"] == 0
    conn.close()


# ---------------------------------------------------------------------------
# Write helpers
# ---------------------------------------------------------------------------
def test_clear_usage_also_clears_mcp(seeded_mcp_db):
    conn = db.open_db(seeded_mcp_db, readonly=False)
    res = db.clear_usage(conn)
    assert res["usage_deleted"] == 0
    assert res["mcp_deleted"] == 8
    assert conn.execute("SELECT COUNT(*) FROM mcp_usage").fetchone()[0] == 0
    conn.close()


def test_clear_usage_clears_both_tables(seeded_db):
    """The TUI button says "Clear usage"; it must not leave MCP counts behind."""
    conn = db.open_db(seeded_db, readonly=False)
    conn.execute(
        "INSERT INTO mcp_usage (server_name, tool_name, session_id, trigger_type,"
        " status, call_id) VALUES ('srv','tool','sess','tool_call','success','c1')"
    )
    conn.commit()

    res = db.clear_usage(conn)
    assert res["usage_deleted"] == 21
    assert res["mcp_deleted"] == 1
    assert conn.execute("SELECT COUNT(*) FROM mcp_usage").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM skills").fetchone()[0] == 6
    conn.close()


def test_cleanup_selftest_covers_mcp_rows(seeded_mcp_db):
    conn = db.open_db(seeded_mcp_db, readonly=False)
    conn.execute(
        "INSERT INTO mcp_usage (server_name, tool_name, session_id, project_path,"
        " trigger_type, status, call_id) "
        "VALUES ('test-server','read_note','sess-st','/tmp/selftest-proj',"
        " 'tool_call','success','c-st')"
    )
    conn.commit()

    dry = db.cleanup_selftest(conn, dry_run=True)
    assert dry["matched"] == 0
    assert dry["mcp_matched"] == 1
    assert conn.execute("SELECT COUNT(*) FROM mcp_usage").fetchone()[0] == 9

    res = db.cleanup_selftest(conn, dry_run=False)
    assert res["mcp_matched"] == 1
    assert conn.execute("SELECT COUNT(*) FROM mcp_usage").fetchone()[0] == 8
    conn.close()


def test_delete_skill_leaves_mcp_untouched(seeded_mcp_db):
    """MCP rows have no skills row to hang off, so delete_skill must not see them."""
    conn = db.open_db(seeded_mcp_db, readonly=False)
    res = db.delete_skill(conn, "basic-memory")
    assert res["usage_deleted"] == 0
    assert conn.execute("SELECT COUNT(*) FROM mcp_usage").fetchone()[0] == 8
    conn.close()


# ---------------------------------------------------------------------------
# Pre-MCP databases
# ---------------------------------------------------------------------------
def test_mcp_reads_tolerate_a_legacy_db(legacy_db):
    """Every read must degrade to empty rather than raise on an old database."""
    conn = db.open_db(legacy_db)
    assert conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name='mcp_usage'"
    ).fetchone()[0] == 0, "fixture must not already have the table"

    assert db.mcp_stats_rows(conn) == []
    assert db.mcp_server_rows(conn) == []
    assert db.mcp_recent_rows(conn) == []
    assert db.mcp_tool_detail(conn, "srv", "tool") is None

    s = db.dashboard_summary(conn)
    assert s["total_mcp"] == 0 and s["today_mcp"] == 0
    assert db.export_document(conn)["mcp_usage"] == []
    conn.close()


def test_migration_adds_mcp_objects_to_a_legacy_db(legacy_db):
    conn = db.open_db(legacy_db, readonly=False)
    res = db.ensure_schema(conn)
    assert res["created_mcp"] is True

    tables = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert "mcp_usage" in tables
    views = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='view'")
    }
    assert {"v_mcp_totals", "v_mcp_last30", "v_mcp_history"} <= views

    # Reads now work instead of degrading.
    assert db.mcp_stats_rows(conn) == []
    assert db.dashboard_summary(conn)["total_mcp"] == 0
    conn.close()


def test_migration_is_idempotent_for_mcp(seeded_mcp_db):
    conn = db.open_db(seeded_mcp_db, readonly=False)
    first = db.ensure_schema(conn)
    second = db.ensure_schema(conn)
    assert first["created_mcp"] is False, "fixture is already migrated"
    assert second["created_mcp"] is False
    assert conn.execute("SELECT COUNT(*) FROM mcp_usage").fetchone()[0] == 8
    conn.close()


def test_clear_usage_tolerates_a_legacy_db(legacy_db):
    conn = db.open_db(legacy_db, readonly=False)
    res = db.clear_usage(conn)
    assert res["mcp_deleted"] == 0
    conn.close()


def test_cleanup_selftest_tolerates_a_legacy_db(legacy_db):
    conn = db.open_db(legacy_db, readonly=False)
    res = db.cleanup_selftest(conn, dry_run=False)
    assert res["mcp_matched"] == 0
    conn.close()


# ---------------------------------------------------------------------------
# Daily activity
# ---------------------------------------------------------------------------
def test_daily_mcp_activity_counts_last_7_days(seeded_mcp_db):
    conn = db.open_db(seeded_mcp_db)
    days = db.daily_mcp_activity(conn, 7)
    conn.close()
    assert len(days) == 7
    assert [d["date"] for d in days] == sorted(d["date"] for d in days)
    # 8 fixture rows, but the playwright one is 45 days old and out of window.
    assert sum(d["count"] for d in days) == 7


def test_daily_mcp_activity_is_zero_filled_on_a_legacy_db(legacy_db):
    conn = db.open_db(legacy_db)
    days = db.daily_mcp_activity(conn, 7)
    conn.close()
    assert len(days) == 7
    assert all(d["count"] == 0 for d in days), days
