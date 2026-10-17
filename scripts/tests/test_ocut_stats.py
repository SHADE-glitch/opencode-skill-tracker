"""test_ocut_stats.py — the headless report CLI's output, pinned where it can be wrong.

`ocut health` is the command a person runs when they suspect capture has decayed, and
until now nothing asserted what it prints: `test_ocut_dispatcher.py` checks which
*script* a command routes to, `test_ocut_sync.py` checks the tables, and the TUI suite
checks the panels. That gap is how the unparsed line came to hold two different
findings — the reader's own holdback deadline and a host format change — inside one
number that could not be read.

The autouse `no_real_machine` fixture in `conftest.py` points `HOST_DB` and
`HOST_CONFIG_DIR` at paths that do not exist, so this module opens the machine's real
OpenCode install no more than any other file in the suite does.
"""

import contextlib
import importlib.util
import io
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import ocut_db as db                                    # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "ocut_stats_under_test", str(SCRIPTS / "ocut-stats.py"))
stats = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(stats)


def health(db_path) -> str:
    """`ocut health` against one database, its stdout as a string."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = stats.main(["--db", str(db_path), "health"])
    assert rc == 0, f"health exited {rc}"
    return out.getvalue()


def test_health_splits_the_unparsed_tally_into_known_and_new(tmp_path):
    """A total that holds a deadline and a format change tells the reader neither.

    `part_left_running` is this reader's own deadline: a `tool` part the host never
    settled is indexed as it stands, and the count is how many rows may be missing a
    status or a duration. A reason nothing here has been taught about is the host
    moving. The two must read differently, or the line is decoration.
    """
    path = tmp_path / "usage.db"
    conn = db.open_db(path)
    db.ensure_schema(conn)
    conn.execute("INSERT INTO unparsed(reason, count) VALUES('part_left_running', 3)")
    conn.commit()
    conn.close()

    text = health(path)
    assert "(3 known, 0 not)" in text, text
    assert "NEW" not in text, "an expected reason was marked as drift"
    assert "nothing has moved" in text

    conn = db.open_db(path)
    conn.execute("INSERT INTO unparsed(reason, count)"
                 " VALUES('unknown_part_type:wat', 2)")
    conn.commit()
    conn.close()

    text = health(path)
    assert "(3 known, 2 not)" in text, text
    assert "NEW" in text
    assert "unknown_part_type:wat" in text
    assert "the format moving" in text


def vacuum(db_path) -> tuple[int, str]:
    """`ocut vacuum` against one database: its exit code and its stdout."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = stats.main(["--db", str(db_path), "vacuum"])
    return rc, out.getvalue()


def test_vacuum_compacts_the_file_after_snapshotting_it(tmp_path):
    """The one command that rewrites the whole file, checked against its own before.

    A dropped index parks its pages on the freelist instead of releasing them, so
    compaction is the only thing that actually gives the space back — and the reading
    that needed it was taken by hand and written down as "`ocut` has no such
    subcommand". What a reader cannot check for themselves is whether the rewrite kept
    the rows, so the command asserts it in its own output and so does this case.
    """
    path = tmp_path / "usage.db"
    conn = db.open_db(path)
    db.ensure_schema(conn)
    # Rows and then no rows: the deletion is what leaves pages on the freelist for a
    # compaction to reclaim, which is the state a schema change also leaves behind.
    conn.executemany(
        "INSERT INTO tool_calls(call_id, session_id, project, tool_name, category,"
        " status, ts, duration_ms) VALUES(?,?,?,?,?,?,?,?)",
        [(f"prt_{i}", "ses_a", "proj", "bash", "builtin", "completed", i, 5)
         for i in range(4000)])
    conn.execute("DELETE FROM tool_calls")
    conn.execute("INSERT INTO tool_calls(call_id, session_id, project, tool_name,"
                 " category, status, ts, duration_ms)"
                 " VALUES('prt_keep','ses_a','proj','bash','builtin','completed',1,5)")
    conn.commit()
    # The deleted pages are in `-wal` until something folds them back, and the main
    # file is what both this case and the command compare — the command checkpoints
    # for the same reason.
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    before = path.stat().st_size
    assert before > 0
    conn.close()

    rc, text = vacuum(path)
    assert rc == 0, text
    assert "reclaimed  " in text, text
    assert "quick_check ok" in text and "every row count is what it was" in text, text
    assert path.stat().st_size < before, (before, path.stat().st_size)
    snapshots = sorted(db.backup_dir(path).glob("*pre-vacuum*.db"))
    assert len(snapshots) == 1, [p.name for p in snapshots]
    assert str(snapshots[0]) in text, text

    # The row that survived is still there, with the number the command promised.
    conn = db.open_db(path, readonly=True)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM tool_calls").fetchone()[0] == 1
    finally:
        conn.close()

    rc, text = vacuum(path)
    assert rc == 0, text
    assert "nothing — the file was already compact" in text, text


def test_vacuum_refuses_a_database_that_is_not_there(tmp_path, capsys):
    """The same refusal `ocut backup` gives, for the same reason: nothing to compact."""
    rc, _ = vacuum(tmp_path / "no-such.db")
    assert rc == 1
    assert "run: ocut sync" in capsys.readouterr().err


def test_health_still_says_zero_and_why_when_nothing_is_unparsed(tmp_path):
    """The empty case is printed too, so the line proves the check ran.

    Its absence would be indistinguishable from a report that forgot to look.
    """
    path = tmp_path / "usage.db"
    conn = db.open_db(path)
    db.ensure_schema(conn)
    conn.close()
    text = health(path)
    assert "unparsed       0 — every record type was claimed" in text, text
