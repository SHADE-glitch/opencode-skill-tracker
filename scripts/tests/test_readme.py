"""The project README must document every required section and limitation."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]      # repo root
README = ROOT / "README.zh-CN.md"
SYSTEMD = ROOT / "skill-tracker" / "systemd"

# Every documented limitation, in both languages. M14-M18 came out of the
# full capture/UI audit; M19 out of the privacy cleanup that followed it; M22-M23
# out of the claude-mem neighbour and the Agents page.
LIMITATIONS = tuple(f"M{i}" for i in range(1, 25))


def _text():
    assert README.is_file(), f"missing README at {README}"
    return README.read_text(encoding="utf-8")


def test_readme_has_all_sections():
    text = _text()
    # The zh-CN README now mirrors README.md's 13 top-level sections in the same
    # order; these names moved with that restructure. The intent is unchanged:
    # the Chinese documentation must carry the sections it is expected to.
    for section in (
        "为什么", "环境要求", "安装", "使用", "设置", "工作原理",
        "备份", "故障排查", "已知限制", "仓库结构", "卸载", "参与贡献", "许可证",
    ):
        assert section in text, f"README is missing the '{section}' section"


def test_readme_section_counts_match():
    """README.md and README.zh-CN.md are one document in two languages.

    The Chinese file is the full reference and legitimately carries more `###`
    subsections, but the two must agree on the number of top-level `##` sections —
    a section added to one side alone is drift no other check here would catch.
    """
    en = (ROOT / "README.md").read_text(encoding="utf-8")
    zh = _text()
    h2 = lambda text: len(re.findall(r"^## ", text, flags=re.M))
    assert h2(en) == h2(zh), (
        f"section count drift: README.md has {h2(en)} '##' sections, "
        f"README.zh-CN.md has {h2(zh)} — add the missing section to the other side")


def test_readme_documents_every_known_limitation():
    text = _text()
    for marker in LIMITATIONS:
        assert marker in text, f"README.zh-CN does not mention {marker}"
    for keyword in (
        "UTC",                          # M1
        "读写方式打开",                  # M2
        "隐式提交",                      # M3
        "半成品",                        # M4
        "UI 线程",                       # M5
        "UnicodeEncodeError",           # M6
        "git 子进程",                    # M7
        "容量上限",                      # M8
        "initDone",                     # M9
        "退出码",                        # M10
        "重启 OpenCode",                 # M11/M12
        "静态扫描",                      # M12
        "allowlist",                      # M13
        # M22: two promises that are easy to lose while summarising the command.
        "只数、不读",                    # `q=` is the user's own prompt text
        "共用一个总截止时间",            # three endpoints, one budget
        "采集顺序",                        # M23: `(unknown)` is not anonymous work
        # M24: the subagent stream's two promises, easy to lose in a summary.
        "subagent_usage", "Spawned", "散文探测器",
    ):
        assert keyword in text, f"known-limitation detail missing: {keyword}"


def test_english_readme_documents_every_known_limitation():
    """The landing page must not fall behind the reference doc.

    Regression: README.md listed "M1 through M11" while README.zh-CN.md had
    M1-M13, and nothing compared them, so an English reader was told a shorter
    story about the same database.
    """
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    for marker in LIMITATIONS:
        assert marker in text, f"README.md does not mention {marker}"
    for keyword in (
        "UTC",                 # M1
        "half-written",        # M4
        "UI thread",           # M5
        "UnicodeEncodeError",  # M6
        "static scan",         # M12
        "allowlist",           # M13
        "prompt",              # M14
        "no row at all",       # M15
        "COALESCE",            # M18
        "The default now writes into",   # M19, fixed — must say where it goes
        "counted, never read",   # M22: `q=` holds the user's prompt text
        "one shared deadline",   # M22: three endpoints, not three timeouts
        "capture order",         # M23: `(unknown)` is not anonymous work
        # M24: the gate is a shape bound, and saying "prose detector" is the lie
        # a future edit would tell. Both READMEs must also name the two things
        # the owner looks for on screen.
        "not a prose detector",
        "subagent_usage",
        "Spawned",
    ):
        assert keyword in text, f"English limitation detail missing: {keyword}"
    zh = _text()
    for keyword in ("散文探测器", "subagent_usage", "Spawned"):
        assert keyword in zh, f"Chinese limitation detail missing: {keyword}"


# --- maintenance checklists -----------------------------------------------
MAINTENANCE = (ROOT / "MAINTENANCE.md", ROOT / "MAINTENANCE.zh-CN.md")


def test_maintenance_checklists_exist_and_cover_the_same_ground():
    """Two languages, one checklist — and it must name the checks it depends on."""
    for path in MAINTENANCE:
        assert path.is_file(), f"missing {path.name}"

    en = MAINTENANCE[0].read_text(encoding="utf-8")
    zh = MAINTENANCE[1].read_text(encoding="utf-8")

    # Everything a reader needs to notice capture has decayed.
    for needle in (
        "skillt doctor", "skillt cleanup-selftest", "skillt sync --dry-run",
        "skillt scrub-metadata", "skillt claude-mem",
        # The two commands that *change* data, named in both checklists: a checklist
        # that describes only the read-only half teaches nobody what to run when the
        # database or the log outgrows the machine.
        "skillt rotate-log", "skillt prune-usage",
        "capture.freshness", "log.errors",
        "env.opencode_version", "backups.latest", "integrity_check",
        "skillt-auto-backup.timer", "pytest scripts/tests", "bash -n bin/skillt",
        # MAINTENANCE §11 — how a green is produced. Named here so a translation
        # that drops the rule drops the build.
        "pipestatus", "command cp -f",
    ):
        assert needle in en, f"MAINTENANCE.md missing: {needle}"
        assert needle in zh, f"MAINTENANCE.zh-CN.md missing: {needle}"

    # The load-bearing invariants, by the name a future editor will grep for.
    for needle in ("UNIQUE(session_id, call_id)", "SCHEMA_VERSION", "export default",
                   "Object.keys()", "TABLES_SQL",
                   # The two free-text keys, named by the guard that holds each:
                   # a one-language edit of the invariant table fails here.
                   "test_the_session_handler_keeps_only_the_fields_it_uses",
                   "test_a_denied_call_stores_no_title_but_still_stores_the_denial"):
        assert needle in en, f"MAINTENANCE.md dropped an invariant: {needle}"
        assert needle in zh, f"MAINTENANCE.zh-CN.md dropped an invariant: {needle}"

    # Deviations must be written down where the next reader will look, not in
    # a commit message. The claude-mem ones are the three things a future editor
    # is most likely to "simplify" back into a wrong answer: read the tail, count
    # categories instead of levels, and put the HTTP probe on a repaint path.
    for needle in ("set_interval", "active_app", "animation_level", "row_targets", "schema_version",
                   "CLAUDE_MEM_LOG_BYTES_CAP", "truncated", "unparsed",
                   "_LOG_LINE_RE", "claude_mem_worker",
                   # The subagent round: three names a future editor would have to
                   # delete on purpose to make the docs disagree with the code.
                   "subagent_usage", "SUBAGENT_LABEL_RE", "Spawned",
                   "plugin_surface_rows", "ever_called",
                   # The selftest log isolation, named the same in both languages.
                   "logPath()", "OPENCODE_SKILL_TRACKER_LOG",
                   "skill-tracker-selftest.log",
                   # The two subagent columns and the injection grouping: an
                   # English-only or Chinese-only edit fails here, not in review.
                   "Ran as", "Spawned", "by_project", "by_day", "projectless",
                   "older_days", "undated", "opencode.db",
                   # The upstream-contract module: both checklists must point at it,
                   # or the one place to edit on an upgrade is undocumented.
                   "opencode_compat", "CONTRACT",
                   # The neighbour modules split out of skill_db: if a checklist
                   # still says `skill_db.agentos_*`, the door it names is gone.
                   "skill_db_agentos",
                   # The S8 UI round: four derived surfaces, each of which was
                   # hand-written and measurably wrong before. A checklist that
                   # stops naming them has lost the reason they are computed.
                   "key_help", "CARD_LABELS", "_empty_note", "_offscreen_note",
                   "call_after_refresh", "$success", "$error"):
        assert needle in en and needle in zh, f"deviation not documented in both: {needle}"

    assert "M14" in en and "M16" in en, "MAINTENANCE.md must point at the limitations"
    assert "M14" in zh and "M16" in zh, "MAINTENANCE.zh-CN.md must point at the limitations"


def test_readme_documents_selftest_isolation_and_duplicates():
    text = _text()
    assert "OPENCODE_SKILL_TRACKER_DB" in text
    assert "Selftest requires isolated database" in text
    assert "同名" in text
    assert "没有唯一约束" in text or "没有**唯一约束" in text


def test_readme_documents_commands_and_backup_ops():
    text = _text()
    for cmd in (
        "skillt health", "skillt auto-backup", "skillt doctor", "skillt sync",
        "skillt insight", "skillt export", "skillt cleanup-selftest",
        "skillt scrub-metadata", "skillt agentos", "skillt claude-mem",
        # The two that arrived with the settings layer: `config` is the only way to
        # edit the file, and `prune-usage` is the only command that deletes history,
        # so both have to be findable in the language the reader reads.
        "skillt prune-usage", "skillt config",
    ):
        assert cmd in text, f"command not documented: {cmd}"
    # the new headless surface must be documented where readers look for it
    en = (ROOT / "README.md").read_text(encoding="utf-8")
    for cmd in ("skillt scrub-metadata", "skillt doctor", "--freshness-days",
                "skillt agentos", "OPENCODE_SKILL_TRACKER_AOS_TIMEOUT_MS",
                "skillt claude-mem", "inject-trace.log", "worker.pid"):
        assert cmd in en, f"README.md does not document: {cmd}"
    assert "enable-linger" in text
    assert "120 秒" in text or "120" in text
    assert "最近 30 个每日" in text
    assert "最近 12 个月每月" in text


def test_readme_documents_ctrl_shortcuts_and_dashboard_cards():
    text = _text()
    # ctrl+s / ctrl+r are the always-available alternates (search box steals letters)
    assert "ctrl+s" in text
    assert "ctrl+r" in text
    # the six Dashboard cards must be named
    for card in ("Skills", "Skill calls", "MCP calls", "Plugin calls", "Today (all)", "Skill success"):
        assert card in text, f"Dashboard card not documented: {card}"
    # the Data-page delete entry is gone; deletion lives on the Skills tab
    assert "删除选中 skill" not in text
    assert "删除 skill 的入口**只**在 Skills 页" in text


def test_readmes_list_every_tab_page():
    """A reader must find exactly the tabs that exist — no more, no fewer.

    The Advisor tab was removed on 2026-10-03 and both page lists still advertised
    it; that is the drift this catches from the documentation side. The widget-tree
    half of the pair is `test_tui_tab_order_is_pinned`, which compares the real
    `TabPane` ids.

    The list is compared *item by item, in order*, against what the app composes —
    so a page that exists but is undocumented fails, and so does a page the docs
    invented. Reading the ids out of the source instead of launching Textual keeps
    this runnable on an interpreter with no textual installed.
    """
    src = (ROOT / "scripts" / "skill-tui.py").read_text(encoding="utf-8")
    panes = re.findall(r'with TabPane\("([^"]+)", id="(tab-[^"]+)"\)', src)
    assert panes, "no TabPane found — the compose() shape changed, fix this test"
    labels = [label for label, _ in panes]
    assert [tid for _, tid in panes][-1] == "tab-data", panes

    en = (ROOT / "README.md").read_text(encoding="utf-8")
    zh = (ROOT / "README.zh-CN.md").read_text(encoding="utf-8")

    def listed(text, marker):
        segment = text[text.index(marker) + len(marker):]
        for bracket in ("(", "（"):                 # the sentence ends at its own note
            at = segment.find(bracket)
            if at != -1:
                segment = segment[:at]
        return [item.strip().strip("*").strip() for item in segment.split("/")]

    for label, items in (("README.md", listed(en, "Pages: ")),
                         ("README.zh-CN.md", listed(zh, "页面："))):
        assert items == labels, f"{label} page list {items} != the app's {labels}"
        assert "Advisor" not in items, f"{label} advertises a tab that is gone"


def test_readmes_say_the_tracker_cannot_record_the_advisor():
    """`skillt agentos` invites one wrong belief: that these numbers were measured.

    The Advisor *page* is gone but the command is not, and the misunderstanding is
    the same one the tab used to answer on screen. Each language must say, in its
    own words, that the advisor registers no tool and no command, so no usage row
    can ever name it — and that every number was written by the advisor into its
    own store. A translation that drops the sentence drops the build.
    """
    en = (ROOT / "README.md").read_text(encoding="utf-8")
    zh = (ROOT / "README.zh-CN.md").read_text(encoding="utf-8")

    for phrase in ("no tool and no command", "zero usage rows", "its own store"):
        assert phrase in en, f"README.md does not say: {phrase}"
    for phrase in ("不注册任何工具", "不注册任何命令", "0 行", "它自己的库"):
        assert phrase in zh, f"README.zh-CN.md does not say: {phrase}"


def test_readme_documents_top_limit_alias():
    text = _text()
    assert "--limit 3" in text or "`--limit" in text


# --- S8 doc gates -----------------------------------------------------------
def test_the_documented_limitation_range_is_the_range_the_gate_pins():
    """Every `M1–M23` is a claim about how many limitations there are.

    Four documents make that claim in two languages and none of them is the list
    itself, so each one quietly froze at whatever the newest id was when it was
    written. Reading the number out of `LIMITATIONS` — the tuple that decides which
    ids both READMEs must carry — turns all four into a copy of one fact.
    """
    first, last = LIMITATIONS[0], LIMITATIONS[-1]
    pattern = re.compile(first + r"(?:–|-| through )(M\d+)")
    seen = 0
    for name in ("README.md", "README.zh-CN.md", "MAINTENANCE.md",
                 "MAINTENANCE.zh-CN.md"):
        text = (ROOT / name).read_text(encoding="utf-8")
        for match in pattern.finditer(text):
            seen += 1
            assert match.group(1) == last, (
                f"{name} says {match.group(0)!r} but the gate pins {first}–{last}")
    assert seen >= 3, f"only {seen} range claims found — the wording changed"


def test_readmes_document_every_key_the_screen_binds():
    """The in-screen help is generated; the README tables are the other copy of the promise.

    A key that reaches neither is a key nobody finds: the footer hides eight of the
    thirteen bindings, and the pre-S8 help named three of them. Read out of the
    source, so this runs on an interpreter with no textual, like the tab-page gate.
    """
    src = (ROOT / "scripts" / "skill-tui.py").read_text(encoding="utf-8")
    screen = src.split("class MainScreen(Screen):", 1)[1]
    bindings = screen.split("class SkillTUI(App):", 1)[0]
    keys = re.findall(r'Binding\("([^"]+)"', bindings)
    assert len(keys) >= 13, f"the binding list shrank to {keys}"
    display = dict(re.findall(r'"([^"]+)": "([^"]+)"',
                              re.search(r"_KEY_DISPLAY = \{([^}]*)\}", src).group(1)))
    shown = sorted({display.get(k, k) for k in keys})

    for name, marker in (("README.md", "| Key"), ("README.zh-CN.md", "| 按键")):
        text = (ROOT / name).read_text(encoding="utf-8")
        rows = []
        for line in text[text.index(marker):].splitlines():
            if not line.strip().startswith("|"):
                break
            rows.append(line)
        table = " ".join(rows).lower()
        for key in shown:
            assert f"`{key.lower()}`" in table, (
                f"{name}'s key table does not document `{key}` "
                f"(bound in MainScreen.BINDINGS)")


def test_readme_does_not_claim_message_text_is_stored():
    """`metadata.summary` was removed; the docs must not advertise storing it."""
    for name in ("README.md", "README.zh-CN.md"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert "branch / summary" not in text, f"{name} still lists summary in metadata"
        assert "message body" in text or "消息正文" in text, (
            f"{name} should state that message text is never stored"
        )


def test_docs_cross_link_so_no_file_is_an_orphan():
    """A checklist nobody links to is a checklist nobody reads.

    Regression: MAINTENANCE.md / .zh-CN.md were added with the English README
    never mentioning them, and the Chinese one only citing them in passing.
    """
    for name in ("README.md", "README.zh-CN.md"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert "MAINTENANCE.md" in text, f"{name} never points at the checklist"
    for path in MAINTENANCE:
        text = path.read_text(encoding="utf-8")
        assert "README.md" in text and "README.zh-CN.md" in text, (
            f"{path.name} does not point back at the documentation"
        )


# The three files that answer "what does this depend on, and how do I re-check it".
# They have no Chinese twin — the parity gates above cover README ×2 and
# MAINTENANCE ×2 — so instead of a translation they owe two things: a link from
# both checklists, and a command beside every reading they state.
MAINT_DOCS = ROOT / "docs" / "maintenance"
TRIO = ("compat-matrix.md", "opencode-interface.md", "measurements.md")


def test_the_maintenance_docs_are_linked_from_both_checklists():
    """A reference nobody links to is a reference nobody finds at the right moment.

    The failure this prevents is the ordinary one: the trio exists, gets written,
    and then a maintainer edits MAINTENANCE.md for a month without ever being told
    there is a file that says which release each claim belongs to.
    """
    for name in TRIO:
        assert (MAINT_DOCS / name).is_file(), f"missing docs/maintenance/{name}"
    for path in MAINTENANCE:
        text = path.read_text(encoding="utf-8")
        for name in TRIO:
            assert name in text, f"{path.name} never points at docs/maintenance/{name}"
    # Back the other way, so none of the three is a leaf that strands a reader.
    for name in TRIO:
        text = (MAINT_DOCS / name).read_text(encoding="utf-8")
        assert "MAINTENANCE" in text, f"{name} never names the checklist to return to"


def test_every_dated_reading_in_measurements_sits_under_a_command():
    """The whole reason `measurements.md` exists: a number with no command is a claim.

    Scoped to a line that *starts* with a date, because prose legitimately says
    "run on 2026-10-09" while describing the file. A reading line is the one that
    reports output, and output must be re-takable from the same section.
    """
    text = (MAINT_DOCS / "measurements.md").read_text(encoding="utf-8")
    sections = re.split(r"\n(?=## )", text)
    silent = []
    for section in sections:
        heading = section.splitlines()[0]
        if not re.search(r"^20\d\d-\d\d-\d\d:", section, re.M):
            continue
        if "```bash" not in section:
            silent.append(heading)
    assert not silent, (
        f"sections state a dated reading with no command to re-take it: {silent}"
    )


def test_measurements_quotes_the_cap_the_code_actually_resolves():
    """`4 MiB` in prose is a claim about a default; the default is a number in code.

    The cheap version of this file's job is to restate the cap. The version worth a
    test is to restate *the resolved value*: if `log.max_bytes` moves and this
    document still says 4,194,304, every reading derived from it — the ~600-day
    figure, when `truncated=` flips — is described by a number that no longer exists.

    Nothing here hard-codes 4 MiB. If it did, the test would go red for the wrong
    reason when the cap moves *and the doc follows it*, which is the case the check
    is supposed to allow through.
    """
    import settings as cfg

    cap = cfg.spec("log.max_bytes")["default"]
    text = (MAINT_DOCS / "measurements.md").read_text(encoding="utf-8")
    assert f"{cap:,}" in text, (
        f"measurements.md must print the resolved log cap ({cap:,} B) where it "
        f"reports a truncation reading"
    )
    assert f"{cap // (1024 * 1024)} MiB" in text, (
        "and the human spelling of the same number, derived from the same value"
    )


def test_systemd_units_exist_and_use_absolute_execstart():
    service = SYSTEMD / "skillt-auto-backup.service"
    timer = SYSTEMD / "skillt-auto-backup.timer"
    assert service.is_file() and timer.is_file()

    svc = service.read_text(encoding="utf-8")
    line = next(l for l in svc.splitlines() if l.startswith("ExecStart="))
    target = line.split("=", 1)[1].split()[0]
    assert target.startswith(("/", "%h")), "ExecStart must be absolute or %h-relative"

    tmr = timer.read_text(encoding="utf-8")
    assert "OnCalendar=daily" in tmr
    assert "Persistent=true" in tmr
    assert "WantedBy=timers.target" in tmr


# --- source hygiene --------------------------------------------------------
SOURCE_DIRS = ("scripts", "plugin", "bin", "skill-tracker")
SOURCE_SUFFIXES = {".py", ".js", ".mjs", ".cjs", ".sh", ".service", ".timer"}
# A red-check edits the thing it pins and then puts it back. A marker that
# survives is not a comment, it is the guard's own off switch left installed: one
# did ride through 318 passes on 2026-10-01 (a `return` after a fixture's setenv
# — inert enough to stay green, and a lie about which guard was being provoked).
# Written as a class instead of literals because this file is itself scanned: a
# guard that trips on its own needle is a guard nobody can read.
CANARY_RE = re.compile(r"TEMP[-_]|CAN[0-9A-Z]ARY|FIXME-REMOV[E]")


def test_no_canary_or_scratch_marker_survives_in_tracked_source():
    """Docs may *name* these markers (MAINTENANCE §11 does); source may not hold one."""
    offenders = []
    for directory in SOURCE_DIRS:
        base = ROOT / directory
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file() or path.suffix not in SOURCE_SUFFIXES:
                continue
            if "__pycache__" in path.parts:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            hit = CANARY_RE.search(text)
            if hit:
                line = text[: hit.start()].count("\n") + 1
                offenders.append(f"{path.relative_to(ROOT)}:{line}: {hit.group()}")
    assert not offenders, f"red-check canaries left in source: {offenders}"


# --- the invariants table's own commands -----------------------------------
INV_ROW = re.compile(r"^\| (?P<promise>[^|]+)\| (?P<breaks>[^|]+)\| (?P<checks>[^|]+)\| (?P<run>[^|]+)\|$")


def _invariant_rows(text: str):
    """(promise, checks cell, command cell) for each pytest row in a table."""
    for line in text.splitlines():
        m = INV_ROW.match(line)
        if m and "python3 -m pytest" in m["run"]:
            yield m["promise"].strip(), m["checks"].strip(), m["run"].strip().strip("`")


def _invariant_row_mismatches(text: str) -> list:
    """Rows whose documented command cannot select a check the same row names.

    Static rather than a nested `--collect-only` for speed, which is legal only while
    every selector here is a plain `or` list of substrings — `-k` matches a needle
    against the test id, so "needle in name" *is* "collected". The assertion below
    refuses any richer expression instead of quietly stop meaning anything, and says
    what to do about it.
    """
    problems = []
    for promise, checks, cmd in _invariant_rows(text):
        names = re.findall(r"`(test_[a-z0-9_]+)`", checks)
        if not names:
            continue                      # the row names whole files; nothing to match
        selector = re.search(r'-k\s+"([^"]*)"', cmd)
        if selector is None:
            scope = _suite_test_names(cmd)
            problems.extend(
                (promise, name, cmd, "not defined in the paths the row runs")
                for name in names if name not in scope)
            continue
        expr = selector.group(1)
        assert re.fullmatch(r"[A-Za-z0-9_]+(?: or [A-Za-z0-9_]+)*", expr), (
            f"{promise}: its -k expression is no longer a plain or-list of "
            "substrings, so this gate can no longer reason about it. Update the "
            "gate to the new grammar — do not loosen it.")
        needles = expr.split(" or ")
        problems.extend(
            (promise, name, expr, "no -k needle matches this test id")
            for name in names
            if not any(needle in name for needle in needles))
    return problems


def test_every_invariant_rows_command_selects_the_checks_it_names():
    """A row that names five checks and runs four of them is a row that lies.

    Found by hand twice in one sitting: the schedule row's
    `-k "timer or scheduled"` did not select the
    `test_an_unreachable_systemd_…` check it names, and the interface row's
    `key_help` needle did not select `…_documents_itself_in_the_help`. Both read
    as documentation, which is exactly why nobody re-ran them: a maintenance
    engineer who runs the row's own command gets a green that omits the guard.
    """
    text = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert list(_invariant_rows(text)), "AGENTS.md's table has no pytest row to check"
    problems = _invariant_row_mismatches(text)
    assert not problems, "invariant rows whose command skips a check they name:\n" + "\n".join(
        f"  {p}: {n} — {sel} — {why}" for p, n, sel, why in problems)


def _suite_test_names(cmd: str) -> set:
    """Every test function defined under the paths a row's command points at."""
    names = set()
    for token in cmd.split():
        if token.startswith("-") or "=" in token:
            continue
        path = (ROOT / token).resolve()
        if path.is_file():
            candidates = [path]
        elif path.is_dir():
            candidates = sorted(path.rglob("test_*.py"))
        else:
            continue
        for candidate in candidates:
            names.update(re.findall(r"^def (test_\w+)", candidate.read_text(encoding="utf-8"),
                                    re.M))
    return names


def test_an_invariant_row_that_names_a_missing_check_is_caught():
    """The gate above must be capable of failing, on the real shape of the bug.

    Provoked on a **string**, not on the file: editing AGENTS.md in place would leave
    a tracked document mutilated for the length of a pytest run, which is a bad deal
    if the run is interrupted and a worse one if another agent is editing the same
    table. The mutated text goes straight to the checker, so the evidence is the same
    and nothing is ever written.
    """
    text = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert not _invariant_row_mismatches(text), "the control: the real table is clean"
    rows = list(_invariant_rows(text))
    assert rows, "the table has no pytest rows to provoke"

    # (a) a row that runs files names a check that is not defined anywhere in them
    promise, checks, cmd = rows[-1]
    assert "-k" not in cmd, f"the last row now carries a selector: {cmd}"
    edited = text.replace(checks, checks + ", `test_a_check_nobody_wrote`")
    assert edited != text, "could not inject into the row"
    found = _invariant_row_mismatches(edited)
    assert any(n == "test_a_check_nobody_wrote" for _, n, _, _ in found), (promise, found)

    # (b) a selector whose first needle cannot match the ids the row claims
    t_promise, _, t_cmd = next(r for r in rows if "-k" in r[2])
    needle = re.search(r'-k\s+"([^"]*)"', t_cmd).group(1).split(" or ")[0]
    broken_cmd = t_cmd.replace(f'"{needle} or', '"zzzcannotmatch or', 1)
    assert broken_cmd != t_cmd, "could not rewrite the selector"
    found = _invariant_row_mismatches(text.replace(t_cmd, broken_cmd))
    assert any(p == t_promise and "zzzcannotmatch" in sel for p, _, sel, _ in found), found
