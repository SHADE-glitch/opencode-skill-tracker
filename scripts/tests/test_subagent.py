"""Subagent runs: the builtin `task` tool, recorded as an event, never as text.

The tracker measures three streams (skill / MCP / plugin) and deliberately not
builtins, so a subagent that only shells and edits leaves no usage row anywhere —
which reads as "subagents are not being recorded" while the recorder is correct.
This stream answers the narrower question the owner asked: that a subagent ran,
which one, from which parent, and how long it took.

Privacy is the whole design: the host hands `state.input.prompt` (a task written
in natural language, measured at 11 KB), `state.input.description`, `state.title`
and `state.output`, and none of them may be named in the writer.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

import skill_db as db

TASK_CALLS = [
    # (parent session, call id, subagent, status, duration ms)
    ("ses_build", "call_1", "general", "success", 4200),
    ("ses_build", "call_2", "explore", "success", 1100),
    ("ses_build", "call_3", "general", "error", 800),
    ("ses_plan", "call_4", "general", "success", 2000),
]


@pytest.fixture
def subagent_db(tmp_path):
    """A fresh database with the subagent table filled by direct inserts.

    The rows are written here rather than through the plugin because the Python
    side must be testable without a Bun host; the writer itself is covered by
    `__selftest` and by the source guards at the bottom of this file.
    """
    path = str(tmp_path / "subagent.db")
    conn = db.open_db(path, readonly=False)
    db.ensure_schema(conn)
    for parent, call, sub, status, dur in TASK_CALLS:
        agent = "build" if parent == "ses_build" else "plan"
        conn.execute(
            "INSERT INTO subagent_usage (parent_session_id, child_session_id, call_id,"
            " subagent, project_path, trigger_type, status, duration_ms, metadata)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (parent, f"ses_child_{call}", call, sub, "/proj", "event_detected",
             status, dur, json.dumps({"agent": agent, "model": "m/x", "source": "event"})),
        )
    conn.commit()
    yield path, conn
    conn.close()


# ---------------------------------------------------------------------------
# the table
# ---------------------------------------------------------------------------
def test_the_subagent_table_is_created_by_ensure_schema(tmp_path):
    """Both schema copies must produce it: a DB from either side has the table."""
    conn = db.open_db(str(tmp_path / "fresh.db"), readonly=False)
    db.ensure_schema(conn)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "subagent_usage" in names, sorted(names)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(subagent_usage)")]
    assert "subagent" in cols and "child_session_id" in cols, cols
    # The dedup invariant the other tables rely on, in this one's own key.
    ddl = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='subagent_usage'").fetchone()[0]
    assert "UNIQUE (parent_session_id, call_id)" in ddl.replace("\n", " "), ddl
    conn.close()


def test_a_repeated_call_id_does_not_double_count(subagent_db):
    _path, conn = subagent_db
    parent, call, sub, status, dur = TASK_CALLS[0]
    with pytest.raises(db.sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO subagent_usage (parent_session_id, call_id, subagent, status,"
            " trigger_type) VALUES (?,?,?,?,?)",
            (parent, call, sub, status, "tool_call"))
    conn.rollback()
    assert conn.execute("SELECT COUNT(*) FROM subagent_usage").fetchone()[0] == 4


# ---------------------------------------------------------------------------
# aggregation
# ---------------------------------------------------------------------------
def test_agent_rows_gain_a_spawned_count_and_total_stays_the_three_streams(
        tmp_path, seeded_db):
    """`Total` must not silently change meaning.

    The Agents page already promises Total = skill + MCP + plugin. Folding spawns
    into it would make the same column name mean two different things on two
    consecutive days, so spawns get their own column and Total keeps its sum.
    """
    conn = db.open_db(seeded_db, readonly=False)
    db.ensure_schema(conn)
    before = {r["agent"]: r for r in db.agent_usage_rows(conn)}
    conn.execute(
        "INSERT INTO subagent_usage (parent_session_id, call_id, subagent, status,"
        " trigger_type, metadata) VALUES ('s-1', 'c-1', 'general', 'success',"
        " 'tool_call', json_object('agent','build'))")
    conn.commit()
    after = {r["agent"]: r for r in db.agent_usage_rows(conn)}
    assert before["build"]["subagent"] == 0, before["build"]
    assert after["build"]["subagent"] == 1, after["build"]
    for row in after.values():
        assert row["total"] == row["skill"] + row["mcp"] + row["plugin"], row
    conn.close()


def test_subagent_summary_groups_by_name_not_by_session(subagent_db):
    _path, conn = subagent_db
    by_name = {r["subagent"]: r for r in db.subagent_summary_rows(conn)}
    assert set(by_name) == {"general", "explore"}, by_name
    assert by_name["general"]["runs"] == 3, by_name["general"]
    assert by_name["general"]["errors"] == 1, by_name["general"]
    assert by_name["explore"]["runs"] == 1, by_name["explore"]


def test_a_subagent_that_made_no_measured_call_is_still_listed(subagent_db):
    """This is the case the owner could not see.

    A subagent that only used builtins produces no usage row anywhere; the spawn
    record is the only evidence it ran at all.
    """
    _path, conn = subagent_db
    rows = db.subagent_summary_rows(conn)
    names = {r["subagent"] for r in rows}
    assert "general" in names
    general = next(r for r in rows if r["subagent"] == "general")
    assert general["last_used"], general
    assert general["avg_ms"] is not None, general


def test_subagent_summary_is_empty_on_a_db_without_the_table(legacy_db):
    conn = db.open_db(legacy_db)
    assert db.subagent_summary_rows(conn) == []
    assert db.subagent_rows(conn) == []
    conn.close()


def test_subagent_rows_return_only_the_whitelisted_columns(subagent_db):
    """No raw event object crosses the data layer."""
    _path, conn = subagent_db
    rows = db.subagent_rows(conn)
    assert len(rows) == 4, rows
    for r in rows:
        assert set(r) <= {"subagent", "parent_session_id", "child_session_id",
                          "call_id", "status", "duration_ms", "timestamp",
                          "agent", "model"}, set(r)


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------
def test_export_carries_subagent_usage_and_says_so(subagent_db):
    """A new stream is a document change: `schema_version` moves to 6."""
    _path, conn = subagent_db
    doc = db.export_document(conn)
    assert doc["schema_version"] == 6, doc["schema_version"]
    assert len(doc["subagent_usage"]) == 4, doc["subagent_usage"]
    row = doc["subagent_usage"][0]
    assert set(row) <= {"subagent", "parent_session_id", "child_session_id",
                        "call_id", "status", "duration_ms", "timestamp",
                        "project_path", "trigger_type", "metadata"}, set(row)
    # metadata is still allowlisted, the same rule as the other three streams.
    if row.get("metadata"):
        assert set(row["metadata"]) <= set(db.EXPORT_METADATA_KEYS), row["metadata"]


# ---------------------------------------------------------------------------
# the writer's privacy contract, pinned against the plugin source and a real run
# ---------------------------------------------------------------------------
PLUGIN = Path(__file__).resolve().parents[2] / "plugin" / "skill-tracker.js"
BUN = shutil.which("bun")
requires_bun = pytest.mark.skipif(BUN is None, reason="bun is not installed")

# The synthetic task text `__selftest()` puts into every prose field of the fake
# `task` part. If it turns up in the database, the writer read text it must not.
TASK_SENTINEL = "SENTINEL-TASK-TEXT-MUST-NEVER-BE-STORED"


def _function_bodies(text, names):
    out = []
    for name in names:
        start = text.index(f"function {name}")
        depth, i = 0, text.index("{", start)
        while True:
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        out.append(text[start:i + 1])
    return "\n".join(out)


def test_the_subagent_writer_never_names_a_text_field():
    """`task` hands us an 11 KB prompt and a 9 KB report; neither may be read.

    Only identifiers are used: `subagent_type`, the two session ids, `status`,
    `time`, `model`. This scans the writer's own body for the prose field names,
    the way the claude-mem reader is scanned, with comments stripped first — the
    block above the writer is a list of what must NOT be read, and that is worth
    keeping readable.
    """
    text = PLUGIN.read_text(encoding="utf-8")
    code = "\n".join(
        line for line in _function_bodies(
            text, ("recordSubagentRun", "trackSubagentPart", "subagentLabel")
        ).splitlines()
        if not line.lstrip().startswith("//")
    )
    banned = ("prompt", "description", "output", "title", "summary")
    hits = [w for w in banned if re.search(r"\b" + re.escape(w) + r"\b", code)]
    assert not hits, f"prose fields named in the subagent writer: {hits}"
    for needed in ("subagent_type", "sessionId", "parentSessionId"):
        assert needed in code, needed


@requires_bun
def test_selftest_drives_a_task_part_and_no_task_text_reaches_the_database(tmp_path):
    """Run the shipped selftest for real, then read its database from this side.

    A source scan proves the writer does not name a field; this proves the row it
    actually wrote carries no task text — including the `error` string a failed
    subagent produces, which every other stream in this tracker sanitises.
    """
    db_path = str(tmp_path / "selftest-subagent.db")
    skills = tmp_path / "skills"
    skills.mkdir()
    (skills / "demo").mkdir()
    (skills / "demo" / "SKILL.md").write_text("---\nname: demo\n---\nbody\n", encoding="utf-8")
    script = """
    const m = await import(process.env.PLUGIN_PATH);
    const ok = await m.__selftest();
    console.log(ok ? 'SELFTEST_OK' : 'SELFTEST_FAILED');
    process.exit(ok ? 0 : 1);
    """
    env = dict(os.environ,
               OPENCODE_SKILL_TRACKER_DB=db_path,
               OPENCODE_SKILL_TRACKER_SKILLS_DIR=str(skills),
               PLUGIN_PATH=str(PLUGIN))
    r = subprocess.run([BUN, "-e", script], env=env, capture_output=True, text=True,
                       timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "SELFTEST_OK" in r.stdout, r.stdout
    assert "FAIL " not in r.stdout, r.stdout

    # The task row must exist...
    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT subagent, status, duration_ms FROM subagent_usage").fetchall()
    assert rows, "__selftest wrote no subagent row — the synthetic task part is gone"
    conn.close()
    # ...and no file the database touches may hold the text it was fed. WAL means
    # the newest writes live in `-wal`, so grepping only the main file would pass
    # on a database that never checkpointed — which is exactly this one.
    blob = b"".join(p.read_bytes() for p in Path(tmp_path).glob("selftest-subagent.db*"))
    assert TASK_SENTINEL.encode() not in blob, "task text reached the database"

