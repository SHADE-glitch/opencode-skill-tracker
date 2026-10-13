#!/usr/bin/env python3
"""ocut-stats — headless reports over the tracker database.

Standard library only (no textual needed). Subcommands:

    stats                 overview of all five categories
    tools|skills|agents|mcp|plugins   per-category tables
    models                per-model token totals from transcript rawUsage
    show <kind> <name>    recent calls for one entity
    recent                most recent tool calls
    inventory             what is installed / available, used vs unused
    export                dump every table as JSON
    health                DB + data-source health check

The interactive view lives in ocut-tui.py; this is for scripting and CI.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ocut_db as db  # noqa: E402


# --- formatting ------------------------------------------------------------


def ts(v):
    if not v:
        return "-"
    return datetime.fromtimestamp(v / 1000).strftime("%Y-%m-%d %H:%M")


def ms(v):
    if v is None:
        return "-"
    if v < 1000:
        return f"{v}ms"
    return f"{v / 1000:.1f}s"


def fmt_n(v):
    """Render a token count: ``-`` for NULL (never a fabricated 0), else grouped."""
    if v is None:
        return "-"
    return f"{v:,}"


def _rendered_width(headers, rows) -> int:
    """Width (chars) of the table ``table()`` would print, including separators."""
    widths = [len(h) for h in headers]
    for r in rows:
        for i, c in enumerate(r):
            widths[i] = max(widths[i], len("" if c is None else str(c)))
    return sum(widths) + 2 * (len(headers) - 1)


def table(headers, rows, aligns=None):
    rows = [[("" if c is None else str(c)) for c in r] for r in rows]
    widths = [len(h) for h in headers]
    for r in rows:
        for i, c in enumerate(r):
            widths[i] = max(widths[i], len(c))
    aligns = aligns or ["<"] * len(headers)

    def line(cells):
        return "  ".join(
            f"{c:{aligns[i]}{widths[i]}}" for i, c in enumerate(cells)
        )

    out = [line(headers), "  ".join("-" * w for w in widths)]
    out += [line(r) for r in rows]
    return "\n".join(out)


def table_or(headers, rows, aligns=None, empty="(none)"):
    """``table(...)`` when there is data, else the ``empty`` notice.

    ``table`` always returns at least a header and a separator line, so the
    ``table(...) or "(none)"`` idiom this replaces never reached its right-hand
    side: an empty report printed a bare header with nothing under it. Branch on
    the rows, which is what the fallback was always trying to say.
    """
    return table(headers, rows, aligns) if rows else empty


def _rows(cur, keys):
    return [[r[k] for k in keys] for r in cur]


def _state(row):
    """The `state` cell for an entity row, from the row's trailing flag pair.

    Every entity query ends with `installed, ever_used` and every list here ends
    with those two, so the flags are addressed from the end of the row rather
    than by a magic index per table. The word comes from `db.entity_state`, the
    same place the TUI gets it: one label per state, one function to change it.
    """
    return db.entity_state(row[-2], row[-1])


# --- subcommands -----------------------------------------------------------


def cmd_stats(conn, args):
    o = db.overview(conn)
    print("OpenCode usage — overview")
    print("=" * 60)
    print(f"  sessions           {o['sessions']}")
    print(f"  projects           {o['projects']}")
    print(f"  tool calls         {o['tool_calls']}  ({o['distinct_tools']} distinct tools)")
    print(f"  skill invocations  {o['skills']}")
    print(f"  agent invocations  {o['agents']}")
    print(f"  MCP invocations    {o['mcp']}")
    # "installed", never "used": a passive reader has no evidence that a plugin or
    # a slash command ran, and a label claiming usage would be a claim about data
    # this database does not hold.
    print(f"  plugins installed  {o['plugins_installed']}")
    print(f"  slash cmds installed {o['commands_installed']}")
    print(f"  model steps        {o['model_calls']} "
          f"({o['model_calls_with_usage']} with usage)")
    print(f"  model tokens       in {o['model_prompt_tokens']:,}  "
          f"out {o['model_completion_tokens']:,}  "
          f"total {o['model_total_tokens']:,}")
    # OpenCode splits the prompt the way the cache works: `input` is what was NOT
    # read from the cache, and cache read/write are separate numbers already inside
    # the host's own total. So "miss" is the `in` line above and is not repeated.
    print(f"  model cache        read {o['model_cache_read_tokens']:,}  "
          f"write {o['model_cache_write_tokens']:,}  "
          f"reasoning {o['model_reasoning_tokens']:,}")
    # The host's own per-session rollup: the same measurement the step rows carry,
    # summed one level up. Printed beside, never added to, the step totals.
    print(f"  host rollup        in {o['host_rollup_input_tokens']:,}  "
          f"out {o['host_rollup_output_tokens']:,}  "
          f"cost ${o['host_rollup_cost']:.4f}")
    print(f"  range              {ts(o['first_ts'])} .. {ts(o['last_ts'])}")
    print()

    print("Top tools")
    rows = _rows(db.q_tools(conn, 10), ["tool_name", "calls", "completed",
                                        "errored", "avg_ms", "last_used",
                                        "installed", "ever_used"])
    print(table(["tool", "state", "calls", "ok", "fail", "avg", "last used"],
                [[r[0], _state(r), r[1], r[2], r[3], ms(r[4]), ts(r[5])]
                 for r in rows]))
    print()

    print("Skills")
    rows = _rows(db.q_skills(conn), ["skill", "calls", "last_used",
                                     "installed", "ever_used"])
    print(table_or(["skill", "state", "calls", "last used"],
                   [[r[0], _state(r), r[1], ts(r[2])] for r in rows],
                   empty="  (none)"))
    print()

    print("Agents")
    rows = _rows(db.q_agents(conn), ["agent_type", "calls", "last_used",
                                     "installed", "ever_used"])
    print(table_or(["agent", "state", "calls", "last used"],
                   [[r[0], _state(r), r[1], ts(r[2])] for r in rows],
                   empty="  (none)"))
    print()

    print("Plugins (installed ∪ used, by attributed usage)")
    rows = _rows(db.q_plugins(conn), ["plugin", "uses", "skills", "agents",
                                      "commands", "installed", "ever_used"])
    print(table(["plugin", "state", "uses", "skills", "agents", "cmds"],
                [[r[0], _state(r), r[1], r[2], r[3], r[4]] for r in rows]))
    print()

    print("MCP")
    rows = _rows(db.q_mcp(conn), ["server", "tool", "calls", "last_used",
                                  "installed", "ever_used"])
    if rows:
        print(table(["server", "tool", "state", "calls", "last used"],
                    [[r[0], r[1], _state(r), r[2], ts(r[3])] for r in rows]))
    else:
        print("  (no MCP tool invocations recorded)")


def cmd_tools(conn, args):
    rows = _rows(db.q_tools(conn, args.limit),
                 ["tool_name", "calls", "completed", "errored", "running",
                  "avg_ms", "last_used", "projects", "installed", "ever_used"])
    # `run` is the host's own `running` state, not a third outcome: a call that is
    # still in flight. Counting it as a failure would report an interrupted tool as
    # a broken one.
    print(table(["tool", "state", "calls", "ok", "fail", "run", "avg",
                 "last used", "proj"],
                [[r[0], _state(r), r[1], r[2], r[3], r[4], ms(r[5]), ts(r[6]),
                  r[7]] for r in rows]))


def cmd_skills(conn, args):
    rows = _rows(db.q_skills(conn, args.limit),
                 ["skill", "calls", "completed", "last_used", "projects",
                  "installed", "ever_used"])
    print(table_or(["skill", "state", "calls", "ok", "last used", "proj"],
                   [[r[0], _state(r), r[1], r[2], ts(r[3]), r[4]]
                    for r in rows]))


def cmd_agents(conn, args):
    rows = _rows(db.q_agents(conn, args.limit),
                 ["agent_type", "calls", "last_used", "projects",
                  "installed", "ever_used"])
    print(table_or(["agent", "state", "calls", "last used", "proj"],
                   [[r[0], _state(r), r[1], ts(r[2]), r[3]] for r in rows]))


def cmd_mcp(conn, args):
    rows = _rows(db.q_mcp(conn, args.limit),
                 ["server", "tool", "calls", "completed", "last_used",
                  "installed", "ever_used"])
    print(table_or(["server", "tool", "state", "calls", "ok", "last used"],
                   [[r[0], r[1], _state(r), r[2], r[3], ts(r[4])] for r in rows]))


# Per-model table: (header, q_model_tokens key). Order matters — the trailing
# cache columns are the first dropped when the terminal is narrow.
MODEL_TABLE_COLUMNS = (
    ("model", "model"),
    ("provider", "provider"),
    ("resp", "responses"),
    ("in", "prompt_tokens"),
    ("out", "completion_tokens"),
    ("total", "total_tokens"),
    ("reason", "reasoning_tokens"),
    ("cache read", "cache_read"),
    ("cache write", "cache_write"),
)
# Dropped left-to-right until the table fits; the identity + size columns
# (model / provider / resp / in / out) always survive.
_MODEL_DROP_ORDER = ("cache write", "cache read", "reason")
_MODEL_ALWAYS_KEEP = 5


def _fit_model_columns(width, headers, rows):
    """Reduce (headers, rows) to fit ``width`` by dropping the cache columns.

    Never drops below the first five columns (model / provider / resp / in / out).
    """
    headers = list(headers)
    rows = [list(r) for r in rows]
    for name in _MODEL_DROP_ORDER:
        if _rendered_width(headers, rows) <= width or len(headers) <= _MODEL_ALWAYS_KEEP:
            break
        if name in headers:
            j = headers.index(name)
            headers.pop(j)
            for r in rows:
                r.pop(j)
    return headers, rows


def cmd_models(conn, args):
    """Per-model token totals from the host's own per-step usage records."""
    data = _rows(db.q_model_tokens(conn), [k for _, k in MODEL_TABLE_COLUMNS])
    # model and provider are text; the rest are counts.
    rows = [[r[0] or "?", db.provider_label(r[1])]
            + [fmt_n(c) for c in r[2:]] for r in data]
    headers = [h for h, _ in MODEL_TABLE_COLUMNS]
    width = shutil.get_terminal_size((100, 24)).columns
    headers, rows = _fit_model_columns(width, headers, rows)
    print("Model token usage (source: step-finish parts in opencode.db)")
    print("Total = the host's own step total when present, else the five parts. "
          "Cache hit/miss/write are separate, not in Total.")
    if rows:
        print(table(headers, rows))
    else:
        print("  (no model responses indexed yet)")
    print()
    print("Recent model steps")
    recent = db.q_model_calls(conn, args.limit)
    # `in` is the uncached prompt, so the cache read is the other half of what the
    # model saw and both are shown; `total` is the host's own step total.
    print(table_or(
        ["when", "model", "in", "out", "reason", "total", "cache read",
         "cache write", "prompt total", "finish", "usage", "session"],
        [[ts(r["ts"]), r["model"] or "?", fmt_n(r["prompt_tokens"]),
          fmt_n(r["completion_tokens"]), fmt_n(r["reasoning_tokens"]),
          fmt_n(r["total_tokens"]), fmt_n(r["cache_read"]),
          fmt_n(r["cache_write"]), fmt_n(r["prompt_total_tokens"]),
          r["finish"] or "-", "yes" if r["usage_available"] else "no",
          (r["session_id"] or "")[:8]] for r in recent], empty="  (none)"))


def cmd_plugins(conn, args):
    # `uses`/`last used` come from plugin_usage, written only by
    # plugin/ocut-hook.js; a plugin the hook has not seen shows 0 / "-". Same
    # fields IN THE SAME ORDER as the TUI's Plugins tab, so the two surfaces
    # cannot disagree about what is known -- the claim was written when this table
    # had five columns and the TUI five different ones.
    #
    # `state` sits after the name here for the same reason it does in the TUI,
    # where the reason was the prefix column plan. There is no width trimming in a
    # terminal report, so that reason does not apply: what it gets instead is the
    # one that applies to both -- the label belongs to the name on its left, and a
    # report read six columns later should not have to scroll back to find out.
    rows = _rows(db.q_plugins(conn),
                 ["plugin", "version", "uses", "last_used", "skills", "agents",
                  "commands", "path", "installed", "ever_used"])
    print(table_or(["plugin", "state", "version", "uses", "last used", "skills",
                    "agents", "cmds", "declared as"],
                   [[r[0], _state(r), r[1] or "-", f"{r[2]:,}", ts(r[3]),
                     f"{r[4]:,}", f"{r[5]:,}", f"{r[6]:,}", r[7] or "-"]
                    for r in rows],
                   empty="  (none)"))


def cmd_show(conn, args):
    rows = _rows(db.q_history(conn, args.kind, args.name, args.limit),
                 ["ts", "project", "session_id", "status", "duration_ms"])
    if not rows:
        print(f"no recorded calls for {args.kind} {args.name!r}")
        return
    print(f"{args.kind} {args.name} — {len(rows)} recent calls")
    print(table(["when", "project", "session", "status", "dur"],
                [[ts(r[0]), r[1] or "-", (r[2] or "")[:8], r[3] or "-", ms(r[4])] for r in rows]))


def cmd_recent(conn, args):
    rows = _rows(db.q_recent(conn, args.limit),
                 ["ts", "tool_name", "category", "status", "duration_ms", "project"])
    print(table(["when", "tool", "category", "status", "dur", "project"],
                [[ts(r[0]), r[1], r[2], r[3] or "-", ms(r[4]), r[5] or "-"] for r in rows]))


def cmd_inventory(conn, args):
    rows = _rows(db.q_inventory(conn, args.kind),
                 ["kind", "name", "owner_plugin", "source", "version"])
    print(table(["kind", "name", "owner", "source", "version"],
                [[r[0], r[1], r[2] or "-", r[3], r[4] or "-"] for r in rows]))


def cmd_export(conn, args):
    out = {}
    for view in ("v_tools", "v_skills", "v_agents", "v_mcp", "v_plugins"):
        out[view] = [dict(r) for r in conn.execute(f"SELECT * FROM {view}")]
    out["inventory"] = [dict(r) for r in conn.execute("SELECT * FROM inventory")]
    out["model_tokens"] = [dict(r) for r in db.q_model_tokens(conn)]
    out["model_calls"] = [
        dict(r) for r in conn.execute(
            "SELECT * FROM model_calls ORDER BY ts DESC")
    ]
    out["sessions"] = [
        dict(r) for r in conn.execute("SELECT * FROM sessions ORDER BY ended_at DESC")
    ]
    out["overview"] = db.overview(conn)
    print(json.dumps(out, indent=2, ensure_ascii=False))


def cmd_health(conn, args):
    print("ocut health")
    print("=" * 60)
    print(f"  db path        {args.db}")
    print(f"  host db        {db.HOST_DB} "
          f"({'ok, ' + format(db.HOST_DB.stat().st_size // 1024 // 1024) + ' MiB' if db.HOST_DB.is_file() else 'MISSING'})")
    print(f"  host config    {db.HOST_CONFIG_DIR} "
          f"({'ok' if db.HOST_CONFIG_DIR.is_dir() else 'MISSING'})")
    # The settings surface, named so it can be found without reading the README.
    # A broken file is reported as one, in place, rather than being quietly skipped.
    try:
        cfg = db.load_config()
        state = "" if db.CONFIG_PATH.is_file() else " (absent — defaults)"
        # Every key the loader knows, spelled the way the settings file spells it.
        # A hand-listed line here goes stale one key at a time, and it silently
        # renames what it does print — both are worse than printing nothing at all.
        values = " ".join(f"{k}={v}{'s' if k.endswith('_secs') else ''}"
                          for k, v in sorted(cfg.items()))
        print(f"  settings       {db.CONFIG_PATH}{state} — {values}")
    except db.ConfigError as exc:
        print(f"  settings       NOT READ — {exc}")
    try:
        ver = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        print(f"  schema version {ver[0] if ver else '?'}")
    except sqlite3.Error:
        print("  schema version (missing)")
    # The cursor, in the host's own numbers: what we consumed, what the host has.
    # `note` is where a pruned or renumbered host table says so, so it is printed
    # even when the counts look healthy.
    for name, cur, hcount, hmax, note in conn.execute(
            "SELECT name, cursor, host_count, host_max, note FROM sync_state"
            " ORDER BY name").fetchall():
        extra = f" — {note}" if note else ""
        print(f"  cursor {name:<8} {cur} of host max {hmax} "
              f"({hcount} live rows){extra}")
    if conn.execute("SELECT COUNT(*) FROM sync_state").fetchone()[0] == 0:
        print("  cursor           none tracked (never synced, or a schema bump)")
    o = db.overview(conn)
    print(f"  indexed        {o['tool_calls']} tool calls, {o['sessions']} sessions, "
          f"{o['model_calls']} model steps "
          f"({o['model_calls_with_usage']} with usage)")
    print(f"  last activity  {ts(o['last_ts'])}")
    # The format-change signal. Printing the "0" case too, so the line itself
    # proves the check ran rather than merely being absent when nothing broke.
    if o["unparsed_records"]:
        print(f"  UNPARSED       {o['unparsed_records']} records "
              f"across {o['unparsed_kinds']} reason(s) no handler claims:")
        for row in db.q_unparsed(conn)[:8]:
            print(f"                 {row['count']:>8}  {row['reason']}")
        print("                 (a jump here right after a OpenCode upgrade "
              "means its log format changed)")
    else:
        print("  unparsed       0 — every record type was claimed")


# --- snapshots -------------------------------------------------------------
#
# These two do not follow the ``cmd_x(conn, args)`` shape the reports use: they
# work on the database *file*, and ``restore`` has to run when the database is
# missing or unreadable — which is exactly when the read-only connection the
# reports open would refuse to start.


def cmd_backup(args) -> int:
    path = Path(args.db)
    if not path.exists():
        print(f"no database at {path} — run: ocut sync", file=sys.stderr)
        return 1
    conn = db.open_db(path)
    try:
        dest = db.snapshot(conn, reason="manual")
    finally:
        conn.close()
    print(f"snapshot  {dest}  ({dest.stat().st_size:,} bytes)")
    print(f"          keeping the newest {db.BACKUP_KEEP} under {dest.parent}")
    return 0


def cmd_restore(args) -> int:
    path = Path(args.db)
    found = db.list_backups(path)
    if not args.name:
        if not found:
            print(f"no backups under {path.parent}", file=sys.stderr)
            return 1
        print(f"backups for {path}")
        for item in found:
            kind = "managed" if item.parent.name == "backups" else "hand-made"
            print(f"  {item.name}  {item.stat().st_size:>12,} B  {kind}")
        print("\n  restore one:  ocut restore NAME     (close the TUI first)")
        return 0
    candidate = Path(args.name)
    target = candidate if candidate.is_file() else next(
        (p for p in found if p.name == args.name), None)
    if target is None:
        print(f"no backup named {args.name!r} — list them with: ocut restore",
              file=sys.stderr)
        return 1
    try:
        db.restore(target, db_path=path)
    except sqlite3.Error as exc:
        # Almost always the TUI holding the file open.
        print(f"could not restore over {path}: {exc}\n"
              f"  close the TUI (and stop ocut-sync.timer) and retry",
              file=sys.stderr)
        return 1
    print(f"restored  {path}\n  from      {target}")
    print(f"  the database that was there is kept in {db.backup_dir(path)}")
    return 0


FORMAT_BEGIN = "<!-- BEGIN generated: ocut format -->"
FORMAT_END = "<!-- END generated -->"


def _replace_generated_block(doc: Path, body: str) -> bool:
    """Rewrite the marked block in ``doc``; False when the markers are gone.

    Refusing beats appending: a generated section dropped into hand-written prose
    is how a document ends up stating two things at once.
    """
    text = doc.read_text(encoding="utf-8") if doc.is_file() else ""
    if FORMAT_BEGIN not in text or FORMAT_END not in text:
        return False
    head, _, rest = text.partition(FORMAT_BEGIN + "\n")
    _, tail = rest.split(FORMAT_END, 1)
    doc.write_text(f"{head}{FORMAT_BEGIN}\n{body}{FORMAT_END}{tail}",
                   encoding="utf-8")
    return True


def cmd_format(args) -> int:
    """Print or refresh the OpenCode format-dependency surface.

    Generated from the parser's registry instead of written by hand, because a
    hand-copied list is how `schema_version` became a value nothing read: a
    description that stopped matching the code and stayed checked in anyway.
    """
    body = db.load_sync().format_doc()
    if not args.write:
        print(body, end="")
        return 0
    doc = (Path(__file__).resolve().parents[1]
           / "docs" / "maintenance" / "opencode-format.md")
    if not _replace_generated_block(doc, body):
        print(f"{doc} has no generated block markers — refusing to rewrite it",
              file=sys.stderr)
        return 1
    print(f"updated   {doc}")
    return 0


# --- driver ----------------------------------------------------------------

COMMANDS = {
    "stats": cmd_stats, "tools": cmd_tools, "skills": cmd_skills,
    "agents": cmd_agents, "mcp": cmd_mcp, "plugins": cmd_plugins,
    "models": cmd_models,
    "show": cmd_show, "recent": cmd_recent, "inventory": cmd_inventory,
    "export": cmd_export, "health": cmd_health,
}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="ocut", description="OpenCode usage reports.")
    ap.add_argument("--db", default=str(db.DB_PATH))
    sub = ap.add_subparsers(dest="cmd")

    def sub_with_db(name, **kw):
        """A subparser that also accepts the global ``--db`` AFTER the subcommand.

        ``bin/ocut`` always places the subcommand first (argparse requires it there
        for the subparser to match), so a ``--db`` given *before* the subcommand
        ends up *after* it — and without this the flag is rejected. The top-level
        ``--db`` stays for direct `python3 ocut-stats.py --db X stats` use.
        ``default=argparse.SUPPRESS`` is load-bearing: a plain default here would
        clobber the top-level value with ``DB_PATH`` whenever ``--db`` was given
        only up front.
        """
        p = sub.add_parser(name, **kw)
        p.add_argument("--db", default=argparse.SUPPRESS)
        return p

    for name in ("stats", "health", "plugins", "inventory", "export"):
        p = sub_with_db(name)
        p.add_argument("--kind", default=None, help="inventory only: filter by kind")
    for name in ("tools", "skills", "agents", "mcp", "recent", "models"):
        sub_with_db(name).add_argument("--limit", type=int, default=30)
    sp = sub_with_db("show")
    sp.add_argument("kind", choices=["tool", "skill", "agent", "mcp"])
    sp.add_argument("name")
    sp.add_argument("--limit", type=int, default=50)
    sub_with_db("backup", help="snapshot the database before a risky step")
    rp = sub_with_db("restore", help="restore a snapshot (omit NAME to list)")
    rp.add_argument("name", nargs="?", default=None)
    fp = sub_with_db("format",
                     help="print the OpenCode format-dependency surface")
    fp.add_argument("--write", action="store_true",
                    help="refresh the generated block in docs/maintenance/")

    args = ap.parse_args(argv)
    if not args.cmd:
        args.cmd = "stats"

    # Not through the report connection: restore has to work when the database
    # is missing or corrupt, which is when opening it read-only would fail.
    if args.cmd == "backup":
        return cmd_backup(args)
    if args.cmd == "restore":
        return cmd_restore(args)
    if args.cmd == "format":
        # Reads no database at all — the surface is in the code.
        return cmd_format(args)

    if not Path(args.db).exists():
        print(f"no database at {args.db} — run: ocut sync", file=sys.stderr)
        return 1
    conn = db.open_db(args.db, readonly=True)
    try:
        COMMANDS[args.cmd](conn, args)
    except sqlite3.OperationalError as exc:
        # A database written by an older build of this tool has no table or
        # column the current queries expect, and this CLI opens it read-only — it
        # cannot repair that itself. A traceback hides an actionable one-liner.
        print(f"ocut: this database is older than this build ({exc})\n"
              f"      run: ocut sync", file=sys.stderr)
        return 2
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
