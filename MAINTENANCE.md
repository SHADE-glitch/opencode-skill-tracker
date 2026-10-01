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
| OpenCode | 1.18.33 (`opencode --version`) |
| Plugin SDK | `@opencode-ai/plugin` 1.18.4 |
| Runtime for the plugin | Bun (`~/.bun/bin/bun`) — `bun:sqlite` |
| TUI venv | `.venv` (Python 3.13.14, textual 8.2.8) |
| Database | `~/.local/share/opencode/skill-usage.db`, mode 0600, WAL, 582 KiB |
| Rows | 39 skills · 21 skill_usage · 550 mcp_usage · 48 plugin_usage · 76 skill_versions · 5 plugin_inventory |
| Schema | `PRAGMA user_version = 2`, `SCHEMA_VERSION = 2` |
| Export document | `schema_version = 4` (4 = metadata is allowlisted) |
| Test suite | 289 passed, 0 failed — on `python3 -m pytest scripts/tests -q` **and** `.venv/bin/python -m pytest scripts/tests -q`, and again with `OPENCODE_SKILL_TRACKER_SKILLS_DIR=/tmp/does-not-exist` to prove the suite is hermetic |
| AgentOS advisor store | `/home/shade/Public/AgentOS/store/aos.db` — 16 telemetry events, 13 retrieval rows over 6 memories, 14 memories, 5 loops. `skillt agentos` needs `AGENT_OS_ROOT` in the environment it runs in; **it is `export`ed in `~/.zshrc`** since 2026-10-01, so an interactive shell has it, while anything non-interactive (cron, systemd, `env -i`) must set it itself. Every stage so far has been 15–172 ms against a 1200 ms budget. **The 5 loops are live-test samples** (`model=opencode/space-bunny-free`), not production usage |
| Tracker log | `~/.config/opencode/logs/skill-tracker.log`, 533 lines over 7.7 d, 0 `[err]` |
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
| Message bodies are never read, so never stored. | `test_readme.py`, `__selftest` assertion |
| The schema lives in two copies: `TABLES_SQL`/`VIEWS_SQL` (plugin) and `SCHEMA_SQL` (Python). Change both in the same commit. | `test_plugin_and_python_schema_do_not_drift` |
| A view change requires a `SCHEMA_VERSION` bump, or existing databases keep the old view (`CREATE VIEW IF NOT EXISTS` never updates). | `test_migration.py`, `test_schema_sync.py` |
| Capture tests are hermetic: nothing may read the real `~/.config/opencode/skills` or the real plugin log. | `temp_skills` fixture, `_isolated()` env, `test_doctor.py` monkeypatches |
| The suite is green before pushing, on **both** documented commands. | `AGENTS.md` |

## 2. Daily (before trusting the numbers)

```bash
skillt doctor            # expect: 0 FAIL
```

Healthy today means, at minimum:

- `db.quick_check` → `ok`
- `plugin.exists` / `plugin.hooks` / `plugin.mcp_hooks` / `plugin.plugin_hooks` → PASS
- `capture.freshness` → PASS with a table named and an age; **WARN means the
  plugin stopped writing**, which is the failure this check exists for
- `log.errors` → `0 error line(s)`; any other number means a hook threw and the
  detail is the last `[err]` line
- `env.opencode_version` → PASS only while the installed version equals the pin
- `backups.latest` → within ~1 day once §3 is enabled

## 3. Weekly

```bash
skillt cleanup-selftest          # dry run; must report no synthetic rows
skillt scrub-metadata            # dry run; must report 0 rows (M14 is closed)
skillt sync --dry-run            # scanned == skills row count, changed == 0
skillt agentos                   # advisor loops: over-budget stages, errors, whether
                                 # the recalled memory reached the prompt, and the
                                 # join with measured usage (needs AGENT_OS_ROOT)
wc -l ~/.config/opencode/logs/skill-tracker.log    # growth watch (M17)
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
3. Get the real list: `curl -s localhost:<port>/experimental/tool/ids` while
   OpenCode's server is up.
4. Update `DEFAULT_BUILTIN_TOOLS` in `plugin/skill-tracker.js` **and** the
   `verified against OpenCode X.Y.Z` comment on the same line — that comment is
   what `doctor` parses, so leaving it behind re-creates the drift it detects.
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

If you don't want to touch the plugin, set `OPENCODE_SKILL_TRACKER_BUILTIN_TOOLS`
instead — but then `env.opencode_version` keeps WARNING on purpose.

## 6. Deliberate deviations

Recorded so the next reader does not "helpfully" fix them back into a broken
state.

- **No background timer in the TUI.** The screen re-reads on keystroke (5 s
  throttle), on tab activation and on `r`, and prints `data as of HH:MM:SS`.
  A `set_interval` was tried first: on textual 8.2.8 **any** app timer created
  after the screens mount makes `run_test`'s teardown raise
  `LookupError: <ContextVar name='active_app'>` — reproduced with an empty
  callback, on both App and Screen, and after `timer.stop()`. That takes 47 TUI
  tests down. `test_tui_creates_no_app_timers` pins this.
- **Row identity is never parsed out of the row key.** Names contain the
  separators (`@scope/pkg`, `conductor:newTrack`, server names with `_`), so
  every table registers `(kind, ...parts)` in `app.row_targets`. Don't reintroduce
  `split()` on a key.
- **`unified_recent_rows` returns detail columns** (`skill_name`,
  `server_name`+`tool_name`, `plugin_name`+`item_kind`+`item_name`) in addition to
  the display `name`. The display string is not a key.
- **Export allowlists metadata** (`EXPORT_METADATA_KEYS`) instead of dumping the
  column. This changed the document to `schema_version = 4`; bump on any further
  shape change and update `test_export.py` / `test_plugin_db.py`.
- **`scrub-metadata` edits rows in place rather than deleting them**, and only
  after a backup when `--yes`.

## 7. Known limitations

M1–M19, with reproduction notes:
[README.zh-CN.md §9](README.zh-CN.md#9-已知限制) /
[README.md](README.md#known-limitations).
The ones most likely to bite during maintenance: **M14** (historical prompt text
in rows — scrubbed 2026-10-01, and the backups predating it were deleted),
**M19** (fixed: backups had two destinations and retention only reached one —
re-check that no `skill-usage-backup-*.db` sits beside the database again),
**M15** (calls that never completed leave no row), **M16** (one failed git
lookup silences `branch` for a directory), **M17** (no log rotation), **M18** (a
late error text can be dropped by the `COALESCE` on metadata).

## 8. Deferred (P2) — ranked, with why they are not fixed

| Item | Cost | Why deferred |
|---|---|---|
| `branchByDir` negative cache has no TTL (M16) | small | touches the capture path; AGENTS.md requires tests for capture changes, and the dominant cause of nulls here is non-git session dirs |
| `metadata` COALESCE drops a late error text (M18) | medium | inside the load-bearing dedup upsert |
| Init blocks ~1.5 s on MCP discovery (p90 of 164 inits) | medium | lowering `MCP_STATUS_TIMEOUT_MS` risks mis-detecting servers, which is worse than slow startup |
| No log rotation (M17) | small | needs a policy decision (rotate vs. cap vs. rely on journald) |
| The Advisor tab reads AgentOS's stage field names | small | `retrieved` / `injection_chars` are engine internals; renaming one blanks those cells instead of breaking anything, and `_count_only` refuses to count text. A column that used to hold numbers showing `-` is the signal |
| `plugin_inventory` shows absolute paths for local plugins | cosmetic | needs a display-only shortening plus a test |
| `skill_versions` grows without bound | small | needs a retention decision; no pruning exists today |

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
