"""Tests for the custom date-range picker: the grids, the cards, and the window out.

The modal is a 1:1 port of CodeBuddy's picker, so the parity claim is not that it
looks the same — it is that the numbers it produces land in the pane that asked for
them and nowhere else. Everything is driven at a pinned instant (the modal takes
`now_ms`, the app takes a `clock`), so the month grid, the "today" mark and the
year wrap are the same on every machine and at every hour of the day.
"""

import asyncio
import importlib.util
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ocut_db as db                                    # noqa: E402
from textual.app import App                             # noqa: E402
from textual.widgets import (                           # noqa: E402
    Button, Checkbox, DataTable, Input, Static,
)

_spec = importlib.util.spec_from_file_location(
    "ocut_calendar_tui", str(Path(__file__).resolve().parents[1] / "ocut-tui.py"))
tui = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tui)

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_ocut_tui as tui_tests                       # noqa: E402

# 2026-03-15 is a Sunday, so the grid's first cell is the 1st — the case that would
# hide an off-by-one in a Sunday-first convention only on months that start elsewhere.
FIXED = datetime(2026, 3, 15, 13, 45).timestamp()
FIXED_MS = int(FIXED * 1000)
SUNDAY_ANCHOR = date(2026, 3, 15)


def lead_of(first: date) -> int:
    """Days since Sunday, computed from an ordinal rather than from `.weekday()`.

    The implementation reads `(weekday() + 1) % 7`; deriving the same number from a
    date known to be a Sunday means a test that disagrees with the product disagrees
    about the convention, not about the arithmetic.
    """
    return (first.toordinal() - SUNDAY_ANCHOR.toordinal()) % 7


class Picker(App):
    """A host that only shows the modal, so the picker can be driven alone."""

    def __init__(self, modal):
        super().__init__()
        self._modal = modal
        self.result = "nothing dismissed the modal"

    def on_mount(self) -> None:
        self.push_screen(self._modal, self._done)

    def _done(self, result) -> None:
        self.result = result


def drive_modal(modal, body, size=(92, 46)):
    captured = {}

    async def _it():
        app = Picker(modal)
        async with app.run_test(size=size) as pilot:
            await pilot.pause(0.5)
            captured["app"] = app
            await body(pilot, modal, app)
    asyncio.run(_it())
    return captured["app"]


def modal_at(current="7d", now_ms=FIXED_MS):
    return tui.DateRangeModal(current, now_ms)


def cells(modal, css_class):
    return list(modal.query(f".{css_class}"))



def classes(modal, wid):
    widget = modal.query_one(wid)
    return set(widget.classes)


# --- the shape of the picker ------------------------------------------------

def test_the_picker_carries_the_same_controls_as_the_reference():
    """Counts and labels, because a chip that silently disappeared is still green."""
    async def body(pilot, m, app):
        # The chip table itself, label to key. Read back through `m.PRESETS` a wrong
        # key is invisible — the picker would faithfully select whatever the table
        # says and the test would agree with it — so the three columns are pinned
        # here as the product's promise, not as its data.
        assert m.PRESETS == (
            ("Today", "dr-today", "1d"),
            ("1d", "dr-1d", "1d"),
            ("7d", "dr-7d", "7d"),
            ("14d", "dr-14d", "14d"),
            ("30d", "dr-30d", "30d"),
            ("All time", "dr-all", "all"),
        ), m.PRESETS
        assert [str(b.label) for b in m.query(Button)
                if b.id in {wid for _l, wid, _k in m.PRESETS}] == \
            [label for label, _wid, _key in m.PRESETS]
        assert len(cells(m, "dr-day")) == 42, len(cells(m, "dr-day"))
        assert len(cells(m, "dr-hour")) == 24, len(cells(m, "dr-hour"))
        assert len(list(m.query("#dr-start-quick Button"))) == len(m.QUICK_START)
        assert len(list(m.query("#dr-end-quick Button"))) == len(m.QUICK_END)
        assert len(list(m.query("#dr-nudge Button"))) == len(m.STEPS)
        assert m.query_one("#dr-follow", Checkbox).value is True
    drive_modal(modal_at(), body)


# --- the month grid ---------------------------------------------------------

@pytest.mark.parametrize("first", [date(2026, m, 1) for m in range(1, 13)])
def test_the_grid_leads_to_sunday_and_covers_the_whole_month(first):
    """Every month of a real year: the lead, the 1st's cell, and the 42-cell claim."""
    async def body(pilot, m, app):
        m._view = first
        m._render_calendar()
        assert m._day_at(0) == first - timedelta(days=lead_of(first)), m._day_at(0)
        assert m._day_at(lead_of(first)) == first
        last_day = (first + timedelta(days=32)).replace(day=1) - timedelta(days=1)
        assert m._day_at(41) >= last_day, "42 cells do not cover the month"
        # The first cell of the month carries no -other; the day before it does.
        assert "-other" not in classes(m, f"#dr-day-{lead_of(first)}")
        if lead_of(first):
            assert "-other" in classes(m, f"#dr-day-{lead_of(first) - 1}")
    drive_modal(modal_at(), body)


def test_today_is_marked_from_the_instant_the_picker_was_given():
    """`-today` follows the pinned now, not the machine's clock."""
    async def body(pilot, m, app):
        m._view = date(2026, 3, 1)
        m._render_calendar()
        marked = [i for i in range(42) if "-today" in classes(m, f"#dr-day-{i}")]
        assert marked == [lead_of(date(2026, 3, 1)) + 14], marked
    drive_modal(modal_at(), body)


def test_stepping_the_month_wraps_the_year_in_both_directions():
    async def body(pilot, m, app):
        m._view = date(2026, 12, 1)
        m._step_month(1)
        assert m._view == date(2027, 1, 1), m._view
        assert str(m.query_one("#dr-cal-month").content) == "January 2027"
        m._view = date(2026, 1, 1)
        m._step_month(-1)
        assert m._view == date(2025, 12, 1), m._view
        assert str(m.query_one("#dr-cal-month").content) == "December 2025"
    drive_modal(modal_at(), body)


# --- the two cards ----------------------------------------------------------

def test_a_day_and_an_hour_write_into_the_card_that_is_active():
    """The grids are one clock with two hands: what they set depends on the card."""
    async def body(pilot, m, app):
        m._view = date(2026, 3, 1)
        m._set_active("start")
        await pilot.click("#dr-day-10")            # 2026-03-01 + lead -> the 7th
        clicked = m._day_at(10)
        assert m.query_one("#dr-start-date", Input).value == db.format_date(clicked)
        assert m.query_one("#dr-end-date", Input).value != db.format_date(clicked)

        await pilot.click("#dr-end-card")
        assert m._active == "end"
        await pilot.click("#dr-day-20")
        later = m._day_at(20)
        assert m.query_one("#dr-end-date", Input).value == db.format_date(later)
        # Writing the end by hand means a fixed end, not "now".
        assert m.query_one("#dr-follow", Checkbox).value is False

        # The grid sets the hour and keeps whatever minute was already typed —
        # which is why the assertion is built from the field's own value, not from
        # a hardcoded ":00".
        before = m.query_one("#dr-end-time", Input).value
        _h, minute = db.parse_time(before)
        await pilot.click("#dr-hour-9")
        assert m.query_one("#dr-end-time", Input).value == db.format_time(9, minute), \
            (before, m.query_one("#dr-end-time", Input).value)
        selected = [str(m.query_one(f"#dr-hour-{h}").content) for h in range(24)
                    if "-selected" in classes(m, f"#dr-hour-{h}")]
        assert selected == ["09"], selected

        # The hour click keeps a typed minute; it only replaces the hour.
        m.query_one("#dr-end-time", Input).value = "07:45"
        m._pick_hour(23)
        assert m.query_one("#dr-end-time", Input).value == "23:45", \
            m.query_one("#dr-end-time", Input).value
        assert m._active == "end"
    drive_modal(modal_at(), body)


def test_the_picker_keeps_the_end_on_now_until_a_person_changes_it():
    """Our own writes must not be mistaken for an edit (both post Input.Changed)."""
    async def body(pilot, m, app):
        assert m.query_one("#dr-follow", Checkbox).value is True
        # A preset writes both fields and keeps following.
        await pilot.click("#dr-7d")
        assert m.query_one("#dr-follow", Checkbox).value is True
        # A typed end date is an edit: follow goes, and the field is what was typed.
        m.query_one("#dr-end-date", Input).value = "03/20/2026"
        await pilot.pause(0.3)
        assert m.query_one("#dr-follow", Checkbox).value is False
    drive_modal(modal_at(), body)


def test_a_preset_names_the_window_the_chip_says():
    async def body(pilot, m, app):
        for label, wid, key in m.PRESETS:
            await pilot.click(f"#{wid}")
            start = m.query_one("#dr-start-date", Input).value
            if key == "all":
                assert start == "", (label, start)
                continue
            # The key is written out here, not read from m.PRESETS: a chip whose
            # label and key disagree is exactly the failure this test is for, and
            # deriving the expectation from the same table would agree with it.
            expected = db.window_bounds(
                {"Today": "1d", "1d": "1d", "7d": "7d", "14d": "14d",
                 "30d": "30d", "All time": "all"}[label], FIXED_MS)[0]
            assert start == db.format_date(datetime.fromtimestamp(expected / 1000)), \
                (label, key, start)
            assert m.query_one("#dr-follow", Checkbox).value is True, label
    drive_modal(modal_at(), body)


# --- confirm ----------------------------------------------------------------

def test_confirm_hands_back_a_window_made_from_the_two_typed_fields():
    async def body(pilot, m, app):
        m.query_one("#dr-start-date", Input).value = "03/01/2026"
        m.query_one("#dr-start-time", Input).value = "06:30"
        m.query_one("#dr-follow", Checkbox).value = False
        m.query_one("#dr-end-date", Input).value = "03/10/2026"
        m.query_one("#dr-end-time", Input).value = "18:00"
        await pilot.pause(0.3)
        await pilot.click("#dr-ok")
        await pilot.pause(0.3)
    app = drive_modal(modal_at(), body)
    window = app.result
    assert isinstance(window, db.CustomWindow), window
    assert window.follow_now is False
    assert window.start_ms == db.parse_datetime("03/01/2026", "06:30", 2026)
    assert window.end_ms == db.parse_datetime("03/10/2026", "18:00", 2026)
    assert window.end_ms > window.start_ms


def test_a_blank_start_is_all_time_and_a_following_end_stays_on_the_clock():
    async def body(pilot, m, app):
        m.query_one("#dr-start-date", Input).value = ""
        m.query_one("#dr-follow", Checkbox).value = True
        await pilot.pause(0.3)
        await pilot.click("#dr-ok")
        await pilot.pause(0.3)
    app = drive_modal(modal_at(current="all"), body)
    window = app.result
    assert isinstance(window, db.CustomWindow), window
    assert window.start_ms is None, window
    assert window.follow_now is True


@pytest.mark.parametrize("start,end,follow,label", [
    ("03/10/2026", "", True, "follow covers a blank end"),        # allowed
    ("03/10/2026", "", False, "blank end, no follow -> refused"),
    ("03/10/2026", "03/01/2026", False, "end before start -> refused"),
    ("13/45/2026", "03/01/2026", False, "impossible date -> refused"),
    # The same unparseable date with the end following: swallowing the parse error
    # would dismiss here as "all time -> now", so this case is what tells a refused
    # input apart from a quietly discarded one.
    ("13/45/2026", "", True, "impossible date, follow on -> refused"),
])
def test_confirm_refuses_rather_than_inventing_a_window(start, end, follow, label):
    """Three refusals and one acceptance, all asserted on whether the modal closed.

    A picker that dismisses with a half-parsed span would silently widen or narrow
    the pane, so the claim is about the *result*, not about a message being shown.
    """
    async def body(pilot, m, app):
        m.query_one("#dr-start-date", Input).value = start
        m.query_one("#dr-start-time", Input).value = "00:00"
        m.query_one("#dr-end-date", Input).value = end
        m.query_one("#dr-end-time", Input).value = "12:00" if end else ""
        await pilot.pause(0.3)
        # Follow last: editing an end field drops it by design (that is
        # test_the_picker_keeps_the_end_on_now_until_a_person_changes_it), so a
        # person who wants "end = now" ticks it after touching the end, not before.
        m.query_one("#dr-follow", Checkbox).value = follow
        await pilot.pause(0.3)
        await pilot.click("#dr-ok")
        await pilot.pause(0.3)
    app = drive_modal(modal_at(), body)
    refused = label.endswith("refused")
    if refused:
        assert app.result == "nothing dismissed the modal", (label, app.result)
    else:
        assert isinstance(app.result, db.CustomWindow), (label, app.result)


def test_cancel_and_escape_both_leave_the_window_untouched():
    for how in ("button", "escape"):
        async def body(pilot, m, app, how=how):
            if how == "button":
                await pilot.click("#dr-cancel")
            else:
                await pilot.press("escape")
            await pilot.pause(0.3)
        app = drive_modal(modal_at(), body)
        assert app.result is None, (how, app.result)


# --- the picker inside the app ---------------------------------------------

@pytest.fixture
def tracker(tmp_path):
    """The same host fixture the TUI suite builds — one definition, two suites."""
    host = tmp_path / "opencode.db"
    tui_tests.build_host(host)
    cfg = tui_tests.build_config(tmp_path / "cfg")
    out = tmp_path / "usage.db"
    db.load_sync().run(db_path=out, host_db=host, host_config=cfg, quiet=True)
    return out


@pytest.fixture
def clock(tracker):
    """`now` pinned after the fixture is written; a callable, as the app wants it."""
    pinned = tui_tests.now_ms() / 1000
    return lambda: pinned


def test_a_picked_window_lands_on_the_tab_that_asked_and_on_no_other(tracker, clock):
    """The whole chain: 📅, typed span, Confirm, then the pane's rows are that span.

    The other tabs keep their own windows: a custom pick that leaked app-wide would
    still leave this tab correct, so the untouched tab's range is read back too.
    """
    conn = sqlite3.connect(tracker)
    conn.row_factory = sqlite3.Row

    async def body(pilot, app):
        await tui_tests.activate(app, pilot, "tab-tools")
        before = dict(app.tab_range)
        await pilot.click("#date-range")
        await pilot.pause(0.6)
        assert isinstance(app.screen, tui.DateRangeModal), type(app.screen)
        m = app.screen
        # A span that keeps only the fixture's old day: 40 days back, whole day.
        old = datetime.fromtimestamp(app._now_ms() / 1000) - timedelta(days=40)
        start = old.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        m.query_one("#dr-start-date", Input).value = db.format_date(start)
        m.query_one("#dr-start-time", Input).value = "00:00"
        m.query_one("#dr-follow", Checkbox).value = False
        m.query_one("#dr-end-date", Input).value = db.format_date(end)
        m.query_one("#dr-end-time", Input).value = "00:00"
        await pilot.pause(0.4)
        await pilot.click("#dr-ok")
        await pilot.pause(0.8)

        window = app.tab_range["tab-tools"]
        assert isinstance(window, db.CustomWindow), window
        assert window.start_ms == int(start.timestamp() * 1000), window
        assert window.follow_now is False
        for pane, value in before.items():
            if pane != "tab-tools":
                assert app.tab_range[pane] == value, (pane, app.tab_range[pane])
        assert app.tab_range["tab-dashboard"] == "1d"

        got = app.query_one("#t-tools", DataTable)
        rows = list(db.q_tools(conn, start_ts=window.start_ms, end_ts=window.end_ms))
        assert [str(got.get_cell_at((i, 0))) for i in range(got.row_count)] == \
            [r["tool_name"] for r in rows], rows
        # The window really filtered: `read` has 5 calls, all on that old day.
        read = [r for r in rows if r["tool_name"] == "read"]
        assert read and read[0]["calls"] == 5, read
        assert any(r["tool_name"] == "bash" and r["calls"] == 0 for r in rows), rows
        # The top bar says what the pane is showing, in the same breath.
        label = str(app.query_one("#date-range", Button).label)
        assert label == "📅 " + db.window_label(window), (label, window)
    tui_tests.drive(lambda: tui.TrackerApp(str(tracker), clock=clock), body,
                  size=(92, 46))


def test_the_plugins_tab_opens_the_picker_like_every_other_tab(tracker, clock):
    """Plugins owns a range now, so the 📅 button must reach it.

    Its *list* is still all-time — the inventory carries no timestamps — but
    ``uses`` and ``last used`` are windowed, so a picked span is meaningful. The
    old contract (this tab refuses the picker) is gone; what replaces it is that
    the picker opens, and cancelling leaves every tab's range untouched.
    """
    async def body(pilot, app):
        await tui_tests.activate(app, pilot, "tab-plugins")
        before = dict(app.tab_range)
        await pilot.click("#date-range")
        await pilot.pause(0.6)
        assert isinstance(app.screen, tui.DateRangeModal), type(app.screen)
        await pilot.press("escape")
        await pilot.pause(0.4)
        assert app.tab_range == before, app.tab_range
        assert app.tab_range["tab-plugins"] == "7d", app.tab_range
    tui_tests.drive(lambda: tui.TrackerApp(str(tracker), clock=clock), body,
                    size=(92, 46))


def test_a_following_window_moves_when_the_clock_moves(tracker):
    """`follow_now` is not a snapshot: the end is re-derived on every refresh."""
    ticks = [datetime(2026, 3, 15, 13, 45).timestamp()]

    async def body(pilot, app):
        start_ms = int(ticks[0] * 1000) - 60_000
        app.tab_range["tab-tools"] = db.CustomWindow(start_ms, start_ms, True)
        first = app._bounds_for("tab-tools")
        ticks[0] += 3600                       # an hour later, no other change
        second = app._bounds_for("tab-tools")
        assert first[0] == second[0] == start_ms, (first, second)
        assert second[1] - first[1] == 3_600_000, (first, second)
        fixed = db.CustomWindow(start_ms, start_ms + 1000, False)
        app.tab_range["tab-tools"] = fixed
        assert app._bounds_for("tab-tools") == (start_ms, start_ms + 1000)
    tui_tests.drive(lambda: tui.TrackerApp(str(tracker), clock=lambda: ticks[0]),
                  body, size=(92, 46))
