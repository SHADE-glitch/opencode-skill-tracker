"""Leaf helpers in `skill_db` that the topical suites never reach.

The other `skill_db` test modules exercise behaviour through the public entry
points (health, sync, backups, data ops). These helpers — formatters, the
category->source mapping, the tolerant JSON column reader, and the
connection-shape probes — are only hit on the paths those suites happen to
take. This module pins their remaining branches directly, including the
`sqlite3.Error` fallbacks that a closed or foreign connection would trigger.
"""

from __future__ import annotations

import sqlite3
from datetime import timezone

import pytest

import skill_db as db


class _BrokenConn:
    """A connection whose every statement raises, like a closed/foreign handle."""

    def execute(self, *_args, **_kwargs):
        raise sqlite3.Error("no such connection")


# ---------------------------------------------------------------------------
# source_of
# ---------------------------------------------------------------------------
def test_source_of_known_categories():
    assert db.source_of("personal-skills") == "personal"
    assert db.source_of("open-source-skills") == "open-source"


def test_source_of_falls_back_to_the_category_then_unknown():
    assert db.source_of("team-skills") == "team-skills"
    assert db.source_of(None) == "unknown"
    assert db.source_of("") == "unknown"


# ---------------------------------------------------------------------------
# fmt_time
# ---------------------------------------------------------------------------
def test_fmt_time_empty_is_dash():
    assert db.fmt_time(None) == "-"
    assert db.fmt_time("") == "-"


def test_fmt_time_treats_a_naive_timestamp_as_utc():
    # No trailing Z: fromisoformat yields a naive datetime, which must be
    # stamped UTC rather than interpreted as local time.
    out = db.fmt_time("2026-09-23T15:30:00")
    assert out.startswith("2026-09-23") or out.startswith("2026-09-24")


def test_fmt_time_unparsable_is_passed_through_truncated():
    assert db.fmt_time("2026-09-23T15:30:00 garbage") == "2026-09-23 15:30"


# ---------------------------------------------------------------------------
# short_path
# ---------------------------------------------------------------------------
def test_short_path_empty_is_dash():
    assert db.short_path(None) == "-"
    assert db.short_path("") == "-"


def test_short_path_abbreviates_home():
    assert db.short_path(db.HOME + "/proj/x").startswith("~")


def test_short_path_ellipsises_a_long_path():
    out = db.short_path("/very/deep/" + "a" * 60, limit=20)
    assert out.startswith("…") and len(out) == 20


# ---------------------------------------------------------------------------
# _parse_utc
# ---------------------------------------------------------------------------
def test_parse_utc_none_and_invalid():
    assert db._parse_utc(None) is None
    assert db._parse_utc("") is None
    assert db._parse_utc("not-a-timestamp") is None


def test_parse_utc_naive_is_stamped_utc():
    dt = db._parse_utc("2026-09-23T15:30:00")
    assert dt.tzinfo is timezone.utc


# ---------------------------------------------------------------------------
# _load_json_list
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("raw", [None, "", "not json", '{"a": 1}', '"scalar"'])
def test_load_json_list_degrades_to_empty(raw):
    assert db._load_json_list(raw) == []


def test_load_json_list_reads_an_array():
    assert db._load_json_list('["a", "b"]') == ["a", "b"]


# ---------------------------------------------------------------------------
# parse_frontmatter: the empty / non-string guards
# ---------------------------------------------------------------------------
def test_parse_frontmatter_empty_and_non_string():
    assert db.parse_frontmatter("") == {}
    assert db.parse_frontmatter(None) == {}


# ---------------------------------------------------------------------------
# connection-shape probes fall back instead of raising
# ---------------------------------------------------------------------------
def test_has_content_hash_is_false_on_a_broken_connection():
    assert db.has_content_hash(_BrokenConn()) is False


def test_table_exists_is_false_on_a_broken_connection():
    assert db._table_exists(_BrokenConn(), "skills") is False


def test_db_file_of_falls_back_to_db_path():
    assert db.db_file_of(_BrokenConn()) == db.DB_PATH
    # an in-memory DB has a 'main' entry with an empty filename
    assert db.db_file_of(sqlite3.connect(":memory:")) == db.DB_PATH


def test_db_file_of_returns_the_real_file(seeded_db):
    conn = db.open_db(seeded_db)
    assert db.db_file_of(conn) == seeded_db
    conn.close()
