"""Static + behavioural checks on the OpenCode plugin.

Most checks are static (assert the shipped guards are present) and exercise the
real UPSERT SQL with Python's sqlite3 against the plugin's own schema. The
loader-contract checks at the bottom DO execute the plugin via Bun — but only
against an isolated temp DB, and they are skipped when Bun is unavailable.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

import skill_db as db

PLUGIN = Path(__file__).resolve().parents[2] / "plugin" / "skill-tracker.js"

BUN = shutil.which("bun")
requires_bun = pytest.mark.skipif(BUN is None, reason="bun is not installed")


@pytest.fixture(scope="module")
def src():
    assert PLUGIN.is_file(), f"missing plugin at {PLUGIN}"
    return PLUGIN.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def upsert_sql(src):
    m = re.search(r"const UPSERT_USAGE_SQL\s*=\s*`([^`]*)`", src)
    assert m, "UPSERT_USAGE_SQL not found in the plugin source"
    return m.group(1)


@pytest.fixture(scope="module")
def upsert_mcp_sql(src):
    m = re.search(r"const UPSERT_MCP_SQL\s*=\s*`([^`]*)`", src)
    assert m, "UPSERT_MCP_SQL not found in the plugin source"
    return m.group(1)


@pytest.fixture(scope="module")
def upsert_plugin_sql(src):
    m = re.search(r"const UPSERT_PLUGIN_SQL\s*=\s*`([^`]*)`", src)
    assert m, "UPSERT_PLUGIN_SQL not found in the plugin source"
    sql = m.group(1)
    # In the plugin this is a JS template literal; resolve the one interpolated
    # constant the way JS would, so the sentinel comparison under test is real
    # rather than the literal text `${UNKNOWN_PLUGIN}`.
    um = re.search(r'const UNKNOWN_PLUGIN\s*=\s*"([^"]*)"', src)
    assert um, "UNKNOWN_PLUGIN not found in the plugin source"
    sql = sql.replace("${UNKNOWN_PLUGIN}", um.group(1))
    assert "${" not in sql, f"unresolved interpolation left in UPSERT_PLUGIN_SQL: {sql}"
    return sql


@pytest.fixture(scope="module")
def upsert_plugin_inventory_sql(src):
    m = re.search(r"const UPSERT_PLUGIN_INVENTORY_SQL\s*=\s*`([^`]*)`", src)
    assert m, "UPSERT_PLUGIN_INVENTORY_SQL not found in the plugin source"
    return m.group(1)


DCP = "@tarquinen/opencode-dcp@3.2.0"
CONDUCTOR = "opencode-conductor-plugin"


def _conn():
    c = sqlite3.connect(":memory:")
    c.executescript(db.SCHEMA_SQL)
    return c


def _put(conn, sql, status, duration=None, session="S", call="C", meta="{}"):
    conn.execute(
        sql,
        (None, "skill-x", session, "/p", "tool_call", status, duration, call, meta),
    )


def _put_mcp(
    conn, sql, status, duration=None, session="S", call="C",
    server="srv", tool="tool-a", args=None, meta="{}",
):
    conn.execute(
        sql,
        (server, tool, session, "/p", "tool_call", status, duration, call, args, meta),
    )


def _put_plugin(
    conn, sql, status, duration=None, session="S", call="C",
    plugin=DCP, kind="tool", item="compress", trigger="tool_call", meta="{}",
):
    conn.execute(
        sql,
        (plugin, kind, item, session, "/p", trigger, status, duration, call, meta),
    )


# ---------------------------------------------------------------------------
# Structure
# ---------------------------------------------------------------------------
def test_plugin_registers_all_hooks(src):
    for marker in ("tool.execute.before", "tool.execute.after", "permission.ask", "event:"):
        assert marker in src, f"hook marker missing: {marker}"


def test_long_lived_maps_are_capacity_bounded(src):
    """M8: caches that outlive a session must not grow without limit.

    `branchByDir` and `pendingPerms` are never cleared during a run, so
    a long-lived OpenCode process would leak entries. They must go through
    `setCapped`.
    """
    assert "const MAP_CAP" in src
    assert "function setCapped(" in src
    for map_name in ("branchByDir", "pendingPerms"):
        assert f"{map_name}.set(" not in src, f"{map_name} must use setCapped, not .set("
        assert f"setCapped({map_name}" in src, f"{map_name} must go through setCapped"


def test_git_lookup_kills_its_subprocess(src):
    """M7: the branch lookup must use a killable subprocess.

    Bun's `$` shell exposes no kill/abort handle, so the old `Promise.race`
    let a slow git run past its 500ms deadline and never cleared the timer.
    """
    body = src.split("async function resolveBranch(", 1)[1].split("\nasync function", 1)[0]
    assert "AbortController" in body
    assert "clearTimeout" in body
    assert "Promise.race" not in body, "the unkillable race must be gone"
    assert "Bun.spawn(" in src


def test_h1_schema_failure_disables_tracker_instead_of_throwing(src):
    assert 'log("fatal", "schema creation failed, tracker disabled: ' in src
    assert 'log("err", "scanSkills: " + errMsg(e));' in src
    assert 'log("err", "init: " + errMsg(e));' in src


def test_h2_selftest_requires_isolated_database(src):
    assert "process.env.OPENCODE_SKILL_TRACKER_DB" in src
    assert "Selftest requires isolated database" in src
    body = src.split("export async function __selftest()", 1)[1]
    guard = body.index("Selftest requires isolated database")
    assert guard < body.index("const results = []"), "guard must run first"
    assert guard < body.index("skillTrackerPlugin"), "guard must precede any DB open"


def test_h3_upsert_only_allows_error_to_override_success(upsert_sql):
    assert "WHEN excluded.status = 'error' AND skill_usage.status = 'success'" in upsert_sql
    assert "(excluded.status = 'error' AND skill_usage.status = 'success')" in upsert_sql
    assert "WHEN skill_usage.status IN ('success','error','denied')" in upsert_sql


# ---------------------------------------------------------------------------
# UPSERT semantics (real SQL, real schema)
# ---------------------------------------------------------------------------
def test_upsert_dedup_and_error_override(upsert_sql):
    c = _conn()
    _put(c, upsert_sql, "success", 100)
    _put(c, upsert_sql, "success", 100)                     # duplicate call
    assert c.execute("SELECT COUNT(*) FROM skill_usage").fetchone()[0] == 1
    assert c.execute("SELECT status, duration_ms FROM skill_usage").fetchone()[:] == ("success", 100)

    _put(c, upsert_sql, "error", 150)                       # event error corrects it
    assert c.execute("SELECT status FROM skill_usage").fetchone()[0] == "error"


def test_upsert_success_never_downgrades_error(upsert_sql):
    c = _conn()
    _put(c, upsert_sql, "error", 10)
    _put(c, upsert_sql, "success", 20)
    assert c.execute("SELECT status FROM skill_usage").fetchone()[0] == "error"


def test_upsert_denied_is_not_overridden_by_error(upsert_sql):
    c = _conn()
    _put(c, upsert_sql, "denied", 10)
    _put(c, upsert_sql, "error", 20)
    assert c.execute("SELECT status FROM skill_usage").fetchone()[0] == "denied"


def test_upsert_completes_unknown_and_ask(upsert_sql):
    c = _conn()
    _put(c, upsert_sql, "unknown", None, call="C1")
    _put(c, upsert_sql, "success", 42, call="C1")
    assert c.execute(
        "SELECT status, duration_ms FROM skill_usage WHERE call_id='C1'"
    ).fetchone()[:] == ("success", 42)

    _put(c, upsert_sql, "ask", None, call="C2")
    _put(c, upsert_sql, "success", 7, call="C2")
    assert c.execute(
        "SELECT status, duration_ms FROM skill_usage WHERE call_id='C2'"
    ).fetchone()[:] == ("success", 7)


def test_upsert_keeps_first_metadata(upsert_sql):
    """COALESCE keeps the original metadata; a later event cannot blank it."""
    c = _conn()
    _put(c, upsert_sql, "success", 10, call="C3")           # metadata "{}"
    c.execute("UPDATE skill_usage SET metadata='{\"a\":1}' WHERE call_id='C3'")
    # a later error event takes the UPDATE path, but must not replace metadata
    _put(c, upsert_sql, "error", 99, call="C3", meta='{"b":2}')
    row = c.execute("SELECT status, metadata FROM skill_usage WHERE call_id='C3'").fetchone()
    assert row[0] == "error"
    assert row[1] == '{"a":1}', "COALESCE must keep the original metadata"


# ---------------------------------------------------------------------------
# MCP UPSERT semantics
# ---------------------------------------------------------------------------
def test_mcp_upsert_dedup_and_error_override(upsert_mcp_sql):
    c = _conn()
    _put_mcp(c, upsert_mcp_sql, "success", 100)
    _put_mcp(c, upsert_mcp_sql, "success", 100)             # duplicate call
    assert c.execute("SELECT COUNT(*) FROM mcp_usage").fetchone()[0] == 1
    assert c.execute("SELECT status, duration_ms FROM mcp_usage").fetchone()[:] == ("success", 100)

    _put_mcp(c, upsert_mcp_sql, "error", 150)               # event error corrects it
    assert c.execute("SELECT status FROM mcp_usage").fetchone()[0] == "error"


def test_mcp_upsert_success_never_downgrades_error(upsert_mcp_sql):
    c = _conn()
    _put_mcp(c, upsert_mcp_sql, "error", 10)
    _put_mcp(c, upsert_mcp_sql, "success", 20)
    assert c.execute("SELECT status FROM mcp_usage").fetchone()[0] == "error"


def test_mcp_upsert_denied_is_not_overridden_by_error(upsert_mcp_sql):
    c = _conn()
    _put_mcp(c, upsert_mcp_sql, "denied", 10)
    _put_mcp(c, upsert_mcp_sql, "error", 20)
    assert c.execute("SELECT status FROM mcp_usage").fetchone()[0] == "denied"


def test_mcp_upsert_upgrades_the_star_sentinel(upsert_mcp_sql):
    """The permission path only knows the server, so it writes tool_name='*'.

    A later tool-level observation for the same call must upgrade it to the
    real tool name — otherwise the TUI would show a permanent '*' bucket.
    """
    c = _conn()
    _put_mcp(c, upsert_mcp_sql, "denied", None, call="C1", tool="*")
    _put_mcp(c, upsert_mcp_sql, "success", 30, call="C1", tool="read_note")
    row = c.execute("SELECT tool_name, status FROM mcp_usage WHERE call_id='C1'").fetchone()
    assert row[0] == "read_note"
    assert row[1] == "denied", "the upgrade must not resurrect a denied call"


def test_mcp_upsert_keeps_first_arg_names(upsert_mcp_sql):
    c = _conn()
    _put_mcp(c, upsert_mcp_sql, "success", 10, call="C2", args='["a"]')
    _put_mcp(c, upsert_mcp_sql, "error", 20, call="C2", args='["b"]')
    assert c.execute(
        "SELECT arg_names FROM mcp_usage WHERE call_id='C2'"
    ).fetchone()[0] == '["a"]'


def test_mcp_upsert_fills_missing_arg_names(upsert_mcp_sql):
    """COALESCE must let a later observation supply names the first one lacked."""
    c = _conn()
    _put_mcp(c, upsert_mcp_sql, "success", 10, call="C3", args=None)
    c.execute("UPDATE mcp_usage SET duration_ms=NULL, status='ask' WHERE call_id='C3'")
    _put_mcp(c, upsert_mcp_sql, "success", 20, call="C3", args='["identifier"]')
    assert c.execute(
        "SELECT arg_names FROM mcp_usage WHERE call_id='C3'"
    ).fetchone()[0] == '["identifier"]'


# ---------------------------------------------------------------------------
# Plugin UPSERT semantics
# ---------------------------------------------------------------------------
def test_plugin_upsert_dedup_and_error_override(upsert_plugin_sql):
    c = _conn()
    _put_plugin(c, upsert_plugin_sql, "success", 100)
    _put_plugin(c, upsert_plugin_sql, "success", 100)           # duplicate call
    assert c.execute("SELECT COUNT(*) FROM plugin_usage").fetchone()[0] == 1
    assert c.execute(
        "SELECT status, duration_ms FROM plugin_usage"
    ).fetchone()[:] == ("success", 100)

    _put_plugin(c, upsert_plugin_sql, "error", 150)             # event error corrects it
    assert c.execute("SELECT status FROM plugin_usage").fetchone()[0] == "error"


def test_plugin_upsert_success_never_downgrades_error(upsert_plugin_sql):
    c = _conn()
    _put_plugin(c, upsert_plugin_sql, "error", 10)
    _put_plugin(c, upsert_plugin_sql, "success", 20)
    assert c.execute("SELECT status FROM plugin_usage").fetchone()[0] == "error"


def test_plugin_upsert_denied_is_not_overridden_by_error(upsert_plugin_sql):
    c = _conn()
    _put_plugin(c, upsert_plugin_sql, "denied", 10, trigger="permission_denied")
    _put_plugin(c, upsert_plugin_sql, "error", 20)
    assert c.execute("SELECT status FROM plugin_usage").fetchone()[0] == "denied"


def test_plugin_upsert_upgrades_the_unknown_sentinel(upsert_plugin_sql):
    """The scan may fail to attribute a tool, so the row is written as
    `(unknown)`. A later observation that *does* resolve the owner must upgrade
    the row — otherwise the TUI keeps a permanent `(unknown)` bucket for a call
    it eventually identified, mirroring the MCP `'*'` sentinel upgrade.
    """
    c = _conn()
    _put_plugin(c, upsert_plugin_sql, "denied", None, call="C1",
                plugin="(unknown)", trigger="permission_denied")
    _put_plugin(c, upsert_plugin_sql, "success", 30, call="C1", plugin=DCP)
    row = c.execute(
        "SELECT plugin_name, status FROM plugin_usage WHERE call_id='C1'"
    ).fetchone()
    assert row[0] == DCP, "the resolved owner must replace the sentinel"
    assert row[1] == "denied", "the upgrade must not resurrect a denied call"


def test_plugin_upsert_never_downgrades_a_real_owner_to_unknown(upsert_plugin_sql):
    """The upgrade is one-way: a resolved name must survive a later unresolved
    observation for the same call (the two hooks disagree about provenance)."""
    c = _conn()
    _put_plugin(c, upsert_plugin_sql, "success", 10, call="C2", plugin=DCP)
    _put_plugin(c, upsert_plugin_sql, "error", 20, call="C2", plugin="(unknown)")
    row = c.execute(
        "SELECT plugin_name, status FROM plugin_usage WHERE call_id='C2'"
    ).fetchone()
    assert row[0] == DCP, "a real owner must not be overwritten by the sentinel"
    assert row[1] == "error", "the error correction still applies"


def test_plugin_upsert_keeps_first_metadata(upsert_plugin_sql):
    c = _conn()
    _put_plugin(c, upsert_plugin_sql, "success", 10, call="C3")  # metadata "{}"
    c.execute("UPDATE plugin_usage SET metadata='{\"a\":1}' WHERE call_id='C3'")
    _put_plugin(c, upsert_plugin_sql, "error", 99, call="C3", meta='{"b":2}')
    row = c.execute(
        "SELECT status, metadata FROM plugin_usage WHERE call_id='C3'"
    ).fetchone()
    assert row[0] == "error"
    assert row[1] == '{"a":1}', "COALESCE must keep the original metadata"


def test_plugin_inventory_upsert_keeps_first_seen(upsert_plugin_inventory_sql):
    """first_seen is the one column the re-scan at the next init must not move."""
    c = _conn()
    c.execute(
        upsert_plugin_inventory_sql,
        (DCP, "3.2.0", "npm", 0, '["compress"]', '["dcp-compress"]', "global"),
    )
    c.execute("UPDATE plugin_inventory SET first_seen='2000-01-01T00:00:00.000Z'")
    c.execute(
        upsert_plugin_inventory_sql,
        (DCP, "3.3.0", "npm", 0, '["compress","expand"]', '["dcp-compress"]', "global"),
    )
    row = c.execute(
        "SELECT version, tools, first_seen FROM plugin_inventory WHERE plugin_name=?",
        (DCP,),
    ).fetchone()
    assert row[0] == "3.3.0", "version must refresh"
    assert row[1] == '["compress","expand"]', "surface must refresh"
    assert row[2] == "2000-01-01T00:00:00.000Z", "first_seen must be immutable"


def test_a_project_scope_row_is_never_downgraded(upsert_plugin_inventory_sql):
    """`project` is the label that protects a row from the prune, so it sticks.

    If a re-scan could overwrite it with `global`, a session started somewhere
    else would delete a plugin that this project still lists — and the inventory
    would flap between two states depending on where OpenCode happened to be run.
    """
    c = _conn()
    c.execute(
        upsert_plugin_inventory_sql,
        (DCP, "3.2.0", "npm", 0, "[]", "[]", "project"),
    )
    c.execute(
        upsert_plugin_inventory_sql,
        (DCP, "3.3.0", "npm", 0, '["compress"]', "[]", "global"),
    )
    assert c.execute(
        "SELECT scope, version FROM plugin_inventory WHERE plugin_name=?", (DCP,)
    ).fetchone() == ("project", "3.3.0"), "scope must stick, everything else must refresh"


# ---------------------------------------------------------------------------
# Attribution policy (static)
# ---------------------------------------------------------------------------
def test_commands_fail_closed_when_unattributed(src):
    """A command whose owner the scan did not resolve must NOT be recorded.

    There is no builtin-command allowlist to fall back on (unlike tools), so
    fail-open here would flood plugin_usage with `/init`, `/undo` and every
    other builtin slash command. The hook must return before writing.
    """
    body = src.split('"command.execute.before": safe(', 1)[1].split("\n    }),", 1)[0]
    assert "commandToPlugin.get(cmd)" in body
    assert "if (!plugin) return;" in body, "the unresolved case must bail out"
    guard = body.index("if (!plugin) return;")
    assert guard < body.index("recordPluginUsage"), "the guard must precede the write"
    assert "status: \"unknown\"" in body, "a command has no completion event"


def test_tool_attribution_fails_open_to_unknown(src):
    """Tools fail the other way: an unresolved non-builtin tool is still a
    plugin tool (the builtin allowlist is verifiable), recorded as `(unknown)`
    rather than dropped, so usage is never silently lost."""
    body = src.split("function classify(", 1)[1].split("\nfunction ", 1)[0]
    assert "builtinToolSet().has(toolId)" in body
    assert "opts && opts.strict" in body, "the permission path must not guess"
    assert "owner || UNKNOWN_PLUGIN" in body


def test_builtin_allowlist_matches_the_shipped_default(src):
    """The allowlist is hardcoded from the host's `/experimental/tool/ids`; pin it
    so a silent edit is caught, and keep the env override documented (M13).

    Measured again on 2026-10-03 against 1.18.34: the same fourteen ids, in the
    same order. Do not "correct" this list from the binary's TUI view registry —
    that one also carries `batch`, `list`, `lsp` and `plan_exit`, which the host
    does *not* report as tool ids (measured, not inferred)."""
    m = re.search(r'const DEFAULT_BUILTIN_TOOLS\s*=\s*"([^"]*)"', src)
    assert m, "DEFAULT_BUILTIN_TOOLS not found"
    assert m.group(1).split(",") == [
        "invalid", "question", "bash", "read", "glob", "grep", "edit", "write",
        "task", "webfetch", "todowrite", "websearch", "skill", "apply_patch",
    ]
    assert "OPENCODE_SKILL_TRACKER_BUILTIN_TOOLS" in src


def test_the_tracker_excludes_itself_structurally(src):
    """Self-exclusion must compare real paths: the installed plugin directory is
    a symlink farm, so `path.resolve` alone would let the tracker record itself."""
    assert "function samePath(" in src
    assert "fs.realpathSync" in src
    assert "SELF_PATH" in src
    assert "isSkippedPlugin(spec, resolved, exclusions)" in src


# ---------------------------------------------------------------------------
# Schema drift
# ---------------------------------------------------------------------------
def _sql_lines(text: str) -> list[str]:
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


def test_plugin_and_python_schema_do_not_drift(src):
    """The DDL lives in two places and nothing else keeps them in sync.

    `skill_db.SCHEMA_SQL` is documented as a verbatim copy of the plugin's
    TABLES_SQL + VIEWS_SQL. If they drift, the two writers/readers silently
    disagree about the schema: a DB created by one would be missing objects
    the other assumes. Compare normalised line sequences so indentation and
    blank lines stay free.
    """
    tables = re.search(r"const TABLES_SQL\s*=\s*`([^`]*)`", src)
    views = re.search(r"const VIEWS_SQL\s*=\s*`([^`]*)`", src)
    assert tables, "TABLES_SQL not found in the plugin source"
    assert views, "VIEWS_SQL not found in the plugin source"

    plugin_lines = _sql_lines(tables.group(1)) + _sql_lines(views.group(1))
    python_lines = _sql_lines(db.SCHEMA_SQL)

    if plugin_lines != python_lines:
        import difflib

        diff = "\n".join(
            difflib.unified_diff(
                plugin_lines, python_lines, "plugin", "skill_db.SCHEMA_SQL", lineterm=""
            )
        )
        pytest.fail("plugin DDL and skill_db.SCHEMA_SQL have drifted:\n" + diff)


# ---------------------------------------------------------------------------
# Loader contract (OpenCode 1.18.32)
#
# The loader invokes EVERY export of a plugin module as a plugin factory, and
# only stops enumerating when the default export is an object carrying
# `server`. Regression: a bare `export default fn` made it call `__selftest` as
# a factory; the isolation guard threw and the WHOLE plugin failed to load, so
# skill_usage stayed empty. These tests pin the contract that prevents that.
# ---------------------------------------------------------------------------
def _run_bun(script: str, extra_env: dict | None = None, cwd: str | None = None):
    """Run a Bun snippet with the plugin path injected and prod paths scrubbed.

    `cwd` is settable because a plugin spec that starts with `./` is a relative
    path, and a test that wants to prove *which* directory it is relative to has
    to control the process's working directory.
    """
    env = dict(os.environ)
    # Never let the plugin reach the production DB, even by accident.
    env.pop("OPENCODE_SKILL_TRACKER_DB", None)
    env["PLUGIN_PATH"] = str(PLUGIN)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [BUN, "-e", script], capture_output=True, text=True, timeout=60, env=env,
        cwd=cwd,
    )


def _temp_skills(tmp_path) -> str:
    """A skills tree the plugin can scan, so no test reads the developer's own.

    The shipped `__selftest()` asserts at least one skill was scanned, which
    silently made it depend on ~/.config/opencode/skills existing and being
    non-empty.
    """
    root = tmp_path / "skills"
    for name in ("brainstorming", "systematic-debugging"):
        d = root / "personal-skills" / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: fixture\n---\nbody\n", encoding="utf-8"
        )
    return str(root)


def _isolated(tmp_path, isolate_db: bool = True, plugins: str = "") -> dict:
    env = {
        "OPENCODE_SKILL_TRACKER_LOG": str(tmp_path / "plugin.log"),
        # Pin the skills tree for the same reason as the MCP servers below: the
        # plugin's own scan must never depend on what the developer has installed.
        "OPENCODE_SKILL_TRACKER_SKILLS_DIR": _temp_skills(tmp_path),
        # Pin the MCP server set so detection never reads the developer's real
        # opencode.json (which exists and does declare servers).
        "OPENCODE_SKILL_TRACKER_MCP_SERVERS": "test-server",
        # Pin the plugin set too. The empty default means "load no plugins",
        # so no test ever scans or records the developer's real plugins; the
        # plugin tests below pass an explicit local spec instead.
        "OPENCODE_SKILL_TRACKER_PLUGINS": plugins,
    }
    if isolate_db:
        env["OPENCODE_SKILL_TRACKER_DB"] = str(tmp_path / "iso.db")
    return env


@requires_bun
def test_default_export_is_server_module(tmp_path):
    """`default` must be {id, server} — that is what makes the loader short-circuit."""
    script = """
    const mod = await import(process.env.PLUGIN_PATH);
    const d = mod.default;
    if (!d || typeof d !== 'object') { console.log('FAIL: default is not an object'); process.exit(1); }
    if (typeof d.server !== 'function') { console.log('FAIL: default.server is not a function'); process.exit(1); }
    console.log('OK id=' + d.id);
    """
    r = _run_bun(script, _isolated(tmp_path))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "OK id=skill-tracker" in r.stdout


@requires_bun
def test_loader_does_not_enumerate_exports(tmp_path):
    """Mirror OpenCode's Qy/Zy: assert the server short-circuit is taken.

    If it is NOT taken the loader enumerates every export as a factory; the
    `ENUMERATED` branch below reproduces that and will fail loudly, which is
    exactly the bug that silently killed recording.
    """
    script = """
    const mod = await import(process.env.PLUGIN_PATH);
    const d = mod.default;
    // mirror rQ(mod, spec, 'server', 'detect')
    let detected;
    if (d && typeof d === 'object' && ('id' in d || 'server' in d || 'tui' in d)) {
      const server = 'server' in d ? d.server : undefined;
      if (typeof server === 'function') detected = d;
    }
    if (!detected) {
      // mirror Zy: call every export as a plugin factory
      for (const k of Object.keys(mod)) {
        const v = mod[k];
        const fn = typeof v === 'function' ? v
          : (v && typeof v === 'object' && typeof v.server === 'function' ? v.server : null);
        if (!fn) { console.log('FAIL non-function export: ' + k); process.exit(2); }
        console.log('ENUMERATED ' + k);
        try {
          await fn({ directory: '/tmp/x', worktree: '/tmp/x', client: {}, $: () => {} }, {});
        } catch (e) {
          console.log('FAIL export threw: ' + k + ': ' + e.message); process.exit(3);
        }
      }
      console.log('NO_SHORTCIRCUIT');
      process.exit(0);
    }
    console.log('SHORTCIRCUIT');
    """
    r = _run_bun(script, _isolated(tmp_path))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "SHORTCIRCUIT" in r.stdout
    assert "NO_SHORTCIRCUIT" not in r.stdout, (
        "loader would enumerate exports and call them as factories:\n" + r.stdout
    )


@requires_bun
def test_a_hand_run_selftest_never_writes_the_shared_plugin_log(tmp_path):
    """The documented recipe isolates the database and says nothing about the log.

    That is how four `[err] selftest FAIL:` lines landed in the owner's live
    `~/.config/opencode/logs/skill-tracker.log` on 2026-10-04: `doctor`'s
    `log.errors` counts them forever, and a green run left no trace at all. The
    plugin has to isolate its own log, because the runner clearly will not.
    """
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    env = _isolated(tmp_path)
    env["OPENCODE_SKILL_TRACKER_CONFIG_DIR"] = str(cfg)
    del env["OPENCODE_SKILL_TRACKER_LOG"]          # the hand-run shape
    r = _run_bun("""
    const m = await import(process.env.PLUGIN_PATH);
    const ok = await m.__selftest();
    console.log(ok ? 'SELFTEST_OK' : 'SELFTEST_FAILED');
    """, env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "SELFTEST_OK" in r.stdout, r.stdout
    assert "FAIL " not in r.stdout, r.stdout

    shared = cfg / "logs" / "skill-tracker.log"
    assert not shared.exists(), \
        f"selftest wrote into the config's shared log: {shared.read_text()[:400]}"
    scratch = tmp_path / "skill-tracker-selftest.log"
    assert scratch.is_file(), "a successful selftest must leave its own trace"
    text = scratch.read_text(encoding="utf-8")
    assert "selftest start" in text and "selftest done" in text, text[:300]
    assert "[err]" not in text, text[:300]


@requires_bun
def test_an_explicit_selftest_log_is_honoured_not_overridden(tmp_path):
    """The redirect is a default, not a demand: the suite's own harness sets a path."""
    chosen = tmp_path / "chosen.log"
    env = _isolated(tmp_path)
    env["OPENCODE_SKILL_TRACKER_LOG"] = str(chosen)
    r = _run_bun("""
    const m = await import(process.env.PLUGIN_PATH);
    const ok = await m.__selftest();
    console.log(ok ? 'SELFTEST_OK' : 'SELFTEST_FAILED');
    """, env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert chosen.is_file(), "an explicit OPENCODE_SKILL_TRACKER_LOG must win"
    assert "selftest done" in chosen.read_text(encoding="utf-8")
    assert not (tmp_path / "skill-tracker-selftest.log").exists()


@requires_bun
def test_selftest_still_refuses_without_isolated_db(tmp_path):
    """The H2 guard must survive: a bare `__selftest()` never touches prod."""
    script = """
    const mod = await import(process.env.PLUGIN_PATH);
    try {
      await mod.__selftest();
      console.log('FAIL: selftest did not refuse');
      process.exit(1);
    } catch (e) {
      console.log('REFUSED: ' + e.message);
    }
    """
    # deliberately NO OPENCODE_SKILL_TRACKER_DB
    r = _run_bun(script, _isolated(tmp_path, isolate_db=False))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Selftest requires isolated database" in r.stdout


@requires_bun
def test_selftest_is_green_end_to_end(tmp_path):
    """Run the shipped selftest exactly as documented, against a temp DB.

    This is the smoke test a user runs by hand, so it must actually pass — and
    it must cover the plugin stream (tables, attribution, the `(unknown)`
    upgrade, the fail-closed command path and the three new views).
    """
    script = """
    const mod = await import(process.env.PLUGIN_PATH);
    const ok = await mod.__selftest();
    console.log(ok ? 'SELFTEST_OK' : 'SELFTEST_FAILED');
    process.exit(ok ? 0 : 1);
    """
    r = _run_bun(script, _isolated(tmp_path))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "SELFTEST_OK" in r.stdout, r.stdout + r.stderr
    assert "FAIL " not in r.stdout, "an assertion inside __selftest failed:\n" + r.stdout

    m = re.search(r"\n(\d+)/(\d+) passed", r.stdout)
    assert m, r.stdout
    assert m.group(1) == m.group(2), r.stdout
    # 62+ assertions ship today (53 before the subagent drive, 61 after it, +1 for
    # the log-isolation guard). The floor is deliberately just under the real count:
    # a deleted assertion should fail here, not silently lower the bar.
    assert int(m.group(2)) >= 62, f"selftest lost assertions: {m.group(0)}"


@requires_bun
def test_init_failure_is_retryable(tmp_path):
    """A failed init() must not permanently disable recording (M9).

    `initDone` used to be set *before* init() did any work, so any early
    failure left the flag true and every later attempt became a silent no-op
    — recording would stay dead for the whole process with nothing but a log
    line. Here the DB's parent is a regular file, so opening/schema creation
    fails on the first factory call; removing that blocker must let the
    second call succeed.
    """
    blocker = tmp_path / "blocker"
    blocker.write_text("")  # a file where a directory must be
    script = """
    const fs = await import('node:fs');
    const mod = await import(process.env.PLUGIN_PATH);
    const input = { directory: '/tmp/x', worktree: '/tmp/x', client: {}, $: () => {} };

    const first = await mod.default.server(input, {});
    if (typeof first['tool.execute.after'] === 'function') {
      console.log('FAIL: init reported success against an unusable DB path');
      process.exit(1);
    }
    console.log('FIRST_FAILED_AS_EXPECTED');

    fs.unlinkSync(process.env.OPENCODE_SKILL_TRACKER_BLOCKER);
    const second = await mod.default.server(input, {});
    if (typeof second['tool.execute.after'] !== 'function') {
      console.log('FAIL: init did not retry after the failure was removed');
      process.exit(2);
    }
    console.log('RETRIED_OK');
    """
    r = _run_bun(script, {
        "OPENCODE_SKILL_TRACKER_LOG": str(tmp_path / "plugin.log"),
        "OPENCODE_SKILL_TRACKER_DB": str(blocker / "iso.db"),
        "OPENCODE_SKILL_TRACKER_BLOCKER": str(blocker),
        "OPENCODE_SKILL_TRACKER_PLUGINS": "",
    })
    assert r.returncode == 0, r.stdout + r.stderr
    assert "FIRST_FAILED_AS_EXPECTED" in r.stdout, r.stdout + r.stderr
    assert "RETRIED_OK" in r.stdout, r.stdout + r.stderr


@requires_bun
def test_git_timeout_is_enforced_and_killed(tmp_path):
    """A hanging git must be killed at the deadline, not left running.

    A fake `git` that `exec sleep 30` is put first on PATH. If the timeout
    still only raced a `setTimeout` (the old behaviour) the hook would block
    for the full 30s; with an abort signal the child dies at ~500ms.
    """
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    fake_git = fakebin / "git"
    fake_git.write_text("#!/bin/sh\nexec sleep 30\n")
    fake_git.chmod(0o755)

    script = """
    const t0 = Date.now();
    const mod = await import(process.env.PLUGIN_PATH);
    const plugin = await mod.default.server(
      { directory: '/tmp/x', worktree: '/tmp/x', client: {} }, {}
    );
    if (typeof plugin['tool.execute.after'] !== 'function') {
      console.log('FAIL: no hooks registered');
      process.exit(1);
    }
    const sid = 'sess-timeout', cid = 'call-timeout';
    await plugin['tool.execute.before'](
      { tool: 'skill', sessionID: sid, callID: cid }, { args: { name: 'brainstorming' } }
    );
    await plugin['tool.execute.after'](
      { tool: 'skill', sessionID: sid, callID: cid, args: { name: 'brainstorming' } },
      { title: '', output: '', metadata: {} }
    );
    const ms = Date.now() - t0;
    console.log('ELAPSED_MS=' + ms);
    if (ms > 3000) { console.log('FAIL: git was not killed at the deadline'); process.exit(2); }
    if (ms < 300) { console.log('FAIL: fake git did not run; timeout path untested'); process.exit(3); }
    console.log('TIMEOUT_ENFORCED');
    """
    r = _run_bun(script, {
        **_isolated(tmp_path),
        "PATH": f"{fakebin}{os.pathsep}{os.environ.get('PATH', '')}",
    })
    assert r.returncode == 0, r.stdout + r.stderr
    assert "TIMEOUT_ENFORCED" in r.stdout, r.stdout + r.stderr


@requires_bun
def test_mcp_calls_are_recorded_and_arg_values_never_are(tmp_path):
    """End-to-end MCP capture through the real hooks.

    Asserts the row shape AND that no argument *value* reached the database —
    the whole point of recording key names only.
    """
    script = """
    const mod = await import(process.env.PLUGIN_PATH);
    const plugin = await mod.default.server(
      { directory: '/tmp/x', worktree: '/tmp/x', client: {} }, {}
    );
    const sid = 'sess-mcp', cid = 'call-mcp';
    const args = { identifier: 'LEAK-ME', query: 'ALSO-LEAK' };
    await plugin['tool.execute.before'](
      { tool: 'test-server_read_note', sessionID: sid, callID: cid }, { args }
    );
    await plugin['tool.execute.after'](
      { tool: 'test-server_read_note', sessionID: sid, callID: cid, args }, {}
    );
    // A builtin tool must be ignored entirely.
    await plugin['tool.execute.before'](
      { tool: 'bash', sessionID: sid, callID: 'call-sh' }, { args: { command: 'x' } }
    );
    await plugin['tool.execute.after'](
      { tool: 'bash', sessionID: sid, callID: 'call-sh', args: { command: 'x' } }, {}
    );
    console.log('HOOKS_OK');
    """
    r = _run_bun(script, _isolated(tmp_path))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "HOOKS_OK" in r.stdout, r.stdout + r.stderr

    c = sqlite3.connect(str(tmp_path / "iso.db"))
    try:
        rows = c.execute(
            "SELECT server_name, tool_name, trigger_type, status, arg_names FROM mcp_usage"
        ).fetchall()
        assert rows == [
            ("test-server", "read_note", "tool_call", "success", '["identifier","query"]')
        ], rows
        assert c.execute("SELECT COUNT(*) FROM skill_usage").fetchone()[0] == 0
        dump = "\n".join(c.iterdump())
        assert "LEAK-ME" not in dump, "an argument value reached the database"
        assert "ALSO-LEAK" not in dump, "an argument value reached the database"
    finally:
        c.close()


FAKE_PLUGIN_SRC = """
export const FakePlugin = async () => ({
  tool: {
    fake_compress: { description: "squeeze", parameters: {}, execute: async () => {} },
  },
  command: {
    "fake:run": { template: "run it" },
  },
});
"""


@requires_bun
def test_plugin_tools_and_commands_are_attributed_end_to_end(tmp_path):
    """End-to-end plugin capture through the real hooks and the real scan.

    A throwaway local plugin is scanned for its surface; then:
      * a scanned tool is attributed to that plugin,
      * a scanned command is recorded from `command.execute.before` (a command
        has no completion event, so its status stays `unknown`),
      * a builtin tool is ignored,
      * an unattributed tool is still recorded as `(unknown)` (fail-open),
      * an unattributed command is NOT recorded (fail-closed).
    """
    fake = tmp_path / "fake-plugin.js"
    fake.write_text(FAKE_PLUGIN_SRC)
    spec = f"file://{fake}"
    # A local `file://` spec resolves to the bare file path (no scheme).
    name = str(fake)

    script = """
    const mod = await import(process.env.PLUGIN_PATH);
    const plugin = await mod.default.server(
      { directory: '/tmp/x', worktree: '/tmp/x', client: {} }, {}
    );
    const sid = 'sess-plug';
    // A scanned plugin tool: before+after => one attributed row.
    await plugin['tool.execute.before'](
      { tool: 'fake_compress', sessionID: sid, callID: 'c1' }, { args: { secret: 'PLUGVALUE' } }
    );
    await plugin['tool.execute.after'](
      { tool: 'fake_compress', sessionID: sid, callID: 'c1', args: { secret: 'PLUGVALUE' } }, {}
    );
    // A builtin tool: never recorded.
    await plugin['tool.execute.before'](
      { tool: 'bash', sessionID: sid, callID: 'c2' }, { args: { command: 'echo' } }
    );
    await plugin['tool.execute.after'](
      { tool: 'bash', sessionID: sid, callID: 'c2', args: { command: 'echo' } }, {}
    );
    // An unattributed tool: recorded as (unknown), fail-open.
    await plugin['tool.execute.before'](
      { tool: 'mystery_tool', sessionID: sid, callID: 'c3' }, { args: {} }
    );
    await plugin['tool.execute.after'](
      { tool: 'mystery_tool', sessionID: sid, callID: 'c3', args: {} }, {}
    );
    // A scanned command: recorded, status unknown.
    await plugin['command.execute.before']({ command: 'fake:run', sessionID: sid, arguments: [] });
    // An unattributed command: dropped, fail-closed.
    await plugin['command.execute.before']({ command: 'init', sessionID: sid, arguments: [] });
    console.log('HOOKS_OK');
    """
    r = _run_bun(script, _isolated(tmp_path, plugins=spec))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "HOOKS_OK" in r.stdout, r.stdout + r.stderr

    c = sqlite3.connect(str(tmp_path / "iso.db"))
    try:
        rows = c.execute(
            "SELECT plugin_name, kind, item_name, trigger_type, status FROM plugin_usage"
            " ORDER BY item_name"
        ).fetchall()
        assert rows == [
            (name, "command", "fake:run", "command_call", "unknown"),
            (name, "tool", "fake_compress", "tool_call", "success"),
            ("(unknown)", "tool", "mystery_tool", "tool_call", "success"),
        ], rows

        inv = c.execute(
            "SELECT plugin_name, source, skipped, tools, commands FROM plugin_inventory"
        ).fetchall()
        assert inv == [
            (name, "local", 0, '["fake_compress"]', '["fake:run"]')
        ], inv

        assert c.execute("SELECT COUNT(*) FROM skill_usage").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM mcp_usage").fetchone()[0] == 0
        dump = "\n".join(c.iterdump())
        assert "PLUGVALUE" not in dump, "an argument value reached the database"
    finally:
        c.close()


def _npm_plugin(tmp_path, name, files, version="1.0.0", main="dist/plugin.js"):
    """A package laid out the way OpenCode caches npm plugins.

    `~/.cache/opencode/packages/<spec>/node_modules/<name>/…` — the nesting is
    what `resolvePluginEntry` walks, so a fixture that flattens it proves nothing
    about resolution. Returns the packages dir to point
    `OPENCODE_SKILL_TRACKER_PACKAGES_DIR` at and the spec to ask for.
    """
    pkg = tmp_path / "packages" / f"{name}@{version}" / "node_modules" / name
    pkg.mkdir(parents=True)
    manifest = {"name": name, "version": version, "main": main}
    (pkg / "package.json").write_text(json.dumps(manifest), encoding="utf-8")
    for rel, body in files.items():
        target = pkg / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    return str(tmp_path / "packages"), f"{name}@{version}"


def _inventory(tmp_path):
    c = sqlite3.connect(str(tmp_path / "iso.db"))
    try:
        return c.execute(
            "SELECT plugin_name, version, source, tools, commands FROM plugin_inventory"
        ).fetchall()
    finally:
        c.close()


_INIT_SCRIPT = """
const mod = await import(process.env.PLUGIN_PATH);
await mod.default.server(
  { directory: '/tmp/x', worktree: '/tmp/x', client: {} }, {}
);
console.log('INIT_OK');
"""


@requires_bun
def test_scan_follows_the_entry_shim_one_hop(tmp_path):
    """The bug the user sees: a plugin that registers a tool shows `tools=[]`.

    `opencode-mem@2.28.1`'s `main` is a 407-byte shim that pulls the real
    registration in with `await import("./index.js")`, so scanning only the entry
    found nothing and every `memory` call landed in `(unknown)`. The scan now
    follows the entry's own relative imports, one hop, and still reads only files
    inside the package: `../escape.js` is *not* this plugin's surface.
    """
    packages, spec = _npm_plugin(tmp_path, "shimmy", {
        "dist/plugin.js": (
            'const { ShimPlugin } = await import("./index.js");\n'
            'const { V2 } = await import("./v2/plugin.js");\n'
            'import("../escape.js");\n'
            "export default { ...V2, server: ShimPlugin };\n"
        ),
        "dist/index.js": (
            "export const ShimPlugin = async () => ({\n"
            "  tool: {\n"
            "    memory: tool({\n"
            '      description: `Manage memory (MATCH USER LANGUAGE: ${x("en")})`,\n'
            "      args: { action: { type: \"string\" } },\n"
            "    }),\n"
            "  },\n"
            "});\n"
        ),
        "dist/v2/plugin.js": "export const V2 = { id: 'shimmy-v2' };\n",
    })
    # The file the `../escape.js` import would reach, outside the package dir.
    outside = Path(packages) / "shimmy@1.0.0" / "node_modules" / "escape.js"
    outside.write_text(
        "export const E = { tool: { should_not_appear: tool({}) } };\n", encoding="utf-8"
    )

    r = _run_bun(_INIT_SCRIPT, {
        **_isolated(tmp_path, plugins=spec),
        "OPENCODE_SKILL_TRACKER_PACKAGES_DIR": packages,
    })
    assert r.returncode == 0, r.stdout + r.stderr
    assert "INIT_OK" in r.stdout, r.stdout + r.stderr

    rows = _inventory(tmp_path)
    assert rows == [
        # `LANGUAGE` is a ternary inside a description string, not a tool id;
        # `should_not_appear` is outside the package and must stay unread.
        (spec, "1.0.0", "npm", '["memory"]', "[]"),
    ], rows


@requires_bun
def test_scan_hops_are_bounded(tmp_path):
    """The hop is single and capped, so discovery cannot crawl a dependency tree.

    Five sibling chunks, each with one tool: only the first three hops are read.
    Without a bound a plugin with a hundred chunks would make every OpenCode
    start read all of them.
    """
    files = {"dist/plugin.js": "".join(
        f'await import("./part{i}.js");\n' for i in range(1, 6)
    )}
    files.update({
        f"dist/part{i}.js": f"export const p{i} = {{ tool: {{ tool{i}: tool({{}}) }} }};\n"
        for i in range(1, 6)
    })
    packages, spec = _npm_plugin(tmp_path, "manyhops", files)

    r = _run_bun(_INIT_SCRIPT, {
        **_isolated(tmp_path, plugins=spec),
        "OPENCODE_SKILL_TRACKER_PACKAGES_DIR": packages,
    })
    assert r.returncode == 0, r.stdout + r.stderr

    tools = json.loads(_inventory(tmp_path)[0][3])
    assert tools == ["tool1", "tool2", "tool3"], tools


@requires_bun
def test_scan_reads_a_large_entry_file_in_full(tmp_path):
    """The size cap is on the hops, never on the entry.

    DCP's bundle is ~300 KiB and holds its whole surface there. A first attempt at
    the one-hop fix capped every file it opened, which silently dropped a plugin
    that was being attributed correctly — the inventory went to `tools=[]`.
    """
    body = "// pad\n" * 40_000          # > 256 KiB of comment
    files = {
        "dist/plugin.js": (
            "export default { server: async () => ({\n"
            "  tool: {\n"
            "    compress: cond ? createMessageTool() : createRangeTool(),\n"
            "  },\n"
            "  command: {\n"
            '    "dcp-compress": { template: "x" },\n'
            "  },\n"
            "});\n"
        ) + body,
    }
    packages, spec = _npm_plugin(tmp_path, "bigentry", files)

    r = _run_bun(_INIT_SCRIPT, {
        **_isolated(tmp_path, plugins=spec),
        "OPENCODE_SKILL_TRACKER_PACKAGES_DIR": packages,
    })
    assert r.returncode == 0, r.stdout + r.stderr

    row = _inventory(tmp_path)[0]
    assert json.loads(row[3]) == ["compress"], row
    assert json.loads(row[4]) == ["dcp-compress"], row
    # The pad is what makes the entry bigger than the hop cap; assert it really is,
    # or this test would pass against a capped read.
    entry = Path(packages) / "bigentry@1.0.0" / "node_modules" / "bigentry" / "dist" / "plugin.js"
    assert entry.stat().st_size > 256 * 1024, entry.stat().st_size


@requires_bun
def test_relative_plugin_spec_resolves_against_the_config_dir(tmp_path):
    """`./plugins/claude-mem.js` is config-relative — that is the whole bug.

    OpenCode resolves a `./` plugin spec against the config directory, and the file
    really is at `<CFG_DIR>/plugins/claude-mem.js` (466 KB, registering
    `claude_mem_search`). The tracker resolved it against the *process* CWD instead,
    found nothing, and recorded the plugin with no source, no version and no
    surface — so every tool it registered fell to `(unknown)` and the Plugins page
    had nothing to show. Reproduced on 2026-10-03 against the installed plugin.
    """
    cfg = tmp_path / "cfg"
    (cfg / "plugins").mkdir(parents=True)
    (cfg / "plugins" / "rel.js").write_text(
        'export default { server: async () => ({\n'
        '  tool: { rel_search: { description: "x" } },\n'
        '}); };\n',
        encoding="utf-8",
    )
    r = _run_bun(_INIT_SCRIPT, {
        **_isolated(tmp_path, plugins="./plugins/rel.js"),
        "OPENCODE_SKILL_TRACKER_CONFIG_DIR": str(cfg),
    })
    assert r.returncode == 0, r.stdout + r.stderr
    assert _inventory(tmp_path) == [
        (str(cfg / "plugins" / "rel.js"), None, "local", '["rel_search"]', "[]"),
    ], _inventory(tmp_path)


@requires_bun
def test_relative_plugin_spec_is_not_resolved_against_the_process_cwd(tmp_path):
    """The same relative path, existing only under the CWD, must stay unresolved.

    Without this pair the test above could pass by accident — the bug *was* resolving
    against `process.cwd()`. An unresolved plugin is still listed, with no source and
    no surface: that is the inventory's fail-open shape, and it is what makes the
    `(unknown)` attribution honest rather than a guess.
    """
    work = tmp_path / "work"
    (work / "plugins").mkdir(parents=True)
    (work / "plugins" / "rel.js").write_text(
        'export default { server: async () => ({\n'
        '  tool: { rel_search: { description: "x" } },\n'
        '}); };\n',
        encoding="utf-8",
    )
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    r = _run_bun(_INIT_SCRIPT, {
        **_isolated(tmp_path, plugins="./plugins/rel.js"),
        "OPENCODE_SKILL_TRACKER_CONFIG_DIR": str(cfg),
    }, cwd=str(work))
    assert r.returncode == 0, r.stdout + r.stderr
    assert _inventory(tmp_path) == [
        ("./plugins/rel.js", None, None, "[]", "[]"),
    ], _inventory(tmp_path)


_INIT_SCRIPT_AT = """
const mod = await import(process.env.PLUGIN_PATH);
await mod.default.server(
  { directory: process.env.PROJECT_DIR, worktree: process.env.PROJECT_DIR, client: {} }, {}
);
console.log('INIT_OK');
"""


def _config_env(tmp_path, cfg, **extra):
    """An env that reads a *real* config file instead of the spec override.

    `_isolated` pins `OPENCODE_SKILL_TRACKER_PLUGINS` precisely so no test can see
    the developer's own plugins — and that override also disables the prune. These
    tests drop it and point the config directory at a temp dir instead, so nothing
    here can reach the real `~/.config/opencode`.
    """
    env = _isolated(tmp_path)
    env.pop("OPENCODE_SKILL_TRACKER_PLUGINS")
    env["OPENCODE_SKILL_TRACKER_CONFIG_DIR"] = str(cfg)
    env.update(extra)
    return env


def _write_config(cfg, specs, name="opencode.json"):
    (cfg / name).write_text(json.dumps({"$schema": "x", "plugin": specs}), encoding="utf-8")


def _inventory_scopes(tmp_path):
    c = sqlite3.connect(str(tmp_path / "iso.db"))
    try:
        return dict(c.execute("SELECT plugin_name, scope FROM plugin_inventory").fetchall())
    finally:
        c.close()


@requires_bun
def test_the_inventory_prunes_a_plugin_the_global_config_dropped(tmp_path):
    """`Loaded` used to mean "seen at some point since the DB was created".

    Two inits against one database, with the global config changed in between —
    which is exactly what a user does when they remove a plugin. Every session
    reads the same global config, so a `global` row this init did not see is a
    plugin that is gone, and it goes with it. The surviving row keeps its scope.
    """
    cfg = tmp_path / "cfg"
    (cfg / "plugins").mkdir(parents=True)
    (cfg / "plugins" / "a.js").write_text(FAKE_PLUGIN_SRC, encoding="utf-8")
    (cfg / "plugins" / "b.js").write_text(FAKE_PLUGIN_SRC, encoding="utf-8")
    a = str(cfg / "plugins" / "a.js")
    b = str(cfg / "plugins" / "b.js")

    _write_config(cfg, [a, b])
    r = _run_bun(_INIT_SCRIPT, _config_env(tmp_path, cfg))
    assert r.returncode == 0 and "INIT_OK" in r.stdout, r.stdout + r.stderr
    assert _inventory_scopes(tmp_path) == {a: "global", b: "global"}

    _write_config(cfg, [a])
    r = _run_bun(_INIT_SCRIPT, _config_env(tmp_path, cfg))
    assert r.returncode == 0 and "INIT_OK" in r.stdout, r.stdout + r.stderr
    assert _inventory_scopes(tmp_path) == {a: "global"}, "the dropped plugin must be gone"


@requires_bun
def test_a_project_scoped_plugin_survives_a_session_started_elsewhere(tmp_path):
    """A plugin listed by *one* project's config must not be deleted by another.

    The prune's premise is "every session reads this file". That is true of the
    global config and the local plugin directory, and false of a project config —
    so those rows are kept, and the TUI can say where each entry came from.
    """
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    here = tmp_path / "proj-here"
    here.mkdir()
    elsewhere = tmp_path / "proj-elsewhere"
    elsewhere.mkdir()
    (here / ".opencode").mkdir()
    plugin = tmp_path / "project-plugin.js"
    plugin.write_text(FAKE_PLUGIN_SRC, encoding="utf-8")

    _write_config(cfg, [])
    _write_config(here / ".opencode", [f"file://{plugin}"])
    r = _run_bun(_INIT_SCRIPT_AT, {
        **_config_env(tmp_path, cfg), "PROJECT_DIR": str(here),
    })
    assert r.returncode == 0 and "INIT_OK" in r.stdout, r.stdout + r.stderr
    assert _inventory_scopes(tmp_path) == {str(plugin): "project"}

    # Same global config (which lists nothing), started from a project that has no
    # config of its own: the project row must still be there afterwards.
    r = _run_bun(_INIT_SCRIPT_AT, {
        **_config_env(tmp_path, cfg), "PROJECT_DIR": str(elsewhere),
    })
    assert r.returncode == 0 and "INIT_OK" in r.stdout, r.stdout + r.stderr
    assert _inventory_scopes(tmp_path) == {
        str(plugin): "project",
    }, "a row another project listed must never be pruned"


@requires_bun
def test_the_env_spec_override_never_prunes(tmp_path):
    """Tests and sandboxes pin the spec list by hand; that says nothing about
    what is installed, so it must not delete anything."""
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    plugin = tmp_path / "kept.js"
    plugin.write_text(FAKE_PLUGIN_SRC, encoding="utf-8")
    _write_config(cfg, [f"file://{plugin}"])
    r = _run_bun(_INIT_SCRIPT, _config_env(tmp_path, cfg))
    assert r.returncode == 0, r.stdout + r.stderr
    assert _inventory_scopes(tmp_path) == {str(plugin): "global"}

    env = _isolated(tmp_path, plugins="")      # the override, deliberately empty
    env["OPENCODE_SKILL_TRACKER_CONFIG_DIR"] = str(cfg)
    r = _run_bun(_INIT_SCRIPT, env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _inventory_scopes(tmp_path) == {str(plugin): "global"}, \
        "an empty override must not empty the inventory"


@requires_bun
def test_an_unreadable_global_config_never_prunes(tmp_path):
    """A broken or deleted config file looks exactly like "no plugins installed".

    The difference matters: one empties the inventory, the other should leave it
    alone. `globalReadable` is the only thing the prune trusts.
    """
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    plugin = tmp_path / "kept.js"
    plugin.write_text(FAKE_PLUGIN_SRC, encoding="utf-8")
    good = cfg / "opencode.json"
    _write_config(cfg, [f"file://{plugin}"])
    assert _run_bun(_INIT_SCRIPT, _config_env(tmp_path, cfg)).returncode == 0
    assert _inventory_scopes(tmp_path) == {str(plugin): "global"}

    good.write_text("{ this is not json", encoding="utf-8")
    assert _run_bun(_INIT_SCRIPT, _config_env(tmp_path, cfg)).returncode == 0
    assert _inventory_scopes(tmp_path) == {str(plugin): "global"}, \
        "an unparsable config must not be read as 'the user removed everything'"

    good.unlink()
    assert _run_bun(_INIT_SCRIPT, _config_env(tmp_path, cfg)).returncode == 0
    assert _inventory_scopes(tmp_path) == {str(plugin): "global"}, \
        "a missing config must not be read as 'the user removed everything'"


@requires_bun
def test_scan_reports_tool_ids_not_parameter_names(tmp_path):
    """`args: { query: … }` names a parameter, not a tool.

    The tool block used to be read with a regex that stopped at the first `}`, so
    the keys of a nested `args` object were collected as tools: claude-mem listed
    `query` beside its real `claude_mem_search`, and a ternary inside a description
    string once listed `LANGUAGE`. Depth-aware scanning separates them — and DCP's
    `cond_tool: cond ? a() : b()` has to survive it, because that shape has already
    cost one regression.
    """
    packages, spec = _npm_plugin(tmp_path, "depthy", {
        "dist/plugin.js": (
            "export default { server: async () => ({\n"
            "  tool: {\n"
            "    real_tool: { description: \"x ? NOISE : y\", args:"
            " { query: S.string(), path: S.string() } },\n"
            "    cond_tool: cond ? a() : b(),\n"
            "    builder_tool: tool({ description: `d ? NOISE : e`,"
            " args: { field: S.string() } }),\n"
            "  },\n"
            "});\n"
        ),
    })
    r = _run_bun(_INIT_SCRIPT, {
        **_isolated(tmp_path, plugins=spec),
        "OPENCODE_SKILL_TRACKER_PACKAGES_DIR": packages,
    })
    assert r.returncode == 0, r.stdout + r.stderr
    assert json.loads(_inventory(tmp_path)[0][3]) == [
        "builder_tool", "cond_tool", "real_tool",
    ], _inventory(tmp_path)


@requires_bun
def test_scan_finds_a_tool_registered_behind_a_spread(tmp_path):
    """DCP registers one level deeper than the ordinary shape, and must still work.

        tool: { ...cond && { compress: a() : b() } }

    A first attempt at the depth-aware scan read only the outermost level and lost
    `compress` outright — the same regression the tool-key rule had from the other
    direction. The rule is therefore "the shallowest level that has keys at all",
    not "level one".
    """
    packages, spec = _npm_plugin(tmp_path, "spready", {
        "dist/plugin.js": (
            "export default { server: async () => ({\n"
            "  tool: {\n"
            "    ...cfg.compress.enabled && {\n"
            "      compress: cfg.mode === \"message\" ? mk1(ctx) : mk2(ctx),\n"
            "    },\n"
            "  },\n"
            "});\n"
        ),
    })
    r = _run_bun(_INIT_SCRIPT, {
        **_isolated(tmp_path, plugins=spec),
        "OPENCODE_SKILL_TRACKER_PACKAGES_DIR": packages,
    })
    assert r.returncode == 0, r.stdout + r.stderr
    assert json.loads(_inventory(tmp_path)[0][3]) == ["compress"], _inventory(tmp_path)


@requires_bun
def test_excluded_and_self_plugins_are_listed_but_not_attributed(tmp_path):
    """The two exclusions still appear in the inventory (with skipped=1) so the
    TUI can show what was deliberately left out — but their tools are never
    attributed, and the tracker never records itself."""
    notifier = tmp_path / "opencode-notifier.js"
    notifier.write_text(FAKE_PLUGIN_SRC)
    other = tmp_path / "other-plugin.js"
    other.write_text(FAKE_PLUGIN_SRC)

    script = """
    const mod = await import(process.env.PLUGIN_PATH);
    const plugin = await mod.default.server(
      { directory: '/tmp/x', worktree: '/tmp/x', client: {} }, {}
    );
    await plugin['tool.execute.before'](
      { tool: 'fake_compress', sessionID: 's', callID: 'c1' }, { args: {} }
    );
    await plugin['tool.execute.after'](
      { tool: 'fake_compress', sessionID: 's', callID: 'c1', args: {} }, {}
    );
    console.log('HOOKS_OK');
    """
    r = _run_bun(script, _isolated(
        tmp_path,
        plugins=f"file://{notifier},file://{other},file://{PLUGIN}",
    ))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "HOOKS_OK" in r.stdout, r.stdout + r.stderr

    c = sqlite3.connect(str(tmp_path / "iso.db"))
    try:
        inv = dict(c.execute("SELECT plugin_name, skipped FROM plugin_inventory").fetchall())
        assert inv.get(str(notifier)) == 1, "the notifier must be listed as skipped"
        assert inv.get(str(PLUGIN)) == 1, "the tracker must exclude itself"
        assert inv.get(str(other)) == 0, "an ordinary plugin is not skipped"

        # `other` and `notifier` both declare fake_compress; the last writer wins
        # in the map, so assert only that SOME owner was attributed, never the
        # sentinel.
        owners = [r[0] for r in c.execute("SELECT DISTINCT plugin_name FROM plugin_usage")]
        assert "(unknown)" not in owners, owners
    finally:
        c.close()


# ---------------------------------------------------------------------------
# Permission rejection — shapes taken verbatim from a live OpenCode 1.18.33
# session on 2026-10-01 (`/tmp/st-live-20261001/probe.log`):
#
#   permission.asked   {always, id, metadata, patterns, permission, sessionID,
#                       tool: {messageID, callID}}
#   permission.replied {reply: "reject", requestID, sessionID}
#
# The tracker used to listen for `permission.updated` and read `permissionID` /
# `response` — none of which exist — so every real rejection fell through and
# the `denied` status was never written by any OpenCode, however many denials
# happened. These tests pin the real shape so that cannot come back.
# ---------------------------------------------------------------------------
PERMISSION_REJECT_SCRIPT = """
const mod = await import(process.env.PLUGIN_PATH);
const plugin = await mod.default.server(
  { directory: '/tmp/x', worktree: '/tmp/x', client: {} }, {}
);
const sid = 'sess-perm', cid = 'call-perm';

// An MCP call in flight, then rejected.
await plugin['tool.execute.before'](
  { tool: 'test-server_read_note', sessionID: sid, callID: cid },
  { args: { identifier: 'x' } }
);
await plugin.event({ event: { type: 'permission.asked', properties: {
  id: 'per_a1', sessionID: sid, permission: 'mcp',
  patterns: ['mcp:test-server:*'], always: ['mcp:test-server:*'],
  tool: { messageID: 'msg_a1', callID: cid },
  metadata: {}, time: { created: 1 }
}}});
await plugin.event({ event: { type: 'permission.replied', properties: {
  sessionID: sid, requestID: 'per_a1', reply: 'reject'
}}});

// A builtin (bash) rejection must stay unrecorded: the tracker measures skills,
// MCP and plugins only, and bash is none of those.
const bsid = 'sess-bash', bcid = 'call-bash';
await plugin['tool.execute.before'](
  { tool: 'bash', sessionID: bsid, callID: bcid }, { args: { command: 'echo hi' } }
);
await plugin.event({ event: { type: 'permission.asked', properties: {
  id: 'per_b2', sessionID: bsid, permission: 'bash',
  patterns: ['echo hi'], always: ['echo *'],
  tool: { messageID: 'msg_b2', callID: bcid },
  metadata: { command: 'echo hi' }, time: { created: 2 }
}}});
await plugin.event({ event: { type: 'permission.replied', properties: {
  sessionID: bsid, requestID: 'per_b2', reply: 'reject'
}}});

// An approval must not be mistaken for a rejection.
const asid = 'sess-allow', acid = 'call-allow';
await plugin['tool.execute.before'](
  { tool: 'test-server_ping', sessionID: asid, callID: acid }, { args: {} }
);
await plugin.event({ event: { type: 'permission.asked', properties: {
  id: 'per_c3', sessionID: asid, permission: 'mcp',
  patterns: ['mcp:test-server:*'], always: [],
  tool: { messageID: 'msg_c3', callID: acid },
  metadata: {}, time: { created: 3 }
}}});
await plugin.event({ event: { type: 'permission.replied', properties: {
  sessionID: asid, requestID: 'per_c3', reply: 'always'
}}});
console.log('DONE');
"""


@requires_bun
def test_permission_rejection_writes_a_denied_row(tmp_path):
    r = _run_bun(PERMISSION_REJECT_SCRIPT, _isolated(tmp_path))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "DONE" in r.stdout, r.stdout + r.stderr

    c = sqlite3.connect(str(tmp_path / "iso.db"))
    try:
        mcp = c.execute(
            "SELECT server_name, tool_name, trigger_type, status, session_id "
            "FROM mcp_usage ORDER BY id"
        ).fetchall()
        assert ("test-server", "read_note", "permission_denied", "denied", "sess-perm") in mcp, mcp
        # the approved call was not recorded as a denial
        assert not [
            r for r in mcp if r[4] == "sess-allow" and r[3] in ("denied",)
        ], mcp
        # ...and the builtin rejection produced nothing anywhere
        assert c.execute("SELECT COUNT(*) FROM skill_usage").fetchone()[0] == 0, "a bash denial leaked in"
        assert c.execute("SELECT COUNT(*) FROM plugin_usage").fetchone()[0] == 0, "a bash denial leaked in"
        assert not [r for r in mcp if r[4] == "sess-bash"], f"bash is not measurable: {mcp}"
    finally:
        c.close()


@requires_bun
def test_permission_events_still_supported_under_the_old_names(tmp_path):
    """`permission.updated` is what the SDK types declare, so keep accepting it.

    Whatever the host calls it, a rejection must land: the old spelling is
    handled alongside the one this OpenCode actually emits.
    """
    script = """
    const mod = await import(process.env.PLUGIN_PATH);
    const plugin = await mod.default.server(
      { directory: '/tmp/x', worktree: '/tmp/x', client: {} }, {}
    );
    const sid = 'sess-old', cid = 'call-old';
    await plugin['tool.execute.before'](
      { tool: 'test-server_read_note', sessionID: sid, callID: cid }, { args: {} }
    );
    await plugin.event({ event: { type: 'permission.updated', properties: {
      id: 'per_old', sessionID: sid, type: 'mcp', pattern: ['mcp:test-server:*'],
      callID: cid, metadata: {}, time: { created: 1 }
    }}});
    await plugin.event({ event: { type: 'permission.replied', properties: {
      sessionID: sid, permissionID: 'per_old', response: 'reject'
    }}});
    console.log('DONE');
    """
    r = _run_bun(script, _isolated(tmp_path))
    assert r.returncode == 0, r.stdout + r.stderr
    c = sqlite3.connect(str(tmp_path / "iso.db"))
    try:
        rows = c.execute(
            "SELECT server_name, tool_name, status FROM mcp_usage"
        ).fetchall()
        assert ("test-server", "read_note", "denied") in rows, rows
    finally:
        c.close()


# ---------------------------------------------------------------------------
# The permission `title` is prose, so the writer must not keep it
# ---------------------------------------------------------------------------
def test_the_writer_never_names_a_permission_title(src):
    """A `title` that is not written needs no scrubbing to stay unwritten.

    `skill_db.METADATA_SCRUB_KEYS` declares that a `title` key must not remain in
    the database, while the writer used to put one there on every denied call.
    A promise that only holds after someone remembers to run `skillt
    scrub-metadata` is not a promise, so the three `permission.ask` branches name
    `source` and may not name `title`. Static because the behavioural checks below
    need Bun, and CI runs without it.
    """
    assert "title: hookInput.title" not in src, "a permission title is being read"
    assert "metadata.title" not in src, "the writer still admits a title key"

    m = re.search(r"const metadata = \{(.*?)\n  \};", src, re.S)
    assert m, "buildMetadata's literal moved — the whitelist below is no longer what is stored"
    keys = {line.strip().rstrip(",").split(":")[0].strip()
            for line in m.group(1).splitlines() if line.strip()}
    assert keys == {"tool", "call_id", "agent", "model", "branch", "source"}, keys
    admitted = set(re.findall(r"if \(meta && meta\.(\w+)\)", src))
    assert admitted == {"error"}, f"optional metadata keys admitted beyond the whitelist: {admitted}"


PERMISSION_TITLE_SCRIPT = """
const mod = await import(process.env.PLUGIN_PATH);
const plugin = await mod.default.server(
  { directory: '/tmp/x', worktree: '/tmp/x', client: {} }, {}
);
const title = process.env.TITLE_SENTINEL;

// The mcp branch of permission.ask, with the host's own self-identifying namespace.
await plugin['permission.ask'](
  { sessionID: 'sess-pt-mcp', callID: 'call-pt-mcp', type: 'mcp:test-server:*',
    pattern: ['mcp:test-server:*'], title: title },
  { status: 'deny' }
);
// The skill branch: `type` is the skill tool id, the name arrives in `pattern`.
await plugin['permission.ask'](
  { sessionID: 'sess-pt-skill', callID: 'call-pt-skill', type: 'skill',
    pattern: 'brainstorming', title: title },
  { status: 'deny' }
);
console.log('DONE');
"""

TITLE_SENTINEL = "0deadbeef-title-carries-user-text-and-must-not-land"


@requires_bun
def test_a_denied_call_stores_no_title_but_still_stores_the_denial(tmp_path):
    """The denial is the measurement; the text beside it in the payload is not.

    Deleting the `title` write must not delete the row, so this drives both
    reachable `permission.ask` branches with a sentinel title and then reads the
    database back: the denied rows are there, their metadata holds only the
    whitelist, and no file the database touches contains the sentinel. WAL means
    the newest bytes live in `-wal`, so the main file alone would prove nothing.
    """
    env = _isolated(tmp_path)
    env["TITLE_SENTINEL"] = TITLE_SENTINEL
    r = _run_bun(PERMISSION_TITLE_SCRIPT, env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "DONE" in r.stdout, r.stdout + r.stderr

    conn = sqlite3.connect(str(tmp_path / "iso.db"))
    try:
        mcp = conn.execute(
            "SELECT server_name, status, metadata FROM mcp_usage"
        ).fetchall()
        skill = conn.execute(
            "SELECT skill_name, status, metadata FROM skill_usage"
        ).fetchall()
        assert ("test-server", "denied") in [(s, st) for s, st, _ in mcp], mcp
        assert ("brainstorming", "denied") in [(n, st) for n, st, _ in skill], skill
        for _, _, metadata in mcp + skill:
            keys = set(json.loads(metadata))
            assert keys == {"tool", "call_id", "agent", "model", "branch", "source"}, keys
        # `source` still says which path wrote it, so the row keeps its provenance.
        assert json.loads(mcp[0][2])["source"] == "permission.ask", mcp
    finally:
        conn.close()

    blob = b"".join(p.read_bytes() for p in Path(tmp_path).glob("iso.db*"))
    assert TITLE_SENTINEL.encode() not in blob, "a permission title reached the database"
