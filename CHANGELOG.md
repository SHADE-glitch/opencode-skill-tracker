# CHANGELOG — opencode-usage-tracker

Original project, **no upstream**: nothing here is a deviation from somebody else's code, so this
record is not a divergence list. It records the repairs, the performance work, the drift guards and
the withdrawals — the things an upgrade of OpenCode, Textual or a neighbouring store can invalidate.

Coverage: 86a8c20..HEAD
Check with `python3 -m pytest scripts/tests/test_record_coverage.py`. Entries are `D-###`, monotonic,
never reused. An entry states what was true **as of its commit**, not current state, and aggregate
counts are printed by the check, never copied into this file.

> **Scope.** Commits whose subject starts with `feat` are deliberately **not** covered: a feature is
> the product, not a droppable deviation, and the READMEs already document them. The exclusion is by
> commit *class*, declared in the check's source — not a per-commit skip flag. The check also only
> looks at commits that touch the **shipped code paths** (`plugin/`, `scripts/`, `bin/`), so a commit
> changing documentation alone is not demanded here; when such a commit carries a decision worth
> keeping, it is named inside the entry it belongs to. AGENTS.md's invariant row says the same thing
> now — it used to claim "every non-feature commit", which the gate never enforced, and the eight
> `docs(...)` commits pushed between `5c6fa1f` and this file's edit passed it while proving the
> sentence false.

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
Evidence L1 (the real event shape was measured, not assumed — see the probe recipe in MAINTENANCE §5)
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
Evidence n/a (no behaviour change)
Cost     The "nothing backfills" statement is the load-bearing half
Commit   e22994a

### D-033 · 2026-10-04 · perf
Symptom  Textual slides the tab-bar underline for 0.3 s on every switch, roughly 305 ms of a
         ~500 ms Dashboard→Skills transition
Change   `SkillTUI.animation_level = "none"` (about 190 ms with it off)
Evidence L0 2026-10-08 re-run: `scripts/tests` 531 passed (including `test_tui_disables_textual_animations`)
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

### D-043 · 2026-10-09 · guard
Symptom  Nothing bounded the rows: three usage tables grew one row per event forever and
         `skill_versions` one row per content change, while the retention work already done
         covered only backup *files*. `doctor backups.latest` looked healthy in the same
         report that said nothing about the database's own growth, so the gap read as if it
         had been handled
Change   `plan_usage_retention()` (pure SELECT, the shape of `plan_retention()`) and
         `prune_usage()` (the dry-run → `--yes` shape of `scrub_metadata()`, one
         `_write_txn`, WAL checkpointed after). Two settings keys arm it —
         `retention.usage_days` and `retention.max_skill_versions`, both default 0 = off —
         resolved through the same flag > file > env > default chain, and `skillt
         prune-usage` prints the plan before removing anything. The guards are the change:
         dry-run and delete share one query; a timestamp that will not parse is kept and
         counted as `undated` (a bare `timestamp < cutoff` would delete `''` as ancient);
         `subagent_usage` is excluded by name and the report names the exclusion (M24 — the
         spawn row is the only witness, and it grows by spawns, not by calls); `--yes`
         writes a backup first and aborts without one, while an empty plan never produces a
         pointless backup file
Evidence L0 2026-10-09: `scripts/tests` 610 passed on `python3`, on `.venv/bin/python` and
         with `OPENCODE_SKILL_TRACKER_SKILLS_DIR` at a nonexistent directory; `__selftest`
         63/63 (the writer was untouched); `bash -n bin/skillt`. Against the live database
         only the read-only paths ran: off → "nothing is scheduled", `--keep-days 365` →
         0 rows, `--keep-days 1 --json` → 163 rows scheduled and `"deleted": {"total": 0}`,
         and the database came back byte-for-byte the same as before the run (606,208 B,
         4 / 21 / 139 / 3 / 44 / 81 rows). The `--yes` path ran on a synthetic sandbox
         database in `/tmp`: a 200-day row deleted, a 1-day row kept, and the backup taken
         first still held both
Cost     One test caught that the fixture, not the code, had the version ordering backwards:
         `h1` was the newest, so "kept the oldest versions" was a wrong *expectation* and the
         `recorded_at DESC` ranking was right. Writing the delete set as an id list rather
         than re-running a DELETE predicate costs a chunked `IN (…)`, and buys the guarantee
         that the printed count is the same query. Rows leave free pages behind, so the
         message says the file will not shrink until `skillt vacuum` rather than implying it
Commit   adbebca

### D-044 · 2026-10-09 · guard
Symptom  M17 had two halves and only one was closed: the reader stopped at
         `TRACKER_LOG_BYTES_CAP` while the file kept growing, one line per init and per
         dispose, with nothing in the tool ever moving it. Two caps that are separate
         numbers are one bug waiting to drift — an active log can hold lines `doctor`
         never sees
Change   `skillt rotate-log` (`plan_log_rotation` / `rotate_log` in `skill_db.py`) renames
         the log at `log.max_bytes`, which **is** `TRACKER_LOG_BYTES_CAP` — one number for
         both sides. `log.keep_files` generations survive as
         `skill-tracker.log.<UTC stamp>[-n]`, mode 0600, and only names matching that shape
         are pruned; the command takes no path argument and `_own_log` refuses any basename
         that is not one of `LOG_FILE_NAMES`, so a neighbour's log in the same directory is
         not ours to move. The live file is re-created empty (a missing log makes `doctor`
         WARN about a healthy plugin) and **without** `O_TRUNC`, because the writer appends
         per line and can land a line between the rename and the re-create. Needs no
         database, and dispatches before the database check
Evidence L0 2026-10-09: `scripts/tests` 628 passed on `python3`, on `.venv/bin/python` and with
         `OPENCODE_SKILL_TRACKER_SKILLS_DIR` at a nonexistent directory; `__selftest` 63/63
         (the writer was untouched); `bash -n bin/skillt install.sh`. Against the live log
         only the read-only paths ran — `rotate-log` and `rotate-log --json` both said
         "below the cap", and the file's sha256 was identical afterwards; the exact command
         the timer runs (`~/.local/bin/skillt rotate-log --yes`, through the installed
         symlink) was also run and left the log byte-identical. `systemd-analyze verify
         --user` accepts the two-command unit. **Not** verified: starting that unit through
         systemd since the change, because doing so also prunes real backups
Cost     The first version of the same-second suffix compared the *name pattern* instead of
         asking the filesystem, so three rotations inside one second all chose `-1` and each
         `os.rename` silently replaced the generation before it. A test that loops three
         rotations found it; `auto-backup`, by contrast, still errors on a same-second name
         (D-043's row says why the two differ). `MAINTENANCE.md` §0 claimed the timer was
         "verified by running the service once" — true of the one-command unit, stale for
         the two-command one, so that row now says which part ran and which did not, in
         both languages
Commit   ecc2d5f

### D-045 · 2026-10-09 · guard
Symptom  The documentation gate that exists because six environment variables shipped
         undocumented did not look at `install.sh`, which reads three more
         (`SKILLT_CONFIG_DIR`, `SKILLT_BIN_DIR`, `SKILLT_SYSTEMD_DIR`) and names none of them
         in either README. Widening it naively also mis-read bash: `${LINK_DST[$i]}` is an
         array element, not an environment read
Change   `install.sh` joined the gate's source list, and the bash pattern now requires the
         name to be followed by `}`, `:-` or `-`. Per-language shapes are the point: the same
         spelling means a string template in JavaScript and an environment read in shell, so
         one regex for both files would either demand documentation for nothing or miss a real
         variable
Evidence L0 2026-10-09: the gate went red naming exactly the three installer variables and
         nothing else; after the README rows it passes on both languages, and
         `test_the_documented_flag_list_matches_the_parsers` caught `--max-bytes` and
         `--keep-files` missing from `bin/skillt` the moment they entered the registry
Cost     `test_installing_the_timer_is_still_opt_in` checks the installer by the *actions*
         (`cp -f`, `systemctl --user`, `mkdir -p "$SYSTEMD_DIR"`) rather than by the words,
         because naming a directory is harmless and filling it is not — a word-based version
         of that assertion failed on the variable definition and would have taught nobody
         anything
Commit   ecc2d5f

### D-046 · 2026-10-09 · fix
Symptom  The new card breakpoints picked three across at 55 columns and six at 109, where the
         layout gives the narrowest card 16 columns and a 13-column label wraps — so the fix for
         "labels wrap on a narrow terminal" reintroduced it at two widths nobody tested
Change   `_CARD_PAGE_PADDING` was the 2 the CSS seems to say; measured, the grid is 4 columns
         narrower than the terminal at every width tried (36 → 32, 48 → 44, 110 → 106), because
         the page padding and the scroll container's own column both come off it. The thresholds
         are now 111 / 57 / floor, and the test pins the chosen column count *and* the wrap at
         both sides of each (40/48/56/57/60/110/111/120) instead of three widths that all
         happened to sit on the safe side of the bug
Evidence L0 2026-10-09: 648 passed on `python3`, on `.venv/bin/python`, and with
         `OPENCODE_SKILL_TRACKER_SKILLS_DIR=/tmp/does-not-exist`; with the old padding the
         boundary cases return 3 at 56 and 6 at 110, which is what makes them boundary cases
Cost     The arithmetic assumes Textual's own remainder split. An upgrade that distributes the
         grid evenly moves the two thresholds, and only the laid-out test will notice — the CSS
         will still read 2
Commit   44372b7

### D-047 · 2026-10-09 · fix
Symptom  `skillt doctor` read the plugin log at `TRACKER_LOG_BYTES_CAP` — `settings`'
         **default** for `log.max_bytes` — while `skillt rotate-log` moved the same file at
         the **resolved** value. The two agreed only at the shipped configuration, and the
         comment on the constant claimed a line invisible to `doctor` was structurally
         impossible. It is not: `log.max_bytes = 8388608` rotates at 8 MiB and counts the
         first 4, measured as the reader passing `4194304` against a resolved `8388608`
Change   `doctor` takes `args.log_max_bytes`, and prints the bound it actually used. The
         two log bounds became class defaults on `Args`, read from the registry, so a
         hand-built `Args` (tests, future entry points) carries the same number instead of
         a second copy of it; the two test stand-ins now subclass `st.Args` rather than
         re-declaring the fields
Evidence L0 2026-10-09: the new guard went red first with those two numbers in the
         assertion, then 649 passed on `python3`, on `.venv/bin/python`, and with
         `OPENCODE_SKILL_TRACKER_SKILLS_DIR=/tmp/does-not-exist`;
         `test_the_rotation_cap_is_the_number_the_reader_reads` was widened from
         default-vs-default to comparing the two commands' resolved values
Cost     A raised cap now costs `doctor` proportionally more reading — that is the owner's
         own number, which is the point; the 1024 floor and the 4 MiB default are what keep
         it from being a trap. Any doc that said "`log.max_bytes` *is* `TRACKER_LOG_BYTES_CAP`"
         was describing the accident and had to be corrected (six passages, both languages)
Commit   26f5473

### D-048 · 2026-10-10 · fix
Symptom  `skillt doctor` reported `backups.latest` PASS at 3.0 days on a host where
         the backup timer had been removed on 2026-10-07 21:48 (`systemctl --user
         is-enabled skillt-auto-backup.timer` → `not-found`, no `skillt*` unit file in
         `~/.config/systemd`, `/etc/systemd` or `~/.local/share/systemd`, and
         `journalctl` shows no service run after 2026-10-07 00:18). The check measured
         **the age of an artifact** and printed that as evidence the next backup was
         coming. Nothing in the suite or in `doctor` could see the difference, and the
         row in MAINTENANCE §0 that asserted the timer was `enabled` had been false for
         three days
Change   `backups.scheduled` asks `systemctl --user is-enabled BACKUP_TIMER_UNIT` with a
         2 s deadline and WARNs on any state but `enabled` — including `None`, because a
         machine it cannot ask is *unknown*, not healthy. Advisory only: not installing a
         user unit is a legitimate choice, so the check can never turn `doctor` red. The
         subprocess lives in `_backup_timer_state()` (the same shape as
         `_opencode_version()`) so no test ever shells out, and the unit name is pinned to
         the files in `skill-tracker/systemd/` and to what `install.sh` copies
Evidence Red first: the 8 new tests fail with `AttributeError: module 'skill_tui' has no
         attribute '_backup_timer_state'` (6 surfaced under `-k "timer or scheduled"`, the
         selector not matching `…unreachable_systemd…`; the 8th, the unit-name pin, is the
         one that needs no subprocess at all). Then green — and `backups.latest` PASS
         beside `backups.scheduled` WARN is asserted as the *expected* disagreement, not
         as a coincidence. Live 2026-10-10: `skillt doctor` → 17 PASS / 4 WARN /
         0 FAIL with the new line naming `not-found` and `./install.sh --with-timer`;
         `--json` carries it. L0: 660 passed on `python3`, on `.venv/bin/python`, and with
         `OPENCODE_SKILL_TRACKER_SKILLS_DIR=/tmp/does-not-exist`; `__selftest` 63/63
         (writer untouched)
Cost     `doctor` now starts a process on a headless CLI path — the reason it is confined
         to one helper with a deadline, and why the repaint path is asserted not to reach
         it. Hosts without user systemd gain a WARN they may not care about; the message
         says "cannot ask", not "broken". MAINTENANCE §0 now records two readings
         (3.0 d fresh / no schedule) where it used to record one
Commit   b56dcc4

### D-049 · 2026-10-10 · fix
Symptom  `_backup_timer_state()` (added the same round, D-048) read the unit's state
         from stdout and **fell back to stderr**. Measured with
         `XDG_RUNTIME_DIR=/nonexistent`: `systemctl --user is-enabled` prints
         `Failed to connect to user scope bus via local transport: No such file or
         directory` to stderr, prints **nothing** to stdout and exits 1 — so the
         fallback returned that sentence as a state. `doctor` then printed a bus error
         where a unit state belongs, and classified an unreachable manager as
         "not scheduled", which is a different claim about a different fact
Change   Only stdout is a state; an empty stdout is unknown whatever the exit code is.
         `not-found` still arrives on stdout with exit 4, so the case that motivated
         D-048 is unaffected — the docstring now says which stream is authoritative
         and why
Evidence Red first: the new test asserts `_backup_timer_state() is None` against a
         captured real result object and fails with the bus sentence in the assertion.
         Then green on both real states of this host: reachable →
         `skillt-auto-backup.timer: not-found — …`, unreachable → `cannot ask systemd
         about skillt-auto-backup.timer (no user `systemctl`?) — unknown is not healthy`,
         `17 PASS / 4 WARN / 0 FAIL` in both. L0 661 passed on `python3` and on the
         hermetic run
Cost     A machine whose user manager is down now says so instead of recommending
         `--with-timer` — which is the right message but a longer one, and a reader
         who only scans the check name loses that distinction
Commit   65b57b3

### D-050 · 2026-10-10 · guard
Symptom  Counting the suite's own subprocess calls found **11 real**
         `systemctl --user is-enabled skillt-auto-backup.timer` runs leaving three
         `_doctor_checks` callers — `test_claude_mem._doctor`, the two calls in
         `test_doctor_reads_a_rotated_log_without_crying_wolf`, and
         `test_doctor_fails_on_corrupt_database`. None of them stubs the schedule
         helper, so they passed by reporting whatever this machine happened to be
         running: green here, a different green on a machine with the timer
         installed, and AGENTS.md was claiming the capture tests are hermetic
Change   `conftest.never_query_the_hosts_service_manager` (autouse) guards
         `subprocess.Popen` — not `run`, because `run`/`call`/`check_output` all
         build a `Popen` through the module global, so a check added later is caught
         whichever door it uses. The three callers now stub
         `_backup_timer_state`, the same way `_healthy_setup` already stubs
         `_opencode_version`. The guard's scope limit is written down: it cannot see
         inside the child processes `test_dispatcher.py` spawns
Evidence Red first, twice. (1) The guard alone, before the stubs: **661 passed, 8
         errors**, the 8 being exactly the 11 attributed calls. (2) A raising-only
         guard is not enough, measured rather than argued — with the call-time raise
         and no teardown record, the same un-stubbed code gave `6 passed`; adding the
         teardown record gave `6 passed, 6 errors`, because every host question in
         `doctor` sits inside `except Exception` ("a version check must never break
         doctor") and turns a thrown guard into a plausible WARN. Both halves are now
         pinned by `test_the_systemd_tripwire_bites_even_when_a_check_swallows_it`,
         which provokes the needle (mutating it to `systemctlZZZ` fails it: DID NOT
         RAISE), replays that swallow, and keeps a `git --version` control so the
         guard has not become "no subprocess ever". After: 662 passed on `python3`,
         on `.venv/bin/python`, and with
         `OPENCODE_SKILL_TRACKER_SKILLS_DIR=/tmp/does-not-exist`; the same spy now
         counts **0** calls. Real-host `skillt doctor` is unchanged —
         `backups.latest` PASS (3.5 d) beside `backups.scheduled` WARN (`not-found`) —
         because no production file was touched
Cost     One more line in every test's teardown, and a stub a future `_doctor_checks`
         caller must remember — the guard exists so forgetting it is loud. A test that
         *legitimately* wants to check systemd behaviour must ask for the hits list and
         clear it, as the tripwire's own test does. MAINTENANCE.md was also carrying
         two schedule invariant rows saying the same thing (mine, from two sittings);
         merged, and both checklists' hermeticity rows now name `systemctl`
Commit   da4ad5f

### D-051 · 2026-10-10 · guard
Symptom  Auditing AGENTS.md's invariants table the way the last entry audited the
         suite (count what the command actually does, don't read the prose) found
         **two rows whose documented command does not run a check the same row
         names**. `-k "timer or scheduled"` collects 7 of test_doctor.py's cases and
         *not* `test_an_unreachable_systemd_is_reported_as_unknown_never_as_healthy`;
         a `key_help` needle does not match the id
         `test_a_new_binding_documents_itself_in_the_help`. Neither row looked wrong —
         each names a real guard and a real command, which is why the mismatch is the
         dangerous shape: the reader runs it, sees green, and believes the guard ran
Change   Both rows' selectors widened to cover what they claim (schedule +=
         `systemd`; interface `key_help` → `help`, still a substring of every name it
         is meant to reach). The pairing is then made machine-checked by
         `test_every_invariant_rows_command_selects_the_checks_it_names`, which parses
         the table, and for each row either matches every named id against the row's
         own `-k` needles or looks the name up in the paths the row runs. It is static
         on purpose — a nested `--collect-only` per row would cost minutes for no
         extra truth, since `-k` *is* substring matching — and it refuses any selector
         that stops being a plain or-list rather than quietly reasoning about a
         grammar it no longer understands
Evidence Instrument faults first, because both were wrong before the answer was:
         splitting the command with `str.split()` shredded the quoted selector (every
         row "failed", 0 collected), and appending `-q` to a row command that already
         carries `-q` collapsed pytest 9's listing to a one-line summary, so a
         `::name` regex found nothing and reported seven guilty rows. `shlex.split()`
         plus one `-q` and parsing both node-ids and `<Function x>` gave the real
         answer: **1 row of 7 mismatched**, then 0 after the fix. The gate is provoked
         by `test_an_invariant_row_that_names_a_missing_check_is_caught`, which injects
         a check that does not exist and a needle that cannot match, asserts each is
         reported, and restores AGENTS.md byte-for-byte in a `finally`
         **(superseded by D-052 — a test must not write a tracked file; provoked on a string now)**
         (`_invariant_rows` reads through `ROOT`, so patching the module name would
         have tested nothing — that is why it edits the file). After: 664 passed on
         `python3`, on `.venv/bin/python`, and with
         `OPENCODE_SKILL_TRACKER_SKILLS_DIR=/tmp/does-not-exist`; the doc gates cost
         0.15 s. No production file touched
Cost     A row's `-k` expression is now constrained to a plain or-list unless someone
         also teaches the gate a richer grammar — deliberate, because the failure mode
         being prevented is a check nobody notices is missing
Commit   6a244c1

### D-052 · 2026-10-10 · chore
Symptom  D-051's provocation test provoked the gate by **writing into AGENTS.md** and
         restoring it in a `finally`. A tracked document therefore sits modified for
         the length of every pytest run: an interrupted run leaves it mutilated, and a
         second agent editing the invariants table fights the test for that file.
         Nothing about the evidence needed a write — the checker only ever takes text
Change   `_invariant_rows()` and the gate's loop split into
         `_invariant_rows(text)` / `_invariant_row_mismatches(text)`. The gate reads
         AGENTS.md and hands the checker its contents; the provocation hands the same
         checker a mutated **string**. Both shapes are still provoked (a named-but-
         absent check, a needle that cannot match), a control now asserts the real
         table is clean, and no file is touched at all
Evidence neutering `_invariant_row_mismatches` to `return []` turns the provocation red
         at `assert any(n == "test_a_check_nobody_wrote" …)`, so the test is still
         load-bearing after the refactor instead of merely passing. Restored
         byte-identically (asserted in the script), with `__pycache__` purged first so
         a same-second edit cannot be served from a stale `.pyc` — the trap this
         project hit in D-048/D-051's own red-checks. During the run `git status
         --short` shows only the source edit: **no test modifies a tracked file any
         more**. 664 passed on `python3`, on `.venv/bin/python`, and with
         `OPENCODE_SKILL_TRACKER_SKILLS_DIR=/tmp/does-not-exist` — re-measured on all
         three at `9fb3648`, where the rule itself was written into AGENTS.md
Cost     Two more module-level functions in `test_readme.py` instead of one test that
         reaches for its own repo root. The old version's `monkeypatch.setattr(tr,
         "ROOT", tmp_path)` form would have been worse than useless here: `ROOT`
         already equalled that value, so the injection would not have entered the
         branch it claimed to exercise
Commit   b9eb105

### D-053 · 2026-10-12 · revert
Symptom  The AgentOS advisor reader and the claude-mem reader were the only parts
         of this project that opened *another program's* state, and the project is
         being rebuilt as a passive usage tracker that reads OpenCode's own
         database. Keeping them would have put two sources of truth under one set
         of panels — and while removing them, three promises turned out to be
         pinned to nothing (D-054, D-055), so the withdrawal is what made them
         visible
Change   `scripts/skill_db_agentos.py`, `scripts/skill_db_claude_mem.py`, the
         `skillt agentos` / `skillt claude-mem` commands, `doctor`'s
         `claude_mem.capture` line and the Plugins page's dim activity line are
         gone, with `test_agentos.py`, `test_claude_mem.py` and
         `test_module_boundaries.py`. AGENTS.md's layer list, its foreign-store
         rule, its conventions and the invariant table now describe only what
         still has a subject *and* a check; M21/M22 are marked withdrawn in place
         and keep their numbers, so `M1–M24` stays the true range and every dated
         reading beside them stays readable
Evidence Red first: deleting the two modules aborted collection with **4 errors**
         (`test_doctor`, `test_module_boundaries`, `test_rotate_log`, `test_tui`),
         rc=2 — the guards were wired to them, which is the proof the deletion was
         real. After the sweep: `598 passed, 2 failed` on `.venv/bin/python` (both
         failures are this record's own gates, closed by this entry), the *same*
         598 with `OPENCODE_SKILL_TRACKER_SKILLS_DIR=/tmp/does-not-exist`, and
         `577 passed, 21 skipped` on system `python3` 3.14. Collection went 666 →
         600, accounted in full rather than eyeballed: 63 `def test_` bodies
         removed (13 + 43 + 3 in the deleted files, 5 individual cases elsewhere)
         plus 4 parametrised instances (two `agentos` rows in the English-output
         matrix, two dispatcher commands). Read-only L2 on the real database:
         `skillt health` and `skillt plugins` both rc=0, and the removed commands
         fail by naming the commands that still exist
Cost     The two readers were the only copy of that integration knowledge; the
         field whitelists and the HTTP-by-type projection survive here as the
         record, and the rule they enforced now governs any future foreign store.
         AgentOS cites these paths in ten documents — no code dependency, so
         nothing breaks, but those anchors now point at withdrawn files and are
         listed for its owner rather than silently rewritten. One unplanned loss:
         `~/.local/bin/skillt` disappeared during this round for a cause not
         established, and the suite **went green while 21 dispatcher tests skipped
         on it** — a whole production surface stopped being tested and only the
         skip count betrayed it. Relinked by hand and re-verified (21 passed)
Commit   0c2a39d

### D-054 · 2026-10-12 · fix
Symptom  `scripts/skill_db.py` — the module that owns the "nothing leaves the
         machine" promise — imported `http.client` and `urllib.request as urlreq`
         with **zero references anywhere in the project**. They were the residue of
         the code that moved out to the neighbours: the writer of the promise kept
         an HTTP client nobody called, and no test could see it, because nothing
         fails when an unused import sits beside a correct one
Change   Both imports removed
Evidence `grep -n "urlreq\|http.client" scripts/skill_db.py` is empty, and the
         suite is unchanged at 598 passed — which is exactly the point: the removal
         is not what the suite proves. What proves the *promise* is the retargeted
         repaint guard in D-055, which now arms the standard library's own doors
         instead of trusting one named function
Cost     None at runtime. A future reader who wants an HTTP call must add the
         import deliberately and answer to the guard, not find one already there
Commit   0c2a39d

### D-055 · 2026-10-12 · guard
Symptom  Two gates could not fail on the shape this round produced. (1)
         `_invariant_row_mismatches()` built its list of cited checks with
         `` `(test_[a-z0-9_]+)` ``, which never matches a *file* name
         (`test_claude_mem.py` has a `.` outside the class), and then bailed with
         `if not names: continue` — so every row citing whole files was unchecked,
         and its command could name three deleted files and pass. Its `-k` half
         matched needles against the **id string** in the document, not against the
         suite, so a citation of a test that no longer existed still "matched".
         (2) `test_no_http_is_reachable_from_the_tui_refresh_path` spied on
         `st.cm.claude_mem_http`; take the neighbour away and the test has no
         target, so the promise "no HTTP on the repaint path" would have left with
         the code it was written against
Change   (1) Existence is now checked before arithmetic: every `test_*.py` a row
         cites must exist under `scripts/tests/`, and every `test_*` function name
         must be defined somewhere in the suite. A provocation case injects
         `test_a_file_nobody_deleted_back.py` into a real row **as a string** and
         requires the gate to catch it — no tracked file is touched, per D-052.
         (2) The repaint guard arms three doors (`urllib.request.urlopen`,
         `http.client.HTTPConnection`, `socket.create_connection`), requires each
         stub to answer once *before* believing the clean run, then visits every
         page plus `s` and `r`. `socket.socket` is deliberately not patched:
         asyncio's own selector loop builds its self-pipe through it, so patching
         it makes the harness fail rather than the app
Evidence The strengthened gate went red on the first run and named **exactly 4
         stale citations across 2 rows** — one deleted check in the "pure local"
         row, three deleted files in the foreign-store row — which is what proves
         it had been blind rather than merely quiet. Both rows were then fixed in
         the document, not by loosening the gate. The new HTTP guard's first
         version failed at the arming step for the reason above, and that failure
         is recorded in its docstring so nobody re-patches `socket.socket` later
Cost     The gate reads every test file to build its two name sets (one pass,
         ~0.1 s in a 0.7 s suite). A row that cites nothing checkable must now be
         deleted instead of left as prose — which is how the foreign-store row
         went, and the reason D-053's Cost line says the promise returns with its
         own check when a foreign store is read again
Commit   0c2a39d

### D-056 · 2026-10-12 · guard
Symptom  The passive reader landed with two checks that *looked* like they proved
         something and did not, and one coverage hole to go with them. (1) The
         privacy scan put its sentinels in prose fields **outside** the tool
         arguments, so a writer that dumped a whole `state.input` dict into
         `skill_usage.skill` — the exact leak the project's one privacy promise
         forbids — passed. (2) The "longest MCP prefix wins" test used `basic` vs
         `basic-memory`, where the separator makes the short name unable to match
         at all, so it stayed green with the prefix sort reversed. (3) Neither
         `scripts/ocut_db.py` nor `scripts/ocut-sync.py` was in
         `test_record_coverage.CODE_PATHS`, which is the list that decides whether
         a commit to a file has to be accounted for at all
Change   Both tests re-cut until they discriminate: a sentinel now sits *inside* a
         skill call's arguments, and the prefix fixture uses `playwright` vs
         `playwright_browser`, the case that really does split a tool name in half.
         The two new code paths are listed the day the files exist, with a comment
         naming what absence costs. `reconcile()` was written and then deleted in
         the same sitting: nothing called it, and its two-granularity check is
         pinned by a test that runs on every suite instead of a helper no command
         reaches — the schema comment that credited `ocut health` with that check
         was reworded to credit the test, because a comment naming a check that
         does not exist is how a panel starts lying
Evidence Provoked against the committed product, eight mutations, eight reds —
         `state.title` into `project`, a read-write host connection, no cursor
         holdback, `session` behind a cursor, `usage_available` back to "nothing
         is missing", dropping an orphan step instead of writing it, shortest
         prefix first, inventory merged instead of rewritten, no renumbering reset,
         unparsed counts silently zeroed, and both host guards removed together.
         The two named above were caught *being ineffective* by the same process,
         which is the only reason they are worth recording. `mode=ro` alone did not
         make the write test fail — `PRAGMA query_only` held it, so that test is
         written against both guards and the mutation has to remove both
Cost     One more path list to keep current when a module is added, and a
         CHANGELOG entry for checks that had not yet failed in production. The
         reader itself is documented in its own registry block and commit message
         rather than restated here
Commit   8ab2b31
### D-057 · 2026-10-12 · fix
Symptom  The suite pushed at 8ab2b31 was **green on this machine and red on the
         runner** (`CI 38243202108`, job `test (3.11)`, IndexError). One test —
         `test_tool_calls_land_in_tool_calls_and_their_specialised_table` — called
         sync without a config home, so the MCP server list and the inventory came
         from the real `~/.config/opencode` on the machine running the test. Here
         that file declares `context7` and `basic-memory`, so the tool name split
         correctly and the row existed; on a runner with no such file the server
         list was empty, nothing was attributed, and `mcp_usage[0]` did not exist.
         The product was right and the test was wrong, which is the worst kind of
         red: it says nothing about the code and everything about where it ran
Change   An autouse fixture `no_real_host_config` points `db.HOST_CONFIG_DIR` and
         `db.HOST_DB` at paths that cannot exist, so a test that forgets
         `host_config` fails **in the test** rather than succeeding here and
         failing elsewhere. The case itself now passes a fixture config declaring
         the server it expects. The docstring names this incident so nobody
         removes the fixture as "paranoid"
Evidence Both directions provoked. (1) Guard on, call still forgetful: red under
         the real HOME — the false green is gone. (2) Guard off *and* call
         forgetful: green under this HOME, red under `HOME=/tmp/fakehome`, i.e.
         the CI/local split reproduced on one machine, then closed. Full suite
         after the fix: 668 passed on the real HOME and 647 passed + 21 skipped
         under `HOME=/tmp/fakehome XDG_CONFIG_HOME=… XDG_DATA_HOME=…` (the skips
         are the dispatcher cases with no `~/.local/bin/skillt`)
Cost     One more autouse fixture every ocut test now inherits, and a test that
         wants the real config must say so explicitly — which is the point. The
         runner being 3.11 while both local interpreters are 3.13/3.14 did **not**
         cause this; the version axis and the machine axis were conflated in the
         first reading of the failure, and only the fake-HOME reproduction
         separated them
Commit   55acde0

### D-058 · 2026-10-12 · fix
Symptom  Two panels said more than the data supports. The cache panel printed
         `Input` a second time under the label "Uncached in" — a number carried
         over from a host where the cache miss *is* a separate field. OpenCode
         puts the uncached prompt in `input` and the reused part in `cache.read`,
         both already inside `tokens.total`, so the two lines were one column
         wearing two names, and a reader could only try to work out what the
         difference meant. The Plugins tab printed four count columns
         (`uses / skills / agents / cmds`) whose only possible value is 0: a
         passive reader has no evidence that a plugin ran, so the panel showed
         "nothing recorded" in the place where a user reads "nothing used".
         The commit that shipped it had already claimed the column was gone.
Change   `dash-cache-miss` / `sum-cache-miss` deleted; the hit rate moved out of
         the Tokens panel into the cache panel it divides (`dash-tok-hit-rate` →
         `dash-cache-rate`), so the cache panel is read → write → rate and the
         uncached number appears once, labelled Input. `PLUGINS_COLUMNS` is now
         plugin / version / declared as, fed by a `path` column added to
         `q_plugins` — the entry the host's own config spells — and `ocut
         plugins` prints the same three fields so the two surfaces cannot
         disagree about what is known. `scripts/tests/test_ocut_tui.py` (nine
         tests) is the TUI's own suite; `bin/ocut`, `scripts/ocut-tui.py` and
         `scripts/ocut-stats.py` joined the coverage gate's `CODE_PATHS`.
Evidence Fourteen provocations, each a mutation of a throwaway copy of the
         product under /tmp — no tracked file was edited to make a test red, and
         the unmutated copy ran green first as the control, because a probe tree
         that is not the product makes every red meaningless. All fourteen went
         red on the assertion they aimed at: a fourth cache number, "Uncached in"
         restored, panel lines stripped of their `[/dim]` marker (the label
         harvest then finds nothing and the floor catches it), a `uses` column
         back on Plugins, a window given to the Plugins tab, a shipped default
         moved to all-time, the dashboard and the tools tab each ignoring their
         own window, the tools tab reading another tab's window, the calls column
         swapped for the completed column, a tab label drifting, a token panel
         switched to another column, the row key built from the clipped cell, and
         a socket opened on the repaint path. Two findings were about the tests:
         clicking a tab *pane* does nothing (the strip's id is
         `--content-tab-<pane>`), which had made every per-tab claim a reading of
         the Dashboard — `activate` now clicks the strip and asserts the switch
         took; and an expectation re-derived from the wall clock is a race, so the
         app's clock is pinned by a fixture. The fixture keeps one failed tool
         call: with every call completing, `calls` and `ok` are the same number
         and a swapped column passes — that mutation was FAKE on the first pass.
Cost     The cache panel no longer prints the uncached prompt next to the cache
         numbers; it is on the Tokens panel under one name. The Plugins tab lost
         the per-plugin counts, which are not lost — `ocut inventory` lists every
         declared surface with its owner plugin, so four numbers that were always
         0 are now one read away as rows that carry information. The TUI suite
         adds about 45 seconds to the run, imports a sibling test module on
         purpose so the host fixture stays one definition, and now watches three
         more paths in the coverage gate — so a TUI-only repair needs an entry
         from here on. Measured: 692 passed / 21 skipped, the same numbers on
         this machine and under `HOME=/tmp/fakehome`.
Commit   d92cb6b

### D-059 · 2026-10-12 · chore
Symptom  Reconciling CI against this machine after the rename, `pytest -rs` printed
         the local skips as `../opencode-skill-tracker/scripts/tests/test_dispatcher.py:49`
         — a directory that does not exist (`ls -ld` says so). The suite was green, so
         nothing looked wrong, and yet every path in its output named a phantom file.
         Nothing here invented that string: `.pyc` validity is judged by source mtime
         plus size, and a **directory rename changes neither**, so the rewriting caches
         written before the rename stayed valid indefinitely and carried the old
         absolute path into every skip line, traceback and report.
Change   AGENTS.md's stale-`.pyc` rule gained the rename branch and the action that
         follows from it (purge `scripts/**/__pycache__` and `.pytest_cache`, and only
         then treat a path in test output as evidence). The caches were purged — all
         three are gitignored, so nothing shipped moved.
Evidence Read from the cache files before deleting them: `grep -c` for the old path
         hits in `test_dispatcher.cpython-313-pytest-9.1.1.pyc` and its 314 twin, and
         zero in the plain `cpython-313.pyc` — the lie lives in pytest's rewriting
         cache, not in the source. Both directions of the fix: before the purge the
         skip line reads `../opencode-skill-tracker/…`, after it reads
         `scripts/tests/test_dispatcher.py:49` with the same reason text ("dispatcher
         not installed"). And the suite is unchanged by the purge — 692 passed / 21
         skipped before, 692 / 21 after — which is the point: this was never a
         correctness failure, it was an instrument printing a path that cannot be
         opened. The same reconciliation closed the CI gap it had been standing in the
         way of: all 21 local skips are `test_dispatcher.py` (its launcher link is
         dangling), and the runner's extra 27 are the bun-gated ones — 692 + 21 =
         665 + 48 = 713 items on both machines.
Cost     No check enforces the purge, so the next rename that skips it prints phantom
         paths again and stays green; only the AGENTS sentence stands between that and
         a reader who believes the output. Recorded rather than automated because the
         trigger is a filesystem event no test inside the repository can observe.
Commit   bda057f

### D-060 · 2026-10-12 · guard
Symptom  The date-range picker had no test in this repository while being the one
         surface whose output is not a number: it decides the window every other
         panel then queries. Nothing said whether the 42 cells led on the right
         weekday, whether a day click landed in the card the person last touched,
         whether "end follows now" survived the picker's own writes, or whether the
         span handed back is the span the pane used. The port was faithful —
         `DateRangeModal` is 430 lines and a `diff` against CodeBuddy's copy reports
         no differing line, comments included — which is precisely the condition
         under which a component stops being tested because it is trusted.
Change   `scripts/tests/test_ocut_calendar.py`, 29 tests, no product line changed.
         Two expectations are deliberately derived *independently* of the product:
         the chip table is written out (label → key → id) instead of read from
         `PRESETS`, and the Sunday lead comes from an ordinal distance to a date
         known to be a Sunday instead of from `.weekday()`, so both disagree with
         the product rather than restate it. Covered: the twelve months of a real
         year plus the 42-cell claim, `-other`/`-today` (today from the instant the
         picker was *given*, not the machine's clock), the month stepper wrapping the
         year both ways, day and hour clicks going to the active card only with the
         hour keeping the typed minute, follow surviving a prefill and dying to a
         typed edit, four refusals and two acceptances, Cancel and Escape, and three
         tests that leave the modal — a picked window landing on the tab that asked
         and on no other, the Plugins tab never being offered a picker, and a
         `follow_now` end moving when the pinned clock moves.
Evidence Twenty provocations against a throwaway copy of the product, with the
         unmutated copy run green first. Three came back FAKE and all three were
         faults of the test, not the mutation: the chip-key check read its own
         expectation from the table it was checking; swallowing a parse error still
         *refused*, for the unrelated blank-end reason, so the case became an
         unparseable start with the end following, where a swallow dismisses as
         "all time"; and the follow assertion was aimed at a test that had itself set
         follow to False, when the one that proves it had set it True. Each was
         re-aimed and bit. Live in a 120x50 terminal: six chips, both cards prefilled
         from the tab's 7-day window, Sep 27 leading the Sunday-first grid, the 24
         hours in six columns, Escape closing. Measured: 721 passed / 21 skipped,
         identical on this machine and under `HOME=/tmp/fakehome`.
Cost     Still unproven, and not by accident: the `TimeInput` nudge (↑/↓ and the
         mouse wheel), the step selector's effect on those nudges, the quick-time
         chips' `now` path through a real click, and the modal's layout at a narrow
         terminal. None of those needed a product change today, so none of them has
         a red yet — the row that watches the panels does not reach inside the modal.
         The suite is Textual-driven, so it inherits the pinned textual range and
         about 45 seconds of runtime.
Commit   b1d34c8

### D-061 · 2026-10-13 · fix
Symptom  The MCP tab keys a row on `(server, tool)` because `v_mcp` groups by both,
         and the drill-down behind that row queried `mcp_usage.tool` alone. Two
         servers exposing a tool named `search` therefore opened the same four calls
         under either row, each screen titled with the server the person clicked and
         each header counting both — the title disagreed with its own list. Second: a
         double click pushed **two** copies of the drill-down, because
         `push_screen` is not awaited inside the row handler, so the tail of the
         click landed while the new screen was still mounting; the footer's "Back"
         then popped one and left the other on screen. Third: the plugin drill-down
         printed "no uses · all time" and "No calls recorded for this name", a count
         of zero where the reader holds no tool-ownership evidence at all.
Change   `server` travels from the row key into `HistoryScreen` and into the query,
         through a new `_history_conds` that `q_history` and `q_entity_summary` now
         share — the list and the header above it are one measurement at one scope,
         so they are not allowed to spell the predicates twice. The router passes the
         half it already parsed out of the key. A row select whose table is not on
         the visible screen is dropped. The plugin's empty state names the missing
         evidence in both places it appears.
Evidence 16 provocations against a throwaway copy of the product under `/tmp`, the
         unmutated copy run green as the control first. Dropping the server at each
         of the four places it now travels (the SQL condition, the list call, the
         summary call, the router) produced four separate reds — including
         `('alpha', 4, 3, 4)`, which is the alpha row showing beta's calls. Removing
         the `escape` half of the Back binding, the second-activation guard, the
         prefix order, the note's list of hidden names, and the exactness of the
         width the note promises each went red for its own assertion. Two of the 16
         were instrument faults, found and re-aimed: one needle did not exist in the
         file (the harness aborted before running it), and one deleted the word
         `hidden` rather than the column names the assertion looks for. Measured:
         736 passed / 21 skipped / 3 subtests, the same under this HOME and a fake
         one with the skills directory pointed at a path that does not exist.
Cost     `ModelResponsesScreen` (the Tokens tab's drill-down) is exercised only as
         far as the row that opens it; its own header and window are not pinned by
         this round's suite. The keyboard path — focus the table, Enter — is not
         driven either; every activation here is a click. And the left-hand half of
         `_cap_cell`, the one that keeps a path's tail, has no table column to reach
         it on this substrate: `session.project_id` is 6 or 40 characters with no
         `/` in any of 324 real sessions (measured 2026-10-13), so the drill-down's
         project column is an opaque id and a path arrives only as
         `tool_calls.directory`, which the detail screen prints whole. It is tested
         directly, and named here, rather than left as a rule with a phantom column.
Commit   8d2f53d

### D-062 · 2026-10-13 · guard
Symptom  The drill-down is three levels that hand identity to each other through a
         row key — the entity tab, that entity's calls in the tab's own window, one
         call's fields — and nothing in this repository followed a key across a
         single one of those handoffs. Every failure it could catch is a visible
         disagreement between two things on screen at once: the title above a table,
         the counts above the list, the row under the cursor, the record it opens.
Change   `scripts/tests/test_ocut_drill.py`, 9 tests, its own fixture because the
         shapes needed are specific: two MCP servers sharing one tool name (the only
         shape that tells a pair key from a tool-only query), a call whose message
         row is absent so a real NULL reaches the detail screen, a 35-character tool
         name and a 40-character project id so the cell cap bites in a table rather
         than only in arithmetic, and one failed call so `calls`, `ok` and `fail` are
         three different numbers. Part shapes still come from `test_ocut_sync`.
Evidence 29 provocations across D-061 and this entry, all red. Three faults were the
         harness's and are recorded because they are how a green suite can mean
         nothing: `app.query_one` searches the **base** screen, so a second-level row
         raised `NoMatches` and read as "nothing opened" when the opposite had
         happened; one activation pushed two screens, so `open_row` now asserts at
         most one level per activation — the Escape assertions in the same file had
         been failing for that reason and not for the one the message claimed; and
         the cap test's first premise was invented, since it asserted the project
         column shows a cut path, which the host's own `project_id` shape makes
         impossible. Measured: 9 passed for the file, 736 passed / 21 skipped / 3
         subtests for the suite, identical on this machine and under a fake HOME.
Cost     The suite drives Textual, so it inherits the pinned textual range and about
         49 seconds. Live-terminal confirmation of the drill-down (a real tmux
         session, real rows) was not re-run this round: the three-level chain is
         asserted against a fixture built through the real indexer, which is a
         stronger claim about the code than about the screen.
Commit   04b2da6

### D-063 · 2026-10-13 · fix
Symptom  `ocut --db X stats` and `ocut stats --db X` both failed with "unrecognized
         arguments: --db X". The launcher always places the subcommand first (argparse
         needs it there to match a subparser), so a `--db` typed before the subcommand
         was re-emitted after it, where a top-level-only `--db` is rejected.
Change   Give every subparser its own `--db` with `default=argparse.SUPPRESS`, so the
         flag is accepted on either side; SUPPRESS keeps the subparser from overwriting
         the top-level value with DB_PATH when `--db` is given only up front.
Evidence `scripts/tests/test_ocut_dispatcher.py::test_db_survives_being_placed_after_the_subcommand`
         (new) runs the real `ocut-stats.py` on both orders and proves the value reached
         the parser; the file re-run green, 34 passed. The stub-based routing cases above
         it never run the real parser, which is why this survived them.
Cost     `--db` is now declared twice (top level and each subparser); the subparser
         default must stay SUPPRESS or the top-level value is clobbered, and that trap
         is silent.
Commit   ef49533

### D-064 · 2026-10-13 · fix
Symptom  Quitting the TUI (`q`) could hang until an in-flight sync finished, and Ctrl+C
         during that wait printed a KeyboardInterrupt traceback out of
         `shutdown_default_executor`. Textual runs a thread worker on the event loop's
         default executor (`textual/worker.py`: `loop.run_in_executor(None, …)`), and
         `asyncio.run()` joins that executor before `app.run()` returns — so a sync
         started with `r` (or the 30 s auto-sync) that was still running at quit blocked
         the exit for as long as it took. Reproduced in a pty: `r` then `q` left the
         process alive past 8 s.
Change   Run the sync in a daemon thread the interpreter never joins
         (`threading.Thread(..., name="ocut-sync", daemon=True)`) instead of
         `run_worker(..., thread=True)`; hand the result back through a guarded
         `call_from_thread` (`_post_sync`, dropping the RuntimeError once the loop is
         gone) and skip the UI refresh in `_on_sync_done` when the app is no longer
         running. A sync abandoned at exit is safe: its cursor advances only on a
         finished pass, so the next sync re-reads what this one did not finish.
Evidence `scripts/tests/test_ocut_tui.py::test_the_sync_runs_in_a_daemon_thread_so_quitting_never_waits`
         (new) pins the thread name and the daemon flag; the file re-run green, 10 passed.
         Real pty: `r`+`q` three times, each `EXIT=0` in ~0.5–1 s, no traceback.
Cost     The sync thread is no longer tracked by Textual's worker manager, so nothing
         cancels it; a sync in flight at exit is abandoned and its transaction rolls back.
Commit   543a46f

### D-065 · 2026-10-13 · fix
Symptom  Once installed, the hook scanned its own file and advertised a tool it does not
         own: `localPluginSpecs()` enumerates `~/.config/opencode/plugin/*.js`, exactly
         where `install.sh` puts `ocut-hook.js`, and the hook's own source carries the
         literal `tool:{ … { compress: … } }` in a comment explaining the scan. The
         shape-based scanner read that as a real `compress` tool, so the Plugins tab
         listed `ocut-hook.js` as declaring `compress`.
Change   Exclude the hook by name: `PLUGIN_EXCLUDE_DEFAULT` gains `ocut-hook` alongside
         `opencode-notifier`, so its own spec yields an empty surface.
Evidence `scripts/tests/test_ocut_hook.py::test_the_hook_does_not_attribute_its_own_source`
         (new) installs the hook into the scanned plugin dir and asserts its surface is
         empty; the file re-run green, 4 passed. Re-run against the real config: the
         `ocut-hook.js` surface is now `tools=[]`, while DCP and claude-mem are unchanged.
Cost     A real plugin whose file name contains `ocut-hook` would be skipped; none is
         known, and the alternative — resolving the module's own realpath — is defeated
         by the install being a symlink.
Commit   530a45d

### D-066 · 2026-10-13 · guard
Symptom  The root-file layer was a naming convention with nothing behind it. No check in this
         repository asserted that the finalized files sit at the repository root, that `LICENSE`
         exists, or that `AGENTS.md` points at the machine-wide standard instead of restating it.
         A renamed or deleted root file would only be noticed by a person reading the diff.
Change   Add `scripts/tests/test_conventions.py` (9 cases, standard library only). It asserts the
         finalized layer is present at the root and that `pyproject.toml` is the only manifest;
         that `AGENTS.md` carries the one-line dimension pointer and reproduces none of the
         standard's own numbered section headings; that `.gitignore` still covers the runtime
         state the standard lists and that `git ls-files` tracks none of it; and that no process
         draft is tracked anywhere. `AGENTS.md` gains the dimension line above the shared-standard
         pointer it already had. Nothing is fetched — every assertion reads this working tree.
Evidence `python3 -m pytest scripts/tests -q` 251 passed (3 subtests), run with the final content
         of this entry in the tree. Provoked for real: moving `LICENSE` out of the tree turns the
         root-layer case red with `AssertionError: Lists differ: [] != ['LICENSE: create this file
         at the repository root']`, and restoring it turns the suite green again. Two earlier
         drafts of the guard were themselves red and were corrected rather than suppressed — a
         heading match that also caught this repository's own `## CI`, and an ignore-list spelling
         that only matched `.venv/`. No test was skipped, widened or moved to make a check pass.
Cost     The manifest and ignore lists are hard-coded, so a deliberate second manifest or a new
         runtime artefact is a change to this file rather than a surprise failure. It finds the
         repository root by walking up to `.git`, so it reads the same tree from `scripts/tests/`.

Commit   b190e60
### D-067 · 2026-10-14 · guard
Symptom  Nothing stopped a reader from importing `socket`, `httpx` or `subprocess`. The one check
         that talked about a network call — `test_no_http_is_reachable_while_the_tui_paints` —
         proves the property at runtime rather than at the import: it shows a request could not be
         made, not that no module was added that could make one, and the sentence the READMEs
         borrow from it describes the wrong thing. `import os` was likewise unremarkable to every
         existing check while being the one import that buys a shell.
Change   Add `scripts/tests/test_ocut_imports.py` (5 cases, standard library only). It scans the
         four production scripts' ASTs against an allowlist keyed per file: `ocut_db.py`,
         `ocut-sync.py` and `ocut-stats.py` are standard library only, and only `ocut-tui.py` may
         reach for a third-party package (`textual`, `rich`). Third-party roots come from
         `sys.stdlib_module_names`, not a typed list, so a dependency added tonight is caught
         tonight. `os.system`/`popen`/`spawn*`/`exec*` are caught as attribute calls, because the
         allowlist alone would pass `import os`. `AGENTS.md` gains the invariant row, whose
         `Run it` selector is checked against the real test names by the existing gate.
Evidence `python3 -m pytest scripts/tests -q` 295 passed (3 subtests), run with the final content of
         this entry in the tree. Provoked for real: inserting `import httpx` into `ocut-stats.py`
         turns two cases red and names the file and line — `ocut-stats.py: httpx (line 22) is a
         network module` — and removing it turns the suite green again. The guard's own meta-case
         provokes on strings rather than on the repository, so its evidence is the same and nothing
         is ever left edited.
Cost     `threading` is deliberately not forbidden and the file says so where a reader will see it:
         the TUI's 30-second sync worker is a thread, so a rule satisfiable only by rewriting the
         rule teaches the next person to route around it. The allowlist is per-file, so a reader
         that genuinely needs a dependency is a deliberate edit to this file rather than a silent
         import. The module does not cover the hook, which runs inside the host process and has its
         own contract in `test_ocut_hook.py`.
Commit   9839bd3
### D-068 · 2026-10-14 · fix
Symptom  The `state` column introduced two commits earlier was wrong about most of what it had
         to say. Every Skills row read `removed` on a machine carrying 27 installed skills in
         `~/.agents/skills`; 39 of 40 Tools rows read `removed` for tools being used at that
         moment. Two unrelated mechanisms behind one label, and the second is the worse half: a
         builtin tool id lives in the host's compiled registry, a builtin agent id in an object
         literal inside it, and no file on disk enumerates either. The host's skill discovery
         pushes `[".claude", ".agents"]` and globs `<root>/skills/**/SKILL.md` under `$HOME`
         *before* it reads its own config home at all; this reader read only the config home,
         which here is empty — so the panel was not a narrower version of the truth, it was an
         inverted one.
Change   Both external roots are read now, with the host's own semantics: dotfiles included and
         symlinked directories followed, which `pathlib`'s `**` does not do, so the walk is by
         hand behind a resolved-path set — the same set that turns a symlink pointing at its own
         ancestor into a return rather than a hang. Both runtime flags are honoured, since a root
         the host is told to ignore must not be inventoried either. The second mechanism cannot
         be fixed by reading harder, so the label gained a fourth state instead of a wrong one:
         `db.ENTITY_CENSUS` records which kinds were actually enumerated, and a tool or agent
         with no inventory row reads `not reported`. It only ever weakens a *negative* answer —
         a census can turn `removed` into "we do not know", never into a positive claim nobody
         made — and `skill`/`plugin`/`mcp` stay enumerated, so `removed` keeps its literal
         meaning on those tabs. Two smaller corrections came out of the same investigation: MCP
         tools are reconciled against the configured servers (a server's tool stopped reading
         `removed` while the server beside it read `installed`), and the one skill the host
         serves out of its own binary — registered before any scan, never written to disk — is
         inventoried, so it is not the single remaining `removed` on a machine where nothing
         was uninstalled. `entity_state()` takes the kind as a **required** argument: a default
         would let a front end keep rendering the old three states with no test noticing.
Evidence `python3 -m pytest scripts/tests -q` 316 passed (3 subtests), run with the final content
         of this entry in the tree. Provoked for real, nine times: deleting the external-root
         loop turns six cases red; making the walk skip symlinks turns
         `test_a_symlinked_skill_directory_is_followed` red; deleting the MCP reconciliation
         turns `test_a_tool_of_a_configured_server_is_inventoried` red; ignoring the runtime
         flags turns both disabling cases red; dropping `dot: true` turns
         `test_a_dot_directory_in_an_external_root_is_a_skill` red; marking every kind censused
         turns three red; removing the builtin-skill row turns two red; and each of M15's and
         M16's two keywords turns its README case red when the word is replaced.
         L1, real logs into a throwaway store (`/tmp`, the real `usage.db` untouched), reported
         per table because a regression in a table nobody expected to move stays invisible
         otherwise. `inventory` 19 → 62: `skill agents-skills-dir 27`, `skill builtin 1`,
         `tool mcp 15`, beside the 18 rows already there. Every other table is unchanged by this
         change — it writes only `inventory` — and the growth visible in `tool_calls` (+430),
         `model_calls` (+359), `sessions` (+3), `agent_usage` (+3) and `plugin_usage` (+4) is the
         real store's cursors catching up on a backlog, which two further syncs on the same
         store show bounded at +1/+1 with `inventory` unchanged. States after: tools 42 `not
         reported` / 17 `in use` / 9 `installed`; skills 22 `installed` / 6 `in use` / 14
         `removed`; agents 6 `not reported`; MCP 15 `in use`. All 14 remaining `removed` skills
         were checked against all three roots on disk and none is there.
Cost     Home is a parameter from `run()` down rather than an env lookup, which is what makes
         the suite able to pin it — a reader reaching for `Path.home()` behind the fixture would
         have read this machine's `~/.agents/skills` into a test's inventory, and did for one
         run before the fixture was fixed. The MCP reconciliation runs against the history
         *already* in the database, because `scan_inventory` runs before the transcripts are
         indexed, so a tool seen for the first time appears on the next sync — the same one-cycle
         delay as any other newly-seen name, and the reason the fixtures run twice. Symlink
         following is unbounded on purpose, to match the host: a cycle is handled by the
         resolved-path set, and a shared subtree is re-walked rather than pruned, so a large
         `~/.agents` tree costs a little time and nothing but time. The fourth state is a wider
         column, and it makes the Tools tab honest at the cost of being less quotable.
Commit   fca7151

### D-069 · 2026-10-15 · perf
Symptom  Every tab switch paid Textual's tab-bar underline animation. The tracker's first TUI had
         turned it off (`1e6342c`, where ~305 ms of a ~490 ms switch was the underline), but that
         line did not survive the rewrite onto the new schema: `scripts/ocut-tui.py` was a new file
         (`a49ca8c`) and `TrackerApp` never set the level, so the slide came back and stayed.
Change   `TrackerApp.__init__` sets `animation_level = "none"` right after `super().__init__()`.
         Textual's `Tabs._highlight_active` animates the underline for 0.3 s at level `"basic"`,
         which the app-wide `"basic"` setting does not suppress -- only `"none"` does. Set on the
         app, so screens pushed on top of it inherit the level too.
Evidence L0 2026-10-15: `test_the_tui_disables_textual_animations`, provoked red by commenting the
         assignment out (the level then resolves to Textual's default `"full"`), green with it.
         `python3 -m pytest scripts/tests -q` 317 passed (3 subtests), run with the final content of
         this entry.
Cost     The underline no longer slides -- deliberate, and the only way to remove it (`"basic"`
         does not). Anyone who re-adds animations later silently re-pays the switch cost.
Commit   02ec229

### D-070 · 2026-10-16 · chore
Symptom  The pointer in AGENTS.md is followed only by an agent that chooses to follow it: it named
         `~/.agents/standards/`, but nothing made that text reach the context, and the section list
         it carried had drifted — §5 (the conflict rule) was missing.
Change   End the pointer block with two bare `@` imports (`@~/.agents/standards/STANDARD.md`,
         `@~/.agents/standards/dimensions/a.md`) and add `opencode.json`, whose `instructions` array
         names the same two files. Add `§5 conflict rule` to the section list. `test_conventions.py`'s
         `ROOT_FILES` gains `opencode.json`.
Evidence 2026-10-16: CodeBuddy CLI (`-p --tools ''`, every tool disabled) in a directory whose only
         memory file was an `AGENTS.md` carrying the two imports printed the tokens they carried — the
         text is injected, not read by a tool. opencode `run` printed the token its `instructions`
         named, with no tool call. `python3 -m pytest scripts/tests/test_conventions.py -q`
         9 passed.
Cost     Both hooks name `~/.agents`, absent in another clone, so they are skipped there — the
         pointer stays the portable part. Nothing is copied, so nothing can drift.
Commit   92bde42

### D-071 · 2026-10-16 · fix · M3
Symptom  The static scan excluded `opencode-notifier` alongside `ocut-hook`, so a plugin shipped
         under that name had its surface read as empty and none of its tools was ever attributed
         — the stated reason (it registers nothing we track) only held for that one package, not
         for the name.
Change   Reduce `PLUGIN_EXCLUDE_DEFAULT` to `["ocut-hook"]` and scan every other plugin, the
         notifier included. Also drop an unused `import importlib.util` from `scripts/ocut-tui.py`.
Evidence L0 2026-10-16: `test_a_notifier_shaped_plugin_is_scanned_not_skipped` fails with the
         notifier put back into the exclusion and passes with it removed;
         `python3 -m pytest scripts/tests -q` 318 passed (venv and system interpreters).
Cost     A plugin whose source only *mentions* a `tool:{ … }` shape can still be misread, which is
         why the hook's own file keeps its by-name exclusion; anything now scanning that name
         needs the same care, and its surface rows are no longer guaranteed empty.
Commit   67c2293

### D-072 · 2026-10-16 · chore
Symptom  `ocut_db.cache_read_rate` had no caller once the TUI's Hit rate moved to
         `cache_hit_rate`, yet it was still shipped and unit-tested — while `cache_hit_rate`,
         the function every panel actually prints from, had no direct coverage of its own.
Change   Remove `cache_read_rate`; point its two cases at `cache_hit_rate`
         (`hit / (hit + miss + write)`, including the `write` denominator term and the
         undefined cases).
Evidence L0 2026-10-16: `python3 -m pytest scripts/tests -q` 318 passed; no remaining reference to
         `cache_read_rate` in the repository.
Cost     The OpenCode-native rate (`cache_read / (cache_read + prompt)`) is no longer written down
         in one place; re-derive it from the two columns if a native-rate view is ever wanted.
Commit   8523243

### D-073 · 2026-10-16 · perf
Symptom  `idx_tool_sess` and `idx_model_call_session` came from the first schema and no query ever
         filtered by either column, so every insert into `tool_calls` and `model_calls` maintained a
         lookup nothing reads. Separately, `SCHEMA_SQL = TABLES_SQL + VIEWS_SQL` had no caller at
         all: `ensure_schema` runs the two halves separately, in that order and with the stored
         version read between them.
Change   Drop both indexes. Because `CREATE INDEX IF NOT EXISTS` can add an index and never take
         one back, `ensure_schema` now retires every `idx_*` the current `TABLES_SQL` does not
         declare — after the newer-than-this-build check, so a future build's indexes are its own.
         Delete the dead constant and add three cases plus the invariant sentence in both
         MAINTENANCE files, so the declaration site stays the only place an index may appear.
Evidence L0 2026-10-16: measured on the real row counts (tool_calls 30,638 / model_calls 24,904 /
         sessions 368) — full-table insert 218.9 ms with the two indexes against 93.0 ms without,
         file 18.55 MiB against 16.44 MiB. Retire is provoked: removing the
         `_retire_undeclared_indexes(conn)` call turns
         `test_an_index_the_schema_no_longer_declares_is_retired_on_open` red, and putting
         `idx_tool_sess` back into `TABLES_SQL` turns
         `test_no_declared_index_is_keyed_on_a_session_no_query_names` red. Real file (snapshot
         `usage-20261016-201115-manual.db`, 20,824,064 B taken first): `migrate` returned
         `current`, `idx_*` went 17 -> 15, `quick_check`/`integrity_check` ok, and the row counts read
         the same on both sides of it (tool_calls 30,638 / model_calls 24,904 / sessions 368).
         `ocut sync` then finished in 0.3 s — 129 new calls, taking the file to 30,767 / 25,007 — and
         `ocut health` named the same schema 3, one cursor pair and 3 `unparsed` records as before.
         `python3 -m pytest scripts/tests -q` 321 passed.
Cost     A dropped index hands its pages to the freelist, not to the filesystem: the real file grew
         665 freelist pages (~2.7 MiB) and keeps its 20.8 MiB until somebody runs a `VACUUM`, which
         this project still has none of. Any index a later commit wants must be declared in
         `TABLES_SQL`, or the next open retires it.
Commit   08eca88

### D-074 · 2026-10-17 · perf
Symptom  `idx_plugin_usage_item` keyed `plugin_usage.item`, a column the shipped code only ever
         *selects*: `q_plugin_history`, `q_plugin_summary` and the plugin views return it, and no
         query compares it — no `=`, `LIKE`, `IN` or `ORDER BY` on it anywhere. It came from the
         same first schema as the two indexes D-073 retired and survived that sweep, because that
         sweep was found by grepping the source for a `WHERE` on the column and every plugin query
         here is built as an f-string, which a regex over source cannot see through. The same
         blind spot cleared `idx_tool_cat`, which is *not* dead (`ocut-sync.py` filters
         `WHERE category = 'builtin'`), so the two cuts are not decided by one argument.
Change   Drop the index. That is also the whole change to a live file: `ensure_schema` reads
         `_DECLARED_INDEXES` back out of `TABLES_SQL`, so deleting the line retires the index on the
         next open. Add one case beside the session-index guard, provoked in both directions.
Evidence L0 2026-10-17: reachability was measured off statements that actually ran, not source
         text — `open_db` was wrapped with `set_trace_callback` for the whole suite, capturing 1,452
         executable statements, each re-planned with `EXPLAIN QUERY PLAN` against a real-size
         database. `idx_plugin_usage_item` is named in 0 of them. The method does see a used index:
         the same scan names `idx_tool_name` in 168 plans, `idx_model_call_ts` in 194 and
         `idx_tool_cat` in 1, and it is what kept `idx_model_call_model_ts` (13 plans) rather than
         cutting it. Read path: every shipped `plugin_usage` read — history and summary for each of
         the three recorded plugins, plus the `plugin_surface` view — returns byte-identical rows
         before and after `DROP INDEX`, and a 50-pass read loop on the real row counts went
         0.861 ms -> 0.781 ms, so there is no read regression to weigh against the write side.
         Write path, at its honest size: with the real table holding 284 rows (327 by the end of
         the session), a 265-row `plugin_usage` load took 0.50 ms with the index and 0.44 ms
         without — noise, so this is **not** a speedup today. Extrapolated to 200,000 rows the same
         insert pass was 794.51 ms against 590.35 ms (+34.6%) and the in-memory database
         26,660,864 B against 23,199,744 B. The guard is provoked both ways: putting the
         `CREATE INDEX` line back into `TABLES_SQL` turns
         `test_no_declared_index_is_keyed_on_a_plugin_item_no_query_compares` red and restoring it
         turns it green. Real file — snapshot `usage-20261017-074740-manual.db` (20,824,064 B)
         taken first: `migrate` returned `current` twice, `idx_*` went 15 -> 14, no declared name was
         missing, `quick_check`/`integrity_check` ok, and `ocut health` printed the same bytes
         before and after. `python3 -m pytest scripts/tests -q` 322 passed.
Cost     A second cut of the same shape means the sweep's *method* was the weak part, not its list:
         any future claim that a column is unqueried has to come from statements that ran, not from
         a grep. The dropped index also hands its pages to the freelist, as D-073 says, and this
         commit left the real file at 20,824,064 B with 592 free pages. A `VACUUM` was run later the
         same day and took it to 17,735,680 B (17,723,392 B after the second drop) with every table
         and every view identical to the snapshot — recorded in `measurements.md` §2, which no
         longer says the file only shrinks under a rebuild. So "this project still has no
         `VACUUM`" above is a statement about this commit's date: the tool still has no subcommand
         for it, and running one is a manual step.

         One thing no test here could have caught, which is why it is written down: **the cut came
         back once.** At 07:48 the real file held 14 `idx_*`; a snapshot taken at 07:54 held 15,
         with `idx_plugin_usage_item` present again — in a window where no command of this cut ran.
         The only mechanism that does that is a process holding the *pre-cut* module: `ensure_schema`
         runs `TABLES_SQL` on every open, and that process's in-memory copy still declares the
         index, so `CREATE INDEX IF NOT EXISTS` puts it back. It was not reproducible afterwards —
         re-running the sweep left it at 14, and a `backup`, a `health`, a `VACUUM` and the whole
         suite each left the count at 14 — and the process was never identified; anything left
         running from before the cut (a TUI, a sync loop) would do it. The sweep is idempotent and
         runs on every open, so the next current build retires it again. What this costs is the
         strength of the claim: "retired" here means *retired on the next open by a build that no
         longer declares it*, which is not quite the same as gone.
Commit   5b2e5a6
