# Measurements

Every number in this repository that is a **measurement** — a size, a duration, a rate, a
count — came out of a run, not out of the code. Code holds pins; runs hold measurements.
This file is the runs: the exact commands, what each one touches, and how to tell whether a
number quoted somewhere else is still alive.

It exists because a checklist that carries numbers with no command beside them goes stale
in silence. A number nobody can re-measure is a claim, not data.

This file is **English only**, like the rest of `docs/maintenance/`. The bilingual parity
gates in `test_readme.py` cover README ×2 and MAINTENANCE ×2; a `docs/maintenance/` file has
no twin, so it is linked from both checklists instead, which
`test_the_maintenance_docs_are_linked_from_both_checklists` enforces.

---

## 0. How to use this file

1. **Date every reading.** Write `2026-10-13: …` beside any number you copy out of a run. A
   count with no date will be read as current forever.
2. **Never copy a count into a document that has no command beside it.** That is a hard rule
   in [`MAINTENANCE.md`](../MAINTENANCE.md) §0, which points here instead of holding numbers
   of its own. This file is the exception by construction: every number below is dated, tied
   to the command that printed it, and marked as a reading.
3. **Tell the three kinds of number apart.** *a pin* — the code asserts it and drift fails a
   test (`name_cap = 28`, `SCHEMA_VERSION`, `BACKUP_KEEP = 5`, textual `>=8.2,<9`); *a
   measurement* — true at a timestamp on one machine (everything below); *a reading of a
   file another process appends to* — our own sidecar, which the hook writes between runs.
4. **`ocut health` is the aggregate; these recipes are the parts.** When a line looks wrong,
   §2–§6 say which side of which promise it belongs to.

```bash
ocut --help
ocut health
```

Every recipe here was run on the host described at the end of
[`compatibility.md`](compatibility.md) §1. The output shown is what that host said that day —
an example of the *shape* of a reading, never an acceptance criterion.

---

## 1. The pins this file leans on, and where each one lives

A recipe below prints a number the code also asserts. Those are **not** measurements — they
are the same value read from the code — and they are listed here so a reader who meets `28`
knows it came from `ocut_db.DEFAULTS`, not from a run.

| Pin | Value lives at | Where this file uses it |
|---|---|---|
| `name_cap` | `ocut_db.py` `DEFAULTS["name_cap"]` | §3 — the widest cell a table renders before a name is clipped |
| `SCHEMA_VERSION` | `ocut_db.py` | §2 — the view-redefinition counter; a bump clears the cursors |
| `BACKUP_KEEP` | `ocut_db.py` | §5 — how many managed snapshots retention keeps |

If one of them moves, this file moves with it and
`test_measurements_quotes_the_cap_the_code_actually_resolves` is what complains.

```bash
python3 - <<'PY'
import sys; sys.path.insert(0, "scripts")
import ocut_db as db
print("name_cap =", db.DEFAULTS["name_cap"], "columns")
print("SCHEMA_VERSION =", db.SCHEMA_VERSION)
print("BACKUP_KEEP =", db.BACKUP_KEEP)
PY
```

2026-10-13: `name_cap = 28 columns`, `SCHEMA_VERSION = 2`, `BACKUP_KEEP = 5`. The 28 is
measured on real data, not a round number — every entity name fits it, only the project path
does not.

---

## 2. Database: shape, integrity, size

Read-only by construction: `mode=ro` in the URI, so nothing in this recipe can move the
file's mtime or take a write lock.

```bash
DB="$HOME/.local/share/opencode-usage-tracker/usage.db"
python3 - "$DB" <<'PY'
import os, sqlite3, sys
path = sys.argv[1]
print("size", os.path.getsize(path), "B")
con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
for q in ("pragma user_version", "pragma journal_mode", "pragma integrity_check"):
    print(q, "->", con.execute(q).fetchone()[0])
for t in ("tool_calls", "model_calls", "skill_usage", "agent_usage", "mcp_usage",
          "plugin_usage", "sessions", "inventory", "unparsed"):
    print(t, con.execute(f"select count(*) from {t}").fetchone()[0])
PY
```

2026-10-13: `integrity_check ok`, `journal_mode wal`, and non-zero `tool_calls` / `sessions`
/ `model_calls` rows. What is safe to assert: `integrity_check` must read `ok`; a row count
that drops without an explicit rebuild is the story, not the number; `size` only shrinks
under a rebuild that rewrites the file.

---

## 3. The TUI: what a page refresh costs, and the clipping cap

**Copy the database first. Do not run a timing recipe against the live one.** The clipping
cap itself is a pin (28 columns, §1); the reading here is the refresh cost.

```bash
cd <repo> && .venv/bin/python - <<'PY'
import asyncio, importlib.util, os, sqlite3, sys, time
SRC = os.path.expanduser("~/.local/share/opencode-usage-tracker/usage.db")
DST = "/tmp/ocut-measure-copy.db"
BEFORE = os.path.getmtime(SRC)
src = sqlite3.connect(f"file:{SRC}?mode=ro", uri=True)
dst = sqlite3.connect(DST); src.backup(dst); src.close(); dst.close()
spec = importlib.util.spec_from_file_location("ocut_m", "scripts/ocut-tui.py")
mod = importlib.util.module_from_spec(spec); sys.modules["ocut_m"] = mod
spec.loader.exec_module(mod)
async def main():
    app = mod.TrackerApp(db_path=DST)
    async with app.run_test(size=(120, 40)) as pilot:
        t0 = time.perf_counter(); await pilot.pause()
        return (time.perf_counter() - t0) * 1000
print("first paint + one frame ms:", round(asyncio.run(main()), 1))
print("live db mtime moved?", os.path.getmtime(SRC) != BEFORE)
PY
```

2026-10-13: `live db mtime moved? False`, and the first paint plus one frame in the tens of
milliseconds on this host. The resolved `name_cap` is **28 columns**: a table cell wider than
28 columns is clipped to a prefix and the status line says how many columns sit past the
right edge. Re-take this when the layout or the row counts change materially.

---

## 4. The plugin-usage sidecar: how fast it fills

The one input that is ours, and the only one that can be truncated by its own writer.

```bash
SIDECAR="$HOME/.local/share/opencode-usage-tracker/plugin-events.jsonl"
python3 - "$SIDECAR" <<'PY'
import collections, os, sys, json
path = sys.argv[1]
if not os.path.exists(path):
    print("no sidecar — the hook has never run"); raise SystemExit
size = os.path.getsize(path)
kinds = collections.Counter()
with open(path, encoding="utf-8", errors="replace") as fh:
    for line in fh:
        line = line.strip()
        if not line:
            continue
        try:
            kinds[json.loads(line).get("t", "?")] += 1
        except ValueError:
            kinds["unparseable"] += 1
print(f"size={size:,} B  cap={8*1024*1024:,} B  rotated={size > 8*1024*1024}")
print(dict(kinds))
PY
```

2026-10-13: a small sidecar well under the 8 MiB cap, with `surface` and `call` records and
no `unparseable` lines. A sidecar that reads `rotated=True` has been truncated by the hook;
the reader notices and re-reads from the start.

---

## 5. Snapshots, retention and the health aggregate

```bash
BACKUPS="$HOME/.local/share/opencode-usage-tracker/backups"
python3 - "$BACKUPS" <<'PY'
import datetime, os, sys
d = sys.argv[1]
names = sorted(n for n in os.listdir(d) if n.endswith(".db")) if os.path.isdir(d) else []
now = datetime.datetime.now(datetime.timezone.utc)
print(f"{len(names)} snapshot(s) in {d}")
for n in names:
    st = os.stat(os.path.join(d, n))
    age = now - datetime.datetime.fromtimestamp(st.st_mtime, datetime.timezone.utc)
    print(f"  {n}  {st.st_size:,} B  {age.total_seconds()/86400:.1f} d old")
PY
```

2026-10-13: a handful of snapshots, all inside the managed directory, the newest of them
recent. Retention keeps the newest `BACKUP_KEEP` (5) by mtime.

```bash
ocut health; echo "exit $?"
```

2026-10-13: `exit 0`, the host database found, the schema version equal to `SCHEMA_VERSION`,
and `unparsed 0 — every record type was claimed`. The `unparsed` line is the aggregate this
whole file is arranged around: a non-zero value is the format-move signal.

---

## 6. The suite, and the run that proves the suite is the machine's

```bash
python3 -m pytest scripts/tests -q                       # system interpreter
.venv/bin/python -m pytest scripts/tests -q              # the venv the launcher uses
.venv/bin/python -m pytest scripts/tests -q -n auto      # the same suite, pytest-xdist
```

2026-10-13: the suite is green on both. **Do not copy the pass total into a document** — it
is a run, with an interpreter and an installed-textual set behind it, and the two
interpreters can differ in whether the TUI files are skipped. If a run reports fewer passes
than the last, the diff to look at is `git log --oneline -- scripts/tests`, not the number.

2026-10-17: `-n auto` on this host (18 cores, `pytest-xdist` 3.8.0, `.venv` python 3.13.14,
pytest 9.1.1, textual 8.2.8) finishes the same suite in **26.2 s against 175.7 s** serial — and
reports the same `322 passed, 3 subtests passed`, which is the reading that matters: the flag
moves the wall clock, not the set of checks. It is opt-in (`requirements-dev.txt`), and the
reason it is a flag and not an `addopts` is in MAINTENANCE.md §11. Useful when a red-check loop
is being run several times in a row; not useful as the gate's definition, because the number a
maintainer reads should come from the command the documentation names.

---

## 7. Recording a reading

When a number from this file goes into another document, write it in this shape:

```
measured 2026-10-13 on <OpenCode release> / python3 <ver> (+ .venv <ver>), textual <ver>,
<command or §-number of this file> — <the number> — <what it is a bound of, if it is a bound>
```

The parts that are easy to skip and expensive to lose: **which command** (so it can be
re-taken), **which interpreter**, and **whether it is a bound** (a truncated sidecar read is
a floor). A reading that cannot be traced to a command in this file is not a measurement.
