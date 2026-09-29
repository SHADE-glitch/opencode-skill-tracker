#!/usr/bin/env python3
"""
skill-stats.py — query the OpenCode skill usage database.

Read-only by default. Only `delete`, `clear`, `backup` and `vacuum` write.

Database: $OPENCODE_SKILL_TRACKER_DB or ~/.local/share/opencode/skill-usage.db

Usage:
    python3 skill-stats.py                 # all skills: count / last used / category
    python3 skill-stats.py stats           # same as above
    python3 skill-stats.py top [N]         # ranking (default 10)
    python3 skill-stats.py show <skill> [--limit N]
    python3 skill-stats.py recent [N]      # newest records (default 20)
    python3 skill-stats.py delete <skill> [--yes]
    python3 skill-stats.py clear --yes [--all]
    python3 skill-stats.py backup [path]
    python3 skill-stats.py vacuum
    python3 skill-stats.py --self-test
Global flags: --json  --db PATH
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import skill_db as db  # noqa: E402

HOME = db.HOME
DEFAULT_DB = db.DB_PATH
BUSY_TIMEOUT_MS = db.BUSY_TIMEOUT_MS

# Schema, views, connection handling and the destructive write helpers all live
# in `skill_db` now. This module used to carry a third copy of the DDL (after
# the plugin and skill_db), which had to be kept in sync by hand; the aliases
# below preserve its historical surface without the duplicate.
SCHEMA_SQL = db.SCHEMA_SQL
open_db = db.open_db
fmt_time = db.fmt_time
short_session = db.short_session
short_path = db.short_path
rows_to_dicts = db.rows_to_dicts


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def hr(title):
    print(title)
    print("-" * 48)


def missing_db(path, json_mode=False):
    if json_mode:
        print(json.dumps({"error": "no_database", "path": path}))
    else:
        print(f"No database yet at {path}")
        print("Run a skill in OpenCode first (the plugin creates it on startup).")
    return 0


# --------------------------------------------------------------------------
# Read commands
# --------------------------------------------------------------------------
def cmd_stats(conn, args):
    sql = """
    SELECT s.name AS skill_name,
           COALESCE(t.total, 0)   AS total,
           t.last_used            AS last_used,
           s.category             AS category,
           COALESCE(t.success, 0) AS success,
           COALESCE(t.errors, 0)  AS errors,
           COALESCE(t.denied, 0)  AS denied
    FROM skills s
    LEFT JOIN v_skill_totals t ON t.skill_name = s.name
    UNION
    SELECT u.skill_name, t.total, t.last_used, s.category,
           t.success, t.errors, t.denied
    FROM v_skill_totals u
    JOIN v_skill_totals t ON t.skill_name = u.skill_name
    LEFT JOIN skills s ON s.name = u.skill_name
    WHERE u.skill_name NOT IN (SELECT name FROM skills)
    ORDER BY total DESC, skill_name ASC
    """
    rows = conn.execute(sql).fetchall()
    if args.json:
        print(json.dumps(rows_to_dicts(rows), ensure_ascii=False, indent=2))
        return 0

    used = [r for r in rows if r["total"] > 0]
    unused = [r for r in rows if r["total"] == 0]
    hr("Skill usage statistics")
    for r in used:
        print(f"  {r['skill_name']}")
        print(f"    uses      : {r['total']}")
        print(f"    last used : {fmt_time(r['last_used'])}")
        print(f"    category  : {r['category'] or '-'}")
        print(f"    success/error/denied: {r['success']}/{r['errors']}/{r['denied']}")
        print("-" * 48)
    print(f"\nUsed {len(used)} of {len(rows)} skills ({len(unused)} never used)")
    return 0


def cmd_top(conn, args):
    # `top N` (positional) and `top --limit N` are equivalent.
    n = args.n if args.n is not None else (args.limit if args.limit is not None else 10)
    rows = conn.execute(
        "SELECT skill_name, total, last_used FROM v_skill_totals "
        "ORDER BY total DESC, last_used DESC LIMIT ?",
        (n,),
    ).fetchall()
    if args.json:
        print(json.dumps(rows_to_dicts(rows), ensure_ascii=False, indent=2))
        return 0
    hr(f"Top skills (top {n})")
    if not rows:
        print("  (no data)")
        return 0
    for i, r in enumerate(rows, 1):
        print(f"  {i:>2}. {r['skill_name']:<40} {r['total']:>5}   (last: {fmt_time(r['last_used'])})")
    return 0


def cmd_show(conn, args):
    name = args.skill
    limit = args.limit if args.limit is not None else 20
    rows = conn.execute(
        "SELECT timestamp, project_path, session_id, status, duration_ms, "
        "       trigger_type, model, agent, branch "
        "FROM v_skill_history WHERE skill_name = ? ORDER BY timestamp DESC LIMIT ?",
        (name, limit),
    ).fetchall()
    if args.json:
        print(json.dumps(rows_to_dicts(rows), ensure_ascii=False, indent=2))
        return 0
    hr(f"Skill usage history: {name}")
    if not rows:
        print("  (no records)")
        return 0
    for r in rows:
        print(f"  time    : {fmt_time(r['timestamp'])}")
        print(f"  project : {short_path(r['project_path'])}")
        print(f"  session : {short_session(r['session_id'])}")
        print(f"  model   : {r['model'] or '-'}    agent: {r['agent'] or '-'}")
        print(f"  branch  : {r['branch'] or '-'}")
        print(f"  status  : {r['status']}   duration: {r['duration_ms'] if r['duration_ms'] is not None else '-'} ms   ({r['trigger_type']})")
        print("-" * 48)
    print(f"\nShown {len(rows)} record(s)")
    return 0


def cmd_recent(conn, args):
    # `recent N` (positional) and `recent --limit N` are equivalent.
    n = args.n if args.n is not None else (args.limit if args.limit is not None else 20)
    rows = conn.execute(
        "SELECT timestamp, skill_name, project_path, session_id, status, duration_ms "
        "FROM skill_usage ORDER BY timestamp DESC LIMIT ?",
        (n,),
    ).fetchall()
    if args.json:
        print(json.dumps(rows_to_dicts(rows), ensure_ascii=False, indent=2))
        return 0
    hr(f"Recent {n} record(s)")
    if not rows:
        print("  (no records)")
        return 0
    for r in rows:
        print(
            f"  {fmt_time(r['timestamp'])}  {r['skill_name']:<32} "
            f"{short_path(r['project_path'], 28):<28} {short_session(r['session_id']):<10} {r['status']}"
        )
    return 0


# --------------------------------------------------------------------------
# Write commands
# --------------------------------------------------------------------------
def confirm(prompt, assume_yes):
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        print("Refusing to modify without --yes (non-interactive).", file=sys.stderr)
        return False
    try:
        return input(prompt).strip().lower() in ("y", "yes")
    except EOFError:
        return False


def cmd_delete(conn, args):
    """Delete a skill's usage + versions + skills row.

    Delegates to `skill_db.delete_skill`. The old body returned early when the
    skill had no usage rows, so a *never-used* skill — exactly the ones the
    health report tells you to prune — could not be deleted at all; and even on
    success it removed only `skill_usage`, leaving the `skills` row behind to
    reappear as "never used".
    """
    name = args.skill
    usage = conn.execute(
        "SELECT COUNT(*) FROM skill_usage WHERE skill_name = ?", (name,)
    ).fetchone()[0]
    try:
        versions = conn.execute(
            "SELECT COUNT(*) FROM skill_versions WHERE skill_name = ?", (name,)
        ).fetchone()[0]
    except sqlite3.Error:
        versions = 0
    skill_row = conn.execute(
        "SELECT COUNT(*) FROM skills WHERE name = ?", (name,)
    ).fetchone()[0]

    if not (usage or versions or skill_row):
        print(f"Nothing to delete for '{name}'.")
        return 0
    if not confirm(
        f"Delete '{name}' ({usage} usage row(s), {versions} version(s), "
        f"{skill_row} skills row)? [y/N] ",
        args.yes,
    ):
        print("Aborted.")
        return 1

    res = db.delete_skill(conn, name)
    print(
        f"Deleted '{name}': {res['usage_deleted']} usage row(s), "
        f"{res['skills_deleted']} skills row(s), versions included."
    )
    return 0


def cmd_clear(conn, args):
    """Clear every kind of usage: skills + MCP + plugins.

    Delegates to `skill_db.clear_usage` so the CLI and the TUI's "Clear usage"
    button mean the same thing. The old body deleted only `skill_usage` (plus
    `skills` with --all), leaving mcp_usage, plugin_usage and skill_versions
    behind — the Dashboard kept showing MCP/plugin counts after a "clear".
    """
    if not args.yes:
        if not confirm("Clear ALL usage records (skills + MCP + plugins)? [y/N] ", False):
            print("Aborted.")
            return 1
    res = db.clear_usage(conn, also_skills=args.all)
    msg = (
        f"Deleted {res['usage_deleted']} skill usage record(s), "
        f"{res['mcp_deleted']} MCP, {res['plugin_deleted']} plugin."
    )
    if args.all:
        msg += (
            f" Also cleared {res['skills_deleted']} skill(s) and their version history."
        )
    print(msg)
    return 0


def cmd_backup(conn, args, db_path):
    """Back up via skill_db.backup_db (same naming and 0600 mode as the TUI).

    The old inline version used a second-resolution name and simply failed when
    that name was taken, so two backups in the same second could not both
    succeed; backup_db bumps the timestamp until the name is free.
    """
    try:
        target = db.backup_db(conn, target=args.path)
    except FileExistsError as e:
        print(f"Refusing to overwrite: {e}", file=sys.stderr)
        return 1
    except sqlite3.Error as e:
        print(f"Backup failed: {e}", file=sys.stderr)
        return 1
    size = os.path.getsize(target)
    print(f"Backed up to {target} ({size} bytes)")
    return 0


def cmd_vacuum(conn, args):
    conn.execute("VACUUM")
    print("VACUUM done.")
    return 0


# --------------------------------------------------------------------------
# Argument parsing (manual — keeps bare `top`/`recent` ergonomic)
# --------------------------------------------------------------------------
class Args:
    pass


def parse_args(argv):
    args = Args()
    args.json = False
    args.db = DEFAULT_DB
    args.self_test = False
    args.command = None
    args.n = None
    args.skill = None
    args.limit = None
    args.yes = False
    args.all = False
    args.path = None

    rest = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--json":
            args.json = True
        elif a == "--self-test":
            args.self_test = True
        elif a == "--db":
            i += 1
            if i >= len(argv):
                raise SystemExit("--db requires a path")
            args.db = argv[i]
        elif a in ("-h", "--help"):
            print(__doc__)
            raise SystemExit(0)
        else:
            rest.append(a)
        i += 1

    if rest:
        args.command = rest[0]
        tail = rest[1:]

        # Split positionals from flags first, so the order does not matter:
        # `show --limit 5 NAME` used to treat "--limit" as the skill name and
        # then fail on the "5" with a confusing "unknown argument: 5".
        positionals: list[str] = []
        flags: list[str] = []
        j = 0
        while j < len(tail):
            t = tail[j]
            if t == "--limit":
                flags.append(t)
                j += 1
                if j >= len(tail):
                    raise SystemExit("--limit requires a number")
                flags.append(tail[j])
            elif t.startswith("-"):
                flags.append(t)
            else:
                positionals.append(t)
            j += 1

        extra: list[str] = []
        if args.command in ("top", "recent"):
            if positionals:
                if positionals[0].isdigit():
                    args.n = int(positionals[0])
                    extra = positionals[1:]
                else:
                    extra = positionals  # `top foo` is a mistake, not a count
        elif args.command in ("show", "delete"):
            if not positionals:
                raise SystemExit(f"{args.command} requires a skill name")
            args.skill = positionals[0]
            extra = positionals[1:]
        elif args.command == "backup":
            if positionals:
                args.path = positionals[0]
                extra = positionals[1:]
        else:
            extra = positionals

        k = 0
        while k < len(flags):
            t = flags[k]
            if t == "--yes":
                args.yes = True
            elif t == "--all":
                args.all = True
            elif t == "--limit":
                k += 1
                raw = flags[k] if k < len(flags) else None
                try:
                    args.limit = int(raw)
                except (TypeError, ValueError):
                    raise SystemExit(f"--limit requires a number, got: {raw}")
                if args.limit < 1:
                    raise SystemExit("--limit must be >= 1")
            else:
                raise SystemExit(
                    f"unknown argument: {t} "
                    "(skill-stats accepts --db, --limit, --yes, --all)"
                )
            k += 1

        # Checked after the flags so an unknown flag reports itself rather than
        # being masked by the stray value that followed it.
        if extra:
            raise SystemExit(f"unexpected argument: {extra[0]}")

    return args


# --------------------------------------------------------------------------
# Self-test
# --------------------------------------------------------------------------
def self_test():
    results = []

    def check(cond, label):
        results.append((bool(cond), label))

    tmpdir = tempfile.mkdtemp(prefix="skill-stats-selftest-")
    db = os.path.join(tmpdir, "test.db")
    try:
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        conn.executescript(SCHEMA_SQL)
        conn.execute(
            "INSERT INTO skills (name, category, path, description) VALUES (?,?,?,?)",
            ("java-review", "personal-skills", "/x/java-review", "Java review"),
        )
        conn.execute(
            "INSERT INTO skills (name, category, path, description) VALUES (?,?,?,?)",
            ("never-used", "open-source-skills", "/x/never-used", "n/a"),
        )
        for i in range(3):
            conn.execute(
                "INSERT INTO skill_usage (skill_id, skill_name, session_id, project_path, "
                "trigger_type, status, timestamp, duration_ms, call_id, metadata) "
                "VALUES (1,'java-review',?,'/proj','tool_call','success',"
                "strftime('%Y-%m-%dT%H:%M:%fZ','now'),42,?,?)",
                (f"s{i}", f"c{i}", json.dumps({"model": "p/m", "agent": "build", "branch": "main", "summary": "hi"})),
            )
        conn.execute(
            "INSERT INTO skill_usage (skill_id, skill_name, session_id, project_path, "
            "trigger_type, status, timestamp, duration_ms, call_id, metadata) "
            "VALUES (1,'java-review','s9','/proj','permission_denied','denied',"
            "strftime('%Y-%m-%dT%H:%M:%fZ','now'),NULL,'c9','{}')"
        )
        conn.commit()

        check(conn.execute("SELECT COUNT(*) FROM skills").fetchone()[0] == 2, "skills table")
        check(conn.execute("SELECT COUNT(*) FROM skill_usage").fetchone()[0] == 4, "usage table")

        tot = conn.execute("SELECT * FROM v_skill_totals WHERE skill_name='java-review'").fetchone()
        check(tot["total"] == 4 and tot["success"] == 3 and tot["denied"] == 1, "v_skill_totals")

        last30 = conn.execute("SELECT * FROM v_skill_last30 WHERE skill_name='java-review'").fetchone()
        check(last30["uses_30d"] == 4, "v_skill_last30")

        # Filter to a known row: all four inserts share a timestamp to the
        # millisecond, and `ORDER BY timestamp DESC LIMIT 1` tie-breaks by
        # rowid, which now (with the timestamp index in the shared schema)
        # returns the row whose metadata is '{}'.
        hist = conn.execute(
            "SELECT * FROM v_skill_history WHERE session_id = 's0'"
        ).fetchone()
        check(hist["model"] == "p/m" and hist["branch"] == "main", "v_skill_history json_extract")

        # Dedup semantics: same (session_id, call_id) cannot be inserted twice.
        try:
            conn.execute(
                "INSERT INTO skill_usage (skill_name, session_id, trigger_type, status, call_id) "
                "VALUES ('java-review','s0','tool_call','success','c0')"
            )
            check(False, "UNIQUE(session_id,call_id) enforced")
        except sqlite3.IntegrityError:
            conn.rollback()
            check(True, "UNIQUE(session_id,call_id) enforced")

        # Duplicated DDL must stay in sync with the plugin's schema.
        cols = {r[1] for r in conn.execute("PRAGMA table_info(skill_usage)")}
        for c in ("skill_id", "skill_name", "session_id", "project_path", "trigger_type",
                  "status", "timestamp", "duration_ms", "call_id", "metadata"):
            check(c in cols, f"column skill_usage.{c}")

        check(fmt_time("2026-09-23T15:30:00.000Z").startswith("2026-09-23"), "fmt_time")
        check(short_session("abcdef1234567890") == "abcdef12…", "short_session")

        # backup
        target = os.path.join(tmpdir, "backup.db")
        conn.commit()
        conn.execute("VACUUM INTO ?", (target,))
        check(os.path.exists(target) and os.path.getsize(target) > 0, "VACUUM INTO backup")
        conn.close()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    failed = [label for ok, label in results if not ok]
    for ok, label in results:
        print(f"{'PASS' if ok else 'FAIL'}  {label}")
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    return 0 if not failed else 1


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def main(argv):
    args = parse_args(argv)

    if args.self_test:
        return self_test()

    command = args.command or "stats"
    write_commands = {"delete", "clear", "backup", "vacuum"}
    readonly = command not in write_commands

    if command not in ("stats", "top", "show", "recent", "delete", "clear", "backup", "vacuum"):
        print(f"Unknown command: {command}\n", file=sys.stderr)
        print(__doc__)
        return 2

    if readonly and not os.path.exists(args.db):
        return missing_db(args.db, args.json)
    if not readonly and not os.path.exists(args.db):
        print(f"No database at {args.db} — nothing to modify.", file=sys.stderr)
        return 1

    try:
        conn = open_db(args.db, readonly)
    except sqlite3.Error as e:
        print(f"Cannot open database {args.db}: {e}", file=sys.stderr)
        return 1

    try:
        if command == "stats":
            return cmd_stats(conn, args)
        if command == "top":
            return cmd_top(conn, args)
        if command == "show":
            return cmd_show(conn, args)
        if command == "recent":
            return cmd_recent(conn, args)
        if command == "delete":
            return cmd_delete(conn, args)
        if command == "clear":
            return cmd_clear(conn, args)
        if command == "backup":
            return cmd_backup(conn, args, args.db)
        if command == "vacuum":
            return cmd_vacuum(conn, args)
    except sqlite3.Error as e:
        print(f"Database error: {e}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
