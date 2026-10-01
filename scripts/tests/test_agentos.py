"""Read-only aggregation of the AgentOS advisor store.

The advisor registers no tool and no command, so it can never appear in the
usage tables; skillt reads its store instead. These tests pin the two promises
that make that acceptable: nothing is ever written to that store, and no field
is ever read that could carry the user's task text.
"""

from __future__ import annotations

import json
import os
import sqlite3

import pytest

from conftest import load_module

import skill_db as db


TASK_PROSE = "TASK-TEXT-MUST-NEVER-LEAVE-THE-STORE"
STAGE_PROSE = "STAGE-PAYLOAD-MUST-NEVER-LEAVE-THE-STORE"
ERROR_PROSE = "ENGINE-ERROR-TEXT-MUST-NEVER-LEAVE-THE-STORE"


def _make_store(tmp_path, *, with_loops=True, drop_retrieval=False):
    """A stand-in advisor store: the shape the aggregator expects, no AgentOS."""
    store = tmp_path / "store"
    store.mkdir()
    conn = sqlite3.connect(str(store / "aos.db"))
    conn.executescript(
        """
        CREATE TABLE telemetry_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, event_type TEXT NOT NULL,
            loop_id TEXT DEFAULT '', task_id TEXT DEFAULT '',
            payload_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL
        );
        CREATE TABLE retrieval_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, memory_id TEXT NOT NULL,
            loop_id TEXT DEFAULT '', query_hash TEXT DEFAULT '', score REAL DEFAULT 0,
            rank INTEGER DEFAULT 0, created_at TEXT NOT NULL
        );
        CREATE TABLE memories (id TEXT PRIMARY KEY, body TEXT);
        CREATE TABLE observations (id INTEGER PRIMARY KEY, kind TEXT);
        CREATE TABLE candidates (id INTEGER PRIMARY KEY, kind TEXT);
        CREATE TABLE learning_reviews (id INTEGER PRIMARY KEY, status TEXT);
        """
    )
    conn.execute(
        "INSERT INTO telemetry_events (event_type, created_at, payload_json)"
        " VALUES ('learning.run', '2026-10-01T05:00:00+00:00',"
        " '{\"candidates\":2,\"promoted\":1}')"
    )
    # An event type the aggregator has never heard of must still surface.
    conn.execute(
        "INSERT INTO telemetry_events (event_type, created_at)"
        " VALUES ('memory.retired', '2026-10-01T06:00:00+00:00')"
    )
    conn.execute("INSERT INTO retrieval_log (memory_id, created_at)"
                 " VALUES ('m1', '2026-10-01T05:00:01+00:00')")
    conn.execute("INSERT INTO retrieval_log (memory_id, created_at)"
                 " VALUES ('m2', '2026-10-01T05:00:02+00:00')")
    conn.execute("INSERT INTO memories (id, body) VALUES ('m1', ?)", (TASK_PROSE,))
    conn.execute("INSERT INTO observations (kind) VALUES ('tool')")
    conn.execute("INSERT INTO candidates (kind) VALUES ('hypothesis')")
    conn.execute("INSERT INTO learning_reviews (status) VALUES ('pending')")
    conn.commit()
    if drop_retrieval:
        conn.execute("DROP TABLE retrieval_log")
        conn.commit()
    conn.close()

    if with_loops:
        loops = store / "loops"
        loops.mkdir()
        loop = {
            "loop_id": "LOOP-TEST-1",
            "session_id": "ses_test_1",
            "stage": "finalize",
            "final_status": "partial",
            "created_at": "2026-10-01T05:00:00.000000+00:00",
            "updated_at": "2026-10-01T05:00:09.000000+00:00",
            "memory_mode": "enabled",
            "provider": "host_delegate",
            "model": "opencode/test-model",
            "task_text": TASK_PROSE,          # must never be read
            "cwd": "/some/project",
            "errors": [],
            "stages": {
                "route": {"status": "completed",
                          "started_at": "2026-10-01T05:00:00.000000+00:00",
                          "completed_at": "2026-10-01T05:00:00.050000+00:00",
                          "error": "", "data": {"prompt": STAGE_PROSE}},
                "recall": {"status": "completed",
                           "started_at": "2026-10-01T05:00:00.060000+00:00",
                           "completed_at": "2026-10-01T05:00:04.060000+00:00",
                           "error": "", "data": {"memories": [TASK_PROSE]}},
                "execute": {"status": "pending", "started_at": "",
                            "completed_at": "", "error": ""},
            },
            "postflight": {
                "aos_status": "ok", "phase": "post", "final_status": "partial",
                "warnings": ["w"], "aos_error": ERROR_PROSE,
                "learning": {"candidates_recorded": 2, "needs_review": 1},
                "recovery": {"recovery_attempted": False},
                "task_id": "t1",
            },
        }
        (loops / "LOOP-TEST-1.json").write_text(json.dumps(loop), encoding="utf-8")
        # A half-written file must not take the digest down with it.
        (loops / "LOOP-TRUNCATED.json").write_text("{not json", encoding="utf-8")
    return str(store / "aos.db")


@pytest.fixture
def tracker(tmp_path):
    path = str(tmp_path / "tracker.db")
    conn = db.open_db(path, readonly=False)
    db.ensure_schema(conn)
    conn.execute(
        "INSERT INTO skill_usage (skill_name, session_id, project_path, trigger_type,"
        " status, timestamp, call_id) VALUES ('s1','ses_test_1','/p','tool_call',"
        " 'success','2026-10-01T05:00:05.000Z','c1')"
    )
    conn.execute(
        "INSERT INTO mcp_usage (server_name, tool_name, session_id, project_path,"
        " trigger_type, status, timestamp, call_id) VALUES ('srv','t','ses_test_1','/p',"
        " 'tool_call','success','2026-10-01T05:00:06.000Z','c2')"
    )
    conn.execute(
        "INSERT INTO mcp_usage (server_name, tool_name, session_id, project_path,"
        " trigger_type, status, timestamp, call_id) VALUES ('srv','t','ses_other','/p',"
        " 'tool_call','success','2026-10-01T05:00:07.000Z','c3')"
    )
    conn.commit()
    yield conn
    conn.close()


# --- resolution: never create, never guess ---------------------------------
def test_store_resolution_prefers_explicit_then_env(tmp_path, monkeypatch):
    real = _make_store(tmp_path)
    monkeypatch.delenv("AGENT_OS_ROOT", raising=False)
    monkeypatch.delenv("OPENCODE_SKILL_TRACKER_AGENTOS_DB", raising=False)
    monkeypatch.delenv("AOS_DB", raising=False)

    out = db.agentos_store()
    assert out["db"] is None and "no store configured" in out["reason"]

    monkeypatch.setenv("AGENT_OS_ROOT", str(tmp_path))
    assert db.agentos_store()["db"] == real

    monkeypatch.setenv("OPENCODE_SKILL_TRACKER_AGENTOS_DB", real)
    assert db.agentos_store()["db"] == real
    assert db.agentos_store(db_path=real)["db"] == real


def test_a_missing_store_is_reported_and_never_created(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCODE_SKILL_TRACKER_AGENTOS_DB", str(tmp_path / "nope.db"))
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE skill_usage (session_id TEXT)")
    res = db.agentos_summary(conn)
    assert res["available"] is False
    assert "no database at" in res["reason"]
    assert not (tmp_path / "nope.db").exists(), "aggregating must never create the store"


# --- privacy: whitelist projection, not scrubbing -------------------------
def test_task_text_and_payloads_are_never_read(tmp_path, tracker):
    res = db.agentos_summary(tracker, db_path=_make_store(tmp_path))
    assert res["available"] is True
    blob = json.dumps(res, ensure_ascii=False, default=str)
    for prose in (TASK_PROSE, STAGE_PROSE, ERROR_PROSE):
        assert prose not in blob, f"{prose} left the store"
    # ...while the facts that make the view useful are all still there.
    loop = res["loops"][0]
    assert loop["loop_id"] == "LOOP-TEST-1"
    assert loop["final_status"] == "partial"
    assert loop["postflight"]["aos_status"] == "ok"
    assert loop["postflight"]["warnings"] == 1
    assert loop["postflight"]["has_error"] is True, "an error must be visible as a flag"
    assert loop["postflight"]["candidates_recorded"] == 2
    assert "task_text" not in loop and "cwd" not in loop and "data" not in loop


def test_stage_timings_and_the_budget_flag(tmp_path, tracker):
    res = db.agentos_summary(tracker, db_path=_make_store(tmp_path))
    loop = res["loops"][0]
    assert loop["stages"]["route"]["ms"] == 50
    assert loop["stages"]["recall"]["ms"] == 4000
    assert loop["stages"]["execute"]["ms"] is None, "a pending stage has no duration"
    assert loop["slowest_stage"] == {"name": "recall", "ms": 4000}
    assert loop["over_budget_ms"] is True, "the advisor's default budget is 1200 ms"
    # Unknown stage order is preserved: route, recall, execute.
    assert list(loop["stages"]) == ["route", "recall", "execute"]


def test_the_join_with_tracker_usage(tmp_path, tracker):
    res = db.agentos_summary(tracker, db_path=_make_store(tmp_path))
    loop = res["loops"][0]
    assert loop["session_id"] == "ses_test_1"
    assert loop["usage"] == {"skill": 1, "mcp": 1, "plugin": 0}
    # a session the tracker never saw is reported as zero, not as absent
    assert res["usage_by_session"].get("ses_test_1") is not None


def test_a_truncated_loop_file_does_not_break_the_digest(tmp_path, tracker):
    res = db.agentos_summary(tracker, db_path=_make_store(tmp_path))
    assert res["loops_total"] == 2, "both files exist"
    assert len(res["loops"]) == 1, "the unparsable one is skipped, not fatal"


# --- degrade honestly, never raise ---------------------------------------
def test_unknown_telemetry_types_and_missing_tables(tmp_path, tracker):
    res = db.agentos_summary(tracker, db_path=_make_store(tmp_path, drop_retrieval=True))
    types = {t["event_type"] for t in res["telemetry"]}
    assert types == {"learning.run", "memory.retired"}, types
    assert res["store_counts"]["retrieval_log"] is None, "a missing table is None, not 0"
    assert res["store_counts"]["memories"] == 1
    assert res["available"] is True


def test_no_loops_directory_is_fine(tmp_path, tracker):
    res = db.agentos_summary(tracker, db_path=_make_store(tmp_path, with_loops=False))
    assert res["available"] is True
    assert res["loops"] == [] and res["loops_total"] == 0
    assert res["loops_dir"] is None


# --- the CLI surface -------------------------------------------------------
def test_cli_agentos_text_and_json(tmp_path, tracker, capsys, monkeypatch):
    st = load_module("skill-tui.py", "skill_tui_agentos")

    class A:
        json = False
        limit = 10

    args = A()
    for var in (db.AGENTOS_DB_ENV, "AOS_DB", "AGENT_OS_ROOT"):
        monkeypatch.delenv(var, raising=False)

    # unconfigured: say so plainly, exit 0 — an absent advisor is not a fault
    assert st._cli_agentos(tracker, args) == 0
    assert "not aggregated" in capsys.readouterr().out

    store = _make_store(tmp_path)
    monkeypatch.setenv(db.AGENTOS_DB_ENV, store)
    assert st._cli_agentos(tracker, args) == 0
    out = capsys.readouterr().out
    assert "AgentOS advisor" in out and "LOOP-TEST-1" in out
    assert "OVER BUDGET" in out
    assert TASK_PROSE not in out, "the CLI printed the task text"

    args.json = True
    assert st._cli_agentos(tracker, args) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["available"] is True
    assert TASK_PROSE not in json.dumps(doc, ensure_ascii=False)


def test_the_advisor_budget_is_configurable(tmp_path, tracker, monkeypatch):
    """M21: the budget is AgentOS's number, so the copy here must be overridable."""
    store = _make_store(tmp_path)
    monkeypatch.setenv("OPENCODE_SKILL_TRACKER_AOS_TIMEOUT_MS", "5000")
    res = db.agentos_summary(tracker, db_path=store)
    assert res["timeout_ms"] == 5000
    assert res["loops"][0]["over_budget_ms"] is False, "recall took 4000ms, under 5000"

    monkeypatch.setenv("OPENCODE_SKILL_TRACKER_AOS_TIMEOUT_MS", "not-a-number")
    assert db.agentos_summary(tracker, db_path=store)["timeout_ms"] == 1200
