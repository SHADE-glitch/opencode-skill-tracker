"""The claude-mem read-only neighbour: counts and freshness, never its text.

Same contract as `test_agentos.py` enforces for the advisor store, for the same
reason: this is another project's database, it is full of the user's own words,
and a live process is writing it. Every test here runs against a fixture store in
`tmp_path` — nothing reads `~/.claude-mem`.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import sqlite3
import time
import urllib.parse
import urllib.request

import pytest
from conftest import load_module, write_claude_mem_files

import skill_db as db
import skill_db_claude_mem as cm

st = load_module("skill-tui.py", "skill_tui")

# Anything that reaches the output fails a test. Every prose column of the real
# store gets this value, and so does the fake key in settings.json.
SENTINEL = "SENTINEL-NEVER-LEAVE-THIS-STORE"
SECRET = "SENTINEL-FAKE-API-KEY"


SCHEMA = """
CREATE TABLE observations (
  id INTEGER PRIMARY KEY, memory_session_id TEXT, project TEXT,
  text TEXT, type TEXT, title TEXT, facts TEXT, narrative TEXT,
  created_at_epoch INTEGER, discovery_tokens INTEGER, agent_type TEXT);
CREATE TABLE user_prompts (
  id INTEGER PRIMARY KEY, content_session_id TEXT, prompt_number INTEGER,
  prompt_text TEXT, created_at_epoch INTEGER);
CREATE TABLE session_summaries (
  id INTEGER PRIMARY KEY, memory_session_id TEXT, project TEXT,
  request TEXT, learned TEXT, completed TEXT,
  created_at_epoch INTEGER, discovery_tokens INTEGER);
CREATE TABLE sdk_sessions (
  id INTEGER PRIMARY KEY, content_session_id TEXT, project TEXT,
  user_prompt TEXT, custom_title TEXT,
  started_at_epoch INTEGER, completed_at_epoch INTEGER, status TEXT);
CREATE TABLE tool_uses (
  id INTEGER PRIMARY KEY, content_session_id TEXT, tool_name TEXT,
  tool_input TEXT, tool_response TEXT, cwd TEXT, created_at_epoch INTEGER);
CREATE TABLE pending_messages (
  id INTEGER PRIMARY KEY, content_session_id TEXT, tool_name TEXT,
  last_user_message TEXT, status TEXT, created_at_epoch INTEGER);
"""


def make_store(tmp_path, *, rows=True, health=True, extra_observations=0,
               stale_ms=0, drop_table=None, drop_column=None,
               weird_label=False, seconds_epoch=False, subdir="cm"):
    """A fixture store: the real table and column names, synthetic values only."""
    root = tmp_path / subdir
    root.mkdir(parents=True, exist_ok=True)
    path = str(root / "claude-mem.db")
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    if drop_table:
        conn.execute(f"DROP TABLE {drop_table}")
    if drop_column:
        conn.execute(f"ALTER TABLE {drop_column[0]} DROP COLUMN {drop_column[1]}")
    if rows:
        now = int(time.time() * 1000) - stale_ms
        if seconds_epoch:
            now = now // 1000
        # Column list built from what actually exists, so a test can drop one and
        # still populate the table (that is the point of those tests).
        cols = [r[1] for r in conn.execute("PRAGMA table_info(observations)")
                if r[1] != "id"]
        values = {"project": "/p", "text": SENTINEL, "type": "discovery",
                  "title": SENTINEL, "facts": SENTINEL, "narrative": SENTINEL,
                  "memory_session_id": "m1", "agent_type": "build",
                  "created_at_epoch": now, "discovery_tokens": 1200}
        # Anything the fixture schema has but this list does not: NULL, never a
        # guess — a column added upstream should read as missing, not as content.
        conn.execute(
            f"INSERT INTO observations ({', '.join(cols)})"
            f" VALUES ({', '.join('?' * len(cols))})",
            tuple(values.get(c) for c in cols),
        )
        for i in range(extra_observations):
            values["type"] = "bugfix"
            values["discovery_tokens"] = 300
            values["created_at_epoch"] = now - 1000 * (i + 1)
            conn.execute(
                f"INSERT INTO observations ({', '.join(cols)})"
                f" VALUES ({', '.join('?' * len(cols))})",
                tuple(values.get(c) for c in cols),
            )
        if weird_label:
            # A `type` that is not a category name: counted, never echoed.
            values["type"] = f"{SENTINEL} because a future version put prose here"
            values["created_at_epoch"] = now
            conn.execute(
                f"INSERT INTO observations ({', '.join(cols)})"
                f" VALUES ({', '.join('?' * len(cols))})",
                tuple(values.get(c) for c in cols),
            )
        conn.execute(
            "INSERT INTO user_prompts (content_session_id, prompt_number, prompt_text,"
            " created_at_epoch) VALUES (?,?,?,?)",
            ("ses_a", 1, SENTINEL, now),
        )
        conn.execute(
            "INSERT INTO session_summaries (memory_session_id, project, request, learned,"
            " completed, created_at_epoch, discovery_tokens) VALUES (?,?,?,?,?,?,?)",
            ("m1", "/p", SENTINEL, SENTINEL, SENTINEL, now, 4000),
        )
        conn.execute(
            "INSERT INTO sdk_sessions (content_session_id, project, user_prompt,"
            " custom_title, started_at_epoch, status) VALUES (?,?,?,?,?,?)",
            (f"opencode-ses_a-{now}", "/p", SENTINEL, SENTINEL, now, "active"),
        )
        conn.commit()
    conn.close()
    if health:
        stamp = int(time.time() * 1000)
        (root / "observer-health.json").write_text(json.dumps({
            "consecutiveFailures": 0,
            "lastSuccessAt": stamp,
            "lastErrorAt": stamp - 60000,
            "lastErrorKind": "quota_exhausted",
            "lastErrorMessage": SENTINEL,
            "lastErrorUrl": "https://example.invalid/" + SENTINEL,
        }), encoding="utf-8")
        (root / "backfill.json").write_text(json.dumps({
            "completedAt": "2026-10-03T10:01:41.220Z", "eventCount": 0,
            "throughDay": "2026-09-30", "version": 2, "installId": SENTINEL,
        }), encoding="utf-8")
        (root / "telemetry.json").write_text(json.dumps({
            "enabled": False, "decidedAt": "2026-10-03T10:02:26.243Z", "installId": SENTINEL,
        }), encoding="utf-8")
    # The file this reader must never open.
    (root / "settings.json").write_text(json.dumps({
        "CLAUDE_MEM_MODE": "manual",
        "CLAUDE_MEM_OPENROUTER_API_KEY": SECRET,
        "CLAUDE_MEM_PRO_MEMORY_KEY": SECRET,
    }), encoding="utf-8")
    return path


@pytest.fixture
def tracker_db(tmp_path):
    path = str(tmp_path / "tracker.db")
    conn = db.open_db(path, readonly=False)
    db.ensure_schema(conn)
    conn.execute(
        "INSERT INTO skill_usage (skill_name, session_id, project_path, trigger_type,"
        " status, timestamp, call_id) VALUES ('grow','s1','/p','tool_call','success',"
        " strftime('%Y-%m-%dT%H:%M:%fZ','now'), 'c1')"
    )
    conn.commit()
    yield conn
    conn.close()


def test_the_suite_never_sees_the_developers_own_store(monkeypatch):
    """The guard for the guard: conftest pins the neighbour's location.

    Without it this file would pass on a machine that has claude-mem installed
    and mean nothing on one that does not — the same class of hole the skills
    directory used to have.
    """
    def no_network(*a, **k):
        raise AssertionError("the isolated suite reached the network")

    monkeypatch.setattr(urllib.request, "urlopen", no_network)
    store = cm.claude_mem_store()
    assert store["db"] is None, store
    assert store["requested"].endswith("no-claude-mem.db"), store
    # The three files the neighbour leaves beside its database resolve from that
    # same pinned directory, so the activity reader and the pid-file liveness
    # check are covered by this fixture too — and with no `worker.pid` there is no
    # port to ask, which is what keeps the HTTP probe out of the whole suite.
    assert store["trace"] is None and store["logs_dir"] is None, store
    assert store["pid_file"] is None, store
    assert cm.claude_mem_activity(store=store)["available"] is False
    assert cm.claude_mem_worker(store=store)["alive"] is False
    assert cm.claude_mem_http()["available"] is False


# --- resolution ------------------------------------------------------------
def test_store_resolution_prefers_explicit_then_env(tmp_path, monkeypatch):
    monkeypatch.delenv(cm.CLAUDE_MEM_DB_ENV, raising=False)
    monkeypatch.delenv(cm.CLAUDE_MEM_DIR_ENV, raising=False)
    real = make_store(tmp_path, subdir="cm")
    via_env = make_store(tmp_path, subdir="env-cm")
    via_dir = make_store(tmp_path, subdir="dir-cm")

    assert cm.claude_mem_store(real)["db"] == real
    monkeypatch.setenv(cm.CLAUDE_MEM_DB_ENV, via_env)
    assert cm.claude_mem_store(real)["db"] == real, "the explicit argument wins"
    assert cm.claude_mem_store()["db"] == via_env
    monkeypatch.delenv(cm.CLAUDE_MEM_DB_ENV)
    monkeypatch.setenv(cm.CLAUDE_MEM_DIR_ENV, str(tmp_path / "dir-cm"))
    assert cm.claude_mem_store()["db"] == via_dir
    monkeypatch.delenv(cm.CLAUDE_MEM_DIR_ENV)
    # With no env at all the well-known default is what makes it discoverable —
    # unlike the advisor, whose location is a project choice and must be told.
    monkeypatch.setattr(db, "HOME", str(tmp_path / "no-home"))
    assert cm.claude_mem_store()["requested"] == str(
        tmp_path / "no-home" / ".claude-mem" / "claude-mem.db")


def test_a_missing_store_is_reported_and_never_created(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "HOME", str(tmp_path))
    gone = str(tmp_path / "nowhere" / "claude-mem.db")
    res = cm.claude_mem_store(gone)
    assert res["db"] is None and "no claude-mem database" in res["reason"]
    assert not os.path.exists(gone), "a reader must not create a foreign store"

    summary = cm.claude_mem_summary(None, db_path=gone)
    assert summary["available"] is False and summary["reason"]


# --- the summary itself ----------------------------------------------------
def test_summary_counts_labels_and_tokens_without_reading_a_word(tmp_path, tracker_db):
    path = make_store(tmp_path, extra_observations=2)
    s = cm.claude_mem_summary(tracker_db, db_path=path, days=7)
    assert s["available"] is True, s["reason"]
    obs = s["tables"]["observations"]
    assert obs["n"] == 3 and obs["n_window"] == 3
    assert obs["tokens"] == 1200 + 300 + 300
    assert obs["by"] == {"discovery": 1, "bugfix": 2}, obs["by"]
    assert s["tables"]["user_prompts"]["n"] == 1
    assert s["tables"]["session_summaries"]["tokens"] == 4000
    assert s["tables"]["sdk_sessions"]["by"] == {"active": 1}
    # Empty tables read as zero rows, not as "cannot tell".
    assert s["tables"]["tool_uses"]["n"] == 0
    assert s["tables"]["tool_uses"]["n_window"] == 0, "0 rows must not read as unknown"
    assert s["health"]["consecutiveFailures"] == 0
    assert s["health"]["lastErrorKind"] == "quota_exhausted"
    assert s["backfill"]["throughDay"] == "2026-09-30"
    assert s["telemetry"] == {"enabled": False, "decidedAt": "2026-10-03T10:02:26.243Z"}
    assert s["newest"] and cm.claude_mem_age_days(s) < 1


def test_the_tracker_join_counts_the_same_window(tmp_path, tracker_db):
    path = make_store(tmp_path)
    s = cm.claude_mem_summary(tracker_db, db_path=path, days=7)
    assert s["tracker_same_window"] == {
        "skill_usage": 1, "mcp_usage": 0, "plugin_usage": 0,
    }, s["tracker_same_window"]
    # A window that excludes everything must say so, not invent a number.
    old = make_store(tmp_path, subdir="old", stale_ms=40 * 86400 * 1000)
    s2 = cm.claude_mem_summary(tracker_db, db_path=old, days=7)
    assert s2["tables"]["observations"]["n_window"] == 0
    assert s2["tables"]["observations"]["n"] == 1


def test_no_tracker_connection_still_reads_the_neighbour(tmp_path):
    path = make_store(tmp_path)
    s = cm.claude_mem_summary(None, db_path=path)
    assert s["available"] is True and s["tracker_same_window"] is None


# --- what must never happen ------------------------------------------------
def test_no_prose_or_secret_reaches_the_output(tmp_path, tracker_db, capsys, monkeypatch):
    """The whole point of the whitelist, checked end to end.

    Every prose column of the fixture holds SENTINEL and settings.json holds a
    fake key; neither may appear in the text output, the JSON output, or the
    summary dict itself — not because they are filtered on the way out, but
    because no query names those columns.
    """
    path = make_store(tmp_path, weird_label=True)
    s = cm.claude_mem_summary(tracker_db, db_path=path, days=7)
    assert SENTINEL not in json.dumps(s, ensure_ascii=False)
    assert SECRET not in json.dumps(s, ensure_ascii=False)

    # The CLI resolves the store the way a user does — from the environment — so
    # that is what the test drives. Passing a path in by hand would skip the only
    # code path the command actually has.
    monkeypatch.setenv(cm.CLAUDE_MEM_DB_ENV, path)
    args = st.Args()
    args.db, args.json, args.days = path, False, 7
    assert st._cli_claude_mem(tracker_db, args) == 0
    text = capsys.readouterr().out
    assert SENTINEL not in text and SECRET not in text, text

    args.json = True
    assert st._cli_claude_mem(tracker_db, args) == 0
    blob = capsys.readouterr().out
    assert SENTINEL not in blob and SECRET not in blob
    assert json.loads(blob)["available"] is True


def test_settings_json_is_never_opened(tmp_path, tracker_db, monkeypatch):
    """It holds API keys, so the file is off limits — not "read but ignored".

    A tripwire on `open` is the only way to prove this: a whitelist that happens
    not to name a key today is one refactor away from naming one tomorrow.
    """
    path = make_store(tmp_path)
    real_open = open

    def guarded(file, *a, **k):
        if str(file).endswith("settings.json"):
            raise AssertionError(f"the reader opened {file}")
        return real_open(file, *a, **k)

    monkeypatch.setitem(__builtins__ if isinstance(__builtins__, dict) else __builtins__.__dict__,
                        "open", guarded)
    s = cm.claude_mem_summary(tracker_db, db_path=path)
    assert s["available"] is True
    assert "settings.json" in cm.CLAUDE_MEM_FORBIDDEN_FILES


def test_a_label_that_is_not_a_category_name_is_counted_not_echoed(tmp_path, tracker_db):
    path = make_store(tmp_path, weird_label=True)
    s = cm.claude_mem_summary(tracker_db, db_path=path)
    by = s["tables"]["observations"]["by"]
    assert SENTINEL not in " ".join(by), by
    assert by.get("(not shown)") == 1, by
    assert s["tables"]["observations"]["n"] == 2


def test_epoch_units_are_validated_not_guessed(tmp_path, tracker_db):
    """A store that switches to seconds must show nothing, not 1970."""
    path = make_store(tmp_path, seconds_epoch=True)
    s = cm.claude_mem_summary(tracker_db, db_path=path)
    assert s["tables"]["observations"]["last"] is None
    assert s["newest"] is None and cm.claude_mem_age_days(s) is None


# --- the activity files it writes ------------------------------------------
TRACE_LINES = [
    "2026-10-03T13:20:40.381Z loaded project=opencode",
    "2026-10-03T13:20:41.000Z injected project=opencode len=120",
    "2026-10-03T14:34:21.000Z injected source=search project=opencode len=1440 "
    'q="' + SENTINEL + '"',
    "2026-10-03T14:35:00.000Z injected source=recency project=opencode len=88",
    "2026-10-03T14:36:00.000Z worker ensure: spawned start "
    "/home/" + SENTINEL + "/.bun/bin/bun",
    "2026-10-03T14:37:00.000Z something a future version invented",
]
WORKER_LINES = [
    "[2026-10-03 13:20:40.381] [INFO ] [OPENCODE] Plugin installed {ok}",
    "[2026-10-03 13:20:41.381] [ERROR] [CHROMA_SYNC] " + SENTINEL + " up the stack",
    "[2026-10-03 13:20:42.381] [WARN ] [WORKER] queue drained late",
    "[2026-10-03 13:20:43.381] [ERROR] [CHROMA_SYNC] second failure",
    "a line with no brackets at all " + SENTINEL,
]


def add_activity_files(tmp_path, trace=None, worker=None, dated=True):
    """The two files claude-mem writes for itself, next to the fixture store.

    The writer itself is in `conftest` because the TUI tests need the same three
    files; these lines are the ones this suite's assertions are written against.
    """
    write_claude_mem_files(tmp_path / "cm",
                           trace=TRACE_LINES if trace is None else trace,
                           worker=WORKER_LINES if worker is None else worker,
                           dated=dated)
    return tmp_path / "cm"


def test_activity_counts_injections_whatever_shape_they_have(tmp_path, tracker_db):
    """The bare `injected project=…` lines are the majority, not the exception.

    A first parser keyed on `source=` and would have reported 2 of 3 injections —
    the file holds both shapes because the inject plugin was edited in place.
    Anything that matches no known shape lands in `unrecognized` rather than
    vanishing.
    """
    root = add_activity_files(tmp_path)
    a = cm.claude_mem_activity(store=cm.claude_mem_store(str(root / "claude-mem.db")))
    t = a["trace"]
    assert t["injected"] == 3, t
    assert t["injected_bare"] == 1 and t["injected_with_source"] == 2, t
    assert t["loaded"] == 1 and t["worker_ensure"] == 1, t
    assert t["unrecognized"] == 1, t
    assert t["chars_total"] == 120 + 1440 + 88, t
    assert t["last_ts"].startswith("2026-10-03 "), t


def test_the_newest_trace_stamp_wins_not_the_last_line_read(tmp_path, tracker_db):
    """`last_ts` is the latest moment in the file, out of order or not.

    Comparing the raw stamp against the already-formatted value let any ISO stamp
    win, because 'T' sorts above the space in `YYYY-MM-DD HH:MM` — so an older
    line read later overwrote the newest. A line with no timestamp at all must
    not freeze the maximum either.
    """
    lines = [
        "2026-10-03T14:37:00.000Z loaded project=written-late",
        "2026-10-03T09:00:00.000Z loaded project=older",
        "not-a-timestamp loaded project=garbage",
    ]
    root = add_activity_files(tmp_path, trace=lines)
    a = cm.claude_mem_activity(store=cm.claude_mem_store(str(root / "claude-mem.db")))
    assert a["trace"]["last_ts"] == db.fmt_time("2026-10-03T14:37:00.000Z"), a["trace"]


def test_activity_counts_worker_log_levels_and_categories(tmp_path, tracker_db):
    root = add_activity_files(tmp_path)
    a = cm.claude_mem_activity(store=cm.claude_mem_store(str(root / "claude-mem.db")))
    w = a["worker_log"]
    # `[时间] [级别] [类别]` — the second bracket is the LEVEL. Reading ERROR as
    # a category (the first attempt) counts nothing at all.
    assert w["levels"] == {"INFO": 1, "ERROR": 2, "WARN": 1}, w
    assert w["categories"].get("CHROMA_SYNC") == 2, w
    assert w["unparsed"] == 1, w


def test_no_verbatim_query_or_log_line_crosses_the_activity_reader(tmp_path, tracker_db):
    """`q=` holds the user's prompt text; log bodies hold paths and messages.

    Neither may appear anywhere in what the reader returns — it counts shapes.
    """
    root = add_activity_files(tmp_path)
    a = cm.claude_mem_activity(store=cm.claude_mem_store(str(root / "claude-mem.db")))
    blob = json.dumps(a, ensure_ascii=False)
    assert SENTINEL not in blob, blob[:400]
    assert SECRET not in blob


def test_the_activity_reader_never_opens_settings_json(tmp_path, tracker_db, monkeypatch):
    """A second door into that directory needs the same tripwire as the first."""
    root = add_activity_files(tmp_path)
    real_open = open

    def guarded(file, *a, **k):
        if str(file).endswith("settings.json"):
            raise AssertionError(f"the activity reader opened {file}")
        return real_open(file, *a, **k)

    monkeypatch.setitem(__builtins__.__dict__ if hasattr(__builtins__, "__dict__")
                        else __builtins__, "open", guarded)
    a = cm.claude_mem_activity(store=cm.claude_mem_store(str(root / "claude-mem.db")))
    assert a["available"] is True


def test_errors_are_counted_from_the_start_of_the_file_not_a_tail(tmp_path, tracker_db):
    """A 64 KB tail on the real log saw 0 of its 57 ERROR lines.

    The failures are early, the chatter is late — so a tail is exactly the wrong
    end to read, and a size cap has to cut the *end* of the file and say so.
    """
    lines = [f"[2026-10-03 00:00:0{i}.0] [ERROR] [CHROMA_SYNC] early {i}"
             for i in range(3)]
    lines += [f"[2026-10-03 00:01:{i:02d}.0] [INFO ] [WORKER] filler" for i in range(60)]
    root = add_activity_files(tmp_path, worker=lines)
    path = str(root / "claude-mem.db")
    full = cm.claude_mem_activity(store=cm.claude_mem_store(path))
    assert full["worker_log"]["levels"]["ERROR"] == 3, full["worker_log"]
    assert full["worker_log"]["truncated"] is False
    capped = cm.claude_mem_activity(store=cm.claude_mem_store(path), log_bytes_cap=200)
    assert capped["worker_log"]["truncated"] is True
    assert capped["worker_log"]["levels"]["ERROR"] == 3, "the head is what survives a cap"


def test_only_the_dated_worker_log_is_read(tmp_path, tracker_db):
    """`manual-restart-*.log` files sit in the same directory and are all empty."""
    root = add_activity_files(tmp_path, dated=False)
    a = cm.claude_mem_activity(store=cm.claude_mem_store(str(root / "claude-mem.db")))
    assert a["worker_log"] is None, a
    add_activity_files(tmp_path)          # now the dated one exists too
    a = cm.claude_mem_activity(store=cm.claude_mem_store(str(root / "claude-mem.db")))
    assert a["worker_log"]["levels"]["ERROR"] == 2, a


def test_activity_is_optional_and_silent_when_the_files_are_absent(tmp_path, tracker_db):
    path = make_store(tmp_path, health=False)
    a = cm.claude_mem_activity(store=cm.claude_mem_store(path))
    assert a["available"] is False
    assert a["trace"] is None and a["worker_log"] is None


# --- the worker: pid file first, HTTP last ---------------------------------
def add_pid_file(root, *, pid=None, port=37701):
    """The file the worker writes for itself. `startToken` authenticates to it."""
    write_claude_mem_files(root, trace=None, worker=None, token=SENTINEL,
                           pid=pid or os.getpid(), port=port)


def _free_pid():
    for candidate in range(40000, 40200):
        try:
            os.kill(candidate, 0)
        except ProcessLookupError:
            return candidate
        except OSError:
            continue
    return None


class _Resp:
    """The minimum `urlopen` result the reader may touch: a status and a body."""

    def __init__(self, body):
        self.status = 200
        self._body = json.dumps(body).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# Exactly what the real endpoints answered with on 2026-10-03, plus the fields
# that must not cross: an absolute path, free text, a version string.
REAL_STATS = {"worker": {"version": SENTINEL, "uptime": 12478, "activeSessions": 3,
                         "sseClients": 6, "port": 37700},
              "database": {"path": "/home/" + SENTINEL + "/.claude-mem/claude-mem.db",
                           "size": 876544, "observations": 210, "sessions": 16,
                           "summaries": 1, "firstObservationAt": "2026-10-03T11:06:59.463Z"}}
REAL_QUEUE = {"isProcessing": True, "queueDepth": 48, "parkedSessions": 0}
REAL_CHROMA = {"status": SENTINEL, "connected": True,
               "timestamp": "2026-10-03T16:22:48.128Z", "deep": False,
               "details": SENTINEL}
BODIES = {"/api/stats": REAL_STATS, "/api/processing-status": REAL_QUEUE,
          "/api/chroma/status": REAL_CHROMA}


def fake_http(monkeypatch, *, bodies=None, step=0.0, fail=False):
    """Stand in for the worker, on a clock the test owns.

    Returns the timeouts it was handed and the clock to pass to the probe. No
    sleeping: a deadline shared between three requests is arithmetic, and
    arithmetic is not flaky.
    """
    calls = []
    ticks = [0.0]
    bodies = BODIES if bodies is None else bodies

    def urlopen(req, timeout=None, **kw):
        calls.append(timeout)
        ticks[0] += step
        if fail:
            raise OSError(111, "Connection refused")
        path = urllib.parse.urlsplit(str(req.full_url)).path
        if path not in bodies:
            raise OSError(404, path)
        return _Resp(bodies[path])

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    return calls, lambda: ticks[0]


def test_worker_pid_gives_the_port_and_liveness_without_http(tmp_path, tracker_db, monkeypatch):
    """Liveness is `os.kill(pid, 0)`: microseconds, and it works when the worker is down."""
    def no_network(*a, **k):
        raise AssertionError("reading the pid file must not reach the network")

    monkeypatch.setattr(urllib.request, "urlopen", no_network)
    root = add_activity_files(tmp_path)
    add_pid_file(root)
    w = cm.claude_mem_worker(store=cm.claude_mem_store(str(root / "claude-mem.db")))
    assert w["alive"] is True, w
    assert w["port"] == 37701, w               # from the file, never hardcoded
    assert w["pid"] == os.getpid(), w
    assert w["started_at"] == db.fmt_time("2026-10-03T12:54:49.179Z"), w
    blob = json.dumps(w, ensure_ascii=False)
    assert SENTINEL not in blob, blob          # startToken authenticates; it is not a stat


def test_a_dead_worker_is_reported_and_not_treated_as_a_failure(tmp_path, tracker_db):
    """It is an on-demand process: absent is a state, not a fault."""
    root = add_activity_files(tmp_path)
    dead = _free_pid()
    add_pid_file(root, pid=dead)
    w = cm.claude_mem_worker(store=cm.claude_mem_store(str(root / "claude-mem.db")))
    assert w["alive"] is False, w
    assert w["port"] == 37701, w               # the port it *would* use
    assert w["reason"], w


def test_no_pid_file_means_no_worker_and_no_crash(tmp_path, tracker_db):
    root = add_activity_files(tmp_path)
    w = cm.claude_mem_worker(store=cm.claude_mem_store(str(root / "claude-mem.db")))
    assert w["alive"] is False and w["port"] is None, w


def test_the_http_probe_shares_one_deadline(tmp_path, monkeypatch):
    """Three endpoints, one budget — not one timeout each.

    A worker that is starting up answers slowly; three separate timeouts would
    make the command wait three times over before it said anything.
    """
    calls, clock = fake_http(monkeypatch, step=0.3)
    out = cm.claude_mem_http(worker={"alive": True, "port": 37701},
                             budget_seconds=0.5, clock=clock)
    assert calls == [0.5, 0.2], calls          # each got what was left, not the full budget
    assert out["skipped"] == ["chroma"], out


def test_no_http_is_attempted_when_the_worker_is_not_running(tmp_path, monkeypatch):
    calls, clock = fake_http(monkeypatch)
    out = cm.claude_mem_http(worker={"alive": False, "port": 37701, "reason": "down"},
                             clock=clock)
    assert calls == [], calls
    assert out["available"] is False and out["reason"], out


def test_the_http_probe_degrades_to_none(tmp_path, monkeypatch):
    """Refused, missing key, wrong type — none of it may raise into a command."""
    calls, clock = fake_http(monkeypatch, fail=True)
    out = cm.claude_mem_http(worker={"alive": True, "port": 37701}, clock=clock)
    assert out["available"] is False, out
    assert out["stats"] is None and out["queue"] is None and out["chroma"] is None
    assert len(calls) == 3, calls              # it tried, and every try came back empty

    calls, clock = fake_http(monkeypatch, bodies={})
    out = cm.claude_mem_http(worker={"alive": True, "port": 37701}, clock=clock)
    assert out["available"] is False, out


def test_no_path_or_free_text_crosses_the_http_whitelist(tmp_path, monkeypatch):
    """Numbers and booleans, and nothing else, whatever the worker answers."""
    calls, clock = fake_http(monkeypatch)
    out = cm.claude_mem_http(worker={"alive": True, "port": 37701}, clock=clock)
    blob = json.dumps(out, ensure_ascii=False)
    assert SENTINEL not in blob, blob
    assert "claude-mem.db" not in blob, blob

    def leaf_types(node):
        if isinstance(node, dict):
            for v in node.values():
                yield from leaf_types(v)
        elif isinstance(node, list):
            for v in node:
                yield from leaf_types(v)
        else:
            yield node

    for key in ("stats", "queue", "chroma"):
        for value in leaf_types(out[key] or {}):
            assert isinstance(value, (int, bool)), (key, value)
    assert out["stats"]["database"]["observations"] == 210, out
    assert out["queue"]["queueDepth"] == 48, out
    assert out["chroma"]["connected"] is True, out


def test_a_whitelisted_key_holding_a_string_is_dropped(tmp_path, monkeypatch):
    """The whitelist is a type contract, not just a name list.

    A future worker that answers `observations: "210"` — or with a path in a field
    this code already trusts by name — must not smuggle text through.
    """
    calls, clock = fake_http(monkeypatch, bodies={"/api/stats": {
        "worker": {"uptime": 10},
        "database": {"observations": SENTINEL, "sessions": 16}}})
    out = cm.claude_mem_http(worker={"alive": True, "port": 37701}, clock=clock)
    assert out["stats"] == {"worker": {"uptime": 10}, "database": {"sessions": 16}}, out


def test_doctor_warns_on_the_error_level_field_not_an_error_category(tmp_path, tracker_db, monkeypatch):
    """The count comes from bracket two of the log line, and it is a WARN."""
    path = make_store(tmp_path)
    root = pathlib.Path(path).parent
    add_activity_files(tmp_path)               # ERROR x2 at the level position
    names = _doctor(tmp_path, tracker_db, monkeypatch, store=path)
    status, detail = names["claude_mem.capture"]
    assert status == "WARN", detail
    assert "2 worker-log ERROR" in detail, detail


def test_doctor_stays_passing_and_silent_about_the_worker(tmp_path, tracker_db, monkeypatch):
    """Neither a clean log nor a missing worker is a warning."""
    path = make_store(tmp_path)
    root = pathlib.Path(path).parent
    (root / "logs").mkdir(parents=True, exist_ok=True)
    (root / "logs" / "claude-mem-2026-10-03.log").write_text(
        "[2026-10-03 13:20:40.381] [INFO ] [WORKER] nothing wrong\n", encoding="utf-8")
    names = _doctor(tmp_path, tracker_db, monkeypatch, store=path)
    status, detail = names["claude_mem.capture"]
    assert status == "PASS", detail
    assert "ERROR" not in detail, detail

    add_pid_file(root)
    names = _doctor(tmp_path, tracker_db, monkeypatch, store=path)
    assert names["claude_mem.capture"][0] == "PASS", names["claude_mem.capture"]


def test_doctor_never_reaches_the_worker_over_http(tmp_path, tracker_db, monkeypatch):
    """Doctor is offline by contract, and the probe is only for headless commands."""
    def no_network(*a, **k):
        raise AssertionError("doctor opened the network")

    monkeypatch.setattr(urllib.request, "urlopen", no_network)
    path = make_store(tmp_path)
    root = pathlib.Path(path).parent
    add_activity_files(tmp_path)
    add_pid_file(root)
    names = _doctor(tmp_path, tracker_db, monkeypatch, store=path)
    assert "claude_mem.capture" in names, sorted(names)


def test_trace_groups_injections_by_project_and_local_day(tmp_path, tracker_db):
    """The log carries `project=` and a timestamp, so grouping needs no new file."""
    lines = [
        "2026-10-03T13:20:40.381Z loaded project=opencode",
        "2026-10-03T13:20:41.000Z injected project=opencode len=120",
        "2026-10-03T14:34:21.000Z injected project=selftest len=1440",
        "2026-10-04T01:00:00.000Z injected project=opencode len=88",
    ]
    root = add_activity_files(tmp_path, trace=lines)
    t = cm.claude_mem_activity(store=cm.claude_mem_store(str(root / "claude-mem.db")))["trace"]
    assert t["by_project"]["opencode"]["injected"] == 2, t["by_project"]
    assert t["by_project"]["opencode"]["loaded"] == 1, t["by_project"]
    assert t["by_project"]["selftest"]["injected"] == 1, t["by_project"]
    assert t["injected"] == 3, t


def test_the_day_buckets_follow_the_local_calendar_not_utc(tmp_path, tracker_db):
    """Same rule as the dashboard trend (M1): a day is a local day.

    UTC `20:00` is the next calendar day here (UTC+8), so a bucket keyed off the
    raw `…Z` prefix would disagree with every other chart on the screen.
    """
    lines = ["2026-10-03T20:00:00.000Z injected project=opencode len=1"]
    root = add_activity_files(tmp_path, trace=lines)
    t = cm.claude_mem_activity(store=cm.claude_mem_store(str(root / "claude-mem.db")))["trace"]
    local_day = db.fmt_time("2026-10-03T20:00:00.000Z")[:10]
    assert list(t["by_day"]) == [local_day], t["by_day"]
    if local_day != "2026-10-03":       # true on this box (UTC+8); UTC hosts agree anyway
        assert "2026-10-03" not in t["by_day"], t["by_day"]


def test_a_project_name_that_is_not_a_slug_is_counted_never_named(tmp_path, tracker_db):
    """`project=` is a foreign field; a sentence there must not become a label.

    Spaces and slashes are rejected on purpose: one is prose, the other a path.
    """
    lines = [
        f'2026-10-03T13:20:41.000Z injected project="{SENTINEL} because it drifted" len=5',
        "2026-10-03T13:20:42.000Z injected project=/home/" + SENTINEL + "/p len=5",
        "2026-10-03T13:20:43.000Z injected len=5",          # no project at all
        "2026-10-03T13:20:44.000Z injected project=opencode len=5",
    ]
    root = add_activity_files(tmp_path, trace=lines)
    a = cm.claude_mem_activity(store=cm.claude_mem_store(str(root / "claude-mem.db")))
    t = a["trace"]
    assert set(t["by_project"]) == {"opencode"}, t["by_project"]
    assert t["by_project"]["opencode"]["injected"] == 1, t
    assert t["projectless"]["injected"] == 3, t["projectless"]
    blob = json.dumps(a, ensure_ascii=False)
    assert SENTINEL not in blob, blob[:400]


def test_grouping_never_loses_a_count(tmp_path, tracker_db):
    """Buckets must account for every injected and loaded line, cap or no cap."""
    lines = []
    for day in range(1, 21):            # 20 distinct days, above the 14-day cap
        lines.append(f"2026-09-{day:02d}T10:00:00.000Z loaded project=opencode")
        lines.append(f"2026-09-{day:02d}T10:00:01.000Z injected project=opencode len=9")
    lines.append("2026-09-05T10:00:02.000Z injected len=9")     # unprojected
    lines.append("notatimestamp injected project=opencode len=1")  # undated
    root = add_activity_files(tmp_path, trace=lines)
    t = cm.claude_mem_activity(store=cm.claude_mem_store(str(root / "claude-mem.db")))["trace"]
    assert t["injected"] == 22 and t["loaded"] == 20, t
    assert len(t["by_day"]) == 14, sorted(t["by_day"])
    assert t["older_days"]["days"] == 6, t["older_days"]
    assert sum(v["injected"] for v in t["by_day"].values()) \
        + t["older_days"]["injected"] + t["undated"]["injected"] == t["injected"], t["by_day"]
    assert sum(v["loaded"] for v in t["by_day"].values()) \
        + t["older_days"]["loaded"] + t["undated"]["loaded"] == t["loaded"], t["by_day"]
    assert sum(v["injected"] for v in t["by_project"].values()) \
        + t["projectless"]["injected"] == t["injected"], t["by_project"]
    assert sum(v["loaded"] for v in t["by_project"].values()) \
        + t["projectless"]["loaded"] == t["loaded"], t["by_project"]
    # The kept window is the most recent days, not the first 14 read.
    assert max(t["by_day"]) == "2026-09-20", sorted(t["by_day"])


def test_the_grouping_still_never_reads_the_query_text(tmp_path, tracker_db):
    """Grouping added two new loops over the same lines; `q=` is still off limits."""
    root = add_activity_files(tmp_path)     # TRACE_LINES carries SENTINEL in q=
    a = cm.claude_mem_activity(store=cm.claude_mem_store(str(root / "claude-mem.db")))
    blob = json.dumps(a, ensure_ascii=False)
    assert SENTINEL not in blob, blob[:400]
    assert a["trace"]["injected_with_query"] >= 1, a["trace"]


def test_cli_prints_the_project_and_day_groups(tmp_path, tracker_db, monkeypatch, capsys):
    """The grouping has to reach the human, not just the dict."""
    root = add_activity_files(tmp_path)
    store = str(root / "claude-mem.db")
    monkeypatch.setenv(cm.CLAUDE_MEM_DB_ENV, store)
    args = st.Args()
    args.db, args.json, args.days = store, False, 7
    assert st._cli_claude_mem(tracker_db, args) == 0
    out = capsys.readouterr().out
    assert "by project" in out, out
    assert "opencode 3/1" in out, out          # the fixture's TRACE_LINES split
    assert "by day" in out, out
    assert "[injected, local days]" in out, out

    # Two-sided: with no trace file the lines must be gone, so the assertions above
    # are not satisfied by an unconditional print.
    gone = str(tmp_path / "empty-cm" / "claude-mem.db")
    (tmp_path / "empty-cm").mkdir()
    monkeypatch.setenv(cm.CLAUDE_MEM_DB_ENV, gone)
    args.db = gone
    assert st._cli_claude_mem(tracker_db, args) == 0
    quiet = capsys.readouterr().out
    assert "by project" not in quiet and "by day" not in quiet, quiet


# --- the whitelist is the contract ----------------------------------------
def test_the_health_whitelist_names_no_prose_field():
    """Pinned as a set, so widening it is a decision and not an accident.

    `observer-health.json` also holds `lastErrorMessage`, `lastErrorUrl` and
    `lastErrorRequestId`; a future "just show me the error" edit would put the
    user's text (and a third-party URL) straight onto the terminal.
    """
    assert cm.CLAUDE_MEM_HEALTH_KEYS == (
        "consecutiveFailures", "failingSinceAt", "lastErrorAt",
        "lastErrorCode", "lastErrorKind", "lastSuccessAt", "quotaCooldown",
    ), cm.CLAUDE_MEM_HEALTH_KEYS
    assert "installId" not in cm.CLAUDE_MEM_BACKFILL_KEYS
    assert "installId" not in cm.CLAUDE_MEM_TELEMETRY_KEYS
    assert cm.CLAUDE_MEM_FORBIDDEN_FILES == ("settings.json",)


def test_no_prose_column_is_named_anywhere_in_the_reader():
    """Privacy by construction, checked against the source.

    A sentinel test only catches text the fixture happens to contain. This one
    reads the reader's own module (`skill_db_claude_mem.py` — the section moved out
    of `skill_db.py` so the file being scanned *is* the reader, no slicing needed)
    and fails if any prose column is named in code. Comments are stripped first,
    because the block comment above the whitelist is precisely a list of what is
    not read, and that is worth keeping readable.
    """
    src = pathlib.Path(cm.__file__).read_text(encoding="utf-8")
    section = src
    lines = [line for line in section.splitlines()
             if not line.lstrip().startswith("#")
             # The guard itself names the file it exists to keep shut.
             and "FORBIDDEN_FILES" not in line
             # `import urllib.request as urlreq` — the alias exists *because* this
             # scan bans the bare word, and the module needs the stdlib client. It
             # names no field and reads no text; exempting the line, not the word,
             # keeps the scan's teeth.
             and not line.startswith("import urllib.request")]
    code = "\n".join(lines)
    banned = (
        "prompt_text", "lastErrorMessage", "lastErrorUrl", "lastErrorRequestId",
        "narrative", "tool_input", "tool_response", "user_prompt", "custom_title",
        "last_user_message", "last_assistant_message", "installId", "settings.json",
        "facts", "next_steps", "request", "learned",
    )
    # Word boundaries matter: `request` is a substring of the `requested` key this
    # module legitimately returns, and a scan that cries wolf there gets ignored.
    hits = [word for word in banned if re.search(r"\b" + re.escape(word) + r"\b", code)]
    assert not hits, f"prose fields named in the reader's code: {hits}"
    # `title` and `text` are too short to scan safely; the sentinel tests cover
    # those, and the columns actually selected are pinned right here.
    selected = {c for spec in cm.CLAUDE_MEM_TABLES.values()
                for c in (spec.get("time"), spec.get("label"), spec.get("tokens")) if c}
    assert selected <= {
        "created_at_epoch", "started_at_epoch", "type", "status", "tool_name",
        "discovery_tokens",
    }, selected


# --- a neighbour that changes under us ------------------------------------
def test_a_missing_table_is_reported_and_not_fatal(tmp_path, tracker_db):
    path = make_store(tmp_path, drop_table="tool_uses")
    s = cm.claude_mem_summary(tracker_db, db_path=path)
    assert s["available"] is True
    assert s["tables"]["tool_uses"].get("missing") is True
    assert s["tables"]["observations"]["n"] == 1, "one absent table must not stop the rest"


def test_a_renamed_column_degrades_instead_of_crashing(tmp_path, tracker_db):
    """M21's shape: a field upstream stops existing, a number goes blank."""
    path = make_store(tmp_path, drop_column=("observations", "discovery_tokens"))
    s = cm.claude_mem_summary(tracker_db, db_path=path)
    obs = s["tables"]["observations"]
    assert obs["n"] == 1 and obs["tokens"] is None
    assert obs["by"] == {"discovery": 1}, "the surviving columns still work"


def test_a_corrupt_store_is_reported_not_raised(tmp_path, tracker_db):
    path = make_store(tmp_path)
    with open(path, "wb") as f:
        f.write(b"not a database at all")
    s = cm.claude_mem_summary(tracker_db, db_path=path)
    assert s["available"] is False and s["reason"]


# --- doctor ----------------------------------------------------------------
class Args(st.Args):
    """A stand-in `skillt doctor` line, subclassed so the log bounds are the
    production defaults rather than a second copy that can drift from them."""

    def __init__(self, db_path):
        self.db = db_path
        self.json = False
        self.limit = 10
        self.freshness_days = 7


def _doctor(tmp_path, tracker_db, monkeypatch, store=None):
    monkeypatch.setattr(db, "SKILLS_DIR", str(tmp_path / "skills"))
    monkeypatch.setattr(st, "_opencode_version", lambda: "1.18.34\n")
    if store is None:
        monkeypatch.delenv(cm.CLAUDE_MEM_DB_ENV, raising=False)
        monkeypatch.delenv(cm.CLAUDE_MEM_DIR_ENV, raising=False)
        monkeypatch.setattr(db, "HOME", str(tmp_path / "no-home"))
    else:
        monkeypatch.setenv(cm.CLAUDE_MEM_DB_ENV, store)
    names = {n: (s, d) for n, s, d in st._doctor_checks(tracker_db, Args(str(tmp_path / "t.db")))}
    return names


def test_doctor_is_silent_when_the_plugin_is_not_installed(tmp_path, tracker_db, monkeypatch):
    """A WARN about a store the user never installed is noise that trains them to
    skip the report, so the check adds no line at all."""
    names = _doctor(tmp_path, tracker_db, monkeypatch)
    assert "claude_mem.capture" not in names, sorted(names)


def test_doctor_passes_then_warns_as_the_neighbour_ages(tmp_path, tracker_db, monkeypatch):
    fresh = make_store(tmp_path)
    names = _doctor(tmp_path, tracker_db, monkeypatch, store=fresh)
    status, detail = names["claude_mem.capture"]
    assert status == "PASS", detail
    assert "observations 1" in detail, detail

    stale = make_store(tmp_path, subdir="stale", stale_ms=40 * 86400 * 1000)
    names = _doctor(tmp_path, tracker_db, monkeypatch, store=stale)
    status, detail = names["claude_mem.capture"]
    assert status == "WARN" and "40.0d" in detail, detail


def test_doctor_warns_on_the_observer_failure_count(tmp_path, tracker_db, monkeypatch):
    """Stale and failing are different stories: the worker can be alive and
    erroring on every attempt, which no freshness number would show."""
    path = make_store(tmp_path)
    health = os.path.join(os.path.dirname(path), "observer-health.json")
    with open(health, encoding="utf-8") as f:
        data = json.load(f)
    data["consecutiveFailures"] = 4
    with open(health, "w", encoding="utf-8") as f:
        json.dump(data, f)
    names = _doctor(tmp_path, tracker_db, monkeypatch, store=path)
    status, detail = names["claude_mem.capture"]
    assert status == "WARN", detail
    assert "4 consecutive observer failures" in detail, detail
