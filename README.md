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

## Why

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

## Requirements

| | |
|---|---|
| OS | Linux — developed and verified on Ubuntu 26.04; other distributions are **unverified** |
| OpenCode | 1.18.x (developed and tested against 1.18.32) |
| Python | 3.11+ for the CLI and TUI (tested on 3.11, 3.13, 3.14) |
| Bun | bundled with OpenCode — used to run the plugin; only needed for the plugin self-test |
| `uv` | optional but recommended for creating the venv; plain `python3 -m venv` also works |

The headless subcommands (`insight`, `health`, `doctor`, `export`, `sync`,
`mcp`, `plugins`, `auto-backup`) need **only the Python standard library**. The
interactive TUI needs [Textual](https://textual.textualize.io/)
(`textual>=8.2,<9`), installed into a venv by `install.sh`.

---

## Install

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

## Usage

### Interactive TUI

```bash
skillt
```

Needs a real terminal (`stdin`/`stdout`/`stderr` all TTYs, and `TERM` neither
empty nor `dumb`). Pages: **Dashboard / Skills / MCP / Plugins / Recent /
Categories / Data** (Data is always last).

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

### Headless subcommands (no Textual needed)

```bash
skillt insight [--days N] [--min-uses N] [--limit N] [--json]
skillt export  [--out FILE] [--pretty] [--force] [--skills-only]
skillt sync    [--dry-run] [--prune-orphans] [--json]
skillt health  [--json] [--limit N]
skillt mcp     [--json] [--limit N]
skillt auto-backup [--dry-run] [--json]
skillt doctor  [--json] [--freshness-days N]
skillt cleanup-selftest [--yes]
skillt scrub-metadata [--yes] [--json] [--limit N]
skillt agentos   [--json] [--limit N]
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
  policy (see [Backups](#backups)).
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

## How it works

```
OpenCode runtime
   │  tool.execute.before/after, permission.ask, event, chat.message
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
  export and backup — used by both `skill-tui.py` and the test suite.

### Environment variables

| Variable | Effect |
|---|---|
| `OPENCODE_SKILL_TRACKER_DB` | override the database path (the plugin) |
| `OPENCODE_SKILL_TRACKER_MCP_SERVERS` | comma-separated MCP server names; **when set, automatic detection is skipped** (even when empty) |
| `OPENCODE_SKILL_TRACKER_MCP_DISABLE` | `1` stops MCP recording while skill recording stays on |
| `OPENCODE_SKILL_TRACKER_DISABLE` | `1` disables the plugin entirely |
| `OPENCODE_SKILL_TRACKER_DEBUG` | `1` enables per-call debug lines in the plugin log |
| `OPENCODE_SKILL_TRACKER_STREAMS` | read side: comma list of streams `doctor` may call stalled (default: all three). An excluded stream is still printed, marked `(excluded)` |
| `SKILLT_SCRIPTS` | override the `scripts/` directory the launcher uses |
| `SKILLT_VENV` | point the launcher at a venv dir or a python binary |

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
- Views: `v_skill_totals`, `v_skill_last30`, `v_skill_history`, plus the MCP
  analogues `v_mcp_totals`, `v_mcp_last30`, `v_mcp_history`.
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

## Backups

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
does not include `~/.local/bin`.

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

## Troubleshooting

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

## Known limitations

The full audit — M1 through M19, with reproduction notes — lives in
[README.zh-CN.md §9](README.zh-CN.md#9-已知限制). Highlights:

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
  shim that re-exports the chunk holding the real registration). New,
  upgraded or renamed plugins (and the tools/commands they register) need an
  OpenCode restart to appear. An unattributable tool is recorded as
  `(unknown)`; an unattributable command is **not recorded** (fail closed, so
  builtin commands like `/init` never land in the table).
- **M13** The builtin-tool `allowlist` is hardcoded and pinned to OpenCode
  1.18.33. A builtin added by a later release would be attributed to a plugin
  as `(unknown)` until the list is refreshed — or set
  `OPENCODE_SKILL_TRACKER_BUILTIN_TOOLS` yourself. `skillt doctor`
  `env.opencode_version` now warns when the installed version drifts.
- **M14** Older builds stored a session summary — the user's **own prompt
  text** — in `metadata.summary`. That writer is gone, the export path now
  emits only an allowlist of keys, and `__selftest` asserts it is never
  written, but **rows already in your database do not remove themselves**, nor
  do the backups taken of it. Run `skillt scrub-metadata` to see them, then
  `--yes` to strip the keys. Measured 40 such rows in a real 8-day database;
  they were stripped with `scrub-metadata --yes` on 2026-10-01, and the three
  loose backups that still held it were deleted. Re-check with
  `skillt scrub-metadata` (it must report 0) and see M19. Note that removing the
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
- **M17** The plugin log is never rotated: `~/.config/opencode/logs/skill-tracker.log`
  only grows (measured ~69 lines/day, one per init and one per dispose). It is
  also the only place capture errors appear, which is what
  `doctor log.errors` reads — clearing it erases that history.
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

If the database was polluted historically, clean it with
`skillt cleanup-selftest --yes`.

---

## Repository layout

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
├── scripts/skill_db.py           # shared data layer
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

## Uninstall

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

## Contributing

Issues and pull requests are welcome. Before opening a PR, make sure the suite
is green:

```bash
python3 -m pytest scripts/tests -q     # standard library only; TUI tests are skipped
```

Keep changes scoped, and update **both** `README.md` and `README.zh-CN.md` when
you change documented behaviour — `scripts/tests/test_readme.py` asserts that the
Chinese documentation covers every known limitation.

---

## License

[MIT](LICENSE) © 2026 SHADE-glitch
