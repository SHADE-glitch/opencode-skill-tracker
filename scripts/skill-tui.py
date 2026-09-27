#!/usr/bin/env python3
"""
skill-tui.py — interactive TUI for the OpenCode skill tracker (`skillt`).

Two modes in one file:
  * TUI   (default)      requires `textual` (installed in the skillt venv)
  * --cli <subcommand>   stdlib only; works on the system python3
                         subcommands: insight | export | sync | cleanup-selftest
                                      | health | auto-backup | doctor

`textual` is imported lazily so the --cli path never depends on the venv.

Data access is delegated to `skill_db.py` (shared with the legacy CLI).
The plugin `plugin/skill-tracker.js` and the legacy `scripts/skill-stats.py`
are intentionally left untouched.
"""

from __future__ import annotations

import json
import os
import sqlite3
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
def sort_rows(rows, mode):
    if mode == "count":
        return sorted(rows, key=lambda r: (-r["total"], r["skill_name"]))
    if mode == "last_used":
        return sorted(rows, key=lambda r: (r["last_used"] is None, _neg_str(r["last_used"]), r["skill_name"]))
    if mode == "success_rate":
        def rate(r):
            sr = db.success_rate(r["total"], r["success"])
            # The trailing name keeps ties deterministic (skills with no data
            # all share `sr is None`).
            return (sr is None, -(sr or 0.0), -r["total"], r["skill_name"])
        return sorted(rows, key=rate)
    return sorted(rows, key=lambda r: r["skill_name"])


def _neg_str(s):
    # Descending sort on an ISO timestamp string without reversing the list.
    return "".join(chr(255 - ord(c)) if ord(c) < 255 else c for c in (s or ""))


def bar(value, peak, width=24):
    if peak <= 0:
        return ""
    n = max(1, round(value / peak * width)) if value > 0 else 0
    return "█" * n


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


def _cli_health(conn, args) -> int:
    report = db.health_report(conn)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    c = report["counts"]
    w = report["windows"]
    print("Skill health (read-only; nothing is deleted)")
    print("-" * 56)
    print(f"  Active   (<= {w['active_days']} days)              {c['active']}")
    print(f"  Stale    ({w['active_days']}-{w['stale_days']} days)             {c['stale']}")
    print(f"  Unused   (>{w['stale_days']} days or never used)   {c['unused']}")
    print(f"  Total                          {c['total']}")
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
    pexists = os.path.isfile(PLUGIN_PATH)
    add("plugin.exists", pexists, PLUGIN_PATH if pexists else f"missing: {PLUGIN_PATH}")
    if pexists:
        try:
            with open(PLUGIN_PATH, encoding="utf-8", errors="replace") as f:
                src = f.read()
            miss = [m for m in PLUGIN_MARKERS if m not in src]
            add("plugin.hooks", not miss,
                "all hooks present" if not miss else f"missing: {', '.join(miss)}", warn=True)
        except OSError as e:
            add("plugin.hooks", False, f"read failed: {e}")

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

    # sync / cleanup-selftest / doctor need write access (migration); health is
    # a pure read and opens read-only so it can never alter the DB.
    try:
        conn = db.open_db(args.db, readonly=(args.command == "health"))
    except sqlite3.Error as e:
        print(f"skillt: cannot open database {args.db}: {e}", file=sys.stderr)
        return 1
    try:
        if args.command in ("sync", "cleanup-selftest", "doctor"):
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

    # ---- skill detail screen ----------------------------------------------
    class SkillDetailScreen(Screen):
        BINDINGS = [Binding("escape,q", "app.pop_screen", "Back")]

        def __init__(self, skill: str, detail: dict):
            super().__init__()
            self.skill = skill
            self.detail = detail

        def compose(self) -> ComposeResult:
            d = self.detail
            yield Header(show_clock=True)
            with VerticalScroll(id="detail-body"):
                yield Static(f"[b]{d['name']}[/b]", id="detail-title")
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
            yield Footer()

        def on_mount(self) -> None:
            table = self.query_one("#detail-history", DataTable)
            table.add_columns("Time", "Project", "session", "Status", "Duration", "Model", "agent", "Branch", "Summary")
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
                    (r["summary"] or "")[:60],
                )
            if self.detail.get("versions"):
                vt = self.query_one("#detail-versions", DataTable)
                vt.add_columns("Recorded", "hash", "Bytes")
                for v in self.detail["versions"]:
                    vt.add_row(db.fmt_time(v["recorded_at"]), (v["content_hash"] or "")[:16], str(v["size_bytes"] or "-"))

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

    # ---- main screen -------------------------------------------------------
    class MainScreen(Screen):
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
                    with Horizontal(id="cards"):
                        yield Static(id="card-skills", classes="card")
                        yield Static(id="card-usage", classes="card")
                        yield Static(id="card-today", classes="card")
                        yield Static(id="card-personal", classes="card")
                        yield Static(id="card-oss", classes="card")
                    yield Static(id="cards-legend")
                    yield Static(id="trend")
                    yield Label("Top Skills", classes="section")
                    t = DataTable(id="dash-top", zebra_stripes=True)
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
                with TabPane("Recent", id="tab-recent"):
                    t = DataTable(id="recent-table", zebra_stripes=True)
                    t.cursor_type = "row"
                    yield t
                with TabPane("Categories", id="tab-cats"):
                    t = DataTable(id="cats-table", zebra_stripes=True)
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
            self.app.conn = db.open_db(self.app.db_path, readonly=False)
            try:
                db.ensure_schema(self.app.conn)
            except Exception as e:  # noqa: BLE001
                self.app.notify(f"Migration failed: {e}", severity="error")
            if not self.app.no_sync:
                try:
                    db.sync_versions(self.app.conn)
                except Exception as e:  # noqa: BLE001
                    self.app.notify(f"sync failed: {e}", severity="warning")
            self.refresh_data()

        # -- rendering ------------------------------------------------------
        def refresh_data(self) -> None:
            conn = self.app.conn
            try:
                s = db.dashboard_summary(conn)
                self.query_one("#card-skills", Static).update(
                    f"[b]{s['total_skills']}[/b]\n[dim]Skills[/dim]"
                )
                self.query_one("#card-usage", Static).update(
                    f"[b]{s['total_usage']}[/b]\n[dim]Uses[/dim]"
                )
                self.query_one("#card-today", Static).update(
                    f"[b]{s['today_usage']}[/b]\n[dim]Today[/dim]"
                )
                self.query_one("#card-personal", Static).update(
                    f"[b]{s['personal']}[/b]\n[dim]Personal[/dim]"
                )
                self.query_one("#card-oss", Static).update(
                    f"[b]{s['open_source']}[/b]\n[dim]OSS[/dim]"
                )
                self.query_one("#cards-legend", Static).update(
                    "[dim]Skills / Personal / OSS = skill counts  ·  "
                    "Uses / Today = invocation counts[/dim]"
                )

                days = db.daily_activity(conn, 7)
                peak = max((d["count"] for d in days), default=0)
                lines = ["[b]Last 7 days[/b]"]
                for d in days:
                    lines.append(f"  {d['date'][5:]}  {bar(d['count'], peak):<24} {d['count']}")
                self.query_one("#trend", Static).update("\n".join(lines))

                top = self.query_one("#dash-top", DataTable)
                top.clear(columns=True)
                top.add_columns("#", "Skill", "Uses", "Success rate", "Last used")
                for i, r in enumerate(db.top_rows(conn, 10), 1):
                    sr = db.success_rate(r["total"], r["success"])
                    top.add_row(
                        str(i), r["skill_name"], str(r["total"]),
                        f"{round(sr*100)}%" if sr is not None else "-",
                        db.fmt_time(r["last_used"]),
                    )

                self.app.all_rows = db.stats_rows(conn)
                # With no usage rows every sort mode collapses to the same
                # order; the Skills tab says so instead of looking broken.
                self.app.has_usage = any(r["total"] for r in self.app.all_rows)
                self.render_skills()

                rec = self.query_one("#recent-table", DataTable)
                rec.clear(columns=True)
                rec.add_columns("Time", "Skill", "Project", "session", "Status", "Duration")
                for r in db.recent_rows(conn, 100):
                    rec.add_row(
                        db.fmt_time(r["timestamp"]), r["skill_name"],
                        db.short_path(r["project_path"], 30), db.short_session(r["session_id"]),
                        r["status"], f"{r['duration_ms']}ms" if r["duration_ms"] is not None else "-",
                    )

                cats = self.query_one("#cats-table", DataTable)
                cats.clear(columns=True)
                cats.add_columns("Source", "category", "skills", "uses", "success", "errors", "denied")
                for r in db.categories(conn):
                    cats.add_row(
                        r["source"], r["category"] or "-", str(r["skills"]), str(r["usage"]),
                        str(r["success"]), str(r["errors"]), str(r["denied"]),
                    )
            except Exception as e:  # noqa: BLE001
                self.app.notify(f"Refresh failed: {e}", severity="error")

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
            self.query_one("#sort-label", Static).update(
                f"[dim]Sort: {SORT_LABELS[self.app.sort_mode]}   ·   "
                f"showing {len(rows)} / {len(self.app.all_rows)}{hint}[/dim]"
            )
            # Columns are static, so a row-level clear is enough (and keeps the
            # column set stable across refreshes).
            if not table.columns:
                table.add_columns("Name", "Source", "Uses", "Last Used", "Success Rate")
            table.clear()
            self.app.path_to_name = {}
            for r in rows:
                sr = db.success_rate(r["total"], r["success"])
                # Rows are keyed by `path` (the only UNIQUE column). Two SKILL.md
                # files sharing a frontmatter name would otherwise raise
                # DuplicateKey and break the whole table.
                self.app.path_to_name[r["path"]] = r["skill_name"]
                table.add_row(
                    r["skill_name"], r["source"], str(r["total"]),
                    db.fmt_time(r["last_used"]),
                    f"{round(sr*100)}%" if sr is not None else "-",
                    key=r["path"],
                )
            if selected is not None:
                try:
                    table.move_cursor(row=table.get_row_index(selected))
                except Exception:  # noqa: BLE001 - filtered out or table empty
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
            inp = self.query_one("#search", Input)
            if inp.value:
                inp.value = ""
                self.render_skills()
            inp.blur()

        def on_input_changed(self, event: Input.Changed) -> None:
            if event.input.id == "search":
                self.render_skills()

        def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
            # Only the Skills table is keyed by path; other tables use positional
            # keys, so ignore their selection events here.
            if event.data_table.id != "skills-table":
                return
            path = event.row_key.value
            name = self.app.path_to_name.get(path)
            if not name:
                return
            try:
                detail = db.skill_detail(self.app.conn, name)
            except Exception as e:  # noqa: BLE001
                self.app.notify(f"Could not read detail: {e}", severity="error")
                return
            if detail:
                self.app.push_screen(SkillDetailScreen(name, detail))

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
                self._confirm("Clear ALL skill_usage records? The skills table is kept.", self._do_clear)

        # -- actions --------------------------------------------------------
        def action_refresh_data(self) -> None:
            self.refresh_data()
            self.app.notify("Refreshed")

        def action_focus_search(self) -> None:
            self.query_one(TabbedContent).active = "tab-skills"
            self.query_one("#search", Input).focus()

        def action_cycle_sort(self) -> None:
            i = SORT_MODES.index(self.app.sort_mode)
            self.app.sort_mode = SORT_MODES[(i + 1) % len(SORT_MODES)]
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
                self._result(f"Backup failed: {e}")
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
                self._result(f"Export failed: {e}")
                self.app.notify(f"Export failed: {e}", severity="error")

        def _do_clear(self) -> None:
            try:
                res = db.clear_usage(self.app.conn, also_skills=False)
                self.refresh_data()
                self._result(f"Cleared {res['usage_deleted']} usage record(s)")
                self.app.notify(f"Cleared {res['usage_deleted']} record(s)")
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

        def _result(self, msg: str) -> None:
            try:
                self.query_one("#data-result", Static).update(msg)
            except Exception:  # noqa: BLE001
                pass

    # ---- app ---------------------------------------------------------------
    class SkillTUI(App):
        CSS = """
        Screen { layout: vertical; }
        #cards { height: 5; }
        .card {
            width: 1fr; height: 5; margin: 0 1; padding: 0 1;
            border: round $primary; content-align: center middle; text-align: center;
        }
        #cards-legend { height: 1; padding: 0 2; }
        #trend { height: auto; padding: 1 2; }
        .section { padding: 1 2 0 2; }
        #sort-label { padding: 0 2; height: 1; }
        #search { margin: 1 2; }
        DataTable { height: 1fr; margin: 0 1; }
        #data-help { padding: 1 2; }
        #data-buttons { height: 3; padding: 0 2; }
        #data-result { padding: 1 2; color: $success; }
        #detail-body { padding: 1 2; }
        #detail-title { padding: 1 0; }
        #detail-meta, #detail-stats { padding: 1 0; }
        #detail-hist-label, #detail-ver-label { padding: 1 0 0 0; }
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
            self.path_to_name: dict = {}
            self.has_usage = True
            self.sort_mode = "count"

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
            "SkillDetailScreen": SkillDetailScreen,
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
            "Or use the headless CLI:  skillt insight | export | sync",
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
    "insight", "export", "sync", "cleanup-selftest", "health", "auto-backup", "doctor",
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
        elif t in ("--db", "--days", "--min-uses", "--limit", "--out"):
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
                "insight|export|sync|cleanup-selftest|health|auto-backup|doctor"
            )
        return cli_main(args)

    # Textual's Linux driver reads stdin and writes stderr, so all three streams
    # must be a terminal. Checking only stdout let `skillt < /dev/null` through,
    # which then spun on EOF and left the terminal in a broken state.
    if not (sys.stdin.isatty() and sys.stdout.isatty() and sys.stderr.isatty()):
        print(
            "skillt: not an interactive terminal (stdin/stdout/stderr must all be TTYs); "
            "cannot start the TUI.\n"
            "Try instead: skillt insight | skillt export | skillt sync | skillt health\n"
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
