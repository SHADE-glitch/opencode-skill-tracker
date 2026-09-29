"""The project README must document every required section and limitation."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]      # repo root
README = ROOT / "README.zh-CN.md"
SYSTEMD = ROOT / "skill-tracker" / "systemd"


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
    for marker in (
        "M1", "M2", "M3", "M4", "M5", "M6", "M7", "M8", "M9", "M10",
        "M11", "M12", "M13",
    ):
        assert marker in text, f"README does not mention {marker}"
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
    ):
        assert cmd in text, f"command not documented: {cmd}"
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
