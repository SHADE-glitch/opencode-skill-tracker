# AGENTS.md

Guidance for AI coding agents working in this repository.

This repository is public.

> **Shared standard.** Root file names, the process-draft location (`docs/reports/`), the
> `CHANGELOG` entry format, CI version pinning and entry commands, the test entry command, and
> the runtime ignore list are defined once in the machine-wide `STANDARD.md` (outside this
> repository) and are not restated here.
>
> **Push over SSH, never HTTPS.** Verify `git remote get-url --push origin` starts with `git@`
> before pushing; if it starts with `https://`, fix it first — never push over HTTPS.

## What this is

`ocut` — a **passive reader** of OpenCode's own SQLite database plus one optional plugin
hook, and the CLI/TUI that report over the result. Three layers, in dependency order:

1. `scripts/ocut-sync.py` — the **indexer**. Opens OpenCode's own
   `~/.local/share/opencode/opencode.db` `mode=ro` with `PRAGMA query_only = ON` and copies
   what it needs into the tracker's own database. It reads three host tables (`session`,
   `message`, `part`), classifies each `tool` part (a builtin, the `skill` tool, the `task`
   tool, or an MCP server's tool), and reads each `step-finish` part's tokens. A record it
   cannot use is counted in `unparsed`, never dropped. The OpenCode format registry at the
   top of the file is the single list of host names it depends on; `ocut format` prints it.
2. `scripts/ocut_db.py` — the shared data layer: the schema (`TABLES_SQL` + `VIEWS_SQL`), the
   settings registry (`DEFAULTS`, `ENV_NAMES`, `_BOUNDS`, `load_config`), the queries, and
   the snapshot/restore helpers. Imported by the indexer, the CLI and the TUI.
3. `scripts/ocut-tui.py` (the eight-tab Textual TUI) and `scripts/ocut-stats.py` (the
   headless reports). Read-only over the tracker database.

`plugin/ocut-hook.js` is the **only** thing outside the reader that writes anything, and it
writes only its own sidecar (`~/.local/share/opencode-usage-tracker/plugin-events.jsonl`).
OpenCode names no plugin behind a tool call and records nothing for a command, so the hook
scans each installed plugin's source at load and appends `surface` and `call` records; it
never opens OpenCode's database, never writes under `~/.config/opencode`, and never reads a
tool's arguments, output or any message text.

`bin/ocut` is a bash dispatcher that resolves its own location through symlinks and execs one
of the above; global value flags may sit before or after the subcommand.

## Commands

```bash
python3 -m pytest scripts/tests -q                     # standard library only; TUI files skip
.venv/bin/python -m pytest scripts/tests -q            # the same suite, venv interpreter
bash -n bin/ocut                                       # syntax-check the dispatcher
./install.sh                                           # create .venv + link install locations
ocut sync                                              # index the host database
ocut health                                            # the aggregate: cursors, unparsed, totals
```

`requirements.txt` is `textual`, so **an interpreter without textual skips the TUI files** —
on this host both interpreters carry it and all of it runs, so the venv command is not a
stronger tier and neither one proves the other passed. Run both anyway. Never write a case
count into this file: the suite prints it.

Single file: `python3 -m pytest scripts/tests/test_ocut_sync.py -q`.

## CI

`.github/workflows/ci.yml` runs on every `push` and `pull_request`, on `ubuntu-latest` with a
Python matrix of **3.11 and 3.14** — the floor (`pyproject.toml` `requires-python`) and the
README's declared upper bound. It installs `requirements.txt` and `requirements-dev.txt`,
then runs the gate:

```bash
python3 -m pytest scripts/tests -q
```

That command installs `requirements.txt` first, which is textual, so the TUI files run there
too — they are not a local-only tier. What CI does not run is anything gated on a binary the
runner lacks; compare the two logs rather than assuming a tier is missing, and never copy
either total into a document (a CI count and a local count differ by the host's binaries, not
by what the suite contains). The suite must stay green: a red build is a stop, not a warning.
Read the result after every push: `gh run view <id> --log`.

**CI covers what has been pushed, and nothing else.** There is no `paths-ignore`, so a commit
that stays on this machine has never been seen by CI. "The suite is green here" and "CI is
green" are two claims, never one by proxy.

The checkout must fetch full history (`fetch-depth: 0`): the record-coverage test walks
`git log <anchor>..HEAD` back to the coverage anchor, which a shallow clone cannot resolve.

**CI maintenance:**

- **Keep CI in step with the code.** Update `.github/workflows/ci.yml` in the *same change*
  that makes it stale — never as a later cleanup.
- **New or renamed tests need no CI edit** as long as CI runs the suite command; it does.
- **Environment changes** — a new dependency, a Python version bump, a new system tool — mean
  updating the workflow's setup/install steps.
- **Renamed or moved code**: the record-coverage test watches a declared list of code paths.
  If a watched path moves, update that list; the test goes red until you do.
- **A dated one: `ubuntu-latest` migrates to a new Ubuntu release periodically.** If CI turns
  red around such a date with nothing local changed, suspect the image before the code;
  pinning `runs-on` is the fallback, and it is a workflow change this section must record.

## Release / version

The package version lives in `pyproject.toml` and is the only version source. Bump it when
what is published changes — for a release, not for an ordinary commit.

It is **separate from `SCHEMA_VERSION`** in `ocut_db.py`: that is a *data* version, bumped
when the database schema or a view changes. The package version tracks the release of the
code; the schema version tracks the data. Never conflate them.

## Hard rules

- **OpenCode's own database and config are never written.** `scripts/ocut-sync.py`'s
  `open_source()` opens `file:<host>?mode=ro` with `PRAGMA query_only = ON` and raises when
  the file is missing rather than creating an empty one. The tracker's own `usage.db` is the
  only database this project writes. Guards: `test_the_host_database_is_never_opened_for_writing`,
  `test_open_source_refuses_to_create_a_host` (`scripts/tests/test_ocut_sync.py`).
- **The privacy promise is enforced at the reader, not at the reader's caller.** A `tool`
  part's `output`, `title`, `metadata` and `error`; a `text`/`reasoning` body; a session
  `title` (OpenCode derives it from the user's first message, so storing it would store the
  message) — none is read. Only identifiers, statuses, durations and counts land in the
  database. Nothing may add a field read or a metadata key without naming it in the format
  registry and the README, and nothing may add a network or telemetry dependency.
  Guard: `test_no_prose_field_is_copied_out_of_the_host`.
- **The plugin-usage sidecar is the only input that is ours, and the hook is its only
  writer.** `plugin/ocut-hook.js` must export its factory **only** as
  `export default { id, server }`; a named export makes OpenCode call it as a factory and
  aborts the whole plugin load, stopping plugin recording silently. `OCUT_HOOK_DISABLE=1`
  turns it off; `OCUT_HOOK_EVENTS` moves the sidecar and the hook and `ocut sync` must agree.
  The sidecar is capped (8 MiB) and rotated by truncation; the reader detects a file shorter
  than its cursor and re-reads from the start. Guards: `test_default_export_is_the_loader_contract_and_nothing_else`,
  `test_disable_turns_the_hook_into_a_no_op`, `test_a_truncated_sidecar_is_reread_from_the_start`.
- **A record the reader cannot use is counted, never dropped.** An unknown part type, an
  unparseable JSON blob, a missing usage field and an unparseable sidecar line each land in
  `unparsed` by reason. This is the runtime signal that OpenCode's format moved; `ocut sync`
  prints it and `ocut health` lists it, including the zero case. Guards:
  `test_an_unknown_part_type_is_counted_per_type`,
  `test_a_json_blob_that_does_not_parse_is_counted_not_skipped`,
  `test_an_unparseable_sidecar_line_is_counted_not_skipped`.
- **Two token granularities are never summed.** `model_calls` is the per-step record;
  `sessions` is the host's per-session rollup. They are one measurement at two levels, so the
  interface shows them beside each other and never adds them.
  Guard: `test_two_granularities_of_the_same_measurement_agree`.
- **A schema or view change requires a `SCHEMA_VERSION` bump, and the bump clears the sync
  cursors** so the new shape is re-read instead of trusted. A database written by a *newer*
  build is left alone and reported as newer, never rewritten. There is deliberately no
  migration chain: the host is the source of truth, so everything here is re-derivable by
  `ocut sync --full`.
- **Settings are one registry.** `ocut_db.py`'s `DEFAULTS` / `ENV_NAMES` / `_BOUNDS` is the
  only place a knob is declared. Precedence is **environment > file > default** and is not
  negotiable. The file is `~/.config/ocut/config.toml` — never anything under
  `~/.config/opencode/**`, which is the host's. A rejected value is a hard error that names
  the key, never a silent default; zero-config creates no file.
- **Anything that deletes is a deliberate act.** `ocut sync --full`, a schema bump and
  `ocut restore` each take a snapshot first; retention keeps the newest `BACKUP_KEEP` (5)
  managed snapshots and never touches a hand-made `usage.db.bak-*` copy. There is no systemd
  timer.
- **Never commit runtime state**: `*.db`, `*.db-wal`, `*.db-shm`, `backups/`, `__pycache__/`,
  `.pytest_cache/`, `.venv/`. See `.gitignore`.
- **Never commit the working reports.** `STATE.md`, `PROFILE.md`, `AUDIT.md`, `PLAN.md`,
  `VERIFY.md` live under `docs/reports/` and are ignored by a **non-anchored** `reports/`
  rule, because this repository is public and those files carry real paths and measured
  counts of a personal machine. `git check-ignore -v docs/reports/STATE.md` must name the
  `.gitignore` line, and `git ls-files docs/reports` must print nothing.
- **Never commit secrets.** The hook records no arguments and no message text; its fixtures
  use synthetic values only.
- **A guard is provoked with a string, not with the repository.** When a check can be handed
  mutated input, take that path: editing a tracked file to make a test go red leaves the
  document broken for the length of every run. `test_an_invariant_row_that_names_a_missing_check_is_caught`
  is the shape — `_invariant_row_mismatches(text)` receives the mutated text, nothing is
  written.

## Invariants and how to check them

There is no `INVARIANTS.md` on purpose: a second file listing the same promises is a second
thing to keep in sync, and the drift between the two is the failure this project keeps
finding. Each promise below names the check that holds it and the command that runs it, so
"we have an invariant" is a claim you can execute rather than read. Run the row that covers
what you touched.

| The promise | What would break it | The check | Run it |
|---|---|---|---|
| Pure local: the host database and config are read-only, and no prose is stored | a write to OpenCode's files, a prose field read, a network call while the interface paints | `test_the_host_database_is_never_opened_for_writing`, `test_no_prose_field_is_copied_out_of_the_host`, `test_no_http_is_reachable_while_the_tui_paints` | `python3 -m pytest scripts/tests -q -k "never_opened_for_writing or no_prose_field or no_http"` |
| The hook is the only writer of `plugin_usage`, still loads, and can be disabled | a named export, a bare-function default, a call the hook cannot attribute | `test_default_export_is_the_loader_contract_and_nothing_else`, `test_the_hook_records_a_surface_and_only_plugin_owned_calls`, `test_disable_turns_the_hook_into_a_no_op` | `python3 -m pytest scripts/tests/test_ocut_hook.py -q` |
| Schema and views roll forward instead of silently keeping the old definition | a view edit without a `SCHEMA_VERSION` bump, a newer database overwritten | `test_ocut_windows.py` | `python3 -m pytest scripts/tests/test_ocut_windows.py -q` |
| A version bump clears the cursors, and a future database is left alone | a bump that keeps the old cursors, a newer database rewritten in place | `test_a_version_bump_clears_the_cursors_so_nothing_is_read_again`, `test_a_database_from_a_future_build_is_left_alone_and_says_so` | `python3 -m pytest scripts/tests/test_ocut_windows.py -q -k "version_bump or future_build"` |
| A record the reader cannot use is counted, never dropped | an unknown part type or an unparseable blob silently skipped | `test_a_json_blob_that_does_not_parse_is_counted_not_skipped`, `test_an_unknown_part_type_is_counted_per_type`, `test_an_unparseable_sidecar_line_is_counted_not_skipped` | `python3 -m pytest scripts/tests/test_ocut_sync.py -q -k "counted_not_skipped or counted_per_type"` |
| Settings are one registry: environment beats the file beats the default, and errors are loud | a value read only from the environment, a key added without a bound, a broken file silently ignored | `test_environment_beats_the_file_and_the_file_beats_the_default`, `test_an_unknown_key_is_refused_and_names_the_real_ones`, `test_a_file_that_does_not_parse_names_the_file` | `python3 -m pytest scripts/tests/test_ocut_settings.py -q -k "beats_the_default or unknown_key or does_not_parse"` |
| Two granularities of the same measurement agree and are never added | a panel summing `model_calls` and `sessions`, one metric printed under two labels | `test_two_granularities_of_the_same_measurement_agree`, `test_no_panel_repeats_the_input_number_under_a_second_label` | `python3 -m pytest scripts/tests -q -k "two_granularities or repeats_the_input_number"` |
| The window a tab shows is the window it queries, and a row opens the entity it names | a picked window leaking to every tab, a `(server, tool)` row queried by tool alone | `test_a_picked_window_lands_on_the_tab_that_asked_and_on_no_other`, `test_two_servers_sharing_a_tool_name_open_two_different_histories` | `python3 -m pytest scripts/tests/test_ocut_calendar.py scripts/tests/test_ocut_drill.py -q -k "lands_on_the_tab or sharing_a_tool_name"` |
| The two languages say the same thing, including how many limitations there are | a one-language edit, a document that froze its `M1–M<n>` at the number of the day | `test_readmes_document_every_key_the_screen_binds`, `test_the_documented_limitation_range_is_the_range_the_gate_pins` | `python3 -m pytest scripts/tests -q` |

Two of these are about documents, not code, and they are the ones an agent is most likely to
break while "just" writing: **never copy an aggregate count into a doc** (the suite prints
it), and **never state a limitation range by hand** — `LIMITATIONS` in `test_readme.py` is
the list, and the gate refuses any document whose `M1–M<n>` disagrees with it.

The table checks its own columns: `test_every_invariant_rows_command_selects_the_checks_it_names`
refuses a row whose `Run it` command cannot select a check its `The check` column names. A
selector that quietly deselects is worse than no selector, because it is run and believed.

## Conventions

- **Code and user-facing output are English** (identifiers, comments, docstrings, CLI/TUI
  text).
- **Docs are bilingual**: `README.md` (English landing page) and `README.zh-CN.md` (Chinese
  reference), plus `MAINTENANCE.md` / `MAINTENANCE.zh-CN.md` (the operating checklist).
  `scripts/tests/test_readme.py` asserts that **both** READMEs document every limitation id
  in `LIMITATIONS` (that tuple is the list — do not state a range by hand), that both
  maintenance checklists name the same checks, commands and invariants, and that both key
  tables cover every key the screen binds (`test_readmes_document_every_key_the_screen_binds`).
- **A module reaches core state through the module object, never by value.** A name copied in
  at import time ignores the suite's `monkeypatch.setattr`, which is how a test would
  silently read this machine instead of its fixture.
- **Column widths are ours to compute, not Textual's to discover.** A populated table is
  sized in the same turn it is filled, or the first frame renders each column header-wide.
- **No `set_interval`/`set_timer` in the TUI.** Refresh is event-driven and per page; an app
  timer created after the screens mount can break `run_test`'s teardown for the whole file.
- **Never parse a row's identity out of its `key`.** Names contain the separators
  (`@scope/pkg@1.0/tool`, `conductor:newTrack`, server names with `_`); each table registers
  the row's target separately and `enter` looks it up.
- Commit messages are **English** and follow Conventional Commits (`feat:`, `docs:`,
  `chore:`, `fix:`), code before docs.
- The test suite must be green before pushing. Tests locate files via
  `Path(__file__).resolve()`, so they pass both from the repo and through the symlinked
  install locations — do not replace those with hardcoded paths.

## Recording conventions

- **`feat` commits are out of scope, by class.** This is an original project with no
  upstream, so a feature is the product, not a droppable deviation. The exclusion lives in
  `scripts/tests/test_record_coverage.py` as one regex over the commit subject.
- `kind` ∈ `fix` | `perf` | `taste` | `guard` | `revert` | `chore`, cut by **who may demand a
  revert**: bug → `fix`; measurable degradation only → `perf`; only my taste → `taste`; no
  behaviour change, detects drift → `guard`; withdraws earlier work → `revert`; cleanup owed
  nothing either way → `chore`.
- **Verification tiers**: **L0** = `python3 -m pytest scripts/tests` (no host, no live
  store); **L1** = a controlled fixture; **L2** = a real OpenCode session with the hook
  installed.
- `Symptom` names the mechanism, never the session: no prompt text, no command lines, no
  paths from a real config. This repo's whole privacy stance is "counts, never content".

## Listed on the GitHub profile

This repo is listed on the owner's profile front page (`SHADE-glitch/SHADE-glitch`). That page
lives in a **separate repository and never updates itself** — when a fact it states about this
repo changes, update the profile README in the same round.

- **Rename / transfer / delete** — the profile link dies; fix the row.
- **Purpose drift** — the profile's one-line description must still be true. The claims it
  copies — *"no network, no message bodies"*, the local-SQLite scope — are **promises**.
- **LICENSE change** — the profile's license badge must equal this repo's `LICENSE`.
- **Version / runtime declaration change** — the matching badge must equal this repo's
  declaration.
- **Public ↔ private** — a repo that goes private must be **delisted**.
