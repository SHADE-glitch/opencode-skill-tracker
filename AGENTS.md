# AGENTS.md

Guidance for AI coding agents working in this repository.

## What this is

An OpenCode plugin plus a CLI/TUI that records **skill, MCP tool and plugin
usage** into a local SQLite database. Three layers, in dependency order:

1. `plugin/skill-tracker.js` — the **only writer**. Runs inside OpenCode's Bun
   runtime. Listens on `tool.execute.before` / `tool.execute.after` /
   `permission.ask` / `command.execute.before` / `event` / `chat.message` /
   `dispose`.
   Every registered tool goes through the same wrapper, so MCP and plugin tools
   are captured alongside skills; an MCP tool id is `{server}_{tool}` and is
   resolved against the configured server list by longest-prefix match. Plugin
   surfaces are best-effort statically scanned once at init; unresolved tools
   become `(unknown)`, while unresolved commands are dropped fail-closed.
2. `scripts/skill_db.py` — the shared data layer (schema, migrations, queries,
   export, backup). Imported by both the TUI and the tests.
3. `scripts/skill-tui.py` (Textual TUI + `--cli` headless subcommands) and
   `scripts/skill-stats.py` (legacy CLI). Read-only apart from explicit
   `delete` / `clear` / `backup` / `vacuum`.
4. `scripts/skill_db.py::agentos_*` — a **read-only neighbour**, not a fourth
   layer: the AgentOS advisor registers no tool and no command, so it can never
   appear in the usage tables, and `skillt agentos` / the Advisor tab read its
   own store instead (`store/aos.db` + `store/loops/*.json`). Resolution is
   explicit path → `OPENCODE_SKILL_TRACKER_AGENTOS_DB` → `AOS_DB` →
   `AGENT_OS_ROOT` → the checkout the installed `plugin/agent-os.js` symlink
   resolves into. The last one exists because the host sets `AGENT_OS_ROOT`
   inline in its `opencode` alias, so no ordinary shell has it; discovery walks
   **outward until it finds a real `store/aos.db`** rather than trimming path
   components, so a copied plugin file yields nothing instead of a plausible
   wrong path — and an explicit-but-broken `AGENT_OS_ROOT` is still an error,
   never replaced by a guess.

`bin/skillt` is a bash dispatcher that resolves its own location through
symlinks and execs one of the above.

## Commands

```bash
python3 -m pytest scripts/tests -q             # no venv needed; skips TUI tests
.venv/bin/python -m pytest scripts/tests -q    # full suite, includes TUI tests
bash -n bin/skillt                             # syntax-check the dispatcher
./install.sh                                   # create .venv + link install locations
```

Single file: `python3 -m pytest scripts/tests/test_sort.py -q`.

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
- **The advisor store is read-only and field-whitelisted.** Never open it with
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
- **Never commit runtime state**: `*.db`, `*.db-wal`, `*.db-shm`, `backups/`,
  `__pycache__/`, `.pytest_cache/`, `.venv/`. See `.gitignore`.
- **Never commit secrets.** The plugin sanitizes secrets before storing them;
  its self-test fixtures use synthetic values only. Keep it that way.
- **MCP capture records argument *names* only, never values.** `mcpArgNames()`
  calls `Object.keys()` and must never read a property. The MCP server list is
  resolved once at init and detection fails closed (no servers → nothing
  written). `OPENCODE_SKILL_TRACKER_MCP_DISABLE=1` is the kill switch.
- **Plugin attribution is deliberately asymmetric.** Non-builtin tools fail
  open to `(unknown)` when the static scan cannot resolve an owner; commands
  fail closed because there is no builtin command allowlist. The default plugin
  exclusions are the tracker itself (realpath-aware) and `opencode-notifier`.
  The builtin tool allowlist is pinned to OpenCode 1.18.33; set
  `OPENCODE_SKILL_TRACKER_BUILTIN_TOOLS` after an OpenCode upgrade if needed.
- **Permission payload shapes are measured, not assumed.** OpenCode 1.18.33
  emits `permission.asked` (id at `id`, refused call at `tool.callID`) followed
  by `permission.replied` (`requestID` + `reply`); the SDK types declare
  `permission.updated` / `permissionID` / `response`, and coding to those names
  made every real rejection write nothing. Both spellings are handled. Before
  changing a permission handler, re-run the `/tmp` probe recipe in
  `MAINTENANCE.md` §5 rather than trusting the SDK.
- **Plugin inventory is init-time state.** A new/renamed plugin or surface needs
  an OpenCode restart to be discovered; `plugin_inventory` is retained when
  usage is cleared.
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
  than the production database; it refuses to run otherwise.

## Conventions

- **Code and user-facing output are English** (identifiers, comments,
  docstrings, CLI/TUI text, `skill_db` health suggestions).
- **Docs are bilingual**: `README.md` (English, the landing page) and
  `README.zh-CN.md` (Chinese, the exhaustive reference), plus
  `MAINTENANCE.md` / `MAINTENANCE.zh-CN.md` (the operating checklist).
  `scripts/tests/test_readme.py` asserts that **both** READMEs document every
  limitation M1–M19, that `README.zh-CN.md` documents every section, and that
  both maintenance checklists name the same checks, commands and invariants —
  update all four when behaviour changes, or the suite fails.
- **No `set_interval`/`set_timer` in the TUI.** On textual 8.2.8, any app timer
  created after the screens mount makes `run_test`'s teardown raise
  `LookupError: <ContextVar name='active_app'>` (empty callback, App or Screen,
  even after `timer.stop()`), which fails all 47 TUI tests at once. Refresh is
  event-driven: keystroke (throttled by `REFRESH_STALE_AFTER_S`), tab
  activation, and `r`; the dashboard prints `data as of HH:MM:SS`.
  `test_tui_creates_no_app_timers` pins it.
- **Never parse a row's identity out of its `key`.** Names contain the
  separators: `@scope/pkg@1.0/tool`, `conductor:newTrack`, server names with
  `_`. Each table registers `(kind, …parts)` in `app.row_targets` at render
  time and Enter looks that up.
- **Export allowlists metadata** (`EXPORT_METADATA_KEYS` in `skill_db.py`).
  Older builds stored the user's prompt text as `metadata.summary`; the writer
  is gone but rows in real databases are not (`skillt scrub-metadata`). The
  export document is `schema_version = 4` — bump it with any shape change.
- Commit messages follow Conventional Commits (`feat:`, `docs:`, `chore:`,
  `fix:`), code before docs.
- The test suite must be green before pushing. Tests locate files via
  `Path(__file__).resolve()`, so they pass both from the repo and through the
  symlinked install locations — do not replace those with hardcoded paths.
