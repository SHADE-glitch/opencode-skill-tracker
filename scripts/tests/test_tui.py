"""Headless smoke tests for the Textual app (requires the skillt venv).

Runs the real app against a temp DB and asserts the plan's acceptance criteria:
37 skills displayed, sorting works, the detail screen opens, the confirm modal
does not write on cancel, and quitting is clean.
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("textual")

from conftest import load_module  # noqa: E402

st = load_module("skill-tui.py", "skill_tui")

from textual.widgets import DataTable  # noqa: E402

SkillTUI = st.get_app_class()


def _run(coro):
    return asyncio.run(coro)


def test_tui_loads_37_skills_and_sorts(empty_db):
    async def _run_it():
        app = SkillTUI(db_path=empty_db, no_sync=False)
        async with app.run_test() as pilot:
            await pilot.pause()
            conn = app.conn
            assert conn is not None
            assert conn.execute("SELECT COUNT(*) FROM skills").fetchone()[0] == 37

            table = app.screen.query_one("#skills-table", DataTable)
            assert table.row_count == 37, "all 37 skills must be listed"

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
            assert 0 < filtered < 37, f"search should narrow the list, got {filtered}"
            await pilot.press("escape")
            await pilot.pause()
            assert app.screen.query_one("#skills-table", DataTable).row_count == 37

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
            for _ in range(4):          # Dashboard -> Skills -> Recent -> Categories -> Data
                await pilot.press("tab")
                await pilot.pause()

            await pilot.press("escape")
            await pilot.press("b")      # backup
            await pilot.pause()
            backups = glob.glob(os.path.join(os.path.dirname(seeded_db), "skill-usage-backup-*.db"))
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
# A2 regression: the five Dashboard cards must be labelled and tall enough to
# show both the number and the label. Before the fix, `padding: 1` left only one
# content row, so the labels were silently clipped.
# ---------------------------------------------------------------------------
CARD_LABELS = {
    "#card-skills": "Skills",
    "#card-usage": "Uses",
    "#card-today": "Today",
    "#card-personal": "Personal",
    "#card-oss": "OSS",
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
    from textual.widgets import Static

    async def _run_it():
        app = SkillTUI(db_path=seeded_db, no_sync=True)
        async with app.run_test() as pilot:
            await pilot.pause()
            legend = str(app.screen.query_one("#cards-legend", Static).content)
            assert "counts" in legend and "invocation" in legend, legend

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
            for _ in range(4):                # -> Data tab
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
            for _ in range(4):
                await pilot.press("tab")
                await pilot.pause()
            await pilot.press("escape")
            for _ in range(2):
                await pilot.press("b")
                await pilot.pause()
            for _ in range(2):
                await pilot.press("e")
                await pilot.pause()

            d = os.path.dirname(seeded_db)
            backups = glob.glob(os.path.join(d, "skill-usage-backup-*.db"))
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

