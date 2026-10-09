"""Every knob must be documented, or the build is red.

This gate did not exist, and its absence is exactly how six environment variables
shipped while `README.md` documented eleven. A reader cannot discover a setting the
docs do not name, and a maintainer cannot keep a table in sync by remembering to.

The check is by *enumeration from source*, not from a list maintained here: a test
that reads a hand-kept inventory proves nothing about the inventory.
"""

from __future__ import annotations

import re
from pathlib import Path

import settings as st

ROOT = Path(__file__).resolve().parents[2]
READMES = (ROOT / "README.md", ROOT / "README.zh-CN.md")
SOURCES = (
    ROOT / "plugin" / "skill-tracker.js",
    ROOT / "scripts" / "skill_db.py",
    ROOT / "scripts" / "skill_db_agentos.py",
    ROOT / "scripts" / "skill_db_claude_mem.py",
    ROOT / "scripts" / "skill-tui.py",
    ROOT / "scripts" / "skill-stats.py",
    ROOT / "scripts" / "settings.py",
    ROOT / "bin" / "skillt",
)


def _readmes() -> dict[str, str]:
    return {p.name: p.read_text(encoding="utf-8") for p in READMES}


def test_every_setting_is_documented_in_both_readmes():
    """Key name, default and both flag and env rule have to appear in each language."""
    texts = _readmes()
    for spec in st.REGISTRY:
        for name, text in texts.items():
            assert spec["key"] in text, f"{name} does not document {spec['key']}"
            assert f"`{spec['default']}`" in text or str(spec["default"]) in text, (
                f"{name} does not give the default for {spec['key']}")
            if spec["flag"]:
                assert spec["flag"] in text, f"{name} omits {spec['key']}'s flag {spec['flag']}"
    for name, text in texts.items():
        assert st.CONFIG_PATH_ENV in text, f"{name} does not document the config path override"
        assert "OPENCODE_SKILL_TRACKER_VIEW_DAYS" in text or "OPENCODE_SKILL_TRACKER_<KEY>" in text, (
            f"{name} states no env rule for settings")


# Read by the dispatcher out of the shell's own machinery, never set by a user,
# and not a knob the documentation could describe usefully.
SHELL_INBUILT = {"BASH_SOURCE", "SELF", "0", "?", "-", "IFS", "PWD", "OLDPWD"}


def _env_names_in_source() -> set[str]:
    """Names actually used as environment keys, not every OPENCODE_* identifier.

    The shapes differ per language, and the gate has to know which is which: in
    JavaScript `${NAME}` is a *string template*, not an environment read — this
    project interpolates its own constants that way (`${DB_PATH}`,
    `${SCOPE_PRUNABLE_SQL}`), and matching those as env vars would demand
    documentation for things no user can set. In bash the same spelling *is* an
    environment read — but a bare `$NAME` is not evidence of one, because bash
    cannot tell an inherited variable from a line the script assigned itself, and
    every unbraced `$UPPER` in `bin/skillt` turned out to be its own local.
    Matching on the bare `OPENCODE_` prefix would sweep in unrelated constants
    (there is one called OPENCODE_VERSION_TIMEOUT_S), and a gate that cries wolf
    is the check people switch off.
    """
    js = re.compile(r"process\.env\.([A-Z_][A-Z0-9_]*)")
    py = re.compile(
        r"os\.environ(?:\.get)?\(\s*[\"']([A-Z_][A-Z0-9_]*)[\"']"
        r"|[A-Za-z_]*_ENV\s*=\s*[\"']([A-Z_][A-Z0-9_]*)[\"']"
    )
    sh = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)[:-]?")
    found = set()
    for path in SOURCES:
        text = path.read_text(encoding="utf-8")
        if path.suffix == ".js":
            found.update(js.findall(text))
        elif path.suffix == ".py":
            for m in py.finditer(text):
                found.add(next(g for g in m.groups() if g))
        else:
            found.update(g for m in sh.finditer(text) for g in m.groups() if g)
    return found - SHELL_INBUILT


def test_no_environment_variable_is_read_without_being_documented():
    """Enumerate the reads from the source, then demand each one is in the table.

    Excluded deliberately: the test suite's own fixtures (`BLOCKER`, the
    selftest's synthetic prose sentinel) — a test helper is not user-facing
    surface, and pretending otherwise would push the docs to describe fixtures.
    """
    found = _env_names_in_source() - {
        "OPENCODE_SKILL_TRACKER_BLOCKER", "OPENCODE_SKILL_TRACKER_SELFTEST_PROSE",
        "PLUGIN_PATH", "PROJECT_DIR",
    }
    texts = _readmes()
    missing = {
        name: [n for n, t in texts.items() if name not in t]
        for name in sorted(found)
        if any(name not in t for t in texts.values())
    }
    assert found, "the enumeration found no environment reads: the patterns went stale"
    assert not missing, "undocumented environment names: " + "; ".join(
        f"{k} missing from {', '.join(v)}" for k, v in missing.items())


def test_the_documented_flag_list_matches_the_parsers():
    """`bin/skillt` carries a hand-synced list of value-taking flags (its own comment
    says so). Here it is checked instead of trusted."""
    dispatcher = (ROOT / "bin" / "skillt").read_text(encoding="utf-8")
    line = next((l for l in dispatcher.splitlines() if 'rest+=("$t"); skip=1' in l), None)
    assert line, "the dispatcher's value-flag case line moved; update this check with it"
    listed = set(re.findall(r"--[a-z][a-z-]*", line))
    assert listed, "the pattern found no flags on that line"
    from_registry = {s["flag"] for s in st.REGISTRY if s["flag"]} | {"--db", "--out"}
    assert listed == from_registry, (
        f"dispatcher and registry disagree: only-listed={sorted(listed - from_registry)} "
        f"only-registry={sorted(from_registry - listed)}")
