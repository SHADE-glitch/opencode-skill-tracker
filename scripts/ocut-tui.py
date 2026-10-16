#!/usr/bin/env python3
"""ocut-tui — interactive viewer for the OpenCode usage tracker.

Tabs: Dashboard · Tools · Skills · Agents · Plugins · MCP · Tokens · Usage.

Requires ``textual`` (see requirements.txt). The headless reports in
ocut-stats.py need no third-party packages; this file is the only place that
imports textual, so importing it without textual fails with a clear message
instead of a traceback.
"""

from __future__ import annotations

import argparse
import importlib.util
import sqlite3
import sys
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ocut_db as db  # noqa: E402

try:
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Grid, Horizontal, Vertical
    from textual.screen import ModalScreen, Screen
    from textual.widgets import (
        Button, Checkbox, DataTable, Footer, Input, Select, Static,
        TabbedContent, TabPane,
    )
    # `Text` is textual's own styling type and is already a hard dependency of
    # it, so importing it costs no extra install step -- it is named in
    # requirements.txt only because this file now imports it directly.
    from rich.text import Text

    HAVE_TEXTUAL = True
except ImportError:  # pragma: no cover - exercised only without the dep
    HAVE_TEXTUAL = False


def _plain(value) -> str:
    """What a cell renders as, without destroying how it renders."""
    if isinstance(value, Text):
        return value.plain
    return "-" if value is None else str(value)


def state_cell(row, kind):
    """The `state` cell for one entity row: a removed name is dimmed.

    Textual's `DataTable` has no per-row style hook -- `add_row` takes cells,
    and the row-level classes only cover the header, fixed rows and the zebra
    stripes -- so the emphasis lands on this one cell. A row is therefore not
    uniformly greyed, which reads as a deliberate choice rather than a missing
    one only if you know it.

    The word itself comes from `db.entity_state`, shared with the headless
    reports, so the two surfaces cannot drift into saying different things
    about the same row. `kind` is passed rather than looked up: the label for a
    builtin tool is not the label for a plugin, and only the kind knows which.
    """
    label = db.entity_state(row["installed"], row["ever_used"], kind)
    if label in (db.ENTITY_STATE_REMOVED, db.ENTITY_STATE_UNREPORTED):
        return Text(label, style="dim")
    return label


def ts(v):
    if not v:
        return "-"
    return datetime.fromtimestamp(v / 1000).strftime("%m-%d %H:%M")


def ms(v):
    if v is None:
        return "-"
    return f"{v}ms" if v < 1000 else f"{v / 1000:.1f}s"


def fmt_n(v):
    """Render a token count, showing ``-`` for NULL (never a fabricated 0)."""
    if v is None:
        return "-"
    return f"{v:,}"


def count_n(v, singular, plural=None):
    """``1 tool call`` / ``12 tool calls``, grouped like every count cell.

    D-005 fixed the plural in one place (Top tools) and left the rest: "1 skills"
    in the status bar and "Synced 1 files" survived it. Counts that read as prose
    are one class, so they agree here rather than being re-remembered per site.
    """
    word = singular if v == 1 else (plural or singular + "s")
    return f"{v:,} {word}"


def clip(text, width):
    """Fit ``text`` into ``width`` cells, marking a cut with an ellipsis.

    The Dashboard's leaderboard lines are column-formatted (``{:<26}``), so a
    name longer than its field does not wrap — it pushes every column after it.
    Observed at 41 characters. Clipping keeps the columns aligned; the full name
    stays in the table on that entity's own tab.
    """
    s = str(text)
    return s if len(s) <= width else s[: width - 1] + "…"


def local_time(ms):
    """Local-time render of an epoch-ms value (query bounds stay in Unix ms)."""
    if not ms:
        return "-"
    return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M:%S")


def _input_tokens(prompt, cache_read, cache_write):
    """The full prompt — CodeBuddy's ``Input`` = cache hit + miss + write.

    OpenCode reports an *uncached* ``prompt_tokens`` (which IS its cache miss)
    with ``cache_read``/``cache_write`` beside it, so the three together are the
    prompt the model saw and match CodeBuddy's ``Input`` column. ``None`` only
    when all three are absent, never a fabricated 0.
    """
    parts = (prompt, cache_read, cache_write)
    if all(p is None for p in parts):
        return None
    return sum(p or 0 for p in parts)


def _usage_total_tokens(prompt, completion, cache_read, cache_write):
    """Display-only ``Input + Output + cache hit`` — CodeBuddy's "Usage Total".

    A deliberate re-add of the cache hit, kept identical to CodeBuddy's panel so
    the two TUIs show the same number; it is never the API total
    (``total_tokens``), which is shown beside it.
    """
    inp = _input_tokens(prompt, cache_read, cache_write)
    if inp is None or completion is None:
        return None
    return inp + completion + (cache_read or 0)


# Usage-page option list (values mirror ocut_db so the two stay in sync).
# Windows are whole local calendar days ending now ("Today", "2 days", …).
# "Custom…" is the escape hatch to the date picker; it is never stored as a
# tab's range (a picked range is a db.CustomWindow instead).
RANGE_OPTIONS = [(db.USAGE_RANGE_LABELS[k], k)
                 for k in ("1d", "2d", "3d", "7d", "30d", "all")] + [
    ("Custom…", "custom")]
CUSTOM_RANGE = "custom"
# The Usage page is a single Request Logs list, one row per OpenCode
# `step-finish` part. The column set and labels are CodeBuddy's TUI verbatim:
# time · model · provider · usage total · input · output · API total ·
# cache hit/miss/write · cache hit rate. The cache vocabulary is CodeBuddy's,
# mapped onto OpenCode's fields — its uncached `prompt_tokens` is the cache miss,
# `cache_read` the hit, `cache_write` the write (see `_input_tokens`).
USAGE_COLUMNS = ("Time", "Model", "Provider", "Usage Total", "Input", "Output",
                 "API Total", "Cache hit", "Cache miss",
                 "Cache write", "Cache hit rate")

# Tokens-tab column order, the same set CodeBuddy's Tokens tab shows.
TOKEN_COLUMNS = ("Model", "Provider", "Usage Total", "Requests", "Input",
                 "Output", "API Total", "Cache hit", "Cache miss", "Cache write")

# Entity tabs and the two detail screens. Declared here rather than inline at
# `add_columns` because the column plan needs the full list to compute a prefix from,
# and a plan that cannot name the columns it dropped is a silent truncation wearing a
# guard's clothes.
#
# ``state`` is last, as in CodeBuddy's TUI: the prefix plan drops the tail first,
# so a narrow terminal loses "last used" before it loses the answer to "is this
# thing still installed".
_STATE = db.ENTITY_STATE_COLUMN
TOOLS_COLUMNS = ("tool", "calls", "ok", "fail", "avg", "last used", _STATE)
SKILLS_COLUMNS = ("skill", "calls", "ok", "last used", _STATE)
AGENTS_COLUMNS = ("agent", "calls", "last used", _STATE)
# `uses` is real — plugin/ocut-hook.js writes it — so 0 means "nothing recorded",
# never "not used". The three counts are what the plugin ships.
PLUGINS_COLUMNS = ("plugin", "uses", "skills", "agents", "cmds", _STATE)
MCP_COLUMNS = ("server", "tool", "calls", "ok", "last used", _STATE)
HISTORY_COLUMNS = ("when", "project", "session", "status", "duration")
# q_plugin_history selects from `plugin_usage` (the hook's sidecar), so one row
# here is one tool or command call the hook saw a plugin make. It carries the
# plugin's own `kind` and `item`, which HISTORY_COLUMNS (keyed by project) does not.
PLUGIN_HISTORY_COLUMNS = ("when", "kind", "item", "status", "duration",
                          "session", "project")
# Nine columns still need ~150 cells: on an 80-wide terminal more than half of this
# table used to sit past the right edge with no key that would bring it back.
RESPONSE_COLUMNS = ("when", "model", "prompt", "completion", "total",
                    "cache read", "cache write", "usage", "message_id")

# The single-call detail screen renders every column of the row. Labels live here
# rather than inline so the DB column name and what the screen calls it stay one
# mapping, and a column with no entry still shows (it falls back to its name).
CALL_DETAIL_LABELS = {
    "call_id": "call id", "tool_name": "tool", "tool": "tool",
    "agent_type": "agent", "child_session_id": "child session",
    "skill": "skill", "category": "category", "model": "model",
    "provider": "provider", "server": "server",
    "plugin": "plugin", "kind": "kind", "item": "item",
    "variant": "variant", "finish": "finish", "status": "status",
    "ts": "when", "duration_ms": "duration",
    "session_id": "session", "project": "project", "directory": "directory",
    "message_id": "message id",
    "prompt_tokens": "input tokens", "completion_tokens": "output tokens",
    "reasoning_tokens": "reasoning tokens",
    "cache_read": "cache read", "cache_write": "cache write",
    "prompt_total_tokens": "prompt total",
    "total_tokens": "api total", "total_tokens_source": "total source",
    "usage_available": "usage available", "missing": "missing fields",
}
_DETAIL_TIME_COLS = {"ts"}
_DETAIL_DURATION_COLS = {"duration_ms"}
_DETAIL_BOOL_COLS = {"usage_available"}
_DETAIL_TOKEN_COLS = {
    "prompt_tokens", "completion_tokens", "reasoning_tokens",
    "cache_read", "cache_write", "prompt_total_tokens", "total_tokens",
}


def _call_detail_rows(row):
    """``(label, text)`` for every field of one call row, in row order.

    NULL renders as "-" (never a fabricated 0); a timestamp, a duration and the
    yes/no flags are formatted, tokens are grouped. An unknown column falls back
    to its name, so a column added to the query still appears.
    """
    out = []
    for col, value in row.items():
        label = CALL_DETAIL_LABELS.get(col, col.replace("_", " "))
        if value is None:
            text = "-"
        elif col in _DETAIL_TIME_COLS:
            text = local_time(value)
        elif col in _DETAIL_DURATION_COLS:
            text = ms(value)
        elif col in _DETAIL_BOOL_COLS:
            text = "yes" if value else "no"
        elif col in _DETAIL_TOKEN_COLS:
            text = fmt_n(value)
        else:
            text = str(value)
        out.append((label, text))
    return out


if HAVE_TEXTUAL:

    class TopBar(Horizontal):
        """标题栏 + 共享时间范围选择器（取代 textual 的 Header）。

        去掉两个内置行为：点击不再切换 'tall' 高度；没有左上角图标，不会打开命令面板。
        只有主界面带选择器；详情屏用 with_range=False。
        """
        DEFAULT_CSS = """
        TopBar { dock: top; width: 100%; height: 1; background: $panel; color: $foreground; }
        TopBar > #topbar-title { width: 1fr; padding: 0 1; content-align: left middle;
                                 text-wrap: nowrap; text-overflow: ellipsis; }
        TopBar > #date-range { width: auto; min-width: 0; height: 1; border: none;
                               padding: 0 1; background: $panel; color: $foreground; }
        TopBar > #date-range:hover { background: $boost; }
        TopBar > #range { width: 30; }
        TopBar > #range SelectCurrent { border: none; padding: 0 1; background: $panel; }
        """

        def __init__(self, title: str, *, with_range: bool = False,
                     range_value: str = "7d", id: str | None = None):
            super().__init__(id=id)
            self._title = title
            self._with_range = with_range
            self._range_value = range_value

        def compose(self) -> ComposeResult:
            yield Static(self._title, id="topbar-title")
            if self._with_range:
                # A compact 📅 control showing the active tab's window; pressing it
                # opens the date picker (same as picking "Custom…" in the Select).
                yield Button("📅", id="date-range")
                yield Select(RANGE_OPTIONS, value=self._range_value,
                             allow_blank=False, id="range")

        def set_range_label(self, text: str) -> None:
            """Show the active tab's window on the 📅 control ('' hides it)."""
            try:
                btn = self.query_one("#date-range", Button)
            except Exception:
                return
            btn.label = f"📅 {text}" if text else "📅"
            btn.display = bool(text)
            # A Button does not re-lay-out when its label grows, so the control
            # kept the width it was built with ("📅 Today" = 10 cells) and cut
            # the longer span down to "📅 10-06" until the next full relayout.
            btn.refresh(layout=True)

    class TimeInput(Input):
        """A clock field you can scrub as well as type.

        Typing is the fast path (``00:00`` and ``9:5 PM`` are both understood);
        ↑/↓ and the mouse wheel step the value so it can be nudged without the
        keyboard. The step is a per-instance count of minutes, set by the
        picker's step selector, so one key press can move a minute, ten, or an
        hour. The value is re-rendered through ``db.format_time``, so what stays
        in the field is always the canonical 24-hour ``HH:MM``.
        """

        BINDINGS = [
            Binding("up", "nudge_up", "Later", show=False),
            Binding("down", "nudge_down", "Earlier", show=False),
        ]
        STEP = 1  # minutes per nudge, until the picker's step selector says otherwise

        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self.step = self.STEP

        def action_nudge_up(self) -> None:
            self._nudge(self.step)

        def action_nudge_down(self) -> None:
            self._nudge(-self.step)

        def on_mouse_scroll_up(self, event) -> None:
            self._nudge(self.step)
            event.stop()

        def on_mouse_scroll_down(self, event) -> None:
            self._nudge(-self.step)
            event.stop()

        def _nudge(self, minutes: int) -> None:
            try:
                hour, minute = db.parse_time(self.value)
            except ValueError:
                # An empty or half-typed field nudges from *now*, so the first
                # ↑/↓ yields a valid time instead of an error.
                now = datetime.now()
                hour, minute = now.hour, now.minute
            total = (hour * 60 + minute + minutes) % (24 * 60)
            self.value = db.format_time(total // 60, total % 60)
            self.cursor_position = len(self.value)

    class DateRangeModal(ModalScreen):
        """The custom date-range picker: preset chips, two cards, two grids.

        Left column: the shortcut chips, an editable START TIME and END TIME (a
        date plus a 24-hour time each), a quick-time row under each card, a
        nudge-step selector and the "end follows now" switch. Right column: a
        month calendar whose ‹ › step the month (and, at the year's ends, the
        year), with a 24-cell hour grid below it. Clicking a card makes it
        active; a day or an hour then writes into the active card — start and
        end are picked the same way, and the calendar and the grid are the two
        halves of one clock.

        Opened from the top bar's 📅 control or the Select's "Custom…". Confirm
        dismisses with a ``db.CustomWindow``; Cancel or Esc dismisses with
        ``None``. A blank start date means all time.
        """

        BINDINGS = [Binding("escape", "cancel", "Cancel")]
        CELLS = 42                       # 6 rows × 7 columns covers any month
        DOW = ("S", "M", "T", "W", "T", "F", "S")
        # (chip label, widget id, preset key). "Today" and "1d" both name the
        # single whole calendar day that our window model calls "1d".
        PRESETS = (
            ("Today", "dr-today", "1d"),
            ("1d", "dr-1d", "1d"),
            ("7d", "dr-7d", "7d"),
            ("14d", "dr-14d", "14d"),
            ("30d", "dr-30d", "30d"),
            ("All time", "dr-all", "all"),
        )
        # The quick-time row under each card. The start card offers the day's
        # edges and the common halves; the end card also offers ``now``, which
        # is the same thing as ticking "follows current time" — the two ends of
        # the same choice, one click each.
        QUICK_START = ("00:00", "06:00", "12:00", "18:00", "23:59")
        QUICK_END = ("now", "00:00", "12:00", "23:59")
        # The nudge selector: (label, minutes). ↑/↓ and the wheel move by the
        # selected step, so a day's edge is reachable without typing.
        STEPS = (("1m", 1), ("10m", 10), ("1h", 60))
        HOURS = 24

        DEFAULT_CSS = """
        DateRangeModal { align: center middle; overflow: auto; }
        /* Width budget, so the whole picker fits an 80-column terminal:
           1 border + 1 padding + 53 (left column, of which the six preset chips
           use 52) + 1 margin + 21 (7 day cells of 3, and 6 hour cells of 3) +
           1 padding + 1 border = 79. The day and hour cells are Statics, not
           Buttons: a Button renders a two-digit label as one digit at width 3. */
        #dr-box { width: auto; height: auto; border: round $primary;
                  background: $surface; padding: 1 1; }
        #dr-left { width: 53; height: auto; }
        #dr-presets { height: auto; }
        .dr-card { border: round $panel; height: auto; padding: 0 1; margin-top: 1; }
        .dr-card.-active { border: round $accent; background: $boost; }
        .dr-card-title { color: $text-muted; text-style: bold; }
        .dr-fields { height: auto; }
        #dr-start-date, #dr-end-date { width: 1fr; margin: 0 1 0 0; }
        /* An Input spends 6 cells on its border and its 0-2 padding, so 16 is
           what a 5-cell "23:59" needs with room to type over it. */
        #dr-start-time, #dr-end-time { width: 16; }
        /* The quick-time rows: one cell tall, so five chips + four chips cost
           two rows rather than six. 5 × (5 + 2 padding + 1 margin) = 40 ≤ 53. */
        #dr-start-quick, #dr-end-quick { height: auto; }
        #dr-follow { height: auto; }
        #dr-nudge { height: auto; }
        /* A bare Static in a Horizontal takes the whole row and pushes the
           buttons past the column (and off the modal), so the label is pinned
           to its own cells. */
        #dr-nudge .dr-card-title { width: 7; }
        #dr-actions { height: auto; align-horizontal: right; }
        #dr-cal { width: 21; height: auto; margin-left: 1; }
        #dr-cal-head { height: auto; align: center middle; }
        #dr-cal-month { width: 1fr; text-align: center; text-style: bold; }
        #dr-cal-grid { grid-size: 7; grid-gutter: 0; height: auto; }
        /* 6 columns of 3 = 18 of the 21 available, so the 24 hours land in 4
           rows under the month. */
        #dr-hour-grid { grid-size: 6; grid-gutter: 0; height: auto; margin-top: 1; }
        /* Every rule that touches a Button is scoped by an id: Button's own
           `Button:ansi.-style-default` selector outranks a bare class, so
           `.dr-day { border: none }` silently lost and the calendar rendered
           with tall borders and no room for the number. */
        #dr-presets Button { width: auto; min-width: 0; height: 3; border: none;
                             padding: 0 1; margin: 0 1 0 0; background: $boost;
                             color: $foreground; }
        #dr-start-quick Button, #dr-end-quick Button {
                             width: auto; min-width: 0; height: 1; border: none;
                             padding: 0 1; margin: 0 1 0 0; background: $boost;
                             color: $foreground; }
        #dr-nudge Button { width: auto; min-width: 0; height: 1; border: none;
                           padding: 0 1; margin: 0 1 0 0; background: $boost;
                           color: $foreground; }
        /* -selected, not -active: Button uses -active for its own 0.2s pressed
           effect and strips it on a timer, so the chosen step flickered off. */
        #dr-nudge Button.-selected { background: $accent; color: $text;
                                     text-style: bold; }
        #dr-actions Button { width: auto; min-width: 0; margin-left: 1; }
        #dr-cal-head Button { width: auto; min-width: 0; border: none;
                              background: $surface; color: $foreground; }
        #dr-cal-grid .dr-dow { text-align: center; color: $text-muted; }
        /* 3, not 4: a Static renders "30" at width 3, where a Button shows " 3 ".
           The clicks are read from the cell id in on_click. */
        #dr-cal-grid .dr-day { width: 3; height: 1; text-align: center;
                               background: $surface; color: $foreground; }
        #dr-cal-grid .dr-day.-other { color: $text-disabled; }
        #dr-cal-grid .dr-day.-today { color: $accent; text-style: bold; }
        #dr-cal-grid .dr-day.-selected { background: $accent; color: $text;
                                         text-style: bold; }
        #dr-hour-grid .dr-hour { width: 3; height: 1; text-align: center;
                                 background: $surface; color: $foreground; }
        #dr-hour-grid .dr-hour.-selected { background: $accent; color: $text;
                                           text-style: bold; }
        """

        def __init__(self, current, now_ms: int, id: str | None = None):
            super().__init__(id=id)
            self._current = current
            self._now = now_ms
            self._active = "start"       # which card the calendar writes into
            self._step = self.STEPS[0][1]   # minutes per ↑/↓ nudge
            self._view = datetime.fromtimestamp(now_ms / 1000).date().replace(day=1)
            # What we last wrote into each field, so a *user* edit can be told
            # from our own prefill/preset write (both post Input.Changed).
            self._written = {}

        def compose(self) -> ComposeResult:
            with Horizontal(id="dr-box"):
                with Vertical(id="dr-left"):
                    with Horizontal(id="dr-presets"):
                        for label, wid, _key in self.PRESETS:
                            yield Button(label, id=wid)
                    with Vertical(id="dr-start-card", classes="dr-card"):
                        yield Static("START TIME", classes="dr-card-title")
                        with Horizontal(classes="dr-fields"):
                            yield Input(placeholder="MM/DD/YYYY", id="dr-start-date")
                            yield TimeInput(placeholder="00:00", id="dr-start-time")
                        with Horizontal(id="dr-start-quick"):
                            for i, label in enumerate(self.QUICK_START):
                                yield Button(label, id=f"dr-start-quick-{i}")
                    with Vertical(id="dr-end-card", classes="dr-card"):
                        yield Static("END TIME", classes="dr-card-title")
                        with Horizontal(classes="dr-fields"):
                            yield Input(placeholder="MM/DD/YYYY", id="dr-end-date")
                            yield TimeInput(placeholder="00:00", id="dr-end-time")
                        with Horizontal(id="dr-end-quick"):
                            for i, label in enumerate(self.QUICK_END):
                                yield Button(label, id=f"dr-end-quick-{i}")
                    yield Checkbox("End time follows current time", True, id="dr-follow")
                    with Horizontal(id="dr-nudge"):
                        yield Static("step", classes="dr-card-title")
                        for label, minutes in self.STEPS:
                            yield Button(label, id=f"dr-step-{minutes}")
                    with Horizontal(id="dr-actions"):
                        yield Button("Cancel", id="dr-cancel")
                        yield Button("Confirm", variant="primary", id="dr-ok")
                with Vertical(id="dr-cal"):
                    with Horizontal(id="dr-cal-head"):
                        yield Button("‹", id="dr-prev")
                        yield Static("", id="dr-cal-month")
                        yield Button("›", id="dr-next")
                    with Grid(id="dr-cal-grid"):
                        for dow in self.DOW:
                            yield Static(dow, classes="dr-dow")
                        for i in range(self.CELLS):
                            # A Static, not a Button: a Button renders the
                            # two-digit label "30" as " 3 " at a 3-cell width,
                            # and widening every cell to 4 would not fit the
                            # calendar beside the cards. The day is read from
                            # the cell id in on_click instead.
                            yield Static("", id=f"dr-day-{i}", classes="dr-day")
                    with Grid(id="dr-hour-grid"):
                        for h in range(self.HOURS):
                            yield Static(f"{h:02d}", id=f"dr-hour-{h}",
                                         classes="dr-hour")

        def on_mount(self) -> None:
            self._prefill()
            # The reference popup's "Supports both date and time" note, on the
            # box's bottom border so it costs no extra row.
            self.query_one("#dr-box").border_subtitle = "Supports both date and time"
            self._set_active("start")
            self._render_steps()

        # -- prefill from the tab's current window --------------------------
        def _prefill(self) -> None:
            cur = self._current
            follow = True
            if isinstance(cur, db.CustomWindow):
                start, end, follow = cur.start_ms, cur.end_ms, cur.follow_now
            else:
                start, end = db.window_bounds(cur, self._now)
            self._set_field("#dr-start", start)
            # A following end shows *now*; a fixed one shows the stored bound.
            self._set_field("#dr-end", self._now if follow else end)
            self.query_one("#dr-follow", Checkbox).value = follow
            anchor = start if start is not None else self._now
            self._view = datetime.fromtimestamp(anchor / 1000).date().replace(day=1)

        def _set_field(self, prefix: str, ms) -> None:
            date_in = self.query_one(f"{prefix}-date", Input)
            time_in = self.query_one(f"{prefix}-time", Input)
            if ms is None:
                date_in.value = time_in.value = ""
            else:
                dt = datetime.fromtimestamp(ms / 1000)
                date_in.value = db.format_date(dt)
                time_in.value = db.format_time(dt.hour, dt.minute)
            self._written[date_in.id] = date_in.value
            self._written[time_in.id] = time_in.value
            self._render_hours()

        def _follows_now(self) -> bool:
            return self.query_one("#dr-follow", Checkbox).value

        # -- the calendar ----------------------------------------------------
        def _grid_start(self) -> date:
            """The date in cell 0: the first day of the view, back to Sunday."""
            lead = (self._view.weekday() + 1) % 7      # Sunday-first column
            return self._view - timedelta(days=lead)

        def _day_at(self, index: int) -> date:
            return self._grid_start() + timedelta(days=index)

        def _active_date(self):
            """The active card's typed date, or ``None`` if it is blank/bad."""
            try:
                return db.parse_date(
                    self.query_one(f"#dr-{self._active}-date", Input).value,
                    self._view.year).date()
            except ValueError:
                return None

        def _render_calendar(self) -> None:
            today = datetime.fromtimestamp(self._now / 1000).date()
            chosen = self._active_date()
            self.query_one("#dr-cal-month", Static).update(
                self._view.strftime("%B %Y"))
            for i in range(self.CELLS):
                day = self._day_at(i)
                cell = self.query_one(f"#dr-day-{i}", Static)
                cell.update(str(day.day))
                cell.set_class(day.month != self._view.month, "-other")
                cell.set_class(day == today, "-today")
                cell.set_class(day == chosen, "-selected")

        def _step_month(self, delta: int) -> None:
            month = self._view.month + delta
            year = self._view.year + (month - 1) // 12
            self._view = date(year, (month - 1) % 12 + 1, 1)
            self._render_calendar()

        # -- the hour grid ---------------------------------------------------
        def _active_time(self):
            """The active card's typed time as ``(hour, minute)``, or ``None``."""
            try:
                return db.parse_time(
                    self.query_one(f"#dr-{self._active}-time", Input).value)
            except ValueError:
                return None

        def _render_hours(self) -> None:
            """Mark the active card's hour in the 24-cell grid."""
            parsed = self._active_time()
            hour = parsed[0] if parsed else None
            for h in range(self.HOURS):
                self.query_one(f"#dr-hour-{h}", Static).set_class(
                    h == hour, "-selected")

        def _pick_hour(self, hour: int) -> None:
            """Write a clicked hour into the active card, keeping its minute.

            The grid is the calendar's other half: the day cells set the date,
            these set the hour, and both land in whichever card is active.
            """
            parsed = self._active_time()
            minute = parsed[1] if parsed else 0
            self._write_time(self._active, db.format_time(hour, minute))

        def _write_time(self, card: str, value: str) -> None:
            """Set one card's time field, and fix the end if that is the card."""
            if card == "end":
                # A chosen end time means a fixed end, not "now".
                self.query_one("#dr-follow", Checkbox).value = False
            self.query_one(f"#dr-{card}-time", Input).value = value
            self._written[f"dr-{card}-time"] = value
            self._render_hours()

        def _pick_day(self, index: int) -> None:
            """Write a clicked day into the active card."""
            day = self._day_at(index)
            if self._active == "end":
                # Choosing an end date means a fixed end, not "now".
                self.query_one("#dr-follow", Checkbox).value = False
            self.query_one(f"#dr-{self._active}-date", Input).value = db.format_date(day)
            self._written[f"dr-{self._active}-date"] = db.format_date(day)
            if (day.year, day.month) != (self._view.year, self._view.month):
                self._view = day.replace(day=1)
            self._render_calendar()

        def _set_active(self, which: str) -> None:
            self._active = which
            self.query_one("#dr-start-card").set_class(which == "start", "-active")
            self.query_one("#dr-end-card").set_class(which == "end", "-active")
            self._render_calendar()
            self._render_hours()

        # -- presets ---------------------------------------------------------
        def _apply_preset(self, key: str) -> None:
            start, _ = db.window_bounds(key, self._now)
            self._set_field("#dr-start", start)
            self.query_one("#dr-follow", Checkbox).value = True
            if start is not None:
                self._view = datetime.fromtimestamp(start / 1000).date().replace(day=1)
            self._render_calendar()

        def _apply_quick(self, card: str, label: str) -> None:
            """A quick-time chip: ``now`` re-follows the clock, a time fixes it."""
            if label == "now":
                self._set_field("#dr-end", self._now)
                self.query_one("#dr-follow", Checkbox).value = True
                self._render_hours()
                return
            hour, minute = db.parse_time(label)
            self._write_time(card, db.format_time(hour, minute))

        # -- nudge step ------------------------------------------------------
        def _set_step(self, minutes: int) -> None:
            self._step = minutes
            for wid in ("#dr-start-time", "#dr-end-time"):
                self.query_one(wid, TimeInput).step = minutes
            self._render_steps()

        def _render_steps(self) -> None:
            for _label, minutes in self.STEPS:
                self.query_one(f"#dr-step-{minutes}", Button).set_class(
                    minutes == self._step, "-selected")

        # -- confirm ---------------------------------------------------------
        def _confirm(self) -> None:
            follow = self._follows_now()
            year = self._view.year
            try:
                start = db.parse_datetime(
                    self.query_one("#dr-start-date", Input).value,
                    self.query_one("#dr-start-time", Input).value, year)
                end = None if follow else db.parse_datetime(
                    self.query_one("#dr-end-date", Input).value,
                    self.query_one("#dr-end-time", Input).value, year)
            except ValueError:
                self.notify("Dates are MM/DD/YYYY, times HH:MM (24-hour)",
                            severity="error")
                return
            if not follow and end is None:
                self.notify("Set an end date, or tick 'End follows current time'",
                            severity="error")
                return
            if start is not None and end is not None and end < start:
                self.notify("End is before start", severity="error")
                return
            self.dismiss(db.CustomWindow(start, end, follow))

        def action_cancel(self) -> None:
            self.dismiss(None)

        # -- events ----------------------------------------------------------
        def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
            if event.checkbox.id == "dr-follow" and event.value:
                # Ticking "follows current time" puts the end back on now.
                self._set_field("#dr-end", self._now)

        def on_input_changed(self, event: Input.Changed) -> None:
            # A *user* edit of an end field means a fixed end, so drop the
            # follow switch. Our own prefill/preset writes are ignored by
            # matching the value we recorded.
            wid = event.input.id or ""
            if wid.endswith("-time"):
                self._render_hours()      # a typed hour re-marks the grid
            if not wid.startswith("dr-end") or not self._follows_now():
                return
            if self._written.get(wid) == event.value:
                return
            self.query_one("#dr-follow", Checkbox).value = False

        def on_click(self, event) -> None:
            # A calendar day and an hour cell are Statics, so their clicks
            # arrive here rather than as a Button.Pressed — read the index from
            # the cell's id.
            wid = getattr(event.widget, "id", "") or ""
            if wid.startswith("dr-day-"):
                self._pick_day(int(wid.rsplit("-", 1)[1]))
                return
            if wid.startswith("dr-hour-"):
                self._pick_hour(int(wid.rsplit("-", 1)[1]))
                return
            # Clicking anywhere on a card makes it the calendar's target.
            node = event.widget
            while node is not None:
                if getattr(node, "has_class", None) and node.has_class("dr-card"):
                    self._set_active(
                        "start" if node.id == "dr-start-card" else "end")
                    return
                node = getattr(node, "parent", None)

        def on_descendant_focus(self, event) -> None:
            wid = getattr(event.control, "id", "") or ""
            if wid.startswith("dr-start"):
                self._set_active("start")
            elif wid.startswith("dr-end"):
                self._set_active("end")

        def on_button_pressed(self, event: Button.Pressed) -> None:
            bid = event.button.id or ""
            preset = next((key for _l, wid, key in self.PRESETS if wid == bid), None)
            if preset is not None:
                self._apply_preset(preset)
            elif bid.startswith("dr-start-quick-"):
                self._apply_quick("start", self.QUICK_START[int(bid.rsplit("-", 1)[1])])
            elif bid.startswith("dr-end-quick-"):
                self._apply_quick("end", self.QUICK_END[int(bid.rsplit("-", 1)[1])])
            elif bid.startswith("dr-step-"):
                self._set_step(int(bid.rsplit("-", 1)[1]))
            elif bid == "dr-prev":
                self._step_month(-1)
            elif bid == "dr-next":
                self._step_month(1)
            elif bid == "dr-cancel":
                self.dismiss(None)
            elif bid == "dr-ok":
                self._confirm()


    class WideTableMixin:
        """The narrow-terminal column plan, shared by every host that owns a table.

        A rule that covers two of the nine tables is not a rule, so the mechanism
        lives here and each host declares its own ``WIDE_TABLES``. ``_wide_rows`` is
        created by each host's ``__init__`` — it must not be a class attribute, or two
        open screens would share one row cache.

        The numbers a host registers are measured on this machine, not derived: a column
        costs its content width plus a 2-cell gutter (``virtual_size.width ==
        sum(content_width) + 2 * n_columns``, checked on every table), and a table loses
        a fixed number of cells to its surroundings — 2 inside a tab pane, 4 on the
        padded Usage page, 0 on a pushed screen that spans the terminal.
        """

        CELL_COST = 2
        MIN_SHOWN = 2      # never narrow a table down to a single unnamed column
        # Measured on this machine's database, not guessed: every entity name fits
        # 28 cells (tool 22, model 24, agent 20, plugin 19, mcp tool 23, skill 28,
        # command 31 once) — the only column that runs past it is the project path,
        # where 162 of 482 rows exceed 24 and the longest is 74. So a cap at 28
        # costs no name and buys back the 46 cells a path was spending.
        NAME_CAP = 28

        @property
        def name_cap(self) -> int:
            """The cap this host actually renders with.

            ``NAME_CAP`` is the shipped default, which the app shadows from the
            settings file. A screen pushed on top of the app has no config of its
            own, so it overrides this to read the app — otherwise a configured cap
            would silently apply to the tabs and not to the detail screens.
            """
            return self.NAME_CAP

        def _cap_cell(self, text):
            """Fit one cell to ``name_cap``, keeping the half that carries meaning.

            A path's beginning is the shared prefix (`/home/user/…` repeated down
            the column) and its end is what distinguishes the rows, so paths are
            capped from the left. Everything else — a tool, a model, a skill — is
            read from the start, so it is capped from the right.

            A styled `Text` is capped on its plain text and rebuilt with the style
            it came in with. `len()` on a `Text` is the plain length already, but
            the slicing is not: truncating the renderable would drop the style along
            with the tail, and `name_cap` is 28 against a "removed" label of 7, so
            this path is only ever reached by a long path or an unusually small cap.
            """
            styled = isinstance(text, Text)
            plain = text.plain if styled else text
            cap = self.name_cap
            if len(plain) <= cap:
                return text
            keep = cap - 1
            clipped = ("…" + plain[-keep:]) if "/" in plain else (plain[:keep] + "…")
            return Text(clipped, style=text.style) if styled else clipped

        def _fill(self, table, rows, key_index=0, key_kind=None, key_rows=None):
            # Remember the highlighted row by its ROW KEY, not by index: a
            # re-sort between refreshes must not move the highlight onto a
            # different entity (index-based restore would).
            cur_key = None
            cur = table.cursor_row
            if key_kind and table.row_count:
                try:
                    cur_key = table.coordinate_to_cell_key(
                        table.cursor_coordinate).row_key
                except Exception:
                    cur_key = None
            table.clear()
            n = 0
            for i, r in enumerate(rows):
                # NULL renders as "-", matching fmt_n (never a blank cell).
                # `Text` is a renderable and must survive to `add_row`; `str()`
                # on it would hand Textual a plain string and the style with it.
                # Testing for `str` alone is the silent version of this bug:
                # `Text` is not a `str` subclass, so it would be flattened
                # while every assertion on the label text still passed.
                cells = [(c if isinstance(c, (str, Text))
                          else ("-" if c is None else str(c))) for c in r]
                key = None
                if key_kind:
                    # key_index may be a single index or a tuple of indices for
                    # a composite (unique) key, e.g. mcp -> (server, tool).
                    # ``key_rows`` carries the UNCAPTED values: two entities whose
                    # names share the visible prefix render identically and must
                    # still route apart, so the key cannot come from the cell.
                    idx = (key_index if isinstance(key_index, (tuple, list))
                           else (key_index,))
                    src = key_rows[i] if key_rows is not None else r
                    key = "\t".join([key_kind] + [str(src[j]) for j in idx])
                table.add_row(*cells, key=key)
                n += 1
            if cur_key is not None and cur_key in table.rows:
                table.move_cursor(row=table.get_row_index(cur_key))
            elif not key_kind and 0 <= cur < n:
                # Unkeyed tables (e.g. the Request Logs list) keep the old
                # index-based restore.
                table.move_cursor(row=cur)

        def _fill_or_empty(self, table, rows, empty_msg, key_index=0,
                           key_kind=None, key_rows=None):
            """Fill a table, or show an explicit empty-state row.

            Mirrors ``_fill_tokens``: an empty tab must never render a blank
            region. The empty row carries no key, so selecting it is a no-op.
            """
            if rows:
                self._fill(table, rows, key_index=key_index, key_kind=key_kind,
                           key_rows=key_rows)
            else:
                table.clear()
                table.add_row(empty_msg, *(["-"] * (len(table.columns) - 1)))

        @staticmethod
        def _column_widths(labels, rows):
            """What DataTable will charge for each column: label or widest cell.

            ``_plain`` rather than ``str()``: `str(Text)` returns the plain text
            today, but it is the one conversion that flattens a styled cell if
            anything ever wraps it, and a width measured off a flatten is a width
            measured off a lie.
            """
            return [max(len(str(label)),
                        max((len(_plain(r[i])) for r in rows), default=0))
                    for i, label in enumerate(labels)]

        def _set_columns(self, table, cols) -> None:
            if [str(c.label) for c in table.columns.values()] == list(cols):
                return
            table.clear(columns=True)
            table.add_columns(*cols)

        def _fill_wide(self, table_id, rows, empty_msg, key_index=0, key_kind=None,
                       width=None, key_rows=None):
            """Fill a wide table with the longest prefix of columns that fits.

            Prefix, not priority re-ordering: both tables are already ordered most
            essential first (name, total, then the cache detail), so dropping the
            tail is the plan. The cells past the right edge used to be cut
            mid-label with no keyboard way to reach them.

            ``key_rows`` carries the un-truncated values a key is built from when
            the key is not among the visible cells — the drill-down's ``call_id``
            rides there, so a row can open its own detail without spending a
            column on it. It defaults to the visible rows.
            """
            table = self.query_one(f"#{table_id}", DataTable)
            labels, note_id, chrome = self.WIDE_TABLES[table_id]
            self._wide_rows[table_id] = (rows, empty_msg, key_index, key_kind,
                                         key_rows)
            note = self.query_one(f"#{note_id}", Static)
            width = width or self.size.width or table.region.width
            if not width or not rows:
                # Either the first layout has not happened yet (both are 0, and
                # planning against 0 would drop everything) or there is nothing to
                # protect: an empty-state row spans the whole label set.
                self._set_columns(table, labels)
                self._fill_or_empty(table, rows, empty_msg, key_index, key_kind,
                                    key_rows=key_rows)
                note.update("")
                note.display = False
                if not width:
                    self.call_after_refresh(self._plan_wide_tables)
                return
            # Same rule as `_fill`: a renderable passes through, everything else is
            # stringified here so `_cap_cell` and `_column_widths` see one type.
            raw = [[(c if isinstance(c, (str, Text))
                     else ("-" if c is None else str(c))) for c in r]
                   for r in rows]
            shown = [[self._cap_cell(c) for c in rr] for rr in raw]
            capped = any(a != b for rr, ss in zip(raw, shown) for a, b in zip(rr, ss))
            full = self._column_widths(labels, raw)
            budget = width - chrome
            if sum(full) + self.CELL_COST * len(labels) <= budget:
                # Everything fits at its real length, so the cap stays out: a wide
                # terminal has no reason to lose characters, and "widen to N" would
                # otherwise promise a number that buys nothing.
                self._set_columns(table, labels)
                self._fill_or_empty(table, raw, empty_msg, key_index, key_kind,
                                    key_rows=key_rows)
                note.update("")
                note.display = False
                return
            widths = self._column_widths(labels, shown)
            keep = len(labels)
            while keep > self.MIN_SHOWN and sum(widths[:keep]) + self.CELL_COST * keep > budget:
                keep -= 1
            self._set_columns(table, labels[:keep])
            self._fill_or_empty(table, [r[:keep] for r in shown],
                                empty_msg, key_index, key_kind,
                                key_rows=key_rows if key_rows is not None else rows)
            cap_note = f" · cells capped at {self.name_cap}" if capped else ""
            if keep >= len(labels):
                # Nothing is hidden, so the only thing worth saying is that
                # characters were cut — and the full value only comes back at the
                # width named below, which is why it has to be said out loud.
                note.update(f"cells capped at {self.name_cap}" if capped else "")
                note.display = capped
                return
            cuts = sum(widths[:keep]) + self.CELL_COST * keep > budget
            note.update(f"{keep} of {len(labels)} columns · hidden: "
                        f"{', '.join(labels[keep:])} · widen to "
                        f"{sum(full) + self.CELL_COST * len(labels) + chrome}"
                        + (" · even these are cut" if cuts else "")
                        + cap_note)
            note.display = True

        def _plan_wide_tables(self, width=None) -> None:
            """Re-plan from the cached rows — a resize must not re-query."""
            for table_id, (rows, empty_msg, key_index, key_kind,
                           key_rows) in list(self._wide_rows.items()):
                self._fill_wide(table_id, rows, empty_msg,
                                key_index=key_index, key_kind=key_kind,
                                width=width, key_rows=key_rows)

    class HistoryScreen(WideTableMixin, Screen):
        """Recent calls for one entity in a window, pushed on row select.

        The header summarises the entity (its counts in the same window); the
        table lists the calls. Selecting a call pushes its full detail.
        """

        BINDINGS = [Binding("escape,q", "app.pop_screen", "Back")]
        # A pushed screen spans the terminal, so its table loses no cells to its
        # surroundings — measured: region width == terminal width at 60/80/100.
        WIDE_TABLES = {"hist-table": (HISTORY_COLUMNS, "colnote-hist", 0)}

        @property
        def name_cap(self) -> int:
            return self.app.name_cap

        def __init__(self, kind: str, name: str, display: str | None = None,
                     start_ts=None, end_ts=None, window_label: str = "all time",
                     server: str | None = None):
            super().__init__()
            self.kind = kind
            self.entity = name              # the value queried against the DB
            self.heading = display or name  # what the title shows
            self.start_ts = start_ts        # the tab's window; None = all-time
            self.end_ts = end_ts
            self.window_label = window_label
            # An MCP row is a (server, tool) pair and the table column is the tool,
            # so the server has to travel with the screen or the history of one row
            # would be the history of both.
            self.server = server
            # A plugin's history names the tool instead of the project, so it is
            # the one drill-down with its own column set.
            self._columns = (PLUGIN_HISTORY_COLUMNS if kind == "plugin"
                             else HISTORY_COLUMNS)
            self._wide_rows = {}
            # Instance registry: the mixin reads ``self.WIDE_TABLES``, so a
            # per-kind column set overrides the class default. The class attribute
            # stays put for the "every table is registered" guard.
            self.WIDE_TABLES = {"hist-table": (self._columns, "colnote-hist", 0)}

        def compose(self) -> ComposeResult:
            yield TopBar(f"{self.kind} · {self.heading}")
            yield Vertical(
                Static(f"[b]{self.kind}[/b] · {self.heading}", id="hist-title"),
                Static(id="hist-summary", classes="entity-summary"),
                Static(id="colnote-hist", classes="col-note"),
                DataTable(id="hist-table", zebra_stripes=True),
            )
            yield Footer()

        def on_mount(self) -> None:
            self.title = f"{self.kind}: {self.heading}"
            self.query_one("#hist-table", DataTable).cursor_type = "row"
            conn = db.open_db(self.app.db_path, readonly=True)
            try:
                if self.kind == "plugin":
                    found = db.q_plugin_history(
                        conn, self.entity, self.app.DETAIL_LIMIT,
                        start_ts=self.start_ts, end_ts=self.end_ts)
                    rows = [[ts(r["ts"]), r["kind"] or "-", r["item"] or "-",
                             r["status"] or "-", ms(r["duration_ms"]),
                             (r["session_id"] or "")[:8], r["project"] or "-"]
                            for r in found]
                    # A plugin's history rows come out of `plugin_usage`, our own
                    # table, so the detail they open is a "plugin" record keyed by
                    # its own call_id — not a tool_calls row (the host names no
                    # plugin, so a tool call cannot be attributed back).
                    keys = [["plugin", r["call_id"]] for r in found]
                    summary = db.q_plugin_summary(
                        conn, self.entity, start_ts=self.start_ts,
                        end_ts=self.end_ts)
                else:
                    found = db.q_history(
                        conn, self.kind, self.entity, self.app.DETAIL_LIMIT,
                        start_ts=self.start_ts, end_ts=self.end_ts,
                        server=self.server)
                    rows = [[ts(r["ts"]), r["project"] or "-",
                             (r["session_id"] or "")[:8], r["status"] or "-",
                             ms(r["duration_ms"])]
                            for r in found]
                    keys = [[self.kind, r["call_id"]] for r in found]
                    summary = db.q_entity_summary(
                        conn, self.kind, self.entity, start_ts=self.start_ts,
                        end_ts=self.end_ts, server=self.server)
            finally:
                conn.close()
            self.query_one("#hist-summary", Static).update(
                self._summary_line(summary))
            # The call key rides as the row key (index 1), not as a column, so a
            # selected call can open its own detail.
            self._fill_wide(
                "hist-table", rows,
                ("No plugin calls recorded by the hook" if self.kind == "plugin"
                 else "No calls recorded for this name"),
                key_index=(0, 1), key_kind="call", key_rows=keys)

        def _summary_line(self, s) -> str:
            """One line of the entity's windowed counts, for the header."""
            if self.kind == "plugin":
                if not s["uses"]:
                    # Not "no uses": usage comes from plugin/ocut-hook.js, and a 0
                    # means the hook recorded nothing for this plugin — it may be
                    # unused, or its surface may be one the hook's static scan
                    # cannot parse. Name the evidence that is absent.
                    return (f"no calls recorded by the hook · "
                            f"{self.window_label}")
                return (f"{count_n(s['uses'], 'use')} · {s['targets']} targets · "
                        f"{s['projects']} projects · {s['sessions']} sessions · "
                        f"{ts(s['first_used'])} → {ts(s['last_used'])} · "
                        f"{self.window_label}")
            if not s["calls"]:
                return f"no calls · {self.window_label}"
            # ok / fail / running are the host's own state.status values
            # (`completed`, `error`, `running`), counted by q_entity_summary.
            return (f"{count_n(s['calls'], 'call')} · {s['completed']} ok · "
                    f"{s['errored']} fail · {s['running']} running · "
                    f"avg {ms(s['avg_ms'])} · {s['projects']} projects · "
                    f"{s['sessions']} sessions · "
                    f"{ts(s['first_used'])} → {ts(s['last_used'])} · "
                    f"{self.window_label}")

    class ModelResponsesScreen(WideTableMixin, Screen):
        """Per-model recent responses in a window, pushed on a Tokens row select."""

        BINDINGS = [Binding("escape,q", "app.pop_screen", "Back")]
        WIDE_TABLES = {"resp-table": (RESPONSE_COLUMNS, "colnote-resp", 0)}

        @property
        def name_cap(self) -> int:
            return self.app.name_cap

        def __init__(self, model: str, start_ts=None, end_ts=None,
                     window_label: str = "all time"):
            super().__init__()
            self.model = model
            self.start_ts = start_ts
            self.end_ts = end_ts
            self.window_label = window_label
            self._wide_rows = {}

        def compose(self) -> ComposeResult:
            yield TopBar(f"Model responses · {self.model}")
            yield Vertical(
                Static(f"[b]Model responses[/b] · {self.model}", id="resp-title"),
                Static(id="resp-summary", classes="entity-summary"),
                Static(id="colnote-resp", classes="col-note"),
                DataTable(id="resp-table", zebra_stripes=True),
            )
            yield Footer()

        def on_mount(self) -> None:
            self.title = f"Model: {self.model}"
            self.query_one("#resp-table", DataTable).cursor_type = "row"
            conn = db.open_db(self.app.db_path, readonly=True)
            try:
                found = db.q_model_calls(conn, model=self.model,
                                         limit=self.app.DETAIL_LIMIT,
                                         start_ts=self.start_ts,
                                         end_ts=self.end_ts)
                rows = []
                for r in found:
                    mid = r["message_id"] or ""
                    masked = (mid[:8] + "…" + mid[-8:]) if len(mid) > 16 else mid
                    rows.append([
                        ts(r["ts"]), r["model"] or "-",
                        fmt_n(r["prompt_tokens"]), fmt_n(r["completion_tokens"]),
                        fmt_n(r["total_tokens"]),
                        fmt_n(r["cache_read"]),
                        fmt_n(r["cache_write"]),
                        "yes" if r["usage_available"] else "not counted",
                        masked,
                    ])
                summary = db.q_model_summary(conn, self.model,
                                             start_ts=self.start_ts,
                                             end_ts=self.end_ts)
            finally:
                conn.close()
            self.query_one("#resp-summary", Static).update(
                self._summary_line(summary))
            self._fill_wide("resp-table", rows,
                            "No responses recorded for this model",
                            key_index=(0, 1), key_kind="call",
                            key_rows=[["model", r["call_id"]] for r in found])

        def _summary_line(self, s) -> str:
            if not s["responses"]:
                return f"no responses · {self.window_label}"
            return (f"{count_n(s['responses'], 'response')} · "
                    f"{s['with_usage']} with usage · "
                    f"in {fmt_n(s['prompt_tokens'])} · "
                    f"out {fmt_n(s['completion_tokens'])} · "
                    f"api total {fmt_n(s['total_tokens'])} · cache "
                    f"{fmt_n(s['cache_read'])}/{fmt_n(s['cache_write'])} · "
                    f"{self.window_label}")

    class CallDetailScreen(Screen):
        """Every field of one call, pushed from a drill-down row.

        A plain ``Screen``, not the ``WideTableMixin``: the detail is a
        label/value list, not a wide table, so it needs neither the column plan
        nor a registered ``DataTable``.
        """

        BINDINGS = [Binding("escape,q", "app.pop_screen", "Back")]

        def __init__(self, kind: str, key):
            super().__init__()
            self.kind = kind
            self.key = str(key)

        def compose(self) -> ComposeResult:
            yield TopBar(f"{self.kind} call")
            with Vertical(id="call-body"):
                yield Static(f"[b]{self.kind} call[/b] · {self.key}",
                             id="call-title")
            yield Footer()

        def on_mount(self) -> None:
            self.title = f"{self.kind} call: {self.key}"
            conn = db.open_db(self.app.db_path, readonly=True)
            try:
                row = db.q_call_detail(conn, self.kind, self.key)
            finally:
                conn.close()
            body = self.query_one("#call-body")
            if row is None:
                body.mount(Static("record not found", classes="call-value"))
                return
            body.mount_all(
                Horizontal(Static(label, classes="call-label"),
                           Static(value, classes="call-value"))
                for label, value in _call_detail_rows(row))

    class TrackerApp(WideTableMixin, App):
        CSS = """
        #hist-title, #resp-title, #call-title { padding: 1 2; }
        DataTable { height: 1fr; }
        #status { padding: 0 2; color: $text-muted; }
        /* The drill-down header (counts) and the single-call detail. The summary
           is one auto-height line; the detail body takes the rest of the screen
           and scrolls, its label column fixed so long values wrap in 1fr. */
        .entity-summary { padding: 0 2; color: $text-muted; height: auto; }
        #call-body { height: 1fr; overflow-y: auto; padding: 0 1; }
        .call-label { width: 18; color: $text-muted; }
        .call-value { width: 1fr; }
        /* Summary pages (Dashboard + Usage). Both share .usage-panel /
           .panel-title. The Usage page has one .summary-panels row (height
           capped, scrolls internally so its table below keeps its rows). The
           Dashboard is a stack of .dash-row rows; #dash-box scrolls. Each row
           is a Horizontal; .compact stacks it on narrow terminals. */
        #usage-box, #dash-box { height: 1fr; padding: 0 1; }
        #dash-box { overflow-y: auto; }
        .summary-panels { height: auto; max-height: 40%; overflow-y: auto; }
        .usage-panel {
            border: round $primary;
            padding: 0 1;
            margin: 0 1 0 0;
            width: 1fr;
            height: auto;
        }
        #panel-runtime, #panel-dash-cache, #panel-dash-tools,
        #panel-dash-activity { margin: 0; }
        .panel-title { text-style: bold; color: $accent; }
        #usage-status, #usage-window { padding: 0; }
        #usage-note, #dash-note { padding: 0 1; color: $text-muted; }
        /* One line per wide table, only when columns were dropped — an always-on
           note above a table that hides nothing is noise that costs a row. Auto
           height because a note that is itself cut off at the right edge would be
           the same defect it exists to report. */
        .col-note { padding: 0 1; color: $text-muted; height: auto; display: none; }
        #dash-activity { color: $accent; }
        #dash-activity-axis { color: $text-muted; }
        #t-usage { height: 1fr; min-height: 6; }
        .summary-panels.compact, .dash-row.compact { layout: vertical; }
        /* Stacked one-per-row wastes the width a narrow terminal still has: at
           80x24 four stacked panels push the table's rows off the visible area.
           Two columns keep a 24-char panel line plus its border and padding
           (~28 cells) legible and leave the table room. */
        .summary-panels.compact { layout: grid; grid-size: 2; }
        /* Below two cells' minimum, stacking is the only layout that does not
           wrap a panel line mid-value. */
        .summary-panels.compact.stacked { layout: vertical; }
        .summary-panels.compact .usage-panel,
        .dash-row.compact .usage-panel { width: 100%; margin: 0 0 1 0; }
        .dash-row { height: auto; margin: 0 0 1 0; }
        """
        # Pane ids in display order. Used for wrap-around tab cycling.
        TAB_IDS = [
            "tab-dashboard", "tab-tools", "tab-skills", "tab-agents",
            "tab-plugins", "tab-mcp", "tab-tokens", "tab-usage",
        ]
        # The tab shown on launch. The TopBar Select must start on this tab's
        # own range (see compose), or its mount-time Changed would overwrite
        # that tab's default before the user touches anything.
        INITIAL_TAB = "tab-dashboard"
        BINDINGS = [
            Binding("q", "quit", "Quit"),
            Binding("r", "refresh", "Refresh"),
            Binding("s", "toggle_auto_sync", "Auto sync"),
            Binding("tab", "next_tab", "Next tab", priority=True),
            Binding("shift+tab", "previous_tab", "Prev tab", priority=True),
        ]
        TITLE = "OpenCode usage tracker"
        # No built-in Header means no ctrl+p palette; disable it outright too.
        ENABLE_COMMAND_PALETTE = False

        USAGE_LOG_LIMIT = 100
        DASH_TOP = 5            # rows in the Dashboard's Top models / Top tools
        DETAIL_LIMIT = 200      # rows on a history / model-response screen

        def __init__(self, db_path, clock=None, config=None):
            super().__init__()
            # Textual animates the tab-bar underline (Tabs._highlight_active calls
            # underline.animate(..., level="basic")). Because it runs at level
            # "basic", the global "basic" animation setting does NOT remove it —
            # only "none" does — and it is the dominant cost of switching tabs.
            # Snap it. Set right after super().__init__() so it applies to every
            # screen pushed on top of this one too.
            self.animation_level = "none"
            self.db_path = str(db_path)
            # Settings come from ~/.config/ocut/config.toml (or OCUT_CONFIG), with the
            # environment able to override one key. Tests pass a dict to avoid reading a
            # real file; a broken file raises ConfigError, which main() turns into a
            # message rather than a traceback.
            self.config = dict(db.DEFAULTS) if config is None else dict(config)
            self.DASH_TOP = self.config["top_n"]
            self.USAGE_LOG_LIMIT = self.config["log_limit"]
            self.DETAIL_LIMIT = self.config["detail_limit"]
            # Shadows WideTableMixin.NAME_CAP for this host and every screen pushed
            # on top of it; the property ``name_cap`` is what reads it.
            self.NAME_CAP = self.config["name_cap"]
            # Injectable wall clock (seconds since the epoch). Defaults to
            # time.time, so a real run is unchanged; tests pass a fixed value to
            # pin "now". The calendar-day windows are derived from this clock,
            # so without a pin a `now - 1h` fixture falls out of a Today window
            # when the suite runs in the first hour after local midnight.
            self._clock = clock or time.time
            self._auto_sync = True
            self._sync_running = False
            self._sync_pending = False
            self.sync_mod = None
            # Per-tab range state: each tab keeps its own window, so changing
            # one tab's range never moves another's. Entity tabs default to 7
            # days; the Dashboard and the Usage page default to Today; Plugins
            # has no window (all-time).
            self.tab_range = {
                "tab-dashboard": "1d",
                "tab-tools": "7d", "tab-skills": "7d", "tab-agents": "7d",
                "tab-mcp": "7d", "tab-tokens": "7d", "tab-usage": "1d",
            }
            self._last_refresh = None      # epoch ms
            self._last_sync = None         # epoch ms
            self._usage_ready = False
            # table id -> (rows, empty message, key index, key kind) for the two
            # wide tables, so a resize can re-plan them without re-querying.
            self._wide_rows = {}
            # Set from migrate() on mount: "" when the schema is usable,
            # otherwise the reason it is not ("newer", "failed: …").
            self._schema_note = ""
            # The status line is composed from these in one function
            # (`_status_line`), because several events change it — a full
            # refresh, a failed re-query, the `s` toggle — and a line overwritten
            # by one of them used to stay wrong until an unrelated refresh.
            self._overview = None            # db.overview() dict, or None
            self._status_note = ""           # last failure the bar is reporting
            # Bumped by every report, so a refresh can tell "nothing complained
            # during this call" from "this call cleared the old warning" without
            # comparing message text.
            self._report_seq = 0

        def compose(self) -> ComposeResult:
            yield TopBar(self.TITLE, with_range=True,
                         range_value=self.tab_range[self.INITIAL_TAB],
                         id="topbar")
            with TabbedContent(initial=self.INITIAL_TAB):
                with TabPane("Dashboard", id="tab-dashboard"):
                    yield Vertical(
                        # Row 1: headline counts, tokens, cache totals.
                        Horizontal(
                            Vertical(
                                Static("Usage", classes="panel-title"),
                                Static(id="dash-kpi-tools"),
                                Static(id="dash-kpi-skills"),
                                Static(id="dash-kpi-agents"),
                                Static(id="dash-kpi-mcp"),
                                Static(id="dash-kpi-plugins"),
                                classes="usage-panel", id="panel-dash-kpi",
                            ),
                            Vertical(
                                Static("Tokens", classes="panel-title"),
                                Static(id="dash-tok-requests"),
                                Static(id="dash-tok-input"),
                                Static(id="dash-tok-output"),
                                Static(id="dash-tok-api"),
                                Static(id="dash-tok-usage"),
                                Static(id="dash-tok-hit-rate"),
                                classes="usage-panel", id="panel-dash-tokens",
                            ),
                            Vertical(
                                Static("Cache", classes="panel-title"),
                                Static(id="dash-cache-hit"),
                                Static(id="dash-cache-miss"),
                                Static(id="dash-cache-write"),
                                classes="usage-panel", id="panel-dash-cache",
                            ),
                            classes="dash-row", id="dash-row-1",
                        ),
                        # Row 2: the leaderboards.
                        Horizontal(
                            Vertical(
                                Static("Top models", classes="panel-title"),
                                Static(id="dash-models"),
                                classes="usage-panel", id="panel-dash-models",
                            ),
                            Vertical(
                                Static("Top tools", classes="panel-title"),
                                Static(id="dash-tools"),
                                classes="usage-panel", id="panel-dash-tools",
                            ),
                            classes="dash-row", id="dash-row-2",
                        ),
                        # Row 3: runtime health + the per-day trend.
                        Horizontal(
                            Vertical(
                                Static("Runtime", classes="panel-title"),
                                Static(id="dash-rt-completed"),
                                Static(id="dash-rt-incomplete"),
                                Static(id="dash-rt-avg"),
                                Static(id="dash-rt-sessions"),
                                Static(id="dash-rt-projects"),
                                classes="usage-panel", id="panel-dash-runtime",
                            ),
                            Vertical(
                                Static("Calls per day", classes="panel-title"),
                                Static(id="dash-activity"),
                                Static(id="dash-activity-axis"),
                                classes="usage-panel", id="panel-dash-activity",
                            ),
                            classes="dash-row", id="dash-row-3",
                        ),
                        Static(id="dash-note"),
                        id="dash-box",
                    )
                with TabPane("Tools", id="tab-tools"):
                    yield Static(id="colnote-tools", classes="col-note")
                    yield DataTable(id="t-tools", zebra_stripes=True)
                with TabPane("Skills", id="tab-skills"):
                    yield Static(id="colnote-skills", classes="col-note")
                    yield DataTable(id="t-skills", zebra_stripes=True)
                with TabPane("Agents", id="tab-agents"):
                    yield Static(id="colnote-agents", classes="col-note")
                    yield DataTable(id="t-agents", zebra_stripes=True)
                with TabPane("Plugins", id="tab-plugins"):
                    yield Static(id="colnote-plugins", classes="col-note")
                    yield DataTable(id="t-plugins", zebra_stripes=True)
                with TabPane("MCP", id="tab-mcp"):
                    yield Static(id="colnote-mcp", classes="col-note")
                    yield DataTable(id="t-mcp", zebra_stripes=True)
                with TabPane("Tokens", id="tab-tokens"):
                    yield Static(id="colnote-tokens", classes="col-note")
                    yield DataTable(id="t-tokens", zebra_stripes=True)
                with TabPane("Usage", id="tab-usage"):
                    yield Vertical(
                        Horizontal(
                            Vertical(
                                Static("Window / Requests", classes="panel-title"),
                                Static(id="usage-window"),
                                Static(id="sum-requests"),
                                Static(id="sum-missing"),
                                classes="usage-panel", id="panel-window",
                            ),
                            Vertical(
                                Static("Model tokens", classes="panel-title"),
                                Static(id="sum-usage-total"),
                                Static(id="sum-input"),
                                Static(id="sum-output"),
                                Static(id="sum-total"),
                                classes="usage-panel", id="panel-tokens",
                            ),
                            Vertical(
                                Static("Cache breakdown", classes="panel-title"),
                                Static(id="sum-cache-hit"),
                                Static(id="sum-cache-miss"),
                                Static(id="sum-cache-write"),
                                Static(id="sum-hit-rate"),
                                classes="usage-panel", id="panel-cache",
                            ),
                            Vertical(
                                Static("Runtime", classes="panel-title"),
                                Static(id="usage-status"),
                                classes="usage-panel", id="panel-runtime",
                            ),
                            id="usage-panels", classes="summary-panels",
                        ),
                        Static(id="usage-note"),
                        Static(id="colnote-usage", classes="col-note"),
                        DataTable(id="t-usage", zebra_stripes=True),
                        id="usage-box",
                    )
            yield Static("", id="status")
            yield Footer()

        def on_mount(self) -> None:
            # One source of column truth: the registry the plan reads is the registry
            # that creates the columns, so a table cannot gain a column the plan does
            # not know how to drop.
            for tid, (cols, _note, _chrome) in self.WIDE_TABLES.items():
                t = self.query_one(f"#{tid}", DataTable)
                t.cursor_type = "row"
                t.add_columns(*cols)
            # Best-effort schema upgrade: an old DB lacks the columns every usage
            # query needs. migrate() returns a status now, so a locked or damaged
            # file is named on the status line instead of showing an empty panel
            # as though that were the user's actual usage.
            note = db.migrate(self.db_path)
            self._schema_note = "" if note in ("current", "migrated") else note
            self._usage_ready = True
            # Auto sync (incremental, ~30s) + UI refresh (~5s). Both are timers,
            # not subprocesses; the sync runs in a worker thread (see _do_sync).
            self.set_interval(self.config["sync_secs"], self._auto_sync_tick)
            self.set_interval(self.config["refresh_secs"], self._auto_refresh_tick)
            self.refresh_data()

        # --- tab cycling (uses the real TabbedContent.active API) -----------

        def _cycle_tab(self, delta: int) -> None:
            tc = self.query_one(TabbedContent)
            try:
                idx = self.TAB_IDS.index(tc.active)
            except ValueError:
                idx = 0
            nxt = (idx + delta) % len(self.TAB_IDS)
            tc.active = self.TAB_IDS[nxt]

        def action_next_tab(self) -> None:
            self._cycle_tab(1)

        def action_previous_tab(self) -> None:
            self._cycle_tab(-1)

        # --- data fill / refresh --------------------------------------------



        def _set_status(self, msg: str) -> None:
            self.query_one("#status", Static).update(msg)

        # --- narrow-terminal column plan -------------------------------------





        def _fill_tools(self, conn, start=None, end=None) -> None:
            # "fail" is the host's `error` status; its third status, `running`, is a
            # call still in flight and is not counted as one.
            self._fill_wide(
                "t-tools",
                [[r["tool_name"], f"{r['calls']:,}", f"{r['completed']:,}",
                  f"{r['errored']:,}", ms(r["avg_ms"]), ts(r["last_used"]),
                  state_cell(r, "tool")]
                 for r in db.q_tools(conn, start_ts=start, end_ts=end)],
                "No tool calls yet", key_kind="tool")

        def _fill_skills(self, conn, start=None, end=None) -> None:
            self._fill_wide(
                "t-skills",
                [[r["skill"], f"{r['calls']:,}", f"{r['completed']:,}",
                  ts(r["last_used"]), state_cell(r, "skill")]
                 for r in db.q_skills(conn, start_ts=start, end_ts=end)],
                "No skills used yet", key_kind="skill")

        def _fill_agents(self, conn, start=None, end=None) -> None:
            self._fill_wide(
                "t-agents",
                [[r["agent_type"], f"{r['calls']:,}", ts(r["last_used"]),
                  state_cell(r, "agent")]
                 for r in db.q_agents(conn, start_ts=start, end_ts=end)],
                "No agents used yet", key_kind="agent")

        def _fill_plugins(self, conn, start=None, end=None) -> None:
            # No time window: the plugin list is all-time (the inventory carries
            # no timestamps), so start/end are accepted for the shared fill
            # signature but ignored. Name is the row key, so Enter/double-click
            # opens the plugin's own drill-down; version is not shown.
            self._fill_wide(
                "t-plugins",
                [[r["plugin"], f"{r['uses']:,}", f"{r['skills']:,}",
                  f"{r['agents']:,}", f"{r['commands']:,}", state_cell(r, "plugin")]
                 for r in db.q_plugins(conn)],
                "No plugins installed", key_kind="plugin")

        def _fill_mcp(self, conn, start=None, end=None) -> None:
            # Row key is unique per (server, tool): v_mcp groups by both, so two
            # tools under one server would otherwise collide and abort the mount.
            self._fill_wide(
                "t-mcp",
                [[r["server"], r["tool"], f"{r['calls']:,}",
                  f"{r['completed']:,}", ts(r["last_used"]), state_cell(r, "mcp")]
                 for r in db.q_mcp(conn, start_ts=start, end_ts=end)],
                "No MCP calls yet", key_index=(0, 1), key_kind="mcp")

        def _fill_tokens(self, conn, start=None, end=None) -> None:
            # Reuses q_model_tokens. API Total is the host's step total; Usage Total
            # is the display-only cache-hit re-add, and the three cache columns are
            # CodeBuddy's hit/miss/write mapped onto OpenCode's read/input/write.
            rows = [[r["model"], db.provider_label(r["provider"]),
                     fmt_n(_usage_total_tokens(r["prompt_tokens"],
                                               r["completion_tokens"],
                                               r["cache_read"],
                                               r["cache_write"])),
                     f"{r['responses']:,}",
                     fmt_n(_input_tokens(r["prompt_tokens"], r["cache_read"],
                                         r["cache_write"])),
                     fmt_n(r["completion_tokens"]),
                     fmt_n(r["total_tokens"]),
                     fmt_n(r["cache_read"]),
                     fmt_n(r["prompt_tokens"]),
                     fmt_n(r["cache_write"])]
                    for r in db.q_model_tokens(conn, start_ts=start, end_ts=end)]
            self._fill_wide("t-tokens", rows, "No model responses yet",
                            key_kind="model")

        # pane id -> the fill method for that tab (usage handled separately).
        _PANE_FILL = {
            "tab-dashboard": "_fill_dashboard",
            "tab-tools": "_fill_tools", "tab-skills": "_fill_skills",
            "tab-agents": "_fill_agents", "tab-plugins": "_fill_plugins",
            "tab-mcp": "_fill_mcp", "tab-tokens": "_fill_tokens",
        }

        def refresh_data(self, conn=None) -> None:
            """Full refresh: every tab + the status bar.

            Used on mount and after a sync that actually indexed new files. Pass
            an existing readonly ``conn`` to avoid opening a second one.
            """
            seq = self._report_seq
            own = conn is None
            if own:
                try:
                    conn = db.open_db(self.db_path, readonly=True)
                except sqlite3.Error as exc:
                    self._report_query_error(exc)
                    return
            try:
                self._sync_range_widget()      # initial 📅 label + Select value
                # Each tab is filled with its own window, so one tab's range
                # never moves another's.
                self._fill_dashboard(conn, *self._bounds_for("tab-dashboard"))
                self._fill_tools(conn, *self._bounds_for("tab-tools"))
                self._fill_skills(conn, *self._bounds_for("tab-skills"))
                self._fill_agents(conn, *self._bounds_for("tab-agents"))
                self._fill_plugins(conn)          # all-time, no window
                self._fill_mcp(conn, *self._bounds_for("tab-mcp"))
                self._fill_tokens(conn, *self._bounds_for("tab-tokens"))
                o = db.overview(conn)          # status bar stays all-time
                if self._usage_ready:
                    self._refresh_usage(conn)   # same connection, no second open
            except sqlite3.Error as exc:
                # This runs during on_mount: letting it raise kills the app
                # before it draws a frame, and the other alternative — panels of
                # zeros — is indistinguishable from a quiet day.
                self._report_query_error(exc)
                return
            finally:
                if own:
                    conn.close()
            self._overview = o
            if self._report_seq == seq:
                # Nothing complained during a refresh that re-queried every tab,
                # so any warning the bar was carrying has been disproved.
                self._status_note = ""
            self._set_status(self._status_line())

        def _status_line(self) -> str:
            """The status line, composed from what the app currently knows.

            One composer because `s`, a failed re-query and a sync with nothing to
            index each used to overwrite the line with a message that then stayed
            there — "Syncing…" outliving a finished sync was the visible one.
            """
            if self._overview is None:
                return f"! {self._status_note}" if self._status_note else ""
            o = self._overview
            status = (
                f"db={self.db_path} · {count_n(o['tool_calls'], 'tool call')} · "
                f"{count_n(o['skills'], 'skill')} · "
                f"{count_n(o['agents'], 'agent')} · "
                # Installed, not a use count: the inventory is the host's config,
                # so this is a different measure from the Plugins tab's `uses`
                # column (which the hook fills). "installed" is what keeps the two
                # from being read as the same number.
                f"{o['plugins_installed']:,} plugins installed · "
                f"{o['mcp']:,} mcp · "
                f"{count_n(o['model_calls'], 'model response')} · "
                f"last {ts(o['last_ts'])}"
            )
            if not self._auto_sync:
                status += " · auto-sync OFF"
            if o["unparsed_records"]:
                # Keep the status line honest: panels showing 0 while records
                # went unclaimed means the format moved, not that usage stopped.
                status += f" · ! {count_n(o['unparsed_records'], 'record')} unparsed"
            if self._schema_note:
                status += f" · schema: {self._schema_note}"
            if self._status_note:
                # Every number above is the last known one, not the current one.
                status += f" · ! {self._status_note}"
            return status

        def _report_query_error(self, exc: sqlite3.Error) -> None:
            """Name a failed re-query instead of leaving the old page on screen.

            A tab that could not be refreshed keeps its previous rows, which look
            exactly like usage the user still has; a locked or damaged file has to
            say so in the one place the user reads.
            """
            self._report_seq += 1
            self._status_note = f"{type(exc).__name__}: {exc}"
            self._set_status(self._status_line())

        def _refresh_active_tab(self, conn=None) -> None:
            """Refresh only the visible tab (5s timer / tab activation).

            Hidden tabs are left alone; they are populated when the user opens
            them (see ``on_tabbed_content_tab_activated``).
            """
            if not self._usage_ready:
                return
            seq = self._report_seq
            pane = self.query_one(TabbedContent).active
            own = conn is None
            if own:
                try:
                    conn = db.open_db(self.db_path, readonly=True)
                except sqlite3.Error as exc:
                    self._report_query_error(exc)
                    return
            try:
                if pane == "tab-usage":
                    self._refresh_usage(conn)
                else:
                    fn = self._PANE_FILL.get(pane)
                    if fn:
                        # Plugins ignores its bounds (all-time).
                        getattr(self, fn)(conn, *self._bounds_for(pane))
            except sqlite3.Error as exc:
                self._report_query_error(exc)
                return
            finally:
                if own:
                    conn.close()
            if self._report_seq == seq and self._status_note:
                # These queries went through and nothing in them complained, so
                # the bar's warning has been disproved. No full refresh is owed:
                # a hidden tab is re-queried the moment the user opens it.
                self._status_note = ""
                self._set_status(self._status_line())

        def on_tabbed_content_tab_activated(
                self, event: TabbedContent.TabActivated) -> None:
            # Populate the tab the user just switched to (hidden tabs are not
            # polled by the 5s timer) and point the range Select at its window.
            if self._usage_ready:
                self._sync_range_widget()
                self._refresh_active_tab()

        # --- Usage Statistics page -----------------------------------------

        def _render_usage_status(self) -> None:
            self.query_one("#usage-status", Static).update(
                f"Auto sync: {'ON' if self._auto_sync else 'OFF'}\n"
                f"Last sync: {local_time(self._last_sync)}\n"
                f"Last refresh: {local_time(self._last_refresh)}"
            )

        @staticmethod
        def _panel_line(label: str, value: str) -> str:
            # Fixed-width label + right-aligned value keeps the panels aligned
            # without relying on text-align. 12 + 12 = 24 columns, so the line
            # does not wrap inside a quarter-width panel at 120+ columns.
            return f"[dim]{label:<12}[/dim]{value:>12}"

        # Below this width the panels stack instead of sitting side by side:
        # each column needs ~28 cells for a 24-char panel line plus
        # border/padding, so 4 x 28 = 112.
        COMPACT_WIDTH = 112
        # A panel line is 24 columns of content plus 2 border and 2 padding, so a
        # two-up cell needs 28. Measured: the panels region is 2 cells narrower
        # than the terminal, so two columns stop fitting below 60.
        STACKED_WIDTH = 60

        # Every table on this screen: id -> (its columns in display order, the note
        # that names what the plan dropped, the cells it loses to its surroundings).
        # Chrome is 2 for a table sitting directly in a tab pane and 4 on the padded
        # Usage page — measured at 80/100/160 columns, not derived. A table that fits
        # keeps every column and shows no note, so registering one that does not need
        # the plan costs nothing.
        WIDE_TABLES = {
            "t-tools": (TOOLS_COLUMNS, "colnote-tools", 2),
            "t-skills": (SKILLS_COLUMNS, "colnote-skills", 2),
            "t-agents": (AGENTS_COLUMNS, "colnote-agents", 2),
            "t-plugins": (PLUGINS_COLUMNS, "colnote-plugins", 2),
            "t-mcp": (MCP_COLUMNS, "colnote-mcp", 2),
            "t-tokens": (TOKEN_COLUMNS, "colnote-tokens", 2),
            "t-usage": (USAGE_COLUMNS, "colnote-usage", 4),
        }

        def _apply_responsive_layout(self, width=None) -> None:
            # Panels side by side on wide terminals; two-up when narrow; stacked
            # below that, where a second column would wrap a value mid-line. Every
            # summary page (Dashboard + Usage) uses .summary-panels. Only
            # .summary-panels has a rule for `stacked`; dash rows never two-up.
            if width is None:
                width = self.size.width
            try:
                compact = width < self.COMPACT_WIDTH
                stacked = width < self.STACKED_WIDTH
                for selector in (".summary-panels", ".dash-row"):
                    for el in self.query(selector):
                        el.set_class(compact, "compact")
                        el.set_class(stacked, "stacked")
            except Exception:
                pass

        def on_resize(self, event) -> None:  # textual.events.Resize
            # event.size carries the NEW size (self.size still lags here).
            self._apply_responsive_layout(event.size.width)
            self._plan_wide_tables(event.size.width)

        def _range_of(self, pane):
            """The window key for a pane, or ``None`` for the all-time Plugins
            tab (which is not in ``self.tab_range``)."""
            return self.tab_range.get(pane)

        def _now_ms(self) -> int:
            """The current time in epoch milliseconds, via the injectable clock."""
            return int(self._clock() * 1000)

        def _bounds_for(self, pane):
            """``(start_ms, end_ms)`` for a pane's **own** window, from now.

            Never cached: every refresh derives the window from the current
            time, so a long-running app does not drift. ``None`` as the start
            means all-time (the Plugins tab).
            """
            rng = self._range_of(pane)
            now = self._now_ms()
            if rng is None:
                return None, now
            if isinstance(rng, db.CustomWindow):
                return rng.start_ms, (now if rng.follow_now else rng.end_ms)
            return db.window_bounds(rng, now)

        def _sync_range_widget(self) -> None:
            """Point the shared range Select at the active tab's own window.

            Every tab keeps its own range (``self.tab_range``), so switching tabs
            shows that tab's range, not the one you last picked elsewhere. The
            Plugins tab has no window, so the Select is hidden there. Setting
            ``Select.value`` posts a ``Changed`` message, which
            ``on_select_changed`` ignores when it already matches the active
            tab's range (so this never re-triggers a refresh loop).
            """
            try:
                sel = self.query_one("#range", Select)
            except Exception:
                return
            pane = self.query_one(TabbedContent).active
            if pane == "tab-plugins":
                sel.display = False
                return
            sel.display = True
            value = self._range_of(pane)
            # A custom window is shown as "Custom…" in the Select, with the span
            # itself on the 📅 control next to it.
            target = CUSTOM_RANGE if isinstance(value, db.CustomWindow) else value
            if target is not None and sel.value != target:
                sel.value = target
            self._set_date_label(db.window_label(value))

        def _set_date_label(self, text: str) -> None:
            try:
                self.query_one("#topbar", TopBar).set_range_label(text)
            except Exception:
                pass

        def _render_usage_window(self) -> None:
            value = self.tab_range["tab-usage"]
            start, end = self._bounds_for("tab-usage")
            end_dt = datetime.fromtimestamp(end / 1000)
            if isinstance(value, db.CustomWindow):
                # A custom window can carry minute precision, so the panel shows
                # the full span with times.
                start_s = ("all" if start is None else datetime.fromtimestamp(
                    start / 1000).strftime("%Y-%m-%d %H:%M"))
                span = f"{start_s} → {end_dt:%Y-%m-%d %H:%M}"
                label = "Custom range"
            else:
                # Presets are whole local calendar days: a clock time would be
                # noise. "Today" is one day, so printing the same date twice read
                # like a bug — every 1d window hit it.
                label = db.USAGE_RANGE_LABELS[value]
                end_date = end_dt.strftime("%Y-%m-%d")
                if start is None:
                    span = end_date
                else:
                    start_date = datetime.fromtimestamp(
                        start / 1000).strftime("%Y-%m-%d")
                    span = (start_date if start_date == end_date
                            else f"{start_date} — {end_date}")
            self.query_one("#usage-window", Static).update(f"{label}\n{span}")

        def _refresh_usage(self, conn=None) -> None:
            # Recompute the window from the CURRENT time on every refresh — never
            # a cached/opening-time range.
            if not self._usage_ready:
                return
            now_ms = self._now_ms()
            start, end = self._bounds_for("tab-usage")
            self._last_refresh = now_ms
            self._render_usage_window()
            self._render_usage_status()
            own = conn is None
            if own:
                try:
                    conn = db.open_db(self.db_path, readonly=True)
                except sqlite3.Error as e:
                    self.query_one("#usage-status", Static).update(
                        f"usage unavailable: {e}")
                    self._report_query_error(e)
                    return
            try:
                s = db.q_usage_summary(conn, start, end)
                self._render_summary_panels(s)
                self._fill_usage_logs(conn, start, end)
            except sqlite3.Error as e:
                # e.g. an un-migrated DB where the newer columns are missing.
                self.query_one("#usage-status", Static).update(
                    f"usage query failed ({e}); run: ocut sync")
                # The page note is local; the bar is what a user reads from
                # another tab, so the failure has to reach both.
                self._report_query_error(e)
            finally:
                if own:
                    conn.close()
            self._apply_responsive_layout()

        def _render_summary_panels(self, s: dict) -> None:
            def put(wid: str, label: str, val: str) -> None:
                self.query_one(wid, Static).update(self._panel_line(label, val))

            req = s["requests"] or 0
            put("#sum-requests", "Requests", f"{req:,}")
            # Usage Total = Input + Output + cache hit (CodeBuddy's display-only
            # re-add); API Total beside it stays the host's own step total.
            put("#sum-usage-total", "Usage Total",
                fmt_n(_usage_total_tokens(s["prompt_tokens"],
                                          s["completion_tokens"],
                                          s["cache_read"], s["cache_write"])))
            put("#sum-input", "Input",
                fmt_n(_input_tokens(s["prompt_tokens"], s["cache_read"],
                                    s["cache_write"])))
            put("#sum-output", "Output", fmt_n(s["completion_tokens"]))
            put("#sum-total", "API Total", fmt_n(s["total_tokens"]))
            put("#sum-cache-hit", "Cache hit", fmt_n(s["cache_read"]))
            put("#sum-cache-miss", "Cache miss", fmt_n(s["prompt_tokens"]))
            put("#sum-cache-write", "Cache write", fmt_n(s["cache_write"]))
            # hit / (hit + miss + write) — the cacheable input.
            frac = db.cache_hit_rate(s["cache_read"], s["prompt_tokens"],
                                     s["cache_write"])
            put("#sum-hit-rate", "Hit rate",
                "-" if frac is None else f"{frac * 100:.1f}%")
            missing = []
            if s["missing_prompt"]:
                missing.append(f"{s['missing_prompt']:,} missing input")
            if s["missing_completion"]:
                missing.append(f"{s['missing_completion']:,} missing output")
            if req == 0:
                note = "no data in window"
            elif missing:
                note = ", ".join(missing)
            else:
                note = "complete"
            # The panel reports completeness either way; naming it "Incomplete"
            # made the good case read "Incomplete: complete".
            put("#sum-missing", "Completeness", note)

        # --- Dashboard page ------------------------------------------------

        def _fill_dashboard(self, conn, start=None, end=None) -> None:
            """Render every Dashboard panel for this tab's own window.

            All panels reflect the Dashboard's window only — the other tabs are
            untouched. Every query takes the same ``(start, end)`` so the tab
            always agrees with itself.
            """
            try:
                kpi = db.q_usage_kpi(conn, start, end)
                s = db.q_usage_summary(conn, start, end)
                act = db.q_usage_activity(conn, start, end)
                models = db.q_usage_model_stats(conn, start, end)
                tools = db.q_tools(conn, limit=self.DASH_TOP,
                                   start_ts=start, end_ts=end)
                daily = db.q_usage_daily(conn, start, end)
            except sqlite3.Error as e:
                # e.g. an un-migrated DB where the newer columns are missing.
                self.query_one("#dash-note", Static).update(
                    f"dashboard query failed ({e}); run: ocut sync")
                self._report_query_error(e)
                return
            self._render_dashboard_kpi(kpi)
            self._render_dashboard_tokens(s)
            self._render_dashboard_cache(s)
            self._render_dashboard_runtime(act)
            self._render_dashboard_models(models)
            self._render_dashboard_tools(tools)
            self._render_dashboard_activity(daily, start, end)
            if not any(kpi.values()) and not (s["requests"] or 0):
                self.query_one("#dash-note", Static).update(
                    "No data in this window — widen the range above.")
            else:
                self.query_one("#dash-note", Static).update(
                    "Range: the top bar · this tab keeps its own window")
            self._apply_responsive_layout()

        def _put_panel(self, wid: str, label: str, val: str) -> None:
            self.query_one(wid, Static).update(self._panel_line(label, val))

        def _render_dashboard_kpi(self, k: dict) -> None:
            # COUNT(*) is authoritative -> a real 0, never a dash.
            self._put_panel("#dash-kpi-tools", "Tool calls", f"{k['tool_calls']:,}")
            self._put_panel("#dash-kpi-skills", "Skills", f"{k['skills']:,}")
            self._put_panel("#dash-kpi-agents", "Agents", f"{k['agents']:,}")
            self._put_panel("#dash-kpi-mcp", "MCP", f"{k['mcp']:,}")
            self._put_panel("#dash-kpi-plugins", "Plugins", f"{k['plugins']:,}")

        def _render_dashboard_tokens(self, s: dict) -> None:
            self._put_panel("#dash-tok-requests", "Requests", f"{s['requests'] or 0:,}")
            self._put_panel("#dash-tok-input", "Input",
                            fmt_n(_input_tokens(s["prompt_tokens"],
                                                s["cache_read"],
                                                s["cache_write"])))
            self._put_panel("#dash-tok-output", "Output", fmt_n(s["completion_tokens"]))
            self._put_panel("#dash-tok-api", "API Total", fmt_n(s["total_tokens"]))
            self._put_panel("#dash-tok-usage", "Usage Total",
                            fmt_n(_usage_total_tokens(s["prompt_tokens"],
                                                      s["completion_tokens"],
                                                      s["cache_read"],
                                                      s["cache_write"])))
            self._put_panel("#dash-tok-hit-rate", "Hit rate",
                            self._hit_rate(s["cache_read"], s["prompt_tokens"],
                                           s["cache_write"]))

        def _render_dashboard_cache(self, s: dict) -> None:
            self._put_panel("#dash-cache-hit", "Cache hit", fmt_n(s["cache_read"]))
            self._put_panel("#dash-cache-miss", "Cache miss", fmt_n(s["prompt_tokens"]))
            self._put_panel("#dash-cache-write", "Cache write", fmt_n(s["cache_write"]))

        def _render_dashboard_runtime(self, a: dict) -> None:
            # counts are COUNT-based -> a real 0; avg_ms is NULL when nothing ran.
            # OpenCode names its two non-completed states `error` and `running`;
            # both are "incomplete" in CodeBuddy's vocabulary, so they are summed.
            self._put_panel("#dash-rt-completed", "Completed", f"{a['completed'] or 0:,}")
            self._put_panel("#dash-rt-incomplete", "Incomplete",
                            f"{(a['errored'] or 0) + (a['running'] or 0):,}")
            self._put_panel("#dash-rt-avg", "Avg tool time", ms(a["avg_ms"]))
            self._put_panel("#dash-rt-sessions", "Sessions", f"{a['sessions'] or 0:,}")
            self._put_panel("#dash-rt-projects", "Projects", f"{a['projects'] or 0:,}")

        def _render_dashboard_models(self, rows) -> None:
            top = rows[: self.DASH_TOP]
            if not top:
                self.query_one("#dash-models", Static).update("nothing in this window")
                return
            lines = [f"{i}. {clip(r['model'], 26):<26}{fmt_n(r['total_tokens']):>12}"
                     f"  {r['requests']:>5} req"
                     for i, r in enumerate(top, 1)]
            self.query_one("#dash-models", Static).update("\n".join(lines))

        def _render_dashboard_tools(self, rows) -> None:
            # The name-keyed list keeps 0-call tools under a window; drop them.
            top = [r for r in rows if r["calls"]][: self.DASH_TOP]
            if not top:
                self.query_one("#dash-tools", Static).update("nothing in this window")
                return
            lines = [f"{i}. {clip(r['tool_name'], 22):<22}"
                     f"{count_n(r['calls'], 'call'):>12}"
                     f" · {ms(r['avg_ms'])}"
                     for i, r in enumerate(top, 1)]
            self.query_one("#dash-tools", Static).update("\n".join(lines))

        # Block characters for the per-day sparkline (index 0 = no activity).
        _SPARK = " ▁▂▃▄▅▆▇█"

        def _sparkline(self, values) -> str:
            peak = max(values) if values else 0
            if peak <= 0:
                # A quiet window still draws something: an empty string left a
                # blank region on the Dashboard, which reads as "widget broken"
                # rather than "no calls". A dotted baseline says the same thing
                # as the zero-height bars it replaces, without inventing a bar.
                return "·" * len(values)
            out = []
            for v in values:
                level = 0 if v <= 0 else min(8, max(1, round(v / peak * 8)))
                out.append(self._SPARK[level])
            return "".join(out)

        def _render_dashboard_activity(self, daily, start, end) -> None:
            """A one-line sparkline of calls per local day across the window.

            ``daily`` only carries days that had calls, so the gaps are filled
            from the window bounds. For the all-time range the series starts at
            the first day with data (never from the epoch).
            """
            counts = {r["day"]: r["calls"] for r in daily}
            now = self._now_ms() if end is None else end
            end_day = datetime.fromtimestamp(now / 1000).date()
            if start is not None:
                start_day = datetime.fromtimestamp(start / 1000).date()
            elif daily:
                start_day = datetime.strptime(daily[0]["day"], "%Y-%m-%d").date()
            else:
                start_day = end_day
            days, d = [], start_day
            while d <= end_day:
                days.append(d)
                d += timedelta(days=1)
            series = [counts.get(day.isoformat(), 0) for day in days]
            self.query_one("#dash-activity", Static).update(self._sparkline(series))
            # The day list is never empty (a window always contains at least
            # today), so the branch that matters is "no calls in it", not
            # "no days" — the old `if series:` test was always true.
            if any(series):
                self.query_one("#dash-activity-axis", Static).update(
                    f"{days[0]:%m-%d} → {days[-1]:%m-%d} · peak {max(series):,}/day")
            else:
                self.query_one("#dash-activity-axis", Static).update(
                    f"{days[0]:%m-%d} → {days[-1]:%m-%d} · no activity")

        @staticmethod
        def _hit_rate(hit, miss, write) -> str:
            """``hit / (hit + miss + write)``, or ``-`` if undefined."""
            frac = db.cache_hit_rate(hit, miss, write)
            return "-" if frac is None else f"{frac * 100:.1f}%"

        def _fill_usage_logs(self, conn, start, end) -> None:
            rows = db.q_usage_request_logs(conn, start, end,
                                           limit=self.USAGE_LOG_LIMIT)
            self.query_one("#usage-note", Static).update(
                "Newest first · cache hit=read, write=create · "
                "no cost/duration/status" if rows else "")
            out = [[
                local_time(r["ts"]), r["model"] or "-",
                db.provider_label(r["provider"]),
                fmt_n(_usage_total_tokens(r["prompt_tokens"],
                                          r["completion_tokens"],
                                          r["cache_read"], r["cache_write"])),
                fmt_n(_input_tokens(r["prompt_tokens"], r["cache_read"],
                                    r["cache_write"])),
                fmt_n(r["completion_tokens"]),
                fmt_n(r["total_tokens"]),
                fmt_n(r["cache_read"]),
                fmt_n(r["prompt_tokens"]),
                fmt_n(r["cache_write"]),
                self._hit_rate(r["cache_read"], r["prompt_tokens"],
                               r["cache_write"]),
            ] for r in rows]
            # The shared empty state, like the other seven tables: a note above an
            # untouched table reads as a page that failed to load.
            self._fill_wide("t-usage", out,
                            "No requests in this window — widen the range above.")

        def on_select_changed(self, event: Select.Changed) -> None:
            if event.select.id != "range":
                return
            pane = self.query_one(TabbedContent).active
            if pane == "tab-plugins":
                return                    # Plugins has no window
            if event.value == CUSTOM_RANGE:
                # "Custom…" opens the picker — unless this is the programmatic
                # sync that points the Select at an already-custom window.
                if not isinstance(self._range_of(pane), db.CustomWindow):
                    self._open_date_range(pane)
                return
            if event.value == self._range_of(pane):
                return                    # programmatic sync, not a user change
            self.tab_range[pane] = event.value
            self._sync_range_widget()     # refresh the 📅 label
            if pane == "tab-usage":
                self._render_usage_window()   # keep the Usage Window panel live
            self._refresh_active_tab()    # re-query only the visible tab

        def _open_date_range(self, pane) -> None:
            """Open the date picker for a pane and apply the picked window to it."""
            modal = DateRangeModal(self._range_of(pane), self._now_ms())

            def done(result) -> None:
                if result is None:
                    self._sync_range_widget()   # cancel: revert the Select
                    return
                self.tab_range[pane] = result
                self._sync_range_widget()
                if pane == "tab-usage":
                    self._render_usage_window()
                self._refresh_active_tab()

            self.push_screen(modal, done)

        def on_button_pressed(self, event: Button.Pressed) -> None:
            # Only the top bar's 📅 control reaches the app; the modal handles its
            # own buttons (it is the active screen).
            if event.button.id == "date-range":
                pane = self.query_one(TabbedContent).active
                if pane != "tab-plugins":
                    self._open_date_range(pane)

        # --- row select: route to detail screens ---------------------------

        def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
            # A row select only means something on the screen that is showing. The
            # push below is not awaited, so a second activation can arrive while the
            # new screen is still mounting — the tail of a double click, an impatient
            # second Enter. Honouring it stacks a copy of what the user is already
            # looking at, and "Back" then removes only one of the two.
            if event.data_table.screen is not self.screen:
                return
            key = event.row_key.value
            if not key or "\t" not in key:
                return
            kind, _, rest = key.partition("\t")
            if kind == "call":
                # A drill-down row: call\t<kind>\t<key> opens that call's detail.
                call_kind, _, call_key = rest.partition("\t")
                self.push_screen(CallDetailScreen(call_kind, call_key))
                return
            # The entity tabs' drill-downs follow the tab's own window, so the
            # summary and the list agree with the row that was opened. The
            # Plugins tab has no window (its range is None) — all-time.
            pane = self.query_one(TabbedContent).active
            rng = self._range_of(pane)
            if rng is None:
                start = end = None
                label = "all time"
            else:
                start, end = self._bounds_for(pane)
                label = db.window_label(rng)
            if kind == "model":
                self.push_screen(ModelResponsesScreen(rest, start, end, label))
            elif kind == "mcp":
                # Key is mcp\t<server>\t<tool>. Both halves travel: the row was keyed
                # on the pair, the table column searched is the tool, and two servers
                # can expose one tool name — so the server is a condition, not a title.
                server, _, tool = rest.partition("\t")
                self.push_screen(HistoryScreen(
                    "mcp", tool, display=f"{server} · {tool}",
                    start_ts=start, end_ts=end, window_label=label, server=server))
            else:
                self.push_screen(HistoryScreen(kind, rest, start_ts=start,
                                               end_ts=end, window_label=label))

        # --- R / S shortcuts ------------------------------------------------

        def action_refresh(self) -> None:
            # R = incremental sync + reload DB + refresh the UI (not just a
            # re-render of already-loaded data).
            self._set_status("Syncing…")
            self._request_sync()

        def action_toggle_auto_sync(self) -> None:
            # S = toggle the automatic sync switch. Distinct from R: it does not
            # trigger a sync itself.
            self._auto_sync = not self._auto_sync
            # The line itself carries the state ("· auto-sync OFF"); a bespoke
            # message here used to wipe the counts off the bar until an unrelated
            # refresh happened to put them back.
            self._set_status(self._status_line())
            self.notify(f"Auto sync: {'ON' if self._auto_sync else 'OFF'}")
            if self._usage_ready:
                self._render_usage_status()

        # --- automatic sync / refresh timers --------------------------------

        def _auto_sync_tick(self) -> None:
            # The same shutdown guard as _auto_refresh_tick (D-003). A timer can
            # fire once more while the app is closing; _request_sync then reaches
            # for an event loop that is already gone and raises
            # "RuntimeError: no running event loop", leaving a worker coroutine
            # that is never awaited.
            if not self.is_running:
                return
            if self._auto_sync:
                self._request_sync()

        def _auto_refresh_tick(self) -> None:
            # Only the visible tab is re-queried; hidden tabs refresh when the
            # user opens them. The is_running guard matters because a timer can
            # fire once more while the app is shutting down (e.g. run_test's
            # teardown), after the widgets are gone — query_one then raises
            # NoMatches from inside the timer and fails the test run.
            if not self.is_running:
                return
            self._refresh_active_tab()

        def _request_sync(self) -> None:
            # Guard against concurrent syncs: if one is running, mark a pending
            # request and return; the running worker will retry on completion.
            if self._sync_running:
                self._sync_pending = True
                return
            self._sync_running = True
            # A plain daemon thread, NOT a Textual thread worker. Textual runs a
            # thread worker on the event loop's default executor
            # (`textual/worker.py`: `loop.run_in_executor(None, …)`), and
            # `asyncio.run()`'s teardown joins that executor before `app.run()`
            # returns. A sync still in flight when the user quits then blocks the
            # exit for as long as the sync runs — pressing `r` and then `q` hung
            # the app, and Ctrl+C surfaced a KeyboardInterrupt traceback out of
            # `shutdown_default_executor`. A daemon thread is not in that
            # executor, so `q` never waits on a sync; it is abandoned at exit,
            # and the sync is idempotent (its cursor advances only on a finished
            # pass), so the next run recovers anything this one did not finish.
            threading.Thread(target=self._do_sync, name="ocut-sync",
                             daemon=True).start()

        def _do_sync(self) -> None:
            # Runs in a daemon thread. ocut-sync.run() opens its own RW
            # connection and uses the incremental sync_state table, so no
            # connection is shared across threads.
            try:
                if self.sync_mod is None:
                    self.sync_mod = db.load_sync()
                stats = self.sync_mod.run(quiet=True, db_path=self.db_path)
                self._post_sync(True, stats, None)
            except Exception as e:  # report, never swallow
                self._post_sync(False, None, str(e))

        def _post_sync(self, ok: bool, stats, err) -> None:
            # Hand the result back to the UI thread. The app may already be
            # closing — `call_from_thread` raises RuntimeError once the loop is
            # gone, and a sync that lands after the last screen is torn down has
            # nothing left to update, so drop it rather than let a daemon thread
            # raise into stderr on the way out.
            try:
                self.call_from_thread(self._on_sync_done, ok, stats, err)
            except RuntimeError:
                pass

        def _on_sync_done(self, ok: bool, stats, err) -> None:
            self._sync_running = False
            # A sync can land after the app began closing (the worker is a
            # daemon, so nothing joins it). The timers guard on is_running for
            # the same reason: refreshing torn-down widgets would raise
            # NoMatches from inside a callback that can no longer be seen.
            if not self.is_running:
                return
            # The indexer names an unreadable host database inside its status dict
            # (`ok: False`, no exception), so both spellings of failure count here.
            if ok and (stats or {}).get("ok", True):
                self._last_sync = self._now_ms()
                read = (stats or {}).get("parts_read", 0)
                if read > 0:
                    self.refresh_data()          # new rows can touch any tab
                else:
                    self._refresh_active_tab()   # nothing changed -> light refresh
                    # A light refresh does not rewrite the bar, so without this
                    # the transient "Syncing…" from `r` survives the sync that
                    # already finished.
                    self._set_status(self._status_line())
                self.notify(f"Synced {count_n(read, 'host part')}")
            else:
                # The same composer as the data line: a failed sync is a fact
                # *about* the numbers, not a replacement for them.
                why = err or (stats or {}).get("error") or "unknown error"
                self._status_note = "Sync failed: " + why[:200]
                self._set_status(self._status_line())
                self.notify("Sync failed — keeping current data", severity="error")
            if self._sync_pending:
                self._sync_pending = False
                self._request_sync()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Interactive OpenCode usage tracker.")
    ap.add_argument("--db", default=str(db.DB_PATH))
    args = ap.parse_args(argv)

    if not HAVE_TEXTUAL:
        print(
            "textual is not installed — the TUI needs it.\n"
            "  pip install -r requirements.txt   (or run ./install.sh)\n"
            "Headless reports still work:  ocut stats",
            file=sys.stderr,
        )
        return 2
    if not Path(args.db).exists():
        print(f"no database at {args.db} — run: ocut sync", file=sys.stderr)
        return 1
    try:
        app = TrackerApp(args.db, config=db.load_config())
    except db.ConfigError as exc:
        # A broken settings file is the user's to fix, and starting with defaults
        # silently would leave them editing a file that is not being read.
        print(f"your settings are not usable: {exc}\n"
              f"  fix or remove that file, or point OCUT_CONFIG somewhere else",
              file=sys.stderr)
        return 2
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
