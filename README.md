# OpenCode Skill + MCP + Plugin Tracker (`skillt`)

![OpenCode](https://img.shields.io/badge/OpenCode-1.18.x-blue)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![SQLite](https://img.shields.io/badge/storage-SQLite-003B57?logo=sqlite)
![License: MIT](https://img.shields.io/badge/license-MIT-green)
[![Repository](https://img.shields.io/badge/repository-GitHub-black?logo=github)](https://github.com/SHADE-glitch/opencode-skill-tracker)

Record and query how every skill, **every MCP tool** and **every plugin tool /
command** in [OpenCode](https://opencode.ai) is actually used: what ran, when,
whether it succeeded, how long it took, and in which project. Everything lands in
a single local SQLite file — no network, no upload, no conversation content.

**English** · [简体中文](README.zh-CN.md)

---

## 🤔 Why

OpenCode has no dedicated skill hook. This project recognises skill usage
indirectly, through the generic tool hooks:

- OpenCode exposes skills as a tool named `skill` with input `{ name }`;
- MCP tools are exposed as `{server}_{tool}` (e.g. `basic-memory_read_note`)
  and go through the **same** generic tool wrapper, so they are observed
  through the same hooks;
- plugin-provided tools go through that wrapper too, and plugin commands fire
  `command.execute.before`;
- the plugin `skill-tracker.js` listens on `tool.execute.before` /
  `tool.execute.after` / `permission.ask` / `command.execute.before` / `event` /
  `chat.message`;
- when the tool is `skill` the call is written to `skill_usage`; when it belongs
  to a known MCP server it is written to `mcp_usage`; anything else that is
  neither skill nor builtin is written to `plugin_usage`;
- `skillt` queries, reports, exports, backs up and health-checks that data.

Design constraints, deliberately kept:

- **No OpenCode source changes** — public plugin hooks only.
- **No instrumentation in `SKILL.md`** — nothing to add to your skills.
- Only structured metadata is stored (tool name, status, duration, project
  path, session, model/agent/branch). **No secrets, no message bodies.** For
  MCP calls, only the argument *key names* are recorded — values are never read.
- One plugin file + one shared data layer + one entry point, minimal deps.

---

## 📋 Requirements

| | |
|---|---|
| OS | Linux — developed and verified on Ubuntu 26.04; other distributions are **unverified** |
| OpenCode | 1.18.x — allowlist verified against 1.18.34; the permission payload shapes were measured on 1.18.33 and have not been re-measured since (M20) |
| Python | 3.11+ for the CLI and TUI (tested on 3.11, 3.13, 3.14) |
| Bun | bundled with OpenCode — used to run the plugin; only needed for the plugin self-test |
| `uv` | optional but recommended for creating the venv; plain `python3 -m venv` also works |

The headless subcommands (`insight`, `health`, `doctor`, `export`, `sync`,
`mcp`, `plugins`, `agentos`, `claude-mem`, `auto-backup`) need **only the Python
standard library**. The
interactive TUI needs [Textual](https://textual.textualize.io/)
(`textual>=8.2,<9`), installed into a venv by `install.sh`.

---

## 📥 Install

### Out of the box

```bash
git clone https://github.com/SHADE-glitch/opencode-skill-tracker.git
cd opencode-skill-tracker
./install.sh
```

Then **restart OpenCode** so it picks up the plugin, and verify:

```bash
skillt doctor      # PASS / WARN / FAIL report; exits 1 if anything FAILs
skillt insight     # usage summary — works without the TUI
skillt             # launch the interactive TUI
```

`install.sh` is idempotent. It:

1. creates `./.venv` and installs `requirements.txt` into it;
2. replaces the four install locations with **symlinks pointing back at this
   repo**, so `git pull` here updates the live install:

   | Install location | Points at |
   |---|---|
   | `~/.config/opencode/plugin/skill-tracker.js` | `plugin/skill-tracker.js` |
   | `~/.config/opencode/scripts` | `scripts/` |
   | `~/.config/opencode/skill-tracker` | `skill-tracker/` |
   | `~/.local/bin/skillt` | `bin/skillt` |

   If a location already holds a real file or directory, the script **refuses to
   overwrite it** and tells you what to move. Pass `--force` to replace it.

3. runs `skillt doctor` once and prints the next steps.

> Make sure `~/.local/bin` is on your `PATH`. If it is not, add
> `export PATH="$HOME/.local/bin:$PATH"` to your shell profile.

### Manual install

Same thing, spelled out — useful if you prefer to place the repo elsewhere:

```bash
git clone https://github.com/SHADE-glitch/opencode-skill-tracker.git
cd opencode-skill-tracker

# 1. venv for the TUI
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python -r requirements.txt

# 2. point the install locations at this repo
#    (-n on directory links: don't create a link *inside* an existing dir)
mkdir -p ~/.config/opencode/plugin ~/.local/bin
ln -sf  "$PWD/plugin/skill-tracker.js" ~/.config/opencode/plugin/skill-tracker.js
ln -sfn "$PWD/scripts"                 ~/.config/opencode/scripts
ln -sfn "$PWD/skill-tracker"           ~/.config/opencode/skill-tracker
ln -sf  "$PWD/bin/skillt"              ~/.local/bin/skillt
```

Remove any pre-existing real `~/.config/opencode/scripts` or
`~/.config/opencode/skill-tracker` directory first (back it up), otherwise
`ln -sfn` would nest a link inside it.

### Headless-only install

No Textual, no venv — just the recorder and the CLI:

```bash
mkdir -p ~/.config/opencode/plugin ~/.local/bin
ln -sf  "$PWD/plugin/skill-tracker.js" ~/.config/opencode/plugin/skill-tracker.js
ln -sfn "$PWD/scripts"                 ~/.config/opencode/scripts
ln -sf  "$PWD/bin/skillt"              ~/.local/bin/skillt
```

Running bare `skillt` will then print how to create the venv; every other
subcommand works.

---

## 🧭 Usage

### Interactive TUI

```bash
skillt
```

Needs a real terminal (`stdin`/`stdout`/`stderr` all TTYs, and `TERM` neither
empty nor `dumb`). Pages: **Dashboard / Skills / MCP / Plugins / Agents /
Recent / Categories / Data** (Data is always last).

The six Dashboard cards, each labelled below its number:

| Card | Meaning |
|---|---|
| `Skills` | total skills known (rows in `skills`) |
| `Skill calls` | total recorded skill invocations (rows in `skill_usage`) |
| `MCP calls` | total recorded MCP tool calls (rows in `mcp_usage`) |
| `Plugin calls` | total recorded plugin calls (rows in `plugin_usage`) |
| `Today (all)` | skill + MCP + plugin calls today (local calendar day) |
| `Skill success` | overall skill success rate |

Below the cards a legend line breaks `Today (all)` down by kind and shows the
observed sample (sessions and days). The three counters are kept separate, so
`Skill calls` never silently includes MCP or plugin traffic.

`Last 7 days` shows three side-by-side charts — skill, MCP and plugin calls —
each scaled to its own peak (MCP/plugin volumes are usually an order of
magnitude smaller). Every day line is a fixed 20 characters and every chart is
pinned to the same height, so the three always sit on **one horizontal line**:
an un-padded count used to make only the chart that had a large number
word-wrap, and its day rows fell out of step with its neighbours. Below 70 columns the row no longer
fits (measured: the cells are exactly 20 wide at 70, and 18/19/19 at 66) and all
three clip alike instead of staggering. Counts beyond five
characters are compacted (`123456` → `123k`), which is what keeps the row a
constant length. Below them, top-10 tables mirror the three kinds: **Top
Skills**, **Top MCP tools** and **Top Plugins**.

The **MCP** page lists one row per `(server, tool)` pair with call count,
30-day calls, sessions, success rate, average duration and last use. Each of
the Skills / MCP / Plugins tables has its own sort mode and filter box — `s`
cycles the sort of whichever page is active, and `Enter` opens the row's
detail page. The **Recent** page merges all three kinds into one timeline;
`Enter` routes each row to its skill/MCP/plugin detail page. Switching to a
page re-reads **only that page** (and `r` re-reads every page), so each page
prints its own `data as of HH:MM:SS` instead of one global timestamp.

Switching pages is instant rather than animated. Textual slides the tab-bar
underline for 0.3 s on every switch, which measured ~305 ms of a ~500 ms
Dashboard→Skills switch on a 44-skill database — more than the page's own
re-read (~70 ms) and Textual's layout/render floor (~125 ms) combined — so the
app disables animations (`animation_level = "none"`) and the underline snaps
into place, bringing the same switch to ~190 ms.

The **Plugins** page lists a registered surface even when it has never been
called: `total` comes from the union of what the scan saw at OpenCode's start and
what has usage, so a `0` means nobody used that tool — not that the tracker
missed it. The footer says how many rows are in that state. `Enter` on one of
them warns that there is no history rather than pushing an empty detail page, and
`skillt plugins --json` carries both claims as `registered` and `ever_called` so
a consumer cannot merge them into one zero either.

The **Agents** page answers "which agent did this work" with one row per agent
and its calls split by kind — `Skill`, `MCP`, `Plugin`, `Total`, `Spawned`,
`Success rate`, `Last used`, most calls first. Read its footer before believing
it: the agent shown is the one the row's **session first reported**, not the one
that ran that call, and a call recorded before its session named an agent stays
in `(unknown)` forever. That bucket is printed with its own count instead of
being hidden (see M23). `s` deliberately does nothing here — the page has no sort
of its own to cycle.

`Spawned` and `Ran as` sit after `Total` and are deliberately outside it. They are
two facts, not one count: `Spawned` is how many subagents this agent started,
`Ran as` is how many times this name ran as somebody else's subagent (such a row
carries `⟨sub⟩`). A subagent that spent its whole run shelling and editing leaves no
usage row anywhere — builtins are not what this tracker measures (M13) — so the
spawn record is the only place it can appear, which is why it gets a row rather
than a footnote. Its `Success rate` is over its runs, because that is the only
outcome it has; `Total` still means skill + MCP + plugin on every row, including
one whose Total is 0.

A name missing from `Ran as` did not run **since OpenCode last started**, not never:
`subagent_usage` begins where the writer begins (M24). Recovering the earlier
history would mean reading OpenCode's own database; that is deliberately not done
here and is recorded as deferred in `MAINTENANCE.md` §8.

There is **no Advisor page**: the tab was removed on 2026-10-03 at the owner's
request. The read-only aggregation it showed still exists headless as
`skillt agentos` (see Commands below), and that command is now the only surface
that opens that store — no page of the TUI reads it, which is pinned by
`test_tui_never_reads_the_advisor_store`.

| Key | Action |
|---|---|
| `Tab` | switch page |
| `↑` `↓` / `j` `k` | move cursor |
| `Enter` / double-click | open the selected row's detail page (skill / MCP / plugin) |
| `/` | jump to the Skills page and focus search |
| `s` / `ctrl+s` | cycle sort (uses ↓ / recently used ↓ / success rate ↓ / name ↑) |
| `Esc` | clear search and unfocus |
| `r` / `ctrl+r` | refresh |
| `q` | quit |
| `d` | delete the skill selected **on the Skills page** (confirms first) |
| `b` `e` `v` | backup / export / vacuum |

> While the search box has focus, plain letters are consumed by it — that is why
> `ctrl+s` (sort) and `ctrl+r` (refresh) are **always available** without pressing
> `Esc` first.

The Data page also has buttons for backup, vacuum, export, **Health**, and
clearing usage. Deleting a skill is possible **only** from the Skills page.

The key table above is the human-facing copy; the **Data page prints the same
promise from the code**. `MainScreen.key_help()` lists every binding the footer
hides (`show=False` — `j` `k` `d` `b` `v` `e` `ctrl+r` `ctrl+s`), generated from
`BINDINGS`, so a new binding documents itself and a removed one cannot keep a line
in the help. `test_a_new_binding_documents_itself_in_the_help` adds a binding and
requires it to appear.

Three things a page now says about itself, because each of them previously read as
a broken tool:

- **An empty table names its cause.** Nothing recorded yet (`press r to re-read`,
  the writer adds a row on the next call), a search hiding every row (`N skills
  hidden`, clear the box), or a skills directory that has nothing in it — and the
  directory's path, since "no skills" and "I looked in the wrong place" otherwise
  look identical.
- **A page's status block is one colour.** Every line under a table is a single
  dimmed span, so nothing in it reads as two kinds of information at once.
- **A table wider than the terminal says so.** `→` scrolls the trailing columns
  into view; what was missing was the admission that they exist, so the status line
  prints how many columns sit past the right edge.

**Terminal widths.** Measured at the sizes named, not read off the CSS. The card
grid comes out 4 columns narrower than the screen and Textual splits the remainder
unevenly, so the *narrowest* card is what a label must fit: six cards across needs
**111** columns, three needs **57**, and two is the floor — below **40** the longest
label (`Skill success`, 13 columns) wraps, and it does so deliberately rather than
the page doubling in height. The confirm dialog is `width: 100%, max-width: 60`,
which keeps it inside the screen at every width; at 36 columns its two buttons still
fit, and that is as narrow as this was tested.

### Headless subcommands (no Textual needed)

```bash
skillt insight [--days N] [--min-uses N] [--limit N] [--json]
skillt export  [--out FILE] [--pretty] [--force] [--skills-only]
skillt sync    [--dry-run] [--prune-orphans] [--json]
skillt health  [--json] [--limit N]
skillt mcp     [--json] [--limit N]
skillt plugins [--json] [--limit N]
skillt auto-backup [--dry-run] [--json]
skillt doctor  [--json] [--freshness-days N]
skillt cleanup-selftest [--yes]
skillt scrub-metadata [--yes] [--json] [--limit N]
skillt prune-usage [--keep-days N] [--keep-versions N] [--yes] [--json]
skillt agentos   [--json] [--limit N]
skillt claude-mem [--json] [--days N]
skillt config [list|get|set|unset|path|explain] [<key> [<value>]] [--json]
skillt rotate-log [--max-bytes N] [--keep-files N] [--yes] [--json]
```

- `insight` — most used / fastest growing / **never used** / dormant / highest
  failure rate, plus the observed sample (sessions and days).
- `export` — dump JSON (mode 0600); prints to stdout without `--out`.
- `sync` — rescan `SKILL.md` files, recording content changes in
  `skill_versions`; `--dry-run` counts without writing.
- `health` — buckets every skill into **active (≤30d) / dormant (30–90d) /
  unused (>90d or never)** with risk flags and suggestions, and reports the
  observed sample. Usage-based pruning suggestions are **suppressed** until at
  least 20 sessions over 14 days have been observed — below that, "0 uses"
  means nothing. **Read-only, advisory only, never deletes.**
- `mcp` — MCP tool usage: per-server roll-up plus the top tools by call count,
  success rate and last use. **Read-only.**
- `auto-backup` — snapshot into the backup directory and prune by retention
  policy (see [Backups](#-backups)).
- `doctor` — health check printing PASS/WARN/FAIL; **exit code 1 if any FAIL**.
  Besides the structural checks (database, skills, plugin file, environment,
  backups) it checks the **capture pipeline itself**: `capture.freshness` (the age
  of the newest row **of each stream separately** — `skill_usage 0.0d · mcp_usage
  2.2d · plugin_usage 0.0d` — against `--freshness-days`, default 7; a stream that
  never recorded a row is reported as `no rows` and is not treated as stalled, and
  `OPENCODE_SKILL_TRACKER_STREAMS=skill,plugin` excludes a stream from the verdict
  while still printing its age), `log.errors` (count of `[err]` lines in the plugin's own log, with
  the last one quoted) and `env.opencode_version` (installed version versus the
  one the builtin-tool allowlist is pinned to, see M13). Those three **WARN,
  never FAIL** — a quiet week is not a fault.
- `cleanup-selftest` — remove synthetic rows left by `__selftest()`
  (`project_path = /tmp/selftest-proj`). Dry-run by default; `--yes` deletes for
  real (after taking a backup).
- `scrub-metadata` — strip free-text keys that must not be stored
  (`summary`, `title`) from historical `metadata` rows. **The rows themselves
  are kept** — usage is the point of this database, leaked prompt text is not.
  Dry-run by default and lists what it would touch; `--yes` applies (backup
  taken first, then the WAL is checkpointed so the text really leaves the disk).
  See M14.

- `claude-mem` — the **claude-mem** plugin's own ledger, read-only: how many
  observations, prompts and sessions it holds, how fresh its newest row is, its
  discovery-token totals, and the background observer's failure counter — beside
  what *this* tracker measured over the same window. Counts and timestamps only:
  the prose columns of that store, and its `settings.json` (which holds its API
  keys), are never read. See M22. Found at its default path when installed;
  otherwise set `OPENCODE_SKILL_TRACKER_CLAUDE_MEM_DB` or `CLAUDE_MEM_DIR`.
  `skillt doctor` says nothing about it when the store is absent.

  It reads three more of claude-mem's own files, in this order: `inject-trace.log`
  (what its hook injected — the query text after `q=` is **counted, never read**,
  and the counts are grouped by `project=` and by **local** day, with anything
  ungroupable counted rather than dropped),
  `logs/claude-mem-<date>.log` (line counts per level, which is where the day's
  ERROR total comes from) and `worker.pid` (liveness and port, so nothing here
  hardcodes `37700`). Only after the files does this one command ask the running
  worker three HTTP questions (`/api/stats`, `/api/processing-status`,
  `/api/chroma/status`) under **one shared deadline, not one timeout each**, and it
  keeps only integers and booleans from the answers — `database.path`,
  `worker.version` and the free-text `details` are dropped. None of it is written
  into the tracker's tables, and none of it happens inside the TUI: the Plugins
  page prints the same log-file numbers as one dim line and never opens a socket.
- `rotate-log` — keeps the plugin's own log from outrunning the reader (M17). It
  **renames** the file rather than emptying it: every line that leaves the live path is
  still on disk in `skill-tracker.log.<UTC stamp>`, because those lines are the evidence
  `doctor log.errors` reads. The cap is `log.max_bytes`, which is the same 4 MiB the
  reader reads, so the two cannot drift apart; `log.keep_files` rotations are kept and
  anything older is pruned (only names matching this rotation shape, never a neighbour's
  log — the command takes no path argument). The live file is re-created empty rather
  than left missing, because a missing log makes `doctor` WARN about a plugin that is
  running fine, and it is created without `O_TRUNC`: the writer appends per line, so a
  line can land between the rename and the re-create. Dry-run by default; the backup
  timer runs it with `--yes`. Needs **no database** — on a fresh machine the log exists
  first. A second rotation inside the same second gets a `-2` suffix instead of renaming
  onto the copy already there — that would lose a generation of the log in silence, and
  it is the opposite of `auto-backup`, where a same-second name is an error.
- `prune-usage` — the only command here that **deletes recorded history**. Off by
  default: `retention.usage_days` and `retention.max_skill_versions` are both `0`, and
  with either at 0 nothing is scheduled. It lists the rows it would remove and deletes
  them only with `--yes`, which writes a backup first and aborts if that backup fails;
  the delete is one transaction. The count in the dry run comes from the same SELECT the
  delete uses, so it is a measurement and not a prediction. Rows whose timestamp will not
  parse are **kept** and reported as `undated` — "older than N days" is a claim about a
  date we do not have. `subagent_usage` is never in the delete set: those rows are events,
  not calls, and the spawn record is the only witness that a subagent ran (M24). Deleting
  rows frees pages; the file does not shrink until `skillt vacuum`.
- `agentos` — the **AgentOS advisor**, aggregated read-only from its own store.
  The advisor registers no tool and no command, so it can never appear in the
  usage tables above: it produces **zero usage rows** there, and
  `plugin_inventory` carries a single row for it with both the tool list and the
  command list empty. This reads its `store/aos.db` + `store/loops/*.json`
  instead, and it is now the only surface that does — the TUI has no Advisor
  page.
  Requires `AGENT_OS_ROOT` (or
  `OPENCODE_SKILL_TRACKER_AGENTOS_DB`) and reports plainly when neither is set.
  Per loop it shows the stage statuses and timings, the slowest stage against
  the advisor's per-call budget, how much measured tool activity that same
  session produced (the join), and whether the recalled memory actually reached
  the prompt — `searched 3 / recalled 3 / reached the prompt 4 (1234 chars)`,
  counted from the advisor's own result numbers. `memory_ids` and
  `injected_memory_ids` are kept apart because
  a hypothesis can be injected on its own, so collapsing them would hide that. **Nothing is ever written to that
  store**, and no field is ever read that could carry text: the projection names
  every emitted field, so a loop's `task_text` and the engines' stage payloads
  stay where they are. See M21.

Argument validation: `--days ≥ 1`, `--min-uses ≥ 0`, `--limit ≥ 1`,
`--freshness-days ≥ 1`; invalid values fail fast with exit code 2. Every
headless subcommand also works when stdout is **not** a TTY (pipes,
redirection), emitting plain text or JSON.

### Legacy CLI (delegates to `skill-stats.py`)

```bash
skillt stats | top [N] | show <skill> | recent [N] | delete <skill> | clear | backup | vacuum
skillt help
```

Command names, positional arguments (`top 3`) and `--json` fields are unchanged
from the original CLI. `top` / `recent` accept either a positional count or
`--limit 3`. `show <skill> --limit N` controls history depth (default 20). The
only visible change is that **output text is now English**, for consistent logs
and script parsing.

---

## ⚙️ Settings

Zero configuration is a supported configuration. Every key has a default, and the
defaults *are* the behaviour the screens already show — nothing here is needed for
the plugin, the CLI or the TUI to work, and the file does not exist until you make
it.

**Precedence: flag → config file → environment → default.** The file is
`~/.local/share/opencode/skillt-config.json`: the tracker's own data directory,
never `~/.config/opencode/`, which belongs to OpenCode. Change it with the CLI
rather than by hand, because the CLI validates and reports where each value came
from:

```bash
skillt config list                    # every key: value, origin, and what it does
skillt config get view.days
skillt config set view.days 45
skillt config unset view.days
skillt config explain view.min_uses   # default, minimum, flag, env name, both languages
skillt config path
skillt config list --json
```

| Key | Default | Flag | Environment | What it changes |
|---|---|---|---|---|
| `view.days` | `30` | `--days` | `OPENCODE_SKILL_TRACKER_VIEW_DAYS` | how many days the headless reports cover (`insight`, `claude-mem`). The interactive screens are pinned to fixed windows — 7-day trends, 30-day totals — so this does not stretch them |
| `view.limit` | `10` | `--limit` | `OPENCODE_SKILL_TRACKER_VIEW_LIMIT` | how many rows `skillt insight` lists per ranked table. The interactive tables hold every row and scroll |
| `view.recent_rows` | `100` | `--recent-rows` | `OPENCODE_SKILL_TRACKER_VIEW_RECENT_ROWS` | how many events the Recent timeline lists. Older events stay in the database; only what the screen holds changes |
| `view.min_uses` | `3` | `--min-uses` | `OPENCODE_SKILL_TRACKER_VIEW_MIN_USES` | how few calls make a skill "worth advising about" in `skillt insight` |
| `doctor.freshness_days` | `7` | `--freshness-days` | `OPENCODE_SKILL_TRACKER_DOCTOR_FRESHNESS_DAYS` | how old the newest recorded call may get before `doctor` warns that capture looks stalled — and the same clock the claude-mem line is judged by |
| `log.max_bytes` | `4194304` | `--max-bytes` | `OPENCODE_SKILL_TRACKER_LOG_MAX_BYTES` | when the plugin's own log passes this size `skillt rotate-log` moves it aside. It is the same number the reader uses (`TRACKER_LOG_BYTES_CAP`) on purpose: a line in the active log that `doctor` cannot see would be a silent loss. Floor 1024 — a smaller cap would rotate on the first line |
| `log.keep_files` | `5` | `--keep-files` | `OPENCODE_SKILL_TRACKER_LOG_KEEP_FILES` | how many rotated logs stay beside the live one. Rotation is a rename, so the lines move rather than disappear; this is how far back that history goes |
| `retention.usage_days` | `0` | `--keep-days` | `OPENCODE_SKILL_TRACKER_RETENTION_USAGE_DAYS` | **`0` = off: nothing is ever deleted.** Usage rows older than this many days become the delete set of `skillt prune-usage`, which lists them first and only removes them with `--yes`, after a backup |
| `retention.max_skill_versions` | `0` | `--keep-versions` | `OPENCODE_SKILL_TRACKER_RETENTION_MAX_SKILL_VERSIONS` | **`0` = off.** Keep at most this many `skill_versions` rows per skill — the newest N, so retention cannot eat the current content. The table gains a row per content change and never loses one |
The environment name is **derived by rule**: uppercase the key, replace `.` with
`_`, prefix `OPENCODE_SKILL_TRACKER_`. There is no second list to keep in step, and
`scripts/tests/test_settings_documented.py` fails the build if a key, its flag or
its default is missing from either README — the gate whose absence let six
variables ship undocumented.

**A refused value is reported, never hidden.** A file saying `"view.days": "thirty"`
still shows 30 on screen, and `skillt config list` prints that key's origin as
`config file (rejected)` with the reason; `skillt doctor` carries a matching
`config.file` WARN. Silent fallback is how a settings layer stops being believable.

**A file that does not parse is never rewritten.** `skillt config set` against a
broken or unknown-key file refuses with exit 2 instead of rebuilding it from the
lines that happened to parse. The typo is your evidence, and it is one line to fix.
Keys are `group.key`, one level; an unrecognised key is named and ignored, and
`skillt config set view.dayz 7` answers with the nearest real key.

What settings deliberately do **not** cover: what the recorder captures, or how
long it keeps it — those are `OPENCODE_SKILL_TRACKER_*` (table below) and code. And
the privacy invariants (purely local, no network, no message text) are not
configurable at all; a toggle for them would be a way to break them.

---

## 🔬 How it works

```
OpenCode runtime
   │  tool.execute.before/after, permission.ask, event, chat.message, chat.params
   ▼
~/.config/opencode/plugin/skill-tracker.js     (Bun runtime, bun:sqlite)
   │  writes
   ▼
~/.local/share/opencode/skill-usage.db         (SQLite, WAL, 0600)
   │  reads
   ├── skillt                → scripts/skill-tui.py    (Textual TUI, needs venv)
   ├── skillt stats|top|...  → scripts/skill-stats.py  (legacy CLI, read-only)
   └── skillt insight|health|doctor|...  → skill-tui.py --cli  (no Textual needed)
```

- **Writer**: only the plugin, only inside the OpenCode runtime. Every write is
  deduplicated by `UNIQUE(session_id, call_id)` with an `ON CONFLICT` upsert.
- **Readers**: every `skillt` command. `health` opens the database read-only and
  never modifies it.
- **Shared data layer**: `scripts/skill_db.py` — schema, migrations, queries,
  export and backup — used by both `skill-tui.py` and the test suite. It holds the
  tracker's own data and nothing else: the two read-only neighbours are
  `skill_db_agentos.py` and `skill_db_claude_mem.py`, which import it and are never
  imported by it.
- **Upstream contract**: every name OpenCode chose is written once per language —
  `scripts/opencode_compat.py` on the Python side, the `CONTRACT` block in
  `plugin/skill-tracker.js` on the writer's side (hooks are registered and events
  switched on *through* it, so each name appears once, as a value).
  `scripts/tests/test_compat.py` compares the two, so a half-finished rename fails
  the build instead of silently recording nothing. Numbers and policy that *this*
  tool chose are not there.

### Environment variables

Every name this project reads, in one table, on both sides of the documentation —
`scripts/tests/test_settings_documented.py` enumerates the environment reads out of
the source and fails if a name is missing here or from `README.zh-CN.md`. That gate
did not exist while six variables shipped undocumented.

| Variable | Effect |
|---|---|
| `OPENCODE_SKILL_TRACKER_DB` | override the database path (the plugin) |
| `OPENCODE_SKILL_TRACKER_CONFIG` | the settings file's path (readers only; default `~/.local/share/opencode/skillt-config.json`) |
| `OPENCODE_SKILL_TRACKER_CONFIG_DIR` | the OpenCode config tree (default `~/.config/opencode`) — **both sides**: the writer records the skills it finds there, the readers scan the same tree, so a one-sided override would show an empty Skills page over real data |
| `OPENCODE_SKILL_TRACKER_SKILLS_DIR` | the skills tree (default `$CONFIG_DIR/skills`) — both writer and readers |
| `OPENCODE_SKILL_TRACKER_PACKAGES_DIR` | where npm plugin sources are looked up for the static scan (default `~/.cache/opencode/packages`) |
| `OPENCODE_SKILL_TRACKER_PLUGINS` | comma-separated plugin specs; **when set, reading the installed set out of OpenCode's config is skipped** (even when empty). Nothing is pruned from the inventory under an override: it says nothing about what is installed |
| `OPENCODE_SKILL_TRACKER_PLUGIN_EXCLUDE` | comma-separated specs appended to the default exclusions (the tracker itself, `opencode-notifier`) |
| `OPENCODE_SKILL_TRACKER_TOOL` | the name of the skill tool (default `skill`), for a host that renamed it |
| `OPENCODE_SKILL_TRACKER_MCP_SERVERS` | comma-separated MCP server names; **when set, automatic detection is skipped** (even when empty) |
| `OPENCODE_SKILL_TRACKER_MCP_DISABLE` | `1` stops MCP recording while skill recording stays on |
| `OPENCODE_SKILL_TRACKER_PLUGIN_DISABLE` | `1` stops plugin tool/command recording only |
| `OPENCODE_SKILL_TRACKER_SUBAGENT_DISABLE` | `1` stops recording that a subagent was started (the builtin `task` tool). The other three streams are unaffected |
| `OPENCODE_SKILL_TRACKER_DISABLE` | `1` disables the plugin entirely |
| `OPENCODE_SKILL_TRACKER_BUILTIN_TOOLS` | replaces the builtin-tool allowlist (pinned to OpenCode 1.18.34, see M13) |
| `OPENCODE_SKILL_TRACKER_LOG` | the plugin's own log file (default `~/.config/opencode/logs/skill-tracker.log`, the one `skillt doctor` reads). `__selftest()` ignores the **default** value and writes beside its temp database instead — a failed selftest must not land in the owner's error count |
| `OPENCODE_SKILL_TRACKER_DEBUG` | `1` enables per-call debug lines in the plugin log |
| `OPENCODE_SKILL_TRACKER_BACKUP_DIR` | where `VACUUM INTO` backups are written and retention sweeps look (default `~/.local/share/opencode/backups`) |
| `OPENCODE_SKILL_TRACKER_STREAMS` | read side: comma list of streams `doctor` may call stalled (default: all three). An excluded stream is still printed, marked `(excluded)` |
| `OPENCODE_SKILL_TRACKER_CLAUDE_MEM_DB` | the claude-mem store to read (default `~/.claude-mem/claude-mem.db`) |
| `CLAUDE_MEM_DIR` | that plugin's own directory name for the same purpose; `…_CLAUDE_MEM_DB` wins |
| `AGENT_OS_ROOT` | the advisor's root, from which `store/aos.db` and `store/loops/*.json` are resolved |
| `OPENCODE_SKILL_TRACKER_AGENTOS_DB` | the advisor database directly, bypassing `AGENT_OS_ROOT` |
| `AOS_DB` | AgentOS's own variable name for the same file, tried last — the readers follow the neighbour's convention rather than inventing a third |
| `OPENCODE_SKILL_TRACKER_AOS_TIMEOUT_MS` | the advisor's per-call budget the "over budget" flag is measured against (default 1200, see M21) |
| `SKILLT_SCRIPTS` | override the `scripts/` directory the launcher uses |
| `SKILLT_VENV` | point the launcher at a venv dir or a python binary |
| `SKILLT_CONFIG_DIR` | installer only: the OpenCode config directory to link into (default `~/.config/opencode`) — for a sandboxed install test |
| `SKILLT_BIN_DIR` | installer only: where the `skillt` launcher is linked (default `~/.local/bin`) |
| `SKILLT_SYSTEMD_DIR` | installer only, and only with `--with-timer`: where the user units are copied (default `~/.config/systemd/user`) |
| `HOME` | inherited, not a knob: the writer builds every default above from it, so setting it moves the whole tree unless a specific override pins it |
| `TMPDIR` | inherited: where `__selftest()` creates its throwaway database (default `/tmp`) |
| `TERM` | inherited: the TUI refuses to start on an empty or `dumb` terminal rather than painting garbage |

### Database

- `skills` — one row per `SKILL.md`. `path` is `UNIQUE`; `name` is **not**.
  Migration adds `content_hash` for version tracking.
- `skill_usage` — one row per skill call. `UNIQUE(session_id, call_id)` makes it
  idempotent. `status ∈ {success, error, denied, ask, unknown}`;
  `trigger_type ∈ {tool_call, event_detected, permission_denied, manual}`.
  `metadata` is sanitized JSON (model / agent / branch) — the message body and
  title are never read, so they cannot be stored.
- `mcp_usage` — one row per MCP tool call, keyed by `(server_name, tool_name)`.
  Same `UNIQUE(session_id, call_id)` and status/trigger vocabulary as
  `skill_usage`. `tool_name = '*'` means only the server was known (the
  permission paths). `arg_names` is a JSON array of the call's **argument key
  names only** — argument values are never read, so they cannot be stored.
- `skill_versions` — content-hash history, `UNIQUE(skill_name, content_hash)`.
- `plugin_usage` — one row per plugin tool/command call, keyed by
  `(plugin_name, kind, item_name)` with the same `UNIQUE(session_id, call_id)`.
  A tool that no installed plugin could be resolved to lands in `(unknown)`;
  an unresolvable **command** is dropped instead (fail-closed, no command
  allowlist exists).
- `plugin_inventory` — one row per plugin seen when OpenCode started: the tool
  and command lists the static scan found, `skipped` for the excluded ones, and
  `scope` (`global` / `localdir` / `project`) naming which config claimed it.
  Retained when usage is cleared; pruned only at the next OpenCode start.
- `subagent_usage` — one row per subagent the host started, keyed by
  `(parent_session_id, call_id)`. Identifiers and durations only:
  `subagent`, `child_session_id`, `status`, `trigger_type`, `duration_ms`. The
  task text, the one-line description, the title and the subagent's own report
  are not read by the writer at all (M24). Both the hook pair and the event
  stream may report one spawn; the conflict rule fills in what the first
  observation left unknown rather than overwriting it, so it is still one row.
- Views: `v_skill_totals`, `v_skill_last30`, `v_skill_history`, plus the MCP
  analogues `v_mcp_totals`, `v_mcp_last30`, `v_mcp_history`, and the plugin
  analogues `v_plugin_totals`, `v_plugin_last30`, `v_plugin_history`.
- `skillt export` writes `schema_version = 6` (4 = metadata is allowlisted,
  5 = `plugin_inventory.scope`, 6 = the `subagent_usage` key). `skillt
  scrub-metadata` reports 0 rows on a clean database.
- `journal_mode = WAL`, `busy_timeout = 5000`, `synchronous = NORMAL`, file mode
  `0600`. The `source` (personal / open-source) column is derived from
  `category` at query time, not stored.

Query it directly:

```bash
sqlite3 ~/.local/share/opencode/skill-usage.db \
  "SELECT skill_name, total, success, errors, last_used FROM v_skill_totals ORDER BY total DESC LIMIT 10;"
sqlite3 ~/.local/share/opencode/skill-usage.db \
  "SELECT server_name, tool_name, total, errors FROM v_mcp_totals ORDER BY total DESC LIMIT 10;"
```

---

## 💾 Backups

```bash
skillt auto-backup --dry-run   # see what would happen
skillt auto-backup             # do it
```

- Snapshots are written to `~/.local/share/opencode/backups/` (0700) as
  `skill-usage-backup-YYYYMMDD-HHMMSS.db`, created with `VACUUM INTO`.
- Retention: the **last 30 daily** snapshots plus the **last 12 monthly** ones;
  everything else is pruned.
- Guardrails: only files matching the exact name pattern are touched; only that
  dedicated directory is pruned (never the live DB, exports or manual copies);
  files younger than 120 seconds are never deleted; unparsable names are never
  deleted; same-second name collisions **error instead of overwrite**; a
  `.lock` file prevents concurrent runs (stale after 10 minutes).
- Manual: `skillt backup` (legacy CLI) or `b` in the TUI. They land in the
  **same directory**, so retention reaches them too (it used to write beside the
  database, where nothing would prune or report them — see M19).

### Daily timer (optional)

```bash
mkdir -p ~/.config/systemd/user
cp ~/.config/opencode/skill-tracker/systemd/skillt-auto-backup.* ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now skillt-auto-backup.timer

systemctl --user list-timers skillt-auto-backup.timer
systemctl --user start skillt-auto-backup.service     # trigger once now

loginctl enable-linger "$USER"                        # run while logged out
```

The unit's `ExecStart` is `%h/.local/bin/skillt` because systemd's user `PATH`
does not include `~/.local/bin`, and it runs two maintenance jobs: `auto-backup` (the
retention above) and `rotate-log --yes` (the plugin's own log, M17). The same thing can
be done by `./install.sh --with-timer`, which is still opt-in — writing a unit is a
change outside this project's directory.

Neither job bounds the **database rows**. Usage rows are kept forever unless
`retention.usage_days` is set and `skillt prune-usage --yes` is run, which is a decision
nobody should make on a schedule they did not choose; and the database *file* only
shrinks under `skillt vacuum`.

### Restore

```bash
# 1. stop any OpenCode session using the DB
# 2. keep the current DB
cp ~/.local/share/opencode/skill-usage.db ~/.local/share/opencode/skill-usage.db.before-restore
# 3. overwrite it with a snapshot
cp ~/.local/share/opencode/backups/skill-usage-backup-YYYYMMDD-HHMMSS.db \
   ~/.local/share/opencode/skill-usage.db
# 4. drop stale WAL/SHM belonging to the old DB
rm -f ~/.local/share/opencode/skill-usage.db-wal ~/.local/share/opencode/skill-usage.db-shm
# 5. verify
skillt doctor
```

---

## 🔧 Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `skillt: not an interactive terminal` / `skillt: TERM is unset or 'dumb'` | Not all of stdin/stdout/stderr are TTYs, or `TERM` is empty/`dumb`. Use `skillt insight`, `skillt health` or `skillt doctor`. |
| `skillt: TUI venv not found` | Run `./install.sh` from the repo root, or `export SKILLT_VENV=/path/to/venv`. |
| No new data in the stats | The plugin only writes while OpenCode runs. Check OpenCode's log for `failed to load plugin` — if `skill-tracker.js` appears there, the plugin failed to load, no hook was registered, and the problem is **not** that skills went uncalled. Then confirm `skillt doctor` shows `plugin.exists` / `plugin.hooks` as PASS. |
| `failed to load plugin path=list error="Plugin export is not a function"` | A project-level `.opencode/opencode.json` lists a non-plugin npm package in its `plugin` array (e.g. `"list"`). Remove that entry. `path=list` means the source is the config's plugin list, not the `plugin/` directory. |
| `doctor` reports `db.wal` FAIL | The file is corrupt or not SQLite. Confirm with `sqlite3 ... "PRAGMA integrity_check;"`, then restore from a backup. |
| Timestamps look shifted | Day buckets follow the local calendar. Older rows written before that fix are still stored in UTC. |
| Mojibake / `UnicodeEncodeError` | Non-UTF-8 locale. Set `LANG=C.UTF-8` or `LC_ALL=C.UTF-8`. |
| `backups.latest` WARN | `auto-backup` has never run, or has not run in 7 days. Run `skillt auto-backup`. |

The plugin logs errors under `~/.config/opencode/logs/` (managed by OpenCode).

---

## 🚧 Known limitations

The full audit — M1 through M24, with reproduction notes — lives in
[README.zh-CN.md §9](README.zh-CN.md#-9-已知限制). Highlights:

- **M1** (fixed) Day buckets (`Today`, daily trend) used to use **UTC**; at
  UTC+8, local 00:00–08:00 landed on the previous day. They now follow the
  local calendar. Time windows themselves were always correct.
- **M2** Some read commands (`insight`, `export`, `doctor`) open the DB
  read-write; `open_db(readonly=True)` creates an empty DB instead of erroring
  when the path does not exist.
- **M3** `ensure_schema`'s `executescript` commits any pending transaction
  implicitly; in its result dict `created_base` is never set and
  `created_versions` is always true. Semantics of that dict only — the tables
  themselves are created correctly.
- **M4** Backups `chmod 0600` **after** `VACUUM INTO`, leaving a brief
  permissive window; a failed VACUUM can leave a **half-written file** behind
  that must be deleted by hand.
- **M19** (fixed) The two ways a backup was made wrote to **two different
  places**, and retention only covered one: `auto-backup` (the timer) wrote into
  `BACKUP_DIR` (`~/.local/share/opencode/backups/`) and pruned it to 30 daily +
  12 monthly, while `skillt backup`, the TUI's `b`, and the automatic pre-write
  backup of `scrub-metadata --yes` / `cleanup-selftest --yes` used `backup_db()`'s
  default — **beside the database**. Nothing pruned those, and `doctor
  backups.latest` could not even see them, so "backups exist" and "the backup
  check says none" were both true at once. The default now writes into
  `BACKUP_DIR`; `test_default_backup_lands_in_the_retention_directory` pins it.
  Strays left outside it are still there until moved by hand:
  `mv ~/.local/share/opencode/skill-usage-backup-*.db
     ~/.local/share/opencode/backups/`
- **M5** `VACUUM` / backup / export / refresh run on the UI thread, so a very
  large database briefly freezes the TUI.
- **M6** (mitigated) Non-UTF-8 locales forced `UnicodeEncodeError`; stdout and
  stderr are now reconfigured to UTF-8, though exotic glyphs may still render
  as substitutes. `LANG=C.UTF-8` avoids it entirely.
- **M7** (fixed) `resolveBranch`'s 500 ms timeout did not kill the `git` child
  process or clear its timer, leaking one process per call on a bad mount
  point. It now spawns git directly and aborts it.
- **M8** (fixed) `branchByDir` / `pendingSkillPerms` had no capacity limit and
  grew for the lifetime of the process; both are now bounded.
- **M9** (fixed) `initDone` was set before `init()` did any work, so a failed
  init left the tracker permanently disabled with only a log line to show for
  it. It is now set only on success, and a failed init can be retried.
- **M10** `app.run()` is not wrapped in `try/finally`, so a fatal error may still
  exit with code 0.
- **M11** The MCP server list is resolved **once, at plugin init**. Adding or
  renaming an MCP server needs an OpenCode restart before its calls are
  recorded — until then those calls are invisible (they are never misrecorded,
  just skipped). Detection fails **closed**: if no server can be identified,
  nothing is written rather than guessing which tools are MCP.
- **M12** The plugin inventory and attribution are also resolved **once, at
  init** by a best-effort **static scan** of each plugin's source: the package's
  entry file, plus **one hop** into the modules it imports by relative path
  (bounded: 3 hops, 256 KiB each — a published `main` is often a few-hundred-byte
  shim that re-exports the chunk holding the real registration). Tool ids are the
  keys at the **shallowest level** inside the `tool: { … }` object, so a tool's own
  `args: { query: … }` is not mistaken for one. New,
  upgraded or renamed plugins (and the tools/commands they register) need an
  OpenCode restart to appear. An unattributable tool is recorded as
  `(unknown)`; an unattributable command is **not recorded** (fail closed, so
  builtin commands like `/init` never land in the table).
  The inventory row records **which config listed the plugin** (`global`,
  `localdir`, `project`), and a `global`/`localdir` row that a later start does not
  see again is deleted — that is what makes the page's list mean "loaded", not
  "seen once since this database was created". `project` rows are never deleted
  from elsewhere, because a session started in another directory cannot see them.
- **M13** The builtin-tool `allowlist` is hardcoded and pinned to OpenCode
  1.18.34 (measured with `curl /experimental/tool/ids` against
  `opencode serve --pure`, which loads no plugins; the fourteen ids are
  unchanged since 1.18.33). A builtin added by a later release would be
  attributed to a plugin as `(unknown)` until the list is refreshed — or set
  `OPENCODE_SKILL_TRACKER_BUILTIN_TOOLS` yourself. `skillt doctor`
  `env.opencode_version` warns when the installed version drifts.
- **M14** Older builds stored a session summary — the user's **own prompt
  text** — in `metadata.summary`. That writer is gone, the export path now
  emits only an allowlist of keys, and `__selftest` asserts it is never
  written, but **rows already in your database do not remove themselves**, nor
  do the backups taken of it. Run `skillt scrub-metadata` to see them, then
  `--yes` to strip the keys. Measured 40 such rows in a real 8-day database;
  they were stripped with `scrub-metadata --yes` on 2026-10-01, and the three
  loose backups that still held it were deleted. Re-check with
  `skillt scrub-metadata` (it must report 0) and see M19. **The other free-text
  key the scrubber knows about — a permission `title` — is no longer read at
  all**: since 2026-10-09 the writer stores none, so a fresh denial cannot
  reintroduce one, and `title` stays in the scrub list only for rows older builds
  wrote. Note that removing the
  value is not the same as removing the bytes: under WAL the replaced text keeps
  living in the `-wal` file until a checkpoint, so `--yes` ends with
  `wal_checkpoint(TRUNCATE)` and reports when the database is too busy to
  checkpoint (then close OpenCode and run `skillt vacuum`).
- **M15** A call that never completes leaves **no row at all**. Usage is written
  at `tool.execute.after` or from `message.part.updated`; `tool.execute.before`
  only parks the start time in memory. So an aborted, crashed or
  after-hook-less call is not misrecorded — it is invisible. `trigger_type`
  means "which path wrote this row first", not "how the call was detected".
- **M16** One failed git lookup caches `branch = null` for that directory
  **forever** (the tradeoff that fixed M7/M8), until `vcs.branch.updated` or a
  process restart. A 500 ms timeout on a healthy repo therefore silences branch
  for the rest of that OpenCode process.
- **M17** The plugin log grows: `~/.config/opencode/logs/skill-tracker.log` gains one
  line per init and one per dispose (measured ~69 lines/day), and it is the only place
  capture errors appear, which is what `doctor log.errors` reads — so clearing it erases
  that history. **Both sides are now bounded.** The reader counts only the first
  `TRACKER_LOG_BYTES_CAP` (4 MiB, the same bound the claude-mem reader uses) from the
  **start** of the file and says so in the line it prints ("a floor, not a total");
  `skillt rotate-log` renames the file at that same size — `log.max_bytes` *is*
  `TRACKER_LOG_BYTES_CAP`, one number, so a line cannot sit in the active log and be
  invisible to the reader at the same time — and keeps `log.keep_files` rotated copies.
  What is left open is the shape of the history: rotation is opt-in unless the backup
  timer is installed (`./install.sh --with-timer`), and a log that nobody rotates stops
  being evidence the moment it is truncated by the cap.
- **M18** All three upserts keep `metadata = COALESCE(existing, incoming)`. If
  the hook path wrote a row first and the event path arrives later carrying the
  actual error text, `status` is corrected to `error` (the monotonic rule) but
  `metadata.error` is **not** filled in. Left alone deliberately: that clause is
  the load-bearing dedup invariant.
- **M20** A `denied` row cannot be produced by OpenCode 1.18.33. Denials do
  reach the tracker — as the bus events `permission.asked` then
  `permission.replied` (fields `requestID` and `reply: "reject"`, the refused
  call under `tool.callID`), measured live on 2026-10-01 — and they are recorded
  correctly now. But the host gates only five permission kinds (`edit`, `bash`,
  `webfetch`, `doom_loop`, `external_directory`) and **none of them is a category
  this tracker measures**: skills, MCP tools and plugin tools run without asking,
  and a denied `bash` is deliberately not recorded. So no table will ever show a
  denial on this version. The earlier code additionally listened for
  `permission.updated` and read `permissionID` / `response`, none of which
  1.18.33 sends — that mismatch is fixed, and
  `test_permission_rejection_writes_a_denied_row` pins the real shapes; what is
  left is a host boundary, not a tracker bug.
- **M21** The advisor's per-call budget is a copied default. skillt cannot read
  `AOS_TIMEOUT_MS` across the seam, so the "over budget" flag uses AgentOS's
  documented default (1200 ms) unless
  `OPENCODE_SKILL_TRACKER_AOS_TIMEOUT_MS` says otherwise. If AgentOS changes its
  default and nobody changes this, the flag goes quietly stale — the same shape
  as M13, and the reason it is named here rather than buried in a constant.
- **M22** The claude-mem numbers come from a foreign schema, and they are only
  numbers. `skillt claude-mem` reads another plugin's SQLite store read-only —
  counts, timestamps and short category labels, never the prose columns beside
  them (`prompt_text`, `text`, `narrative`, `tool_input`, and so on) and never its
  `settings.json`. A rename upstream blanks a number rather than breaking anything
  (the M21 shape again), and a store whose tables are all missing is reported as
  unreadable rather than as installed-and-empty. What claude-mem *does* — its own
  hooks, its own summariser — is not observable from here at all: this reads what
  it wrote, not what it did. Its two log files are read under the same contract,
  parsed by line **shape** (a line whose format has changed is counted as
  `unrecognized` rather than dropped, and no log line is ever echoed) and scanned
  from the **start** of the file under a byte cap — a tail is blind to the early
  part, and on the real log every one of its 57 ERROR lines is early. When the cap
  bites the result says `truncated` and the number is a floor, not a total. The
  HTTP projection is by field name *and* by type, so a field that is renamed to
  carry prose yields nothing instead of leaking it.
- **M23** The Agents page counts the agent a row's session **first** reported, not
  the agent that ran that call. All three usage upserts write
  `metadata = COALESCE(existing, excluded)`, so whichever JSON lands first is
  frozen in that row for good; `agent` only ever arrives later, on
  `chat.message`. A call recorded before its session named an agent is therefore
  `(unknown)` forever, and `(unknown)` measures *capture order*, not anonymous
  work. It is shown with its own count so the table cannot be read as "these
  agents did this". Measured on the two 2026-10-03 backups — 619 and 664 rows, 0
  of them without an agent — so on a healthy machine the bucket is empty; it is
  written down because it is not derivable from the page.
- **M24** A subagent's work is invisible unless it used a measured tool. The
  `task` tool is a builtin and builtins are not measured (M13), so the spawn
  record is the only witness that a subagent ran at all: the name the host asked
  for, the child session it got, whether it finished, and how long it took. Two
  things follow. `Spawned` on the Agents page counts spawns and is never folded
  into `Total`, so one column cannot mean "calls" one day and "calls plus events"
  the next. And the name arrives in the host's `subagent_type` argument, so it is
  admitted only when it is label-shaped (`SUBAGENT_LABEL_RE`: no spaces, no path,
  at most 40 characters) — that is a **shape** test, not a prose detector, and a
  value that fails it is counted as `(unnamed)` rather than printed. The row holds
  none of the task text, description, title, report or error string that sit in
  the same payload: the writer names no field but those identifiers. The stream
  also starts empty on an existing database — the table appears the next time a
  migrating surface runs (`skillt`, `sync`, `doctor`), and a running OpenCode
  picks the writer up only after a restart, so `Spawned` reads 0 for sessions that
  began before either happened. `OPENCODE_SKILL_TRACKER_SUBAGENT_DISABLE=1` turns
  it off; which of the two write paths the host actually fires for a builtin is
  not assumed, so both write and the `(parent_session_id, call_id)` key decides.
  The same boundary makes a row *absent* rather than wrong: `Ran as` says nothing
  about a subagent that ran before the writer existed. Recovering that history is
  technically clean — OpenCode's own `opencode.db` keeps every past `task` with its
  `subagent_type` and both session ids — and is deliberately **not** done here,
  because it would put a live foreign database into this project's read set. That
  choice is recorded as deferred in `MAINTENANCE.md` §8, so "not implemented" and
  "not decided" do not look the same from here.
- `skills.name` has **no unique constraint** (only `path` does). `delete_skill`
  deletes by name, so if two skills ever share a name, both sets of records go.

### Plugin export contract (important)

`plugin/skill-tracker.js` must expose its factory **only** via
`export default { id, server }`:

```js
export default { id: "skill-tracker", server: skillTrackerPlugin };
```

OpenCode 1.18.32's loader treats **every exported function** in a plugin module
as a plugin factory, and only stops enumerating when the default export is an
object carrying `server`. Do not add named `export function`s
(`parseFrontmatter` / `sanitize` / `__selftest` exist for tests and manual
self-test). If the default export ever regresses to a bare function, the loader
will call `__selftest`, whose isolation guard throws — the **whole plugin fails
to load, no hook registers, and `skill_usage` stays empty forever**.

Loader failures are not written to this plugin's own log; they appear in
OpenCode's log only:

```bash
grep 'failed to load plugin' ~/.local/share/opencode/log/opencode.log | tail
```

Regression test: `test_loader_does_not_enumerate_exports` in
`scripts/tests/test_plugin.py` (replicates the loader logic; requires `bun`).

### Self-test isolation

`__selftest()` inserts synthetic `call-test-*` rows. It checks the environment
**before opening any database** and throws `Selftest requires isolated database`
when `OPENCODE_SKILL_TRACKER_DB` is unset or resolves to the production path.
Always run it like this:

```bash
OPENCODE_SKILL_TRACKER_DB=/tmp/st-selftest-$$.db \
  bun -e 'import("~/.config/opencode/plugin/skill-tracker.js").then(m => m.__selftest())'
```

The selftest isolates its **log** as well as its database: unless
`OPENCODE_SKILL_TRACKER_LOG` names a path of its own, its lines go to
`skill-tracker-selftest.log` beside that temp database, never to the
`~/.config/opencode/logs/skill-tracker.log` that `skillt doctor` reads. Before this,
a hand-run selftest that failed wrote `[err] selftest FAIL` into the shared log and
`doctor log.errors` counted them as plugin errors forever. A successful run now
leaves its own record (`selftest start` / `selftest done: N/N passed`) where you can
find it, and nowhere else.

If the database was polluted historically, clean it with
`skillt cleanup-selftest --yes`.

---

## 📦 Repository layout

```
opencode-skill-tracker/
├── README.md                     # this file (English)
├── README.zh-CN.md               # full documentation, Chinese
├── MAINTENANCE.md                # operating checklist (English)
├── MAINTENANCE.zh-CN.md          # operating checklist, Chinese
├── LICENSE                       # MIT
├── install.sh                    # idempotent installer
├── requirements.txt              # textual
├── requirements-dev.txt          # + pytest
├── bin/skillt                    # launcher (location-independent)
├── plugin/skill-tracker.js       # OpenCode plugin — the only writer
├── scripts/skill_db.py           # shared data layer (the tracker's own data only)
├── scripts/opencode_compat.py    # every OpenCode name, in one place
├── scripts/skill_db_agentos.py   # read-only neighbour: the advisor's own store
├── scripts/skill_db_claude_mem.py # read-only neighbour: claude-mem's own ledger
├── scripts/skill-tui.py          # TUI + --cli headless subcommands
├── scripts/skill-stats.py        # legacy CLI (backwards compatible)
├── scripts/tests/                # pytest suite
└── skill-tracker/systemd/        # optional auto-backup timer + service
```

### Development

```bash
python3 -m pytest scripts/tests -q                       # standard library only
                                                         # (TUI tests are skipped)

uv pip install --python .venv/bin/python -r requirements-dev.txt   # once
.venv/bin/python -m pytest scripts/tests -q              # full suite, incl. TUI tests
```

Tests resolve their own paths with `Path(__file__).resolve()`, so the suite
passes both from this repo and through the symlinked install locations.

**Before operating this on a machine you care about**, read
[MAINTENANCE.md](MAINTENANCE.md) (Chinese: `MAINTENANCE.zh-CN.md`): the
daily / weekly / monthly checks, the procedure that reconciles recorded rows
against OpenCode's own `part` table, the invariants with the test guarding each
one, and the deviations that must not be "fixed" back.

---

## 🧹 Uninstall

```bash
rm ~/.local/bin/skillt
rm ~/.config/opencode/plugin/skill-tracker.js
rm -rf ~/.config/opencode/scripts ~/.config/opencode/skill-tracker

# optional, if you installed them
rm -rf .venv

# data — back it up first
rm ~/.local/share/opencode/skill-usage.db*
rm -rf ~/.local/share/opencode/backups
```

If you enabled the timer:

```bash
systemctl --user disable --now skillt-auto-backup.timer
rm ~/.config/systemd/user/skillt-auto-backup.{service,timer}
systemctl --user daemon-reload
```

---

## 🤝 Contributing

Issues and pull requests are welcome. Before opening a PR, make sure the suite
is green:

```bash
python3 -m pytest scripts/tests -q     # standard library only; TUI tests are skipped
```

Keep changes scoped, and update **both** `README.md` and `README.zh-CN.md` when
you change documented behaviour — `scripts/tests/test_readme.py` asserts that the
Chinese documentation covers every known limitation.

---

## ⚖️ License

[MIT](LICENSE) © 2026 SHADE-glitch
