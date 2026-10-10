"""Shared fixtures for the skillt test suite.

`skill_db` imports normally (underscore name). `skill-tui.py` and
`skill-stats.py` are hyphenated and cannot be imported by name, so they are
loaded with importlib when needed.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import skill_db as db  # noqa: E402
import settings as cfg  # noqa: E402


def load_module(filename: str, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


def _make_db(path: str, migrate: bool = True) -> sqlite3.Connection:
    conn = db.open_db(path, readonly=False)
    if migrate:
        db.ensure_schema(conn)
    else:
        conn.executescript(db.SCHEMA_SQL)  # base schema only, pre-migration
    conn.commit()
    return conn


@pytest.fixture
def temp_skills(tmp_path, monkeypatch):
    """A skills tree of known shape, pointed at by db.SKILLS_DIR.

    Never use the real ~/.config/opencode/skills in a test: it changes whenever
    a skill is added, and assertions written against it then fail for reasons
    that have nothing to do with the code under test. Five skills, two
    categories, so the category derivation is actually exercised.
    """
    skills = tmp_path / "skills"
    layout = [
        ("personal-skills", "brainstorming"),
        ("personal-skills", "systematic-debugging"),
        ("open-source-skills", "code-review-and-quality"),
        ("open-source-skills", "executing-plans"),
        ("open-source-skills", "verification-before-completion"),
    ]
    for category, name in layout:
        d = skills / category / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: the {name} skill\n---\nbody of {name}\n",
            encoding="utf-8",
        )
    monkeypatch.setattr(db, "SKILLS_DIR", str(skills))
    return str(skills)


@pytest.fixture(autouse=True)
def isolate_claude_mem_store(tmp_path, monkeypatch):
    """No test may read the developer's own `~/.claude-mem`.

    The neighbour resolves to a well-known default path, so unlike the advisor
    store it needs no configuration to be *found* — which also means `skillt
    doctor`'s `claude_mem.capture` check would appear or vanish depending on what
    this machine has installed. Pinning the env here keeps every test's view
    empty; the tests that want a store pass `db_path=` explicitly.
    """
    monkeypatch.setenv("OPENCODE_SKILL_TRACKER_CLAUDE_MEM_DB",
                       str(tmp_path / "no-claude-mem.db"))
    monkeypatch.delenv("CLAUDE_MEM_DIR", raising=False)


# The three files claude-mem leaves beside its database. Short, synthetic, and in
# the exact shapes the reader parses: a bare `injected` line, one with `source=`,
# `[time] [level] [category] message` log lines, and a pid file with a token.
CM_TRACE_LINES = [
    "2026-10-03T13:20:40.381Z loaded project=opencode",
    "2026-10-03T13:20:41.000Z injected project=opencode len=120",
    "2026-10-03T14:34:21.000Z injected source=search project=opencode len=1440",
]
CM_WORKER_LINES = [
    "[2026-10-03 13:20:40.381] [INFO ] [WORKER] ready",
    "[2026-10-03 13:20:41.381] [ERROR] [CHROMA_SYNC] sync failed",
    "[2026-10-03 13:20:42.381] [ERROR] [CHROMA_SYNC] sync failed again",
]


def write_claude_mem_files(root, *, trace=CM_TRACE_LINES, worker=CM_WORKER_LINES,
                           pid=None, port=37701, dated=True,
                           token="SENTINEL-WORKER-TOKEN"):
    """Write those files under `root`, the directory that holds `claude-mem.db`.

    `None` for `trace`/`worker`/`pid` skips that file, which is how a test asks
    what happens when the neighbour is only partly there. `token` is whatever the
    caller's sentinel happens to be: it must never leave the reader.
    """
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    if trace is not None:
        (root / "inject-trace.log").write_text("\n".join(trace) + "\n", encoding="utf-8")
    if worker is not None:
        logs = root / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        name = "claude-mem-2026-10-03.log" if dated else "manual-restart-203731.log"
        (logs / name).write_text("\n".join(worker) + "\n", encoding="utf-8")
    if pid is not None:
        (root / "worker.pid").write_text(json.dumps({
            "pid": pid, "port": port, "startToken": token,
            "startedAt": "2026-10-03T12:54:49.179Z",
        }), encoding="utf-8")
    return str(root / "claude-mem.db")


class HostServiceQuery(RuntimeError):
    """Raised when a test asks this machine's service manager about something.

    A `RuntimeError` on purpose: `skill-tui.py`'s host checks catch
    `(OSError, subprocess.SubprocessError)` and each of their own wrappers, so an
    exception from those families would be swallowed by the very code the tripwire
    exists to audit. It cannot be swallowed anyway — see the fixture.
    """


@pytest.fixture(autouse=True)
def never_query_the_hosts_service_manager(monkeypatch):
    """No test may ask `systemctl` about this machine.

    `doctor`'s `backups.scheduled` check runs `systemctl --user is-enabled`. A test
    that reaches `_doctor_checks()` without stubbing that helper still *passes* — it
    just reports whatever this machine happens to be running, and would flip between
    green and red depending on whether the owner installed the timer. That is the
    definition of a non-hermetic suite, and `test_doctor._healthy_setup` already
    sets the precedent of stubbing the host-dependent helpers around it. This
    fixture turns that mistake into a failure instead of a quiet reading.

    Two surfaces, because one is not enough:

    * the call raises, so the host is never actually asked, and the developer sees
      the exact line that asked;
    * the call is also **recorded and reported at teardown**, because every host
      question in `doctor` sits inside `except Exception` ("a version check must
      never break doctor"). A tripwire that only raises gets caught there, becomes a
      plausible-looking WARN, and the suite goes green while reading the machine —
      which is precisely how this defect hid the first time it was measured.

    The guard wraps `subprocess.Popen` rather than `subprocess.run` because `run`,
    `call` and `check_output` all construct a `Popen` through the module global, so
    a check added later is caught whichever entry point it uses.

    Scope, stated honestly: this catches **in-process** calls only. A child process
    spawned by `test_dispatcher.py` has its own `subprocess` module and cannot be
    reached from here; those tests assert an exit code and the absence of a
    traceback, never the timer line, so a real read there cannot flip an assertion.

    Yields the list of offending command lines so the test that *deliberately*
    provokes the guard can assert it fired and then clear its own hits.
    """
    hits: list[str] = []
    real_popen = subprocess.Popen

    def guarded(*args, **kwargs):
        argv = args[0] if args else kwargs.get("args")
        line = (" ".join(str(part) for part in argv)
                if isinstance(argv, (list, tuple)) else str(argv))
        if "systemctl" in line:
            hits.append(line)
            raise HostServiceQuery(
                f"a test asked the host's service manager: {line!r}. Stub "
                "`skill_tui._backup_timer_state` (house precedent: "
                "`test_doctor._healthy_setup`) instead of reading this machine.")
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", guarded)
    yield hits
    if hits:
        raise HostServiceQuery(
            f"{len(hits)} systemd call(s) escaped this test's exception handlers: "
            + "; ".join(hits))


@pytest.fixture(autouse=True)
def isolate_config_file(tmp_path, monkeypatch):
    """No test may read or write the developer's own `skillt config` file.

    The settings layer resolves to `~/.local/share/opencode/skillt-config.json`
    with no configuration, which is exactly what makes it dangerous in a suite:
    `skillt config set view.days 45` on this machine would change what every
    unrelated test sees, and the failure would look like the code under test had
    changed. Tests that want a file point the env var at their own path.
    """
    monkeypatch.setenv(cfg.CONFIG_PATH_ENV,
                       str(tmp_path / "isolated-skillt-config.json"))
    for spec in cfg.REGISTRY:
        monkeypatch.delenv(cfg.env_name(spec["key"]), raising=False)


@pytest.fixture(autouse=True)
def isolate_backup_dir(tmp_path, monkeypatch):
    """No test may write into the real ~/.local/share/opencode/backups.

    `backup_db()`'s default target is BACKUP_DIR, and quite a few tests take a
    backup (the Data page's `b`, the pre-delete safety backup, retention runs).
    Pointing it at a per-test directory also makes each test's view of
    `backups.latest` its own instead of this machine's.
    """
    monkeypatch.setattr(db, "BACKUP_DIR", str(tmp_path / "backups"))


@pytest.fixture
def empty_db(tmp_path):
    """A base-schema DB that has NOT been migrated yet.

    Migration tests need the pre-migration state, so this fixture deliberately
    skips ensure_schema().
    """
    path = str(tmp_path / "empty.db")
    conn = _make_db(path, migrate=False)
    conn.close()
    return path


@pytest.fixture
def seeded_db(tmp_path):
    """A migrated DB with crafted usage rows for insight/export tests."""
    path = str(tmp_path / "seeded.db")
    conn = _make_db(path)

    skills = [
        ("grow", "personal-skills", "/s/grow", "grows fast"),
        ("flat", "personal-skills", "/s/flat", "stays flat"),
        ("decline", "open-source-skills", "/s/decline", "declining"),
        ("bad", "open-source-skills", "/s/bad", "always fails"),
        ("ok", "open-source-skills", "/s/ok", "always fine"),
        ("unused", "open-source-skills", "/s/unused", "never used"),
    ]
    for name, cat, p, desc in skills:
        conn.execute(
            "INSERT INTO skills (name, category, path, description) VALUES (?,?,?,?)",
            (name, cat, p, desc),
        )

    def add(name, status, ago_days, trigger="tool_call"):
        conn.execute(
            "INSERT INTO skill_usage (skill_name, session_id, project_path, trigger_type,"
            " status, timestamp, duration_ms, call_id, metadata) "
            "VALUES (?,?,?,?,?, strftime('%Y-%m-%dT%H:%M:%fZ','now',?), 42, ?, ?)",
            (
                name,
                f"s-{name}-{ago_days}",
                "/proj",
                trigger,
                status,
                f"-{ago_days} days",
                f"c-{name}-{ago_days}",
                '{"model":"p/m","agent":"build","branch":"main","summary":"hello"}',
            ),
        )

    for d in range(1, 6):            # 5 recent uses
        add("grow", "success", d)
    add("flat", "success", 2)
    add("flat", "success", 3)
    add("flat", "success", 40)
    add("flat", "success", 41)
    add("decline", "success", 5)
    for d in (35, 36, 37, 38, 39):
        add("decline", "success", d)
    for d in (1, 2, 3):              # 100% failure
        add("bad", "error", d)
    for d in (1, 2, 3):
        add("ok", "success", d)
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def seeded_mcp_db(tmp_path):
    """A migrated DB with crafted MCP rows.

    Deliberately separate from `seeded_db`: every count in that fixture is
    asserted exactly, so adding MCP rows to it would ripple through unrelated
    tests.
    """
    path = str(tmp_path / "seeded-mcp.db")
    conn = _make_db(path)

    def add(server, tool, status, ago_days, duration=100, session=None,
            project="/proj", args='["identifier"]'):
        conn.execute(
            "INSERT INTO mcp_usage (server_name, tool_name, session_id, project_path,"
            " trigger_type, status, timestamp, duration_ms, call_id, arg_names, metadata) "
            "VALUES (?,?,?,?,?,?, strftime('%Y-%m-%dT%H:%M:%fZ','now',?),?,?,?,?)",
            (
                server,
                tool,
                session or f"s-{server}-{tool}-{ago_days}",
                project,
                "tool_call",
                status,
                f"-{ago_days} days",
                duration,
                f"c-{server}-{tool}-{ago_days}",
                args,
                '{"model":"p/m","agent":"build","branch":"main"}',
            ),
        )

    # basic-memory: a busy tool, a flaky tool, and a denied one.
    for d in range(1, 4):
        add("basic-memory", "read_note", "success", d, duration=10)
    add("basic-memory", "search_notes", "success", 2, duration=500)
    add("basic-memory", "search_notes", "error", 3, duration=900)
    add("basic-memory", "write_note", "denied", 4, duration=None, args=None)
    # playwright: older usage only, so it falls outside the 30-day window.
    add("playwright", "browser_click", "success", 45, duration=2000)
    # The permission path only knows the server.
    add("context7", "*", "denied", 5, duration=None, args=None)

    conn.commit()
    conn.close()
    return path


@pytest.fixture
def seeded_plugin_db(tmp_path):
    """A migrated DB with crafted plugin rows and an inventory.

    Separate from `seeded_db` and `seeded_mcp_db` for the same reason: every
    count in those fixtures is asserted exactly.
    """
    path = str(tmp_path / "seeded-plugin.db")
    conn = _make_db(path)

    def add(plugin, kind, item, status, ago_days, duration=100, session=None,
            project="/proj", trigger="tool_call"):
        conn.execute(
            "INSERT INTO plugin_usage (plugin_name, kind, item_name, session_id,"
            " project_path, trigger_type, status, timestamp, duration_ms, call_id, metadata) "
            "VALUES (?,?,?,?,?,?,?, strftime('%Y-%m-%dT%H:%M:%fZ','now',?),?,?,?)",
            (
                plugin,
                kind,
                item,
                session or f"s-{plugin}-{item}-{ago_days}",
                project,
                trigger,
                status,
                f"-{ago_days} days",
                duration,
                f"c-{plugin}-{kind}-{item}-{ago_days}",
                '{"model":"p/m","agent":"build","branch":"main"}',
            ),
        )

    dcp = "@tarquinen/opencode-dcp@3.2.0"
    conductor = "opencode-conductor-plugin"

    # DCP: a busy tool plus one error, so success rate is not 100%.
    for d in range(1, 4):
        add(dcp, "tool", "compress", "success", d, duration=10)
    add(dcp, "tool", "compress", "error", 4, duration=900)
    # conductor: commands only, and only ever 'unknown' status (no after-hook).
    add(conductor, "command", "conductor:status", "unknown", 2, duration=None,
        trigger="command_call")
    # An unresolved tool lands in the unknown bucket.
    add("(unknown)", "tool", "mystery_tool", "success", 3)
    # Older than the 30-day window.
    add(dcp, "tool", "expand", "success", 45, duration=2000)

    inventory = [
        (dcp, "3.2.0", "npm", 0, '["compress","expand"]', '["dcp-compress"]', "global"),
        (conductor, None, "npm", 0, "[]", '["conductor:status"]', "project"),
        ("/cfg/plugin/skill-tracker.js", None, "local", 1, "[]", "[]", "localdir"),
        ("@mohak34/opencode-notifier@0.4.0", "0.4.0", "npm", 1, "[]", "[]", "global"),
    ]
    for name, version, source, skipped, tools, commands, scope in inventory:
        conn.execute(
            "INSERT INTO plugin_inventory (plugin_name, version, source, skipped, tools,"
            " commands, scope, first_seen, last_seen) "
            "VALUES (?,?,?,?,?,?,?, strftime('%Y-%m-%dT%H:%M:%fZ','now','-10 days'),"
            " strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
            (name, version, source, skipped, tools, commands, scope),
        )

    conn.commit()
    conn.close()
    return path


@pytest.fixture
def legacy_db(tmp_path):
    """A DB as an older version of the tool left it: no MCP or plugin objects.

    Derived by dropping those objects from the current base schema, so it
    keeps tracking SCHEMA_SQL instead of duplicating an old copy of the DDL.
    """
    path = str(tmp_path / "legacy.db")
    conn = _make_db(path, migrate=False)
    conn.executescript(
        "DROP VIEW IF EXISTS v_mcp_totals;"
        "DROP VIEW IF EXISTS v_mcp_last30;"
        "DROP VIEW IF EXISTS v_mcp_history;"
        "DROP VIEW IF EXISTS v_plugin_totals;"
        "DROP VIEW IF EXISTS v_plugin_last30;"
        "DROP VIEW IF EXISTS v_plugin_history;"
        "DROP TABLE IF EXISTS mcp_usage;"
        "DROP TABLE IF EXISTS plugin_usage;"
        "DROP TABLE IF EXISTS plugin_inventory;"
    )
    conn.commit()
    conn.close()
    return path
