"""Tests for the TUI's own contract: the eight tabs, their windows, and the
numbers they print.

Everything here is pinned against a fixture built through the real indexer
(`ocut-sync.py`), not against a hand-inserted database, so the chain
host rows -> cursor -> table -> panel is one test. The row counts and token sums
are re-read from the same database with plain SQL, so a panel that silently
switched to a different query — or forgot the window — fails here instead of
looking plausible.
"""

import asyncio
import importlib.util
import json
import sqlite3
import sys
import threading
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ocut_db as db                                    # noqa: E402
from textual.widgets import DataTable                   # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "ocut_tui_under_test", str(Path(__file__).resolve().parents[1] / "ocut-tui.py"))
tui = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tui)

# Reuse the host-database builders from the sync suite. Importing a sibling test
# module is unusual, and deliberate: the fixture shapes (part JSON, session
# columns, the sentinel vocabulary) must be ONE definition, or the two suites
# drift and the TUI is tested against a host that no longer exists.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_ocut_sync as ts_sync                        # noqa: E402

TRACKER_TABS = ["tab-dashboard", "tab-tools", "tab-skills", "tab-agents",
                "tab-plugins", "tab-mcp", "tab-tokens", "tab-usage"]

DAY = 86_400_000


def now_ms():
    return int(datetime.now().timestamp() * 1000)


def build_host(path: Path):
    """One session with calls today and calls 40 days ago, and known tokens."""
    now = now_ms()
    old = now - 40 * DAY
    conn = ts_sync.make_host(path)
    ts_sync.add_session(conn, "ses_today", tokens=(0, 0, 0, 0, 0))
    ts_sync.add_session(conn, "ses_old", tokens=(0, 0, 0, 0, 0),
                        started=old, ended=old + 60_000)
    ts_sync.add_message(conn, "msg_today", "ses_today")
    ts_sync.add_message(conn, "msg_old", "ses_old")
    # today: 3 bash, 1 skill, 1 task, 2 mcp, 2 steps. One bash is an `error`:
    # without a non-completed row, calls == completed == errored+1 for every tool and
    # a panel that printed the wrong one of those columns would still match the
    # query. Keep this row when editing the fixture.
    for i in range(3):
        ts_sync.add_tool(conn, f"prt_bash_{i}", "msg_today", "ses_today",
                         tool="bash", ts=now - i * 60_000,
                         status="error" if i == 2 else "completed")
    ts_sync.add_tool(conn, "prt_skill", "msg_today", "ses_today", tool="skill",
                     state={"input": {"name": "triage"}}, ts=now - 5_000)
    ts_sync.add_tool(conn, "prt_task", "msg_today", "ses_today", tool="task",
                     state={"input": {"subagent_type": "explore"},
                            "metadata": {"sessionId": "ses_child"}}, ts=now - 4_000)
    for i, name in enumerate(("context7_query-docs", "context7_resolve-library-id")):
        ts_sync.add_tool(conn, f"prt_mcp_{i}", "msg_today", "ses_today", tool=name,
                         state={"input": {}}, ts=now - 3_000 + i)
    ts_sync.add_step(conn, "prt_step_a", "msg_today", "ses_today", ts=now - 2_000,
                     tokens={"total": 1000, "input": 400, "output": 100,
                             "reasoning": 50, "cache": {"read": 450, "write": 0}})
    ts_sync.add_step(conn, "prt_step_b", "msg_today", "ses_today", ts=now - 1_000,
                     tokens={"total": 2000, "input": 800, "output": 200,
                             "reasoning": 0, "cache": {"read": 1000, "write": 0}})
    # 40 days ago: 5 read calls and one step, so "Today" and "All time" differ
    for i in range(5):
        ts_sync.add_tool(conn, f"prt_read_{i}", "msg_old", "ses_old", tool="read",
                         ts=old + i * 1000)
    ts_sync.add_step(conn, "prt_step_old", "msg_old", "ses_old", ts=old + 60_000,
                     tokens={"total": 500, "input": 200, "output": 50,
                             "reasoning": 0, "cache": {"read": 250, "write": 0}})
    # update the rollups so the two granularities still agree
    conn.execute("UPDATE session SET tokens_input=1200, tokens_output=300,"
                 " tokens_reasoning=50, tokens_cache_read=1450 WHERE id='ses_today'")
    conn.execute("UPDATE session SET tokens_input=200, tokens_output=50,"
                 " tokens_reasoning=0, tokens_cache_read=250 WHERE id='ses_old'")
    conn.commit()
    conn.close()


def build_config(root: Path):
    cfg = root / "opencode.json"
    root.mkdir(parents=True, exist_ok=True)
    skill = root / "skills" / "personal" / "a-very-long-skill-name-for-clipping"
    skill.mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text(
        "---\nname: a-very-long-skill-name-for-clipping\n---\nbody\n")
    cfg.write_text(json.dumps({
        "$schema": "https://opencode.ai/config.json",
        "mcp": {"context7": {"url": "https://x"}},
        "plugin": ["@acme/demo-plugin@1.2.3", "./plugins/local-one.js"],
        "provider": {}, "command": {}}))
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
    """`now` pinned after the fixture database is written (seconds, as the app wants).

    Re-deriving the window from the current time is right for a long-running session
    and wrong for a test that compares two derivings: they must see the same `now`,
    or the assertion is measuring the wall clock instead of the wiring.

    The app stores the callable (`self._clock = clock or time.time`), so this yields
    a function, not a number — and the number is read once, here. `lambda: now_ms()`
    would be a pinned clock that re-reads the wall clock on every call, which is how
    this fixture first produced a one-millisecond "regression" in a bounds comparison.
    """
    pinned = now_ms() / 1000
    return lambda: pinned


def drive(app_setup, body, size=(150, 55)):
    """Run `body(pilot, app)` inside a live app; surfaces any exception as a failure."""
    async def _it():
        app = app_setup()
        async with app.run_test(size=size) as pilot:
            await pilot.pause(0.7)
            await body(pilot, app)
    asyncio.run(_it())


def headers(table):
    return [str(table.columns[k].label) for k in table.columns]


async def activate(app, pilot, pane: str) -> None:
    """Switch to a tab the way a person does — from the tab strip.

    ``#tab-tools`` is the TabPane, which is hidden until it is active, so clicking
    it hits nothing and the app quietly stays where it was. The strip's own id is
    ``--content-tab-<pane>``. The assert is the reason this is a helper rather than
    a line: only the active tab is re-queried on a window change, so a click that
    never landed would make every later reading a picture of a tab nobody opened.
    """
    from textual.widgets import TabbedContent
    await pilot.click(f"#--content-tab-{pane}")
    await pilot.pause(0.6)
    active = app.query_one(TabbedContent).active
    assert active == pane, f"the click did not switch tabs; still on {active}"


def cell(table, i, j):
    return str(table.get_cell_at((i, j)))


def first_column(table):
    return [cell(table, i, 0) for i in range(table.row_count)]


def value_labels(app, selector: str) -> list[str]:
    """The label half of every panel line under `selector`.

    A panel *title* ("Cache", "Cache breakdown") and a prose line ("usage unavailable:
    …") are `Static` widgets inside `.usage-panel` too, and neither carries the
    `[/dim]` marker that `_panel_line()` always emits. Harvesting every Static made
    furniture into metrics — the first version of this check "found" two cache numbers
    called Cache and Cache breakdown.
    """
    labels = []
    for w in app.query(f"{selector} Static"):
        text = str(getattr(w, "content", ""))
        if "[/dim]" not in text:
            continue
        labels.append(" ".join(text.split()).split("[/dim]")[0]
                      .replace("[dim]", "").strip())
    return labels


# --- the tab set -----------------------------------------------------------

def test_the_tracker_shows_cbuts_eight_tabs_in_order(tracker):
    async def body(pilot, app):
        from textual.widgets import Tab
        panes = [x.id for x in app.query("TabPane")]
        assert panes == TRACKER_TABS, panes
        # TabPane keeps no title of its own; the tab strip is what a person reads,
        # and its ids name the pane it switches to.
        tabs = [(x.id, str(x.label)) for x in app.query(Tab)]
        assert [label for _id, label in tabs] == [
            "Dashboard", "Tools", "Skills", "Agents", "Plugins", "MCP", "Tokens",
            "Usage"], tabs
        assert [_id.replace("--content-tab-", "") for _id, _l in tabs] == TRACKER_TABS
    drive(lambda: tui.TrackerApp(str(tracker)), body)


def test_every_tab_has_its_own_window_and_no_default_is_all_time(tracker):
    """The parity claim: one window per tab, not one window for the app.

    ``tab-plugins`` is deliberately absent from that dict — the inventory carries no
    timestamps, so a window on the Plugins list would filter nothing while looking
    like it did. It is asserted absent rather than silently missing.
    """
    async def body(pilot, a):
        assert a.tab_range == {
            "tab-dashboard": "1d", "tab-usage": "1d",
            "tab-tools": "7d", "tab-skills": "7d", "tab-agents": "7d",
            "tab-mcp": "7d", "tab-tokens": "7d",
        }, a.tab_range
        for pane, value in a.tab_range.items():
            if not isinstance(value, db.CustomWindow):
                assert value in db.USAGE_RANGES, (pane, value)
        a.tab_range["tab-tools"] = "all"
        assert a.tab_range["tab-skills"] != "all"
        assert a.tab_range["tab-agents"] != "all"
        assert a.tab_range["tab-dashboard"] != "all"
    drive(lambda: tui.TrackerApp(str(tracker)), body)


# --- numbers ----------------------------------------------------------------

def test_the_tools_tab_prints_exactly_its_own_window_of_the_tools_query(
        tracker, clock):
    """The tab's list is `q_tools` at the app's own bounds — names, order, counts.

    The bounds are read back from `_bounds_for` with the app's clock pinned (see the
    `clock` fixture), so neither side of the comparison re-reads the wall clock. What
    is pinned is the wiring: this pane, this query, these bounds, these columns — and
    `window_bounds` itself is measured directly in `test_ocut_windows.py`.

    ``7d → all → 7d``: the fixture gives `read` 0 calls in a week and 5 all-time, so
    the middle step must differ and the last must come back. A tab that latched onto
    the widest window it was ever shown would fail the third step.
    """
    conn = sqlite3.connect(tracker)
    conn.row_factory = sqlite3.Row          # as the app opens it

    async def body(pilot, app):
        await activate(app, pilot, "tab-tools")
        table = app.query_one("#t-tools", DataTable)
        assert headers(table) == list(tui.TOOLS_COLUMNS), headers(table)
        default = app.tab_range["tab-tools"]
        seen = {}
        for rng in (default, "all", default):
            app.tab_range["tab-tools"] = rng
            app._refresh_active_tab()
            await pilot.pause(0.6)
            start, end = app._bounds_for("tab-tools")
            assert (start, end) == db.window_bounds(rng, app._now_ms()), (rng,)
            rows = list(db.q_tools(conn, start_ts=start, end_ts=end))
            assert rows, f"nothing in the {rng} window"
            # The fixture must keep one non-completed call, or `calls` and `ok`
            # hold the same number for every row and a swapped column is invisible.
            assert any(r["calls"] != r["completed"] for r in rows), \
                "fixture has no failed call: the ok column cannot be told apart"
            assert first_column(table) == [r["tool_name"] for r in rows], (rng, rows)
            for i, r in enumerate(rows):
                # Column 1 is `state` now, so the counts start at 2. Read the
                # state back through the shared helper rather than pasting the
                # three labels here: this case is about the wiring, and a label
                # spelled twice in two files is a label that can disagree with
                # itself.
                assert [cell(table, i, j) for j in (2, 3, 4)] == \
                    [f"{r['calls']:,}", f"{r['completed']:,}", f"{r['errored']:,}"], \
                    (rng, r)
                assert cell(table, i, 1) == db.entity_state(
                    r["installed"], r["ever_used"], "tool"), (rng, r)
            seen[rng] = [cell(table, i, 2) for i in range(table.row_count)]
        assert seen[default] != seen["all"], (default, seen)
    drive(lambda: tui.TrackerApp(str(tracker), clock=clock), body)
    conn.close()


def test_the_window_changes_what_the_dashboard_counts(tracker, clock):
    """The same pane, two windows, two answers — the fixture spans two days.

    The expected counts are SELECTed at the bounds the app itself derived, so this
    pins "the dashboard applies its own window" rather than re-deriving midnight a
    second time in the test.
    """
    conn = sqlite3.connect(tracker)

    def count(start, end):
        return conn.execute("SELECT COUNT(*) FROM tool_calls"
                            " WHERE (? IS NULL OR ts >= ?) AND (? IS NULL OR ts <= ?)",
                            (start, start, end, end)).fetchone()[0]

    async def body(pilot, app):
        assert app.tab_range["tab-dashboard"] != "all"
        assert count(None, None) != count(*db.window_bounds("1d", app._now_ms())), \
            "the fixture must span two days for this to mean anything"
        seen = {}
        for rng in ("1d", "all", "1d"):
            app.tab_range["tab-dashboard"] = rng
            app._refresh_active_tab()
            await pilot.pause(0.6)
            start, end = app._bounds_for("tab-dashboard")
            # The pane's key goes through the one shared window function; nothing
            # here re-derives midnight, so a panel with its own private arithmetic
            # would disagree with `test_ocut_windows.py`'s direct readings.
            assert (start, end) == db.window_bounds(rng, app._now_ms()), (rng,)
            shown = int(str(app.query_one("#dash-kpi-tools").content)
                        .split()[-1].replace(",", ""))
            assert shown == count(start, end), (rng, shown, start, end)
            seen[rng] = shown
        assert seen["1d"] != seen["all"], seen
    drive(lambda: tui.TrackerApp(str(tracker), clock=clock), body)
    conn.close()


def test_token_panels_print_the_hosts_own_step_numbers(tracker, clock):
    """The Tokens panel sums `model_calls` — the per-step granularity, not the rollup.

    Summed at the bounds the app derived, so the panel cannot pass by accident while
    reading a different table or a different window.
    """
    conn = sqlite3.connect(tracker)

    async def body(pilot, app):
        app.tab_range["tab-dashboard"] = "all"
        app._refresh_active_tab()
        await pilot.pause(0.6)
        start, end = app._bounds_for("tab-dashboard")
        rows = conn.execute(
            "SELECT prompt_tokens, cache_read, total_tokens FROM model_calls"
            " WHERE (? IS NULL OR ts >= ?) AND (? IS NULL OR ts <= ?)",
            (start, start, end, end)).fetchall()
        assert rows, f"nothing in {start}..{end}"
        prompt = sum(r[0] for r in rows)
        read = sum(r[1] for r in rows)
        total = sum(r[2] for r in rows)
        got = {w.id: str(w.content) for w in app.query("#dash-tok-input, #dash-tok-api,"
                                                      " #dash-cache-hit,"
                                                      " #dash-tok-usage")}
        assert f"{prompt:,}" in got["dash-tok-input"], got
        assert f"{total:,}" in got["dash-tok-api"], got
        assert f"{read:,}" in got["dash-cache-hit"], got
        # prompt total = input + cache read, and it is NOT the host total
        assert f"{prompt + read:,}" in got["dash-tok-usage"], got
    drive(lambda: tui.TrackerApp(str(tracker), clock=clock), body)
    conn.close()


def test_no_panel_repeats_the_input_number_under_a_second_label(tracker):
    """The one duplicate the ported layout really carried, pinned as a label claim.

    CodeBuddy's cache panel had three numbers (hit / miss / write); OpenCode has two
    (read / write) and its `input` IS the miss. Printing it again as "Uncached in"
    made one column look like two measurements of different things — and a general
    "no two panels may show the same number" rule is not an invariant, because two
    different metrics legitimately coincide (every call completing is also every
    call not failing). So this names the labels instead of policing the values.
    """
    async def body(pilot, app):
        for pane, cache_panel in (("tab-dashboard", "panel-dash-cache"),
                                  ("tab-usage", "panel-cache")):
            await activate(app, pilot, pane)
            app.tab_range[pane] = "all"
            app.refresh_data()
            await pilot.pause(0.5)
            labels = value_labels(app, f"#{pane} .usage-panel")
            assert len(labels) >= 9, f"{pane} has no panel lines to check: {labels}"
            assert not any("Uncached" in x for x in labels), (pane, labels)
            cache = value_labels(app, f"#{cache_panel}")
            assert sorted(cache) == ["Cache read", "Cache write", "Hit rate"], \
                (pane, cache)
    drive(lambda: tui.TrackerApp(str(tracker)), body)


# --- the absent metrics ----------------------------------------------------

def test_the_plugins_tab_shows_a_uses_column(tracker):
    """`uses` is real now — the hook writes it, so 0 means "nothing recorded".

    Pinned as the exact header list, so changing the column set is a decision with a
    red test in front of it rather than a quiet edit. The fixture runs `sync` with no
    hook sidecar, so both plugins show 0 uses and no last-used time — the honest
    reading of "the hook recorded nothing", never "not used". `state` sits second
    because the column plan drops the tail first: a nine-wide Plugins table loses its
    last four columns long before it loses "is this still here".
    """
    async def body(pilot, app):
        await activate(app, pilot, "tab-plugins")
        table = app.query_one("#t-plugins", DataTable)
        assert headers(table) == ["plugin", "state", "version", "uses",
                                  "last used", "skills", "agents", "cmds",
                                  "declared as"]
        assert table.row_count == 2, first_column(table)
        rows = {cell(table, i, 0): (cell(table, i, 1), cell(table, i, 2),
                                    cell(table, i, 3), cell(table, i, 4),
                                    cell(table, i, 5), cell(table, i, 6),
                                    cell(table, i, 7), cell(table, i, 8))
                for i in range(table.row_count)}
        assert rows["demo-plugin"] == ("installed", "1.2.3", "0", "-",
                                       "0", "0", "0",
                                       "@acme/demo-plugin@1.2.3"), rows
        assert rows["local-one"] == ("installed", "-", "0", "-",
                                     "0", "0", "0", "./plugins/local-one.js"), rows
    drive(lambda: tui.TrackerApp(str(tracker)), body)


# --- the state column ------------------------------------------------------

ENTITY_TABS = [("tab-tools", "t-tools", tui.TOOLS_COLUMNS),
               ("tab-skills", "t-skills", tui.SKILLS_COLUMNS),
               ("tab-agents", "t-agents", tui.AGENTS_COLUMNS),
               ("tab-plugins", "t-plugins", tui.PLUGINS_COLUMNS),
               ("tab-mcp", "t-mcp", tui.MCP_COLUMNS)]

STATES = {db.ENTITY_STATE_USED, db.ENTITY_STATE_INSTALLED, db.ENTITY_STATE_REMOVED,
          db.ENTITY_STATE_UNREPORTED}


def test_every_entity_tab_prints_a_state_next_to_the_name(tracker):
    """`state` is column 1 on every entity tab — or column 2 on MCP, which is keyed
    by (server, tool) and cannot put a label between the two halves of its own key.

    Not column last: the column plan keeps the longest *prefix* that fits, so the
    last column is the first one a narrow terminal loses, and "is this still here"
    is the one thing on the row that the count beside it cannot answer.
    """
    for columns in (tui.TOOLS_COLUMNS, tui.SKILLS_COLUMNS, tui.AGENTS_COLUMNS,
                    tui.PLUGINS_COLUMNS):
        assert columns[1] == db.ENTITY_STATE_COLUMN, columns
    assert tui.MCP_COLUMNS[:2] == ("server", "tool"), tui.MCP_COLUMNS
    assert tui.MCP_COLUMNS[2] == db.ENTITY_STATE_COLUMN, tui.MCP_COLUMNS


def test_each_entity_tab_renders_a_state_label_from_the_shared_helper(tracker):
    """Header from the tuple, cell from `db.entity_state` — and one of the three
    labels, never anything else and never a blank.

    The empty-state row spans the label set with "-" padding, so it is skipped by
    name rather than by row index: its state cell is a filler, not a claim.
    """
    async def body(pilot, app):
        for pane, table_id, columns in ENTITY_TABS:
            await activate(app, pilot, pane)
            table = app.query_one(f"#{table_id}", DataTable)
            assert headers(table) == list(columns), (pane, headers(table))
            at = list(columns).index(db.ENTITY_STATE_COLUMN)
            for i in range(table.row_count):
                value = str(table.get_cell_at((i, at)))
                if value == "-":
                    continue          # empty-state filler, not an entity
                assert value in STATES, (pane, i, value)
    drive(lambda: tui.TrackerApp(str(tracker)), body)


def test_a_kind_we_cannot_census_says_so_instead_of_calling_it_removed(tracker):
    """The bug the fourth state exists for: 39 of 40 rows on the Tools tab read
    `removed`, because OpenCode's builtin tool ids live in its compiled registry
    and a passive reader cannot enumerate them. A label is a claim, and "you
    deleted this" is a claim this database cannot support about a builtin. The
    two censored kinds have to read `not reported` and stay dim -- the same
    visual weight as `removed`, because neither is something the reader can act
    on, and a louder row would read as a verdict.

    `Textual` has no per-row style hook on `DataTable` -- `add_row` takes cells,
    and the only row-level classes are header/fixed/zebra -- so the dimming
    covers this one cell and the rest of the row stays at full contrast. That is
    a deliberate limit, not a missing one, and asserting on the *cell* rather
    than the row is what makes the limit visible.
    """
    conn = sqlite3.connect(tracker)
    conn.row_factory = sqlite3.Row
    tools = [r for r in db.q_tools(conn) if not r["installed"]]
    censored = [k for k, ok in db.ENTITY_CENSUS.items() if not ok]
    alive = [r for r in db.q_skills(conn) if r["installed"]]
    gone = [r for r in db.q_skills(conn) if not r["installed"]]
    conn.close()
    assert tools and alive, "the fixture needs an uncatalogued tool and an installed skill"
    assert "tool" in censored, censored
    assert all(r["installed"] for r in alive), alive

    async def body(pilot, app):
        await activate(app, pilot, "tab-tools")
        table = app.query_one("#t-tools", DataTable)
        row = first_column(table).index(tools[0]["tool_name"])
        cell_value = table.get_cell_at((row, 1))
        assert str(cell_value) == db.ENTITY_STATE_UNREPORTED, tools[0]["tool_name"]
        assert isinstance(cell_value, tui.Text), type(cell_value)
        assert cell_value.style == "dim", cell_value.style
        # Not `str` on purpose: a plain string would also satisfy
        # `str(...) == "not reported"`, so without the type assertion the dimming
        # could go away unnoticed.
        assert not isinstance(cell_value, str)

        # The tab next door can be censused, so "gone" is a claim it can make --
        # and the inventory row that supports it is what the fixture removed.
        await activate(app, pilot, "tab-skills")
        skills = app.query_one("#t-skills", DataTable)
        names = first_column(skills)
        if alive:
            row = names.index(alive[0]["skill"])
            assert skills.get_cell_at((row, 1)) == db.ENTITY_STATE_INSTALLED, \
                alive[0]["skill"]
        if gone:
            row = names.index(gone[0]["skill"])
            assert str(skills.get_cell_at((row, 1))) == db.ENTITY_STATE_REMOVED, \
                gone[0]["skill"]
    drive(lambda: tui.TrackerApp(str(tracker)), body)


def test_a_styled_cell_survives_the_name_cap():
    """`_cap_cell` used to be `(self, text: str) -> str` and measured with `len()`,
    so a renderable could not pass through it with its style intact. At `name_cap`
    below `len("installed")` the dimmed label is cut — and a cut that drops the style
    is the same silent lie as a silent column drop.
    """
    class _Host(tui.WideTableMixin):
        name_cap = 8

    host = _Host()
    clipped = host._cap_cell(tui.Text("installed", style="dim"))
    assert str(clipped) == "install…", str(clipped)
    assert isinstance(clipped, tui.Text) and clipped.style == "dim"
    # The same cut on an unstyled cell stays a plain string: the cap decides the
    # text, the incoming type decides the type.
    plain = host._cap_cell("installed")
    assert plain == "install…" and isinstance(plain, str)


# --- clipping and the row key ----------------------------------------------

def test_a_clipped_cell_still_keys_on_the_full_name(tracker):
    """`name_cap` is a display limit, never a row identity.

    A skill long enough to be clipped is real here (`basic-memory_*`-shaped names
    exceed 28 characters on any machine that uses MCP), and the drill-down must
    still open the right entity. Asserted against `db.q_skills` — the very query
    the panel fills from — so the key is compared with the name the database holds,
    not with another rendering of the cell.
    """
    conn = sqlite3.connect(tracker)
    conn.row_factory = sqlite3.Row      # as the app opens it; q_skills names columns
    full = {r["skill"] for r in db.q_skills(conn)}
    conn.close()

    async def body(pilot, app):
        await activate(app, pilot, "tab-skills")
        table = app.query_one("#t-skills", DataTable)
        names = first_column(table)
        keys = [table.coordinate_to_cell_key((i, 0)).row_key.value
                for i in range(table.row_count)]
        keyed = [k.split("\t", 1)[1] for k in keys]
        # The precondition of this test, asserted rather than assumed: the terminal
        # is narrow enough that at least one name really was cut. When everything
        # fits, `_fill_wide` deliberately skips the cap and cells equal keys, so a
        # pass at that width would prove nothing about clipping.
        assert any(len(k) > len(c) for k, c in zip(keyed, names)), (keyed, names)
        assert all(k in full for k in keyed), [k for k in keyed if k not in full]
        # The cell is exactly the head of the key that fits — the display rule —
        # while the key itself stays the whole name the database holds. A skill
        # never contains "/", so this column is capped from the right by `_cap_cell`.
        assert names == [k if len(k) <= app.name_cap
                         else k[:app.name_cap - 1] + "…" for k in keyed], \
            (names, keyed)
        assert any(len(k) > app.name_cap for k in keyed), keyed
    drive(lambda: tui.TrackerApp(str(tracker), config={**db.DEFAULTS,
                                                       "name_cap": 12}), body,
          size=(60, 40))


# --- the network promise ---------------------------------------------------

def test_no_http_is_reachable_while_the_tui_paints(tracker, monkeypatch):
    """Same armed-door guard the previous TUI carried, re-pointed at this one.

    The claim is that the interface is local: nothing on the repaint path may open
    a socket. Three doors are armed, each is proven to answer once before the clean
    run is believed, and `socket.socket` itself is deliberately NOT patched —
    asyncio's selector loop builds its own self-pipe through it, so patching it
    makes the harness fail rather than the app, which is a different claim.
    """
    import http
    import socket
    import urllib.request

    hits = []

    def _blocked(name):
        def _fail(*a, **kw):
            hits.append(name)
            raise AssertionError(f"{name} called from the TUI")
        return _fail

    doors = (("urllib.request.urlopen", urllib.request, "urlopen",
              lambda: urllib.request.urlopen("http://127.0.0.1:1/armed")),
             ("http.client.HTTPConnection", http.client, "HTTPConnection",
              lambda: http.client.HTTPConnection("127.0.0.1", 1)),
             ("socket.create_connection", socket, "create_connection",
              lambda: socket.create_connection(("127.0.0.1", 1))))
    for _name, module, attr, _call in doors:
        monkeypatch.setattr(module, attr, _blocked(_name))
    for name, _m, _a, call in doors:          # arming proof first
        try:
            call()
        except AssertionError:
            pass
        assert hits == [name], f"{name} is not armed; the stub never answered: {hits}"
        hits.clear()

    async def body(pilot, app):
        for pane in TRACKER_TABS:
            await pilot.click(f"#{pane}")
            await pilot.pause(0.45)
        for key in ("s", "r"):
            await pilot.press(key)
            await pilot.pause(0.45)
        app.refresh_data()
        await pilot.pause(0.4)
    drive(lambda: tui.TrackerApp(str(tracker)), body)
    assert hits == [], hits


def test_the_sync_runs_in_a_daemon_thread_so_quitting_never_waits(tmp_path):
    """`q` must never block on an in-flight sync.

    Textual runs a thread worker on the event loop's default executor
    (`textual/worker.py`: ``loop.run_in_executor(None, …)``), and ``asyncio.run()``
    joins that executor before ``app.run()`` returns. So a sync still running when
    the user quits blocks the exit for as long as the sync takes — measured on this
    machine: ``r`` then ``q`` hung the app, and Ctrl+C produced a
    ``KeyboardInterrupt`` traceback out of ``shutdown_default_executor``. The sync
    therefore runs in a daemon thread, which the interpreter never joins.
    """
    host = tmp_path / "opencode.db"
    build_host(host)
    cfg = build_config(tmp_path / "cfg")
    out = tmp_path / "usage.db"
    db.load_sync().run(db_path=out, host_db=host, host_config=cfg, quiet=True)
    seen = {}

    class _RecordingSync:
        def run(self, **kwargs):
            t = threading.current_thread()
            seen["name"], seen["daemon"] = t.name, t.daemon
            return {"ok": True, "parts_read": 0}

    async def body(pilot, app):
        app.sync_mod = _RecordingSync()
        await pilot.press("r")
        for _ in range(400):
            if "name" in seen:
                break
            await asyncio.sleep(0.01)

    drive(lambda: tui.TrackerApp(str(out)), body)
    assert seen.get("name") == "ocut-sync", (
        "the sync did not run in the named daemon thread; it may be back on "
        f"Textual's default executor, which blocks quit: {seen!r}")
    assert seen.get("daemon") is True, (
        "the sync thread must be a daemon, or quitting waits for it")
