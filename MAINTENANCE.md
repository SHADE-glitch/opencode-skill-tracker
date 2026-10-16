# Maintenance checklist

Companion to [README.md](README.md) (English landing page) and
[README.zh-CN.md](README.zh-CN.md) (exhaustive reference). This file is the
**operating** document: what to check, in what order, what "healthy" looks like,
and what is deliberately left as it is.

Chinese version: [MAINTENANCE.zh-CN.md](MAINTENANCE.zh-CN.md).

The long-form maintenance assets are indexed at
[`docs/maintenance/README.md`](docs/maintenance/README.md) — the version matrix, the
host-interface list and the measurements — and the rules for working in this repository
live in [`AGENTS.md`](AGENTS.md). This file is the **operating checklist** rather than a
pointer to them: its numbered sections are stable anchors cited from `AGENTS.md`, both
READMEs and the documents below, so renumbering one is a rename. Do not copy a table out
of `docs/maintenance/` into here — a duplicated fact is the thing that drifts.

| Document | Read it when |
|---|---|
| [`compatibility.md`](docs/maintenance/compatibility.md) | An OpenCode, Textual or Python version moved, or a pin needs re-verifying |
| [`opencode-interface.md`](docs/maintenance/opencode-interface.md) | Adding or renaming a host dependency |
| [`measurements.md`](docs/maintenance/measurements.md) | Restating a size, a rate or a duration |

## 0. Facts about the machine this was last verified on

Machine identity (OpenCode, interpreters, textual) is in
[`docs/maintenance/compatibility.md`](docs/maintenance/compatibility.md) §1; the host's
interface this project leans on, by name, is
[`docs/maintenance/opencode-interface.md`](docs/maintenance/opencode-interface.md);
and every size, rate and duration below is a **run**, so it lives with its command in
[`docs/maintenance/measurements.md`](docs/maintenance/measurements.md) rather than here.
A count in a checklist is read as current forever, so this file states shapes, not
volumes.

| Item | Value |
|---|---|
| OpenCode | **which release is running is a reading, not a fact of this file** — compatibility §1 records the pins, and `ocut sync` reads whatever `~/.local/share/opencode/opencode.db` holds today |
| Host database | `~/.local/share/opencode/opencode.db` — opened `mode=ro` with `PRAGMA query_only = ON`, **never written** |
| Host config | `~/.config/opencode` — read-only; a parse failure is never read as "empty config" |
| Tracker database | `~/.local/share/opencode-usage-tracker/usage.db`, WAL, written only by `ocut` |
| Plugin sidecar | `~/.local/share/opencode-usage-tracker/plugin-events.jsonl`, appended by `plugin/ocut-hook.js`, ingested by `ocut sync` |
| TUI venv | `./.venv` (textual `>=8.2,<9`) |
| Settings | `~/.config/ocut/config.toml` (or `OCUT_CONFIG`) — resolved environment > file > default |
| Schema | `SCHEMA_VERSION` in `ocut_db.py`; a bump clears the sync cursors, forcing one clean re-read |
| Snapshots | `~/.local/share/opencode-usage-tracker/backups/`, newest `BACKUP_KEEP` (5) kept |
| Install layout | two symlinks: `~/.local/bin/ocut -> <repo>/bin/ocut` and `~/.config/opencode/plugin/ocut-hook.js -> <repo>/plugin/ocut-hook.js` |

Install layout — both are **symlinks back into this repo**, so editing the repo is live
(no reinstall needed) except that OpenCode must be restarted to reload the hook:

```
~/.local/bin/ocut                        -> <repo>/bin/ocut
~/.config/opencode/plugin/ocut-hook.js   -> <repo>/plugin/ocut-hook.js
```

Verify with:

```bash
readlink -f ~/.local/bin/ocut ~/.config/opencode/plugin/ocut-hook.js
ocut health
```

If either is a **plain copy**, it silently stops tracking the repo. `install.sh` refuses
to overwrite a non-symlink; re-run it with `--force` only after confirming the copy holds
nothing you need.

## 1. Invariants — never break these

Each of these is enforced by a test. If a test below goes red, the invariant, not the
test, is what changed.

| Invariant | Guard |
|---|---|
| OpenCode's own database and config are **never written**. The host database is opened `mode=ro` with `PRAGMA query_only = ON`; the tracker's own store is the only thing this project writes. | `scripts/tests/test_ocut_sync.py`, `test_the_host_database_is_never_opened_for_writing`, `test_open_source_refuses_to_create_a_host` |
| Message and tool **prose is never read**, so never stored. A `tool` part's `output`/`title`/`metadata`/`error`, a `text`/`reasoning` body and a session `title` are named nowhere. | `scripts/tests/test_ocut_sync.py`, `test_no_prose_field_is_copied_out_of_the_host` |
| The hook `plugin/ocut-hook.js` is the **only writer of `plugin_usage`**, and it never opens OpenCode's database. | `scripts/tests/test_ocut_hook.py`, `test_the_hook_records_a_surface_and_only_plugin_owned_calls` |
| The hook exports its factory **only** as `export default { id, server }`. A named export makes OpenCode call it as a factory and aborts the whole plugin load. | `scripts/tests/test_ocut_hook.py`, `test_default_export_is_the_loader_contract_and_nothing_else` |
| The two token granularities — per-step `model_calls` and the host's per-session rollup `sessions` — are one measurement at two levels and are **never added**. | `scripts/tests/test_ocut_sync.py`, `test_two_granularities_of_the_same_measurement_agree`; `scripts/tests/test_ocut_tui.py`, `test_no_panel_repeats_the_input_number_under_a_second_label` |
| A record the reader cannot use is **counted**, never dropped: an unknown part type, an unparseable JSON blob, an unparseable sidecar line each land in `unparsed`. | `scripts/tests/test_ocut_sync.py`, `test_a_json_blob_that_does_not_parse_is_counted_not_skipped`, `test_an_unknown_part_type_is_counted_per_type`, `test_an_unparseable_sidecar_line_is_counted_not_skipped` |
| A schema or view change requires a `SCHEMA_VERSION` bump, and the bump **clears the sync cursors** so the new shape is re-read instead of trusted. | `scripts/tests/test_ocut_windows.py`, `test_a_version_bump_clears_the_cursors_so_nothing_is_read_again` |
| A database written by a **newer** build is left alone (tables ensured, views untouched) and reported as newer, never rewritten. | `scripts/tests/test_ocut_windows.py`, `test_a_database_from_a_future_build_is_left_alone_and_says_so` |
| Settings are one registry: **environment > file > default**, a bad value is a hard error that names the key, and zero-config creates nothing. | `scripts/tests/test_ocut_settings.py`, `test_environment_beats_the_file_and_the_file_beats_the_default`, `test_an_unknown_key_is_refused_and_names_the_real_ones`, `test_no_file_is_the_normal_case_and_creates_nothing` |
| The suite is green before pushing. | `AGENTS.md` |

## 2. Daily (before trusting the numbers)

```bash
ocut health
ocut sync
```

Healthy means:

- `ocut health` finds the host database (`ok, N MiB`) and the host config (`ok`).
- `ocut health` prints a `schema version` equal to `SCHEMA_VERSION` in `ocut_db.py`.
- Each `cursor` line shows a cursor at or below the host's own `host max`, with no
  `note` — a note means a pruned or renumbered host table, which is expected to happen
  but should be read, not ignored.
- `ocut health` prints `unparsed 0 — every record type was claimed`. A non-zero line is
  the signal that OpenCode's persisted format moved; the reason string names it.
- `ocut sync` ends without an `UNPARSED … this run` line and reports plausible
  `tool_calls` / `sessions` counts.

## 3. Weekly

```bash
ocut sync --full          # rebuild from scratch (snapshots first) when counts look wrong
ocut backup               # a deliberate snapshot before anything risky
ocut plugins              # installed ∪ used; a plugin with 0 uses means the hook saw nothing
ocut format               # the OpenCode format surface, read out of the parser's registry
```

- `ocut sync --full` wipes the tracker tables and re-indexes. It snapshots the database
  first, but the **host** is the source of truth: a session OpenCode has already deleted
  cannot be recovered from it.
- Plugin usage is recorded **only** by the hook, and only after an OpenCode restart. If
  every plugin shows `0 uses`, check that `~/.config/opencode/plugin/ocut-hook.js`
  exists, that `OCUT_HOOK_DISABLE` is unset, and that the sidecar
  `~/.local/share/opencode-usage-tracker/plugin-events.jsonl` is being appended to. A
  `0` is "nothing recorded", not "not used".
- `ocut format` prints the host names the reader depends on. Compare it with
  [`docs/maintenance/opencode-interface.md`](docs/maintenance/opencode-interface.md)
  after any OpenCode upgrade.

## 4. Monthly / after any change

```bash
sqlite3 -readonly ~/.local/share/opencode-usage-tracker/usage.db "PRAGMA integrity_check;"
python3 -m pytest scripts/tests -q                     # no venv needed
.venv/bin/python -m pytest scripts/tests -q            # full suite incl. TUI
bash -n bin/ocut                                       # dispatcher still parses
```

The second and third commands are the same suite on two interpreters; the TUI files skip
where textual is not installed, which is why both are run.

## 5. After upgrading OpenCode

Start with [`docs/maintenance/compatibility.md`](docs/maintenance/compatibility.md) §2 —
it lists which release each claim in this project belongs to — and with
[`docs/maintenance/opencode-interface.md`](docs/maintenance/opencode-interface.md), which
is the full list of host names, payload paths and directories this project reads. Then:

1. `opencode --version` — note the number.
2. `ocut sync` — if it prints an `UNPARSED … this run` line, the host's format moved.
   `ocut health` lists the reasons (`unknown_part_type:<type>`, `unparseable_part_data`,
   …); each one names what the reader could not use.
3. Re-read the format registry: `ocut format`. It is generated from
   `scripts/ocut-sync.py`'s constants, so it cannot describe a surface the code no longer
   reads.
4. If a host field was renamed, fix it in `scripts/ocut-sync.py` and add its declaration
   to the registry **in the same change**; `scripts/tests/test_ocut_sync.py` locks both
   directions — no field may be read without being registered, and no registered field
   may go unread.
5. `ocut sync --full` to re-index under the new shape (it snapshots first).
6. Restart OpenCode so the hook reloads and re-scans plugin surfaces.

## 6. Deliberate deviations

Recorded so the next reader does not "helpfully" fix them back into a broken state.

- **The host is read, never written.** `open_source()` opens
  `file:<host>?mode=ro` with `PRAGMA query_only = ON` and raises when the file is
  missing, rather than creating an empty one. This is why the reader can never repair or
  backfill OpenCode; `ocut sync --full` rebuilds only the tracker's own copy.
- **Plugin usage comes from a sidecar, not from the host.** OpenCode names no plugin
  behind a tool call and records nothing for a command, so `plugin_usage` is the one
  table fed from outside the host database — by `plugin/ocut-hook.js` appending to
  `plugin-events.jsonl`. The hook never opens OpenCode's database and never reads a
  tool's arguments, output or any message text. `OCUT_HOOK_DISABLE=1` turns it off;
  `OCUT_HOOK_EVENTS` moves the sidecar, and the hook and `ocut sync` must agree on it.
- **The hook's attribution is a best-effort static scan.** It reads each installed
  plugin's source for the `tool: { … }` object and the two command-registration shapes,
  one hop into a bundle's relative imports. A plugin whose surface it cannot parse
  records nothing — a guessed owner would be worse than a missing row.
- **A plugin call has no status.** A tool's after-hook carries none and a command has no
  after-hook at all, so `plugin_usage.status` is NULL and the interface prints `-`, not
  `0`. Its `ts` is the hook's own clock, not the host's.
- **The sidecar is capped and rotated by truncation.** `ocut-hook.js` truncates the file
  past 8 MiB; `ocut sync` detects it (the file is shorter than its byte-offset cursor)
  and re-reads from the start — idempotent, because every record is keyed by its own id.
  `OCUT_HOOK_DISABLE`, `OCUT_HOOK_EVENTS`, `OCUT_HOOK_PACKAGES` and
  `OCUT_OPENCODE_CONFIG` are the hook's knobs.
- **The reader counts what it cannot use.** `unparsed` accumulates by reason instead of
  dropping an unknown part type or an unparseable blob; a run that quietly claims nothing
  still prints "done", and the count is what turns a format change into something
  visible. `ocut health` prints the zero case too, so the line itself proves the check
  ran.
- **Two token granularities are shown beside each other and never summed.** `model_calls`
  is the per-step record; `sessions` is the host's per-session rollup. They are one
  measurement at two levels, so adding them would double-count every token.
- **MCP is attributed by name, longest prefix first.** A tool name no configured server
  owns stays `builtin` and is reported as server-shaped-unowned rather than guessed into
  a server.
- **Settings live outside the host's tree.** `~/.config/ocut/config.toml` (or
  `OCUT_CONFIG`), never anything under `~/.config/opencode/**`, which is the host's. A
  file that does not parse is an error that names the key, not a silent fallback.
- **Backups are manual.** `ocut backup` is the deliberate act; `ocut sync --full`, a
  schema bump and `ocut restore` each snapshot first. There is **no systemd** timer any
  more, and retention keeps the newest `BACKUP_KEEP` (5) managed snapshots, never a
  hand-made `usage.db.bak-*` copy.
- **The schema is one copy, in `ocut_db.py`.** `TABLES_SQL` creates the tables and
  `VIEWS_SQL` the views; `SCHEMA_VERSION` is bumped whenever a view changes, and the
  bump clears `sync_state` so the cursors are re-read rather than trusted.
  `plugin_surface` (the hook's tool/command ids) is folded back into `inventory` after
  each filesystem rewrite, so the surfaces survive it.

## 7. Known limitations

M1–M17, with reproduction notes:
[README.zh-CN.md §9](README.zh-CN.md#-9-已知限制) /
[README.md](README.md#-known-limitations).
The ones most likely to bite during maintenance: **M1** (the host is the source of truth;
a call it never persisted is invisible), **M3** (plugin usage needs the hook *and* an
OpenCode restart), **M4** (the scan is best-effort), **M6** (`unparsed` is the format-move
signal), **M10** (the sidecar rotates by truncation), **M13** (Tokens/Usage carry no cache-miss
column, because the host publishes no such field) and **M14** (on the Plugins tab the `state`
label's `installed` means *no evidence*, for the same reason as M3), **M15** (on Tools and
Agents, a missing inventory row reads `not reported` rather than `removed`, because the
builtins cannot be enumerated without running the host) and **M16** (only the two global
external skill roots are read; project-level ones are not).

## 8. Deferred — ranked, with why they are not fixed

| Item | Cost | Why deferred |
|---|---|---|
| Backfill plugin usage for calls the hook missed before it existed | medium | the host names no plugin behind a tool call, so there is nothing to backfill *from*; recovering it would mean guessing an owner |
| Attribute a plugin command the scan cannot parse | medium | a guessed owner is worse than a missing row (M4); the honest answer is a `0` |
| Per-model cost charts | small | `sessions.cost` and `model_calls.cost` are the same money counted twice; only one is shown anywhere |
| `v_plugins` carries `version` / `source` / `path` | small | `q_plugins` has had them all along and the export view does not, so `ocut export` makes a consumer join `inventory` for three fields it could have been handed. Nothing reads the view programmatically yet; fixing it is a schema-visible change to an exported surface, so it waits for a consumer that asks |

## 9. Rollback

One commit per change, code before docs (Conventional Commits). To back out a phase:

```bash
git log --oneline -12
git revert <sha>          # prefer revert over reset; the suite must stay green
```

`ocut restore NAME` is the way back from a bad database state: it snapshots the live
database before replacing it, so the restore itself is reversible. Close the TUI first —
it holds the file open.

## 10. Report format for each phase

Every change here is reported with the same five items, so a reader can accept or reject
it without re-deriving anything:

1. **What changed** — behaviour, not code.
2. **How to see it** — the exact command or keystroke that demonstrates it.
3. **Test delta** — before → after counts, with any red-first evidence.
4. **Deviations** — anything done differently from the approved plan, and why.
5. **Rollback** — the commit to revert.

## 11. How a `green` is produced

The suite is only evidence if the command that ran it could have failed loudly. Four
traps, all of them paid for on this machine:

- **Chaining swallows failures.** `a && b` reports only `b`'s status, and
  `pytest -q | grep -c passed` reports `grep`'s. One command per check, its own status,
  no pipe: `python3 -m pytest scripts/tests -q > /tmp/pt.log 2>&1; echo "exit=$?"`.
- **`${PIPESTATUS[0]}` is bash.** This project is driven from **zsh**, where the array is
  `$pipestatus` and is 1-indexed; the bash spelling expands to nothing, so a failed
  `git push` prints `push exit=` — a blank status that reads as though the check had run.
  Take statuses without a pipe.
- **A tail is not a conclusion.** There is no output-changing pytest config in this repo — no
  `addopts`, no summary line a second `-q` could suppress (`scripts/tests/conftest.py` holds
  fixtures only) — so the count is always there: read the last line of the log file, not a
  fragment of a screen.
- **A red-check leaves a canary behind.** Proving a guard can fail means editing the
  implementation or the fixture for a moment, and a moment that survives the edit is a
  fake green. `TEMP-` markers in tracked source are refused by
  `test_no_canary_or_scratch_marker_survives_in_tracked_source`. Restore by re-reading
  the file, not by trusting that the last edit undid the first.
