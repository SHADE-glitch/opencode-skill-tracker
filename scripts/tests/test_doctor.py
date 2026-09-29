"""Tests for `skillt doctor` — PASS/WARN/FAIL checks and exit codes."""

from __future__ import annotations

import os

import pytest

from conftest import load_module

import skill_db as db

st = load_module("skill-tui.py", "skill_tui")


class Args:
    def __init__(self, db_path, json=False):
        self.db = db_path
        self.json = json
        self.limit = 10


def _healthy_setup(tmp_path, monkeypatch):
    """A DB + skills dir + plugin + backup that should produce zero FAILs."""
    skills = tmp_path / "skills"
    for name in ("alpha", "beta"):
        d = skills / "personal-skills" / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: d\n---\nbody\n", encoding="utf-8"
        )
    monkeypatch.setattr(db, "SKILLS_DIR", str(skills))

    db_path = str(tmp_path / "ok.db")
    conn = db.open_db(db_path, readonly=False)
    db.ensure_schema(conn)
    for name in ("alpha", "beta"):
        conn.execute(
            "INSERT INTO skills (name, category, path, description) VALUES (?,?,?,?)",
            (name, "personal-skills", str(skills / "personal-skills" / name), "d"),
        )
    conn.commit()
    os.chmod(db_path, 0o600)

    plugin = tmp_path / "skill-tracker.js"
    plugin.write_text(
        "tool.execute.before tool.execute.after permission.ask event:\n"
        "mcp_usage function classify( recordMcpUsage\n"
        "plugin_usage recordPluginUsage command.execute.before\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(st, "PLUGIN_PATH", str(plugin))

    bdir = tmp_path / "backups"
    bdir.mkdir()
    (bdir / "skill-usage-backup-20260615-120000.db").write_bytes(b"x")
    monkeypatch.setattr(db, "BACKUP_DIR", str(bdir))
    return conn, db_path


def test_doctor_reports_no_failures_on_healthy_setup(tmp_path, monkeypatch, capsys):
    conn, db_path = _healthy_setup(tmp_path, monkeypatch)
    rc = st._cli_doctor(conn, Args(db_path))
    out = capsys.readouterr().out
    assert "[FAIL]" not in out, out
    assert "0 FAIL" in out
    assert rc == 0
    conn.close()


def test_doctor_json_is_structured(tmp_path, monkeypatch, capsys):
    import json

    conn, db_path = _healthy_setup(tmp_path, monkeypatch)
    rc = st._cli_doctor(conn, Args(db_path, json=True))
    doc = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert doc["summary"]["fail"] == 0
    names = {c["name"] for c in doc["checks"]}
    assert {"db.quick_check", "plugin.hooks", "plugin.mcp_hooks", "plugin.plugin_hooks",
            "skills.frontmatter_names"} <= names
    conn.close()


def test_doctor_fails_on_corrupt_database(tmp_path, capsys):
    bad = tmp_path / "bad.db"
    bad.write_text("this is definitely not a sqlite database\n", encoding="utf-8")
    conn = db.open_db(str(bad), readonly=False)   # lazy connect: no error yet
    rc = st._cli_doctor(conn, Args(str(bad)))
    out = capsys.readouterr().out
    assert rc == 1, "a FAIL must produce exit code 1"
    assert "[FAIL]" in out
    assert "db.quick_check" in out
    conn.close()


def test_doctor_fails_when_plugin_missing(tmp_path, monkeypatch, capsys):
    conn, db_path = _healthy_setup(tmp_path, monkeypatch)
    monkeypatch.setattr(st, "PLUGIN_PATH", str(tmp_path / "nope.js"))
    rc = st._cli_doctor(conn, Args(db_path))
    out = capsys.readouterr().out
    assert "[FAIL]" in out
    assert rc == 1
    conn.close()


def test_doctor_warns_on_unparsed_frontmatter(tmp_path, monkeypatch, capsys):
    conn, db_path = _healthy_setup(tmp_path, monkeypatch)
    # a SKILL.md with no frontmatter name -> WARN, not FAIL
    d = tmp_path / "skills" / "personal-skills" / "no-name"
    d.mkdir()
    (d / "SKILL.md").write_text("---\ndescription: no name here\n---\nbody\n", encoding="utf-8")
    rc = st._cli_doctor(conn, Args(db_path))
    out = capsys.readouterr().out
    assert "skills.frontmatter_names" in out
    assert "[WARN]" in out
    assert rc == 0, "WARN must not fail the command"
    conn.close()


def test_doctor_reports_missing_skills_dir(tmp_path, monkeypatch, capsys):
    """A missing SKILLS_DIR must WARN, not vacuously PASS the scan."""
    conn, db_path = _healthy_setup(tmp_path, monkeypatch)
    monkeypatch.setattr(db, "SKILLS_DIR", str(tmp_path / "gone"))
    rc = st._cli_doctor(conn, Args(db_path))
    out = capsys.readouterr().out
    assert "skills.dir" in out
    assert "[WARN]" in out
    assert "missing" in out
    assert "skills.frontmatter_names" not in out, "must not vacuously pass"
    assert rc == 0
    conn.close()
