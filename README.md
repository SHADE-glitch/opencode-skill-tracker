<p align="right"><a href="README.md"><b>English</b></a> | <a href="README.zh-CN.md">简体中文</a></p>

# 🎯 OpenCode usage tracker (`ocut`)

![OpenCode](https://img.shields.io/badge/OpenCode-1.18.x-blue)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![SQLite](https://img.shields.io/badge/storage-SQLite-003B57?logo=sqlite)
![License: MIT](https://img.shields.io/badge/license-MIT-green)
[![Repository](https://img.shields.io/badge/repository-GitHub-black?logo=github)](https://github.com/SHADE-glitch/opencode-usage-tracker)

**What it is** — A passive, read-only tracker that shows how you actually use OpenCode: every tool call, skill, subagent, MCP tool, model step and session.

**What it solves** — OpenCode persists sessions for resuming, not for answering "which skills do I actually use?"; `ocut` indexes that data into its own database and turns it into panels and reports.

**How to run** — `./install.sh`, then `ocut sync`.

**Where to look** — See [AGENTS.md](AGENTS.md) and [MAINTENANCE.md](MAINTENANCE.md).

`ocut` shows how you actually use [OpenCode](https://opencode.ai): every tool call,
every skill and subagent, every MCP tool, every model step and its tokens, and every
session — what ran, when, whether it finished, how long it took, and in which project.

It is a **passive reader**. OpenCode keeps its own sessions in a single SQLite file
(`~/.local/share/opencode/opencode.db`); `ocut` indexes that file into its own database
(`~/.local/share/opencode-usage-tracker/usage.db`) and never writes back to OpenCode's
directories. One optional plugin hook fills the one gap the host cannot expose — which
plugin a tool call belonged to. Everything stays local: no network, no upload, no
message text.

---

## 🤔 Why

OpenCode already persists what you need — but in a shape built for resuming sessions,
not for answering "which of my skills do I actually use?".

- Every tool call is a `tool` part in `opencode.db`, whatever the tool is: a builtin,
  the `skill` tool, the `task` tool that spawns a subagent, or an MCP server's tool.
- Each model step is a `step-finish` part carrying that step's token counts and cost;
  the model lives on its *parent message*, not on the step.
- Sessions, their projects and their directories are rows in `session`.
- What is *installed* — skills, agents, slash commands, MCP servers, plugins — lives in
  the config home (`~/.config/opencode`), not in the database.

`ocut` reads all of that **read-only** and turns it into panels and reports. Two things
it deliberately does *not* do:

- **It never writes OpenCode's database or config.** The host database is opened
  `mode=ro` and `PRAGMA query_only = ON`, so a bug in this project cannot make that
  claim false.
- **It never stores message or tool prose.** A tool part carries `output`, `title`,
  `metadata` and `error`; a `text`/`reasoning` part carries your prompt and the model's
  answer; a session row carries a `title` OpenCode derives from your first message.
  None of those is read. Only identifiers, statuses, durations, token counts and counts
  land in `usage.db`.

Design constraints, kept on purpose:

- **No OpenCode source changes** — a passive reader plus one public plugin hook.
- **No instrumentation in `SKILL.md`** — nothing to add to your skills.
- One database of the tracker's own + one entry point + one optional hook, minimal deps.

### 🧩 The one thing a passive reader cannot see

OpenCode records a plugin's tool under the tool's own name, with nothing naming the
plugin, and records nothing at all for a plugin *command*. So `ocut` alone can never
attribute a plugin call. The optional hook `plugin/ocut-hook.js` closes that gap: at
load it statically scans each installed plugin's source for the tool and command ids it
registers, and on a call it can attribute it appends one record to a local JSONL sidecar
that `ocut sync` ingests. The hook is the **only** writer of `plugin_usage`, and it never
opens OpenCode's database.

---

## 📋 Requirements

| | |
|---|---|
| OS | Linux — developed and verified on Ubuntu; other distributions are **unverified** |
| OpenCode | 1.18.x — the host whose database and config this project reads (see [compatibility](docs/maintenance/compatibility.md)) |
| Python | 3.11+ for the CLI and TUI; CI runs the suite on **3.11 and 3.14** |
| Textual | `textual>=8.2,<9` — needed by the interactive TUI only, installed into a venv by `install.sh` |
| `uv` | optional but recommended for creating the venv; plain `python3 -m venv` also works |

Every headless subcommand (`sync`, `stats`, `tools`, `health`, `export`, `backup`,
`restore`, `format`, …) needs **only the Python standard library**. The hook runs inside
OpenCode's own Bun runtime and needs nothing installed separately.

---

## 📥 Install

### 📦 Out of the box

```bash
git clone https://github.com/SHADE-glitch/opencode-usage-tracker.git
cd opencode-usage-tracker
./install.sh
```

Then **restart OpenCode** so it loads the hook, and verify:

```bash
ocut health    # is the tracker database readable, and is the host database found?
ocut sync      # index what OpenCode has written so far
ocut           # launch the interactive TUI (eight tabs)
```

`install.sh` is idempotent. It:

1. creates `./.venv` and installs `requirements.txt` (textual) into it;
2. symlinks exactly **two** install locations back at this repo, so `git pull` here
   updates the live install:

   | Install location | Points at |
   |---|---|
   | `~/.config/opencode/plugin/ocut-hook.js` | `plugin/ocut-hook.js` |
   | `~/.local/bin/ocut` | `bin/ocut` |

   `bin/ocut` resolves its own `scripts/` directory from the repo it lives in, so the
   scripts are never copied to a user path. If a location already holds a real file or
   directory, the script **refuses to overwrite it** and tells you what to move; pass
   `--force` to replace it.

3. runs `ocut health` once and prints the next steps.

> Make sure `~/.local/bin` is on your `PATH`. If it is not, add
> `export PATH="$HOME/.local/bin:$PATH"` to your shell profile.

### 🛠️ Manual install

The same thing, spelled out — useful if you prefer to place the repo elsewhere:

```bash
git clone https://github.com/SHADE-glitch/opencode-usage-tracker.git
cd opencode-usage-tracker

# 1. venv for the TUI
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python -r requirements.txt

# 2. point the two install locations at this repo
mkdir -p ~/.config/opencode/plugin ~/.local/bin
ln -sf "$PWD/plugin/ocut-hook.js" ~/.config/opencode/plugin/ocut-hook.js
ln -sf "$PWD/bin/ocut"            ~/.local/bin/ocut
```

### 📟 Headless-only install

No Textual, no venv — just the reader and the reports:

```bash
mkdir -p ~/.local/bin
ln -sf "$PWD/bin/ocut" ~/.local/bin/ocut
```

`ocut` then falls back to the system `python3` for every subcommand; only launching the
bare TUI needs the venv. You can also point the launcher at a venv of your own with
`OCUT_VENV`.

---

## 🧭 Usage

### 🖥️ Interactive TUI

```bash
ocut
```

Needs a real terminal (`stdin`/`stdout`/`stderr` all TTYs).
Pages: **Dashboard / Tools / Skills / Agents / Plugins / MCP / Tokens / Usage** (eight
tabs, in that order). The tab bar and footer are the whole key surface:

| Key | Action |
|---|---|
| `q` | quit |
| `r` | refresh — run an incremental sync, then reload the screen |
| `s` | toggle auto-sync (the 30-second background indexer) |
| `tab` | next tab |
| `shift+tab` | previous tab |
| `enter` / double-click | open the selected row's detail screen |
| `escape` / `q` | go back from a pushed detail screen |

The **Dashboard** is three rows of panels. The first row is the headline counts and the
token totals, each label printed beside its number:

| Panel | Labels |
|---|---|
| Usage | `Tool calls`, `Skills`, `Agents`, `MCP`, `Plugins` |
| Tokens | `Requests`, `Input`, `Output`, `API Total`, `Usage Total`, `Hit rate` |
| Cache | `Cache hit`, `Cache miss`, `Cache write` |
| Runtime | `Completed`, `Incomplete`, `Avg tool time`, `Sessions`, `Projects` |

Below those sit the `Top models` and `Top tools` leaderboards, and a `Calls per day`
trend. `Plugins` counts the distinct plugins the optional hook recorded in the window —
a passive reader has no evidence a plugin or command ran unless the hook wrote it (M3).

The **Tools / Skills / Agents / Plugins / MCP** tabs are one row per entity, with calls,
success, average duration and last use; `enter` on a row opens that entity's recent-call
history. The **Tokens** tab is per-model token totals from the host's own step records,
and the **Usage** tab is the request log for the window you pick. Each tab keeps its own
time window (a preset like `7d`, or a custom span), so changing one tab's window never
moves another's.

A plugin call that the hook recorded carries a NULL status — a tool's after-hook carries
no status and a command has no after-hook at all — so a plugin row's status column reads
`-`, not `0`.

### ⚡ Headless subcommands (no Textual needed)

```bash
ocut sync    [--full] [--quiet] [--db PATH] [--host-db PATH] [--host-config PATH] [--plugin-events PATH]
ocut stats   [--kind K] [--db PATH]
ocut tools   [--limit N]
ocut skills  [--limit N]
ocut agents  [--limit N]
ocut mcp     [--limit N]
ocut plugins
ocut models  [--limit N]
ocut show    <tool|skill|agent|mcp> <name> [--limit N]
ocut recent  [--limit N]
ocut inventory [--kind K]
ocut export
ocut health
ocut backup
ocut restore [NAME]
ocut vacuum
ocut format  [--write]
```

- `sync` — index OpenCode's database into the tracker's own. `--full` rebuilds from
  scratch (taking a snapshot first); `--quiet` is what the TUI's background worker runs.
  It is incremental: a rowid cursor consumes new `part` rows, and `session` is re-read in
  full each run because the host rewrites its rollup.
- `stats` — an overview of every category: sessions, projects, tool calls, skill / agent /
  MCP invocations, plugins and commands *installed*, model steps and tokens.
- `tools` / `skills` / `agents` / `mcp` — one category's table. `plugins` is the
  installed ∪ used union, with the owning plugin's version and declared path.
- `models` — per-model token totals from the host's own `step-finish` records, plus the
  most recent steps. `Total` is the host's own step total when it sent one; cache
  read/write are shown separately and are **not** inside it.
- `show <kind> <name>` — the recent calls for one entity. `recent` — the most recent tool
  calls of any kind. `inventory` — what is installed or available.
- `export` — dump every view and table as JSON.
- `health` — a read-only report: the database path, the host database and config, the
  resolved settings, the schema version, each sync cursor against the host's own counts,
  the indexed totals, and — the point of the command — the `unparsed` counters. A run
  that quietly claims nothing still prints "done"; `health` is where a format change
  becomes visible.
- `backup` / `restore` — snapshot the database, or list and restore a snapshot (see
  [Backups](#-backups)).
- `format` — print the OpenCode format-dependency surface, read out of the parser's
  registry. `--write` refreshes a generated block **already present** in a file, and
  refuses to create one: the markers are the opt-in, so the flag cannot bring a document
  into existence that nobody asked for.

Global value flags (`--db`, `--kind`, `--limit`, `--host-db`, `--host-config`,
`--plugin-events`) may sit before **or** after the subcommand: `ocut --limit 5 tools` and
`ocut tools --limit 5` are the same. A command is never mistaken for a flag's value.

## ⚙️ Settings

Zero configuration is a supported configuration: with no file, every key takes its
default and nothing is created. Settings live in **`~/.config/ocut/config.toml`**
(override the path with `OCUT_CONFIG`; `XDG_CONFIG_HOME` is honoured), read with the
standard library's `tomllib`.

**Precedence: environment → file → default.**

| Key | Default | Environment | What it changes |
|---|---|---|---|
| `top_n` | `5` | `OCUT_TOP_N` | rows in the Dashboard's Top models / Top tools |
| `log_limit` | `100` | `OCUT_LOG_LIMIT` | request rows on the Usage tab |
| `detail_limit` | `200` | `OCUT_DETAIL_LIMIT` | rows on a history / model-response screen |
| `name_cap` | `28` | `OCUT_NAME_CAP` | widest cell a table renders before the name is clipped |
| `refresh_secs` | `5` | `OCUT_REFRESH_SECS` | TUI redraw timer |
| `sync_secs` | `30` | `OCUT_SYNC_SECS` | TUI auto-sync timer |

A file that cannot be read is an **error**, not a fallback: an unknown key, malformed
TOML, a value of the wrong type, or a value outside its bounds is a hard failure that
names the path (or variable) and the key. Someone who edits `config.toml` and is
silently ignored is the worst outcome this layer can produce.

### 🌱 Environment variables

| Variable | Effect |
|---|---|
| `OCUT_DB` | the tracker database (default `~/.local/share/opencode-usage-tracker/usage.db`) |
| `OCUT_OPENCODE_DATA` | OpenCode's data home (default `~/.local/share/opencode`) |
| `OCUT_OPENCODE_CONFIG` | OpenCode's config home (default `~/.config/opencode`) — **both** the reader and the hook |
| `OCUT_HOST_DB` | the host database to read (default `<data home>/opencode.db`) |
| `OCUT_HOOK_EVENTS` | the plugin-usage sidecar (default `~/.local/share/opencode-usage-tracker/plugin-events.jsonl`) — **both** the hook and `ocut sync` |
| `OCUT_CONFIG` | the settings file (default `~/.config/ocut/config.toml`) |
| `XDG_CONFIG_HOME` | base for the default settings path |
| `OCUT_HOOK_DISABLE` | `1` turns the hook off entirely |
| `OCUT_HOOK_PACKAGES` | where npm plugin sources are looked up for the hook's static scan (default `~/.cache/opencode/packages`) |
| `OCUT_SCRIPTS` | override the `scripts/` directory the launcher uses |
| `OCUT_VENV` | point the launcher at a venv dir **or** a python binary |
| `OCUT_CONFIG_DIR` | installer only: the OpenCode config directory to link into (default `~/.config/opencode`) |
| `OCUT_BIN_DIR` | installer only: where the `ocut` launcher is linked (default `~/.local/bin`) |
| `HOME` | inherited, not a knob: every default above is built from it |

---

## 🔬 How it works

`ocut` reads OpenCode's own state **read-only** and copies what it needs into its own
database; nothing is ever written back to OpenCode.

```
OpenCode's own state (read-only — never opened for writing)
  ~/.local/share/opencode/opencode.db    session · message · part
  ~/.config/opencode/                    installed skills · agents · commands · mcp · plugin
        │
        │  ocut sync   → scripts/ocut-sync.py   (opens the host with mode=ro)
        ▼
~/.local/share/opencode-usage-tracker/usage.db   (SQLite — the tracker's own store)
        │
        │  reads
        ├── ocut                 → scripts/ocut-tui.py    (Textual TUI, eight tabs, needs venv)
        ├── ocut stats|tools|…   → scripts/ocut-stats.py  (headless reports, stdlib only)
        └── ocut backup|restore|vacuum|format

  ~/.local/share/opencode-usage-tracker/plugin-events.jsonl
        ▲  written only by plugin/ocut-hook.js, ingested by ocut sync
```

- **Source — read-only.** OpenCode's `opencode.db` (`session`, `message`, `part`) plus
  the installed inventory under `~/.config/opencode`. The host database is opened
  `mode=ro` with `PRAGMA query_only = ON`, and a missing file is an error, not an empty
  success.
- **Sync.** `ocut sync` copies what it needs into the tracker's own database; `--full`
  rebuilds it from scratch (after a snapshot).
- **Store.** `~/.local/share/opencode-usage-tracker/usage.db` — the tracker's own SQLite,
  written only by `ocut`, in WAL mode with a 5-second busy timeout.
- **Readers.** Every `ocut` command. The TUI needs Textual (a venv); the headless reports
  need only the standard library.

### 🗄️ Database

Tables: `tool_calls`, `model_calls`, `skill_usage`, `agent_usage`, `mcp_usage`,
`plugin_usage`, `plugin_surface`, `sessions`, `inventory`, `unparsed`, `sync_state`.
Views: `v_tools`, `v_skills`, `v_agents`, `v_mcp`, `v_plugins`.

- `tool_calls` — one row per `tool` part, whatever the tool is. `category` is
  `builtin | skill | agent | mcp`; `status` is the host's own `state.status` verbatim.
- `skill_usage` — the `skill` tool's calls; the skill name is read from
  `state.input.name`, one of the only two argument names this project looks at.
- `agent_usage` — the `task` tool's calls (a subagent was started), keyed by
  `state.input.subagent_type`.
- `mcp_usage` — MCP tool calls, found by name (`<server>_<tool>`) and resolved against
  the servers the host's config lists, longest prefix first.
- `model_calls` — one row per `step-finish` part; tokens and cost live here, the model
  comes from the parent message.
- `sessions` — one row per OpenCode session (including child sessions), with the host's
  own per-session rollup. `sessions` and `model_calls` are one measurement at two
  granularities and are **never added**.
- `inventory` — what is installed, from the host's directories and config: skills, agents,
  commands, MCP servers, plugins, providers, and the tool ids each plugin registers.
- `plugin_usage` — one row per plugin tool/command call the hook saw. This is the one
  table fed from outside the host database.
- `plugin_surface` — the tool and command ids each plugin registers, as reported by the
  hook at load; `ocut sync` folds it back into `inventory` after the filesystem rewrite,
  and drops the rows of a plugin that is no longer installed.
- `sync_state` — one row per host table the reader tracks: the rowid cursor plus the
  host's own `count(*)`/`max(rowid)` and a `note` where a pruned or renumbered table says
  so. The cursor is held back at the first non-final `running` tool part so a call that
  finishes after the row was inserted is re-read; the hold is capped, so an interrupted
  session cannot pin the cursor forever.
- `unparsed` — records the indexer saw but could not use, keyed by reason. This is the
  runtime signal that OpenCode's persisted format moved.

Query it directly:

```bash
sqlite3 ~/.local/share/opencode-usage-tracker/usage.db \
  "SELECT tool_name, calls, errors FROM v_tools ORDER BY calls DESC LIMIT 10;"
sqlite3 ~/.local/share/opencode-usage-tracker/usage.db \
  "SELECT skill, calls, last_ts FROM v_skills ORDER BY calls DESC LIMIT 10;"
```

---

## 💾 Backups

```bash
ocut backup            # snapshot the database now
ocut restore           # list available snapshots
ocut restore NAME      # restore one (close the TUI first)
ocut vacuum            # compact the file, reporting what came back
```

- Snapshots are written to `~/.local/share/opencode-usage-tracker/backups/` as
  `usage-YYYYMMDD-HHMMSS-<reason>.db`, created with SQLite's online backup API — not a
  file copy, because under WAL the newest committed frames can still be in `-wal`, and a
  copied main file would be a stale database.
- The newest `BACKUP_KEEP` (5) managed snapshots are kept; older ones are pruned by
  mtime, not by name, because several can land in the same second.
- `ocut sync --full`, a schema-version bump, and `ocut restore` all take a snapshot
  **first**, so a destructive step is undoable.
- Restore keeps the database that was there: it snapshots the live file before replacing
  it, so a wrong restore is itself reversible.
- Hand-made `usage.db.bak-*` files beside the database are listed as restore sources but
  are never pruned — this tool did not create them and does not get to delete them.

There is **no systemd timer**: backups are manual and pre-destructive only. `ocut backup`
is the deliberate act.

`ocut vacuum` is the other deliberate one. It snapshots first (reason `pre-vacuum`), runs
SQLite's `VACUUM`, and prints the file before and after with the pages and freelist on each
side — then re-reads `quick_check` and every row count it printed before, because a rewrite
is the one step here that could lose a row. It is the only thing that returns the space a
dropped index leaves behind: dropping one *parks* its pages on the freelist instead of
releasing them, so the file stays the size it was until something rewrites it. Run it after
a schema change that drops something, or when the size stops matching the row count — never
on a schedule, since it rewrites the whole file and needs room for a second copy. **Close
the TUI first.**

---

## 🔧 Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `no database at … — run: ocut sync` | The tracker database has not been built yet. Run `ocut sync`. |
| `no OpenCode database at …` | `ocut` cannot find `~/.local/share/opencode/opencode.db`. Check that OpenCode has run, or point `OCUT_HOST_DB` at it. |
| Plugin usage stays empty / a plugin shows `0 uses` | Plugin usage is recorded **only** by the hook, and only after an OpenCode restart. Confirm `~/.config/opencode/plugin/ocut-hook.js` exists (it is a symlink), restart OpenCode, and check that `OCUT_HOOK_DISABLE` is not set. A `0 uses` means "the hook recorded nothing", not "the plugin was not used". |
| `ocut: this database is older than this build` | The database predates the current schema. Run `ocut sync` to roll it forward. |
| `ocut health` shows an `UNPARSED` line | The reader met a record type or field it does not know — usually right after an OpenCode upgrade. The reason string names it; see [compatibility](docs/maintenance/compatibility.md). |
| `ocut health` reports `settings NOT READ` | `~/.config/ocut/config.toml` has an unknown key, malformed TOML, or an out-of-range value. The message names the key. |
| The TUI refuses to start | It needs a real terminal and a venv with textual. Run `./install.sh`, or set `OCUT_VENV` to a python with textual. |

---

## 🚧 Known limitations

The list below runs **M1–M17**. The Chinese README carries the same list with more
detail in [§9](README.zh-CN.md#-9-已知限制).

- **M1** OpenCode's own database is the **source of truth**. `ocut` can only see what the
  host persisted; a call the host never wrote a `part` for is invisible, not misrecorded.
- **M2** The host database and config are read-only and **never written** — opened
  `mode=ro` with `PRAGMA query_only = ON`. `ocut` therefore cannot repair or backfill the
  host, only index it.
- **M3** Plugin usage requires the opt-in hook **and** an OpenCode restart. Until then
  `plugin_usage` is empty, and a `0 uses` on the Plugins tab means **nothing recorded**,
  not "not used".
- **M4** The hook's attribution is a best-effort **static scan** of each plugin's source.
  A plugin whose surface the scan cannot parse records nothing — honest, because a
  guessed owner would be worse than a missing row. New or renamed plugins need a restart.
- **M5** A plugin call's `ts` is the **hook's own clock**, not the host's, and its status
  is NULL: a tool's after-hook carries no status and a command has no after-hook at all.
- **M6** When OpenCode's persisted format moves, the reader counts the record as
  `unparsed` instead of guessing. A jump in `ocut health`'s `UNPARSED` line after an
  upgrade means the format changed, not that you stopped using a tool.
- **M7** No message body and no tool prose is ever stored. A part's `output`, `title`,
  `metadata`, `error`, a `text`/`reasoning` body, and a session `title` are named nowhere
  the reader reads them; only identifiers, statuses, durations and counts land in the
  database.
- **M8** The per-step `model_calls` and the host's per-session rollup `sessions` are one
  measurement at two granularities. They are shown beside each other and are **never added**.
- **M9** MCP calls are attributed by name against the host's own config, resolved
  **longest prefix** first. A tool name no configured server owns is not guessed into a
  server; it stays `builtin` and is reported as server-shaped-unowned.
- **M10** The plugin-usage sidecar is capped and rotated by **truncation** (8 MiB). The
  reader notices the file is shorter than its cursor and re-reads from the start —
  idempotent, because every record is keyed by its own id.
- **M11** The Plugins tab is the union of what is **installed** and what has usage, keyed
  by name. An installed-but-unused plugin shows a zero count; a plugin with usage and no
  inventory row still appears. The list is **not** windowed — an installed plugin stays
  listed at every range — while `uses` and `last used` follow the tab's window, like
  every other entity tab.
- **M12** Backups are manual and pre-destructive only. There is **no systemd** timer any
  more; retention keeps the newest five managed snapshots and never touches hand-made
  copies.
- **M15** The `state` column has four labels, and the fourth means **no evidence**.
  OpenCode's builtin tool and agent ids live in its compiled registry: there is no
  file on disk that enumerates them, and the API that would answer needs the server
  running. So on Tools and Agents, a name with history and no inventory row reads
  `not reported`, not `removed` — this reader will not call a builtin deleted. On
  Skills, Plugins and MCP the inventory is a complete census, so `removed` there
  means what it says.
- **M16** Only the two **global** external skill roots are read:
  `~/.claude/skills` and `~/.agents/skills`. OpenCode also loads skills from each
  project — `.claude/` and `.agents/` walked from a session's directory up to its
  worktree root — plus `~/.opencode`, `$OPENCODE_CONFIG_DIR` and the
  `skills.paths` / `skills.urls` config keys. None of those are inventoried: the
  first needs a session directory to start from and the last downloads something
  this reader does not do. A skill installed only at project level therefore does
  not appear as installed. Put shared skills in one of the two roots above and they
  will.
- **M13** The Tokens and Usage lists show a `Cache miss` column and a `Usage Total`
  even though the host publishes no such fields: a `step-finish` part carries only an
  *uncached* `prompt_tokens` plus `tokens.cache.read` / `tokens.cache.write`. The labels
  mirror the sibling CodeBuddy tracker, so this is a **re-labelling**, not new
  measurements — `Cache miss` is the uncached input, `Cache hit` is the cache read,
  `Input` is all three added (the full prompt), and `Usage Total` is the display-only
  `Input + Output + cache hit`. `API Total` stays the host's own step total; read the
  derived numbers beside it, never instead of it.
- **M14** The `state` column reads `installed` when there is **no evidence** to the
  contrary, and that is what it means on the Plugins tab. Usage there is written only by
  the optional hook (M3), so an installed plugin whose hook has recorded nothing carries
  exactly the same label as one that was installed and never touched. The label is
  deliberately about the inventory row, not about usage: nothing in this column is a claim
  that the thing ran.
- **M17** The TUI's **tab-level** columns, panels and labels mirror the sibling
  CodeBuddy tracker's, but the two pushed **detail** screens and the status line keep
  ocut's own fields: OpenCode names no plugin `target`/`marketplace` and reports no
  separate cache hit/miss beside `cache_read`, so the plugin-history and
  model-response tables cannot be column-for-column identical without inventing a
  field.

---

## 📦 Repository layout

```
opencode-usage-tracker/
├── README.md                     # this file (English)
├── README.zh-CN.md               # full documentation, Chinese
├── MAINTENANCE.md                # operating checklist (English)
├── MAINTENANCE.zh-CN.md          # operating checklist, Chinese
├── AGENTS.md                     # the agent contract (architecture, invariants, CI)
├── LICENSE                       # MIT
├── install.sh                    # idempotent installer
├── requirements.txt              # textual (the TUI's only dependency)
├── requirements-dev.txt          # + pytest
├── bin/ocut                      # bash dispatcher (location-independent)
├── plugin/ocut-hook.js           # the one optional OpenCode plugin — writes the sidecar
├── scripts/ocut_db.py            # shared data layer: schema, settings, queries, snapshots
├── scripts/ocut-sync.py          # the passive reader: host database → tracker database
├── scripts/ocut-tui.py           # Textual TUI (eight tabs)
├── scripts/ocut-stats.py         # headless reports (stdlib only)
├── scripts/tests/                # pytest suite
└── docs/maintenance/             # compat matrix · host interface · measurements
```

### 🧑💻 Development

```bash
python3 -m pytest scripts/tests -q                       # standard library only; TUI tests
#                                                         need textual
.venv/bin/python -m pytest scripts/tests -q              # full suite, incl. TUI tests
.venv/bin/python -m pytest scripts/tests -q -n auto      # the same suite in parallel
#                                                         (pytest-xdist; timing in
#                                                         docs/maintenance/measurements.md §6)
```

**Before operating this on a machine you care about**, read
[MAINTENANCE.md](MAINTENANCE.md) (Chinese: `MAINTENANCE.zh-CN.md`): the checks to run, the
invariants with the test guarding each one, and the deviations that must not be "fixed"
back. Behind it sit three references:
[`docs/maintenance/compatibility.md`](docs/maintenance/compatibility.md) records the pins
and which OpenCode release each claim belongs to,
[`docs/maintenance/opencode-interface.md`](docs/maintenance/opencode-interface.md) lists
every host name this project reads, and
[`docs/maintenance/measurements.md`](docs/maintenance/measurements.md) holds the command
behind every size, rate and duration quoted anywhere in this repository.

---

## 🧹 Uninstall

```bash
rm ~/.local/bin/ocut
rm ~/.config/opencode/plugin/ocut-hook.js

# the venv, if you installed it
rm -rf .venv

# data — back it up first
rm ~/.local/share/opencode-usage-tracker/usage.db*
rm -rf ~/.local/share/opencode-usage-tracker/backups
rm ~/.local/share/opencode-usage-tracker/plugin-events.jsonl
```

`ocut` never writes anything under `~/.config/opencode` except through `install.sh`'s one
symlink, so removing that link and the launcher is a complete uninstall.

---

## 🤝 Contributing

Issues and pull requests are welcome. Before opening a PR, make sure the suite is green:

```bash
python3 -m pytest scripts/tests -q     # standard library only; TUI tests need textual
.venv/bin/python -m pytest scripts/tests -q -n auto     # the same suite in parallel
#                                       (needs pytest-xdist; timing in
#                                       docs/maintenance/measurements.md §6)
```

Keep changes scoped, and update **both** `README.md` and `README.zh-CN.md` when you change
documented behaviour — `scripts/tests/test_readme.py` asserts that both READMEs carry the
same limitations, the same tabs and the same keys.

---

## ⚖️ License

[MIT](LICENSE) © 2026 SHADE-glitch
