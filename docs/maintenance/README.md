# Maintenance notes

Everything here exists because **OpenCode changing its host interface — or a pinned version
moving — is the main maintenance risk** for this tool: a renamed hook, event, config field or
tool id reads as a silent zero, the pages empty out, the writer keeps running, and CI stays
green.

| Document | Read it when |
|---|---|
| [`compatibility.md`](compatibility.md) | An OpenCode, Textual or Python version moved, or a pin needs re-verifying |
| [`opencode-interface.md`](opencode-interface.md) | Adding or renaming a host dependency — every name this project reads from OpenCode |
| [`measurements.md`](measurements.md) | Restating a size, a rate or a duration, or comparing against a dated baseline |

These three are English-only on purpose: they carry commands and readings, not prose a reader
needs in two languages. The bilingual pair of documents is `README.md` / `README.zh-CN.md` and
`MAINTENANCE.md` / `MAINTENANCE.zh-CN.md`.

Standing checks over this area — all part of `python3 -m pytest scripts/tests`:

- `test_maintenance_docs.py` — the CI interpreter these documents state is the one
  `.github/workflows/ci.yml` runs (the file a reader is sent to for "what does CI prove").
- `test_compat.py` — every host name `scripts/opencode_compat.py` declares is still present in
  the writer, so the declaration cannot drift into a second, wrong copy.
- `test_readme.py` — the README pair, its limitations and its status codes, and these three
  documents being linked from both checklists.
- `test_record_coverage.py` — the changelog records every covered production commit.

`MAINTENANCE.md` is the operating checklist, and its numbered sections (`§0`–`§11`) are cited
from `AGENTS.md`, the READMEs and the documents above — so a section number is a stable anchor
and a renumber is a rename. Do not duplicate those tables here; a duplicated fact is the thing
that drifts.
