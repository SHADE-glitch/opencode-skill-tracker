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

import pytest
from conftest import load_module

import skill_db as db

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


def test_the_suite_never_sees_the_developers_own_store():
    """The guard for the guard: conftest pins the neighbour's location.

    Without it this file would pass on a machine that has claude-mem installed
    and mean nothing on one that does not — the same class of hole the skills
    directory used to have.
    """
    store = db.claude_mem_store()
    assert store["db"] is None, store
    assert store["requested"].endswith("no-claude-mem.db"), store["requested"]


# --- resolution ------------------------------------------------------------
def test_store_resolution_prefers_explicit_then_env(tmp_path, monkeypatch):
    monkeypatch.delenv(db.CLAUDE_MEM_DB_ENV, raising=False)
    monkeypatch.delenv(db.CLAUDE_MEM_DIR_ENV, raising=False)
    real = make_store(tmp_path, subdir="cm")
    via_env = make_store(tmp_path, subdir="env-cm")
    via_dir = make_store(tmp_path, subdir="dir-cm")

    assert db.claude_mem_store(real)["db"] == real
    monkeypatch.setenv(db.CLAUDE_MEM_DB_ENV, via_env)
    assert db.claude_mem_store(real)["db"] == real, "the explicit argument wins"
    assert db.claude_mem_store()["db"] == via_env
    monkeypatch.delenv(db.CLAUDE_MEM_DB_ENV)
    monkeypatch.setenv(db.CLAUDE_MEM_DIR_ENV, str(tmp_path / "dir-cm"))
    assert db.claude_mem_store()["db"] == via_dir
    monkeypatch.delenv(db.CLAUDE_MEM_DIR_ENV)
    # With no env at all the well-known default is what makes it discoverable —
    # unlike the advisor, whose location is a project choice and must be told.
    monkeypatch.setattr(db, "HOME", str(tmp_path / "no-home"))
    assert db.claude_mem_store()["requested"] == str(
        tmp_path / "no-home" / ".claude-mem" / "claude-mem.db")


def test_a_missing_store_is_reported_and_never_created(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "HOME", str(tmp_path))
    gone = str(tmp_path / "nowhere" / "claude-mem.db")
    res = db.claude_mem_store(gone)
    assert res["db"] is None and "no claude-mem database" in res["reason"]
    assert not os.path.exists(gone), "a reader must not create a foreign store"

    summary = db.claude_mem_summary(None, db_path=gone)
    assert summary["available"] is False and summary["reason"]


# --- the summary itself ----------------------------------------------------
def test_summary_counts_labels_and_tokens_without_reading_a_word(tmp_path, tracker_db):
    path = make_store(tmp_path, extra_observations=2)
    s = db.claude_mem_summary(tracker_db, db_path=path, days=7)
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
    assert s["newest"] and db.claude_mem_age_days(s) < 1


def test_the_tracker_join_counts_the_same_window(tmp_path, tracker_db):
    path = make_store(tmp_path)
    s = db.claude_mem_summary(tracker_db, db_path=path, days=7)
    assert s["tracker_same_window"] == {
        "skill_usage": 1, "mcp_usage": 0, "plugin_usage": 0,
    }, s["tracker_same_window"]
    # A window that excludes everything must say so, not invent a number.
    old = make_store(tmp_path, subdir="old", stale_ms=40 * 86400 * 1000)
    s2 = db.claude_mem_summary(tracker_db, db_path=old, days=7)
    assert s2["tables"]["observations"]["n_window"] == 0
    assert s2["tables"]["observations"]["n"] == 1


def test_no_tracker_connection_still_reads_the_neighbour(tmp_path):
    path = make_store(tmp_path)
    s = db.claude_mem_summary(None, db_path=path)
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
    s = db.claude_mem_summary(tracker_db, db_path=path, days=7)
    assert SENTINEL not in json.dumps(s, ensure_ascii=False)
    assert SECRET not in json.dumps(s, ensure_ascii=False)

    # The CLI resolves the store the way a user does — from the environment — so
    # that is what the test drives. Passing a path in by hand would skip the only
    # code path the command actually has.
    monkeypatch.setenv(db.CLAUDE_MEM_DB_ENV, path)
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
    s = db.claude_mem_summary(tracker_db, db_path=path)
    assert s["available"] is True
    assert "settings.json" in db.CLAUDE_MEM_FORBIDDEN_FILES


def test_a_label_that_is_not_a_category_name_is_counted_not_echoed(tmp_path, tracker_db):
    path = make_store(tmp_path, weird_label=True)
    s = db.claude_mem_summary(tracker_db, db_path=path)
    by = s["tables"]["observations"]["by"]
    assert SENTINEL not in " ".join(by), by
    assert by.get("(not shown)") == 1, by
    assert s["tables"]["observations"]["n"] == 2


def test_epoch_units_are_validated_not_guessed(tmp_path, tracker_db):
    """A store that switches to seconds must show nothing, not 1970."""
    path = make_store(tmp_path, seconds_epoch=True)
    s = db.claude_mem_summary(tracker_db, db_path=path)
    assert s["tables"]["observations"]["last"] is None
    assert s["newest"] is None and db.claude_mem_age_days(s) is None


# --- the whitelist is the contract ----------------------------------------
def test_the_health_whitelist_names_no_prose_field():
    """Pinned as a set, so widening it is a decision and not an accident.

    `observer-health.json` also holds `lastErrorMessage`, `lastErrorUrl` and
    `lastErrorRequestId`; a future "just show me the error" edit would put the
    user's text (and a third-party URL) straight onto the terminal.
    """
    assert db.CLAUDE_MEM_HEALTH_KEYS == (
        "consecutiveFailures", "failingSinceAt", "lastErrorAt",
        "lastErrorCode", "lastErrorKind", "lastSuccessAt", "quotaCooldown",
    ), db.CLAUDE_MEM_HEALTH_KEYS
    assert "installId" not in db.CLAUDE_MEM_BACKFILL_KEYS
    assert "installId" not in db.CLAUDE_MEM_TELEMETRY_KEYS
    assert db.CLAUDE_MEM_FORBIDDEN_FILES == ("settings.json",)


def test_no_prose_column_is_named_anywhere_in_the_reader():
    """Privacy by construction, checked against the source.

    A sentinel test only catches text the fixture happens to contain. This one
    reads the claude-mem section of `skill_db.py` and fails if any prose column is
    named in code — comments are stripped first, because the block comment above
    the whitelist is precisely a list of what is not read, and that is worth
    keeping readable.
    """
    src = pathlib.Path(db.__file__).read_text(encoding="utf-8")
    section = src[src.index("# claude-mem — read-only neighbour"):]
    lines = [line for line in section.splitlines()
             if not line.lstrip().startswith("#")
             # The guard itself names the file it exists to keep shut.
             and "FORBIDDEN_FILES" not in line]
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
    selected = {c for spec in db.CLAUDE_MEM_TABLES.values()
                for c in (spec.get("time"), spec.get("label"), spec.get("tokens")) if c}
    assert selected <= {
        "created_at_epoch", "started_at_epoch", "type", "status", "tool_name",
        "discovery_tokens",
    }, selected


# --- a neighbour that changes under us ------------------------------------
def test_a_missing_table_is_reported_and_not_fatal(tmp_path, tracker_db):
    path = make_store(tmp_path, drop_table="tool_uses")
    s = db.claude_mem_summary(tracker_db, db_path=path)
    assert s["available"] is True
    assert s["tables"]["tool_uses"].get("missing") is True
    assert s["tables"]["observations"]["n"] == 1, "one absent table must not stop the rest"


def test_a_renamed_column_degrades_instead_of_crashing(tmp_path, tracker_db):
    """M21's shape: a field upstream stops existing, a number goes blank."""
    path = make_store(tmp_path, drop_column=("observations", "discovery_tokens"))
    s = db.claude_mem_summary(tracker_db, db_path=path)
    obs = s["tables"]["observations"]
    assert obs["n"] == 1 and obs["tokens"] is None
    assert obs["by"] == {"discovery": 1}, "the surviving columns still work"


def test_a_corrupt_store_is_reported_not_raised(tmp_path, tracker_db):
    path = make_store(tmp_path)
    with open(path, "wb") as f:
        f.write(b"not a database at all")
    s = db.claude_mem_summary(tracker_db, db_path=path)
    assert s["available"] is False and s["reason"]


# --- doctor ----------------------------------------------------------------
class Args:
    def __init__(self, db_path):
        self.db = db_path
        self.json = False
        self.limit = 10
        self.freshness_days = 7


def _doctor(tmp_path, tracker_db, monkeypatch, store=None):
    monkeypatch.setattr(db, "SKILLS_DIR", str(tmp_path / "skills"))
    monkeypatch.setattr(st, "_opencode_version", lambda: "1.18.34\n")
    if store is None:
        monkeypatch.delenv(db.CLAUDE_MEM_DB_ENV, raising=False)
        monkeypatch.delenv(db.CLAUDE_MEM_DIR_ENV, raising=False)
        monkeypatch.setattr(db, "HOME", str(tmp_path / "no-home"))
    else:
        monkeypatch.setenv(db.CLAUDE_MEM_DB_ENV, store)
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
