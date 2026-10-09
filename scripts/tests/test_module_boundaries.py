"""Module boundaries are contracts too, so they get checked.

The point of splitting the two read-only neighbours out of `skill_db.py` was not a
smaller file — it was that a name should have one home and the dependency graph
should have one direction. Both of those are only true while nobody re-adds the
convenient copy, and "convenient" is exactly what an AI editing this repo will
reach for. These tests are what makes the boundary hold without a human rereading
915 lines.
"""

from __future__ import annotations

import pathlib
import re

import skill_db as db
import skill_db_agentos as aos
import skill_db_claude_mem as cm

SCRIPTS = pathlib.Path(db.__file__).resolve().parent


def test_the_neighbour_readers_are_no_longer_hosted_by_the_core_module():
    """`skill_db` must not answer for another project's schema any more.

    Not a style preference: while the readers lived in `skill_db`, importing the
    core pulled in ~915 lines of another product's column names, and every test of
    the writer scanned that one file. Re-exporting the names from here would undo
    the split while still looking tidy, so the names must be *absent*.
    """
    for name in ("claude_mem_store", "claude_mem_activity", "claude_mem_http",
                 "agentos_store", "agentos_summary"):
        assert not hasattr(db, name), f"skill_db hosts {name} again"
        assert hasattr(cm if name.startswith("claude_mem") else aos, name), name


def test_the_dependency_direction_is_one_way():
    """Neighbours import the core; the core never imports a neighbour.

    A back-edge would make `import skill_db` depend on load order, which is the
    kind of failure that only shows up when something imports the leaf first.
    """
    core = (SCRIPTS / "skill_db.py").read_text(encoding="utf-8")
    assert not re.search(r"^\s*(import|from)\s+skill_db_(agentos|claude_mem)", core, re.M), \
        "skill_db imports a neighbour module: the graph now has a cycle"
    for leaf in ("skill_db_agentos.py", "skill_db_claude_mem.py"):
        text = (SCRIPTS / leaf).read_text(encoding="utf-8")
        assert re.search(r"^import skill_db as db$", text, re.M), f"{leaf} does not import the core"


def test_the_neighbours_reach_core_state_through_the_module_object():
    """`from skill_db import HOME` would freeze the value at import time.

    The suite repoints `skill_db.HOME` to prove default-path resolution, and a
    copied-in name would keep answering with the developer's real home — a test
    that quietly reads the machine it is supposed to be isolating from.
    """
    for leaf in ("skill_db_agentos.py", "skill_db_claude_mem.py"):
        text = (SCRIPTS / leaf).read_text(encoding="utf-8")
        assert not re.search(r"^from skill_db import", text, re.M), \
            f"{leaf} imports names by value; monkeypatching skill_db would stop reaching it"
        assert "db.HOME" in text or "rows_to_dicts" in text, leaf
