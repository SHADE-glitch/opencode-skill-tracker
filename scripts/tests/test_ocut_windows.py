"""Tests for ocut_db's window, date-entry and schema-status surface.

The calendar-day windows and the typed date entry are the two places where this
project touches a *clock*, so both are pinned against fixed timestamps rather
than against `datetime.now()`: a window that is right "today" can be wrong at
23:59 or on the first minute of a month, and only a frozen `now` shows that.
"""

import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ocut_db as db


# --- windows ---------------------------------------------------------------

def ms(y, m, d, hh=0, mm=0, ss=0):
    """Local wall-clock time as epoch milliseconds, the unit every bound uses."""
    return int(datetime(y, m, d, hh, mm, ss).timestamp() * 1000)


def test_one_day_means_today_not_the_last_24_hours():
    """The claim in the constant's comment, pinned at 23:59.

    An hour-based "1d" would open at 23:59 yesterday and cut a session that
    started at 09:00 today out of "Today".
    """
    now = ms(2026, 10, 12, 23, 59)
    start, end = db.window_bounds("1d", now)
    assert (start, end) == (ms(2026, 10, 12), now)


def test_two_days_starts_at_yesterday_midnight():
    now = ms(2026, 10, 12, 8, 30)
    start, end = db.window_bounds("2d", now)
    assert start == ms(2026, 10, 11)
    assert end == now


@pytest.mark.parametrize("key, days", [("1d", 1), ("2d", 2), ("3d", 3),
                                       ("7d", 7), ("14d", 14), ("30d", 30)])
def test_every_preset_opens_at_local_midnight_n_days_back(key, days):
    """The claim is about **day boundaries**, not a duration.

    Asserting a span length would be wrong on a DST day (23 or 25 hours), and it
    is not what the preset promises. The boundary is what a person reads as
    "the last 7 days", and the last instant of the excluded day is what must sit
    outside the window.
    """
    now = ms(2026, 10, 12, 13, 7, 5)
    start, end = db.window_bounds(key, now)
    assert end == now
    assert start == ms(2026, 10, 12) - (days - 1) * 86_400_000
    assert start <= ms(2026, 10, 12)          # today is always inside
    assert end > now - 1                       # and now itself is included


def test_all_has_no_lower_bound():
    assert db.window_bounds("all", ms(2026, 10, 12))[0] is None


def test_a_window_crossing_a_month_boundary_still_counts_days():
    now = ms(2026, 11, 1, 0, 5)
    start, _ = db.window_bounds("3d", now)
    assert start == ms(2026, 10, 30)


def test_the_labels_name_the_preset_and_a_custom_span_shows_the_clock():
    assert db.window_label("7d") == "7 days"
    assert db.window_label("all") == "All time"
    assert db.window_label(db.CustomWindow(ms(2026, 10, 1), None,
                                           follow_now=True)).endswith("→ now")
    fixed = db.CustomWindow(ms(2026, 10, 1), ms(2026, 10, 5, 12, 0))
    assert "→ now" not in db.window_label(fixed)
    assert "10-05 12:00" in db.window_label(fixed)


# --- typed entry -----------------------------------------------------------

@pytest.mark.parametrize("text", ["10-01", "10/1", "10.01", "10-1"])
def test_the_ambiguous_two_field_date_is_month_first(text):
    assert db.parse_date(text, 2026).date() == datetime(2026, 10, 1).date()


def test_a_four_digit_leading_field_is_the_year():
    assert db.parse_date("2026-10-01", 1999).year == 2026


def test_a_two_digit_trailing_year_uses_the_yy_pivot():
    assert db.parse_date("10-01-69", 2026).year == 1969
    assert db.parse_date("10-01-70", 2026).year == 1970


def test_an_unrecognised_date_names_what_was_handed_in():
    with pytest.raises(ValueError, match="unrecognised date"):
        db.parse_date("not a date", 2026)
    with pytest.raises(ValueError, match="unrecognised date"):
        db.parse_date("13-45", 2026)


@pytest.mark.parametrize("text, want", [
    ("21:05", (21, 5)), ("9:5 PM", (21, 5)), ("09:05PM", (21, 5)),
    ("9 PM", (21, 0)), ("12:00 AM", (0, 0)), ("12:00 PM", (12, 0)),
])
def test_the_time_accepts_am_pm_and_displays_24_hour(text, want):
    assert db.parse_time(text) == want


def test_an_empty_time_is_not_midnight_it_is_an_error():
    """Silently reading a blank field as 00:00 would move a window by hours."""
    with pytest.raises(ValueError, match="empty time"):
        db.parse_time("   ")


def test_parse_datetime_returns_none_for_a_blank_date_only():
    assert db.parse_datetime("", "21:05", 2026) is None
    assert db.parse_datetime("  ", "", 2026) is None
    assert db.parse_datetime("10-12", "", 2026) == ms(2026, 10, 12)
    assert db.parse_datetime("10-12", "9:5 PM", 2026) == ms(2026, 10, 12, 21, 5)


def test_parse_datetime_fills_the_missing_year_from_the_view():
    """The picker shows MM/DD and the calendar view knows which year it is on."""
    assert db.parse_datetime("2-29", "", 2028) == ms(2028, 2, 29)
    with pytest.raises(ValueError):
        db.parse_datetime("2-29", "", 2027)   # not a leap year


def test_the_display_format_is_a_suggestion_not_a_syntax():
    assert db.format_date(datetime(2026, 10, 1)) == "10/01/2026"
    assert db.format_time(0, 0) == "00:00"
    assert db.format_time(23, 5) == "23:05"


# --- cache rate ------------------------------------------------------------

def test_the_cache_rate_divides_by_the_cacheable_input():
    """``hit / (hit + miss + write)`` — the ``Hit rate`` the panels print."""
    assert db.cache_hit_rate(800, 200) == 0.8
    assert db.cache_hit_rate(800, 100, 100) == 0.8
    assert db.cache_hit_rate(0, 100) == 0.0


def test_the_cache_rate_is_none_when_it_would_be_invented():
    """A step that sent neither hit nor miss must not read as a 0% hit rate."""
    assert db.cache_hit_rate(None, 100) is None
    assert db.cache_hit_rate(100, None) is None
    assert db.cache_hit_rate(0, 0) is None


# --- schema status ---------------------------------------------------------

def test_ensure_schema_names_a_brand_new_database_created():
    conn = sqlite3.connect(":memory:")
    assert db.ensure_schema(conn) == "created"
    assert db.ensure_schema(conn) == "current"


def test_a_database_from_a_future_build_is_left_alone_and_says_so(tmp_path):
    path = tmp_path / "usage.db"
    conn = db.open_db(path)
    db.ensure_schema(conn)
    conn.execute("UPDATE meta SET value='99' WHERE key='schema_version'")
    conn.execute("INSERT INTO tool_calls(call_id, tool_name) VALUES('prt_x','bash')")
    # a view definition this build has never seen. VIEWS_SQL opens by DROPping
    # every view it knows, so running it here would destroy the row that follows it
    conn.execute("DROP VIEW v_tools")
    conn.execute("CREATE VIEW v_tools AS SELECT 42 AS from_a_future_build")
    conn.commit()
    conn.close()
    conn = db.open_db(path)
    assert db.ensure_schema(conn) == "newer"
    # the future build's row survived, and so did a view this version does not know
    assert conn.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0] == 1
    assert conn.execute("SELECT from_a_future_build FROM v_tools").fetchone()[0] == 42
    conn.close()


def test_a_version_bump_clears_the_cursors_so_nothing_is_read_again(
        tmp_path, monkeypatch):
    """The cursors are the one thing a schema change cannot leave standing.

    A cursor into the host's rowid space plus rows written under the old DDL would
    re-index nothing while every panel still showed last version's numbers. The
    bump is simulated by moving the constant one past its current value, because
    the file just written is always at the current version — waiting for a real v3
    to find out whether this branch works would be the worse plan.
    """
    path = tmp_path / "usage.db"
    conn = db.open_db(path)
    db.ensure_schema(conn)
    conn.execute("INSERT INTO sync_state(name, cursor, host_count, host_max)"
                 " VALUES('part', 100, 5, 100)")
    conn.execute("INSERT INTO tool_calls(call_id, tool_name) VALUES('prt_x','bash')")
    conn.commit()
    assert db.ensure_schema(conn) == "current"   # no bump: nothing is cleared
    assert conn.execute("SELECT cursor FROM sync_state"
                        " WHERE name='part'").fetchone()[0] == 100
    monkeypatch.setattr(db, "SCHEMA_VERSION", db.SCHEMA_VERSION + 1)
    assert db.ensure_schema(conn) == "migrated"
    assert conn.execute("SELECT COUNT(*) FROM sync_state").fetchone()[0] == 0
    # rows are kept: they are re-derivable, and dropping them would silently empty
    # every panel until the next sync
    assert conn.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0] == 1
    conn.close()


def test_migrate_reports_a_locked_or_damaged_file_instead_of_a_no_op(tmp_path):
    path = tmp_path / "usage.db"
    path.write_bytes(b"this is not a database")
    assert db.migrate(path).startswith("failed: ")


def test_reset_enumerates_the_schema_so_a_new_table_cannot_hide(tmp_path):
    """A table added to TABLES_SQL and forgotten in reset() would survive --full.

    The list is read out of the database, so the test cannot be satisfied by
    editing it in step with a new table — which is the whole point.
    """
    path = tmp_path / "usage.db"
    conn = db.open_db(path)
    db.ensure_schema(conn)
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
        " AND name NOT IN ('meta','sqlite_sequence')")]
    assert tables, "reset() would pass on an empty set"
    for table in tables:
        cols = [c[1] for c in conn.execute(f"PRAGMA table_info({table})")]
        first = cols[0]
        conn.execute(f"INSERT OR IGNORE INTO {table}({first}) VALUES('probe')")
    conn.commit()
    db.reset(conn)
    for table in tables:
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table
    assert conn.execute("SELECT COUNT(*) FROM meta").fetchone()[0] == 1
    conn.close()


def test_stored_version_is_zero_for_a_database_with_no_meta_row(tmp_path):
    """A fresh file looks like version 0, and 0 must mean "new", not "ancient"."""
    path = tmp_path / "usage.db"
    conn = db.open_db(path)
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.commit()
    assert db.stored_version(conn) == 0
    assert db.ensure_schema(conn) == "created"
    conn.close()


def test_an_index_the_schema_no_longer_declares_is_retired_on_open(tmp_path):
    """`CREATE INDEX IF NOT EXISTS` can add one and can never take one back.

    A file written before an index was dropped from `TABLES_SQL` therefore keeps
    paying for it on every insert, with no query reaching it, for the rest of that
    file's life — the one piece of DDL a re-read cannot fix. An index this build
    does declare has to survive the sweep, and so does a primary key's own
    `sqlite_autoindex_*`, which is not droppable at all.
    """
    path = tmp_path / "usage.db"
    conn = db.open_db(path)
    db.ensure_schema(conn)
    declared = sorted(r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name GLOB 'idx_*'"))
    assert declared == sorted(db._DECLARED_INDEXES), "the sweep would retire the schema"
    for stale in ("idx_tool_sess", "idx_model_call_session"):
        conn.execute(f"CREATE INDEX {stale} ON tool_calls(ts)")
    conn.commit()
    assert db.ensure_schema(conn) == "current"
    left = sorted(r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name GLOB 'idx_*'"))
    assert left == declared
    assert conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='index'"
        " AND name LIKE 'sqlite_autoindex_%'").fetchone()[0] > 0
    conn.close()


def test_a_newer_database_keeps_the_indexes_this_build_has_never_seen(tmp_path):
    """The sweep is a write, so it stops where every other one does: at "newer".

    A future build is entitled to declare index names this one does not know, and
    dropping them would be the same mistake as running `VIEWS_SQL` over its views.
    """
    path = tmp_path / "usage.db"
    conn = db.open_db(path)
    db.ensure_schema(conn)
    conn.execute("UPDATE meta SET value='99' WHERE key='schema_version'")
    conn.execute("CREATE INDEX idx_from_a_future_build ON tool_calls(ts)")
    conn.commit()
    assert db.ensure_schema(conn) == "newer"
    assert conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='index'"
        " AND name='idx_from_a_future_build'").fetchone()[0] == 1
    conn.close()


def test_no_declared_index_is_keyed_on_a_session_no_query_names():
    """The guard for a cut, not for a shape: `session_id` stayed unindexed.

    `idx_tool_sess` and `idx_model_call_session` existed from the first schema and
    no query ever filtered by either — the only joins on a session key go through
    `sessions`, whose own unique index serves them. Measured on the real row counts
    (tool_calls 30,638 / model_calls 24,904 / sessions 368): a full-table insert
    took 218.9 ms with the two indexes and 93.0 ms without, and the file 18.55 MiB
    against 16.44 MiB. Re-adding one has to come with the query that reads it.
    """
    conn = sqlite3.connect(":memory:")
    db.ensure_schema(conn)
    keyed = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name GLOB 'idx_*'"
        " AND sql LIKE '%session_id%'")]
    assert keyed == [], f"an index on a session key nothing queries is back: {keyed}"
    conn.close()


def test_no_declared_index_is_keyed_on_a_plugin_item_no_query_compares():
    """The same cut one table over: `item` is written and read, never compared.

    `idx_plugin_usage_item` keyed a column the shipped code only ever *selects*.
    `q_plugin_history`, `q_plugin_summary` and the plugin views return `item`, and
    nothing compares it — no `=`, `LIKE`, `IN` or `ORDER BY` on it, in
    `ocut_db.py`, `ocut-sync.py`, `ocut-stats.py` or the hook. The count is not
    read off a regex over source (f-string SQL defeats that, which is how this one
    survived the first sweep): the suite's own statements were captured with
    `set_trace_callback` and re-planned with `EXPLAIN QUERY PLAN` on a real-size
    database, and the index is named in 0 of the 1,452 plans that parsed.

    The honest size of it: at today's 284 rows this buys nothing a stopwatch can
    see — a 265-row `plugin_usage` load took 0.50 ms with the index and 0.44 ms
    without. What it removes is a structure no reader reaches that the hook pays
    for on every write, forever. Extrapolated to 200,000 rows the same insert pass
    was 794.5 ms against 590.4 ms, and the file 26.7 MB against 23.2 MB.
    """
    conn = sqlite3.connect(":memory:")
    db.ensure_schema(conn)
    keyed = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name GLOB 'idx_*'"
        " AND sql LIKE '%plugin_usage(item)%'")]
    assert keyed == [], f"an index on a plugin item nothing queries is back: {keyed}"
    conn.close()
