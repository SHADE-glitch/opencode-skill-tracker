"""Write helpers used by the TUI data page and the --cli path."""

from __future__ import annotations

import json
import os

import pytest

import skill_db as db


LEAKED = '{"model":"p/m","agent":"build","summary":"the user prompt","title":"bash: ls"}'


def test_scrub_metadata_dry_run_leaves_the_database_untouched(seeded_db):
    conn = db.open_db(seeded_db, readonly=False)
    leaked = "SELECT COUNT(*) FROM skill_usage WHERE json_extract(metadata,'$.summary') IS NOT NULL"
    assert conn.execute(leaked).fetchone()[0] == 21

    res = db.scrub_metadata(conn)  # dry-run is the default
    assert res["dry_run"] is True
    assert res["matched"] == 21
    assert res["by_table"]["skill_usage"] == 21
    assert res["rows"][0]["keys"] == ["summary"]
    assert conn.execute(leaked).fetchone()[0] == 21, "a dry run must write nothing"
    conn.close()


def test_scrub_metadata_apply_strips_keys_from_all_three_tables(seeded_db):
    conn = db.open_db(seeded_db, readonly=False)
    conn.execute(
        "INSERT INTO mcp_usage (server_name, tool_name, session_id, project_path,"
        " trigger_type, status, timestamp, call_id, metadata) "
        "VALUES ('s','t','m1','/p','tool_call','success',"
        " strftime('%Y-%m-%dT%H:%M:%fZ','now'), 'c-leak', ?)",
        (LEAKED,),
    )
    conn.execute(
        "INSERT INTO plugin_usage (plugin_name, kind, item_name, session_id,"
        " project_path, trigger_type, status, timestamp, duration_ms, call_id, metadata) "
        "VALUES ('plug','tool','thing','m2','/p','tool_call','success',"
        " strftime('%Y-%m-%dT%H:%M:%fZ','now'), 5, 'c-leak-p', ?)",
        (LEAKED,),
    )
    conn.commit()

    res = db.scrub_metadata(conn, dry_run=False)
    assert res["matched"] == 23
    assert res["by_table"] == {"skill_usage": 21, "mcp_usage": 1, "plugin_usage": 1}

    for table in ("skill_usage", "mcp_usage", "plugin_usage"):
        left = conn.execute(
            f"SELECT COUNT(*) FROM {table}"
            " WHERE json_extract(metadata,'$.summary') IS NOT NULL"
            "    OR json_extract(metadata,'$.title') IS NOT NULL"
        ).fetchone()[0]
        assert left == 0, f"{table} still carries a disallowed key"

    # Rows are the point of this database: nothing is deleted, and the keys the
    # views and the TUI actually read survive.
    assert conn.execute("SELECT COUNT(*) FROM skill_usage").fetchone()[0] == 21
    kept = json.loads(
        conn.execute("SELECT metadata FROM mcp_usage WHERE call_id='c-leak'").fetchone()[0]
    )
    assert kept == {"model": "p/m", "agent": "build"}
    assert db.scrub_metadata(conn, dry_run=False)["matched"] == 0, "must be idempotent"
    conn.close()


def test_scrub_metadata_tolerates_malformed_and_empty_metadata(seeded_db):
    conn = db.open_db(seeded_db, readonly=False)
    conn.execute(
        "INSERT INTO skill_usage (skill_name, session_id, trigger_type, status,"
        " call_id, metadata) VALUES ('grow','junk','tool_call','success','c-junk','{not json')"
    )
    conn.execute(
        "INSERT INTO skill_usage (skill_name, session_id, trigger_type, status,"
        " call_id, metadata) VALUES ('grow','null','tool_call','success','c-null', NULL)"
    )
    conn.commit()
    res = db.scrub_metadata(conn)
    assert res["matched"] == 21, "unparsable and NULL metadata must not count as hits"
    conn.close()


def test_clear_usage_keeps_skills(seeded_db):
    conn = db.open_db(seeded_db, readonly=False)
    res = db.clear_usage(conn, also_skills=False)
    assert res["usage_deleted"] == 21
    assert conn.execute("SELECT COUNT(*) FROM skill_usage").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM skills").fetchone()[0] == 6
    conn.close()


def test_clear_all_removes_skills_and_versions(seeded_db):
    conn = db.open_db(seeded_db, readonly=False)
    conn.execute("INSERT INTO skill_versions (skill_name, content_hash) VALUES ('grow','zz')")
    conn.commit()
    res = db.clear_usage(conn, also_skills=True)
    assert res["skills_deleted"] == 6
    assert conn.execute("SELECT COUNT(*) FROM skills").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM skill_versions").fetchone()[0] == 0
    conn.close()


def test_delete_skill_removes_usage_and_versions(seeded_db):
    conn = db.open_db(seeded_db, readonly=False)
    conn.execute("INSERT INTO skill_versions (skill_name, content_hash) VALUES ('grow','h1')")
    conn.commit()
    res = db.delete_skill(conn, "grow")
    assert res["usage_deleted"] == 5
    assert res["skills_deleted"] == 1
    assert conn.execute("SELECT COUNT(*) FROM skill_usage WHERE skill_name='grow'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM skill_versions WHERE skill_name='grow'").fetchone()[0] == 0
    # other skills untouched
    assert conn.execute("SELECT COUNT(*) FROM skill_usage WHERE skill_name='flat'").fetchone()[0] == 4
    conn.close()


def test_vacuum_runs(seeded_db):
    conn = db.open_db(seeded_db, readonly=False)
    db.vacuum_db(conn)
    assert conn.execute("SELECT COUNT(*) FROM skills").fetchone()[0] == 6
    conn.close()


def test_cleanup_selftest_only_touches_test_rows(seeded_db):
    conn = db.open_db(seeded_db, readonly=False)
    # realistic usage + the synthetic rows a __selftest() run leaves behind
    conn.execute(
        "INSERT INTO skill_usage (skill_name, session_id, project_path, trigger_type, status, call_id) "
        "VALUES ('grow','real','/home/user/realproj','tool_call','success','c-real')"
    )
    for i, (name, status) in enumerate(
        [("brainstorming", "success"), ("docker-env-normalization", "error")]
    ):
        conn.execute(
            "INSERT INTO skill_usage (skill_name, session_id, project_path, trigger_type, status, call_id) "
            "VALUES (?,?,'/tmp/selftest-proj','tool_call',?,?)",
            (name, f"synthetic-{i}", status, f"c-syn-{i}"),
        )
    conn.commit()

    dry = db.cleanup_selftest(conn, dry_run=True)
    assert dry["matched"] == 2
    assert conn.execute("SELECT COUNT(*) FROM skill_usage").fetchone()[0] == 24, "dry-run must not delete"

    real = db.cleanup_selftest(conn, dry_run=False)
    assert real["matched"] == 2
    left = conn.execute("SELECT COUNT(*) FROM skill_usage").fetchone()[0]
    assert left == 22, "only the synthetic rows are removed"
    # the real row for 'grow' survives
    assert conn.execute(
        "SELECT COUNT(*) FROM skill_usage WHERE call_id='c-real'"
    ).fetchone()[0] == 1
    conn.close()


def test_cleanup_selftest_noop_when_clean(seeded_db):
    conn = db.open_db(seeded_db, readonly=False)
    res = db.cleanup_selftest(conn, dry_run=False)
    assert res["matched"] == 0
    conn.close()


def test_write_txn_rolls_back_and_leaves_connection_clean(seeded_db):
    """H4: a mid-way failure must roll back and leave no open transaction."""
    conn = db.open_db(seeded_db, readonly=False)
    before = conn.execute("SELECT COUNT(*) FROM skill_usage").fetchone()[0]

    with pytest.raises(RuntimeError):
        with db._write_txn(conn):
            conn.execute("DELETE FROM skill_usage")
            raise RuntimeError("boom")

    assert conn.execute("SELECT COUNT(*) FROM skill_usage").fetchone()[0] == before, \
        "failed transaction must be rolled back"

    # The connection must be reusable: a later commit is not polluted by the
    # rolled-back work, and ensure_schema's implicit commit cannot resurrect it.
    db.ensure_schema(conn)
    assert conn.execute("SELECT COUNT(*) FROM skill_usage").fetchone()[0] == before

    with db._write_txn(conn):
        conn.execute(
            "INSERT INTO skill_usage (skill_name, session_id, call_id, trigger_type, status) "
            "VALUES ('grow','z','z','tool_call','success')"
        )
    assert conn.execute("SELECT COUNT(*) FROM skill_usage").fetchone()[0] == before + 1
    conn.close()


def test_delete_skill_removes_renamed_orphan_versions(seeded_db):
    """H5: versions left under an old name (same skill_id) must also go."""
    conn = db.open_db(seeded_db, readonly=False)
    sid = conn.execute("SELECT id FROM skills WHERE name='grow'").fetchone()[0]
    conn.execute(
        "INSERT INTO skill_versions (skill_id, skill_name, content_hash) VALUES (?,?,?)",
        (sid, "grow-old-name", "hx"),
    )
    conn.execute(
        "INSERT INTO skill_versions (skill_id, skill_name, content_hash) VALUES (?,?,?)",
        (sid, "grow", "hy"),
    )
    conn.commit()

    db.delete_skill(conn, "grow")
    left = conn.execute("SELECT COUNT(*) FROM skill_versions WHERE skill_id=?", (sid,)).fetchone()[0]
    assert left == 0, "both the current and the renamed orphan versions must be removed"
    # unrelated versions survive
    assert conn.execute(
        "SELECT COUNT(*) FROM skill_versions WHERE skill_name='flat'"
    ).fetchone()[0] == 0
    conn.close()


def test_delete_skill_removes_renamed_orphan_usage(seeded_db):
    """C11: usage rows left under an old name (same skill_id) must also go."""
    conn = db.open_db(seeded_db, readonly=False)
    sid = conn.execute("SELECT id FROM skills WHERE name='grow'").fetchone()[0]
    conn.execute(
        "INSERT INTO skill_usage (skill_id, skill_name, session_id, call_id, trigger_type, status) "
        "VALUES (?,?,?,?,?,?)",
        (sid, "grow-old-name", "s-orphan", "c-orphan", "tool_call", "success"),
    )
    conn.commit()

    res = db.delete_skill(conn, "grow")
    assert res["usage_deleted"] == 6, "5 named rows + 1 renamed orphan"
    assert conn.execute(
        "SELECT COUNT(*) FROM skill_usage WHERE skill_id=?", (sid,)
    ).fetchone()[0] == 0
    # other skills' usage is untouched
    assert conn.execute(
        "SELECT COUNT(*) FROM skill_usage WHERE skill_name='flat'"
    ).fetchone()[0] == 4
    conn.close()


def test_delete_skill_is_atomic_on_failure(seeded_db):
    """A failure part-way through delete_skill must leave the DB untouched."""
    conn = db.open_db(seeded_db, readonly=False)
    before = (
        conn.execute("SELECT COUNT(*) FROM skill_usage").fetchone()[0],
        conn.execute("SELECT COUNT(*) FROM skills").fetchone()[0],
    )

    class FlakyConn:
        """sqlite3.Connection rejects attribute patching, so wrap it."""

        def __init__(self, real):
            self._c = real

        def execute(self, sql, *args):
            if isinstance(sql, str) and sql.startswith("DELETE FROM skills"):
                raise RuntimeError("disk full")
            return self._c.execute(sql, *args)

        def commit(self):
            return self._c.commit()

        def rollback(self):
            return self._c.rollback()

    with pytest.raises(RuntimeError):
        db.delete_skill(FlakyConn(conn), "grow")

    after = (
        conn.execute("SELECT COUNT(*) FROM skill_usage").fetchone()[0],
        conn.execute("SELECT COUNT(*) FROM skills").fetchone()[0],
    )
    assert after == before, "a failed delete must not partially apply"
    conn.close()


def test_scrub_metadata_clears_the_wal_so_the_text_leaves_disk(tmp_path):
    """A redaction that only rewrites the row has not removed the text.

    Under WAL the replaced pages live on in the -wal file until a checkpoint, so
    without one `scrub-metadata --yes` leaves the redacted prompt readable with
    `grep` on the file it was supposedly removed from.
    """
    path = str(tmp_path / "wal.db")
    conn = db.open_db(path, readonly=False)
    db.ensure_schema(conn)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"

    marker = b"MARKER-MUST-NOT-SURVIVE" + b" padding" * 400
    for i in range(200):
        conn.execute(
            "INSERT INTO skill_usage (skill_name, session_id, trigger_type,"
            " status, call_id, metadata) VALUES ('s',?,'tool_call','success',?,?)",
            (f"z{i}", f"c{i}", '{"model":"p/m","summary":"' + marker.decode() + '"}'),
        )
    conn.commit()

    wal = path + "-wal"
    assert os.path.exists(wal), "fixture must still be holding uncheckpointed pages"
    assert marker in open(wal, "rb").read(), "fixture must leave the text in the WAL"

    res = db.scrub_metadata(conn, dry_run=False)
    assert res["matched"] == 200
    assert res["checkpointed"] is True, "a declined checkpoint must be reported"
    assert marker not in open(path, "rb").read(), "text still in the main file"
    assert not os.path.exists(wal) or marker not in open(wal, "rb").read(), \
        "text still in the -wal file"
    conn.close()


def test_scrub_metadata_reports_checkpointed_none_when_nothing_changed(tmp_path):
    """An untouched database must not be checkpointed, and must say so as None."""
    path = str(tmp_path / "clean.db")
    conn = db.open_db(path, readonly=False)
    db.ensure_schema(conn)
    conn.execute(
        "INSERT INTO skill_usage (skill_name, session_id, trigger_type, status,"
        " call_id, metadata) VALUES ('s','z','tool_call','success','c1',"
        " '{\"model\":\"p/m\"}')"
    )
    conn.commit()
    res = db.scrub_metadata(conn, dry_run=False)
    assert res["matched"] == 0 and res["checkpointed"] is None
    conn.close()
