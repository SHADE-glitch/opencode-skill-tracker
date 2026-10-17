"""test_ocut_indexes.py — no index this build declares may be one nothing reaches.

An index is not free. It is written on every insert, it is copy-on-write for every
`UPDATE`, it is half its own size in the file, and it pays all of that for the rest
of the database's life. The only thing that buys it back is a statement that reads
through it. This suite has cut two such indexes by hand — `idx_tool_sess` /
`idx_model_call_session` and `idx_plugin_usage_item`, both in `test_ocut_windows.py`
— and both times the *shape* of the index was pinned (a `session_id` key, an `item`
key) rather than the question, because the question had no answer: the first cut was
found by a regex over the source, and f-string SQL defeats a regex (`idx_plugin_usage_item`
survived the first sweep because its only reader built its statement from parts).

The two shape guards above stay: they are cheap, they name the exact miss nobody
should re-introduce, and they run without a database. What they cannot do is notice
the *next* one, which is what this file is for. The rule here is the general one —
**every index `TABLES_SQL` declares must be named by the plan of a statement this
build actually executes** — and the statements are not read out of the source, they
are *run*: the writer (`ocut-sync.py`) against a fixture host, every report command
in `ocut-stats.py`, and every public `q_*` in `ocut_db.py` through its own signature.
`set_trace_callback` records what ran; `EXPLAIN QUERY PLAN` then says which index
each statement can reach.

Reachability is necessary but not sufficient, and the 2026-10-17 cut is the case
that says so: `idx_model_call_model` **was** named by two of the 118 plans this file
collects — both full scans, which a wider key serves identically — so the reachability
rule would have passed it. The second rule is structural and needs no rows: an index
whose key is a **strict prefix** of another declared index's key on the same table can
never win a plan, because the wider index answers every lookup the narrower one can.
`PRAGMA index_info` reads the keys from the schema `ensure_schema` builds, so neither
rule parses DDL.

Two properties of the measurement make it exact rather than approximate:

* **Plans do not depend on the rows, so no fixture has to be sized.** Nothing in
  this build runs `ANALYZE`, so there is no `sqlite_stat1` for the planner to read
  and the plan is a function of the schema alone (measured: a `GROUP BY model` over
  an *empty* `model_calls` still picks `idx_model_call_model`, and the same query on
  the real 25,340-row table picks the same one). A guard that needed realistic row
  counts would be a guard nobody runs.
* **A statement is only skipped by keyword, never by a failure it caused.**
  `PRAGMA`, the transaction control statements and the DDL of `TABLES_SQL` have no
  plan to read; anything else that fails to plan fails the test, with the statement
  in the message. A guard that swallowed its own errors would report "nothing
  unreachable" on the day the parser broke.

`test_ocut_windows.py` owns the shape cuts; this file owns the reachability rule, so
a future cut has one place to land in.
"""

import contextlib
import importlib.util
import inspect
import io
import re
import sqlite3
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import ocut_db as db                                     # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_ocut_sync as ts_sync                         # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "ocut_stats_indexes", str(SCRIPTS / "ocut-stats.py"))
stats = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(stats)

_sync_spec = importlib.util.spec_from_file_location(
    "ocut_sync_indexes", str(SCRIPTS / "ocut-sync.py"))
sync = importlib.util.module_from_spec(_sync_spec)
_sync_spec.loader.exec_module(sync)

# The statements a plan cannot be read from: SQLite's own knobs, the transaction
# boundaries, and the DDL `ensure_schema` runs. Everything else is planned.
NO_PLAN = ("PRAGMA", "BEGIN", "COMMIT", "ROLLBACK", "SAVEPOINT", "RELEASE",
           "CREATE", "DROP", "ALTER", "VACUUM", "ANALYZE", "EXPLAIN")
PLANNABLE = ("SELECT", "WITH", "INSERT", "UPDATE", "DELETE", "REPLACE")

# The index names a plan can name. `sqlite_autoindex_*` is a primary key's own
# structure and is not in `_DECLARED_INDEXES`, so it is deliberately not matched.
INDEX_IN_PLAN = re.compile(r"\b(?:idx|ux)_\w+")

# Values for the `q_*` parameters, so every query function can be called the way the
# TUI calls it without this file keeping a list of the functions themselves. A query
# function that grows a parameter this map does not have fails the check below
# instead of being silently skipped.
QUERY_ARGUMENTS = {
    "limit": 5, "start_ts": None, "end_ts": None, "kind": "tool", "name": "bash",
    "model": "model-x", "plugin": "some-plugin", "server": "some-server",
    "offset": 0, "key": "call-1",
    # `None` for the whitelisted sort key: `q_usage_model_stats` falls back to its
    # own default for an unknown value, so the fallback's ORDER BY is what is planned.
    "order_by": None,
}
# The windowed half of the run: a query that leaves ts unconstrained can reach a
# different index than the same query with a window, and the tabs only ever ask the
# windowed form.
WINDOW = {"start_ts": 0, "end_ts": 2**62}


def _host(tmp_path: Path) -> Path:
    """A host database with one of everything the writer handles."""
    path = tmp_path / "opencode.db"
    conn = ts_sync.make_host(path)
    ts_sync.add_session(conn, "ses_parent", tokens=(100, 20, 5, 800, 0), cost=0.25)
    ts_sync.add_message(conn, "msg_a", "ses_parent")
    ts_sync.add_step(conn, "prt_step1", "msg_a", "ses_parent", cost=0.25)
    ts_sync.add_tool(conn, "prt_bash", "msg_a", "ses_parent")
    ts_sync.add_tool(conn, "prt_skill", "msg_a", "ses_parent",
                     tool="skill", ts=1_700_000_200_001)
    ts_sync.add_ignored(conn, "prt_text", "msg_a", "ses_parent")
    conn.commit()
    conn.close()
    return path


def _run_everything(tmp_path: Path) -> list[str]:
    """Every statement this build runs, captured as it runs it.

    The patch is `db.open_db` and not a list of call sites, so this covers the
    writer, the report CLI and the query functions through the same door they use
    in production — and it covers a *new* caller without being edited. The host
    connection is opened with a bare `sqlite3.connect` inside `ocut-sync.py`, so the
    host's own schema never enters the corpus.
    """
    tracker = tmp_path / "usage.db"
    conn = db.open_db(tracker)
    db.ensure_schema(conn)
    conn.close()
    host = _host(tmp_path)
    config = tmp_path / "ocut-config"
    ts_sync.make_config(config)

    seen: list[str] = []
    real_open = db.open_db

    def recording(*args, **kwargs):
        opened = real_open(*args, **kwargs)
        opened.set_trace_callback(seen.append)
        return opened

    db.open_db = recording
    try:
        result = sync.run(db_path=tracker, host_db=host, host_config=config,
                          plugin_events=tmp_path / "no-such-sidecar.jsonl",
                          home=tmp_path / "no-such-home")
        assert result["ok"] is True, result
        for name in sorted(stats.COMMANDS):
            argv = ["show", "tool", "bash"] if name == "show" else [name]
            with contextlib.redirect_stdout(io.StringIO()):
                stats.main(["--db", str(tracker), *argv])
        _call_every_query(tracker)
    finally:
        db.open_db = real_open
    return seen


def _call_every_query(tracker: Path) -> None:
    """Call every public `q_*` in `ocut_db`, all-time and windowed.

    The parameter names come from `inspect.signature`, so a query function added to
    the module is planned without this file being touched; a *parameter* with no
    value in `QUERY_ARGUMENTS` is a hard failure, because a query this check cannot
    call is a query whose index reachability is not being checked.
    """
    conn = db.open_db(tracker, readonly=True)
    try:
        for name, fn in sorted(vars(db).items()):
            if not name.startswith("q_") or not inspect.isfunction(fn):
                continue
            parameters = list(inspect.signature(fn).parameters)[1:]
            unknown = sorted(set(parameters) - set(QUERY_ARGUMENTS))
            assert not unknown, (
                f"{name} takes {unknown}, which this check has no value for: add "
                "them to QUERY_ARGUMENTS so its statement is still planned")
            for overrides in ({}, WINDOW):
                kwargs = {p: QUERY_ARGUMENTS[p] for p in parameters}
                kwargs.update({k: v for k, v in overrides.items() if k in kwargs})
                fn(conn, **kwargs)
    finally:
        conn.close()


def _placeholders(sql: str) -> int:
    """How many bindings the statement needs, ignoring `?` inside a literal."""
    stripped = re.sub(r"'(?:[^']|'')*'", "''", sql)
    stripped = re.sub(r"--[^\n]*", "", stripped)
    return stripped.count("?")


def _plan_index(statements, conn) -> tuple[set[str], int, int]:
    """``(indexes named, statements planned, statements skipped)``."""
    named: set[str] = set()
    planned = skipped = 0
    for sql in sorted(set(statements)):
        text = " ".join(sql.split())
        if not text:
            continue
        keyword = text.split()[0].upper()
        if keyword in NO_PLAN or keyword not in PLANNABLE:
            skipped += 1
            continue
        try:
            steps = conn.execute("EXPLAIN QUERY PLAN " + sql,
                                 [None] * _placeholders(sql)).fetchall()
        except sqlite3.Error as exc:
            raise AssertionError(
                f"a statement this build runs could not be planned ({exc}): {text}"
            ) from exc
        planned += 1
        for step in steps:
            named.update(INDEX_IN_PLAN.findall(step["detail"]))
    return named, planned, skipped


def _unreachable(named: set[str], declared) -> list[str]:
    """The declared indexes no plan named. The one decision this file exists for."""
    return sorted(set(declared) - named)


def _shape(conn, names) -> dict[str, tuple[str, tuple[str, ...]]]:
    """Each declared index's table and key columns, read from the schema it built.

    Not parsed out of `TABLES_SQL`: a regex over DDL is a second reader of it, and
    the one that drifts. `PRAGMA index_info` answers what SQLite actually created.
    """
    shape = {}
    for name in names:
        cols = tuple(r[2] or "" for r in conn.execute(f"PRAGMA index_info({name})"))
        table = conn.execute("SELECT tbl_name FROM sqlite_master WHERE name = ?",
                             (name,)).fetchone()
        shape[name] = (table[0] if table else "", cols)
    return shape


def _prefix_duplicates(shape) -> list[str]:
    """Indexes whose key is a strict prefix of another index's key on the same table.

    Reachability cannot see these, which is why they need a rule of their own:
    `idx_model_call_model` was named by three plans — one equality search and two
    full scans, all of which a wider index serves just as well, measured at the same
    cost — so a plan-count check passes it on the day it is written and keeps
    passing it. A wider index answers every lookup the narrower one can (same
    leading columns, plus more), so the narrower is a write cost with no plan it can
    win.
    """
    return sorted(
        name for name, (table, cols) in shape.items()
        if any(other != name and other_table == table and len(cols) < len(other_cols)
               and other_cols[:len(cols)] == cols
               for other, (other_table, other_cols) in shape.items()))


@pytest.fixture
def statements(tmp_path):
    return _run_everything(tmp_path)


def test_no_declared_index_is_unreachable_from_a_statement_the_code_runs(statements):
    """An index nothing reads is a write cost with a comment attached to it."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        db.ensure_schema(conn)
        named, planned, skipped = _plan_index(statements, conn)
    finally:
        conn.close()

    # A corpus that came out empty would report every index as unreachable, so the
    # floor is not decoration: it is what separates "nothing is dead" from "nothing
    # was captured". `idx_tool_name` is asserted *reached* for the same reason.
    assert planned >= 40, (
        f"only {planned} statements were planned ({skipped} skipped): the corpus is "
        "too small for this check to mean anything")
    assert "idx_tool_name" in named, (
        "`idx_tool_name` is reached by the tools report; if the plans no longer name "
        "it, this check is reading something other than the planner's answer")

    missing = _unreachable(named, db._DECLARED_INDEXES)
    assert missing == [], (
        f"{missing} is declared by TABLES_SQL and named in none of the {planned} "
        "plans this build's own statements produce. An index no statement reads is "
        "written on every insert and buys nothing — either drop it from TABLES_SQL "
        "(the retire-on-open sweep in `ensure_schema` removes it from existing "
        "databases) or write the query that reads it.")


def test_no_declared_index_is_a_prefix_of_a_wider_one():
    """The rule the reachability check above cannot be, and the one that cut last.

    The 2026-10-17 cut (`idx_model_call_model`) was named by three plans, so
    `test_no_declared_index_is_unreachable_from_a_statement_the_code_runs` would
    have passed it — and the docstring on that cut says the reachability rule found
    it, which it did not. What found it is this: `(model)` is `(model, ts)` minus a
    column, and SQLite without `ANALYZE` cannot prefer the narrower key for a plan
    the wider one also serves. Measured on a copy of the live file, all 118
    statements this check collects, planned with and without the index in a fresh
    connection each: exactly three plans moved — one `SEARCH model_calls USING
    INDEX (model=?)` and two `SCAN model_calls USING INDEX` swapping one covering
    key for the other — and 300 timed runs of each roll-up were indistinguishable
    (27.8 against 28.1 ms, 28.4 against 28.0 — the sign flips).
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        db.ensure_schema(conn)
        shape = _shape(conn, db._DECLARED_INDEXES)
    finally:
        conn.close()

    missing = sorted(name for name, (_, cols) in shape.items() if not cols)
    assert not missing, (
        f"{missing} is declared but not built by `ensure_schema`, so its key is "
        "unknown and this check cannot see the index that duplicates it")
    assert _prefix_duplicates(shape) == [], (
        f"{_prefix_duplicates(shape)} has a key that is a strict prefix of another "
        "declared index's key on the same table. A wider index serves every lookup "
        "the narrower one can, and no plan can prefer the narrow one, so drop it "
        "from `TABLES_SQL` — the retire-on-open sweep removes it from existing "
        "databases.")


def test_a_prefix_duplicate_is_reported_and_its_wider_key_is_not():
    """The guard, provoked with a name instead of with the repository."""
    planted = {
        "idx_a": ("t", ("model",)),
        "idx_b": ("t", ("model", "ts")),
        "idx_c": ("t", ("tool",)),
        "idx_d": ("other", ("model",)),
        "idx_e": ("t", ("ts", "model")),
    }
    assert _prefix_duplicates(planted) == ["idx_a"], (
        "a narrower key must be reported, and the wider key it duplicates, the "
        "same key on a different table and a different column order must not be")


def test_an_index_that_never_appears_in_a_plan_is_reported(statements):
    """The guard, provoked with a name instead of with the repository.

    Editing `TABLES_SQL` to add a dead index would prove the same thing and leave a
    broken schema behind for the length of the run (AGENTS.md: a guard is provoked
    with a string, not with the repository). Handing `_unreachable` a declared name
    the statements cannot reach is the whole check: it must come back, and it must
    come back alone.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        db.ensure_schema(conn)
        named, _, _ = _plan_index(statements, conn)
    finally:
        conn.close()

    planted = "idx_nothing_reads"
    assert planted not in named, "the planted name reached a plan — pick another"
    assert _unreachable(named, (*db._DECLARED_INDEXES, planted)) == [planted], (
        "a declared index no plan names must be reported, and only it")
    assert _unreachable(named, db._DECLARED_INDEXES) == [], (
        "the same call with the real list must be empty, or the provocation above "
        "proves nothing about the real list")
