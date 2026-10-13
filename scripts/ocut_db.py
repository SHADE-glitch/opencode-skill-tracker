"""ocut_db — storage layer for the OpenCode usage tracker.

Defines the SQLite schema (tables + views) and the small helpers shared by the
indexer (ocut-sync.py), the headless CLI (ocut-stats.py) and the TUI
(ocut-tui.py).

The tracker is a *passive reader*: it never writes into OpenCode's own
directories. It only reads the host's data and config homes and writes this one
database.

Standard library only.
"""

from __future__ import annotations

import importlib.util
import os
import re
import sqlite3
import sys
import tomllib
from datetime import datetime, timedelta
from pathlib import Path
from typing import NamedTuple

SCHEMA_VERSION = 2

# --- locations -------------------------------------------------------------

# OpenCode keeps its own state in two places, and the tracker reads both without
# ever opening either for writing: the data home holds `opencode.db`, the config
# home holds the skills/agents/commands it has installed.
HOST_DATA_DIR = Path(
    os.environ.get("OCUT_OPENCODE_DATA",
                   str(Path.home() / ".local" / "share" / "opencode"))
)
HOST_CONFIG_DIR = Path(
    os.environ.get("OCUT_OPENCODE_CONFIG", str(Path.home() / ".config" / "opencode"))
)
# The host's own database. This is the source of truth for every panel: sessions,
# per-step usage, per-call tool records. It is *never* written, and it is opened
# with `mode=ro` so a bug in this file cannot make that claim false. It is also
# never opened through `open_db` below: that function creates what it opens, and an
# empty `opencode.db` left in somebody's data directory would be a destructive bug.
HOST_DB = Path(os.environ.get("OCUT_HOST_DB", str(HOST_DATA_DIR / "opencode.db")))

DB_PATH = Path(
    os.environ.get(
        "OCUT_DB",
        str(Path.home() / ".local" / "share" / "opencode-usage-tracker" / "usage.db"),
    )
)

# The sidecar `plugin/ocut-hook.js` appends to. It is the one input to this
# database that does not come from OpenCode's own files, so it lives here beside
# the tracker's own store rather than under OpenCode's data home. The hook and the
# reader must agree on the path; `OCUT_HOOK_EVENTS` moves both.
PLUGIN_EVENTS = Path(
    os.environ.get(
        "OCUT_HOOK_EVENTS",
        str(Path.home() / ".local" / "share" / "opencode-usage-tracker"
            / "plugin-events.jsonl"),
    )
)


# --- settings --------------------------------------------------------------
#
# One file, outside the repository, read with the standard library's own TOML
# parser (``tomllib``, Python 3.11+ — which is the floor this project already
# promises), so the settings layer cost no dependency. Precedence is environment
# > file > DEFAULTS.
#
# A file that cannot be read is an error, not a fallback. Someone editing
# ``~/.config/ocut/config.toml`` and being silently ignored is the worst outcome
# this layer can produce, so every failure names the path or variable and the key.

CONFIG_PATH = Path(os.environ.get("OCUT_CONFIG") or (
    Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    / "ocut" / "config.toml"
))

# Defaults are the values the interface already shipped with. Changing one here is
# a behaviour change, and ``test_settings.py`` pins each against the constant it
# replaces.
DEFAULTS = {
    "top_n": 5,           # rows in the Dashboard's Top models / Top tools
    "log_limit": 100,     # Usage page request rows
    "detail_limit": 200,  # rows on a history / model-response screen
    "name_cap": 28,       # widest cell a table renders; 28 is measured on real
                          # data, not a round number — every entity name fits it,
                          # only the project path does not. See NAME_CAP in
                          # ocut-tui.py.
    "refresh_secs": 5,    # TUI redraw timer
    "sync_secs": 30,      # TUI auto-sync timer
}

ENV_NAMES = {
    "top_n": "OCUT_TOP_N",
    "log_limit": "OCUT_LOG_LIMIT",
    "detail_limit": "OCUT_DETAIL_LIMIT",
    "name_cap": "OCUT_NAME_CAP",
    "refresh_secs": "OCUT_REFRESH_SECS",
    "sync_secs": "OCUT_SYNC_SECS",
}

# (low, high, accepted types). Ranges are wide enough to be useful and tight
# enough that a typo fails loudly: 0 rows of anything is not a preference, it is a
# blank panel.
_BOUNDS = {
    "top_n": (1, 100, int),
    "log_limit": (1, 1000, int),
    "detail_limit": (1, 1000, int),
    # below 8 nothing readable survives the cut; above ~200 the cap is not a cap
    "name_cap": (8, 200, int),
    "refresh_secs": (0.5, 3600, (int, float)),
    "sync_secs": (1, 86400, (int, float)),
}


class ConfigError(ValueError):
    """The settings file, or an environment value, is not usable."""


def _setting_value(key, value, source):
    low, high, kind = _BOUNDS[key]
    # bool is an int subclass: ``top_n = true`` must not quietly become top_n = 1.
    if isinstance(value, bool) or not isinstance(value, kind):
        raise ConfigError(
            f"{source}: {key} = {value!r} must be "
            f"{'a whole number' if kind is int else 'a number'}")
    if not low <= value <= high:
        raise ConfigError(f"{source}: {key} = {value!r} is outside {low}..{high}")
    return value


def load_config(path=None):
    """Resolve settings from environment, then file, then ``DEFAULTS``.

    Never raises for a *missing* file — that is the normal case — but raises
    ``ConfigError`` for a malformed one, an unknown key, or an unusable value in
    either source.
    """
    cfg = dict(DEFAULTS)
    source = Path(path if path is not None else CONFIG_PATH)
    if source.is_file():
        try:
            parsed = tomllib.loads(source.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{source}: not valid TOML ({exc})") from None
        for key, value in parsed.items():
            if key not in DEFAULTS:
                raise ConfigError(
                    f"{source}: unknown setting {key!r} — known keys are "
                    f"{', '.join(sorted(DEFAULTS))}")
            cfg[key] = _setting_value(key, value, str(source))
    for key, env in ENV_NAMES.items():
        raw = os.environ.get(env)
        if raw is None:
            continue
        try:
            value = int(raw)
        except ValueError:
            try:
                value = float(raw)
            except ValueError:
                raise ConfigError(f"{env}={raw!r} is not a number") from None
        cfg[key] = _setting_value(key, value, f"{env} environment variable")
    return cfg


def load_sync():
    """Import ``ocut-sync.py`` — a hyphenated name cannot be imported normally.

    The TUI's sync worker, ``ocut format`` and the tests all need the parser as a
    module, so the loader lives here instead of being copied a third time.
    """
    name = "ocut_sync_embedded"
    mod = sys.modules.get(name)
    if mod is None:
        path = Path(__file__).with_name("ocut-sync.py")
        spec = importlib.util.spec_from_file_location(name, str(path))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    return mod


# --- schema ----------------------------------------------------------------
#
# Shaped by what OpenCode actually persists (measured on this host 2026-10-12):
# one SQLite file, `session` / `message` / `part`, with the usage numbers living
# on the `step-finish` part and the model living on its parent *message*. Every
# row below is keyed by the host's own id, so re-reading the same row replaces it,
# never duplicates it.

TABLES_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- One row per OpenCode session, including the child sessions a `task` call
-- spawns (those carry `parent_id`). Deliberately no `title` and no `slug`
-- column: OpenCode derives the title from the user's first message, so storing
-- it would store the message, and the slug is a two-word label generated from
-- the same conversation.
CREATE TABLE IF NOT EXISTS sessions (
    session_id         TEXT PRIMARY KEY,
    project            TEXT,        -- the host's project id ('global' off-repo)
    directory          TEXT,        -- cwd the session ran in
    agent              TEXT,
    model              TEXT,        -- the session-level model id, as the host rolled it up
    provider           TEXT,
    parent_id          TEXT,        -- set when this session IS a subagent's session
    is_subagent        INTEGER NOT NULL DEFAULT 0,
    started_at         INTEGER,     -- epoch ms, the host's own clock
    ended_at           INTEGER,
    cost               REAL,        -- host rollup: kept BESIDE model_calls, never summed
    tokens_input       INTEGER,
    tokens_output      INTEGER,
    tokens_reasoning   INTEGER,
    tokens_cache_read  INTEGER,
    tokens_cache_write INTEGER,
    host_version       TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_started ON sessions(started_at);
CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project);

-- One row per `step-finish` part: the host's own per-step usage record. The step
-- carries tokens and cost but NO model, so model/provider/variant come from its
-- parent message (`message.data.modelID` / `providerID` / `variant`).
--
-- Token columns are written only from that part's JSON. A field the host did not
-- send stays NULL and is named in `missing` — never a fabricated 0, because a 0
-- and an absent field mean different things to every panel that divides by them.
--
-- `sessions` stores a rollup of the same numbers. They are one measurement at two
-- granularities: sum(model_calls.prompt_tokens) equals sum(sessions.tokens_input)
-- exactly on this host (265,531,738 both, measured twice in one connection), and
-- `test_two_granularities_of_the_same_measurement_agree` re-asserts it on a fixture
-- where a dropped or duplicated step would show up.
CREATE TABLE IF NOT EXISTS model_calls (
    call_id           TEXT PRIMARY KEY,   -- the part id (prt_...)
    message_id        TEXT,
    session_id        TEXT,
    project           TEXT,
    directory         TEXT,
    agent             TEXT,
    model             TEXT,
    provider          TEXT,
    variant           TEXT,
    prompt_tokens     INTEGER,
    completion_tokens INTEGER,
    reasoning_tokens  INTEGER,
    cache_read        INTEGER,
    cache_write       INTEGER,
    total_tokens      INTEGER,
    cost              REAL,
    ts                INTEGER,            -- part time_created, epoch ms
    finish            TEXT,               -- the step's own `reason`
    usage_available   INTEGER NOT NULL DEFAULT 1,
    missing           TEXT                -- JSON list of usage fields the host omitted
);
CREATE INDEX IF NOT EXISTS idx_model_call_model   ON model_calls(model);
CREATE INDEX IF NOT EXISTS idx_model_call_ts      ON model_calls(ts);
CREATE INDEX IF NOT EXISTS idx_model_call_model_ts ON model_calls(model, ts);
CREATE INDEX IF NOT EXISTS idx_model_call_session ON model_calls(session_id);

-- One row per `tool` part, whatever the tool is: builtin, skill, task or an MCP
-- server's tool. The specialised tables below are the same calls seen through a
-- specific tool name, which is why each of them keeps `call_id` as its own primary
-- key too — a call counted twice in a panel and once in `Total` would make the
-- arithmetic unreadable.
CREATE TABLE IF NOT EXISTS tool_calls (
    call_id      TEXT PRIMARY KEY,
    session_id   TEXT,
    project      TEXT,
    directory    TEXT,
    tool_name    TEXT,
    category     TEXT,      -- builtin | skill | agent | mcp
    status       TEXT,      -- the host's state.status, verbatim
    ts           INTEGER,
    duration_ms  INTEGER,   -- state.time.end - start, NULL when either is absent
    model        TEXT,      -- resolved from the owning message, as in model_calls
    provider     TEXT
);
CREATE INDEX IF NOT EXISTS idx_tool_name ON tool_calls(tool_name);
CREATE INDEX IF NOT EXISTS idx_tool_ts   ON tool_calls(ts);
CREATE INDEX IF NOT EXISTS idx_tool_cat  ON tool_calls(category);
CREATE INDEX IF NOT EXISTS idx_tool_sess ON tool_calls(session_id);

-- The `skill` tool's calls. The skill *name* is read from `state.input.name`,
-- which is one of the two argument names this project is allowed to look at;
-- every other argument value is prose and is never read.
CREATE TABLE IF NOT EXISTS skill_usage (
    call_id      TEXT PRIMARY KEY,
    skill        TEXT,
    session_id   TEXT,
    project      TEXT,
    ts           INTEGER,
    status       TEXT,
    duration_ms  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_skill_name ON skill_usage(skill);

-- The `task` tool's calls: a subagent was started. `child_session_id` comes from
-- the part's own metadata, so a spawn and the session it created can be joined
-- without guessing. The task's `prompt`/`description`/`title` are prose and are
-- named nowhere in this project.
CREATE TABLE IF NOT EXISTS agent_usage (
    call_id          TEXT PRIMARY KEY,
    agent_type       TEXT,      -- state.input.subagent_type
    session_id       TEXT,      -- the parent session
    child_session_id TEXT,
    project          TEXT,
    ts               INTEGER,
    status           TEXT,
    duration_ms      INTEGER
);
CREATE INDEX IF NOT EXISTS idx_agent_type ON agent_usage(agent_type);

-- MCP calls are found by name: OpenCode reports an MCP tool as
-- `<server>_<tool>` with a single underscore, resolved against the servers the
-- host's own config lists, longest prefix first. A tool that no configured
-- server owns is NOT counted here (it stays in `tool_calls` and is counted as
-- `unattributed_tool:<name>`) — guessing a server out of an underscore would
-- invent a row the host never named.
CREATE TABLE IF NOT EXISTS mcp_usage (
    call_id      TEXT PRIMARY KEY,
    server       TEXT,
    tool         TEXT,
    session_id   TEXT,
    project      TEXT,
    ts           INTEGER,
    status       TEXT,
    duration_ms  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_mcp_server ON mcp_usage(server);
CREATE INDEX IF NOT EXISTS idx_mcp_tool   ON mcp_usage(tool);

-- What is installed, as read from the host's own directories and config: skills
-- (from each `SKILL.md`), agents, custom slash commands, MCP servers, plugins,
-- configured providers, and the tool ids each plugin registers. `owner_plugin`
-- lets a panel attribute a tool name back to the plugin that registered it, which
-- is the only way a passive reader can fill the Plugins page at all.
CREATE TABLE IF NOT EXISTS inventory (
    kind          TEXT NOT NULL,
    name          TEXT NOT NULL,
    owner_plugin  TEXT NOT NULL DEFAULT '',
    version       TEXT,
    path          TEXT,
    source        TEXT,
    PRIMARY KEY (kind, name, owner_plugin)
);

-- Plugin usage, and ONLY plugin usage, comes from somewhere other than the host
-- database. OpenCode records a plugin's tool under the tool's own name, with
-- nothing naming the plugin, and records nothing at all for a plugin command —
-- so a passive reader can never attribute a call. This one table is fed by the
-- sidecar `plugin/ocut-hook.js` appends to and `ocut sync` ingests: a row is one
-- call of a tool or command the plugin registers.
--
-- `call_id` is the host's own call id for a tool, and a synthetic
-- `cmd:<session>:<ms>` for a command (there is no command.execute.after, so a
-- command has no host id and every invocation must stay its own row). `status`
-- is NULL: a tool's after-hook carries no status, and a command has no
-- after-hook at all. `ts` is the hook's clock, not the host's.
CREATE TABLE IF NOT EXISTS plugin_usage (
    call_id      TEXT PRIMARY KEY,
    plugin       TEXT,
    kind         TEXT,      -- tool | command
    item         TEXT,      -- the tool id or command id that ran
    session_id   TEXT,
    ts           INTEGER,   -- epoch ms, the hook's own clock
    status       TEXT,      -- NULL: see above
    duration_ms  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_plugin_usage_plugin ON plugin_usage(plugin);
CREATE INDEX IF NOT EXISTS idx_plugin_usage_ts     ON plugin_usage(ts);
CREATE INDEX IF NOT EXISTS idx_plugin_usage_item   ON plugin_usage(item);

-- The tool and command ids each installed plugin registers, as scanned by
-- `plugin/ocut-hook.js` at load. Kept apart from `inventory` on purpose: the hook
-- reports a whole plugin's surface at once and only at process start, while
-- `inventory` is DELETEd and rewritten from the filesystem on every sync — so
-- `ocut sync` folds this table back into `inventory` (kind='tool'/'command',
-- owner_plugin=plugin) AFTER that rewrite, and the surfaces survive it.
CREATE TABLE IF NOT EXISTS plugin_surface (
    plugin  TEXT NOT NULL,
    kind    TEXT NOT NULL,   -- tool | command
    name    TEXT NOT NULL,
    PRIMARY KEY (plugin, kind, name)
);

-- One row per host table the reader tracks: `part` (read by rowid cursor) and
-- `session` (re-read in full, because the host rewrites its rollup). `host_count`
-- and `host_max` are the host's own numbers at the moment the run finished, and
-- they are what detect the two things a rowid cursor cannot see on its own — rows
-- that were deleted, and a table whose rowids were renumbered. `note` is where
-- that finding is written down instead of being silently corrected.
CREATE TABLE IF NOT EXISTS sync_state (
    name         TEXT PRIMARY KEY,   -- 'part' | 'session'
    cursor       INTEGER NOT NULL DEFAULT 0,
    host_count   INTEGER,
    host_max     INTEGER,
    note         TEXT
);

-- Records the indexer saw but could not use: an unknown part `type`, a JSON blob
-- that did not parse, a field we expected and did not find. Accumulated, because
-- a run that silently claims nothing still prints "done" — the count is what
-- turns a host format change into something visible.
CREATE TABLE IF NOT EXISTS unparsed (
    reason TEXT PRIMARY KEY,
    count  INTEGER NOT NULL DEFAULT 0
);
"""

# --- name sets for the "used ∪ installed" union ----------------------------
#
# Every panel lists the union of what is *installed/available* (the static
# `inventory`) and what has *ever been used* (the usage tables), keyed by NAME
# alone. Name is the primary key: a renamed, updated or removed entity keeps its
# history, a name that appears under several owners merges into one row, and an
# installed-but-unused entity still shows with a zero count. Under a time window
# only the counts shrink -- the list itself stays full.
_TOOL_NAMES_CTE = (
    "WITH names AS ("
    " SELECT name FROM inventory WHERE kind='tool'"
    " UNION SELECT DISTINCT tool_name FROM tool_calls WHERE tool_name IS NOT NULL)")
_SKILL_NAMES_CTE = (
    "WITH names AS ("
    " SELECT name FROM inventory WHERE kind='skill'"
    " UNION SELECT DISTINCT skill FROM skill_usage WHERE skill IS NOT NULL)")
_AGENT_NAMES_CTE = (
    "WITH names AS ("
    " SELECT name FROM inventory WHERE kind='agent'"
    " UNION SELECT DISTINCT agent_type FROM agent_usage"
    " WHERE agent_type IS NOT NULL)")
_PLUGIN_NAMES_CTE = (
    "WITH names AS ("
    " SELECT name FROM inventory WHERE kind='plugin'"
    " UNION SELECT DISTINCT owner_plugin FROM inventory WHERE owner_plugin <> ''"
    " UNION SELECT DISTINCT plugin FROM plugin_usage WHERE plugin IS NOT NULL)")
# MCP is keyed by (server, tool). A configured server with no usage yet is listed
# once as (server, NULL) so the tab shows configured-but-unused servers without
# forking a server that already has recorded calls.
_MCP_NAMES_CTE = (
    "WITH pairs AS ("
    " SELECT server, tool FROM mcp_usage"
    " UNION SELECT name, NULL FROM inventory i WHERE kind='mcp'"
    "   AND NOT EXISTS (SELECT 1 FROM mcp_usage m WHERE m.server = i.name))")

VIEWS_SQL = f"""
DROP VIEW IF EXISTS v_tools;
CREATE VIEW v_tools AS
{_TOOL_NAMES_CTE}
SELECT n.name AS tool_name,
       COUNT(c.call_id) AS calls,
       SUM(c.status = 'error') AS errors,
       MAX(c.ts) AS last_ts
FROM names n
LEFT JOIN tool_calls c ON c.tool_name = n.name
GROUP BY n.name;

DROP VIEW IF EXISTS v_skills;
CREATE VIEW v_skills AS
{_SKILL_NAMES_CTE}
SELECT n.name AS skill,
       COUNT(s.call_id) AS calls,
       MAX(s.ts) AS last_ts
FROM names n
LEFT JOIN skill_usage s ON s.skill = n.name
GROUP BY n.name;

DROP VIEW IF EXISTS v_agents;
CREATE VIEW v_agents AS
{_AGENT_NAMES_CTE}
SELECT n.name AS agent_type,
       COUNT(a.call_id) AS calls,
       MAX(a.ts) AS last_ts
FROM names n
LEFT JOIN agent_usage a ON a.agent_type = n.name
GROUP BY n.name;

DROP VIEW IF EXISTS v_mcp;
CREATE VIEW v_mcp AS
{_MCP_NAMES_CTE}
SELECT p.server, p.tool,
       COUNT(m.call_id) AS calls,
       MAX(m.ts) AS last_ts
FROM pairs p
LEFT JOIN mcp_usage m ON m.server = p.server AND (m.tool = p.tool OR p.tool IS NULL)
GROUP BY p.server, p.tool;

DROP VIEW IF EXISTS v_plugins;
CREATE VIEW v_plugins AS
{_PLUGIN_NAMES_CTE}
SELECT n.name AS plugin,
       (SELECT COUNT(*) FROM inventory i
          WHERE i.owner_plugin = n.name AND i.kind = 'tool') AS tools,
       (SELECT COUNT(*) FROM inventory i
          WHERE i.owner_plugin = n.name AND i.kind = 'command') AS commands,
       COUNT(u.call_id) AS uses,
       MAX(u.ts) AS last_ts
FROM names n
LEFT JOIN plugin_usage u ON u.plugin = n.name
GROUP BY n.name;
"""

SCHEMA_SQL = TABLES_SQL + VIEWS_SQL


# --- helpers ---------------------------------------------------------------


def ensure_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def open_db(path: Path | str = DB_PATH, readonly: bool = False) -> sqlite3.Connection:
    """Open the tracker DB. Creates the file and its parent unless ``readonly``."""
    path = Path(path)
    if readonly:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    else:
        ensure_dir(path)
        conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    if not readonly:
        conn.execute("PRAGMA journal_mode = WAL")
    return conn


# --- snapshots -------------------------------------------------------------
#
# A version bump or a ``--full`` rebuild deletes indexed state. Here the source is
# a database the host keeps mutating *and pruning*, so "just re-read it" is not a
# guarantee: a session OpenCode has already deleted is gone from the host forever.
# Every destructive path therefore takes a snapshot first.
#
# Managed snapshots live in their own ``backups/`` directory. Hand-made
# ``usage.db.bak-*`` files beside the database are listed as restore sources but
# never pruned: this code did not create them and does not get to delete them.

BACKUP_KEEP = 5
BACKUP_GLOB = "usage.db.bak-*"          # the shape a person makes by hand
BACKUP_PATTERN = "usage-*.db"           # the shape this module makes


def db_file(conn) -> Path | None:
    """The file behind an open connection (``None`` for an in-memory one)."""
    row = conn.execute("PRAGMA database_list").fetchone()
    return Path(row[2]) if row and row[2] else None


def backup_dir(db_path=None) -> Path:
    return Path(db_path or DB_PATH).parent / "backups"


def snapshot(conn, reason: str = "manual", db_path=None) -> Path:
    """Copy the live database aside using SQLite's online backup API.

    ``conn.backup`` rather than a file copy: with WAL on, the newest committed
    frames can still be sitting in ``-wal``, so a copied main file is a stale
    database — exactly the thing you need the snapshot to not be.
    """
    src = Path(db_path) if db_path else db_file(conn)
    dest_dir = backup_dir(src)
    dest_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now()
    base = f"usage-{now:%Y%m%d-%H%M%S}"
    dest = dest_dir / f"{base}-{reason}.db"
    n = 1
    while dest.exists():            # never overwrite an earlier snapshot
        dest = dest_dir / f"{base}-{reason}-{n}.db"
        n += 1
    target = sqlite3.connect(dest)
    try:
        conn.backup(target)
    finally:
        target.close()
    prune_backups(src)
    return dest


def prune_backups(db_path=None, keep: int = BACKUP_KEEP) -> list[Path]:
    """Delete managed snapshots beyond the newest ``keep``.

    Only files matching :data:`BACKUP_PATTERN` inside the managed directory are
    eligible. Ordered by mtime, not by name: several snapshots can land in the
    same second, and the ``-1``/``-2`` suffixes that disambiguate them do not sort
    in creation order — trusting them would delete the newer file.
    """
    d = backup_dir(db_path)
    if not d.is_dir():
        return []
    newest_first = sorted(d.glob(BACKUP_PATTERN),
                          key=lambda p: (p.stat().st_mtime, p.name), reverse=True)
    stale = newest_first[keep:]
    for path in stale:
        path.unlink()
    return stale


def list_backups(db_path=None) -> list[Path]:
    """Everything worth restoring: managed snapshots plus hand-made copies."""
    src = Path(db_path or DB_PATH)
    out: list[Path] = []
    d = backup_dir(src)
    if d.is_dir():
        out += list(d.glob(BACKUP_PATTERN))
    if src.parent.is_dir():
        out += list(src.parent.glob(BACKUP_GLOB))
    return sorted(out, key=lambda p: (p.stat().st_mtime, p.name), reverse=True)


def restore(backup_path, db_path=None, reason: str = "pre-restore") -> Path:
    """Replace the database with a snapshot, after snapshotting what is live.

    A wrong restore should be undoable, so the current database is copied first.
    The TUI must not be running: it holds the file open and would keep writing
    into the copy you are replacing.
    """
    src = Path(db_path or DB_PATH)
    backup_path = Path(backup_path)
    if not backup_path.is_file():
        raise FileNotFoundError(f"no such backup: {backup_path}")
    if src.exists():
        conn = sqlite3.connect(src)
        try:
            conn.execute("PRAGMA busy_timeout = 5000")
            snapshot(conn, reason=reason, db_path=src)
        finally:
            conn.close()
    ensure_dir(src)
    source = sqlite3.connect(backup_path)
    target = sqlite3.connect(src)
    try:
        target.execute("PRAGMA busy_timeout = 5000")
        source.backup(target)
    finally:
        target.close()
        source.close()
    return backup_path


def stored_version(conn: sqlite3.Connection) -> int:
    """The schema version this database was last written at.

    ``0`` when there is no usable value, which is what a freshly created database
    looks like — so ``ensure_schema`` can tell "brand new" from "one version
    behind" instead of assuming the older shape.
    """
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'").fetchone()
    except sqlite3.Error:
        return 0
    try:
        return int(row[0])
    except (TypeError, ValueError, IndexError):
        return 0


def ensure_schema(conn: sqlite3.Connection) -> str:
    """Create the schema, or roll an existing database forward.

    Tables are created first and the stored version is read *before* any view is
    written: `VIEWS_SQL` begins by DROPping each view, so running it against a
    database written by a newer build would destroy a definition this version
    cannot interpret. A newer database therefore gets its tables ensured — additive,
    no row touched — and is otherwise left alone, reported as ``"newer"``.

    There is deliberately no migration chain. This is the first version of *this*
    database, and OpenCode's own tables stay the source of truth, so everything in
    here is re-derivable by `ocut sync --full`. A version bump therefore changes
    the DDL and clears the cursors — forcing one clean re-read — instead of
    rewriting rows in place through a chain of ALTERs that has to be right every
    time somebody upgrades from an arbitrary older build.
    """
    conn.executescript(TABLES_SQL)
    stored = stored_version(conn)
    if stored > SCHEMA_VERSION:
        conn.commit()
        return "newer"
    conn.executescript(VIEWS_SQL)
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(SCHEMA_VERSION),),
    )
    if 0 < stored < SCHEMA_VERSION:
        conn.execute("DELETE FROM sync_state")
        conn.commit()
        return "migrated"
    conn.commit()
    return "created" if stored == 0 else "current"


def migrate(db_path: Path | str = DB_PATH) -> str:
    """Best-effort upgrade of the tracker DB to the current schema.

    Returns a status rather than a boolean: ``"created"``, ``"current"``,
    ``"migrated"``, ``"newer"`` (a database from a future build — not touched), or
    ``"failed: <sqlite message>"``. A boolean made the last three look like the
    same no-op, which let a locked or damaged file pass as up to date.
    """
    try:
        conn = open_db(db_path)
        try:
            return ensure_schema(conn)
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return f"failed: {exc}"


def reset(conn: sqlite3.Connection) -> None:
    """Drop all indexed data (keeps the schema). Used by ``sync --full``.

    Enumerated from the schema rather than hand-copied: a table added to
    ``TABLES_SQL`` and forgotten here would survive a "full rebuild" and quietly
    double-count in the next one.
    """
    keep = {"meta", "sqlite_sequence"}
    tables = sorted(
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        if r[0] not in keep)
    for table in tables:
        conn.execute(f"DELETE FROM {table}")
    conn.commit()


def note_unparsed(conn: sqlite3.Connection, reason: str, n: int) -> None:
    """Add ``n`` to the count of records the indexer could not use.

    Called once per reason per batch, not per record: a format change can make tens
    of thousands of records unknown in one run, and one statement each would cost
    more than the parsing it is reporting on.
    """
    conn.execute(
        "INSERT INTO unparsed(reason, count) VALUES(?,?)"
        " ON CONFLICT(reason) DO UPDATE SET count = count + excluded.count",
        (reason, n),
    )


def q_unparsed(conn: sqlite3.Connection):
    """Every reason records were skipped for, most frequent first."""
    return conn.execute(
        "SELECT reason, count FROM unparsed ORDER BY count DESC, reason").fetchall()


# --- shared queries (used by both the CLI and the TUI) ---------------------
#
# NOTE: the aggregate SQL below intentionally duplicates VIEWS_SQL. The views
# remain the export path (`ocut export`); these ``q_*`` functions aggregate the
# base tables directly so they can take an optional time window (SQLite views
# cannot be parameterized).


def _on_time(conds) -> tuple[str, list]:
    """``(" AND <pred> AND ...", params)`` for the LEFT JOIN's ON clause.

    The window lives in ON (never WHERE) so an out-of-window entity still
    yields a row with a zero count instead of dropping out of the list.
    """
    if not conds:
        return "", []
    return (" AND " + " AND ".join(c for c, _ in conds),
            [p for _, p in conds])


def q_tools(conn, limit=None, start_ts=None, end_ts=None):
    on_time, params = _on_time(_time_conds(start_ts, end_ts, col="c.ts"))
    sql = (f"{_TOOL_NAMES_CTE}"
           " SELECT n.name AS tool_name, COUNT(c.call_id) AS calls,"
           " SUM(CASE WHEN c.status='completed' THEN 1 ELSE 0 END) AS completed,"
           " SUM(CASE WHEN c.status='error' THEN 1 ELSE 0 END) AS errored,"
           " SUM(CASE WHEN c.status='running' THEN 1 ELSE 0 END) AS running,"
           " CAST(AVG(c.duration_ms) AS INTEGER) AS avg_ms,"
           " MAX(c.ts) AS last_used, MIN(c.ts) AS first_used,"
           " COUNT(DISTINCT c.project) AS projects,"
           " COUNT(DISTINCT c.session_id) AS sessions"
           " FROM names n LEFT JOIN tool_calls c ON c.tool_name = n.name"
           f"{on_time} GROUP BY n.name ORDER BY calls DESC, n.name")
    if limit:
        sql += f" LIMIT {int(limit)}"
    return conn.execute(sql, params).fetchall()


def q_skills(conn, limit=None, start_ts=None, end_ts=None):
    """Skills keyed by NAME only — a skill name that ships under several
    plugins is merged into one row (no ``plugin`` column)."""
    on_time, params = _on_time(_time_conds(start_ts, end_ts, col="s.ts"))
    sql = (f"{_SKILL_NAMES_CTE}"
           " SELECT n.name AS skill, COUNT(s.call_id) AS calls,"
           " SUM(CASE WHEN s.status='completed' THEN 1 ELSE 0 END) AS completed,"
           " SUM(CASE WHEN s.status='error' THEN 1 ELSE 0 END) AS errored,"
           " MAX(s.ts) AS last_used, MIN(s.ts) AS first_used,"
           " COUNT(DISTINCT s.project) AS projects,"
           " COUNT(DISTINCT s.session_id) AS sessions"
           " FROM names n LEFT JOIN skill_usage s ON s.skill = n.name"
           f"{on_time} GROUP BY n.name ORDER BY calls DESC, n.name")
    if limit:
        sql += f" LIMIT {int(limit)}"
    return conn.execute(sql, params).fetchall()


def q_agents(conn, limit=None, start_ts=None, end_ts=None):
    """Agents keyed by NAME only.

    A child session spawned by one of these calls is counted where it is visible
    (``sessions.is_subagent``), not here: ``calls`` answers "how often was this
    agent type started", which is a different question from "how many sessions
    exist because of it".
    """
    on_time, params = _on_time(_time_conds(start_ts, end_ts, col="a.ts"))
    sql = (f"{_AGENT_NAMES_CTE}"
           " SELECT n.name AS agent_type, COUNT(a.call_id) AS calls,"
           " SUM(CASE WHEN a.status='completed' THEN 1 ELSE 0 END) AS completed,"
           " SUM(CASE WHEN a.status='error' THEN 1 ELSE 0 END) AS errored,"
           " COUNT(a.child_session_id) AS children,"
           " MAX(a.ts) AS last_used, MIN(a.ts) AS first_used,"
           " COUNT(DISTINCT a.project) AS projects,"
           " COUNT(DISTINCT a.session_id) AS sessions"
           " FROM names n LEFT JOIN agent_usage a ON a.agent_type = n.name"
           f"{on_time} GROUP BY n.name ORDER BY calls DESC, n.name")
    if limit:
        sql += f" LIMIT {int(limit)}"
    return conn.execute(sql, params).fetchall()


def q_mcp(conn, limit=None, start_ts=None, end_ts=None):
    """One row per (server, tool) pair, from the union of configured and used.

    A configured server with no recorded tool yet appears once with ``tool`` NULL;
    the join deliberately matches ``p.tool IS NULL`` so that row still reports the
    server's whole call count rather than dropping it (measured on this host: 2
    configured servers, 467 attributed calls).
    """
    on_time, params = _on_time(_time_conds(start_ts, end_ts, col="m.ts"))
    sql = (f"{_MCP_NAMES_CTE}"
           " SELECT p.server, p.tool, COUNT(m.call_id) AS calls,"
           " SUM(CASE WHEN m.status='completed' THEN 1 ELSE 0 END) AS completed,"
           " SUM(CASE WHEN m.status='error' THEN 1 ELSE 0 END) AS errored,"
           " CAST(AVG(m.duration_ms) AS INTEGER) AS avg_ms,"
           " MAX(m.ts) AS last_used, MIN(m.ts) AS first_used,"
           " COUNT(DISTINCT m.project) AS projects,"
           " COUNT(DISTINCT m.session_id) AS sessions"
           " FROM pairs p"
           " LEFT JOIN mcp_usage m ON m.server = p.server AND m.tool = p.tool"
           f"{on_time} GROUP BY p.server, p.tool"
           " ORDER BY calls DESC, p.server, p.tool")
    if limit:
        sql += f" LIMIT {int(limit)}"
    return conn.execute(sql, params).fetchall()


def q_plugins(conn):
    """Every plugin the host's config declares, what it ships, and how often it ran.

    Usage comes from ``plugin_usage``, which only ``plugin/ocut-hook.js`` writes:
    OpenCode's own database records a plugin's tool under the tool's own name with
    nothing naming the plugin, and records nothing at all for a plugin command, so
    a passive reader can never attribute a call. ``uses`` is therefore 0 for a
    plugin whose hook has recorded nothing — which is the honest answer, not a
    verdict: the plugin may be installed and unused, or its surface may be one the
    hook's static scan cannot parse. ``tools``/``commands`` count what the scan
    found; ``skills``/``agents`` come from ``owner_plugin``, which the host sets.
    """
    return conn.execute(
        f"{_PLUGIN_NAMES_CTE}"
        " SELECT n.name AS plugin,"
        "  (SELECT version FROM inventory v WHERE v.name = n.name"
        "   AND v.kind = 'plugin' LIMIT 1) AS version,"
        "  (SELECT source FROM inventory v WHERE v.name = n.name"
        "   AND v.kind = 'plugin' LIMIT 1) AS source,"
        # what the host's own config spells for this plugin — an npm name with a
        # version, or a local file. `source` only says which root was walked.
        "  (SELECT path FROM inventory v WHERE v.name = n.name"
        "   AND v.kind = 'plugin' LIMIT 1) AS path,"
        " COUNT(u.call_id) AS uses, MAX(u.ts) AS last_used,"
        "  (SELECT COUNT(*) FROM inventory x WHERE x.owner_plugin = n.name"
        "   AND x.kind = 'skill') AS skills,"
        "  (SELECT COUNT(*) FROM inventory x WHERE x.owner_plugin = n.name"
        "   AND x.kind = 'agent') AS agents,"
        "  (SELECT COUNT(*) FROM inventory x WHERE x.owner_plugin = n.name"
        "   AND x.kind = 'command') AS commands,"
        "  (SELECT COUNT(*) FROM inventory x WHERE x.owner_plugin = n.name"
        "   AND x.kind = 'tool') AS tools"
        " FROM names n"
        " LEFT JOIN plugin_usage u ON u.plugin = n.name"
        " GROUP BY n.name ORDER BY uses DESC, n.name",
    ).fetchall()


def q_recent(conn, limit=30):
    return conn.execute(
        "SELECT ts, tool_name, category, status, duration_ms, project"
        " FROM tool_calls ORDER BY ts DESC LIMIT ?",
        (int(limit),),
    ).fetchall()


# entity -> (table, name column). The drill-downs share it: the history list, the
# windowed summary, and (via the primary key) the single-call detail. ``mcp`` is
# keyed by the TOOL alone here; the MCP tab's own row key carries the server too.
_HISTORY_TABLE = {
    "tool": ("tool_calls", "tool_name"),
    "skill": ("skill_usage", "skill"),
    "agent": ("agent_usage", "agent_type"),
    "mcp": ("mcp_usage", "tool"),
}


def _history_conds(kind, name, server, start_ts, end_ts):
    """The WHERE pieces one entity shares between the list and its header counts.

    ``server`` exists because the MCP tab keys a row on (server, tool) while the
    table column it searches is `tool`: two servers exposing the same tool name are
    two rows, and a history that ignored the server would show both of their calls
    under whichever server the person clicked. The screen title and its rows have to
    be about the same thing, so the pair is a condition, not a display hint.
    """
    table, col = _HISTORY_TABLE[kind]
    conds = [(f"{col} = ?", name)]
    if kind == "mcp" and server is not None:
        conds.append(("server = ?", server))
    return table, conds + _time_conds(start_ts, end_ts, col="ts")


def q_history(conn, kind, name, limit=50, start_ts=None, end_ts=None, server=None):
    """Recent calls for one entity in a window, newest first. kind in tool|skill|agent|mcp.

    ``call_id`` rides along so a row can open the single-call detail screen; it
    is not one of the columns the list shows. The window is half-open
    (``ts >= start AND ts < end``); both bounds ``None`` is all-time. ``server``
    scopes an MCP row to the pair it was keyed on — see :func:`_history_conds`.
    """
    table, conds = _history_conds(kind, name, server, start_ts, end_ts)
    where = " WHERE " + " AND ".join(c for c, _ in conds)
    return conn.execute(
        "SELECT ts, project, session_id, status, duration_ms, call_id"
        f" FROM {table}{where} ORDER BY ts DESC LIMIT ?",
        [p for _, p in conds] + [int(limit)],
    ).fetchall()


def q_model_calls(conn, limit=50, model=None, start_ts=None, end_ts=None):
    """Recent per-step usage rows (newest first).

    One row per OpenCode ``step-finish`` part — the host's own usage record for
    one model step. Token columns are that part's numbers, and NULL means the
    field was absent: never a 0 invented for it. ``total_tokens`` prefers the
    host's own total and falls back to the five-part sum; ``total_tokens_source``
    says which was used ('host' | 'derived' | NULL). ``prompt_total_tokens`` is
    the prompt with the cache read added back. The window is half-open; both
    bounds ``None`` is all-time.
    """
    conds: list = []
    if model:
        conds.append(("model = ?", model))
    conds += _time_conds(start_ts, end_ts, col="ts")
    sql = (
        "SELECT call_id, message_id, session_id, project, directory, agent,"
        " model, provider, variant, prompt_tokens, completion_tokens,"
        " reasoning_tokens, cache_read, cache_write,"
        f" {_TOTAL_EXPR} AS total_tokens,"
        f" {_TOTAL_SOURCE_EXPR} AS total_tokens_source,"
        f" {_PROMPT_TOTAL_EXPR} AS prompt_total_tokens,"
        " cost, ts, finish, usage_available, missing"
        " FROM model_calls"
    )
    params = [p for _, p in conds]
    if conds:
        sql += " WHERE " + " AND ".join(c for c, _ in conds)
    sql += " ORDER BY ts DESC, call_id LIMIT ?"
    params.append(int(limit))
    return conn.execute(sql, params).fetchall()


def q_entity_summary(conn, kind, name, start_ts=None, end_ts=None, server=None):
    """Windowed aggregates for one entity — the numbers the drill-down header shows.

    Every entity table carries ``status`` and ``duration_ms``, so one shape fits
    all four. The completed/errored/running counts COALESCE to 0 because "0 of 0
    calls completed" is true, not unknown; ``avg_ms`` and the first/last stamps
    stay NULL over an empty window (never a fabricated 0 or epoch).

    The header and the list are the same measurement at the same scope, so they
    share :func:`_history_conds` rather than each spelling the predicates out: a
    header counting both MCP servers above a list of one is exactly the disagreement
    this exists to prevent.
    """
    table, conds = _history_conds(kind, name, server, start_ts, end_ts)
    where = " WHERE " + " AND ".join(c for c, _ in conds)
    return conn.execute(
        "SELECT COUNT(*) AS calls,"
        " COALESCE(SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END), 0)"
        "   AS completed,"
        " COALESCE(SUM(CASE WHEN status='error' THEN 1 ELSE 0 END), 0)"
        "   AS errored,"
        " COALESCE(SUM(CASE WHEN status='running' THEN 1 ELSE 0 END), 0)"
        "   AS running,"
        " COALESCE(SUM(CASE WHEN status IS NULL THEN 1 ELSE 0 END), 0)"
        "   AS unreported,"
        " CAST(AVG(duration_ms) AS INTEGER) AS avg_ms,"
        " MIN(ts) AS first_used, MAX(ts) AS last_used,"
        " COUNT(DISTINCT project) AS projects,"
        " COUNT(DISTINCT session_id) AS sessions"
        f" FROM {table}{where}",
        [p for _, p in conds],
    ).fetchone()


def q_plugin_history(conn, plugin, limit=50, start_ts=None, end_ts=None):
    """Recent calls of one plugin's tools/commands, newest first.

    Reads ``plugin_usage`` (the hook's sidecar), not ``tool_calls``: the host names
    no plugin, so the passive tables cannot attribute a call. ``project`` is joined
    from ``sessions`` on the session id, and ``call_id`` rides along as the row key
    for the detail screen.
    """
    conds = [("p.plugin = ?", plugin)] + _time_conds(start_ts, end_ts, col="p.ts")
    where = " WHERE " + " AND ".join(c for c, _ in conds)
    return conn.execute(
        "SELECT p.ts, p.kind, p.item, p.status, p.duration_ms, p.session_id,"
        " s.project, p.call_id"
        f" FROM plugin_usage p LEFT JOIN sessions s ON s.session_id = p.session_id"
        f"{where} ORDER BY p.ts DESC LIMIT ?",
        [p for _, p in conds] + [int(limit)],
    ).fetchall()


def q_plugin_summary(conn, plugin, start_ts=None, end_ts=None):
    """Windowed call counts for one plugin.

    The static inventory counts (skills/agents/commands/tools it ships) are not
    here — they have no timestamp and come from ``q_plugins``.
    """
    conds = [("p.plugin = ?", plugin)] + _time_conds(start_ts, end_ts, col="p.ts")
    where = " WHERE " + " AND ".join(c for c, _ in conds)
    return conn.execute(
        "SELECT COUNT(*) AS uses,"
        " COUNT(DISTINCT p.item) AS targets,"
        " COUNT(DISTINCT s.project) AS projects,"
        " COUNT(DISTINCT p.session_id) AS sessions,"
        " MIN(p.ts) AS first_used, MAX(p.ts) AS last_used"
        " FROM plugin_usage p LEFT JOIN sessions s ON s.session_id = p.session_id"
        f"{where}",
        [p for _, p in conds],
    ).fetchone()


def q_model_summary(conn, model, start_ts=None, end_ts=None):
    """Windowed token rollup for one model — the drill-down's header numbers.

    Same expressions the Tokens tab and the Usage page use, so the header can
    never disagree with the row it was opened from. ``total_tokens`` is the host's
    own per-step sum (or the five-part fallback); ``prompt_total_tokens`` is the
    prompt with the cache read added back.
    """
    conds = [("model = ?", model)] + _time_conds(start_ts, end_ts, col="ts")
    where = " WHERE " + " AND ".join(c for c, _ in conds)
    return conn.execute(
        "SELECT COUNT(*) AS responses,"
        " COALESCE(SUM(CASE WHEN usage_available=1 THEN 1 ELSE 0 END), 0)"
        "   AS with_usage,"
        " SUM(prompt_tokens) AS prompt_tokens,"
        " SUM(completion_tokens) AS completion_tokens,"
        " SUM(reasoning_tokens) AS reasoning_tokens,"
        f" {_AGG_TOTAL_EXPR} AS total_tokens,"
        f" {_AGG_TOTAL_SOURCE_EXPR} AS total_tokens_source,"
        f" {_AGG_PROMPT_TOTAL_EXPR} AS prompt_total_tokens,"
        " SUM(cache_read) AS cache_read,"
        " SUM(cache_write) AS cache_write,"
        " SUM(cost) AS cost,"
        " MIN(ts) AS first_used, MAX(ts) AS last_used"
        f" FROM model_calls{where}",
        [p for _, p in conds],
    ).fetchone()


# Single-call detail: table, primary key, and the explicit column order the detail
# screen renders. Explicit, not SELECT *, so a renamed column fails the query
# loudly instead of quietly changing what the screen shows.
_CALL_DETAIL = {
    "tool": ("tool_calls", "call_id",
             ("call_id", "tool_name", "category", "model", "provider", "status",
              "ts", "duration_ms", "session_id", "project", "directory")),
    "skill": ("skill_usage", "call_id",
              ("call_id", "skill", "status",
               "ts", "duration_ms", "session_id", "project")),
    "agent": ("agent_usage", "call_id",
              ("call_id", "agent_type", "child_session_id", "status",
               "ts", "duration_ms", "session_id", "project")),
    "mcp": ("mcp_usage", "call_id",
            ("call_id", "server", "tool", "status",
             "ts", "duration_ms", "session_id", "project")),
}
# A step's detail is a plain row of model_calls plus the derived totals, so it
# shares the list's expressions rather than the plain table map above.
_MODEL_DETAIL_COLS = (
    "call_id", "message_id", "session_id", "model", "provider", "variant",
    "agent", "prompt_tokens", "completion_tokens", "reasoning_tokens",
    "cache_read", "cache_write", "cost", "finish",
)


def q_call_detail(conn, kind, key):
    """Every field of one call as an ordered dict, or ``None`` if the key is unknown.

    ``kind`` selects the table from a whitelist (never user SQL); ``key`` is the
    table's primary key and is always parameterised. No window: a call is a point.
    """
    if kind == "model":
        row = conn.execute(
            "SELECT " + ", ".join(_MODEL_DETAIL_COLS)
            + f", {_TOTAL_EXPR} AS total_tokens,"
            + f" {_TOTAL_SOURCE_EXPR} AS total_tokens_source,"
            + f" {_PROMPT_TOTAL_EXPR} AS prompt_total_tokens,"
            " ts, project, directory, usage_available, missing"
            " FROM model_calls WHERE call_id = ?",
            (key,),
        ).fetchone()
        return dict(row) if row else None
    table, pk, cols = _CALL_DETAIL[kind]
    row = conn.execute(
        f"SELECT {', '.join(cols)} FROM {table} WHERE {pk} = ?",
        (key,),
    ).fetchone()
    return dict(row) if row else None


def q_model_tokens(conn, start_ts=None, end_ts=None):
    """Per-model token totals, one row per (model, provider) the host reported.

    ``SUM`` skips NULLs, so a step recorded with ``usage_available=0`` adds
    nothing instead of a fabricated zero.

    ``total_tokens`` is the **host total**: the sum of the per-row totals, i.e.
    the host's own ``tokens.total`` where present, else the five-part sum. It is
    NULL only when a model contributes no total at all. ``total_tokens_source``
    reports 'host' / 'derived' / 'mixed' / NULL.

    ``prompt_total_tokens`` is ``prompt + cache_read`` — the whole prompt the model
    actually saw. It is *not* added into ``total_tokens``, because the host's total
    already contains it; the two answer different questions ("what did this cost"
    versus "how much did the model read").
    """
    where, params = _time_filter(start_ts, end_ts)
    return conn.execute(
        "SELECT model, provider,"
        " COUNT(*) AS responses,"
        " SUM(CASE WHEN usage_available=1 THEN 1 ELSE 0 END) AS with_usage,"
        " SUM(prompt_tokens) AS prompt_tokens,"
        " SUM(completion_tokens) AS completion_tokens,"
        " SUM(reasoning_tokens) AS reasoning_tokens,"
        " SUM(cache_read) AS cache_read,"
        " SUM(cache_write) AS cache_write,"
        " SUM(cost) AS cost,"
        f" {_AGG_TOTAL_EXPR} AS total_tokens,"
        f" {_AGG_TOTAL_SOURCE_EXPR} AS total_tokens_source,"
        f" {_AGG_PROMPT_TOTAL_EXPR} AS prompt_total_tokens"
        # Grouped by (model, provider): the same model id can be served through
        # two configured providers, and those must not merge.
        f" FROM model_calls{where}"
        " GROUP BY model, provider ORDER BY responses DESC",
        params,
    ).fetchall()


# --- usage statistics (calendar-day windows) -------------------------------

# Window sizes in CALENDAR days; None = All time (no lower bound). A window is
# whole local days ending *now*: "1d" starts at today's local 00:00, "2d" at
# yesterday's 00:00, and so on — never "now minus N hours".
USAGE_RANGES = {"1d": 1, "2d": 2, "3d": 3, "7d": 7, "14d": 14, "30d": 30,
                "all": None}
USAGE_RANGE_LABELS = {
    "1d": "Today", "2d": "2 days", "3d": "3 days",
    "7d": "7 days", "14d": "14 days", "30d": "30 days", "all": "All time",
}

# The route origin lives in ``model_calls.provider``: the host's own `providerID`
# (a name the user typed into their config, or a vendor id), never inferred from
# the model name. No sentinel is written here — a passive reader has no evidence
# for "built-in" versus "configured", and inventing one would put a claim in the
# panel that the host never made.
PROVIDER_BUILTIN = "builtin"


def provider_label(value):
    """The display label for a ``model_calls.provider`` value.

    ``None`` (unknown) reads as ``-``, the missing-value marker the rest of the
    interface uses; the built-in sentinel reads as ``Built-in``; every other
    value — a vendor name, a url host or an endpoint tag — is shown as stored.
    """
    if value is None:
        return "-"
    return "Built-in" if value == PROVIDER_BUILTIN else value


# Per-row total: the host's own ``tokens.total`` when it sent one, else the sum of
# all five usage parts. OpenCode's ``input`` is the *uncached* prompt — `cache.read`
# is a separate number and is already inside `total` — which is why the derived
# fallback adds all five and requires every one of them to be present. Measured on
# this host: total == input+output+reasoning+cache.read+cache.write in every one of
# the 20,591 steps that carry a total, and 36 rows carry the five parts with no
# total, so the fallback is exercised by real data rather than only by a test.
# NULL only when neither is available (never a fabricated 0).
_TOTAL_EXPR = (
    "COALESCE(total_tokens,"
    " CASE WHEN prompt_tokens IS NOT NULL AND completion_tokens IS NOT NULL"
    " AND reasoning_tokens IS NOT NULL AND cache_read IS NOT NULL"
    " AND cache_write IS NOT NULL"
    " THEN prompt_tokens + completion_tokens + reasoning_tokens"
    " + cache_read + cache_write ELSE NULL END)"
)
# Provenance of the per-row value above: 'host' | 'derived' | NULL.
_TOTAL_SOURCE_EXPR = (
    "CASE WHEN total_tokens IS NOT NULL THEN 'host'"
    " WHEN prompt_tokens IS NOT NULL AND completion_tokens IS NOT NULL"
    " AND reasoning_tokens IS NOT NULL AND cache_read IS NOT NULL"
    " AND cache_write IS NOT NULL"
    " THEN 'derived' ELSE NULL END"
)
# Aggregate total: SUM of the PER-ROW total. Never SUM(total_tokens), which would
# silently drop the rows that fall back to the derived value.
_AGG_TOTAL_EXPR = f"SUM({_TOTAL_EXPR})"
_AGG_HOST_N = (
    "SUM(CASE WHEN total_tokens IS NOT NULL THEN 1 ELSE 0 END)"
)
_AGG_DERIVED_N = (
    "SUM(CASE WHEN total_tokens IS NULL"
    " AND prompt_tokens IS NOT NULL AND completion_tokens IS NOT NULL"
    " AND reasoning_tokens IS NOT NULL AND cache_read IS NOT NULL"
    " AND cache_write IS NOT NULL THEN 1 ELSE 0 END)"
)
# Aggregate provenance over the contributing rows:
#   NULL     -> no row contributes a total
#   host     -> every contributing row carries the host's own total
#   derived  -> none carry one (all fell back to the five-part sum)
#   mixed    -> both kinds are present
# COALESCE guards the empty-window case, where SUM(...) is NULL (not 0).
_AGG_TOTAL_SOURCE_EXPR = (
    f"CASE WHEN COALESCE({_AGG_HOST_N},0) = 0"
    f" AND COALESCE({_AGG_DERIVED_N},0) = 0 THEN NULL"
    f" WHEN COALESCE({_AGG_DERIVED_N},0) = 0 THEN 'host'"
    f" WHEN COALESCE({_AGG_HOST_N},0) = 0 THEN 'derived'"
    " ELSE 'mixed' END"
)

# --- prompt size with the cache read ---------------------------------------
# ``prompt_tokens`` is the *uncached* prompt, so a cache hit is not part of it and
# the two can be added without double-counting. This is what "how much did the
# model actually read" means here, and it is deliberately a separate key from
# ``total_tokens`` rather than a replacement for it: the transcript-based tracker
# this design was copied from needed a display-only re-add because its provider's
# total already contained the hit. Copying that expression here would have made
# every number in the Tokens panel wrong by the size of the cache.
_PROMPT_TOTAL_EXPR = (
    "CASE WHEN prompt_tokens IS NOT NULL"
    " THEN prompt_tokens + COALESCE(cache_read, 0) ELSE NULL END"
)
_AGG_PROMPT_TOTAL_EXPR = f"SUM({_PROMPT_TOTAL_EXPR})"


def cache_read_rate(cache_read, prompt):
    """``cache_read / (cache_read + prompt)`` as a fraction, or ``None``.

    The denominator is the whole prompt the model saw — the uncached input the host
    counts in ``prompt_tokens`` plus what it read from the cache. A row with neither
    is undefined, not 0%: "0 of 0" is not a rate.
    """
    if cache_read is None or prompt is None:
        return None
    base = cache_read + prompt
    if base == 0:
        return None
    return cache_read / base


class CustomWindow(NamedTuple):
    """An absolute window chosen in the date picker (as opposed to a preset).

    ``start_ms`` of ``None`` means no lower bound (all time). ``follow_now``
    keeps the end pinned to the current time on every refresh; otherwise the
    window is the fixed ``[start_ms, end_ms]`` span. Times are epoch
    milliseconds, the same unit as every other bound in this module.
    """

    start_ms: int | None
    end_ms: int | None
    follow_now: bool = False


def _short_time(ms) -> str:
    return datetime.fromtimestamp(ms / 1000).strftime("%m-%d %H:%M")


def window_label(value) -> str:
    """A short label for a window value: a preset's name, or a custom span.

    A ``follow_now`` end reads as ``now`` rather than a timestamp, so the label
    does not go stale between refreshes. Kept free of a clock argument on
    purpose: the label depends only on what was chosen, never on the time it is
    read.
    """
    if not isinstance(value, CustomWindow):
        return USAGE_RANGE_LABELS.get(value, str(value))
    if value.start_ms is None:
        return "all → now" if value.follow_now else f"all → {_short_time(value.end_ms)}"
    start = _short_time(value.start_ms)
    if value.follow_now:
        return f"{start} → now"
    return f"{start} → {_short_time(value.end_ms)}"


def window_bounds(range_key: str, now_ms: int):
    """``(start_ms, end_ms)`` for a whole-calendar-day window ending at ``now_ms``.

    The window is a whole number of **local** calendar days: ``"1d"`` runs from
    today's local 00:00 to ``now_ms``, ``"2d"`` from yesterday's 00:00, and so on.
    It never uses "now minus N hours", so the bounds are stable through the day and
    match what a person means by "today" / "the last 2 days".

    ``start_ms`` is ``None`` for ``"all"`` (no lower bound).
    """
    days = USAGE_RANGES[range_key]
    end = int(now_ms)
    if days is None:
        return None, end
    midnight = datetime.fromtimestamp(end / 1000).replace(
        hour=0, minute=0, second=0, microsecond=0)
    start = midnight - timedelta(days=days - 1)
    return int(start.timestamp() * 1000), end


# --- display-only "prompt total" ------------------------------------------

# --- typed date / time entry (the date picker) -----------------------------
# The picker lets a date and a time be *typed* as well as clicked, so the two
# strings a person types are normalised before anything compares them: "10-01"
# and "10-1" are the same day, "9:5 AM" and "09:05 AM" the same minute. That is
# the whole point — the display format is a suggestion, not a syntax to match.
#
# Month-first is assumed for the ambiguous two-field date form (the picker shows
# MM/DD/YYYY), and a leading four-digit field is read as the year. Both the date
# and the time are displayed in the same form the rest of the app prints them:
# a 24-hour clock, so midnight reads "00:00" rather than "12:00 AM". An am/pm
# marker is still *accepted* on input ("12:00 AM" is midnight).
DATE_DISPLAY_FMT = "%m/%d/%Y"
TIME_DISPLAY_FMT = "%H:%M"
# The date is split by hand rather than through a table of strptime formats:
# a yearless "%m-%d" parse is deprecated in 3.14 (the default year is ambiguous
# and leap day cannot resolve), so the missing year is filled in explicitly.
_DATE_SPLIT = re.compile(r"[-/.]")
_TIME_FORMATS = ("%I:%M %p", "%I:%M%p", "%I %p", "%I%p", "%H:%M", "%H:%M:%S")


def parse_date(text: str, default_year: int) -> datetime:
    """A typed date -> ``datetime`` at local midnight. Raises ``ValueError``.

    ``10-01``, ``10/1`` and ``10.01`` all mean the same day once ``default_year``
    fills in the missing year; a four-digit leading field is the year
    (``2026-10-01``). The month comes first in the ambiguous two-field form, and
    a two-digit trailing year uses the same 69/70 pivot as ``%y``.
    """
    s = (text or "").strip()
    fields = _DATE_SPLIT.split(s)
    if len(fields) == 2:
        year, (month, day) = str(default_year), fields
    elif len(fields) == 3:
        first, second, third = fields
        if len(first) == 4:                       # YYYY-MM-DD
            year, month, day = first, second, third
        else:                                     # MM-DD-YYYY / MM-DD-YY
            month, day, year = first, second, third
            if len(year) == 2:
                year = ("19" if int(year) >= 69 else "20") + year
    else:
        raise ValueError(f"unrecognised date: {text!r}")
    try:
        return datetime(int(year), int(month), int(day))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"unrecognised date: {text!r}") from exc


def parse_time(text: str):
    """A typed time -> ``(hour, minute)`` in 24-hour form. Raises ``ValueError``.

    An am/pm marker makes it 12-hour ("9:5 PM" == "09:05 PM"); without one it is
    24-hour ("21:05"). ``12:00 AM`` is midnight, ``12:00 PM`` noon.
    """
    s = (text or "").strip()
    if not s:
        raise ValueError("empty time")
    for fmt in _TIME_FORMATS:
        try:
            dt = datetime.strptime(s, fmt)
        except ValueError:
            continue
        return dt.hour, dt.minute
    raise ValueError(f"unrecognised time: {text!r}")


def format_date(value: datetime) -> str:
    """A datetime's date as the picker displays it (``MM/DD/YYYY``)."""
    return value.strftime(DATE_DISPLAY_FMT)


def format_time(hour: int, minute: int) -> str:
    """``(hour, minute)`` as the picker shows it (24-hour, e.g. ``00:00``)."""
    return datetime(2000, 1, 1, hour, minute).strftime(TIME_DISPLAY_FMT)


def parse_datetime(date_text: str, time_text: str, default_year: int) -> int | None:
    """A typed date plus an optional time -> epoch **milliseconds**, or ``None``.

    Both fields come from the date picker, and either may be blank: a blank date
    and a blank time are "not chosen", which the caller reads as an unbounded
    edge of the window rather than as midnight on some invented day. An empty
    time means the start of the day (and the end of it for an upper bound, which
    is the caller's decision, not this function's). Raises ``ValueError`` with a
    message naming the field that failed, because the picker has two inputs and a
    bare "invalid" does not say which one to look at.
    """
    date = (date_text or "").strip()
    if not date:
        return None
    day = parse_date(date, default_year)
    hour, minute = 0, 0
    clock = (time_text or "").strip()
    if clock:
        hour, minute = parse_time(clock)
    return int(day.replace(hour=hour, minute=minute).timestamp() * 1000)


def _ts_where(start_ts, end_ts):
    if start_ts is None:
        return "ts < ?", [end_ts]
    return "ts >= ? AND ts < ?", [start_ts, end_ts]


def _time_conds(start_ts, end_ts, col="ts"):
    """Time predicates as ``(sql, param)`` pairs; empty when both bounds are None."""
    conds = []
    if start_ts is not None:
        conds.append((f"{col} >= ?", start_ts))
    if end_ts is not None:
        conds.append((f"{col} < ?", end_ts))
    return conds


def _time_filter(start_ts, end_ts, col="ts"):
    """``(where_prefix, params)`` for the predicates above; ``("", [])`` if none."""
    conds = _time_conds(start_ts, end_ts, col)
    if not conds:
        return "", []
    return " WHERE " + " AND ".join(c for c, _ in conds), [p for _, p in conds]


def q_usage_summary(conn, start_ts, end_ts) -> dict:
    """Totals for one calendar-day window. ``SUM`` keeps NULL (no fabricated 0).

    ``total_tokens`` is the **host total** (its own ``tokens.total``, else the
    five-part sum); ``prompt_total_tokens`` is ``prompt + cache read`` (see
    :data:`_PROMPT_TOTAL_EXPR`). The two are separate keys and are never conflated.
    ``cost`` is the host's own money for the same steps, in USD.
    """
    where, params = _ts_where(start_ts, end_ts)
    row = conn.execute(
        "SELECT COUNT(*) AS requests,"
        " SUM(CASE WHEN usage_available=1 THEN 1 ELSE 0 END) AS with_usage,"
        " SUM(prompt_tokens) AS prompt_tokens,"
        " SUM(completion_tokens) AS completion_tokens,"
        " SUM(reasoning_tokens) AS reasoning_tokens,"
        f" {_AGG_TOTAL_EXPR} AS total_tokens,"
        f" {_AGG_TOTAL_SOURCE_EXPR} AS total_tokens_source,"
        f" {_AGG_PROMPT_TOTAL_EXPR} AS prompt_total_tokens,"
        " SUM(cache_read) AS cache_read,"
        " SUM(cache_write) AS cache_write,"
        " SUM(cost) AS cost,"
        " SUM(CASE WHEN prompt_tokens IS NULL THEN 1 ELSE 0 END) AS missing_prompt,"
        " SUM(CASE WHEN completion_tokens IS NULL THEN 1 ELSE 0 END)"
        " AS missing_completion"
        f" FROM model_calls WHERE {where}",
        params,
    ).fetchone()
    return dict(row)


def q_usage_kpi(conn, start_ts=None, end_ts=None) -> dict:
    """Windowed call counts for the Dashboard (the status-bar numbers, windowed).

    ``COUNT(*)`` per usage table equals the sum of the ``calls`` column of
    q_tools/q_skills/q_agents/q_mcp in the same window (every call carries a
    name), so the Dashboard always agrees with the per-entity tabs. ``sessions`` is
    the number of sessions that *started* in the window, which is not the same
    question as "how many calls happened in it". There is no ``plugins`` key: see
    :func:`q_plugins` for why a passive reader cannot count a plugin running.
    """
    where, params = _time_filter(start_ts, end_ts, col="ts")
    started, started_params = _time_filter(start_ts, end_ts, col="started_at")

    def one(sql, p=None):
        return conn.execute(sql, params if p is None else p).fetchone()[0]

    return {
        "tool_calls": one(f"SELECT COUNT(*) FROM tool_calls{where}"),
        "model_calls": one(f"SELECT COUNT(*) FROM model_calls{where}"),
        "skills": one(f"SELECT COUNT(*) FROM skill_usage{where}"),
        "agents": one(f"SELECT COUNT(*) FROM agent_usage{where}"),
        "mcp": one(f"SELECT COUNT(*) FROM mcp_usage{where}"),
        "sessions": one(f"SELECT COUNT(*) FROM sessions{started}", started_params),
    }


def q_usage_request_logs(conn, start_ts, end_ts, limit=100, offset=0):
    """Per-step rows in the window, newest first (never re-reads the host).

    Carries both ``total_tokens`` (the host's own step total) and
    ``prompt_total_tokens`` (prompt with the cache read added back), plus the raw
    cache numbers, the step's finish reason and the agent that ran it.
    """
    where, params = _ts_where(start_ts, end_ts)
    sql = (
        "SELECT ts, model, provider, variant, agent, prompt_tokens,"
        " completion_tokens, reasoning_tokens,"
        f" {_TOTAL_EXPR} AS total_tokens,"
        f" {_TOTAL_SOURCE_EXPR} AS total_tokens_source,"
        f" {_PROMPT_TOTAL_EXPR} AS prompt_total_tokens,"
        " cache_read, cache_write, cost, finish, usage_available, missing,"
        " call_id, message_id, session_id, project"
        f" FROM model_calls WHERE {where}"
        " ORDER BY ts DESC, call_id LIMIT ? OFFSET ?"
    )
    return conn.execute(sql, params + [int(limit), int(offset)]).fetchall()


# Whitelisted orderings for q_usage_model_stats (never user-supplied SQL).
_MODEL_STATS_ORDER = {
    "total_tokens": "total_tokens DESC, requests DESC",
    "requests": "requests DESC, total_tokens DESC",
    "model": "model COLLATE NOCASE ASC",
}


def q_usage_model_stats(conn, start_ts, end_ts, order_by="total_tokens"):
    """Per-model rollup for one window. ``order_by`` is whitelisted."""
    where, params = _ts_where(start_ts, end_ts)
    order = _MODEL_STATS_ORDER.get(order_by, _MODEL_STATS_ORDER["total_tokens"])
    return conn.execute(
        "SELECT model, provider,"
        " COUNT(*) AS requests,"
        " SUM(CASE WHEN usage_available=1 THEN 1 ELSE 0 END) AS with_usage,"
        " SUM(prompt_tokens) AS prompt_tokens,"
        " SUM(completion_tokens) AS completion_tokens,"
        " SUM(reasoning_tokens) AS reasoning_tokens,"
        f" {_AGG_TOTAL_EXPR} AS total_tokens,"
        f" {_AGG_TOTAL_SOURCE_EXPR} AS total_tokens_source,"
        " SUM(cache_read) AS cache_read,"
        " SUM(cache_write) AS cache_write,"
        " SUM(cost) AS cost"
        f" FROM model_calls WHERE {where}"
        f" GROUP BY model, provider ORDER BY {order}",
        params,
    ).fetchall()


def q_usage_activity(conn, start_ts=None, end_ts=None) -> dict:
    """Windowed tool-runtime rollup for the Dashboard's "more metrics" panel.

    How the calls in the window behaved: ``completed``/``errored``/``running`` are
    the host's own ``state.status`` values verbatim (measured on this host:
    `completed`, `error`, `running`, and no others), ``avg_ms`` is the mean tool
    duration (NULL when nothing ran, never a fabricated 0), and
    ``sessions``/``projects`` are the DISTINCT ids seen in the window — the
    windowed form of the all-time ``overview()`` counts. Both bounds ``None``
    means all-time.
    """
    where, params = _time_filter(start_ts, end_ts, col="ts")
    row = conn.execute(
        "SELECT COUNT(*) AS calls,"
        " SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) AS completed,"
        " SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) AS errored,"
        " SUM(CASE WHEN status='running' THEN 1 ELSE 0 END) AS running,"
        " CAST(AVG(duration_ms) AS INTEGER) AS avg_ms,"
        " COUNT(DISTINCT session_id) AS sessions,"
        " COUNT(DISTINCT project) AS projects"
        f" FROM tool_calls{where}",
        params,
    ).fetchone()
    return dict(row)


def q_usage_daily(conn, start_ts=None, end_ts=None):
    """Tool-call counts per LOCAL calendar day in the window, oldest first.

    Rows are ``{"day": "YYYY-MM-DD", "calls": n}`` for days that have calls. The
    day is bucketed in local time (``localtime``) so it lines up with the local-day
    windows; the Dashboard draws the series as a sparkline. Days with no calls are
    simply absent — the caller fills the gaps.
    """
    where, params = _time_filter(start_ts, end_ts, col="ts")
    return conn.execute(
        "SELECT strftime('%Y-%m-%d', ts / 1000, 'unixepoch', 'localtime') AS day,"
        " COUNT(*) AS calls"
        f" FROM tool_calls{where}"
        " GROUP BY day ORDER BY day",
        params,
    ).fetchall()


def q_inventory(conn, kind=None):
    sql = "SELECT * FROM inventory"
    params = ()
    if kind:
        sql += " WHERE kind = ?"
        params = (kind,)
    sql += " ORDER BY owner_plugin, kind, name"
    return conn.execute(sql, params).fetchall()


def overview(conn) -> dict:
    """All-time numbers for the status bar and ``ocut health``.

    Two families of keys and never a third: the *rows the tracker holds*
    (``tool_calls``, ``model_calls``, …) and the *numbers the host rolled up*
    (``host_rollup_*``). They are one measurement at two granularities, and the
    equality between the families is what
    ``test_two_granularities_of_the_same_measurement_agree`` pins — so a caller that
    added the two families together would double-count every token in the session.
    """
    def one(sql, *p):
        return conn.execute(sql, p).fetchone()[0]

    return {
        "tool_calls": one("SELECT COUNT(*) FROM tool_calls"),
        "distinct_tools": one("SELECT COUNT(DISTINCT tool_name) FROM tool_calls"),
        "skills": one("SELECT COUNT(*) FROM skill_usage"),
        "agents": one("SELECT COUNT(*) FROM agent_usage"),
        "mcp": one("SELECT COUNT(*) FROM mcp_usage"),
        "sessions": one("SELECT COUNT(*) FROM sessions"),
        "subagent_sessions": one(
            "SELECT COUNT(*) FROM sessions WHERE is_subagent=1"),
        # Installed, not used: the inventory is the only place a passive reader can
        # see a plugin or a slash command at all.
        "plugins_installed": one(
            "SELECT COUNT(*) FROM inventory WHERE kind='plugin'"),
        "commands_installed": one(
            "SELECT COUNT(*) FROM inventory WHERE kind='command'"),
        "projects": one(
            "SELECT COUNT(DISTINCT project) FROM sessions"
            " WHERE project IS NOT NULL"),
        "directories": one(
            "SELECT COUNT(DISTINCT directory) FROM sessions"
            " WHERE directory IS NOT NULL"),
        "first_ts": one("SELECT MIN(ts) FROM tool_calls"),
        "last_ts": one("SELECT MAX(ts) FROM tool_calls"),
        # The host's own per-session rollup. `sessions.cost` and `model_calls.cost`
        # are the same money counted twice, which is why only one of them is shown
        # anywhere in the interface.
        "host_rollup_input_tokens": one(
            "SELECT COALESCE(SUM(tokens_input),0) FROM sessions"),
        "host_rollup_output_tokens": one(
            "SELECT COALESCE(SUM(tokens_output),0) FROM sessions"),
        "host_rollup_cost": one("SELECT COALESCE(SUM(cost),0) FROM sessions"),
        "model_calls": one("SELECT COUNT(*) FROM model_calls"),
        "model_calls_with_usage": one(
            "SELECT COUNT(*) FROM model_calls WHERE usage_available=1"),
        "model_prompt_tokens": one(
            "SELECT COALESCE(SUM(prompt_tokens),0) FROM model_calls"),
        "model_completion_tokens": one(
            "SELECT COALESCE(SUM(completion_tokens),0) FROM model_calls"),
        "model_reasoning_tokens": one(
            "SELECT COALESCE(SUM(reasoning_tokens),0) FROM model_calls"),
        "model_cache_read_tokens": one(
            "SELECT COALESCE(SUM(cache_read),0) FROM model_calls"),
        "model_cache_write_tokens": one(
            "SELECT COALESCE(SUM(cache_write),0) FROM model_calls"),
        # Sum of the per-row totals: the host's own total where it sent one, else
        # the five-part sum.
        "model_total_tokens": one(
            f"SELECT COALESCE(SUM({_TOTAL_EXPR}),0) FROM model_calls"),
        "inventory": one("SELECT COUNT(*) FROM inventory"),
        # Records no handler claimed. Non-zero after an OpenCode upgrade means the
        # format moved under us, not that the user stopped using a tool.
        "unparsed_records": one("SELECT COALESCE(SUM(count),0) FROM unparsed"),
        "unparsed_kinds": one("SELECT COUNT(*) FROM unparsed"),
    }
