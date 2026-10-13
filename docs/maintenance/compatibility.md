# Compatibility matrix

What this project has actually been run against, which release each claim belongs to,
and what to re-verify when one of them moves. Numbers here are **pins** (values the
code asserts or a workflow runs), not measurements — measurements live in
[`measurements.md`](measurements.md), because a number copied out of a run goes stale
in silence while a pin fails loudly.

There is a shorter path for most of this: `scripts/ocut-sync.py`'s **format registry**
is where the host's names live, and `ocut format` prints it. This file is about
**versions**, which the registry does not record.

---

## 1. The pins, and who reads them

| Pin | Value | Where it lives | What it claims | Who checks it |
|---|---|---|---|---|
| Python floor | 3.11 | `pyproject.toml` (`requires-python`) | the oldest interpreter the CLI/TUI promises to start on | the README badge and `test_the_workflow_states_the_versions_it_runs` |
| textual | `>=8.2,<9` | `pyproject.toml`, `requirements.txt` | the TUI stack; the interface claims are measured on the installed 8.x | the requirements file the installer reads |
| CI interpreters | 3.11 / 3.14 | `.github/workflows/ci.yml` | both ends of the declared range — the floor and the highest version the suite was run green on — are the interpreters CI runs the suite on | `test_maintenance_docs.py` (`test_every_ci_claim_in_the_docs_states_the_real_matrix`), which reads the workflow's own `python-version` list |
| OpenCode format | not a single version | `scripts/ocut-sync.py`'s registry | the host tables (`session`, `message`, `part`), part types, tool names and argument names this reader reads | `scripts/tests/test_ocut_sync.py` — no field may be read without being registered, no registered field may go unread; `ocut format` prints the surface |

The host this was written on runs OpenCode 1.18.x, `python3` 3.14 and a 3.13 `.venv`.
**That is not a compatibility claim** — it is where the measurements were taken. Re-take
them with [`measurements.md`](measurements.md) before quoting any of it.

---

## 2. OpenCode release → what changed for us

Only the rows we have evidence for. An empty row is not "nothing changed", it is
"nobody looked", so the honest action when a release lands is the recipe in
[`MAINTENANCE.md`](../MAINTENANCE.md) §5, not a guess here.

| OpenCode | What this project observed | Consequence we carry |
|---|---|---|
| any | `session` / `message` / `part` is the persisted shape; `step-finish` carries tokens, its parent `message` carries the model | the reader selects a whitelist of columns and reads a whitelist of fields; a rename lands in `unparsed`, not in a NULL panel |
| any | a plugin's tool is recorded under the tool's own name, with nothing naming the plugin; a plugin command is not recorded at all | `plugin_usage` is fed by `plugin/ocut-hook.js`'s sidecar, never by the host database (README M3) |
| any | `opencode.db` is a live file the host keeps mutating and pruning | the reader opens it `mode=ro` and treats a deletion as a `sync_state.note`, never as an error |

Two things are true across all of them and are easy to break from our side:

- **A hook name is not a table name.** The hook is the *only* writer of the sidecar; the
  reader is the *only* writer of `usage.db`. They meet at `plugin-events.jsonl`, and the
  two must agree on that path (`OCUT_HOOK_EVENTS`).
- **Nothing about the host's storage format is assumed beyond the registry.** A field the
  reader cannot use is counted in `unparsed`, which is what makes a format move visible.

---

## 3. Textual: behaviours this project depends on

Both were found by being broken, and both are pinned rather than commented.

| Behaviour | What it costs us | The guard |
|---|---|---|
| An app timer created after the screens mount can break `run_test`'s teardown | the TUI creates no app timers: refresh is event-driven and per page | `scripts/tests/test_ocut_tui.py` |
| `DataTable` measures auto-width columns one pump cycle after the rows arrive | every populated table is sized in the same turn it is filled, or the first frame renders each column header-wide | the TUI width guards in `scripts/tests/test_ocut_tui.py` and `scripts/tests/test_ocut_drill.py` |

A Textual upgrade should re-run, in this order: `pytest scripts/tests/test_ocut_tui.py`,
then the two latency recipes in [`measurements.md`](measurements.md) §3, then the geometry
claims in README's "Terminal widths" paragraph (those are measurements, so they get
re-measured rather than trusted).

---

## 4. What is read outside this project's own database

Not OpenCode's database, but the same decay risk, so the same table.

| Source | What is read | Contract |
|---|---|---|
| The host database (`~/.local/share/opencode/opencode.db`) | `session`, `message`, `part` rows | opened `mode=ro` with `PRAGMA query_only = ON`; a missing file raises rather than creating an empty one; a prose field (`output`, `title`, `metadata`, `error`, a `text`/`reasoning` body, a session `title`) is **counted, never read** |
| The host config (`~/.config/opencode`) | MCP server names, plugin specs, and the installed inventory | read-only; a parse failure is **never** read as "empty config" |
| This project's own sidecar (`plugin-events.jsonl`) | plugin `surface` and `call` records | read by byte offset; a file shorter than the cursor was truncated, so it is re-read from the start (idempotent — every record is keyed by its own id) |
| This project's own settings (`~/.config/ocut/config.toml`) | the six keys | read-only; a bad value is a hard error that names the key, never a silent default |

If a foreign store is ever read again, the correct failure mode is "reported unreadable",
never "reported empty": a missing source answers with a reason and is never created by
the reader.
