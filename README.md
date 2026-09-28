# OpenCode Skill + MCP Tracker (`skillt`)

Record and query how every skill **and every MCP tool** in
[OpenCode](https://opencode.ai) is actually used: what ran, when, whether it
succeeded, how long it took, and in which project. Everything lands in a single
local SQLite file — no network, no upload, no conversation content.

**English** · [简体中文](README.zh-CN.md)

---

## Why

OpenCode has no dedicated skill hook. This project recognises skill usage
indirectly, through the generic tool hooks:

- OpenCode exposes skills as a tool named `skill` with input `{ name }`;
- MCP tools are exposed as `{server}_{tool}` (e.g. `basic-memory_read_note`)
  and go through the **same** generic tool wrapper, so they are observed
  through the same hooks;
- the plugin `skill-tracker.js` listens on `tool.execute.before` /
  `tool.execute.after` / `permission.ask` / `event` / `chat.message`;
- when the tool is `skill` the call is written to `skill_usage`; when it belongs
  to a known MCP server it is written to `mcp_usage`;
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
| OpenCode | 1.18.x (developed and tested against 1.18.32) |
| Python | 3.11+ for the CLI and TUI (tested on 3.11, 3.13, 3.14) |
| Bun | bundled with OpenCode — used to run the plugin; only needed for the plugin self-test |
| `uv` | optional but recommended for creating the venv; plain `python3 -m venv` also works |

The headless subcommands (`insight`, `health`, `doctor`, `export`, `sync`,
`mcp`, `auto-backup`) need **only the Python standard library**. The interactive
TUI needs [Textual](https://textual.textualize.io/) (`textual>=8.2,<9`),
installed into a venv by `install.sh`.

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
empty nor `dumb`). Pages: **Dashboard / Skills / Recent / Categories / Data /
MCP**.

The five Dashboard cards, each labelled above its number:

| Card | Meaning |
|---|---|
| `Skills` | total skills known (rows in `skills`) |
| `Uses` | total recorded skill uses (rows in `skill_usage`) |
| `Today` | skill uses today (local calendar day) |
| `Personal` | locally authored skills (`category = personal`) |
| `OSS` | skills synced from an open-source upstream (`category = open-source`) |

Below the cards a single line summarises MCP activity — `MCP: N call(s) · S
server(s) · T tool(s) · D today`. The counters are kept separate from the skill
ones, so `Uses` never silently includes MCP traffic.

The **MCP** page lists one row per `(server, tool)` pair with call count,
success rate, average duration and last use. It shares the `s` / `ctrl+s` sort
cycling with the Skills page.

| Key | Action |
|---|---|
| `Tab` | switch page |
| `↑` `↓` / `j` `k` | move cursor |
| `Enter` | open the selected skill's detail |
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
skillt doctor  [--json]
skillt cleanup-selftest [--yes]
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
- `cleanup-selftest` — remove synthetic rows left by `__selftest()`
  (`project_path = /tmp/selftest-proj`). Dry-run by default; `--yes` deletes for
  real (after taking a backup).

Argument validation: `--days ≥ 1`, `--min-uses ≥ 0`, `--limit ≥ 1`; invalid
values fail fast with exit code 2. Every headless subcommand also works when
stdout is **not** a TTY (pipes, redirection), emitting plain text or JSON.

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
| `SKILLT_SCRIPTS` | override the `scripts/` directory the launcher uses |
| `SKILLT_VENV` | point the launcher at a venv dir or a python binary |

### Database

- `skills` — one row per `SKILL.md`. `path` is `UNIQUE`; `name` is **not**.
  Migration adds `content_hash` for version tracking.
- `skill_usage` — one row per skill call. `UNIQUE(session_id, call_id)` makes it
  idempotent. `status ∈ {success, error, denied, ask, unknown}`;
  `trigger_type ∈ {tool_call, event_detected, permission_denied, manual}`.
  `metadata` is sanitized JSON (model / agent / branch / summary).
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
- Manual: `skillt backup` (legacy CLI) or `b` in the TUI.

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

The full audit — M1 through M11, with reproduction notes — lives in
[README.zh-CN.md §9](README.zh-CN.md#9-已知限制). Highlights:

- **M1** (fixed) Day buckets (`Today`, daily trend) used to use **UTC**; at
  UTC+8, local 00:00–08:00 landed on the previous day. They now follow the
  local calendar. Time windows themselves were always correct.
- **M2** Some read commands (`insight`, `export`, `doctor`) open the DB
  read-write; `open_db(readonly=True)` creates an empty DB instead of erroring
  when the path does not exist.
- **M5** `VACUUM` / backup / export / refresh run on the UI thread, so a very
  large database briefly freezes the TUI.
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

## License

[MIT](LICENSE) © 2026 SHADE-glitch
