# Measurements

Every number in this repository that is a **measurement** — a size, a duration, a
rate, a count — came out of a run, not out of the code. Code holds pins; runs hold
measurements. This file is the runs: the exact commands, what each one touches, and
how to tell whether a number quoted somewhere else is still alive.

It exists because MAINTENANCE §0 used to carry numbers with no command beside them.
Two of them were already wrong when this file was written — "~69 lines/day" against a
measured 74.4, "8.2–15.8 ms" against a measured 12.2–14.0 — and neither could be
re-taken without guessing at the method. A number nobody can re-measure is a claim,
not data.

This file is **English only**, like the rest of `docs/maintenance/`. The bilingual
parity gates in `test_readme.py` cover README ×2 and MAINTENANCE ×2; a
`docs/maintenance/` file has no twin, so it is linked from both checklists instead,
which `test_the_maintenance_docs_are_linked_from_both_checklists` enforces.

---

## 0. How to use this file

1. **Date every reading.** Write `measured 2026-10-09` beside any number you copy out
   of a run. A count with no date will be read as current forever.
2. **Never copy a count into a document that has no command beside it.** That is a
   hard rule in AGENTS.md, and it is why MAINTENANCE §0 now points here instead of
   holding numbers of its own. **This file is the exception by construction, not by
   convenience**: the rule exists because an undated count in a user-facing document
   reads as current state forever, and every number below is dated, tied to the
   command that printed it, and marked as a reading when it is one. A number from here
   may be quoted elsewhere only together with its command and its date — and a suite
   total may never be quoted at all (§10).
3. **Tell the three kinds of number apart.**
   *a pin* — the code asserts it and drift fails a test (`4 * 1024 * 1024`,
   `SCHEMA_VERSION = 2`, textual `>=8.2,<9`);
   *a measurement* — true at a timestamp on one machine (everything below);
   *a reading of somebody else's store* — true at a timestamp **and moving while you
   are not looking** (claude-mem's ledger and logs, the AgentOS store, our own plugin
   log). The third kind is why §6 and §7 state no thresholds.
4. **`skillt doctor` is the aggregate; these recipes are the parts.** When doctor
   WARNs, §4–§9 say which side of which promise the WARN belongs to.

Every recipe here was run on 2026-10-09 on the host described at the end of
[`compat-matrix.md`](compat-matrix.md) §1. The output shown is what that host said
that day — an example of the *shape* of a reading, never an acceptance criterion.

---

## 1. The pins this file leans on, and where each one lives

A recipe below prints a number that the code also asserts. Those are **not**
measurements — they are the same value read from the registry — and they are listed
here so a reader who meets `4,194,304` in §4 knows it came from code, not from a run.
If one of them moves, this file moves with it and
`test_measurements_quotes_the_cap_the_code_actually_resolves` is what complains.

| Pin | Value lives at | Where this file uses it |
|---|---|---|
| `log.max_bytes` | `settings.py` `REGISTRY`, surfaced as `TRACKER_LOG_BYTES_CAP` in `skill-tui.py` | §4's `truncated=` test and §8's rotation plan — the same number on both sides, which is the invariant |
| `CLAUDE_MEM_LOG_BYTES_CAP` | `skill_db_claude_mem.py` | §6 — pinned equal to the row above by `test_the_rotation_cap_is_the_number_the_reader_reads` |
| `PRAGMA user_version` / `SCHEMA_VERSION` | `skill_db.py` | §2 — the view-redefinition counter, bumped only by `test_migration.py`'s rule |
| freshness thresholds | `skill-tui.py` `CAPTURE_FRESHNESS_DAYS`, `doctor`'s 7-day backup age | §8 and §9 quote them as the reason a line is PASS, not as a measurement |

The export document's own `schema_version` (`export_document` in `skill_db.py`, and
`SCHEMA_VERSION` two below it — different counters on purpose) is not measured here at
all; it is a contract, and `test_export.py` reads it out of the JSON rather than out of
a run.

Two numbers below are the opposite case, and it is worth stating which is which:

- **`__selftest`'s 63/63 (§5) is a gate.** Its floor is pinned by
  `test_selftest_is_green_end_to_end`, so the count is a claim the suite defends and
  may be quoted — with its date, like everything else here.
- **The suite's pass count (§10) is not.** 649 on one host is 489-plus-skips on CI
  because `bun` and the interpreter differ; it is the third kind of number, and
  AGENTS.md's ban on copying aggregate counts into documents is what keeps §10's own
  figure from being quoted anywhere else. Note which one §10 tells you not to copy.

---

## 2. Database: shape, integrity, size

Read-only by construction: `mode=ro` in the URI, so nothing in this recipe can move
the file's mtime or take a write lock.

```bash
DB="$HOME/.local/share/opencode/skill-usage.db"
python3 - "$DB" <<'PY'
import os, sqlite3, sys
path = sys.argv[1]
wal = path + "-wal"
print("size", os.path.getsize(path), "B | wal", os.path.getsize(wal) if os.path.exists(wal) else 0, "B")
con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
for q in ("pragma user_version", "pragma journal_mode", "pragma integrity_check"):
    print(q, "->", con.execute(q).fetchone()[0])
for t in ("skills", "skill_usage", "mcp_usage", "plugin_usage",
          "subagent_usage", "skill_versions", "plugin_inventory"):
    print(t, con.execute(f"select count(*) from {t}").fetchone()[0])
PY
```

2026-10-09: `606,208 B`, `-wal 0 B`, `user_version 2`, `journal_mode wal`,
`integrity_check ok`, and `44 / 4 / 21 / 139 / 5 / 81 / 5` rows in that column order.

What is safe to assert against that output, and what is not:

| Line | Kind | If it moves |
|---|---|---|
| `user_version` | a pin (`SCHEMA_VERSION = 2`) | must equal `skill_db.SCHEMA_VERSION`; a difference is the silent-old-view bug, `test_migration.py` |
| `integrity_check` | health | anything but `ok` — stop, take a backup, do not run any mutating command |
| row counts | readings of our own store | expected to *grow*; a count that drops without a `prune-usage --yes` is the story, not the number |
| `size` | a reading | grows with rows; `VACUUM` is the only thing that shrinks it, and `backup_db` uses `VACUUM INTO` so backups are compact by design |

`-wal 0 B` means a checkpoint has happened since the last write. It is **not** a
health signal — a busy host normally shows a non-zero `-wal`.

---

## 3. Metadata census: what the writer actually stored

The privacy promise is that no prose lands in `metadata`. This is the check that
holds it against real rows rather than against fixtures — it reads only **key names**
and counts, and prints no values, so running it in a terminal that logs its scrollback
is safe.

```bash
DB="$HOME/.local/share/opencode/skill-usage.db"
python3 - "$DB" <<'PY'
import collections, json, sqlite3, sys
con = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
c = collections.Counter()
for t in ("skill_usage", "mcp_usage", "plugin_usage", "subagent_usage"):
    for (m,) in con.execute(f"select metadata from {t}"):
        if m:
            c.update(f"{t}.{k}" for k in json.loads(m))
print(dict(sorted(c.items())))
PY
```

2026-10-09, grouped: every table carries `agent · branch · call_id · model · source ·
tool`; `plugin_usage` additionally carries `error` on **45** of its rows. `title`
appears **0 times** — that is the whole point of the run, and the number to look at
after any upgrade.

The key set is a pin, not a measurement: `METADATA_KEYS` in `skill_db.py` is what the
export contract allows, `test_no_path_or_free_text_crosses_the_http_whitelist` and
`test_the_writer_never_names_a_permission_title` hold it, and `skillt scrub-metadata`
must answer 0 rows for anything outside it. So a **new key name** in this output is a
finding, even if the count is small; a **changed count** of an allowed key is not.

The values behind `error` are the one allowed prose-shaped field, and they are
truncated by the writer. If you want to know *which* errors, group them:

```bash
python3 - "$DB" <<'PY'
import collections, json, sqlite3, sys
con = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
c = collections.Counter()
for (m,) in con.execute("select metadata from plugin_usage where metadata is not null"):
    d = json.loads(m)
    if "error" in d:
        c[str(d["error"])[:60]] += 1
for text, n in c.most_common():
    print(n, text)
PY
```

That second command **prints values** — it is the exception, so run it where the
output is not recorded. The first one never does.

---

## 4. Growth: how fast the log and the tables fill

The only number here that a maintenance decision depends on, because it sets how
often `skillt rotate-log` has to run and how long a retention window is in rows.

```bash
LOG="$HOME/.config/opencode/logs/skill-tracker.log"
python3 - "$LOG" <<'PY'
import collections, datetime, glob, os, re, sys
path = sys.argv[1]
cap = 4 * 1024 * 1024            # TRACKER_LOG_BYTES_CAP, and the resolved `log.max_bytes`
size = os.path.getsize(path)
text = open(path, encoding="utf-8", errors="replace").read(cap)
lines = text.splitlines()
days = collections.Counter()
errors = sum(1 for ln in lines if "[err]" in ln)
for ln in lines:
    m = re.match(r"(\d{4}-\d{2}-\d{2})T", ln)
    if m:
        days[m.group(1)] += 1
span = sorted(days)
n = (datetime.date.fromisoformat(span[-1]) - datetime.date.fromisoformat(span[0])).days + 1
print(f"size={size:,} B  lines={len(lines)}  truncated={size > cap}")
print(f"{span[0]}..{span[-1]} over {n} d  ->  {len(lines)/n:.1f} lines/day  errors={errors}")
print("rotated files:", len(glob.glob(path + ".*")))
PY
```

2026-10-09: `118,147 B`, 1,264 lines, `truncated=False`, 2026-09-23..2026-10-09,
**74.4 lines/day**, 4 `[err]` lines, 0 rotated files beside it.

Read it as: at 74.4 lines/day the log grows ~6.9 KB/day, so the cap — 4 MiB, written
as `4,194,304 B`, the resolved default of `log.max_bytes` and spelled `4 * 1024 * 1024`
in the recipe above so you can see which side of the pin you are on — is ~600 days away
at current volume. Rotation is therefore not a space remedy here; it is the thing that
keeps `log.errors` from becoming a *floor* instead of a count. That distinction is
MAINTENANCE §6 and `test_a_bounded_log_read_reports_a_floor_never_a_clean_bill`.
`truncated=False` is
what "this count is a total" looks like; the moment it says `True`, every error count
derived from it — here and in `doctor` — is a lower bound and has to be reported as
one.

For the database, the same arithmetic over rows instead of lines — but take the span
from the table you are counting, not from the log:

```bash
python3 - "$DB" <<'PY'
import sqlite3, sys
con = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
for t in ("skill_usage", "mcp_usage", "plugin_usage", "subagent_usage"):
    print(t, con.execute(
        f"select count(*), min(timestamp), max(timestamp) from {t}").fetchone())
PY
```

2026-10-09: 4 rows 10-03→10-09 · 21 rows 10-03→10-08 · 139 rows 10-03→10-07 ·
5 rows 10-04→10-09. Usage rows are **far** sparser than log lines, and their span is
**shorter than the log's** because capture restarted on 2026-10-03 (M24) while the log
goes back to 2026-09-23 — so a rows-per-day figure divides by a different denominator
than §4's, and quoting one as the other is how the "~69 lines/day" number got into a
document as though it described rows. Both facts are why row retention defaults off
(`retention.usage_days = 0`, §8) while log rotation is a real operation.

---

## 5. The writer's own selftest

The shipped end-to-end check: `__selftest()` in `plugin/skill-tracker.js` drives the
real writer through Bun against a temporary database. It is the only measurement in
this file that is a **gate** — the assertion floor is pinned by
`test_selftest_is_green_end_to_end` — so its pass count belongs in documents and its
*timing* does not.

```bash
LOG="$HOME/.config/opencode/logs/skill-tracker.log"
sha256sum "$LOG" > /tmp/st-log-before.txt
OPENCODE_SKILL_TRACKER_DB=/tmp/st-selftest.db \
  "$HOME/.bun/bin/bun" -e 'import(process.env.HOME + "/.config/opencode/plugin/skill-tracker.js").then(m => m.__selftest())'
sha256sum "$LOG" | diff -q - /tmp/st-log-before.txt && echo "LOG UNCHANGED"
```

2026-10-09: **63/63 passed**, `LOG UNCHANGED`.

Two things this recipe is easy to get wrong, both of which I did today:

- **Pin only the database.** Adding `OPENCODE_SKILL_TRACKER_SKILLS_DIR` or
  `..._PLUGINS` to make the run "hermetic" makes it *not* a selftest: those pins send
  the skill and plugin inventory sections looking at empty directories and the count
  drops (59/63, 62/63). The isolation the check needs is the temp DB — `__selftest()`
  already redirects its **own** log beside that DB, which is what `LOG UNCHANGED`
  proves and what the deliberately-failed run proved in the other direction
  (MAINTENANCE §0's Tracker-log row).
- **The log hash is part of the recipe**, not decoration. Without it, a green 63/63
  tells you the writer works and a line of synthetic `[err]` residue has just landed
  in the file `doctor log.errors` reads — which is exactly how the 4 historical lines
  got there.

---

## 6. Neighbour stores: what reading them costs

The Plugins page's dim line pays this on every repaint, and it is why
`claude_mem_http` is never called from a refresh path (MAINTENANCE §6,
`test_no_http_is_reachable_from_the_tui_refresh_path`).

```bash
cd <repo> && python3 - <<'PY'
import sys, time
sys.path.insert(0, "scripts")
import skill_db_claude_mem as cm
runs = []
for _ in range(5):
    t0 = time.perf_counter()
    act = cm.claude_mem_activity(cm.claude_mem_store())
    runs.append((time.perf_counter() - t0) * 1000)
print("ms per run:", ", ".join(f"{r:.1f}" for r in runs))
print(f"min={min(runs):.1f} max={max(runs):.1f}")
PY
```

2026-10-09: **12.2–14.0 ms** (5 runs). The number that was in MAINTENANCE §0 was
**8.2–15.8 ms** measured 2026-10-04. Both are honest; they are not comparable,
because the third kind of number (§0 rule 3) is being read from files another process
is appending to — its worker log had rolled over twice more in between, and its
ERROR count had moved from 5 to 16.

That is the reason to re-run this recipe rather than trust either reading: the cost
scales with how much that log has grown since its last rotation, bounded by
`CLAUDE_MEM_LOG_BYTES_CAP` (`4 MiB`, and `test_the_rotation_cap_is_the_number_the_reader_reads`
keeps it equal to our own `log.max_bytes`). If a future reading comes out an order of
magnitude higher, the suspect is the neighbour's log size, not our reader.

The AgentOS store is measured the same way through `skill_db_agentos` — counts and
timestamps only, read-only URI, never from a repaint. Its loops get *slower over time*
by design (they are LLM runs), so no threshold is written for it here or anywhere.

---

## 7. TUI: what a page refresh costs, and against which database

**Copy the database first. Do not run this against the live one.**

`MainScreen.on_mount` opens the database **read-write** (it needs `ensure_schema`), so
a timing run against `~/.local/share/opencode/skill-usage.db` moves its mtime and can
checkpoint its WAL. Row counts survive that; the point of a measurement run is that
it should not touch the thing it measures. `src.backup(dst)` rather than `cp`: a WAL
database copied with `cp` loses whatever is still in `-wal`, so the copy would measure
a *different dataset* than the live one.

```bash
cd <repo> && python3 - <<'PY'
import asyncio, importlib.util, os, sqlite3, sys, time
SRC = os.path.expanduser("~/.local/share/opencode/skill-usage.db")
DST = "/tmp/st-measure-copy.db"
BEFORE = os.path.getmtime(SRC)
src = sqlite3.connect(f"file:{SRC}?mode=ro", uri=True)
dst = sqlite3.connect(DST); src.backup(dst); src.close(); dst.close()

spec = importlib.util.spec_from_file_location("st_m", "scripts/skill-tui.py")
mod = importlib.util.module_from_spec(spec); sys.modules["st_m"] = mod
spec.loader.exec_module(mod)

async def sections(app, pilot):
    screen = app.screen
    await pilot.pause()
    out = {}
    for page, secs in screen._page_sections().items():
        t0 = time.perf_counter()
        for _label, render in secs:
            render()
        out[page] = (time.perf_counter() - t0) * 1000
    return out

async def full(app, pilot):
    screen = app.screen
    await pilot.pause()
    out = {}
    for page in list(screen._page_sections()):
        t0 = time.perf_counter()
        screen._refresh_pages([page])
        await pilot.pause()
        out[page] = (time.perf_counter() - t0) * 1000
    return out

async def main(which):
    app = mod.get_app_class()(db_path=DST, no_sync=True)
    async with app.run_test(size=(120, 40)) as pilot:
        return await (sections(app, pilot) if which == "sections" else full(app, pilot))

for i in range(3):
    print("ours  ", {k: round(v, 1) for k, v in asyncio.run(main("sections")).items()})
for i in range(3):
    print("framed", {k: round(v, 1) for k, v in asyncio.run(main("full")).items()})
print("live db mtime moved?", os.path.getmtime(SRC) != BEFORE)
PY
```

2026-10-09, milliseconds per page, three runs each. The table is the **system
interpreter** (`python3` 3.14.4); the same script under `.venv/bin/python` (3.13.14)
came out 1.3–1.4× slower (`tab-dash` 7.7–12.1, `tab-plugins` 16.1–21.8, everything
else within a millisecond), which matters because the launcher uses the venv — quote
the interpreter with the number or the two runs look like a regression:

| Page | Our section renders only | Whole `_refresh_pages` + one Textual frame |
|---|---|---|
| `tab-dash` | 5.9–7.3 | 61.2–83.7 |
| `tab-skills` | 1.8 | 56.1–82.0 |
| `tab-mcp` | 0.9–1.2 | 37.6–41.8 |
| `tab-plugins` | **15.6–15.8** | 78.2–95.5 |
| `tab-recent` | 3.8–4.0 | 57.2–99.3 |
| `tab-cats` | 0.8 | 43.8–55.6 |
| `tab-agents` | 1.4–1.5 | 43.1–56.6 |
| `tab-data` | 0.5 | 52.3–54.5 |

Read the two columns differently.

- **Left column is ours.** It is the number a maintenance decision can act on:
  `tab-plugins` is 4–30× the others, and §6's 12.2–14.0 ms accounts for essentially
  all of that gap — i.e. the cost is the *neighbour read*, not the queries. If a
  future reading shows `tab-plugins` still ~15 ms while §6 has dropped to ~2 ms, then
  our own query on that page has become the problem and that is worth a fix.
- **Right column is Textual's frame plus ours.** The 40–100 ms floor on every page,
  including the 0.5 ms one, is `pilot.pause()`'s layout pass. It is an upper bound on
  what a tab switch costs, and it is **not** comparable to a click-to-paint number on
  real hardware — headless `run_test` sizes the screen artificially. Don't quote it
  as "the TUI takes 60 ms"; quote it as "the refresh path plus one frame costs
  37–99 ms in this harness".
- The `live db mtime moved? False` line is part of the recipe for the same reason the
  sha256 is in §5: it proves the run stayed on the copy. The first time I ran this
  today I ran it against the live database, the mtime moved, and that side effect is
  why the warning is here.
- **Do not reconcile this table with MAINTENANCE §6's "~70 ms page re-read / ~125 ms
  Textual floor".** That reading was a Dashboard→Skills *tab switch* on a 44-skill
  database, measured against the animation cost, and its "re-read" figure includes the
  frame and the row fitting. This table isolates the two halves on purpose, and the
  left column is an order of magnitude smaller — which is the useful fact: our code is
  not what a tab switch pays for. The two numbers answer different questions, so the
  action they imply is different too.

For an interactive look at the same snapshot rather than a timed one, the copy opens
in the real TUI with `python3 scripts/skill-tui.py --db /tmp/st-measure-copy.db`
(`--db` is one of the flags `parse_args()` accepts, and an absent `command` is what
means "interactive"); `skillt` with no subcommand always takes the live database.

---

## 8. Backups, retention and rotation: state, not opinions

```bash
BACKUPS="$HOME/.local/share/opencode/backups"
python3 - "$BACKUPS" <<'PY'
import datetime, os, sys
d = sys.argv[1]
names = sorted(n for n in os.listdir(d) if n.endswith(".db"))
now = datetime.datetime.now(datetime.timezone.utc)
print(f"{len(names)} backup(s) in {d}")
for n in names:
    st = os.stat(os.path.join(d, n))
    age = now - datetime.datetime.fromtimestamp(st.st_mtime, datetime.timezone.utc)
    print(f"  {n}  {st.st_size:,} B  {age.total_seconds()/86400:.1f} d old")
PY
```

2026-10-09: 6 backups, newest **2026-10-07 00:18 (3.0 d)**, sizes 111 KB–586 KB.
`doctor`'s `backups.latest` threshold is 7 days, so a 3.0 d reading is PASS with room.
The ages are what the timer's `Persistent=true` is for: a machine that was off across
00:09 shows up here as a large `age`, not as a missing row.

Then the two read-only plans. **Both are dry-run by default and print a count before
touching anything**; neither needs the `--yes` flag to be worth running, and the way
to run them without risk is to run them exactly as written:

```bash
skillt auto-backup --dry-run              # what would be created, what would be pruned
skillt prune-usage                        # dry run: retention is off, so it says so
skillt rotate-log                         # dry run: below the cap, so nothing moves
```

`--dry-run` on the first line is **not optional**: `auto-backup` is the one command in
this family whose default *writes* — it creates a backup and then prunes per
retention, because that is what the systemd unit runs. `prune-usage` and `rotate-log`
default to dry-run and need `--yes` to mutate; `auto-backup` defaults to doing the
thing and needs `--dry-run` to only report it.

2026-10-09 those three answered, in order: the retention plan against the 6 backups
above with `kept: 6, delete: 0`; `retention.usage_days` and
`retention.max_skill_versions` are 0, so `nothing is scheduled`; and `below the cap;
nothing to rotate` with the log's sha256 unchanged after the run.

`skillt prune-usage --keep-days N --json` is the way to *trial* a window without
committing to it: it reports rows by table and `deleted.total = 0` unless `--yes` is
present. Rows whose timestamp does not parse are **kept** and reported under
`undated` — a bare `timestamp < cutoff` would read an empty string as "ancient" and
delete them, which is what
`test_undatable_rows_are_reported_and_never_deleted` (`test_row_retention.py`) holds.

---

## 9. `skillt doctor`: the aggregate reading

```bash
skillt doctor; echo "exit $?"
```

2026-10-10: **17 PASS, 4 WARN, 0 FAIL, exit 0** — on the venv interpreter it reports
`env.python 3.13.14 (want >= 3.11)`. The day before, the same command reported
**17 PASS / 3 WARN**; the added line is `backups.scheduled`, and it appeared because
the check was written, not because the machine changed.

The four WARNs, and which of them is not a finding about this project:

| Check | Today's line | What it means |
|---|---|---|
| `backups.scheduled` | `skillt-auto-backup.timer: not-found — nothing runs auto-backup or rotate-log on a schedule; ./install.sh --with-timer arms it` | **the finding this file exists for.** The unit files are gone from every systemd directory and `journalctl --user -u skillt-auto-backup.timer` ends at `Stopped … 2026-10-07 21:48:34`, while `backups.latest` on the same screen reads PASS at 3.0 days. A recent file and a live schedule are two facts; only the second one produces the next backup |
| `claude_mem.capture` | 1802 observations, newest 0.1 d, **16 worker-log ERROR lines**, worker up :37700 | the *neighbour's* worker is erroring. It is not our capture: the WARN is the reason the line exists. Compare §6's reading — the ERROR count is the same fact, and it moved 5 → 16 in five days |
| `log.errors` | 4 error lines; last `2026-10-04T07:32:25.031Z [err] selftest FAIL: …` | residue from *our own* test runs, dated before the log-isolation fix (§5). Honest history, deliberately not hand-deleted |
| `env.opencode_version` | `` `opencode --version` unavailable (not on PATH?) `` | **an instrument fault, not host drift.** This shell has no `opencode` on PATH. It previously read "1.18.35 vs the 1.18.34 pin", which *was* drift. Same WARN, different reason — read the message, not the check name |

That last row is the reason this section exists. A WARN's *name* is stable while its
*reason* is not, and a maintenance note that records "the version WARN" without the
message will be wrong the next time it fires. `MAINTENANCE §0` therefore cites the
line text.

To tell a PASS about this machine from a PASS about the code, run doctor with the
skills directory pinned:

```bash
OPENCODE_SKILL_TRACKER_SKILLS_DIR=/tmp/does-not-exist skillt doctor
```

2026-10-10 that changed **17 PASS / 4 WARN** to **16 PASS / 5 WARN**:
`skills.frontmatter_names` (which scans the directory) leaves and `skills.dir`
`missing: /tmp/does-not-exist` appears. Two readings worth keeping from that diff:

- `skills.count` stayed **44**. It is `SELECT COUNT(*) FROM skills` — a reading of the
  database, not of the directory — so pinning the directory cannot falsify it. A count
  that looks environment-proof usually is not; check which table the line queries.
- `plugin.exists` also stayed PASS, because it stats `CONFIG_DIR/plugin/skill-tracker.js`,
  which the skills pin does not move.

The other two files in this directory split the remaining ground:
[`compat-matrix.md`](compat-matrix.md) is the pins, [`opencode-interface.md`](opencode-interface.md)
is the surface we depend on by name, and this file is everything that only a run can
answer.

---

## 10. The suite, and the run that proves the suite is hers

Three commands, in this order, all four phases of the day. The first two are the
documented install paths and must both be green before anything is pushed; the third
is the one that says the green was not bought with this machine's real files.

```bash
python3 -m pytest scripts/tests -q                                  # system interpreter
.venv/bin/python -m pytest scripts/tests -q                         # the venv the launcher uses
OPENCODE_SKILL_TRACKER_SKILLS_DIR=/tmp/does-not-exist \
  python3 -m pytest scripts/tests -q                                # hermetic proof
```

2026-10-09: **649 passed** on each of the three, **0 skipped**, with textual 8.2.8
installed in *both* interpreters. On CI the same suite reported **489 passed /
48 skipped** at that run (`37722605159`, 2026-10-08T03:26Z) — the skips were `requires_bun`
and the dispatcher test, and the pin there is Python 3.12; see
[`compat-matrix.md`](compat-matrix.md) §1.

measured 2026-10-10 on python3 3.14.4 (+ `.venv` 3.13.14), textual 8.2.8, the three
commands above, one per run so the seconds mean something — **664 passed, 0 skipped**
on each (105.91s / 117.18s / 108.47s at `9fb3648`, then 111.19s / 120.42s / 107.24s at
`5c6fa1f`) — the totals are equal, which is the point of the third command.

**CI on the pushed commit `5c6fa1f`: `614 passed / 50 skipped`, conclusion `success`,
1m34s, Python 3.12** (run `38025361248`; read with
`gh run view 38025361248 --log | grep "passed"`). The two skip classes were then
*counted*, not assumed, and they close:

```bash
env PATH=/usr/bin:/bin python3 -m pytest scripts/tests -q -rs -p no:cacheprovider
#  637 passed, 27 skipped   — the 27 are `requires_bun`: 25 test_plugin, 1 test_compat,
#                             1 test_subagent (bun lives at ~/.bun/bin, off the stripped PATH)
python3 -m pytest scripts/tests/test_dispatcher.py -q --collect-only
#  23 tests collected       — the file skips whole when ~/.local/bin/skillt is absent
```

27 + 23 = **50**, and 614 + 50 = **664** = the local total. So CI's green is the same
664-test suite with exactly those two classes switched off, nothing else skipped, and no
test that passes on the runner is one that cannot run here.

**Both CI readings line up with the local collection at their own commit.** The run
before this one (`37722605159`) reports `headSha` **`b57d6b0`** — this round's *starting*
commit — and its `489 passed / 48 skipped` sums to **537**, which is exactly the local
L0 baseline recorded at `b57d6b0`. So the 38 commits of this round had **no CI coverage at
all** until `5c6fa1f` was pushed, and the two totals agree across the two machines at both
ends of the range. `gh run view <id> --json headSha,conclusion,createdAt` is how that was
read, not inferred from the run list.

**Do not write this count into a document.** It is the third kind of number (§0 rule
3): a run, with a Python version, a plugin set and a `bun` binary behind it. AGENTS.md
bans copying aggregate counts for exactly this reason; the sentence above is allowed
because it is in a file whose whole subject is dated readings.

If a run reports fewer passes than the last, the diff to look at is
`git log --oneline -- scripts/tests`, not the number.

---

## 11. Recording a reading

When a number from this file goes into another document, or into a maintenance note,
write it in this shape:

```
measured 2026-10-09 on 1.18.35 / bun 1.3.13 / python3 3.14.4 (+ .venv 3.13.14),
textual 8.2.8, <command or §-number of this file> — <the number> — <what it is a
bound of, if it is a bound>
```

The three parts that are easy to skip and expensive to lose: **which command** (so it
can be re-taken), **which interpreter** (the 12 ms and the 16 ms readings are different
machines' numbers), and **whether it is a bound** (a truncated read is a floor;
`74.4 lines/day` is a mean over a span that includes a two-day gap).

A reading that cannot be traced to a command in this file is not a measurement. Add
the command here first, run it, then quote it.
