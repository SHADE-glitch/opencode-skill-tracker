"""`opencode_compat` must be the real single source, not a second copy.

A module that *restates* the names scattered through the code is worse than the
scattering: it looks like the answer and drifts. These tests make the
declaration load-bearing — either the code agrees with it, or the suite goes red.

Direction of the check, stated honestly: each test here proves a declared name is
still present in the writer. None of them can prove the writer reads *only* what
is declared; that stays a review rule (AGENTS.md), and adding a read means adding
its name to `opencode_compat` in the same commit.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
from pathlib import Path

import pytest

import opencode_compat as compat
import skill_db as db

PLUGIN = Path(__file__).resolve().parents[2] / "plugin" / "skill-tracker.js"

BUN = shutil.which("bun")
requires_bun = pytest.mark.skipif(BUN is None, reason="bun is not installed")


@pytest.fixture(scope="module")
def src():
    assert PLUGIN.is_file(), f"missing plugin at {PLUGIN}"
    return PLUGIN.read_text(encoding="utf-8")


def test_source_case_sql_and_source_of_agree_on_every_sample():
    """The two halves of the `source` rule used to be hand-mirrored.

    `skill_db` embedded a SQL CASE and also carried a Python function that said
    the same thing, with a comment asking for sync. Here both are run over the
    same categories — SQLite evaluates the CASE, Python evaluates `source_of` —
    and any disagreement between them is a bug this test names.
    """
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE s (category TEXT)")
    categories = [None, ""] + [c for _, c in compat.CATEGORY_SAMPLES if c is not None]
    conn.executemany(
        "INSERT INTO s (category) VALUES (?)", [(c,) for c in categories]
    )
    rows = conn.execute(
        f"SELECT category, {compat.source_case_sql()} FROM s"
    ).fetchall()
    for category, sql_source in rows:
        assert compat.source_of(category) == sql_source, (
            f"category {category!r}: Python says {compat.source_of(category)!r}, "
            f"SQL says {sql_source!r}"
        )
    conn.close()


def test_the_rendered_case_is_still_the_string_the_queries_embed():
    """A formatting change here silently changes seven queries' SQL text.

    The exact string is pinned because `SOURCE_CASE_SQL` is interpolated into
    f-strings across the read layer; a renamed alias or a dropped space would not
    fail any other test until a page came up empty.
    """
    assert compat.source_case_sql() == (
        "CASE s.category WHEN 'personal-skills' THEN 'personal' "
        "WHEN 'open-source-skills' THEN 'open-source' "
        "ELSE COALESCE(NULLIF(s.category, ''), 'unknown') END"
    )
    assert db.SOURCE_CASE_SQL == compat.source_case_sql()
    assert db.source_of("personal-skills") == "personal"


def test_category_rule_treats_the_root_as_no_category():
    """`os.path.relpath` says "." where `path.relative` says "".

    Both mean "the skills directory itself", and a "." reaching the `category`
    column would surface on the Skills page as a source named ".".
    """
    for rel, expected in compat.CATEGORY_SAMPLES:
        assert compat.category_of(rel) == expected, rel


@requires_bun
def test_the_writer_categories_the_same_directories(tmp_path):
    """Same rule, other language, real directories — not two readings of a regex.

    A fixture tree is scanned by the plugin (`skillt sync` path inside
    `__selftest` is not enough: this needs its own DB) and by `scan_skills`, and
    the two category sets must be identical.
    """
    import subprocess

    root = tmp_path / "skills"
    for cat, name in (("personal-skills", "alpha"), ("open-source-skills", "beta"),
                      ("work", "gamma")):
        d = root / cat / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(f"---\nname: {name}\n---\nb\n", encoding="utf-8")

    script = """
    const m = await import(process.env.PLUGIN_PATH);
    const ok = await m.__selftest();
    console.log(ok ? 'SELFTEST_OK' : 'SELFTEST_FAILED');
    process.exit(ok ? 0 : 1);
    """
    env = dict(os.environ,
               PLUGIN_PATH=str(PLUGIN),
               OPENCODE_SKILL_TRACKER_DB=str(tmp_path / "cat.db"),
               OPENCODE_SKILL_TRACKER_SKILLS_DIR=str(root),
               OPENCODE_SKILL_TRACKER_MCP_SERVERS="test-server",
               OPENCODE_SKILL_TRACKER_PLUGINS="")
    r = subprocess.run([BUN, "-e", script], env=env, capture_output=True, text=True,
                       timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr

    plugin_cats = {
        row[0] for row in sqlite3.connect(str(tmp_path / "cat.db")).execute(
            "SELECT DISTINCT category FROM skills"
        )
    }
    python_cats = {s["category"] for s in db.scan_skills(str(root))}
    assert plugin_cats == python_cats == {"personal-skills", "open-source-skills", "work"}


def test_every_declared_opencode_name_is_still_present_in_the_writer(src):
    """A name that leaves the writer means either we stopped reading it or upstream
    renamed it. Both are exactly what this module exists to notice.

    Payload paths are checked by their **leaf accessor** (`.parentSessionId`,
    `.subagent_type`), because the writer reaches them through short-lived locals:
    `state.input` is written `st.input`, `hookInput.type` is written `perm.type`.
    The dotted form in the module is the host's shape, which is what a reader
    needs; the leaf is what a rename would take away. Limit stated plainly: a leaf
    that also exists under another name (`type`, `input`) is weaker evidence than
    one that does not (`parentSessionId`), and no form of this check can see a read
    that was never declared.
    """
    declared = (
        list(compat.HOOKS)
        + [
            compat.EVENT_SESSION_CREATED,
            compat.EVENT_SESSION_UPDATED,
            compat.EVENT_VCS_BRANCH_UPDATED,
            compat.EVENT_MESSAGE_PART_UPDATED,
            compat.EVENT_PERMISSION_ASKED,
            compat.EVENT_PERMISSION_REPLIED,
            compat.EVENT_PERMISSION_UPDATED,
        ]
        + [compat.SKILL_TOOL, compat.TASK_TOOL, compat.TASK_AGENT_FIELD]
        + [compat.SKILL_FILE_NAME, compat.PIN_COMMENT_PREFIX]
    )
    missing = [name for name in declared if name not in src]
    leaves = sorted({f".{path.rsplit('.', 1)[-1]}" for path in compat.PAYLOAD_FIELDS})
    missing += [leaf for leaf in leaves if leaf not in src]
    assert not missing, f"declared upstream names the writer no longer names: {missing}"


def test_the_host_paths_come_from_the_module_not_from_callers():
    """`~/.config/opencode` was spelled out in three files.

    The tracker's own filenames stay with the tracker; the directory names are the
    host's, so `skill_db` must build its paths from these tuples.
    """
    assert db.CONFIG_DIR.endswith(os.path.join(*compat.CONFIG_HOME))
    assert db.DATA_DIR.endswith(os.path.join(*compat.DATA_HOME))
    assert db.SKILLS_DIR == os.path.join(db.CONFIG_DIR, compat.SKILLS_SUBDIR)
    assert compat.CONFIG_FILE_NAMES == ("opencode.json", "opencode.jsonc")
    assert compat.PROJECT_CONFIG_DIR in ("./.opencode", ".opencode")


def test_status_vocabulary_matches_both_schema_copies(src):
    """The enum is declared here, and both `CHECK` copies must still match it.

    The DDL keeps its own text on purpose (the schema-drift test compares the two
    copies verbatim), so this is the only thing that notices a rename in one of
    them: three tables take `denied`, `subagent_usage` does not.
    """
    with_denied = compat.status_check_sql(compat.STATUS_VALUES)
    without_denied = compat.status_check_sql(compat.STATUS_VALUES_NO_DENIED)
    for text, label in ((db.TABLES_SQL, "skill_db"), (src, "plugin")):
        assert text.count(with_denied) == 3, f"{label}: expected 3 denied-capable tables"
        assert text.count(without_denied) == 1, f"{label}: subagent_usage's clause moved"


def test_the_tool_id_pin_is_a_claim_the_plugin_actually_makes(src):
    """`doctor` parses the plugin's own comment; the comment must carry this pin.

    Two independent copies of "which release" is the drift that M13 is about, and
    the refresh recipe (MAINTENANCE §5) updates the comment, not a constant.
    """
    m = compat.VERSION_PIN_RE.search(src)
    assert m, "the plugin lost the `verified against OpenCode X.Y.Z` comment doctor parses"
    assert m.group(1) == compat.PIN_TOOL_IDS, (
        f"plugin says {m.group(1)}, compat says {compat.PIN_TOOL_IDS} — edit both on a refresh"
    )
