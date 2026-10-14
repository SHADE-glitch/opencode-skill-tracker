"""test_conventions.py — the dimension-A layout layer cannot quietly rot.

The finalized layer at the root of this repository (STANDARD.md, outside this
repository, "Root files") is the one thing every other reader assumes. It is also
the one thing nothing enforces, because each of its entries is a *file name*: a
rename looks exactly like a deletion to every test that was written before it,
and `docs/` or `.github/workflows/` can vanish without a single suite noticing.

So the checks here are deliberately dull. They assert that the root layer is
present at the root, that this file's own AGENTS.md names the dimension in one
line instead of copying the standard's body, and that the runtime state the
ignore list covers is really ignored rather than merely commented about.

Two rules about how this file behaves, both from the standard it enforces:

* **Offline.** It reads the working tree and `git ls-files`. It never reaches the
  network and never reads the machine-wide standard, because a repository that
  cannot be checked offline is a repository that is only checked on one machine.
* **Nothing here is written.** A failing layout check says which entry is missing
  and where it belongs; it does not create the file.

It lives under `scripts/tests/`, so `pytest scripts/tests` and
`unittest discover -s scripts/tests` collect it with no change to either entry
command and no change to CI.

Standard library only.
"""

import re
import subprocess
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
if (HERE / "_hermetic.py").exists():
    # Tripwire, shared by every sibling test module: the suite must never open the
    # real host database. This file touches only the working tree and `git ls-files`,
    # but it is collected into the same run and installs the same guard.
    import _hermetic  # noqa: E402,F401

# The repository root, found by the one marker every clone has. Resolved this way
# so the same file works from scripts/tests/ and from tests/ (see this repository's
# AGENTS.md for why the test directory is a project-specific choice).
REPO = next(p for p in Path(__file__).resolve().parents if (p / ".git").exists())

# STANDARD.md "Root files (the finalized layer)", minus the LICENSE that only
# dimension A requires, and with the tool manifest rather than the plugin one.
ROOT_FILES = (
    "AGENTS.md",
    "README.md",
    "README.zh-CN.md",
    "CHANGELOG.md",
    "MAINTENANCE.md",
    "MAINTENANCE.zh-CN.md",
    "LICENSE",
    ".gitignore",
    "pyproject.toml",
)
ROOT_DIRS = ("docs", ".github/workflows")

# STANDARD.md "Runtime ignore list". A tool that drops one of these names starts
# writing its own database into `git status`. The trailing slash is optional in a
# pattern: `.venv` and `.venv/` ignore the same directory, so what is checked is
# the name, not the spelling.
RUNTIME_IGNORE = (
    ".venv",
    "venv",
    "__pycache__",
    "*.db",
    "*.db-wal",
    "*.db-shm",
    ".pytest_cache",
    "reports/",
)

# The heading strings of STANDARD.md and of the machine-wide CONVENTIONS.md,
# each in the numbered form both documents use ("## 5. CI", not "## CI") — an
# unnumbered word like "CI" is an ordinary section title here, and matching it
# would go red on a repository that is merely being thorough.
STANDARD_HEADINGS = (
    "Repositories governed by this standard",
    "Root files (the finalized layer)",
    "Language policy",
    "Process-draft location",
    "CHANGELOG format",
    "CI",
    "Test entry commands",
    "Runtime ignore list",
    "Conflict rule",
    "Remotes and pushing",
    "The three dimensions",
    "The shared common core",
    "What each dimension requires",
    "Unify the common core; never flatten",
    "Where a repository points",
)

# Files whose tracked contents would mean runtime state reached the repository.
RUNTIME_TRACKED = re.compile(
    r"(?:^|/)(?:__pycache__|node_modules)/"
    r"|\.py[co]$"
    r"|\.(?:db|db-wal|db-shm|sqlite|sqlite3|log)$"
    r"|\.venv/"
    r"|^reports/"
)

# Process drafts belong under the ignored docs/reports/, never at the root.
DRAFT_NAMES = ("STATE.md", "PLAN.md", "VERIFY.md", "AUDIT.md", "PROFILE.md", "REVIEW-PACK.md")


def tracked_files() -> list[str]:
    """Every path git has, forward-slashed, relative to the repository root."""
    out = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.splitlines()


class RootLayerTest(unittest.TestCase):
    def test_the_root_layer_is_present_at_the_root(self):
        """The layer this repository's README and CI assume exists."""
        missing = [f"{name}: create this file at the repository root"
                   for name in ROOT_FILES if not (REPO / name).exists()]
        missing += [f"{name}/: create this directory at the repository root"
                    for name in ROOT_DIRS if not (REPO / name).is_dir()]
        self.assertEqual([], missing, "the dimension-A root layer is incomplete:\n  "
                         + "\n  ".join(missing))

    def test_pyproject_toml_is_the_only_manifest(self):
        """STANDARD.md allows one manifest, and for a tool it is pyproject.toml.

        A second manifest would be a second place where the floor is written
        down, and the two would drift.
        """
        extra = sorted(p.name for p in REPO.glob("*.json")
                       if p.name in {"package.json", "Cargo.toml", "go.mod"})
        self.assertEqual([], extra,
                         f"this is a dimension-A tool: remove the {extra} from the root, "
                         "or record the exception in AGENTS.md")

    def test_agents_zh_cn_does_not_exist(self):
        """The bilingual AGENTS pair belongs to dimension B, not to this one."""
        self.assertFalse((REPO / "AGENTS.zh-CN.md").exists(),
                         "dimension A keeps one AGENTS.md; AGENTS.zh-CN.md is "
                         "dimension B's shape — delete it and fold any rule it "
                         "holds into AGENTS.md")


def copied_standard_headings(text: str) -> list[str]:
    """Section headings in `text` that are the standard's own, numbered form only.

    A repository's own `## CI` or `## Conventions` is an ordinary section title.
    A line that reproduces the standard's `## 5. CI` — heading marks, section
    number, and the standard's exact title — is a copy of it.
    """
    found = set()
    for raw in text.splitlines():
        line = re.sub(r"^\s*>\s*", "", raw)
        if not line.startswith("#"):
            continue
        m = re.fullmatch(r"(\d+)\.\s*(.+)", re.sub(r"^#+\s*", "", line))
        if m and m.group(2).strip() in STANDARD_HEADINGS:
            found.add(m.group(2).strip())
    return sorted(found)


class AgentsPointerTest(unittest.TestCase):
    def setUp(self):
        self.text = (REPO / "AGENTS.md").read_text(encoding="utf-8")

    def test_agents_md_names_its_dimension_in_one_line(self):
        """CONVENTIONS.md "Where a repository points": one line, then a pointer."""
        self.assertRegex(self.text, r"(?m)^\s*>\s*\*\*Dimension\.\*\*",
                         "AGENTS.md has no dimension line — add, directly above the "
                         "'Shared standard' blockquote, one blockquote line naming "
                         "this repository's dimension in CONVENTIONS.md (outside "
                         "this repository) and pointing at STANDARD.md")

    def test_agents_md_points_at_conventions_md(self):
        """The line must name where the standard lives, not just a dimension."""
        self.assertIn("CONVENTIONS.md", self.text,
                      "the dimension line must point at CONVENTIONS.md so a reader "
                      "can find the other dimensions")

    def test_agents_md_does_not_copy_the_standard(self):
        """STANDARD.md is the single source; a copied heading means a second copy."""
        quoted = copied_standard_headings(self.text)
        self.assertEqual([], quoted,
                         "AGENTS.md quotes the standard's own section heading(s) "
                         f"{quoted}: replace them with a clause name (the standard's "
                         "'Runtime ignore list', not 'Standard §4') so there is "
                         "exactly one copy to keep correct")


class RuntimeStateTest(unittest.TestCase):
    def test_gitignore_carries_the_runtime_ignore_list(self):
        """The list is only useful if the patterns are in the file."""
        text = (REPO / ".gitignore").read_text(encoding="utf-8")
        patterns = {line.strip().rstrip("/") for line in text.splitlines()
                    if line.strip() and not line.strip().startswith("#")}
        missing = [p for p in RUNTIME_IGNORE if p.rstrip("/") not in patterns]
        self.assertEqual([], missing,
                         f".gitignore is missing {missing} — add them under a "
                         "comment saying these are runtime data, never source")

    def test_no_runtime_state_is_tracked(self):
        """The real check: the ignore rules and the index have to agree."""
        tracked = sorted(p for p in tracked_files() if RUNTIME_TRACKED.search(p))
        self.assertEqual([], tracked,
                         "runtime state is tracked (git rm --cached the entry, then "
                         f"keep it out): {tracked}")

    def test_no_process_draft_is_tracked_anywhere(self):
        """Process drafts live under the ignored docs/reports/, so none is tracked."""
        drafts = sorted(p for p in tracked_files()
                        if p.rsplit("/", 1)[-1] in DRAFT_NAMES)
        self.assertEqual([], drafts,
                         f"a process draft is tracked at the root: {drafts} — move it "
                         "to docs/reports/, which .gitignore covers")


if __name__ == "__main__":
    unittest.main()