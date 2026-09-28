# AGENTS.md

Guidance for AI coding agents working in this repository.

## What this is

An OpenCode plugin plus a CLI/TUI that records **skill and MCP tool** usage into
a local SQLite database. Three layers, in dependency order:

1. `plugin/skill-tracker.js` — the **only writer**. Runs inside OpenCode's Bun
   runtime. Listens on `tool.execute.before` / `tool.execute.after` /
   `permission.ask` / `event` / `chat.message`. Every registered tool goes
   through the same wrapper, so MCP calls are captured alongside skills; an MCP
   tool id is `{server}_{tool}` and is resolved against the configured server
   list by longest-prefix match.
2. `scripts/skill_db.py` — the shared data layer (schema, migrations, queries,
   export, backup). Imported by both the TUI and the tests.
3. `scripts/skill-tui.py` (Textual TUI + `--cli` headless subcommands) and
   `scripts/skill-stats.py` (legacy CLI). Read-only apart from explicit
   `delete` / `clear` / `backup` / `vacuum`.

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
- **Never commit runtime state**: `*.db`, `*.db-wal`, `*.db-shm`, `backups/`,
  `__pycache__/`, `.pytest_cache/`, `.venv/`. See `.gitignore`.
- **Never commit secrets.** The plugin sanitizes secrets before storing them;
  its self-test fixtures use synthetic values only. Keep it that way.
- **MCP capture records argument *names* only, never values.** `mcpArgNames()`
  calls `Object.keys()` and must never read a property. The MCP server list is
  resolved once at init and detection fails closed (no servers → nothing
  written). `OPENCODE_SKILL_TRACKER_MCP_DISABLE=1` is the kill switch.
- **The schema lives in three places and must not drift**: `TABLES_SQL` /
  `VIEWS_SQL` in the plugin, `SCHEMA_SQL` in `skill_db.py`, and the fixtures in
  `scripts/tests/conftest.py`. `test_plugin_and_python_schema_do_not_drift`
  enforces plugin ↔ Python; update all copies together.
- `__selftest()` requires `OPENCODE_SKILL_TRACKER_DB` to point somewhere other
  than the production database; it refuses to run otherwise.

## Conventions

- **Code and user-facing output are English** (identifiers, comments,
  docstrings, CLI/TUI text, `skill_db` health suggestions).
- **Docs are bilingual**: `README.md` (English, the landing page) and
  `README.zh-CN.md` (Chinese, the exhaustive reference). `scripts/tests/test_readme.py`
  asserts that `README.zh-CN.md` documents every section and limitation
  M1–M11 — update the Chinese README when behaviour changes, or the suite fails.
- Commit messages follow Conventional Commits (`feat:`, `docs:`, `chore:`,
  `fix:`), code before docs.
- The test suite must be green before pushing. Tests locate files via
  `Path(__file__).resolve()`, so they pass both from the repo and through the
  symlinked install locations — do not replace those with hardcoded paths.
