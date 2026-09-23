"""Unit tests for the Skills-tab sort orders (A3/A4 regression).

The original bug: with zero usage rows every mode produced the *same* order,
so pressing `s` looked broken. These tests pin the ordering rules, including
the deterministic name tiebreak that makes the success-rate order stable.
"""

from __future__ import annotations

import pytest

from conftest import load_module

st = load_module("skill-tui.py", "skill_tui")

sort_rows = st.sort_rows
SORT_MODES = st.SORT_MODES
SORT_LABELS = st.SORT_LABELS


def row(name, total=0, success=0, last_used=None, path=None):
    return {
        "skill_name": name,
        "total": total,
        "success": success,
        "last_used": last_used,
        "path": path or f"/s/{name}",
        "description": "",
        "category": "personal-skills",
    }


def names(rows):
    return [r["skill_name"] for r in rows]


# --- count -----------------------------------------------------------------
def test_sort_count_desc_then_name():
    rows = [row("b", total=1), row("a", total=3), row("c", total=3)]
    assert names(sort_rows(rows, "count")) == ["a", "c", "b"]


# --- last_used -------------------------------------------------------------
def test_sort_last_used_none_last_and_desc():
    rows = [
        row("never", last_used=None),
        row("old", last_used="2024-01-01T00:00:00Z"),
        row("new", last_used="2026-01-01T00:00:00Z"),
    ]
    assert names(sort_rows(rows, "last_used")) == ["new", "old", "never"]


# --- success_rate ----------------------------------------------------------
def test_sort_success_rate_none_last():
    rows = [
        row("none", total=0),
        row("half", total=4, success=2),
        row("full", total=2, success=2),
    ]
    assert names(sort_rows(rows, "success_rate")) == ["full", "half", "none"]


def test_sort_success_rate_ties_break_on_total_then_name():
    """Equal rates must fall back to total desc, then name asc (A4 fix)."""
    rows = [
        row("z", total=2, success=2),          # 100%, total 2
        row("a", total=6, success=6),          # 100%, total 6  -> first
        row("m", total=2, success=2),          # 100%, total 2  -> name tie
    ]
    assert names(sort_rows(rows, "success_rate")) == ["a", "m", "z"]


def test_sort_success_rate_is_deterministic_across_calls():
    rows = [row(f"s{i}", total=0) for i in range(20)]
    first = names(sort_rows(rows, "success_rate"))
    second = names(sort_rows(list(reversed(rows)), "success_rate"))
    assert first == second


# --- name ------------------------------------------------------------------
def test_sort_name_ascending():
    assert names(sort_rows([row("c"), row("a"), row("b")], "name")) == ["a", "b", "c"]


# --- the actual reported symptom ------------------------------------------
def test_all_modes_coincide_when_no_usage():
    """With no usage data every order collapses — the UI says so via a hint.

    This documents *why* the user thought sorting was broken; the hint text
    ("no usage data yet, all orders coincide") is asserted in test_tui.py.
    """
    rows = [row("b"), row("a"), row("c")]
    orders = {mode: names(sort_rows(rows, mode)) for mode in ("count", "last_used", "success_rate")}
    assert orders["count"] == orders["last_used"] == orders["success_rate"] == ["a", "b", "c"]


# --- labels ----------------------------------------------------------------
@pytest.mark.parametrize("mode", SORT_MODES)
def test_every_mode_has_a_label(mode):
    assert mode in SORT_LABELS and SORT_LABELS[mode]


def test_sort_labels_have_no_cjk():
    for label in SORT_LABELS.values():
        assert all(ord(ch) < 0x2E80 for ch in label), f"CJK left in label: {label!r}"
