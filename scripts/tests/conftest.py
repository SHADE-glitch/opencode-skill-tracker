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
