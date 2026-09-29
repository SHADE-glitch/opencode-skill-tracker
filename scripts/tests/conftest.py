"""Shared fixtures for the skillt test suite.

`skill_db` imports normally (underscore name). `skill-tui.py` and
`skill-stats.py` are hyphenated and cannot be imported by name, so they are
loaded with importlib when needed.
"""

from __future__ import annotations

import importlib.util
import os
import sqlite3
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import skill_db as db  # noqa: E402


def load_module(filename: str, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


def _make_db(path: str, migrate: bool = True) -> sqlite3.Connection:
    conn = db.open_db(path, readonly=False)
    if migrate:
        db.ensure_schema(conn)
    else:
        conn.executescript(db.SCHEMA_SQL)  # base schema only, pre-migration
    conn.commit()
    return conn


@pytest.fixture
def empty_db(tmp_path):
    """A base-schema DB that has NOT been migrated yet.

    Migration tests need the pre-migration state, so this fixture deliberately
    skips ensure_schema().
    """
    path = str(tmp_path / "empty.db")
    conn = _make_db(path, migrate=False)
    conn.close()
    return path


@pytest.fixture
def seeded_db(tmp_path):
    """A migrated DB with crafted usage rows for insight/export tests."""
    path = str(tmp_path / "seeded.db")
    conn = _make_db(path)

    skills = [
        ("grow", "personal-skills", "/s/grow", "grows fast"),
        ("flat", "personal-skills", "/s/flat", "stays flat"),
        ("decline", "open-source-skills", "/s/decline", "declining"),
        ("bad", "open-source-skills", "/s/bad", "always fails"),
        ("ok", "open-source-skills", "/s/ok", "always fine"),
        ("unused", "open-source-skills", "/s/unused", "never used"),
    ]
    for name, cat, p, desc in skills:
        conn.execute(
            "INSERT INTO skills (name, category, path, description) VALUES (?,?,?,?)",
            (name, cat, p, desc),
        )

    def add(name, status, ago_days, trigger="tool_call"):
        conn.execute(
            "INSERT INTO skill_usage (skill_name, session_id, project_path, trigger_type,"
            " status, timestamp, duration_ms, call_id, metadata) "
            "VALUES (?,?,?,?,?, strftime('%Y-%m-%dT%H:%M:%fZ','now',?), 42, ?, ?)",
            (
                name,
                f"s-{name}-{ago_days}",
                "/proj",
                trigger,
                status,
                f"-{ago_days} days",
                f"c-{name}-{ago_days}",
                '{"model":"p/m","agent":"build","branch":"main","summary":"hello"}',
            ),
        )

    for d in range(1, 6):            # 5 recent uses
        add("grow", "success", d)
    add("flat", "success", 2)
    add("flat", "success", 3)
    add("flat", "success", 40)
    add("flat", "success", 41)
    add("decline", "success", 5)
    for d in (35, 36, 37, 38, 39):
        add("decline", "success", d)
    for d in (1, 2, 3):              # 100% failure
        add("bad", "error", d)
    for d in (1, 2, 3):
        add("ok", "success", d)
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def seeded_mcp_db(tmp_path):
    """A migrated DB with crafted MCP rows.

    Deliberately separate from `seeded_db`: every count in that fixture is
    asserted exactly, so adding MCP rows to it would ripple through unrelated
    tests.
    """
    path = str(tmp_path / "seeded-mcp.db")
    conn = _make_db(path)

    def add(server, tool, status, ago_days, duration=100, session=None,
            project="/proj", args='["identifier"]'):
        conn.execute(
            "INSERT INTO mcp_usage (server_name, tool_name, session_id, project_path,"
            " trigger_type, status, timestamp, duration_ms, call_id, arg_names, metadata) "
            "VALUES (?,?,?,?,?,?, strftime('%Y-%m-%dT%H:%M:%fZ','now',?),?,?,?,?)",
            (
                server,
                tool,
                session or f"s-{server}-{tool}-{ago_days}",
                project,
                "tool_call",
                status,
                f"-{ago_days} days",
                duration,
                f"c-{server}-{tool}-{ago_days}",
                args,
                '{"model":"p/m","agent":"build","branch":"main"}',
            ),
        )

    # basic-memory: a busy tool, a flaky tool, and a denied one.
    for d in range(1, 4):
        add("basic-memory", "read_note", "success", d, duration=10)
    add("basic-memory", "search_notes", "success", 2, duration=500)
    add("basic-memory", "search_notes", "error", 3, duration=900)
    add("basic-memory", "write_note", "denied", 4, duration=None, args=None)
    # playwright: older usage only, so it falls outside the 30-day window.
    add("playwright", "browser_click", "success", 45, duration=2000)
    # The permission path only knows the server.
    add("context7", "*", "denied", 5, duration=None, args=None)

    conn.commit()
    conn.close()
    return path


@pytest.fixture
def seeded_plugin_db(tmp_path):
    """A migrated DB with crafted plugin rows and an inventory.

    Separate from `seeded_db` and `seeded_mcp_db` for the same reason: every
    count in those fixtures is asserted exactly.
    """
    path = str(tmp_path / "seeded-plugin.db")
    conn = _make_db(path)

    def add(plugin, kind, item, status, ago_days, duration=100, session=None,
            project="/proj", trigger="tool_call"):
        conn.execute(
            "INSERT INTO plugin_usage (plugin_name, kind, item_name, session_id,"
            " project_path, trigger_type, status, timestamp, duration_ms, call_id, metadata) "
            "VALUES (?,?,?,?,?,?,?, strftime('%Y-%m-%dT%H:%M:%fZ','now',?),?,?,?)",
            (
                plugin,
                kind,
                item,
                session or f"s-{plugin}-{item}-{ago_days}",
                project,
                trigger,
                status,
                f"-{ago_days} days",
                duration,
                f"c-{plugin}-{kind}-{item}-{ago_days}",
                '{"model":"p/m","agent":"build","branch":"main"}',
            ),
        )

    dcp = "@tarquinen/opencode-dcp@3.2.0"
    conductor = "opencode-conductor-plugin"

    # DCP: a busy tool plus one error, so success rate is not 100%.
    for d in range(1, 4):
        add(dcp, "tool", "compress", "success", d, duration=10)
    add(dcp, "tool", "compress", "error", 4, duration=900)
    # conductor: commands only, and only ever 'unknown' status (no after-hook).
    add(conductor, "command", "conductor:status", "unknown", 2, duration=None,
        trigger="command_call")
    # An unresolved tool lands in the unknown bucket.
    add("(unknown)", "tool", "mystery_tool", "success", 3)
    # Older than the 30-day window.
    add(dcp, "tool", "expand", "success", 45, duration=2000)

    inventory = [
        (dcp, "3.2.0", "npm", 0, '["compress","expand"]', '["dcp-compress"]'),
        (conductor, None, "npm", 0, "[]", '["conductor:status"]'),
        ("/cfg/plugin/skill-tracker.js", None, "local", 1, "[]", "[]"),
        ("@mohak34/opencode-notifier@0.4.0", "0.4.0", "npm", 1, "[]", "[]"),
    ]
    for name, version, source, skipped, tools, commands in inventory:
        conn.execute(
            "INSERT INTO plugin_inventory (plugin_name, version, source, skipped, tools,"
            " commands, first_seen, last_seen) "
            "VALUES (?,?,?,?,?,?, strftime('%Y-%m-%dT%H:%M:%fZ','now','-10 days'),"
            " strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
            (name, version, source, skipped, tools, commands),
        )

    conn.commit()
    conn.close()
    return path


@pytest.fixture
def legacy_db(tmp_path):
    """A DB as an older version of the tool left it: no MCP or plugin objects.

    Derived by dropping those objects from the current base schema, so it
    keeps tracking SCHEMA_SQL instead of duplicating an old copy of the DDL.
    """
    path = str(tmp_path / "legacy.db")
    conn = _make_db(path, migrate=False)
    conn.executescript(
        "DROP VIEW IF EXISTS v_mcp_totals;"
        "DROP VIEW IF EXISTS v_mcp_last30;"
        "DROP VIEW IF EXISTS v_mcp_history;"
        "DROP VIEW IF EXISTS v_plugin_totals;"
        "DROP VIEW IF EXISTS v_plugin_last30;"
        "DROP VIEW IF EXISTS v_plugin_history;"
        "DROP TABLE IF EXISTS mcp_usage;"
        "DROP TABLE IF EXISTS plugin_usage;"
        "DROP TABLE IF EXISTS plugin_inventory;"
    )
    conn.commit()
    conn.close()
    return path
