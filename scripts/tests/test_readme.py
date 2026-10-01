"""The project README must document every required section and limitation."""

from __future__ import annotations

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
