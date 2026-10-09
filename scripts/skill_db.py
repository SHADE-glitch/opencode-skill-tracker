"""
skill_db.py — shared data layer for the OpenCode skill tracker.

Used by both `skill-stats.py` (legacy CLI, unchanged) and `skill-tui.py` (new TUI).
Owns: schema, idempotent migration, queries, formatters, hashing/sync, insight,
export, and the write helpers used by the TUI's data-management page.

Safety contract:
  * The OpenCode plugin `plugin/skill-tracker.js` owns the base schema. We only
    ADD columns/tables idempotently, and rebuild views when `SCHEMA_VERSION`
    changes.
  * The plugin's `skills` upsert writes only name/category/path/description, so
    `content_hash` survives every plugin scan.
  * Never log or export secrets. `skill_usage.metadata` is sanitized by the
    plugin before it is written; we pass it through untouched. The views extract
    only model/agent/branch — never the message body or title, which the plugin
    no longer reads at all.
"""

from __future__ import annotations

import contextlib
import hashlib
import http.client
import json
import os
import re
import sqlite3
import time
import urllib.request as urlreq   # aliased: the prose-field tripwire bans the bare word
from datetime import datetime, timedelta, timezone

import opencode_compat as compat

HOME = os.path.expanduser("~")
# The directories belong to OpenCode; the filenames inside them are ours. The
# directory names come from `opencode_compat` so a host layout change has one
# place to edit.
DATA_DIR = os.path.join(HOME, *compat.DATA_HOME)
# The writer resolves its config tree through the same variable, so the readers
# must too: with the override in force and only one side honouring it, a relocated
# config records skills that the TUI then scans for in the default tree, and the
# Skills page reads as "none installed".
CONFIG_DIR = os.environ.get(
    "OPENCODE_SKILL_TRACKER_CONFIG_DIR", os.path.join(HOME, *compat.CONFIG_HOME))
# The defaults are named so a test can assert *where the default came from*
# without depending on whether an environment override is in force (the suite
# runs both ways on purpose).
DB_PATH_DEFAULT = os.path.join(DATA_DIR, "skill-usage.db")
SKILLS_DIR_DEFAULT = os.path.join(CONFIG_DIR, compat.SKILLS_SUBDIR)
BACKUP_DIR_DEFAULT = os.path.join(DATA_DIR, "backups")
DB_PATH = os.environ.get("OPENCODE_SKILL_TRACKER_DB", DB_PATH_DEFAULT)
SKILLS_DIR = os.environ.get("OPENCODE_SKILL_TRACKER_SKILLS_DIR", SKILLS_DIR_DEFAULT)
BUSY_TIMEOUT_MS = 5000

# Dedicated directory for retained backups. Kept separate from the DB's own
# directory so the retention sweep can never touch an unrelated .db file.
BACKUP_DIR = os.environ.get("OPENCODE_SKILL_TRACKER_BACKUP_DIR", BACKUP_DIR_DEFAULT)

# Only files matching this exact name shape are ever considered for deletion.
# Anything else (hand-made copies, the live DB, exports) is left alone.
BACKUP_RE = re.compile(r"^skill-usage-backup-(\d{8})-(\d{6})\.db$")

DAILY_KEEP = 30
MONTHLY_KEEP = 12
MIN_BACKUP_AGE_S = 120  # never delete a file younger than this

# Source is derived, never stored (the user explicitly asked for no duplicated
# storage). Both renderings come from `compat.SOURCE_BY_CATEGORY`: this SQL for
# the queries, `source_of` below for Python callers.
SOURCE_CASE_SQL = compat.source_case_sql()

# Base DDL — verbatim copy of the plugin's schema, for fresh DBs and tests.
# Tables and views are kept as separate blobs so the views can be rebuilt on a
# version bump (see ensure_schema) and diffed against the plugin's copy by
# tests/test_schema_sync.py.
TABLES_SQL = """
CREATE TABLE IF NOT EXISTS skills (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  name        TEXT    NOT NULL,
  category    TEXT,
  path        TEXT    NOT NULL UNIQUE,
  description TEXT,
  created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_skills_name     ON skills(name);
CREATE INDEX IF NOT EXISTS idx_skills_category ON skills(category);

CREATE TABLE IF NOT EXISTS skill_usage (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  skill_id     INTEGER REFERENCES skills(id) ON DELETE SET NULL,
  skill_name   TEXT    NOT NULL,
  session_id   TEXT,
  project_path TEXT,
  trigger_type TEXT    NOT NULL
               CHECK (trigger_type IN ('tool_call','event_detected','permission_denied','manual')),
  status       TEXT    NOT NULL DEFAULT 'unknown'
               CHECK (status IN ('success','error','denied','ask','unknown')),
  timestamp    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  duration_ms  INTEGER,
  call_id      TEXT,
  metadata     TEXT,
  UNIQUE (session_id, call_id)
);
CREATE INDEX IF NOT EXISTS idx_usage_skill   ON skill_usage(skill_name);
CREATE INDEX IF NOT EXISTS idx_usage_ts      ON skill_usage(timestamp);
CREATE INDEX IF NOT EXISTS idx_usage_session ON skill_usage(session_id);
CREATE INDEX IF NOT EXISTS idx_usage_trigger ON skill_usage(trigger_type);

CREATE TABLE IF NOT EXISTS mcp_usage (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  server_name  TEXT    NOT NULL,
  tool_name    TEXT    NOT NULL,
  session_id   TEXT,
  project_path TEXT,
  trigger_type TEXT    NOT NULL
               CHECK (trigger_type IN ('tool_call','event_detected','permission_denied','manual')),
  status       TEXT    NOT NULL DEFAULT 'unknown'
               CHECK (status IN ('success','error','denied','ask','unknown')),
  timestamp    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  duration_ms  INTEGER,
  call_id      TEXT,
  arg_names    TEXT,
  metadata     TEXT,
  UNIQUE (session_id, call_id)
);
CREATE INDEX IF NOT EXISTS idx_mcp_server  ON mcp_usage(server_name);
CREATE INDEX IF NOT EXISTS idx_mcp_tool    ON mcp_usage(tool_name);
CREATE INDEX IF NOT EXISTS idx_mcp_ts      ON mcp_usage(timestamp);
CREATE INDEX IF NOT EXISTS idx_mcp_session ON mcp_usage(session_id);
CREATE INDEX IF NOT EXISTS idx_mcp_trigger ON mcp_usage(trigger_type);

CREATE TABLE IF NOT EXISTS plugin_usage (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  plugin_name  TEXT    NOT NULL,
  kind         TEXT    NOT NULL CHECK (kind IN ('tool','command')),
  item_name    TEXT    NOT NULL,
  session_id   TEXT,
  project_path TEXT,
  trigger_type TEXT    NOT NULL
               CHECK (trigger_type IN ('tool_call','command_call','event_detected','permission_denied','manual')),
  status       TEXT    NOT NULL DEFAULT 'unknown'
               CHECK (status IN ('success','error','denied','ask','unknown')),
  timestamp    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  duration_ms  INTEGER,
  call_id      TEXT,
  metadata     TEXT,
  UNIQUE (session_id, call_id)
);
CREATE INDEX IF NOT EXISTS idx_plugin_name    ON plugin_usage(plugin_name);
CREATE INDEX IF NOT EXISTS idx_plugin_item    ON plugin_usage(item_name);
CREATE INDEX IF NOT EXISTS idx_plugin_ts      ON plugin_usage(timestamp);
CREATE INDEX IF NOT EXISTS idx_plugin_session ON plugin_usage(session_id);
CREATE INDEX IF NOT EXISTS idx_plugin_trigger ON plugin_usage(trigger_type);

CREATE TABLE IF NOT EXISTS plugin_inventory (
  plugin_name TEXT PRIMARY KEY,
  version     TEXT,
  source      TEXT,
  skipped     INTEGER NOT NULL DEFAULT 0,
  tools       TEXT,
  commands    TEXT,
  scope       TEXT,
  first_seen  TEXT NOT NULL,
  last_seen   TEXT NOT NULL
);

-- One row per subagent the host started through the builtin task tool. It is
-- not a fourth usage stream: the columns are identifiers only, because the same
-- payload carries the task text and the subagent's report. Keyed by the *parent*
-- session and the call, so a spawn that is seen twice (hook plus event) is one row.
CREATE TABLE IF NOT EXISTS subagent_usage (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  parent_session_id TEXT    NOT NULL,
  child_session_id  TEXT,
  call_id           TEXT,
  subagent          TEXT,
  project_path      TEXT,
  trigger_type      TEXT    NOT NULL
                    CHECK (trigger_type IN ('tool_call','event_detected','manual')),
  status            TEXT    NOT NULL DEFAULT 'unknown'
                    CHECK (status IN ('success','error','ask','unknown')),
  timestamp         TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  duration_ms       INTEGER,
  metadata          TEXT,
  UNIQUE (parent_session_id, call_id)
);
CREATE INDEX IF NOT EXISTS idx_subagent_name    ON subagent_usage(subagent);
CREATE INDEX IF NOT EXISTS idx_subagent_ts      ON subagent_usage(timestamp);
CREATE INDEX IF NOT EXISTS idx_subagent_session ON subagent_usage(parent_session_id);
"""

VIEWS_SQL = """
CREATE VIEW IF NOT EXISTS v_skill_totals AS
SELECT skill_name,
       COUNT(*)              AS total,
       SUM(status='success') AS success,
       SUM(status='error')   AS errors,
       SUM(status='denied')  AS denied,
       MAX(timestamp)        AS last_used
FROM skill_usage GROUP BY skill_name;

CREATE VIEW IF NOT EXISTS v_skill_last30 AS
SELECT skill_name, COUNT(*) AS uses_30d, MAX(timestamp) AS last_used_30d
FROM skill_usage
WHERE timestamp >= strftime('%Y-%m-%dT%H:%M:%fZ','now','-30 days')
GROUP BY skill_name;

CREATE VIEW IF NOT EXISTS v_skill_history AS
SELECT u.id, u.skill_name, u.timestamp, u.project_path, u.session_id,
       u.status, u.duration_ms, u.trigger_type,
       json_extract(u.metadata,'$.model')   AS model,
       json_extract(u.metadata,'$.agent')   AS agent,
       json_extract(u.metadata,'$.branch')  AS branch
FROM skill_usage u
ORDER BY u.timestamp DESC;

CREATE VIEW IF NOT EXISTS v_mcp_totals AS
SELECT server_name,
       tool_name,
       COUNT(*)              AS total,
       SUM(status='success') AS success,
       SUM(status='error')   AS errors,
       SUM(status='denied')  AS denied,
       MAX(timestamp)        AS last_used
FROM mcp_usage GROUP BY server_name, tool_name;

CREATE VIEW IF NOT EXISTS v_mcp_last30 AS
SELECT server_name, tool_name, COUNT(*) AS uses_30d, MAX(timestamp) AS last_used_30d
FROM mcp_usage
WHERE timestamp >= strftime('%Y-%m-%dT%H:%M:%fZ','now','-30 days')
GROUP BY server_name, tool_name;

CREATE VIEW IF NOT EXISTS v_mcp_history AS
SELECT u.id, u.server_name, u.tool_name, u.timestamp, u.project_path, u.session_id,
       u.status, u.duration_ms, u.trigger_type, u.arg_names,
       json_extract(u.metadata,'$.model')   AS model,
       json_extract(u.metadata,'$.agent')   AS agent,
       json_extract(u.metadata,'$.branch')  AS branch
FROM mcp_usage u
ORDER BY u.timestamp DESC;

CREATE VIEW IF NOT EXISTS v_plugin_totals AS
SELECT plugin_name,
       kind,
       item_name,
       COUNT(*)              AS total,
       SUM(status='success') AS success,
       SUM(status='error')   AS errors,
       SUM(status='denied')  AS denied,
       MAX(timestamp)        AS last_used
FROM plugin_usage GROUP BY plugin_name, kind, item_name;

CREATE VIEW IF NOT EXISTS v_plugin_last30 AS
SELECT plugin_name, kind, item_name, COUNT(*) AS uses_30d, MAX(timestamp) AS last_used_30d
FROM plugin_usage
WHERE timestamp >= strftime('%Y-%m-%dT%H:%M:%fZ','now','-30 days')
GROUP BY plugin_name, kind, item_name;

CREATE VIEW IF NOT EXISTS v_plugin_history AS
SELECT u.id, u.plugin_name, u.kind, u.item_name, u.timestamp, u.project_path, u.session_id,
       u.status, u.duration_ms, u.trigger_type,
       json_extract(u.metadata,'$.model')   AS model,
       json_extract(u.metadata,'$.agent')   AS agent,
       json_extract(u.metadata,'$.branch')  AS branch
FROM plugin_usage u
ORDER BY u.timestamp DESC;
"""

SCHEMA_SQL = TABLES_SQL + VIEWS_SQL  # one blob for fresh DBs and the test fixtures

# Every view we own, so ensure_schema can drop and rebuild them on a bump.
VIEW_NAMES = (
    "v_skill_totals",
    "v_skill_last30",
    "v_skill_history",
    "v_mcp_totals",
    "v_mcp_last30",
    "v_mcp_history",
    "v_plugin_totals",
    "v_plugin_last30",
    "v_plugin_history",
)

# Bump when any DDL above changes. `CREATE VIEW IF NOT EXISTS` alone would keep
# an old definition forever, so ensure_schema rebuilds the views whenever the
# DB's recorded version is behind.
SCHEMA_VERSION = 2

# Migration-only DDL (new in this tool).
VERSIONS_SQL = """
CREATE TABLE IF NOT EXISTS skill_versions (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  skill_id     INTEGER REFERENCES skills(id) ON DELETE SET NULL,
  skill_name   TEXT    NOT NULL,
  content_hash TEXT    NOT NULL,
  size_bytes   INTEGER,
  description  TEXT,
  recorded_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  UNIQUE (skill_name, content_hash)
);
CREATE INDEX IF NOT EXISTS idx_skill_versions_name     ON skill_versions(skill_name);
CREATE INDEX IF NOT EXISTS idx_skill_versions_recorded ON skill_versions(recorded_at);
"""


# ---------------------------------------------------------------------------
# Connection & formatters
# ---------------------------------------------------------------------------
def open_db(path: str = DB_PATH, readonly: bool = True) -> sqlite3.Connection:
    """Open the DB. Falls back to read-write if a read-only open fails (WAL)."""
    if readonly and os.path.exists(path):
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
            return conn
        except sqlite3.Error:
            pass  # e.g. WAL needs -shm write access; fall through
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    return conn


def fmt_time(ts) -> str:
    """Stored UTC '...Z' -> local 'YYYY-MM-DD HH:MM'."""
    if not ts:
        return "-"
    try:
        t = str(ts).replace("Z", "+00:00")
        dt = datetime.fromisoformat(t)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return str(ts)[:16].replace("T", " ")


def short_session(sid) -> str:
    return (str(sid)[:8] + "…") if sid and len(str(sid)) > 8 else (str(sid) if sid else "-")


def short_path(p, limit: int = 40) -> str:
    if not p:
        return "-"
    p = str(p)
    if p.startswith(HOME):
        p = "~" + p[len(HOME):]
    return p if len(p) <= limit else "…" + p[-(limit - 1):]


def rows_to_dicts(rows):
    return [dict(r) for r in rows]


def source_of(category) -> str:
    """The Python half of `SOURCE_BY_CATEGORY` — the SQL half is `SOURCE_CASE_SQL`."""
    return compat.source_of(category)


def success_rate(total, success) -> float | None:
    if not total:
        return None
    return round(float(success) / float(total), 3)


def iso_utc(dt: datetime) -> str:
    """Format like SQLite's strftime('%Y-%m-%dT%H:%M:%fZ', ...) — ms precision."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def local_day_bounds(days_back: int = 0) -> tuple[str, str]:
    """UTC ISO [start, end) of a local calendar day, `days_back` days ago.

    Timestamps are stored as UTC strings, so "today" for a local calendar means
    a UTC instant range. Expressing the filter as a range on `timestamp` lets
    SQLite use idx_usage_ts; the old `date(timestamp,'localtime') = ...`
    comparison forced a full scan of every usage table.
    """
    midnight = (
        datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
        - timedelta(days=days_back)
    )
    start = midnight.astimezone(timezone.utc)
    return iso_utc(start), iso_utc(start + timedelta(days=1))


# ---------------------------------------------------------------------------
# Schema + idempotent migration
# ---------------------------------------------------------------------------
def _column_exists(conn, table: str, col: str) -> bool:
    return any(r[1] == col for r in conn.execute(f"PRAGMA table_info({table})"))


def has_content_hash(conn) -> bool:
    try:
        return _column_exists(conn, "skills", "content_hash")
    except sqlite3.Error:
        return False


def ensure_schema(conn) -> dict:
    """Create base schema, then apply the additive migration. Idempotent.

    Also puts the DB into WAL mode with a busy timeout, matching the plugin, so
    our writer and the plugin's bun:sqlite writer can coexist.
    """
    result = {
        "created_base": False,
        "added_content_hash": False,
        "added_inventory_scope": False,
        "created_versions": False,
        "created_mcp": False,
        "created_plugin": False,
        "rebuilt_views": False,
    }
    # busy_timeout first: switching journal_mode needs a lock, and with no
    # timeout set the switch fails immediately on a busy DB — silently leaving
    # the file in rollback-journal mode for the rest of the session.
    for pragma in (f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}",
                   "PRAGMA journal_mode = WAL",
                   "PRAGMA synchronous = NORMAL"):
        try:
            conn.execute(pragma)
        except sqlite3.Error:
            pass

    # Sampled before the DDL runs, which is what actually creates these
    # objects. Unlike `created_versions` these are truthful signals.
    mcp_existed = _table_exists(conn, "mcp_usage")
    plugin_existed = _table_exists(conn, "plugin_usage")

    conn.executescript(TABLES_SQL)
    result["created_mcp"] = not mcp_existed
    result["created_plugin"] = not plugin_existed

    if not _column_exists(conn, "skills", "content_hash"):
        try:
            conn.execute("ALTER TABLE skills ADD COLUMN content_hash TEXT")
            result["added_content_hash"] = True
        except sqlite3.OperationalError as e:
            # Race with a second migrator (e.g. TUI + CLI starting together).
            if "duplicate column name" not in str(e).lower():
                raise

    # `scope` is how the plugin knows which rows it may delete at the next init
    # (see SCOPE_* in the plugin). The plugin adds the same column on its side,
    # because it is often the first thing to open the DB after an upgrade.
    if not _column_exists(conn, "plugin_inventory", "scope"):
        try:
            conn.execute("ALTER TABLE plugin_inventory ADD COLUMN scope TEXT")
            result["added_inventory_scope"] = True
        except sqlite3.OperationalError as e:
            if "duplicate column name" not in str(e).lower():
                raise

    conn.executescript(VERSIONS_SQL)
    result["created_versions"] = True

    # Index on the new column is created after the column is guaranteed present.
    try:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_skills_content_hash ON skills(content_hash)")
    except sqlite3.OperationalError:
        pass

    # Views are versioned. `CREATE VIEW IF NOT EXISTS` on its own would keep an
    # old definition forever, so a changed view (or a column added to one) could
    # never reach an existing DB. Drop and rebuild whenever the recorded
    # version is behind; the plugin never reads the views, so this is safe to do
    # while it is running.
    try:
        current = conn.execute("PRAGMA user_version").fetchone()[0]
    except sqlite3.Error:
        current = 0
    if current < SCHEMA_VERSION:
        for name in VIEW_NAMES:
            conn.execute(f"DROP VIEW IF EXISTS {name}")
        result["rebuilt_views"] = True
    conn.executescript(VIEWS_SQL)
    conn.execute(f"PRAGMA user_version = {int(SCHEMA_VERSION)}")

    conn.commit()
    return result


# ---------------------------------------------------------------------------
# Skill scanning, hashing, version sync
# ---------------------------------------------------------------------------
def hash_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_frontmatter(text: str) -> dict:
    """Tolerant parser for the SKILL.md YAML frontmatter. Never raises."""
    out: dict = {}
    if not isinstance(text, str) or not text:
        return out
    src = text.replace("\ufeff", "")
    m = __import__("re").compile(r"^---\r?\n([\s\S]*?)\r?\n---(?:\r?\n|$)").search(src)
    if not m:
        return out
    lines = m.group(1).split("\n")
    i = 0
    while i < len(lines):
        line = lines[i].rstrip("\r")
        mm = __import__("re").compile(r"^([A-Za-z0-9_-]+)\s*:\s*(.*)$").match(line)
        if not mm:
            i += 1
            continue
        key, val = mm.group(1), mm.group(2).strip()
        if val in (">", "|", ">-", "|-"):
            buf = []
            while i + 1 < len(lines) and lines[i + 1][:1] in (" ", "\t"):
                i += 1
                buf.append(lines[i].strip())
            val = " ".join(buf)
        elif len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        out[key] = val
        i += 1
    return out


def walk_skill_files(root: str) -> list[str]:
    out = []
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            entries = os.scandir(d)
        except OSError:
            continue
        with entries:
            for e in entries:
                if e.is_dir(follow_symlinks=False):
                    stack.append(e.path)
                elif e.is_file() and e.name == compat.SKILL_FILE_NAME:
                    out.append(e.path)
    return sorted(out)


def scan_skills(skills_dir: str | None = None) -> list[dict]:
    """Return [{name, category, path, description, content_hash, size_bytes}].

    `skills_dir` defaults to SKILLS_DIR at call time, not at def time, so a
    caller (or a test) that repoints the module attribute gets that value.
    """
    skills_dir = SKILLS_DIR if skills_dir is None else skills_dir
    found = []
    for file in walk_skill_files(skills_dir):
        try:
            with open(file, "r", encoding="utf-8", errors="replace") as f:
                raw = f.read()
            fm = parse_frontmatter(raw)
            d = os.path.dirname(file)
            rel = os.path.relpath(d, skills_dir)
            category = compat.category_of(rel)
            found.append(
                {
                    "name": fm.get(compat.FRONTMATTER_NAME_KEY) or os.path.basename(d),
                    "category": category,
                    "path": d,
                    "description": fm.get(compat.FRONTMATTER_DESCRIPTION_KEY) or None,
                    "content_hash": hash_file(file),
                    "size_bytes": os.path.getsize(file),
                    "parsed_name": bool(fm.get(compat.FRONTMATTER_NAME_KEY)),
                }
            )
        except OSError:
            continue
    return found


def sync_versions(conn, skills_dir: str | None = None, dry_run: bool = False,
                  prune_orphans: bool = False) -> dict:
    """Hash every SKILL.md; record a skill_versions row per content change."""
    stats = {
        "scanned": 0, "baseline": 0, "changed": 0, "unchanged": 0,
        "unparsed_name": 0, "orphan_versions_pruned": 0, "dry_run": dry_run,
    }
    if not dry_run:
        # Writing needs content_hash + skill_versions to exist.
        ensure_schema(conn)
    found = scan_skills(skills_dir)
    stats["scanned"] = len(found)

    def _process(write: bool) -> None:
        for s in found:
            if not s["parsed_name"]:
                stats["unparsed_name"] += 1
            row = conn.execute(
                "SELECT id, content_hash FROM skills WHERE path = ?", (s["path"],)
            ).fetchone()
            old_hash = row["content_hash"] if row and "content_hash" in row.keys() else None
            skill_id = row["id"] if row else None

            is_baseline = old_hash is None
            is_change = old_hash is not None and old_hash != s["content_hash"]

            if is_baseline or is_change:
                stats["baseline" if is_baseline else "changed"] += 1
                if not write:
                    continue
                conn.execute(
                    """INSERT INTO skills (name, category, path, description, content_hash)
                       VALUES (?,?,?,?,?)
                       ON CONFLICT(path) DO UPDATE SET
                         name         = excluded.name,
                         category     = COALESCE(excluded.category, skills.category),
                         description  = COALESCE(excluded.description, skills.description),
                         content_hash = excluded.content_hash,
                         updated_at   = strftime('%Y-%m-%dT%H:%M:%fZ','now')""",
                    (s["name"], s["category"], s["path"], s["description"], s["content_hash"]),
                )
                if skill_id is None:
                    skill_id = conn.execute(
                        "SELECT id FROM skills WHERE path = ?", (s["path"],)
                    ).fetchone()["id"]
                conn.execute(
                    """INSERT OR IGNORE INTO skill_versions
                       (skill_id, skill_name, content_hash, size_bytes, description)
                       VALUES (?,?,?,?,?)""",
                    (skill_id, s["name"], s["content_hash"], s["size_bytes"], s["description"]),
                )
            else:
                stats["unchanged"] += 1

    if dry_run:
        _process(False)
    else:
        with _write_txn(conn):
            _process(True)
            if prune_orphans:
                cur = conn.execute(
                    "DELETE FROM skill_versions WHERE skill_name NOT IN (SELECT name FROM skills)"
                )
                stats["orphan_versions_pruned"] = cur.rowcount or 0

    return stats


# ---------------------------------------------------------------------------
# Query layer
# ---------------------------------------------------------------------------
def sample_size(conn) -> dict:
    """How much data do we actually have?

    `sessions` is the denominator that makes "0 uses" meaningful: OpenCode
    offers every discovered skill in every session, so a skill with no
    invocation after N sessions is a signal, while one with no invocation
    after 2 sessions is noise.
    """
    row = conn.execute(
        "SELECT COUNT(DISTINCT session_id)              AS sessions, "
        "       COUNT(DISTINCT date(timestamp,'localtime')) AS days_observed, "
        "       MIN(timestamp)                           AS first_usage, "
        "       MAX(timestamp)                           AS last_usage, "
        "       COUNT(*)                                 AS total_usage "
        "FROM skill_usage"
    ).fetchone()
    sessions = row["sessions"] or 0
    days = row["days_observed"] or 0
    return {
        "sessions": sessions,
        "days_observed": days,
        "total_usage": row["total_usage"] or 0,
        "first_usage": row["first_usage"],
        "last_usage": row["last_usage"],
        "enough_for_advice": (
            sessions >= MIN_SESSIONS_FOR_ADVICE and days >= MIN_DAYS_FOR_ADVICE
        ),
    }


def dashboard_summary(conn) -> dict:
    total_skills = conn.execute("SELECT COUNT(*) FROM skills").fetchone()[0]
    total_usage = conn.execute("SELECT COUNT(*) FROM skill_usage").fetchone()[0]

    # Range predicate instead of date(timestamp,'localtime') = ... so the
    # timestamp index can be used.
    today_start, today_end = local_day_bounds()
    today = conn.execute(
        "SELECT COUNT(*) FROM skill_usage WHERE timestamp >= ? AND timestamp < ?",
        (today_start, today_end),
    ).fetchone()[0]

    # MCP counters are kept separate from the skill ones — `total_usage` and
    # `today_usage` mean skills and must keep meaning that.
    total_mcp = today_mcp = 0
    if _mcp_available(conn):
        total_mcp = conn.execute("SELECT COUNT(*) FROM mcp_usage").fetchone()[0]
        today_mcp = conn.execute(
            "SELECT COUNT(*) FROM mcp_usage WHERE timestamp >= ? AND timestamp < ?",
            (today_start, today_end),
        ).fetchone()[0]

    # Plugin counters are kept separate, same contract as the MCP ones above.
    total_plugin = today_plugin = 0
    if _plugin_available(conn):
        total_plugin = conn.execute("SELECT COUNT(*) FROM plugin_usage").fetchone()[0]
        today_plugin = conn.execute(
            "SELECT COUNT(*) FROM plugin_usage WHERE timestamp >= ? AND timestamp < ?",
            (today_start, today_end),
        ).fetchone()[0]

    by_source = {
        r["src"]: r["n"]
        for r in conn.execute(
            f"SELECT {SOURCE_CASE_SQL} AS src, COUNT(*) n FROM skills s GROUP BY src"
        )
    }
    return {
        "total_skills": total_skills,
        "total_usage": total_usage,
        "today_usage": today,
        "personal": by_source.get("personal", 0),
        "open_source": by_source.get("open-source", 0),
        "total_mcp": total_mcp,
        "today_mcp": today_mcp,
        "total_plugin": total_plugin,
        "today_plugin": today_plugin,
        "today_all": today + today_mcp + today_plugin,
        "total_calls_all": total_usage + total_mcp + total_plugin,
        "sample": sample_size(conn),
    }


def _zero_filled_days(rows: dict, days: int) -> list[dict]:
    """Zip a {date: count} map onto the last `days` local days, zero-filled.

    The dates are computed in Python rather than with one `SELECT date(...)`
    per day — that was `days` extra round-trips on every refresh.
    """
    today = datetime.now().astimezone().date()
    return [
        {"date": (d := (today - timedelta(days=i)).isoformat()), "count": rows.get(d, 0)}
        for i in range(days - 1, -1, -1)
    ]


def _daily_counts(conn, table: str, days: int) -> list[dict]:
    """Shared body of the three daily_* helpers.

    The WHERE clause is a range on `timestamp` so idx_*_ts can be used; the
    GROUP BY still buckets by local calendar day, but only over the rows the
    index already narrowed down.
    """
    start, _ = local_day_bounds(days - 1)
    rows = {
        r["d"]: r["n"]
        for r in conn.execute(
            f"SELECT date(timestamp,'localtime') AS d, COUNT(*) n FROM {table} "
            "WHERE timestamp >= ? GROUP BY d",
            (start,),
        )
    }
    return _zero_filled_days(rows, days)


def daily_activity(conn, days: int = 7) -> list[dict]:
    """[{date, count}] for the last `days` days, zero-filled."""
    return _daily_counts(conn, "skill_usage", days)


def daily_mcp_activity(conn, days: int = 7) -> list[dict]:
    """[{date, count}] of MCP calls for the last `days` days, zero-filled.

    A DB that predates the mcp_usage table reports all zeros instead of
    raising — same degrade contract as the other MCP reads.
    """
    if not _mcp_available(conn):
        return _zero_filled_days({}, days)
    return _daily_counts(conn, "mcp_usage", days)


def daily_plugin_activity(conn, days: int = 7) -> list[dict]:
    """[{date, count}] of plugin calls for the last `days` days, zero-filled.

    Same pre-migration degrade contract as daily_mcp_activity.
    """
    if not _plugin_available(conn):
        return _zero_filled_days({}, days)
    return _daily_counts(conn, "plugin_usage", days)


def stats_rows(conn) -> list[dict]:
    """All skills (used and unused) with totals + derived source."""
    sql = f"""
    SELECT s.name AS skill_name, s.category, {SOURCE_CASE_SQL} AS source,
           s.path, s.description,
           COALESCE(t.total, 0)   AS total,
           COALESCE(t.success, 0) AS success,
           COALESCE(t.errors, 0)  AS errors,
           COALESCE(t.denied, 0)  AS denied,
           t.last_used            AS last_used,
           COALESCE(l.uses_30d, 0) AS uses_30d,
           COALESCE(sc.sessions, 0) AS sessions
    FROM skills s
    LEFT JOIN v_skill_totals t ON t.skill_name = s.name
    LEFT JOIN v_skill_last30 l ON l.skill_name = s.name
    LEFT JOIN (
        SELECT skill_name, COUNT(DISTINCT session_id) AS sessions
        FROM skill_usage GROUP BY skill_name
    ) sc ON sc.skill_name = s.name
    ORDER BY total DESC, s.name ASC
    """
    return rows_to_dicts(conn.execute(sql))


def top_rows(conn, limit: int = 10) -> list[dict]:
    return rows_to_dicts(
        conn.execute(
            "SELECT skill_name, total, success, errors, denied, last_used "
            "FROM v_skill_totals ORDER BY total DESC, last_used DESC LIMIT ?",
            (limit,),
        )
    )


def history_rows(conn, skill: str, limit: int = 200) -> list[dict]:
    return rows_to_dicts(
        conn.execute(
            "SELECT * FROM v_skill_history WHERE skill_name = ? "
            "ORDER BY timestamp DESC LIMIT ?",
            (skill, limit),
        )
    )


def recent_rows(conn, limit: int = 50) -> list[dict]:
    return rows_to_dicts(
        conn.execute(
            "SELECT timestamp, skill_name, project_path, session_id, status, "
            "       duration_ms, trigger_type FROM skill_usage "
            "ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        )
    )


def unified_recent_rows(conn, limit: int = 100) -> list[dict]:
    """One timeline across skills + MCP + plugins, newest first.

    Read-only. Each row: {timestamp, kind, name, project_path, session_id,
    status, duration_ms, trigger_type} plus the columns needed to reopen that
    row's detail page: `skill_name`, or `server_name` + `tool_name`, or
    `plugin_name` + `item_kind` + `item_name`. The pieces are returned as real
    columns because `name` is a display string — a plugin command is
    `conductor:newTrack` and a scoped npm plugin is `@scope/pkg@1.0`, so
    splitting `name` apart again is ambiguous by construction. `kind` is one of
    "skill" / "mcp" / "plugin". Tables that do not exist yet (unmigrated DB)
    are skipped, so a skills-only DB simply returns skill rows.

    Each source is bounded to `limit` rows *before* merging, so SQLite can walk
    the per-table `timestamp` index instead of sorting every row of every table.
    The merged set is at most 3 * limit rows. Errors are *not* swallowed: a
    source that fails to read raises rather than silently dropping to skills
    only (which would misreport the timeline as complete).
    """
    sources: list[str] = [
        "SELECT timestamp, 'skill' AS kind, skill_name AS name, project_path,"
        "       session_id, status, duration_ms, trigger_type, skill_name"
        " FROM skill_usage ORDER BY timestamp DESC LIMIT ?"
    ]
    params: list[int] = [limit]
    if _mcp_available(conn):
        sources.append(
            "SELECT timestamp, 'mcp' AS kind,"
            "       server_name || '.' || tool_name AS name, project_path,"
            "       session_id, status, duration_ms, trigger_type,"
            "       server_name, tool_name"
            " FROM mcp_usage ORDER BY timestamp DESC LIMIT ?"
        )
        params.append(limit)
    if _plugin_available(conn):
        sources.append(
            "SELECT timestamp, 'plugin' AS kind,"
            "       plugin_name || '/' || kind || '/' || item_name AS name,"
            "       project_path, session_id, status, duration_ms,"
            "       trigger_type, plugin_name, kind AS item_kind, item_name"
            " FROM plugin_usage ORDER BY timestamp DESC LIMIT ?"
        )
        params.append(limit)

    merged: list[dict] = []
    for sql, bound in zip(sources, params):
        merged.extend(rows_to_dicts(conn.execute(sql, (bound,))))
    # ISO-8601 UTC strings of fixed width sort correctly as plain text.
    merged.sort(key=lambda r: r["timestamp"] or "", reverse=True)
    return merged[:limit]


# `json_extract` does not return NULL for invalid JSON — it *raises*. Rows written
# by older builds can hold anything in `metadata` (see the scrub-metadata
# command), so every read of a metadata key goes through this.
AGENT_EXPR = ("CASE WHEN json_valid(metadata) THEN json_extract(metadata, '$.agent')"
              " END")


def _agent_name(value):
    """An agent name, or None for the `(unknown)` bucket.

    A number, a blank string or a non-string is not an agent name: it belongs to
    the unknown bucket rather than becoming a label invented out of nothing.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _agent_slot(agent):
    """One row of the Agents table. `total` is only ever the three call streams."""
    return {
        "agent": agent, "skill": 0, "mcp": 0, "plugin": 0, "total": 0,
        "success": 0, "errors": 0, "denied": 0, "last_used": None,
        "subagent": 0,            # spawns this agent started (an event, not a call)
        "runs_as": 0,             # times this name was run as somebody's subagent
        "runs_ok": 0, "runs_errors": 0,
        "is_subagent": False,     # renders as `name ⟨sub⟩` on the page
    }


def agent_usage_rows(conn) -> list[dict]:
    """Call counts grouped by the agent each usage row reported.

    Read-only. One row per agent: {agent, skill, mcp, plugin, total, success,
    errors, denied, last_used}, biggest first. `agent` is None for rows that did
    not carry one, and that bucket is returned rather than dropped — a table that
    quietly omits rows reads as a complete picture of less than what happened.

    What the None bucket means is a real limitation (M23), not a data-quality
    detail: `agent` only ever arrives on the `chat.message` hook, and all three
    usage upserts write `metadata = COALESCE(existing, excluded)`. Since
    `metadata` is a JSON string that is never SQL NULL once written, the first
    writer's JSON is frozen forever — a call recorded before the session named its
    agent can never be repaired. So this column is "the agent the session first
    reported", not "the agent that ran this call".

    Missing tables are skipped the way `unified_recent_rows` skips them; a query
    that *raises* is allowed to, because half an aggregate presented as the whole
    one is the failure mode worth avoiding here.
    """
    per_agent: dict = {}
    streams = (("skill_usage", "skill", True),
               ("mcp_usage", "mcp", _mcp_available(conn)),
               ("plugin_usage", "plugin", _plugin_available(conn)))
    for table, kind, present in streams:
        if not present:
            continue
        rows = conn.execute(f"""
        SELECT {AGENT_EXPR} AS agent,
               COUNT(*) AS n,
               SUM(status = 'success') AS success,
               SUM(status = 'error')   AS errors,
               SUM(status = 'denied')  AS denied,
               MAX(timestamp)          AS last_used
        FROM {table} GROUP BY agent
        """).fetchall()
        for r in rows:
            agent = _agent_name(r["agent"])
            slot = per_agent.setdefault(agent, _agent_slot(agent))
            slot[kind] += r["n"]
            slot["total"] += r["n"]
            slot["success"] += r["success"] or 0
            slot["errors"] += r["errors"] or 0
            slot["denied"] += r["denied"] or 0
            if r["last_used"] and (slot["last_used"] is None
                                   or r["last_used"] > slot["last_used"]):
                slot["last_used"] = r["last_used"]

    # Two roles, two columns, and never inside `total`: the three columns above are
    # calls this tracker measured, while a spawn is one event that may contain none
    # of them. Folding the two roles into one number would make `Total` mean
    # "calls" or "calls plus events" depending on whether a session delegated.
    if _subagent_available(conn):
        for r in conn.execute(f"""
        SELECT {AGENT_EXPR} AS agent, COUNT(*) AS n
        FROM subagent_usage GROUP BY agent
        """).fetchall():
            agent = _agent_name(r["agent"])
            slot = per_agent.setdefault(agent, _agent_slot(agent))
            slot["subagent"] += r["n"]

        for r in conn.execute("""
        SELECT subagent, COUNT(*) AS n,
               SUM(status = 'success') AS successes,
               SUM(status = 'error')   AS errors,
               MAX(timestamp)          AS last_used
        FROM subagent_usage GROUP BY subagent
        """).fetchall():
            # A spawn with no recorded name is counted against its parent above and
            # gets no row here: there is no name to print, and inventing one would
            # put a fake agent on a page about agents.
            name = _agent_name(r["subagent"])
            if name is None:
                continue
            slot = per_agent.setdefault(name, _agent_slot(name))
            slot["runs_as"] += r["n"]
            slot["runs_ok"] += r["successes"] or 0
            slot["runs_errors"] += r["errors"] or 0
            slot["is_subagent"] = True
            if r["last_used"] and (slot["last_used"] is None
                                   or r["last_used"] > slot["last_used"]):
                slot["last_used"] = r["last_used"]
    return sorted(per_agent.values(), key=lambda row: (-row["total"], row["agent"] or ""))


def subagent_rows(conn) -> list[dict]:
    """Every recorded subagent run, whitelisted columns only.

    The table itself holds nothing else, but this is the surface that would grow
    the leak if it ever did, so the columns are named here rather than `SELECT *`.
    """
    if not _subagent_available(conn):
        return []
    return [
        {k: r[k] for k in ("subagent", "parent_session_id", "child_session_id",
                           "call_id", "status", "duration_ms", "timestamp",
                           "agent", "model")}
        for r in conn.execute(f"""
        SELECT subagent, parent_session_id, child_session_id, call_id, status,
               duration_ms, timestamp,
               {AGENT_EXPR} AS agent,
               CASE WHEN json_valid(metadata) THEN json_extract(metadata, '$.model') END AS model
        FROM subagent_usage ORDER BY timestamp DESC
        """).fetchall()
    ]


def subagent_summary_rows(conn) -> list[dict]:
    """Per subagent *name*: runs, failures, the longest and the most recent.

    This is what makes a subagent that only used builtin tools visible at all. Its
    calls are not measured by design, so without the spawn record the Agents page
    has nothing to say about it — which the owner reasonably read as "not recorded".
    """
    if not _subagent_available(conn):
        return []
    rows = conn.execute("""
    SELECT subagent,
           COUNT(*)                                AS runs,
           SUM(status = 'error')                   AS errors,
           SUM(status = 'success')                 AS successes,
           MAX(timestamp)                          AS last_used,
           MAX(duration_ms)                        AS max_ms,
           CAST(AVG(duration_ms) AS INTEGER)       AS avg_ms
    FROM subagent_usage GROUP BY subagent
    """).fetchall()
    out = []
    for r in rows:
        name = r["subagent"]
        out.append({
            "subagent": name.strip() if isinstance(name, str) and name.strip() else None,
            "runs": r["runs"], "errors": r["errors"] or 0,
            "successes": r["successes"] or 0, "last_used": r["last_used"],
            "max_ms": r["max_ms"], "avg_ms": r["avg_ms"],
        })
    return sorted(out, key=lambda row: (-row["runs"], row["subagent"] or ""))


# ---------------------------------------------------------------------------
# MCP reads
#
# Migration is only triggered by the TUI, `sync`, `doctor` and
# `cleanup-selftest`. `health`, `insight` and `export` can therefore meet a DB
# that predates the mcp_usage table, so every read below degrades to an empty
# result instead of raising.
# ---------------------------------------------------------------------------
def _mcp_available(conn) -> bool:
    return _table_exists(conn, "mcp_usage")


def mcp_stats_rows(conn) -> list[dict]:
    """Every observed (server, tool) pair with totals. [] when unmigrated."""
    if not _mcp_available(conn):
        return []
    sql = """
    SELECT t.server_name, t.tool_name,
           t.server_name || '_' || t.tool_name AS tool_id,
           t.total, t.success, t.errors, t.denied, t.last_used,
           COALESCE(l.uses_30d, 0)  AS uses_30d,
           COALESCE(sc.sessions, 0) AS sessions,
           COALESCE(sc.projects, 0) AS projects,
           sc.avg_ms                AS avg_ms
    FROM v_mcp_totals t
    LEFT JOIN v_mcp_last30 l
           ON l.server_name = t.server_name AND l.tool_name = t.tool_name
    LEFT JOIN (
        SELECT server_name, tool_name,
               COUNT(DISTINCT session_id)   AS sessions,
               COUNT(DISTINCT project_path) AS projects,
               AVG(duration_ms)             AS avg_ms
        FROM mcp_usage GROUP BY server_name, tool_name
    ) sc ON sc.server_name = t.server_name AND sc.tool_name = t.tool_name
    ORDER BY t.total DESC, t.server_name ASC, t.tool_name ASC
    """
    return rows_to_dicts(conn.execute(sql))


def mcp_server_rows(conn) -> list[dict]:
    """Per-server roll-up. [] when unmigrated."""
    if not _mcp_available(conn):
        return []
    sql = """
    SELECT server_name,
           COUNT(DISTINCT tool_name) AS tools,
           COUNT(*)                  AS total,
           SUM(status='success')     AS success,
           SUM(status='error')       AS errors,
           SUM(status='denied')      AS denied,
           MAX(timestamp)            AS last_used
    FROM mcp_usage GROUP BY server_name
    ORDER BY total DESC, server_name ASC
    """
    return rows_to_dicts(conn.execute(sql))


def mcp_recent_rows(conn, limit: int = 50) -> list[dict]:
    if not _mcp_available(conn):
        return []
    return rows_to_dicts(
        conn.execute(
            "SELECT timestamp, server_name, tool_name, project_path, session_id, "
            "       status, duration_ms, trigger_type, arg_names FROM mcp_usage "
            "ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        )
    )


def mcp_tool_detail(conn, server: str, tool: str, limit: int = 200) -> dict | None:
    if not _mcp_available(conn):
        return None
    row = conn.execute(
        "SELECT server_name, tool_name, COUNT(*) AS total, "
        "       SUM(status='success') AS success, SUM(status='error') AS errors, "
        "       SUM(status='denied') AS denied, MAX(timestamp) AS last_used, "
        "       AVG(duration_ms) AS avg_ms "
        "FROM mcp_usage WHERE server_name=? AND tool_name=? "
        "GROUP BY server_name, tool_name",
        (server, tool),
    ).fetchone()
    if not row:
        return None
    detail = dict(row)
    detail["history"] = rows_to_dicts(
        conn.execute(
            "SELECT timestamp, project_path, session_id, status, duration_ms, "
            "       trigger_type, arg_names FROM mcp_usage "
            "WHERE server_name=? AND tool_name=? ORDER BY timestamp DESC LIMIT ?",
            (server, tool, limit),
        )
    )
    return detail


# ---------------------------------------------------------------------------
# Plugin reads
#
# Same contract as the MCP reads above: `health`, `insight` and `export` never
# migrate, so they can meet a DB that predates these tables and must degrade to
# an empty result instead of raising.
# ---------------------------------------------------------------------------
def _plugin_available(conn) -> bool:
    return _table_exists(conn, "plugin_usage")


def _plugin_inventory_available(conn) -> bool:
    return _table_exists(conn, "plugin_inventory")


def _subagent_available(conn) -> bool:
    return _table_exists(conn, "subagent_usage")


def _load_json_list(raw) -> list:
    """Parse a JSON array column; anything unparsable degrades to []."""
    try:
        value = json.loads(raw) if raw else []
    except (json.JSONDecodeError, TypeError):
        return []
    return value if isinstance(value, list) else []


def plugin_stats_rows(conn) -> list[dict]:
    """Every observed (plugin, kind, item) triple with totals. [] when unmigrated."""
    if not _plugin_available(conn):
        return []
    sql = """
    SELECT t.plugin_name, t.kind, t.item_name,
           t.plugin_name || '/' || t.kind || '/' || t.item_name AS item_id,
           t.total, t.success, t.errors, t.denied, t.last_used,
           COALESCE(l.uses_30d, 0)  AS uses_30d,
           COALESCE(sc.sessions, 0) AS sessions,
           COALESCE(sc.projects, 0) AS projects,
           sc.avg_ms                AS avg_ms
    FROM v_plugin_totals t
    LEFT JOIN v_plugin_last30 l
           ON l.plugin_name = t.plugin_name AND l.kind = t.kind AND l.item_name = t.item_name
    LEFT JOIN (
        SELECT plugin_name, kind, item_name,
               COUNT(DISTINCT session_id)   AS sessions,
               COUNT(DISTINCT project_path) AS projects,
               AVG(duration_ms)             AS avg_ms
        FROM plugin_usage GROUP BY plugin_name, kind, item_name
    ) sc ON sc.plugin_name = t.plugin_name AND sc.kind = t.kind
        AND sc.item_name = t.item_name
    ORDER BY t.total DESC, t.plugin_name ASC, t.kind ASC, t.item_name ASC
    """
    return rows_to_dicts(conn.execute(sql))


def plugin_surface_rows(conn) -> list[dict]:
    """Every plugin surface the tracker knows about — whether or not it was called.

    `plugin_stats_rows` can only report a triple that already has a usage row, so a
    plugin with ten registered tools and two calls rendered as two rows and read as
    "plugins are not being recorded". This unions the init-time inventory with the
    usage totals and keeps two separate claims on each row: `registered` (the scan
    saw it at OpenCode start) and `ever_called` (usage exists). Most-called first,
    so the zero rows sit at the bottom rather than crowding out the signal.

    Rows are still keyed by the unparseable `plugin/kind/item` id — see the note in
    `render_plugins`; nothing downstream may split it back apart.
    """
    if not (_plugin_available(conn) or _plugin_inventory_available(conn)):
        return []

    rows: dict[str, dict] = {}

    def slot(plugin, kind, item):
        key = f"{plugin}/{kind}/{item}"
        return rows.setdefault(key, {
            "plugin_name": plugin, "kind": kind, "item_name": item, "item_id": key,
            "total": 0, "success": 0, "errors": 0, "denied": 0, "last_used": None,
            "uses_30d": 0, "sessions": 0, "projects": 0, "avg_ms": None,
            "registered": False, "ever_called": False,
        })

    for r in plugin_stats_rows(conn):
        row = slot(r["plugin_name"], r["kind"], r["item_name"])
        for field in ("total", "success", "errors", "denied", "last_used",
                      "uses_30d", "sessions", "projects", "avg_ms"):
            row[field] = r[field]
        row["ever_called"] = True

    for inv in plugin_inventory_rows(conn):
        # An excluded plugin has no surface to list: its absence is a decision, and
        # printing a 0 for it would invent a row that can never become anything.
        if inv["skipped"]:
            continue
        for kind, names in (("tool", inv["tools"]), ("command", inv["commands"])):
            for item in names:
                if isinstance(item, str) and item.strip():
                    slot(inv["plugin_name"], kind, item.strip())["registered"] = True

    return sorted(rows.values(), key=lambda r: (-r["total"], r["plugin_name"],
                                                r["kind"], r["item_name"]))


def plugin_inventory_rows(conn) -> list[dict]:
    """One row per plugin seen at init, joined with its usage totals.

    Includes skipped (excluded) plugins, so the page can show what was seen and
    deliberately not measured. [] when the inventory table is absent.
    """
    if not _plugin_inventory_available(conn):
        return []
    sql = """
    SELECT i.plugin_name, i.version, i.source, i.skipped, i.tools, i.commands,
           i.scope, i.first_seen, i.last_seen,
           COALESCE(u.total, 0)  AS total,
           COALESCE(u.errors, 0) AS errors,
           u.last_used           AS last_used
    FROM plugin_inventory i
    LEFT JOIN (
        SELECT plugin_name, COUNT(*) AS total,
               SUM(status='error') AS errors, MAX(timestamp) AS last_used
        FROM plugin_usage GROUP BY plugin_name
    ) u ON u.plugin_name = i.plugin_name
    ORDER BY i.skipped ASC, total DESC, i.plugin_name ASC
    """
    rows = rows_to_dicts(conn.execute(sql))
    for r in rows:
        r["tools"] = _load_json_list(r.get("tools"))
        r["commands"] = _load_json_list(r.get("commands"))
    return rows


def plugin_recent_rows(conn, limit: int = 50) -> list[dict]:
    if not _plugin_available(conn):
        return []
    return rows_to_dicts(
        conn.execute(
            "SELECT timestamp, plugin_name, kind, item_name, project_path, session_id, "
            "       status, duration_ms, trigger_type FROM plugin_usage "
            "ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        )
    )


def plugin_item_detail(conn, plugin: str, kind: str, item: str, limit: int = 200) -> dict | None:
    if not _plugin_available(conn):
        return None
    row = conn.execute(
        "SELECT plugin_name, kind, item_name, COUNT(*) AS total, "
        "       SUM(status='success') AS success, SUM(status='error') AS errors, "
        "       SUM(status='denied') AS denied, MAX(timestamp) AS last_used, "
        "       AVG(duration_ms) AS avg_ms "
        "FROM plugin_usage WHERE plugin_name=? AND kind=? AND item_name=? "
        "GROUP BY plugin_name, kind, item_name",
        (plugin, kind, item),
    ).fetchone()
    if not row:
        return None
    detail = dict(row)
    detail["history"] = rows_to_dicts(
        conn.execute(
            "SELECT timestamp, project_path, session_id, status, duration_ms, "
            "       trigger_type FROM plugin_usage "
            "WHERE plugin_name=? AND kind=? AND item_name=? "
            "ORDER BY timestamp DESC LIMIT ?",
            (plugin, kind, item, limit),
        )
    )
    return detail


def skill_detail(conn, skill: str) -> dict | None:
    row = conn.execute(
        f"""SELECT s.*, {SOURCE_CASE_SQL} AS source,
                   COALESCE(t.total,0) total, COALESCE(t.success,0) success,
                   COALESCE(t.errors,0) errors, COALESCE(t.denied,0) denied,
                   t.last_used, COALESCE(l.uses_30d,0) uses_30d
            FROM skills s
            LEFT JOIN v_skill_totals t ON t.skill_name = s.name
            LEFT JOIN v_skill_last30 l ON l.skill_name = s.name
            WHERE s.name = ?""",
        (skill,),
    ).fetchone()
    if not row:
        return None
    detail = dict(row)
    try:
        detail["versions"] = rows_to_dicts(
            conn.execute(
                "SELECT content_hash, size_bytes, recorded_at FROM skill_versions "
                "WHERE skill_name = ? ORDER BY recorded_at DESC",
                (skill,),
            )
        )
    except sqlite3.Error:
        detail["versions"] = []
    detail["history"] = history_rows(conn, skill, limit=200)
    return detail


def categories(conn) -> list[dict]:
    sql = f"""
    SELECT {SOURCE_CASE_SQL} AS source, s.category,
           COUNT(*) AS skills,
           COALESCE(SUM(t.total), 0) AS usage,
           COALESCE(SUM(t.success), 0) AS success,
           COALESCE(SUM(t.errors), 0) AS errors,
           COALESCE(SUM(t.denied), 0) AS denied
    FROM skills s
    LEFT JOIN v_skill_totals t ON t.skill_name = s.name
    GROUP BY s.category ORDER BY usage DESC, source ASC
    """
    return rows_to_dicts(conn.execute(sql))


# ---------------------------------------------------------------------------
# Insight
# ---------------------------------------------------------------------------
def insight_most_used(conn, limit: int = 10) -> list[dict]:
    return top_rows(conn, limit)


def insight_fastest_growing(conn, days: int = 30, min_uses: int = 3,
                            limit: int = 10) -> list[dict]:
    sql = """
    WITH recent AS (
      SELECT skill_name, COUNT(*) c FROM skill_usage
      WHERE timestamp >= strftime('%Y-%m-%dT%H:%M:%fZ','now', ?)
      GROUP BY skill_name),
    prev AS (
      SELECT skill_name, COUNT(*) c FROM skill_usage
      WHERE timestamp >= strftime('%Y-%m-%dT%H:%M:%fZ','now', ?)
        AND timestamp <  strftime('%Y-%m-%dT%H:%M:%fZ','now', ?)
      GROUP BY skill_name),
    j AS (
      -- Both branches must be kept for every skill: summing them per skill is
      -- what yields prev > 0 for a skill that is used in both windows. An
      -- earlier `WHERE skill_name NOT IN (SELECT .. FROM recent)` on the prev
      -- branch zeroed prev for exactly the skills that had recent usage, so
      -- every row reported prev=0 and "fastest growing" degenerated into
      -- "most used recently" (a skill going 10 -> 5 was shown as +5 growth).
      SELECT skill_name, c AS r, 0 AS p FROM recent
      UNION ALL
      SELECT skill_name, 0 AS r, c AS p FROM prev
    )
    SELECT skill_name, SUM(r) AS recent, SUM(p) AS prev,
           SUM(r) - SUM(p) AS delta
    FROM j GROUP BY skill_name
    HAVING SUM(r) >= ?
    ORDER BY (SUM(r) + 1.0) / (SUM(p) + 1.0) DESC, delta DESC, recent DESC
    LIMIT ?
    """
    args = (f"-{days} days", f"-{2 * days} days", f"-{days} days", min_uses, limit)
    return rows_to_dicts(conn.execute(sql, args))


def insight_long_unused(conn, days: int = 30, limit: int = 50) -> list[dict]:
    sql = f"""
    SELECT s.name AS skill_name, s.category, {SOURCE_CASE_SQL} AS source,
           t.last_used, COALESCE(t.total, 0) AS total,
           (t.last_used IS NULL) AS never_used
    FROM skills s LEFT JOIN v_skill_totals t ON t.skill_name = s.name
    WHERE t.last_used IS NULL
       OR t.last_used < strftime('%Y-%m-%dT%H:%M:%fZ','now', ?)
    ORDER BY t.last_used IS NOT NULL, t.last_used ASC, s.name ASC
    LIMIT ?
    """
    return rows_to_dicts(conn.execute(sql, (f"-{days} days", limit)))


def insight_failure_rate(conn, min_uses: int = 3, limit: int = 10) -> list[dict]:
    sql = """
    SELECT skill_name, total, success, errors, denied,
           ROUND(1.0*(errors+denied)/total, 3) AS fail_rate
    FROM v_skill_totals WHERE total >= ?
    ORDER BY fail_rate DESC, total DESC LIMIT ?
    """
    return rows_to_dicts(conn.execute(sql, (min_uses, limit)))


def insight(conn, days: int = 30, min_uses: int = 3, limit: int = 10) -> dict:
    return {
        "windows": {"grow_days": days, "unused_days": days, "min_uses": min_uses},
        "sample": sample_size(conn),
        "most_used": insight_most_used(conn, limit),
        "fastest_growing": insight_fastest_growing(conn, days, min_uses, limit),
        "long_unused": insight_long_unused(conn, days, limit),
        "highest_failure_rate": insight_failure_rate(conn, min_uses, limit),
    }


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------
def _load_meta(raw):
    try:
        return json.loads(raw) if raw else {}
    except (json.JSONDecodeError, TypeError):
        return {}


# Keys the plugin has ever written. `summary` (a verbatim snippet of the user's
# prompt, written by older builds and still present in historical rows) and
# `title` (a permission title that echoes the same text) are deliberately NOT
# here: export is the one path that leaves the machine, so it emits an
# allowlist rather than whatever happens to be in the column.
EXPORT_METADATA_KEYS = (
    "tool",
    "call_id",
    "agent",
    "model",
    "branch",
    "source",
    "error",
)


def _export_metadata(raw):
    meta = _load_meta(raw)
    return {k: v for k, v in meta.items() if k in EXPORT_METADATA_KEYS}


def export_document(conn, include_usage: bool = True, include_insight: bool = True) -> dict:
    doc = {
        # 2 added the `mcp_usage` key; 3 added `plugin_usage` and
        # `plugin_inventory`. Both changes are additive — a v1 consumer that
        # iterates `skills`/`usage` is unaffected — but the document shape did
        # change, so the version says so.
        # 4 narrows `metadata` to EXPORT_METADATA_KEYS. Older builds wrote the
        # user's prompt text as `summary`, and those rows are still in existing
        # databases, so a v4 document is not a superset of a v3 one.
        # 5 adds `plugin_inventory.scope` (which config listed the plugin, and so
        # whether the plugin may prune it). Additive for the rows that have it;
        # rows written before the column existed export it as null.
        # 6 adds `subagent_usage`: one row per subagent the host started, with
        # identifiers and durations only. Additive for a v5 consumer, but a
        # database that has the table and a database that does not are different
        # documents, so the version says which one this is.
        "schema_version": 6,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "db_path": db_file_of(conn),
        "skills": [],
        "usage": [],
        "versions": [],
        "mcp_usage": [],
        "plugin_usage": [],
        "plugin_inventory": [],
        "subagent_usage": [],
    }
    for s in stats_rows(conn):
        doc["skills"].append(
            {
                "name": s["skill_name"],
                "category": s["category"],
                "source": s["source"],
                "path": s["path"],
                "description": s["description"],
                "stats": {
                    "total": s["total"],
                    "success": s["success"],
                    "errors": s["errors"],
                    "denied": s["denied"],
                    "last_used": s["last_used"],
                    "uses_30d": s["uses_30d"],
                    "success_rate": success_rate(s["total"], s["success"]),
                },
            }
        )
    # content_hash is attached only when the column exists, so export keeps
    # working against a DB that has not been migrated yet.
    if has_content_hash(conn):
        hash_map = {
            r["name"]: r["content_hash"]
            for r in conn.execute("SELECT name, content_hash FROM skills")
        }
        for d in doc["skills"]:
            d["content_hash"] = hash_map.get(d["name"])

    if include_usage:
        for r in conn.execute("SELECT * FROM skill_usage ORDER BY timestamp DESC"):
            row = dict(r)
            row["metadata"] = _export_metadata(row.get("metadata"))
            doc["usage"].append(row)

    try:
        doc["versions"] = rows_to_dicts(
            conn.execute(
                "SELECT skill_name, content_hash, size_bytes, recorded_at "
                "FROM skill_versions ORDER BY recorded_at DESC"
            )
        )
    except sqlite3.Error:
        doc["versions"] = []

    # Absent on a DB that predates the mcp_usage table.
    if _mcp_available(conn):
        for r in conn.execute("SELECT * FROM mcp_usage ORDER BY timestamp DESC"):
            row = dict(r)
            row["metadata"] = _export_metadata(row.get("metadata"))
            doc["mcp_usage"].append(row)

    # Absent on a DB that predates the plugin tables.
    if _plugin_available(conn):
        for r in conn.execute("SELECT * FROM plugin_usage ORDER BY timestamp DESC"):
            row = dict(r)
            row["metadata"] = _export_metadata(row.get("metadata"))
            doc["plugin_usage"].append(row)
    if _plugin_inventory_available(conn):
        for r in conn.execute("SELECT * FROM plugin_inventory ORDER BY plugin_name"):
            row = dict(r)
            for col in ("tools", "commands"):
                row[col] = _load_json_list(row.get(col))
            doc["plugin_inventory"].append(row)

    # Columns named on purpose: this table holds identifiers and durations only, and
    # `SELECT *` would quietly export whatever a future column adds.
    if _subagent_available(conn):
        for r in conn.execute(
            "SELECT parent_session_id, child_session_id, call_id, subagent,"
            " project_path, trigger_type, status, timestamp, duration_ms, metadata"
            " FROM subagent_usage ORDER BY timestamp DESC"
        ):
            row = dict(r)
            row["metadata"] = _export_metadata(row.get("metadata"))
            doc["subagent_usage"].append(row)

    if include_insight:
        doc["insight"] = insight(conn)
    return doc


def write_private_json(path: str, doc: dict, pretty: bool = False, force: bool = False) -> str:
    """Write JSON with 0600, refusing to clobber unless force."""
    path = os.path.abspath(os.path.expanduser(path))
    if os.path.exists(path) and not force:
        raise FileExistsError(f"refusing to overwrite existing file: {path}")
    data = json.dumps(doc, ensure_ascii=False, indent=2 if pretty else None)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
    finally:
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    return path


# ---------------------------------------------------------------------------
# Write helpers (used by the TUI data page and --cli)
# ---------------------------------------------------------------------------
@contextlib.contextmanager
def _write_txn(conn):
    """Run a write helper atomically.

    Python's sqlite3 opens an implicit transaction on the first DML statement.
    Without an explicit rollback a mid-way failure leaves that transaction open,
    and a later `executescript` (in ensure_schema) would implicitly COMMIT the
    half-finished work. Callers such as the TUI catch exceptions and keep using
    the same connection, so this must always leave it clean.
    """
    try:
        yield
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass  # the original exception is what matters
        raise


def db_file_of(conn) -> str:
    """Path of the main database file behind this connection.

    Used instead of the module-level DB_PATH so that a caller pointed at a
    different DB (tests, --db PATH) writes backups/exports next to *its* file.
    """
    try:
        for row in conn.execute("PRAGMA database_list"):
            if row[1] == "main" and row[2]:
                return row[2]
    except sqlite3.Error:
        pass
    return DB_PATH


def backup_db(conn, target: str | None = None) -> str:
    if target:
        target = os.path.abspath(os.path.expanduser(target))
    else:
        # BACKUP_DIR, not next to the database: retention only ever sweeps
        # BACKUP_DIR, so a default that landed beside the DB produced backups
        # that nothing would ever prune (and nothing would ever report — doctor
        # reads backups.latest from BACKUP_DIR only).
        # The default name is second-resolution; two backups in the same second
        # would otherwise collide. Bump the timestamp until a name is free.
        os.makedirs(BACKUP_DIR, mode=0o700, exist_ok=True)
        try:
            os.chmod(BACKUP_DIR, 0o700)
        except OSError:
            pass  # the backup itself still needs to be private; the dir is best effort
        now = datetime.now(timezone.utc)
        for bump in range(5):
            ts = (now + timedelta(seconds=bump)).strftime("%Y%m%d-%H%M%S")
            candidate = os.path.join(BACKUP_DIR, f"skill-usage-backup-{ts}.db")
            if not os.path.exists(candidate):
                target = candidate
                break
        else:
            raise FileExistsError(f"no free backup name near {BACKUP_DIR}")
    if os.path.exists(target):
        raise FileExistsError(f"refusing to overwrite existing file: {target}")
    conn.commit()  # VACUUM cannot run inside an open transaction
    conn.execute("VACUUM INTO ?", (target,))
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass
    return target


def vacuum_db(conn) -> None:
    conn.commit()
    conn.execute("VACUUM")


def clear_usage(conn, also_skills: bool = False) -> dict:
    with _write_txn(conn):
        n = conn.execute("SELECT COUNT(*) FROM skill_usage").fetchone()[0]
        conn.execute("DELETE FROM skill_usage")
        # "Clear usage" means every kind of usage. Leaving MCP or plugin rows
        # behind would keep the Dashboard showing counts after a clear.
        c = 0
        if _mcp_available(conn):
            c = conn.execute("SELECT COUNT(*) FROM mcp_usage").fetchone()[0]
            conn.execute("DELETE FROM mcp_usage")
        p = 0
        if _plugin_available(conn):
            p = conn.execute("SELECT COUNT(*) FROM plugin_usage").fetchone()[0]
            conn.execute("DELETE FROM plugin_usage")
        # plugin_inventory is deliberately kept: it is what is installed, not
        # what was used, so clearing usage must not make the inventory vanish.
        m = 0
        if also_skills:
            m = conn.execute("SELECT COUNT(*) FROM skills").fetchone()[0]
            conn.execute("DELETE FROM skills")
            conn.execute("DELETE FROM skill_versions")
    return {
        "usage_deleted": n,
        "skills_deleted": m,
        "mcp_deleted": c,
        "plugin_deleted": p,
    }


def delete_skill(conn, skill: str) -> dict:
    """Delete a skill's usage + versions + skills row, leaving no orphans.

    Python connections never enable PRAGMA foreign_keys, so the declared
    `ON DELETE SET NULL` does not fire; usage and versions are removed
    explicitly. Both are matched by name OR by skill_id, so rows left behind by
    a renamed skill are cleaned up too.
    """
    by_name_or_id = (
        "skill_name = ? OR skill_id IN (SELECT id FROM skills WHERE name = ?)"
    )
    with _write_txn(conn):
        n = conn.execute(
            f"SELECT COUNT(*) FROM skill_usage WHERE {by_name_or_id}", (skill, skill)
        ).fetchone()[0]
        conn.execute(f"DELETE FROM skill_usage WHERE {by_name_or_id}", (skill, skill))
        conn.execute(f"DELETE FROM skill_versions WHERE {by_name_or_id}", (skill, skill))
        s = conn.execute("DELETE FROM skills WHERE name = ?", (skill,)).rowcount
    return {"usage_deleted": n, "skills_deleted": s}


SELFTEST_PROJECT = "/tmp/selftest-proj"


def cleanup_selftest(conn, dry_run: bool = True) -> dict:
    """Remove the synthetic rows a `__selftest()` run left in the production DB."""
    rows = rows_to_dicts(
        conn.execute(
            "SELECT id, skill_name, status, timestamp FROM skill_usage "
            "WHERE project_path = ? ORDER BY id",
            (SELFTEST_PROJECT,),
        )
    )
    mcp_rows = []
    if _mcp_available(conn):
        mcp_rows = rows_to_dicts(
            conn.execute(
                "SELECT id, server_name, tool_name, status, timestamp FROM mcp_usage "
                "WHERE project_path = ? ORDER BY id",
                (SELFTEST_PROJECT,),
            )
        )
    plugin_rows = []
    if _plugin_available(conn):
        plugin_rows = rows_to_dicts(
            conn.execute(
                "SELECT id, plugin_name, kind, item_name, status, timestamp FROM plugin_usage "
                "WHERE project_path = ? ORDER BY id",
                (SELFTEST_PROJECT,),
            )
        )
    if not dry_run and (rows or mcp_rows or plugin_rows):
        with _write_txn(conn):
            conn.execute("DELETE FROM skill_usage WHERE project_path = ?", (SELFTEST_PROJECT,))
            if _mcp_available(conn):
                conn.execute("DELETE FROM mcp_usage WHERE project_path = ?", (SELFTEST_PROJECT,))
            if _plugin_available(conn):
                conn.execute(
                    "DELETE FROM plugin_usage WHERE project_path = ?", (SELFTEST_PROJECT,)
                )
    return {
        "dry_run": dry_run,
        "matched": len(rows),
        "rows": rows,
        "mcp_matched": len(mcp_rows),
        "mcp_rows": mcp_rows,
        "plugin_matched": len(plugin_rows),
        "plugin_rows": plugin_rows,
    }


# Free-text metadata keys that must not stay in the database. `summary` held a
# snippet of the user's own prompt: older builds wrote it, the writer has since
# been removed, but the historical rows are still here. `title` is a permission
# title, which routinely echoes the same input — the plugin stopped writing it in
# 2026-10-09, and it stays listed because rows written before that are still in
# real databases and in old backups. Both are already excluded from export by
# EXPORT_METADATA_KEYS; this closes the other half of the path.
METADATA_SCRUB_KEYS = ("summary", "title")


def checkpoint_wal(conn) -> bool:
    """Fold the WAL back into the main file and truncate it. Returns True if the
    WAL is fully checkpointed.

    Why this exists: under WAL, values that a write just replaced keep living in
    the `-wal` file until a checkpoint. For a redaction that is the difference
    between "the row no longer holds it" and "the file no longer holds it" —
    scrubbing metadata without checkpointing leaves the redacted text on disk.
    A busy database declines (busy_timeout already applies); callers treat that
    as "retry later", never as an error.
    """
    row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    # (busy, log size, checkpointed size); busy=0 and log==checkpoint means done.
    return bool(row) and row[0] == 0


def scrub_metadata(conn, keys=METADATA_SCRUB_KEYS, dry_run: bool = True) -> dict:
    """Drop disallowed keys from stored metadata, keeping every usage row.

    The rows are the point of this database; the leaked text is not. So this
    rewrites the JSON blob rather than deleting anything, and reports exactly
    which keys came off. Read-only unless `dry_run` is False.
    """
    wanted = set(keys)
    streams = [("skill_usage", "skill_name")]
    if _mcp_available(conn):
        streams.append(("mcp_usage", "server_name || '.' || tool_name"))
    if _plugin_available(conn):
        streams.append(("plugin_usage", "plugin_name || '/' || kind || '/' || item_name"))

    hits: list[dict] = []
    updates: list[tuple[str, int, str]] = []
    checkpointed = None  # None = nothing was written, so no checkpoint was needed
    for table, name_expr in streams:
        # `table`/`name_expr` come from the fixed list above, never from input.
        for r in rows_to_dicts(
            conn.execute(f"SELECT id, timestamp, metadata, {name_expr} AS name FROM {table}")
        ):
            meta = _load_meta(r["metadata"])
            off = sorted(k for k in meta if k in wanted)
            if not off:
                continue
            hits.append(
                {"table": table, "id": r["id"], "name": r["name"],
                 "timestamp": r["timestamp"], "keys": off}
            )
            kept = {k: v for k, v in meta.items() if k not in wanted}
            updates.append((table, r["id"], json.dumps(kept, ensure_ascii=False)))

    if not dry_run and updates:
        with _write_txn(conn):
            for table, rid, payload in updates:
                conn.execute(f"UPDATE {table} SET metadata = ? WHERE id = ?", (payload, rid))
        # The point of a redaction is that the bytes stop being on disk, and
        # under WAL they survive in the -wal file until a checkpoint.
        checkpointed = checkpoint_wal(conn)

    return {
        "dry_run": dry_run,
        "matched": len(hits),
        "keys": sorted(wanted),
        "by_table": {t: sum(1 for h in hits if h["table"] == t) for t, _ in streams},
        "rows": hits,
        "checkpointed": checkpointed,
    }


# ---------------------------------------------------------------------------
# Row-level retention — the same dry-run shape as scrub_metadata, the same
# "plan is a pure SELECT" rule as plan_retention above.
# ---------------------------------------------------------------------------

# The three streams `skillt doctor` calls usage, and the three this deletes.
USAGE_STREAMS = ("skill_usage", "mcp_usage", "plugin_usage")

# `subagent_usage` is deliberately **not** in that list. Its rows are events, not
# calls — the spawn record is the only witness that a subagent ran at all (README
# M24) — and they grow by task spawns rather than by tool calls, so the volume that
# makes row retention necessary elsewhere does not exist here. Saying which table is
# excluded is also the point: a silent omission reads like a bug in the next audit.
RETENTION_EXCLUDED = ("subagent_usage",)

# A timestamp this tool wrote is `YYYY-MM-DD…`; anything else is a row nobody can
# date. Comparing strings that are not dates against a cutoff is not a no-op — an
# empty string sorts before every real timestamp, so `WHERE timestamp < cutoff`
# deletes it as "ancient". The shape test is the guard, and a row that fails it is
# counted (`undated`) and kept.
_DATED_GLOB = "[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*"

# Newest-first per skill, so "keep at most N" is a filter on the row number rather
# than a Python-side grouping of the whole table. `id DESC` breaks ties because two
# versions recorded in the same millisecond would otherwise order arbitrarily.
_OVER_N_VERSIONS_SQL = """
WITH ranked AS (
  SELECT id, ROW_NUMBER() OVER (
           PARTITION BY skill_name
           ORDER BY recorded_at DESC, id DESC
         ) AS rn
  FROM skill_versions
  WHERE recorded_at GLOB :dated
)
SELECT id FROM ranked WHERE rn > :keep
"""


def _check_retention_number(name: str, value) -> int:
    """0 means off, a negative means "delete the table" — so refuse it here."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an int (0 = off), got {value!r}")
    if value < 0:
        raise ValueError(f"{name} must be >= 0 (0 = off); a negative would match "
                         f"every row, got {value}")
    return value


def _retention_cutoff(conn, days: int) -> str:
    """The ISO string `days` ago, produced by SQLite so the format is not restated."""
    return conn.execute("SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now', ?)",
                        (f"-{int(days)} days",)).fetchone()[0]


def _retention_targets(conn, usage_days: int, max_versions: int) -> dict:
    """The ids that would go, per table, plus what could not be dated.

    One function behind both the plan and the delete: if `--dry-run` ran a different
    query than `--yes`, the count it printed would be a prediction rather than a
    measurement.
    """
    ids: dict[str, list[int]] = {}
    undated = 0
    if usage_days > 0:
        cutoff = _retention_cutoff(conn, usage_days)
        for table in USAGE_STREAMS:
            if not _table_exists(conn, table):
                continue
            ids[table] = [r[0] for r in conn.execute(
                f"SELECT id FROM {table} WHERE timestamp GLOB ? AND timestamp < ?",
                (_DATED_GLOB, cutoff),
            )]
            undated += conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE timestamp NOT GLOB ?",
                (_DATED_GLOB,),
            ).fetchone()[0]
    if _table_exists(conn, "skill_versions"):
        undated += conn.execute(
            "SELECT COUNT(*) FROM skill_versions WHERE recorded_at NOT GLOB ?",
            (_DATED_GLOB,),
        ).fetchone()[0]
    if max_versions > 0 and _table_exists(conn, "skill_versions"):
        ids["skill_versions"] = [r[0] for r in conn.execute(
            _OVER_N_VERSIONS_SQL, {"dated": _DATED_GLOB, "keep": max_versions})]
    return {"ids": ids, "undated": undated}


def _retention_shape(conn, usage_days, max_versions) -> dict:
    """The shared report: counts by table, totals, and which tables were considered."""
    usage_days = _check_retention_number("usage_days", usage_days)
    max_versions = _check_retention_number("max_versions", max_versions)
    present = [t for t in USAGE_STREAMS if _table_exists(conn, t)]
    if _table_exists(conn, "skill_versions"):
        present.append("skill_versions")

    targets = _retention_targets(conn, usage_days, max_versions)
    by_table = {t: len(targets["ids"].get(t, [])) for t in present}
    usage_rows = sum(n for t, n in by_table.items() if t in USAGE_STREAMS)
    version_rows = by_table.get("skill_versions", 0)
    return {
        "usage_days": usage_days,
        "max_skill_versions": max_versions,
        "enabled": usage_days > 0 or max_versions > 0,
        "tables_present": present,
        "excluded": list(RETENTION_EXCLUDED),
        "by_table": by_table,
        "usage_rows": usage_rows,
        "version_rows": version_rows,
        "total": usage_rows + version_rows,
        "undated": targets["undated"],
        "_targets": targets,
    }


def plan_usage_retention(conn, usage_days: int, max_versions: int) -> dict:
    """Which rows *would* be deleted, computed by SELECT only. Strictly read-only.

    `usage_days = 0` and `max_versions = 0` are the shipped defaults and mean off:
    nothing is scheduled, and the caller says so rather than printing a zero-length
    list that looks like an empty verdict.
    """
    shape = _retention_shape(conn, usage_days, max_versions)
    shape.pop("_targets", None)
    return shape


def prune_usage(conn, usage_days: int, max_versions: int, dry_run: bool = True) -> dict:
    """Delete rows past the retention window. Read-only unless `dry_run` is False.

    Every guard here exists because this is the one path in the project that removes
    recorded history: the numbers come from the same SELECT the delete uses, the
    whole delete is one transaction (`_write_txn`) so a crash cannot leave half a
    timeline gone, and the rows nobody can date stay. A backup is the caller's job —
    `skillt prune-usage --yes` takes one first and refuses to continue if it fails.
    """
    shape = _retention_shape(conn, usage_days, max_versions)
    targets = shape.pop("_targets")
    ids = targets["ids"]

    deleted = {"usage": 0, "versions": 0, "total": 0}
    checkpointed = None  # None = nothing was written, so nothing needs folding in
    if not dry_run and ids:
        with _write_txn(conn):
            for table, row_ids in ids.items():
                if not row_ids:
                    continue
                # Chunked so a huge plan cannot exceed SQLite's parameter limit.
                for i in range(0, len(row_ids), 500):
                    chunk = row_ids[i:i + 500]
                    marks = ",".join("?" * len(chunk))
                    cur = conn.execute(
                        f"DELETE FROM {table} WHERE id IN ({marks})", chunk)
                    n = cur.rowcount or 0
                    deleted["total"] += n
                    if table == "skill_versions":
                        deleted["versions"] += n
                    else:
                        deleted["usage"] += n
        checkpointed = checkpoint_wal(conn)

    return {
        "dry_run": dry_run,
        "deleted": deleted,
        "by_table": shape["by_table"],
        "plan": shape,
        "checkpointed": checkpointed,
    }


# ---------------------------------------------------------------------------
# Health report (read-only) — active / stale / unused + risk flags
# ---------------------------------------------------------------------------
HEALTH_ACTIVE_DAYS = 30
HEALTH_STALE_DAYS = 90
HIGH_FAILURE_MIN_USES = 5
HIGH_FAILURE_RATE = 0.5
FREQUENTLY_EDITED_VERSIONS = 5

# A pruning decision needs a denominator. OpenCode exposes every discovered
# skill to every session, so the number of observed sessions is what makes a
# "0 uses" count meaningful. Below these thresholds the report still shows the
# facts (buckets, flags) but refuses to advise on pruning.
MIN_SESSIONS_FOR_ADVICE = 20
MIN_DAYS_FOR_ADVICE = 14

_BUCKET_ORDER = {"unused": 0, "stale": 1, "active": 2}


def _table_exists(conn, name: str) -> bool:
    try:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone() is not None
    except sqlite3.Error:
        return False


def _parse_utc(ts):
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def health_report(conn, now=None) -> dict:
    """Classify every skill and flag risks. Strictly read-only.

    Buckets: active = used within 30 days, stale = 30-90 days, unused = >90
    days or never used. Flags are advisory only — nothing is ever deleted here.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    cut_active = now.timestamp() - HEALTH_ACTIVE_DAYS * 86400
    cut_stale = now.timestamp() - HEALTH_STALE_DAYS * 86400

    has_versions = _table_exists(conn, "skill_versions")
    versions_sub = (
        "(SELECT COUNT(*) FROM skill_versions v WHERE v.skill_name = s.name)"
        if has_versions else "0"
    )
    sql = f"""
    SELECT s.name AS skill_name, s.category, {SOURCE_CASE_SQL} AS source, s.path,
           COALESCE(t.total, 0)   AS total,
           COALESCE(t.success, 0) AS success,
           COALESCE(t.errors, 0)  AS errors,
           COALESCE(t.denied, 0)  AS denied,
           t.last_used            AS last_used,
           {versions_sub}         AS versions,
           COALESCE(sc.sessions, 0) AS sessions
    FROM skills s
    LEFT JOIN v_skill_totals t ON t.skill_name = s.name
    LEFT JOIN (
        SELECT skill_name, COUNT(DISTINCT session_id) AS sessions
        FROM skill_usage GROUP BY skill_name
    ) sc ON sc.skill_name = s.name
    """
    skills = []
    counts = {"total": 0, "active": 0, "stale": 0, "unused": 0}
    risk_counts = {"high_failure": 0, "frequently_edited": 0, "never_used": 0}

    for row in conn.execute(sql):
        d = dict(row)
        dt = _parse_utc(d["last_used"])
        if dt is None:
            bucket = "unused"
        elif dt.timestamp() >= cut_active:
            bucket = "active"
        elif dt.timestamp() >= cut_stale:
            bucket = "stale"
        else:
            bucket = "unused"

        flags = []
        total = d["total"] or 0
        if total == 0:
            flags.append("never_used")
        if total >= HIGH_FAILURE_MIN_USES and (d["errors"] + d["denied"]) / total >= HIGH_FAILURE_RATE:
            flags.append("high_failure")
        if (d["versions"] or 0) >= FREQUENTLY_EDITED_VERSIONS:
            flags.append("frequently_edited")

        d["bucket"] = bucket
        d["flags"] = flags
        d["failure_rate"] = round((d["errors"] + d["denied"]) / total, 3) if total else None
        skills.append(d)
        counts["total"] += 1
        counts[bucket] += 1
        for f in flags:
            risk_counts[f] += 1

    skills.sort(key=lambda r: (_BUCKET_ORDER[r["bucket"]], -(r["total"] or 0), r["skill_name"]))

    sample = sample_size(conn)
    suggestions = []
    never_used_n = risk_counts["never_used"]
    stale_unused_n = counts["unused"] - never_used_n
    if not sample["enough_for_advice"]:
        suggestions.append(
            f"Not enough data to advise on pruning yet: {sample['sessions']} "
            f"session(s) over {sample['days_observed']} day(s); need >= "
            f"{MIN_SESSIONS_FOR_ADVICE} sessions over >= {MIN_DAYS_FOR_ADVICE} days"
        )
    else:
        if never_used_n:
            suggestions.append(
                f"{never_used_n} skill(s) have never been used; review them before "
                "deciding whether to remove any (this command never deletes data)"
            )
        if stale_unused_n:
            suggestions.append(
                f"{stale_unused_n} skill(s) unused for over {HEALTH_STALE_DAYS} days; "
                "review them before deciding whether to remove any "
                "(this command never deletes data)"
            )
        if counts["stale"]:
            suggestions.append(
                f"{counts['stale']} skill(s) not used for "
                f"{HEALTH_ACTIVE_DAYS}-{HEALTH_STALE_DAYS} days; worth a look"
            )
    if risk_counts["high_failure"]:
        suggestions.append(
            f"{risk_counts['high_failure']} skill(s) fail at >= 50% "
            f"(at least {HIGH_FAILURE_MIN_USES} calls); check their docs or dependencies"
        )
    if risk_counts["frequently_edited"]:
        suggestions.append(
            f"{risk_counts['frequently_edited']} skill(s) have >= "
            f"{FREQUENTLY_EDITED_VERSIONS} versions, i.e. still changing often"
        )
    if not suggestions:
        suggestions.append("All skills look healthy")

    return {
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "windows": {
            "active_days": HEALTH_ACTIVE_DAYS,
            "stale_days": HEALTH_STALE_DAYS,
            "high_failure_min_uses": HIGH_FAILURE_MIN_USES,
            "high_failure_rate": HIGH_FAILURE_RATE,
            "frequently_edited_versions": FREQUENTLY_EDITED_VERSIONS,
            "min_sessions_for_advice": MIN_SESSIONS_FOR_ADVICE,
            "min_days_for_advice": MIN_DAYS_FOR_ADVICE,
        },
        "sample": sample,
        "counts": counts,
        "risk_counts": risk_counts,
        "skills": skills,
        "suggestions": suggestions,
    }


# ---------------------------------------------------------------------------
# Backup retention + auto-backup
# ---------------------------------------------------------------------------
def _parse_backup_name(path: str):
    m = BACKUP_RE.match(os.path.basename(path))
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def plan_retention(paths, now=None) -> dict:
    """Split backup paths into keep/delete. Pure function (no filesystem).

    Keeps the newest backup of each of the last 30 days plus the newest of each
    of the last 12 months. Files whose name does not match BACKUP_RE, or whose
    timestamp cannot be parsed, are never scheduled for deletion.
    """
    parsed, unparsed = [], []
    for p in paths:
        dt = _parse_backup_name(p)
        if dt is None:
            unparsed.append(p)
        else:
            parsed.append((p, dt))

    keep: set = set()
    by_day: dict = {}
    by_month: dict = {}
    for p, dt in parsed:
        by_day.setdefault(dt.date(), []).append((p, dt))
        by_month.setdefault((dt.year, dt.month), []).append((p, dt))

    for d in sorted(by_day.keys(), reverse=True)[:DAILY_KEEP]:
        keep.add(max(by_day[d], key=lambda t: t[1])[0])
    for mo in sorted(by_month.keys(), reverse=True)[:MONTHLY_KEEP]:
        keep.add(max(by_month[mo], key=lambda t: t[1])[0])

    return {
        "keep": sorted(keep),
        "delete": sorted(p for p, _ in parsed if p not in keep),
        "unparsed": sorted(unparsed),
    }


class BackupLockError(RuntimeError):
    """Raised when another auto-backup appears to be running."""


_LOCK_STALE_S = 600


@contextlib.contextmanager
def _backup_lock(lock_path: str):
    """O_EXCL lock with a staleness takeover so a crash cannot deadlock forever."""
    if os.path.exists(lock_path):
        try:
            age = time.time() - os.path.getmtime(lock_path)
        except OSError:
            age = _LOCK_STALE_S + 1
        if age > _LOCK_STALE_S:
            try:
                os.remove(lock_path)
            except OSError:
                pass
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise BackupLockError(
            f"another auto-backup appears to be running (lock: {lock_path})"
        )
    try:
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        yield
    finally:
        try:
            os.remove(lock_path)
        except OSError:
            pass


_LOCK_NAME = ".lock"


def _list_backup_files() -> list:
    """All regular files in BACKUP_DIR except our own lock file."""
    if not os.path.isdir(BACKUP_DIR):
        return []
    out = []
    for n in os.listdir(BACKUP_DIR):
        if n == _LOCK_NAME:
            continue
        p = os.path.join(BACKUP_DIR, n)
        if os.path.isfile(p):
            out.append(p)
    return out


def auto_backup(conn, dry_run: bool = False, now=None) -> dict:
    """Create a backup in BACKUP_DIR and prune old ones per plan_retention.

    Safety: only files matching BACKUP_RE inside BACKUP_DIR are ever deleted;
    files younger than MIN_BACKUP_AGE_S are skipped; a same-second name clash
    fails loudly instead of overwriting; a lock file serialises concurrent runs.
    """
    now = now or datetime.now(timezone.utc)
    target_name = f"skill-usage-backup-{now.strftime('%Y%m%d-%H%M%S')}.db"
    target = os.path.join(BACKUP_DIR, target_name)

    if dry_run:
        plan = plan_retention(_list_backup_files(), now)
        return {
            "dry_run": True, "backup": target, "created": False,
            "deleted": plan["delete"], "skipped_young": [],
            "kept": plan["keep"], "unparsed": plan["unparsed"],
            "backup_dir": BACKUP_DIR,
        }

    os.makedirs(BACKUP_DIR, mode=0o700, exist_ok=True)
    try:
        os.chmod(BACKUP_DIR, 0o700)
    except OSError:
        pass

    with _backup_lock(os.path.join(BACKUP_DIR, _LOCK_NAME)):
        created = backup_db(conn, target)  # refuses to clobber an existing name

        plan = plan_retention(_list_backup_files(), now)

        deleted, skipped = [], []
        for p in plan["delete"]:
            try:
                if time.time() - os.path.getmtime(p) < MIN_BACKUP_AGE_S:
                    skipped.append(p)
                    continue
                os.remove(p)
                deleted.append(p)
            except OSError:
                skipped.append(p)

    return {
        "dry_run": False, "backup": created, "created": True,
        "deleted": sorted(deleted), "skipped_young": sorted(skipped),
        "kept": plan["keep"], "unparsed": plan["unparsed"],
        "backup_dir": BACKUP_DIR,
    }



def _read_bounded_text(path: str, cap: int):
    """The first `cap` bytes of a log, and whether that cut anything off.

    The cap cuts from the *start* of the file on purpose. On the real log the
    failures were at the top and the filler at the bottom: a 64 KB tail reported
    0 of its 57 ERROR lines.
    """
    try:
        size = os.path.getsize(path)
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read(cap)
    except OSError:
        return None, 0, False
    return text, size, size > len(text.encode("utf-8", "replace"))


# ---------------------------------------------------------------------------
# Log rotation — the growth half of M17. The reader above stops at a cap; this
# keeps the file from outrunning it.
# ---------------------------------------------------------------------------

# The only two files this may ever move. A path argument is deliberately not part of
# the public surface: the neighbour's log lives in the same directory as ours, and
# "rotate a log" without a name whitelist is "rename any file in ~/.config/opencode".
LOG_FILE_NAMES = ("skill-tracker.log", "skill-tracker-selftest.log")


def _own_log(path: str) -> str:
    """Refuse any basename that is not ours. Returns the base name."""
    base = os.path.basename(path)
    if base not in LOG_FILE_NAMES:
        raise ValueError(
            f"refusing to rotate {base!r}: only {', '.join(LOG_FILE_NAMES)} are "
            f"this project's logs to move")
    return base


def _rotated_pattern(base: str):
    """`skill-tracker.log.<YYYYMMDD>-<HHMMSS>[-<n>]` — nothing else is a rotation of ours."""
    return re.compile(rf"^{re.escape(base)}\.\d{{8}}-\d{{6}}(-\d+)?$")


def _rotation_target(base: str, directory: str = ".") -> str:
    """A UTC stamp in the name, because the rows in the file are UTC timestamps.

    The suffix is a *filesystem* check, not a pattern check: two rotations inside one
    second would otherwise pick the same name, and `os.rename` onto an existing file
    replaces it — which is a lost generation of the log, silently.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    name = f"{base}.{stamp}"
    n = 1
    while os.path.exists(os.path.join(directory, name)):
        n += 1
        name = f"{base}.{stamp}-{n}"
    if n > 1000:
        raise OSError(f"too many log rotations inside {stamp}: giving up at {name}")
    return name


def plan_log_rotation(path: str, max_bytes: int, keep: int) -> dict:
    """What rotation would do, from a stat and a directory listing. Writes nothing."""
    base = _own_log(path)
    max_bytes = int(max_bytes)
    keep = int(keep)
    if max_bytes < 1 or keep < 1:
        raise ValueError(f"max_bytes must be >= 1 and keep >= 1, got {max_bytes}/{keep}")
    exists = os.path.isfile(path)
    size = os.path.getsize(path) if exists else 0
    pattern = _rotated_pattern(base)
    directory = os.path.dirname(path) or "."
    rotated = sorted((n for n in os.listdir(directory) if pattern.match(n)), reverse=True) \
        if os.path.isdir(directory) else []
    return {
        "path": path,
        "base": base,
        "exists": exists,
        "size": size,
        "max_bytes": max_bytes,
        "keep_files": keep,
        "needed": bool(exists and size > max_bytes),
        "target": _rotation_target(base, directory),
        "rotated": rotated,
        # Only ever names that matched the rotated shape for this base name.
        "prune": rotated[keep:] if len(rotated) > keep else [],
    }


def _prune_rotations(directory: str, names: list) -> list:
    """Delete the named rotation files, oldest first. Returns what actually went."""
    removed = []
    for name in names:
        try:
            os.remove(os.path.join(directory, name))
        except OSError:
            # A rotation file we cannot delete is reported, not retried blindly: the
            # live log is already small again, which is what the cap is for.
            continue
        removed.append(name)
    return removed


def rotate_log(path: str, max_bytes: int, keep: int, dry_run: bool = True) -> dict:
    """Move a too-big log aside and keep the newest `keep` rotations.

    A rename, never a deletion of content: the lines that leave the live file are the
    evidence `doctor log.errors` reads, so rotation preserves them under a name and
    prunes only *rotated* files beyond the number named.

    The live path is re-created empty rather than left missing, because a missing log
    makes `doctor` WARN "the plugin has not initialised" about a plugin that is
    running fine. It is created **without** `O_TRUNC`: the writer appends with
    `appendFileSync`, so a line can land between the rename and this call, and
    truncating here would eat it.
    """
    plan = plan_log_rotation(path, max_bytes, keep)
    directory = os.path.dirname(path) or "."
    result = {**{k: plan[k] for k in ("path", "size", "max_bytes", "keep_files",
                                      "needed", "target")},
              "dry_run": dry_run, "rotated": None, "pruned": [],
              "would_rotate": plan["needed"], "would_prune": plan["prune"]}
    if dry_run:
        return result
    if not plan["needed"]:
        # Nothing to move, but the backlog may still be longer than the number named.
        result["pruned"] = _prune_rotations(directory, plan["prune"])
        return result

    os.chmod(path, 0o600)          # the log carries paths; the rotated copy inherits
    # Recomputed rather than taken from the plan: a second rotation running
    # concurrently must not be renamed onto this one's target.
    target = _rotation_target(plan["base"], directory)
    os.rename(path, os.path.join(directory, target))
    result["rotated"] = target

    fd = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
    os.close(fd)

    result["pruned"] = _prune_rotations(directory, plan["prune"])
    return result


# ---------------------------------------------------------------------------
# The two read-only neighbours live in their own modules:
# `skill_db_agentos.py` and `skill_db_claude_mem.py`. They import this module;
# this one never imports them, so a name has one home and the dependency graph
# has no cycle. Consumers (the TUI, the CLI, the tests) import the neighbour
# module directly.
# ---------------------------------------------------------------------------
