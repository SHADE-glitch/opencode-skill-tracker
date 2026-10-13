"""No production script may reach for the network, a process, or a socket.

The allowlist is per file because the four scripts are not the same shape:
`ocut-stats.py` and `ocut_db.py` are imported by tests and by each other and stay on
the standard library, `ocut-tui.py` additionally needs the two packages that paint the
screen, and `ocut-sync.py` reads a host database.

This is the one check that is about *imports* rather than about behaviour, and it is
here because the behaviour checks all had a gap of the same shape: the sentinel scan
proves no prose reaches a column, the no-write cases prove the host files are not
written, and nothing in either says the reader could not have phoned home on the way.
"Could" is the whole claim — nothing in this repository currently does, and the only
thing keeping that true is that adding the import is now a test failure.

Scope, stated the way AGENTS.md requires: this inspects *this repository's* four
scripts. A third-party package it imports has its own imports, which are not walked
here. For `rich` — the one new entry below — that was measured once rather than
assumed: in rich 15.0.0 no module imports `socket`, `ssl`, `http`, `urllib`,
`httpx`, `requests` or `subprocess`; the four hits for a network-adjacent pattern are
`threading` only (`console.py`, `live.py`, `layout.py`, `progress.py`), which is a
render lock. Re-measure before restating that; the command is in
`docs/maintenance/compatibility.md`.
"""

import ast
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent

PRODUCTION = ["ocut_db.py", "ocut-sync.py", "ocut-stats.py", "ocut-tui.py"]

# Non-stdlib roots each file may name, beyond the standard library. An empty tuple is
# a real constraint, not a placeholder: the three non-TUI scripts must not import a
# third-party package at all, so that `ocut stats` works on a bare interpreter.
ALLOWED_BY_FILE = {
    "ocut_db.py": (),
    "ocut-sync.py": (),
    "ocut-stats.py": (),
    # `textual` paints the screen; `rich.text.Text` is the renderable that dims one
    # cell of a removed row. Both are inside the `try: … except ImportError` block
    # that lets the module import without them, which is why an interpreter with
    # neither still fails with a message instead of a traceback.
    "ocut-tui.py": ("textual", "rich"),
}

# Named by the category they open, not by module: a socket and a TLS session are the
# same promise from the user's side, and a list of module names is how the next one
# gets in (httpx, websockets, smtplib, …).
#
# `threading` is deliberately absent from the list. It is not a leak path — the TUI's
# 30-second sync worker is a thread and the design depends on it — and a guard that
# refused it would be refused in order to be ignored. `multiprocessing` and
# `subprocess` stay, because those spawn a *second interpreter*, and a second
# interpreter is a second place every other guard would have to be repeated.
FORBIDDEN_ROOTS = {
    "network": ("socket", "ssl", "http", "urllib", "ftplib", "smtplib",
                "telnetlib", "xmlrpc", "requests", "httpx", "websockets",
                "aiohttp", "urllib3", "asyncio"),
    "process": ("subprocess", "multiprocessing", "pty", "shlex", "runpy"),
    "exit": ("atexit",),
}

# This repository's own modules, imported by name through `sys.path`. Not
# third-party, and not a hole: `ocut_db.py` is itself under this same sweep.
OUR_MODULES = ("ocut_db",)

ALLOWED_ANY = frozenset(n for a in ALLOWED_BY_FILE.values() for n in a)


def stdlib_roots() -> frozenset:
    """Top-level module names the running interpreter ships as standard library.

    Taken from `sys.stdlib_module_names` (3.10+) rather than from a hand-typed list,
    so a guard written on 3.14 does not call `tomllib` a third-party package because
    nobody remembered it.
    """
    return frozenset(getattr(sys, "stdlib_module_names", ()))


def imports_of(path: Path) -> list[tuple[str, int]]:
    """Every top-level module name a file imports, with the line it did so on.

    Walks the whole tree, so an import nested in the TUI's `try:` block is found the
    same way as one at the top — which is where both new entries live.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [(a.name.split(".")[0], node.lineno) for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:                      # relative: our own modules
                continue
            if node.module:
                found.append((node.module.split(".")[0], node.lineno))
    return found


def _imports_of_text(text: str) -> list[tuple[str, int]]:
    """The same walk as `imports_of`, on a string — so a case can hand it a mutation."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [(a.name.split(".")[0], node.lineno) for a in node.names]
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            found.append((node.module.split(".")[0], node.lineno))
    return found


def _attribute_calls(text: str) -> list[str]:
    """`os.system(...)`, `subprocess.run(...)` and friends, which an Import sweep misses.

    `import os` passes an import allowlist and is exactly how a shell-out gets in, so
    the attribute name has to be checked as well as the module. Only `os` is walked:
    every other forbidden module is refused at the import line above, so by the time
    there is a call to look at, it can only be on a module that is already in.
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    return [node.attr for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "os"
            and node.attr in ("system", "popen", "spawn", "spawnl", "spawnv",
                              "exec", "execl", "execlp", "execv", "execve",
                              "posix_spawn", "startfile")]


def offenders(text: str) -> list[str]:
    """Why a guard refuses this document, given as sentences a failure can print.

    A guard that only says "no" leaves the reader to guess which rule fired, and a
    guard that says "no" to three categories at once cannot be read either. Takes the
    source as a string rather than a path so a case can hand it mutated input --
    AGENTS.md's rule about provoking a guard with a string, not with the repository.
    """
    problems: list[str] = []
    stdlib = stdlib_roots()
    for name, line in _imports_of_text(text):
        where = f"{name} (line {line})"
        for category, roots in FORBIDDEN_ROOTS.items():
            if name in roots:
                problems.append(f"{where} is a {category} module")
        if name in stdlib or name in ALLOWED_ANY or name in OUR_MODULES:
            continue
        if name.startswith("_"):
            continue
        problems.append(f"{where} is not in this repository's dependency list "
                        f"({', '.join(sorted(ALLOWED_ANY)) or 'standard library only'})")
    for attr in _attribute_calls(text):
        problems.append(f"os.{attr}() is a process call hiding behind a passing import")
    return problems


def test_no_production_script_imports_the_network_a_process_or_a_socket():
    """One sweep over the four scripts, naming the file and line of each refusal."""
    refusals = []
    for filename in PRODUCTION:
        for problem in offenders((SCRIPTS / filename).read_text(encoding="utf-8")):
            refusals.append(f"{filename}: {problem}")
    assert refusals == [], "\n".join(refusals)


def test_the_three_non_tui_scripts_are_standard_library_only():
    """`ocut stats`, `ocut sync` and `ocut health` must run on a bare interpreter.

    Stated separately from the sweep above because it is a different promise: the
    sweep says "no network", this says "nothing third-party either", so a panel's
    data can be reported on a machine where the TUI's dependencies were never
    installed.
    """
    for filename in ("ocut_db.py", "ocut-sync.py", "ocut-stats.py"):
        third_party = {name for name, _ in imports_of(SCRIPTS / filename)
                       if name not in stdlib_roots() and name not in OUR_MODULES}
        assert third_party == set(), f"{filename} imports {sorted(third_party)}"


def test_the_tui_may_import_textual_and_rich_and_nothing_else():
    """`rich` is here because of the dimmed `removed` cell; it is pinned to a
    measured justification in the module docstring, not waved in."""
    third_party = {name for name, _ in imports_of(SCRIPTS / "ocut-tui.py")
                   if name not in stdlib_roots() and name not in OUR_MODULES}
    assert third_party <= {"textual", "rich"}, sorted(third_party)


def test_a_dependency_added_tonight_is_caught_without_touching_the_repository():
    """The provoke, as a case: feed the guard a line that breaks the rule and read
    the refusal. AGENTS.md requires a guard be provokable with a string rather than
    by editing a tracked file, and this is that shape for the allowlist."""
    assert offenders("import httpx\n") == [
        "httpx (line 1) is a network module",
        "httpx (line 1) is not in this repository's dependency list "
        "(rich, textual)"]
    assert offenders("import rich.text\n") == []
    assert offenders("import ocut_db\n") == []
    # `import os` is standard library, so the import sweep waves it through — which is
    # the shape a real shell-out takes. Checked from the same string.
    assert offenders("import os\nos.system('id')\n") == [
        "os.system() is a process call hiding behind a passing import"]
    assert offenders("import os\nos.path.join('a', 'b')\n") == []


def test_stdlib_and_third_party_are_not_told_apart_by_a_hand_typed_list():
    """The list above says which *categories* are refused. Which names are standard
    library is the interpreter's, so a name added to 3.13 is not reported here as a
    mystery third-party package on 3.11."""
    assert "tomllib" in stdlib_roots(), "the stdlib list is not being read"
    assert "textual" not in stdlib_roots()