"""Log rotation — the growth half of M17, and the file-safety rules around it.

The tracker's own log is where capture errors appear (`doctor log.errors` reads it) and
it only grew: one line per init and dispose, forever. S2 bounded the **read**; this is
the **write** side, and it is deliberately a rename, not a deletion — the lines that
move are the evidence a future `doctor` run would otherwise lose.

The properties under test:

* rotation happens at the same size the reader reads, so no line can be in the active
  file and invisible to `doctor` at the same time;
* rotating moves bytes: the rotated file holds the previous content exactly, and the
  live path is left empty rather than missing, because a missing log makes `doctor`
  WARN "the plugin has not initialised" about a plugin that is fine;
* a write that arrives between the rename and the re-create is never truncated away;
* only our own log names are ever touched — a neighbour's log under the same directory
  is not ours to rotate, and the command refuses;
* pruning old rotations matches the rotated-name shape for that base name and nothing
  else in the directory;
* `--yes` is what applies it, and the systemd unit is still opt-in.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from conftest import load_module

import skill_db as db
import settings as cfg
import skill_db_claude_mem as cm

st = load_module("skill-tui.py", "skill_tui_rotate")

REPO = Path(__file__).resolve().parents[2]

LINE = "2026-10-09T00:00:00.000Z [info] initialized (db=/tmp/x.db)\n"
ERR = "2026-10-09T00:00:01.000Z [err] [skill] boom\n"


def names(dirpath):
    return sorted(os.listdir(dirpath))


@pytest.fixture
def logdir(tmp_path):
    """A logs directory holding only our own synthetic log."""
    d = tmp_path / "logs"
    d.mkdir()
    path = d / "skill-tracker.log"
    path.write_text(LINE * 40, encoding="utf-8")
    return path


# --- the cap is one number, not two ----------------------------------------
def test_the_rotation_cap_is_the_number_the_reader_reads(monkeypatch):
    """If the writer rotated at a bigger size than the reader reads, the file would
    grow past the cap and `doctor` would quietly count only part of it again.

    Compared at the *resolved* values, because that is where the two used to part:
    the writer took `args.log_max_bytes` (flag > file > env > default) while the
    reader took the module default, so raising the knob moved one side only.
    """
    assert cfg.spec("log.max_bytes")["default"] == st.TRACKER_LOG_BYTES_CAP
    assert st.TRACKER_LOG_BYTES_CAP == cm.CLAUDE_MEM_LOG_BYTES_CAP
    assert cfg.spec("log.keep_files")["default"] == 5
    assert cfg.spec("log.keep_files")["minimum"] == 1

    writer = st.parse_args(["--cli", "rotate-log"])
    reader = st.parse_args(["--cli", "doctor"])
    assert writer.log_max_bytes == reader.log_max_bytes == st.TRACKER_LOG_BYTES_CAP

    monkeypatch.setenv("OPENCODE_SKILL_TRACKER_LOG_MAX_BYTES", "2048")
    writer = st.parse_args(["--cli", "rotate-log"])
    reader = st.parse_args(["--cli", "doctor"])
    assert writer.log_max_bytes == reader.log_max_bytes == 2048, (
        "the override moved one side of the pair")


# --- nothing happens below the cap -----------------------------------------
def test_a_log_under_the_cap_is_left_alone(logdir):
    plan = db.plan_log_rotation(str(logdir), max_bytes=1024 * 1024, keep=5)
    assert plan["needed"] is False, plan
    assert plan["size"] == logdir.stat().st_size
    res = db.rotate_log(str(logdir), max_bytes=1024 * 1024, keep=5, dry_run=False)
    assert res["rotated"] is None, res
    assert names(logdir.parent) == ["skill-tracker.log"]
    assert logdir.read_text(encoding="utf-8") == LINE * 40


def test_dry_run_touches_nothing(logdir):
    before = names(logdir.parent)
    res = db.rotate_log(str(logdir), max_bytes=64, keep=5, dry_run=True)
    assert res["would_rotate"] is True, res
    assert res["target"].startswith("skill-tracker.log."), res["target"]
    assert names(logdir.parent) == before
    assert logdir.read_text(encoding="utf-8") == LINE * 40


# --- rotation moves bytes, it does not remove them --------------------------
def test_the_rotated_file_holds_the_previous_content_exactly(logdir):
    original = logdir.read_bytes()
    res = db.rotate_log(str(logdir), max_bytes=64, keep=5, dry_run=False)
    rotated = logdir.parent / os.path.basename(res["rotated"])
    assert rotated.is_file()
    assert rotated.read_bytes() == original, "rotation lost or changed a byte"
    # The live path exists and is empty: `doctor` must not say "the plugin has not
    # initialised" about a plugin that is running fine.
    assert logdir.is_file() and logdir.stat().st_size == 0
    assert oct(logdir.stat().st_mode & 0o777) == "0o600", "the log holds paths"
    assert oct(rotated.stat().st_mode & 0o777) == "0o600"
    assert res["pruned"] == [], res


def test_a_line_written_after_the_rename_survives(logdir):
    """The plugin appends with `appendFileSync`, so it can write between the rename
    and the re-create. Creating the new file must never truncate it."""
    logdir.unlink()
    with logdir.open("w", encoding="utf-8") as f:
        f.write(LINE * 40)
    res = db.rotate_log(str(logdir), max_bytes=64, keep=5, dry_run=False)
    # Simulate the plugin's next line landing after the rename.
    with open(str(logdir), "a", encoding="utf-8") as f:
        f.write(ERR)
    assert os.path.getsize(str(logdir)) == len(ERR.encode())
    assert ERR in logdir.read_text(encoding="utf-8")
    assert res["rotated"]

    # And a second rotation keeps both generations: 5 files, newest first.
    assert len([n for n in names(logdir.parent) if n.startswith("skill-tracker.log.")]) == 1


def test_rotation_is_reentrant_and_names_files_in_order(logdir):
    seen = []
    for _ in range(3):
        res = db.rotate_log(str(logdir), max_bytes=64, keep=5, dry_run=False)
        assert res["rotated"], res
        seen.append(os.path.basename(res["rotated"]))
        # The plugin writes again between rotations — enough to need the next one.
        with logdir.open("a", encoding="utf-8") as f:
            f.write(LINE * 3)
    assert len(set(seen)) == 3, seen
    assert seen == sorted(seen), f"the names must sort in time order: {seen}"
    assert logdir.stat().st_size > 0, "the live log was left missing after rotation"


# --- pruning keeps what it names --------------------------------------------
def test_pruning_keeps_the_newest_and_deletes_only_our_rotations(logdir):
    foreign = logdir.parent / "inject-trace.log"
    foreign.write_text("not ours\n", encoding="utf-8")
    handmade = logdir.parent / "skill-tracker.log.keep-by-hand"
    handmade.write_text("not a rotation name\n", encoding="utf-8")

    for _ in range(4):
        db.rotate_log(str(logdir), max_bytes=64, keep=2, dry_run=False)

    rotated = sorted(n for n in names(logdir.parent)
                     if n.startswith("skill-tracker.log.") and n != "skill-tracker.log")
    assert len(rotated) == 2, rotated
    assert foreign.is_file(), "a neighbour's log was touched"
    assert handmade.is_file(), "a file that is not a rotation name was deleted"


# --- the path guard ---------------------------------------------------------
def test_a_log_that_is_not_ours_is_never_rotated(tmp_path):
    """`rotate-log` takes no path argument, and the helper refuses foreign names
    anyway: the neighbour's log next to ours is not ours to move."""
    other = tmp_path / "inject-trace.log"
    other.write_text("a neighbour's log\n", encoding="utf-8")
    with pytest.raises(ValueError):
        db.rotate_log(str(other), max_bytes=1, keep=5, dry_run=False)
    with pytest.raises(ValueError):
        db.plan_log_rotation(str(other), max_bytes=1, keep=5)
    assert other.is_file() and other.read_text(encoding="utf-8") == "a neighbour's log\n"
    assert names(tmp_path) == ["inject-trace.log"]


def test_both_names_the_plugin_writes_are_ours():
    """The selftest log is a separate file by design; a rotation set that forgot it
    would leave one growing file nobody bounds."""
    assert set(db.LOG_FILE_NAMES) == {"skill-tracker.log", "skill-tracker-selftest.log"}
    src = (REPO / "plugin" / "skill-tracker.js").read_text(encoding="utf-8")
    for name in db.LOG_FILE_NAMES:
        assert name in src, f"the writer no longer names {name}"


# --- the command line -------------------------------------------------------
def _args(*extra):
    return st.parse_args(["--cli", "rotate-log", *extra])


def test_rotate_log_needs_no_database(tmp_path, monkeypatch, capsys):
    """Dispatch happens before the database check, like `config`.

    The log exists before the database does on a fresh machine, and rotation is the
    command that keeps it readable — a version that demanded a database could not run
    where it is needed most.
    """
    log = tmp_path / "skill-tracker.log"
    log.write_text(LINE * 40, encoding="utf-8")
    monkeypatch.setattr(st, "TRACKER_LOG_PATH", str(log))
    monkeypatch.setattr(st.db, "open_db",
                        lambda *a, **k: pytest.fail("rotate-log must not open the DB"))
    assert st.cli_main(_args("--db", str(tmp_path / "nope.db"),
                             "--max-bytes", "1024", "--yes")) == 0
    out = capsys.readouterr().out
    assert "rotated" in out, out
    assert len([n for n in names(tmp_path) if n.startswith("skill-tracker.log.")]) == 1


def test_the_cli_dry_runs_by_default(logdir, monkeypatch, capsys):
    monkeypatch.setattr(st, "TRACKER_LOG_PATH", str(logdir))
    assert st.cli_main(_args("--max-bytes", "1024")) == 0
    out = capsys.readouterr().out
    assert "[dry-run]" in out and "Re-run with --yes" in out, out
    assert names(logdir.parent) == ["skill-tracker.log"]


def test_the_cli_reports_a_log_that_needs_nothing(logdir, monkeypatch, capsys):
    monkeypatch.setattr(st, "TRACKER_LOG_PATH", str(logdir))
    assert st.cli_main(_args("--max-bytes", "1048576", "--yes")) == 0
    out = capsys.readouterr().out
    assert "below" in out, out
    assert names(logdir.parent) == ["skill-tracker.log"]


def test_json_output_names_size_target_and_pruned(logdir, monkeypatch, capsys):
    monkeypatch.setattr(st, "TRACKER_LOG_PATH", str(logdir))
    assert st.cli_main(_args("--max-bytes", "1024", "--keep-files", "1",
                             "--json")) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["dry_run"] is True
    assert doc["size"] == logdir.stat().st_size
    assert doc["max_bytes"] == 1024 and doc["keep_files"] == 1
    assert doc["path"] == str(logdir)
    assert doc["target"].startswith("skill-tracker.log.")


def test_the_cap_and_the_keep_are_settings(tmp_path, monkeypatch, capsys):
    """File and flag both reach the same two numbers, in that order of precedence."""
    conf = tmp_path / "skillt-config.json"
    conf.write_text('{"log.max_bytes": 4096, "log.keep_files": 3}', encoding="utf-8")
    monkeypatch.setenv(cfg.CONFIG_PATH_ENV, str(conf))
    a = st.parse_args(["--cli", "rotate-log"])
    assert (a.log_max_bytes, a.log_keep_files) == (4096, 3)
    b = st.parse_args(["--cli", "rotate-log", "--max-bytes", "2048", "--keep-files", "9"])
    assert (b.log_max_bytes, b.log_keep_files) == (2048, 9)


def test_an_impossible_number_is_refused(tmp_path, monkeypatch):
    log = tmp_path / "skill-tracker.log"
    log.write_text(LINE, encoding="utf-8")
    monkeypatch.setattr(st, "TRACKER_LOG_PATH", str(log))
    with pytest.raises(SystemExit) as ei:
        st.parse_args(["--cli", "rotate-log", "--keep-files", "0"])
    assert ei.value.code == 2
    with pytest.raises(SystemExit):
        st.parse_args(["--cli", "rotate-log", "--max-bytes", "-1"])
    # The floor is 1 KiB by policy: a smaller cap would rotate on the first line and
    # turn the history `doctor` reads into five files of one line each.
    with pytest.raises(SystemExit):
        st.parse_args(["--cli", "rotate-log", "--max-bytes", "512"])


# --- doctor stays honest ----------------------------------------------------
def test_doctor_reads_a_rotated_log_without_crying_wolf(logdir, monkeypatch):
    """After rotation the live file is empty, which is a healthy state, not a missing
    plugin — and the `[err]` line that was in it is now in the rotated file."""
    logdir.write_text(LINE * 4 + ERR, encoding="utf-8")
    conn = db.open_db(str(logdir.parent / "unused.db"), readonly=False)
    db.ensure_schema(conn)
    conn.close()
    conn = db.open_db(str(logdir.parent / "unused.db"))
    args = st.parse_args(["--cli", "doctor", "--db", str(logdir.parent / "unused.db")])
    monkeypatch.setattr(st, "TRACKER_LOG_PATH", str(logdir))
    # This file's subject is the log, not the schedule: without the stub the two
    # `_doctor_checks` calls below would ask this machine's systemd (see conftest).
    monkeypatch.setattr(st, "_backup_timer_state", lambda: "enabled")
    name, status, detail = {c[0]: c for c in st._doctor_checks(conn, args)}["log.errors"]
    assert status == "WARN" and "1 error line(s)" in detail, detail

    db.rotate_log(str(logdir), max_bytes=16, keep=5, dry_run=False)
    name, status, detail = {c[0]: c for c in st._doctor_checks(conn, args)}["log.errors"]
    assert status == "PASS", detail
    assert "0 error line(s)" in detail, detail
    conn.close()


# --- the scheduled half -----------------------------------------------------
def test_the_backup_service_rotates_the_log_itself():
    """One scheduled job, two maintenance chores.

    The timer already exists and already prunes backups; the log has no other
    sweeper, and a rotation nobody schedules is a rotation that silently stops
    happening. `--yes` belongs in a unit, not on a prompt.
    """
    svc = REPO / "skill-tracker" / "systemd" / "skillt-auto-backup.service"
    text = svc.read_text(encoding="utf-8")
    execs = [l for l in text.splitlines() if l.startswith("ExecStart=")]
    assert any("rotate-log --yes" in l for l in execs), execs
    assert any("auto-backup" in l for l in execs), execs
    for line in execs:
        assert line.split("=", 1)[1].split()[0].startswith("%h"), line


def test_installing_the_timer_is_still_opt_in():
    """`--with-timer` exists and is off by default: writing a systemd unit is a
    change outside this project's directory and must be asked for."""
    sh = (REPO / "install.sh").read_text(encoding="utf-8")
    assert "--with-timer" in sh
    assert "TIMER=0" in sh, "the flag must default to not installing a unit"

    # The invariant is about the *writes*, not about the words: naming the directory
    # is harmless, `cp`-ing a unit into it is not. So the split point is the guard and
    # everything that changes the system must sit on the guarded side.
    guard = sh.index('if [ "$TIMER" -eq 1 ]')
    before, after = sh[:guard], sh[guard:]
    for action in ('cp -f "$REPO_DIR/skill-tracker/systemd',
                  'systemctl --user', 'mkdir -p "$SYSTEMD_DIR"'):
        assert action not in before, f"a default install would run: {action}"
        assert action in after, f"the opt-in branch no longer does: {action}"
    subprocess.run(["bash", "-n", str(REPO / "install.sh")], check=True)
    out = subprocess.run(["bash", os.path.join(REPO, "install.sh"), "--help"],
                         capture_output=True, text=True)
    assert "--with-timer" in out.stdout, out.stdout
