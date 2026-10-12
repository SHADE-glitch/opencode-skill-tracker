#!/usr/bin/env python3
"""ocut-sync — index OpenCode's own database into the tracker database.

Passive reader: opens ``~/.local/share/opencode/opencode.db`` **read-only** and
copies what it needs into the tracker's own SQLite file. Nothing here ever writes
to OpenCode's directories or its database; the write path is a single
``sqlite3.connect`` of the tracker file.

    python3 scripts/ocut-sync.py            # incremental
    python3 scripts/ocut-sync.py --full     # wipe tracker tables, re-index all
    python3 scripts/ocut-sync.py --quiet    # for the TUI's 30-second worker

Why SQLite and not transcripts: CodeBuddy's tracker tails ``*.jsonl`` files with
byte offsets, because there the transcript *is* the record. OpenCode keeps its
sessions in one SQLite file, so the cursor is a rowid, not an offset — and that
swap is where all the interesting problems come from (see the cursor rules).

Standard library only.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ocut_db as db  # noqa: E402

# --- OpenCode format registry ---------------------------------------------
#
# Everything ocut assumes about OpenCode's persisted data: which tables are read,
# which record types and fields are parsed, which tool names drive classification,
# and which paths the inventory is scanned from. Re-verify this block when
# OpenCode ships a new version; AGENTS.md points here.
#
# Two kinds of entry, on purpose:
#   * Values reached through a call (table names, part types, tool names, config
#     keys, globs) are named constants used at their sites, so a rename is one
#     edit and a half-applied rename cannot hide.
#   * Record *field names* stay inline in the handlers — a parser is easier to
#     trust when the field it grabs is visible — and are mirrored in the
#     ``*_FIELDS`` sets below.
# ``scripts/tests/test_ocut_sync.py`` locks both directions: no field may be read
# without being registered, and no registered field may go unread.
#
# Why this exists at all: a renamed field raises nothing. It reads as NULL, the
# panel empties, ``ocut sync`` prints "done", CI stays green. The ``unparsed``
# counter is the runtime signal; this registry is the surface it is measured
# against.

# The three host tables this reader touches, in the order they are consumed.
# Deliberately not ``event``/``todo``/``workspace``/``share`: they carry no usage.
TABLE_SESSION = "session"
TABLE_MESSAGE = "message"
TABLE_PART = "part"
CURSOR_TABLES = (TABLE_PART,)          # see "cursor rules" below

# Part types. The set is the *measured* census of this host's 92,858 live parts
# (2026-10-12), not the union of everything a doc claims exists:
#   tool 24665 · step-start 20919 · step-finish 20627 · reasoning 15451
#   text 8768 · patch 2375 · file 33 · compaction 14 · agent 6
# ``HANDLED`` types produce rows; ``IGNORED`` types are known and deliberately
# produce none (they hold prompt text, reasoning text, or a step-start with no
# numbers in it). Anything outside both sets is drift and is counted per type.
PART_TYPE_FIELD = "type"
HANDLED_PART_TYPES = frozenset({"tool", "step-finish"})
IGNORED_PART_TYPES = frozenset(
    {"text", "reasoning", "step-start", "patch", "file", "compaction", "agent"})

# ``part.data`` shape. Only these keys are read from a tool part:
#   {"tool": "bash", "callID": "call_...",
#    "state": {"status": "completed", "input": {...}, "time": {"start":…, "end":…},
#              "output": …, "title": …, "metadata": …, "error": …}}
# ``output``, ``title``, ``metadata`` and ``error`` are in the list because the
# registry has to name what it refuses: they hold the command line, the tool's
# answer and the error text — prose that never enters this database.
TOOL_NAME_FIELD = "tool"
TOOL_STATE_FIELDS = frozenset(
    {"status", "input", "time", "output", "title", "metadata", "error"})
TOOL_TIME_FIELDS = frozenset({"start", "end"})
# The two argument names this project is allowed to look at. Every other tool
# argument is a file path, a shell command or a prompt.
SKILL_ARG_FIELDS = frozenset({"name"})
TASK_ARG_FIELDS = frozenset({"subagent_type"})
SKILL_TOOL = "skill"
TASK_TOOL = "task"
# Measured on this host: 5916 completed / 82 error / 2 running out of 6000 recent
# tool parts. ``running`` is the only state that changes after the row is
# inserted, and it is what the cursor holdback is for.
STATUS_FINAL = frozenset({"completed", "error"})
STATUS_RUNNING = "running"

# ``step-finish`` is the host's own per-step usage record:
#   {"reason": "tool-calls", "cost": 0,
#    "tokens": {"total": 118488, "input": 195, "output": 404, "reasoning": 1,
#               "cache": {"read": 117888, "write": 0}}}
# The step carries NO model: model/provider/variant come from its parent message.
STEP_FIELDS = frozenset({"reason", "cost", "tokens"})
TOKEN_FIELDS = frozenset({"total", "input", "output", "reasoning", "cache"})
CACHE_FIELDS = frozenset({"read", "write"})
# The leaf names a usage row is checked against, in the flat form the missing list
# stores them in. TOKEN_FIELDS | CACHE_FIELDS is NOT this set: "cache" is a dict,
# and "read"/"write" are not keys of "tokens" -- checking those would report every
# single row as missing (measured: 20,627 of 20,627) and empty the Tokens panel.
USAGE_LEAF_FIELDS = ("input", "output", "reasoning", "total", "cache.read",
                     "cache.write")
# A row counts as carrying usage when the host gave a step at all and named at
# least one of the two directions. Anything narrower turns "the provider did not
# report a reasoning count" into "this response cost nothing".
USAGE_PRESENT_FIELDS = ("input", "output")
# Proven: over all 20,627 step-finish rows on this host, ``total`` equals
# input+output+reasoning+cache.read+cache.write in every row that carries a
# total. So the host total is preferred and the sum is only a fallback for the 3
# rows measured without one; the fallback requires all five parts to be present
# rather than assuming a missing one is zero.

# ``message.data`` — read for one purpose: the model behind a step.
#   assistant: {"role","agent","mode","modelID","providerID","variant","path",
#               "cost","tokens","time":{"created","completed"},"parentID","finish","error"}
#   user:      {"role","time","agent","model":{"providerID","modelID"},"summary"}
# Of these, only the four below are ever read; ``path``, ``summary`` and
# ``error.data.message`` are prose or absolute paths and stay in the host.
MESSAGE_MODEL_FIELDS = frozenset({"modelID", "providerID", "variant", "agent"})

# ``session`` columns: id, project_id, directory, agent, model, parent_id, cost,
# tokens_*, version, time_created, time_updated. ``title`` and ``slug`` exist and
# are NEVER selected — OpenCode generates both from the user's first message, so
# storing either would store the message.
SESSION_COLUMNS = (
    "id", "project_id", "directory", "agent", "model", "parent_id",
    "cost", "tokens_input", "tokens_output", "tokens_reasoning",
    "tokens_cache_read", "tokens_cache_write", "version",
    "time_created", "time_updated",
)
SESSION_MODEL_FIELDS = frozenset({"id", "providerID", "variant"})
# Rows whose `metadata`/`revert`/`permission`/`share_url`/`summary_*`/`time_*`
# columns are not in SESSION_COLUMNS are not read at all; that is the whitelist,
# not a filter.

# An MCP tool is reported as ``<server>_<tool>`` with ONE underscore (measured:
# `context7_query-docs`, `basic-memory_search_notes`), resolved against the
# servers the host's own config lists, longest prefix first so a server name that
# contains an underscore cannot be split in the wrong place. A name no configured
# server owns is not guessed at.
MCP_TOOL_SEP = "_"
# Measured on this host, and the reason there is NO per-name counter here:
# `list_mcp_resources` and `list_mcp_resource_templates` are OpenCode's own
# builtin tools and their names contain the separator, while `playwright_*` (888
# calls) is a server somebody configured and later removed. Nothing in the host's
# rows distinguishes the two — no name table is shipped, and no event records a
# server being added. A per-name alarm would therefore ring on every builtin
# forever, and an alarm that always rings is not an alarm. So nothing is counted
# here at all: `run()` reports how many server-shaped calls have no owner, beside
# how many did, and the interface shows both numbers. A person can see whether
# that number is 2 builtin tools or 888 calls from a removed server; the code
# cannot tell, and pretending otherwise would be a claim about the host.
MCP_SHAPE_QUERY = ("SELECT COUNT(*) FROM tool_calls WHERE category = 'builtin'"
                   " AND tool_name LIKE '%!_%' ESCAPE '!'")

# Inventory: the config home is what OpenCode loads, so it is what is installed.
CONFIG_FILE = "opencode.json"
CONFIG_JSONC = "opencode.jsonc"
DIR_SKILLS = "skills"
DIR_AGENTS = "agents"
DIR_COMMAND = "command"
SKILL_MANIFEST = "SKILL.md"
AGENT_SPEC_GLOB = "*.md"
COMMAND_SPEC_GLOB = "*.md"
# Config keys read for the inventory. `lsp`, `permission` and `$schema` describe
# behaviour, not entities, and are not inventoried.
CONFIG_KEYS = frozenset({"mcp", "plugin", "provider", "command"})
PLUGIN_VERSION_SEP = "@"
FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---", re.S)
FRONTMATTER_FIELDS = frozenset({"name", "version"})

# Cursor rules — the reason this is not just "SELECT * WHERE rowid > cursor".
#
# 1. A rowid cursor cannot see a row that CHANGES after it was consumed. Exactly
#    one thing here does: a tool part in ``running`` state, which the host UPDATEs
#    in place when the call finishes. So the cursor is held back at the first
#    non-final part in a batch and the next run re-reads it; the re-read writes
#    with OR REPLACE, so the final status wins. Holding is capped: a part that
#    never finishes (an interrupted session) would pin the cursor forever, so
#    past ``MAX_HELD_ROWS`` rows it is recorded, written as it stands, and passed.
# 2. A rowid cursor cannot see a row that was DELETED. Deletions are routine here
#    (max rowid 147,418 over 92,858 live parts). Rows we already indexed for a
#    pruned transcript stay in the tracker on purpose — it is an index of history,
#    and the alternative would silently forget sessions that happened. The count
#    drop is recorded in ``sync_state.note`` so it is visible, never inferred.
# 3. A rowid cursor is meaningless if the table was rebuilt under it. If the
#    host's ``max(rowid)`` is now *below* our cursor, rowids have been renumbered:
#    the cursor is reset and everything is re-read (idempotent, because every row
#    is keyed by the host's own id).
# 4. ``session`` is not cursor-read at all. Its cost/token columns are a running
#    rollup the host rewrites for the life of the session, and 323 rows is nothing
#    — the whole table is re-read and upserted every run. That is the honest
#    answer to a mutable table, and it is why sessions and model_calls can be
#    reconciled against each other at any moment.
MAX_HELD_ROWS = 5000
PART_BATCH_ROWS = 2000
MESSAGE_LOOKUP_CHUNK = 400

# ``sync_state.name`` values.
CURSOR_PART = "part"
STATE_SESSIONS = "session"

UNPARSEABLE_DATA = "unparseable_part_data"
UNPARSEABLE_MESSAGE = "unparseable_message_data"
NO_TOKENS = "step_finish_without_tokens"
NO_TOOL_NAME = "tool_part_without_name"
NO_SKILL_NAME = "skill_call_without_name"
NO_TASK_TYPE = "task_call_without_subagent_type"
MESSAGE_ROW_ABSENT = "step_finish_message_row_absent"
UNKNOWN_PART_TYPE = "unknown_part_type"
HELD_GAVE_UP = "part_left_running"


# --- reading ---------------------------------------------------------------

class SourceMissing(Exception):
    """OpenCode's own database is not there (or cannot be opened read-only)."""


def open_source(host_db=None) -> sqlite3.Connection:
    """Open the host database strictly read-only.

    ``mode=ro`` is not a comment about intent: without it any bug in this file
    could rewrite OpenCode's store, and the file is 3.5 GB of somebody's actual
    sessions. A missing file raises rather than creating an empty one, which is
    what ``mode=ro`` guarantees and why the path is not run through the
    tracker's own ``open_db``.
    """
    path = Path(host_db or db.HOST_DB)
    if not path.is_file():
        raise SourceMissing(f"no OpenCode database at {path}")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
    conn.execute("PRAGMA query_only = ON")
    conn.row_factory = sqlite3.Row
    return conn


def host_stats(src, table: str) -> tuple[int, int]:
    """``(count(*), max(rowid))`` for a host table, as of right now."""
    row = src.execute(f"SELECT COUNT(*) AS n, MAX(rowid) AS m FROM {table}").fetchone()
    return int(row["n"] or 0), int(row["m"] or 0)


def cursor_of(conn, name: str) -> int:
    row = conn.execute(
        "SELECT cursor FROM sync_state WHERE name=?", (name,)).fetchone()
    return int(row[0]) if row else 0


def remember_state(conn, name: str, cursor: int, host_count: int,
                   host_max: int, note: str = "") -> None:
    conn.execute(
        "INSERT INTO sync_state(name, cursor, host_count, host_max, note)"
        " VALUES(?,?,?,?,?)"
        " ON CONFLICT(name) DO UPDATE SET cursor=excluded.cursor,"
        " host_count=excluded.host_count, host_max=excluded.host_max,"
        " note=excluded.note",
        (name, cursor, host_count, host_max, note),
    )


class Counts:
    """Per-run tally, keyed by the reason string so it can be reported verbatim."""

    def __init__(self):
        self.parts = 0
        self.calls = 0
        self.steps = 0
        self.skills = 0
        self.agents = 0
        self.mcp = 0
        self.sessions = 0
        self.unparsed = {}

    def bump(self, reason: str, n: int = 1) -> None:
        self.unparsed[reason] = self.unparsed.get(reason, 0) + n

    def flush(self, conn) -> int:
        total = 0
        for reason, n in self.unparsed.items():
            db.note_unparsed(conn, reason, n)
            total += n
        self.unparsed.clear()
        return total


# --- classification --------------------------------------------------------

def mcp_servers(host_config=None) -> tuple[str, ...]:
    """The MCP server names the host's own config lists, longest first.

    Read from ``opencode.json`` rather than inferred from an underscore: a tool
    name is the only evidence available to a passive reader, and a server name
    containing no underscore is indistinguishable from a builtin tool whose name
    happens to contain one.
    """
    cfg = load_config_json(host_config)
    mcp = cfg.get("mcp") if isinstance(cfg, dict) else None
    if not isinstance(mcp, dict):
        return ()
    return tuple(sorted((str(k) for k in mcp), key=len, reverse=True))


def split_mcp(name: str, servers: tuple[str, ...]):
    """``(server, tool)`` when a configured server owns the name, else ``None``.

    Longest prefix wins, so a server named ``basic-memory`` claims
    ``basic-memory_search_notes`` and a name that merely starts with ``basic``
    cannot be half-claimed.
    """
    for server in servers:
        prefix = server + MCP_TOOL_SEP
        if name.startswith(prefix) and len(name) > len(prefix):
            return server, name[len(prefix):]
    return None


def classify(name: str, servers: tuple[str, ...]) -> tuple[str, str | None, str | None]:
    """``(category, mcp_server, mcp_tool)`` for one tool name.

    ``category`` is what the Tools tab groups by. ``skill`` and ``agent`` are the
    two *tool names* that carry a specialised table; the rest are builtins unless
    a configured MCP server owns the name.
    """
    if name == SKILL_TOOL:
        return "skill", None, None
    if name == TASK_TOOL:
        return "agent", None, None
    owned = split_mcp(name, servers)
    if owned:
        return "mcp", owned[0], owned[1]
    return "builtin", None, None


# --- writers ---------------------------------------------------------------

def write_model_call(conn, part, data: dict, ctx: dict | None, counts: Counts) -> None:
    """One ``step-finish`` part -> one row of ``model_calls``.

    ``ctx`` is the parent message's model context, or ``None`` when the message
    row is gone (pruned, or inserted after this part was consumed). The usage
    numbers are still the host's own and still get written — losing a token count
    because a label is missing would be the worse error — with the reason counted.
    """
    tokens = data.get("tokens")
    if not isinstance(tokens, dict):
        counts.bump(NO_TOKENS)
        return
    cache = tokens.get("cache")
    cache = cache if isinstance(cache, dict) else {}
    missing = [f for f in USAGE_LEAF_FIELDS
               if _token_value(tokens, cache, f) is None]
    present = any(_token_value(tokens, cache, f) is not None
                  for f in USAGE_PRESENT_FIELDS)
    conn.execute(
        "INSERT OR REPLACE INTO model_calls(call_id, message_id, session_id, project,"
        " directory, agent, model, provider, variant, prompt_tokens,"
        " completion_tokens, reasoning_tokens, cache_read, cache_write,"
        " total_tokens, cost, ts, finish, usage_available, missing)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            part["id"], part["message_id"], part["session_id"],
            (ctx or {}).get("project"), (ctx or {}).get("directory"),
            (ctx or {}).get("agent"),
            (ctx or {}).get("model"), (ctx or {}).get("provider"),
            (ctx or {}).get("variant"),
            _token_value(tokens, cache, "input"),
            _token_value(tokens, cache, "output"),
            _token_value(tokens, cache, "reasoning"),
            _token_value(tokens, cache, "cache.read"),
            _token_value(tokens, cache, "cache.write"),
            _token_value(tokens, cache, "total"),
            data.get("cost") if isinstance(data.get("cost"), (int, float)) else None,
            part["time_created"], data.get("reason"),
            1 if present else 0,
            json.dumps(missing) if missing else None,
        ),
    )
    counts.steps += 1


def _token_value(tokens: dict, cache: dict, field: str):
    """One usage number, or None — never a 0 standing in for an absent field."""
    if field.startswith("cache."):
        value = cache.get(field.split(".", 1)[1])
    else:
        value = tokens.get(field)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def write_tool_call(conn, part, data: dict, ctx: dict | None,
                    servers: tuple[str, ...], counts: Counts) -> bool:
    """One ``tool`` part -> ``tool_calls`` plus its specialised table.

    Returns False when the part is not final and the cursor must not pass it.
    """
    name = data.get(TOOL_NAME_FIELD)
    if not isinstance(name, str) or not name:
        counts.bump(NO_TOOL_NAME)
        return True
    state = data.get("state")
    state = state if isinstance(state, dict) else {}
    status = state.get("status")
    status = status if isinstance(status, str) else None
    when = state.get("time")
    when = when if isinstance(when, dict) else {}
    start, end = when.get("start"), when.get("end")
    duration = (end - start) if isinstance(start, int) and isinstance(end, int) else None
    category, server, mcp_tool = classify(name, servers)
    args = state.get("input")
    args = args if isinstance(args, dict) else {}
    session_id = part["session_id"]
    project = (ctx or {}).get("project")
    directory = (ctx or {}).get("directory")
    ts = part["time_created"]
    conn.execute(
        "INSERT OR REPLACE INTO tool_calls(call_id, session_id, project, directory,"
        " tool_name, category, status, ts, duration_ms, model, provider)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (part["id"], session_id, project, directory, name, category, status, ts,
         duration, (ctx or {}).get("model"), (ctx or {}).get("provider")),
    )
    counts.calls += 1
    if category == "skill":
        skill = args.get("name")
        if isinstance(skill, str) and skill:
            conn.execute(
                "INSERT OR REPLACE INTO skill_usage(call_id, skill, session_id,"
                " project, ts, status, duration_ms) VALUES(?,?,?,?,?,?,?)",
                (part["id"], skill, session_id, project, ts, status, duration))
            counts.skills += 1
        else:
            counts.bump(NO_SKILL_NAME)
    elif category == "agent":
        agent_type = args.get("subagent_type")
        if isinstance(agent_type, str) and agent_type:
            meta = state.get("metadata")
            child = meta.get("sessionId") if isinstance(meta, dict) else None
            conn.execute(
                "INSERT OR REPLACE INTO agent_usage(call_id, agent_type, session_id,"
                " child_session_id, project, ts, status, duration_ms)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (part["id"], agent_type, session_id,
                 child if isinstance(child, str) else None,
                 project, ts, status, duration))
            counts.agents += 1
        else:
            counts.bump(NO_TASK_TYPE)
    elif category == "mcp":
        conn.execute(
            "INSERT OR REPLACE INTO mcp_usage(call_id, server, tool, session_id,"
            " project, ts, status, duration_ms) VALUES(?,?,?,?,?,?,?,?)",
            (part["id"], server, mcp_tool, session_id, project, ts, status, duration))
        counts.mcp += 1
    return status in STATUS_FINAL or status is None


# --- message context -------------------------------------------------------

def message_context(src, message_ids: set[str], counts: Counts) -> dict[str, dict]:
    """``message id -> {model, provider, variant, agent}`` for a batch of ids.

    Resolved with chunked key lookups, not a per-part query: a cold backfill reads
    20,627 steps, and one indexed ``IN`` query per chunk is what keeps the whole
    pass at a few seconds. Only the four registered fields are pulled out of the
    JSON; the rest of ``message.data`` is never copied anywhere.
    """
    out: dict[str, dict] = {}
    ids = sorted(message_ids)
    for i in range(0, len(ids), MESSAGE_LOOKUP_CHUNK):
        chunk = ids[i:i + MESSAGE_LOOKUP_CHUNK]
        marks = ",".join("?" * len(chunk))
        rows = src.execute(
            f"SELECT m.id, m.session_id, s.project_id, s.directory,"
            f" m.data, s.model FROM message m"
            f" LEFT JOIN session s ON s.id = m.session_id"
            f" WHERE m.id IN ({marks})", chunk).fetchall()
        for row in rows:
            try:
                data = json.loads(row["data"])
            except (TypeError, ValueError):
                counts.bump(UNPARSEABLE_MESSAGE)
                continue
            if not isinstance(data, dict) or data.get("role") != "assistant":
                # A step's parent is always the assistant message; a user message
                # never owns one, so it has no model to lend.
                continue
            out[row["id"]] = {
                "model": data.get("modelID"),
                "provider": data.get("providerID"),
                "variant": data.get("variant"),
                "agent": data.get("agent"),
                "project": row["project_id"],
                "directory": row["directory"],
            }
    return out


# --- sessions --------------------------------------------------------------

def index_sessions(conn, src, counts: Counts) -> None:
    """Re-read the host's ``session`` table in full and upsert it.

    Not a cursor read: see cursor rule 4. The projection names every column so a
    host column that starts carrying text cannot be picked up by a ``SELECT *``.
    """
    rows = src.execute(
        "SELECT " + ", ".join(SESSION_COLUMNS) + " FROM " + TABLE_SESSION).fetchall()
    host_count, host_max = host_stats(src, TABLE_SESSION)
    for row in rows:
        model = _session_model(row["model"])
        parent = row["parent_id"]
        conn.execute(
            "INSERT OR REPLACE INTO sessions(session_id, project, directory, agent,"
            " model, provider, parent_id, is_subagent, started_at, ended_at, cost,"
            " tokens_input, tokens_output, tokens_reasoning, tokens_cache_read,"
            " tokens_cache_write, host_version) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (row["id"], row["project_id"], row["directory"], row["agent"],
             model.get("id"), model.get("providerID"), parent,
             1 if parent else 0, row["time_created"], row["time_updated"],
             _number_or_none(row["cost"]), _int_or_none(row["tokens_input"]),
             _int_or_none(row["tokens_output"]), _int_or_none(row["tokens_reasoning"]),
             _int_or_none(row["tokens_cache_read"]),
             _int_or_none(row["tokens_cache_write"]), row["version"]),
        )
        counts.sessions += 1
    previous = conn.execute(
        "SELECT host_count FROM sync_state WHERE name=?", (STATE_SESSIONS,)).fetchone()
    note = ""
    if previous and previous[0] is not None and host_count < previous[0]:
        note = f"host sessions pruned: {previous[0]} -> {host_count}"
    remember_state(conn, STATE_SESSIONS, host_max, host_count, host_max,
                   note or "full reload each run (host rewrites these rows)")


def _session_model(raw) -> dict:
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _number_or_none(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _int_or_none(value):
    value = _number_or_none(value)
    return None if value is None else int(value)


# --- parts -----------------------------------------------------------------

def index_parts(conn, src, counts: Counts, servers: tuple[str, ...]) -> None:
    """Append-read ``part`` by rowid, with the holdback for mutating rows.

    The cursor is written only after every batch has been read. If any tool part
    in the run was still non-final, the cursor stops at the *earliest* such row and
    the next run re-reads from there, so the finished status replaces the running
    one; re-reading is cheap and correct because every write is keyed by the host's
    own id. A part that never finishes (an interrupted session) would pin the
    cursor forever, so a hold spanning more than ``MAX_HELD_ROWS`` rows is recorded,
    written as it stands, and passed. The cap measures the distance to the END of
    the table, not to where this run started: a row near the tail is a call that is
    still in flight and will finish before the next poll, while a row from a session
    interrupted months ago would otherwise pin the cursor and re-read everything.
    """
    cursor = cursor_of(conn, CURSOR_PART)
    host_count, host_max = host_stats(src, TABLE_PART)
    note = ""
    if cursor and host_max < cursor:
        note = f"host parts table renumbered (max rowid {host_max} < cursor {cursor})"
        cursor = 0
    previous = conn.execute(
        "SELECT host_count FROM sync_state WHERE name=?", (CURSOR_PART,)).fetchone()
    if previous and previous[0] is not None and host_count < previous[0]:
        note = (note + "; " if note else "") + \
            f"host parts pruned: {previous[0]} -> {host_count} (tracker rows kept)"

    hold_at = None
    while True:
        rows = src.execute(
            "SELECT rowid, id, message_id, session_id, time_created, data"
            f" FROM {TABLE_PART} WHERE rowid > ? ORDER BY rowid LIMIT ?",
            (cursor, PART_BATCH_ROWS)).fetchall()
        if not rows:
            break
        parsed = []
        for row in rows:
            try:
                data = json.loads(row["data"])
            except (TypeError, ValueError):
                data = None
            if not isinstance(data, dict):
                counts.bump(UNPARSEABLE_DATA)
                continue
            parsed.append((row, data))

        need = {row["message_id"] for row, data in parsed
                if data.get(PART_TYPE_FIELD) in HANDLED_PART_TYPES and row["message_id"]}
        contexts = message_context(src, need, counts) if need else {}

        for row, data in parsed:
            ptype = data.get(PART_TYPE_FIELD)
            if ptype in HANDLED_PART_TYPES:
                ctx = contexts.get(row["message_id"])
                if ptype == "step-finish" and ctx is None:
                    counts.bump(MESSAGE_ROW_ABSENT)
                if ptype == "tool":
                    if not write_tool_call(conn, row, data, ctx, servers, counts):
                        hold_at = row["rowid"] if hold_at is None \
                            else min(hold_at, row["rowid"])
                else:
                    write_model_call(conn, row, data, ctx, counts)
            elif ptype not in IGNORED_PART_TYPES:
                counts.bump(f"{UNKNOWN_PART_TYPE}:{ptype}")
            counts.parts += 1

        cursor = rows[-1]["rowid"]
        conn.commit()

    if hold_at is not None and host_max - hold_at > MAX_HELD_ROWS:
        counts.bump(HELD_GAVE_UP)
        hold_at = None
    if hold_at is not None:
        cursor = hold_at - 1
        note = (note + "; " if note else "") + \
            f"{STATUS_RUNNING} part held at rowid {hold_at} for the next run"
    remember_state(conn, CURSOR_PART, cursor, host_count, host_max, note)


# --- inventory -------------------------------------------------------------

def config_dir() -> Path:
    return Path(db.HOST_CONFIG_DIR)


def load_config_json(host_config=None) -> dict:
    """The host's own ``opencode.json``, or ``{}``.

    Read per run and never cached on disk: the inventory is a claim about what is
    installed *now*, and a stale copy would list a server somebody removed.
    """
    root = Path(host_config or config_dir())
    for name in (CONFIG_FILE, CONFIG_JSONC):
        path = root / name
        if path.is_file():
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                return {}
            stripped = re.sub(r"^\s*//.*$", "", text, flags=re.M)
            try:
                data = json.loads(stripped)
            except ValueError:
                return {}
            return data if isinstance(data, dict) else {}
    return {}


def _frontmatter(text: str) -> dict:
    match = FRONTMATTER_RE.match(text)
    if not match:
        return {}
    out = {}
    for line in match.group(1).splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        if key in FRONTMATTER_FIELDS:
            out[key] = value.strip().strip("\"'")
    return out


def scan_inventory(conn, host_config=None) -> dict:
    """What is installed, as a set of ``inventory`` rows.

    Deleted first and rewritten whole: an entry that was uninstalled must not keep
    a row, and unlike a usage table an inventory row has no history to protect.
    ``source`` names where the row came from, so a panel can show whether a thing
    was read from a directory or from a config key.
    """
    root = Path(host_config or config_dir())
    rows: list[tuple] = []

    for path in sorted(root.glob(f"{DIR_SKILLS}/**/{SKILL_MANIFEST}")):
        text = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
        fm = _frontmatter(text)
        name = fm.get("name") or path.parent.name
        rows.append(("skill", name, "", fm.get("version"), str(path), "skills-dir"))

    for path in sorted(root.glob(f"{DIR_AGENTS}/{AGENT_SPEC_GLOB}")):
        if path.name.startswith(".") or path.suffix != ".md":
            continue
        rows.append(("agent", path.stem, "", None, str(path), "agents-dir"))

    for path in sorted(root.glob(f"{DIR_COMMAND}/{COMMAND_SPEC_GLOB}")):
        if path.name.startswith(".") or path.suffix != ".md":
            continue
        rows.append(("command", path.stem, "", None, str(path), "command-dir"))

    cfg = load_config_json(root)
    for key in ("mcp", "plugin", "provider", "command"):
        value = cfg.get(key)
        if key == "mcp" and isinstance(value, dict):
            for name in value:
                rows.append(("mcp", str(name), "", None, str(root / CONFIG_FILE),
                             "config"))
        elif key == "plugin" and isinstance(value, list):
            for entry in value:
                name, version = _plugin_name_version(entry)
                if name:
                    rows.append(("plugin", name, "", version, str(entry), "config"))
        elif key == "provider" and isinstance(value, dict):
            for name in value:
                rows.append(("provider", str(name), "", None,
                             str(root / CONFIG_FILE), "config"))
        elif key == "command" and isinstance(value, dict):
            for name, spec in value.items():
                version = spec.get("version") if isinstance(spec, dict) else None
                rows.append(("command", str(name), "", version,
                             str(root / CONFIG_FILE), "config"))

    conn.execute("DELETE FROM inventory")
    for row in rows:
        conn.execute(
            "INSERT OR REPLACE INTO inventory(kind, name, owner_plugin, version,"
            " path, source) VALUES(?,?,?,?,?,?)", row)
    out: dict[str, int] = {}
    for row in rows:
        out[row[0]] = out.get(row[0], 0) + 1
    return out


def _plugin_name_version(entry):
    """``"@scope/pkg@1.2.3"`` -> ``("pkg", "1.2.3")``; a local path keeps no version.

    Split from the right, because an npm package name may itself contain an ``@``
    (the scope) but only one version separator.
    """
    text = str(entry)
    if text.endswith(".js") or "/" in text and not text.startswith("@"):
        return Path(text).stem, None
    head, sep, tail = text.rpartition(PLUGIN_VERSION_SEP)
    if sep and head and not head.endswith("/"):
        return head.split("/")[-1], tail
    return text, None


# --- format document -------------------------------------------------------

def format_doc() -> str:
    """Render the format registry as markdown (``ocut format``).

    Every value is read back out of the constants rather than retyped, so the
    checked-in document cannot describe a surface the code no longer reads.
    """
    def row(title, values):
        items = ", ".join(f"`{v}`" for v in sorted(values))
        return f"- **{title}**: {items}\n"

    return "".join([
        f"- **Host database**: `{db.HOST_DB}` (opened `mode=ro`, never written)\n",
        f"- **Host tables read**: `{TABLE_SESSION}`, `{TABLE_MESSAGE}`, "
        f"`{TABLE_PART}`\n",
        row("Part types that produce rows", HANDLED_PART_TYPES),
        row("Part types known and deliberately not recorded", IGNORED_PART_TYPES),
        f"- **Tool part fields**: `{TOOL_NAME_FIELD}` + state "
        f"{', '.join(f'`{f}`' for f in sorted(TOOL_STATE_FIELDS))}, of which "
        "`output`, `title`, `metadata` and `error` are never read\n",
        f"- **Tool state times**: "
        f"{', '.join(f'`{f}`' for f in sorted(TOOL_TIME_FIELDS))}\n",
        f"- **Final tool statuses**: "
        f"{', '.join(f'`{s}`' for s in sorted(STATUS_FINAL))} "
        f"(non-final `{STATUS_RUNNING}` holds the cursor)\n",
        row("Tool argument names read", SKILL_ARG_FIELDS | TASK_ARG_FIELDS),
        f"- **Specialising tool names**: `{SKILL_TOOL}` -> skill_usage, "
        f"`{TASK_TOOL}` -> agent_usage\n",
        f"- **MCP name shape**: `<server>{MCP_TOOL_SEP}<tool>`, resolved against "
        f"the `{CONFIG_FILE}` `mcp` keys, longest prefix first. A name no "
        f"configured server owns stays `builtin` and is NOT counted as drift "
        f"(`list_mcp_resources` is a builtin with exactly that shape). `run() "
        f"reports the two numbers instead: `mcp_shape_unowned_calls` and "
        f"`rows_mcp`\n",
        row("step-finish fields", STEP_FIELDS),
        row("step-finish tokens fields", TOKEN_FIELDS),
        row("step-finish cache fields", CACHE_FIELDS),
        row("usage leaf fields checked", USAGE_LEAF_FIELDS),
        row("fields that make a row carry usage", USAGE_PRESENT_FIELDS),
        row("Message fields read", MESSAGE_MODEL_FIELDS),
        f"- **Session columns selected** (a whitelist, not a filter): "
        f"{', '.join(f'`{c}`' for c in SESSION_COLUMNS)}\n",
        row("session.model fields", SESSION_MODEL_FIELDS),
        f"- **Inventory sources**: `{DIR_SKILLS}/**/{SKILL_MANIFEST}` frontmatter "
        f"{', '.join(f'`{f}`' for f in sorted(FRONTMATTER_FIELDS))}, "
        f"`{DIR_AGENTS}/{AGENT_SPEC_GLOB}`, `{DIR_COMMAND}/{COMMAND_SPEC_GLOB}`, "
        f"and the `{CONFIG_FILE}` keys "
        f"{', '.join(f'`{k}`' for k in sorted(CONFIG_KEYS))}\n",
        f"- **Cursor rules**: `{CURSOR_TABLES[0]}` by rowid with holdback at the "
        f"first non-final tool part (cap `{MAX_HELD_ROWS}` rows), batch "
        f"`{PART_BATCH_ROWS}` rows, message lookups in chunks of "
        f"`{MESSAGE_LOOKUP_CHUNK}`; `{TABLE_SESSION}` re-read in full each run "
        f"(the host rewrites its rollup)\n",
        "- **Unparsed reasons counted**: "
        + ", ".join(f"`{r}`" for r in (
            UNPARSEABLE_DATA, UNPARSEABLE_MESSAGE, NO_TOKENS, NO_TOOL_NAME,
            NO_SKILL_NAME, NO_TASK_TYPE, MESSAGE_ROW_ABSENT, UNKNOWN_PART_TYPE,
            HELD_GAVE_UP))
        + f", and per name for `{UNKNOWN_PART_TYPE}:<type>`\n",
    ])


# --- entry points ----------------------------------------------------------

def run(full: bool = False, quiet: bool = False, db_path=db.DB_PATH,
        host_db=None, host_config=None) -> dict:
    """Index the host database into the tracker. Never raises for a missing host.

    Returns a status dict. A missing or unreadable ``opencode.db`` is reported in
    ``error`` with ``ok=False`` rather than as an empty successful run: an empty
    database and a database that could not be opened look identical on every panel
    and only one of them is a fact about the user's usage.

    ``host_config`` overrides the config home that is inventoried. Tests must pass
    a fixture directory: without it they would read the real ``~/.config/opencode``
    and a machine without that tree would run a different test.
    """
    conn = db.open_db(db_path)
    try:
        status = db.ensure_schema(conn)
        if full:
            if (conn.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0]
                    or conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]):
                db.snapshot(conn, reason="pre-full-rebuild")
            db.reset(conn)
        try:
            src = open_source(host_db)
        except (SourceMissing, sqlite3.Error) as exc:
            conn.commit()
            return {"ok": False, "error": str(exc), "schema": status,
                    "unparsed_this_run": 0}
        try:
            stats = _run(conn, src, status, quiet=quiet, host_config=host_config)
        finally:
            src.close()
        return stats
    finally:
        # The TUI calls this from a worker thread every 30 seconds, so an
        # exception that skipped the close would leak a read-write connection on
        # every failed poll rather than once per process.
        conn.close()


def _run(conn, src, status: str, quiet: bool = False, host_config=None) -> dict:
    counts = Counts()
    inventory = scan_inventory(conn, host_config)
    servers = mcp_servers(host_config)
    conn.commit()
    index_sessions(conn, src, counts)
    conn.commit()
    index_parts(conn, src, counts, servers)
    n_unparsed = counts.flush(conn)
    conn.commit()

    stats = {
        "ok": True,
        "schema": status,
        "host_db": str(src.execute("PRAGMA database_list").fetchone()[2]),
        "parts_read": counts.parts,
        "tool_calls": counts.calls,
        "steps": counts.steps,
        "skills": counts.skills,
        "agents": counts.agents,
        "mcp": counts.mcp,
        "mcp_servers_configured": len(servers),
        "sessions": counts.sessions,
        "inventory": inventory,
        "cursor": cursor_of(conn, CURSOR_PART),
        # Per-run and cumulative: a format change moves both, while an old
        # unaddressed count only shows in the total.
        "unparsed_this_run": n_unparsed,
        "unparsed": conn.execute(
            "SELECT COALESCE(SUM(count),0) FROM unparsed").fetchone()[0],
        "unparsed_kinds": conn.execute(
            "SELECT COUNT(*) FROM unparsed").fetchone()[0],
        "rows_tool_calls": conn.execute(
            "SELECT COUNT(*) FROM tool_calls").fetchone()[0],
        "rows_model_calls": conn.execute(
            "SELECT COUNT(*) FROM model_calls").fetchone()[0],
        "rows_sessions": conn.execute(
            "SELECT COUNT(*) FROM sessions").fetchone()[0],
        "rows_mcp": conn.execute("SELECT COUNT(*) FROM mcp_usage").fetchone()[0],
        # Measured, not alarmed: recorded calls with the MCP name shape that no
        # config entry owns today.
        "mcp_shape_unowned_calls": conn.execute(MCP_SHAPE_QUERY).fetchone()[0],
        "prompt_tokens": conn.execute(
            "SELECT COALESCE(SUM(prompt_tokens),0) FROM model_calls").fetchone()[0],
        "completion_tokens": conn.execute(
            "SELECT COALESCE(SUM(completion_tokens),0) FROM model_calls").fetchone()[0],
    }
    # Loud on purpose: an unclaimed record is the only signal that OpenCode's
    # format moved, and a run that prints only "done" hides it.
    if not quiet and stats["unparsed_this_run"]:
        reasons = ", ".join(f"{r[0]} x{r[1]}" for r in db.q_unparsed(conn)[:5])
        print(f"  UNPARSED {stats['unparsed_this_run']} records this run "
              f"({stats['unparsed']} total, {reasons}) — ocut health lists them")
    if not quiet:
        print(f"host {stats['host_db']}")
        print(f"  parts_read={stats['parts_read']}  cursor={stats['cursor']}")
        print(f"  tool_calls={stats['rows_tool_calls']}  "
              f"model_calls={stats['rows_model_calls']}  "
              f"sessions={stats['rows_sessions']}")
        print(f"  this run: calls={stats['tool_calls']} steps={stats['steps']} "
              f"skills={stats['skills']} agents={stats['agents']} "
              f"mcp={stats['mcp']} sessions={stats['sessions']}")
        print(f"  mcp: servers={stats['mcp_servers_configured']} "
              f"attributed={stats['rows_mcp']} server-shaped-unowned="
              f"{stats['mcp_shape_unowned_calls']}")
        print(f"  inventory: " + ", ".join(
            f"{k}={v}" for k, v in sorted(inventory.items())))
    return stats


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Index OpenCode's own database into the tracker DB (read-only).")
    ap.add_argument("--full", action="store_true", help="wipe and re-index everything")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--db", default=str(db.DB_PATH))
    ap.add_argument("--host-db", default=None,
                    help="override the OpenCode database to read (tests, dry runs)")
    ap.add_argument("--host-config", default=None,
                    help="override the OpenCode config home to inventory")
    args = ap.parse_args(argv)
    t0 = time.time()
    stats = run(full=args.full, quiet=args.quiet, db_path=args.db,
                host_db=args.host_db, host_config=args.host_config)
    if not stats.get("ok"):
        print(f"nothing indexed: {stats.get('error')}", file=sys.stderr)
        return 1
    if not args.quiet:
        print(f"done in {time.time() - t0:.1f}s -> {args.db}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
