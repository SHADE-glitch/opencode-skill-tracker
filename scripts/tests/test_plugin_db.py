"""Plugin read paths, write helpers, and tolerance of a pre-plugin database.

`plugin_usage` / `plugin_inventory` are created by the plugin and mirrored into
`SCHEMA_SQL`; `ensure_schema` only adds them to a database that already exists.
Migration is triggered by the TUI, `sync`, `doctor` and `cleanup-selftest` —
NOT by `health`, `insight` or `export` — so every plugin read has to survive
meeting a database that predates the tables.
"""

from __future__ import annotations

import pytest

import skill_db as db

DCP = "@tarquinen/opencode-dcp@3.2.0"
CONDUCTOR = "opencode-conductor-plugin"


# ---------------------------------------------------------------------------
# Aggregates
# ---------------------------------------------------------------------------
def test_plugin_stats_rows_aggregates_by_plugin_kind_and_item(seeded_plugin_db):
    conn = db.open_db(seeded_plugin_db)
    rows = db.plugin_stats_rows(conn)
    by_key = {(r["plugin_name"], r["kind"], r["item_name"]): r for r in rows}

    assert len(rows) == 4, [(r["plugin_name"], r["kind"], r["item_name"]) for r in rows]

    compress = by_key[(DCP, "tool", "compress")]
    assert compress["total"] == 4
    assert compress["success"] == 3
    assert compress["errors"] == 1
    assert compress["denied"] == 0
    assert compress["uses_30d"] == 4
    assert compress["sessions"] == 4
    assert compress["item_id"] == f"{DCP}/tool/compress"

    # Commands carry no duration and only ever a non-terminal status.
    status_cmd = by_key[(CONDUCTOR, "command", "conductor:status")]
    assert status_cmd["total"] == 1
    assert status_cmd["success"] == 0
    assert status_cmd["avg_ms"] is None

    # A tool no installed plugin could be matched to lands in the unknown bucket.
    assert (("(unknown)", "tool", "mystery_tool")) in by_key
    conn.close()


def test_plugin_stats_rows_orders_by_total_then_name(seeded_plugin_db):
    conn = db.open_db(seeded_plugin_db)
    rows = db.plugin_stats_rows(conn)
    assert [(r["plugin_name"], r["kind"], r["item_name"]) for r in rows] == [
        (DCP, "tool", "compress"),              # 4
        ("(unknown)", "tool", "mystery_tool"),  # 1, '(' sorts before '@'
        (DCP, "tool", "expand"),                # 1
        (CONDUCTOR, "command", "conductor:status"),  # 1
    ]
    conn.close()


def test_plugin_uses_30d_excludes_older_rows(seeded_plugin_db):
    conn = db.open_db(seeded_plugin_db)
    rows = {
        (r["plugin_name"], r["kind"], r["item_name"]): r for r in db.plugin_stats_rows(conn)
    }
    # dcp/expand is 45 days old: counted overall, not in the window.
    expand = rows[(DCP, "tool", "expand")]
    assert expand["total"] == 1
    assert expand["uses_30d"] == 0
    conn.close()


def test_plugin_recent_rows_are_newest_first(seeded_plugin_db):
    conn = db.open_db(seeded_plugin_db)
    rows = db.plugin_recent_rows(conn, limit=3)
    assert len(rows) == 3
    stamps = [r["timestamp"] for r in rows]
    assert stamps == sorted(stamps, reverse=True)
    conn.close()


def test_plugin_item_detail_includes_history(seeded_plugin_db):
    conn = db.open_db(seeded_plugin_db)
    detail = db.plugin_item_detail(conn, DCP, "tool", "compress")
    assert detail["total"] == 4
    assert detail["errors"] == 1
    assert len(detail["history"]) == 4
    assert detail["avg_ms"] == pytest.approx(232.5)  # (10*3 + 900) / 4

    assert db.plugin_item_detail(conn, DCP, "tool", "nope") is None
    conn.close()


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------
def test_plugin_inventory_rows_joins_usage_and_parses_surface(seeded_plugin_db):
    conn = db.open_db(seeded_plugin_db)
    rows = db.plugin_inventory_rows(conn)
    by_name = {r["plugin_name"]: r for r in rows}
    assert len(rows) == 4

    dcp = by_name[DCP]
    assert dcp["version"] == "3.2.0"
    assert dcp["source"] == "npm"
    assert dcp["skipped"] == 0
    assert dcp["total"] == 5, "compress x4 plus expand x1"
    assert dcp["errors"] == 1
    # The JSON columns come back as lists, not raw text.
    assert dcp["tools"] == ["compress", "expand"]
    assert dcp["commands"] == ["dcp-compress"]

    # A plugin with no surface still appears, with empty lists.
    conductor = by_name[CONDUCTOR]
    assert conductor["tools"] == []
    assert conductor["commands"] == ["conductor:status"]
    conn.close()


def test_plugin_inventory_rows_keeps_excluded_plugins_last(seeded_plugin_db):
    conn = db.open_db(seeded_plugin_db)
    rows = db.plugin_inventory_rows(conn)
    # Monitored plugins first (by usage), then the excluded ones (by name).
    assert [r["plugin_name"] for r in rows] == [
        DCP,
        CONDUCTOR,
        "/cfg/plugin/skill-tracker.js",
        "@mohak34/opencode-notifier@0.4.0",
    ]
    assert [r["skipped"] for r in rows] == [0, 0, 1, 1]
    conn.close()


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------
def test_dashboard_skill_counters_ignore_plugin_rows(seeded_plugin_db):
    conn = db.open_db(seeded_plugin_db)
    s = db.dashboard_summary(conn)
    # Plugin usage must never inflate the skill counters.
    assert s["total_usage"] == 0
    assert s["today_usage"] == 0
    conn.close()


# ---------------------------------------------------------------------------
# Write helpers
# ---------------------------------------------------------------------------
def test_clear_usage_also_clears_plugins(seeded_plugin_db):
    conn = db.open_db(seeded_plugin_db, readonly=False)
    res = db.clear_usage(conn)
    assert res["usage_deleted"] == 0
    assert res["mcp_deleted"] == 0
    assert res["plugin_deleted"] == 7
    assert conn.execute("SELECT COUNT(*) FROM plugin_usage").fetchone()[0] == 0
    conn.close()


def test_clear_usage_keeps_the_inventory(seeded_plugin_db):
    """The inventory is what is installed, not what was used."""
    conn = db.open_db(seeded_plugin_db, readonly=False)
    db.clear_usage(conn)
    assert conn.execute("SELECT COUNT(*) FROM plugin_inventory").fetchone()[0] == 4
    conn.close()


def test_clear_usage_clears_all_three_streams(seeded_db):
    """The TUI button says "Clear usage"; it must not leave counts behind."""
    conn = db.open_db(seeded_db, readonly=False)
    conn.execute(
        "INSERT INTO mcp_usage (server_name, tool_name, session_id, trigger_type,"
        " status, call_id) VALUES ('srv','tool','sess','tool_call','success','c1')"
    )
    conn.execute(
        "INSERT INTO plugin_usage (plugin_name, kind, item_name, session_id,"
        " trigger_type, status, call_id) "
        "VALUES ('plug','tool','thing','sess','tool_call','success','c2')"
    )
    conn.commit()

    res = db.clear_usage(conn)
    assert res["usage_deleted"] == 21
    assert res["mcp_deleted"] == 1
    assert res["plugin_deleted"] == 1
    assert conn.execute("SELECT COUNT(*) FROM plugin_usage").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM skills").fetchone()[0] == 6
    conn.close()


def test_cleanup_selftest_covers_plugin_rows(seeded_plugin_db):
    conn = db.open_db(seeded_plugin_db, readonly=False)
    conn.execute(
        "INSERT INTO plugin_usage (plugin_name, kind, item_name, session_id,"
        " project_path, trigger_type, status, call_id) "
        "VALUES ('plug','tool','thing','sess-st','/tmp/selftest-proj',"
        " 'tool_call','success','c-st')"
    )
    conn.commit()

    dry = db.cleanup_selftest(conn, dry_run=True)
    assert dry["matched"] == 0
    assert dry["plugin_matched"] == 1
    assert conn.execute("SELECT COUNT(*) FROM plugin_usage").fetchone()[0] == 8

    res = db.cleanup_selftest(conn, dry_run=False)
    assert res["plugin_matched"] == 1
    assert conn.execute("SELECT COUNT(*) FROM plugin_usage").fetchone()[0] == 7
    conn.close()


def test_delete_skill_leaves_plugin_rows_untouched(seeded_plugin_db):
    """Plugin rows have no skills row to hang off, so delete_skill must not see them."""
    conn = db.open_db(seeded_plugin_db, readonly=False)
    res = db.delete_skill(conn, "compress")
    assert res["usage_deleted"] == 0
    assert conn.execute("SELECT COUNT(*) FROM plugin_usage").fetchone()[0] == 7
    conn.close()


def test_export_includes_plugin_usage_and_inventory(seeded_plugin_db):
    conn = db.open_db(seeded_plugin_db)
    doc = db.export_document(conn)
    assert doc["schema_version"] == 3
    assert len(doc["plugin_usage"]) == 7
    assert len(doc["plugin_inventory"]) == 4
    row = next(r for r in doc["plugin_usage"] if r["item_name"] == "compress")
    assert row["plugin_name"] == DCP
    assert row["kind"] == "tool"
    # metadata is parsed into an object, same as the other streams.
    assert row["metadata"].get("model") == "p/m"
    inv = next(r for r in doc["plugin_inventory"] if r["plugin_name"] == DCP)
    assert inv["tools"] == ["compress", "expand"]
    conn.close()


# ---------------------------------------------------------------------------
# Pre-plugin databases
# ---------------------------------------------------------------------------
def test_plugin_reads_tolerate_a_legacy_db(legacy_db):
    """Every read must degrade to empty rather than raise on an old database."""
    conn = db.open_db(legacy_db)
    assert conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name='plugin_usage'"
    ).fetchone()[0] == 0, "fixture must not already have the table"

    assert db.plugin_stats_rows(conn) == []
    assert db.plugin_inventory_rows(conn) == []
    assert db.plugin_recent_rows(conn) == []
    assert db.plugin_item_detail(conn, "plug", "tool", "thing") is None

    doc = db.export_document(conn)
    assert doc["plugin_usage"] == []
    assert doc["plugin_inventory"] == []
    conn.close()


def test_migration_adds_plugin_objects_to_a_legacy_db(legacy_db):
    conn = db.open_db(legacy_db, readonly=False)
    res = db.ensure_schema(conn)
    assert res["created_plugin"] is True

    tables = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"plugin_usage", "plugin_inventory"} <= tables
    views = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='view'")
    }
    assert {"v_plugin_totals", "v_plugin_last30", "v_plugin_history"} <= views

    # Reads now work instead of degrading.
    assert db.plugin_stats_rows(conn) == []
    assert db.plugin_inventory_rows(conn) == []
    conn.close()


def test_migration_is_idempotent_for_plugins(seeded_plugin_db):
    conn = db.open_db(seeded_plugin_db, readonly=False)
    first = db.ensure_schema(conn)
    second = db.ensure_schema(conn)
    assert first["created_plugin"] is False, "fixture is already migrated"
    assert second["created_plugin"] is False
    assert conn.execute("SELECT COUNT(*) FROM plugin_usage").fetchone()[0] == 7
    assert conn.execute("SELECT COUNT(*) FROM plugin_inventory").fetchone()[0] == 4
    conn.close()


def test_clear_usage_tolerates_a_legacy_db(legacy_db):
    conn = db.open_db(legacy_db, readonly=False)
    res = db.clear_usage(conn)
    assert res["plugin_deleted"] == 0
    conn.close()


def test_cleanup_selftest_tolerates_a_legacy_db(legacy_db):
    conn = db.open_db(legacy_db, readonly=False)
    res = db.cleanup_selftest(conn, dry_run=False)
    assert res["plugin_matched"] == 0
    conn.close()


# ---------------------------------------------------------------------------
# Daily activity
# ---------------------------------------------------------------------------
def test_daily_plugin_activity_counts_last_7_days(seeded_plugin_db):
    conn = db.open_db(seeded_plugin_db)
    days = db.daily_plugin_activity(conn, 7)
    conn.close()
    assert len(days) == 7
    assert [d["date"] for d in days] == sorted(d["date"] for d in days)
    # 7 fixture rows, but the expand one is 45 days old and out of window.
    assert sum(d["count"] for d in days) == 6


def test_daily_plugin_activity_is_zero_filled_on_a_legacy_db(legacy_db):
    conn = db.open_db(legacy_db)
    days = db.daily_plugin_activity(conn, 7)
    conn.close()
    assert len(days) == 7
    assert all(d["count"] == 0 for d in days), days
