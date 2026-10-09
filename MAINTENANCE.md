# Maintenance checklist

Companion to [README.md](README.md) (features) and
[README.zh-CN.md](README.zh-CN.md) (exhaustive reference). This file is the
**operating** document: what to check, in what order, what "healthy" looks like,
and what is deliberately left broken.

Chinese version: [MAINTENANCE.zh-CN.md](MAINTENANCE.zh-CN.md).

## 0. Facts about the machine this was last verified on

Measured 2026-10-01. Re-measure before trusting any number here.

| Item | Value |
|---|---|
| OpenCode | 1.18.34 (`opencode --version`); the builtin allowlist was re-read from the host on 2026-10-03 and the fourteen ids had not changed |
| Plugin SDK | `@opencode-ai/plugin` 1.18.4 |
| Runtime for the plugin | Bun (`~/.bun/bin/bun`) — `bun:sqlite` |
| TUI venv | `.venv` (Python 3.13.14, textual 8.2.8) |
| Database | `~/.local/share/opencode/skill-usage.db`, mode 0600, WAL; 606,208 bytes on 2026-10-04 (was 592 KiB on 2026-10-01) |
| Rows (recounted 2026-10-04 19:36, live) | 44 skills · 2 skill_usage · 2 mcp_usage · 26 plugin_usage · 81 skill_versions · 5 plugin_inventory · **1 subagent_usage** (`auditor` ×1, 18:00) — the owner restarted OpenCode at 16:31, so spawns after that are recorded and everything before is not (M24). `claude_mem_search` has now been called (1 row); the 9 corpus tools `claude-mem-inject.js` registered on 2026-10-03 23:32 are still at 0, so `skillt plugins` reports 12 surfaces of which 10 are never called |
| Schema | `PRAGMA user_version = 2`, `SCHEMA_VERSION = 2`. `subagent_usage` was added 2026-10-04 **without** a bump: the counter exists because a view cannot be redefined by `CREATE VIEW IF NOT EXISTS`, and this is a table, which `ensure_schema()` creates idempotently on every run |
| Export document | `schema_version = 6` (4 = metadata is allowlisted, 5 = `plugin_inventory.scope`, 6 = the `subagent_usage` key). `PRAGMA user_version` / `SCHEMA_VERSION` stayed at **2**: that counter exists because `CREATE VIEW IF NOT EXISTS` cannot re-define a view, and this round added a table, not a view — `ensure_schema()` runs the table DDL every time, so no bump was owed |
| Test suite | 530 passed, 0 failed (re-counted 2026-10-04, after the coverage-gap round: new `test_skill_stats.py` and `test_skill_db_helpers.py`, the TUI write-action/button cases, and the added `skill_db` leaf-helper, migration-race, backup and health branches); the shipped `__selftest()` is at 62/62 assertions and `test_selftest_is_green_end_to_end` refuses below 62 — on `python3 -m pytest scripts/tests -q` **and** `.venv/bin/python -m pytest scripts/tests -q`, and again with `OPENCODE_SKILL_TRACKER_SKILLS_DIR=/tmp/does-not-exist` to prove the suite is hermetic |
| AgentOS advisor store (measured 2026-10-03; it is another process's state and moves) | `/home/shade/Public/AgentOS/store/aos.db` — 22 telemetry events, 51 retrieval rows over 8 memories, 14 memories, **83 loop files**. Stages are no longer small: the newest loops run `validate` at 31–37 s, i.e. ~30× the 1200 ms budget. **The TUI has no Advisor page any more** (removed 2026-10-03 at the owner's request); `skillt agentos` is now the only surface that opens that store, and `test_tui_never_reads_the_advisor_store` pins that no tab reads it. `skillt agentos` needs `AGENT_OS_ROOT` in the environment it runs in; **it is `export`ed in `~/.zshrc`** since 2026-10-01, so an interactive shell has it, while anything non-interactive (cron, systemd, `env -i`) must set it itself. Whether a loop is a live-test sample or real usage is not this project's call to make; the store is read-only here and the newest loops are labelled with their own `model`. |
| claude-mem neighbour (re-measured 2026-10-04 15:56; another process's state, and it moves) | `~/.claude-mem/` — its ledger: 403 observations, newest 0.0 d. Its own files: `inject-trace.log` 157 lines → **103 injected** (59 bare + 44 with `source=`), 40 loaded, `unrecognized` 0; the worker log **rolled over to `logs/claude-mem-2026-10-04.log`** (374,014 bytes / 2,463 lines) → INFO 2,290 · WARN 168 · **ERROR 5**, `unparsed` 0 — the reader took the newest dated file by mtime and left 2026-10-03's 609,860-byte log (with its 57 ERROR lines) alone, which is `test_only_the_dated_worker_log_is_read` playing out on real data; `worker.pid` → pid alive, port 37700, `startToken` never read. Reading all three costs **8.2–15.8 ms** (five runs) — that is what the Plugins page's dim line pays on a repaint, and why `claude_mem_http` is never called from it. The 9 tools `claude-mem-inject.js` now registers have **0** recorded calls, so the inventory line and the activity line still describe different things. |
| Tracker log | `~/.config/opencode/logs/skill-tracker.log`, 1138 lines over 11.3 d (first line 2026-09-23). It holds **4 `[err]` lines, all `selftest FAIL: …`, written by this project on 2026-10-04 07:32Z** while the subagent assertions were still red — `doctor`'s `log.errors` warns on them, and they are residue from a test run, not a capture fault. **The gap they exposed is fixed** (2026-10-04): `__selftest()` now redirects its own log to `skill-tracker-selftest.log` beside its temp database unless `OPENCODE_SKILL_TRACKER_LOG` names a path explicitly, so the documented hand-run recipe can no longer touch this file — verified by running that recipe and comparing this log's sha256 before and after (unchanged), including a deliberately failed selftest, whose `[err]` line went to the scratch file. Those 4 historical lines are still here; they predate the fix and are not a capture fault. Do not delete lines from a live log by hand. |
| Backup timer | **enabled** — `systemctl --user is-enabled skillt-auto-backup.timer` → `enabled`, next run daily 00:09 CST, `Linger=yes`; verified by running the service once (exit 0, backup created, 0 deleted) |
| Loose backups (M19) | 5 files in `~/.local/share/opencode/` **outside** `BACKUP_DIR`, never pruned; the 4 pre-2026-10-01 ones still contain the M14 prompt text |
| Backup default path | **fixed** (M19): manual `skillt backup`, the TUI's `b`, and the automatic pre-`--yes` rollback backups all land in `BACKUP_DIR` now. The last stray beside the database was moved in on 2026-10-01, so retention sees everything: its first dry-run said `kept: 2, delete: 1` (the older of two same-day 09-23 snapshots) — that deletion happens on the next nightly run |
| Metadata scrub | Applied 2026-10-01: `scrub-metadata --yes` stripped 40 rows (17 skill / 10 mcp / 13 plugin), usage rows and all 46 `error` texts kept; 3 loose backups still holding the text were deleted and the live DB vacuumed. `skillt scrub-metadata` must now report 0, and `grep -l '<一段已知原文>' ~/.local/share/opencode/skill-usage.db*` must find nothing |

Install layout — all four are **symlinks back into this repo**, so editing the
repo is live (no reinstall needed) except that OpenCode must be restarted to
reload the plugin:

```
~/.local/bin/skillt                          -> <repo>/bin/skillt
~/.config/opencode/scripts                   -> <repo>/scripts
~/.config/opencode/skill-tracker             -> <repo>/skill-tracker
~/.config/opencode/plugin/skill-tracker.js   -> <repo>/plugin/skill-tracker.js
```

Verify with `skillt doctor` (`plugin.exists`, `env.*`) or:

```bash
readlink -f ~/.local/bin/skillt ~/.config/opencode/scripts
```

If any of them is a **plain copy**, it will silently stop tracking the repo.
`install.sh` refuses to overwrite a non-symlink; re-run it with `--force` only
after confirming the copy holds nothing you need.

## 1. Invariants — never break these

Each of these is enforced by a test. If a test below goes red, the invariant,
not the test, is what changed.

| Invariant | Guard |
|---|---|
| The plugin is the **only writer**. `skill_db.py` / the TUI / the legacy CLI never record usage. | `AGENTS.md`, `scripts/tests/test_plugin_db.py` |
| Export contract: `export default { id, server }`. A named `export function`, or a bare function default, aborts the **whole** plugin load and recording stops silently. | `test_loader_does_not_enumerate_exports` |
| One row per call: `UNIQUE(session_id, call_id)` + `ON CONFLICT` upsert. | `test_plugin_db.py`, `test_mcp_db.py` |
| Status is monotonic: an observed `error` may override an optimistic `success`, never the reverse. | `UPSERT_*_SQL` CASE branches |
| MCP records argument **names** only. `mcpArgNames()` calls `Object.keys()` and must never read a value. | `test_plugin.py` |
| Message bodies are never read, so never stored — and neither is a session `title`, which the host derives from the user's first message. The writer keeps only the fields anything reads. | `test_readme.py`, `test_the_session_handler_keeps_only_the_fields_it_uses`, `__selftest` assertion |
| A permission `title` is never read by the writer either — no scrubbing run is what keeps it out. `skillt scrub-metadata` still lists `title` for rows older builds wrote. | `test_the_writer_never_names_a_permission_title`, `test_a_denied_call_stores_no_title_but_still_stores_the_denial`, `__selftest` |
| The schema lives in two copies: `TABLES_SQL`/`VIEWS_SQL` (plugin) and `SCHEMA_SQL` (Python). Change both in the same commit. | `test_plugin_and_python_schema_do_not_drift` |
| A view change requires a `SCHEMA_VERSION` bump, or existing databases keep the old view (`CREATE VIEW IF NOT EXISTS` never updates). | `test_migration.py`, `test_schema_sync.py` |
| Capture tests are hermetic: nothing may read the real `~/.config/opencode/skills` or the real plugin log. | `temp_skills` fixture, `_isolated()` env, `test_doctor.py` monkeypatches |
| Tables are sized by `fit_columns()` at render time; Textual's own auto-width pass
  runs in `_on_idle` and is one frame late, which truncates every column to its
  header width. | `test_tui_dashboard_tables_fit_their_content_before_idle`,
  `test_tui_page_tables_cover_every_tab` |
| The suite is green before pushing, on **both** documented commands. | `AGENTS.md` |

## 2. Daily (before trusting the numbers)

```bash
skillt doctor            # expect: 0 FAIL
```

Healthy today means, at minimum:

- `db.quick_check` → `ok`
- `plugin.exists` / `plugin.hooks` / `plugin.mcp_hooks` / `plugin.plugin_hooks` → PASS
- `capture.freshness` → PASS listing **every** stream's age (`skill_usage 0.0d ·
  mcp_usage 2.2d · plugin_usage 0.0d`); **WARN names the stalled one**, which is the
  failure this check exists for. It used to take the newest row across the three
  tables, so one live stream made it permanently green: not a false alarm, a false
  silence. A stream that never recorded a row reads `no rows` and is not stalled;
  `OPENCODE_SKILL_TRACKER_STREAMS` excludes a stream from the verdict while still
  printing its age
- `log.errors` → `0 error line(s)`; any other number means a hook threw and the
  detail is the last `[err]` line
- `env.opencode_version` → PASS only while the installed version equals the pin
- `backups.latest` → within ~1 day once §3 is enabled

## 3. Weekly

```bash
skillt cleanup-selftest          # dry run; must report no synthetic rows
skillt scrub-metadata            # dry run; must report 0 rows (M14 is closed)
skillt sync --dry-run            # scanned == skills row count, changed == 0
skillt claude-mem                # the memory plugin's own ledger: how fresh its
                                 # background capture is, its token totals, and the
                                 # observer's consecutive-failure count (silent in
                                 # `doctor` when that store is absent). Below the
                                 # ledger it prints the plugin's own two log files
                                 # (injected count, ERROR lines) and `worker.pid`
                                 # liveness, then the worker's three HTTP answers.
                                 # `worker log ERROR …` is the line that tells you
                                 # the background syncer is failing while its
                                 # ledger still looks fresh.
skillt agentos                   # advisor loops: over-budget stages, errors, whether
                                 # the recalled memory reached the prompt, and the
                                 # join with measured usage (needs AGENT_OS_ROOT)
wc -l ~/.config/opencode/logs/skill-tracker.log    # growth watch (M17)
stat -c %s ~/.config/opencode/logs/skill-tracker.log   # bytes: doctor reads only the
                                 # first TRACKER_LOG_BYTES_CAP (4 MiB) and then calls
                                 # its own counts a floor, so this number is about the
                                 # file, never about how long doctor takes
```

A redaction removes the *value*; under WAL the *bytes* stay in the `-wal` file
until a checkpoint, which is why `scrub-metadata --yes` ends with
`wal_checkpoint(TRUNCATE)` and says so if the database is busy. Prove it with the
marker you know is in an old row:

```bash
grep -l '一段你确定写过的原文' ~/.local/share/opencode/skill-usage.db*   # must print nothing
```

Then confirm nothing is hiding outside the directory retention can reach. There
should be **no** matches (M19 fixed the default, and the older strays were dealt
with on 2026-10-01):

```bash
for f in ~/.local/share/opencode/skill-usage-backup-*.db; do
  printf '%s  summary_rows=%s\n' "$(basename "$f")" \
    "$(sqlite3 -readonly "file:$f?mode=ro" "SELECT (SELECT COUNT(*) FROM skill_usage WHERE json_extract(metadata,'\$.summary') IS NOT NULL)+(SELECT COUNT(*) FROM mcp_usage WHERE json_extract(metadata,'\$.summary') IS NOT NULL)+(SELECT COUNT(*) FROM plugin_usage WHERE json_extract(metadata,'\$.summary') IS NOT NULL);" 2>/dev/null || echo 'pre-migration')"
done
```

Then confirm the capture is still telling the truth against OpenCode's own
record — this is the single most valuable check in this file, because it catches
"recording silently changed shape" that every other check misses:

```bash
# 1. what the tracker recorded, per MCP server, over the window it covers
sqlite3 -readonly ~/.local/share/opencode/skill-usage.db \
  "SELECT server_name, COUNT(*), SUM(status='success'), SUM(status='error')
   FROM mcp_usage GROUP BY 1;"

# 2. ground truth from OpenCode's part table, same window
sqlite3 -readonly "file:$HOME/.local/share/opencode/opencode.db?mode=ro" \
  "WITH t AS (SELECT json_extract(data,'\$.tool') tool,
                    json_extract(data,'\$.state.status') st, time_created tc
             FROM part WHERE json_extract(data,'\$.type')='tool')
   SELECT substr(tool,1,instr(tool,'_')-1) server, COUNT(*), SUM(st='completed'), SUM(st='error')
   FROM t WHERE tc > strftime('%s','2026-09-28T11:55:00Z')*1000
        AND tool LIKE 'playwright_%' OR tool LIKE 'basic-memory_%'
   GROUP BY 1;"
```

Per-server counts must match. Known, explained differences: rows whose session
was later deleted (the tracker keeps them, `part` loses them), and calls that
predate the tracker's install. **A new unexplained gap is a capture regression** —
check `log.errors` and whether OpenCode restarted without reloading the plugin.

## 4. Monthly / after any change

```bash
systemctl --user is-enabled skillt-auto-backup.timer   # must print: enabled
sqlite3 -readonly ~/.local/share/opencode/skill-usage.db "PRAGMA integrity_check;"
python3 -m pytest scripts/tests -q                     # no venv needed
.venv/bin/python -m pytest scripts/tests -q            # full suite incl. TUI
OPENCODE_SKILL_TRACKER_SKILLS_DIR=/tmp/nope python3 -m pytest scripts/tests -q   # hermetic proof
bash -n bin/skillt                                     # dispatcher still parses
```

Backups are only real if retention is running. Enable it once:

```bash
mkdir -p ~/.config/systemd/user
cp skill-tracker/systemd/skillt-auto-backup.* ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now skillt-auto-backup.timer
loginctl enable-linger "$USER"       # so it runs without a logged-in session
```

Retention policy (`skill_db.py`): keep the 30 most recent daily and 12 monthly;
never touch a file younger than 120 s or one whose name doesn't match
`skill-usage-backup-<date>-<time>.db`.

## 5. After upgrading OpenCode

The builtin-tool allowlist is pinned to one release (M13), and MCP/plugin
inventories resolve only at init (M11/M12). So:

1. `opencode --version` — note the number.
2. `skillt doctor` → `env.opencode_version` must WARN with the drift.
3. Get the real list: `curl -s http://127.0.0.1:<port>/experimental/tool/ids`.
   A running TUI does not necessarily expose it — the port you find may belong to
   a *plugin's* web server, not the host. So start one that loads nothing:
   `~/.opencode/bin/opencode serve --pure --port 14096 --hostname 127.0.0.1`,
   curl it, kill it. `--pure` is what makes this safe while another OpenCode
   session is live: no plugin is loaded, so nothing is captured and nothing is
   written. It still needs the owner's go-ahead — starting a second host instance
   is their call, not a routine check.
   Do **not** substitute `strings` on the installed binary: its TUI view registry
   also carries `batch`, `list`, `lsp` and `plan_exit`, none of which the host
   reports as tool ids (measured 2026-10-03, right after that exact inference had
   already been written into a report as a fact).
4. Update `DEFAULT_BUILTIN_TOOLS` in `plugin/skill-tracker.js` **and** the
   `verified against OpenCode X.Y.Z` comment on the same line — that comment is
   what `doctor` parses, so leaving it behind re-creates the drift it detects.
   `scripts/opencode_compat.py` carries the same claim as `PIN_TOOL_IDS`, plus
   `PIN_LOADER` / `PIN_PERMISSION_SHAPE`; `test_compat.py` fails if the comment and
   `PIN_TOOL_IDS` diverge, so edit them together.
5. Restart OpenCode. Confirm with `skillt plugins` that the inventory refreshed.

6. Re-measure the event payloads after any major upgrade. Payload names have
   already bitten once (M20: the code followed the SDK types while the host sent
   different ones), so the check is to **observe, not to trust the docs**. Build
   a throwaway project under `/tmp` with a probe plugin that logs the *keys and
   enum values* of `permission.asked` / `permission.replied` /
   `command.execute.before`, run it with `OPENCODE_SKILL_TRACKER_DB` pointed at a
   temp database, and compare what arrives with what the tracker reads. Keep the
   probe in `/tmp`: it dumps field values, which is fine for a synthetic sandbox
   session and not fine anywhere real text lives.

   A rename you *do* find is a two-file edit, in one commit: the `CONTRACT` block in
   `plugin/skill-tracker.js` and `scripts/opencode_compat.py`. Nothing else may hold
   a hook id or an event type — `test_compat.py` compares the two sets by value and
   checks the writer registers through `CONTRACT`, so a half-edit fails the build
   instead of silently recording nothing.

If you don't want to touch the plugin, set `OPENCODE_SKILL_TRACKER_BUILTIN_TOOLS`
instead — but then `env.opencode_version` keeps WARNING on purpose.

## 6. Deliberate deviations

Recorded so the next reader does not "helpfully" fix them back into a broken
state.

- **No background timer in the TUI.** Refresh is event-driven and per page: a
  keystroke (5 s throttle) and a tab activation re-read only the page on
  screen, `r` and the first paint re-read every page, and each page prints its
  own `data as of HH:MM:SS`.
  A `set_interval` was tried first: on textual 8.2.8 **any** app timer created
  after the screens mount makes `run_test`'s teardown raise
  `LookupError: <ContextVar name='active_app'>` — reproduced with an empty
  callback, on both App and Screen, and after `timer.stop()`. That takes the whole
  TUI file down with it — 62 tests today, 47 when it was first measured. `test_tui_creates_no_app_timers` pins this.
- **The TUI disables Textual's animations.** `SkillTUI.animation_level` is
  `"none"`, so the tab-bar underline snaps instead of sliding. Textual's `Tabs`
  animates it for 0.3 s at level `"basic"` (`_tabs.py`'s `_highlight_active`), so
  the global `"basic"` level does **not** remove it — only `"none"` does. Measured
  on a 44-skill database: a Dashboard→Skills switch is ~500 ms with the animation
  and ~190 ms without; the animation is ~305 ms of that, against ~70 ms for the
  page's own re-read and ~125 ms for Textual's layout/render floor.
  `test_tui_disables_textual_animations` pins the setting. Do not re-enable it as
  "cosmetic" — it is the largest single cost of switching tabs.
- **A visual assertion must not ask the widget's own helper what it returned.**
  When the Dashboard ranking bars were being built, every rendered bar was
  compared to `rank_bar_width(...)` itself; flattening that ladder to a constant
  left all of them green — an expectation derived from the thing under test is not
  a guard. The column was later removed at the owner's request, and the lesson
  stays: `test_tui_trend_rows_are_a_fixed_width` names the absolute row width
  (20) in its own breath, not just the equality between the lines.
- **No page of the TUI opens the advisor store.** The Advisor tab was removed on
  2026-10-03 at the owner's request; `skill_db.agentos_*` and `skillt agentos`
  stayed, so that store is still readable headless — just not from a screen.
  `test_tui_never_reads_the_advisor_store` spies on the one door into it and
  visits every tab, mount and `refresh_all` included, and must see zero calls.
- **The advisor's three recall counts are never collapsed into one.** `retrieved`
  is the engine's self-report, `recalled` is `len(memory_ids)`, `injected` is
  `len(injected_memory_ids)`; a hypothesis can be injected on its own, so merging
  them hides a real fact. The field that used to be printed under a header saying
  "recall" was a reporting error, not a style problem.
  `test_the_recall_counts_surface_and_the_query_dict_does_not` pins the three
  numbers on the projected dict; the fixture loop gives them different values,
  because with equal ones no test could tell the difference.
- **Row dispatch is an explicit kind list ending in `else: return`.** It used to
  end in the plugin call, so any kind it had not heard of was handed three
  positional arguments it did not have and the handler raised `IndexError`. A new
  kind needs a branch, never the `else` —
  `test_tui_row_dispatch_ignores_unknown_kinds` still pins it now that the advisor
  kind is gone with its tab. Row identity never comes out of the key string: the
  key is an opaque lookup token (`dash-skill:<name>`, `recent#<n>`) and the target
  is the tuple stored in `app.row_targets`, because parsing a name back out of a
  key — or out of a rendered cell — is how a value containing the separator turns
  into the wrong row.
- **The advisor store is reached only through `skill_db.agentos_*`, and only as a
  projection.** The digest lists `store/loops/*.json`, `json.load`s one file at a
  time and hands out what `_project_loop` names — a caller never receives
  `task_text`, a recall `query` or a stage payload. The id comes from the file's
  content, not its name, so no path is ever assembled from store-supplied text.
  `test_task_text_and_payloads_are_never_read` and
  `test_a_text_valued_count_field_is_never_counted` are the guards. The screen
  that used to need the same rule was deleted with the tab, and its source-grepping
  test went with it.
- **`bar(value, peak, width)` overflows when `value > peak` and raises on `None`.**
  The advisor's per-stage chart learned this on real data: with the peak taken from
  the stages alone, a loop whose slowest stage was 18 ms against a 1200 ms budget
  drew **1467 blocks** and wrapped the page — the fixture loop had a 4000 ms stage,
  which hid it completely. That chart is gone with the tab. The dashboard trend
  charts still call the same helper and are safe for the same two reasons: their
  peak is `max()` of the values they plot, and a day with no calls is `0`, never
  `None`.
- **Double-click needs no new code, but does need a coordinate that is on a row.**
  Textual posts `RowSelected` when a click lands on the cell that already holds the
  row cursor, i.e. the second click; no click-chain timer, so the no-app-timers rule
  still holds. Measured again on `#dash-top` at 120x40 with five rows: every body
  coordinate tried — `(4, 1)`, `(2, 1)`, `(60, 1)`, `(3, 5)` — opens that row's
  detail page, and `(4, 0)`, the header, opens nothing.
  `test_tui_dashboard_row_double_click_opens_detail` asserts the header first so the
  positive case cannot be vacuous. Note for anyone carrying old numbers over: the
  Advisor table's `(4, 2)` negative was an artifact of that fixture holding **one**
  row — on a table with several rows y=2 is a row and it opens the page.
- **The plugin surface scan is one hop wide, and the entry is never size-capped.**
  A published `main` is often a shim: `opencode-mem@2.28.1`'s `dist/plugin.js` is
  407 bytes and registers nothing — its tool lives in the chunk the shim pulls in
  with `await import("./index.js")`. Scanning only the entry therefore listed
  `tools=[]` for a plugin that does register a tool, and every call it made landed
  in `(unknown)`. The scan now follows the entry's own relative imports:
  `PLUGIN_SCAN_HOPS_MAX` (3), `PLUGIN_SCAN_HOP_MAX_BYTES` (256 KiB each), only
  inside the entry's directory, never transitively — discovery runs inside
  `init()`, which the host watches a deadline on. Both edges of that were found by
  breaking the other one: a first attempt capped the entry too and silently lost
  DCP's `compress` (a 300 KiB bundle that had always been read in full), and a
  stricter tool-key rule (`id: {`) lost it again, because DCP registers
  `compress: cond ? a() : b()`. Guards:
  `test_scan_follows_the_entry_shim_one_hop`, `test_scan_hops_are_bounded`,
  `test_scan_reads_a_large_entry_file_in_full`. Measured cost of the whole `init()`
  over the three real plugins: median 45.1 ms before, 44.7 ms after.
- **Tool ids are the keys at the shallowest level that has any — not "level one".**
  The block used to be matched with a regex that stopped at the first `}`, which did
  two wrong things at once: it invented tools (the keys of a nested
  `args: { query: … }` — claude-mem listed `query` beside its real
  `claude_mem_search`) and it lost them (any tool written *after* a nested object).
  The walk now skips string literals and takes the shallowest populated level, which
  is the level where DCP's `tool:{ ...cond && { compress: … } }` and opencode-mem's
  `tool: { memory: tool({ args: {…} }) }` both come out right. An ALL_CAPS filter
  was tried first and has been removed: it patched the symptom, and a tool genuinely
  named after a constant reference would have been dropped by it. Guards:
  `test_scan_reports_tool_ids_not_parameter_names`,
  `test_scan_finds_a_tool_registered_behind_a_spread`.
- **The inventory is a union, and only two of its four claims are prunable.**
  `scope` records which config listed a plugin. `global` and `localdir` are read by
  *every* session, so a row of theirs that this init did not see again is a plugin
  the user removed, and it is deleted at the end of `loadPlugins()` — that is what
  lets the page's list mean "loaded at the last start" instead of "seen once since
  this database was created". `project` rows are never deleted from elsewhere (a
  session started in another directory cannot see them), and a row that predates the
  column reads as `global`: being wrong there costs only the row, since usage stays
  and the next session from that project re-adds it. Two more guards, each provoked
  by disabling it: an `OPENCODE_SKILL_TRACKER_PLUGINS` override never prunes, and
  nothing prunes unless a global config file actually parsed — a broken or missing
  config looks exactly like "no plugins installed". Guards:
  `test_the_inventory_prunes_a_plugin_the_global_config_dropped`,
  `test_a_project_scoped_plugin_survives_a_session_started_elsewhere`,
  `test_the_env_spec_override_never_prunes`,
  `test_an_unreadable_global_config_never_prunes`,
  `test_a_project_scope_row_is_never_downgraded`.
- **Table column widths are measured here, not by Textual.** `DataTable` recomputes
  auto widths in `_on_idle`, so the first painted frame gives every column its
  header's width (`frozen-gnome-fork-maintenance` → `froze`) and later frames lag by
  one. `fit_columns()` measures the cells it already has and sets `auto_width = False`;
  `_PAGE_TABLES` says which tables each page owns so only the page just written is
  re-measured 2026-10-04 on a read-only snapshot of the live database, with the
  Agents tab in: **≈1 ms for all nine tables** (four runs, 0.7–2.0 ms) and the whole
  `refresh_all()` around it 8.9–14.8 ms. The 2026-10-03 figures — 2.7–3.1 ms over
  eight tables, 21–24 ms for the sweep — were taken while the Recent table still
  held 100 rows; the owner had cleared usage before the re-run, so both passes got
  cheaper on data, not on code. `plain_len`'s no-bracket fast path is what took that
  pass from ~21 ms to ~3 ms when it was introduced.
  The tests read widths **without** a `pilot.pause()` first — pausing lets the idle
  pass repair the frame and the assertion would pass on broken code.
- **A selftest isolates its log, not only its database.** `logPath()` is a `let`
  that `__selftest()` moves to `skill-tracker-selftest.log` beside its temp DB, and
  restores at **both** exits — normal and aborted. Restoring matters: a selftest and
  a live session can share one module instance in the same process, and if the
  override stayed, every later real `[err]` would be written where `doctor` cannot
  see it. An explicit `OPENCODE_SKILL_TRACKER_LOG` is honoured (the suite's harness
  always sets one, which is why CI never showed this); only the shared default is
  redirected. Proved by the recipe in README's "Self-test isolation" section — run
  once clean and once deliberately failed — with the shared log's sha256 compared
  before/after both times.
- **A drawn chart row must not depend on its data for width.** The trend rows
  are a fixed `TREND_ROW_WIDTH` (20) and `.trend` pins the height to
  `TREND_LINES`; `fmt_count` caps a count at five characters. Neither leg is
  decoration: with only one removed the other hides the drift, and the pinned
  height deliberately trades **staggering for clipping** below 70 columns (measured, not estimated). The
  three series are still scaled to their **own** peak on purpose. The guards are
  `test_tui_trend_charts_share_one_line_when_a_series_is_huge`,
  `test_tui_trend_css_height_matches_the_line_count` and
  `test_tui_trend_rows_are_a_fixed_width` — the last measures the rendered text
  with the markup stripped, because `Static.content` keeps it.
- **The two subagent roles get two columns.** `Spawned` is what this agent started, `Ran as` how often this name was somebody's child; they never merge into one number and neither enters `Total`, which stays skill + MCP + plugin on every row — pinned row by row in `test_spawns_and_runs_are_two_columns_and_never_inside_total`, not just against totals that happen to exist. A child-only row's `Success rate` is over its runs (the only outcome it has) and carries `⟨sub⟩` so it cannot be read as a lazy primary agent.
- **Injection grouping conserves, and the day is local.** `by_project` and `by_day` are derived from what the trace line already carries; every line that will not group is counted in `projectless`, `older_days` or `undated` instead of vanishing, and `test_grouping_never_loses_a_count` asserts the sums add back to the totals. Days follow `fmt_time`'s local calendar because the trend charts do (M1); a `project=` value is admitted only if it is slug-shaped (no spaces, no path), so a sentence in that foreign field becomes a count, never a label.
- **Row identity is never parsed out of the row key.** Names contain the
  separators (`@scope/pkg`, `conductor:newTrack`, server names with `_`), so
  every table registers `(kind, ...parts)` in `app.row_targets`. Don't reintroduce
  `split()` on a key.
- **`unified_recent_rows` returns detail columns** (`skill_name`,
  `server_name`+`tool_name`, `plugin_name`+`item_kind`+`item_name`) in addition to
  the display `name`. The display string is not a key.
- **Export allowlists metadata** (`EXPORT_METADATA_KEYS`) instead of dumping the
  column. That changed the document to `schema_version = 4`; adding
  `plugin_inventory.scope` made it 5, and adding the `subagent_usage` key made
  it 6. Bump on any further shape change and update
  `test_export.py` / `test_plugin_db.py` — the shape test now pins the full key
  set, so a new key cannot arrive silently.
- **`scrub-metadata` edits rows in place rather than deleting them**, and only
  after a backup when `--yes`.
- **A neighbour's log is scanned from the start, not from the tail.** Its worker
  log held **57 ERROR lines**, every one of them in the early part of the file; a
  64 KB tail reported 0 of them. So `CLAUDE_MEM_LOG_BYTES_CAP` cuts the *end*, and
  when it bites the result carries `truncated` and the counts are labelled floors
  rather than totals. An estimate of "1–2 ms to read the whole file" was written
  into the plan before it was measured; it is really **8.2–15.8 ms** for the three
  files, which is why the HTTP probe stays off the repaint path.
- **The level is the second bracket, the category the third.** `[time] [LEVEL]
  [category] message`: reading ERROR as a category counts nothing at all, and
  counting categories instead of levels would have called a clean day healthy.
  `_LOG_LINE_RE` is positioned on that, and a line that does not match is counted
  in `unparsed` instead of being dropped.
- **Files before HTTP, and HTTP never inside the TUI.** `claude_mem_worker()` reads
  `worker.pid` and calls `os.kill(pid, 0)` — a syscall, so liveness and port cost
  microseconds and still answer when nothing is running. The three endpoints are
  only reached from `skillt claude-mem`, share **one** deadline (`clock` is
  injected so that arithmetic is tested without sleeping), and are projected by
  field name **and** by type: `database.path`, `worker.version` and chroma's free
  text `details` are dropped because they are not integers or booleans, not because
  a rule named them. Guards: `test_the_http_probe_shares_one_deadline`,
  `test_no_path_or_free_text_crosses_the_http_whitelist`,
  `test_no_http_is_reachable_from_the_tui_refresh_path`.
- **doctor is offline, and a stopped worker is not a warning.** The worker is an
  on-demand process; its absence is a state, not a fault, so `claude_mem.capture`
  prints `worker down` and stays PASS. What does warn is the log's ERROR count —
  the one signal the ledger cannot give, because a syncer can fail every attempt
  while its newest row still looks fresh.
- **The Agents page has no sort, on purpose.** `action_cycle_sort` returns for
  `tab-agents`; falling through to the `else` would cycle the *Skills* sort while
  the user stands on Agents and sees nothing change. Its `(unknown)` row is real
  and explained on the page (M23).
- **The Plugins page is a union, and `plugin_stats_rows` is not.** The page and
  `skillt plugins` read `plugin_surface_rows` (inventory ∪ usage) because a
  registered tool nobody called produced no row at all — eleven surfaces rendered
  as one line, and the owner read that as a broken recorder. The dashboard's
  Top Plugins still uses the usage-only view: a top-10 of zeros is not a ranking.
  Excluded plugins get no 0 rows; they have no surface by decision. `registered`
  and `ever_called` stay two separate fields so no consumer can merge them into
  one ambiguous zero.
- **A subagent is recorded as an event, never as a fourth stream.** `Spawned` sits
  beside `Total` and not inside it, because `task` is a builtin and its run may
  contain zero measured calls — folding it in would let one column mean "calls"
  or "calls plus events" depending on whether a session delegated. Held by
  `test_agent_rows_gain_a_spawned_count_and_total_stays_the_three_streams`.
- **Both write paths are wired, because which one the host fires for a builtin is
  not known and not assumed.** The hook pair and `message.part.updated` each call
  the writer, and `UNIQUE(parent_session_id, call_id)` with a fill-only rule turns
  the two into one row: the event adds the child session id a hook cannot know,
  and never overwrites a name already recorded. `__selftest` drives both and
  asserts one row. **Which path a real OpenCode uses is still unverified** — that
  needs one live session after a restart, and the answer would not change the code.
- **`subagent_type` is gated by shape, not by meaning.** `SUBAGENT_LABEL_RE`
  admits no spaces, no path, at most 40 characters; anything else is counted as
  `(unnamed)`. That is a bound, not a prose detector — a 39-character hyphenated
  token passes it, and this round's own sentinel slipped through exactly that way
  until the test value was made a sentence. The payload's prose fields
  (`prompt`, `description`, `title`, `output`, `error`) are named nowhere in the
  writer; `test_the_subagent_writer_never_names_a_text_field` reads those three
  function bodies to keep it true, and a source scan that cries wolf on its own
  comment is a scan nobody can edit.

## 7. Known limitations

M1–M23, with reproduction notes:
[README.zh-CN.md §9](README.zh-CN.md#-9-已知限制) /
[README.md](README.md#-known-limitations).
The ones most likely to bite during maintenance: **M14** (historical prompt text
in rows — scrubbed 2026-10-01, and the backups predating it were deleted),
**M19** (fixed: backups had two destinations and retention only reached one —
re-check that no `skill-usage-backup-*.db` sits beside the database again),
**M15** (calls that never completed leave no row), **M16** (one failed git
lookup silences `branch` for a directory), **M17** (no log rotation), **M18** (a
late error text can be dropped by the `COALESCE` on metadata) and **M23** (that
same `COALESCE` freezes `agent`, so the Agents page counts the agent a session
*first* reported and `(unknown)` measures capture order, not anonymous work).

## 8. Deferred (P2) — ranked, with why they are not fixed

| Item | Cost | Why deferred |
|---|---|---|
| `branchByDir` negative cache has no TTL (M16) | small | touches the capture path; AGENTS.md requires tests for capture changes, and the dominant cause of nulls here is non-git session dirs |
| `metadata` COALESCE drops a late error text (M18) | medium | inside the load-bearing dedup upsert |
| Init blocks ~1.5 s on MCP discovery (p90 of 164 inits) | medium | lowering `MCP_STATUS_TIMEOUT_MS` risks mis-detecting servers, which is worse than slow startup |
| No log rotation (M17) | small | the read is capped now (`TRACKER_LOG_BYTES_CAP`); the file still only grows, and rotate-vs-journald is still a policy decision |
| `skillt agentos` re-reads every loop file in `store/loops/` on each call (`--limit N` caps how many are projected, not how many are listed) | small | the store holds five loops today; there is no filename↔loop_id convention to exploit, and a cache would mean holding a second copy of state another process owns. It was the Advisor tab that made this a per-keystroke cost; the tab is gone, so the cost is now paid only when the command is run |
| The advisor digest reads AgentOS's stage field names | small | `retrieved` / `injection_chars` are engine internals; renaming one empties those numbers instead of breaking anything, and `_count_only` refuses to count text. A printed count that used to be a number turning into `-` is the signal |
| The Plugins page re-reads claude-mem's whole worker log on every repaint — measured 8.2–15.8 ms for all three files | small | Bounded by `CLAUDE_MEM_LOG_BYTES_CAP` (4 MiB), and the page repaints on a tab change or a keypress at most once per `REFRESH_STALE_AFTER_S`. An mtime/size cache would make the line stale in exactly the case it exists for — noticing the syncer *started* failing — and milliseconds are not the freeze that banned HTTP from this path |
| `plugin_inventory` shows absolute paths for local plugins | cosmetic | needs a display-only shortening plus a test |
| `skill_versions` grows without bound | small | needs a retention decision; no pruning exists today |
| Backfill `subagent_usage` from OpenCode's own `part` table (the 84 pre-restart spawns: `explore` 61, `general` 13, `auditor` 5, `researcher` 3, `reviewer` 2, `verifier` 1) | small — one read-only pass, 8 whitelisted json paths, idempotent on `(parent_session_id, call_id)` | **the owner declined it 2026-10-04**: it would add a live foreign database (OpenCode's own, WAL, being written while a session runs) to this project's read set for history nobody is asked about daily. `Ran as` therefore says "since OpenCode last started", and M24 says so on the page. Revisit only if a real question needs the older numbers. |

### Resolved by live verification (2026-10-01)

Both previously-unproven capture paths were exercised against a real
`opencode run` session in a `/tmp` sandbox (isolated DB via
`OPENCODE_SKILL_TRACKER_DB`, model `opencode/space-bunny-free`, synthetic
prompts, binary invoked by absolute path so the shell alias never applies):

- **Plugin command capture works.** `--command dcp-compress` fired
  `command.execute.before` and wrote
  `@tarquinen/opencode-dcp@3.2.0 / command / dcp-compress / command_call / unknown`.
- **The denial path was broken, and is fixed.** OpenCode 1.18.33 emits
  `permission.asked` then `permission.replied`, with the id in `id`, the refused
  call in `tool.callID`, and the answer in `reply` keyed by `requestID`. The
  tracker listened for `permission.updated` and read `permissionID` / `response`
  — names this host never sends — so **every real rejection wrote nothing**,
  which is why `denied` was 0 in production rather than merely unobserved.
  Rejections now land correctly, but M20 still holds: the host gates only
  `edit`/`bash`/`webfetch`/`doom_loop`/`external_directory`, none of which the
  tracker measures, so expect 0 `denied` rows regardless.

## 9. Rollback

One commit per change, code before docs (Conventional Commits). To back out a
phase:

```bash
git log --oneline -12
git revert <sha>          # prefer revert over reset; the suite must stay green
```

`scrub-metadata --yes` is the one destructive operation in this project. It
writes a `skill-usage-backup-*.db` next to the database **before** applying, so
restoring is: `cp ~/.local/share/opencode/skill-usage-backup-<ts>.db
~/.local/share/opencode/skill-usage.db` with OpenCode not running.

## 10. Report format for each phase

Every change here is reported with the same five items, so a reader can accept
or reject it without re-deriving anything:

1. **What changed** — behaviour, not code.
2. **How to see it** — the exact command or keystroke that demonstrates it.
3. **Test delta** — before → after counts, with any red-first evidence.
4. **Deviations** — anything done differently from the approved plan, and why.
5. **Rollback** — the commit to revert.

## 11. How a `green` is produced

The suite is only evidence if the command that ran it could have failed loudly.
Four traps, all of them paid for on this machine:

- **Chaining swallows failures.** `a && b` reports only `b`'s status, and
  `pytest -q | grep -c passed` reports `grep`'s. One command per check, its own
  status, no pipe: `python3 -m pytest scripts/tests -q > /tmp/pt.log 2>&1;
  echo "exit=$?"`.
- **`${PIPESTATUS[0]}` is bash.** This project is driven from **zsh**, where the
  array is `$pipestatus` and is 1-indexed; the bash spelling expands to nothing,
  so a failed `git push` printed `push exit=` — a blank status that reads as
  though the check had run. Verified here: `false | true` then
  `${pipestatus[1]}` = `1` and `${PIPESTATUS[0]}` = *empty*. Take statuses
  without a pipe.
- **A tail is not a conclusion.** A `tail -3` once described a backup as already
  pruned while it was still on disk. There is no pytest config in this repo — no
  `addopts`, no summary line a second `-q` could suppress — so the count is
  always there: read the last line of the log file, not a fragment of a screen.
- **A red-check leaves a canary behind.** Proving a guard can fail means editing
  the implementation or the fixture for a moment, and a moment that survives the
  edit is a fake green. `TEMP-` markers in tracked source are refused by
  `test_no_canary_or_scratch_marker_survives_in_tracked_source`; one in a
  conftest fixture passed 318 tests, so the sweep is not paranoia. Restore by
  re-reading the file, not by trusting that the last edit undid the first. The restore
  command can abort on its own: `rm` and `cp` are aliased interactive here, so
  `cp a b && pytest …` stopped at an unanswered prompt, left the canary in the file,
  and the green check after it never ran at all. Use `command cp -f` / `command rm -f`,
  and print `git diff --stat` after restoring — an empty diff is the proof, not an
  `exit=0` from a command that was skipped.
