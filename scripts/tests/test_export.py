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
    assert doc["schema_version"] == 1
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
    assert json.loads(out.read_text(encoding="utf-8"))["schema_version"] == 1
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
