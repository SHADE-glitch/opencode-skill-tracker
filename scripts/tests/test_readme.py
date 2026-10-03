"""The project README must document every required section and limitation."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]      # repo root
README = ROOT / "README.zh-CN.md"
SYSTEMD = ROOT / "skill-tracker" / "systemd"

# Every documented limitation, in both languages. M14-M18 came out of the
# full capture/UI audit; M19 out of the privacy cleanup that followed it. The
# English README used to stop at M11, so the landing page quietly disagreed with
# the reference doc.
LIMITATIONS = tuple(f"M{i}" for i in range(1, 22))


def _text():
    assert README.is_file(), f"missing README at {README}"
    return README.read_text(encoding="utf-8")


def test_readme_has_all_sections():
    text = _text()
    for section in (
        "项目介绍", "架构", "目录说明", "命令说明", "数据库说明",
        "备份与恢复", "故障排查", "删除方法", "已知限制",
    ):
        assert section in text, f"README is missing the '{section}' section"


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
    ):
        assert keyword in text, f"English limitation detail missing: {keyword}"


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
        "skillt scrub-metadata", "capture.freshness", "log.errors",
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
                   "Object.keys()", "TABLES_SQL"):
        assert needle in en, f"MAINTENANCE.md dropped an invariant: {needle}"
        assert needle in zh, f"MAINTENANCE.zh-CN.md dropped an invariant: {needle}"

    # Deviations must be written down where the next reader will look, not in
    # a commit message.
    for needle in ("set_interval", "active_app", "row_targets", "schema_version"):
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
        "skillt scrub-metadata", "skillt agentos",
    ):
        assert cmd in text, f"command not documented: {cmd}"
    # the new headless surface must be documented where readers look for it
    en = (ROOT / "README.md").read_text(encoding="utf-8")
    for cmd in ("skillt scrub-metadata", "skillt doctor", "--freshness-days",
                "skillt agentos", "OPENCODE_SKILL_TRACKER_AOS_TIMEOUT_MS"):
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
    """
    en = (ROOT / "README.md").read_text(encoding="utf-8")
    zh = (ROOT / "README.zh-CN.md").read_text(encoding="utf-8")

    def listed(text, marker):
        start = text.index(marker) + len(marker)
        segment = text[start:start + 400]
        return segment[: segment.index("(") if "(" in segment[:400] else 400]

    for label, segment in (("README.md", listed(en, "Pages: ")),
                           ("README.zh-CN.md", listed(zh, "页面："))):
        for page in ("Dashboard", "Skills", "MCP", "Plugins", "Recent",
                     "Categories", "Data"):
            assert page in segment, f"{label} page list omits {page}: {segment[:120]!r}"
        assert "Advisor" not in segment, f"{label} advertises a tab that is gone"


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
