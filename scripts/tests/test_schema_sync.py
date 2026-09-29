"""View definitions are versioned, not just created-if-absent.

`test_plugin.py::test_plugin_and_python_schema_do_not_drift` already pins the
plugin's DDL and `skill_db`'s against each other. This module covers what that
test cannot: that a *changed* view actually reaches an existing database.
`CREATE VIEW IF NOT EXISTS` alone would keep the old definition forever, so a
view that gained or lost a column would fail at query time with "no such
column" and no way to recover short of deleting the file.
"""

from __future__ import annotations

import re

import skill_db as db


def test_view_names_cover_every_view():
    """VIEW_NAMES drives the rebuild, so it must list exactly the defined views.

    A view left out of the tuple would never be dropped, so a changed
    definition would never reach an existing DB.
    """
    defined = set(re.findall(r"CREATE VIEW IF NOT EXISTS (\w+)", db.VIEWS_SQL))
    assert set(db.VIEW_NAMES) == defined, (
        f"VIEW_NAMES out of sync with VIEWS_SQL: {set(db.VIEW_NAMES) ^ defined}"
    )


def test_views_never_expose_message_text():
    """No view may surface message bodies; that contract is README-level."""
    assert "$.summary" not in db.VIEWS_SQL
    assert "$.summary" not in db.TABLES_SQL


def test_ensure_schema_rebuilds_stale_views(empty_db):
    """A DB whose user_version is behind must get its views rebuilt."""
    conn = db.open_db(empty_db, readonly=False)

    # Simulate a stale view: replace one with a stand-in and rewind the version.
    conn.execute("DROP VIEW IF EXISTS v_skill_history")
    conn.execute("CREATE VIEW v_skill_history AS SELECT 1 AS id")
    conn.execute("PRAGMA user_version = 1")
    conn.commit()

    result = db.ensure_schema(conn)

    assert result["rebuilt_views"] is True
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    cols = {r[1] for r in conn.execute("PRAGMA table_info(v_skill_history)")}
    assert "branch" in cols and "summary" not in cols, cols

    # A second run has nothing left to rebuild.
    assert db.ensure_schema(conn)["rebuilt_views"] is False
    conn.close()
