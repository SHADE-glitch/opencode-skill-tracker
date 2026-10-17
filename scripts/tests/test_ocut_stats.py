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
