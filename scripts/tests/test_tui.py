"""Headless smoke tests for the Textual app (requires the skillt venv).

Runs the real app against a temp DB and asserts the plan's acceptance criteria:
the skills directory is listed, sorting works, the detail screen opens, the
confirm modal does not write on cancel, and quitting is clean. Anything that
touches SKILLS_DIR uses the `temp_skills` fixture — never the real
~/.config/opencode/skills, which changes on its own and makes the suite fail
for reasons unrelated to the code.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

pytest.importorskip("textual")

from conftest import load_module  # noqa: E402

st = load_module("skill-tui.py", "skill_tui")

from textual.widgets import DataTable, Static  # noqa: E402


def static_text(widget) -> str:
    """Plain text of a Static (Textual 8.x exposes it as `.content`)."""
    try:
        return str(widget.content)
    except Exception:  # noqa: BLE001
        return ""

SkillTUI = st.get_app_class()


def _run(coro):
    return asyncio.run(coro)


def test_tui_loads_skills_and_sorts(empty_db, temp_skills):
    async def _run_it():
        app = SkillTUI(db_path=empty_db, no_sync=False)
        async with app.run_test() as pilot:
            await pilot.pause()
            conn = app.conn
            assert conn is not None
            assert conn.execute("SELECT COUNT(*) FROM skills").fetchone()[0] == 5

            table = app.screen.query_one("#skills-table", DataTable)
            assert table.row_count == 5, "all 5 fixture skills must be listed"

            # sort cycles through all four modes without error
            for _ in range(4):
                await pilot.press("s")
                await pilot.pause()
            assert app.sort_mode == "count"

            # search filters
            await pilot.press("slash")
            await pilot.press("b", "r", "a", "i", "n")
            await pilot.pause()
            filtered = app.screen.query_one("#skills-table", DataTable).row_count
            assert 0 < filtered < 5, f"search should narrow the list, got {filtered}"
            await pilot.press("escape")
            await pilot.pause()
            assert app.screen.query_one("#skills-table", DataTable).row_count == 5

            await pilot.press("r")
            await pilot.pause()

    _run(_run_it())


def test_tui_dashboard_and_recent_tabs(seeded_db):
    async def _run_it():
        app = SkillTUI(db_path=seeded_db, no_sync=True)
        async with app.run_test() as pilot:
            await pilot.pause()
            top = app.screen.query_one("#dash-top", DataTable)
            assert top.row_count >= 1
            rec = app.screen.query_one("#recent-table", DataTable)
            assert rec.row_count >= 1

    _run(_run_it())


def test_tui_detail_screen_opens(seeded_db):
    async def _run_it():
        app = SkillTUI(db_path=seeded_db, no_sync=True)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("tab")          # -> Skills tab
            await pilot.pause()
            await pilot.press("j")
            await pilot.press("enter")
            await pilot.pause()
            # detail screen replaced the main screen
            assert app.screen.query("#detail-history")
            await pilot.press("escape")
            await pilot.pause()
            assert app.screen.query("#skills-table")

    _run(_run_it())


def test_tui_confirm_cancel_does_not_write(seeded_db):
    async def _run_it():
        app = SkillTUI(db_path=seeded_db, no_sync=True)
        async with app.run_test() as pilot:
            await pilot.pause()
            before = app.conn.execute("SELECT COUNT(*) FROM skill_usage").fetchone()[0]

            await pilot.press("tab")          # Skills tab
            await pilot.pause()
            await pilot.press("j")
            await pilot.press("d")            # delete -> confirm modal
            await pilot.pause()
            assert app.screen.query("#confirm-box"), "confirm modal must appear"

            await pilot.press("escape")       # cancel
            await pilot.pause()
            after = app.conn.execute("SELECT COUNT(*) FROM skill_usage").fetchone()[0]
            assert after == before, "cancelling must not delete anything"

    _run(_run_it())


def test_tui_quits_cleanly(seeded_db):
    async def _run_it():
        app = SkillTUI(db_path=seeded_db, no_sync=True)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("escape")       # release focus from any input
            await pilot.press("q")
        assert not app.is_running

    _run(_run_it())


def test_tui_data_page_backup_and_export(seeded_db, tmp_path):
    """The Data page's write actions must actually produce files (0600)."""
    import glob
    import os
    import stat

    async def _run_it():
        app = SkillTUI(db_path=seeded_db, no_sync=True)
        async with app.run_test() as pilot:
            await pilot.pause()
            for _ in range(7):          # Dashboard -> Skills -> MCP -> Plugins -> Recent -> Cats -> Data
                await pilot.press("tab")
                await pilot.pause()

            await pilot.press("escape")
            await pilot.press("b")      # backup
            await pilot.pause()
            # Backups land in BACKUP_DIR (M19), exports stay beside the DB.
            import skill_db as db
            backups = glob.glob(os.path.join(db.BACKUP_DIR, "skill-usage-backup-*.db"))
            assert backups, "backup button must create a file"
            assert stat.S_IMODE(os.stat(backups[0]).st_mode) == 0o600

            await pilot.press("e")      # export JSON
            await pilot.pause()
            exports = glob.glob(os.path.join(os.path.dirname(seeded_db), "skill-usage-export-*.json"))
            assert exports, "export button must create a file"
            assert stat.S_IMODE(os.stat(exports[0]).st_mode) == 0o600

    _run(_run_it())


def test_tui_non_tty_guard(monkeypatch, empty_db):
    """Without a TTY, main() must refuse to start the TUI and exit 2."""
    monkeypatch.setattr("sys.stdout.isatty", lambda: False)
    assert st.main(["--db", empty_db]) == 2


def test_tui_guard_requires_all_three_streams(monkeypatch, empty_db):
    """H6: stdout may be a tty while stdin is not (`skillt < /dev/null`)."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.setattr("sys.stderr.isatty", lambda: True)
    assert st.main(["--db", empty_db]) == 2, "stdin must be a tty too"

    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.setattr("sys.stderr.isatty", lambda: False)
    assert st.main(["--db", empty_db]) == 2, "stderr must be a tty too"


def test_delete_requires_skills_tab(seeded_db):
    """C1: `d` off the Skills tab must not resolve to a hidden table row."""
    async def _run_it():
        app = SkillTUI(db_path=seeded_db, no_sync=True)
        async with app.run_test() as pilot:
            await pilot.pause()
            ms = app.screen
            before = app.conn.execute("SELECT COUNT(*) FROM skill_usage").fetchone()[0]

            # initial tab is Dashboard: nothing is selected, no modal appears
            assert ms._selected_skill() is None
            await pilot.press("d")
            await pilot.pause()
            assert not app.screen.query("#confirm-box"), "no modal off the Skills tab"
            assert app.conn.execute("SELECT COUNT(*) FROM skill_usage").fetchone()[0] == before

            # on the Skills tab but without focus, still refused
            from textual.widgets import TabbedContent
            ms.query_one(TabbedContent).active = "tab-skills"
            await pilot.pause()
            assert ms._selected_skill() is None

            # focused Skills table resolves the row under the cursor
            ms.query_one("#skills-table").focus()
            await pilot.pause()
            assert ms._selected_skill() is not None

    _run(_run_it())


# ---------------------------------------------------------------------------
# A2 regression: the Dashboard cards must be labelled and tall enough to
# show both the number and the label. Before the fix, `padding: 1` left only one
# content row, so the labels were silently clipped.
# ---------------------------------------------------------------------------
CARD_LABELS = {
    "#card-skills": "Skills",
    "#card-usage": "Skill calls",
    "#card-mcp": "MCP calls",
    "#card-plugin": "Plugin calls",
    "#card-today": "Today",
    "#card-rate": "Skill success",
}


def test_tui_dashboard_cards_have_labels_and_height(seeded_db):
    from textual.widgets import Static

    async def _run_it():
        app = SkillTUI(db_path=seeded_db, no_sync=True)
        async with app.run_test() as pilot:
            await pilot.pause()
            for wid, label in CARD_LABELS.items():
                w = app.screen.query_one(wid, Static)
                text = str(w.content)
                assert label in text, f"{wid} must be labelled '{label}', got {text!r}"
                assert w.size.height >= 2, (
                    f"{wid} content height is {w.size.height}; the label row is clipped"
                )

    _run(_run_it())


def test_tui_dashboard_legend_present(seeded_db):
    """The legend breaks down Today and the observation window.

    It no longer spells out "Skill calls / MCP calls / Plugin calls =
    invocation counts" — the card labels carry that now.
    """
    from textual.widgets import Static

    async def _run_it():
        app = SkillTUI(db_path=seeded_db, no_sync=True)
        async with app.run_test() as pilot:
            await pilot.pause()
            legend = str(app.screen.query_one("#cards-legend", Static).content)
            assert "Today (all)" in legend, legend
            for word in ("skill", "mcp", "plugin"):
                assert word in legend, legend
            assert "observed" in legend and "session(s)" in legend, legend

    _run(_run_it())


def test_tui_no_usage_hint_shown_when_empty(tmp_path):
    """With skills but zero usage, the sort label must explain the flat order."""
    import sqlite3
    import skill_db as db

    path = str(tmp_path / "nousage.db")
    conn = sqlite3.connect(path)
    conn.executescript(db.SCHEMA_SQL)
    for n in ("alpha", "beta", "gamma"):
        conn.execute(
            "INSERT INTO skills (name, category, path, description) VALUES (?,?,?,?)",
            (n, "personal-skills", f"/s/{n}", "d"),
        )
    conn.commit()
    conn.close()

    from textual.widgets import Static

    async def _run_it():
        app = SkillTUI(db_path=path, no_sync=True)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.has_usage is False
            label = str(app.screen.query_one("#sort-label", Static).content)
            assert "no usage data yet" in label, label

    _run(_run_it())


# ---------------------------------------------------------------------------
# A3/A4 regression: with real data the sort order must actually change, and the
# cursor must stay on the same skill across a re-sort.
# ---------------------------------------------------------------------------
def _skill_order(app):
    table = app.screen.query_one("#skills-table", DataTable)
    return [table.coordinate_to_cell_key((i, 0)).row_key.value for i in range(table.row_count)]


def test_tui_sort_order_changes_with_data(seeded_db):
    async def _run_it():
        app = SkillTUI(db_path=seeded_db, no_sync=True)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("tab")          # Skills tab
            await pilot.pause()
            assert app.sort_mode == "count"
            orders = [_skill_order(app)]
            for _ in range(3):
                await pilot.press("s")
                await pilot.pause()
                orders.append(_skill_order(app))
            assert len({tuple(o) for o in orders}) > 1, (
                "sorting must change the order when usage data exists"
            )

    _run(_run_it())


def test_tui_sort_keeps_cursor_on_same_skill(seeded_db):
    async def _run_it():
        app = SkillTUI(db_path=seeded_db, no_sync=True)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("tab")
            await pilot.pause()
            table = app.screen.query_one("#skills-table", DataTable)
            table.focus()
            table.move_cursor(row=1)
            await pilot.pause()
            pinned = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value

            await pilot.press("s")
            await pilot.pause()
            after = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
            assert after == pinned, "the cursor must follow the selected skill across a re-sort"

    _run(_run_it())


# ---------------------------------------------------------------------------
# The search Input steals plain letters, so ctrl+s / ctrl+r must always work.
# ---------------------------------------------------------------------------
def test_tui_ctrl_shortcuts_work_while_search_focused(seeded_db):
    from textual.widgets import Input

    async def _run_it():
        app = SkillTUI(db_path=seeded_db, no_sync=True)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("slash")        # focus the search box
            await pilot.pause()
            search = app.screen.query_one("#search", Input)

            await pilot.press("a")
            await pilot.pause()
            assert search.value == "a", "plain letters go into the search box"

            before = app.sort_mode
            await pilot.press("ctrl+s")
            await pilot.pause()
            assert app.sort_mode != before, "ctrl+s must sort even with search focused"
            assert search.value == "a", "ctrl+s must not be typed into the search box"

            await pilot.press("ctrl+r")
            await pilot.pause()
            assert search.value == "a", "ctrl+r must not be typed into the search box"

    _run(_run_it())


# ---------------------------------------------------------------------------
# A5 regression: the Data page has no delete affordance.
# ---------------------------------------------------------------------------
def test_tui_data_page_has_no_delete_button(seeded_db):
    from textual.widgets import Static

    async def _run_it():
        app = SkillTUI(db_path=seeded_db, no_sync=True)
        async with app.run_test() as pilot:
            await pilot.pause()
            for _ in range(7):                # -> Data tab
                await pilot.press("tab")
                await pilot.pause()
            assert not app.screen.query("#btn-delete"), "the Data page delete button must be gone"
            for bid in ("#btn-backup", "#btn-vacuum", "#btn-export", "#btn-health", "#btn-clear"):
                assert app.screen.query(bid), f"expected button {bid}"
            help_text = str(app.screen.query_one("#data-help", Static).content)
            assert "Delete the selected skill" not in help_text, help_text

    _run(_run_it())


def test_tui_backup_and_export_twice_are_unique(seeded_db):
    """Two backups / exports in the same second must not overwrite each other."""
    import glob
    import os

    async def _run_it():
        app = SkillTUI(db_path=seeded_db, no_sync=True)
        async with app.run_test() as pilot:
            await pilot.pause()
            for _ in range(7):
                await pilot.press("tab")
                await pilot.pause()
            await pilot.press("escape")
            for _ in range(2):
                await pilot.press("b")
                await pilot.pause()
            for _ in range(2):
                await pilot.press("e")
                await pilot.pause()

            import skill_db as db
            d = os.path.dirname(seeded_db)
            backups = glob.glob(os.path.join(db.BACKUP_DIR, "skill-usage-backup-*.db"))
            exports = glob.glob(os.path.join(d, "skill-usage-export-*.json"))
            assert len(backups) == 2, f"expected 2 distinct backups, got {backups}"
            assert len(exports) == 2, f"expected 2 distinct exports, got {exports}"

    _run(_run_it())


def test_duplicate_skill_names_do_not_crash(tmp_path):
    """H7: two SKILL.md with the same name must render (keyed by path)."""
    import sqlite3
    import skill_db as db

    path = str(tmp_path / "dup.db")
    conn = sqlite3.connect(path)
    conn.executescript(db.SCHEMA_SQL)
    conn.execute(
        "INSERT INTO skills (name, category, path, description) VALUES ('dup','personal-skills','/p/one','a')"
    )
    conn.execute(
        "INSERT INTO skills (name, category, path, description) VALUES ('dup','personal-skills','/p/two','b')"
    )
    conn.commit()
    conn.close()

    async def _run_it():
        app = SkillTUI(db_path=path, no_sync=True)
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.screen.query_one("#skills-table", DataTable)
            assert table.row_count == 2, "both duplicate-named skills must render"
            assert len(app.path_to_name) == 2
            assert set(app.path_to_name.values()) == {"dup"}

    _run(_run_it())



def test_tui_tab_order_is_pinned(seeded_db):
    """The tab contract: Data stays LAST; skills-adjacent tabs group together.

    Order: Dashboard, Skills, MCP, Plugins, Advisor, Recent, Categories, Data.
    Seven `tab` presses from Dashboard land on Data — Advisor sits with the
    "who acted" group and Data stays last, because that is where the destructive
    keys are.
    """
    from textual.widgets import TabPane

    async def _run_it():
        app = SkillTUI(db_path=seeded_db, no_sync=True)
        async with app.run_test() as pilot:
            await pilot.pause()
            ids = [p.id for p in app.screen.query(TabPane)]
            assert ids == [
                "tab-dash", "tab-skills", "tab-mcp", "tab-plugins", "tab-advisor",
                "tab-recent", "tab-cats", "tab-data",
            ], ids

            # Two presses land on MCP, three on Plugins (grouped after Skills).
            for _ in range(2):
                await pilot.press("tab")
                await pilot.pause()
            assert app.screen.query_one("TabbedContent").active == "tab-mcp"

            await pilot.press("tab")
            await pilot.pause()
            assert app.screen.query_one("TabbedContent").active == "tab-plugins"

            # Four more land on Data, which is what the Data tests rely on.
            for _ in range(4):
                await pilot.press("tab")
                await pilot.pause()
            assert app.screen.query_one("TabbedContent").active == "tab-data"

    _run(_run_it())


def test_tui_mcp_tab_renders_rows(seeded_mcp_db):
    async def _run_it():
        app = SkillTUI(db_path=seeded_mcp_db, no_sync=True)
        async with app.run_test() as pilot:
            await pilot.pause()

            dash_mcp = app.screen.query_one("#dash-mcp", DataTable)
            assert dash_mcp.row_count == 5, "dashboard Top MCP mirrors the tab"

            table = app.screen.query_one("#mcp-table", DataTable)
            assert table.row_count == 5, "one row per (server, tool) pair"

            # The table is reachable by tabbing and supports sorting.
            for _ in range(2):
                await pilot.press("tab")
                await pilot.pause()
            assert app.screen.query_one("TabbedContent").active == "tab-mcp"
            await pilot.press("s")
            await pilot.pause()
            assert table.row_count == 5

    _run(_run_it())


def test_tui_mcp_tab_is_empty_without_mcp_data(seeded_db):
    """A skills-only DB must render empty MCP tables, not an error."""
    async def _run_it():
        app = SkillTUI(db_path=seeded_db, no_sync=True)
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.screen.query_one("#mcp-table", DataTable)
            assert table.row_count == 0
            dash_mcp = app.screen.query_one("#dash-mcp", DataTable)
            assert dash_mcp.row_count == 0, "dashboard Top MCP stays empty too"
            assert dash_mcp.columns, "headers render even with no rows"

    _run(_run_it())


def test_tui_dashboard_has_an_mcp_card(seeded_mcp_db):
    """MCP sits in the same card row as Skills/Uses, plus a Top-MCP table."""
    async def _run_it():
        app = SkillTUI(db_path=seeded_mcp_db, no_sync=True)
        async with app.run_test() as pilot:
            await pilot.pause()
            text = static_text(app.screen.query_one("#card-mcp", Static))
            assert "8" in text, f"MCP count missing from the card: {text!r}"
            # The per-tool detail lives in the dashboard table now (the old
            # one-line summary was removed as redundant with the cards).
            assert not app.screen.query("#mcp-summary"), "the summary line is gone"
            assert app.screen.query_one("#dash-mcp", DataTable).row_count == 5

    _run(_run_it())


# ---------------------------------------------------------------------------
# New-feature coverage: unified timeline, MCP/Plugin detail pages, independent
# per-table sort/filter, dashboard plugin card, status/rate helpers.
# ---------------------------------------------------------------------------
def test_unified_recent_rows_merges_all_kinds(tmp_path):
    """One skill + one MCP + one plugin row merge newest-first."""
    import sqlite3
    import skill_db as db

    path = str(tmp_path / "unified.db")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA_SQL)
    conn.execute(
        "INSERT INTO skills (name, category, path, description) VALUES ('s','personal-skills','/s','d')"
    )
    conn.execute(
        "INSERT INTO skill_usage (skill_name, session_id, project_path, trigger_type,"
        " status, timestamp, duration_ms) VALUES ('s','a','/p','tool_call','success',"
        " strftime('%Y-%m-%dT%H:%M:%fZ','now','-3 days'), 10)"
    )
    conn.execute(
        "INSERT INTO mcp_usage (server_name, tool_name, session_id, project_path,"
        " trigger_type, status, timestamp, duration_ms) VALUES ('srv','tool','b','/p',"
        " 'tool_call','error', strftime('%Y-%m-%dT%H:%M:%fZ','now','-1 day'), 20)"
    )
    conn.execute(
        "INSERT INTO plugin_usage (plugin_name, kind, item_name, session_id,"
        " project_path, trigger_type, status, timestamp, duration_ms)"
        " VALUES ('plug','tool','it','c','/p','tool_call','denied',"
        " strftime('%Y-%m-%dT%H:%M:%fZ','now','-2 days'), 30)"
    )
    conn.commit()

    rows = db.unified_recent_rows(conn, 100)
    conn.close()
    assert [r["kind"] for r in rows] == ["mcp", "plugin", "skill"], rows
    assert rows[1]["status"] == "denied"
    assert rows[0]["name"] == "srv.tool"
    assert rows[2]["name"] == "s"

    # limit is honoured
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    assert len(db.unified_recent_rows(conn, 2)) == 2
    conn.close()


def test_unified_recent_rows_bounds_each_source(tmp_path):
    """Bounding each source before merging still yields the global newest-N.

    A source with many old rows must not starve a source with one new row: the
    global top-N of a union equals the merge of each source's own top-N.
    """
    import sqlite3
    import skill_db as db

    path = str(tmp_path / "bounded.db")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA_SQL)
    conn.execute(
        "INSERT INTO skills (name, category, path, description) VALUES ('s','personal-skills','/s','d')"
    )
    # 4 old skill rows, each one day older than the last.
    for days in (4, 5, 6, 7):
        conn.execute(
            "INSERT INTO skill_usage (skill_name, session_id, project_path,"
            " trigger_type, status, timestamp, duration_ms) VALUES ('s','a','/p',"
            " 'tool_call','success', strftime('%Y-%m-%dT%H:%M:%fZ','now', ?), 10)",
            (f"-{days} days",),
        )
    # A single, much newer MCP row.
    conn.execute(
        "INSERT INTO mcp_usage (server_name, tool_name, session_id, project_path,"
        " trigger_type, status, timestamp, duration_ms) VALUES ('srv','tool','b','/p',"
        " 'tool_call','success', strftime('%Y-%m-%dT%H:%M:%fZ','now','-1 day'), 20)"
    )
    conn.commit()

    assert [r["kind"] for r in db.unified_recent_rows(conn, 1)] == ["mcp"]
    rows = db.unified_recent_rows(conn, 3)
    conn.close()
    # Newest is the MCP row, then the two most recent skill rows (-4d, -5d).
    assert [r["kind"] for r in rows] == ["mcp", "skill", "skill"], rows
    assert rows[1]["timestamp"] > rows[2]["timestamp"]


def test_unified_recent_rows_skills_only_db(seeded_db):
    """A DB without MCP/plugin rows degrades to skill rows only."""
    import sqlite3
    import skill_db as db

    conn = sqlite3.connect(seeded_db)
    conn.row_factory = sqlite3.Row
    try:
        rows = db.unified_recent_rows(conn, 100)
    finally:
        conn.close()
    assert rows, "seeded skills must produce timeline rows"
    assert {r["kind"] for r in rows} == {"skill"}


def test_status_cell_and_rate_text_helpers():
    assert "success" in st.status_cell("success")
    assert "error" in st.status_cell("error")
    assert st.rate_text(0, 0) == "-"
    assert "90%" in st.rate_text(10, 9)
    assert "100%" in st.rate_text(3, 3)


def test_tui_recent_tab_shows_unified_kinds(seeded_db, seeded_mcp_db):
    """Recent tab renders the unified timeline (kind column, newest first)."""
    from textual.widgets import TabbedContent

    async def _run_it():
        app = SkillTUI(db_path=seeded_db, no_sync=True)
        async with app.run_test() as pilot:
            await pilot.pause()
            kinds = {r["kind"] for r in app.recent_rows_cache}
            assert kinds == {"skill"}, kinds
            table = app.screen.query_one("#recent-table", DataTable)
            assert table.row_count == len(app.recent_rows_cache) >= 1
            assert table.columns, "the Kind column must exist"

        app2 = SkillTUI(db_path=seeded_mcp_db, no_sync=True)
        async with app2.run_test() as pilot:
            await pilot.pause()
            assert app2.recent_rows_cache, "MCP rows must reach the timeline"
            assert {r["kind"] for r in app2.recent_rows_cache} == {"mcp"}
            assert app2.screen.query_one("#recent-table", DataTable).row_count == 8

    _run(_run_it())


def test_tui_mcp_detail_screen_opens(seeded_mcp_db):
    async def _run_it():
        app = SkillTUI(db_path=seeded_mcp_db, no_sync=True)
        async with app.run_test() as pilot:
            await pilot.pause()
            for _ in range(2):                # Dashboard -> Skills -> MCP
                await pilot.press("tab")
                await pilot.pause()
            await pilot.press("j")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert app.screen.query("#detail-history"), "MCP detail must open"
            await pilot.press("escape")
            await pilot.pause()
            assert app.screen.query("#mcp-table")

    _run(_run_it())


def test_tui_plugin_detail_screen_opens(seeded_plugin_db):
    async def _run_it():
        app = SkillTUI(db_path=seeded_plugin_db, no_sync=True)
        async with app.run_test() as pilot:
            await pilot.pause()
            for _ in range(3):                # Dashboard -> Skills -> MCP -> Plugins
                await pilot.press("tab")
                await pilot.pause()
            table = app.screen.query_one("#plugins-table", DataTable)
            assert table.row_count == 4, "one row per (plugin, kind, item)"
            await pilot.press("j")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert app.screen.query("#detail-history"), "plugin detail must open"
            await pilot.press("escape")
            await pilot.pause()
            assert app.screen.query("#plugins-table")

    _run(_run_it())


def test_tui_sort_modes_are_independent_per_table(seeded_mcp_db):
    """Cycling sort on MCP must not reshuffle the Skills table mode."""
    from textual.widgets import TabbedContent

    async def _run_it():
        app = SkillTUI(db_path=seeded_mcp_db, no_sync=True)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert (app.sort_mode, app.mcp_sort_mode) == ("count", "count")
            for _ in range(2):
                await pilot.press("tab")
                await pilot.pause()
            assert app.screen.query_one(TabbedContent).active == "tab-mcp"
            await pilot.press("s")
            await pilot.pause()
            assert app.mcp_sort_mode == "last_used", app.mcp_sort_mode
            assert app.sort_mode == "count", "Skills sort mode must be untouched"

    _run(_run_it())


def test_tui_mcp_search_narrows_rows(seeded_mcp_db):
    from textual.widgets import Input

    async def _run_it():
        app = SkillTUI(db_path=seeded_mcp_db, no_sync=True)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.screen.query_one("#mcp-table", DataTable).row_count == 5
            app.screen.query_one("#mcp-search", Input).value = "read_note"
            app.screen.render_mcp()
            await pilot.pause()
            assert app.screen.query_one("#mcp-table", DataTable).row_count == 1
            app.screen.query_one("#mcp-search", Input).value = ""
            app.screen.render_mcp()
            await pilot.pause()
            assert app.screen.query_one("#mcp-table", DataTable).row_count == 5

    _run(_run_it())


def test_tui_plugins_search_narrows_rows(seeded_plugin_db):
    from textual.widgets import Input

    async def _run_it():
        app = SkillTUI(db_path=seeded_plugin_db, no_sync=True)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.screen.query_one("#plugins-table", DataTable).row_count == 4
            app.screen.query_one("#plugins-search", Input).value = "compress"
            app.screen.render_plugins()
            await pilot.pause()
            assert app.screen.query_one("#plugins-table", DataTable).row_count == 1
            app.screen.query_one("#plugins-search", Input).value = ""
            app.screen.render_plugins()
            await pilot.pause()
            assert app.screen.query_one("#plugins-table", DataTable).row_count == 4

    _run(_run_it())


def test_tui_dashboard_plugin_card_shows_count(seeded_plugin_db):
    async def _run_it():
        app = SkillTUI(db_path=seeded_plugin_db, no_sync=True)
        async with app.run_test() as pilot:
            await pilot.pause()
            text = static_text(app.screen.query_one("#card-plugin", Static))
            assert "Plugin calls" in text, text
            assert "7" in text, f"plugin call count missing from the card: {text!r}"

    _run(_run_it())


def test_tui_dashboard_top_mcp_and_plugins_tables(seeded_plugin_db, seeded_mcp_db):
    """Dashboard carries Top-10 MCP/plugin tables mirroring their tabs."""
    async def _run_it():
        app = SkillTUI(db_path=seeded_plugin_db, no_sync=True)
        async with app.run_test() as pilot:
            await pilot.pause()
            plugins = app.screen.query_one("#dash-plugins", DataTable)
            assert plugins.row_count == 4, "one row per (plugin, kind, item)"
            assert app.screen.query_one("#dash-mcp", DataTable).row_count == 0

        app2 = SkillTUI(db_path=seeded_mcp_db, no_sync=True)
        async with app2.run_test() as pilot:
            await pilot.pause()
            mcp = app2.screen.query_one("#dash-mcp", DataTable)
            assert mcp.row_count == 5
            # pre-sorted by total DESC: the busiest tool is on top
            first = [str(c) for c in mcp.get_row_at(0)]
            assert first[1] == "basic-memory" and first[2] == "read_note", first

    _run(_run_it())


def test_tui_dashboard_trend_has_all_three_charts(seeded_mcp_db):
    """The trend row charts skill + MCP + plugin calls side by side."""
    async def _run_it():
        app = SkillTUI(db_path=seeded_mcp_db, no_sync=True)
        async with app.run_test() as pilot:
            await pilot.pause()
            for wid, label in (
                ("#trend-skills", "skill calls"),
                ("#trend-mcp", "MCP calls"),
                ("#trend-plugins", "plugin calls"),
            ):
                text = static_text(app.screen.query_one(wid, Static))
                assert label in text, f"{wid} missing {label!r}: {text!r}"
                # 7 day-rows under the title
                assert len(text.strip().splitlines()) == 8, text

    _run(_run_it())


# --- the three trend charts must stay on one horizontal line ---------------
# They are `Static`s of different data. As long as a row's length depends on the
# number printed at its end, one big count word-wraps *that* chart only, it grows
# a line taller, and the three day-rows that used to share a line no longer do.
def test_fmt_count_stays_five_chars_or_less():
    """The fixed row width is only fixed because the count field is capped."""
    for n in (0, 1, 999, 9_999, 10_000, 99_999, 100_000, 999_999, 1_000_000,
              1_234_567, 40_000_000, 99_999_999, 999_999_999, 1_000_000_000):
        assert len(st.fmt_count(n)) <= 5, (n, st.fmt_count(n))
    assert st.fmt_count(99_999) == "99999"
    assert st.fmt_count(123_456) == "123k"
    assert st.fmt_count(4_000_000) == "4.0M"


def test_tui_trend_rows_are_a_fixed_width(seeded_mcp_db):
    """Every day line of every chart has the same rendered length.

    Measured on the *unmarked-up* text: `Static.content` keeps the markup, and it
    is the parsed string that gets laid out, so `[cyan]…[/cyan]` must not count.
    """
    from rich.text import Text

    async def _run_it():
        app = SkillTUI(db_path=seeded_mcp_db, no_sync=True)
        async with app.run_test(size=(80, 40)) as pilot:
            await pilot.pause()
            for wid in ("#trend-skills", "#trend-mcp", "#trend-plugins"):
                raw = static_text(app.screen.query_one(wid, Static)).splitlines()[1:]
                assert len(raw) == st.TREND_DAYS, (wid, raw)
                lines = [Text.from_markup(line).plain for line in raw]
                assert {len(line) for line in lines} == {st.TREND_ROW_WIDTH}, (wid, lines)

    _run(_run_it())


def test_tui_trend_charts_share_one_line_when_a_series_is_huge(seeded_mcp_db, monkeypatch):
    """A six-digit count in one series must not push that chart off the line.

    Measured before the fix: heights 9 / 8 / 8 — the skills chart had wrapped its
    own day rows, which is exactly what the owner sees as "the bars are not on
    the same level".
    """
    days = [f"2026-09-{27 + i}" for i in range(7)]
    counts = [123_456, 1, 2, 3, 4, 5, 6]

    def big_skills(conn, n=7):
        return [{"date": d, "count": c} for d, c in zip(days, counts)]

    def small(conn, n=7):
        return [{"date": d, "count": 3} for d in days]

    monkeypatch.setattr(st.db, "daily_activity", big_skills)
    monkeypatch.setattr(st.db, "daily_mcp_activity", small)
    monkeypatch.setattr(st.db, "daily_plugin_activity", small)

    async def _run_it():
        app = SkillTUI(db_path=seeded_mcp_db, no_sync=True)
        async with app.run_test(size=(80, 40)) as pilot:
            await pilot.pause()
            regions = [
                app.screen.query_one(wid, Static).region
                for wid in ("#trend-skills", "#trend-mcp", "#trend-plugins")
            ]
            assert {r.height for r in regions} == {st.TREND_LINES}, regions
            assert len({r.y for r in regions}) == 1, regions

    _run(_run_it())


def test_tui_trend_css_height_matches_the_line_count():
    """The pinned height is tied to the number of lines, not a magic 8.

    Bump TREND_DAYS alone and this goes red: the CSS would then clip a chart that
    grew, which is the failure mode the pinned height is here to make loud.
    """
    import re

    css = st.get_app_class().CSS
    match = re.search(r"\.trend\s*\{[^}]*?height:\s*(\d+)", css)
    assert match, ".trend has no pinned height — the charts can drift apart again"
    assert int(match.group(1)) == st.TREND_LINES


# --- the dashboard ranking: a bar column whose length *is* the call count ----
def advisor_cell(table, label: str, row: int = 0) -> str:
    """One Advisor cell, addressed by its header so the columns may move."""
    column = next((c for c in table.columns.values() if str(c.label) == label), None)
    if column is None:
        raise AssertionError(
            f"no {label!r} column; headers are "
            f"{[str(c.label) for c in table.columns.values()]}"
        )
    return str(table.get_cell(list(table.rows)[row], column.key))


def test_tui_dashboard_tables_size_to_content(seeded_db):
    """Dashboard tables must show all their rows, not collapse to headers.

    #dash-top used `height: 1fr`; once the dashboard grew taller than the
    viewport (trend x3 + two more tables) the fraction resolved to ~0 and
    Top Skills rendered as a header-only strip on short terminals.
    """
    async def _run_it():
        app = SkillTUI(db_path=seeded_db, no_sync=True)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            top = app.screen.query_one("#dash-top", DataTable)
            assert top.row_count == 5
            assert top.outer_size.height >= top.row_count + 1, (
                f"dashboard table collapsed to height {top.outer_size.height}"
            )

    _run(_run_it())


def test_tui_dashboard_scrolls_to_last_section(seeded_plugin_db):
    """The dashboard overflows an 80x30 terminal and must scroll to it.

    Without a scroll container Textual rendered the overflow off-screen and
    "Top Plugins" was unreachable at every terminal size (measured: its region
    started at y=45 on a 45-row viewport even at 120x45).
    """
    from textual.containers import VerticalScroll

    async def _run_it():
        app = SkillTUI(db_path=seeded_plugin_db, no_sync=True)
        async with app.run_test(size=(80, 30)) as pilot:
            await pilot.pause()
            body = app.screen.query_one("#dash-body", VerticalScroll)
            assert body.virtual_size.height > body.size.height, (
                "precondition: dashboard content should exceed an 80x30 viewport"
            )
            plugins = app.screen.query_one("#dash-plugins", DataTable)
            assert plugins.region.bottom > body.region.bottom, (
                "precondition: Top Plugins should start out below the fold"
            )
            body.scroll_end(animate=False)
            await pilot.pause()
            assert plugins.region.bottom <= body.region.bottom, (
                "Top Plugins must be reachable after scrolling the dashboard"
            )

    _run(_run_it())


def test_mcp_detail_stats_always_include_last_used(seeded_mcp_db):
    """The MCP stats panel must survive both branches of the avg check.

    It used to read `f"...rate {..}   " f"avg {..}ms" if avg is not None else
    "" + "   " f"last used {..}"`. Because an inline conditional binds to the
    whole concatenated string, the avg-present branch dropped "last used" and
    the avg-absent branch dropped every total.
    """
    async def _run_it():
        app = SkillTUI(db_path=seeded_mcp_db, no_sync=True)
        async with app.run_test(size=(120, 45)) as pilot:
            await pilot.pause()
            screen_cls = st._tui_classes()["McpDetailScreen"]
            base = {
                "total": 2, "success": 2, "errors": 0, "denied": 0,
                "last_used": "2026-01-01T00:00:00.000Z", "history": [],
            }
            for avg in (123.0, None):
                app.push_screen(screen_cls("srv", "tool", dict(base, avg_ms=avg)))
                await pilot.pause()
                text = static_text(app.screen.query_one("#detail-stats", Static))
                assert "last used" in text, (avg, text)
                assert "total 2" in text and "success rate" in text, (avg, text)
                if avg is not None:
                    assert "avg 123ms" in text, text
                app.pop_screen()
                await pilot.pause()

    _run(_run_it())


def test_tui_dashboard_mcp_row_enter_opens_detail(seeded_mcp_db):
    """The dashboard promised "Enter opens detail" and did nothing.

    Regression: `dash-mcp` kept the default cell cursor, so Enter raised
    CellSelected instead of RowSelected and no handler was listening.
    """
    async def _run_it():
        app = SkillTUI(db_path=seeded_mcp_db, no_sync=True)
        async with app.run_test(size=(120, 45)) as pilot:
            await pilot.pause()
            table = app.screen.query_one("#dash-mcp", DataTable)
            assert table.cursor_type == "row", "row cursor or Enter never selects a row"
            assert table.row_count >= 1
            table.focus()
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert app.screen.__class__.__name__ == "McpDetailScreen", (
                "Enter must open the MCP detail screen"
            )
            await pilot.press("escape")
            await pilot.pause()
            assert app.screen.query("#dash-mcp")

    _run(_run_it())


def test_tui_dashboard_plugin_row_enter_opens_detail(seeded_plugin_db):
    """A scoped npm plugin name carries a "/" itself.

    Regression: the plugin row key was `plugin/kind/item` and was split back
    apart on Enter, so `@tarquinen/opencode-dcp@3.2.0` parsed into a bogus
    plugin and the detail page never opened — from the Plugins tab as well as
    from the dashboard.
    """
    async def _run_it():
        app = SkillTUI(db_path=seeded_plugin_db, no_sync=True)
        async with app.run_test(size=(120, 45)) as pilot:
            await pilot.pause()
            for tid in ("#dash-plugins", "#plugins-table"):
                table = app.screen.query_one(tid, DataTable)
                if tid == "#plugins-table":
                    app.screen.query_one("TabbedContent").active = "tab-plugins"
                    await pilot.pause()
                assert table.row_count >= 1
                table.focus()
                await pilot.pause()
                await pilot.press("enter")
                await pilot.pause()
                assert app.screen.__class__.__name__ == "PluginDetailScreen", (
                    f"Enter on {tid} must open the plugin detail screen"
                )
                await pilot.press("escape")
                await pilot.pause()

    _run(_run_it())


def test_tui_recent_row_with_colon_in_name_opens_detail(seeded_plugin_db):
    """A plugin command name carries ":" — the timeline key used to split on it.

    Regression: the Recent row key was "kind:name:timestamp" and Enter parsed it
    with split(":", 2), so `conductor:status` mis-parsed and Enter did nothing
    at all: no page, no warning, no trace of the promise in the label.
    """
    async def _run_it():
        app = SkillTUI(db_path=seeded_plugin_db, no_sync=True)
        async with app.run_test(size=(120, 45)) as pilot:
            await pilot.pause()
            app.screen.query_one("TabbedContent").active = "tab-recent"
            await pilot.pause()
            table = app.screen.query_one("#recent-table", DataTable)
            idx = next(
                i for i, r in enumerate(app.recent_rows_cache)
                if ":" in (r["name"] or "") and r["kind"] == "plugin"
            )
            table.move_cursor(row=idx)
            table.focus()
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert app.screen.__class__.__name__ == "PluginDetailScreen"

    _run(_run_it())


def test_tui_picks_up_rows_written_by_the_plugin(seeded_db):
    """The writer is a separate process, so navigating must re-read.

    Regression: cards only changed on manual `r`, so a dashboard that said
    "21 Skill calls" kept saying it while the plugin recorded more.
    """
    import sqlite3

    async def _run_it():
        app = SkillTUI(db_path=seeded_db, no_sync=True)
        async with app.run_test(size=(120, 45)) as pilot:
            await pilot.pause()
            before = static_text(app.screen.query_one("#card-usage", Static))
            assert "21" in before, before

            extra = sqlite3.connect(seeded_db)
            extra.execute(
                "INSERT INTO skill_usage (skill_name, session_id, project_path,"
                " trigger_type, status, timestamp, call_id)"
                " VALUES ('grow','s-live','/p','tool_call','success',"
                " strftime('%Y-%m-%dT%H:%M:%fZ','now'), 'c-live')"
            )
            extra.commit()
            extra.close()

            screen = app.screen
            skills = screen.query_one("#skills-table", DataTable)
            cursor_before = skills.cursor_coordinate
            # Pretend the last read was a while ago, then navigate.
            screen._last_refresh = 0.0
            await pilot.press("j")
            await pilot.pause()

            after = static_text(app.screen.query_one("#card-usage", Static))
            assert "22" in after, f"{before!r} -> {after!r}"
            assert skills.cursor_coordinate == cursor_before, "a refresh must not move the cursor"
            app.conn.close()

    _run(_run_it())


def test_tui_refresh_is_throttled_not_per_keystroke(seeded_db):
    """_maybe_refresh must be cheap: a re-read at most every stale window."""
    async def _run_it():
        app = SkillTUI(db_path=seeded_db, no_sync=True)
        async with app.run_test(size=(120, 45)) as pilot:
            await pilot.pause()
            calls = []
            screen = app.screen
            real = screen.refresh_data
            screen.refresh_data = lambda: calls.append(1) or real()
            screen._last_refresh = 0.0
            await pilot.press("j")
            await pilot.pause()
            assert len(calls) == 1, calls
            for _ in range(5):
                await pilot.press("j")
                await pilot.pause()
            assert len(calls) == 1, f"fresh data re-read {len(calls)} times in a row"
            app.conn.close()

    _run(_run_it())


def test_tui_shows_the_age_of_what_is_on_screen(seeded_db):
    """Idle numbers must not look live."""
    async def _run_it():
        app = SkillTUI(db_path=seeded_db, no_sync=True)
        async with app.run_test(size=(140, 45)) as pilot:
            await pilot.pause()
            legend = static_text(app.screen.query_one("#cards-legend", Static))
            assert "data as of" in legend, legend
            assert "observed" in legend
    _run(_run_it())


def test_tui_refreshes_when_a_tab_is_activated(seeded_db):
    import sqlite3

    async def _run_it():
        app = SkillTUI(db_path=seeded_db, no_sync=True)
        async with app.run_test(size=(120, 45)) as pilot:
            await pilot.pause()
            extra = sqlite3.connect(seeded_db)
            extra.execute(
                "INSERT INTO skill_usage (skill_name, session_id, project_path,"
                " trigger_type, status, timestamp, call_id)"
                " VALUES ('grow','s-tab','/p','tool_call','success',"
                " strftime('%Y-%m-%dT%H:%M:%fZ','now'), 'c-tab')"
            )
            extra.commit()
            extra.close()

            app.screen.query_one("TabbedContent").active = "tab-skills"
            await pilot.pause()
            assert "22" in static_text(app.screen.query_one("#card-usage", Static))
            app.conn.close()

    _run(_run_it())


def test_tui_reports_an_unopenable_database_instead_of_crashing(tmp_path):
    """A path whose directory does not exist must not escape on_mount."""
    db_path = str(tmp_path / "nope" / "skill-usage.db")
    assert not os.path.isdir(os.path.dirname(db_path))

    async def _run_it():
        app = SkillTUI(db_path=db_path, no_sync=True)
        async with app.run_test(size=(120, 45)) as pilot:
            await pilot.pause()
            assert app.conn is None
            legend = static_text(app.screen.query_one("#cards-legend", Static))
            assert "Cannot open" in legend, legend

    _run(_run_it())


def test_tui_creates_no_app_timers(seeded_db):
    """textual 8.2.8: an app timer makes run_test teardown raise LookupError.

    The refresh is deliberately event-driven, so nothing is left pending when a
    headless test exits. Pin that, or the whole TUI suite goes red at once.
    """
    async def _run_it():
        app = SkillTUI(db_path=seeded_db, no_sync=True)
        async with app.run_test(size=(120, 45)) as pilot:
            await pilot.pause()
            names = {t.name or "" for t in app._timers}
            assert not [n for n in names if "refresh" in n], names
            app.conn.close()
    _run(_run_it())


def test_tui_advisor_tab_renders_the_store(seeded_db, tmp_path, monkeypatch):
    """The advisor registers no tool and no command, so it needs its own view."""
    import skill_db as db
    from test_agentos import TASK_PROSE, _make_store
    from textual.widgets import DataTable, Static

    monkeypatch.setenv(db.AGENTOS_DB_ENV, _make_store(tmp_path))

    async def _run_it():
        app = SkillTUI(db_path=seeded_db, no_sync=True)
        async with app.run_test(size=(140, 45)) as pilot:
            await pilot.pause()
            # The advisor loop's session_id must join to real tracker rows.
            app.conn.execute(
                "INSERT INTO skill_usage (skill_name, session_id, project_path,"
                " trigger_type, status, timestamp, call_id) VALUES"
                " ('grow','ses_test_1','/p','tool_call','success',"
                " '2026-10-01T05:00:05.000Z','c-join')"
            )
            app.conn.commit()
            app.screen.query_one("TabbedContent").active = "tab-advisor"
            await pilot.pause()
            label = str(app.screen.query_one("#advisor-label", Static).content)
            assert "LOOP-TEST-1" not in label
            assert "memories" in label
            # the budget number is now explained in the legend, not squeezed into
            # the header line
            assert "per-call budget of 1200 ms" in static_text(
                app.screen.query_one("#advisor-legend", Static))
            table = app.screen.query_one("#advisor-table", DataTable)
            assert table.row_count == 1, table.row_count
            assert table.cursor_type == "row"
            cells = [str(table.get_cell(list(table.rows)[0], c)) for c in table.columns]
            assert any("LOOP-TEST-1" in c for c in cells), cells
            # route + recall completed, execute still pending. The denominator is
            # the stages this loop *recorded*, not the advisor's ten known stages.
            assert advisor_cell(table, "Stages recorded") == "2 of 3 recorded", cells
            assert advisor_cell(table, "Outcome") == "partial", cells
            # the join: give the loop's session a real usage row, then let the
            # tab activation re-read — no manual refresh
            assert advisor_cell(table, "Measured calls") == (
                "1 skill, 0 mcp, 0 plugin"), cells
            # three different counts, in three different columns: the engine's
            # self-report, the memories selected, and what reached the prompt
            assert advisor_cell(table, "Searched") == "3", cells
            assert advisor_cell(table, "Recalled") == "3", cells
            assert advisor_cell(table, "Reached the prompt") == "4 (1234 chars)", cells
            assert "over budget by 2800ms" in advisor_cell(table, "Slowest vs budget"), cells
            assert TASK_PROSE not in " ".join(cells) and TASK_PROSE not in label

    _run(_run_it())


def test_tui_advisor_tab_has_a_plain_language_legend(seeded_db, tmp_path, monkeypatch):
    from test_agentos import _make_store
    """The tab must explain itself, not depend on the reader knowing the jargon."""
    import skill_db as db

    monkeypatch.setenv(db.AGENTOS_DB_ENV, _make_store(tmp_path))

    async def _run_it():
        app = SkillTUI(db_path=seeded_db, no_sync=True)
        async with app.run_test(size=(140, 45)) as pilot:
            await pilot.pause()
            app.screen.query_one("TabbedContent").active = "tab-advisor"
            await pilot.pause()
            legend = static_text(app.screen.query_one("#advisor-legend", Static))
            for phrase in ("one advisor participation", "Reached the prompt",
                           "measured", "budget", "read-only"):
                assert phrase.lower() in legend.lower(), (phrase, legend)
            # the budget number is printed, so a reader can judge "over" at all
            assert "1200" in legend, legend
            headers = [str(c.label) for c in
                       app.screen.query_one("#advisor-table", DataTable).columns.values()]
            assert not any("sk/mcp/pl" in h for h in headers), headers
            assert not any("→" in h for h in headers), headers

    _run(_run_it())


def test_tui_advisor_searched_recalled_and_reached_are_three_numbers(seeded_db, monkeypatch, tmp_path):
    from test_agentos import _make_store
    """`retrieved`, `len(memory_ids)` and `len(injected_memory_ids)` are not one number.

    With a single fixture loop they coincide, which is how a column printing the
    wrong one stayed invisible: 5 searched, 3 recalled, 4 reached the prompt.
    """
    import skill_db as db

    monkeypatch.setenv(db.AGENTOS_DB_ENV, _make_store(tmp_path, extra_loop=True))

    async def _run_it():
        app = SkillTUI(db_path=seeded_db, no_sync=True)
        async with app.run_test(size=(140, 45)) as pilot:
            await pilot.pause()
            app.screen.query_one("TabbedContent").active = "tab-advisor"
            await pilot.pause()
            table = app.screen.query_one("#advisor-table", DataTable)
            assert table.row_count == 2, table.row_count
            rows = {advisor_cell(table, "Loop", i): i for i in range(table.row_count)}
            assert "LOOP-TEST-2" in rows, rows
            i = rows["LOOP-TEST-2"]
            assert advisor_cell(table, "Searched", i) == "5"
            assert advisor_cell(table, "Recalled", i) == "3"
            assert advisor_cell(table, "Reached the prompt", i) == "4 (1359 chars)"

    _run(_run_it())


def test_tui_advisor_columns_carry_no_abbreviations(seeded_db, monkeypatch, tmp_path):
    from test_agentos import _make_store
    """`1/0/0` and `2/3 ok` were guessable only by whoever wrote them."""
    import skill_db as db

    monkeypatch.setenv(db.AGENTOS_DB_ENV, _make_store(tmp_path))

    async def _run_it():
        app = SkillTUI(db_path=seeded_db, no_sync=True)
        async with app.run_test(size=(140, 45)) as pilot:
            await pilot.pause()
            app.screen.query_one("TabbedContent").active = "tab-advisor"
            await pilot.pause()
            table = app.screen.query_one("#advisor-table", DataTable)
            cells = [str(table.get_cell(list(table.rows)[0], c)) for c in table.columns]
            for cryptic in ("1/0/0", "2/3 ok", "3 → 4 (1234c)", "within", "over"):
                assert cryptic not in cells, (cryptic, cells)

    _run(_run_it())


def test_tui_dead_bar_wrapper_is_gone():
    """`uses_bar` had no caller; a second name for one helper invites drift."""
    assert not hasattr(st, "uses_bar")


# --- Advisor rows must open a detail page, like every other table ----------
def _enter_advisor_row(seeded_db, tmp_path, monkeypatch, check, which=0):
    """Mount the app on a stand-in advisor store and Enter one Advisor row.

    `check` runs *inside* `run_test`: after the context exits the app is torn down
    and `app.screen` raises `ScreenStackError`, which is what the first draft of
    these tests mistook for a missing screen.
    """
    import skill_db as db
    from test_agentos import _make_store

    monkeypatch.setenv(db.AGENTOS_DB_ENV, _make_store(tmp_path))

    async def _run_it():
        app = SkillTUI(db_path=seeded_db, no_sync=True)
        async with app.run_test(size=(160, 50)) as pilot:
            await pilot.pause()
            app.screen.query_one("TabbedContent").active = "tab-advisor"
            await pilot.pause()
            table = app.screen.query_one("#advisor-table", DataTable)
            table.move_cursor(row=which)
            table.focus()
            await pilot.press("enter")
            await pilot.pause()
            check(app, [k.value for k in table.rows])

    _run(_run_it())


def test_tui_advisor_row_has_a_resolvable_key(seeded_db, tmp_path, monkeypatch):
    """Advisor rows were added without `key=`, so every lookup missed.

    `RowKey(None)` for all of them: Enter and the second click both landed on
    `if not target: return` — the tab looked interactive and did nothing.
    """
    def check(app, keys):
        assert keys, "no advisor rows rendered"
        assert all(keys), f"rows still carry no key: {keys}"
        for key in keys:
            assert key in app.row_targets, key
            assert app.row_targets[key][0] == "advisor", app.row_targets[key]
        # identity travels in the tuple, never parsed back out of the key
        assert app.row_targets[keys[0]][1] == "LOOP-TEST-1", app.row_targets[keys[0]]

    _enter_advisor_row(seeded_db, tmp_path, monkeypatch, check)


def test_tui_advisor_row_enter_opens_detail(seeded_db, tmp_path, monkeypatch):
    def check(app, keys):
        assert app.screen.__class__.__name__ == "AdvisorDetailScreen", app.screen

    _enter_advisor_row(seeded_db, tmp_path, monkeypatch, check)


def test_tui_advisor_row_double_click_opens_detail(seeded_db, tmp_path, monkeypatch):
    """The mouse behaviour the other tables already have.

    Textual's `DataTable` posts `RowSelected` when a click lands on the cell that
    already holds the row cursor, so the *second* click of a double click is the
    trigger — no timer and no click-chain bookkeeping of ours, which is what keeps
    this inside the no-app-timers rule. This test is the evidence that the claim
    holds under the headless pilot; it is not inherited from reading the widget.
    """
    import skill_db as db
    from test_agentos import _make_store

    monkeypatch.setenv(db.AGENTOS_DB_ENV, _make_store(tmp_path))

    async def _run_it():
        app = SkillTUI(db_path=seeded_db, no_sync=True)
        async with app.run_test(size=(160, 50)) as pilot:
            await pilot.pause()
            app.screen.query_one("TabbedContent").active = "tab-advisor"
            await pilot.pause()
            table = app.screen.query_one("#advisor-table", DataTable)
            table.move_cursor(row=0)
            await pilot.pause()
            # (4, 1) is the first body cell. A probe showed why the obvious y=2
            # does not work: the click lands between cells, `DataTable._on_click`
            # receives an empty style meta and returns without selecting, so a
            # wrong offset reads as a broken feature. The offset is the test.
            await pilot.double_click("#advisor-table", offset=(4, 1))
            await pilot.pause()
            assert app.screen.__class__.__name__ == "AdvisorDetailScreen", (
                "double click did nothing: check whether the synthetic click still "
                "carries the row/column style meta DataTable reads"
            )

    _run(_run_it())


def test_tui_row_dispatch_ignores_unknown_kinds(seeded_db):
    """The dispatch chain ended in `else: _open_plugin_detail(rest[0..2])`.

    A kind it had never heard of was handed three positional arguments it did not
    have — an `IndexError` inside a message handler. Registering the advisor kind
    is what turned that latent bug into a certain one, so the branch order and the
    terminal `else: return` are pinned here.
    """
    from textual.widgets.data_table import RowKey

    async def _run_it():
        app = SkillTUI(db_path=seeded_db, no_sync=True)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            screen = app.screen
            table = screen.query_one("#dash-top", DataTable)
            app.row_targets["bogus#1"] = ("not-a-known-kind", "x")
            event = DataTable.RowSelected(table, 0, RowKey("bogus#1"))
            screen.on_data_table_row_selected(event)      # must not raise
            await pilot.pause()
            assert app.screen is screen, "an unknown kind should do nothing"

    _run(_run_it())


def test_tui_advisor_label_says_the_tracker_cannot_see_the_advisor(seeded_db, tmp_path, monkeypatch):
    """The page must state where its numbers come from.

    A reader who assumes these are tracker-measured usage rows will believe the
    advisor's behaviour was measured. It never can be: the plugin registers no
    tool and no command, so no usage row can name it.
    """
    import skill_db as db
    from test_agentos import _make_store
    from textual.widgets import Static

    monkeypatch.setenv(db.AGENTOS_DB_ENV, _make_store(tmp_path))

    async def _run_it():
        app = SkillTUI(db_path=seeded_db, no_sync=True)
        async with app.run_test(size=(160, 50)) as pilot:
            await pilot.pause()
            app.screen.query_one("TabbedContent").active = "tab-advisor"
            await pilot.pause()
            label = static_text(app.screen.query_one("#advisor-label", Static))
            assert "own store" in label, label
            assert "no tool and no command" in label, label
            assert "not measured here" in label, label

    _run(_run_it())


def test_tui_advisor_label_says_it_even_without_a_store(seeded_db, monkeypatch):
    """The explanation belongs to the page, not to one of its branches."""
    import skill_db as db
    from textual.widgets import Static

    for var in (db.AGENTOS_DB_ENV, "AOS_DB", "AGENT_OS_ROOT"):
        monkeypatch.delenv(var, raising=False)

    async def _run_it():
        app = SkillTUI(db_path=seeded_db, no_sync=True)
        async with app.run_test(size=(160, 50)) as pilot:
            await pilot.pause()
            app.screen.query_one("TabbedContent").active = "tab-advisor"
            await pilot.pause()
            label = static_text(app.screen.query_one("#advisor-label", Static))
            assert "not aggregated" in label, label
            assert "own store" in label and "no tool and no command" in label, label

    _run(_run_it())


def test_tui_advisor_detail_dismisses_like_the_other_detail_screens(
    seeded_db, tmp_path, monkeypatch
):
    """Esc and `q` both go back to the Advisor tab — the peers' contract."""
    import skill_db as db
    from test_agentos import _make_store
    from textual.widgets import Static

    monkeypatch.setenv(db.AGENTOS_DB_ENV, _make_store(tmp_path))

    async def _run_it():
        app = SkillTUI(db_path=seeded_db, no_sync=True)
        async with app.run_test(size=(160, 50)) as pilot:
            await pilot.pause()
            app.screen.query_one("TabbedContent").active = "tab-advisor"
            await pilot.pause()
            table = app.screen.query_one("#advisor-table", DataTable)
            table.focus()
            for key in ("enter", "escape"):
                await pilot.press(key)
                await pilot.pause()
                if key == "enter":
                    assert app.screen.__class__.__name__ == "AdvisorDetailScreen"
                else:
                    assert app.screen.query_one("#advisor-table", DataTable)
            # `q` on a detail screen is bound to Back too
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("q")
            await pilot.pause()
            assert app.screen.query_one("#advisor-legend", Static)
            assert table.row_count >= 1, "the tab it returns to must still be populated"

    _run(_run_it())


def test_tui_advisor_detail_draws_per_stage_bars(seeded_db, tmp_path, monkeypatch):
    """The detail page is where 'what did it actually do' becomes a shape."""
    import skill_db as db
    from test_agentos import _make_store
    from textual.widgets import Static

    monkeypatch.setenv(db.AGENTOS_DB_ENV, _make_store(tmp_path))

    async def _run_it():
        app = SkillTUI(db_path=seeded_db, no_sync=True)
        async with app.run_test(size=(160, 50)) as pilot:
            await pilot.pause()
            app.screen.query_one("TabbedContent").active = "tab-advisor"
            await pilot.pause()
            app.screen.query_one("#advisor-table", DataTable).focus()
            await pilot.press("enter")
            await pilot.pause()
            chart = static_text(app.screen.query_one("#advisor-stage-chart", Static))
            lines = [ln for ln in chart.splitlines() if ln.strip()]
            by_name = {ln.split()[0]: ln for ln in lines}
            for stage in ("route", "recall", "execute", "budget"):
                assert stage in by_name, (stage, chart)
            blocks = {k: v.count("█") for k, v in by_name.items()}
            # recall (4000ms) is this loop's own peak, so it fills the column
            assert blocks["recall"] == max(blocks.values()) == 22, blocks
            assert blocks["route"] < blocks["recall"], blocks
            # the budget sits on the same scale so a reader can see the margin
            assert blocks["budget"] < blocks["recall"], blocks
            assert blocks["execute"] == 0, by_name["execute"]
            assert "pending, no timing recorded" in by_name["execute"], by_name["execute"]
            assert "over" in by_name["recall"], "a 4000ms stage against 1200ms must say so"
            assert isinstance(app.screen.query_one("#advisor-loop-id", Static), Static)

    _run(_run_it())


def test_tui_advisor_detail_budget_row_fits_when_every_stage_is_fast(seeded_db):
    """Real data, not the fixture: slowest stage 18ms against a 1200ms budget.

    With the peak taken from the stages alone, `bar()` scales the budget as
    1200/18*22 and that row printed 1466 blocks, wrapping the whole page. The
    fixture loop has a 4000ms stage, so every earlier test was blind to it.
    """
    from rich.text import Text

    loop = {
        "loop_id": "LOOP-REALISH", "session_id": "ses_f0a1", "stage": "finalize",
        "final_status": "partial", "created_at": "2026-10-01T05:27:14+00:00",
        "updated_at": "2026-10-01T05:27:30+00:00", "memory_mode": "enabled",
        "provider": "host_delegate", "model": "opencode/test-model", "errors": 0,
        "stage_count": 3, "slowest_stage": {"name": "record", "ms": 18},
        "over_budget_ms": False, "injection": None, "postflight": None,
        "usage": {"skill": 0, "mcp": 0, "plugin": 0},
        "stages": {
            "route": {"status": "completed", "ms": 16, "failed": False},
            "record": {"status": "completed", "ms": 18, "failed": False},
            "execute": {"status": "pending", "ms": None, "failed": False},
        },
    }

    async def _run_it():
        app = SkillTUI(db_path=seeded_db, no_sync=True)
        async with app.run_test(size=(160, 50)) as pilot:
            await pilot.pause()
            app.push_screen(st._tui_classes()["AdvisorDetailScreen"](loop, 1200))
            await pilot.pause()
            chart = Text.from_markup(
                str(app.screen.query_one("#advisor-stage-chart", Static).content)
            ).plain
            by_name = {ln.split()[0]: ln for ln in chart.splitlines() if ln.strip()}
            counts = {k: v.count("█") for k, v in by_name.items()}
            assert max(counts.values()) <= st.STAGE_CHART_WIDTH, counts
            # the budget is the peak here, so it is the full column and every
            # stage reads as the small fraction of it that it really is
            assert counts["budget"] == st.STAGE_CHART_WIDTH, counts
            assert 0 < counts["record"] < counts["budget"], counts
            assert counts["execute"] == 0, counts

    _run(_run_it())


def test_tui_advisor_detail_never_renders_store_text(seeded_db, tmp_path, monkeypatch):
    """The four sentinels live in the same files the tab reads. None may surface."""
    import skill_db as db
    from test_agentos import (
        ERROR_PROSE,
        QUERY_PROSE,
        STAGE_PROSE,
        TASK_PROSE,
        _make_store,
    )
    from textual.widgets import Static

    monkeypatch.setenv(db.AGENTOS_DB_ENV, _make_store(tmp_path))

    async def _run_it():
        app = SkillTUI(db_path=seeded_db, no_sync=True)
        async with app.run_test(size=(160, 50)) as pilot:
            await pilot.pause()
            app.screen.query_one("TabbedContent").active = "tab-advisor"
            await pilot.pause()
            app.screen.query_one("#advisor-table", DataTable).focus()
            await pilot.press("enter")
            await pilot.pause()
            seen = [static_text(w) for w in app.screen.query(Static)]
            for table in app.screen.query(DataTable):
                for row_key in table.rows:
                    for col in table.columns.values():
                        seen.append(str(table.get_cell(row_key, col.key)))
            blob = " ".join(seen)
            for prose in (TASK_PROSE, STAGE_PROSE, QUERY_PROSE, ERROR_PROSE):
                assert prose not in blob, f"{prose} left the store"

    _run(_run_it())


def test_tui_advisor_detail_reads_only_through_the_projection():
    """Source-level guard: the screen must not gain its own way into the store.

    A sentinel sweep only catches text that is in the fixture. This catches the
    change that would let anything out: a screen that opens the file or the
    database itself, or that splats a dict into a widget.
    """
    import re

    source = (Path(__file__).resolve().parents[1] / "skill-tui.py").read_text(
        encoding="utf-8"
    )
    start = source.index("class AdvisorDetailScreen")
    end = source.index("class HealthScreen", start)
    screen_src = source[start:end]
    start2 = source.index("def _open_advisor_detail")
    end2 = source.index("def ", start2 + len("def _open_advisor_detail"))
    open_src = source[start2:end2]

    for forbidden in ("json.load", "open(", "sqlite3.connect", "loops/", "**loop",
                      '"task_text"', '"query"', '"data"'):
        assert forbidden not in screen_src, (forbidden, "in AdvisorDetailScreen")
        assert forbidden not in open_src, (forbidden, "in _open_advisor_detail")
    assert re.search(r"agentos_summary\(", open_src), "must read through the digest"


def test_tui_advisor_detail_of_a_loop_that_is_gone_notifies(seeded_db, tmp_path, monkeypatch):
    """The store is written by another process: the row may already be history."""
    import skill_db as db
    from test_agentos import _make_store
    from textual.widgets import Static

    monkeypatch.setenv(db.AGENTOS_DB_ENV, _make_store(tmp_path))

    async def _run_it():
        app = SkillTUI(db_path=seeded_db, no_sync=True)
        async with app.run_test(size=(160, 50)) as pilot:
            await pilot.pause()
            app.screen.query_one("TabbedContent").active = "tab-advisor"
            await pilot.pause()
            app.screen._open_advisor_detail("LOOP-THAT-IS-GONE")
            await pilot.pause()
            assert app.screen.__class__.__name__ == "MainScreen", app.screen
            assert isinstance(app.screen.query_one("#advisor-legend", Static), Static)

    _run(_run_it())


def test_tui_advisor_tab_says_when_there_is_no_store(seeded_db, monkeypatch):
    import skill_db as db
    from test_agentos import _make_store
    from textual.widgets import Static

    for var in (db.AGENTOS_DB_ENV, "AOS_DB", "AGENT_OS_ROOT"):
        monkeypatch.delenv(var, raising=False)

    async def _run_it():
        app = SkillTUI(db_path=seeded_db, no_sync=True)
        async with app.run_test(size=(140, 45)) as pilot:
            await pilot.pause()
            app.screen.query_one("TabbedContent").active = "tab-advisor"
            await pilot.pause()
            label = str(app.screen.query_one("#advisor-label", Static).content)
            assert "not aggregated" in label, label
            assert "never writes" in label, label

    _run(_run_it())
