"""The project README must document every required section and limitation.

Repointed from the retired `skillt` tool to its successor `ocut`. The gates that
named a deleted artifact were rewritten against the ocut code (the TUI's tabs and
bindings, the headless commands, the Dashboard panels, the settings the resolver
actually uses) rather than dropped: the point of each one — a document that cannot
drift from the code — is unchanged.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]      # repo root
README = ROOT / "README.zh-CN.md"

sys.path.insert(0, str(ROOT / "scripts"))
import ocut_db as db  # noqa: E402  (stdlib-only: tomllib, sqlite3, pathlib)

# Every documented limitation, in both languages. Derived from the ocut code: the
# passive reader's boundaries (the host is the source of truth; nothing is written
# back; prose is never read), the one thing the opt-in hook must supply and how
# best-effort it is, the two token granularities, the sidecar's cap, the token
# columns the host has no field for, and the blind spot the state label inherits from
# the hook, the two state labels that are answers and the one that is an admission, and
# the external skill roots the reader does not walk, and the tab-level CodeBuddy
# parity that stops at the detail screens. M1-M17.
LIMITATIONS = tuple(f"M{i}" for i in range(1, 18))

# The load-bearing half of each limitation. A marker alone could survive a rewrite
# that dropped the meaning, so each Mx must sit beside a keyword that is actually
# true of the ocut code. The two READMEs are one document in two languages.
EN_KEYWORDS = (
    "source of truth",      # M1
    "never written",        # M2
    "nothing recorded",     # M3
    "static scan",          # M4
    "hook's own clock",     # M5
    "unparsed",             # M6
    "message body",         # M7
    "never added",          # M8
    "longest prefix",       # M9
    "truncation",           # M10
    "installed",            # M11
    "no systemd",           # M12
    "re-labelling",         # M13
    "no evidence",          # M14
    "not reported",         # M15
    "~/.agents/skills",      # M16
    "column-for-column",    # M17
)
ZH_KEYWORDS = (
    "事实来源",              # M1
    "从不写入宿主",          # M2
    "没有任何记录",          # M3
    "静态扫描",              # M4
    "hook 自己的时钟",       # M5
    "unparsed",              # M6
    "消息正文",              # M7
    "绝不相加",              # M8
    "最长前缀",              # M9
    "截断",                  # M10
    "已安装",                # M11
    "没有 systemd",          # M12
    "重新标注",              # M13
    "没有证据",              # M14
    "not reported",         # M15
    "~/.agents/skills",      # M16
    "无法逐字",               # M17
)

# --- HTTP status-code parity across a bilingual pair -------------------------
# A status code named in one language alone is a fact that silently contradicts the
# other language. A code counts only in an HTTP context (or quoted in backticks), so
# `500 ms` or `200 rows` is never mistaken for one.
_STATUS = (
    "200|201|202|204|206|301|302|303|304|307|308|400|401|402|403|404|405|406|"
    "407|408|409|410|411|412|413|414|415|416|417|418|421|422|423|424|425|426|"
    "428|429|431|451|499|500|501|502|503|504|505|506|507|508|510|511"
)
_STATUS_RE = re.compile(
    r"(?:HTTP|status|状态码|返回|returns?|responds?|replies?|answers?|gives?)[^\n]{0,30}?\b(" + _STATUS + r")\b"
    r"|`(" + _STATUS + r")`"
    r"|`(" + _STATUS + r")\s*\+", re.I)


def status_codes(path: Path) -> set[str]:
    return {m.group(1) or m.group(2) or m.group(3)
            for m in _STATUS_RE.finditer(path.read_text(encoding="utf-8"))}


def _text():
    assert README.is_file(), f"missing README at {README}"
    return README.read_text(encoding="utf-8")


def test_readme_has_all_sections():
    text = _text()
    # The zh-CN README mirrors README.md's 13 top-level sections in the same order.
    # The intent is unchanged: the Chinese documentation must carry the sections it
    # is expected to.
    for section in (
        "为什么", "环境要求", "安装", "使用", "设置", "工作原理",
        "备份", "故障排查", "已知限制", "仓库结构", "卸载", "参与贡献", "许可证",
    ):
        assert section in text, f"README is missing the '{section}' section"


def test_readme_section_counts_match():
    """README.md and README.zh-CN.md are one document in two languages.

    The Chinese file is the full reference and legitimately carries more `###`
    subsections, but the two must agree on the number of top-level `##` sections —
    a section added to one side alone is drift no other check here would catch.
    """
    en = (ROOT / "README.md").read_text(encoding="utf-8")
    zh = _text()
    h2 = lambda text: len(re.findall(r"^## ", text, flags=re.M))
    assert h2(en) == h2(zh), (
        f"section count drift: README.md has {h2(en)} '##' sections, "
        f"README.zh-CN.md has {h2(zh)} — add the missing section to the other side")


def test_bilingual_pairs_name_the_same_status_codes():
    """A status code named in one language alone contradicts the other language."""
    for zh in sorted(ROOT.glob("*.zh-CN.md")):
        en = ROOT / zh.name.replace(".zh-CN.md", ".md")
        if not en.is_file():
            continue
        ce, cz = status_codes(en), status_codes(zh)
        assert ce == cz, (
            f"{en.name} and {zh.name} disagree on an HTTP status code: only "
            f"{en.name} -> {sorted(ce - cz) or None}; only {zh.name} -> {sorted(cz - ce) or None}")


def test_readme_documents_every_known_limitation():
    text = _text()
    for marker in LIMITATIONS:
        assert marker in text, f"README.zh-CN does not mention {marker}"
    for keyword in ZH_KEYWORDS:
        assert keyword in text, f"known-limitation detail missing: {keyword}"


def test_english_readme_documents_every_known_limitation():
    """The landing page must not fall behind the reference doc.

    Regression: README.md once listed "M1 through M11" while README.zh-CN.md had
    M1-M13, and nothing compared them, so an English reader was told a shorter
    story about the same database.
    """
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    for marker in LIMITATIONS:
        assert marker in text, f"README.md does not mention {marker}"
    for keyword in EN_KEYWORDS:
        assert keyword in text, f"English limitation detail missing: {keyword}"


# --- maintenance checklists -----------------------------------------------
MAINTENANCE = (ROOT / "MAINTENANCE.md", ROOT / "MAINTENANCE.zh-CN.md")


def test_maintenance_checklists_exist_and_cover_the_same_ground():
    """Two languages, one checklist — and it must name the checks it depends on."""
    for path in MAINTENANCE:
        assert path.is_file(), f"missing {path.name}"

    en = MAINTENANCE[0].read_text(encoding="utf-8")
    zh = MAINTENANCE[1].read_text(encoding="utf-8")

    # The commands a reader runs when they suspect capture has decayed.
    for needle in (
        "ocut health", "ocut sync", "ocut sync --full", "ocut backup", "ocut restore",
        "ocut plugins", "ocut format",
    ):
        assert needle in en, f"MAINTENANCE.md missing: {needle}"
        assert needle in zh, f"MAINTENANCE.zh-CN.md missing: {needle}"

    # The load-bearing invariants, by the name a future editor will grep for.
    for needle in ("SCHEMA_VERSION", "TABLES_SQL", "VIEWS_SQL", "PRAGMA query_only",
                   "mode=ro", "unparsed", "sync_state", "plugin_surface"):
        assert needle in en, f"MAINTENANCE.md dropped an invariant: {needle}"
        assert needle in zh, f"MAINTENANCE.zh-CN.md dropped an invariant: {needle}"

    # Deviations that must not be "fixed" back, named in both languages.
    for needle in ("OCUT_HOOK_DISABLE", "OCUT_HOOK_EVENTS", "OCUT_CONFIG",
                   "OCUT_OPENCODE_CONFIG", "ocut-hook.js", "plugin-events.jsonl",
                   "BACKUP_KEEP"):
        assert needle in en and needle in zh, f"deviation not documented in both: {needle}"

    # LIMITATIONS[-1], not a literal: the end of the range moves whenever a
    # limitation is added, and a hand-typed "M12" here is a document that quietly
    # stops naming the newest one.
    last = LIMITATIONS[-1]
    assert "M1" in en and last in en, "MAINTENANCE.md must point at the limitations"
    assert "M1" in zh and last in zh, \
        "MAINTENANCE.zh-CN.md must point at the limitations"


def test_readme_documents_the_hook_is_the_only_plugin_writer_and_can_be_disabled():
    """The one writer outside the host database, and its kill switch.

    Repointed from `skillt`'s `__selftest` isolation (that plugin is gone). The
    surviving promise has the same shape: a second input must be named, and it must
    have an off switch a reader can find.
    """
    for name in ("README.md", "README.zh-CN.md"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert "OCUT_HOOK_DISABLE" in text, f"{name} never names the hook kill switch"
        assert "ocut-hook.js" in text, f"{name} never names the one hook"


def test_readme_documents_commands_and_backup_ops():
    for name in ("README.md", "README.zh-CN.md"):
        text = (ROOT / name).read_text(encoding="utf-8")
        for cmd in (
            "ocut sync", "ocut stats", "ocut tools", "ocut skills", "ocut agents",
            "ocut plugins", "ocut mcp", "ocut models", "ocut show", "ocut recent",
            "ocut inventory", "ocut export", "ocut health", "ocut format",
            # The two that touch the database file itself: a backup is the only
            # deliberate snapshot, and restore is the only way back from a bad one.
            "ocut backup", "ocut restore",
        ):
            assert cmd in text, f"{name} does not document: {cmd}"
    assert "`--limit" in _text()


def test_readme_documents_the_dashboard_panels():
    text = _text()
    # The KPI labels are what `_put_panel` writes; a rename there must fail here.
    for label in ("Tool calls", "Skills", "Agents", "MCP", "Plugins"):
        assert label in text, f"Dashboard KPI not documented: {label}"
    # The token, cache and runtime panels.
    for label in ("Requests", "Input", "Output", "API Total", "Usage Total",
                  "Cache hit", "Cache miss", "Cache write", "Hit rate",
                  "Completed", "Incomplete", "Avg tool time", "Sessions",
                  "Projects"):
        assert label in text, f"Dashboard panel not documented: {label}"


def test_readmes_list_every_tab_page():
    """A reader must find exactly the tabs that exist — no more, no fewer.

    The list is compared *item by item, in order*, against what the app composes —
    so a page that exists but is undocumented fails, and so does a page the docs
    invented. Reading the ids out of the source instead of launching Textual keeps
    this runnable on an interpreter with no textual installed.
    """
    src = (ROOT / "scripts" / "ocut-tui.py").read_text(encoding="utf-8")
    panes = re.findall(r'with TabPane\("([^"]+)", id="(tab-[^"]+)"\)', src)
    assert panes, "no TabPane found — the compose() shape changed, fix this test"
    labels = [label for label, _ in panes]
    assert [tid for _, tid in panes][-1] == "tab-usage", panes

    en = (ROOT / "README.md").read_text(encoding="utf-8")
    zh = (ROOT / "README.zh-CN.md").read_text(encoding="utf-8")

    def listed(text, marker):
        segment = text[text.index(marker) + len(marker):]
        for bracket in ("(", "（"):                 # the sentence ends at its own note
            at = segment.find(bracket)
            if at != -1:
                segment = segment[:at]
        return [item.strip().strip("*").strip() for item in segment.split("/")]

    for label, items in (("README.md", listed(en, "Pages: ")),
                         ("README.zh-CN.md", listed(zh, "页面："))):
        assert items == labels, f"{label} page list {items} != the app's {labels}"


def test_readme_documents_top_limit_alias():
    text = _text()
    assert "--limit 3" in text or "`--limit" in text


# --- S8 doc gates -----------------------------------------------------------
def test_the_documented_limitation_range_is_the_range_the_gate_pins():
    """Every `M1–M12` is a claim about how many limitations there are.

    Four documents make that claim in two languages and none of them is the list
    itself, so each one quietly froze at whatever the newest id was when it was
    written. Reading the number out of `LIMITATIONS` — the tuple that decides which
    ids both READMEs must carry — turns all four into a copy of one fact.
    """
    first, last = LIMITATIONS[0], LIMITATIONS[-1]
    pattern = re.compile(first + r"(?:–|-| through )(M\d+)")
    seen = 0
    for name in ("README.md", "README.zh-CN.md", "MAINTENANCE.md",
                 "MAINTENANCE.zh-CN.md"):
        text = (ROOT / name).read_text(encoding="utf-8")
        for match in pattern.finditer(text):
            seen += 1
            assert match.group(1) == last, (
                f"{name} says {match.group(0)!r} but the gate pins {first}–{last}")
    assert seen >= 3, f"only {seen} range claims found — the wording changed"


def test_readmes_document_every_key_the_screen_binds():
    """The README tables are the human copy of the promise the app binds.

    A key that reaches neither is a key nobody finds. Read out of the source, so
    this runs on an interpreter with no textual, like the tab-page gate.
    """
    src = (ROOT / "scripts" / "ocut-tui.py").read_text(encoding="utf-8")
    app = src.split("class TrackerApp(", 1)[1]
    block = app.split("BINDINGS = [", 1)[1].split("]", 1)[0]
    keys = re.findall(r'Binding\("([^"]+)"', block)
    assert len(keys) >= 5, f"the binding list shrank to {keys}"

    for name, marker in (("README.md", "| Key"), ("README.zh-CN.md", "| 按键")):
        text = (ROOT / name).read_text(encoding="utf-8")
        rows = []
        for line in text[text.index(marker):].splitlines():
            if not line.strip().startswith("|"):
                break
            rows.append(line)
        table = " ".join(rows).lower()
        for key in keys:
            assert f"`{key.lower()}`" in table, (
                f"{name}'s key table does not document `{key}` "
                f"(bound in TrackerApp.BINDINGS)")


def test_readme_does_not_claim_message_text_is_stored():
    """No message text is stored, and the docs must not advertise storing it."""
    for name in ("README.md", "README.zh-CN.md"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert "branch / summary" not in text, f"{name} still lists summary in metadata"
        assert "message body" in text or "消息正文" in text, (
            f"{name} should state that message text is never stored"
        )


def test_docs_cross_link_so_no_file_is_an_orphan():
    """A checklist nobody links to is a checklist nobody reads.

    Regression: MAINTENANCE.md / .zh-CN.md were added with the English README
    never mentioning them, and the Chinese one only citing them in passing.
    """
    for name in ("README.md", "README.zh-CN.md"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert "MAINTENANCE.md" in text, f"{name} never points at the checklist"
    for path in MAINTENANCE:
        text = path.read_text(encoding="utf-8")
        assert "README.md" in text and "README.zh-CN.md" in text, (
            f"{path.name} does not point back at the documentation"
        )


# The three files that answer "what does this depend on, and how do I re-check it".
# They have no Chinese twin — the parity gates above cover README ×2 and
# MAINTENANCE ×2 — so instead of a translation they owe two things: a link from
# both checklists, and a command beside every reading they state.
MAINT_DOCS = ROOT / "docs" / "maintenance"
TRIO = ("compatibility.md", "opencode-interface.md", "measurements.md")


def test_the_maintenance_docs_are_linked_from_both_checklists():
    """A reference nobody links to is a reference nobody finds at the right moment.

    The failure this prevents is the ordinary one: the trio exists, gets written,
    and then a maintainer edits MAINTENANCE.md for a month without ever being told
    there is a file that says which release each claim belongs to.
    """
    for name in TRIO:
        assert (MAINT_DOCS / name).is_file(), f"missing docs/maintenance/{name}"
    for path in MAINTENANCE:
        text = path.read_text(encoding="utf-8")
        for name in TRIO:
            assert name in text, f"{path.name} never points at docs/maintenance/{name}"
    # Back the other way, so none of the three is a leaf that strands a reader.
    for name in TRIO:
        text = (MAINT_DOCS / name).read_text(encoding="utf-8")
        assert "MAINTENANCE" in text, f"{name} never names the checklist to return to"


def test_every_dated_reading_in_measurements_sits_under_a_command():
    """The whole reason `measurements.md` exists: a number with no command is a claim.

    Scoped to a line that *starts* with a date, because prose legitimately says
    "run on 2026-10-09" while describing the file. A reading line is the one that
    reports output, and output must be re-takable from the same section.
    """
    text = (MAINT_DOCS / "measurements.md").read_text(encoding="utf-8")
    sections = re.split(r"\n(?=## )", text)
    silent = []
    for section in sections:
        heading = section.splitlines()[0]
        if not re.search(r"^20\d\d-\d\d-\d\d:", section, re.M):
            continue
        if "```bash" not in section:
            silent.append(heading)
    assert not silent, (
        f"sections state a dated reading with no command to re-take it: {silent}"
    )


def test_measurements_quotes_the_cap_the_code_actually_resolves():
    """A cap printed in prose is a claim about a default; the default is in code.

    Repointed from `skillt`'s `settings` module (deleted) to `ocut_db`, whose
    resolved `name_cap` is the widest cell a table renders. The spirit is unchanged:
    the number in the document is read out of the code, not restated by hand, so a
    value that moves and a document that follows it do not go red for the wrong
    reason.
    """
    cap = db.DEFAULTS["name_cap"]
    text = (MAINT_DOCS / "measurements.md").read_text(encoding="utf-8")
    assert f"{cap:,}" in text, (
        f"measurements.md must print the resolved name cap ({cap:,}) where it "
        f"reports the clipping reading"
    )
    assert f"{cap} columns" in text, (
        "and the human spelling of the same number, derived from the same value"
    )


# `test_systemd_units_exist_and_use_absolute_execstart` was deleted with the
# artifact it guarded: `ocut` ships **no** systemd unit any more. Backups are
# manual and pre-destructive only (README M12), so there is no ExecStart, no
# OnCalendar and no timer file left to pin. The `SYSTEMD` constant went with it.


# --- source hygiene --------------------------------------------------------
# `skill-tracker` was dropped from this tuple when that directory was deleted;
# the surviving source trees are the ones that ship.
SOURCE_DIRS = ("scripts", "plugin", "bin")
SOURCE_SUFFIXES = {".py", ".js", ".mjs", ".cjs", ".sh", ".service", ".timer"}
# A red-check edits the thing it pins and then puts it back. A marker that
# survives is not a comment, it is the guard's own off switch left installed: one
# did ride through 318 passes on 2026-10-01 (a `return` after a fixture's setenv
# — inert enough to stay green, and a lie about which guard was being provoked).
# Written as a class instead of literals because this file is itself scanned: a
# guard that trips on its own needle is a guard nobody can read.
CANARY_RE = re.compile(r"TEMP[-_]|CAN[0-9A-Z]ARY|FIXME-REMOV[E]")


def test_no_canary_or_scratch_marker_survives_in_tracked_source():
    """Docs may *name* these markers; source may not hold one."""
    offenders = []
    for directory in SOURCE_DIRS:
        base = ROOT / directory
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file() or path.suffix not in SOURCE_SUFFIXES:
                continue
            if "__pycache__" in path.parts:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            hit = CANARY_RE.search(text)
            if hit:
                line = text[: hit.start()].count("\n") + 1
                offenders.append(f"{path.relative_to(ROOT)}:{line}: {hit.group()}")
    assert not offenders, f"red-check canaries left in source: {offenders}"


# --- the invariants table's own commands -----------------------------------
INV_ROW = re.compile(r"^\| (?P<promise>[^|]+)\| (?P<breaks>[^|]+)\| (?P<checks>[^|]+)\| (?P<run>[^|]+)\|$")


def _invariant_rows(text: str):
    """(promise, checks cell, command cell) for each pytest row in a table."""
    for line in text.splitlines():
        m = INV_ROW.match(line)
        if m and "python3 -m pytest" in m["run"]:
            yield m["promise"].strip(), m["checks"].strip(), m["run"].strip().strip("`")


def _suite_files() -> set:
    """The test files that actually exist, by name."""
    return {f.name for f in (ROOT / "scripts" / "tests").glob("test_*.py")}


def _suite_functions() -> set:
    """Every `def test_*` name defined anywhere in the suite."""
    names = set()
    for f in sorted((ROOT / "scripts" / "tests").glob("test_*.py")):
        names.update(re.findall(r"^def (test_\w+)", f.read_text(encoding="utf-8"), re.M))
    return names


def _invariant_row_mismatches(text: str) -> list:
    """Rows whose documented command cannot select a check the same row names.

    Static rather than a nested `--collect-only` for speed, which is legal only while
    every selector here is a plain `or` list of substrings — `-k` matches a needle
    against the test id, so "needle in name" *is* "collected". The assertion below
    refuses any richer expression instead of quietly stop meaning anything, and says
    what to do about it.
    """
    problems = []
    for promise, checks, cmd in _invariant_rows(text):
        # Existence, not just selector arithmetic. A row can cite a check by name and
        # a command can point at a file, and both can outlive what they name: the
        # suite stayed green while three of the files this table runs had already been
        # deleted. Two holes made that possible — the `if not names: continue` below
        # skipped every row that cited files instead of functions, and a `-k` needle
        # happily "matched" the id of a test that no longer exists. Both are closed.
        for cited in re.findall(r"`(test_[a-z0-9_]+\.py)`", checks):
            if cited not in _suite_files():
                problems.append((promise, cited, cmd, "names a test file that does not exist"))
        for cited in re.findall(r"`(test_[a-z0-9_]+)`", checks):
            if cited not in _suite_functions():
                problems.append((promise, cited, cmd, "names a check the suite no longer defines"))
        names = re.findall(r"`(test_[a-z0-9_]+)`", checks)
        if not names:
            continue                      # the row names whole files; nothing to match
        selector = re.search(r'-k\s+"([^"]*)"', cmd)
        if selector is None:
            scope = _suite_test_names(cmd)
            problems.extend(
                (promise, name, cmd, "not defined in the paths the row runs")
                for name in names if name not in scope)
            continue
        expr = selector.group(1)
        assert re.fullmatch(r"[A-Za-z0-9_]+(?: or [A-Za-z0-9_]+)*", expr), (
            f"{promise}: its -k expression is no longer a plain or-list of "
            "substrings, so this gate can no longer reason about it. Update the "
            "gate to the new grammar — do not loosen it.")
        needles = expr.split(" or ")
        problems.extend(
            (promise, name, expr, "no -k needle matches this test id")
            for name in names
            if not any(needle in name for needle in needles))
    return problems


def test_every_invariant_rows_command_selects_the_checks_it_names():
    """A row that names five checks and runs four of them is a row that lies.

    Found by hand twice in one sitting: a `-k "timer or scheduled"` needle did not
    select a check the row named, and a `key_help` needle did not select the
    binding-help check. Both read as documentation, which is exactly why nobody
    re-ran them: a maintenance engineer who runs the row's own command gets a green
    that omits the guard.
    """
    text = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert list(_invariant_rows(text)), "AGENTS.md's table has no pytest row to check"
    problems = _invariant_row_mismatches(text)
    assert not problems, "invariant rows whose command skips a check they name:\n" + "\n".join(
        f"  {p}: {n} — {sel} — {why}" for p, n, sel, why in problems)


def _suite_test_names(cmd: str) -> set:
    """Every test function defined under the paths a row's command points at."""
    names = set()
    for token in cmd.split():
        if token.startswith("-") or "=" in token:
            continue
        path = (ROOT / token).resolve()
        if path.is_file():
            candidates = [path]
        elif path.is_dir():
            candidates = sorted(path.rglob("test_*.py"))
        else:
            continue
        for candidate in candidates:
            names.update(re.findall(r"^def (test_\w+)", candidate.read_text(encoding="utf-8"),
                                    re.M))
    return names


def test_an_invariant_row_that_names_a_missing_check_is_caught():
    """The gate above must be capable of failing, on the real shape of the bug.

    Provoked on a **string**, not on the file: editing AGENTS.md in place would leave
    a tracked document mutilated for the length of a pytest run, which is a bad deal
    if the run is interrupted and a worse one if another agent is editing the same
    table. The mutated text goes straight to the checker, so the evidence is the same
    and nothing is ever written.
    """
    text = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert not _invariant_row_mismatches(text), "the control: the real table is clean"
    rows = list(_invariant_rows(text))
    assert rows, "the table has no pytest rows to provoke"

    # (a) a row that runs files names a check that is not defined anywhere in them
    promise, checks, cmd = rows[-1]
    assert "-k" not in cmd, f"the last row now carries a selector: {cmd}"
    edited = text.replace(checks, checks + ", `test_a_check_nobody_wrote`")
    assert edited != text, "could not inject into the row"
    found = _invariant_row_mismatches(edited)
    assert any(n == "test_a_check_nobody_wrote" for _, n, _, _ in found), (promise, found)

    # (c) the deleted-file shape this gate used to miss: a row that cites a test file
    # which no longer exists. The control above proves the real table is clean; this
    # proves the gate can see the difference.
    file_row = next((r for r in rows if re.search(r"`test_[a-z0-9_]+\.py`", r[1])), None)
    assert file_row, "no invariant row cites a test file, so the gate above is unprovable"
    cited = re.findall(r"`(test_[a-z0-9_]+\.py)`", file_row[1])[0]
    edited = text.replace(f"`{cited}`", "`test_a_file_nobody_deleted_back.py`")
    assert edited != text, "could not inject the missing-file citation"
    found = _invariant_row_mismatches(edited)
    assert any("does not exist" in why for _, _, _, why in found), \
        "a row citing a deleted test file slipped through"

    # (b) a selector whose first needle cannot match the ids the row claims
    t_promise, _, t_cmd = next(r for r in rows if "-k" in r[2])
    needle = re.search(r'-k\s+"([^"]*)"', t_cmd).group(1).split(" or ")[0]
    broken_cmd = t_cmd.replace(f'"{needle} or', '"zzzcannotmatch or', 1)
    assert broken_cmd != t_cmd, "could not rewrite the selector"
    found = _invariant_row_mismatches(text.replace(t_cmd, broken_cmd))
    assert any(p == t_promise and "zzzcannotmatch" in sel for p, _, sel, _ in found), found
