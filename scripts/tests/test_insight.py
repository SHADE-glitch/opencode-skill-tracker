"""Insight analytics: ordering, thresholds, and the derived source column."""

from __future__ import annotations

import skill_db as db


def test_most_used_orders_by_count(seeded_db):
    conn = db.open_db(seeded_db)
    rows = db.insight_most_used(conn, limit=10)
    # decline has 6 uses (1 recent + 5 older), grow has 5
    assert rows[0]["skill_name"] == "decline"
    assert rows[0]["total"] == 6
    assert rows[1]["skill_name"] == "grow"
    assert rows[1]["total"] == 5
    conn.close()


def test_fastest_growing_respects_min_uses(seeded_db):
    conn = db.open_db(seeded_db)
    # min_uses=3 -> grow (5 recent) and bad (3 recent); flat (2) and decline (1) excluded
    rows = db.insight_fastest_growing(conn, days=30, min_uses=3, limit=10)
    names = [r["skill_name"] for r in rows]
    assert names[0] == "grow", names
    assert "bad" in names
    assert "flat" not in names and "decline" not in names, names
    assert rows[0]["recent"] == 5
    assert rows[0]["prev"] == 0
    assert rows[0]["delta"] == 5

    # lowering the threshold admits 'flat' (2 recent)
    rows2 = db.insight_fastest_growing(conn, days=30, min_uses=1, limit=10)
    names2 = [r["skill_name"] for r in rows2]
    assert "flat" in names2
    conn.close()


def test_fastest_growing_field_names_are_window_agnostic(seeded_db):
    """Field names must not embed the window (old names were *_30d)."""
    conn = db.open_db(seeded_db)
    for days in (7, 30, 90):
        rows = db.insight_fastest_growing(conn, days=days, min_uses=1, limit=10)
        for r in rows:
            assert set(r) == {"skill_name", "recent", "prev", "delta"}, r
    conn.close()


def test_long_unused_includes_never_used_and_stale(seeded_db):
    conn = db.open_db(seeded_db)
    rows = db.insight_long_unused(conn, days=30, limit=50)
    names = [r["skill_name"] for r in rows]
    assert "unused" in names, "never-used skill must be listed"
    assert "grow" not in names, "recently used skill must not be listed"
    # never-used sorts before stale-but-used
    assert names.index("unused") < names.index("decline") if "decline" in names else True
    conn.close()


def test_failure_rate_threshold_and_ordering(seeded_db):
    conn = db.open_db(seeded_db)
    rows = db.insight_failure_rate(conn, min_uses=3, limit=10)
    assert rows[0]["skill_name"] == "bad"
    assert rows[0]["fail_rate"] == 1.0
    # 'flat' has 4 uses and 0 failures -> present but last
    assert any(r["skill_name"] == "flat" for r in rows)

    # with a high threshold the low-count skills drop out entirely
    rows_hi = db.insight_failure_rate(conn, min_uses=5, limit=10)
    assert all(r["total"] >= 5 for r in rows_hi)
    conn.close()


def test_source_is_derived_not_stored(seeded_db):
    conn = db.open_db(seeded_db)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(skills)")]
    assert "source" not in cols, "source must be derived, never a column"

    rows = {r["skill_name"]: r["source"] for r in db.stats_rows(conn)}
    assert rows["grow"] == "personal"
    assert rows["decline"] == "open-source"
    conn.close()


def test_insight_bundle_shape(seeded_db):
    conn = db.open_db(seeded_db)
    data = db.insight(conn)
    assert set(data) == {"windows", "most_used", "fastest_growing", "long_unused", "highest_failure_rate"}
    assert data["windows"]["min_uses"] == 3
    conn.close()


def test_dashboard_and_categories(seeded_db):
    conn = db.open_db(seeded_db)
    s = db.dashboard_summary(conn)
    assert s["total_skills"] == 6
    assert s["total_usage"] == 5 + 4 + 6 + 3 + 3
    assert s["personal"] == 2 and s["open_source"] == 4

    cats = {c["source"]: c for c in db.categories(conn)}
    assert cats["personal"]["skills"] == 2
    assert cats["open-source"]["skills"] == 4

    days = db.daily_activity(conn, 7)
    assert len(days) == 7
    # grow 5 + flat 2 + decline 1 + bad 3 + ok 3 = 14 rows fall inside the 7-day window
    assert sum(d["count"] for d in days) == 14
    assert days[-1]["count"] == 0, "nothing is dated today in the fixture"
    conn.close()


def test_today_usage_uses_local_calendar(seeded_db):
    """`Today` and the daily trend must bucket by the local calendar day.

    A row written at the current instant has to land in the last bucket,
    whatever the machine's UTC offset is.
    """
    conn = db.open_db(seeded_db, readonly=False)
    conn.execute(
        "INSERT INTO skill_usage (skill_name, session_id, project_path, trigger_type,"
        " status, timestamp, call_id) "
        "VALUES ('ok','s-today','/proj','tool_call','success',"
        " strftime('%Y-%m-%dT%H:%M:%fZ','now'), 'c-today')"
    )
    conn.commit()

    assert db.dashboard_summary(conn)["today_usage"] >= 1
    days = db.daily_activity(conn, 7)
    assert days[-1]["count"] >= 1, "a row dated now must fall in the last (today) bucket"
    conn.close()
