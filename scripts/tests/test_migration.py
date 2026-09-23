"""Migration must be idempotent and must not disturb existing data."""

from __future__ import annotations

import os
import shutil

import skill_db as db


def test_ensure_schema_is_idempotent(empty_db):
    conn = db.open_db(empty_db, readonly=False)
    first = db.ensure_schema(conn)
    second = db.ensure_schema(conn)
    third = db.ensure_schema(conn)

    assert first["added_content_hash"] is True
    assert second["added_content_hash"] is False
    assert third["added_content_hash"] is False
    assert db.has_content_hash(conn)

    cols = [r[1] for r in conn.execute("PRAGMA table_info(skills)")]
    assert cols.count("content_hash") == 1

    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "skill_versions" in tables
    conn.close()


def test_migration_preserves_rows_and_schema(empty_db):
    conn = db.open_db(empty_db, readonly=False)
    conn.execute("INSERT INTO skills (name, category, path, description) VALUES ('a','personal-skills','/a','A')")
    conn.execute(
        "INSERT INTO skill_usage (skill_name, session_id, project_path, trigger_type, status, call_id) "
        "VALUES ('a','s1','/p','tool_call','success','c1')"
    )
    conn.commit()

    before_skills = conn.execute("SELECT COUNT(*) FROM skills").fetchone()[0]
    before_usage = conn.execute("SELECT COUNT(*) FROM skill_usage").fetchone()[0]

    db.ensure_schema(conn)
    db.ensure_schema(conn)

    assert conn.execute("SELECT COUNT(*) FROM skills").fetchone()[0] == before_skills
    assert conn.execute("SELECT COUNT(*) FROM skill_usage").fetchone()[0] == before_usage
    # existing columns and their order are unchanged (only appended)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(skills)")]
    assert cols[:7] == ["id", "name", "category", "path", "description", "created_at", "updated_at"]
    conn.close()


def test_migration_on_real_db_copy(tmp_path):
    """Migrating a copy of the production DB must keep every row."""
    real = db.DB_PATH
    if not os.path.exists(real):
        return  # nothing to copy in this environment
    copy = str(tmp_path / "real.db")
    shutil.copy(real, copy)

    src = db.open_db(real, readonly=True)
    n_skills = src.execute("SELECT COUNT(*) FROM skills").fetchone()[0]
    n_usage = src.execute("SELECT COUNT(*) FROM skill_usage").fetchone()[0]
    src.close()

    conn = db.open_db(copy, readonly=False)
    db.ensure_schema(conn)
    db.ensure_schema(conn)

    assert conn.execute("SELECT COUNT(*) FROM skills").fetchone()[0] == n_skills
    assert conn.execute("SELECT COUNT(*) FROM skill_usage").fetchone()[0] == n_usage
    conn.close()


def test_plugin_upsert_does_not_clobber_content_hash(empty_db):
    """The plugin writes only name/category/path/description — hash must survive."""
    conn = db.open_db(empty_db, readonly=False)
    db.ensure_schema(conn)
    conn.execute(
        "INSERT INTO skills (name, category, path, description, content_hash) "
        "VALUES ('a','personal-skills','/a','A','deadbeef')"
    )
    conn.commit()

    # Exactly the plugin's scanSkills upsert.
    conn.execute(
        """INSERT INTO skills (name, category, path, description) VALUES (?, ?, ?, ?)
           ON CONFLICT(path) DO UPDATE SET
             name = excluded.name, category = excluded.category,
             description = excluded.description,
             updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')""",
        ("a", "personal-skills", "/a", "A renamed"),
    )
    conn.commit()

    row = conn.execute("SELECT content_hash, description FROM skills WHERE path='/a'").fetchone()
    assert row["content_hash"] == "deadbeef"
    assert row["description"] == "A renamed"
    conn.close()
