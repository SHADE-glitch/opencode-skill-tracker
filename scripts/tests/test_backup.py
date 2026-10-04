"""Tests for backup retention (plan_retention) and auto_backup."""

from __future__ import annotations

import os
import stat
import time
from datetime import datetime, timedelta, timezone

import pytest

import skill_db as db


def _name(y, mo, d, h=0, mi=0, s=0):
    return f"skill-usage-backup-{y:04d}{mo:02d}{d:02d}-{h:02d}{mi:02d}{s:02d}.db"


# ---------------------------------------------------------------------------
# plan_retention (pure)
# ---------------------------------------------------------------------------
def test_plan_keeps_newest_per_day_within_daily_window(monkeypatch):
    monkeypatch.setattr(db, "DAILY_KEEP", 2)
    monkeypatch.setattr(db, "MONTHLY_KEEP", 12)
    paths = [
        "/b/" + _name(2026, 6, 1, 1), "/b/" + _name(2026, 6, 1, 2),   # both old days
        "/b/" + _name(2026, 6, 2, 1), "/b/" + _name(2026, 6, 2, 2),
        "/b/" + _name(2026, 6, 3, 1), "/b/" + _name(2026, 6, 3, 2),
    ]
    plan = db.plan_retention(paths)
    assert set(plan["keep"]) == {
        "/b/" + _name(2026, 6, 3, 2),   # newest day, newest file
        "/b/" + _name(2026, 6, 2, 2),   # 2nd newest day
    }
    # June 1 is outside the daily window; its newest file survives only because
    # the monthly rule keeps the newest of the month — but that is June 3.
    assert set(plan["delete"]) == {
        "/b/" + _name(2026, 6, 1, 1), "/b/" + _name(2026, 6, 1, 2),
        "/b/" + _name(2026, 6, 2, 1), "/b/" + _name(2026, 6, 3, 1),
    }


def test_plan_monthly_keeps_older_months(monkeypatch):
    monkeypatch.setattr(db, "DAILY_KEEP", 1)
    monkeypatch.setattr(db, "MONTHLY_KEEP", 12)
    paths = [
        "/b/" + _name(2026, 1, 5), "/b/" + _name(2026, 2, 5), "/b/" + _name(2026, 3, 5),
        "/b/" + _name(2026, 3, 20),
    ]
    plan = db.plan_retention(paths)
    assert set(plan["keep"]) == {
        "/b/" + _name(2026, 3, 20),   # newest day + newest of March
        "/b/" + _name(2026, 2, 5),    # newest of February
        "/b/" + _name(2026, 1, 5),    # newest of January
    }
    assert plan["delete"] == ["/b/" + _name(2026, 3, 5)]


def test_plan_monthly_window_limit(monkeypatch):
    monkeypatch.setattr(db, "DAILY_KEEP", 1)
    monkeypatch.setattr(db, "MONTHLY_KEEP", 2)
    paths = ["/b/" + _name(2026, m, 10) for m in (1, 2, 3, 4)]
    plan = db.plan_retention(paths)
    assert set(plan["keep"]) == {
        "/b/" + _name(2026, 4, 10), "/b/" + _name(2026, 3, 10),
    }
    assert set(plan["delete"]) == {
        "/b/" + _name(2026, 2, 10), "/b/" + _name(2026, 1, 10),
    }


def test_plan_never_deletes_unparseable_names():
    paths = [
        "/b/random.db",
        "/b/skill-usage-backup-not-a-date.db",
        "/b/skill-usage.db",
        "/b/" + _name(2026, 6, 1),
    ]
    plan = db.plan_retention(paths)
    assert plan["delete"] == []
    assert set(plan["unparsed"]) == {
        "/b/random.db",
        "/b/skill-usage-backup-not-a-date.db",
        "/b/skill-usage.db",
    }
    assert plan["keep"] == ["/b/" + _name(2026, 6, 1)]


def test_plan_treats_a_shaped_but_impossible_date_as_unparsed():
    """Matches BACKUP_RE yet month 13 — strptime fails, so it is never deleted."""
    bad = "/b/skill-usage-backup-20261340-000000.db"
    plan = db.plan_retention([bad])
    assert plan["unparsed"] == [bad]
    assert plan["delete"] == []


def test_plan_empty_is_empty():
    assert db.plan_retention([]) == {"keep": [], "delete": [], "unparsed": []}


# ---------------------------------------------------------------------------
# auto_backup (filesystem)
# ---------------------------------------------------------------------------
def _backup_dir(tmp_path, monkeypatch):
    bdir = tmp_path / "backups"
    monkeypatch.setattr(db, "BACKUP_DIR", str(bdir))
    return bdir


def test_auto_backup_creates_file_with_safe_modes_and_prunes_old(seeded_db, tmp_path, monkeypatch):
    bdir = _backup_dir(tmp_path, monkeypatch)
    monkeypatch.setattr(db, "DAILY_KEEP", 1)
    monkeypatch.setattr(db, "MONTHLY_KEEP", 1)
    bdir.mkdir()
    old = [bdir / _name(2020, 1, d) for d in (1, 2)]
    for p in old:
        p.write_bytes(b"old")
        os.utime(p, (time.time() - 3 * 86400,) * 2)

    conn = db.open_db(seeded_db, readonly=False)
    res = db.auto_backup(conn)

    assert res["created"] is True
    assert os.path.isfile(res["backup"])
    assert os.path.basename(res["backup"]).startswith("skill-usage-backup-")
    assert stat.S_IMODE(os.stat(res["backup_dir"]).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(res["backup"]).st_mode) == 0o600
    assert not old[0].exists() and not old[1].exists(), "old backups pruned"
    assert os.path.basename(res["backup"]) in {os.path.basename(p) for p in res["kept"]}

    # row parity: the backup is a faithful snapshot
    snap = db.open_db(res["backup"])
    for table in ("skills", "skill_usage", "skill_versions"):
        assert (
            snap.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            == conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        ), table
    snap.close()
    conn.close()


def test_auto_backup_dry_run_writes_nothing(seeded_db, tmp_path, monkeypatch):
    bdir = _backup_dir(tmp_path, monkeypatch)
    conn = db.open_db(seeded_db, readonly=False)
    res = db.auto_backup(conn, dry_run=True)
    assert res["dry_run"] is True and res["created"] is False
    assert not bdir.exists(), "dry-run must not create the backup directory"
    assert res["backup"].startswith(str(bdir))
    conn.close()


def test_auto_backup_skips_young_files(seeded_db, tmp_path, monkeypatch):
    bdir = _backup_dir(tmp_path, monkeypatch)
    monkeypatch.setattr(db, "DAILY_KEEP", 1)
    monkeypatch.setattr(db, "MONTHLY_KEEP", 1)
    bdir.mkdir()
    young = bdir / _name(2020, 1, 1)     # would be pruned, but it is brand new
    young.write_bytes(b"young")

    conn = db.open_db(seeded_db, readonly=False)
    res = db.auto_backup(conn)
    assert young.exists(), "files younger than MIN_BACKUP_AGE_S are never deleted"
    assert str(young) in res["skipped_young"]
    conn.close()


def test_auto_backup_lock_blocks_concurrent_run(seeded_db, tmp_path, monkeypatch):
    bdir = _backup_dir(tmp_path, monkeypatch)
    bdir.mkdir()
    (bdir / ".lock").write_text("99999")  # fresh lock -> not stale
    conn = db.open_db(seeded_db, readonly=False)
    with pytest.raises(db.BackupLockError):
        db.auto_backup(conn)
    conn.close()


def test_auto_backup_takes_over_stale_lock(seeded_db, tmp_path, monkeypatch):
    bdir = _backup_dir(tmp_path, monkeypatch)
    bdir.mkdir()
    lock = bdir / ".lock"
    lock.write_text("99999")
    old = time.time() - db._LOCK_STALE_S - 60
    os.utime(lock, (old, old))

    conn = db.open_db(seeded_db, readonly=False)
    res = db.auto_backup(conn)          # must not raise
    assert res["created"] is True
    assert not lock.exists(), "lock is released after a successful run"
    conn.close()


def test_auto_backup_refuses_same_second_name(seeded_db, tmp_path, monkeypatch):
    bdir = _backup_dir(tmp_path, monkeypatch)
    conn = db.open_db(seeded_db, readonly=False)
    fixed = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
    db.auto_backup(conn, now=fixed)
    with pytest.raises(FileExistsError):
        db.auto_backup(conn, now=fixed)   # loud failure, never overwrite
    conn.close()


def test_auto_backup_ignores_non_matching_files(seeded_db, tmp_path, monkeypatch):
    bdir = _backup_dir(tmp_path, monkeypatch)
    monkeypatch.setattr(db, "DAILY_KEEP", 1)
    monkeypatch.setattr(db, "MONTHLY_KEEP", 1)
    bdir.mkdir()
    keep_me = bdir / "notes.txt"
    keep_me.write_text("hand-written, must survive")
    conn = db.open_db(seeded_db, readonly=False)
    res = db.auto_backup(conn)
    assert keep_me.exists()
    assert str(keep_me) in res["unparsed"]
    conn.close()


def test_backup_db_avoids_same_second_collision(seeded_db):
    """Two default-named backups in the same second must not collide."""
    conn = db.open_db(seeded_db, readonly=False)
    first = db.backup_db(conn)
    second = db.backup_db(conn)
    assert first != second, "the second backup must pick a free name"
    assert os.path.isfile(first) and os.path.isfile(second)
    # explicit targets still refuse to overwrite (loud failure preserved)
    with pytest.raises(FileExistsError):
        db.backup_db(conn, first)
    conn.close()


def test_backup_db_fails_loudly_when_no_name_is_free(seeded_db, tmp_path, monkeypatch):
    """Five occupied candidate names in a row must raise, not overwrite."""
    bdir = _backup_dir(tmp_path, monkeypatch)
    bdir.mkdir()
    now = datetime.now(timezone.utc)
    for bump in range(-1, 7):  # cover the call-time window regardless of a tick
        ts = (now + timedelta(seconds=bump)).strftime("%Y%m%d-%H%M%S")
        (bdir / f"skill-usage-backup-{ts}.db").write_bytes(b"x")
    conn = db.open_db(seeded_db, readonly=False)
    with pytest.raises(FileExistsError):
        db.backup_db(conn)
    conn.close()


def test_default_backup_lands_in_the_retention_directory(seeded_db):
    """M19: a backup nobody's retention can reach is a backup nobody rotates.

    backup_db()'s default used to write beside the database, while
    `plan_retention` and `doctor backups.latest` only ever look in BACKUP_DIR —
    so `skillt backup` silently created unpruned, unreported files (and, once
    redacted metadata existed, unredacted copies of the database).
    """
    conn = db.open_db(seeded_db, readonly=False)
    path = db.backup_db(conn)

    assert os.path.dirname(path) == db.BACKUP_DIR
    assert db.BACKUP_RE.match(os.path.basename(path)), path
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600

    db_dir = os.path.dirname(db.db_file_of(conn))
    assert db_dir != db.BACKUP_DIR, "fixture must not put the DB in BACKUP_DIR"
    assert not [n for n in os.listdir(db_dir) if db.BACKUP_RE.match(n)], \
        "the database's own directory must stay free of backups"

    # doctor reads BACKUP_DIR, so it must now be able to see this one.
    latest = max(os.path.getmtime(os.path.join(db.BACKUP_DIR, n))
                 for n in os.listdir(db.BACKUP_DIR) if db.BACKUP_RE.match(n))
    assert abs(latest - os.path.getmtime(path)) < 2
    conn.close()
