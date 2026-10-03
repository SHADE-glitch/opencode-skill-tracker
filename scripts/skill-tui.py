#!/usr/bin/env python3
"""
skill-tui.py — interactive TUI for the OpenCode skill tracker (`skillt`).

Two modes in one file:
  * TUI   (default)      requires `textual` (installed in the skillt venv)
  * --cli <subcommand>   stdlib only; works on the system python3
                         subcommands: insight | export | sync | cleanup-selftest
                                      | scrub-metadata | health | mcp | plugins
                                      | agentos | claude-mem | auto-backup | doctor

`textual` is imported lazily so the --cli path never depends on the venv.

Data access is delegated to `skill_db.py` (shared with the legacy CLI).
The plugin `plugin/skill-tracker.js` and the legacy `scripts/skill-stats.py`
are intentionally left untouched.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import skill_db as db  # noqa: E402

SORT_MODES = ["count", "last_used", "success_rate", "name"]
SORT_LABELS = {
    "count": "Uses ↓",
    "last_used": "Last used ↓",
    "success_rate": "Success rate ↓",
    "name": "Name ↑",
}


# ===========================================================================
# Shared helpers
# ===========================================================================
def sort_rows(rows, mode, name_key="skill_name"):
    """Sort rows by `mode`. `name_key` lets the MCP table reuse this verbatim."""
    if mode == "count":
        return sorted(rows, key=lambda r: (-r["total"], r[name_key]))
    if mode == "last_used":
        return sorted(rows, key=lambda r: (r["last_used"] is None, _neg_str(r["last_used"]), r[name_key]))
    if mode == "success_rate":
        def rate(r):
            sr = db.success_rate(r["total"], r["success"])
            # The trailing name keeps ties deterministic (rows with no data
            # all share `sr is None`).
            return (sr is None, -(sr or 0.0), -r["total"], r[name_key])
        return sorted(rows, key=rate)
    return sorted(rows, key=lambda r: r[name_key])


def _neg_str(s):
    # Descending sort on an ISO timestamp string without reversing the list.
    return "".join(chr(255 - ord(c)) if ord(c) < 255 else c for c in (s or ""))


KIND_LABELS = {"skill": "skill", "mcp": "mcp", "plugin": "plugin"}

STATUS_MARKUP = {
    "success": "[green]● success[/]",
    "error": "[red]● error[/]",
    "denied": "[yellow]● denied[/]",
}


def status_cell(status: str | None) -> str:
    """Rich-markup status dot. Plain fallback keeps headless tests stable."""
    if status in STATUS_MARKUP:
        return STATUS_MARKUP[status]
    return f"[dim]● {status or '-'}[/]"


def rate_text(total, success) -> str:
    sr = db.success_rate(total, success)
    if sr is None:
        return "-"
    pct = round(sr * 100)
    if pct >= 90:
        return f"[green]{pct}%[/]"
    if pct >= 70:
        return f"[yellow]{pct}%[/]"
    return f"[red]{pct}%[/]"


def bar(value, peak, width=24):
    if peak <= 0:
        return ""
    n = max(1, round(value / peak * width)) if value > 0 else 0
    return "█" * n


# The Dashboard trend row: a date, two spaces, a padded bar field, and a count
# capped to five characters. `TREND_ROW_WIDTH` is only a constant because
# `fmt_count` is one — see the note in `_render_trends` about why a trailing
# field that grows with its data moves one chart off the line the others hold.
TREND_DAYS = 7
TREND_LINES = TREND_DAYS + 1          # seven day rows under the chart's title
TREND_BARW = 8
TREND_ROW_WIDTH = 5 + 2 + TREND_BARW + 5


def fmt_count(n) -> str:
    """A call count in at most five characters: 99999, 123k, 4.0M.

    Not decoration. The trend rows are compared by length by a test precisely so
    that a six-digit day count cannot silently wrap only its own chart — the
    defect this caps against.
    """
    try:
        value = int(n or 0)
    except (TypeError, ValueError):
        return "-"
    if value < 0:
        value = 0
    if value < 100_000:
        return str(value)
    if value < 1_000_000:
        return f"{round(value / 1_000)}k"          # 100k … 1000k
    if value < 1_000_000_000:
        text = f"{value / 1_000_000:.1f}M"
        return text if len(text) <= 5 else f"{round(value / 1_000_000)}M"
    return f"{round(value / 1_000_000_000)}G"


def plain_len(value) -> int:
    """Visible length of a cell: markup counts as what it renders to.

    Most cells are plain, but the rate and status columns carry markup
    (`[green]100%[/]` is 4 characters on screen, not 14). A name can also contain
    a stray closing tag, which is not markup and makes the parser raise — that is
    measured literally rather than skipped. An unclosed *opening* tag is not an
    error: rich closes it silently, and the table renders it that way too, so
    measuring it the same way is the truth about the painted width.
    """
    text = str(value)
    if "[" not in text:
        # The fast path, and the common one: names, timestamps and paths carry no
        # markup, and skipping rich's parser is most of the cost of a fit pass.
        return len(text)
    try:
        from rich.text import Text
        return len(Text.from_markup(text).plain)
    except Exception:  # noqa: BLE001 - invalid markup is data, not an error
        return len(text)


def fit_columns(table) -> None:
    """Size every column to its content *now*, instead of one idle cycle later.

    `DataTable` measures auto-width columns in `_on_idle`, i.e. only once the
    message pump goes quiet. The first frame therefore renders every column
    exactly as wide as its header — `frozen-gnome-fork-maintenance` as `froze` —
    and each later frame shows the widths of the *previous* content. Measured on
    the live database: immediately after a refresh the render widths are
    [3, 7, 6, 14, 11], and only after the pump idles do they become
    [4, 32, 6, 14, 18].

    The cells are already in hand, so measure them here and turn auto width off
    for these columns. The cost is one pass over the cells per refresh (the
    largest table is 100 rows × 6 columns), which is what the tests measure
    *without* a `pilot.pause()` afterwards — a paused test would pass either way.
    """
    for column in table.columns.values():
        widest = plain_len(column.label)
        for cell in table.get_column(column.key):
            length = plain_len(cell)
            if length > widest:
                widest = length
        column.width = widest
        column.auto_width = False
    table.refresh()


# ===========================================================================
# CLI mode (no textual)
# ===========================================================================
def _cli_insight(conn, args) -> int:
    data = db.insight(conn, days=args.days, min_uses=args.min_uses, limit=args.limit)
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0

    def section(title):
        print(title)
        print("-" * 56)

    section("Most used")
    if not data["most_used"]:
        print("  (no data)")
    for r in data["most_used"]:
        print(f"  {r['skill_name']:<40} {r['total']:>4}   last: {db.fmt_time(r['last_used'])}")

    print()
    section(f"Fastest growing in the last {args.days} days (min {args.min_uses} uses)")
    if not data["fastest_growing"]:
        print("  (no data)")
    for r in data["fastest_growing"]:
        print(f"  {r['skill_name']:<40} {r['recent']:>4}  (prev {r['prev']}, delta {r['delta']:+d})")

    never = [r for r in data["long_unused"] if r["never_used"]]
    stale = [r for r in data["long_unused"] if not r["never_used"]]

    print()
    section(f"Never used ({len(never)})")
    if not never:
        print("  (no data)")
    for r in never:
        print(f"  {r['skill_name']:<40} {r['source']}")

    print()
    section(f"Unused for >{args.days} days ({len(stale)})")
    if not stale:
        print("  (no data)")
    for r in stale:
        print(f"  {r['skill_name']:<40} {r['source']:<12} {db.fmt_time(r['last_used'])}")

    print()
    section(f"Highest failure rate (min {args.min_uses} uses)")
    if not data["highest_failure_rate"]:
        print("  (no data)")
    for r in data["highest_failure_rate"]:
        print(
            f"  {r['skill_name']:<40} {r['fail_rate']*100:>5.1f}%  "
            f"(total {r['total']}, err {r['errors']}, denied {r['denied']})"
        )
    return 0


def _cli_export(conn, args) -> int:
    doc = db.export_document(conn, include_usage=not args.skills_only)
    if not args.out:
        print(json.dumps(doc, ensure_ascii=False, indent=2 if args.pretty else None))
        return 0
    try:
        path = db.write_private_json(args.out, doc, pretty=args.pretty, force=args.force)
    except FileExistsError as e:
        print(f"{e}\n(use --force to overwrite)", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"skillt: cannot write {args.out}: {e}", file=sys.stderr)
        return 2
    print(f"Exported to {path} (mode 0600, {len(doc['skills'])} skills, {len(doc['usage'])} usage rows)")
    return 0


def _cli_sync(conn, args) -> int:
    stats = db.sync_versions(conn, dry_run=args.dry_run, prune_orphans=args.prune_orphans)
    if args.json:
        print(json.dumps(stats, ensure_ascii=False, indent=2))
    else:
        prefix = "[dry-run] " if stats["dry_run"] else ""
        print(
            f"{prefix}scanned={stats['scanned']} baseline={stats['baseline']} "
            f"changed={stats['changed']} unchanged={stats['unchanged']} "
            f"unparsed_name={stats['unparsed_name']} "
            f"orphans_pruned={stats['orphan_versions_pruned']}"
        )
    return 0


def _cli_cleanup_selftest(conn, args) -> int:
    res = db.cleanup_selftest(conn, dry_run=not args.yes)
    if not res["rows"]:
        print(f"No synthetic rows found (project_path = {db.SELFTEST_PROJECT}).")
        return 0
    if res["dry_run"]:
        print(f"[dry-run] would delete {res['matched']} row(s) with project_path={db.SELFTEST_PROJECT}:")
        for r in res["rows"]:
            print(f"  id={r['id']} {r['skill_name']:<34} {r['status']:<8} {db.fmt_time(r['timestamp'])}")
        print("\nRe-run with --yes to delete (a backup is taken first by the caller).")
        return 0
    print(f"Deleted {res['matched']} synthetic row(s).")
    return 0


def _cli_scrub_metadata(conn, args) -> int:
    res = db.scrub_metadata(conn, dry_run=not args.yes)
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0
    if not res["matched"]:
        print(f"No rows carry the disallowed metadata keys {res['keys']}.")
        return 0
    counts = "  ".join(f"{t}={n}" for t, n in res["by_table"].items() if n)
    if res["dry_run"]:
        print(f"[dry-run] would strip {res['matched']} row(s): {counts}")
        for r in res["rows"][: args.limit]:
            print(
                f"  {r['table']:<13} id={r['id']:<6} {','.join(r['keys']):<16} "
                f"{db.fmt_time(r['timestamp'])}  {str(r['name'])[:44]}"
            )
        if res["matched"] > args.limit:
            print(f"  ... {res['matched'] - args.limit} more (use --limit N)")
        print("\nUsage rows are kept; only the listed keys come off the metadata.")
        print("Re-run with --yes to apply (a backup is taken first by the caller).")
        return 0
    print(f"Stripped {res['matched']} row(s): {counts}")
    if res["checkpointed"] is False:
        print("  warning: the write could not be folded out of the WAL, so the "
              "redacted text is still in the -wal file. Close OpenCode and run "
              "`skillt vacuum`.")
    elif res["checkpointed"]:
        print("  WAL checkpointed: the removed text is no longer on disk.")
    return 0


def _cli_claude_mem(conn, args) -> int:
    """claude-mem's own ledger, read-only. Nothing here writes to that store."""
    res = db.claude_mem_summary(conn, days=args.days)
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0
    if not res["available"]:
        print(f"claude-mem: not aggregated ({res['reason']})")
        print("  set OPENCODE_SKILL_TRACKER_CLAUDE_MEM_DB, or CLAUDE_MEM_DIR"
              " pointing at the directory holding claude-mem.db")
        return 0

    t = res["tables"]
    age = db.claude_mem_age_days(res)
    print("claude-mem (read-only; that store is never written from here)")
    print("-" * 56)
    print(f"  store      {res['db_path']}")
    print(f"  newest row {res['newest'] or '-'}"
          + (f"  ({age:.1f} d ago)" if age is not None else ""))
    print("  " + "   ".join(
        f"{name} {(t[name] or {}).get('n')}" for name in db.CLAUDE_MEM_TABLES))
    obs = t.get("observations") or {}
    if obs.get("by"):
        print("  by type    " + " · ".join(f"{k} {v}" for k, v in obs["by"].items()))
    sess = t.get("sdk_sessions") or {}
    if sess.get("by"):
        print("  sessions   " + " · ".join(f"{k} {v}" for k, v in sess["by"].items()))
    tokens = [(n, (t.get(n) or {}).get("tokens")) for n in ("observations", "session_summaries")]
    tokens = [(n, v) for n, v in tokens if isinstance(v, int) and v]
    if tokens:
        print("  discovery tokens  " + " + ".join(f"{v:,} ({n})" for n, v in tokens))
    h = res.get("health") or {}
    if h:
        bits = [f"{h.get('consecutiveFailures', 0)} consecutive failures"]
        if h.get("lastSuccessAt"):
            bits.append(f"last success {h['lastSuccessAt']}")
        if h.get("lastErrorAt"):
            bits.append(f"last error {h['lastErrorAt']}"
                        + (f" ({h['lastErrorKind']})" if h.get("lastErrorKind") else ""))
        print("  observer   " + " · ".join(bits))
    b = res.get("backfill") or {}
    if b:
        print(f"  backfill   through {b.get('throughDay')} · {b.get('eventCount')} events"
              f" · completed {b.get('completedAt')}")
    tw = res.get("tracker_same_window")
    if tw:
        print(f"  same {res['days']} d in this tracker: {tw.get('skill_usage')} skill"
              f" / {tw.get('mcp_usage')} mcp / {tw.get('plugin_usage')} plugin"
              "   (builtins are never measured here, so 0 is not 'nothing happened')")
    return 0


def _cli_agentos(conn, args) -> int:
    """The AgentOS advisor store, read-only. Nothing here writes to that store."""
    res = db.agentos_summary(conn, limit=args.limit)
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0
    if not res["available"]:
        print(f"AgentOS advisor: not aggregated ({res['reason']})")
        print("  set AGENT_OS_ROOT, or OPENCODE_SKILL_TRACKER_AGENTOS_DB=<path to store/aos.db>")
        return 0

    sc = res["store_counts"]
    print("AgentOS advisor (read-only; that store is never written from here)")
    print("-" * 56)
    print(f"  store      {res['db_path']}")
    if res["loops_dir"]:
        print(f"  loops dir  {res['loops_dir']}")
    print(f"  memories {sc.get('memories')}   retrieval {res['retrieval'].get('n')}"
          f" over {res['retrieval'].get('memories')} memory/memories"
          f"   observations {sc.get('observations')}   candidates {sc.get('candidates')}"
          f"   reviews {sc.get('learning_reviews')}")
    if res["telemetry"]:
        parts = []
        for t in res["telemetry"]:
            last = (db.fmt_time(t["last"]) or "")[5:]
            parts.append(f"{t['event_type']} {t['n']} (last {last})")
        print("  telemetry  " + "  ·  ".join(parts))
    print(f"  per-call budget {res['timeout_ms']} ms — a slower stage never reached the prompt")
    sized = [l.get("injection") or {} for l in res["loops"] if l.get("injection")]
    if sized:
        newest, oldest = sized[0], sized[-1]
        chars = [c.get("chars") for c in sized if isinstance(c.get("chars"), int)]
        if chars:
            print(f"  injected {newest.get('injected')} memories / "
                  f"{newest.get('chars')} chars in the newest loop"
                  f"   (oldest shown: {oldest.get('injected')} / {oldest.get('chars')} chars"
                  f"; peak {max(chars)})")

    print()
    print(f"Loops (showing {len(res['loops'])} of {res['loops_total']}, newest first)")
    print("-" * 56)
    if not res["loops"]:
        print("  (no loop files yet)")
    for loop in res["loops"]:
        usage = loop.get("usage") or {}
        used = (f"{usage.get('skill', 0)} skill"
                f" / {usage.get('mcp', 0)} mcp / {usage.get('plugin', 0)} plugin")
        slow = loop.get("slowest_stage") or {}
        slow_txt = f"{slow.get('name')} {slow.get('ms')}ms" if slow else "-"
        if loop.get("over_budget_ms") and slow.get("ms") is not None:
            slow_txt += f" · over the {res['timeout_ms']}ms budget"
        inj = loop.get("injection") or {}
        inj_txt = ("-" if not inj else
                   f"searched {inj.get('retrieved')} / recalled {inj.get('recalled')}"
                   f" / reached the prompt {inj.get('injected')}"
                   f" ({inj.get('chars')} chars)")
        print(f"  {str(loop.get('loop_id')):<28} {(db.fmt_time(loop.get('created_at')) or '')[5:]:<12}"
              f" {str(loop.get('final_status')):<9} measured calls {used}   {inj_txt}"
              f"   slowest {slow_txt}")
        cells = []
        for name, st in loop.get("stages", {}).items():
            mark = {"completed": "ok", "pending": "-", "failed": "ERR"}.get(st["status"], st["status"][:3])
            cells.append(f"{name}:{mark}")
        print("      " + " ".join(cells))
        if loop.get("errors") or (loop.get("postflight") or {}).get("has_error"):
            print("      errors:"
                  f" loop={loop.get('errors')}"
                  f" postflight={'error' if (loop.get('postflight') or {}).get('has_error') else 'clean'}"
                  "  (text lives in the store; not copied here)")
    return 0


def _cli_health(conn, args) -> int:
    report = db.health_report(conn)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    c = report["counts"]
    w = report["windows"]
    s = report["sample"]
    print("Skill health (read-only; nothing is deleted)")
    print("-" * 56)
    print(f"  Active   (<= {w['active_days']} days)              {c['active']}")
    print(f"  Stale    ({w['active_days']}-{w['stale_days']} days)             {c['stale']}")
    print(f"  Unused   (>{w['stale_days']} days or never used)   {c['unused']}")
    print(f"  Total                          {c['total']}")
    print(
        f"  Observed {s['sessions']} session(s) over {s['days_observed']} day(s)"
        + ("" if s["enough_for_advice"] else "  (too little to advise on pruning)")
    )
    print()
    print("Risk flags")
    print("-" * 56)
    print(f"  never used        {report['risk_counts']['never_used']}")
    print(f"  high failure rate {report['risk_counts']['high_failure']}")
    print(f"  frequently edited {report['risk_counts']['frequently_edited']}")
    flagged = [s for s in report["skills"] if s["flags"]]
    if flagged:
        print()
        print(f"Skills needing attention ({len(flagged)})")
        print("-" * 56)
        for s in flagged[: args.limit]:
            fr = f"{round(s['failure_rate'] * 100)}%" if s["failure_rate"] is not None else "-"
            print(f"  {s['skill_name']:<40} {s['bucket']:<7} fail {fr:<5} {','.join(s['flags'])}")
        if len(flagged) > args.limit:
            print(f"  ... {len(flagged) - args.limit} more (use --limit N)")
    print()
    print("Suggestions (advisory only, never executed)")
    print("-" * 56)
    for s in report["suggestions"]:
        print(f"  · {s}")
    return 0


def _cli_mcp(conn, args) -> int:
    servers = db.mcp_server_rows(conn)
    tools = db.mcp_stats_rows(conn)

    if args.json:
        print(json.dumps({"servers": servers, "tools": tools}, ensure_ascii=False, indent=2))
        return 0

    print("MCP tool usage (read-only)")
    print("-" * 56)
    if not tools:
        print("  no MCP calls recorded yet")
        print("  (the plugin records MCP tools once a session actually invokes one)")
        return 0

    print("Servers")
    print("-" * 56)
    for s in servers:
        sr = db.success_rate(s["total"], s["success"])
        rate = f"{round(sr * 100)}%" if sr is not None else "-"
        print(
            f"  {s['server_name']:<24} {s['total']:>5} calls  "
            f"{s['tools']:>2} tool(s)  ok {rate:<5} last {db.fmt_time(s['last_used'])}"
        )

    shown = min(args.limit, len(tools))
    print()
    print(f"Top tools (showing {shown} of {len(tools)})")
    print("-" * 56)
    for t in tools[: args.limit]:
        sr = db.success_rate(t["total"], t["success"])
        rate = f"{round(sr * 100)}%" if sr is not None else "-"
        print(
            f"  {t['tool_id']:<44} {t['total']:>4}  ok {rate:<5} "
            f"last {db.fmt_time(t['last_used'])}"
        )
    if len(tools) > args.limit:
        print(f"  ... {len(tools) - args.limit} more (use --limit N)")
    return 0


def _plugin_label(row) -> str:
    """Plugin name for display. npm specs already carry their version in the
    name (`@scope/name@1.2.3`), so only append it when it is not already there."""
    name = row["plugin_name"]
    version = row.get("version")
    if version and not name.endswith(f"@{version}"):
        return f"{name}@{version}"
    return name


def _cli_plugins(conn, args) -> int:
    inventory = db.plugin_inventory_rows(conn)
    items = db.plugin_stats_rows(conn)

    if args.json:
        print(json.dumps({"inventory": inventory, "items": items}, ensure_ascii=False, indent=2))
        return 0

    print("Plugin tool/command usage (read-only)")
    print("-" * 56)
    if not inventory and not items:
        print("  no plugins recorded yet")
        print("  (the plugin records its inventory when OpenCode starts)")
        return 0

    if inventory:
        # Not "installed": this table is a union that only OpenCode's start
        # refreshes, and a `project` row is seen by no other session. Say where
        # each entry came from and when it was last seen.
        print("Plugins seen at the last OpenCode start")
        print("-" * 56)
        for p in inventory:
            label = _plugin_label(p)
            surface = []
            if p["tools"]:
                surface.append(f"{len(p['tools'])} tool(s)")
            if p["commands"]:
                surface.append(f"{len(p['commands'])} command(s)")
            detail = ", ".join(surface) if surface else "no surface detected"
            where = f"{p.get('scope') or 'unknown'}, seen {db.fmt_time(p['last_seen'])}"
            if p["skipped"]:
                print(f"  {label:<44} excluded  ({where})")
            else:
                print(f"  {label:<44} {detail}, {p['total']} call(s)  ({where})")

    shown = min(args.limit, len(items))
    print()
    print(f"Top items (showing {shown} of {len(items)})")
    print("-" * 56)
    for it in items[: args.limit]:
        sr = db.success_rate(it["total"], it["success"])
        rate = f"{round(sr * 100)}%" if sr is not None else "-"
        print(
            f"  {it['item_id']:<44} {it['total']:>4}  ok {rate:<5} "
            f"last {db.fmt_time(it['last_used'])}"
        )
    if len(items) > args.limit:
        print(f"  ... {len(items) - args.limit} more (use --limit N)")
    return 0


def _cli_auto_backup(conn, args) -> int:
    try:
        res = db.auto_backup(conn, dry_run=args.dry_run)
    except db.BackupLockError as e:
        print(f"auto-backup: {e}", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"auto-backup failed: {e}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0

    prefix = "[dry-run] " if res["dry_run"] else ""
    verb = "would create" if res["dry_run"] else "created"
    print(f"{prefix}backup {verb}: {res['backup']}")
    print(f"  backup dir: {res['backup_dir']}")
    print(f"  kept:   {len(res['kept'])}")
    print(f"  delete: {len(res['deleted'])}")
    for p in res["deleted"]:
        print(f"    - {os.path.basename(p)}")
    if res["skipped_young"]:
        print(f"  skipped (younger than {db.MIN_BACKUP_AGE_S}s): {len(res['skipped_young'])}")
    if res["unparsed"]:
        print(f"  ignored (unrecognised name): {len(res['unparsed'])}")
    return 0


PLUGIN_PATH = os.path.join(db.HOME, ".config", "opencode", "plugin", "skill-tracker.js")
PLUGIN_MARKERS = ("tool.execute.before", "tool.execute.after", "permission.ask", "event:")
# Markers of MCP capture. An older plugin records skills only, so a missing
# marker is a WARN (MCP data stays empty), never a FAIL.
PLUGIN_MCP_MARKERS = ("mcp_usage", "function classify(", "recordMcpUsage")
# Same for plugin tool/command capture.
PLUGIN_PLUGIN_MARKERS = ("plugin_usage", "recordPluginUsage", "command.execute.before")

# The plugin's builtin-tool allowlist is verified against one OpenCode release
# (limitation M13). Read the pin out of the plugin source rather than keeping a
# second copy here, which is exactly the drift this check exists to catch.
VERSION_PIN_RE = re.compile(r"verified against OpenCode (\d+\.\d+\.\d+)")
OPENCODE_VERSION_TIMEOUT_S = 2

# The tracker's own log. `log()` never throws, so a hook that starts failing is
# invisible in the database — it only ever shows up here.
TRACKER_LOG_PATH = os.path.join(db.HOME, ".config", "opencode", "logs", "skill-tracker.log")

# How stale the newest recorded call may get before doctor says so. A tracker
# that stopped writing looks exactly like an idle machine from the inside.
CAPTURE_FRESHNESS_DAYS = 7

# The three streams the plugin writes, in the order the report lists them.
STREAM_TABLES = ("skill_usage", "mcp_usage", "plugin_usage")

# The TUI is a monitor: the writer is another process (the OpenCode plugin), so
# a screen that only updates on manual `r` shows numbers the user must not trust
# as current. Every interaction re-reads, and the age of what is on screen is
# always printed. This is *not* a background timer on purpose: on textual 8.2.8
# any app timer created after the screens are mounted makes `run_test`'s
# teardown raise `LookupError: active_app`, which would take the whole suite
# down with it. See `skillt doctor` capture.freshness for the idle case.
REFRESH_STALE_AFTER_S = 5


def _opencode_version():
    """Output of `opencode --version`, or None when it cannot be run.

    Separate function so the check above never has to shell out in a test.
    """
    try:
        proc = subprocess.run(
            ["opencode", "--version"], capture_output=True, text=True,
            timeout=OPENCODE_VERSION_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout or None


def _doctor_checks(conn, args) -> list:
    """Return [(name, 'PASS'|'WARN'|'FAIL', detail)] for `skillt doctor`."""
    checks: list = []

    def add(name, ok, detail, warn=False):
        checks.append((name, "PASS" if ok else ("WARN" if warn else "FAIL"), detail))

    # --- SQLite ---------------------------------------------------------
    exists = os.path.exists(args.db)
    add("db.exists", exists, args.db if exists else f"missing: {args.db}")
    if exists:
        try:
            mode = os.stat(args.db).st_mode & 0o777
            add("db.mode", mode == 0o600, f"{oct(mode)} (want 0o600)", warn=True)
        except OSError as e:
            add("db.mode", False, f"stat failed: {e}", warn=True)
    try:
        jm = conn.execute("PRAGMA journal_mode").fetchone()[0]
        add("db.wal", str(jm).lower() == "wal", str(jm), warn=True)
    except Exception as e:  # noqa: BLE001
        add("db.wal", False, f"error: {e}")
    try:
        qc = conn.execute("PRAGMA quick_check").fetchone()[0]
        add("db.quick_check", qc == "ok", str(qc))
    except Exception as e:  # noqa: BLE001
        add("db.quick_check", False, f"error: {e}")

    # --- Skills ---------------------------------------------------------
    try:
        n = conn.execute("SELECT COUNT(*) FROM skills").fetchone()[0]
        add("skills.count", n > 0, str(n), warn=True)
        missing = sum(
            1 for r in conn.execute("SELECT path FROM skills")
            if not (r["path"] and os.path.isfile(os.path.join(r["path"], "SKILL.md")))
        )
        add("skills.paths_exist", missing == 0, f"{missing} row(s) without SKILL.md", warn=True)
    except Exception as e:  # noqa: BLE001
        add("skills.count", False, f"error: {e}")
    if not os.path.isdir(db.SKILLS_DIR):
        # Without the directory, scan_skills() returns [] and the frontmatter
        # check below would vacuously PASS. Report the missing dir instead.
        add("skills.dir", False, f"missing: {db.SKILLS_DIR}", warn=True)
    else:
        try:
            # Pass SKILLS_DIR explicitly: scan_skills' default is bound at import
            # time, so a runtime override would otherwise be ignored.
            found = db.scan_skills(db.SKILLS_DIR)
            unparsed = sum(1 for s in found if not s["parsed_name"])
            add(
                "skills.frontmatter_names", unparsed == 0,
                f"{len(found)} scanned, {unparsed} without frontmatter name", warn=True,
            )
        except Exception as e:  # noqa: BLE001
            add("skills.scan", False, f"error: {e}")

    # --- Plugin ---------------------------------------------------------
    plugin_src = None
    pexists = os.path.isfile(PLUGIN_PATH)
    add("plugin.exists", pexists, PLUGIN_PATH if pexists else f"missing: {PLUGIN_PATH}")
    if pexists:
        try:
            with open(PLUGIN_PATH, encoding="utf-8", errors="replace") as f:
                src = f.read()
            plugin_src = src
            miss = [m for m in PLUGIN_MARKERS if m not in src]
            add("plugin.hooks", not miss,
                "all hooks present" if not miss else f"missing: {', '.join(miss)}", warn=True)
            miss_mcp = [m for m in PLUGIN_MCP_MARKERS if m not in src]
            add("plugin.mcp_hooks", not miss_mcp,
                "MCP capture present" if not miss_mcp
                else f"missing: {', '.join(miss_mcp)} (MCP calls will not be recorded)",
                warn=True)
            miss_plugin = [m for m in PLUGIN_PLUGIN_MARKERS if m not in src]
            add("plugin.plugin_hooks", not miss_plugin,
                "plugin capture present" if not miss_plugin
                else f"missing: {', '.join(miss_plugin)} (plugin calls will not be recorded)",
                warn=True)
        except OSError as e:
            add("plugin.hooks", False, f"read failed: {e}")
            add("plugin.mcp_hooks", False, f"read failed: {e}", warn=True)
            add("plugin.plugin_hooks", False, f"read failed: {e}", warn=True)

    # --- Environment ----------------------------------------------------
    add("env.python", sys.version_info >= (3, 10), sys.version.split()[0], warn=True)
    has_textual = _textual_available()
    add("env.textual", has_textual, "available" if has_textual else "not installed", warn=True)
    # Inside *a* virtualenv? The venv's name/path is deliberately not checked:
    # the repo's .venv and the legacy ~/.local/share/opencode/skillt-venv are
    # both valid, so matching on "skillt-venv" reported a false WARN.
    in_venv = sys.prefix != sys.base_prefix
    add("env.venv", in_venv, sys.executable, warn=True)

    # --- Backups --------------------------------------------------------
    try:
        latest = None
        if os.path.isdir(db.BACKUP_DIR):
            for n in os.listdir(db.BACKUP_DIR):
                p = os.path.join(db.BACKUP_DIR, n)
                if os.path.isfile(p) and db.BACKUP_RE.match(n):
                    latest = max(latest or 0, os.path.getmtime(p))
        if latest is None:
            add("backups.latest", False, f"none in {db.BACKUP_DIR}", warn=True)
        else:
            age_d = (time.time() - latest) / 86400
            stamp = datetime.fromtimestamp(latest, timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")
            add("backups.latest", age_d <= 7, f"{stamp} ({age_d:.1f}d ago)", warn=True)
    except Exception as e:  # noqa: BLE001
        add("backups.latest", False, f"error: {e}", warn=True)

    # --- Capture pipeline ----------------------------------------------
    # Everything above proves the parts exist. These three prove it is still
    # *recording*, which is the only thing this tool is for.
    def has_table(name: str) -> bool:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone() is not None

    limit = getattr(args, "freshness_days", CAPTURE_FRESHNESS_DAYS)
    # Which streams the check is allowed to call stale. A stream with zero rows is
    # "never used", not "stopped"; and a stream the owner removed on purpose is
    # neither — but both are still printed, because an exclusion nobody can see is
    # the same false silence this check exists to remove.
    wanted = {s.strip() for s in
              os.environ.get("OPENCODE_SKILL_TRACKER_STREAMS", "").split(",") if s.strip()}
    judged = {f"{s}_usage" for s in wanted} if wanted else {t for t in STREAM_TABLES}
    try:
        parts, stalled, excluded, seen_any, rows_any = [], [], False, False, False
        for table in STREAM_TABLES:
            if not has_table(table):
                continue
            seen_any = True
            newest = conn.execute(f"SELECT MAX(timestamp) FROM {table}").fetchone()[0]
            if not newest:
                parts.append(f"{table}: no rows")
                continue
            rows_any = True
            # ISO-8601 UTC of fixed width, so julianday is the only parse needed.
            age = conn.execute(
                "SELECT julianday('now') - julianday(?)", (newest,)
            ).fetchone()[0]
            if table not in judged:
                parts.append(f"{table} {age:.1f}d (excluded)")
                excluded = True
                continue
            parts.append(f"{table} {age:.1f}d")
            if age > limit:
                stalled.append((table, age))
        if not seen_any:
            add("capture.freshness", False,
                "no usage tables at all (database not migrated?)", warn=True)
        elif not rows_any:
            add("capture.freshness", False,
                "no usage rows in any table  ·  " + "  ·  ".join(parts), warn=True)
        elif stalled:
            worst = ", ".join(f"{t} {a:.1f}d" for t, a in stalled)
            add("capture.freshness", False,
                f"stalled: {worst}  ·  want <= {limit}d  ·  " + "  ·  ".join(parts),
                warn=True)
        else:
            detail = "  ·  ".join(parts) + f"  ·  want <= {limit}d"
            if excluded:
                detail += ("  ·  OPENCODE_SKILL_TRACKER_STREAMS excludes a stream;"
                           " it is shown above but not judged")
            add("capture.freshness", True, detail)
    except Exception as e:  # noqa: BLE001
        add("capture.freshness", False, f"error: {e}", warn=True)

    # --- claude-mem: a read-only neighbour, silent when not installed -----
    # Deliberately no line at all when there is no store: warning about a plugin
    # the user never installed is noise, and noise trains people to skip the
    # report. When it *is* installed it is judged on the same clock as
    # capture.freshness, and its own failure counter is surfaced — that store is
    # written by a background worker, so "stale" and "failing" are different
    # stories and both matter.
    try:
        cm = db.claude_mem_summary(conn, days=CAPTURE_FRESHNESS_DAYS)
        if cm["available"]:
            age = db.claude_mem_age_days(cm)
            obs = (cm["tables"].get("observations") or {}).get("n")
            health = cm.get("health") or {}
            fails = health.get("consecutiveFailures")
            parts = [f"observations {obs}",
                     f"newest {cm['newest'] or '-'}"
                     + (f" ({age:.1f}d)" if age is not None else "")]
            if isinstance(fails, int) and fails:
                parts.append(f"{fails} consecutive observer failures"
                             + (f" ({health.get('lastErrorKind')})"
                                if health.get("lastErrorKind") else ""))
            want = f"want <= {CAPTURE_FRESHNESS_DAYS}d"
            if age is None:
                add("claude_mem.capture", False,
                    "  ·  ".join(parts + ["no rows in its own ledger", want]), warn=True)
            elif age > CAPTURE_FRESHNESS_DAYS or (isinstance(fails, int) and fails > 0):
                add("claude_mem.capture", False, "  ·  ".join(parts + [want]), warn=True)
            else:
                add("claude_mem.capture", True, "  ·  ".join(parts + [want]))
    except Exception as e:  # noqa: BLE001
        add("claude_mem.capture", False, f"error: {e}", warn=True)

    try:
        if not os.path.isfile(TRACKER_LOG_PATH):
            add(
                "log.errors", False,
                f"no log at {TRACKER_LOG_PATH} — the plugin has not initialised",
                warn=True,
            )
        else:
            count, last = 0, ""
            with open(TRACKER_LOG_PATH, encoding="utf-8", errors="replace") as f:
                for line in f:
                    if "[err]" in line:
                        count += 1
                        last = line.rstrip()
            add(
                "log.errors", count == 0,
                f"{count} error line(s)" + (f"; last: {last[:90]}" if last else ""),
                warn=True,
            )
    except OSError as e:
        add("log.errors", False, f"read failed: {e}", warn=True)

    try:
        pinned = VERSION_PIN_RE.search(plugin_src).group(1) if plugin_src else None
        out = _opencode_version()
        running = re.search(r"\d+\.\d+\.\d+", out or "")
        if not pinned:
            add(
                "env.opencode_version", False,
                "cannot read the allowlist pin from the plugin source", warn=True,
            )
        elif running is None:
            add("env.opencode_version", False,
                "`opencode --version` unavailable (not on PATH?)", warn=True)
        else:
            same = running.group(0) == pinned
            add(
                "env.opencode_version", same,
                f"opencode {running.group(0)}, builtin-tool allowlist pinned to {pinned}"
                + ("" if same else " — refresh DEFAULT_BUILTIN_TOOLS or set "
                                    "OPENCODE_SKILL_TRACKER_BUILTIN_TOOLS"),
                warn=True,
            )
    except Exception as e:  # noqa: BLE001 - a version check must never break doctor
        add("env.opencode_version", False, f"version check failed: {e}", warn=True)

    return checks


def _cli_doctor(conn, args) -> int:
    checks = _doctor_checks(conn, args)
    n_fail = sum(1 for _, s, _ in checks if s == "FAIL")
    n_warn = sum(1 for _, s, _ in checks if s == "WARN")

    if args.json:
        print(json.dumps({
            "checks": [{"name": n, "status": s, "detail": d} for n, s, d in checks],
            "summary": {"pass": len(checks) - n_fail - n_warn, "warn": n_warn, "fail": n_fail},
        }, ensure_ascii=False, indent=2))
    else:
        for name, status, detail in checks:
            print(f"  [{status:<4}] {name:<26} {detail}")
        print(f"\n{len(checks) - n_fail - n_warn} PASS, {n_warn} WARN, {n_fail} FAIL")
    return 1 if n_fail else 0


def _textual_available() -> bool:
    try:
        import textual  # noqa: F401
        return True
    except ImportError:
        return False


def cli_main(args) -> int:
    if not os.path.exists(args.db):
        print(f"No database at {args.db}. Run a skill in OpenCode first.", file=sys.stderr)
        return 1
    if not os.path.isfile(args.db):
        print(f"skillt: --db must be a file, not a directory: {args.db}", file=sys.stderr)
        return 2

    # sync / cleanup-selftest / scrub-metadata / doctor need write access
    # (migration); health, mcp, plugins and agentos are pure reads and open the
    # DB read-only so they can never alter it.
    try:
        conn = db.open_db(
            args.db, readonly=(args.command in ("health", "mcp", "plugins", "agentos",
                                                "claude-mem"))
        )
    except sqlite3.Error as e:
        print(f"skillt: cannot open database {args.db}: {e}", file=sys.stderr)
        return 1
    try:
        if args.command in ("sync", "cleanup-selftest", "scrub-metadata", "doctor"):
            try:
                db.ensure_schema(conn)
            except Exception as e:  # noqa: BLE001 - never hard-fail the CLI
                print(f"warning: migration skipped: {e}", file=sys.stderr)

        if args.command == "insight":
            return _cli_insight(conn, args)
        if args.command == "export":
            return _cli_export(conn, args)
        if args.command == "sync":
            return _cli_sync(conn, args)
        if args.command == "health":
            return _cli_health(conn, args)
        if args.command == "agentos":
            return _cli_agentos(conn, args)
        if args.command == "claude-mem":
            return _cli_claude_mem(conn, args)
        if args.command == "mcp":
            return _cli_mcp(conn, args)
        if args.command == "plugins":
            return _cli_plugins(conn, args)
        if args.command == "auto-backup":
            return _cli_auto_backup(conn, args)
        if args.command == "cleanup-selftest":
            if args.yes:
                # Probe first: never write a backup when there is nothing to
                # delete (a clean DB is the common case).
                if not db.cleanup_selftest(conn, dry_run=True)["rows"]:
                    return _cli_cleanup_selftest(conn, args)
                try:
                    path = db.backup_db(conn)
                    print(f"Backup written to {path}")
                except Exception as e:  # noqa: BLE001
                    print(f"warning: backup failed, aborting: {e}", file=sys.stderr)
                    return 1
            return _cli_cleanup_selftest(conn, args)
        if args.command == "scrub-metadata":
            if args.yes:
                # Same discipline as cleanup-selftest: probe first so a clean DB
                # never produces a pointless backup file.
                if not db.scrub_metadata(conn, dry_run=True)["rows"]:
                    return _cli_scrub_metadata(conn, args)
                try:
                    path = db.backup_db(conn)
                    print(f"Backup written to {path}")
                except Exception as e:  # noqa: BLE001
                    print(f"warning: backup failed, aborting: {e}", file=sys.stderr)
                    return 1
            return _cli_scrub_metadata(conn, args)
        if args.command == "doctor":
            return _cli_doctor(conn, args)
        print(f"unknown --cli command: {args.command}", file=sys.stderr)
        return 2
    finally:
        conn.close()


# ===========================================================================
# TUI mode
# ===========================================================================
_TUI_CACHE: dict = {}


def _tui_classes() -> dict:
    """Import textual and build the app classes once (lazy, cached)."""
    if _TUI_CACHE:
        return _TUI_CACHE
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Horizontal, Vertical, VerticalScroll
    from textual.screen import ModalScreen, Screen
    from textual.widgets import (
        Button,
        DataTable,
        Footer,
        Header,
        Input,
        Label,
        Static,
        TabbedContent,
        TabPane,
    )

    # ---- confirm modal -----------------------------------------------------
    class ConfirmScreen(ModalScreen[bool]):
        def __init__(self, message: str, danger: bool = True):
            super().__init__()
            self.message = message
            self.danger = danger

        def compose(self) -> ComposeResult:
            with Vertical(id="confirm-box"):
                yield Label("⚠  Confirm action" if self.danger else "Confirm", id="confirm-title")
                yield Static(self.message, id="confirm-msg")
                with Horizontal(id="confirm-buttons"):
                    yield Button("Cancel (Esc)", variant="default", id="no")
                    yield Button("Confirm (y)", variant="error" if self.danger else "primary", id="yes")

        def on_button_pressed(self, event: Button.Pressed) -> None:
            self.dismiss(event.button.id == "yes")

        def key_escape(self) -> None:
            self.dismiss(False)

        def key_y(self) -> None:
            self.dismiss(True)

    # ---- detail screens ----------------------------------------------------
    class DetailScreen(Screen):
        """Shared shell for the three row-detail pages.

        Skill, MCP and plugin detail are one page: a Header, a
        scrolling body, a bold title, the page's own sections, and a Footer,
        with the same escape/q back binding. Only the title and the sections
        differ, so the shell and the binding live here once instead of in three
        hand-copied screens that can drift apart.
        """

        BINDINGS = [Binding("escape,q", "app.pop_screen", "Back")]

        # The title Static's id; the shared CSS styles it. Reused by every
        # subclass so the three pages stay one page visually.
        TITLE_ID = "detail-title"

        def __init__(self, title: str):
            super().__init__()
            self._title = title

        def title_text(self) -> str:
            return f"[b]{self._title}[/b]"

        def compose(self) -> ComposeResult:
            yield Header(show_clock=True)
            with VerticalScroll(id="detail-body"):
                yield Static(self.title_text(), id=self.TITLE_ID)
                yield from self.sections()
            yield Footer()

        def sections(self) -> ComposeResult:
            """The page's own widgets, below the title."""
            raise NotImplementedError

        def on_mount(self) -> None:
            self.populate()
            # The pages fill their tables here, so the sizing belongs here too:
            # one place, and every subclass gets a first frame that fits.
            for table in self.query(DataTable):
                fit_columns(table)

        def populate(self) -> None:
            """Fill the page's tables. Pages without a table do nothing."""

    class SkillDetailScreen(DetailScreen):
        def __init__(self, skill: str, detail: dict):
            super().__init__(detail["name"])
            self.skill = skill
            self.detail = detail

        def sections(self) -> ComposeResult:
            d = self.detail
            yield Static(
                f"[dim]source[/dim]   {d.get('source')}\n"
                f"[dim]category[/dim] {d.get('category')}\n"
                f"[dim]path[/dim]     {d.get('path')}\n"
                f"[dim]hash[/dim]     {(d.get('content_hash') or '-')[:16]}"
                f"  ({len(d.get('versions') or [])} versions)\n\n"
                f"[dim]description[/dim]\n{d.get('description') or '(none)'}",
                id="detail-meta",
            )
            sr = db.success_rate(d.get("total"), d.get("success"))
            yield Static(
                f"[b]Stats[/b]\n"
                f"  total {d.get('total', 0)}   success {d.get('success', 0)}   "
                f"errors {d.get('errors', 0)}   denied {d.get('denied', 0)}\n"
                f"  success rate {('%d%%' % round(sr * 100)) if sr is not None else '-'}   "
                f"last 30d {d.get('uses_30d', 0)}   last used {db.fmt_time(d.get('last_used'))}",
                id="detail-stats",
            )
            yield Label("Usage history", id="detail-hist-label")
            table = DataTable(id="detail-history", zebra_stripes=True)
            table.cursor_type = "row"
            yield table
            if d.get("versions"):
                yield Label("Version history (SKILL.md content changes)", id="detail-ver-label")
                vt = DataTable(id="detail-versions", zebra_stripes=True)
                yield vt

        def populate(self) -> None:
            table = self.query_one("#detail-history", DataTable)
            table.add_columns("Time", "Project", "Session", "Status", "Duration", "Model", "Agent", "Branch")
            for r in self.detail.get("history", []):
                table.add_row(
                    db.fmt_time(r["timestamp"]),
                    db.short_path(r["project_path"], 28),
                    db.short_session(r["session_id"]),
                    r["status"],
                    f"{r['duration_ms']}ms" if r["duration_ms"] is not None else "-",
                    r["model"] or "-",
                    r["agent"] or "-",
                    r["branch"] or "-",
                )
            if self.detail.get("versions"):
                vt = self.query_one("#detail-versions", DataTable)
                vt.add_columns("Recorded", "Hash", "Bytes")
                for v in self.detail["versions"]:
                    vt.add_row(db.fmt_time(v["recorded_at"]), (v["content_hash"] or "")[:16], str(v["size_bytes"] or "-"))

    # ---- mcp / plugin detail screens ------------------------------------
    class McpDetailScreen(DetailScreen):
        def __init__(self, server: str, tool: str, detail: dict):
            super().__init__(f"{server}.{tool}")
            self.server = server
            self.tool = tool
            self.detail = detail

        def sections(self) -> ComposeResult:
            d = self.detail
            sr = db.success_rate(d.get("total"), d.get("success"))
            # Keep `avg` out of the conditional: an inline `... if x else ...`
            # here binds to the whole concatenated string, which silently
            # dropped "last used" when avg was present and the entire stats
            # block when it was absent.
            avg = d.get("avg_ms")
            avg_part = f"avg {round(avg)}ms   " if avg is not None else ""
            yield Static(
                f"[dim]server[/dim] {self.server}   [dim]tool[/dim] {self.tool}\n"
                f"total {d.get('total', 0)}   success {d.get('success', 0)}   "
                f"errors {d.get('errors', 0)}   denied {d.get('denied', 0)}\n"
                f"success rate {('%d%%' % round(sr * 100)) if sr is not None else '-'}   "
                f"{avg_part}last used {db.fmt_time(d.get('last_used'))}",
                id="detail-stats",
            )
            yield Label("Call history (Enter on the MCP tab opens this page)", id="detail-hist-label")
            table = DataTable(id="detail-history", zebra_stripes=True)
            table.cursor_type = "row"
            yield table

        def populate(self) -> None:
            table = self.query_one("#detail-history", DataTable)
            table.add_columns("Time", "Project", "Session", "Status", "Duration", "Trigger", "Args")
            for r in self.detail.get("history", []):
                table.add_row(
                    db.fmt_time(r["timestamp"]),
                    db.short_path(r["project_path"], 28),
                    db.short_session(r["session_id"]),
                    r["status"],
                    f"{r['duration_ms']}ms" if r["duration_ms"] is not None else "-",
                    r.get("trigger_type") or "-",
                    (r.get("arg_names") or "")[:50],
                )

    class PluginDetailScreen(DetailScreen):
        def __init__(self, plugin: str, kind: str, item: str, detail: dict):
            super().__init__(f"{plugin} / {kind} / {item}")
            self.plugin = plugin
            self.kind = kind
            self.item = item
            self.detail = detail

        def sections(self) -> ComposeResult:
            d = self.detail
            sr = db.success_rate(d.get("total"), d.get("success"))
            yield Static(
                f"total {d.get('total', 0)}   success {d.get('success', 0)}   "
                f"errors {d.get('errors', 0)}   denied {d.get('denied', 0)}\n"
                f"success rate {('%d%%' % round(sr * 100)) if sr is not None else '-'}   "
                f"last used {db.fmt_time(d.get('last_used'))}",
                id="detail-stats",
            )
            yield Label("Call history (Enter on the Plugins tab opens this page)", id="detail-hist-label")
            table = DataTable(id="detail-history", zebra_stripes=True)
            table.cursor_type = "row"
            yield table

        def populate(self) -> None:
            table = self.query_one("#detail-history", DataTable)
            table.add_columns("Time", "Project", "Session", "Status", "Duration", "Trigger")
            for r in self.detail.get("history", []):
                table.add_row(
                    db.fmt_time(r["timestamp"]),
                    db.short_path(r["project_path"], 28),
                    db.short_session(r["session_id"]),
                    r["status"],
                    f"{r['duration_ms']}ms" if r["duration_ms"] is not None else "-",
                    r.get("trigger_type") or "-",
                )

    # ---- health screen (read-only) ----------------------------------------
    class HealthScreen(Screen):
        BINDINGS = [Binding("escape,q", "app.pop_screen", "Back")]

        def __init__(self, report: dict):
            super().__init__()
            self.report = report

        def compose(self) -> ComposeResult:
            r = self.report
            c = r["counts"]
            yield Header(show_clock=True)
            with VerticalScroll(id="health-body"):
                yield Static("[b]Skill health[/b] (read-only; nothing is deleted)", id="health-title")
                yield Static(
                    f"[b]Active[/b] {c['active']}   [b]Stale[/b] {c['stale']}   "
                    f"[b]Unused[/b] {c['unused']}   [dim]/ total {c['total']}[/dim]",
                    id="health-counts",
                )
                yield Static(
                    f"[dim]Observed {r['sample']['sessions']} session(s) over "
                    f"{r['sample']['days_observed']} day(s)[/dim]",
                    id="health-sample",
                )
                yield Static(
                    "[b]Risk flags[/b]  "
                    f"never used {r['risk_counts']['never_used']}   "
                    f"high failure {r['risk_counts']['high_failure']}   "
                    f"frequently edited {r['risk_counts']['frequently_edited']}",
                    id="health-risks",
                )
                yield Label("Suggestions (advisory only, never executed)", id="health-sug-label")
                yield Static("\n".join(f"  · {s}" for s in r["suggestions"]), id="health-sug")
                yield Label("Details", id="health-list-label")
                table = DataTable(id="health-table", zebra_stripes=True)
                table.cursor_type = "row"
                yield table
            yield Footer()

        def on_mount(self) -> None:
            table = self.query_one("#health-table", DataTable)
            table.add_columns("Skill", "State", "Uses", "Failure rate", "Versions", "Flags")
            for s in self.report["skills"]:
                fr = f"{round(s['failure_rate']*100)}%" if s["failure_rate"] is not None else "-"
                table.add_row(
                    s["skill_name"], s["bucket"], str(s["total"]), fr,
                    str(s["versions"] or 0), ",".join(s["flags"]) or "-",
                )
            fit_columns(table)

    # ---- main screen -------------------------------------------------------
    class MainScreen(Screen):
        # Wall-clock time of the last full re-read; drives _maybe_refresh.
        _last_refresh: float = 0.0

        # Where each page's status line lives. A page stamps its own line with
        # when *it* was read (see _freshness), because with per-page refresh
        # the pages are read at different times.
        _PAGE_STATUS_WIDGET = {
            "tab-dash": "#cards-legend",
            "tab-skills": "#sort-label",
            "tab-mcp": "#mcp-label",
            "tab-plugins": "#plugins-label",
            "tab-recent": "#recent-label",
            "tab-cats": "#cats-label",
            "tab-agents": "#agents-label",
            "tab-data": "#data-info",
        }

        def __init__(self):
            super().__init__()
            # Read time per tab id, plus the base text and the failure note of
            # each page's status line. A single global "data as of" would be a
            # lie once pages refresh independently.
            self._page_read_at: dict[str, float] = {}
            self._page_status_base: dict[str, str] = {}
            self._page_stale: dict[str, str] = {}

        BINDINGS = [
            Binding("q", "app.quit", "Quit"),
            Binding("r", "refresh_data", "Refresh"),
            Binding("slash", "focus_search", "Search"),
            Binding("s", "cycle_sort", "Sort"),
            Binding("tab", "next_tab", "Tab", priority=True),
            # Ctrl alternates: always reachable even while the search box has
            # focus (a focused Input consumes plain letters).
            Binding("ctrl+r", "refresh_data", "Refresh", show=False),
            Binding("ctrl+s", "cycle_sort", "Sort", show=False),
            Binding("j", "cursor_down", "Down", show=False),
            Binding("k", "cursor_up", "Up", show=False),
            Binding("d", "delete_selected", "Delete", show=False),
            Binding("b", "backup", "Backup", show=False),
            Binding("v", "vacuum", "Vacuum", show=False),
            Binding("e", "export", "Export", show=False),
        ]

        def compose(self) -> ComposeResult:
            yield Header(show_clock=True)
            with TabbedContent(initial="tab-dash"):
                with TabPane("Dashboard", id="tab-dash"):
                    # The dashboard is taller than most terminals (cards + legend
                    # + 3 charts + 3 top-10 tables). Without a scroll container
                    # Textual renders the overflow *off-screen* and the last
                    # sections become unreachable, so it must scroll.
                    with VerticalScroll(id="dash-body"):
                        with Horizontal(id="cards"):
                            yield Static(id="card-skills", classes="card")
                            yield Static(id="card-usage", classes="card")
                            yield Static(id="card-mcp", classes="card")
                            yield Static(id="card-plugin", classes="card")
                            yield Static(id="card-today", classes="card")
                            yield Static(id="card-rate", classes="card")
                        yield Static(id="cards-legend")
                        yield Label("Last 7 days", classes="section")
                        with Horizontal(id="trend-row"):
                            yield Static(id="trend-skills", classes="trend")
                            yield Static(id="trend-mcp", classes="trend")
                            yield Static(id="trend-plugins", classes="trend")
                        yield Label("Top Skills", classes="section")
                        t = DataTable(id="dash-top", zebra_stripes=True)
                        t.cursor_type = "row"
                        yield t
                        yield Label("Top MCP tools", classes="section")
                        # All three dashboard tables need the row cursor: Enter
                        # raises RowSelected, which is what opens a detail page.
                        # With the default cell cursor Enter silently did
                        # nothing while the label below promised otherwise.
                        t = DataTable(id="dash-mcp", zebra_stripes=True)
                        t.cursor_type = "row"
                        yield t
                        yield Label("Top Plugins", classes="section")
                        t = DataTable(id="dash-plugins", zebra_stripes=True)
                        t.cursor_type = "row"
                        yield t
                with TabPane("Skills", id="tab-skills"):
                    yield Input(
                        placeholder="Filter (Esc clears)  ·  ctrl+s sort · ctrl+r refresh",
                        id="search",
                    )
                    yield Static(id="sort-label")
                    t = DataTable(id="skills-table", zebra_stripes=True)
                    t.cursor_type = "row"
                    yield t
                with TabPane("MCP", id="tab-mcp"):
                    yield Input(
                        placeholder="Filter server/tool (Esc clears)  ·  s sort · Enter detail",
                        id="mcp-search",
                    )
                    yield Static(id="mcp-label")
                    t = DataTable(id="mcp-table", zebra_stripes=True)
                    t.cursor_type = "row"
                    yield t
                with TabPane("Plugins", id="tab-plugins"):
                    yield Input(
                        placeholder="Filter plugin/item (Esc clears)  ·  s sort · Enter detail",
                        id="plugins-search",
                    )
                    yield Static(id="plugins-label")
                    t = DataTable(id="plugins-table", zebra_stripes=True)
                    t.cursor_type = "row"
                    yield t
                with TabPane("Recent", id="tab-recent"):
                    yield Static(
                        "[dim]Skills + MCP + plugins in one timeline  ·  "
                        "Enter opens the row's detail page[/dim]",
                        id="recent-label",
                    )
                    t = DataTable(id="recent-table", zebra_stripes=True)
                    t.cursor_type = "row"
                    yield t
                with TabPane("Categories", id="tab-cats"):
                    yield Static(id="cats-label")
                    t = DataTable(id="cats-table", zebra_stripes=True)
                    t.cursor_type = "row"
                    yield t
                with TabPane("Agents", id="tab-agents"):
                    yield Static(id="agents-label")
                    t = DataTable(id="agents-table", zebra_stripes=True)
                    # `row`, not the default `cell`: a cell cursor leaves
                    # `active_row` None and Enter then selects nothing.
                    t.cursor_type = "row"
                    yield t
                with TabPane("Data", id="tab-data"):
                    yield Static(
                        "[b]Data management[/b]\n"
                        "  b   Backup database (VACUUM INTO, 0600)\n"
                        "  v   Vacuum database (VACUUM)\n"
                        "  e   Export JSON (0600)\n"
                        "  r   Refresh\n\n"
                        "Deleting a skill is done from the Skills tab: select a row, press d.\n"
                        "Dangerous actions always ask for confirmation.",
                        id="data-help",
                    )
                    yield Static(id="data-info")
                    with Horizontal(id="data-buttons"):
                        yield Button("Backup", id="btn-backup", variant="primary")
                        yield Button("Vacuum", id="btn-vacuum")
                        yield Button("Export", id="btn-export")
                        yield Button("Health", id="btn-health")
                        yield Button("Clear usage", id="btn-clear", variant="warning")
                    yield Static(id="data-result")
            yield Footer()

        # -- lifecycle ------------------------------------------------------
        def on_mount(self) -> None:
            try:
                self.app.conn = db.open_db(self.app.db_path, readonly=False)
            except sqlite3.Error as e:
                # A database that will not open is not a reason to hand the
                # user Textual's internal traceback: say what failed, on the
                # screen, and stop there.
                self._fail_with(f"Cannot open {self.app.db_path}: {e}")
                return
            try:
                db.ensure_schema(self.app.conn)
            except Exception as e:  # noqa: BLE001
                self.app.notify(f"Migration failed: {e}", severity="error")
            if not self.app.no_sync:
                try:
                    db.sync_versions(self.app.conn)
                except Exception as e:  # noqa: BLE001
                    self.app.notify(f"sync failed: {e}", severity="warning")
            self._layout_cards()
            # First paint populates every page, so no tab opens empty. After
            # this, only the page on screen is re-read.
            self.refresh_all()

        def _fail_with(self, message: str) -> None:
            """Leave an error on screen, not only in a transient toast."""
            try:
                self.query_one("#cards-legend", Static).update(f"[red]{message}[/red]")
            except Exception:  # noqa: BLE001 - widget not composed yet
                pass
            self.app.notify(message, severity="error")

        def _maybe_refresh(self) -> None:
            """Re-read the data if what is on screen has gone stale."""
            if self.app.conn is None or self.app.screen is not self:
                return
            if time.time() - self._last_refresh < REFRESH_STALE_AFTER_S:
                return
            self.refresh_data()

        def on_key(self, event) -> None:
            # Navigating is a reason to look again: another process may have
            # written since the screen was drawn.
            self._maybe_refresh()

        def _freshness(self, page: str) -> str:
            """The suffix a page's status line carries.

            Either when that page's data was last read, or that its last read
            failed. Computed from the page's own stamp, never a global clock,
            so a page that was not re-read cannot look freshly read.
            """
            stale = self._page_stale.get(page)
            if stale:
                return f"  ·  [red]Partly stale[/red] ({stale})"
            ts = self._page_read_at.get(page)
            if not ts:
                return ""
            return f"  ·  [dim]data as of {datetime.fromtimestamp(ts).strftime('%H:%M:%S')}[/dim]"

        def _paint_status(self, page: str) -> None:
            """Write `base_text + freshness` to the page's status widget.

            Always recomputed from the stored base, so a failure repaint cannot
            stack a second suffix on top of the first.
            """
            wid = self._PAGE_STATUS_WIDGET.get(page)
            if not wid:
                return
            base = self._page_status_base.get(page, "")
            try:
                self.query_one(wid, Static).update(base + self._freshness(page))
            except Exception:  # noqa: BLE001 - widget not composed yet
                pass

        def on_tabbed_content_tab_activated(self, event) -> None:
            # Switching tabs is when a user looks; do not make them press `r`.
            # Only the page they switched to is re-read.
            if self.app.conn is not None:
                self.refresh_data()

        def on_resize(self, event) -> None:
            self._layout_cards()

        def _layout_cards(self) -> None:
            """Six cards across on a wide terminal, 3x2 on a narrow one.

            A single six-across row wraps every label below ~120 columns
            ("Plugin calls" becomes "Plugin" / "calls"), which looks broken.
            """
            try:
                cards = self.query_one("#cards")
            except Exception:  # noqa: BLE001 - before compose in tests
                return
            wide = self.size.width >= 120
            cards.styles.grid_size_columns = 6 if wide else 3
            cards.styles.grid_size_rows = 1 if wide else 2

        # -- rendering ------------------------------------------------------
        # Which tables belong to which page, so a per-page refresh sizes only
        # what it just wrote. Kept beside `_page_sections`, which owns the same
        # page list: a new tab needs an entry in both or its tables stay
        # header-wide until the pump idles.
        _PAGE_TABLES = {
            "tab-dash": ("#dash-top", "#dash-mcp", "#dash-plugins"),
            "tab-skills": ("#skills-table",),
            "tab-mcp": ("#mcp-table",),
            "tab-plugins": ("#plugins-table",),
            "tab-recent": ("#recent-table",),
            "tab-cats": ("#cats-table",),
            "tab-agents": ("#agents-table",),
            "tab-data": (),
        }

        def _page_sections(self) -> dict:
            """The sections each tab owns, by tab id.

            The active tab decides what a refresh re-reads, so a tab that is not
            on screen is never queried.
            """
            conn = self.app.conn
            return {
                "tab-dash": [
                    ("cards", lambda: self._render_cards(conn)),
                    ("trends", lambda: self._render_trends(conn)),
                    ("top tables", lambda: self._render_top_tables(conn)),
                ],
                "tab-skills": [("skills", lambda: self._render_skills_section(conn))],
                "tab-mcp": [("mcp", self.render_mcp)],
                "tab-plugins": [("plugins", self.render_plugins)],
                "tab-recent": [("recent timeline", self._render_recent_section)],
                "tab-cats": [("categories", lambda: self._render_categories(conn))],
                "tab-agents": [("agents", lambda: self._render_agents(conn))],
                "tab-data": [("data page", self._render_data_info)],
            }

        def _active_page(self) -> str:
            try:
                return self.query_one(TabbedContent).active
            except Exception:  # noqa: BLE001 - before compose in tests
                return "tab-dash"

        def refresh_data(self) -> None:
            """Re-read the page that is on screen.

            A page that is not visible is not re-queried, so switching tabs no
            longer repaints every page. The explicit `r` key and the first
            paint use `refresh_all`.
            """
            self._refresh_pages([self._active_page()])

        def refresh_all(self) -> None:
            """Re-read every page. Used for the first paint and the `r` key."""
            self._refresh_pages(list(self._page_sections()))

        def _refresh_pages(self, pages) -> None:
            """Run each named page's sections, isolated from the others.

            Each section is attempted on its own: a failure halfway through
            leaves the sections it already wrote current and marks only its own
            page stale, instead of the whole screen looking refreshed.
            """
            if self.app.conn is None:
                return
            sections = self._page_sections()
            failures: list[tuple[str, str]] = []
            now = time.time()
            for page in pages:
                # Stamp the read before the sections run, so a page's own
                # status line can say when this read happened.
                self._page_read_at[page] = now
                self._page_stale.pop(page, None)
                for label, render in sections.get(page, []):
                    try:
                        render()
                    except Exception as e:  # noqa: BLE001
                        failures.append((page, f"{label}: {e}"))
            # Size the tables that were just written. Textual would do this in
            # `_on_idle`, one frame too late — see `fit_columns`.
            for page in pages:
                for wid in self._PAGE_TABLES.get(page, ()):
                    try:
                        fit_columns(self.query_one(wid, DataTable))
                    except Exception as e:  # noqa: BLE001
                        failures.append((page, f"{wid} widths: {e}"))
            self._last_refresh = now
            if failures:
                for page, message in failures:
                    self._page_stale[page] = message
                for page in {p for p, _ in failures}:
                    self._paint_status(page)
                self.app.notify("; ".join(m for _, m in failures), severity="error")

        def _render_cards(self, conn) -> None:
            s = db.dashboard_summary(conn)
            self.query_one("#card-skills", Static).update(
                f"[b]{s['total_skills']}[/b]\n[dim]Skills[/dim]"
            )
            self.query_one("#card-usage", Static).update(
                f"[b]{s['total_usage']}[/b]\n[dim]Skill calls[/dim]"
            )
            self.query_one("#card-mcp", Static).update(
                f"[b]{s['total_mcp']}[/b]\n[dim]MCP calls[/dim]"
            )
            self.query_one("#card-plugin", Static).update(
                f"[b]{s.get('total_plugin', 0)}[/b]\n[dim]Plugin calls[/dim]"
            )
            self.query_one("#card-today", Static).update(
                f"[b]{s.get('today_all', s['today_usage'])}[/b]\n[dim]Today (all)[/dim]"
            )
            rows = db.stats_rows(conn)
            tot = sum(r["total"] for r in rows)
            ok = sum(r["success"] for r in rows)
            # The number carries the meaning (green/amber/red), so the card
            # border stays neutral instead of being permanently amber.
            self.query_one("#card-rate", Static).update(
                f"[b]{rate_text(tot, ok)}[/b]\n[dim]Skill success[/dim]"
            )
            self._page_status_base["tab-dash"] = (
                f"[dim]Today (all) = {s['today_usage']} skill + {s['today_mcp']} mcp"
                f" + {s.get('today_plugin', 0)} plugin  ·  "
                f"{s['personal']} personal / {s['open_source']} OSS skills  ·  "
                f"observed {s['sample']['sessions']} session(s) over "
                f"{s['sample']['days_observed']} day(s)[/dim]"
            )
            # The screen only re-reads on interaction, so its own status line
            # says when it last did: idle numbers must never look live.
            self._paint_status("tab-dash")

        def _render_trends(self, conn) -> None:
            # Three 7-day charts side by side, each scaled to its own peak:
            # MCP and plugin volumes are usually an order of magnitude
            # below skill calls, so a shared peak would flatten them
            # invisible.
            #
            # Every day line is exactly TREND_ROW_WIDTH characters — date(5) +
            # two spaces + a padded bar + a count capped by `fmt_count`. A
            # trailing field printed at its natural length makes one line longer
            # than its neighbours whenever a count gains a digit, that chart
            # word-wraps on its own, and the three no longer share a horizontal
            # line. `.trend` pins the height for the same reason: below 70
            # columns (measured: cells are 20 wide at 70, 18/19/19 at 66) all
            # three clip alike instead of drifting apart.
            for wid, label, color, data in (
                ("#trend-skills", "skill calls", "cyan",
                 db.daily_activity(conn, TREND_DAYS)),
                ("#trend-mcp", "MCP calls", "magenta",
                 db.daily_mcp_activity(conn, TREND_DAYS)),
                ("#trend-plugins", "plugin calls", "yellow",
                 db.daily_plugin_activity(conn, TREND_DAYS)),
            ):
                peak = max((d["count"] for d in data), default=0)
                chart = [f"[b]{label}[/b]"]
                for d in data:
                    cells = f"{bar(d['count'], peak, TREND_BARW):<{TREND_BARW}}"
                    chart.append(
                        f"{d['date'][5:]}  [{color}]{cells}[/{color}]"
                        f"{fmt_count(d['count']):>5}"
                    )
                self.query_one(wid, Static).update("\n".join(chart))

        def _render_top_tables(self, conn) -> None:
            # The MCP and Plugins pages render themselves; the dashboard fills
            # only its own top-10 tables, so a dashboard refresh no longer
            # repaints two unrelated pages.
            mcp_rows = db.mcp_stats_rows(conn)
            plugin_rows = db.plugin_stats_rows(conn)

            top_rows = db.top_rows(conn, 10)
            top = self.query_one("#dash-top", DataTable)
            top.clear(columns=True)
            top.add_columns("#", "Skill", "Uses", "Success rate", "Last used")
            for i, r in enumerate(top_rows, 1):
                key = f"dash-skill:{r['skill_name']}"
                self.app.row_targets[key] = ("skill", r["skill_name"])
                top.add_row(
                    str(i), r["skill_name"], str(r["total"]),
                    rate_text(r["total"], r["success"]),
                    db.fmt_time(r["last_used"]),
                    key=key,
                )

            # Top-10 companions to the Skills table above. `mcp_rows` /
            # `plugin_rows` arrive pre-sorted by total DESC, so slicing is
            # the whole ranking. Empty states keep headers only, mirroring
            # the MCP/Plugins tabs on a skills-only DB.
            dm = self.query_one("#dash-mcp", DataTable)
            dm.clear(columns=True)
            dm.add_columns("#", "Server", "Tool", "Calls", "Success rate", "Last used")
            for i, r in enumerate(mcp_rows[:10], 1):
                key = f"dash-mcp:{r['server_name']}:{r['tool_name']}"
                self.app.row_targets[key] = ("mcp", r["server_name"], r["tool_name"])
                dm.add_row(
                    str(i), r["server_name"], r["tool_name"], str(r["total"]),
                    rate_text(r["total"], r["success"]),
                    db.fmt_time(r["last_used"]),
                    key=key,
                )

            dp = self.query_one("#dash-plugins", DataTable)
            dp.clear(columns=True)
            dp.add_columns("#", "Plugin", "Kind", "Item", "Calls", "Success rate", "Last used")
            for i, r in enumerate(plugin_rows[:10], 1):
                key = f"dash-plugin:{r['plugin_name']}:{r['kind']}:{r['item_name']}"
                self.app.row_targets[key] = (
                    "plugin", r["plugin_name"], r["kind"], r["item_name"],
                )
                dp.add_row(
                    str(i), r["plugin_name"], r["kind"], r["item_name"],
                    str(r["total"]),
                    rate_text(r["total"], r["success"]),
                    db.fmt_time(r["last_used"]),
                    key=key,
                )

        def _render_skills_section(self, conn) -> None:
            self.app.all_rows = db.stats_rows(conn)
            # With no usage rows every sort mode collapses to the same
            # order; the Skills tab says so instead of looking broken.
            self.app.has_usage = any(r["total"] for r in self.app.all_rows)
            self.render_skills()

        def _render_recent_section(self) -> None:
            self.render_recent()

        def _render_categories(self, conn) -> None:
            cats = self.query_one("#cats-table", DataTable)
            cats.clear(columns=True)
            cats.add_columns("Source", "Category", "Skills", "Uses", "Success", "Errors", "Denied")
            for r in db.categories(conn):
                cats.add_row(
                    r["source"], r["category"] or "-", str(r["skills"]), str(r["usage"]),
                    str(r["success"]), str(r["errors"]), str(r["denied"]),
                )
            self._page_status_base["tab-cats"] = "[dim]One row per source / category[/dim]"
            self._paint_status("tab-cats")

        def _render_agents(self, conn) -> None:
            """Calls per agent, most first. Read-only, no filter, no sort cycle."""
            table = self.query_one("#agents-table", DataTable)
            table.clear(columns=True)
            table.add_columns(
                "Agent", "Skill", "MCP", "Plugin", "Total", "Success rate", "Last used"
            )
            rows = db.agent_usage_rows(conn)
            for r in rows:
                table.add_row(
                    r["agent"] or "(unknown)", str(r["skill"]), str(r["mcp"]),
                    str(r["plugin"]), str(r["total"]),
                    rate_text(r["total"], r["success"]),
                    db.fmt_time(r["last_used"]),
                )
            unknown = sum(r["total"] for r in rows if r["agent"] is None)
            note = (
                "[dim]One row per agent, most calls first. The agent is the one its"
                " session [b]first[/b] reported: `metadata` is written once and never"
                f" updated, so a call recorded before that is `(unknown)` forever"
                f" ({unknown} such call(s) now). It is not 'the agent that ran this"
                " call'. See M23.[/dim]"
            )
            self._page_status_base["tab-agents"] = note
            self._paint_status("tab-agents")

        def _render_data_info(self) -> None:
            self._page_status_base["tab-data"] = self._data_info()
            self._paint_status("tab-data")

        def _data_info(self) -> str:
            """DB path/size/row counts + last backup, shown on the Data page.

            The page was mostly empty; this is the information you actually
            want before pressing Backup / Vacuum / Clear.
            """
            conn = self.app.conn
            path = self.app.db_path
            try:
                size_kib = os.path.getsize(path) / 1024
            except OSError:
                size_kib = 0.0

            def count(table: str) -> int:
                try:
                    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                except sqlite3.Error:
                    return 0

            latest = None
            try:
                if os.path.isdir(db.BACKUP_DIR):
                    for n in os.listdir(db.BACKUP_DIR):
                        p = os.path.join(db.BACKUP_DIR, n)
                        if os.path.isfile(p) and db.BACKUP_RE.match(n):
                            latest = max(latest or 0, os.path.getmtime(p))
            except OSError:
                pass
            if latest:
                stamp = datetime.fromtimestamp(latest, timezone.utc).astimezone().strftime(
                    "%Y-%m-%d %H:%M"
                )
            else:
                stamp = "never"

            return (
                "[b]Database[/b]\n"
                f"  [dim]path[/dim]  {db.short_path(path, 64)}\n"
                f"  [dim]size[/dim]  {size_kib:.0f} KiB\n"
                f"  [dim]rows[/dim]  {count('skills')} skills · "
                f"{count('skill_usage')} skill calls · {count('mcp_usage')} MCP · "
                f"{count('plugin_usage')} plugin · {count('skill_versions')} versions\n"
                f"  [dim]last backup[/dim]  {stamp}"
            )

        def render_skills(self) -> None:
            needle = self.query_one("#search", Input).value.strip().lower()
            rows = self.app.all_rows
            if needle:
                rows = [
                    r for r in rows
                    if needle in r["skill_name"].lower()
                    or needle in (r["description"] or "").lower()
                    or needle in (r["category"] or "").lower()
                ]
            rows = sort_rows(rows, self.app.sort_mode)

            table = self.query_one("#skills-table", DataTable)
            # Remember the selected row by its unique key (path) so a re-sort or
            # refresh keeps the cursor on the same skill instead of jumping to
            # the top.
            selected = None
            if table.row_count:
                try:
                    selected = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
                except Exception:  # noqa: BLE001
                    selected = None

            hint = "" if self.app.has_usage else "   ·   no usage data yet, all orders coincide"
            self._page_status_base["tab-skills"] = (
                f"[dim]Sort: {SORT_LABELS[self.app.sort_mode]}   ·   "
                f"showing {len(rows)} / {len(self.app.all_rows)}   ·   "
                f"Enter opens detail  ·  d deletes{hint}[/dim]"
            )
            self._paint_status("tab-skills")
            # Columns are static, so a row-level clear is enough (and keeps the
            # column set stable across refreshes).
            if not table.columns:
                table.add_columns("Name", "Source", "Uses", "30d", "Sessions", "Last Used", "Success Rate")
            table.clear()
            self.app.path_to_name = {}
            for r in rows:
                # Rows are keyed by `path` (the only UNIQUE column). Two SKILL.md
                # files sharing a frontmatter name would otherwise raise
                # DuplicateKey and break the whole table.
                self.app.path_to_name[r["path"]] = r["skill_name"]
                self.app.row_targets[r["path"]] = ("skill", r["skill_name"])
                table.add_row(
                    r["skill_name"], r["source"], str(r["total"]),
                    str(r.get("uses_30d", 0)), str(r.get("sessions", 0)),
                    db.fmt_time(r["last_used"]),
                    rate_text(r["total"], r["success"]),
                    key=r["path"],
                )
            if selected is not None:
                try:
                    table.move_cursor(row=table.get_row_index(selected))
                except Exception:  # noqa: BLE001 - filtered out or table empty
                    pass

        def render_recent(self) -> None:
            rows = db.unified_recent_rows(self.app.conn, 100)
            self.app.recent_rows_cache = rows
            table = self.query_one("#recent-table", DataTable)
            if not table.columns:
                table.add_columns("Time", "Kind", "Name", "Project", "Status", "Duration")
            table.clear(columns=False)
            for i, r in enumerate(rows):
                kind = r.get("kind") or "-"
                # Keyed by position in the cache rendered just above, and the
                # detail target is taken from real columns. The old key was
                # "kind:name:timestamp" and was split back apart on Enter —
                # which broke on every name carrying a ":" (a plugin command is
                # `conductor:newTrack`) and on the timestamp's own colons.
                key = f"recent#{i}"
                if kind == "skill":
                    self.app.row_targets[key] = ("skill", r["skill_name"])
                elif kind == "mcp":
                    self.app.row_targets[key] = ("mcp", r["server_name"], r["tool_name"])
                elif kind == "plugin":
                    self.app.row_targets[key] = (
                        "plugin", r["plugin_name"], r["item_kind"], r["item_name"],
                    )
                table.add_row(
                    db.fmt_time(r["timestamp"]),
                    KIND_LABELS.get(kind, kind),
                    (r.get("name") or "")[:60],
                    db.short_path(r["project_path"], 28),
                    status_cell(r.get("status")),
                    f"{r['duration_ms']}ms" if r["duration_ms"] is not None else "-",
                    key=key,
                )
            self._page_status_base["tab-recent"] = (
                "[dim]Skills + MCP + plugins in one timeline  ·  "
                "Enter opens the row's detail page[/dim]"
            )
            self._paint_status("tab-recent")

        def render_mcp(self, rows=None) -> None:
            if rows is None:
                rows = db.mcp_stats_rows(self.app.conn)
            try:
                needle = self.query_one("#mcp-search", Input).value.strip().lower()
            except Exception:  # noqa: BLE001 - before mount in tests
                needle = ""
            if needle:
                rows = [
                    r for r in rows
                    if needle in r["server_name"].lower()
                    or needle in r["tool_name"].lower()
                ]
            rows = sort_rows(rows, self.app.mcp_sort_mode, name_key="tool_id")

            table = self.query_one("#mcp-table", DataTable)
            selected = None
            if table.row_count:
                try:
                    selected = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
                except Exception:  # noqa: BLE001
                    selected = None

            self._page_status_base["tab-mcp"] = (
                f"[dim]Sort: {SORT_LABELS[self.app.mcp_sort_mode]}   ·   "
                f"showing {len(rows)} tool(s)  ·  Enter opens detail[/dim]"
            )
            self._paint_status("tab-mcp")
            if not table.columns:
                table.add_columns(
                    "Server", "Tool", "Calls", "30d", "Sessions", "Success Rate", "Avg", "Last Used"
                )
            table.clear()
            for r in rows:
                avg = r.get("avg_ms")
                # `tool_id` is unique per row, and it is only ever a key: a
                # server name may itself contain "_", so the pair is registered
                # here rather than split back out of the string on Enter.
                self.app.row_targets[r["tool_id"]] = ("mcp", r["server_name"], r["tool_name"])
                table.add_row(
                    r["server_name"], r["tool_name"], str(r["total"]),
                    str(r.get("uses_30d", 0)), str(r.get("sessions", 0)),
                    rate_text(r["total"], r["success"]),
                    f"{round(avg)}ms" if avg is not None else "-",
                    db.fmt_time(r["last_used"]),
                    key=r["tool_id"],
                )
            if selected is not None:
                try:
                    table.move_cursor(row=table.get_row_index(selected))
                except Exception:  # noqa: BLE001 - table empty
                    pass

        def render_plugins(self, rows=None) -> None:
            if rows is None:
                rows = db.plugin_stats_rows(self.app.conn)
            try:
                needle = self.query_one("#plugins-search", Input).value.strip().lower()
            except Exception:  # noqa: BLE001 - before mount in tests
                needle = ""
            if needle:
                rows = [
                    r for r in rows
                    if needle in r["plugin_name"].lower()
                    or needle in r["item_name"].lower()
                    or needle in r["kind"].lower()
                ]
            rows = sort_rows(rows, self.app.plugin_sort_mode, name_key="item_id")

            table = self.query_one("#plugins-table", DataTable)
            selected = None
            if table.row_count:
                try:
                    selected = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
                except Exception:  # noqa: BLE001
                    selected = None

            # The inventory goes in the label rather than a second table: it is
            # a handful of rows, and keeping one table means sorting stays
            # unambiguous.
            # The inventory is init-time state *and* a union: a project's config is
            # only read by sessions started in that project, so its rows mean
            # something different from the ones every session refreshes. Printing
            # all of them under "Loaded" advertised plugins the user had removed.
            current, project, excluded = [], [], []
            for r in db.plugin_inventory_rows(self.app.conn):
                counts = []
                if r["tools"]:
                    counts.append(f"{len(r['tools'])} tool(s)")
                if r["commands"]:
                    counts.append(f"{len(r['commands'])} command(s)")
                entry = r["plugin_name"]
                if counts:
                    entry += f" ({', '.join(counts)})"
                # MM-DD HH:MM only: this is one of several entries on a line.
                entry += f" [dim]{db.fmt_time(r['last_seen'])[5:]}[/dim]"
                if r["skipped"]:
                    excluded.append(entry)
                elif r.get("scope") == "project":
                    project.append(entry)
                else:
                    current.append(entry)

            lines = [
                f"[dim]Sort: {SORT_LABELS[self.app.plugin_sort_mode]}   ·   "
                f"showing {len(rows)} item(s)  ·  Enter opens detail[/dim]"
            ]
            if current or project or excluded:
                lines.append("[b]Inventory[/b]  " + "   ·   ".join(current))
                lines.append(
                    "[dim]what OpenCode loaded at its last start, with the age of"
                    " that sighting. Rows for plugins the global config no longer"
                    " lists are dropped at the next start; clearing usage does not"
                    " touch this list.[/dim]"
                )
            else:
                lines.append(
                    "[dim]No inventory yet — the plugin records it when OpenCode "
                    "starts.[/dim]"
                )
            if project:
                lines.append(
                    "[dim]From a project config, so loaded only in that project: "
                    + "   ·   ".join(project) + "[/dim]"
                )
            if excluded:
                lines.append("[dim]Excluded: " + "   ·   ".join(excluded) + "[/dim]")
            self._page_status_base["tab-plugins"] = "\n".join(lines)
            self._paint_status("tab-plugins")

            if not table.columns:
                table.add_columns(
                    "Plugin", "Kind", "Item", "Calls", "30d", "Sessions", "Success Rate", "Avg", "Last Used"
                )
            table.clear()
            for r in rows:
                # `item_id` is unique per row but not parseable: a scoped npm
                # plugin name (`@scope/pkg@1.0`) contains a "/" itself, so the
                # triple is stored here instead of being split out on Enter.
                self.app.row_targets[r["item_id"]] = (
                    "plugin", r["plugin_name"], r["kind"], r["item_name"],
                )
                avg = r.get("avg_ms")
                table.add_row(
                    r["plugin_name"], r["kind"], r["item_name"], str(r["total"]),
                    str(r.get("uses_30d", 0)), str(r.get("sessions", 0)),
                    rate_text(r["total"], r["success"]),
                    f"{round(avg)}ms" if avg is not None else "-",
                    db.fmt_time(r["last_used"]),
                    key=r["item_id"],
                )
            if selected is not None:
                try:
                    table.move_cursor(row=table.get_row_index(selected))
                except Exception:  # noqa: BLE001 - table empty
                    pass
        def _selected_skill(self) -> str | None:
            """The skill name explicitly selected on the Skills tab, else None.

            Only valid while the Skills tab is active and its table holds focus
            with at least one row. Without this guard, pressing `d` on any other
            tab (or the Data page) would resolve to the hidden skills table's
            cursor at coordinate (0,0) — deleting an arbitrary, unselected skill.
            """
            try:
                tc = self.query_one(TabbedContent)
                if tc.active != "tab-skills":
                    return None
                table = self.query_one("#skills-table", DataTable)
                if not table.has_focus or not table.row_count:
                    return None
                path = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
                return self.app.path_to_name.get(path)
            except Exception:  # noqa: BLE001
                return None

        # -- events ---------------------------------------------------------
        def key_escape(self) -> None:
            """Esc clears the search filter and releases focus."""
            for wid, renderer in (
                ("#search", self.render_skills),
                ("#mcp-search", self.render_mcp),
                ("#plugins-search", self.render_plugins),
            ):
                try:
                    inp = self.query_one(wid, Input)
                except Exception:  # noqa: BLE001
                    continue
                if inp.value:
                    inp.value = ""
                    renderer()
                inp.blur()

        def on_input_changed(self, event: Input.Changed) -> None:
            if event.input.id == "search":
                self.render_skills()
            elif event.input.id == "mcp-search":
                self.render_mcp()
            elif event.input.id == "plugins-search":
                self.render_plugins()

        def _open_skill_detail(self, name: str | None) -> None:
            if not name:
                return
            try:
                detail = db.skill_detail(self.app.conn, name)
            except Exception as e:  # noqa: BLE001
                self.app.notify(f"Could not read detail: {e}", severity="error")
                return
            if detail:
                self.app.push_screen(SkillDetailScreen(name, detail))

        def _open_mcp_detail(self, server: str, tool: str) -> None:
            try:
                detail = db.mcp_tool_detail(self.app.conn, server, tool)
            except Exception as e:  # noqa: BLE001
                self.app.notify(f"Could not read MCP detail: {e}", severity="error")
                return
            if detail:
                self.app.push_screen(McpDetailScreen(server, tool, detail))
            else:
                self.app.notify(f"No MCP history for {server}.{tool}", severity="warning")

        def _open_plugin_detail(self, plugin: str, kind: str, item: str) -> None:
            try:
                detail = db.plugin_item_detail(self.app.conn, plugin, kind, item)
            except Exception as e:  # noqa: BLE001
                self.app.notify(f"Could not read plugin detail: {e}", severity="error")
                return
            if detail:
                self.app.push_screen(PluginDetailScreen(plugin, kind, item, detail))
            else:
                self.app.notify(f"No plugin history for {plugin}/{kind}/{item}", severity="warning")

        def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
            target = self.app.row_targets.get(event.row_key.value)
            if not target:
                return
            kind, *rest = target
            if kind == "skill":
                self._open_skill_detail(rest[0])
            elif kind == "mcp":
                self._open_mcp_detail(rest[0], rest[1])
            elif kind == "plugin":
                self._open_plugin_detail(rest[0], rest[1], rest[2])
            else:
                # This branch used to be the plugin call: an unknown kind was
                # handed three positional arguments it never had, and the handler
                # raised IndexError. New kinds get a branch, never the `else`.
                return

        def on_button_pressed(self, event: Button.Pressed) -> None:
            bid = event.button.id
            if bid == "btn-backup":
                self.action_backup()
            elif bid == "btn-vacuum":
                self.action_vacuum()
            elif bid == "btn-export":
                self.action_export()
            elif bid == "btn-health":
                self.action_health()
            elif bid == "btn-clear":
                self._confirm(
                    "Clear ALL usage records (skills + MCP + plugins)? "
                    "The skills table is kept.",
                    self._do_clear,
                )

        # -- actions --------------------------------------------------------
        def action_refresh_data(self) -> None:
            # An explicit refresh re-reads every page, not just the one shown.
            self.refresh_all()
            self.app.notify("Refreshed")

        def action_focus_search(self) -> None:
            self.query_one(TabbedContent).active = "tab-skills"
            self.query_one("#search", Input).focus()

        def action_cycle_sort(self) -> None:
            # Each table keeps its own sort mode so cycling on MCP does not
            # reshuffle Skills. `app.sort_mode` stays as the Skills mode for
            # backward compatibility (tests + shared helper default).
            active = self.query_one(TabbedContent).active
            if active == "tab-mcp":
                self.app.mcp_sort_mode = SORT_MODES[
                    (SORT_MODES.index(self.app.mcp_sort_mode) + 1) % len(SORT_MODES)
                ]
                self.render_mcp()
            elif active == "tab-plugins":
                self.app.plugin_sort_mode = SORT_MODES[
                    (SORT_MODES.index(self.app.plugin_sort_mode) + 1) % len(SORT_MODES)
                ]
                self.render_plugins()
            elif active == "tab-agents":
                # One order only (most calls first). Falling through to the
                # `else` would cycle the *Skills* sort while the user stands on
                # the Agents page and see nothing change.
                return
            else:
                self.app.sort_mode = SORT_MODES[
                    (SORT_MODES.index(self.app.sort_mode) + 1) % len(SORT_MODES)
                ]
                self.render_skills()

        def action_next_tab(self) -> None:
            tc = self.query_one(TabbedContent)
            ids = [p.id for p in self.query(TabPane)]
            if tc.active in ids:
                tc.active = ids[(ids.index(tc.active) + 1) % len(ids)]

        def _active_table(self) -> DataTable | None:
            tc = self.query_one(TabbedContent)
            mapping = {
                "tab-dash": "#dash-top",
                "tab-skills": "#skills-table",
                "tab-recent": "#recent-table",
                "tab-cats": "#cats-table",
                "tab-mcp": "#mcp-table",
                "tab-plugins": "#plugins-table",
                "tab-agents": "#agents-table",
            }
            sel = mapping.get(tc.active)
            return self.query_one(sel, DataTable) if sel else None

        def action_cursor_down(self) -> None:
            t = self._active_table()
            if t:
                t.focus()
                t.action_cursor_down()

        def action_cursor_up(self) -> None:
            t = self._active_table()
            if t:
                t.focus()
                t.action_cursor_up()

        def _confirm(self, message: str, on_yes) -> None:
            self.app.push_screen(ConfirmScreen(message), lambda ok: on_yes() if ok else None)

        def action_backup(self) -> None:
            try:
                path = db.backup_db(self.app.conn)
                self._result(f"Backed up: {path}")
                self.app.notify(f"Backup done: {os.path.basename(path)}")
            except Exception as e:  # noqa: BLE001
                self._result(f"Backup failed: {e}", ok=False)
                self.app.notify(f"Backup failed: {e}", severity="error")

        def action_health(self) -> None:
            try:
                report = db.health_report(self.app.conn)
            except Exception as e:  # noqa: BLE001
                self.app.notify(f"Health check failed: {e}", severity="error")
                return
            self.app.push_screen(HealthScreen(report))

        def action_vacuum(self) -> None:
            self._confirm("Vacuum the database (VACUUM)?", self._do_vacuum)

        def _do_vacuum(self) -> None:
            try:
                db.vacuum_db(self.app.conn)
                self._result("VACUUM done")
                self.app.notify("VACUUM done")
            except Exception as e:  # noqa: BLE001
                self.app.notify(f"VACUUM failed: {e}", severity="error")

        def action_export(self) -> None:
            # Microseconds avoid a same-second collision between two exports.
            ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
            target = os.path.join(os.path.dirname(self.app.db_path), f"skill-usage-export-{ts}.json")
            try:
                doc = db.export_document(self.app.conn)
                path = db.write_private_json(target, doc, pretty=True)
                self._result(f"Exported: {path}")
                self.app.notify(f"Export done: {os.path.basename(path)}")
            except Exception as e:  # noqa: BLE001
                self._result(f"Export failed: {e}", ok=False)
                self.app.notify(f"Export failed: {e}", severity="error")

        def _do_clear(self) -> None:
            try:
                res = db.clear_usage(self.app.conn, also_skills=False)
                self.refresh_data()
                # Report every stream. The toast used to name only the skill
                # count, silently hiding the MCP and plugin rows it had just
                # deleted.
                total = res["usage_deleted"] + res["mcp_deleted"] + res["plugin_deleted"]
                detail = (
                    f"{res['usage_deleted']} skill, {res['mcp_deleted']} MCP, "
                    f"{res['plugin_deleted']} plugin"
                )
                self._result(f"Cleared {total} usage record(s): {detail}")
                self.app.notify(f"Cleared {total} record(s)")
            except Exception as e:  # noqa: BLE001
                self.app.notify(f"Clear failed: {e}", severity="error")

        def action_delete_selected(self) -> None:
            name = self._selected_skill()
            if not name:
                self.app.notify(
                    "Switch to the Skills tab and select a row with j/k first",
                    severity="warning", timeout=5,
                )
                return
            self._confirm(
                f"Delete all records for [b]{name}[/b] (usage + versions + skills row)?",
                lambda: self._do_delete(name),
            )

        def _do_delete(self, name: str) -> None:
            try:
                res = db.delete_skill(self.app.conn, name)
                self.refresh_data()
                self._result(f"Deleted {name}: {res['usage_deleted']} usage row(s)")
                self.app.notify(f"Deleted {name}")
            except Exception as e:  # noqa: BLE001
                self.app.notify(f"Delete failed: {e}", severity="error")

        def _result(self, msg: str, ok: bool = True) -> None:
            """Report an action's outcome on the Data page.

            The colour is set here rather than in CSS: failures used to be
            written in the success colour, so a failed backup looked fine.
            """
            try:
                w = self.query_one("#data-result", Static)
                w.styles.color = "green" if ok else "red"
                w.update(msg)
            except Exception:  # noqa: BLE001
                pass

    # ---- app ---------------------------------------------------------------
    class SkillTUI(App):
        CSS = """
        Screen { layout: vertical; }
        #dash-body { padding: 0 1; }
        /* Grid so the six cards keep their full labels instead of wrapping:
           six columns on a wide terminal, three columns (two rows) on a
           narrow one. on_resize switches grid-size-columns. */
        #cards {
            layout: grid; grid-size: 6 1; grid-rows: 5; grid-gutter: 0 1;
            height: auto;
        }
        .card {
            width: 1fr; height: 5; padding: 0 1;
            border: round $primary; content-align: center middle; text-align: center;
            background: $surface;
        }
        #cards-legend { height: auto; padding: 0 2; }
        #dash-top, #dash-mcp, #dash-plugins { height: auto; max-height: 13; }
        #recent-label { padding: 1 2 0 2; height: auto; }
        #trend-row { height: auto; border: round $primary-muted; margin: 0 2; padding: 0 1; }
        /* height is pinned to TREND_LINES (skill-tui.py) so one chart that has
           to clip cannot stand taller than the two beside it. Keep the two in
           step: test_tui_trend_css_height_matches_the_line_count fails if they
           drift. */
        .trend { width: 1fr; height: 8; }
        .section { padding: 1 2 0 2; }
        #sort-label, #mcp-label, #plugins-label, #cats-label, #agents-label { padding: 0 2; height: auto; }
        #search, #mcp-search, #plugins-search { margin: 0 2; }
        DataTable { height: 1fr; margin: 0 1; }
        DataTable > .datatable--header { text-style: bold; }
        #data-help { padding: 1 2; }
        #data-info { padding: 0 2 1 2; height: auto; }
        #data-buttons { height: 3; padding: 0 2; }
        /* colour is set from _result() so failures are not shown in green */
        #data-result { padding: 1 2; }
        #detail-body { padding: 1 2; }
        #detail-title { padding: 1 0; text-style: bold; }
        #detail-meta, #detail-stats { padding: 1 2; border: round $primary-muted; margin: 0 0 1 0; }
        #detail-hist-label, #detail-ver-label { padding: 1 0 0 0; text-style: bold; }
        #detail-history { height: auto; max-height: 18; }
        #detail-versions { height: auto; max-height: 12; }
        #health-body { padding: 1 2; }
        #health-title { padding: 1 0; }
        #health-counts, #health-risks { padding: 0 0 1 0; }
        #health-sug-label, #health-list-label { padding: 1 0 0 0; }
        #health-sug { padding: 0 0 1 0; }
        #health-table { height: auto; max-height: 20; }
        ConfirmScreen { align: center middle; }
        #confirm-box {
            width: 60; height: auto; padding: 1 2;
            border: thick $error; background: $surface;
        }
        #confirm-msg { padding: 1 0; }
        #confirm-buttons { height: 3; align: right middle; }
        #confirm-buttons Button { margin: 0 1; }
        """
        TITLE = "Skill Tracker"
        SUB_TITLE = "OpenCode skill usage"

        def __init__(self, db_path: str, no_sync: bool = False):
            super().__init__()
            self.db_path = db_path
            self.no_sync = no_sync
            self.conn = None
            self.all_rows: list[dict] = []
            self.recent_rows_cache: list[dict] = []
            self.path_to_name: dict = {}
            # row key -> what to open. A row's identity is (server, tool) or
            # (plugin, kind, item); any single-string encoding of it is
            # ambiguous, because those names contain the separators themselves
            # (`@scope/name`, `conductor:newTrack`).
            self.row_targets: dict = {}
            self.has_usage = True
            self.sort_mode = "count"
            self.mcp_sort_mode = "count"
            self.plugin_sort_mode = "count"

        def on_mount(self) -> None:
            self.push_screen(MainScreen())

        def on_unmount(self) -> None:
            try:
                if self.conn:
                    self.conn.close()
            except Exception:  # noqa: BLE001
                pass

    _TUI_CACHE.update(
        {
            "ConfirmScreen": ConfirmScreen,
            "DetailScreen": DetailScreen,
            "SkillDetailScreen": SkillDetailScreen,
            "McpDetailScreen": McpDetailScreen,
            "PluginDetailScreen": PluginDetailScreen,
            "HealthScreen": HealthScreen,
            "MainScreen": MainScreen,
            "SkillTUI": SkillTUI,
        }
    )
    return _TUI_CACHE


def get_app_class():
    """Return the Textual App class (also used by the headless tests)."""
    return _tui_classes()["SkillTUI"]


def tui_main(args) -> int:
    try:
        SkillTUI = get_app_class()
    except ImportError:
        print(
            "skillt: the TUI requires 'textual'.\n"
            "Install it from the repo root:\n"
            "  ./install.sh\n"
            "or manually:\n"
            "  uv venv --python 3.13 .venv && uv pip install -r requirements.txt\n"
            "Or use the headless CLI:  skillt insight | export | sync | mcp | plugins",
            file=sys.stderr,
        )
        return 3

    if not os.path.exists(args.db):
        print(f"No database at {args.db}. Run a skill in OpenCode first.", file=sys.stderr)
        return 1
    if not os.path.isfile(args.db):
        print(f"skillt: --db must be a file, not a directory: {args.db}", file=sys.stderr)
        return 2

    app = SkillTUI(db_path=args.db, no_sync=args.no_sync)
    app.run()
    return 0


# ===========================================================================
# Argument parsing
# ===========================================================================
class Args:
    pass


CLI_COMMANDS = {
    "insight", "export", "sync", "cleanup-selftest", "scrub-metadata", "health",
    "mcp", "plugins", "agentos", "claude-mem", "auto-backup", "doctor",
}


def _int_arg(flag: str, raw, minimum: int | None = None) -> int:
    """Parse an integer flag, failing with a friendly message (exit 2)."""
    try:
        n = int(raw)
    except (TypeError, ValueError):
        print(f"skillt: {flag} requires an integer, got {raw!r}", file=sys.stderr)
        raise SystemExit(2)
    if minimum is not None and n < minimum:
        print(f"skillt: {flag} must be >= {minimum}, got {n}", file=sys.stderr)
        raise SystemExit(2)
    return n


def parse_args(argv):
    a = Args()
    a.db = db.DB_PATH
    a.cli = False
    a.command = None
    a.json = False
    a.days = 30
    a.min_uses = 3
    a.limit = 10
    a.out = None
    a.pretty = False
    a.force = False
    a.skills_only = False
    a.dry_run = False
    a.prune_orphans = False
    a.freshness_days = CAPTURE_FRESHNESS_DAYS
    a.yes = False
    a.no_sync = False

    rest = []
    i = 0
    while i < len(argv):
        t = argv[i]
        if t == "--cli":
            a.cli = True
        elif t == "--json":
            a.json = True
        elif t == "--pretty":
            a.pretty = True
        elif t == "--force":
            a.force = True
        elif t == "--skills-only":
            a.skills_only = True
        elif t == "--dry-run":
            a.dry_run = True
        elif t == "--prune-orphans":
            a.prune_orphans = True
        elif t == "--yes":
            a.yes = True
        elif t == "--no-sync":
            a.no_sync = True
        elif t in ("--db", "--days", "--min-uses", "--limit", "--out", "--freshness-days"):
            i += 1
            if i >= len(argv):
                raise SystemExit(f"{t} requires a value")
            val = argv[i]
            if t == "--db":
                a.db = val
            elif t == "--days":
                a.days = _int_arg("--days", val, minimum=1)
            elif t == "--min-uses":
                a.min_uses = _int_arg("--min-uses", val, minimum=0)
            elif t == "--limit":
                a.limit = _int_arg("--limit", val, minimum=1)
            elif t == "--freshness-days":
                a.freshness_days = _int_arg("--freshness-days", val, minimum=1)
            else:
                a.out = val
        elif t in ("-h", "--help"):
            print(__doc__)
            raise SystemExit(0)
        else:
            rest.append(t)
        i += 1

    if rest:
        a.command = rest[0]
        if a.command not in CLI_COMMANDS:
            raise SystemExit(f"unknown command: {a.command} (expected one of {sorted(CLI_COMMANDS)})")
    return a


def _reconfigure_streams(streams=None) -> None:
    """Force UTF-8 on stdout/stderr so non-ASCII output never raises.

    Under a C/POSIX locale Python may pick an ASCII codec; the box-drawing and
    arrow characters would then raise UnicodeEncodeError. Best effort: exotic
    wrappers without reconfigure() are left alone.
    """
    for s in (streams if streams is not None else (sys.stdout, sys.stderr)):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def main(argv):
    _reconfigure_streams()
    args = parse_args(argv)
    if args.cli:
        if not args.command:
            raise SystemExit(
                "--cli requires a subcommand: "
                "insight|export|sync|cleanup-selftest|scrub-metadata|health|mcp|"
                "plugins|agentos|claude-mem|auto-backup|doctor"
            )
        return cli_main(args)

    # Textual's Linux driver reads stdin and writes stderr, so all three streams
    # must be a terminal. Checking only stdout let `skillt < /dev/null` through,
    # which then spun on EOF and left the terminal in a broken state.
    if not (sys.stdin.isatty() and sys.stdout.isatty() and sys.stderr.isatty()):
        print(
            "skillt: not an interactive terminal (stdin/stdout/stderr must all be TTYs); "
            "cannot start the TUI.\n"
            "Try instead: skillt insight | skillt export | skillt sync | skillt mcp | skillt plugins\n"
            "Or:          skillt --cli insight",
            file=sys.stderr,
        )
        return 2
    if os.environ.get("TERM", "") in ("", "dumb"):
        print(
            "skillt: TERM is unset or 'dumb'; cannot start the TUI.\n"
            "Try instead: skillt insight | skillt export | skillt sync | skillt health",
            file=sys.stderr,
        )
        return 2
    return tui_main(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
