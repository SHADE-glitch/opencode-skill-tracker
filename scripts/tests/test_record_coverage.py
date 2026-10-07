"""
test_record_coverage.py — the recording-coverage check for this repo.

Why a pytest file rather than scripts/check-log.mjs (the form the GNOME extensions use): this
repo's gate is `python3 -m pytest scripts/tests`, there is no package.json, and a check that is
not in the gate is a check that does not run.

Scope rule, stated here and in CHANGELOG.md's header: commits whose subject starts with `feat`
are **not** covered by this check. This is an original project with no upstream, so a feature is
the product itself, not a deviation that an upgrade may drop — the README documents features.
The exclusion is categorical (by commit class) and declared in source, not a per-commit skip
flag, so it cannot accumulate excuses one commit at a time.

Checks:
  0. the window must contain at least one covered commit and one entry (no vacuous green)
  1. the coverage anchor resolves
  2. D-### ids unique, strictly increasing, gapless
  3. every covered commit is cited; every cited hash resolves
  4. every D-### in any tracked .md resolves to an entry
  5. every entry has all five fields and an allowed kind
"""

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

CODE_PATHS = [
    "plugin/skill-tracker.js",
    "scripts/skill_db.py",
    "scripts/skill-tui.py",
    "scripts/skill-stats.py",
    "bin/skillt",
]
EXCLUDED_SUBJECT = re.compile(r"^feat(\([^)]*\))?!?:")
KINDS = {"fix", "perf", "taste", "guard", "revert", "chore"}
FIELDS = ["Symptom", "Change", "Evidence", "Cost", "Commit"]


def git(*args):
    return subprocess.run(["git", "-C", str(ROOT), *args],
                          capture_output=True, text=True, check=True).stdout


def changelog():
    p = ROOT / "CHANGELOG.md"
    assert p.exists(), f"CHANGELOG.md missing from {ROOT}"
    return p.read_text(encoding="utf-8")


def anchor_of(text):
    m = re.search(r"^Coverage:\s*([0-9a-zA-Z^~_/.-]+)\.\.HEAD\s*$", text, re.M)
    assert m, 'CHANGELOG.md needs a "Coverage: <anchor>..HEAD" line'
    return m.group(1)


def resolve(ref):
    try:
        return git("rev-parse", "--verify", f"{ref}^{{commit}}").strip()
    except subprocess.CalledProcessError:
        return None


def covered_commits(anchor):
    """Commits in the window that touched production code and are not excluded by class."""
    out = git("log", "--format=%H%x1f%s", f"{anchor}..HEAD", "--name-only", "--", *CODE_PATHS)
    result, sha, subject, touched = [], None, "", False

    def flush():
        if sha and touched and not EXCLUDED_SUBJECT.match(subject):
            result.append((sha, subject))

    for line in out.split("\n"):
        line = line.strip()
        if not line:
            continue
        if "\x1f" in line and re.fullmatch(r"[0-9a-f]{40}", line.split("\x1f", 1)[0]):
            flush()
            sha, subject = line.split("\x1f", 1)
            touched = False
        elif line in CODE_PATHS:
            touched = True
    flush()
    return result


def test_coverage_anchor_resolves():
    assert resolve(anchor_of(changelog())), "coverage anchor does not resolve"


def test_record_is_not_empty():
    text = changelog()
    covered = covered_commits(resolve(anchor_of(text)))
    entries = re.findall(r"^### (D-\d+) · ", text, re.M)
    assert covered, "the declared window contains no covered commit — widen it or state why not"
    assert entries, "the record has no entries: a check over an empty set proves nothing"


def test_ids_are_monotonic_and_gapless():
    numbers = [int(m) for m in re.findall(r"^### D-(\d+) · ", changelog(), re.M)]
    assert numbers == sorted(numbers), f"ids are not increasing: {numbers}"
    assert len(numbers) == len(set(numbers)), "an id number is reused"
    assert numbers == list(range(1, len(numbers) + 1)), "there is a gap: a deleted entry is a failure, not a cleanup"


def test_every_covered_commit_is_cited():
    text = changelog()
    cited = set()
    for refs in re.findall(r"^Commit\s+(.*)$", text, re.M):
        for ref in refs.split():
            sha = resolve(ref)
            assert sha, f"CHANGELOG cites a commit that does not exist: {ref}"
            cited.add(sha)
    missing = [(s[:7], subj) for s, subj in covered_commits(resolve(anchor_of(text))) if s not in cited]
    assert not missing, "uncovered production commits:\n" + "\n".join(f"  {s} {subj}" for s, subj in missing)


def test_kind_and_fields():
    text = changelog()
    blocks = re.split(r"^### ", text, flags=re.M)[1:]
    for block in blocks:
        head = block.split("\n", 1)[0]
        m = re.match(r"D-\d+ · \d{4}-\d{2}-\d{2} · ([a-z]+)(?: · .*)?$", head)
        assert m, f"malformed entry header: {head!r}"
        assert m.group(1) in KINDS, f"{head}: kind {m.group(1)!r} not in {sorted(KINDS)}"
        eid = head.split(" · ")[0]
        for field in FIELDS:
            assert re.search(rf"^{field}\s", block, re.M), f"{eid}: missing field {field}"


def test_references_from_other_documents_resolve():
    text = changelog()
    known = {int(n) for n in re.findall(r"^### D-(\d+) · ", text, re.M)}
    tracked = git("ls-files", "-z", "*.md").split("\0")
    for rel in [f for f in tracked if f and f != "CHANGELOG.md"]:
        body = (ROOT / rel).read_text(encoding="utf-8")
        for n in {int(m) for m in re.findall(r"\bD-(\d{3,})\b", body)}:
            assert n in known, f"{rel} references D-{n:03d}, which has no entry"
