"""The TUI writer must coexist with the plugin's bun:sqlite writer (WAL)."""

from __future__ import annotations

import sqlite3

import pytest

import skill_db as db


def test_concurrent_reader_and_writer(empty_db):
    writer = db.open_db(empty_db, readonly=False)
    db.ensure_schema(writer)
    reader = db.open_db(empty_db, readonly=False)

    assert writer.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"

    # WAL: a reader is not blocked by an uncommitted writer, and does not see it.
    writer.execute("BEGIN IMMEDIATE")
    writer.execute(
        "INSERT INTO skills (name, category, path, description) VALUES ('a','personal-skills','/a','A')"
    )
    assert reader.execute("SELECT COUNT(*) FROM skills").fetchone()[0] == 0
    writer.commit()
    assert reader.execute("SELECT COUNT(*) FROM skills").fetchone()[0] == 1

    # Sequential short writes from two connections both succeed (the real usage
    # pattern: the plugin and this tool never hold a long write transaction).
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("INSERT INTO skills (name, category, path) VALUES ('b','personal-skills','/b')")
    writer.commit()
    reader.execute("BEGIN IMMEDIATE")
    reader.execute("INSERT INTO skills (name, category, path) VALUES ('c','personal-skills','/c')")
    reader.commit()
    assert writer.execute("SELECT COUNT(*) FROM skills").fetchone()[0] == 3

    # Boundary: an overlapping writer that exceeds busy_timeout fails loudly
    # rather than silently losing data. Verified with a deliberately short
    # timeout so the test stays fast.
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("INSERT INTO skills (name, category, path) VALUES ('d','personal-skills','/d')")
    impatient = db.open_db(empty_db, readonly=False)
    impatient.execute("PRAGMA busy_timeout=100")
    with pytest.raises(sqlite3.OperationalError):
        impatient.execute("BEGIN IMMEDIATE")
    impatient.close()
    writer.commit()

    assert writer.execute("SELECT COUNT(*) FROM skills").fetchone()[0] == 4
    writer.close()
    reader.close()


def test_busy_timeout_is_set(empty_db):
    conn = db.open_db(empty_db)
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == db.BUSY_TIMEOUT_MS
    conn.close()
