"""Everything this project knows about **OpenCode itself**, in one module.

Why this file exists: the tracker's whole failure mode is that OpenCode renames
something — a hook, an event type, a payload field, a config file — and the
recorder keeps working while quietly recording nothing. Before this module those
names lived in ~40 places across three languages, and `skill-tui.py` even
re-declared the plugin's hook names as strings so `doctor` could grep them.

The rule for what belongs here:

* **A name OpenCode chose** (hook ids, event types, payload field paths, the
  host's own directory layout, the tool ids it owns) belongs here.
* **A number or policy this project chose** (byte caps, retention counts, busy
  timeouts, sanitiser limits, export allowlists, backup name shapes) belongs in
  the module that acts on it. Putting those here would make this file a config
  dump and would not reduce any coupling.

What this module can and cannot prove: `test_compat.py` can show that every name
declared here is still present in the writer (a name that goes missing means the
writer stopped reading it, or upstream renamed it). It cannot show that the
writer reads *only* what is declared — that stays a review rule, and adding a
read means adding its name here in the same commit.
"""

from __future__ import annotations

import os
import re

# ---------------------------------------------------------------------------
# Hooks the host offers, under the names it offers them under.
# `permission.ask` and `command.execute.before` are hook names; the bus events
# with similar names below are a different mechanism and are listed separately.
# ---------------------------------------------------------------------------
HOOK_TOOL_BEFORE = "tool.execute.before"
HOOK_TOOL_AFTER = "tool.execute.after"
HOOK_PERMISSION_ASK = "permission.ask"
HOOK_COMMAND_BEFORE = "command.execute.before"
HOOK_EVENT = "event"
HOOK_CHAT_MESSAGE = "chat.message"
HOOK_DISPOSE = "dispose"

HOOKS = (
    HOOK_TOOL_BEFORE,
    HOOK_TOOL_AFTER,
    HOOK_PERMISSION_ASK,
    HOOK_COMMAND_BEFORE,
    HOOK_EVENT,
    HOOK_CHAT_MESSAGE,
    HOOK_DISPOSE,
)

# What `doctor` looks for in the **installed** plugin file to tell one plugin
# generation from another. The hook names come from above; the rest are this
# project's own identifiers (`mcp_usage` is our table, `recordPluginUsage` our
# writer), which is why they live here rather than in the TUI.
PLUGIN_BASE_MARKERS = ("event:", "chat.message")
PLUGIN_SKILL_MARKERS = (HOOK_TOOL_BEFORE, HOOK_TOOL_AFTER, HOOK_PERMISSION_ASK)
PLUGIN_MCP_MARKERS = ("mcp_usage", "function classify(", "recordMcpUsage")
PLUGIN_PLUGIN_MARKERS = ("plugin_usage", "recordPluginUsage", HOOK_COMMAND_BEFORE)

# ---------------------------------------------------------------------------
# Bus event types the `event` handler switches on.
#
# The permission pair is *measured*, not typed: OpenCode 1.18.33 emits
# `permission.asked` then `permission.replied`, while the SDK types declare
# `permission.updated` / `permissionID`. Coding to the SDK names made every real
# rejection write nothing (limitation M20), so both spellings are handled and
# both are declared here.
# ---------------------------------------------------------------------------
EVENT_SESSION_CREATED = "session.created"
EVENT_SESSION_UPDATED = "session.updated"
EVENT_VCS_BRANCH_UPDATED = "vcs.branch.updated"
EVENT_MESSAGE_PART_UPDATED = "message.part.updated"
EVENT_PERMISSION_ASKED = "permission.asked"
EVENT_PERMISSION_REPLIED = "permission.replied"
EVENT_PERMISSION_UPDATED = "permission.updated"      # the SDK spelling, still accepted

# Field-name pairs for the same fact under the two spellings. A handler tries the
# pair in this order; it never picks one by "the docs say so".
PERMISSION_ID_KEYS = ("id", "permissionID")
PERMISSION_REPLY_ID_KEYS = ("requestID", "permissionID")
PERMISSION_REPLY_KEYS = ("reply", "response")
PERMISSION_CALL_ID_PATHS = ("tool.callID", "callID")

# ---------------------------------------------------------------------------
# Payload paths read out of hook and event inputs, as the writer names them.
# Each must still appear in `plugin/skill-tracker.js` — see test_compat.py.
# ---------------------------------------------------------------------------
PAYLOAD_FIELDS = (
    "hookInput.tool",
    "hookInput.sessionID",
    "hookInput.callID",
    "hookInput.args",
    "hookInput.command",
    "hookInput.pattern",
    "hookInput.type",
    "hookInput.action",
    "hookInput.model.providerID",
    "hookInput.model.modelID",
    "hookOutput.args",
    "hookOutput.status",
    "hookOutput.metadata",
    "props.info",
    "props.branch",
    "props.sessionID",
    "props.part",
    "part.state",
    "part.callID",
    "state.input",
    "state.time",
    "state.metadata",
    "metadata.parentSessionId",
    "metadata.sessionId",
)

# ---------------------------------------------------------------------------
# Tool ids and argument keys the host owns.
# ---------------------------------------------------------------------------
SKILL_TOOL = "skill"                 # the builtin that loads a skill
TASK_TOOL = "task"                   # the builtin that starts a subagent
SKILL_ARG_NAME = "name"              # args.name of the skill tool
TASK_AGENT_FIELD = "subagent_type"   # args.subagent_type of the task tool
MCP_ID_SEPARATOR = "_"               # ids are `{server}_{tool}`
MCP_NAMESPACE_PREFIX = "mcp:"        # permission payloads self-identify this way
MCP_TOOL_UNKNOWN = "*"               # server known, tool not yet observed

# The builtin-tool allowlist itself lives in the plugin (it is a Bun-side set,
# pinned by `DEFAULT_BUILTIN_TOOLS` and refreshed from `/experimental/tool/ids`).
# What lives here is the *claim* about which release it was verified against.

# ---------------------------------------------------------------------------
# Version pins. Four separate places used to assert a version; these are the
# three claims that are actually different facts.
#   PIN_LOADER           — the release whose plugin loader enumerates every export
#   PIN_PERMISSION_SHAPE — the release the permission payloads were measured on
#   PIN_TOOL_IDS         — the release `DEFAULT_BUILTIN_TOOLS` was refreshed from;
#                          the plugin's own comment carries it, and `doctor`
#                          parses that comment back out (VERSION_PIN_RE)
# ---------------------------------------------------------------------------
PIN_LOADER = "1.18.32"
PIN_PERMISSION_SHAPE = "1.18.33"
PIN_TOOL_IDS = "1.18.34"
PIN_SDK_TYPES = "1.18.4"
VERSION_PIN_RE = re.compile(r"verified against OpenCode (\d+\.\d+\.\d+)")
PIN_COMMENT_PREFIX = "verified against OpenCode"

# ---------------------------------------------------------------------------
# The host's filesystem layout. `skill_db.py` owns the tracker's own filenames
# (`skill-usage.db`, backups, exports); this owns the directories under them.
# ---------------------------------------------------------------------------
DATA_HOME = (".local", "share", "opencode")
CONFIG_HOME = (".config", "opencode")
PACKAGES_HOME = (".cache", "opencode", "packages")
PLUGIN_SUBDIR = "plugin"
LOGS_SUBDIR = "logs"
SKILLS_SUBDIR = "skills"
CONFIG_FILE_NAMES = ("opencode.json", "opencode.jsonc")
PROJECT_CONFIG_DIR = ".opencode"
SKILL_FILE_NAME = "SKILL.md"
FRONTMATTER_NAME_KEY = "name"
FRONTMATTER_DESCRIPTION_KEY = "description"

# ---------------------------------------------------------------------------
# The two derived vocabularies the schema and the queries both repeat.
# ---------------------------------------------------------------------------

# A skill's category is the first path segment under the skills directory, and
# `source` is derived from that category rather than stored. The map is the only
# copy: `source_case_sql()` renders the SQL and `source_of()` is the Python side
# that used to be a hand-written mirror of it (the old comment asked for sync;
# now there is one list to change).
SOURCE_BY_CATEGORY = {
    "personal-skills": "personal",
    "open-source-skills": "open-source",
}
SOURCE_UNKNOWN = "unknown"

# Pairs (relative skill directory, category) checked against *both* renderings —
# the SQL by SQLite itself, the Python by `source_of()`. The "." case is what the
# two languages spell "the skills root" differently and must still agree.
CATEGORY_SAMPLES = (
    ("personal-skills/brainstorming", "personal-skills"),
    ("open-source-skills/systematic-debugging", "open-source-skills"),
    ("work/skillt", "work"),
    ("solo", "solo"),
    (".", None),
    ("", None),
)


def category_of(rel: str) -> str | None:
    """The first path segment of a skill directory, or None for the root itself.

    `os.path.relpath` says "." where `path.relative` says ""; both mean "no
    category", and a "." in the `category` column would surface as a source named
    "." on the Skills page. `rel` arrives from `os.path.relpath`, so it is split
    on the platform separator, not on a literal "/".
    """
    if rel in (".", ""):
        return None
    return rel.split(os.sep, 1)[0] or None


def source_of(category: str | None) -> str:
    """The `source` label a category maps to — same map as the SQL."""
    mapped = SOURCE_BY_CATEGORY.get(category or "")
    if mapped is not None:
        return mapped
    return category or SOURCE_UNKNOWN


def source_case_sql(column: str = "s.category") -> str:
    """Render `SOURCE_BY_CATEGORY` as the SQL CASE the queries embed.

    `NULLIF(..., '')` is not decoration: the old hand-written CASE let an empty
    category through as `''` while `source_of()` mapped it to `unknown`, so the
    two halves disagreed on that one input. Our writer cannot produce `''`
    (`category_of` returns None for the root), but a derived column should have
    one answer, not two.
    """
    whens = " ".join(
        f"WHEN '{cat}' THEN '{src}'" for cat, src in SOURCE_BY_CATEGORY.items()
    )
    return (
        f"CASE {column} {whens} "
        f"ELSE COALESCE(NULLIF({column}, ''), '{SOURCE_UNKNOWN}') END"
    )


# The status vocabulary. The DDL keeps its own `CHECK (status IN (...))` text in
# both copies (schema drift is pinned elsewhere and deliberately compares the
# two DDL blobs verbatim); these tuples exist so a rename has to be written down
# where someone looking for the enum will find it, and so `test_compat.py` can
# fail if either DDL copy stops matching.
STATUS_VALUES = ("success", "error", "denied", "ask", "unknown")
STATUS_VALUES_NO_DENIED = ("success", "error", "ask", "unknown")  # subagent_usage
TRIGGER_VALUES = ("tool_call", "event_detected", "permission_denied", "manual")


def status_check_sql(values=STATUS_VALUES) -> str:
    """The exact clause text the two schema copies contain."""
    return "CHECK (status IN (" + ",".join(f"'{v}'" for v in values) + "))"
