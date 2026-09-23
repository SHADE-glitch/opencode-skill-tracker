"""Skill scanning, frontmatter parsing, and version sync."""

from __future__ import annotations

import os

import skill_db as db


def test_scan_finds_all_skills_and_parses_frontmatter():
    found = db.scan_skills()
    if not os.path.isdir(db.SKILLS_DIR):
        return
    assert len(found) == 37, f"expected 37 SKILL.md, got {len(found)}"
    assert all(s["parsed_name"] for s in found), "every skill must have a frontmatter name"
    assert all(s["content_hash"] and len(s["content_hash"]) == 64 for s in found)
    cats = {s["category"] for s in found}
    assert cats == {"personal-skills", "open-source-skills"}


def test_frontmatter_handles_quotes_colons_and_folded():
    assert db.parse_frontmatter("---\nname: foo\ndescription: bar: baz\n---\nbody") == {
        "name": "foo",
        "description": "bar: baz",
    }
    assert db.parse_frontmatter('---\nname: q\ndescription: "quoted"\n---\n')["description"] == "quoted"
    folded = db.parse_frontmatter("---\nname: f\ndescription: >\n  line one\n  line two\n---\n")
    assert folded["description"] == "line one line two"
    assert db.parse_frontmatter("no frontmatter here") == {}
    assert db.parse_frontmatter("\ufeff---\nname: bom\n---\n")["name"] == "bom"


def test_sync_baseline_then_noop(empty_db):
    conn = db.open_db(empty_db, readonly=False)
    first = db.sync_versions(conn)
    assert first["scanned"] == 37
    assert first["baseline"] == 37
    assert first["changed"] == 0

    second = db.sync_versions(conn)
    assert second["baseline"] == 0
    assert second["changed"] == 0
    assert second["unchanged"] == 37

    versions = conn.execute("SELECT COUNT(*) FROM skill_versions").fetchone()[0]
    assert versions == 37, "one baseline row per skill, no duplicates"
    conn.close()


def test_sync_detects_content_change(tmp_path, monkeypatch):
    """Editing a SKILL.md must create exactly one new version row."""
    skills = tmp_path / "skills"
    skill = skills / "personal-skills" / "demo"
    skill.mkdir(parents=True)
    md = skill / "SKILL.md"
    md.write_text("---\nname: demo\ndescription: v1\n---\nbody v1\n", encoding="utf-8")

    conn = db.open_db(str(tmp_path / "t.db"), readonly=False)
    db.ensure_schema(conn)

    r1 = db.sync_versions(conn, skills_dir=str(skills))
    assert (r1["scanned"], r1["baseline"]) == (1, 1)

    r2 = db.sync_versions(conn, skills_dir=str(skills))
    assert (r2["changed"], r2["unchanged"]) == (0, 1)

    md.write_text("---\nname: demo\ndescription: v2\n---\nbody v2 changed\n", encoding="utf-8")
    r3 = db.sync_versions(conn, skills_dir=str(skills))
    assert r3["changed"] == 1
    assert conn.execute("SELECT COUNT(*) FROM skill_versions WHERE skill_name='demo'").fetchone()[0] == 2

    # an identical rewrite (same bytes, new mtime) must NOT add a version
    os.utime(md, None)
    r4 = db.sync_versions(conn, skills_dir=str(skills))
    assert r4["changed"] == 0
    assert conn.execute("SELECT COUNT(*) FROM skill_versions WHERE skill_name='demo'").fetchone()[0] == 2

    # dry-run must not write
    md.write_text("---\nname: demo\ndescription: v3\n---\nthird\n", encoding="utf-8")
    r5 = db.sync_versions(conn, skills_dir=str(skills), dry_run=True)
    assert r5["changed"] == 1
    assert conn.execute("SELECT COUNT(*) FROM skill_versions WHERE skill_name='demo'").fetchone()[0] == 2
    conn.close()


def test_prune_orphan_versions(empty_db):
    conn = db.open_db(empty_db, readonly=False)
    db.ensure_schema(conn)
    conn.execute(
        "INSERT INTO skill_versions (skill_name, content_hash) VALUES ('ghost','abc')"
    )
    conn.commit()
    stats = db.sync_versions(conn, prune_orphans=True)
    assert stats["orphan_versions_pruned"] == 1
    assert conn.execute("SELECT COUNT(*) FROM skill_versions WHERE skill_name='ghost'").fetchone()[0] == 0
    conn.close()
