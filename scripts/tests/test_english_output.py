"""The user-visible UI must be English (no CJK).

Scope, per the agreed plan: TUI + `--cli` output + the health suggestions.
Code comments/docstrings and the README stay Chinese, so this test only checks
*rendered* output, never the source text.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import load_module

SCRIPTS = Path(__file__).resolve().parent.parent
st = load_module("skill-tui.py", "skill_tui")

CJK_RANGES = (
    (0x3000, 0x303F),   # CJK punctuation
    (0x3400, 0x4DBF),   # CJK ext A
    (0x4E00, 0x9FFF),   # CJK unified
    (0xF900, 0xFAFF),   # CJK compat
    (0xFF00, 0xFFEF),   # fullwidth forms
)


def static_text(widget) -> str:
    """Plain text of a Static, including its markup (Textual 8.x: `.content`)."""
    try:
        return str(widget.content)
    except Exception:  # noqa: BLE001
        return ""


def cjk_chars(text: str) -> list[str]:
    out = []
    for ch in text:
        o = ord(ch)
        if any(lo <= o <= hi for lo, hi in CJK_RANGES):
            out.append(ch)
    return out


def assert_no_cjk(text: str, where: str):
    bad = cjk_chars(text)
    assert not bad, f"CJK characters in {where}: {''.join(sorted(set(bad)))}"


# --- --cli output ----------------------------------------------------------
def _run_cli(seeded_db, *args):
    cmd = [sys.executable, str(SCRIPTS / "skill-tui.py"), "--cli", *args, "--db", seeded_db]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60)


@pytest.mark.parametrize(
    "args",
    [
        ("insight",),
        ("insight", "--json"),
        ("health",),
        ("mcp",),
        ("mcp", "--json"),
        ("doctor",),
        ("export",),
        ("sync", "--dry-run"),
        ("auto-backup", "--dry-run"),
        ("cleanup-selftest",),
    ],
)
def test_cli_output_is_english(seeded_db, args):
    r = _run_cli(seeded_db, *args)
    assert "Traceback" not in r.stderr, r.stderr
    assert_no_cjk(r.stdout, f"--cli {' '.join(args)} stdout")
    assert_no_cjk(r.stderr, f"--cli {' '.join(args)} stderr")


# --- health suggestions ----------------------------------------------------
def test_health_suggestions_are_english(tmp_path):
    import skill_db as db

    p = str(tmp_path / "h.db")
    conn = db.open_db(p, readonly=False)
    db.ensure_schema(conn)
    for i in range(3):
        conn.execute(
            "INSERT INTO skills (name, category, path) VALUES (?,?,?)",
            (f"s{i}", "personal-skills", f"/s/{i}"),
        )
    conn.commit()
    report = db.health_report(conn)
    conn.close()
    assert_no_cjk(" ".join(report["suggestions"]), "health suggestions")
    assert report["suggestions"], "expected at least one advisory suggestion"


# --- TUI rendering ---------------------------------------------------------
def test_tui_visible_text_is_english(seeded_db):
    pytest.importorskip("textual")
    from textual.widgets import DataTable, Static

    SkillTUI = st.get_app_class()

    async def _run_it():
        app = SkillTUI(db_path=seeded_db, no_sync=True)
        async with app.run_test() as pilot:
            await pilot.pause()
            for _ in range(5):          # walk every tab, MCP included
                for widget in app.screen.query(Static):
                    assert_no_cjk(static_text(widget), f"Static {widget.id}")
                for table in app.screen.query(DataTable):
                    for col in table.columns.values():
                        assert_no_cjk(str(col.label), f"column label {col.label}")
                await pilot.press("tab")
                await pilot.pause()

    asyncio.run(_run_it())


def test_tui_help_and_placeholder_english(seeded_db):
    pytest.importorskip("textual")
    from textual.widgets import Input, Static

    SkillTUI = st.get_app_class()

    async def _run_it():
        app = SkillTUI(db_path=seeded_db, no_sync=True)
        async with app.run_test() as pilot:
            await pilot.pause()
            search = app.screen.query_one("#search", Input)
            assert_no_cjk(search.placeholder or "", "search placeholder")
            help_text = app.screen.query_one("#data-help", Static)
            assert_no_cjk(static_text(help_text), "Data page help")
            sort_label = app.screen.query_one("#sort-label", Static)
            assert_no_cjk(static_text(sort_label), "sort label")

    asyncio.run(_run_it())
