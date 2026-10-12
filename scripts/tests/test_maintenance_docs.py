"""test_maintenance_docs.py — the maintenance documents cannot drift from the code.

`docs/maintenance/compat-matrix.md` is the version matrix: among its pins it states which
interpreter CI runs. It said **3.12** while `.github/workflows/ci.yml` had become a `3.11` +
`3.14` matrix and `AGENTS.md` §CI described the same two ends — so the document announced an
interpreter CI does not run, in the one file a reader is sent to for "what does CI prove". All
the while the suite was green and CI reported success. A description of a check that no longer
describes the check is the same failure as `schema_version`: it was true once and nobody re-read
it. This class makes re-reading mandatory.

Scope, stated honestly: this holds the **stated CI interpreter** to the workflow. It does not
re-check the OpenCode or Textual pins in the same table — those name a release, not a workflow
input, and each is already covered by the guard named beside it in the matrix.

Standard library only. Nothing in the repository is written by these tests.
"""

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

ROOT = Path(__file__).resolve().parents[2]


class CiMatrixIsDocumentedAsItRuns(unittest.TestCase):
    WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
    # The documents a reader is sent to for "what does CI run". A version named in either must
    # be the set the workflow actually runs.
    DOCS = ("AGENTS.md", "docs/maintenance/compat-matrix.md")

    MATRIX_RE = re.compile(r'^\s*python-version:\s*\[([^\]]*)\]', re.M)
    VERSION_RE = re.compile(r'\b3\.\d+\b')
    # A line only makes a claim about CI when it names the workflow file or says what CI runs /
    # is / matrixes / uses as its interpreter. Prose about the local venv is somebody else's
    # fact and must not be swept into this comparison.
    CLAIM_RE = re.compile(r'ci\.yml|CI (runs|is|matrix|section|interpreter)')

    def setUp(self):
        text = self.WORKFLOW.read_text(encoding="utf-8")
        lists = self.MATRIX_RE.findall(text)
        self.assertTrue(lists, f"{self.WORKFLOW} has no python-version list to compare against")
        self.matrix = {v.strip().strip("\"'") for chunk in lists for v in chunk.split(",")}

    def offending(self, text):
        """Lines that state a CI version, with the versions they state."""
        out = []
        for number, line in enumerate(text.splitlines(), 1):
            if not self.CLAIM_RE.search(line):
                continue
            versions = set(self.VERSION_RE.findall(line))
            if versions and versions != self.matrix:
                out.append((number, sorted(versions), line.strip()[:72]))
        return out

    def test_the_workflow_states_the_versions_it_runs(self):
        self.assertTrue(self.matrix, "empty matrix: the guard would pass every document")
        for version in self.matrix:
            self.assertRegex(version, r"^3\.\d+$",
                             f"unexpected token in the CI matrix: {version!r}")

    def test_every_ci_claim_in_the_docs_states_the_real_matrix(self):
        wrong = {}
        for name in self.DOCS:
            found = self.offending((ROOT / name).read_text(encoding="utf-8"))
            if found:
                wrong[name] = found
        self.assertEqual(
            wrong, {},
            f"CI runs {sorted(self.matrix)}, but these lines say otherwise: {wrong}")

    def test_the_docs_really_do_make_ci_claims(self):
        """Positive control. Both checks above would pass on documents that never mention CI at
        all — the same emptiness that makes a scan over no fields a free pass. So the scan's
        coverage is asserted, not assumed."""
        lines = []
        for name in self.DOCS:
            lines += [(name, number, line) for number, line
                      in enumerate((ROOT / name).read_text(encoding="utf-8").splitlines(), 1)
                      if self.CLAIM_RE.search(line) and self.VERSION_RE.search(line)]
        self.assertGreaterEqual(
            len(lines), 1,
            "no document states a CI version anywhere the check can see it, so it is unguarded")

    def test_a_stale_version_next_to_a_ci_claim_is_detected(self):
        """The failure mode being guarded against, reproduced on a synthetic document: the
        workflow moved on and the prose did not."""
        fake = ("| `.github/workflows/ci.yml` | 3.12 | the only interpreter CI runs |\n"
                "| `pyproject.toml` `requires-python` | `>=3.11` | the floor |\n")
        found = self.offending(fake)
        self.assertEqual(len(found), 1,
                         f"a document that still says 3.12 must be caught, got {found}")
        self.assertEqual(found[0][1], ["3.12"])
        # The second line states 3.11 but makes no CI claim: it is the packaging floor.
        self.assertNotIn(2, [hit[0] for hit in found])


if __name__ == "__main__":
    unittest.main()
