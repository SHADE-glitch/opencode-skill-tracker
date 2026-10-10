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
   `chat.params` / `dispose`, all of them registered through one `CONTRACT` block.
   Every registered tool goes through the same wrapper, so MCP and plugin tools
   are captured alongside skills; an MCP tool id is `{server}_{tool}` and is
   resolved against the configured server list by longest-prefix match. Plugin
   surfaces are best-effort statically scanned once at init (the entry file plus
   one hop into its relative imports; tool ids are the keys at the shallowest
   level of the `tool: { … }` object); unresolved tools
   become `(unknown)`, while unresolved commands are dropped fail-closed.
2. `scripts/skill_db.py` — the shared data layer (schema, migrations, queries,
   export, backup). Imported by both the TUI and the tests. It holds **only** the
   tracker's own data; the two neighbours are separate modules (item 4) that import
   it, and it never imports them.
3. `scripts/skill-tui.py` (Textual TUI + `--cli` headless subcommands) and
   `scripts/skill-stats.py` (legacy CLI). Read-only apart from explicit
   `delete` / `clear` / `backup` / `vacuum`.
4. `scripts/skill_db_agentos.py` and `scripts/skill_db_claude_mem.py` — **read-only
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

`scripts/settings.py` is **not** a configuration system either: it is one registry
of numbers this project already used, each with a default, a floor, a flag and a
two-language explanation. `skillt config` reads and writes it, `bin/skillt` passes
the resolved values down, and no module consults the environment for one of these
again. It is separate from `opencode_compat.py` because the two answer different
questions — *what does the host call this?* versus *what number does the owner
want?* — and an OpenCode upgrade must never be able to change an owner's setting.

## Commands

```bash
python3 -m pytest scripts/tests -q                                   # L0
.venv/bin/python -m pytest scripts/tests -q                          # the same suite, venv interpreter
OPENCODE_SKILL_TRACKER_SKILLS_DIR=/tmp/does-not-exist python3 -m pytest scripts/tests -q   # L0, hermetic
bash -n bin/skillt                                                   # syntax-check the dispatcher
./install.sh                                                         # create .venv + link install locations
```

Three commands, one tier: the third proves the suite never reaches this machine's
real skills directory. `requirements.txt` is `textual`, so **an interpreter without
textual installed skips the TUI files** — on this host both interpreters carry
8.2.8 and all of it runs, so the venv command is not a stronger tier and neither
one proves the other passed. Run both anyway: the shipped `pyproject.toml` pins
`textual>=8.2,<9`, and an interpreter that silently skips half the suite is how a
green local run stops meaning anything. Never write a case count into this file:
the suite prints it.

Single file: `python3 -m pytest scripts/tests/test_sort.py -q`.

## CI

`.github/workflows/ci.yml` runs on every `push` and `pull_request`, on
`ubuntu-latest` with Python 3.12. It installs `requirements.txt` and
`requirements-dev.txt`, then runs the gate:

```bash
python3 -m pytest scripts/tests -q
```

That command installs `requirements.txt` first, which is textual, so **CI runs the
TUI files too** — they are not a local-only tier. What CI does not run is everything
gated on a binary or an install it does not have: the `requires_bun` tests (the
runner has no bun) and `test_dispatcher.py` (it skips unless `~/.local/bin/skillt`
exists). That is the whole difference between a CI count and a local one — the last
run ends with a pile of skips while this machine records the same suite with none —
so compare the two logs rather than assuming a tier is missing, and never copy either
total into a document (a CI count and a local count differ by the host's binaries, not
by what the suite contains).
The suite must stay green: a red build is a stop, not a warning, and it is
the same check you run locally. Read the result after every push:
`gh run view <id> --log`.

**CI covers what has been pushed, and nothing else.** There is no `paths-ignore`, so a commit that
stays on this machine has never been seen by CI — measured 2026-10-10, the previous green run's
`headSha` (read with `gh run view <id> --json headSha,conclusion`) was the round's *starting*
commit, which left 38 commits of work with zero CI coverage until the push. So "the suite is green
here" and "CI is green" are two claims, never one by proxy. The gap between their numbers is
*counted*, not assumed: `requires_bun` (27) + the whole of `test_dispatcher.py` (23) are the
runner's 50 skips, and the runner's passes plus those skips equal this machine's total. Commands
and dated readings: [`docs/maintenance/measurements.md`](docs/maintenance/measurements.md) §10.

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
- **A setting is a registry entry or it does not exist.** `scripts/settings.py` is
  the only place a knob is declared: default, floor, flag, and an explanation in
  both languages, or `test_every_key_carries_an_explanation_in_both_languages` and
  `test_every_setting_is_documented_in_both_readmes` fail. Precedence is one rule and
  it is not negotiable: **flag > config file > environment > default**
  (`test_precedence_is_flag_then_file_then_env_then_default`). The file is
  `~/.local/share/opencode/skillt-config.json` — never anything under
  `~/.config/opencode/**`, which is the host's: TOML there is read-only for us and a
  writer would eat its comments. Env names are *derived* from the key
  (`OPENCODE_SKILL_TRACKER_` + key with `.` → `_`), so a new key brings its own
  override and cannot be forgotten. A rejected value is reported as rejected rather
  than disguised as a default, and a file that does not parse is never rewritten —
  `skillt config set` refuses to overwrite the one artifact a user hand-edited.
  Zero-config must keep working: with no file, nothing is created and every number
  is the default (`test_no_config_file_is_the_normal_case_and_changes_nothing`).
- **Anything that deletes is off by default, dry-run first, and cannot be reached by
  an accident.** `retention.usage_days` and `retention.max_skill_versions` ship at 0,
  and at 0 `skillt prune-usage` performs **no write at all** — proven by comparing the
  database bytes, not by trusting the flag (`test_the_knobs_ship_off`,
  `test_off_means_the_plan_says_nothing_and_no_write_happens`). The plan is computed by
  SELECT and the delete removes exactly what that plan counted
  (`test_dry_run_counts_exactly_what_the_delete_removes`); `--yes` takes a backup
  before it touches a row; a row that cannot be dated is never deleted, because
  "older than N days" is a claim that row cannot support; `subagent_usage` is not a
  usage stream and is excluded **by name**. Log rotation is the same shape:
  dry-run unless `--yes`, a **rename** rather than a truncation (the lines that leave
  the live path are the evidence `doctor` reads), pruning restricted to filenames this
  command itself produced, the live file re-created without `O_TRUNC` because the
  writer appends and a line can land between the two, and the rotation cap being *the
  same number the reader reads* — both take the **resolved** `log.max_bytes`, so an
  owner who raises it moves the rotator and the reader together
  (`test_the_reader_bound_moves_with_the_writer_bound`; the first version compared each
  side to the shipped default instead, which is why an override could separate them).
  The systemd timer stays
  opt-in: `install.sh --with-timer`, and every systemd write sits inside that guard
  (`test_installing_the_timer_is_still_opt_in`, checked by the mutating *actions*, not
  by words). Because it is opt-in, its *absence* has to be visible rather than assumed
  healthy: `doctor`'s `backups.scheduled` asks `systemctl --user is-enabled` and WARNs
  on anything but `enabled` — including "cannot ask" — since a fresh backup **file**
  and a live **schedule** are two different facts and only the second one produces the
  next backup (`test_a_removed_timer_is_warned_about_even_though_the_backup_is_recent`
  asserts they may disagree; the unit name is pinned to what `install.sh` copies by
  `test_the_timer_check_asks_about_the_unit_install_sh_installs`).
- **The privacy promise is enforced at the writer, not at the reader.** No message
  body, no prompt text, and no session `title` — the host derives a title from the
  user's first message, so storing it would store the message. `metadata.title` is
  not written by any path any more (`test_the_writer_never_names_a_permission_title`),
  which is what makes M14's "should report 0 rows" hold going forward instead of
  only for the rows scrubbed. `error` stays: it is the one free-text field with a
  reader. Nothing may add a metadata key without naming it in the compat allowlist
  and in the README, and nothing may add a network or telemetry dependency, or a
  dependency that puts message content on disk.
- **What the interface prints about itself is derived from the code that does it.**
  The Data page's key list comes from `MainScreen.BINDINGS`
  (`test_a_new_binding_documents_itself_in_the_help`), the six card labels come from
  `MainScreen.CARD_LABELS` (the grid arithmetic needs the longest one), an empty table's
  explanation comes from `_empty_note()` on the page's own status line, and a colour
  comes from a CSS class against a theme token, never from an imperative assignment.
  Layout claims are **measured at a size** (`run_test(size=(48, 20))`), never read off
  the CSS — the CSS said the card grid was 2 columns narrower than the terminal and the
  layout said 4, which put the breakpoints two columns early. Two traps this exists to
  avoid: a widget that has not been laid out reports `region.width == 0` (assert it
  non-zero before concluding anything from a geometry comparison, and paint the
  width-dependent status through `call_after_refresh`), and a `DataTable` *does* scroll
  its trailing columns, so a narrow table is a discoverability problem, not a data
  problem — measure `virtual_size.width` against `region.width` before "fixing" it.
- **A guard is provoked with a string, not with the repository.** When a check can be
  handed mutated input, take that path: editing a tracked file to make a test go red leaves
  the document broken for the length of every run, survives an interrupted run, and fights
  any other agent editing the same table. `test_an_invariant_row_that_names_a_missing_check_is_caught`
  is the shape — `_invariant_row_mismatches(text)` receives the mutated text, nothing is written
  (this rule was written the other way in `6a244c1` and corrected in `b9eb105`, D-052). Where a
  file really must be edited, the restore is byte-exact, asserted by re-reading, and the
  `__pycache__` entry is purged first — a same-second, same-length edit is otherwise served
  from a stale `.pyc` and the red-check proves nothing.
- **Never commit runtime state**: `*.db`, `*.db-wal`, `*.db-shm`, `backups/`,
  `__pycache__/`, `.pytest_cache/`, `.venv/`. See `.gitignore`.
- **Never commit the working reports.** `/STATE.md`, `/PROFILE.md`, `/AUDIT.md`,
  `/PLAN.md`, `/VERIFY.md` are ignored **root-anchored** (leading slash) because this
  repository is public and those files carry real paths, measured row counts and byte
  sizes of a personal machine. A pattern without the slash would also ignore a
  same-named file inside a subdirectory, which is how a report ends up in a docs
  folder and then in a commit. Proof, both directions:
  `git check-ignore -v STATE.md` (must name the `.gitignore` line) and
  `git ls-files STATE.md PROFILE.md AUDIT.md PLAN.md VERIFY.md` (must print nothing).
  `git add -f` on one of them is the only way to break this, so do not use `-f` here.
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
- **An OpenCode name is written once per language, and the two copies are
  compared.** Python: `scripts/opencode_compat.py`. Writer: the `CONTRACT` block in
  `plugin/skill-tracker.js` — hooks are registered as `[CONTRACT.HOOK_*]:
  safe(CONTRACT.HOOK_*, …)` and bus events are switched on as
  `case CONTRACT.EVENT_*:`, so each host string occurs exactly once, as a value.
  `test_compat.py` compares the two sets by value; it cannot import across
  languages, so it parses the block. Hook ids, event types, payload field paths,
  `~/.config/opencode` layout, tool ids and the version pins come from these two
  places; every other module refers to them. What does **not** belong there:
  numbers and policy this project chose (byte caps, retention counts,
  `BUSY_TIMEOUT_MS`, sanitiser limits, export allowlists, `BACKUP_RE`) — those stay
  with the code that acts on them, or the module becomes a config dump and stops
  reducing coupling. Provenance labels the database stores and the TUI prints
  (`meta.source`, `triggerType ? "event" : "hook"`) are ours, not the host's, and
  stay literal. When you add a read of a host field, add its declaration in the
  same commit: the tests can prove a declared name is still present in the writer,
  and cannot prove an undeclared read exists.
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

## Invariants and how to check them

There is no `INVARIANTS.md` on purpose: a second file listing the same promises is
a second thing to keep in sync, and the drift between the two is the failure this
project keeps finding. Each promise below names the check that holds it and the
command that runs it, so "we have an invariant" is a claim you can execute rather
than read. Run the row that covers what you touched; `python3 -m pytest scripts/tests`
runs all of them, and the hermetic run below proves the suite never touched this
machine's real files.

| The promise | What would break it | The check | Run it |
|---|---|---|---|
| Pure local, no network, nothing that stores message content | a new field read, an HTTP call on a repaint, a dependency that phones home | `test_no_http_is_reachable_from_the_tui_refresh_path`, `test_the_writer_never_names_a_permission_title`, `test_the_subagent_writer_never_names_a_text_field`, `test_no_path_or_free_text_crosses_the_http_whitelist` | `python3 -m pytest scripts/tests -q -k "title or text_field or http or whitelist or forbidden"` |
| The writer is the only writer, and it still loads | a named `export function`, a bare-function default, a schema edit in one copy only | `test_loader_does_not_enumerate_exports`, `test_default_export_is_server_module`, `test_plugin_and_python_schema_do_not_drift` | `python3 -m pytest scripts/tests -q -k "export or loader or drift or schema"` |
| Every OpenCode name lives in one place per language | a handler that spells a host string again, a pin that no longer matches the writer | `test_compat.py` in full — value-compared against the `CONTRACT` block | `python3 -m pytest scripts/tests/test_compat.py -q` |
| Views and schema roll forward instead of silently keeping the old definition | a `CREATE VIEW` edit without a `SCHEMA_VERSION` bump | `test_migration.py` | `python3 -m pytest scripts/tests/test_migration.py -q` |
| Deleting rows or moving logs is opt-in, counted first, and backed up before it happens | a default that becomes "helpful", a dry-run that is a different query from the delete, a rotation that truncates | `test_row_retention.py` + `test_rotate_log.py` | `python3 -m pytest scripts/tests/test_row_retention.py scripts/tests/test_rotate_log.py -q` |
| A schedule is reported as a schedule, never inferred from the file it produces | a `backups.latest` PASS masking a removed timer unit, a check that FAILs a machine for choosing not to install, a check that shells out on the repaint path, "cannot ask systemd" read as healthy | `test_a_removed_timer_is_warned_about_even_though_the_backup_is_recent`, `test_a_disabled_timer_is_named_as_disabled_not_as_missing`, `test_an_unreachable_systemd_is_reported_as_unknown_never_as_healthy`, `test_the_timer_check_never_fails_and_never_shells_out_uninvited`, `test_the_timer_check_asks_about_the_unit_install_sh_installs` | `python3 -m pytest scripts/tests/test_doctor.py -q -k "timer or scheduled or systemd"` |
| The plugin log's reader and its rotator stop at the **same number** | a setting resolved by one side and defaulted by the other, which is how `doctor` starts reporting a floor as a total | `test_the_rotation_cap_is_the_number_the_reader_reads` (both commands, at the default *and* under an override), `test_the_reader_bound_moves_with_the_writer_bound` (spies on the actual read), `test_a_bounded_log_read_reports_a_floor_never_a_clean_bill` | `python3 -m pytest scripts/tests -q -k "bound or rotation_cap or truncated or bounded"` |
| A knob exists in one registry, documented in both languages, and zero-config still works | a setting that only reads the environment, a key added without an explanation, a writer under `~/.config/opencode` | `test_settings.py` + `test_settings_documented.py` | `python3 -m pytest scripts/tests/test_settings.py scripts/tests/test_settings_documented.py -q` |
| A foreign store is read-only, field-whitelisted, and never reached from a repaint | a neighbour importing the TUI, a `open_db()` on someone else's file, a prose column named | `test_module_boundaries.py`, `test_claude_mem.py`, `test_agentos.py` | `python3 -m pytest scripts/tests/test_claude_mem.py scripts/tests/test_agentos.py scripts/tests/test_module_boundaries.py -q` |
| The interface says what it actually does — keys, empty states, colours, widths | a hand-copied key list or label set, an imperative colour, a layout claim read off the CSS | the S8 guards: `test_a_new_binding_documents_itself_in_the_help`, `test_every_table_page_status_line_is_dimmed_throughout`, `test_the_card_grid_switches_where_the_layout_actually_fits`, `test_the_result_colour_is_a_theme_token_and_not_a_literal`, `test_tui_creates_no_app_timers`, `test_tui_disables_textual_animations` | `python3 -m pytest scripts/tests/test_tui.py -q -k "timer or animation or http or dimmed or help or card_grid or right_edge or theme_token"` |
| The two languages say the same thing, including how many limitations there are | a one-language edit, a document that froze its `M1–M<n>` at the number of the day | `test_readme.py` (bilingual parity + `test_the_documented_limitation_range_is_the_range_the_gate_pins` + `test_readmes_document_every_key_the_screen_binds`) | `python3 -m pytest scripts/tests/test_readme.py -q` |
| Every non-feature commit is recorded, and the record is not a diary | a repair that exists only in a commit message, an entry that quotes no commit | `test_record_coverage.py` | `python3 -m pytest scripts/tests/test_record_coverage.py -q` |
| The suite is hermetic | a test that reads `~/.config/opencode/skills` and passes because this machine happens to have rows — or a `doctor` check that asks `systemctl`, which passes *and stays* passing because the check's own `except Exception` swallows a guard that only raises | the same suite, pointed at a directory that does not exist; `never_query_the_hosts_service_manager` (conftest), provoked by `test_the_systemd_tripwire_bites_even_when_a_check_swallows_it` | `OPENCODE_SKILL_TRACKER_SKILLS_DIR=/tmp/does-not-exist python3 -m pytest scripts/tests -q` |

Two of these are about documents, not code, and they are the ones an agent is most
likely to break while "just" writing: **never copy an aggregate count into a doc**
(the suite prints it, and a stale number reads as a claim about current state), and
**never state a limitation range by hand** — `LIMITATIONS` in `test_readme.py` is the
list, and the gate now refuses any document whose `M1–M<n>` disagrees with it.

The table checks its own columns: `test_every_invariant_rows_command_selects_the_checks_it_names`
refuses a row whose `Run it` command cannot select a check its `The check` column names. It is
there because two rows were wrong at once when it was written — `-k "timer or scheduled"` skipped
the unreachable-systemd check and a `key_help` needle skipped the binding-help check — and both
read perfectly well. A selector that quietly deselects is worse than no selector, because it is
run and believed.

## Conventions

- **Code and user-facing output are English** (identifiers, comments,
  docstrings, CLI/TUI text, `skill_db` health suggestions).
- **Docs are bilingual**: `README.md` (English, the landing page) and
  `README.zh-CN.md` (Chinese, the exhaustive reference), plus
  `MAINTENANCE.md` / `MAINTENANCE.zh-CN.md` (the operating checklist).
  `scripts/tests/test_readme.py` asserts that **both** READMEs document every
  limitation id in `LIMITATIONS` (that tuple is the list — do not state a range by
  hand, `test_the_documented_limitation_range_is_the_range_the_gate_pins` refuses a
  document whose `M1–M<n>` has frozen), that `README.zh-CN.md`
  documents every section, that both maintenance checklists name the same checks,
  commands and invariants, and that both key tables cover every key the screen
  binds (`test_readmes_document_every_key_the_screen_binds`) —
  update all four when behaviour changes, or the suite fails.
- **The advisor store is reached only through `skill_db_agentos.agentos_*`, and
  only from the CLI.** No TUI page may open it:
  `test_tui_never_reads_the_advisor_store` spies on the single door and visits
  every tab. Whatever does read it receives only the dict `_project_loop` produced
  — nobody may open the file, open the database, or assemble a path from a
  store-supplied id.
  `test_task_text_and_payloads_are_never_read` and
  `test_a_text_valued_count_field_is_never_counted` are the guards; the
  source-scanning TUI test that used to cover the screen went with that screen.
- **A neighbour module imports the core, never the reverse**
  (`skill_db_agentos.py` / `skill_db_claude_mem.py` → `skill_db.py`), and it reaches
  core state as `db.HOME` / `db.fmt_time`, not by `from skill_db import ...`: a
  name copied in at import time ignores the suite's
  `monkeypatch.setattr(db, "HOME", …)`, which is how a test would silently read
  this machine instead of its fixture. `test_module_boundaries.py` pins both halves.
  A scan that used to slice a section out of `skill_db.py` now reads the reader's
  own file — when a section moves, the scan moves with it or it passes vacuously.
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
  even after `timer.stop()`), and because that teardown belongs to the *app*, it
  fails every test in the TUI file at once rather than the one that added the timer.
  Refresh is
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
