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
import opencode_compat as compat  # noqa: E402
import skill_db_claude_mem as cm  # noqa: E402
import skill_db_agentos as aos  # noqa: E402
import settings as cfg  # noqa: E402

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


def _cli_prune_usage(conn, args) -> int:
    """`skillt prune-usage` — delete rows past the retention window, off by default.

    The dry run is the default, and its numbers come from the same SELECT the delete
    uses: this is the only command in the tool that removes recorded history, so a
    count that merely looked like the outcome would be the worst kind of report. A
    backup is taken by the caller before `--yes` reaches this function, and a failed
    backup means no delete.
    """
    res = db.prune_usage(conn, usage_days=args.keep_days,
                         max_versions=args.keep_versions, dry_run=not args.yes)
    plan = res["plan"]

    if args.json:
        doc = dict(plan)
        doc.update({"dry_run": res["dry_run"], "deleted": res["deleted"],
                    "checkpointed": res["checkpointed"],
                    "note": "deleting rows frees pages; the file does not shrink "
                            "until `skillt vacuum`"})
        print(json.dumps(doc, ensure_ascii=False, indent=2))
        return 0

    if not plan["enabled"]:
        print("Retention is off: both `retention.usage_days` and "
              "`retention.max_skill_versions` are 0, so nothing is scheduled.")
        print("Arm one with `skillt config set retention.usage_days 365`, or pass "
              "--keep-days N here. Either way this command lists the rows first, "
              "and only --yes deletes them (after a backup).")
        return 0

    counts = "  ".join(f"{t}={n}" for t, n in plan["by_table"].items())
    window = (f"older than {plan['usage_days']} day(s)" if plan["usage_days"]
              else "no age limit")
    versions = (f"beyond the newest {plan['max_skill_versions']} per skill"
                if plan["max_skill_versions"] else "every version kept")
    print(f"Retention: {window}  ·  skill_versions {versions}")
    print(f"  tables: {', '.join(plan['tables_present'])}")
    print(f"  never considered: {', '.join(plan['excluded'])}"
          "  (events, not calls — the spawn record is the only witness)")
    if plan["undated"]:
        print(f"  {plan['undated']} row(s) have a timestamp that will not parse; "
              "they are kept, because \"older than N days\" is a claim about a "
              "date we do not have")

    if res["dry_run"]:
        print(f"[dry-run] would delete {plan['total']} row(s): {counts}")
        print("Nothing was written. Re-run with --yes to apply; a backup is taken "
              "first and the delete is one transaction.")
        return 0

    print(f"deleted {res['deleted']['total']} row(s): {counts}")
    print("  rows are gone; the file does not shrink until `skillt vacuum`.")
    if res["checkpointed"] is False:
        print("  warning: the WAL could not be checkpointed (database busy) — "
              "retry later or run `skillt vacuum` when OpenCode is closed.")
    return 0


def _flat_bits(node, prefix=""):
    """`queueDepth=48 · chroma.connected=yes`, booleans spelled out."""
    bits = []
    for key, value in node.items():
        if isinstance(value, dict):
            bits.extend(_flat_bits(value, f"{key}."))
        elif isinstance(value, bool):
            bits.append(f"{prefix}{key}={'yes' if value else 'no'}")
        else:
            bits.append(f"{prefix}{key}={value:,}")
    return bits


def _print_claude_mem_activity(activity, worker, http) -> None:
    """Files first, HTTP last.

    The two log files say whether the plugin is working even when its database is
    missing or its worker is stopped, so they print in every case; the endpoints
    are only this command's, and only ever after the files.
    """
    trace = activity.get("trace")
    if trace:
        print(f"  inject log   {trace['injected']} injected"
              f" ({trace['injected_bare']} bare, {trace['injected_with_source']} with source)"
              f" · {trace['loaded']} loaded · {trace['worker_ensure']} worker ensures"
              f" · {trace['chars_total']:,} chars injected")
        extra = []
        if trace.get("unrecognized"):
            extra.append(f"{trace['unrecognized']} line(s) of a shape this version does not know")
        if trace.get("injected_with_query"):
            extra.append(f"{trace['injected_with_query']} carried a query (counted, never read)")
        if trace.get("truncated"):
            extra.append("read capped at the byte limit, so this is a floor")
        if extra:
            print("               " + " · ".join(extra))
        groups = trace.get("by_project") or {}
        if groups:
            bits = [f"{name} {v['injected']}/{v['loaded']}" for name, v in groups.items()]
            if trace.get("projectless", {}).get("injected") or \
                    trace.get("projectless", {}).get("loaded"):
                pl = trace["projectless"]
                bits.append(f"(no usable project name) {pl['injected']}/{pl['loaded']}")
            print("  by project   " + " · ".join(bits) + "   [injected/loaded]")
        days = trace.get("by_day") or {}
        if days:
            bits = [f"{day[5:]} {v['injected']}" for day, v in days.items()]
            older = trace.get("older_days") or {}
            if older.get("days"):
                bits.append(f"… {older['days']} older day(s): {older['injected']}")
            if (trace.get("undated") or {}).get("injected"):
                bits.append(f"undated {trace['undated']['injected']}")
            print("  by day       " + " · ".join(bits) + "   [injected, local days]")
    log = activity.get("worker_log")
    if log:
        levels = " · ".join(f"{k} {v:,}" for k, v in sorted((log.get("levels") or {}).items()))
        print(f"  worker log   {levels}   ({log['lines']:,} lines, "
              f"{log['size_bytes']:,} bytes scanned)")
        extra = []
        if log.get("unparsed"):
            extra.append(f"{log['unparsed']} line(s) not in `[time] [level] [category]` shape")
        if log.get("truncated"):
            extra.append("scanned bytes capped, so the counts are a floor")
        if extra:
            print("               " + " · ".join(extra))
    if worker.get("available"):
        print(f"  worker       {'up on port ' + str(worker['port']) if worker['alive'] else 'not running'}"
              + (f" · pid {worker['pid']}" if worker.get("pid") else "")
              + (f" · since {worker['started_at']}" if worker.get("started_at") else "")
              + (f" · {worker['reason']}" if worker.get("reason") else ""))
    if http.get("available"):
        for field in ("stats", "queue", "chroma"):
            node = http.get(field)
            if node:
                print(f"  {field:<12} " + " · ".join(_flat_bits(node)))
        if http.get("skipped"):
            print("               not reached inside the shared deadline: "
                  + ", ".join(http["skipped"]))
    elif worker.get("alive"):
        print(f"  http         {http.get('reason') or 'no answer from the worker'}")


def _cli_claude_mem(conn, args) -> int:
    """claude-mem's own ledger, read-only. Nothing here writes to that store."""
    res = cm.claude_mem_summary(conn, days=args.days)
    store = cm.claude_mem_store()
    activity = cm.claude_mem_activity(store=store)
    worker = cm.claude_mem_worker(store=store)
    # Only a headless command reaches this: the same call from the TUI would put a
    # network wait on the path that repaints for every key.
    http = cm.claude_mem_http(worker=worker)
    res["activity"], res["worker"], res["http"] = activity, worker, http
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0
    if not res["available"]:
        print(f"claude-mem: not aggregated ({res['reason']})")
        print("  set OPENCODE_SKILL_TRACKER_CLAUDE_MEM_DB, or CLAUDE_MEM_DIR"
              " pointing at the directory holding claude-mem.db")
        _print_claude_mem_activity(activity, worker, http)
        return 0

    t = res["tables"]
    age = cm.claude_mem_age_days(res)
    print("claude-mem (read-only; that store is never written from here)")
    print("-" * 56)
    print(f"  store      {res['db_path']}")
    print(f"  newest row {res['newest'] or '-'}"
          + (f"  ({age:.1f} d ago)" if age is not None else ""))
    print("  " + "   ".join(
        f"{name} {(t[name] or {}).get('n')}" for name in cm.CLAUDE_MEM_TABLES))
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
    _print_claude_mem_activity(activity, worker, http)
    tw = res.get("tracker_same_window")
    if tw:
        print(f"  same {res['days']} d in this tracker: {tw.get('skill_usage')} skill"
              f" / {tw.get('mcp_usage')} mcp / {tw.get('plugin_usage')} plugin"
              "   (builtins are never measured here, so 0 is not 'nothing happened')")
    return 0


def _cli_agentos(conn, args) -> int:
    """The AgentOS advisor store, read-only. Nothing here writes to that store."""
    res = aos.agentos_summary(conn, limit=args.limit)
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
    # The union, not just the usage rows: a registered tool with no calls is a fact
    # about the plugin, and printing only `plugin_usage` made it look like the
    # recorder was blind to that plugin.
    items = db.plugin_surface_rows(conn)
    never = sum(1 for it in items if not it["ever_called"])

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
    if never:
        print(f"  {never} of them are registered but never called yet — a 0 means"
              " nobody used that tool, not that the recorder missed it")
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


# Everything above the next line that says what OpenCode calls things comes from
# `opencode_compat`, including the hook names these markers look for. The
# filenames (`skill-tracker.js`, `skill-tracker.log`) are ours, so they stay here.
PLUGIN_PATH = os.path.join(
    db.CONFIG_DIR, compat.PLUGIN_SUBDIR, "skill-tracker.js"
)
PLUGIN_MARKERS = compat.PLUGIN_SKILL_MARKERS + compat.PLUGIN_BASE_MARKERS
# Markers of MCP capture. An older plugin records skills only, so a missing
# marker is a WARN (MCP data stays empty), never a FAIL.
PLUGIN_MCP_MARKERS = compat.PLUGIN_MCP_MARKERS
# Same for plugin tool/command capture.
PLUGIN_PLUGIN_MARKERS = compat.PLUGIN_PLUGIN_MARKERS

# The plugin's builtin-tool allowlist is verified against one OpenCode release
# (limitation M13). Read the pin out of the plugin source rather than keeping a
# second copy here, which is exactly the drift this check exists to catch.
VERSION_PIN_RE = compat.VERSION_PIN_RE
OPENCODE_VERSION_TIMEOUT_S = 2

# The tracker's own log. `log()` never throws, so a hook that starts failing is
# invisible in the database — it only ever shows up here. The filename is ours; the
# directory comes from the host's layout through `compat`.
TRACKER_LOG_PATH = os.path.join(
    db.CONFIG_DIR, compat.LOGS_SUBDIR, db.LOG_FILE_NAMES[0]
)

# How stale the newest recorded call may get before doctor says so. A tracker
# that stopped writing looks exactly like an idle machine from the inside. The
# number is `settings`' default for `doctor.freshness_days`, so the knob, the
# `config list` line and the fallback here can never disagree.
CAPTURE_FRESHNESS_DAYS = cfg.spec("doctor.freshness_days")["default"]

# The floor `pyproject.toml`'s `requires-python` states and the READMEs badge.
# Kept as a tuple here because doctor must compare, and `test_doctor.py` pins
# that the three places (this tuple, `requires-python`, the README badge) still
# say the same number rather than trusting anybody to remember.
MIN_PYTHON = (3, 11)

# The plugin log is read from the start and never in full: it grows one line per
# error forever (M17, no rotation), and `doctor` is the cheapest diagnostic this
# tool has — it must not get slower with age. 4 MiB is this project's own choice;
# it happens to match `CLAUDE_MEM_LOG_BYTES_CAP`, and `test_doctor.py` pins that
# they have not drifted apart, but the tracker's bound is not derived from a
# neighbour's. It is also `settings`' default for `log.max_bytes`, which is what
# `skillt rotate-log` rotates at: one number for the reader and the writer, so a line
# in the active log can never be invisible to `doctor`.
TRACKER_LOG_BYTES_CAP = cfg.spec("log.max_bytes")["default"]

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
    want_python = ".".join(str(p) for p in MIN_PYTHON)
    add("env.python", sys.version_info >= MIN_PYTHON,
        f"{sys.version.split()[0]} (want >= {want_python})", warn=True)
    has_textual = _textual_available()
    add("env.textual", has_textual, "available" if has_textual else "not installed", warn=True)
    # Inside *a* virtualenv? The venv's name/path is deliberately not checked:
    # the repo's .venv and the legacy ~/.local/share/opencode/skillt-venv are
    # both valid, so matching on "skillt-venv" reported a false WARN.
    in_venv = sys.prefix != sys.base_prefix
    add("env.venv", in_venv, sys.executable, warn=True)

    # --- Settings -------------------------------------------------------
    # The config file is the one place a value can be typed once and then quietly
    # not apply. Printing only the path would still leave the owner guessing why
    # the screen says 30 after they wrote 45, so the refused values go here.
    try:
        cfg_path = cfg.config_path()
        cfg_values, cfg_problems = cfg.read_file(cfg_path)
        if cfg_problems:
            add("config.file", False, "  ·  ".join(cfg_problems), warn=True)
        elif not os.path.isfile(cfg_path):
            add("config.file", True, f"{cfg_path} — not created, every default applies")
        else:
            add("config.file", True,
                f"{cfg_path} — {len(cfg_values)} key(s) in force")
    except Exception as e:  # noqa: BLE001
        add("config.file", False, f"error: {e}", warn=True)

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
        summary = cm.claude_mem_summary(conn, days=limit)
        if summary["available"]:
            age = cm.claude_mem_age_days(summary)
            obs = (summary["tables"].get("observations") or {}).get("n")
            health = summary.get("health") or {}
            fails = health.get("consecutiveFailures")
            parts = [f"observations {obs}",
                     f"newest {summary['newest'] or '-'}"
                     + (f" ({age:.1f}d)" if age is not None else "")]
            if age is None:
                parts.append("no rows in its own ledger")
            if isinstance(fails, int) and fails:
                parts.append(f"{fails} consecutive observer failures"
                             + (f" ({health.get('lastErrorKind')})"
                                if health.get("lastErrorKind") else ""))
            # Its own log and its own pid file, both read from disk. The ERROR
            # count is the one thing the ledger cannot show: a worker can be
            # healthy-looking and failing on every sync. HTTP is not consulted
            # here at all — doctor stays offline.
            store = cm.claude_mem_store()
            levels = ((cm.claude_mem_activity(store=store).get("worker_log")
                       or {}).get("levels") or {})
            errs = levels.get("ERROR", 0)
            if isinstance(errs, int) and errs:
                parts.append(f"{errs} worker-log ERROR lines")
            worker = cm.claude_mem_worker(store=store)
            if worker["available"]:
                parts.append(f"worker up :{worker['port']}" if worker["alive"]
                             else "worker down (on-demand)")
            want = f"want <= {limit}d"
            if age is None or age > limit or fails or errs:
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
            text, size, truncated = db._read_bounded_text(
                TRACKER_LOG_PATH, TRACKER_LOG_BYTES_CAP
            )
            if text is None:
                add("log.errors", False, f"cannot read {TRACKER_LOG_PATH}", warn=True)
            else:
                lines = [ln for ln in text.splitlines() if "[err]" in ln]
                detail = (
                    f"{len(lines)} error line(s)"
                    + (f"; last: {lines[-1][:90]}" if lines else "")
                )
                if truncated:
                    detail += (
                        f"; counted the first {TRACKER_LOG_BYTES_CAP:,} B of "
                        f"{size:,} B — a floor, not a total"
                    )
                add("log.errors", not lines, detail, warn=True)
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


def _cli_rotate_log(args) -> int:
    """`skillt rotate-log` — keep the plugin's own log from outrunning the reader.

    Rotation is a rename: every line that leaves the live file is still on disk in the
    rotated copy, because those lines are the evidence `doctor log.errors` reads. The
    cap is the same number the reader reads, so an active log can never hold a line
    `doctor` cannot see. Dry-run by default; a scheduled unit passes `--yes`.
    """
    path = TRACKER_LOG_PATH          # module attribute, so a test can point it elsewhere
    res = db.rotate_log(path, args.log_max_bytes, args.log_keep_files,
                        dry_run=not args.yes)

    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0

    def mb(n):
        return f"{n:,} B"

    print(f"Log     {path}")
    if not os.path.isfile(path):
        print(f"  no log yet ({mb(res['size'])}) — the plugin has not initialised; "
              "nothing to rotate")
        return 0
    print(f"  size {mb(res['size'])}  ·  cap {mb(res['max_bytes'])}  ·  "
          f"keep {res['keep_files']} rotated file(s)")

    if not res["needed"]:
        print("  below the cap; nothing to rotate")
        if res["pruned"]:
            print(f"  pruned {len(res['pruned'])} older rotation(s): "
                  + ", ".join(res["pruned"]))
        return 0

    if res["dry_run"]:
        print(f"[dry-run] would move the file to {res['target']} and start a fresh one.")
        if res["would_prune"]:
            print(f"  would prune {len(res['would_prune'])} rotation(s) beyond the "
                  f"number kept: " + ", ".join(res["would_prune"]))
        print("Nothing was moved. Re-run with --yes to apply.")
        return 0

    print(f"rotated to {res['rotated']}  ·  the live file is empty again and the "
          "plugin's next append recreates its contents there")
    print(f"  every previous line is in {res['rotated']} (mode 0600), not deleted")
    if res["pruned"]:
        print(f"  pruned {len(res['pruned'])} older rotation(s): "
              + ", ".join(res["pruned"]))
    return 0


def _cli_config(args) -> int:
    """`skillt config` — every setting, where its value came from, and how to change it.

    `list` is the honest view: value, origin and the plain-language line in the
    language the terminal is asking for. A value whose origin reads
    `config file (rejected)` is the whole reason this command exists — the number
    on screen is then *not* the one the owner typed, and nothing else would say so.
    """
    action = args.positional[0] if args.positional else "list"
    key = args.positional[1] if len(args.positional) > 1 else None
    raw = args.positional[2] if len(args.positional) > 2 else None
    path = cfg.config_path()

    if action == "path":
        print(path)
        return 0

    if action == "explain":
        if not key:
            print("skillt config explain <key>", file=sys.stderr)
            return 2
        try:
            spec = cfg.spec(key)
        except KeyError:
            print(f"unknown setting {key!r} — list them with: skillt config list",
                  file=sys.stderr)
            return 2
        print(f"{spec['key']}\n  default   {spec['default']}\n"
              f"  minimum   {spec['minimum']}\n"
              f"  flag      {spec['flag'] or '(no flag)'}\n"
              f"  env       {cfg.env_name(spec['key'])}\n"
              f"  file      {path}\n\n  {spec['en']}\n  {spec['zh']}")
        return 0

    if action in ("set", "unset"):
        if not key:
            print(f"skillt config {action} <key>{' <value>' if action == 'set' else ''}",
                  file=sys.stderr)
            return 2
        code, msg = (cfg.write_value(key, raw) if action == "set"
                     else cfg.reset_value(key))
        print(msg, file=sys.stderr if code else sys.stdout)
        return code

    if action not in ("list", "get"):
        print(f"unknown action {action!r} — expected: list | get | set | unset | path | explain",
              file=sys.stderr)
        return 2

    resolved, problems = cfg.effective()
    if action == "get":
        if not key:
            print("skillt config get <key>", file=sys.stderr)
            return 2
        if key not in resolved:
            print(f"unknown setting {key!r}", file=sys.stderr)
            return 2
        value, origin = resolved[key]
        if args.json:
            print(json.dumps({"key": key, "value": value, "origin": origin,
                              "default": cfg.spec(key)["default"]}))
        else:
            print(f"{key} = {value}  ({origin})")
        return 0

    if args.json:
        print(json.dumps({
            "path": path,
            "exists": os.path.isfile(path),
            "problems": problems,
            "settings": {k: {"value": v, "origin": o, "default": cfg.spec(k)["default"]}
                         for k, (v, o) in resolved.items()},
        }, indent=2))
        return 0

    print(f"Settings  (file: {path}{'' if os.path.isfile(path) else ' — not created'})")
    print(f"precedence: flag > file > env > default")
    print("-" * 72)
    for spec in cfg.REGISTRY:
        value, origin = resolved[spec["key"]]
        marker = "" if origin != "default" else "  (default)"
        print(f"{spec['key']:<32}{value:>6}   from {origin}{marker}")
        print(f"  {spec['en']}")
    if problems:
        print("-" * 72)
        for p in problems:
            print(f"  ! {p}")
        print("  Fix the file before setting anything: a rewrite would drop these lines.")
    return 0


def cli_main(args) -> int:
    if args.command in ("config", "rotate-log"):
        # Neither needs a database. The log exists before the database does on a
        # fresh machine, and rotation is the command that keeps it readable there.
        return _cli_config(args) if args.command == "config" else _cli_rotate_log(args)
        # Settings need no database: on a fresh machine `skillt config list` is the
        # one command that must work before the first skill has ever run.
        return _cli_config(args)

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
        if args.command in ("sync", "cleanup-selftest", "scrub-metadata", "doctor",
                            "prune-usage"):
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
        if args.command == "prune-usage":
            if args.yes:
                # The discipline `cleanup-selftest` and `scrub-metadata` already keep:
                # probe first, so a database with nothing to delete never gains a
                # pointless backup file — and never *needs* one.
                if not db.plan_usage_retention(
                        conn, args.keep_days, args.keep_versions)["total"]:
                    return _cli_prune_usage(conn, args)
                try:
                    path = db.backup_db(conn)
                    print(f"Backup written to {path}")
                except Exception as e:  # noqa: BLE001
                    print(f"warning: backup failed, aborting: {e}", file=sys.stderr)
                    return 1
            return _cli_prune_usage(conn, args)
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

        # What the eight `show=False` keys do. The Footer drops them with no trace,
        # and this block is the only place a user can find out they exist — the
        # hand-written text it replaces named b/v/e and never mentioned d or j/k.
        # A key missing here falls back to its own Binding description, so adding a
        # binding cannot produce a key that is bound but undocumented.
        _KEY_DISPLAY = {"slash": "/"}
        _KEY_HELP = {
            "ctrl+r": "refresh while the search box has focus",
            "ctrl+s": "cycle the sort while the search box has focus",
            "j": "move the row cursor down",
            "k": "move the row cursor up",
            "d": "delete the skill on the cursor (asks first)",
            "b": "back up the database (VACUUM INTO, 0600)",
            "v": "vacuum the database (VACUUM)",
            "e": "export the database as JSON (0600)",
        }

        @classmethod
        def _key_name(cls, binding) -> str:
            """A key as it is typed: `slash` is the binding's name, `/` the press."""
            return cls._KEY_DISPLAY.get(binding.key, binding.key)

        @classmethod
        def key_help(cls) -> str:
            """The keys the footer does not show, one line each, in binding order."""
            hidden = [b for b in cls.BINDINGS if not b.show]
            if not hidden:
                return "[dim]every key this screen binds is in the footer below[/dim]"
            pad = max(len(cls._key_name(b)) for b in hidden) + 2
            lines = ["[b]Keys the footer at the bottom does not show[/b]"]
            for b in hidden:
                note = cls._KEY_HELP.get(b.key) or (b.description or b.action)
                lines.append(f"  {cls._key_name(b):<{pad}} {note}")
            return "\n".join(lines)

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
                with TabPane("Agents", id="tab-agents"):
                    yield Static(id="agents-label")
                    t = DataTable(id="agents-table", zebra_stripes=True)
                    # `row`, not the default `cell`: a cell cursor leaves
                    # `active_row` None and Enter then selects nothing.
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
                with TabPane("Data", id="tab-data"):
                    yield Static(
                        self.key_help(),
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

        def _empty_note(self, noun: str, total: int = 0, filtered: bool = False,
                        where: str | None = None) -> str:
            """One line naming why a table has no rows.

            A header with no rows underneath it is three states wearing one face:
            nothing has been recorded, a search is hiding everything, or the source
            directory has no skills in it. All three read on screen as "this tool is
            broken", and two of them are not even a problem. The note goes on the
            page's own status line because that is where the freshness suffix already
            lives, so a page never carries two competing claims about its data.
            """
            if filtered:
                return (f"[dim]nothing matches the search   ·   {total:,} {noun} "
                        f"hidden   ·   clear the box to see them[/dim]")
            if noun == "skills" and where:
                return (f"[dim]no skills recorded yet   ·   press r to re-read   ·   "
                        f"the scan reads {where} and `skillt sync` refreshes it[/dim]")
            return (f"[dim]no {noun} recorded yet   ·   press r to re-read   ·   "
                    "the writer adds a row on the next call[/dim]")

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
            # `Spawned` and `Ran as` sit after `Total`, never inside it: the four
            # numbers before them are calls this tracker measured, and a spawn is
            # one event that may contain none. Two columns, because starting a
            # subagent and being one are different facts about the same name.
            table.add_columns(
                "Agent", "Skill", "MCP", "Plugin", "Total", "Spawned", "Ran as",
                "Success rate", "Last used"
            )
            rows = db.agent_usage_rows(conn)
            for r in rows:
                label = r["agent"] or "(unknown)"
                if r["is_subagent"]:
                    label += " ⟨sub⟩"
                # A child-only row has no calls to rate, but its runs did succeed or
                # fail, and printing `-` would throw that away. `Total` stays the
                # sum of the three call streams on every row either way.
                rate = (rate_text(r["total"], r["success"]) if r["total"]
                        else rate_text(r["runs_as"], r["runs_ok"]))
                table.add_row(
                    label, str(r["skill"]), str(r["mcp"]),
                    str(r["plugin"]), str(r["total"]), str(r["subagent"]),
                    str(r["runs_as"]),
                    rate,
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
            note += (
                "\n[dim]Spawned = subagents this agent started. Ran as = times this"
                " name ran as somebody else's subagent (those rows carry ⟨sub⟩) — a"
                " child that only used builtin tools has no calls to show, so its"
                " rate is over runs, not calls. Neither column is ever added into"
                " Total, which stays skill + MCP + plugin. See M24.[/dim]"
            )
            subs = db.subagent_summary_rows(conn)
            if subs:
                bits = []
                for s in subs:
                    label = s["subagent"] or "(unnamed)"
                    tail = f", {s['errors']} failed" if s["errors"] else ""
                    last = f", last {db.fmt_time(s['last_used'])}" if s["last_used"] else ""
                    bits.append(f"{label} ×{s['runs']}{tail}{last}")
                note += (
                    "\n[dim]Started here: " + "   ·   ".join(bits) + ". A name that is"
                    " absent did not run since OpenCode last started, not never —"
                    " rows begin where the writer begins (M24).[/dim]"
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
                + ("" if rows else "\n" + self._empty_note(
                    "skills", total=len(self.app.all_rows), filtered=bool(needle),
                    where=db.SKILLS_DIR))
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
            rows = db.unified_recent_rows(self.app.conn, self.app.recent_rows)
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
                + ("" if rows else "\n" + self._empty_note("events"))
            )
            self._paint_status("tab-recent")

        def render_mcp(self, rows=None) -> None:
            if rows is None:
                rows = db.mcp_stats_rows(self.app.conn)
            unfiltered = len(rows)
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
                + ("" if rows else "\n" + self._empty_note(
                    "MCP tools", total=unfiltered, filtered=bool(needle)))
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

        def _claude_mem_activity_line(self) -> str:
            """What claude-mem's own files say, in one line.

            Its injection runs in hooks, so no call of ours records it — whether or
            not the same plugin also registers tools, which is a surface this page
            already counts separately.

            Files only, never HTTP: this page repaints on every tab change and on
            any keypress once it is `REFRESH_STALE_AFTER_S` stale, and one network
            wait there freezes the interface — the same freeze the no-timers rule
            guards against, arrived at by a different mechanism. Liveness is
            `os.kill(pid, 0)`, a syscall, not a request.
            """
            store = cm.claude_mem_store()
            activity = cm.claude_mem_activity(store=store)
            if not activity["available"]:
                return ""
            trace = activity.get("trace") or {}
            log = activity.get("worker_log") or {}
            bits = []
            if trace:
                bits.append(f"injected {trace['injected']:,}")
                if trace.get("loaded"):
                    bits.append(f"loaded {trace['loaded']:,}")
                if trace.get("unrecognized"):
                    bits.append(f"{trace['unrecognized']} trace line(s) of an unknown shape")
            if log:
                bits.append(f"worker ERROR {(log.get('levels') or {}).get('ERROR', 0):,}")
            worker = cm.claude_mem_worker(store=store)
            if worker["available"]:
                bits.append(f"worker up :{worker['port']}" if worker["alive"]
                            else "worker down")
            if trace.get("truncated") or log.get("truncated"):
                bits.append("counts are floors: the byte cap cut a file short")
            return ("[dim]claude-mem, from its own log files — its injection hooks"
                    " make no call for the tables below to count: "
                    + " · ".join(bits) + "[/dim]")

        def render_plugins(self, rows=None) -> None:
            if rows is None:
                # The union of what the scan registered and what was called: a
                # tool nobody has used still has to appear, or an empty-looking
                # page reads as a broken recorder.
                rows = db.plugin_surface_rows(self.app.conn)
            try:
                needle = self.query_one("#plugins-search", Input).value.strip().lower()
            except Exception:  # noqa: BLE001 - before mount in tests
                needle = ""
            plugin_unfiltered = len(rows)
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
                # No markup of its own: the whole status block is dimmed, and a
                # `[dim]` age tail inside a `[dim]` line is what made this one
                # block read as two kinds of information.
                entry += f" {db.fmt_time(r['last_seen'])[5:]}"
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
                lines.append("[dim][b]Inventory[/b]  " + "   ·   ".join(current) + "[/dim]")
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
            never = sum(1 for r in rows if not r["ever_called"])
            if never:
                lines.append(
                    f"[dim]{never} registered surface item(s) never called yet"
                    " — a 0 here means the tool was not used, not that it was"
                    " missed[/dim]"
                )
            activity_line = self._claude_mem_activity_line()
            if activity_line:
                lines.append(activity_line)
            if not rows:
                lines.append(self._empty_note(
                    "plugin surfaces", total=plugin_unfiltered, filtered=bool(needle)))
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

        def __init__(self, db_path: str, no_sync: bool = False,
                     recent_rows: int | None = None):
            super().__init__()
            # Textual slides the tab-bar underline for 0.3 s on every switch
            # (`Tabs._highlight_active` animates at level "basic", so the global
            # "basic" setting does not remove it — only "none" does). Measured on
            # a 44-skill database: a Dashboard->Skills switch is ~500 ms with the
            # animation and ~190 ms without; it is ~305 ms of that. The screen
            # exists to read numbers, so it snaps instead of sliding.
            self.animation_level = "none"
            self.db_path = db_path
            self.no_sync = no_sync
            # `view.recent_rows`, resolved by the caller. Defaulted here rather than
            # read from the environment inside the app so a test can build an app
            # with a known length and `tui_main` stays the only place settings are
            # looked up.
            self.recent_rows = (cfg.spec("view.recent_rows")["default"]
                                 if recent_rows is None else recent_rows)
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

    app = SkillTUI(db_path=args.db, no_sync=args.no_sync, recent_rows=args.recent_rows)
    app.run()
    return 0


# ===========================================================================
# Argument parsing
# ===========================================================================
class Args:
    pass


CLI_COMMANDS = {
    "insight", "export", "sync", "cleanup-selftest", "scrub-metadata", "health",
    "mcp", "plugins", "agentos", "claude-mem", "auto-backup", "doctor", "config",
    "prune-usage", "rotate-log",
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
    # Every numeric default is the resolved setting, so precedence holds no matter
    # which entry point ran: flag > config file > env > default. A flag below
    # overwrites what `cfg.values()` worked out; nothing here re-reads the file.
    settings = cfg.values()
    a.days = settings["view.days"]
    a.min_uses = settings["view.min_uses"]
    a.limit = settings["view.limit"]
    a.recent_rows = settings["view.recent_rows"]
    a.out = None
    a.pretty = False
    a.force = False
    a.skills_only = False
    a.dry_run = False
    a.prune_orphans = False
    a.freshness_days = settings["doctor.freshness_days"]
    # The two delete knobs resolve through the same chain as everything else, so
    # `skillt config set retention.usage_days 365` arms retention without a flag and
    # a flag overrides it for one run. Both default to 0, which means off.
    a.keep_days = settings["retention.usage_days"]
    a.keep_versions = settings["retention.max_skill_versions"]
    a.log_max_bytes = settings["log.max_bytes"]
    a.log_keep_files = settings["log.keep_files"]
    a.yes = False
    a.no_sync = False
    a.positional = []

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
        elif t in ("--db", "--days", "--min-uses", "--limit", "--out",
                   "--freshness-days", "--recent-rows", "--keep-days",
                   "--keep-versions", "--max-bytes", "--keep-files"):
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
            elif t == "--recent-rows":
                a.recent_rows = _int_arg("--recent-rows", val, minimum=1)
            elif t == "--freshness-days":
                a.freshness_days = _int_arg("--freshness-days", val, minimum=1)
            elif t == "--keep-days":
                # 0 is a value here ("off"), so the floor is 0 and not 1.
                a.keep_days = _int_arg("--keep-days", val, minimum=0)
            elif t == "--keep-versions":
                a.keep_versions = _int_arg("--keep-versions", val, minimum=0)
            elif t == "--max-bytes":
                a.log_max_bytes = _int_arg("--max-bytes", val, minimum=1024)
            elif t == "--keep-files":
                # 0 would delete the only copy of the lines that just rotated out.
                a.log_keep_files = _int_arg("--keep-files", val, minimum=1)
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
        a.positional = list(rest[1:])
        if a.command not in CLI_COMMANDS:
            raise SystemExit(f"unknown command: {a.command} (expected one of {sorted(CLI_COMMANDS)})")
        if a.positional and a.command != "config":
            # Only `config` reads words after the command name. Accepting them
            # anywhere else turned a typo (`skillt insight --dsys 5`) into a run
            # with the wrong assumptions.
            raise SystemExit(
                f"{a.command} takes no arguments, got: {' '.join(a.positional)}")
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
                "plugins|agentos|claude-mem|auto-backup|doctor|config|prune-usage|"
                "rotate-log"
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
