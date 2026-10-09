# CHANGELOG — opencode-skill-tracker

Original project, **no upstream**: nothing here is a deviation from somebody else's code, so this
record is not a divergence list. It records the repairs, the performance work, the drift guards and
the withdrawals — the things an upgrade of OpenCode, Textual or a neighbouring store can invalidate.

Coverage: 86a8c20..HEAD
Check with `python3 -m pytest scripts/tests/test_record_coverage.py`. Entries are `D-###`, monotonic,
never reused. An entry states what was true **as of its commit**, not current state, and aggregate
counts are printed by the check, never copied into this file.

> **Scope.** Commits whose subject starts with `feat` are deliberately **not** covered: a feature is
> the product, not a droppable deviation, and the READMEs already document them. The exclusion is by
> commit *class*, declared in the check's source — not a per-commit skip flag.

> **How these were written.** `Symptom` / `Change` are compressed from commit subjects plus the state
> of the touched file at HEAD. `Evidence` names a test only where that suite was re-run while writing
> this (`scripts/tests`, 531 passed at adoption); the rest are `L?`. Subjects carrying `M<n>` are the
> limitation ids in the READMEs and are kept verbatim so the two can be cross-read.

---

### D-001 · 2026-09-27 · fix
Symptom  Daily usage bucketed by UTC, so a local evening's calls land on the previous day
Change   Bucket by local calendar day
Evidence L0 2026-10-08 re-run: `scripts/tests` 531 passed
Cost     Any date comparison in this repo inherits the same local/UTC trap
Commit   97cf941

### D-002 · 2026-09-27 · fix
Symptom  "Never used" and "unused for > N days" were reported as one category, so fresh installs
         looked like abandoned skills
Change   Separate the two buckets
Evidence L?
Cost     The distinction is what the pruning advice means; collapsing it re-misleads the reader
Commit   27e3fd9

### D-003 · 2026-09-27 · fix · M9
Symptom  A failing plugin `init()` left no retry path, so one transient error stopped all recording
         for the session
Change   Make `init()` failure retryable
Evidence L?
Cost     Recording must degrade loudly and retry, never degrade silently
Commit   02e90b7

### D-004 · 2026-09-27 · fix · M8
Symptom  `branchByDir` and `pendingSkillPerms` grew without bound
Change   Bound both
Evidence L?
Cost     Unbounded dictionaries in a long-lived Bun process are a leak, not a style issue
Commit   0355029

### D-005 · 2026-09-27 · fix · M7
Symptom  A git branch lookup on the capture path could hang recording until the host timed out
Change   Kill the branch lookup on timeout
Evidence L?
Cost     Any subprocess on the capture path needs the same deadline
Commit   6b74561

### D-006 · 2026-09-29 · fix
Symptom  History views projected `metadata.summary`, which older builds had filled with the user's
         prompt text
Change   Stop projecting `metadata.summary`
Evidence L0 2026-10-08 re-run: `scripts/tests` 531 passed
Cost     **Privacy surface.** The rows in real databases still exist — see D-019 and `skillt scrub-metadata`
Commit   f4b5af7

### D-007 · 2026-09-29 · fix
Symptom  Message text could be read into the store; redaction was narrower than the risk; writes
         were not hardened against a partial failure
Change   Never read message text, broaden redaction, harden writes
Evidence L0 2026-10-08 re-run: `scripts/tests` 531 passed
Cost     This is the repo's privacy floor. Every later reader inherits "count shapes, never return lines"
Commit   f49e9ef

### D-008 · 2026-09-29 · fix
Symptom  `fastest growing` was computed wrong, day buckets were UTC, and the timeline was unbounded
Change   Correct the ranking, use local day buckets, bound the timeline
Evidence L?
Cost     Same local/UTC class as D-001
Commit   49c2f04

### D-009 · 2026-09-29 · fix
Symptom  The legacy CLI carried its own copy of the data layer, so the two could disagree
Change   Make the legacy CLI delegate to the shared data layer
Evidence L?
Cost     One copy of the schema rules; see AGENTS "the schema lives in two places and must not drift"
Commit   ba9218d

### D-010 · 2026-09-29 · fix
Symptom  The dashboard was unreachable and the MCP stats panel broken
Change   Repair both
Evidence L?
Cost     —
Commit   1c27983

### D-011 · 2026-10-01 · fix
Symptom  Export emitted arbitrary metadata keys, including historical rows carrying prompt text
Change   Emit only allowlisted metadata keys (`EXPORT_METADATA_KEYS`)
Evidence L0 2026-10-08 re-run: `scripts/tests` 531 passed
Cost     An allowlist fails closed — a new key is silently dropped, which is the intended direction
Commit   1453d45

### D-012 · 2026-10-01 · fix
Symptom  Table rows resolved their target by parsing the row key, but names contain the separators
         (`@scope/pkg@1.0/tool`, `conductor:newTrack`, `_` in server names)
Change   Register `(kind, …parts)` in `app.row_targets` at render time and look that up
Evidence L?
Cost     Any new table must go through the target map; parsing a key re-creates the bug
Commit   417d059

### D-013 · 2026-10-01 · fix
Symptom  The Recent timeline parsed its row key for a target, same class as D-012
Change   Stop parsing the key
Evidence L?
Cost     See D-012
Commit   22a31bb

### D-014 · 2026-10-01 · guard
Symptom  The suite read the live skills directory, so tests passed or failed depending on the
         machine rather than on the code
Change   Make the suite independent of the live skills directory
Evidence L0 2026-10-08 re-run: `scripts/tests` 531 passed
Cost     A test that reads real user state is an instrument error waiting to be misread as a finding
Commit   aba2809

### D-015 · 2026-10-01 · fix
Symptom  Stripping metadata left the WAL behind, so the removed bytes were still on disk
Change   Checkpoint the WAL after stripping
Evidence L0 2026-10-08 re-run: `scripts/tests` 531 passed
Cost     "Deleted" means the bytes are gone; a delete that leaves a WAL is a false assurance
Commit   0b16366

### D-016 · 2026-10-01 · fix
Symptom  Backups landed outside the directory that retention sweeps, so they accumulated forever
Change   Put every backup inside the retention-swept directory
Evidence L0 2026-10-08 re-run: `scripts/tests` 531 passed
Cost     Retention correctness is about location, not about intent
Commit   e764776

### D-017 · 2026-10-01 · fix
Symptom  Permission handlers were coded to the SDK's declared event names; OpenCode 1.18.33 actually
         emits `permission.asked` / `permission.replied` with different fields, so every real
         rejection wrote nothing
Change   Read the events the host actually sends; handle both spellings
Evidence L1（真机事件形状是测出来的，见 MAINTENANCE §5 的 probe recipe）
Cost     **Measured, not assumed.** Before changing any permission handler, re-run the probe recipe
Commit   01350c5

### D-018 · 2026-10-01 · fix
Symptom  The advisor store was located through an environment variable that the installed plugin
         does not set
Change   Find it from the installed plugin instead
Evidence L?
Cost     **Withdrawn by D-020.** Kept here because the withdrawal is the current state; reading only
         this entry would send you back to a design that was rejected
Commit   64c7387

### D-019 · 2026-10-01 · revert
Symptom  D-018's approach was wrong for this repo's contract (locating a foreign store through an
         installed plugin's env)
Change   Revert "fix(agentos): find the advisor store from the installed plugin, not an env var"
Evidence L?
Cost     Pairs with D-018. The door is `skill_db.agentos_*` only, CLI-side, never from the TUI
Commit   421140b

### D-020 · 2026-10-01 · fix
Symptom  The three trend charts broke onto separate lines
Change   Keep them on one horizontal line
Evidence L?
Cost     Layout-only; Textual re-measures, see D-022
Commit   c1097c1

### D-021 · 2026-10-02 · revert
Symptom  The dashboard's call-count bar column multiplied against a peak that did not include the
         reference value it printed
Change   Revert "feat(tui): rank the dashboard tables with a call-count bar column"
Evidence L?
Cost     The rule that replaced it lives in AGENTS ("a hand-drawn bar is scaled against a peak that
         includes every value it prints"). Withdrawal plus rule, not withdrawal alone
Commit   5495bb6

### D-022 · 2026-10-02 · perf
Symptom  Every refresh re-read and re-rendered all pages
Change   Refresh only the page on screen
Evidence L?
Cost     Event-driven refresh is what lets this repo forbid app timers (see D-025)
Commit   be3bdd3

### D-023 · 2026-10-02 · chore
Symptom  Four detail pages each carried their own screen scaffolding
Change   One `DetailScreen` shell for all four (no user-visible behaviour change)
Evidence L?
Cost     Refactor only; no test will notice if it is undone
Commit   80970b7

### D-024 · 2026-10-02 · fix
Symptom  `DataTable` re-measures auto-width columns in `_on_idle`, one message-pump cycle after rows
         arrive — so the first frame cut every name to its header width
Change   Size table columns at render time via `fit_columns()`
Evidence L?
Cost     Every new table must go through `fit_columns()` or it will cut names again
Commit   dd73183

### D-025 · 2026-10-02 · fix
Symptom  Capture freshness was judged by the newest row overall, so a busy stream hid a dead one
Change   Judge freshness per stream
Evidence L?
Cost     Aggregate-vs-per-source is the same error class as D-014 in doctor output
Commit   d85bcd1

### D-026 · 2026-10-03 · fix
Symptom  A plugin whose tools were declared behind one relative import resolved to `(unknown)`
Change   Scan one hop past the entry shim
Evidence L?
Cost     Attribution fails open to `(unknown)` by design; the scan depth is what limits it
Commit   829a2fc

### D-027 · 2026-10-03 · fix
Symptom  `./` plugin specs were resolved against the current working directory, not the config dir
Change   Resolve against the config dir
Evidence L?
Cost     Any path resolution here must be relative to config, never to CWD
Commit   03fae72

### D-028 · 2026-10-03 · fix
Symptom  The plugins page and the command list advertised history as if it were loaded
Change   Stop claiming loaded state that was never read
Evidence L?
Cost     See "instrument vs phenomenon": an absent signal must first be checked against the instrument
Commit   14aaf0b

### D-029 · 2026-10-03 · fix
Symptom  Tool ids were extracted by regex over the plugin's text, so any `key: value` in a comment
         became a tool
Change   Read ids at the shallowest level of the `tool: { … }` object
Evidence L?
Cost     Static scanning is approximate by nature; what must stay exact is not reading prose
Commit   7f36279

### D-030 · 2026-10-03 · guard
Symptom  The builtin tool allowlist was written from memory and drifted from what the host reports
Change   Pin the allowlist to OpenCode 1.18.34, measured off `opencode serve --pure`
Evidence L?
Cost     Read `/experimental/tool/ids`, never the binary's TUI view registry (it lists four names the
         host does not report). Re-check after any OpenCode upgrade
Commit   bd9a951

### D-031 · 2026-10-04 · fix
Symptom  `__selftest` isolated its database but not its log, so a failed selftest left `[err]` lines
         in the log that `skillt doctor` counts as real errors
Change   Redirect the selftest log at both exits (normal and aborted)
Evidence L0 2026-10-08 re-run: `scripts/tests` 531 passed
Cost     A probe that pollutes the signal it measures is worse than no probe; both exits must undo it
Commit   d1884cd

### D-032 · 2026-10-04 · chore
Symptom  Documentation did not state what the Agents page's two columns and the injection grouping
         mean, nor why nothing is backfilled
Change   Document them (M22 amended, M23 added)
Evidence 不适用（无行为变化）
Cost     The "nothing backfills" statement is the load-bearing half
Commit   e22994a

### D-033 · 2026-10-04 · perf
Symptom  Textual slides the tab-bar underline for 0.3 s on every switch, roughly 305 ms of a
         ~500 ms Dashboard→Skills transition
Change   `SkillTUI.animation_level = "none"` (about 190 ms with it off)
Evidence L0 2026-10-08 re-run: `scripts/tests` 531 passed（含 `test_tui_disables_textual_animations`）
Cost     `"basic"` does **not** remove the underline slide; only `"none"` does. Measured numbers are
         that day's and need re-measuring on another machine
Commit   1e6342c

### D-034 · 2026-10-09 · fix
Symptom  `buildMetadata` admitted `metadata.title` from the `permission.ask` payload while
         `METADATA_SCRUB_KEYS` declared that key must not remain in the database. The promise was held
         by an operator remembering to run `skillt scrub-metadata`, and README already asserted the
         title "is never read" — the documentation was right and the code was not
Change   All three `permission.ask` branches stopped naming `title`, `buildMetadata` stopped admitting
         the key, `error` is untouched. `title` stays in the scrub list, for rows older builds wrote
Evidence L0 2026-10-09: `scripts/tests` 539 passed on all three documented commands; both new guards
         were red first (the sentinel really did land in `mcp_usage.metadata`); `__selftest` 63/63; the
         live database held 0 `title` keys before the change, so this closed a latent path, not a leak
Cost     A denial's title text can no longer be read back. Nothing ever read it, and `source` still
         names the path that wrote the row
Commit   ce3c525

### D-035 · 2026-10-09 · perf
Symptom  `doctor log.errors` read the whole plugin log with a plain `open()` and a per-line loop. That
         log has no rotation (M17) and grows one line per recorded error forever, so the cheapest
         diagnostic in the tool slowed down with the file's age — while the claude-mem reader beside it
         already stopped at `CLAUDE_MEM_LOG_BYTES_CAP`
Change   Read it through `_read_bounded_text` under a named `TRACKER_LOG_BYTES_CAP`, from the start;
         when the cap bites, the printed line says how many bytes it covered and calls its own counts
         a floor. An unreported bound would read as a clean bill of health
Evidence L0 2026-10-09: `scripts/tests` 541 passed; both new `test_doctor.py` checks were red first
         (the second failed because no cap existed to aim it at); the live 117,698 B log still reports
         the same 4 error lines and the same last line
Cost     Errors past 4 MiB of an unrotated log are no longer counted — bounded, stated, and still
         reachable by reading the file directly. The growth side stays open until rotation is decided
Commit   e9086f3

### D-036 · 2026-10-09 · fix
Symptom  `session.created` passed `info.title` into the in-memory session context and nothing read it
         back. A session title is text the host derives from the user's first message, so the writer
         held prose for the lifetime of the process — one `...ctx` splat away from the database
Change   The patch keeps `directory` only (it resolves the git branch). The guard compares the keys of
         the patch literal, not the text around it, because the explanation of *why* the field is
         dropped necessarily names it
Evidence L0 2026-10-09: `scripts/tests` 542 passed; the guard was red first with `{directory, title}`;
         `scripts/tests/test_plugin.py` 57 passed, which includes running `__selftest` under Bun
Cost     Nothing was lost: no view, query or export ever named a session title. It is a second
         instance of the D-034 shape — a read that no consumer wanted
Commit   550635f

### D-037 · 2026-10-09 · fix
Symptom  OpenCode's own names lived in about forty places across three languages, and
         `skill-tui.py` re-spelled the plugin's hook names as strings so `doctor` could grep the
         installed file: one upstream rename needed edits in at least three files. The two
         hand-mirrored halves of the derived `source` rule also disagreed on one input — the SQL
         CASE let an empty category through as `''` while `source_of()` answered `unknown`
Change   `scripts/opencode_compat.py` now holds the hook ids, event types (both permission
         spellings), payload field paths, the host's directory layout, the version pins and the
         `source`/`status` vocabularies. `skill_db` and `skill-tui` refer to it; the CASE renders
         from the same map as `source_of()`, with `NULLIF` closing the disagreement
Evidence L0 2026-10-09: `scripts/tests` 550 passed. `test_compat.py` evaluates the SQL CASE and
         `source_of()` over the same categories inside one SQLite instance, and greps the writer for
         every declared name. The **installed** `skillt` (symlinked `scripts/` dir, so the sibling
         import is exercised) ran `doctor` and `stats` from outside the repo: 16 PASS / 3 WARN, same
         numbers as before the change
Cost     One semantic delta, on a value the writer cannot produce: a `''` category now reads
         `unknown`. Payload paths are checked by leaf accessor (`.parentSessionId`) because the
         writer reaches host fields through short-lived locals (`st`, `md`, `perm`) — and no form of
         this check can see a read that was never declared, which stays a review rule
Commit   5d20b9a

### D-038 · 2026-10-09 · guard
Symptom  The writer spelled each hook name at least twice — once as the registration key, once as
         the `safe()` label — and its selftest drove the hooks through a third spelling
         (`plugin["tool.execute.before"]`). A rename had three places to forget, and a half-rename
         registers nothing while the TUI still prints "0 calls" as if the week were quiet
Change   One `CONTRACT` block holds the host's hook ids and event types; registration keys, `safe()`
         labels, hook accesses and the seven `case` labels all reference it, so each string exists
         once, as a value. `test_compat.py` compares the block's `HOOK_*`/`EVENT_*` values against
         `opencode_compat` by set and proves the writer registers by reference
Evidence L0 2026-10-09: `scripts/tests` 552 passed on all three documented commands, and the plugin
         loaded under Bun reports the eight keys it registers: chat.message, chat.params,
         command.execute.before, dispose, event, permission.ask, tool.execute.after,
         tool.execute.before — with `EXPORT_KEYS=["id","server"]` intact
Cost     Comparing two languages means parsing one, so `_js_contract` is a regex over a deliberately
         flat object literal — nesting that block would make the helper read a partial list, and the
         test would go red rather than pass quietly. Provenance labels (`meta.source`) stayed
         literal: they are data the database holds and the TUI prints. The comparison caught that
         `chat.params` had never been declared in Python — a module that is a second copy is worth
         what it costs
Commit   67e3563

### D-039 · 2026-10-09 · guard
Symptom  One file held six layers: 915 of `skill_db.py`'s 3116 lines described **other projects'**
         schemas (the advisor store, claude-mem's ledger) while every consumer imported them, and
         the privacy scan had to slice a section out of the file to know what it was covering
Change   `skill_db_agentos.py` and `skill_db_claude_mem.py`, importing the core and never the
         reverse; core state is reached as `db.HOME` / `db.fmt_time` instead of by-value imports;
         `_read_bounded_text` moved the other way into the core, because doctor reads the tracker's
         own log with it now. `test_module_boundaries.py` pins the absence of the names from the
         core, the one-way import, and the attribute-access rule
Evidence L0 2026-10-09: `scripts/tests` 555 passed; live `skillt claude-mem` printed the same
         ledger/log/worker lines as before (684 injected · ERROR 4 · worker up), `skillt agentos`
         and `skillt doctor` unchanged at 16 PASS / 3 WARN
Cost     Three bug classes surfaced only because the tests moved with the code: a TUI local named
         `cm` shadowed the new module alias (a silent `UnboundLocalError` swallowed by doctor's own
         try/except); two spies patched `st.db.<name>` for functions the TUI no longer calls there,
         which would have left the "no HTTP on the repaint path" guard spying on nothing; and the
         prose tripwire fired on `import urllib.request as urlreq` — the line that exists *because*
         of it, exempted by line rather than by word. When a section moves, any scan of it must move
         too, or it passes vacuously
Commit   6b465bd

### D-040 · 2026-10-09 · fix
Symptom  `OPENCODE_SKILL_TRACKER_CONFIG_DIR` was read by the writer and ignored by the readers,
         which built the config tree out of `HOME` alone. With the override in force the plugin
         recorded the skills it could see and every screen scanned the default tree for them: an
         empty Skills page standing over a database that says otherwise, and `doctor` pointing its
         `plugin.exists` check at a directory nobody chose
Change   `skill_db.py` reads the same variable, so `SKILLS_DIR_DEFAULT`, `PLUGIN_PATH` and
         `TRACKER_LOG_PATH` follow one config tree instead of two
Evidence L0 2026-10-09: `scripts/tests` 596 passed on all three documented commands; the new test
         reloads the module under the override instead of patching the value, because these paths
         are bound at import — a by-value patch would prove nothing about whether the environment
         is read at all
Cost     One machine's default behaviour is unchanged (the variable is unset, so both spellings
         agree); what changes is that a second machine may now move the tree and stay coherent
Commit   ee9f537

### D-041 · 2026-10-09 · fix
Symptom  There was no settings surface: no config file, four flags the interactive screens never
         read (`--days`, `--limit`, `--min-uses`, `--freshness-days` — they were parsed into
         `Args` and dropped, since `SkillTUI.__init__` took only the database path), each default
         written as a fresh literal on its own parser line, and `parse_args` throwing away every
         token after the command name. Meanwhile `pyproject.toml` stated no Python floor at all
         while the READMEs badged 3.11+ and `doctor`'s `env.python` warned below 3.10 — three
         prints, two numbers
Change   `scripts/settings.py`: one registry (key, default, minimum, flag, explanation in both
         documentation languages) resolved by **flag > config file > environment > default**, and
         `skillt config list|get|set|unset|path|explain`, which dispatches before the
         database-exists check because on a fresh machine it is the one command that must work
         before the first skill has ever run. The file is
         `~/.local/share/opencode/skillt-config.json` — the tracker's own data directory, never
         `~/.config/opencode/`. Refused values keep a visible origin
         (`config file (rejected)`) instead of becoming a default nobody chose, and a file that
         reports problems is never rewritten. `view.recent_rows` is the knob the Recent timeline
         actually reads (it replaces the literal 100); the three headless-only keys now say so in
         both languages, as does the reason the trend charts cannot follow `view.days` (their
         height is pinned to it). `CAPTURE_FRESHNESS_DAYS` is the registry's default, and
         claude-mem's staleness line is judged by the resolved `--freshness-days` rather than a
         second copy of 7. `requires-python = ">=3.11"` plus `MIN_PYTHON`, which one test compares
         against the badge and the package. `a.positional` carries `config`'s words and is refused
         anywhere else
Evidence L0 2026-10-09: `scripts/tests` 596 passed on `python3`, on `.venv/bin/python` and with
         `OPENCODE_SKILL_TRACKER_SKILLS_DIR` pointing at a nonexistent directory; `__selftest`
         63/63 under Bun against the repo file; `bash -n bin/skillt`; live `skillt doctor` 17 PASS
         / 3 WARN / 0 FAIL with the new `config.file` line and `env.python … (want >= 3.11)`; live
         `skillt config set/get/list --json/unset` round-tripped against a sandbox path through the
         environment override, refusing to rewrite a file holding a non-numeric value and an
         unknown key (exit 2), and deleting the file once `unset` emptied it; `scrub-metadata`
         still reports no rows carrying `summary`/`title`; no settings file exists on this machine,
         which is the zero-config path still working
Cost     The layer is only trustworthy if two properties hold, and both are now tested: a refused
         value must be *named* rather than absorbed, and a broken file must never be repaired by
         overwriting it. `parse_args` resolves the file on every call, so the suite isolates it
         autouse — an unisolated run would pass or fail depending on what this machine last typed
Commit   4e0c29d

### D-042 · 2026-10-09 · guard
Symptom  Six environment variables were read by the code and named in neither README; the
         dispatcher carried a hand-synced list of value-taking flags whose own comment said "must
         stay in sync" and nothing checked it; and the two READMEs each documented a different
         subset of the same surface
Change   `scripts/tests/test_settings_documented.py` enumerates the reads *out of the source* and
         demands each one in both languages, plus every key's flag and default. Enumeration, not a
         maintained list: a test that reads a hand-kept inventory proves nothing about the
         inventory. The shapes are per-language because `${NAME}` is a string template in
         JavaScript (this project interpolates its own constants that way, and demanding docs for
         them would be demanding docs for nothing) and an environment read in bash — where a bare
         `$NAME` is not evidence of one, since bash cannot tell an inherited variable from the
         script's own assignment
Evidence L0 2026-10-09: the gate was written red first and named 18 names across the two files;
         after the tables it passes, and `test_the_documented_flag_list_matches_the_parsers`
         caught `--recent-rows` missing from `bin/skillt` the moment it entered the registry
Cost     A new environment read must be documented in the same change or the suite is red. That is
         the point; the cost is that the gate can only ever see a *read it can pattern-match*, so a
         new spelling of reading the environment has to be added to the patterns, so
         the gate's own list is a third thing to keep in step
Commit   4e0c29d
