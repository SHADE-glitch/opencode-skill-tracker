"""Tests for skill_db.health_report — bucket classification + risk flags.

All timestamps are anchored to a fixed reference instant and passed via
`now=`, so the 30/90-day boundaries are deterministic.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import skill_db as db

REF = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)


def _db(tmp_path):
    conn = db.open_db(str(tmp_path / "h.db"), readonly=False)
    db.ensure_schema(conn)
    return conn


def _skill(conn, name):
    conn.execute(
        "INSERT INTO skills (name, category, path, description) VALUES (?,?,?,?)",
        (name, "personal-skills", f"/s/{name}", "d"),
    )


def _use(conn, name, status="success", at=None, i=0):
    conn.execute(
        "INSERT INTO skill_usage (skill_name, session_id, project_path, trigger_type,"
        " status, timestamp, call_id) VALUES (?,?,?,?,?,?,?)",
        (
            name, f"s{name}{i}", "/p", "tool_call", status,
            at.strftime("%Y-%m-%dT%H:%M:%S.%fZ") if at else None,
            f"c{name}{i}",
        ),
    )


def test_buckets_active_stale_unused_and_never(tmp_path):
    conn = _db(tmp_path)
    _skill(conn, "active")
    _skill(conn, "stale")
    _skill(conn, "old")
    _skill(conn, "never")
    _use(conn, "active", at=REF - timedelta(days=5))
    _use(conn, "stale", at=REF - timedelta(days=45))
    _use(conn, "old", at=REF - timedelta(days=200))
    conn.commit()

    rep = db.health_report(conn, now=REF)
    buckets = {s["skill_name"]: s["bucket"] for s in rep["skills"]}
    assert buckets == {
        "active": "active", "stale": "stale", "old": "unused", "never": "unused",
    }
    assert rep["counts"] == {"total": 4, "active": 1, "stale": 1, "unused": 2}
    assert rep["risk_counts"]["never_used"] == 1
    conn.close()


def test_thirty_day_boundary_is_inclusive(tmp_path):
    conn = _db(tmp_path)
    _skill(conn, "edge")
    _skill(conn, "just_past")
    _use(conn, "edge", at=REF - timedelta(days=30))          # exactly 30d -> active
    _use(conn, "just_past", at=REF - timedelta(days=30, seconds=1))  # -> stale
    conn.commit()

    buckets = {s["skill_name"]: s["bucket"] for s in db.health_report(conn, now=REF)["skills"]}
    assert buckets["edge"] == "active"
    assert buckets["just_past"] == "stale"
    conn.close()


def test_ninety_day_boundary_is_inclusive(tmp_path):
    conn = _db(tmp_path)
    _skill(conn, "edge")
    _skill(conn, "just_past")
    _use(conn, "edge", at=REF - timedelta(days=90))          # exactly 90d -> stale
    _use(conn, "just_past", at=REF - timedelta(days=90, seconds=1))  # -> unused
    conn.commit()

    buckets = {s["skill_name"]: s["bucket"] for s in db.health_report(conn, now=REF)["skills"]}
    assert buckets["edge"] == "stale"
    assert buckets["just_past"] == "unused"
    conn.close()


def test_high_failure_requires_min_uses(tmp_path):
    conn = _db(tmp_path)
    for name in ("one_fail", "four_fail", "five_fail", "five_mostly_ok"):
        _skill(conn, name)
    _use(conn, "one_fail", status="error", at=REF - timedelta(days=1))          # 1/1
    for i in range(4):
        _use(conn, "four_fail", status="error", at=REF - timedelta(days=1), i=i)  # 4/4
    for i in range(5):
        _use(conn, "five_fail", status="error", at=REF - timedelta(days=1), i=i)  # 5/5
    for i in range(5):
        _use(conn, "five_mostly_ok", status="error" if i < 2 else "success",
             at=REF - timedelta(days=1), i=i)                                      # 2/5 = 0.4
    conn.commit()

    flagged = {
        s["skill_name"] for s in db.health_report(conn, now=REF)["skills"]
        if "high_failure" in s["flags"]
    }
    assert flagged == {"five_fail"}, "floor is total>=5 AND rate>=0.5"
    conn.close()


def test_high_failure_counts_denied(tmp_path):
    conn = _db(tmp_path)
    _skill(conn, "denied_heavy")
    _use(conn, "denied_heavy", status="error", at=REF - timedelta(days=1), i=0)
    for i in (1, 2):
        _use(conn, "denied_heavy", status="denied", at=REF - timedelta(days=1), i=i)
    for i in (3, 4):
        _use(conn, "denied_heavy", status="success", at=REF - timedelta(days=1), i=i)
    conn.commit()

    rep = db.health_report(conn, now=REF)
    entry = next(s for s in rep["skills"] if s["skill_name"] == "denied_heavy")
    assert entry["failure_rate"] == 0.6  # (1 error + 2 denied) / 5
    assert "high_failure" in entry["flags"]
    conn.close()


def test_frequently_edited_threshold(tmp_path):
    conn = _db(tmp_path)
    _skill(conn, "churny")
    _skill(conn, "calm")
    for i in range(5):
        conn.execute(
            "INSERT INTO skill_versions (skill_name, content_hash) VALUES (?,?)",
            ("churny", f"h{i}"),
        )
    for i in range(4):
        conn.execute(
            "INSERT INTO skill_versions (skill_name, content_hash) VALUES (?,?)",
            ("calm", f"h{i}"),
        )
    conn.commit()

    flags = {s["skill_name"]: s["flags"] for s in db.health_report(conn, now=REF)["skills"]}
    assert "frequently_edited" in flags["churny"]
    assert "frequently_edited" not in flags["calm"]
    conn.close()


def test_health_report_is_read_only(tmp_path):
    conn = _db(tmp_path)
    _skill(conn, "a")
    _use(conn, "a", at=REF - timedelta(days=2))
    conn.execute("INSERT INTO skill_versions (skill_name, content_hash) VALUES ('a','h')")
    conn.commit()

    def snapshot():
        return (
            tuple(conn.execute("SELECT COUNT(*), COALESCE(SUM(id),0) FROM skills").fetchone()),
            tuple(conn.execute("SELECT COUNT(*), COALESCE(SUM(id),0) FROM skill_usage").fetchone()),
            tuple(conn.execute("SELECT COUNT(*), COALESCE(SUM(id),0) FROM skill_versions").fetchone()),
        )

    before = snapshot()
    for _ in range(3):
        db.health_report(conn, now=REF)
    assert snapshot() == before, "health_report must not modify a single row"
    conn.close()


def test_suggestions_never_instruct_deletion(tmp_path):
    conn = _db(tmp_path)
    _skill(conn, "never")
    _skill(conn, "used")
    # Pruning advice is only emitted once enough sessions/days have been
    # observed; seed past the threshold so the real suggestions are exercised.
    for i in range(20):
        _use(conn, "used", at=REF - timedelta(days=i % 14), i=i)
    conn.commit()
    rep = db.health_report(conn, now=REF)
    assert rep["sample"]["enough_for_advice"] is True
    assert rep["suggestions"], "at least one suggestion"
    joined = " ".join(rep["suggestions"])
    assert "review" in joined and "never deletes" in joined
    conn.close()


def test_insufficient_data_suppresses_pruning_advice(tmp_path):
    conn = _db(tmp_path)
    _skill(conn, "never")
    _use(conn, "never", at=REF - timedelta(days=1))
    conn.commit()
    rep = db.health_report(conn, now=REF)
    assert rep["sample"]["enough_for_advice"] is False
    joined = " ".join(rep["suggestions"])
    assert "Not enough data" in joined
    assert "review" not in joined, "no pruning advice while the sample is too small"
    # the facts themselves are still reported
    assert rep["counts"]["total"] == 1
    conn.close()
