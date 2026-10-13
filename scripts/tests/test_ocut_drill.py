"""Tests for the drill-down: which row opens what, at whose window, and what it prints.

The TUI has three levels — an entity tab, that entity's calls in the tab's own window,
and one call's fields — and each level hands identity to the next through a row key.
These tests follow that handoff, because every failure here is a disagreement between
two things the person sees at once: the title above a table, the counts in a header,
the row under the cursor, and the record it opens.

The fixture is its own on purpose. It needs **two MCP servers exposing the same tool
name** — the only shape that tells a (server, tool) key from a tool-only query — a call
whose message row is absent, which is how a real NULL reaches the detail screen, and
values longer than the cell cap on both sides of it (an opaque 40-character project id
and a 35-character tool name), because the cap is what makes the row key load-bearing.
The part shapes still come from `test_ocut_sync`, so the host rows are the same JSON
the indexer is tested against.
"""

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ocut_db as db                                    # noqa: E402
from textual.widgets import DataTable, Static           # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "ocut_drill_tui", str(Path(__file__).resolve().parents[1] / "ocut-tui.py"))
tui = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tui)

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_ocut_sync as ts_sync                        # noqa: E402
import test_ocut_tui as tui_tests                       # noqa: E402

DAY = 86_400_000
# Longer than NAME_CAP (28), and only its tail distinguishes it from other paths.
DEEP_DIR = "/home/user/projects/very/deep/nested/workspace/name/for/left/capping"
# Longer than NAME_CAP too, on the other side of it: a tool name is capped from the
# right, so this literal is what both the column plan and `_cap_cell` are checked
# against in the same table.
LONG_NAME = "context7_resolve-library-id-and-more"
# The shape the host actually puts in `session.project_id`, measured 2026-10-13 on
# 324 real sessions: 229 of them 6 characters, 95 of them 40, none containing a "/".
# So the drill-down's "project" column is an opaque id here, not a path — the value
# the cap has to shorten without the row key losing it.
DEEP_PROJECT_ID = "3f2a1c9d8b7e6f5a4b3c2d1e0f9a8b7c6d5e4f3a"


def build_host(path: Path):
    """Two servers sharing a tool name, one deep path, one orphaned call."""
    now = tui_tests.now_ms()
    conn = ts_sync.make_host(path)
    ts_sync.add_session(conn, "ses_alpha", tokens=(0, 0, 0, 0, 0))
    ts_sync.add_session(conn, "ses_beta", tokens=(0, 0, 0, 0, 0))
    ts_sync.add_session(conn, "ses_deep", tokens=(0, 0, 0, 0, 0),
                        project=DEEP_PROJECT_ID, directory=DEEP_DIR)
    ts_sync.add_session(conn, "ses_old", tokens=(0, 0, 0, 0, 0),
                        started=now - 40 * DAY, ended=now - 40 * DAY + 60_000,
                        directory="/home/user/work")
    for mid, sid in (("msg_alpha", "ses_alpha"), ("msg_beta", "ses_beta"),
                     ("msg_deep", "ses_deep"), ("msg_old", "ses_old")):
        ts_sync.add_message(conn, mid, sid)
    # `search` on two servers: three calls on alpha (one an error), one on beta.
    for i in range(3):
        ts_sync.add_tool(conn, f"prt_a_{i}", "msg_alpha", "ses_alpha",
                         tool="alpha_search", ts=now - i * 60_000,
                         status="error" if i == 2 else "completed")
    ts_sync.add_tool(conn, "prt_b_0", "msg_beta", "ses_beta", tool="beta_search",
                     ts=now - 30_000)
    # `read`: one call today, five forty days back, so a day and all-time disagree.
    ts_sync.add_tool(conn, "prt_read_today", "msg_alpha", "ses_alpha", tool="read",
                     ts=now - 10_000)
    for i in range(5):
        ts_sync.add_tool(conn, f"prt_old_{i}", "msg_old", "ses_old", tool="read",
                         ts=now - 40 * DAY + i * 1000)
    # A name longer than NAME_CAP, so the wide/narrow column plans differ by more
    # than arithmetic on the header labels, and a cut is visible in a real cell.
    ts_sync.add_tool(conn, "prt_long", "msg_alpha", "ses_alpha",
                     tool=LONG_NAME, ts=now - 8_000)
    # A call naming a message row that does not exist: the indexer leaves `model`
    # NULL for it (MESSAGE_ROW_ABSENT), so the detail screen has a genuine absence
    # to render — this is the shape, not an invented one.
    ts_sync.add_tool(conn, "prt_orphan", "msg_absent", "ses_alpha", tool="ghost",
                     ts=now - 9_000)
    # A skill call inside the deep-path session, for the left-capped project cell.
    ts_sync.add_tool(conn, "prt_skill", "msg_deep", "ses_deep", tool="skill",
                     state={"input": {"name": "triage"}}, ts=now - 5_000)
    ts_sync.add_step(conn, "prt_step", "msg_alpha", "ses_alpha", ts=now - 2_000,
                     tokens={"total": 1000, "input": 400, "output": 100,
                             "reasoning": 50, "cache": {"read": 450, "write": 0}})
    conn.execute("UPDATE session SET tokens_input=400, tokens_output=100,"
                 " tokens_reasoning=50, tokens_cache_read=450 WHERE id='ses_alpha'")
    conn.commit()
    conn.close()


def build_config(root: Path):
    """Only the servers the fixture calls.

    A declared-but-unused server still earns a row on the MCP tab (with `-` as its
    tool), which would put a third row where these tests count two.
    """
    root.mkdir(parents=True, exist_ok=True)
    (root / "opencode.json").write_text(json.dumps({
        "$schema": "https://opencode.ai/config.json",
        "mcp": {"alpha": {"url": "https://a"}, "beta": {"url": "https://b"}},
        "plugin": ["@acme/demo-plugin@1.2.3"]}))
    return root


@pytest.fixture
def tracker(tmp_path):
    host = tmp_path / "opencode.db"
    build_host(host)
    cfg = build_config(tmp_path / "cfg")
    out = tmp_path / "usage.db"
    db.load_sync().run(db_path=out, host_db=host, host_config=cfg, quiet=True)
    return out


@pytest.fixture
def clock(tracker):
    pinned = tui_tests.now_ms() / 1000
    return lambda: pinned


@pytest.fixture
def hooked_tracker(tmp_path):
    """The same host + config, plus a hook sidecar — plugin usage that is real."""
    host = tmp_path / "opencode.db"
    build_host(host)
    cfg = build_config(tmp_path / "cfg")
    events = tmp_path / "plugin-events.jsonl"
    events.write_text("\n".join(json.dumps(r) for r in (
        {"t": "surface", "plugin": "@acme/demo-plugin@1.2.3",
         "tools": ["compress"], "commands": ["dcp-compress"]},
        {"t": "call", "ts": tui_tests.now_ms() - 1_000, "sid": "ses_alpha",
         "cid": "c1", "plugin": "@acme/demo-plugin@1.2.3", "kind": "tool",
         "item": "compress", "status": None, "ms": 5},
        {"t": "call", "ts": tui_tests.now_ms() - 500, "sid": "ses_alpha",
         "cid": "cmd:ses_alpha:1", "plugin": "@acme/demo-plugin@1.2.3",
         "kind": "command", "item": "dcp-compress", "status": None, "ms": None},
    )) + "\n")
    out = tmp_path / "usage.db"
    db.load_sync().run(db_path=out, host_db=host, host_config=cfg,
                       plugin_events=events, quiet=True)
    return out


def make_app(tracker, clock=None):
    return lambda: (tui.TrackerApp(str(tracker)) if clock is None
                    else tui.TrackerApp(str(tracker), clock=clock))


def headers_of(table):
    return [str(table.columns[k].label) for k in table.columns]


def rows_of(table):
    return [[str(table.get_cell_at((i, j))) for j in range(len(table.columns))]
            for i in range(table.row_count)]


async def open_row(app, pilot, selector, index=0):
    """Route a row the way the interface does, and return the screen it pushed.

    `DataTable` posts `RowSelected` when a click lands on the cell that already holds
    the row cursor, so the *second* click of a double click is the trigger — the same
    recipe the legacy suite uses. `pilot.press("enter")` alone is not it: the binding
    is on the table and focus stays on the tab strip, which is how a first draft of
    these tests "passed" while opening nothing at all.

    The table is looked up on `app.screen`, not on `app`: `App.query_one` searches the
    base screen, so a second-level row (a call inside a pushed history) is invisible
    to it and raises `NoMatches` — which reads as "nothing opened" when the opposite
    happened.
    """
    before = len(app.screen_stack)
    table = app.screen.query_one(selector, DataTable)
    table.move_cursor(row=index)
    await pilot.pause(0.3)
    await pilot.double_click(selector, offset=(4, index + 1))
    await pilot.pause(0.7)
    # One activation, one screen. The push is not awaited inside the handler, so the
    # tail of a double click can land while the new screen is still mounting; a second
    # copy of it would leave the footer's "Back" popping to the same list.
    assert len(app.screen_stack) <= before + 1, \
        f"{selector} row {index} pushed {len(app.screen_stack) - before} screens"
    if len(app.screen_stack) == before:
        return None
    return app.screen_stack[-1]


def text_of(screen, selector):
    return str(screen.query_one(selector, Static).content)


def conn_of(tracker):
    conn = sqlite3.connect(str(tracker))
    conn.row_factory = sqlite3.Row
    return conn


# --- level one: which row is which entity ----------------------------------

def test_two_servers_sharing_a_tool_name_open_two_different_histories(tracker, clock):
    """An MCP row's identity is the (server, tool) pair, not the tool alone.

    `mcp_usage.tool` holds `search` for both rows, so a history that queried the tool
    without the server would show four calls under each row while titling them
    differently — and the header counts would inherit the same scope error.
    """
    conn = conn_of(tracker)

    async def body(pilot, app):
        await tui_tests.activate(app, pilot, "tab-mcp")
        table = app.query_one("#t-mcp", DataTable)
        assert headers_of(table) == list(tui.MCP_COLUMNS), headers_of(table)
        assert [[r[0], r[1]] for r in rows_of(table)] == \
            [["alpha", "search"], ["beta", "search"]], rows_of(table)

        for i, (server, calls) in enumerate((("alpha", 3), ("beta", 1))):
            screen = await open_row(app, pilot, "#t-mcp", i)
            assert isinstance(screen, tui.HistoryScreen), type(screen)
            assert (screen.kind, screen.entity, screen.server) == \
                ("mcp", "search", server), (screen.kind, screen.entity, screen.server)
            hist = screen.query_one("#hist-table", DataTable)
            expected = db.q_history(conn, "mcp", "search", server=server,
                                    start_ts=screen.start_ts, end_ts=screen.end_ts)
            assert hist.row_count == len(expected) == calls, \
                (server, hist.row_count, calls, len(expected))
            assert f"{calls} call" in text_of(screen, "#hist-summary"), server
            assert f"{server} · search" in text_of(screen, "#hist-title"), server
            await pilot.press("escape")
            await pilot.pause(0.5)
            assert len(app.screen_stack) == 1, "popping did not come back to the tab"
    tui_tests.drive(make_app(tracker, clock), body)
    conn.close()


def test_the_header_counts_are_the_lists_own_window_and_scope(tracker, clock):
    """The summary line above the table and the rows in it are one measurement.

    The fixture's `read` has 6 calls all-time and 1 today, so a drill-down that
    dropped the window would print 6 above a list of 1. The window travels from the
    tab; nothing here sets it on the screen.
    """
    conn = conn_of(tracker)

    async def body(pilot, app):
        await tui_tests.activate(app, pilot, "tab-tools")
        for rng, expect in (("1d", 1), ("all", 6)):
            app.tab_range["tab-tools"] = rng
            app._refresh_active_tab()
            await pilot.pause(0.6)
            table = app.query_one("#t-tools", DataTable)
            index = next(i for i in range(table.row_count)
                         if str(table.get_cell_at((i, 0))) == "read")
            screen = await open_row(app, pilot, "#t-tools", index)
            assert isinstance(screen, tui.HistoryScreen), type(screen)
            hist = screen.query_one("#hist-table", DataTable)
            assert hist.row_count == expect, (rng, hist.row_count, expect)
            assert (screen.start_ts, screen.end_ts) == app._bounds_for("tab-tools"), rng
            assert len(db.q_history(conn, "tool", "read", start_ts=screen.start_ts,
                                   end_ts=screen.end_ts)) == expect, rng
            summary = text_of(screen, "#hist-summary")
            assert f"{expect} call" in summary, (rng, summary)
            assert db.window_label(rng) in summary, (rng, summary)
            await pilot.press("escape")
            await pilot.pause(0.5)
    tui_tests.drive(make_app(tracker, clock), body)
    conn.close()


def test_the_plugins_row_opens_a_screen_that_names_the_missing_evidence(tracker, clock):
    """The fixture runs no hook, so the drill-down says which evidence is absent.

    Usage now comes from `plugin/ocut-hook.js`'s sidecar, and this fixture has none —
    so `q_plugin_history` finds nothing. Both the header and the table's empty row have
    to say the hook recorded nothing, instead of sounding like a usage figure ("no
    uses") or blaming an inventory for ownership the host never records.
    """
    async def body(pilot, app):
        await tui_tests.activate(app, pilot, "tab-plugins")
        table = app.query_one("#t-plugins", DataTable)
        assert table.row_count == 1, rows_of(table)
        screen = await open_row(app, pilot, "#t-plugins", 0)
        assert isinstance(screen, tui.HistoryScreen), type(screen)
        summary = text_of(screen, "#hist-summary")
        assert "no calls recorded by the hook" in summary, summary
        assert "no uses" not in summary, summary
        hist = screen.query_one("#hist-table", DataTable)
        assert hist.row_count == 1, rows_of(hist)
        assert "recorded by the hook" in str(hist.get_cell_at((0, 0)))
    tui_tests.drive(make_app(tracker, clock), body)


def test_the_plugins_row_opens_the_hooks_own_record(hooked_tracker):
    """With a sidecar, the plugin row opens a call from plugin_usage, not tool_calls.

    Two levels below the tab: the history lists the hook's tool + command, and each
    row opens a "plugin" detail — our own record, keyed by plugin_usage.call_id. The
    host names no plugin, so there is no tool_calls row to open instead.
    """
    async def body(pilot, app):
        await tui_tests.activate(app, pilot, "tab-plugins")
        table = app.query_one("#t-plugins", DataTable)
        assert table.row_count == 1, rows_of(table)
        assert str(table.get_cell_at((0, 2))) == "2", rows_of(table)   # uses
        screen = await open_row(app, pilot, "#t-plugins", 0)
        assert isinstance(screen, tui.HistoryScreen), type(screen)
        hist = screen.query_one("#hist-table", DataTable)
        assert hist.row_count == 2, rows_of(hist)
        assert {str(hist.get_cell_at((i, 1))) for i in range(2)} == {"tool",
                                                                     "command"}
        detail = await open_row(app, pilot, "#hist-table", 0)
        assert isinstance(detail, tui.CallDetailScreen), type(detail)
        text = "\n".join(str(w.content) for w in detail.query(Static))
        assert "compress" in text, text
    tui_tests.drive(make_app(hooked_tracker), body)


# --- level two: the call the row points at ---------------------------------

def test_a_call_row_opens_its_own_record_and_prints_no_prose(tracker, clock):
    """Third level: the key is the call's own id, and only metadata reaches the screen.

    The host's part JSON carries prose in `state.title`, `state.output`,
    `state.metadata` and `state.error.data.message`. None of it may be stored, so
    every sentinel the sync fixtures use is searched for in everything the detail
    renders — including the values on the failed call, whose error text does exist.
    """
    conn = conn_of(tracker)

    async def body(pilot, app):
        await tui_tests.activate(app, pilot, "tab-mcp")
        screen = await open_row(app, pilot, "#t-mcp", 0)
        hist = screen.query_one("#hist-table", DataTable)
        key = hist.coordinate_to_cell_key((0, 0)).row_key.value
        kind, _, rest = key.partition("\t")
        inner, _, call_id = rest.partition("\t")
        assert (kind, inner) == ("call", "mcp"), key
        assert call_id, key
        mine = db.q_history(conn, "mcp", "search", server="alpha",
                            start_ts=screen.start_ts, end_ts=screen.end_ts)
        assert call_id in {r["call_id"] for r in mine}, (call_id, list(mine))

        detail = await open_row(app, pilot, "#hist-table", 0)
        assert isinstance(detail, tui.CallDetailScreen), type(detail)
        assert detail.key == call_id, (detail.key, call_id)
        text = "\n".join(str(w.content) for w in detail.query(Static))
        assert call_id in text, text
        for name, value in ts_sync.SENTINELS.items():
            assert str(value) not in text, (name, value)
        assert "search" in text and "alpha" in text, text
        assert "mcp_usage" not in text, "a raw table name leaked into the detail"
    tui_tests.drive(make_app(tracker, clock), body)
    conn.close()


def test_an_absent_field_reads_as_a_dash_never_as_zero(tracker, clock):
    """NULL is "the host did not report it"; 0 would be a measurement of the call.

    Reached with a call whose message row does not exist: the indexer writes the row
    and leaves `model` NULL (`MESSAGE_ROW_ABSENT`). The assertion is on the rendered
    pair, and the database is read back to prove the absence is real rather than a
    dash the formatter chose for a value that was there.
    """
    conn = conn_of(tracker)

    async def body(pilot, app):
        await tui_tests.activate(app, pilot, "tab-tools")
        table = app.query_one("#t-tools", DataTable)
        index = next(i for i in range(table.row_count)
                     if str(table.get_cell_at((i, 0))) == "ghost")
        screen = await open_row(app, pilot, "#t-tools", index)
        detail = await open_row(app, pilot, "#hist-table", 0)
        assert isinstance(detail, tui.CallDetailScreen), type(detail)

        row = db.q_call_detail(conn, "tool", detail.key)
        assert row is not None and row["model"] is None, dict(row or {})
        pairs = tui._call_detail_rows(row)
        assert [t for label, t in pairs if label == "model"] == ["-"], pairs
        # The rest of the same row still reports real values, so the dash is not the
        # whole screen — a formatter that broke would print "-" for everything.
        assert [t for label, t in pairs if label == "status"] == ["completed"], pairs
        assert any(t not in ("-", "") for label, t in pairs
                   if label in ("duration", "session", "project")), pairs
    tui_tests.drive(make_app(tracker, clock), body)
    conn.close()


# --- the wide-table machinery under a cap ----------------------------------

def test_a_narrow_screen_shows_a_prefix_and_says_what_is_hidden(tracker, clock):
    """The column plan is a prefix, and the note names the cut — at both widths.

    Both directions are asserted because at a wide terminal nothing is hidden and the
    note must be *empty*: a note that always speaks trains people to ignore it.
    """
    seen = {}

    async def wide(pilot, app):
        await tui_tests.activate(app, pilot, "tab-tools")
        table = app.query_one("#t-tools", DataTable)
        assert headers_of(table) == list(tui.TOOLS_COLUMNS), headers_of(table)
        seen["note"] = text_of(app.screen, "#colnote-tools")
        seen["first"] = str(table.get_cell_at((0, 0)))
        seen["key"] = table.coordinate_to_cell_key((0, 0)).row_key.value
        # Nothing was cut, so the long name is there in full: a plan that fits does
        # not spend a cell, and the note must not claim otherwise.
        row = next(i for i in range(table.row_count)
                   if str(table.get_cell_at((i, 0))).startswith("context7"))
        assert str(table.get_cell_at((row, 0))) == LONG_NAME, table.get_cell_at((row, 0))

    tui_tests.drive(make_app(tracker, clock), wide, size=(150, 50))
    assert seen["note"] == "", seen

    async def small(pilot, app):
        table = app.query_one("#t-tools", DataTable)
        headers = headers_of(table)
        assert len(headers) < len(tui.TOOLS_COLUMNS), headers
        assert len(headers) >= tui.WideTableMixin.MIN_SHOWN, headers
        assert headers == list(tui.TOOLS_COLUMNS)[:len(headers)], headers
        note = text_of(app.screen, "#colnote-tools")
        assert note, "a cut with no note is silent"
        for hidden in [c for c in tui.TOOLS_COLUMNS if c not in headers]:
            assert hidden in note, (hidden, note)
        # The width the note names is the least one that costs nothing: at it every
        # column shows and no cell is shortened, one cell below the cap still bites.
        # The number is derived from the same widths the plan decides with, so only
        # driving it back through the plan can tell an honest one from a padded one.
        promised = int(note.rpartition("widen to ")[2].partition(" ")[0])
        app._plan_wide_tables(width=promised)
        await pilot.pause(0.4)
        assert headers_of(app.query_one("#t-tools", DataTable)) == \
            list(tui.TOOLS_COLUMNS), promised
        assert text_of(app.screen, "#colnote-tools") == "", promised
        app._plan_wide_tables(width=promised - 1)
        await pilot.pause(0.4)
        assert "capped" in text_of(app.screen, "#colnote-tools"), promised - 1
        app._plan_wide_tables(width=52)
        await pilot.pause(0.4)
        # The cut is visible in the data, not only in the header list: the same name
        # that was whole at 150 cells now comes back shortened.
        cells = [str(table.get_cell_at((i, 0))) for i in range(table.row_count)]
        assert LONG_NAME[:app.name_cap - 1] + "…" in cells, cells
        assert max(len(c) for c in cells) <= app.name_cap, cells
        # A narrower plan is the same data: the first cell is unchanged, and the row
        # under it is the same entity — the key is what the drill-down reads, and it
        # is built from the uncapped value.
        assert str(table.get_cell_at((0, 0))) == seen["first"]
        assert table.coordinate_to_cell_key((0, 0)).row_key.value == seen["key"]
    tui_tests.drive(make_app(tracker, clock), small, size=(52, 40))


def test_resizing_replans_the_columns_without_querying_again(tracker, clock):
    """A resize is a layout event, not a data event — `_wide_rows` is the cache."""
    calls = []
    real = db.q_tools

    def spy(conn, *a, **kw):
        calls.append(1)
        return real(conn, *a, **kw)

    db.q_tools = spy
    try:
        async def body(pilot, app):
            await tui_tests.activate(app, pilot, "tab-tools")
            before = headers_of(app.query_one("#t-tools", DataTable))
            base = len(calls)
            assert base, "the tab was never queried, so the count proves nothing"
            app._plan_wide_tables(width=52)
            await pilot.pause(0.5)
            after = headers_of(app.query_one("#t-tools", DataTable))
            assert len(after) < len(before), (before, after)
            assert len(calls) == base, "a resize re-queried the database"
            app._plan_wide_tables(width=150)
            await pilot.pause(0.5)
            assert headers_of(app.query_one("#t-tools", DataTable)) == before
            assert len(calls) == base, "a resize back to width re-queried"
        tui_tests.drive(make_app(tracker, clock), body, size=(150, 50))
    finally:
        db.q_tools = real


def test_a_cell_is_cut_for_the_screen_and_whole_for_the_key(tracker, clock):
    """The cap is a display rule; the row key keeps the value the drill-down needs.

    Which half survives is a fact about the value: a path shares its head and differs
    at the end, so it is cut from the left; a name or an id reads from its start, so it
    is cut from the right. Both directions are called directly, because on this
    substrate only one of them is reachable through a table — `session.project_id` is
    6 or 40 characters with no "/" in any of 324 real sessions (measured 2026-10-13),
    so the drill-down's "project" column is an opaque id and a real path arrives only as
    `tool_calls.directory`, which the detail screen prints whole. A rule this narrow
    must be seen doing both jobs, or the second branch is a claim nobody checks.

    The key half is the one the handoff depends on: at a narrow terminal two entities
    whose names share a 27-cell prefix render identically, and only the un-truncated
    key tells them apart.
    """
    conn = conn_of(tracker)

    async def body(pilot, app):
        cap = app.name_cap
        assert len(DEEP_DIR) > cap and len(LONG_NAME) > cap, cap
        path = app._cap_cell(DEEP_DIR)
        name = app._cap_cell(LONG_NAME)
        assert len(path) <= cap and len(name) <= cap, (path, name)
        assert path.startswith("…") and DEEP_DIR.endswith(path[1:]), (path, DEEP_DIR)
        assert name.endswith("…") and name[:-1] == LONG_NAME[:cap - 1], name
        # A value that already fits is returned untouched — a cap that always spent a
        # cell would shorten every row of a wide table for nothing.
        assert app._cap_cell("read") == "read"

        await tui_tests.activate(app, pilot, "tab-tools")
        table = app.query_one("#t-tools", DataTable)
        index = next(i for i in range(table.row_count)
                     if str(table.get_cell_at((i, 0))).endswith("…"))
        cell = str(table.get_cell_at((index, 0)))
        key = table.coordinate_to_cell_key((index, 0)).row_key.value
        assert cell == LONG_NAME[:cap - 1] + "…", (cell, cap)
        assert key == "tool\t" + LONG_NAME, key
        # …and the screen the row opens is about the whole name, not its display form.
        screen = await open_row(app, pilot, "#t-tools", index)
        assert screen.entity == LONG_NAME, screen.entity
        assert LONG_NAME in text_of(screen, "#hist-title"), text_of(screen, "#hist-title")
        # The whole name matches a call; the clipped display form matches nothing. So
        # a row that renders like a neighbour still opens its own entity, and the cap
        # is provably not what the query received.
        assert len(db.q_history(conn, "tool", LONG_NAME, start_ts=screen.start_ts,
                                end_ts=screen.end_ts)) == 1
        assert db.q_history(conn, "tool", cell, start_ts=screen.start_ts,
                            end_ts=screen.end_ts) == []

    async def deep_project_cell(pilot, app):
        await tui_tests.activate(app, pilot, "tab-skills")
        screen = await open_row(app, pilot, "#t-skills", 0)
        hist = screen.query_one("#hist-table", DataTable)
        projects = [str(hist.get_cell_at((i, 1))) for i in range(hist.row_count)]
        assert DEEP_PROJECT_ID not in projects, projects
        assert DEEP_PROJECT_ID[:app.name_cap - 1] + "…" in projects, projects
        assert all(len(p) <= app.name_cap for p in projects), projects
    tui_tests.drive(make_app(tracker, clock), body, size=(52, 40))
    tui_tests.drive(make_app(tracker, clock), deep_project_cell, size=(52, 40))
    conn.close()


def test_an_empty_state_row_opens_nothing(tracker, clock):
    """The explanation for an empty tab is not a row, so it must not route.

    The Agents tab is the honest case: this fixture starts no subagent and its config
    declares no agents, so the name list is genuinely empty. The table still holds one
    row — the note — and selecting it has to leave the screen stack exactly where it
    was, because a drill-down onto NULL would open a history for nothing.
    """
    async def body(pilot, app):
        await tui_tests.activate(app, pilot, "tab-agents")
        table = app.query_one("#t-agents", DataTable)
        assert table.row_count == 1, rows_of(table)
        note = str(table.get_cell_at((0, 0)))
        assert "No agent" in note, note
        screen = await open_row(app, pilot, "#t-agents", 0)
        assert screen is None, type(screen)
        assert len(app.screen_stack) == 1, note
    tui_tests.drive(make_app(tracker, clock), body)
