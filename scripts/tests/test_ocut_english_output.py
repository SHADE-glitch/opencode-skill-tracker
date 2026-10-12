"""Every string this project writes is English — the headless commands and the TUI.

Scope: what the owner *sees from our code*. Command output (stdout and stderr), the
TUI's Statics, column labels, button labels, input placeholders, screen titles, and
the two error paths in `main`. Not in scope: source comments and docstrings, and the
Chinese README — those are meant to be Chinese. Data is not checked either: a model
or a path the host reports can contain anything, and the reader does not translate it.

The rule this replaces was written for the plugin-era TUI, where the interface started
in Chinese and was converted. The new stack was written in English from the first
line, which is exactly the condition under which the conversion stops being watched
and a translated string slips back in.
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))
import ocut_db as db                                    # noqa: E402
from textual.widgets import (Button, DataTable, Input, Static,
                             TabPane)                     # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "ocut_english_tui", str(SCRIPTS / "ocut-tui.py"))
tui = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tui)

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_ocut_drill as drill_tests                   # noqa: E402
import test_ocut_tui as tui_tests                       # noqa: E402

CJK_RANGES = (
    (0x3000, 0x303F),   # CJK punctuation
    (0x3400, 0x4DBF),   # CJK ext A
    (0x4E00, 0x9FFF),   # CJK unified
    (0xF900, 0xFAFF),   # CJK compat
    (0xFF00, 0xFFEF),   # fullwidth forms
)

def cjk_chars(text):
    return [ch for ch in text
            if any(lo <= ord(ch) <= hi for lo, hi in CJK_RANGES)]


def assert_no_cjk(text, where):
    bad = cjk_chars(str(text))
    assert not bad, f"CJK characters in {where}: {''.join(sorted(set(bad)))}"


@pytest.fixture
def tracker(tmp_path):
    host = tmp_path / "opencode.db"
    drill_tests.build_host(host)
    cfg = drill_tests.build_config(tmp_path / "cfg")
    out = tmp_path / "usage.db"
    db.load_sync().run(db_path=out, host_db=host, host_config=cfg, quiet=True)
    return out


# --- headless commands ------------------------------------------------------

READ_ONLY = [
    ["stats"], ["tools"], ["skills"], ["agents"], ["mcp"], ["plugins"],
    ["models"], ["recent"], ["inventory"], ["export"], ["health"],
    ["show", "tool", "read"], ["show", "mcp", "search"],
    ["format"], ["restore"],
]


@pytest.mark.parametrize("args", READ_ONLY)
def test_a_headless_command_speaks_english(args, tracker):
    """stdout *and* stderr, because a translated hint on stderr is still UI."""
    r = subprocess.run([sys.executable, str(SCRIPTS / "ocut-stats.py"),
                        "--db", str(tracker), *args],
                       capture_output=True, text=True, timeout=120)
    assert "Traceback" not in r.stderr, r.stderr
    assert_no_cjk(r.stdout, f"ocut {' '.join(args)} stdout")
    assert_no_cjk(r.stderr, f"ocut {' '.join(args)} stderr")


def test_a_headless_error_path_is_english_too(tracker):
    """`show` on a kind the whitelist refuses is our sentence, not argparse's."""
    r = subprocess.run([sys.executable, str(SCRIPTS / "ocut-stats.py"),
                        "--db", str(tracker), "show", "nonsense", "read"],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode != 0, r.stdout + r.stderr
    assert_no_cjk(r.stdout + r.stderr, "ocut show error")


def test_the_settings_error_the_tui_prints_is_english(tmp_path, capsys):
    """`main` writes the refusal itself, so its wording is covered here."""
    path = tmp_path / "config.toml"
    path.write_text("top_n = 999999\n")
    (tmp_path / "usage.db").write_bytes(b"")
    original, db.CONFIG_PATH = db.CONFIG_PATH, path
    try:
        rc = tui.main(["--db", str(tmp_path / "usage.db")])
    finally:
        db.CONFIG_PATH = original
    captured = capsys.readouterr()
    assert rc == 2, (rc, captured.err)
    assert "not usable" in captured.err, captured.err   # the branch was reached
    assert_no_cjk(captured.err, "main's refusal")
    assert_no_cjk(captured.out, "main's stdout")


# --- the TUI ---------------------------------------------------------------

def test_every_string_the_tui_draws_is_english(tracker):
    """All eight tabs, then both drill screens, the detail screen and the picker."""
    def check(screen, where):
        for widget in screen.query(Static):
            assert_no_cjk(str(widget.content), f"{where} Static #{widget.id}")
        for table in screen.query(DataTable):
            for key in table.columns:
                assert_no_cjk(str(table.columns[key].label),
                              f"{where} column {table.columns[key].label}")
        for button in screen.query(Button):
            assert_no_cjk(str(button.label), f"{where} button #{button.id}")
        for field in screen.query(Input):
            assert_no_cjk(field.placeholder or "", f"{where} placeholder")

    async def body(pilot, app):
        # The pane list comes from the mounted widget tree, not from a copy of it:
        # which tabs exist and in what order is pinned by the TUI's own suite, and a
        # second frozen list here would silently stop walking a renamed one.
        for pane in [p.id for p in app.query(TabPane)]:
            await tui_tests.activate(app, pilot, pane)
            check(app.screen, pane)

        # The pushed screens: entity history, model responses, one call's detail.
        # Pushed directly rather than clicked — the click path is the drill suite's
        # job, and what is under test here is the text these screens draw.
        app.push_screen(tui.HistoryScreen("tool", "read"))
        await pilot.pause(0.5)
        check(app.screen, "HistoryScreen")
        app.pop_screen()
        await pilot.pause(0.3)
        app.push_screen(tui.ModelResponsesScreen("model-x", None, None, "1d"))
        await pilot.pause(0.5)
        check(app.screen, "ModelResponsesScreen")
        app.pop_screen()
        await pilot.pause(0.3)
        app.push_screen(tui.CallDetailScreen("tool", "prt_read_today"))
        await pilot.pause(0.5)
        check(app.screen, "CallDetailScreen")
        app.pop_screen()
        await pilot.pause(0.3)

        # The date picker: the one surface copied whole from the reference, and the
        # likeliest place for a translated label to arrive.
        app._open_date_range("tab-usage")
        await pilot.pause(0.6)
        check(app.screen, "DateRangeModal")
    tui_tests.drive(lambda: tui.TrackerApp(str(tracker)), body, size=(150, 55))
