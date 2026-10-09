"""claude-mem — read-only neighbour: what its background capture actually did.

The tracker measures skill / MCP / plugin tool calls and nothing else, so a session
that only used builtins (`bash`, `edit`, …) leaves no row here at all. claude-mem
keeps its own ledger of the same sessions, and reading it answers the question the
tracker structurally cannot: "did anything happen".

Same contract as the advisor store, for the same reason: this is another project's
database, it is full of prompt-derived prose, and it is being written by a live
process. Read-only, field-whitelisted, never created, never exported. Guards in
`scripts/tests/test_claude_mem.py`. Import direction is one-way: this module imports
`skill_db`, never the reverse.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import sqlite3
import time
import urllib.request as urlreq   # aliased: the prose-field tripwire bans the bare word
from datetime import datetime, timezone

import skill_db as db

# The tracker measures skill / MCP / plugin tool calls and nothing else, so a
# session that only used builtins (`bash`, `edit`, …) leaves no row here at all.
# claude-mem keeps its own ledger of the same sessions, and reading it answers
# the question the tracker structurally cannot: "did anything happen".
#
# Same contract as the advisor store above, for the same reason: this is another
# project's database, it is full of prompt-derived prose, and it is being written
# by a live process. Read-only, field-whitelisted, never created, never exported.
# ---------------------------------------------------------------------------

CLAUDE_MEM_DB_ENV = "OPENCODE_SKILL_TRACKER_CLAUDE_MEM_DB"
CLAUDE_MEM_DIR_ENV = "CLAUDE_MEM_DIR"

# The tables this reader names, and the only columns it names from each. Every
# entry is a counter, a timestamp or a short enum — because the columns beside
# them (`observations.text`, `user_prompts.prompt_text`,
# `session_summaries.request`/`investigated`/`learned`/`completed`/`next_steps`,
# `sdk_sessions.user_prompt`/`custom_title`, `tool_uses.tool_input`/`tool_response`/`cwd`,
# `pending_messages.last_user_message`/`last_assistant_message`) are the user's
# own words. They appear nowhere below, in any SELECT, WHERE or log line.
CLAUDE_MEM_TABLES = {
    "observations":      {"time": "created_at_epoch", "label": "type",
                          "tokens": "discovery_tokens"},
    "user_prompts":      {"time": "created_at_epoch"},
    "session_summaries": {"time": "created_at_epoch", "tokens": "discovery_tokens"},
    "sdk_sessions":      {"time": "started_at_epoch", "label": "status"},
    "tool_uses":         {"time": "created_at_epoch", "label": "tool_name"},
    "pending_messages":  {"time": "created_at_epoch", "label": "status"},
}

# The sidecar files, and the only keys read from them. `observer-health.json`
# also holds `lastErrorMessage` / `lastErrorUrl` / `lastErrorRequestId` (free
# text and a URL from a third-party error page) and `supervisor.json` holds a
# process table with paths in it — none of that is named here. `installId` is a
# stable identifier of this machine, so it stays out too.
CLAUDE_MEM_HEALTH_KEYS = ("consecutiveFailures", "failingSinceAt", "lastErrorAt",
                          "lastErrorCode", "lastErrorKind", "lastSuccessAt",
                          "quotaCooldown")
CLAUDE_MEM_BACKFILL_KEYS = ("completedAt", "eventCount", "throughDay", "version")
CLAUDE_MEM_TELEMETRY_KEYS = ("enabled", "decidedAt")

# Never opened, at any cost: it holds `CLAUDE_MEM_OPENROUTER_API_KEY` and
# `CLAUDE_MEM_PRO_MEMORY_KEY`. A reader that "just looks at the mode" here is one
# refactor away from printing a secret into a terminal.
CLAUDE_MEM_FORBIDDEN_FILES = ("settings.json",)

# A label from another store is only echoed when it looks like a category name.
_LABEL_RE = re.compile(r"^[\w][\w.:/ -]{0,39}$")

# How many distinct labels one table may show: enough for the categories that
# exist, small enough that the output stays a summary rather than a dump.
CLAUDE_MEM_LABEL_CAP = 12

# The tracker's own three streams, for the same-window comparison below. Named
# here rather than imported from the TUI: the data layer must not depend on a
# screen.
CLAUDE_MEM_TRACKER_TABLES = ("skill_usage", "mcp_usage", "plugin_usage")


def claude_mem_store(db_path: str | None = None) -> dict:
    """Locate claude-mem's store. Never creates anything, never reads it here.

    Resolution order, all read at call time: explicit argument, then
    `OPENCODE_SKILL_TRACKER_CLAUDE_MEM_DB`, then `$CLAUDE_MEM_DIR/claude-mem.db`,
    then the default `~/.claude-mem/claude-mem.db`. The last one is why this
    neighbour needs no configuration to be discovered — unlike the advisor, whose
    location is a project choice and therefore has to be told to us.
    """
    path = db_path or os.environ.get(CLAUDE_MEM_DB_ENV)
    if not path:
        cm_dir = os.environ.get(CLAUDE_MEM_DIR_ENV)
        if cm_dir:
            path = os.path.join(cm_dir, "claude-mem.db")
    if not path:
        path = os.path.join(db.HOME, ".claude-mem", "claude-mem.db")
    result = {"requested": path, "db": None, "dir": None, "reason": "",
              "trace": None, "logs_dir": None, "pid_file": None}
    # The directory and its files are resolved whether or not the database is
    # there: the activity logs exist on their own, and bailing out early used to
    # hide them from `claude_mem_activity` on a machine whose worker had not
    # created a database yet.
    result["dir"] = os.path.dirname(path)
    for key, name, is_dir in (("trace", CLAUDE_MEM_TRACE_NAME, False),
                              ("logs_dir", "logs", True),
                              ("pid_file", CLAUDE_MEM_PID_NAME, False)):
        found = os.path.join(result["dir"], name)
        result[key] = found if (os.path.isdir(found) if is_dir
                                else os.path.isfile(found)) else None
    if not os.path.isfile(path):
        result["reason"] = f"no claude-mem database at {path}"
        return result
    result["db"] = path
    return result


def _open_claude_mem_ro(path: str):
    """Read-only on purpose; `open_db()` would fall back to read-write and
    create a missing file, which would hand a foreign store to a reader."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={db.BUSY_TIMEOUT_MS}")
    return conn


def _epoch_iso(value) -> str | None:
    """Epoch milliseconds from another store -> local 'YYYY-MM-DD HH:MM'.

    Anything that is not a plausible millisecond count yields None: a store that
    changes its time unit shows up as a missing number, not as a date in 1970.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value <= 0 or value < 10 ** 11 or value > 10 ** 14:
        return None
    return db.fmt_time(datetime.fromtimestamp(value / 1000, timezone.utc)
                    .strftime("%Y-%m-%dT%H:%M:%S.%fZ"))


def _short_label(value) -> str | None:
    """Echo a foreign enum only if it looks like a category name.

    `type`, `status` and `tool_name` are supposed to be short tokens, but they
    live in someone else's column and nothing stops a future version from putting
    a sentence there. A value that is not label-shaped becomes None rather than
    a line of the user's text on the terminal.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text if _LABEL_RE.match(text) else None


def _read_sidecar(dir_path: str, name: str, keys) -> dict | None:
    """Whitelisted keys of one small JSON file; None when it is not there."""
    if name in CLAUDE_MEM_FORBIDDEN_FILES:
        return None
    path = os.path.join(dir_path, name)
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    out = {}
    for key in keys:
        value = raw.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            out[key] = value
        elif isinstance(value, bool):
            out[key] = value
        elif isinstance(value, str) and len(value) <= 40 and not value.startswith(("http://", "https://", "/")):
            out[key] = value
    return out


def _sidecar_times(fields: dict | None) -> dict | None:
    """Epoch-ms fields become the same local string every other time uses.

    Only a plausible millisecond count is converted: `backfill.completedAt` is
    already an ISO string, and rewriting it to None would lose the one number
    that field carries.
    """
    if not fields:
        return fields
    for key in list(fields):
        if key.endswith("At"):
            iso = _epoch_iso(fields[key])
            if iso:
                fields[key] = iso
    return fields


# claude-mem's own activity files. Both are line-oriented and both carry prose
# the tracker must not take with it: `q=` in the trace log is the search text the
# plugin built from the user's prompt, and the worker log's message column holds
# absolute paths and error text. So the reader counts *shapes* and returns numbers.
CLAUDE_MEM_TRACE_NAME = "inject-trace.log"
CLAUDE_MEM_PID_NAME = "worker.pid"
CLAUDE_MEM_LOG_PREFIX = "claude-mem-"        # dated logs; `manual-restart-*` are noise
CLAUDE_MEM_LOG_BYTES_CAP = 4 * 1024 * 1024
# `[timestamp] [LEVEL] [category] message` — the second bracket is the level.
# Reading ERROR as a category counts nothing at all.
_LOG_LINE_RE = re.compile(r"^\[[^\]]+\]\s+\[\s*([A-Za-z]+)\s*]\s+\[([^\]]+)]")
_LEN_RE = re.compile(r"\blen=(\d+)")
# `project=` belongs to another tool's log. A space makes it a sentence and a
# slash makes it a path, so neither is admitted: a value that is not slug-shaped
# is counted, never echoed (the read side gates foreign enums the same way, via
# `_short_label`).
_PROJECT_LABEL_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,40}$")
_PROJECT_RE = re.compile(r"\bproject=([^ \"]+)")
# The day table keeps the most recent N days; the rest is added up and reported,
# never silently dropped. 14 is the same window the dashboard trend covers.
CLAUDE_MEM_DAY_CAP = 14



def _read_inject_trace(path: str, cap: int) -> dict | None:
    text, size, truncated = db._read_bounded_text(path, cap)
    if text is None:
        return None
    out = {"path": path, "lines": 0, "loaded": 0, "injected": 0,
           "injected_with_source": 0, "injected_bare": 0, "injected_with_query": 0,
           "worker_ensure": 0, "unrecognized": 0, "chars_total": 0,
           "last_ts": None, "size_bytes": size, "truncated": truncated,
           "by_project": {}, "projectless": {"injected": 0, "loaded": 0},
           "by_day": {}, "older_days": {"days": 0, "injected": 0, "loaded": 0},
           "undated": {"injected": 0, "loaded": 0}}
    projects: dict = {}
    days: dict = {}
    newest = ""
    for line in text.splitlines():
        body = line.strip()
        if not body:
            continue
        out["lines"] += 1
        stamp, _, rest = body.partition(" ")
        kind = None
        if rest.startswith("loaded"):
            out["loaded"] += 1
            kind = "loaded"
        elif rest.startswith("injected"):
            # Both shapes count: the plugin was edited in place, so the older bare
            # `injected project=… len=…` lines and the newer `source=` ones share
            # the file. Keying on `source=` alone dropped 91% of the injections.
            out["injected"] += 1
            kind = "injected"
            if " source=" in rest:
                out["injected_with_source"] += 1
            else:
                out["injected_bare"] += 1
            if " q=" in rest:
                # Counted, never read: what follows is the user's own text.
                out["injected_with_query"] += 1
            m = _LEN_RE.search(rest)
            if m:
                out["chars_total"] += int(m.group(1))
        elif rest.startswith("worker ensure"):
            out["worker_ensure"] += 1
        else:
            out["unrecognized"] += 1

        if kind:
            m = _PROJECT_RE.search(rest)
            name = m.group(1) if m else None
            if name and _PROJECT_LABEL_RE.match(name):
                slot = projects.setdefault(name, {"injected": 0, "loaded": 0})
                slot[kind] += 1
            else:
                out["projectless"][kind] += 1
            # Local calendar day, because that is how every chart on the screen
            # buckets (M1). `db.fmt_time`'s own output is the single source for it.
            local = _epoch_iso_from_iso_text(stamp)
            if local:
                slot = days.setdefault(local[:10], {"injected": 0, "loaded": 0})
                slot[kind] += 1
            else:
                out["undated"][kind] += 1

        if stamp > newest:
            # Only a stamp that parses may become the newest: a line that does not
            # start with a timestamp must not freeze out the real ones after it.
            converted = _epoch_iso_from_iso_text(stamp)
            if converted:
                newest, out["last_ts"] = stamp, converted

    out["by_project"] = dict(sorted(
        projects.items(),
        key=lambda kv: (-kv[1]["injected"], -kv[1]["loaded"], kv[0])))
    ordered = sorted(days)                       # ISO date strings sort as text
    keep = ordered[-CLAUDE_MEM_DAY_CAP:]
    for day in keep:
        out["by_day"][day] = days[day]
    for day in ordered[:len(ordered) - len(keep)]:
        out["older_days"]["days"] += 1
        for key in ("injected", "loaded"):
            out["older_days"][key] += days[day][key]
    return out


def _read_worker_log(logs_dir: str, cap: int) -> dict | None:
    try:
        names = [n for n in os.listdir(logs_dir)
                 if n.startswith(CLAUDE_MEM_LOG_PREFIX) and n.endswith(".log")]
    except OSError:
        return None
    newest = None
    for n in names:
        full = os.path.join(logs_dir, n)
        try:
            m = os.path.getmtime(full)
        except OSError:
            continue
        if newest is None or m > newest[1]:
            newest = (full, m)
    if not newest:
        return None
    path = newest[0]
    text, size, truncated = db._read_bounded_text(path, cap)
    if text is None:
        return None
    out = {"path": path, "lines": 0, "levels": {}, "categories": {},
           "unparsed": 0, "size_bytes": size, "truncated": truncated,
           "mtime": db.fmt_time(datetime.fromtimestamp(newest[1], timezone.utc)
                             .strftime("%Y-%m-%dT%H:%M:%S.%fZ"))}
    for line in text.splitlines():
        if not line.strip():
            continue
        out["lines"] += 1
        m = _LOG_LINE_RE.match(line)
        if not m:
            # The message is dropped here, not stored: a log line can carry a path
            # or a user string, and neither belongs in this report.
            out["unparsed"] += 1
            continue
        level, category = m.group(1).strip().upper(), m.group(2).strip()
        level = level if _short_label(level) else "?"
        category = _short_label(category) or "?"
        out["levels"][level] = out["levels"].get(level, 0) + 1
        out["categories"][category] = out["categories"].get(category, 0) + 1
    return out


def _epoch_iso_from_iso_text(value: str):
    """An ISO-Z stamp from another tool -> the local string every time here uses."""
    try:
        datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return db.fmt_time(value)


def claude_mem_activity(store: dict | None = None,
                        log_bytes_cap: int = CLAUDE_MEM_LOG_BYTES_CAP) -> dict:
    """What claude-mem's own log files say it did. Numbers only, read-only.

    `store` is a `claude_mem_store()` result; one is taken when not given. Absent
    files are absent, not zero: a machine without the plugin gets `available:
    False` and no lines in the report.
    """
    if store is None:
        store = claude_mem_store()
    out = {"available": False, "reason": store.get("reason", ""), "dir": store.get("dir"),
           "trace": None, "worker_log": None}
    if store.get("trace"):
        out["trace"] = _read_inject_trace(store["trace"], log_bytes_cap)
    if store.get("logs_dir"):
        out["worker_log"] = _read_worker_log(store["logs_dir"], log_bytes_cap)
    out["available"] = bool(out["trace"] or out["worker_log"])
    if not out["available"] and not out["reason"]:
        out["reason"] = f"no activity files under {store.get('dir')}"
    return out


# The worker answers HTTP, but only the last of the three sources below, and only
# from headless commands. Its answers carry a `database.path`, a version string
# and a free-text `details`, so the projection is by name *and* by type.
CLAUDE_MEM_HTTP_BUDGET_SECONDS = 2.0
CLAUDE_MEM_HTTP_WORKER_KEYS = ("uptime", "activeSessions", "sseClients")
CLAUDE_MEM_HTTP_DATABASE_KEYS = ("size", "observations", "sessions", "summaries")
CLAUDE_MEM_HTTP_QUEUE_KEYS = ("isProcessing", "queueDepth", "parkedSessions")
CLAUDE_MEM_HTTP_CHROMA_KEYS = ("connected", "deep")
CLAUDE_MEM_HTTP_ENDPOINTS = (("/api/stats", "stats"),
                             ("/api/processing-status", "queue"),
                             ("/api/chroma/status", "chroma"))


def claude_mem_worker(store: dict | None = None) -> dict:
    """What `worker.pid` says: which process, on which port, and whether it is up.

    No network. Liveness is `os.kill(pid, 0)`, which costs microseconds and still
    answers when the worker has never been started; the port comes from the file,
    so a worker moved off 37700 is still found. `startToken` is what authenticates
    to that worker and is deliberately not part of the result.
    """
    if store is None:
        store = claude_mem_store()
    out = {"available": False, "alive": False, "pid": None, "port": None,
           "started_at": None, "reason": ""}
    path = store.get("pid_file")
    if not path:
        out["reason"] = f"no {CLAUDE_MEM_PID_NAME} under {store.get('dir')}"
        return out
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        out["reason"] = f"unreadable {CLAUDE_MEM_PID_NAME}: {e}"
        return out
    if not isinstance(data, dict):
        out["reason"] = f"{CLAUDE_MEM_PID_NAME} holds no object"
        return out
    out["available"] = True
    # A non-integer pid or port is not repaired into one: it is reported as absent.
    out["pid"] = data.get("pid") if isinstance(data.get("pid"), int) else None
    out["port"] = data.get("port") if isinstance(data.get("port"), int) else None
    started = data.get("startedAt")
    out["started_at"] = (_epoch_iso_from_iso_text(started)
                         if isinstance(started, str) else None)
    if out["pid"] is None:
        out["reason"] = f"{CLAUDE_MEM_PID_NAME} carries no usable pid"
        return out
    try:
        os.kill(out["pid"], 0)
        out["alive"] = True
    except ProcessLookupError:
        out["reason"] = f"pid {out['pid']} is not running"
    except PermissionError:
        out["alive"] = True       # it exists; it is only not ours to signal
    except OSError as e:
        out["reason"] = f"pid {out['pid']}: {e}"
    return out


def _http_json(path: str, port: int, timeout: float):
    """One GET against the local worker. None on any failure, never an exception.

    `timeout` is what is left of the shared deadline, not a fresh budget.
    """
    url = f"http://127.0.0.1:{port}{path}"
    try:
        req = urlreq.Request(url, headers={"Accept": "application/json"})
        with urlreq.urlopen(req, timeout=timeout) as resp:
            if getattr(resp, "status", 200) != 200:
                return None
            body = json.loads(resp.read().decode("utf-8", "replace"))
    except (OSError, ValueError, EOFError, http.client.HTTPException):
        return None
    return body if isinstance(body, dict) else None


def _numbers_only(node, keys):
    """Whitelisted names, and only where the value is a number or a boolean.

    `isinstance(True, int)` holds, so the booleans come through; a path or a
    sentence in a trusted field name does not.
    """
    if not isinstance(node, dict):
        return None
    picked = {k: node[k] for k in keys if isinstance(node.get(k), int)}
    return picked or None


def claude_mem_http(worker: dict | None = None,
                    budget_seconds: float = CLAUDE_MEM_HTTP_BUDGET_SECONDS,
                    clock=time.monotonic) -> dict:
    """The worker's three status endpoints on one shared deadline.

    Headless only: the TUI repaints on every keypress and doctor runs offline, so
    neither may call this. The budget is not per endpoint — a worker that is
    starting up answers slowly, and three timeouts in a row would be three waits
    before the command said anything. Whatever the deadline does not reach is
    listed in `skipped` instead of being reported as zero.

    `clock` is injected so the deadline arithmetic can be tested without sleeping.
    """
    if worker is None:
        worker = claude_mem_worker()
    out = {"available": False, "reason": "", "skipped": [],
           "stats": None, "queue": None, "chroma": None}
    port = worker.get("port")
    if not worker.get("alive") or not isinstance(port, int):
        out["reason"] = worker.get("reason") or "the worker is not running"
        return out
    deadline = clock() + budget_seconds
    for path, field in CLAUDE_MEM_HTTP_ENDPOINTS:
        remaining = deadline - clock()
        if remaining <= 0:
            out["skipped"].append(field)
            continue
        body = _http_json(path, port, remaining)
        if field == "stats":
            view = {} if body is None else {
                name: got for name, got in (
                    ("worker", _numbers_only(body.get("worker"), CLAUDE_MEM_HTTP_WORKER_KEYS)),
                    ("database", _numbers_only(body.get("database"),
                                               CLAUDE_MEM_HTTP_DATABASE_KEYS))) if got}
            out[field] = view or None
        else:
            keys = (CLAUDE_MEM_HTTP_QUEUE_KEYS if field == "queue"
                    else CLAUDE_MEM_HTTP_CHROMA_KEYS)
            out[field] = _numbers_only(body, keys)
    out["available"] = any(out[field] for _, field in CLAUDE_MEM_HTTP_ENDPOINTS)
    if not out["available"]:
        out["reason"] = f"no answer from the worker on port {port}"
    return out


def claude_mem_summary(conn, days: int = 7, db_path: str | None = None) -> dict:
    """Counts and timestamps of claude-mem's own ledger, read-only.

    `conn` is the *tracker* database and is only used to say how much of the same
    window the tracker measured; nothing here writes to either store. A table that
    does not exist, or whose columns were renamed upstream, is reported as
    `missing` / `degraded` with no numbers — it never raises, because a neighbour
    upgrading under us must not break `skillt doctor`.
    """
    store = claude_mem_store(db_path)
    out = {
        "available": False, "reason": store["reason"], "db_path": store["db"],
        "dir": store["dir"], "days": days, "tables": {}, "health": None,
        "backfill": None, "telemetry": None, "newest_epoch": None,
        "newest": None, "errors": [], "tracker_same_window": None,
    }
    if not store["db"]:
        return out
    try:
        cm = _open_claude_mem_ro(store["db"])
    except sqlite3.Error as e:
        out["reason"] = f"could not open read-only: {e}"
        return out
    try:
        cutoff = int((datetime.now(timezone.utc).timestamp() * 1000)
                     - max(1, int(days)) * 86400 * 1000)
        for name, spec in CLAUDE_MEM_TABLES.items():
            row = {"n": None, "n_window": None, "last": None, "last_epoch": None,
                   "tokens": None, "by": None}
            try:
                cols = {r[1] for r in cm.execute(f"PRAGMA table_info({name})")}
            except sqlite3.Error:
                cols = set()
            if not cols:
                row["missing"] = True
                out["tables"][name] = row
                continue
            time_col = spec["time"] if spec["time"] in cols else None
            select = ["COUNT(*) AS n"]
            if time_col:
                # COALESCE: `SUM` over zero rows is NULL, and "0 in the window"
                # is a different fact from "cannot tell" — an empty table must say 0.
                select.append(f"COALESCE(SUM(CASE WHEN {time_col} >= ? THEN 1 ELSE 0 END), 0) AS n_window")
                select.append(f"MAX({time_col}) AS last_epoch")
            tokens = spec.get("tokens")
            if tokens and tokens in cols:
                select.append(f"COALESCE(SUM({tokens}), 0) AS tokens")
            try:
                r = cm.execute(f"SELECT {', '.join(select)} FROM {name}",
                               (cutoff,) if time_col else ()).fetchone()
            except sqlite3.Error as e:
                row["degraded"] = str(e)
                out["tables"][name] = row
                continue
            row["n"] = r["n"]
            if time_col:
                row["n_window"] = r["n_window"]
                row["last"] = _epoch_iso(r["last_epoch"])
                # `newest_epoch` is only ever set to a value the formatter also
                # accepted, so `claude_mem_age_days` can never date a row that the
                # report shows as unknown (a store that switched to seconds would
                # otherwise read as "20708 days ago").
                if row["last"] and (out["newest_epoch"] is None
                                    or r["last_epoch"] > out["newest_epoch"]):
                    out["newest_epoch"] = r["last_epoch"]
                else:
                    row["last_epoch"] = None
            if tokens and tokens in cols:
                row["tokens"] = r["tokens"]
            label = spec.get("label")
            if label and label in cols:
                try:
                    rows = cm.execute(
                        f"SELECT {label} AS v, COUNT(*) AS n FROM {name}"
                        f" GROUP BY {label} ORDER BY n DESC, v ASC LIMIT {CLAUDE_MEM_LABEL_CAP}"
                    ).fetchall()
                except sqlite3.Error:
                    rows = []
                counts: dict = {}
                for g in rows:
                    key = _short_label(g["v"])
                    if key is None:
                        # Counted, never echoed: an un-label-shaped value still
                        # belongs to the total, it just cannot be named here.
                        counts.setdefault("(not shown)", 0)
                        counts["(not shown)"] += g["n"]
                        continue
                    counts[key] = g["n"]
                row["by"] = counts
            out["tables"][name] = row
    finally:
        try:
            cm.close()
        except sqlite3.Error:
            pass

    readable = [n for n, r in out["tables"].items() if not r.get("missing")]
    if out["tables"] and not readable:
        # Every table we know about is absent: this is not an empty claude-mem,
        # it is some other file (or a rename we cannot follow). Saying
        # "installed, zero rows" would be a lie with a number on it.
        out["reason"] = ("no claude-mem tables readable in "
                         f"{store['db']} (tables: {', '.join(CLAUDE_MEM_TABLES)})")
        return out

    out["newest"] = _epoch_iso(out["newest_epoch"])
    # The join that makes the two numbers worth reading together: claude-mem saw
    # the session, and this is how much of the same window the tracker was
    # *allowed* to see. Only skill/MCP/plugin calls are measured here, so a zero
    # beside a non-zero claude-mem count is the design boundary, not a gap.
    if conn is not None:
        cutoff_iso = (datetime.fromtimestamp(cutoff / 1000, timezone.utc)
                      .strftime("%Y-%m-%dT%H:%M:%S.%fZ"))
        counts = {}
        for table in CLAUDE_MEM_TRACKER_TABLES:
            try:
                counts[table] = conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE timestamp >= ?", (cutoff_iso,)
                ).fetchone()[0]
            except sqlite3.Error:
                counts[table] = None
        out["tracker_same_window"] = counts
    if out["dir"]:
        out["health"] = _sidecar_times(
            _read_sidecar(out["dir"], "observer-health.json", CLAUDE_MEM_HEALTH_KEYS))
        out["backfill"] = _sidecar_times(
            _read_sidecar(out["dir"], "backfill.json", CLAUDE_MEM_BACKFILL_KEYS))
        out["telemetry"] = _sidecar_times(
            _read_sidecar(out["dir"], "telemetry.json", CLAUDE_MEM_TELEMETRY_KEYS))
    out["available"] = True
    out["reason"] = ""
    return out


def claude_mem_age_days(summary: dict):
    """Days since claude-mem's newest row; None when there is nothing to date."""
    epoch = (summary or {}).get("newest_epoch")
    if not isinstance(epoch, (int, float)) or isinstance(epoch, bool):
        return None
    return (datetime.now(timezone.utc).timestamp() * 1000 - epoch) / 86400000.0
