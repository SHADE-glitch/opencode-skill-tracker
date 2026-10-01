"""Export shape, privacy, and file permissions."""

from __future__ import annotations

import json
import os
import stat

import pytest

import skill_db as db


def test_export_document_shape(seeded_db):
    conn = db.open_db(seeded_db)
    doc = db.export_document(conn)
    assert doc["schema_version"] == 4
    assert doc["generated_at"].endswith("Z")
    assert len(doc["skills"]) == 6
    assert len(doc["usage"]) == 21
    assert set(doc["insight"]) >= {"most_used", "fastest_growing", "long_unused", "highest_failure_rate"}

    grow = next(s for s in doc["skills"] if s["name"] == "grow")
    assert grow["source"] == "personal"
    assert grow["stats"]["total"] == 5
    assert grow["stats"]["success_rate"] == 1.0
    conn.close()


def test_export_metadata_is_parsed_object(seeded_db):
    conn = db.open_db(seeded_db)
    doc = db.export_document(conn)
    for row in doc["usage"]:
        assert isinstance(row["metadata"], dict)
    assert doc["usage"][0]["metadata"].get("model") == "p/m"
    conn.close()


def test_export_scrubs_metadata_keys_that_are_not_allowlisted(seeded_db, seeded_mcp_db,
                                                              seeded_plugin_db):
    """Export is the one path that leaves the machine, so it emits an allowlist.

    Regression: older builds wrote the user's prompt text as `metadata.summary`.
    The writer is gone but the rows are not, and a `SELECT *` export shipped them
    verbatim.
    """
    for path in (seeded_db, seeded_mcp_db, seeded_plugin_db):
        conn = db.open_db(path)
        doc = db.export_document(conn)
        for key in ("usage", "mcp_usage", "plugin_usage"):
            for row in doc[key]:
                extra = set(row["metadata"]) - set(db.EXPORT_METADATA_KEYS)
                assert not extra, f"{key}: {extra} leaked from {path}"
                assert "summary" not in row["metadata"]
                assert "title" not in row["metadata"]
        conn.close()

    # The allowlisted keys must survive, or this is just data loss.
    conn = db.open_db(seeded_db)
    row = db.export_document(conn)["usage"][0]
    assert row["metadata"]["model"] == "p/m"
    assert row["metadata"]["agent"] == "build"
    assert row["metadata"]["branch"] == "main"
    conn.close()


def test_export_tolerates_malformed_metadata(seeded_db):
    conn = db.open_db(seeded_db, readonly=False)
    conn.execute(
        "INSERT INTO skill_usage (skill_name, session_id, trigger_type, status, call_id, metadata) "
        "VALUES ('grow','bad-meta','tool_call','success','c-bad','{not valid json')"
    )
    conn.commit()
    doc = db.export_document(conn)
    bad = next(r for r in doc["usage"] if r["call_id"] == "c-bad")
    assert bad["metadata"] == {}
    conn.close()


def test_export_skills_only_omits_usage(seeded_db):
    conn = db.open_db(seeded_db)
    doc = db.export_document(conn, include_usage=False)
    assert doc["usage"] == []
    assert len(doc["skills"]) == 6
    conn.close()


def test_export_includes_mcp_usage(seeded_mcp_db):
    conn = db.open_db(seeded_mcp_db)
    doc = db.export_document(conn)
    assert len(doc["mcp_usage"]) == 8
    row = next(r for r in doc["mcp_usage"] if r["tool_name"] == "read_note")
    assert row["server_name"] == "basic-memory"
    assert row["arg_names"] == '["identifier"]'
    # metadata is parsed into an object, same as the skill usage rows.
    assert row["metadata"].get("model") == "p/m"
    conn.close()


def test_export_mcp_key_exists_on_an_unmigrated_db(empty_db):
    """`export` never migrates, so it must tolerate a DB without the new tables.

    The keys are still present (empty lists) so consumers can rely on the shape
    regardless of which layer last touched the database.
    """
    conn = db.open_db(empty_db)
    doc = db.export_document(conn)
    assert doc["mcp_usage"] == []
    assert doc["plugin_usage"] == []
    assert doc["plugin_inventory"] == []
    assert doc["schema_version"] == 4
    conn.close()


def test_write_private_json_permissions_and_guard(tmp_path, seeded_db):
    conn = db.open_db(seeded_db)
    doc = db.export_document(conn)
    out = tmp_path / "export.json"

    path = db.write_private_json(str(out), doc, pretty=True)
    assert path == str(out)
    mode = stat.S_IMODE(os.stat(out).st_mode)
    assert mode == 0o600, f"expected 0600, got {oct(mode)}"

    with pytest.raises(FileExistsError):
        db.write_private_json(str(out), doc)

    # force allows overwrite
    db.write_private_json(str(out), doc, force=True)
    assert json.loads(out.read_text(encoding="utf-8"))["schema_version"] == 4
    conn.close()


def test_backup_is_private_and_readable(tmp_path, seeded_db):
    conn = db.open_db(seeded_db, readonly=False)
    target = tmp_path / "backup.db"
    path = db.backup_db(conn, str(target))
    assert os.path.exists(path)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600

    b = db.open_db(path)
    assert b.execute("SELECT COUNT(*) FROM skills").fetchone()[0] == 6
    b.close()
    conn.close()
