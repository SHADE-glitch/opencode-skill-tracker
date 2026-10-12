# Compatibility matrix

What this project has actually been run against, which release each claim belongs to,
and what to re-verify when one of them moves. Numbers here are **pins** (values the
code asserts), not measurements — measurements live in
[`measurements.md`](measurements.md), because a number copied out of a run goes stale
in silence while a pin fails loudly.

There is a shorter path for most of this: `scripts/opencode_compat.py` is where the
host's names live, and `plugin/skill-tracker.js`'s `CONTRACT` block is where the writer
spells them. This file is about **versions**, which those two do not record.

---

## 1. The pins, and who reads them

| Pin | Value | Where it lives | What it claims | Who checks it |
|---|---|---|---|---|
| `PIN_LOADER` | OpenCode 1.18.32 | `opencode_compat.py` | the release whose plugin loader calls **every** exported function as a factory, which is why the writer may only `export default { id, server }` | `test_loader_does_not_enumerate_exports`, `test_default_export_is_server_module` (both need `bun`) |
| `PIN_PERMISSION_SHAPE` | OpenCode 1.18.33 | `opencode_compat.py` | the release the permission payloads were **measured** on: `permission.asked` (`id`) then `permission.replied` (`requestID` + `reply`), not the SDK's `permission.updated` / `permissionID` | `test_the_writer_declares_the_same_names_the_module_declares`, and the handler accepts both spellings by pair (`PERMISSION_*_KEYS`) |
| `PIN_TOOL_IDS` | OpenCode 1.18.34 | `opencode_compat.py`, mirrored in the writer's comment | the release `DEFAULT_BUILTIN_TOOLS` was refreshed from via `GET /experimental/tool/ids` | `test_the_tool_id_pin_is_a_claim_the_plugin_actually_makes` (the module's pin must equal the string inside the plugin comment), and `skillt doctor`'s `env.opencode_version` parses that comment back out (`VERSION_PIN_RE`) and compares it to `opencode --version` |
| `PIN_SDK_TYPES` | OpenCode 1.18.4 | `opencode_compat.py` | the version of `@opencode-ai/sdk` types that were read — recorded **because it disagreed with reality** (M20), not because anything depends on it | nothing; it is a citation, and that is fine as long as nobody treats it as a source of truth |
| textual | `>=8.2,<9` | `pyproject.toml`, `requirements.txt` | the TUI stack. 8.2.8 is what every claim about the interface was measured on | `MIN_PYTHON` and the version-floor guard (`test_the_python_floor_is_the_same_number_everywhere`) pin the interpreter side |
| Python floor | 3.11 | `pyproject.toml` (`requires-python`), `MIN_PYTHON` in `skill-tui.py` | the oldest interpreter the CLI/TUI promises to start on | the README badge, the package metadata and `skillt doctor`'s `env.python` are one number, pinned by the test above |
| CI interpreters | 3.11 / 3.14 | `.github/workflows/ci.yml` | both ends of the matrix — the floor (`requires-python`) and the README's declared upper bound — are the interpreters the suite is guaranteed to run on *without* `bun` | what CI proves is stated as a *set*, not a count: the only skips there are the `requires_bun` tests and the dispatcher test, because neither end of the matrix has bun. A pass **total** is never copied into a document (AGENTS.md), and `measurements.md` §10 is where a dated run goes |

The host this was written on runs OpenCode 1.18.35, bun 1.3.13, `python3` 3.14 and a
3.13 `.venv`. **That is not a compatibility claim** — it is where the measurements were
taken. Re-take them with `measurements.md` before quoting any of it.

---

## 2. OpenCode release → what changed for us

Only the rows we have evidence for. An empty row is not "nothing changed", it is
"nobody looked", so the honest action when a release lands is the recipe in
MAINTENANCE §5, not a guess here.

| OpenCode | What this project observed at or around it | Consequence we carry |
|---|---|---|
| 1.18.4 | SDK types declare `permission.updated` / `permissionID` / `response` | recorded as `PIN_SDK_TYPES`; coding to these names was M20 — every real rejection wrote nothing |
| 1.18.32 | loader enumerates every export as a factory | the writer's export shape is frozen: `export default { id, server }`, `__selftest` reachable only as a property of that object |
| 1.18.33 | measured bus events `permission.asked` → `permission.replied`, with `id` / `requestID` / `reply` | both spellings handled; a handler picks by *pair order*, never by "the docs say so" |
| 1.18.34 | `/experimental/tool/ids` answered with the builtin set baked into the writer | `DEFAULT_BUILTIN_TOOLS`; a rename here silently reclassifies a builtin as an unknown tool, which is M13's shape |
| 1.18.35 | the host this repo runs on today; **not independently verified** | `skillt doctor` reports `env.opencode_version` as WARN whenever the running version has passed `PIN_TOOL_IDS`. That WARN is the designed state, not a fault |

Two more things are true across all of them and are easy to break from our side:

- **A hook name is not a bus event name.** `permission.ask` (hook) and
  `permission.asked` (event) are different mechanisms that happen to look alike;
  the tracker registers the former and switches on the latter.
- **Nothing about the host's storage format is assumed.** The tracker writes its own
  database and reads the host's *config* files only to discover servers, plugins and
  skill directories. `opencode.db` is not read (MAINTENANCE §8 records why).

---

## 3. Textual: two behaviours this project depends on

Both were found by being broken, and both are pinned rather than commented.

| Behaviour | Version | What it costs us | The guard |
|---|---|---|---|
| `Tabs._highlight_active` animates the underline at level `"basic"`, and `run_test`'s teardown raises `LookupError: <ContextVar name='active_app'>` if any app timer was created after the screens mount | 8.2.8 | no timers at all: refresh is event-driven per page, and `animation_level = "none"` is not cosmetic (the slide is ~305 ms of a ~500 ms tab switch) | `test_tui_creates_no_app_timers`, `test_tui_disables_textual_animations` |
| `DataTable` measures auto-width columns in `_on_idle`, one pump cycle after the rows arrive | 8.2.8 | every populated table must go through `fit_columns()` in the same turn it is filled, or the first frame renders each column header-wide and later frames show the *previous* content's widths | `test_plugin.py` / TUI width guards; `fit_columns`' docstring carries the measured before/after |
| An un-laid-out widget reports `region.width == 0` | 8.2.8 | any layout claim must assert `width > 0` before comparing, and width-dependent status text is painted through `call_after_refresh` | `test_the_confirm_modal_stays_inside_a_narrow_screen`, `test_columns_past_the_right_edge_are_announced` |

A Textual upgrade should re-run, in this order: `pytest scripts/tests/test_tui.py`,
then the two latency recipes in `measurements.md` §3, then the geometry claims in
README's "Terminal widths" paragraph (those are measurements, so they get re-measured
rather than trusted).

---

## 4. What is read outside this project's own database

Not OpenCode, but the same decay risk, so the same table. Since 2026-10-12 (D-053) it has
one row that is a withdrawal notice instead of a read.

| Source | What is read | Contract |
|---|---|---|
| The host's skills tree (`~/.config/opencode/skills/**/SKILL.md`) | frontmatter `name`, plus the `source`/`category` derived from the path | read-only, and a value that could carry prose is **counted, never read** — the body is not opened (`test_scan_sync.py`) |
| The host's config (`opencode.json`, `.jsonc`) | MCP servers, plugin specs, skill directories | read-only; a parse failure is **never** read as "empty config" |
| This project's own plugin log | `[err]` line counts | scanned from the **start** under the resolved `log.max_bytes`; when the cap bites the line says `truncated`, so the count reads as a floor |
| A foreign SQLite store | **nothing — withdrawn 2026-10-12 (D-053)** | claude-mem's ledger (with its `inject-trace.log`, worker log and `worker.pid`, and three HTTP endpoints on that worker) and the AgentOS advisor's `store/aos.db` + `store/loops/*.json` left with `skillt claude-mem` and `skillt agentos`. The contract they were held to is the standing rule for the next one: read-only URI (`mode=ro`), never `open_db()` — that helper falls back to read-write and creates a missing file — every emitted field named by name **and** by type, never a dict splat, never reached from a repaint, and its own check named in `AGENTS.md`'s invariant table |

If a foreign store is ever read again, the correct failure mode is the one those readers had:
"reported unreadable", never "reported empty" — a missing store answers `available: False`
with a `reason` and is never created by the reader. A foreign field that is renamed should
make a count go to zero and print that zero with its reason; a foreign field renamed to carry
prose should yield nothing at all.
