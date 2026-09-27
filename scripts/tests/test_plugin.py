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


def _conn():
    c = sqlite3.connect(":memory:")
    c.executescript(db.SCHEMA_SQL)
    return c


def _put(conn, sql, status, duration=None, session="S", call="C", meta="{}"):
    conn.execute(
        sql,
        (None, "skill-x", session, "/p", "tool_call", status, duration, call, meta),
    )


# ---------------------------------------------------------------------------
# Structure
# ---------------------------------------------------------------------------
def test_plugin_registers_all_hooks(src):
    for marker in ("tool.execute.before", "tool.execute.after", "permission.ask", "event:"):
        assert marker in src, f"hook marker missing: {marker}"


def test_long_lived_maps_are_capacity_bounded(src):
    """M8: caches that outlive a session must not grow without limit.

    `branchByDir` and `pendingSkillPerms` are never cleared during a run, so
    a long-lived OpenCode process would leak entries. They must go through
    `setCapped`.
    """
    assert "const MAP_CAP" in src
    assert "function setCapped(" in src
    for map_name in ("branchByDir", "pendingSkillPerms"):
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


def _isolated(tmp_path, isolate_db: bool = True) -> dict:
    env = {"OPENCODE_SKILL_TRACKER_LOG": str(tmp_path / "plugin.log")}
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
