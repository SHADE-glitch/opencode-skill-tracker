"""The AgentOS advisor store — a read-only neighbour.

Split out of `skill_db.py` so the module that owns the tracker's own schema is not
also the module that knows another project's column names. Nothing here writes:
the advisor owns its store, the tracker owns the usage rows, and neither has to
know the other's schema to be watched.

Privacy is by construction, not by scrubbing — see the whitelist and the
`_project_loop` / `_count_only` projections below, and the tests in
`scripts/tests/test_agentos.py`. Import direction is one-way: this module imports
`skill_db`, never the reverse.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime

import skill_db as db

# The advisory plugin (AgentOS) records its own work in its own SQLite store
# plus a `loops/` directory of per-session JSON. This aggregator *reads* them
# and never writes: the advisor owns that store, and the tracker owns the usage
# rows, so neither side has to know the other's schema to be watched.
#
# Privacy is by construction, not by scrubbing: a loop file carries `task_text`
# (the user's task verbatim) and stage `data` carries engine payloads, so the
# projection below names every field it emits and simply never reads the rest.
# A new field cannot leak by being added upstream; it has to be added here.

# Read at call time, never at import: a module-level default froze this
# machine's environment into the signature (the same trap scan_skills fell into).
AGENTOS_DB_ENV = "OPENCODE_SKILL_TRACKER_AGENTOS_DB"

# Stage names the advisor's loop goes through, in order. Unknown names are still
# reported (a new stage should not be silently dropped), this only fixes order.
AGENTOS_STAGES = (
    "route", "resolve_role", "recall", "plan", "execute",
    "evidence", "validate", "record", "evolve", "finalize",
)

# Loop fields projected as-is. Anything not listed is never read.
AGENTOS_LOOP_FIELDS = ("loop_id", "session_id", "stage", "final_status",
                       "created_at", "updated_at", "memory_mode", "provider", "model")


def agentos_store(db_path: str | None = None) -> dict:
    """Locate the advisor store. Never creates anything.

    Resolution order, all read at call time (a def-time default would freeze the
    developer's machine into the signature): explicit argument, then
    `OPENCODE_SKILL_TRACKER_AGENTOS_DB`, then `$AGENT_OS_ROOT/store/aos.db`.
    """
    path = db_path or os.environ.get(AGENTOS_DB_ENV) or os.environ.get("AOS_DB")
    if not path:
        root = os.environ.get("AGENT_OS_ROOT")
        if root:
            path = os.path.join(root, "store", "aos.db")
    result = {"requested": path, "db": None, "loops": None, "reason": ""}
    if not path:
        result["reason"] = "no store configured (set AGENT_OS_ROOT or OPENCODE_SKILL_TRACKER_AGENTOS_DB)"
        return result
    if not os.path.isfile(path):
        result["reason"] = f"no database at {path}"
        return result
    loops = os.path.join(os.path.dirname(path), "loops")
    result["db"] = path
    result["loops"] = loops if os.path.isdir(loops) else None
    return result


def _open_agentos_ro(path: str):
    """Read-only, and explicitly *not* skill_db.open_db(): that helper falls
    back to a read-write open and would happily create an empty store."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={db.BUSY_TIMEOUT_MS}")
    return conn


def _ts_diff_ms(start, end) -> int | None:
    """Milliseconds between two advisor timestamps; None when unusable.

    The advisor writes `+00:00` offsets; `Z` is tolerated because the tracker's
    own stamps use it and a reader cannot assume which writer produced a row.
    """
    def parse(value):
        text = str(value or "").strip()
        if not text:
            return None
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    a, b = parse(start), parse(end)
    if a is None or b is None:
        return None
    return int((b - a).total_seconds() * 1000)


def agentos_usage_by_session(conn, session_ids) -> dict:
    """How much measured tool activity each advisor session saw.

    The join that makes the two stores worth reading together: an advisor loop
    with zero usage rows means the model worked without touching anything the
    tracker measures, which is a different story from a loop that never ran.
    """
    ids = sorted({s for s in session_ids if s})
    if not ids:
        return {}
    out = {s: {"skill": 0, "mcp": 0, "plugin": 0} for s in ids}
    marks = ",".join("?" * len(ids))
    for table, stream in (
        ("skill_usage", "skill"), ("mcp_usage", "mcp"), ("plugin_usage", "plugin"),
    ):
        try:
            rows = conn.execute(
                f"SELECT session_id, COUNT(*) AS n FROM {table}"
                f" WHERE session_id IN ({marks}) GROUP BY session_id",
                ids,
            ).fetchall()
        except sqlite3.Error:
            continue  # an unmigrated DB simply has no such stream yet
        for r in rows:
            if r["session_id"] in out:
                out[r["session_id"]][stream] = r["n"]
    return out


# The advisor's recall stage reports its own result as integers and id lists.
# The same dict also holds `query` — a nested object of text derived from the
# user's task — so only these names are read, and only ever as counts.
AGENTOS_INJECTION_NAMES = ("retrieved", "memory_ids", "injected_memory_ids",
                          "injection_chars")


def _count_only(value):
    """A count from an int or a sized collection; anything else is None.

    Text never becomes a number here, which is what keeps `query` unreadable by
    construction rather than by remembering to filter it out later.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, (list, dict)):
        return len(value)
    return None


def _project_injection(raw: dict) -> dict | None:
    recall = ((raw.get("stages") or {}).get("recall") or {}).get("data")
    if not isinstance(recall, dict):
        return None
    return {
        "retrieved": _count_only(recall.get("retrieved")),
        # `memory_ids` is what recall returned; `injected_memory_ids` is what
        # actually went into the prompt. They differ (a hypothesis can be
        # injected on its own), so they are kept apart rather than averaged.
        "recalled": _count_only(recall.get("memory_ids")),
        "injected": _count_only(recall.get("injected_memory_ids")),
        "chars": _count_only(recall.get("injection_chars")),
    }


def _project_loop(raw: dict, limit_ms: int) -> dict:
    """Whitelist projection of one loop file. See the note above: no `**raw`."""
    stages = {}
    slowest = None
    for name, st in (raw.get("stages") or {}).items():
        if not isinstance(st, dict):
            continue
        ms = _ts_diff_ms(st.get("started_at"), st.get("completed_at"))
        entry = {
            "status": str(st.get("status") or ""),
            "ms": ms,
            "failed": bool(st.get("error")),
        }
        stages[name] = entry
        if ms is not None and (slowest is None or ms > slowest[0]):
            slowest = (ms, name)
    order = [s for s in AGENTOS_STAGES if s in stages]
    order += [s for s in stages if s not in AGENTOS_STAGES]
    pf = raw.get("postflight")
    post = {}
    if isinstance(pf, dict):
        learning = pf.get("learning")
        recovery = pf.get("recovery")
        post = {
            "aos_status": pf.get("aos_status"),
            "phase": pf.get("phase"),
            "final_status": pf.get("final_status"),
            "warnings": len(pf.get("warnings") or []),
            "has_error": bool(pf.get("aos_error")),
            "candidates_recorded": (learning or {}).get("candidates_recorded") if isinstance(learning, dict) else None,
            "needs_review": (learning or {}).get("needs_review") if isinstance(learning, dict) else None,
            "recovery_attempted": (recovery or {}).get("recovery_attempted") if isinstance(recovery, dict) else None,
        }
    return {
        **{k: raw.get(k) for k in AGENTOS_LOOP_FIELDS},
        "errors": len(raw.get("errors") or []),
        "stages": {name: stages[name] for name in order},
        "stage_count": len(stages),
        "slowest_stage": {"name": slowest[1], "ms": slowest[0]} if slowest else None,
        "over_budget_ms": bool(slowest and slowest[0] > limit_ms),
        "injection": _project_injection(raw),
        "postflight": post or None,
    }


def agentos_summary(conn, limit: int = 10, db_path: str | None = None,
                    timeout_ms: int | None = None) -> dict:
    """Read-only digest of the advisor store, joined with the tracker's sessions.

    `timeout_ms` is the advisor's per-call budget: a stage slower than that never
    made it into the prompt, so "over budget" is not "took a while". The default
    is AgentOS's own AOS_TIMEOUT_MS default, which this module cannot read from
    the other side — override it with
    OPENCODE_SKILL_TRACKER_AOS_TIMEOUT_MS when that default changes (M21).
    """
    if timeout_ms is None:
        raw = os.environ.get("OPENCODE_SKILL_TRACKER_AOS_TIMEOUT_MS")
        try:
            timeout_ms = int(raw) if raw else 1200
        except ValueError:
            timeout_ms = 1200
    store = agentos_store(db_path)
    out = {
        "available": False,
        "db_path": store["db"],
        "loops_dir": store["loops"],
        "reason": store["reason"],
        "timeout_ms": timeout_ms,
        "telemetry": [],
        "retrieval": {},
        "store_counts": {},
        "loops": [],
        "loops_total": 0,
    }
    if not store["db"]:
        return out

    try:
        ac = _open_agentos_ro(store["db"])
    except sqlite3.Error as e:
        out["reason"] = f"cannot open read-only: {e}"
        return out
    try:
        for table in ("telemetry_events", "retrieval_log", "memories",
                      "observations", "candidates", "learning_reviews"):
            try:
                out["store_counts"][table] = ac.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0]
            except sqlite3.Error:
                out["store_counts"][table] = None
        try:
            out["telemetry"] = db.rows_to_dicts(ac.execute(
                "SELECT event_type, COUNT(*) AS n, MAX(created_at) AS last "
                "FROM telemetry_events GROUP BY event_type ORDER BY n DESC"
            ))
        except sqlite3.Error:
            pass
        try:
            row = ac.execute(
                "SELECT COUNT(*) AS n, COUNT(DISTINCT memory_id) AS memories, "
                "MAX(created_at) AS last FROM retrieval_log"
            ).fetchone()
            out["retrieval"] = dict(row) if row else {}
        except sqlite3.Error:
            pass
    finally:
        ac.close()

    files = []
    if store["loops"]:
        files = sorted(
            (os.path.join(store["loops"], n) for n in os.listdir(store["loops"])
             if n.endswith(".json")),
            key=lambda p: os.path.getmtime(p), reverse=True,
        )
    out["loops_total"] = len(files)
    loops = []
    for path in files[:limit]:
        try:
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, ValueError):
            continue  # a half-written loop file must not break the digest
        if isinstance(raw, dict):
            loops.append(_project_loop(raw, timeout_ms))
    out["usage_by_session"] = agentos_usage_by_session(
        conn, [l.get("session_id") for l in loops]
    )
    for loop in loops:
        loop["usage"] = out["usage_by_session"].get(loop.get("session_id")) or {}
    out["loops"] = loops
    out["available"] = True
    out["reason"] = ""
    return out


