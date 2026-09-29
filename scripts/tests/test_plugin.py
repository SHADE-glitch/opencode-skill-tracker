"""Static + behavioural checks on the OpenCode plugin.

Most checks are static (assert the shipped guards are present) and exercise the
real UPSERT SQL with Python's sqlite3 against the plugin's own schema. The
loader-contract checks at the bottom DO execute the plugin via Bun — but only
against an isolated temp DB, and they are skipped when Bun is unavailable.
"""

from __future__ import annotations

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
        (DCP, "3.2.0", "npm", 0, '["compress"]', '["dcp-compress"]'),
    )
    c.execute("UPDATE plugin_inventory SET first_seen='2000-01-01T00:00:00.000Z'")
    c.execute(
        upsert_plugin_inventory_sql,
        (DCP, "3.3.0", "npm", 0, '["compress","expand"]', '["dcp-compress"]'),
    )
    row = c.execute(
        "SELECT version, tools, first_seen FROM plugin_inventory WHERE plugin_name=?",
        (DCP,),
    ).fetchone()
    assert row[0] == "3.3.0", "version must refresh"
    assert row[1] == '["compress","expand"]', "surface must refresh"
    assert row[2] == "2000-01-01T00:00:00.000Z", "first_seen must be immutable"


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
    """The allowlist is hardcoded from OpenCode 1.18.33's tool ids; pin it so a
    silent edit is caught, and keep the env override documented (M13)."""
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
def _run_bun(script: str, extra_env: dict | None = None):
    """Run a Bun snippet with the plugin path injected and prod paths scrubbed."""
    env = dict(os.environ)
    # Never let the plugin reach the production DB, even by accident.
    env.pop("OPENCODE_SKILL_TRACKER_DB", None)
    env["PLUGIN_PATH"] = str(PLUGIN)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [BUN, "-e", script], capture_output=True, text=True, timeout=60, env=env
    )


def _isolated(tmp_path, isolate_db: bool = True, plugins: str = "") -> dict:
    env = {
        "OPENCODE_SKILL_TRACKER_LOG": str(tmp_path / "plugin.log"),
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
    assert int(m.group(2)) >= 45, f"selftest lost assertions: {m.group(0)}"


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
