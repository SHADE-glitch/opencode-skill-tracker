# AGENTS.md

Guidance for AI coding agents working in this repository.

This repository is public.

## What this is

An OpenCode plugin plus a CLI/TUI that records **skill, MCP tool and plugin
usage** into a local SQLite database, plus one builtin exception: that a
**subagent** was started (`subagent_usage`, identifiers only — see the rule
below). Three layers, in dependency order:

1. `plugin/skill-tracker.js` — the **only writer**. Runs inside OpenCode's Bun
   runtime. Listens on `tool.execute.before` / `tool.execute.after` /
   `permission.ask` / `command.execute.before` / `event` / `chat.message` /
   `dispose`.
   Every registered tool goes through the same wrapper, so MCP and plugin tools
   are captured alongside skills; an MCP tool id is `{server}_{tool}` and is
   resolved against the configured server list by longest-prefix match. Plugin
   surfaces are best-effort statically scanned once at init (the entry file plus
   one hop into its relative imports; tool ids are the keys at the shallowest
   level of the `tool: { … }` object); unresolved tools
   become `(unknown)`, while unresolved commands are dropped fail-closed.
2. `scripts/skill_db.py` — the shared data layer (schema, migrations, queries,
   export, backup). Imported by both the TUI and the tests.
3. `scripts/skill-tui.py` (Textual TUI + `--cli` headless subcommands) and
   `scripts/skill-stats.py` (legacy CLI). Read-only apart from explicit
   `delete` / `clear` / `backup` / `vacuum`.
4. `scripts/skill_db.py::agentos_*` and `::claude_mem_*` — **read-only
   neighbours**, not a fourth layer. The AgentOS advisor registers no tool and no
   command, so it can never appear in the usage tables, and `skillt agentos` is
   the only surface that reads its own store
   (`$AGENT_OS_ROOT/store/aos.db` + `store/loops/*.json`). claude-mem registers a
   tool but does its real work in its own hooks, which the host never reports to
   us, so `skillt claude-mem` reads what it wrote (counts, timestamps, token
   totals) and nothing else.
   Those counts may be **grouped** (`by_project`, `by_day`) because the trace line
   already carries `project=` and a timestamp — grouping adds no new read, only
   buckets, and any line that will not group is counted (`projectless`,
   `older_days`, `undated`) rather than dropped. A `project=` value is admitted only
   if it is slug-shaped, so prose in that foreign field becomes a number, never a
   label. OpenCode's own `opencode.db` is **not** a data source for this project:
   it holds the subagent history our writer predates, and reading a live foreign
   database for it was considered and declined (MAINTENANCE §8).
   The Advisor tab that used to show this in the TUI was removed on 2026-10-03 at
   the owner's request; the data layer and the command stayed on purpose, so the
   page can come back without re-deriving the projection.

`bin/skillt` is a bash dispatcher that resolves its own location through
symlinks and execs one of the above.

`scripts/opencode_compat.py` is **not** a fifth layer: it is the single place
where OpenCode's own names are written down (hook ids, event types, payload field
paths, the host's directory layout, the version pins, the derived `source` and
`status` vocabularies). Everything else in the project refers to it.

## Commands

```bash
python3 -m pytest scripts/tests -q             # L0: no venv needed
.venv/bin/python -m pytest scripts/tests -q    # the same suite on the venv interpreter
bash -n bin/skillt                             # syntax-check the dispatcher
./install.sh                                   # create .venv + link install locations
```

Both interpreters on this machine carry textual 8.2.8, so **both commands run the whole
suite including the TUI tests** — the venv run is not a stronger tier, and neither one
is a proof that the other passed. Never write a case count into this file: the suite
prints it.

Single file: `python3 -m pytest scripts/tests/test_sort.py -q`.

## CI

`.github/workflows/ci.yml` runs on every `push` and `pull_request`, on
`ubuntu-latest` with Python 3.12. It installs `requirements.txt` and
`requirements-dev.txt`, then runs the gate:

```bash
python3 -m pytest scripts/tests -q
```

That is the same no-venv command as above, so CI runs the same subset (the TUI
tests are skipped). The suite must stay green: a red build is a stop, not a
warning, and it is the same check you run locally.

The checkout must fetch full history (`fetch-depth: 0`): the record-coverage
test walks `git log <anchor>..HEAD` back to the coverage anchor, which a
shallow clone cannot resolve.

**CI maintenance:**

- **Keep CI in step with the code.** Update `.github/workflows/ci.yml` in the *same change* that
  makes it stale — never as a later cleanup.
- **New or renamed tests need no CI edit** as long as CI runs the suite command
  (`python3 -m pytest scripts/tests`); it does, so it picks them up automatically. Only touch CI if
  the *command itself* changes.
- **Environment changes** — a new dependency, a Python version bump, or a new system tool — mean
  updating the workflow's setup/install steps.
- **Renamed or moved code**: the record-coverage test watches a declared list of code paths. If a
  watched path moves, update that list; the test goes red until you do.
- **After a refactor**, confirm CI still exercises the real code and the declared paths still cover
  it. A green CI that no longer touches the changed code is worse than a red one.
- **A new verification tier** (e.g. a live/TUI layer) — decide explicitly whether CI runs it; do not
  add it silently.
- If what CI runs changes, update this section too. CI is a signal, not a gate, until branch protection
  is enabled — read the result after every push.

## Release / version

The package version lives in `pyproject.toml` (`version = "0.1.0"` today) and is
the only version source. Bump it when what is published changes — for a release,
not for an ordinary commit.

It is **separate from `SCHEMA_VERSION`** in `skill_db.py` and from the export
document's `schema_version`: those are *data* versions, bumped when the database
schema or the export shape changes. The package version tracks the release of
the code; the schema versions track the data. Never conflate them.

## Hard rules

- **Plugin export contract.** `plugin/skill-tracker.js` must expose its factory
  only as `export default { id, server }`. OpenCode 1.18.32 invokes *every*
  exported function in a plugin module as a factory and stops only when the
  default export is an object carrying `server`. Adding a named `export
  function` — or regressing the default export to a bare function — makes the
  loader call `__selftest`, whose isolation guard throws, which aborts the whole
  plugin load and silently stops all recording. Covered by
  `test_loader_does_not_enumerate_exports` in `scripts/tests/test_plugin.py`.
- **Do not change the capture logic in the plugin** without tests. Writes are
  deduplicated by `UNIQUE(session_id, call_id)` + `ON CONFLICT` upsert; that
  invariant is load-bearing.
- **A foreign store is read-only and field-whitelisted.** The claude-mem reader
  may name only counters, timestamps and short category labels; its prose columns
  (`prompt_text`, `text`, `narrative`, `tool_input`, …) are named nowhere in the
  code, and `~/.claude-mem/settings.json` — which holds that plugin's API keys —
  is never opened at all (`CLAUDE_MEM_FORBIDDEN_FILES`). A value from a foreign
  enum column is echoed only if it is label-shaped (`_short_label`), and a store
  with none of our tables present is reported unreadable rather than empty.
  The advisor store is read-only and field-whitelisted the same way. Never open it with
  `open_db()` — that helper falls back to read-write and creates a missing file,
  which would hand a foreign store to a "read-only" reader. Use
  `_open_agentos_ro()` (`file:...?mode=ro`). Loop files carry `task_text` (the
  user's task verbatim) and stage `data` carries engine payloads, so
  `_project_loop()` names every field it emits and never splats a dict: adding a
  field upstream cannot leak it here, only omitting one would. One level deeper
  is the same rule: the recall stage's `data` holds `query` (text derived from
  the user's task) right next to `injection_chars`, so `_project_injection()`
  names each field and `_count_only()` accepts integers and sized collections
  only — a text-valued count yields `None`, never a character count.
- **A foreign *file* is read under the same contract as a foreign store.** A log
  line is prose: `inject-trace.log`'s `q=` carries text derived from the user's
  prompt and a worker-log line carries paths and messages, so the reader counts
  **shapes** and returns integers — nothing returns a line, and a line matching no
  known shape lands in `unrecognized` / `unparsed` instead of vanishing. Scan from
  the **start** under a byte cap (`CLAUDE_MEM_LOG_BYTES_CAP`); a tail is blind to
  the early part, and when the cap bites the result says `truncated` so its counts
  read as floors, not totals. An HTTP answer from that neighbour is projected by
  field name **and** by type (`_numbers_only()`), because a field renamed later can
  start carrying a path or a sentence. `worker.pid`'s `startToken` is what
  authenticates to that worker: parsed, never emitted.
- **No HTTP and no subprocess on the repaint path.** A keystroke (throttled by
  `REFRESH_STALE_AFTER_S`), a tab activation and `r` all re-render, so one network
  wait there freezes the interface for the same reason app timers are banned —
  different mechanism, identical failure. `claude_mem_http()` is reached only from
  headless commands, and
  `test_no_http_is_reachable_from_the_tui_refresh_path` pins that the TUI never
  calls it. `os.kill(pid, 0)` and reading files are allowed: measured 8.2–15.8 ms
  for all three claude-mem files together, which that page pays per repaint,
  against 0.7–2.0 ms for the tables.
- **Never commit runtime state**: `*.db`, `*.db-wal`, `*.db-shm`, `backups/`,
  `__pycache__/`, `.pytest_cache/`, `.venv/`. See `.gitignore`.
- **Never commit secrets.** The plugin sanitizes secrets before storing them;
  its self-test fixtures use synthetic values only. Keep it that way.
- **MCP capture records argument *names* only, never values.** `mcpArgNames()`
  calls `Object.keys()` and must never read a property. The MCP server list is
  resolved once at init and detection fails closed (no servers → nothing
  written). `OPENCODE_SKILL_TRACKER_MCP_DISABLE=1` is the kill switch.
- **The one builtin that is recorded is `task`, and only as an event.** A
  subagent spawn goes in `subagent_usage` — `subagent`, the two session ids,
  `status`, `trigger_type`, `duration_ms` — and nothing else, because the same
  payload carries the task in prose (`prompt`), a prose `description`, a prose
  `title`, and the subagent's own prose report. `recordSubagentRun`,
  `trackSubagentPart` and `subagentLabel` name no other field, and
  `test_the_subagent_writer_never_names_a_text_field` scans those bodies to keep
  that true while `__selftest` drives a task part whose text fields all carry a
  sentinel. The name is admitted only if `SUBAGENT_LABEL_RE` accepts it (no
  spaces, no path, ≤ 40 characters): a shape bound, not a prose detector. Do not
  fold spawns into `Total`, do not add another builtin to this path without the
  same field audit, and do not assume either write path (hook pair vs
  `message.part.updated`) is the one the host fires — both write, and
  `UNIQUE(parent_session_id, call_id)` decides.
- **Plugin attribution is deliberately asymmetric.** Non-builtin tools fail
  open to `(unknown)` when the static scan cannot resolve an owner; commands
  fail closed because there is no builtin command allowlist. The default plugin
  exclusions are the tracker itself (realpath-aware) and `opencode-notifier`.
  The builtin tool allowlist is pinned to OpenCode 1.18.34 — refreshed by
  reading `/experimental/tool/ids` off an `opencode serve --pure` instance,
  never by reading the binary's TUI view registry, which lists four extra names
  the host does not report as tool ids. Set
  `OPENCODE_SKILL_TRACKER_BUILTIN_TOOLS` after an OpenCode upgrade if needed.
- **Permission payload shapes are measured, not assumed.** OpenCode 1.18.33
  emits `permission.asked` (id at `id`, refused call at `tool.callID`) followed
  by `permission.replied` (`requestID` + `reply`); the SDK types declare
  `permission.updated` / `permissionID` / `response`, and coding to those names
  made every real rejection write nothing. Both spellings are handled. Before
  changing a permission handler, re-run the `/tmp` probe recipe in
  `MAINTENANCE.md` §5 rather than trusting the SDK.
- **Plugin inventory is init-time state, and every row says which config claimed
  it.** A new/renamed plugin or surface needs an OpenCode restart to be discovered;
  `plugin_inventory` is retained when usage is cleared. `scope` is
  `global` / `localdir` / `project` (or `NULL`, from before the column, treated as
  `global`), and the prune at the end of `loadPlugins()` deletes only
  `global`/`localdir` rows this init did not see again — every session reads those
  two, whereas a project's config is read only by sessions started in it. Never
  prune on the basis of an `OPENCODE_SKILL_TRACKER_PLUGINS` override, and never
  prune at all unless a global config file parsed: a broken or missing config looks
  exactly like "no plugins installed".
- **An OpenCode name is written once, in `scripts/opencode_compat.py`.** Hook ids,
  event types, payload field paths, `~/.config/opencode` layout, tool ids and the
  version pins come from that module; callers import it. What does **not** belong
  there: numbers and policy this project chose (byte caps, retention counts,
  `BUSY_TIMEOUT_MS`, sanitiser limits, export allowlists, `BACKUP_RE`) — those stay
  with the code that acts on them, or the module becomes a config dump and stops
  reducing coupling. When you add a read of a host field, add its declaration in
  the same commit: `test_compat.py` can prove a declared name is still present in
  the writer, and cannot prove an undeclared read exists.
- **The schema lives in two places and must not drift**: `TABLES_SQL` /
  `VIEWS_SQL` in the plugin, and `SCHEMA_SQL` in `skill_db.py`. Nothing else
  carries a copy — `skill-stats.py` imports `skill_db`, and the test fixtures
  build their databases through `db.SCHEMA_SQL` / `db.ensure_schema()`.
  `test_plugin_and_python_schema_do_not_drift` enforces plugin ↔ Python;
  update both copies in the same commit.
- **View changes need a version bump.** `CREATE VIEW IF NOT EXISTS` never
  updates an existing view, so `ensure_schema()` drops and recreates the
  views in `VIEW_NAMES` whenever `PRAGMA user_version` is below
  `SCHEMA_VERSION`, then stamps it. Bump `SCHEMA_VERSION` whenever a view
  definition changes, or existing databases keep the old view.
- `__selftest()` requires `OPENCODE_SKILL_TRACKER_DB` to point somewhere other
  than the production database; it refuses to run otherwise. It isolates its
  **log** the same way: unless `OPENCODE_SKILL_TRACKER_LOG` names a path of its
  own, `__selftest` redirects writes to `skill-tracker-selftest.log` beside that
  temp database, because the documented hand-run recipe sets only the DB and a
  failed selftest used to leave `[err] selftest FAIL` lines in the log
  `skillt doctor`'s `log.errors` counts. The redirect is undone at both exits
  (normal and aborted) — a selftest and a live session can share one module
  instance, and leaving the override in place would hide every later real error.
  Guards: `test_a_hand_run_selftest_never_writes_the_shared_plugin_log`,
  `test_an_explicit_selftest_log_is_honoured_not_overridden`, and the selftest's
  own `selftest never logs to the shared plugin log` assertion.

## Conventions

- **Code and user-facing output are English** (identifiers, comments,
  docstrings, CLI/TUI text, `skill_db` health suggestions).
- **Docs are bilingual**: `README.md` (English, the landing page) and
  `README.zh-CN.md` (Chinese, the exhaustive reference), plus
  `MAINTENANCE.md` / `MAINTENANCE.zh-CN.md` (the operating checklist).
  `scripts/tests/test_readme.py` asserts that **both** READMEs document every
  limitations M1–M24 (the range `test_readme.py` pins), that `README.zh-CN.md`
  documents every section, and that
  both maintenance checklists name the same checks, commands and invariants —
  update all four when behaviour changes, or the suite fails.
- **The advisor store is reached only through `skill_db.agentos_*`, and only from
  the CLI.** No TUI page may open it: `test_tui_never_reads_the_advisor_store`
  spies on the single door and visits every tab. Whatever does read it receives
  only the dict `_project_loop` produced — nobody may open the file, open the
  database, or assemble a path from a store-supplied id.
  `test_task_text_and_payloads_are_never_read` and
  `test_a_text_valued_count_field_is_never_counted` are the guards; the
  source-scanning TUI test that used to cover the screen went with that screen.
- **A hand-drawn bar is scaled against a peak that includes every value it
  prints.** `bar()` multiplies `value / peak * width`, so a reference line (the
  advisor's per-call budget) that can exceed the data's own maximum overflows the
  column — real loops run 0–20 ms against a 1200 ms budget and drew 1467 blocks.
  Fold the reference into the peak, and never hand `bar()` a `None`.
- **Column widths are ours to compute, not Textual's to discover.**
  `DataTable` re-measures auto-width columns in `_on_idle`, one message-pump
  cycle after the rows arrive: the first frame renders every column exactly as
  wide as its header, and each later frame shows the previous content's widths.
  Every table this app populates must go through `fit_columns()` (the per-page
  refresh does it via `_PAGE_TABLES`, the detail shell does it after
  `populate()`), or names get cut to `froze`.
- **No `set_interval`/`set_timer` in the TUI.** On textual 8.2.8, any app timer
  created after the screens mount makes `run_test`'s teardown raise
  `LookupError: <ContextVar name='active_app'>` (empty callback, App or Screen,
  even after `timer.stop()`), which fails the whole TUI file at once (62 tests
  today, 47 when it was first measured). Refresh is
  event-driven and per page: a keystroke (throttled by `REFRESH_STALE_AFTER_S`)
  and a tab activation re-read only the active page, `r` and the first paint
  re-read every page, and each page prints its own `data as of HH:MM:SS`.
  `test_tui_creates_no_app_timers` pins it.
- **Animations are off.** `SkillTUI.animation_level = "none"`. Textual slides the
  tab-bar underline for 0.3 s on every switch (`Tabs._highlight_active`, at level
  `"basic"` — so the global `"basic"` level does not remove it, only `"none"`
  does); that is ~305 ms of a ~500 ms Dashboard→Skills switch on a 44-skill DB,
  against ~190 ms with it off. `test_tui_disables_textual_animations` pins it.
- **Never parse a row's identity out of its `key`.** Names contain the
  separators: `@scope/pkg@1.0/tool`, `conductor:newTrack`, server names with
  `_`. Each table registers `(kind, …parts)` in `app.row_targets` at render
  time and Enter looks that up.
- **Export allowlists metadata** (`EXPORT_METADATA_KEYS` in `skill_db.py`).
  Older builds stored the user's prompt text as `metadata.summary`; the writer
  is gone but rows in real databases are not (`skillt scrub-metadata`). The
  export document is `schema_version = 6` (5 = `plugin_inventory.scope`,
  6 = the `subagent_usage` key) — bump it with any shape change.
- Commit messages follow Conventional Commits (`feat:`, `docs:`, `chore:`,
  `fix:`), code before docs.
- The test suite must be green before pushing. Tests locate files via
  `Path(__file__).resolve()`, so they pass both from the repo and through the
  symlinked install locations — do not replace those with hardcoded paths.

## Recording conventions
- Repairs, performance work, drift guards and withdrawals land in
  [`CHANGELOG.md`](CHANGELOG.md) as `D-###` entries; ids are monotonic and never reused, so a gap
  means an entry was deleted and the check fails rather than calling it cleanup.
- **`feat` commits are out of scope, by class.** This is an original project with no upstream, so a
  feature is the product, not a droppable deviation — features are documented in the READMEs. The
  exclusion lives in `scripts/tests/test_record_coverage.py` as one regex over the commit subject;
  it is not a per-commit skip flag and must not become one.
- `kind` ∈ `fix` | `perf` | `taste` | `guard` | `revert` | `chore`, cut by **who may demand a
  revert**: bug → `fix`; measurable degradation only → `perf`; only my taste → `taste` (zero
  obligation); no behaviour change, detects drift → `guard`; withdraws earlier work → `revert`;
  cleanup owed nothing either way → `chore`.
- A pair of entries that cancel out must both stay (`D-018` + `D-019`, `D-021`): the withdrawal is
  the current state, and a record showing only the reverted-to version would misdirect an upgrade.
- An entry is an assertion **as of its commit**, not current state. Never re-verify an old entry;
  never hand-copy an aggregate count here — the check and the test suite print them.
- **Verification tiers** (named by what the claim needs, not by the tool): **L0** =
  `python3 -m pytest scripts/tests` (no host, no live store), **L1** = a controlled fixture or the
  `/tmp` probe recipe in `MAINTENANCE.md` §5 (a throwaway OpenCode/Textual surface), **L2** = a real
  user session on an installed plugin.
- `Symptom` names the mechanism, never the session: no prompt text, no command lines, no MCP
  argument values, no paths from a real config. This repo's whole privacy stance is "counts, never
  content" — the record is held to the same standard as the code.
- Run the coverage check before committing docs: `python3 -m pytest scripts/tests/test_record_coverage.py`.
  It is part of the suite, so `python3 -m pytest scripts/tests` already runs it.
