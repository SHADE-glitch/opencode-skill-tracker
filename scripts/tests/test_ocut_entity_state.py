"""Tests for the `state` column every entity row now carries.

The panels list the union of what is installed (`inventory`, rewritten on every
sync) and what has ever been used (the usage tables). Before this, that `UNION`
erased the one piece of evidence that separates a name still on disk from one
that is gone: an installed-and-unused entity and an uninstalled one whose calls
stayed both rendered as a row with a zero count, and the only difference was a
`-` where `last used` should be. A user's reading of that is "I deleted it, why
is it still listed", and the database had no way to answer it either.

These cases pin the three states and the two flags they are derived from, and
they are deliberately read back through the real `q_*` functions rather than
through the values a case just inserted, so a flag that stops reaching a panel
fails here.
"""

import subprocess
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ocut_db as db  # noqa: E402

SCRIPTS = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location(
    "ocut_state_tui", str(SCRIPTS / "ocut-tui.py"))
tui = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tui)


# --- fixture ----------------------------------------------------------------

# kind -> (query, the row field that names it). MCP is keyed by (server, tool)
# and the others by a single name, which is why the lookup below special-cases it.
_QUERY = {
    "tool": (db.q_tools, "tool_name"),
    "skill": (db.q_skills, "skill"),
    "agent": (db.q_agents, "agent_type"),
    "plugin": (db.q_plugins, "plugin"),
    "mcp": (db.q_mcp, "server"),
}


def _table(kind: str) -> tuple[str, str]:
    """The usage table and name column behind one entity kind."""
    return {
        "tool": ("tool_calls", "tool_name"),
        "skill": ("skill_usage", "skill"),
        "agent": ("agent_usage", "agent_type"),
        "plugin": ("plugin_usage", "plugin"),
        "mcp": ("mcp_usage", "server"),
    }[kind]


@pytest.fixture
def conn(tmp_path):
    c = db.open_db(tmp_path / "state.db")
    db.ensure_schema(c)
    yield c
    c.close()


def install(c, kind, name, owner="", version=None):
    c.execute(
        "INSERT OR REPLACE INTO inventory(kind, name, owner_plugin, version, path,"
        " source) VALUES(?,?,?,?,NULL,'fixture')", (kind, name, owner, version))


def used(c, kind, name, ts=1_700_000_000_000, n=1):
    """Record `n` calls against a name, the way the reader would."""
    table, column = _table(kind)
    cols = [f"call_id", column]
    row = [f"{kind}-{name}-{ts}-0", name]
    if kind == "mcp":
        cols.append("tool")
        row.append(f"{name}_tool")
    cols += ["session_id", "ts"]
    row += ["ses_1", ts]
    for i in range(n):
        row[0] = f"{kind}-{name}-{ts}-{i}"
        c.execute(f"INSERT OR REPLACE INTO {table}({','.join(cols)})"
                  f" VALUES({','.join('?' * len(cols))})", row)
    c.commit()


def states(c, kind):
    """{name: label} for every row the panel would show."""
    query, key = _QUERY[kind]
    return {r[key]: db.entity_state(r["installed"], r["ever_used"])
            for r in query(c)}


# --- the three states --------------------------------------------------------

@pytest.mark.parametrize("kind", sorted(_QUERY))
def test_a_name_with_history_and_no_inventory_row_is_removed(conn, kind):
    """The state the panels could not express before: `inventory` is a snapshot of
    what is installed *now*, so a missing row is the only evidence of removal, and
    without this label a removed entity and an installed-but-idle one look alike.
    """
    used(conn, kind, "gone")
    assert states(conn, kind)["gone"] == db.ENTITY_STATE_REMOVED


@pytest.mark.parametrize("kind", sorted(_QUERY))
def test_an_installed_entity_that_was_never_called_is_installed(conn, kind):
    install(conn, kind, "fresh")
    assert states(conn, kind)["fresh"] == db.ENTITY_STATE_INSTALLED


@pytest.mark.parametrize("kind", sorted(_QUERY))
def test_a_live_entity_in_use_is_in_use(conn, kind):
    install(conn, kind, "live")
    used(conn, kind, "live")
    assert states(conn, kind)["live"] == db.ENTITY_STATE_USED


@pytest.mark.parametrize("kind", sorted(_QUERY))
def test_removing_an_entity_does_not_remove_its_history(conn, kind):
    """`installed` is what changes and only that: the row keeps its counts, which
    is the whole point of a union -- a deleted skill's history is the only record
    that it existed.
    """
    install(conn, kind, "was-here")
    used(conn, kind, "was-here", n=3)
    before = _QUERY[kind][0](conn)[0]["calls" if kind != "plugin" else "uses"]
    conn.execute("DELETE FROM inventory")
    conn.commit()
    assert states(conn, kind)["was-here"] == db.ENTITY_STATE_REMOVED
    after = _QUERY[kind][0](conn)[0]["calls" if kind != "plugin" else "uses"]
    assert after == before == 3


def test_the_state_does_not_follow_the_window(conn):
    """`ever_used` comes from an un-windowed scan of the usage tables, not from
    the panel's call count. Reading it off the count would relabel every tool
    used last week "installed" the moment the range narrowed to a day.
    """
    install(conn, "tool", "yesterday")
    used(conn, "tool", "yesterday", ts=1_700_000_000_000)
    row = db.q_tools(conn, start_ts=1_900_000_000_000)[0]
    assert row["calls"] == 0, "the window should have emptied the count"
    assert db.entity_state(row["installed"], row["ever_used"]) == db.ENTITY_STATE_USED


def test_removed_outranks_in_use(conn):
    """A name that is not on disk is not "in use" even though its history says
    otherwise: that half of the label is the one a person can act on.
    """
    used(conn, "tool", "uninstalled-tool", n=5)
    assert db.entity_state(0, 1) == db.ENTITY_STATE_REMOVED
    assert db.entity_state(1, 0) == db.ENTITY_STATE_INSTALLED
    assert db.entity_state(1, 1) == db.ENTITY_STATE_USED


# --- the flags reach the rows -------------------------------------------------

def test_a_name_on_both_sides_is_counted_once(conn):
    """Regression: projecting the flags onto the raw UNION instead of aggregating
    them leaves an installed-and-used name with two rows in the names CTE, and
    the LEFT JOIN then counts its usage once per row -- two plugin uses read as
    four.
    """
    install(conn, "plugin", "alpha")
    used(conn, "plugin", "alpha", n=2)
    install(conn, "tool", "bash")
    used(conn, "tool", "bash", n=2)
    row = db.q_plugins(conn)[0]
    assert (row["uses"], row["installed"], row["ever_used"]) == (2, 1, 1)
    assert db.q_tools(conn)[0]["calls"] == 2


def test_mcp_rows_inherit_their_servers_install_state(conn):
    """MCP is keyed by (server, tool) but the config declares servers, so the
    lookup cannot be a flag carried through the UNION -- and a server that was
    configured and never called must not be relabelled "in use" by the flag being
    hard-wired.
    """
    install(conn, "mcp", "live-server")
    install(conn, "mcp", "idle-server")
    used(conn, "mcp", "live-server")
    used(conn, "mcp", "retired-server")
    rows = {r["server"]: db.entity_state(r["installed"], r["ever_used"])
            for r in db.q_mcp(conn)}
    assert rows["live-server"] == db.ENTITY_STATE_USED
    assert rows["idle-server"] == db.ENTITY_STATE_INSTALLED
    assert rows["retired-server"] == db.ENTITY_STATE_REMOVED
    idle = [r for r in db.q_mcp(conn) if r["server"] == "idle-server"][0]
    assert idle["tool"] is None, "a configured-but-unused server lists once"


def test_a_plugin_known_only_as_an_owner_is_not_installed(conn):
    """`owner_plugin` names a plugin that ships a tool without having a
    `kind='plugin'` row. It is not evidence of installation -- only the host's own
    config says a plugin is installed -- so this arm contributes `installed = 0`
    rather than borrowing the owner's.
    """
    install(conn, "tool", "plugin-tool", owner="orphan")
    install(conn, "plugin", "declared")
    rows = {r["plugin"]: db.entity_state(r["installed"], r["ever_used"])
            for r in db.q_plugins(conn)}
    assert rows["orphan"] == db.ENTITY_STATE_REMOVED
    assert rows["declared"] == db.ENTITY_STATE_INSTALLED


def test_the_state_is_derived_not_stored(conn):
    """No column anywhere: the two flags are what `inventory` and the usage
    tables already say, and a third copy could only ever disagree with them.
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(inventory)")}
    assert "installed" not in cols and "ever_used" not in cols
    assert db.ENTITY_STATE_COLUMN == "state"


# --- the views ---------------------------------------------------------------

@pytest.mark.parametrize("view", ["v_tools", "v_skills", "v_agents", "v_mcp",
                                 "v_plugins"])
def test_every_entity_view_carries_the_two_flags(conn, view):
    """`ocut export` reads the views, not the queries. A flag that reached the
    panels but not the views would make the export silently disagree with the
    screen.
    """
    cols = [c[0] for c in conn.execute(f"SELECT * FROM {view} LIMIT 0").description]
    assert cols[-2:] == ["installed", "ever_used"], cols


def test_the_export_views_agree_with_the_queries(conn):
    install(conn, "skill", "tdd")
    used(conn, "skill", "tdd", n=2)
    used(conn, "skill", "retired")
    q = {r["skill"]: (r["calls"], r["installed"], r["ever_used"])
         for r in db.q_skills(conn)}
    v = {r["skill"]: (r["calls"], r["installed"], r["ever_used"])
         for r in conn.execute("SELECT * FROM v_skills")}
    assert q == v


def test_v_plugins_counts_the_four_kinds_a_plugin_can_own(conn):
    """It counted two of the four. A plugin that ships a skill but no tool was
    reported as shipping nothing, and nothing in this repository says the view is
    only about tools -- `q_plugins` has always counted all four.
    """
    install(conn, "tool", "t1", owner="alpha")
    install(conn, "command", "c1", owner="alpha")
    install(conn, "skill", "s1", owner="alpha")
    install(conn, "agent", "a1", owner="alpha")
    row = conn.execute("SELECT * FROM v_plugins WHERE plugin='alpha'").fetchone()
    assert (row["tools"], row["commands"], row["skills"], row["agents"]) == (1, 1, 1, 1)
    q = {r["plugin"]: r for r in db.q_plugins(conn)}["alpha"]
    assert (q["tools"], q["commands"], q["skills"], q["agents"]) == (1, 1, 1, 1)

# --- the headless surface ---------------------------------------------------

PLUGIN_HEADERS = ["plugin", "state", "version", "uses", "last used", "skills",
                  "agents", "cmds", "declared as"]


def stats(conn, *args) -> str:
    """`ocut <args>` against this case's own database, as a person would run it.

    The commit is load-bearing: this is a second process reading the file, and
    `sqlite3` holds this case's inserts in an open transaction until something says
    otherwise. Without it the child sees a schema and no rows, and the case fails on
    "(none)" — a real symptom of the wrong thing.
    """
    path = Path(conn.execute("PRAGMA database_list").fetchone()[2])
    conn.commit()
    r = subprocess.run([sys.executable, str(SCRIPTS / "ocut-stats.py"),
                        "--db", str(path), *args],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    return r.stdout


def header_line(out: str) -> list[str]:
    """The Plugins header, found by its own separator rule rather than by position.

    A two-word label ("last used") is one column, so token-counting is wrong and
    line-offset is wrong; the line whose every field after the first is a run of
    dashes is the one thing in this output that cannot also be a data row.
    """
    lines = out.splitlines()
    for i, line in enumerate(lines[1:], start=1):
        fields = line.split()
        if len(fields) > 1 and all(set(f) <= {"-"} for f in fields[1:]):
            return lines[i - 1].split()
    raise AssertionError(f"no Plugins header in:\n{out}")


def test_the_headless_plugins_list_is_column_for_column_the_tui(conn):
    """`cmd_plugins` says in its own comment that it prints the same fields, in the
    same order, as the Plugins tab, "so the two surfaces cannot disagree about what
    is known". That sentence was false before either side carried `state`, and it
    would stay false the moment one gained a column the other did not -- so it is
    compared against the tuple rather than trusted.

    Compared after collapsing runs of spaces: the headless side pads to column
    width, so the padding is the one thing that legitimately differs.
    """
    install(conn, "plugin", "alpha", version="1.2.3")
    install(conn, "skill", "s1", owner="alpha")
    out = stats(conn, "plugins")
    assert " ".join(header_line(out)) == " ".join(PLUGIN_HEADERS), out
    assert " ".join(PLUGIN_HEADERS) == " ".join(tui.PLUGINS_COLUMNS), "drifted"


def test_the_headless_plugins_list_prints_a_state_per_row(conn):
    """The count alone cannot say whether a plugin is still there, which is what
    the column added — and it must reach the headless surface too, or a user who
    scripts `ocut plugins` gets the older, less honest answer than the one in the TUI.
    """
    install(conn, "plugin", "alpha", version="1.2.3")
    install(conn, "skill", "s1", owner="alpha")
    used(conn, "plugin", "retired")
    body = [ln for ln in stats(conn, "plugins").splitlines()
            if ln.split() and ln.split()[0] in ("alpha", "retired")]
    assert len(body) == 2, body
    printed = {ln.split()[0]: ln.split()[1] for ln in body}
    assert printed["alpha"] == db.ENTITY_STATE_INSTALLED, body
    assert printed["retired"] == db.ENTITY_STATE_REMOVED, body
