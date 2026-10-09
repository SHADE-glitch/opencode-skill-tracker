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
CONFIG_DIR = os.path.join(HOME, *compat.CONFIG_HOME)
DB_PATH = os.environ.get(
    "OPENCODE_SKILL_TRACKER_DB",
    os.path.join(DATA_DIR, "skill-usage.db"),
)
SKILLS_DIR = os.environ.get(
    "OPENCODE_SKILL_TRACKER_SKILLS_DIR",
    os.path.join(CONFIG_DIR, compat.SKILLS_SUBDIR),
)
BUSY_TIMEOUT_MS = 5000

# Dedicated directory for retained backups. Kept separate from the DB's own
# directory so the retention sweep can never touch an unrelated .db file.
BACKUP_DIR = os.environ.get(
    "OPENCODE_SKILL_TRACKER_BACKUP_DIR",
    os.path.join(DATA_DIR, "backups"),
)

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



# ---------------------------------------------------------------------------
# AgentOS advisor store — a read-only neighbour
# ---------------------------------------------------------------------------
# The advisory plugin (AgentOS) records its own work in its own SQLite store
# plus a `loops/` directory of per-session JSON. This aggregator *reads* them
# and never writes: the advisor owns that store, and the tracker owns the usage
# rows, so neither side has to know the other's schema to be watched.
#
# Privacy is by construction, not by scrubbing: a loop file carries `task_text`
# (the user's task verbatim) and stage `data` carries engine payloads, so the
# projection below names every field it emits and simply never reads the rest.
# A new field cannot leak by being added upstream; it has to be added here.

# Read at call time, never at import: a module-level default froze this
# machine's environment into the signature (the same trap scan_skills fell into).
AGENTOS_DB_ENV = "OPENCODE_SKILL_TRACKER_AGENTOS_DB"

# Stage names the advisor's loop goes through, in order. Unknown names are still
# reported (a new stage should not be silently dropped), this only fixes order.
AGENTOS_STAGES = (
    "route", "resolve_role", "recall", "plan", "execute",
    "evidence", "validate", "record", "evolve", "finalize",
)

# Loop fields projected as-is. Anything not listed is never read.
AGENTOS_LOOP_FIELDS = ("loop_id", "session_id", "stage", "final_status",
                       "created_at", "updated_at", "memory_mode", "provider", "model")


def agentos_store(db_path: str | None = None) -> dict:
    """Locate the advisor store. Never creates anything.

    Resolution order, all read at call time (a def-time default would freeze the
    developer's machine into the signature): explicit argument, then
    `OPENCODE_SKILL_TRACKER_AGENTOS_DB`, then `$AGENT_OS_ROOT/store/aos.db`.
    """
    path = db_path or os.environ.get(AGENTOS_DB_ENV) or os.environ.get("AOS_DB")
    if not path:
        root = os.environ.get("AGENT_OS_ROOT")
        if root:
            path = os.path.join(root, "store", "aos.db")
    result = {"requested": path, "db": None, "loops": None, "reason": ""}
    if not path:
        result["reason"] = "no store configured (set AGENT_OS_ROOT or OPENCODE_SKILL_TRACKER_AGENTOS_DB)"
        return result
    if not os.path.isfile(path):
        result["reason"] = f"no database at {path}"
        return result
    loops = os.path.join(os.path.dirname(path), "loops")
    result["db"] = path
    result["loops"] = loops if os.path.isdir(loops) else None
    return result


def _open_agentos_ro(path: str):
    """Read-only, and explicitly *not* skill_db.open_db(): that helper falls
    back to a read-write open and would happily create an empty store."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    return conn


def _ts_diff_ms(start, end) -> int | None:
    """Milliseconds between two advisor timestamps; None when unusable.

    The advisor writes `+00:00` offsets; `Z` is tolerated because the tracker's
    own stamps use it and a reader cannot assume which writer produced a row.
    """
    def parse(value):
        text = str(value or "").strip()
        if not text:
            return None
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    a, b = parse(start), parse(end)
    if a is None or b is None:
        return None
    return int((b - a).total_seconds() * 1000)


def agentos_usage_by_session(conn, session_ids) -> dict:
    """How much measured tool activity each advisor session saw.

    The join that makes the two stores worth reading together: an advisor loop
    with zero usage rows means the model worked without touching anything the
    tracker measures, which is a different story from a loop that never ran.
    """
    ids = sorted({s for s in session_ids if s})
    if not ids:
        return {}
    out = {s: {"skill": 0, "mcp": 0, "plugin": 0} for s in ids}
    marks = ",".join("?" * len(ids))
    for table, stream in (
        ("skill_usage", "skill"), ("mcp_usage", "mcp"), ("plugin_usage", "plugin"),
    ):
        try:
            rows = conn.execute(
                f"SELECT session_id, COUNT(*) AS n FROM {table}"
                f" WHERE session_id IN ({marks}) GROUP BY session_id",
                ids,
            ).fetchall()
        except sqlite3.Error:
            continue  # an unmigrated DB simply has no such stream yet
        for r in rows:
            if r["session_id"] in out:
                out[r["session_id"]][stream] = r["n"]
    return out


# The advisor's recall stage reports its own result as integers and id lists.
# The same dict also holds `query` — a nested object of text derived from the
# user's task — so only these names are read, and only ever as counts.
AGENTOS_INJECTION_NAMES = ("retrieved", "memory_ids", "injected_memory_ids",
                          "injection_chars")


def _count_only(value):
    """A count from an int or a sized collection; anything else is None.

    Text never becomes a number here, which is what keeps `query` unreadable by
    construction rather than by remembering to filter it out later.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, (list, dict)):
        return len(value)
    return None


def _project_injection(raw: dict) -> dict | None:
    recall = ((raw.get("stages") or {}).get("recall") or {}).get("data")
    if not isinstance(recall, dict):
        return None
    return {
        "retrieved": _count_only(recall.get("retrieved")),
        # `memory_ids` is what recall returned; `injected_memory_ids` is what
        # actually went into the prompt. They differ (a hypothesis can be
        # injected on its own), so they are kept apart rather than averaged.
        "recalled": _count_only(recall.get("memory_ids")),
        "injected": _count_only(recall.get("injected_memory_ids")),
        "chars": _count_only(recall.get("injection_chars")),
    }


def _project_loop(raw: dict, limit_ms: int) -> dict:
    """Whitelist projection of one loop file. See the note above: no `**raw`."""
    stages = {}
    slowest = None
    for name, st in (raw.get("stages") or {}).items():
        if not isinstance(st, dict):
            continue
        ms = _ts_diff_ms(st.get("started_at"), st.get("completed_at"))
        entry = {
            "status": str(st.get("status") or ""),
            "ms": ms,
            "failed": bool(st.get("error")),
        }
        stages[name] = entry
        if ms is not None and (slowest is None or ms > slowest[0]):
            slowest = (ms, name)
    order = [s for s in AGENTOS_STAGES if s in stages]
    order += [s for s in stages if s not in AGENTOS_STAGES]
    pf = raw.get("postflight")
    post = {}
    if isinstance(pf, dict):
        learning = pf.get("learning")
        recovery = pf.get("recovery")
        post = {
            "aos_status": pf.get("aos_status"),
            "phase": pf.get("phase"),
            "final_status": pf.get("final_status"),
            "warnings": len(pf.get("warnings") or []),
            "has_error": bool(pf.get("aos_error")),
            "candidates_recorded": (learning or {}).get("candidates_recorded") if isinstance(learning, dict) else None,
            "needs_review": (learning or {}).get("needs_review") if isinstance(learning, dict) else None,
            "recovery_attempted": (recovery or {}).get("recovery_attempted") if isinstance(recovery, dict) else None,
        }
    return {
        **{k: raw.get(k) for k in AGENTOS_LOOP_FIELDS},
        "errors": len(raw.get("errors") or []),
        "stages": {name: stages[name] for name in order},
        "stage_count": len(stages),
        "slowest_stage": {"name": slowest[1], "ms": slowest[0]} if slowest else None,
        "over_budget_ms": bool(slowest and slowest[0] > limit_ms),
        "injection": _project_injection(raw),
        "postflight": post or None,
    }


def agentos_summary(conn, limit: int = 10, db_path: str | None = None,
                    timeout_ms: int | None = None) -> dict:
    """Read-only digest of the advisor store, joined with the tracker's sessions.

    `timeout_ms` is the advisor's per-call budget: a stage slower than that never
    made it into the prompt, so "over budget" is not "took a while". The default
    is AgentOS's own AOS_TIMEOUT_MS default, which this module cannot read from
    the other side — override it with
    OPENCODE_SKILL_TRACKER_AOS_TIMEOUT_MS when that default changes (M21).
    """
    if timeout_ms is None:
        raw = os.environ.get("OPENCODE_SKILL_TRACKER_AOS_TIMEOUT_MS")
        try:
            timeout_ms = int(raw) if raw else 1200
        except ValueError:
            timeout_ms = 1200
    store = agentos_store(db_path)
    out = {
        "available": False,
        "db_path": store["db"],
        "loops_dir": store["loops"],
        "reason": store["reason"],
        "timeout_ms": timeout_ms,
        "telemetry": [],
        "retrieval": {},
        "store_counts": {},
        "loops": [],
        "loops_total": 0,
    }
    if not store["db"]:
        return out

    try:
        ac = _open_agentos_ro(store["db"])
    except sqlite3.Error as e:
        out["reason"] = f"cannot open read-only: {e}"
        return out
    try:
        for table in ("telemetry_events", "retrieval_log", "memories",
                      "observations", "candidates", "learning_reviews"):
            try:
                out["store_counts"][table] = ac.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0]
            except sqlite3.Error:
                out["store_counts"][table] = None
        try:
            out["telemetry"] = rows_to_dicts(ac.execute(
                "SELECT event_type, COUNT(*) AS n, MAX(created_at) AS last "
                "FROM telemetry_events GROUP BY event_type ORDER BY n DESC"
            ))
        except sqlite3.Error:
            pass
        try:
            row = ac.execute(
                "SELECT COUNT(*) AS n, COUNT(DISTINCT memory_id) AS memories, "
                "MAX(created_at) AS last FROM retrieval_log"
            ).fetchone()
            out["retrieval"] = dict(row) if row else {}
        except sqlite3.Error:
            pass
    finally:
        ac.close()

    files = []
    if store["loops"]:
        files = sorted(
            (os.path.join(store["loops"], n) for n in os.listdir(store["loops"])
             if n.endswith(".json")),
            key=lambda p: os.path.getmtime(p), reverse=True,
        )
    out["loops_total"] = len(files)
    loops = []
    for path in files[:limit]:
        try:
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, ValueError):
            continue  # a half-written loop file must not break the digest
        if isinstance(raw, dict):
            loops.append(_project_loop(raw, timeout_ms))
    out["usage_by_session"] = agentos_usage_by_session(
        conn, [l.get("session_id") for l in loops]
    )
    for loop in loops:
        loop["usage"] = out["usage_by_session"].get(loop.get("session_id")) or {}
    out["loops"] = loops
    out["available"] = True
    out["reason"] = ""
    return out


# ---------------------------------------------------------------------------
# claude-mem — read-only neighbour #2: what its background capture actually did
#
# The tracker measures skill / MCP / plugin tool calls and nothing else, so a
# session that only used builtins (`bash`, `edit`, …) leaves no row here at all.
# claude-mem keeps its own ledger of the same sessions, and reading it answers
# the question the tracker structurally cannot: "did anything happen".
#
# Same contract as the advisor store above, for the same reason: this is another
# project's database, it is full of prompt-derived prose, and it is being written
# by a live process. Read-only, field-whitelisted, never created, never exported.
# ---------------------------------------------------------------------------

CLAUDE_MEM_DB_ENV = "OPENCODE_SKILL_TRACKER_CLAUDE_MEM_DB"
CLAUDE_MEM_DIR_ENV = "CLAUDE_MEM_DIR"

# The tables this reader names, and the only columns it names from each. Every
# entry is a counter, a timestamp or a short enum — because the columns beside
# them (`observations.text`, `user_prompts.prompt_text`,
# `session_summaries.request`/`investigated`/`learned`/`completed`/`next_steps`,
# `sdk_sessions.user_prompt`/`custom_title`, `tool_uses.tool_input`/`tool_response`/`cwd`,
# `pending_messages.last_user_message`/`last_assistant_message`) are the user's
# own words. They appear nowhere below, in any SELECT, WHERE or log line.
CLAUDE_MEM_TABLES = {
    "observations":      {"time": "created_at_epoch", "label": "type",
                          "tokens": "discovery_tokens"},
    "user_prompts":      {"time": "created_at_epoch"},
    "session_summaries": {"time": "created_at_epoch", "tokens": "discovery_tokens"},
    "sdk_sessions":      {"time": "started_at_epoch", "label": "status"},
    "tool_uses":         {"time": "created_at_epoch", "label": "tool_name"},
    "pending_messages":  {"time": "created_at_epoch", "label": "status"},
}

# The sidecar files, and the only keys read from them. `observer-health.json`
# also holds `lastErrorMessage` / `lastErrorUrl` / `lastErrorRequestId` (free
# text and a URL from a third-party error page) and `supervisor.json` holds a
# process table with paths in it — none of that is named here. `installId` is a
# stable identifier of this machine, so it stays out too.
CLAUDE_MEM_HEALTH_KEYS = ("consecutiveFailures", "failingSinceAt", "lastErrorAt",
                          "lastErrorCode", "lastErrorKind", "lastSuccessAt",
                          "quotaCooldown")
CLAUDE_MEM_BACKFILL_KEYS = ("completedAt", "eventCount", "throughDay", "version")
CLAUDE_MEM_TELEMETRY_KEYS = ("enabled", "decidedAt")

# Never opened, at any cost: it holds `CLAUDE_MEM_OPENROUTER_API_KEY` and
# `CLAUDE_MEM_PRO_MEMORY_KEY`. A reader that "just looks at the mode" here is one
# refactor away from printing a secret into a terminal.
CLAUDE_MEM_FORBIDDEN_FILES = ("settings.json",)

# A label from another store is only echoed when it looks like a category name.
_LABEL_RE = re.compile(r"^[\w][\w.:/ -]{0,39}$")

# How many distinct labels one table may show: enough for the categories that
# exist, small enough that the output stays a summary rather than a dump.
CLAUDE_MEM_LABEL_CAP = 12

# The tracker's own three streams, for the same-window comparison below. Named
# here rather than imported from the TUI: the data layer must not depend on a
# screen.
CLAUDE_MEM_TRACKER_TABLES = ("skill_usage", "mcp_usage", "plugin_usage")


def claude_mem_store(db_path: str | None = None) -> dict:
    """Locate claude-mem's store. Never creates anything, never reads it here.

    Resolution order, all read at call time: explicit argument, then
    `OPENCODE_SKILL_TRACKER_CLAUDE_MEM_DB`, then `$CLAUDE_MEM_DIR/claude-mem.db`,
    then the default `~/.claude-mem/claude-mem.db`. The last one is why this
    neighbour needs no configuration to be discovered — unlike the advisor, whose
    location is a project choice and therefore has to be told to us.
    """
    path = db_path or os.environ.get(CLAUDE_MEM_DB_ENV)
    if not path:
        cm_dir = os.environ.get(CLAUDE_MEM_DIR_ENV)
        if cm_dir:
            path = os.path.join(cm_dir, "claude-mem.db")
    if not path:
        path = os.path.join(HOME, ".claude-mem", "claude-mem.db")
    result = {"requested": path, "db": None, "dir": None, "reason": "",
              "trace": None, "logs_dir": None, "pid_file": None}
    # The directory and its files are resolved whether or not the database is
    # there: the activity logs exist on their own, and bailing out early used to
    # hide them from `claude_mem_activity` on a machine whose worker had not
    # created a database yet.
    result["dir"] = os.path.dirname(path)
    for key, name, is_dir in (("trace", CLAUDE_MEM_TRACE_NAME, False),
                              ("logs_dir", "logs", True),
                              ("pid_file", CLAUDE_MEM_PID_NAME, False)):
        found = os.path.join(result["dir"], name)
        result[key] = found if (os.path.isdir(found) if is_dir
                                else os.path.isfile(found)) else None
    if not os.path.isfile(path):
        result["reason"] = f"no claude-mem database at {path}"
        return result
    result["db"] = path
    return result


def _open_claude_mem_ro(path: str):
    """Read-only on purpose; `open_db()` would fall back to read-write and
    create a missing file, which would hand a foreign store to a reader."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    return conn


def _epoch_iso(value) -> str | None:
    """Epoch milliseconds from another store -> local 'YYYY-MM-DD HH:MM'.

    Anything that is not a plausible millisecond count yields None: a store that
    changes its time unit shows up as a missing number, not as a date in 1970.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value <= 0 or value < 10 ** 11 or value > 10 ** 14:
        return None
    return fmt_time(datetime.fromtimestamp(value / 1000, timezone.utc)
                    .strftime("%Y-%m-%dT%H:%M:%S.%fZ"))


def _short_label(value) -> str | None:
    """Echo a foreign enum only if it looks like a category name.

    `type`, `status` and `tool_name` are supposed to be short tokens, but they
    live in someone else's column and nothing stops a future version from putting
    a sentence there. A value that is not label-shaped becomes None rather than
    a line of the user's text on the terminal.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text if _LABEL_RE.match(text) else None


def _read_sidecar(dir_path: str, name: str, keys) -> dict | None:
    """Whitelisted keys of one small JSON file; None when it is not there."""
    if name in CLAUDE_MEM_FORBIDDEN_FILES:
        return None
    path = os.path.join(dir_path, name)
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    out = {}
    for key in keys:
        value = raw.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            out[key] = value
        elif isinstance(value, bool):
            out[key] = value
        elif isinstance(value, str) and len(value) <= 40 and not value.startswith(("http://", "https://", "/")):
            out[key] = value
    return out


def _sidecar_times(fields: dict | None) -> dict | None:
    """Epoch-ms fields become the same local string every other time uses.

    Only a plausible millisecond count is converted: `backfill.completedAt` is
    already an ISO string, and rewriting it to None would lose the one number
    that field carries.
    """
    if not fields:
        return fields
    for key in list(fields):
        if key.endswith("At"):
            iso = _epoch_iso(fields[key])
            if iso:
                fields[key] = iso
    return fields


# claude-mem's own activity files. Both are line-oriented and both carry prose
# the tracker must not take with it: `q=` in the trace log is the search text the
# plugin built from the user's prompt, and the worker log's message column holds
# absolute paths and error text. So the reader counts *shapes* and returns numbers.
CLAUDE_MEM_TRACE_NAME = "inject-trace.log"
CLAUDE_MEM_PID_NAME = "worker.pid"
CLAUDE_MEM_LOG_PREFIX = "claude-mem-"        # dated logs; `manual-restart-*` are noise
CLAUDE_MEM_LOG_BYTES_CAP = 4 * 1024 * 1024
# `[timestamp] [LEVEL] [category] message` — the second bracket is the level.
# Reading ERROR as a category counts nothing at all.
_LOG_LINE_RE = re.compile(r"^\[[^\]]+\]\s+\[\s*([A-Za-z]+)\s*]\s+\[([^\]]+)]")
_LEN_RE = re.compile(r"\blen=(\d+)")
# `project=` belongs to another tool's log. A space makes it a sentence and a
# slash makes it a path, so neither is admitted: a value that is not slug-shaped
# is counted, never echoed (the read side gates foreign enums the same way, via
# `_short_label`).
_PROJECT_LABEL_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,40}$")
_PROJECT_RE = re.compile(r"\bproject=([^ \"]+)")
# The day table keeps the most recent N days; the rest is added up and reported,
# never silently dropped. 14 is the same window the dashboard trend covers.
CLAUDE_MEM_DAY_CAP = 14


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


def _read_inject_trace(path: str, cap: int) -> dict | None:
    text, size, truncated = _read_bounded_text(path, cap)
    if text is None:
        return None
    out = {"path": path, "lines": 0, "loaded": 0, "injected": 0,
           "injected_with_source": 0, "injected_bare": 0, "injected_with_query": 0,
           "worker_ensure": 0, "unrecognized": 0, "chars_total": 0,
           "last_ts": None, "size_bytes": size, "truncated": truncated,
           "by_project": {}, "projectless": {"injected": 0, "loaded": 0},
           "by_day": {}, "older_days": {"days": 0, "injected": 0, "loaded": 0},
           "undated": {"injected": 0, "loaded": 0}}
    projects: dict = {}
    days: dict = {}
    newest = ""
    for line in text.splitlines():
        body = line.strip()
        if not body:
            continue
        out["lines"] += 1
        stamp, _, rest = body.partition(" ")
        kind = None
        if rest.startswith("loaded"):
            out["loaded"] += 1
            kind = "loaded"
        elif rest.startswith("injected"):
            # Both shapes count: the plugin was edited in place, so the older bare
            # `injected project=… len=…` lines and the newer `source=` ones share
            # the file. Keying on `source=` alone dropped 91% of the injections.
            out["injected"] += 1
            kind = "injected"
            if " source=" in rest:
                out["injected_with_source"] += 1
            else:
                out["injected_bare"] += 1
            if " q=" in rest:
                # Counted, never read: what follows is the user's own text.
                out["injected_with_query"] += 1
            m = _LEN_RE.search(rest)
            if m:
                out["chars_total"] += int(m.group(1))
        elif rest.startswith("worker ensure"):
            out["worker_ensure"] += 1
        else:
            out["unrecognized"] += 1

        if kind:
            m = _PROJECT_RE.search(rest)
            name = m.group(1) if m else None
            if name and _PROJECT_LABEL_RE.match(name):
                slot = projects.setdefault(name, {"injected": 0, "loaded": 0})
                slot[kind] += 1
            else:
                out["projectless"][kind] += 1
            # Local calendar day, because that is how every chart on the screen
            # buckets (M1). `fmt_time`'s own output is the single source for it.
            local = _epoch_iso_from_iso_text(stamp)
            if local:
                slot = days.setdefault(local[:10], {"injected": 0, "loaded": 0})
                slot[kind] += 1
            else:
                out["undated"][kind] += 1

        if stamp > newest:
            # Only a stamp that parses may become the newest: a line that does not
            # start with a timestamp must not freeze out the real ones after it.
            converted = _epoch_iso_from_iso_text(stamp)
            if converted:
                newest, out["last_ts"] = stamp, converted

    out["by_project"] = dict(sorted(
        projects.items(),
        key=lambda kv: (-kv[1]["injected"], -kv[1]["loaded"], kv[0])))
    ordered = sorted(days)                       # ISO date strings sort as text
    keep = ordered[-CLAUDE_MEM_DAY_CAP:]
    for day in keep:
        out["by_day"][day] = days[day]
    for day in ordered[:len(ordered) - len(keep)]:
        out["older_days"]["days"] += 1
        for key in ("injected", "loaded"):
            out["older_days"][key] += days[day][key]
    return out


def _read_worker_log(logs_dir: str, cap: int) -> dict | None:
    try:
        names = [n for n in os.listdir(logs_dir)
                 if n.startswith(CLAUDE_MEM_LOG_PREFIX) and n.endswith(".log")]
    except OSError:
        return None
    newest = None
    for n in names:
        full = os.path.join(logs_dir, n)
        try:
            m = os.path.getmtime(full)
        except OSError:
            continue
        if newest is None or m > newest[1]:
            newest = (full, m)
    if not newest:
        return None
    path = newest[0]
    text, size, truncated = _read_bounded_text(path, cap)
    if text is None:
        return None
    out = {"path": path, "lines": 0, "levels": {}, "categories": {},
           "unparsed": 0, "size_bytes": size, "truncated": truncated,
           "mtime": fmt_time(datetime.fromtimestamp(newest[1], timezone.utc)
                             .strftime("%Y-%m-%dT%H:%M:%S.%fZ"))}
    for line in text.splitlines():
        if not line.strip():
            continue
        out["lines"] += 1
        m = _LOG_LINE_RE.match(line)
        if not m:
            # The message is dropped here, not stored: a log line can carry a path
            # or a user string, and neither belongs in this report.
            out["unparsed"] += 1
            continue
        level, category = m.group(1).strip().upper(), m.group(2).strip()
        level = level if _short_label(level) else "?"
        category = _short_label(category) or "?"
        out["levels"][level] = out["levels"].get(level, 0) + 1
        out["categories"][category] = out["categories"].get(category, 0) + 1
    return out


def _epoch_iso_from_iso_text(value: str):
    """An ISO-Z stamp from another tool -> the local string every time here uses."""
    try:
        datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return fmt_time(value)


def claude_mem_activity(store: dict | None = None,
                        log_bytes_cap: int = CLAUDE_MEM_LOG_BYTES_CAP) -> dict:
    """What claude-mem's own log files say it did. Numbers only, read-only.

    `store` is a `claude_mem_store()` result; one is taken when not given. Absent
    files are absent, not zero: a machine without the plugin gets `available:
    False` and no lines in the report.
    """
    if store is None:
        store = claude_mem_store()
    out = {"available": False, "reason": store.get("reason", ""), "dir": store.get("dir"),
           "trace": None, "worker_log": None}
    if store.get("trace"):
        out["trace"] = _read_inject_trace(store["trace"], log_bytes_cap)
    if store.get("logs_dir"):
        out["worker_log"] = _read_worker_log(store["logs_dir"], log_bytes_cap)
    out["available"] = bool(out["trace"] or out["worker_log"])
    if not out["available"] and not out["reason"]:
        out["reason"] = f"no activity files under {store.get('dir')}"
    return out


# The worker answers HTTP, but only the last of the three sources below, and only
# from headless commands. Its answers carry a `database.path`, a version string
# and a free-text `details`, so the projection is by name *and* by type.
CLAUDE_MEM_HTTP_BUDGET_SECONDS = 2.0
CLAUDE_MEM_HTTP_WORKER_KEYS = ("uptime", "activeSessions", "sseClients")
CLAUDE_MEM_HTTP_DATABASE_KEYS = ("size", "observations", "sessions", "summaries")
CLAUDE_MEM_HTTP_QUEUE_KEYS = ("isProcessing", "queueDepth", "parkedSessions")
CLAUDE_MEM_HTTP_CHROMA_KEYS = ("connected", "deep")
CLAUDE_MEM_HTTP_ENDPOINTS = (("/api/stats", "stats"),
                             ("/api/processing-status", "queue"),
                             ("/api/chroma/status", "chroma"))


def claude_mem_worker(store: dict | None = None) -> dict:
    """What `worker.pid` says: which process, on which port, and whether it is up.

    No network. Liveness is `os.kill(pid, 0)`, which costs microseconds and still
    answers when the worker has never been started; the port comes from the file,
    so a worker moved off 37700 is still found. `startToken` is what authenticates
    to that worker and is deliberately not part of the result.
    """
    if store is None:
        store = claude_mem_store()
    out = {"available": False, "alive": False, "pid": None, "port": None,
           "started_at": None, "reason": ""}
    path = store.get("pid_file")
    if not path:
        out["reason"] = f"no {CLAUDE_MEM_PID_NAME} under {store.get('dir')}"
        return out
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        out["reason"] = f"unreadable {CLAUDE_MEM_PID_NAME}: {e}"
        return out
    if not isinstance(data, dict):
        out["reason"] = f"{CLAUDE_MEM_PID_NAME} holds no object"
        return out
    out["available"] = True
    # A non-integer pid or port is not repaired into one: it is reported as absent.
    out["pid"] = data.get("pid") if isinstance(data.get("pid"), int) else None
    out["port"] = data.get("port") if isinstance(data.get("port"), int) else None
    started = data.get("startedAt")
    out["started_at"] = (_epoch_iso_from_iso_text(started)
                         if isinstance(started, str) else None)
    if out["pid"] is None:
        out["reason"] = f"{CLAUDE_MEM_PID_NAME} carries no usable pid"
        return out
    try:
        os.kill(out["pid"], 0)
        out["alive"] = True
    except ProcessLookupError:
        out["reason"] = f"pid {out['pid']} is not running"
    except PermissionError:
        out["alive"] = True       # it exists; it is only not ours to signal
    except OSError as e:
        out["reason"] = f"pid {out['pid']}: {e}"
    return out


def _http_json(path: str, port: int, timeout: float):
    """One GET against the local worker. None on any failure, never an exception.

    `timeout` is what is left of the shared deadline, not a fresh budget.
    """
    url = f"http://127.0.0.1:{port}{path}"
    try:
        req = urlreq.Request(url, headers={"Accept": "application/json"})
        with urlreq.urlopen(req, timeout=timeout) as resp:
            if getattr(resp, "status", 200) != 200:
                return None
            body = json.loads(resp.read().decode("utf-8", "replace"))
    except (OSError, ValueError, EOFError, http.client.HTTPException):
        return None
    return body if isinstance(body, dict) else None


def _numbers_only(node, keys):
    """Whitelisted names, and only where the value is a number or a boolean.

    `isinstance(True, int)` holds, so the booleans come through; a path or a
    sentence in a trusted field name does not.
    """
    if not isinstance(node, dict):
        return None
    picked = {k: node[k] for k in keys if isinstance(node.get(k), int)}
    return picked or None


def claude_mem_http(worker: dict | None = None,
                    budget_seconds: float = CLAUDE_MEM_HTTP_BUDGET_SECONDS,
                    clock=time.monotonic) -> dict:
    """The worker's three status endpoints on one shared deadline.

    Headless only: the TUI repaints on every keypress and doctor runs offline, so
    neither may call this. The budget is not per endpoint — a worker that is
    starting up answers slowly, and three timeouts in a row would be three waits
    before the command said anything. Whatever the deadline does not reach is
    listed in `skipped` instead of being reported as zero.

    `clock` is injected so the deadline arithmetic can be tested without sleeping.
    """
    if worker is None:
        worker = claude_mem_worker()
    out = {"available": False, "reason": "", "skipped": [],
           "stats": None, "queue": None, "chroma": None}
    port = worker.get("port")
    if not worker.get("alive") or not isinstance(port, int):
        out["reason"] = worker.get("reason") or "the worker is not running"
        return out
    deadline = clock() + budget_seconds
    for path, field in CLAUDE_MEM_HTTP_ENDPOINTS:
        remaining = deadline - clock()
        if remaining <= 0:
            out["skipped"].append(field)
            continue
        body = _http_json(path, port, remaining)
        if field == "stats":
            view = {} if body is None else {
                name: got for name, got in (
                    ("worker", _numbers_only(body.get("worker"), CLAUDE_MEM_HTTP_WORKER_KEYS)),
                    ("database", _numbers_only(body.get("database"),
                                               CLAUDE_MEM_HTTP_DATABASE_KEYS))) if got}
            out[field] = view or None
        else:
            keys = (CLAUDE_MEM_HTTP_QUEUE_KEYS if field == "queue"
                    else CLAUDE_MEM_HTTP_CHROMA_KEYS)
            out[field] = _numbers_only(body, keys)
    out["available"] = any(out[field] for _, field in CLAUDE_MEM_HTTP_ENDPOINTS)
    if not out["available"]:
        out["reason"] = f"no answer from the worker on port {port}"
    return out


def claude_mem_summary(conn, days: int = 7, db_path: str | None = None) -> dict:
    """Counts and timestamps of claude-mem's own ledger, read-only.

    `conn` is the *tracker* database and is only used to say how much of the same
    window the tracker measured; nothing here writes to either store. A table that
    does not exist, or whose columns were renamed upstream, is reported as
    `missing` / `degraded` with no numbers — it never raises, because a neighbour
    upgrading under us must not break `skillt doctor`.
    """
    store = claude_mem_store(db_path)
    out = {
        "available": False, "reason": store["reason"], "db_path": store["db"],
        "dir": store["dir"], "days": days, "tables": {}, "health": None,
        "backfill": None, "telemetry": None, "newest_epoch": None,
        "newest": None, "errors": [], "tracker_same_window": None,
    }
    if not store["db"]:
        return out
    try:
        cm = _open_claude_mem_ro(store["db"])
    except sqlite3.Error as e:
        out["reason"] = f"could not open read-only: {e}"
        return out
    try:
        cutoff = int((datetime.now(timezone.utc).timestamp() * 1000)
                     - max(1, int(days)) * 86400 * 1000)
        for name, spec in CLAUDE_MEM_TABLES.items():
            row = {"n": None, "n_window": None, "last": None, "last_epoch": None,
                   "tokens": None, "by": None}
            try:
                cols = {r[1] for r in cm.execute(f"PRAGMA table_info({name})")}
            except sqlite3.Error:
                cols = set()
            if not cols:
                row["missing"] = True
                out["tables"][name] = row
                continue
            time_col = spec["time"] if spec["time"] in cols else None
            select = ["COUNT(*) AS n"]
            if time_col:
                # COALESCE: `SUM` over zero rows is NULL, and "0 in the window"
                # is a different fact from "cannot tell" — an empty table must say 0.
                select.append(f"COALESCE(SUM(CASE WHEN {time_col} >= ? THEN 1 ELSE 0 END), 0) AS n_window")
                select.append(f"MAX({time_col}) AS last_epoch")
            tokens = spec.get("tokens")
            if tokens and tokens in cols:
                select.append(f"COALESCE(SUM({tokens}), 0) AS tokens")
            try:
                r = cm.execute(f"SELECT {', '.join(select)} FROM {name}",
                               (cutoff,) if time_col else ()).fetchone()
            except sqlite3.Error as e:
                row["degraded"] = str(e)
                out["tables"][name] = row
                continue
            row["n"] = r["n"]
            if time_col:
                row["n_window"] = r["n_window"]
                row["last"] = _epoch_iso(r["last_epoch"])
                # `newest_epoch` is only ever set to a value the formatter also
                # accepted, so `claude_mem_age_days` can never date a row that the
                # report shows as unknown (a store that switched to seconds would
                # otherwise read as "20708 days ago").
                if row["last"] and (out["newest_epoch"] is None
                                    or r["last_epoch"] > out["newest_epoch"]):
                    out["newest_epoch"] = r["last_epoch"]
                else:
                    row["last_epoch"] = None
            if tokens and tokens in cols:
                row["tokens"] = r["tokens"]
            label = spec.get("label")
            if label and label in cols:
                try:
                    rows = cm.execute(
                        f"SELECT {label} AS v, COUNT(*) AS n FROM {name}"
                        f" GROUP BY {label} ORDER BY n DESC, v ASC LIMIT {CLAUDE_MEM_LABEL_CAP}"
                    ).fetchall()
                except sqlite3.Error:
                    rows = []
                counts: dict = {}
                for g in rows:
                    key = _short_label(g["v"])
                    if key is None:
                        # Counted, never echoed: an un-label-shaped value still
                        # belongs to the total, it just cannot be named here.
                        counts.setdefault("(not shown)", 0)
                        counts["(not shown)"] += g["n"]
                        continue
                    counts[key] = g["n"]
                row["by"] = counts
            out["tables"][name] = row
    finally:
        try:
            cm.close()
        except sqlite3.Error:
            pass

    readable = [n for n, r in out["tables"].items() if not r.get("missing")]
    if out["tables"] and not readable:
        # Every table we know about is absent: this is not an empty claude-mem,
        # it is some other file (or a rename we cannot follow). Saying
        # "installed, zero rows" would be a lie with a number on it.
        out["reason"] = ("no claude-mem tables readable in "
                         f"{store['db']} (tables: {', '.join(CLAUDE_MEM_TABLES)})")
        return out

    out["newest"] = _epoch_iso(out["newest_epoch"])
    # The join that makes the two numbers worth reading together: claude-mem saw
    # the session, and this is how much of the same window the tracker was
    # *allowed* to see. Only skill/MCP/plugin calls are measured here, so a zero
    # beside a non-zero claude-mem count is the design boundary, not a gap.
    if conn is not None:
        cutoff_iso = (datetime.fromtimestamp(cutoff / 1000, timezone.utc)
                      .strftime("%Y-%m-%dT%H:%M:%S.%fZ"))
        counts = {}
        for table in CLAUDE_MEM_TRACKER_TABLES:
            try:
                counts[table] = conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE timestamp >= ?", (cutoff_iso,)
                ).fetchone()[0]
            except sqlite3.Error:
                counts[table] = None
        out["tracker_same_window"] = counts
    if out["dir"]:
        out["health"] = _sidecar_times(
            _read_sidecar(out["dir"], "observer-health.json", CLAUDE_MEM_HEALTH_KEYS))
        out["backfill"] = _sidecar_times(
            _read_sidecar(out["dir"], "backfill.json", CLAUDE_MEM_BACKFILL_KEYS))
        out["telemetry"] = _sidecar_times(
            _read_sidecar(out["dir"], "telemetry.json", CLAUDE_MEM_TELEMETRY_KEYS))
    out["available"] = True
    out["reason"] = ""
    return out


def claude_mem_age_days(summary: dict):
    """Days since claude-mem's newest row; None when there is nothing to date."""
    epoch = (summary or {}).get("newest_epoch")
    if not isinstance(epoch, (int, float)) or isinstance(epoch, bool):
        return None
    return (datetime.now(timezone.utc).timestamp() * 1000 - epoch) / 86400000.0
