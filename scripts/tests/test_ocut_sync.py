"""Tests for the passive OpenCode reader: ocut-sync.py and the schema it fills.

Every fixture here is a *built* OpenCode database — the host's own table and JSON
shapes, copied off the real one on this machine (see the measurements in
ocut-sync.py's registry block). Nothing in this file reads or writes
``~/.local/share/opencode``: the host path is only ever a default that the tests
override.
"""

import json
import os
import sqlite3
import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ocut_db as db          # noqa: E402
import importlib.util         # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "ocut_sync_under_test",
    str(Path(__file__).resolve().parents[1] / "ocut-sync.py"))
sync = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sync)

# Prose the reader must never copy out of the host. Each sentinel sits in a field
# that is *present* in real OpenCode rows and is deliberately not in the whitelist.
SENTINELS = {
    "title": "SENTINEL-session-title-derived-from-the-first-message",
    "slug": "SENTINEL-tidy-two-word-label",
    "prompt": "SENTINEL-the-users-actual-prompt-text",
    "description": "SENTINEL-task-description-prose",
    "tool_title": "SENTINEL-tool-state-title-humans-read",
    "output": "SENTINEL-tool-output-content",
    "error": "SENTINEL-error-message-with-a-request-id",
    "reasoning": "SENTINEL-model-reasoning-text",
    "text": "SENTINEL-assistant-message-text",
    "metadata": "SENTINEL-tool-metadata",
    "path": "SENTINEL-message-path-cwd",
    "summary": "SENTINEL-user-message-summary",
    "msg_error": "SENTINEL-api-error-message",
    "worktree": "SENTINEL-project-worktree-root",
}


def make_host(path: Path) -> sqlite3.Connection:
    """Create a host database with OpenCode's real shape and fill it."""
    conn = sqlite3.connect(path)
    conn.executescript("""
    CREATE TABLE session (
        id TEXT PRIMARY KEY, project_id TEXT, workspace_id TEXT, parent_id TEXT,
        slug TEXT, directory TEXT, path TEXT, title TEXT, version TEXT,
        share_url TEXT, summary_additions INTEGER, summary_deletions INTEGER,
        summary_files INTEGER, summary_diffs TEXT, metadata TEXT,
        cost REAL, tokens_input INTEGER, tokens_output INTEGER,
        tokens_reasoning INTEGER, tokens_cache_read INTEGER,
        tokens_cache_write INTEGER, revert TEXT, permission TEXT, agent TEXT,
        model TEXT, time_created INTEGER, time_updated INTEGER,
        time_compacting INTEGER, time_archived INTEGER
    );
    CREATE TABLE message (
        id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER,
        time_updated INTEGER, data TEXT
    );
    CREATE TABLE part (
        id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT,
        time_created INTEGER, time_updated INTEGER, data TEXT
    );
    CREATE TABLE event (id TEXT PRIMARY KEY, data TEXT);
    """)
    return conn


def add_session(conn, sid, *, model=None, agent="build", parent=None,
                started=1_700_000_000_000, ended=1_700_000_600_000,
                tokens=(0, 0, 0, 0, 0), cost=0.0, project="proj1",
                directory="/home/user/work"):
    model = model or json.dumps({"id": "model-x", "providerID": "vendor",
                                 "variant": "max"})
    conn.execute(
        "INSERT INTO session(id, project_id, parent_id, slug, directory, path,"
        " title, version, cost, tokens_input, tokens_output, tokens_reasoning,"
        " tokens_cache_read, tokens_cache_write, agent, model, time_created,"
        " time_updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (sid, project, parent, SENTINELS["slug"], directory, SENTINELS["path"],
         SENTINELS["title"], "1.18.4", cost, *tokens, agent, model,
         started, ended))


def add_message(conn, mid, sid, *, role="assistant", model="model-x",
                provider="vendor", variant="max", agent="build",
                created=1_700_000_000_000, completed=None):
    data = {"role": role, "time": {"created": created}, "agent": agent}
    if role == "assistant":
        data.update({"mode": "build", "modelID": model, "providerID": provider,
                     "variant": variant, "path": {"cwd": SENTINELS["path"]},
                     "cost": 0, "tokens": {"input": 0, "output": 0},
                     "finish": "stop"})
        if completed:
            data["time"]["completed"] = completed
    else:
        data["model"] = {"providerID": provider, "modelID": model}
        data["summary"] = {"diffs": [SENTINELS["summary"]]}
    conn.execute("INSERT INTO message(id, session_id, time_created, data)"
                 " VALUES(?,?,?,?)", (mid, sid, created, json.dumps(data)))


def add_step(conn, pid, mid, sid, *, ts=1_700_000_100_000, reason="tool-calls",
             cost=0.0, tokens=None, message_present=True):
    """One `step-finish` part. `tokens=None` writes a step with no usage at all."""
    if tokens is None:
        tokens = {"total": 1000, "input": 100, "output": 20, "reasoning": 5,
                  "cache": {"read": 800, "write": 75}}
    data = {"type": "step-finish", "reason": reason, "cost": cost}
    if tokens != "absent":
        data["tokens"] = tokens
    conn.execute("INSERT INTO part(id, message_id, session_id, time_created, data)"
                 " VALUES(?,?,?,?,?)",
                 (pid, mid if message_present else "msg_gone", sid, ts,
                  json.dumps(data)))


def add_tool(conn, pid, mid, sid, *, ts=1_700_000_200_000, tool="bash",
             status="completed", state=None, start=1_700_000_200_000,
             end=1_700_000_201_000):
    body = state if state is not None else {}
    input_ = body.get("input", {})
    data = {"type": "tool", "tool": tool, "callID": "call_" + pid,
            "state": {
                "status": status,
                "input": input_,
                "title": SENTINELS["tool_title"],
                "output": SENTINELS["output"] if status != "running" else None,
                "metadata": dict({"truncated": False},
                                 **body.get("metadata", {}),
                                 **({"hidden": SENTINELS["metadata"]}
                                    if "metadata" not in body else {})),
                "time": {"start": start, "end": end},
            }}
    if status == "error":
        data["state"]["error"] = {"name": "Error",
                                  "data": {"message": SENTINELS["error"]}}
    if body:
        data["state"].update({k: v for k, v in body.items()
                              if k not in ("input", "metadata")})
    conn.execute("INSERT INTO part(id, message_id, session_id, time_created, data)"
                 " VALUES(?,?,?,?,?)",
                 (pid, mid, sid, ts, json.dumps(data)))


def add_ignored(conn, pid, mid, sid, ptype="text"):
    data = {"type": ptype, "text": SENTINELS["text"],
            "content": SENTINELS["reasoning"]}
    conn.execute("INSERT INTO part(id, message_id, session_id, time_created, data)"
                 " VALUES(?,?,?,?,?)",
                 (pid, mid, sid, 1_700_000_300_000, json.dumps(data)))


def make_config(root: Path, mcp=None, plugins=None, providers=None):
    """A config home with the directories and the JSON the reader scans."""
    (root / "agents").mkdir(parents=True, exist_ok=True)
    (root / "command").mkdir(parents=True, exist_ok=True)
    for name in ("auditor", "reviewer"):
        (root / "agents" / f"{name}.md").write_text(
            "---\ndescription: read-only audit\nmode: subagent\n---\nbody\n")
    (root / "command" / "digest.md").write_text("---\nagent: build\n---\nbody\n")
    (root / ".md.hidden.md").write_text("not an entity\n")
    for group, skill, sk_version in (("open-source", "tdd", None),
                                     ("personal", "triage", "2.1.0")):
        d = root / "skills" / group / skill
        d.mkdir(parents=True, exist_ok=True)
        fm = f"name: {skill}\n" + (f"version: {sk_version}\n" if sk_version else "")
        (d / "SKILL.md").write_text(f"---\n{fm}description: prose body\n---\nbody\n")
    cfg = {"$schema": "https://opencode.ai/config.json",
           "mcp": mcp if mcp is not None else {"context7": {"url": "https://x"}},
           "command": {},
           "plugin": plugins if plugins is not None else [],
           "provider": providers if providers is not None else {}}
    (root / "opencode.json").write_text(json.dumps(cfg))
    return cfg


@pytest.fixture
def host(tmp_path):
    """A host database with one session, one message, and the parts around them."""
    path = tmp_path / "opencode.db"
    conn = make_host(path)
    add_session(conn, "ses_parent", tokens=(100, 20, 5, 800, 0), cost=0.25,
                started=1_700_000_000_000, ended=1_700_000_900_000)
    add_message(conn, "msg_a", "ses_parent")
    add_step(conn, "prt_step1", "msg_a", "ses_parent", cost=0.25)
    add_tool(conn, "prt_bash", "msg_a", "ses_parent")
    add_ignored(conn, "prt_text", "msg_a", "ses_parent")
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def tracker(tmp_path):
    return tmp_path / "usage.db"


def run(host, tracker, **kw):
    return sync.run(db_path=tracker, host_db=host, **kw)


def rows(tracker, table, where="1"):
    conn = sqlite3.connect(tracker)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(f"SELECT * FROM {table} WHERE {where}")]
    finally:
        conn.close()


# --- the happy path --------------------------------------------------------

def test_a_step_finish_becomes_one_model_call(host, tracker):
    stats = run(host, tracker)
    assert stats["ok"] is True
    got = rows(tracker, "model_calls")
    assert len(got) == 1
    row = got[0]
    assert row["call_id"] == "prt_step1"
    assert row["message_id"] == "msg_a"
    assert row["session_id"] == "ses_parent"
    # the numbers come off the part, the model off its parent message
    assert (row["prompt_tokens"], row["completion_tokens"],
            row["reasoning_tokens"], row["cache_read"], row["cache_write"],
            row["total_tokens"]) == (100, 20, 5, 800, 75, 1000)
    assert row["model"] == "model-x" and row["provider"] == "vendor"
    assert row["variant"] == "max" and row["agent"] == "build"
    assert row["finish"] == "tool-calls"
    assert row["usage_available"] == 1 and row["missing"] is None
    # project/directory come from the session the message belongs to
    assert row["project"] == "proj1" and row["directory"] == "/home/user/work"


def test_the_session_rollup_is_stored_verbatim(host, tracker):
    run(host, tracker)
    row = rows(tracker, "sessions")[0]
    assert row["session_id"] == "ses_parent"
    assert row["model"] == "model-x" and row["provider"] == "vendor"
    assert (row["tokens_input"], row["tokens_output"], row["tokens_reasoning"],
            row["tokens_cache_read"]) == (100, 20, 5, 800)
    assert row["cost"] == 0.25
    assert row["started_at"] == 1_700_000_000_000
    assert row["ended_at"] == 1_700_000_900_000
    assert row["is_subagent"] == 0 and row["parent_id"] is None


def test_two_granularities_of_the_same_measurement_agree(host, tracker):
    """The invariant the schema comment claims, pinned here rather than asserted.

    ``sessions.tokens_*`` is the host's rollup and ``model_calls`` is its per-step
    record. If our parse is right, summing either family gives the host's number.
    A fixture where they disagree would mean the reader dropped or duplicated a
    step, which is exactly the failure the panels cannot show.
    """
    add_more_parts(host)
    run(host, tracker)
    s = rows(tracker, "sessions")
    m = rows(tracker, "model_calls")
    assert sum(r["tokens_input"] for r in s) == sum(r["prompt_tokens"] for r in m)
    assert sum(r["tokens_output"] for r in s) == sum(r["completion_tokens"] for r in m)
    assert sum(r["tokens_cache_read"] for r in s) == sum(r["cache_read"] for r in m)
    assert round(sum(r["cost"] for r in s), 6) == round(sum(r["cost"] for r in m), 6)


def add_more_parts(path):
    """Two more steps in the same session, with a matching rollup bump."""
    conn = sqlite3.connect(path)
    add_step(conn, "prt_step2", "msg_a", "ses_parent", ts=1_700_000_400_000,
             cost=0.1, tokens={"total": 500, "input": 40, "output": 10,
                               "reasoning": 0, "cache": {"read": 450, "write": 0}})
    add_step(conn, "prt_step3", "msg_a", "ses_parent", ts=1_700_000_500_000,
             cost=0.15, tokens={"total": 300, "input": 60, "output": 30,
                                "reasoning": 10, "cache": {"read": 200, "write": 0}})
    conn.execute("UPDATE session SET tokens_input=200, tokens_output=60,"
                 " tokens_reasoning=15, tokens_cache_read=1450, cost=0.5"
                 " WHERE id='ses_parent'")
    conn.commit()
    conn.close()


def test_tool_calls_land_in_tool_calls_and_their_specialised_table(host, tracker):
    conn = sqlite3.connect(host)
    add_tool(conn, "prt_skill", "msg_a", "ses_parent", tool="skill",
             state={"input": {"name": "tdd", "extra": SENTINELS["prompt"]}})
    add_tool(conn, "prt_task", "msg_a", "ses_parent", tool="task",
             state={"input": {"subagent_type": "explore",
                              "prompt": SENTINELS["prompt"],
                              "description": SENTINELS["description"]},
                    "metadata": {"sessionId": "ses_child",
                                 "parentSessionId": "ses_parent",
                                 "model": {"id": "model-x"}}})
    add_tool(conn, "prt_mcp", "msg_a", "ses_parent",
             tool="context7_query-docs", state={"input": {"query": "docs"}})
    conn.commit()
    conn.close()
    cfg = tracker.parent / "cfg"
    make_config(cfg, mcp={"context7": {"url": "https://x"}})
    sync.run(db_path=tracker, host_db=host, host_config=cfg)
    assert len(rows(tracker, "tool_calls")) == 4
    assert [r["skill"] for r in rows(tracker, "skill_usage")] == ["tdd"]
    spawn = rows(tracker, "agent_usage")[0]
    assert spawn["agent_type"] == "explore"
    assert spawn["child_session_id"] == "ses_child"
    mcp = rows(tracker, "mcp_usage")[0]
    assert (mcp["server"], mcp["tool"]) == ("context7", "query-docs")
    cats = {r["tool_name"]: r["category"] for r in rows(tracker, "tool_calls")}
    assert cats["skill"] == "skill"
    assert cats["task"] == "agent" and cats["context7_query-docs"] == "mcp"
    assert cats["bash"] == "builtin"


def test_duration_and_status_are_carried_from_the_host_state(host, tracker):
    conn = sqlite3.connect(host)
    add_tool(conn, "prt_err", "msg_a", "ses_parent", tool="bash", status="error",
             start=1_700_000_200_000, end=1_700_000_202_500)
    conn.commit()
    conn.close()
    run(host, tracker)
    row = [r for r in rows(tracker, "tool_calls") if r["call_id"] == "prt_err"][0]
    assert row["status"] == "error" and row["duration_ms"] == 2500


# --- privacy ---------------------------------------------------------------

def test_no_prose_field_is_copied_out_of_the_host(host, tracker):
    """Every sentinel in the fixture stays in the host.

    This is the whole privacy claim of the project, checked by scanning every
    value of every tracker table rather than by trusting the column list: a
    future writer that grabs ``state.title`` would empty the Skills panel *and*
    leak the command line, and only this scan sees the second half.
    """
    conn = sqlite3.connect(host)
    # a skill call's arguments carry prose too, so the sentinel goes INSIDE the
    # input dict: reading the whole dict instead of `name` must be caught.
    add_tool(conn, "prt_skill", "msg_a", "ses_parent", tool="skill",
             state={"input": {"name": "tdd", "instruction": SENTINELS["prompt"]},
                    "metadata": {"dir": "/x"}})
    add_tool(conn, "prt_task", "msg_a", "ses_parent", tool="task",
             state={"input": {"subagent_type": "explore",
                              "prompt": SENTINELS["prompt"],
                              "description": SENTINELS["description"]}})
    add_tool(conn, "prt_err", "msg_a", "ses_parent", tool="bash", status="error")
    add_ignored(conn, "prt_reason", "msg_a", "ses_parent", ptype="reasoning")
    conn.execute("INSERT INTO event(id,data) VALUES('evt1',?)",
                 (json.dumps({"x": SENTINELS["text"]}),))
    conn.commit()
    conn.close()
    make_config(tracker.parent / "cfg")
    cfg = tracker.parent / "cfg"
    make_config(cfg)
    stats = sync.run(db_path=tracker, host_db=host, host_config=cfg)
    assert stats["ok"] is True
    conn = sqlite3.connect(tracker)
    offenders = []
    for table, in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"):
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
        for row in conn.execute(f"SELECT * FROM {table}"):
            for col, value in zip(cols, row):
                if isinstance(value, str) and any(
                        s in value for s in SENTINELS.values()):
                    offenders.append((table, col, value[:60]))
    assert offenders == []


def test_a_child_session_is_flagged_and_its_parent_kept(host, tracker):
    conn = sqlite3.connect(host)
    add_session(conn, "ses_child", parent="ses_parent", agent="explore",
                tokens=(10, 1, 0, 5, 0))
    conn.commit()
    conn.close()
    run(host, tracker)
    by_id = {r["session_id"]: r for r in rows(tracker, "sessions")}
    assert by_id["ses_child"]["is_subagent"] == 1
    assert by_id["ses_child"]["parent_id"] == "ses_parent"
    assert by_id["ses_parent"]["is_subagent"] == 0


# --- cursors, idempotency and the mutating row -----------------------------

def test_a_second_run_reads_nothing_and_duplicates_nothing(host, tracker):
    first = run(host, tracker)
    assert first["parts_read"] == 3        # step-finish, tool, text
    before = rows(tracker, "tool_calls")
    second = run(host, tracker)
    assert second["parts_read"] == 0
    assert second["cursor"] == first["cursor"]
    assert rows(tracker, "tool_calls") == before


def test_the_cursor_holds_a_running_tool_and_the_next_run_finishes_it(host, tracker):
    """Cursor rule 1: a `running` part is written and re-read, never frozen.

    The host UPDATEs the same part row when the call ends, so a plain rowid cursor
    would keep the running status forever and every panel would report a call that
    never finished. The cursor stops at that row instead.
    """
    conn = sqlite3.connect(host)
    add_tool(conn, "prt_live", "msg_a", "ses_parent", tool="bash",
             status="running", start=1_700_000_600_000)
    conn.execute("DELETE FROM part WHERE id IN ('prt_text')")   # keep batch small
    conn.commit()
    conn.close()
    first = run(host, tracker)
    live = [r for r in rows(tracker, "tool_calls") if r["call_id"] == "prt_live"]
    assert live[0]["status"] == "running"
    assert first["cursor"] < first_host_max(host, "prt_live")

    state = rows(tracker, "sync_state", "name='part'")[0]
    assert "running part held" in state["note"]

    # the host finishes the call by updating the same row
    conn = sqlite3.connect(host)
    data = json.loads(conn.execute(
        "SELECT data FROM part WHERE id='prt_live'").fetchone()[0])
    data["state"]["status"] = "completed"
    data["state"]["time"]["end"] = 1_700_000_601_000
    conn.execute("UPDATE part SET data=? WHERE id='prt_live'", (json.dumps(data),))
    conn.commit()
    conn.close()

    second = run(host, tracker)
    live = [r for r in rows(tracker, "tool_calls") if r["call_id"] == "prt_live"][0]
    assert live["status"] == "completed"
    assert live["duration_ms"] == 1000
    assert second["cursor"] >= first["cursor"]


def first_host_max(path, part_id):
    conn = sqlite3.connect(path)
    try:
        return conn.execute(
            "SELECT rowid FROM part WHERE id=?", (part_id,)).fetchone()[0]
    finally:
        conn.close()


def test_a_stuck_running_part_is_passed_after_the_cap(host, tracker, monkeypatch):
    """The holdback is capped, and passing a row is a *recorded* decision."""
    monkeypatch.setattr(sync, "MAX_HELD_ROWS", 1)
    conn = sqlite3.connect(host)
    add_tool(conn, "prt_stuck", "msg_a", "ses_parent", tool="bash",
             status="running")
    for i in range(6):                      # push the held row far from the tail
        add_ignored(conn, f"prt_tail_{i}", "msg_a", "ses_parent")
    conn.commit()
    conn.close()
    stats = run(host, tracker)
    assert stats["unparsed_this_run"] >= 1
    reasons = {r["reason"] for r in rows(tracker, "unparsed")}
    assert sync.HELD_GAVE_UP in reasons
    assert "held at rowid" not in rows(tracker, "sync_state", "name='part'")[0]["note"]


def test_a_renumbered_host_table_resets_the_cursor_instead_of_trusting_it(host,
                                                                         tracker):
    """Cursor rule 3: max(rowid) below the cursor means rowids no longer mean
    what they did, so everything is re-read. Silently continuing would index
    nothing at all while printing "done"."""
    run(host, tracker)
    state = rows(tracker, "sync_state", "name='part'")[0]
    assert state["cursor"] > 0
    conn = sqlite3.connect(host)
    conn.execute("DELETE FROM part")
    add_step(conn, "prt_new", "msg_a", "ses_parent", ts=1_700_000_999_000)
    conn.commit()
    conn.close()
    stats = run(host, tracker)
    assert stats["parts_read"] >= 1
    fresh = rows(tracker, "model_calls", "call_id='prt_new'")
    assert len(fresh) == 1
    note = rows(tracker, "sync_state", "name='part'")[0]["note"]
    assert "renumbered" in note


def test_pruned_host_rows_are_reported_not_silently_dropped(host, tracker):
    """Cursor rule 2: the tracker keeps its own history and says what the host lost."""
    first = run(host, tracker)
    assert rows(tracker, "sync_state", "name='part'")[0]["host_count"] == 3
    conn = sqlite3.connect(host)
    conn.execute("DELETE FROM part WHERE id='prt_text'")
    conn.commit()
    conn.close()
    run(host, tracker, quiet=True)
    state = rows(tracker, "sync_state", "name='part'")[0]
    assert "pruned" in state["note"]
    assert "3 -> 3" not in state["note"]
    # and the indexed row for the deleted part is still there — this is an index
    # of history, not a mirror
    assert rows(tracker, "model_calls", "call_id='prt_step1'")


def test_sessions_are_re_read_in_full_because_the_host_rewrites_them(host, tracker):
    """Cursor rule 4: the rollup moves, so the row is reloaded, never cached."""
    run(host, tracker)
    assert rows(tracker, "sessions")[0]["tokens_input"] == 100
    conn = sqlite3.connect(host)
    conn.execute("UPDATE session SET tokens_input=999, cost=1.5"
                 " WHERE id='ses_parent'")
    conn.commit()
    conn.close()
    stats = run(host, tracker, quiet=True)
    row = rows(tracker, "sessions")[0]
    assert row["tokens_input"] == 999 and row["cost"] == 1.5
    assert stats["sessions"] == 1            # re-visited, not re-added
    assert len(rows(tracker, "sessions")) == 1


# --- drift detection -------------------------------------------------------

def test_an_unknown_part_type_is_counted_per_type(host, tracker):
    conn = sqlite3.connect(host)
    conn.execute("INSERT INTO part(id, message_id, session_id, time_created, data)"
                 " VALUES('prt_new','msg_a','ses_parent',1700000400000,?)",
                 (json.dumps({"type": "brand-new-thing", "x": 1}),))
    conn.commit()
    conn.close()
    run(host, tracker)
    got = {r["reason"]: r["count"] for r in rows(tracker, "unparsed")}
    assert got[f"{sync.UNKNOWN_PART_TYPE}:brand-new-thing"] == 1
    # and it produced no rows anywhere
    assert rows(tracker, "tool_calls", "call_id='prt_new'") == []


def test_a_json_blob_that_does_not_parse_is_counted_not_skipped(host, tracker):
    conn = sqlite3.connect(host)
    conn.execute("INSERT INTO part(id, message_id, session_id, time_created, data)"
                 " VALUES('prt_bad','msg_a','ses_parent',1700000400000,'{not json')")
    conn.commit()
    conn.close()
    run(host, tracker)
    got = {r["reason"] for r in rows(tracker, "unparsed")}
    assert sync.UNPARSEABLE_DATA in got


def test_a_step_without_a_message_row_still_keeps_its_numbers(host, tracker):
    """Losing a label must not throw away a token count."""
    conn = sqlite3.connect(host)
    add_step(conn, "prt_orphan", "msg_gone", "ses_parent",
             tokens={"total": 7, "input": 3, "output": 2, "reasoning": 1,
                     "cache": {"read": 1, "write": 0}})
    conn.commit()
    conn.close()
    run(host, tracker)
    run(host, tracker)
    row = rows(tracker, "model_calls", "call_id='prt_orphan'")[0]
    assert row["prompt_tokens"] == 3 and row["model"] is None
    assert row["usage_available"] == 1
    got = {r["reason"]: r["count"] for r in rows(tracker, "unparsed")}
    assert got[sync.MESSAGE_ROW_ABSENT] == 1


def test_a_step_with_no_tokens_object_is_counted(host, tracker):
    conn = sqlite3.connect(host)
    add_step(conn, "prt_notok", "msg_a", "ses_parent", tokens="absent")
    conn.commit()
    conn.close()
    run(host, tracker)
    assert rows(tracker, "model_calls", "call_id='prt_notok'") == []
    assert {r["reason"] for r in rows(tracker, "unparsed")} == {sync.NO_TOKENS}


def test_a_missing_usage_field_stays_null_and_is_named(host, tracker):
    conn = sqlite3.connect(host)
    add_step(conn, "prt_partial", "msg_a", "ses_parent",
             tokens={"input": 10, "output": 5})
    conn.commit()
    conn.close()
    run(host, tracker)
    row = rows(tracker, "model_calls", "call_id='prt_partial'")[0]
    assert row["reasoning_tokens"] is None
    assert json.loads(row["missing"]) == ["reasoning", "total", "cache.read",
                                          "cache.write"]
    # input and output are there, so the row DOES carry usage
    assert row["usage_available"] == 1


def test_a_step_with_neither_direction_carries_no_usage(host, tracker):
    conn = sqlite3.connect(host)
    add_step(conn, "prt_empty", "msg_a", "ses_parent",
             tokens={"total": 90, "reasoning": 1,
                     "cache": {"read": 0, "write": 0}})
    conn.commit()
    conn.close()
    run(host, tracker)
    row = rows(tracker, "model_calls", "call_id='prt_empty'")[0]
    assert row["usage_available"] == 0
    assert row["prompt_tokens"] is None


def test_a_skill_call_without_a_name_is_counted(host, tracker):
    conn = sqlite3.connect(host)
    add_tool(conn, "prt_no_name", "msg_a", "ses_parent", tool="skill",
             state={"input": {}})
    conn.commit()
    conn.close()
    run(host, tracker)
    assert len(rows(tracker, "tool_calls", "call_id='prt_no_name'")) == 1
    assert rows(tracker, "skill_usage") == []
    assert {r["reason"] for r in rows(tracker, "unparsed")} == {sync.NO_SKILL_NAME}


# --- MCP attribution -------------------------------------------------------

def mcp_host(tmp_path, tool_name, servers):
    path = tmp_path / "opencode.db"
    conn = make_host(path)
    add_session(conn, "ses1", tokens=(0, 0, 0, 0, 0))
    add_message(conn, "msg1", "ses1")
    add_tool(conn, "prt1", "msg1", "ses1", tool=tool_name, state={"input": {}})
    conn.commit()
    conn.close()
    cfg = tmp_path / "cfg"
    make_config(cfg, mcp={s: {"url": "https://x"} for s in servers})
    return path, cfg


def test_a_server_qualified_name_is_split_against_the_config(tmp_path, tracker):
    path, cfg = mcp_host(tmp_path, "context7_query-docs", ["context7"])
    sync.run(db_path=tracker, host_db=path, host_config=cfg)
    row = rows(tracker, "mcp_usage")[0]
    assert (row["server"], row["tool"]) == ("context7", "query-docs")


def test_the_longest_configured_prefix_wins(tmp_path, tracker):
    """`playwright` must not claim `playwright_browser_navigate` when a server
    named `playwright_browser` is also configured.

    The separator is what makes the short case safe by accident, so the fixture
    names the shape that is not: two configured servers, one a prefix of the other
    followed by the separator itself. Sorted the wrong way, the shorter server
    wins and the split lands in the middle of the tool's name.
    """
    path, cfg = mcp_host(tmp_path, "playwright_browser_navigate",
                         ["playwright", "playwright_browser"])
    stats = sync.run(db_path=tracker, host_db=path, host_config=cfg)
    row = rows(tracker, "mcp_usage")[0]
    assert (row["server"], row["tool"]) == ("playwright_browser", "navigate")
    assert stats["unparsed_this_run"] == 0


def test_a_name_no_configured_server_owns_stays_builtin_and_is_reported(tmp_path,
                                                                        tracker):
    """The playwright case, measured on the real host: 888 calls, no evidence.

    A server removed from the config leaves its historical tool names behind with
    nothing that names the server. Splitting on the underscore anyway would invent
    a server the host no longer declares, so the call stays `builtin` — and the
    run reports the two counts side by side instead of picking a winner, because
    `list_mcp_resources` has the same shape and is a builtin.
    """
    path, cfg = mcp_host(tmp_path, "playwright_browser_navigate", ["context7"])
    stats = sync.run(db_path=tracker, host_db=path, host_config=cfg)
    assert rows(tracker, "tool_calls")[0]["category"] == "builtin"
    assert rows(tracker, "mcp_usage") == []
    assert stats["unparsed_this_run"] == 0
    assert (stats["mcp_servers_configured"], stats["rows_mcp"],
            stats["mcp_shape_unowned_calls"]) == (1, 0, 1)


def test_a_builtin_name_containing_an_underscore_is_not_flagged(tmp_path, tracker):
    """`list_mcp_resources` is one of OpenCode's own tools, measured on this host.

    Guarding the other direction: a rule as loose as "any underscore is a server"
    would call a builtin an orphan and put a phantom server in the MCP tab.
    """
    path, cfg = mcp_host(tmp_path, "list_mcp_resources", ["context7"])
    stats = sync.run(db_path=tracker, host_db=path, host_config=cfg)
    assert rows(tracker, "tool_calls")[0]["category"] == "builtin"
    assert stats["unparsed_this_run"] == 0
    assert rows(tracker, "unparsed") == []


def test_the_server_list_is_reread_every_run(tmp_path, tracker):
    """A config edit must be visible without a restart (the TUI syncs every 30 s)."""
    path, cfg = mcp_host(tmp_path, "context7_query-docs", ["context7"])
    sync.run(db_path=tracker, host_db=path, host_config=cfg)
    assert rows(tracker, "mcp_usage")[0]["server"] == "context7"
    make_config(cfg, mcp={"playwright": {"command": ["pw"]}})
    conn = sqlite3.connect(path)
    add_tool(conn, "prt2", "msg1", "ses1", tool="playwright_browser_navigate",
             state={"input": {}})
    conn.commit()
    conn.close()
    sync.run(db_path=tracker, host_db=path, host_config=cfg, quiet=True)
    # `context7` stays: the usage tables are history, so removing a server from
    # the config does not delete the calls it made. Only the *inventory* forgets it.
    servers = {r["server"] for r in rows(tracker, "mcp_usage")}
    assert servers == {"context7", "playwright"}
    assert [r["name"] for r in rows(tracker, "inventory", "kind='mcp'")] == \
        ["playwright"]


# --- inventory -------------------------------------------------------------

def test_the_inventory_reads_the_directories_and_the_config(tmp_path, tracker):
    path = tmp_path / "opencode.db"
    conn = make_host(path)
    conn.commit()
    conn.close()
    cfg = tmp_path / "cfg"
    make_config(cfg, mcp={"context7": {}, "basic-memory": {}},
                plugins=["@mohak34/opencode-notifier@0.4.0",
                         "./plugins/claude-mem.js"],
                providers={"huoshan": {}})
    stats = sync.run(db_path=tracker, host_db=path, host_config=cfg)
    kinds = {r["kind"]: r["name"] for r in rows(tracker, "inventory")}
    by_kind: dict[str, list[str]] = {}
    for r in rows(tracker, "inventory"):
        by_kind.setdefault(r["kind"], []).append(r["name"])
    # the builtin is inventoried too, though no fixture put it on disk
    assert sorted(by_kind["skill"]) == sorted(
        ["tdd", "triage", sync.BUILTIN_SKILL_NAME])
    assert sorted(by_kind["agent"]) == ["auditor", "reviewer"]
    assert by_kind["command"] == ["digest"]
    assert sorted(by_kind["mcp"]) == ["basic-memory", "context7"]
    assert sorted(by_kind["provider"]) == ["huoshan"]
    assert by_kind["plugin"] == ["opencode-notifier", "claude-mem"]
    versions = {r["name"]: r["version"] for r in rows(tracker, "inventory")
                if r["kind"] == "plugin"}
    assert versions["opencode-notifier"] == "0.4.0"
    assert versions["claude-mem"] is None
    skill_versions = {r["name"]: r["version"] for r in rows(tracker, "inventory")
                      if r["kind"] == "skill"}
    assert skill_versions["triage"] == "2.1.0"
    assert stats["inventory"]["skill"] == 3


# --- the two skill roots the host loads from outside its config home --------


def external_home(tmp_path, *, claude=None, agents=None) -> Path:
    """A `$HOME` holding the roots OpenCode scans before it reads any config.

    Both come back as directories, because the reader only looks at a root that
    is already there -- the same `isDir` guard the host applies.
    """
    home = tmp_path / "home"
    for name, skills in ((sync.CLAUDE_SKILL_ROOT, claude),
                         (".agents", agents)):
        if not skills:
            continue
        for skill in skills:
            d = home / name / "skills" / skill
            d.mkdir(parents=True)
            (d / "SKILL.md").write_text(f"---\nname: {skill}\n---\nbody\n")
    return home


def _scan(tmp_path, tracker, home, env=None, passes=2, **cfg):
    """Sync the fixture host against the given `$HOME`, then read the inventory.

    Two passes by default, and not for thoroughness: `scan_inventory` reconciles
    MCP tools against the `tool_calls` already in the database, and a first run
    indexes the transcripts *after* it has scanned. So the tool a fixture makes
    a call to becomes an installed tool on the *next* sync -- one cycle, exactly
    as it is for a tool a person calls for the first time. Every other assertion
    in this section is about roots, which are read from disk and are visible on
    the first pass; the two passes only exist so the MCP cases can run at all.
    """
    path = tmp_path / "opencode.db"
    conn = make_host(path)
    # One MCP tool part, so the reconciliation has something to reconcile against:
    # the inventory can only report a tool the config and the history agree on.
    add_tool(conn, "prt_mcp", "msg_a", "ses_parent",
             tool="context7_query-docs", state={"input": {"query": "docs"}})
    conn.commit()
    conn.close()
    conf = tmp_path / "cfg"
    make_config(conf, **cfg)
    for _ in range(passes):
        if env:
            with mock.patch.dict(os.environ, env, clear=False):
                sync.run(db_path=tracker, host_db=path, host_config=conf, home=home)
        else:
            sync.run(db_path=tracker, host_db=path, host_config=conf, home=home)
    return {r["name"] for r in rows(tracker, "inventory", "kind='skill'")}


def test_a_skill_in_the_homes_agents_root_is_inventoried(tmp_path, tracker):
    """The bug this row was written for: OpenCode loads `~/.agents/skills`
    unconditionally, ahead of its own config home, and the reader only read the
    config home. Every one of those skills was therefore absent from the
    inventory, so the union had nothing to match them against and printed
    `removed` for skills the model was being offered every single session.
    """
    home = external_home(tmp_path, agents=["grilling", "tdd"])
    names = _scan(tmp_path, tracker, home)
    assert {"grilling", "tdd"} <= names, sorted(names)


def test_a_skill_in_the_homes_claude_root_is_inventoried(tmp_path, tracker):
    home = external_home(tmp_path, claude=["from-claude"])
    assert "from-claude" in _scan(tmp_path, tracker, home)


def test_the_external_roots_are_not_the_config_home_renamed(tmp_path, tracker):
    """A root that does not exist contributes nothing, rather than being read as
    an empty-but-present store that could shadow the real one."""
    home = external_home(tmp_path, agents=["present"])
    (home / ".agents" / "skills" / "present" / "SKILL.md").write_text(
        "---\nname: present\n---\n")
    names = _scan(tmp_path, tracker, tmp_path / "a-home-with-nothing-in-it")
    assert "present" not in names, sorted(names)
    assert {"tdd", "triage"} <= names, sorted(names)   # the config home still read


def test_a_symlinked_skill_directory_is_followed(tmp_path, tracker):
    """`~/.agents/skills` on this machine is nothing but symlinks into a second
    store, and the host follows them: its scanner sets `symlink: true` on every
    one of its four roots. `Path.glob("**")` does not, which is why the
    inventory found exactly nothing there.
    """
    elsewhere = tmp_path / "elsewhere" / "tdd"
    elsewhere.mkdir(parents=True)
    (elsewhere / "SKILL.md").write_text("---\nname: linked-tdd\n---\n")
    home = tmp_path / "home"
    (home / ".agents" / "skills").mkdir(parents=True)
    (home / ".agents" / "skills" / "tdd").symlink_to(elsewhere)
    assert "linked-tdd" in _scan(tmp_path, tracker, home)


def test_a_symlink_that_points_at_its_own_ancestor_does_not_hang(tmp_path, tracker):
    """Following symlinks is a loop hazard, and the loop is one `ln -s` away.
    Python's `os.walk(followlinks=True)` does not break it; the reader's own
    `resolved()`-keyed visited set does.
    """
    home = tmp_path / "home"
    deep = home / ".agents" / "skills" / "loop"
    deep.mkdir(parents=True)
    (deep / "SKILL.md").write_text("---\nname: loop\n---\n")
    (deep / "back").symlink_to(home / ".agents" / "skills")
    names = _scan(tmp_path, tracker, home)
    assert "loop" in names, sorted(names)


def test_a_dot_directory_in_an_external_root_is_a_skill(tmp_path, tracker):
    """The host passes `dot: true` on exactly these two roots and on the project
    ones -- so `.skill/` is a real skill there, and the hidden-prefix rule that
    keeps it out of a plugin's directory is a plugin rule, not a skill rule.
    """
    home = tmp_path / "home"
    d = home / ".agents" / "skills" / ".skill"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: dotted\n---\n")
    assert "dotted" in _scan(tmp_path, tracker, home)


def test_the_source_says_which_of_the_three_roots_a_skill_came_from(tmp_path, tracker):
    """`source` exists so a panel can say where a row was read from, and three
    roots that all read `skills-dir` throw that away. A shared store split across
    two homes is exactly the case where "which one" is the question.
    """
    home = external_home(tmp_path, claude=["from-claude"], agents=["from-agents"])
    _scan(tmp_path, tracker, home)
    got = {r["name"]: r["source"] for r in rows(tracker, "inventory", "kind='skill'")}
    assert got["from-claude"] == "claude-skills-dir", got
    assert got["from-agents"] == "agents-skills-dir", got
    assert got["tdd"] == "skills-dir", got            # the config home, unchanged


def test_the_builtin_skill_is_inventoried_though_it_is_never_on_disk(tmp_path, tracker):
    """`customize-opencode` is the one skill the host serves out of its own
    binary. It was the last Skills row still reading `removed` on a machine where
    nothing had been uninstalled — and unlike the tool and agent registries, this
    one is a single name, so there is no reason a reader cannot name it.
    """
    home = external_home(tmp_path, agents=["from-agents"])
    _scan(tmp_path, tracker, home)
    got = {r["name"]: r["source"]
           for r in rows(tracker, "inventory", "kind='skill'")}
    assert got.get(sync.BUILTIN_SKILL_NAME) == "builtin", got
    assert (tmp_path / ".agents" / "skills" / sync.BUILTIN_SKILL_NAME).exists() is False


def test_disabling_external_skills_removes_both_roots(tmp_path, tracker):
    """`OPENCODE_DISABLE_EXTERNAL_SKILLS` turns off the block that reads both
    roots, so a skill the host will not offer must not be reported installed."""
    home = external_home(tmp_path, claude=["from-claude"], agents=["from-agents"])
    names = _scan(tmp_path, tracker, home,
                  env={sync.DISABLE_EXTERNAL_SKILLS_ENV: "1"})
    assert not {"from-claude", "from-agents"} & names, sorted(names)
    assert {"tdd", "triage"} <= names, "the config home is not an external root"


def test_disabling_claude_code_leaves_the_agents_root_alone(tmp_path, tracker):
    """A narrower flag: it drops `.claude` and keeps `.agents`, which is what the
    host's `if(!X) push(.claude); push(.agents)` does. Both spellings count."""
    home = external_home(tmp_path, claude=["from-claude"], agents=["from-agents"])
    for i, var in enumerate(sync.DISABLE_CLAUDE_SKILLS_ENV):
        # A store per spelling: the reader rebuilds the host fixture each pass,
        # and two runs against one fixture file is a second reader, not a second
        # assertion.
        case = tmp_path / f"claude-off-{i}"
        case.mkdir()
        names = _scan(case, case / "usage.db", home, env={var: "1"})
        assert "from-claude" not in names, var
        assert "from-agents" in names, (var, sorted(names))


def test_a_tool_of_a_configured_server_is_inventoried(tmp_path, tracker):
    """MCP tool ids are `<server>_<tool>`, and the config names the *server*.
    Without this the whole MCP surface fell through to the same `removed` the
    skills did -- 2 of this fixture's tools in every window, both of them real.
    """
    home = tmp_path / "home"
    _scan(tmp_path, tracker, home, mcp={"context7": {}})
    tools = {r["name"] for r in rows(tracker, "inventory", "kind='tool'")}
    # `make_host` writes one `context7_query-docs` part, and the config names the
    # server it belongs to. Split back apart, that is one installed tool.
    assert "context7_query-docs" in tools, sorted(tools)
    owned = {r["owner_plugin"] for r in rows(tracker, "inventory", "kind='tool'")}
    assert owned == {"context7"}, sorted(owned)


def test_a_tool_of_an_unconfigured_server_is_not_inventoried(tmp_path, tracker):
    """Reconciling the other way would be worse than not reconciling: a tool whose
    server is gone from the config must not be reported installed by a name
    match that happens to look right.
    """
    home = tmp_path / "home"
    _scan(tmp_path, tracker, home, mcp={"playwright": {}})
    tools = {r["name"] for r in rows(tracker, "inventory", "kind='tool'")}
    assert "context7_query-docs" not in tools, sorted(tools)


def test_a_skill_name_comes_from_the_frontmatter_not_the_folder(tmp_path, tracker):
    """The folder is a fallback: OpenCode itself reads `name:` out of the file."""
    path = tmp_path / "opencode.db"
    conn = make_host(path)
    conn.commit()
    conn.close()
    cfg = tmp_path / "cfg"
    make_config(cfg)
    d = cfg / "skills" / "group" / "folder-name"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: declared-name\n---\nbody\n")
    sync.run(db_path=tracker, host_db=path, host_config=cfg)
    names = {r["name"] for r in rows(tracker, "inventory", "kind='skill'")}
    assert "declared-name" in names and "folder-name" not in names


def test_an_uninstalled_entity_loses_its_inventory_row(tmp_path, tracker):
    """The inventory is rewritten, not merged: a removed server must disappear."""
    path = tmp_path / "opencode.db"
    conn = make_host(path)
    conn.commit()
    conn.close()
    cfg = tmp_path / "cfg"
    make_config(cfg, mcp={"context7": {}, "gone-tomorrow": {}})
    sync.run(db_path=tracker, host_db=path, host_config=cfg)
    assert len(rows(tracker, "inventory", "kind='mcp'")) == 2
    make_config(cfg, mcp={"context7": {}})
    sync.run(db_path=tracker, host_db=path, host_config=cfg, quiet=True)
    assert [r["name"] for r in rows(tracker, "inventory", "kind='mcp'")] == \
        ["context7"]


# --- the plugin-usage sidecar ----------------------------------------------

def plugin_host(tmp_path, plugins):
    """A host + config, so a sidecar test has somewhere for a plugin to live."""
    path = tmp_path / "opencode.db"
    conn = make_host(path)
    conn.commit()
    conn.close()
    cfg = tmp_path / "cfg"
    make_config(cfg, plugins=plugins)
    return path, cfg


def sidecar(tmp_path, *records):
    """Write the hook's JSONL sidecar. A record is a dict or a raw line."""
    path = tmp_path / "plugin-events.jsonl"
    path.write_text("\n".join(
        r if isinstance(r, str) else json.dumps(r) for r in records) + "\n")
    return path


def test_a_surface_record_becomes_hook_owned_inventory_rows(tmp_path, tracker):
    """A passive scan cannot name a plugin's own tools; the hook reports them.

    A `surface` record is folded into `inventory` as kind='tool'/'command' with the
    plugin as owner — the only place the tracker ever learns those names.
    """
    host, cfg = plugin_host(tmp_path, ["@acme/demo-plugin@1.2.3"])
    events = sidecar(tmp_path, {
        "t": "surface", "plugin": "@acme/demo-plugin@1.2.3",
        "tools": ["compress"], "commands": ["dcp-compress"]})
    sync.run(db_path=tracker, host_db=host, host_config=cfg,
             plugin_events=events, quiet=True)
    owned = rows(tracker, "inventory", "owner_plugin='demo-plugin'")
    assert {(r["kind"], r["name"], r["source"]) for r in owned} == {
        ("tool", "compress", "hook"), ("command", "dcp-compress", "hook")}


def test_a_call_record_feeds_plugin_usage_and_q_plugins(tmp_path, tracker):
    """A `call` record becomes one plugin_usage row, and `q_plugins().uses` counts it."""
    host, cfg = plugin_host(tmp_path, ["@acme/demo-plugin@1.2.3"])
    events = sidecar(tmp_path, {
        "t": "surface", "plugin": "@acme/demo-plugin@1.2.3",
        "tools": ["compress"], "commands": ["dcp-compress"]},
        {"t": "call", "ts": 1_700_000_300_000, "sid": "ses_parent", "cid": "c1",
         "plugin": "@acme/demo-plugin@1.2.3", "kind": "tool", "item": "compress",
         "status": None, "ms": 4},
        {"t": "call", "ts": 1_700_000_400_000, "sid": "ses_parent",
         "cid": "cmd:ses_parent:1700000400000", "plugin": "@acme/demo-plugin@1.2.3",
         "kind": "command", "item": "dcp-compress", "status": None, "ms": None})
    stats = sync.run(db_path=tracker, host_db=host, host_config=cfg,
                     plugin_events=events, quiet=True)
    assert stats["plugin_events"]["calls"] == 2
    assert stats["rows_plugin_usage"] == 2
    assert {(r["kind"], r["item"]) for r in rows(tracker, "plugin_usage")} == {
        ("tool", "compress"), ("command", "dcp-compress")}
    conn = db.open_db(tracker)
    try:
        plug = next(r for r in db.q_plugins(conn) if r["plugin"] == "demo-plugin")
    finally:
        conn.close()
    assert plug["uses"] == 2
    assert plug["commands"] == 1      # the command surface the hook reported


def test_the_hook_name_is_shortened_so_one_plugin_lists_once(tmp_path, tracker):
    """The hook spells `@scope/pkg@1.2.3`; the inventory shortens to `pkg`.

    Without the fold the Plugins tab would list the plugin twice — once with the
    hook's usage and once, empty, from the config.
    """
    host, cfg = plugin_host(tmp_path, ["@acme/demo-plugin@1.2.3"])
    events = sidecar(tmp_path, {
        "t": "call", "ts": 1_700_000_300_000, "sid": "ses_parent", "cid": "c1",
        "plugin": "@acme/demo-plugin@1.2.3", "kind": "tool", "item": "compress",
        "status": None, "ms": 1})
    sync.run(db_path=tracker, host_db=host, host_config=cfg,
             plugin_events=events, quiet=True)
    conn = db.open_db(tracker)
    try:
        names = [r["plugin"] for r in db.q_plugins(conn)]
    finally:
        conn.close()
    assert names.count("demo-plugin") == 1, names


def test_a_surface_survives_the_next_syncs_inventory_rewrite(tmp_path, tracker):
    """scan_inventory DELETEs `inventory` every run; the hook's rows have to return."""
    host, cfg = plugin_host(tmp_path, ["@acme/demo-plugin@1.2.3"])
    events = sidecar(tmp_path, {
        "t": "surface", "plugin": "@acme/demo-plugin@1.2.3",
        "tools": ["compress"], "commands": []})
    sync.run(db_path=tracker, host_db=host, host_config=cfg,
             plugin_events=events, quiet=True)
    assert rows(tracker, "inventory", "source='hook'")
    # A second run reads no new events (the cursor moved), and the rewrite must not
    # have dropped the tool row.
    stats = sync.run(db_path=tracker, host_db=host, host_config=cfg,
                     plugin_events=events, quiet=True)
    assert stats["plugin_events"]["events"] == 0
    assert [r["name"] for r in rows(
        tracker, "inventory", "kind='tool' AND owner_plugin='demo-plugin'")] \
        == ["compress"]


def test_a_missing_sidecar_is_not_an_error(tmp_path, tracker):
    """The hook may never have run; that is an empty plugin list, not a failure."""
    host, cfg = plugin_host(tmp_path, ["@acme/demo-plugin@1.2.3"])
    stats = sync.run(db_path=tracker, host_db=host, host_config=cfg,
                     plugin_events=tmp_path / "absent.jsonl", quiet=True)
    assert stats["ok"] is True
    assert stats["plugin_events"]["events"] == 0
    assert rows(tracker, "plugin_usage") == []


def test_a_truncated_sidecar_is_reread_from_the_start(tmp_path, tracker):
    """The hook caps and rotates its file; a shorter file means "start over"."""
    host, cfg = plugin_host(tmp_path, ["@acme/demo-plugin@1.2.3"])
    events = sidecar(tmp_path, {
        "t": "call", "ts": 1_700_000_300_000, "sid": "ses_parent", "cid": "c1",
        "plugin": "@acme/demo-plugin@1.2.3", "kind": "tool", "item": "compress",
        "status": None, "ms": 1}, {
        "t": "call", "ts": 1_700_000_400_000, "sid": "ses_parent", "cid": "c2",
        "plugin": "@acme/demo-plugin@1.2.3", "kind": "tool", "item": "compress",
        "status": None, "ms": 2})
    sync.run(db_path=tracker, host_db=host, host_config=cfg,
             plugin_events=events, quiet=True)
    assert {r["call_id"] for r in rows(tracker, "plugin_usage")} == {"c1", "c2"}
    # Rotated: a fresh, strictly shorter file. The cursor is past its end, so it
    # resets to 0 and the one record is read — rather than being skipped as "already
    # seen" because the offset happened to land past the new EOF.
    events.write_text(json.dumps({
        "t": "call", "ts": 1_700_000_500_000, "sid": "ses_parent", "cid": "c3",
        "plugin": "@acme/demo-plugin@1.2.3", "kind": "tool", "item": "compress",
        "status": None, "ms": 3}) + "\n")
    stats = sync.run(db_path=tracker, host_db=host, host_config=cfg,
                     plugin_events=events, quiet=True)
    assert stats["plugin_events"]["calls"] == 1
    assert {r["call_id"] for r in rows(tracker, "plugin_usage")} == {"c1", "c2", "c3"}


def test_an_unparseable_sidecar_line_is_counted_not_skipped(tmp_path, tracker):
    host, cfg = plugin_host(tmp_path, ["@acme/demo-plugin@1.2.3"])
    events = sidecar(tmp_path, {
        "t": "call", "ts": 1_700_000_300_000, "sid": "ses_parent", "cid": "c1",
        "plugin": "@acme/demo-plugin@1.2.3", "kind": "tool", "item": "compress",
        "status": None, "ms": 1}, "not json at all")
    stats = sync.run(db_path=tracker, host_db=host, host_config=cfg,
                     plugin_events=events, quiet=True)
    assert stats["unparsed_this_run"] == 1
    assert stats["plugin_events"]["calls"] == 1


# --- the host file itself --------------------------------------------------

def test_a_missing_host_db_is_an_explicit_failure_not_an_empty_success(tmp_path):
    tracker = tmp_path / "usage.db"
    stats = sync.run(db_path=tracker, host_db=tmp_path / "nowhere.db")
    assert stats["ok"] is False
    assert "no OpenCode database" in stats["error"]
    # and no cursor was written to pretend the run happened
    conn = sqlite3.connect(tracker)
    assert conn.execute("SELECT COUNT(*) FROM sync_state").fetchone()[0] == 0
    conn.close()


def test_the_host_database_is_never_opened_for_writing(host, tracker):
    """The claim in this module's docstring, checked rather than asserted.

    ``mode=ro`` is what makes it true, and a connection that is read-only cannot
    be tricked by the code below: the write raises. If the reader ever drops the
    URI option, this test is the thing that notices.
    """
    before = host.read_bytes()
    run(host, tracker)
    assert host.read_bytes() == before
    conn = sync.open_source(host)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("UPDATE part SET data='{}' WHERE id='prt_step1'")
    finally:
        conn.close()


def test_open_source_refuses_to_create_a_host(tmp_path):
    path = tmp_path / "absent" / "opencode.db"
    with pytest.raises(sync.SourceMissing):
        sync.open_source(path)
    assert not path.exists()
    assert not path.parent.exists()


def test_full_rebuild_reproduces_the_same_database(host, tracker):
    run(host, tracker)
    first = rows(tracker, "model_calls")
    stats = run(host, tracker, full=True, quiet=True)
    assert stats["schema"] in ("current", "created", "migrated")
    assert rows(tracker, "model_calls") == first
    # the snapshot the destructive path promises to take actually exists
    assert list((tracker.parent / "backups").glob("usage-*-pre-full-rebuild.db")) \
        or not first     # an empty database is not worth a snapshot, by design


# --- format document -------------------------------------------------------

def test_format_doc_names_every_registered_field():
    """The document is rendered off the constants, so it cannot describe a parser
    that no longer exists — and it must name the fields the reader refuses."""
    doc = sync.format_doc()
    for field in sync.USAGE_LEAF_FIELDS:
        assert f"`{field}`" in doc
    for name in sorted(sync.HANDLED_PART_TYPES | sync.IGNORED_PART_TYPES):
        assert f"`{name}`" in doc
    for value in ("output", "title", "metadata", "error"):
        assert f"`{value}`" in doc
    assert str(sync.MAX_HELD_ROWS) in doc
    assert "mcp_shape_unowned_calls" in doc and "rows_mcp" in doc


def test_format_doc_does_not_claim_a_field_the_registry_dropped():
    """The other direction: a needle the code no longer reads must not survive in
    the document that claims to describe it.

    ``jsonl`` and ``offset`` used to be on this list, because the host's records
    are read by rowid and not by a transcript byte offset. They came back on
    2026-10-13 with the plugin-usage sidecar, which really is an append-only JSONL
    read by byte offset — so they are asserted *present* below instead. The point
    of the list is unchanged: a word the code does not use must not be in the doc.
    """
    doc = sync.format_doc()
    for gone in ("prompt_cache_hit_tokens", "conversation_request_id",
                 "marketplace", "tokenDelta"):
        assert gone not in doc
    assert str(sync.db.PLUGIN_EVENTS) in doc
    assert "byte offset" in doc


# --- registry integrity ----------------------------------------------------

def test_every_registered_tool_field_is_either_read_or_refused():
    """The `state` keys the registry names must match the ones the writer handles.

    A field can leave the registry two ways: it gets read, or it gets explicitly
    refused. A field that simply vanishes from the list is a claim about the host
    that stopped being checked.
    """
    source = (Path(__file__).resolve().parents[1] / "ocut-sync.py").read_text()
    for field in sync.TOOL_STATE_FIELDS:
        assert f'"{field}"' in source or f"'{field}'" in source, field
    for field in sync.TOKEN_FIELDS | sync.CACHE_FIELDS:
        assert f'"{field}"' in source, field
    for field in sync.MESSAGE_MODEL_FIELDS:
        assert f'"{field}"' in source, field


def test_the_status_vocabulary_is_the_hosts_and_not_a_translation():
    """`completed|error|running` are the host's own strings.

    Renaming `error` to `failed` in the writer would break every query that counts
    it and would make the panel disagree with the host. This pins the exact set so
    a normalisation step has to be a decision, not an accident.
    """
    assert sync.STATUS_FINAL == frozenset({"completed", "error"})
    assert sync.STATUS_RUNNING == "running"
    source = (Path(__file__).resolve().parents[1] / "ocut_db.py").read_text()
    assert "status='incomplete'" not in source
    assert "status='pending'" not in source
