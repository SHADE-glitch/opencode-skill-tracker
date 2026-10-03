/**
 * OpenCode Skill + MCP Usage Tracker
 * ---------------------------------------------------------------------------
 * Records every `skill` tool invocation and every MCP tool invocation into a
 * SQLite database.
 *
 * Verified against OpenCode 1.18.32 / @opencode-ai/plugin 1.18.4:
 *   - Skills are exposed to the model as a tool named exactly "skill"
 *     (input schema `{ name: string }`). There is NO dedicated skill hook.
 *   - MCP tools are exposed as `{serverName}_{toolName}`, e.g.
 *     `basic-memory_read_note`, `playwright_browser_click`. Splitting on "_"
 *     is ambiguous (`basic-memory_list_memory_projects`), so the server set
 *     is resolved once at init and matched by longest prefix.
 *   - Every tool in the registry goes through ONE generic wrapper that fires
 *     `tool.execute.before` / `tool.execute.after` with `tool` = the tool id,
 *     so MCP tools are observed through exactly the same hooks as skills.
 *   - Detection therefore uses `tool.execute.before` / `tool.execute.after`
 *     as the primary path, `event` -> `message.part.updated` as a fallback,
 *     and the permission events for denials: OpenCode 1.18.33 emits
 *     `permission.asked` (id at `id`, the refused call at `tool.callID`) then
 *     `permission.replied` (`requestID` + `reply: "reject"`), measured live
 *     on 2026-10-01; the SDK's `permission.updated` / `permissionID` /
 *     `response` spellings are handled alongside. The `permission.ask` hook was
 *     never invoked by `opencode run`. Boundary: the host gates only edit /
 *     bash / webfetch / doom_loop / external_directory, none of which this
 *     tracker measures, so a `denied` row cannot appear on 1.18.33 even though
 *     the plumbing now works.
 *
 * Hard rule: no hook may ever throw into OpenCode, and no hook mutates the
 * `output` object it receives. Every body is wrapped in try/catch, and
 * `bun:sqlite` is imported dynamically so a load failure degrades to a no-op.
 *
 * Privacy: MCP argument *values* are never read. Only the argument key names
 * are recorded, and only for the MCP path.
 *
 * Storage : ~/.local/share/opencode/skill-usage.db   (SQLite, WAL)
 * Log     : ~/.config/opencode/logs/skill-tracker.log
 * ---------------------------------------------------------------------------
 */

import fs from "node:fs";
import path from "node:path";

// ---------------------------------------------------------------------------
// Constants (all overridable via environment variables)
// ---------------------------------------------------------------------------
const HOME = process.env.HOME || "";
const CFG_DIR =
  process.env.OPENCODE_SKILL_TRACKER_CONFIG_DIR || path.join(HOME, ".config/opencode");
const DB_PATH =
  process.env.OPENCODE_SKILL_TRACKER_DB ||
  path.join(HOME, ".local/share/opencode/skill-usage.db");
const SKILLS_DIR =
  process.env.OPENCODE_SKILL_TRACKER_SKILLS_DIR || path.join(CFG_DIR, "skills");
const LOG_PATH =
  process.env.OPENCODE_SKILL_TRACKER_LOG ||
  path.join(CFG_DIR, "logs/skill-tracker.log");
const SKILL_TOOL = process.env.OPENCODE_SKILL_TRACKER_TOOL || "skill";
const DISABLED = process.env.OPENCODE_SKILL_TRACKER_DISABLE === "1";
const DEBUG = process.env.OPENCODE_SKILL_TRACKER_DEBUG === "1";
// MCP recording has its own kill switch so the skill side can stay on.
const MCP_DISABLED = process.env.OPENCODE_SKILL_TRACKER_MCP_DISABLE === "1";
// Plugin-provided tool/command recording has a third one, for the same reason.
const PLUGIN_DISABLED = process.env.OPENCODE_SKILL_TRACKER_PLUGIN_DISABLE === "1";

const SANITIZE_MAX = 120;
const META_TEXT_MAX = 200; // cap for sanitized free-text metadata (title/error)
const BUSY_TIMEOUT_MS = 5000;
const MAP_CAP = 5000; // FIFO eviction guard for in-memory maps
const GIT_TIMEOUT_MS = 500;
const MCP_STATUS_TIMEOUT_MS = 1500; // plugin init must never hang on MCP
const MCP_SERVER_CAP = 256;
const MCP_ARG_MAX = 32; // argument names recorded per call
const MCP_ARG_NAME_MAX = 64; // characters per argument name
const PLUGIN_NAME_MAX = 120;
const PLUGIN_ITEM_MAX = 120;
// The plugin name used when a tool is provably not builtin and not MCP, but no
// installed plugin could be matched to it. Never a guess at a specific plugin.
const UNKNOWN_PLUGIN = "(unknown)";
// Builtin tool ids, verified against OpenCode 1.18.33 with
// `curl /experimental/tool/ids`. Anything outside this set that is not the
// skill tool and not an MCP tool is treated as plugin-provided. Refresh this
// list on an OpenCode upgrade, or override it with
// OPENCODE_SKILL_TRACKER_BUILTIN_TOOLS (comma-separated).
const DEFAULT_BUILTIN_TOOLS =
  "invalid,question,bash,read,glob,grep,edit,write,task,webfetch,todowrite,websearch,skill,apply_patch";
// Infrastructure plugins we do not measure. The tracker excludes *itself* by
// path (see loadPlugins), never by name, so renaming this file is safe.
const PLUGIN_EXCLUDE_DEFAULT = ["opencode-notifier"];
const PLUGIN_EXCLUDE_EXTRA = (process.env.OPENCODE_SKILL_TRACKER_PLUGIN_EXCLUDE || "")
  .split(",")
  .map((s) => s.trim())
  .filter(Boolean);

// ---------------------------------------------------------------------------
// Logging — never logs payloads, messages, args or metadata (privacy).
// ---------------------------------------------------------------------------
function log(level, msg) {
  try {
    fs.mkdirSync(path.dirname(LOG_PATH), { recursive: true });
    fs.appendFileSync(LOG_PATH, `${new Date().toISOString()} [${level}] ${msg}\n`);
  } catch {
    /* logging must never break anything */
  }
}
function debug(msg) {
  if (DEBUG) log("debug", msg);
}

// ---------------------------------------------------------------------------
// Schema
// ---------------------------------------------------------------------------
const TABLES_SQL = `
CREATE TABLE IF NOT EXISTS skills (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  name        TEXT    NOT NULL,
  category    TEXT,
  path        TEXT    NOT NULL UNIQUE,
  description TEXT,
  created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_skills_name     ON skills(name);
CREATE INDEX IF NOT EXISTS idx_skills_category ON skills(category);

CREATE TABLE IF NOT EXISTS skill_usage (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  skill_id     INTEGER REFERENCES skills(id) ON DELETE SET NULL,
  skill_name   TEXT    NOT NULL,
  session_id   TEXT,
  project_path TEXT,
  trigger_type TEXT    NOT NULL
               CHECK (trigger_type IN ('tool_call','event_detected','permission_denied','manual')),
  status       TEXT    NOT NULL DEFAULT 'unknown'
               CHECK (status IN ('success','error','denied','ask','unknown')),
  timestamp    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  duration_ms  INTEGER,
  call_id      TEXT,
  metadata     TEXT,
  UNIQUE (session_id, call_id)
);
CREATE INDEX IF NOT EXISTS idx_usage_skill   ON skill_usage(skill_name);
CREATE INDEX IF NOT EXISTS idx_usage_ts      ON skill_usage(timestamp);
CREATE INDEX IF NOT EXISTS idx_usage_session ON skill_usage(session_id);
CREATE INDEX IF NOT EXISTS idx_usage_trigger ON skill_usage(trigger_type);

CREATE TABLE IF NOT EXISTS mcp_usage (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  server_name  TEXT    NOT NULL,
  tool_name    TEXT    NOT NULL,
  session_id   TEXT,
  project_path TEXT,
  trigger_type TEXT    NOT NULL
               CHECK (trigger_type IN ('tool_call','event_detected','permission_denied','manual')),
  status       TEXT    NOT NULL DEFAULT 'unknown'
               CHECK (status IN ('success','error','denied','ask','unknown')),
  timestamp    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  duration_ms  INTEGER,
  call_id      TEXT,
  arg_names    TEXT,
  metadata     TEXT,
  UNIQUE (session_id, call_id)
);
CREATE INDEX IF NOT EXISTS idx_mcp_server  ON mcp_usage(server_name);
CREATE INDEX IF NOT EXISTS idx_mcp_tool    ON mcp_usage(tool_name);
CREATE INDEX IF NOT EXISTS idx_mcp_ts      ON mcp_usage(timestamp);
CREATE INDEX IF NOT EXISTS idx_mcp_session ON mcp_usage(session_id);
CREATE INDEX IF NOT EXISTS idx_mcp_trigger ON mcp_usage(trigger_type);

CREATE TABLE IF NOT EXISTS plugin_usage (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  plugin_name  TEXT    NOT NULL,
  kind         TEXT    NOT NULL CHECK (kind IN ('tool','command')),
  item_name    TEXT    NOT NULL,
  session_id   TEXT,
  project_path TEXT,
  trigger_type TEXT    NOT NULL
               CHECK (trigger_type IN ('tool_call','command_call','event_detected','permission_denied','manual')),
  status       TEXT    NOT NULL DEFAULT 'unknown'
               CHECK (status IN ('success','error','denied','ask','unknown')),
  timestamp    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  duration_ms  INTEGER,
  call_id      TEXT,
  metadata     TEXT,
  UNIQUE (session_id, call_id)
);
CREATE INDEX IF NOT EXISTS idx_plugin_name    ON plugin_usage(plugin_name);
CREATE INDEX IF NOT EXISTS idx_plugin_item    ON plugin_usage(item_name);
CREATE INDEX IF NOT EXISTS idx_plugin_ts      ON plugin_usage(timestamp);
CREATE INDEX IF NOT EXISTS idx_plugin_session ON plugin_usage(session_id);
CREATE INDEX IF NOT EXISTS idx_plugin_trigger ON plugin_usage(trigger_type);

CREATE TABLE IF NOT EXISTS plugin_inventory (
  plugin_name TEXT PRIMARY KEY,
  version     TEXT,
  source      TEXT,
  skipped     INTEGER NOT NULL DEFAULT 0,
  tools       TEXT,
  commands    TEXT,
  scope       TEXT,
  first_seen  TEXT NOT NULL,
  last_seen   TEXT NOT NULL
);
`;

// Views use json_extract (JSON1). Wrapped separately so a missing JSON1 build
// still leaves the tables usable.
const VIEWS_SQL = `
CREATE VIEW IF NOT EXISTS v_skill_totals AS
SELECT skill_name,
       COUNT(*)              AS total,
       SUM(status='success') AS success,
       SUM(status='error')   AS errors,
       SUM(status='denied')  AS denied,
       MAX(timestamp)        AS last_used
FROM skill_usage GROUP BY skill_name;

CREATE VIEW IF NOT EXISTS v_skill_last30 AS
SELECT skill_name, COUNT(*) AS uses_30d, MAX(timestamp) AS last_used_30d
FROM skill_usage
WHERE timestamp >= strftime('%Y-%m-%dT%H:%M:%fZ','now','-30 days')
GROUP BY skill_name;

CREATE VIEW IF NOT EXISTS v_skill_history AS
SELECT u.id, u.skill_name, u.timestamp, u.project_path, u.session_id,
       u.status, u.duration_ms, u.trigger_type,
       json_extract(u.metadata,'$.model')   AS model,
       json_extract(u.metadata,'$.agent')   AS agent,
       json_extract(u.metadata,'$.branch')  AS branch
FROM skill_usage u
ORDER BY u.timestamp DESC;

CREATE VIEW IF NOT EXISTS v_mcp_totals AS
SELECT server_name,
       tool_name,
       COUNT(*)              AS total,
       SUM(status='success') AS success,
       SUM(status='error')   AS errors,
       SUM(status='denied')  AS denied,
       MAX(timestamp)        AS last_used
FROM mcp_usage GROUP BY server_name, tool_name;

CREATE VIEW IF NOT EXISTS v_mcp_last30 AS
SELECT server_name, tool_name, COUNT(*) AS uses_30d, MAX(timestamp) AS last_used_30d
FROM mcp_usage
WHERE timestamp >= strftime('%Y-%m-%dT%H:%M:%fZ','now','-30 days')
GROUP BY server_name, tool_name;

CREATE VIEW IF NOT EXISTS v_mcp_history AS
SELECT u.id, u.server_name, u.tool_name, u.timestamp, u.project_path, u.session_id,
       u.status, u.duration_ms, u.trigger_type, u.arg_names,
       json_extract(u.metadata,'$.model')   AS model,
       json_extract(u.metadata,'$.agent')   AS agent,
       json_extract(u.metadata,'$.branch')  AS branch
FROM mcp_usage u
ORDER BY u.timestamp DESC;

CREATE VIEW IF NOT EXISTS v_plugin_totals AS
SELECT plugin_name,
       kind,
       item_name,
       COUNT(*)              AS total,
       SUM(status='success') AS success,
       SUM(status='error')   AS errors,
       SUM(status='denied')  AS denied,
       MAX(timestamp)        AS last_used
FROM plugin_usage GROUP BY plugin_name, kind, item_name;

CREATE VIEW IF NOT EXISTS v_plugin_last30 AS
SELECT plugin_name, kind, item_name, COUNT(*) AS uses_30d, MAX(timestamp) AS last_used_30d
FROM plugin_usage
WHERE timestamp >= strftime('%Y-%m-%dT%H:%M:%fZ','now','-30 days')
GROUP BY plugin_name, kind, item_name;

CREATE VIEW IF NOT EXISTS v_plugin_history AS
SELECT u.id, u.plugin_name, u.kind, u.item_name, u.timestamp, u.project_path, u.session_id,
       u.status, u.duration_ms, u.trigger_type,
       json_extract(u.metadata,'$.model')   AS model,
       json_extract(u.metadata,'$.agent')   AS agent,
       json_extract(u.metadata,'$.branch')  AS branch
FROM plugin_usage u
ORDER BY u.timestamp DESC;
`;

// Single upsert used by every detection path. UNIQUE(session_id, call_id)
// guarantees exactly one row per tool call; the DO UPDATE clause only enriches
// a row that is still missing duration or a terminal status.
const UPSERT_USAGE_SQL = `
INSERT INTO skill_usage
  (skill_id, skill_name, session_id, project_path, trigger_type, status,
   timestamp, duration_ms, call_id, metadata)
VALUES (?, ?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'), ?, ?, ?)
ON CONFLICT(session_id, call_id) DO UPDATE SET
  duration_ms  = COALESCE(skill_usage.duration_ms, excluded.duration_ms),
  status       = CASE
                   -- A genuine error observed on the event path may correct a
                   -- success that tool.execute.after optimistically wrote.
                   -- The reverse never happens: success cannot downgrade a
                   -- recorded error/denied.
                   WHEN excluded.status = 'error' AND skill_usage.status = 'success'
                     THEN 'error'
                   WHEN skill_usage.status IN ('success','error','denied')
                     THEN skill_usage.status
                   ELSE excluded.status END,
  metadata     = COALESCE(skill_usage.metadata, excluded.metadata),
  project_path = COALESCE(skill_usage.project_path, excluded.project_path),
  skill_id     = COALESCE(skill_usage.skill_id, excluded.skill_id)
WHERE skill_usage.duration_ms IS NULL
   OR skill_usage.status IN ('unknown','ask')
   OR (excluded.status = 'error' AND skill_usage.status = 'success')
`;

// Same contract as UPSERT_USAGE_SQL, for the MCP table. The permission paths
// can only name the server, so they write the '*' sentinel for the tool; a
// later tool-level observation upgrades it to the real tool name.
const UPSERT_MCP_SQL = `
INSERT INTO mcp_usage
  (server_name, tool_name, session_id, project_path, trigger_type, status,
   timestamp, duration_ms, call_id, arg_names, metadata)
VALUES (?, ?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'), ?, ?, ?, ?)
ON CONFLICT(session_id, call_id) DO UPDATE SET
  tool_name    = CASE
                   WHEN mcp_usage.tool_name = '*' AND excluded.tool_name <> '*'
                     THEN excluded.tool_name
                   ELSE mcp_usage.tool_name END,
  duration_ms  = COALESCE(mcp_usage.duration_ms, excluded.duration_ms),
  status       = CASE
                   -- Mirrors the skill table: a genuine error may correct an
                   -- optimistic success, never the other way round.
                   WHEN excluded.status = 'error' AND mcp_usage.status = 'success'
                     THEN 'error'
                   WHEN mcp_usage.status IN ('success','error','denied')
                     THEN mcp_usage.status
                   ELSE excluded.status END,
  metadata     = COALESCE(mcp_usage.metadata, excluded.metadata),
  arg_names    = COALESCE(mcp_usage.arg_names, excluded.arg_names),
  project_path = COALESCE(mcp_usage.project_path, excluded.project_path)
WHERE mcp_usage.duration_ms IS NULL
   OR mcp_usage.status IN ('unknown','ask')
   OR (excluded.status = 'error' AND mcp_usage.status = 'success')
`;

// Sibling of UPSERT_MCP_SQL for the plugin table. A plugin tool whose owner
// could not be resolved is written as UNKNOWN_PLUGIN; a later observation that
// does resolve it upgrades the row, mirroring the MCP '*' sentinel upgrade.
// The sentinel is interpolated so there is exactly one definition of it.
const UPSERT_PLUGIN_SQL = `
INSERT INTO plugin_usage
  (plugin_name, kind, item_name, session_id, project_path, trigger_type, status,
   timestamp, duration_ms, call_id, metadata)
VALUES (?, ?, ?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'), ?, ?, ?)
ON CONFLICT(session_id, call_id) DO UPDATE SET
  plugin_name  = CASE
                   WHEN plugin_usage.plugin_name = '${UNKNOWN_PLUGIN}'
                        AND excluded.plugin_name <> '${UNKNOWN_PLUGIN}'
                     THEN excluded.plugin_name
                   ELSE plugin_usage.plugin_name END,
  duration_ms  = COALESCE(plugin_usage.duration_ms, excluded.duration_ms),
  status       = CASE
                   -- Same rule as the other two tables: an error may correct an
                   -- optimistic success, never the other way round.
                   WHEN excluded.status = 'error' AND plugin_usage.status = 'success'
                     THEN 'error'
                   WHEN plugin_usage.status IN ('success','error','denied')
                     THEN plugin_usage.status
                   ELSE excluded.status END,
  metadata     = COALESCE(plugin_usage.metadata, excluded.metadata),
  project_path = COALESCE(plugin_usage.project_path, excluded.project_path)
WHERE plugin_usage.duration_ms IS NULL
   OR plugin_usage.status IN ('unknown','ask')
   OR (excluded.status = 'error' AND plugin_usage.status = 'success')
`;

// One row per plugin seen at init. first_seen is deliberately never updated,
// so the inventory doubles as a record of when each plugin first appeared.
//
// `scope` is what makes the row deletable later (see SCOPE_* below), so an
// existing row is never downgraded from `project`: a plugin that any session has
// seen coming from *that project's* config must not be pruned by a session
// running somewhere else.
const UPSERT_PLUGIN_INVENTORY_SQL = `
INSERT INTO plugin_inventory
  (plugin_name, version, source, skipped, tools, commands, scope, first_seen, last_seen)
VALUES (?, ?, ?, ?, ?, ?, ?,
        strftime('%Y-%m-%dT%H:%M:%fZ','now'),
        strftime('%Y-%m-%dT%H:%M:%fZ','now'))
ON CONFLICT(plugin_name) DO UPDATE SET
  version   = excluded.version,
  source    = excluded.source,
  skipped   = excluded.skipped,
  tools     = excluded.tools,
  commands  = excluded.commands,
  scope     = CASE WHEN plugin_inventory.scope = 'project' THEN 'project'
                   ELSE excluded.scope END,
  last_seen = excluded.last_seen
`;

// ---------------------------------------------------------------------------
// Module state
// ---------------------------------------------------------------------------
let Database = null; // bun:sqlite Database constructor, resolved dynamically
let db = null; // open connection
let initDone = false;
let initInFlight = null; // dedupes concurrent init() calls
let sqliteUnavailable = false;
let pluginDir = null; // PluginInput.directory / worktree fallback
let pluginClient = null; // PluginInput.client, used once for MCP server discovery

let skillIdByName = new Map(); // skill name -> skills.id
let mcpServers = new Set(); // configured MCP server names (lowercased never)
let pluginSurface = new Map(); // plugin name -> { tools: Set, commands: Set }
let toolToPlugin = new Map(); // plugin tool id  -> plugin name
let commandToPlugin = new Map(); // plugin command id -> plugin name
let builtinTools = null; // lazily built; null means "not read yet"
const sessionCtx = new Map(); // sessionID -> { directory, model, agent }
// callIDs are only unique within a session, so every in-flight key includes the
// session id. Keying on the call id alone let two sessions running the same id
// share (and clobber) each other's start time, kind and skill name.
function callKey(sessionID, callID) {
  return `${sessionID ?? ""}:${callID ?? ""}`;
}
const callCtx = new Map(); // callKey(sessionID, callID) -> { sessionID, startMs, kind, skillName, server, tool, argNames }
const branchByDir = new Map(); // dir       -> branch | null
const pendingPerms = new Map(); // permissionID -> { kind, name, server, tool, callID }

// Replies that mean "no". OpenCode 1.18.33 sends `reject`; other spellings are
// kept because the value is a host-side string, not a type.
const DENIED_REPLIES = [
  "reject", "rejected", "deny", "denied", "no", "cancel", "cancelled",
];

// What did a permission reply actually refuse?
//
// Two sources disagree in precision. The permission payload names the
// *permission* — for MCP that is only the server, for a builtin it is a tool
// the tracker deliberately does not measure. The in-flight call, still parked
// from `tool.execute.before`, names the real tool id. The call wins when it is
// there; otherwise fall back to the payload, and to nothing when neither points
// at a measurable category.
function attributeDenial(perm, sessionID) {
  const sid = sessionID || perm.sessionID || null;
  const ctx = perm.callID ? callCtx.get(callKey(sid, perm.callID)) : null;
  if (!ctx) return perm.kind ? perm : null;
  return {
    kind: ctx.kind,
    itemKind: ctx.itemKind || (ctx.kind === "plugin" ? "tool" : "tool"),
    name: ctx.skillName || ctx.item || perm.name,
    server: ctx.server ?? perm.server ?? null,
    tool: ctx.tool ?? perm.tool ?? null,
    item: ctx.item ?? perm.item ?? null,
    plugin: ctx.plugin ?? perm.plugin ?? null,
    callID: perm.callID,
  };
}

// Git branch lookup spawns a real subprocess so the timeout can actually kill
// it — Bun's `$` shell exposes no kill/abort handle, so a slow git would
// outlive its 500ms deadline. Injectable so __selftest can avoid spawning git.
let spawnGit = (args, signal) =>
  Bun.spawn(args, { stdout: "pipe", stderr: "ignore", signal });

function setCapped(map, key, value) {
  if (map.size >= MAP_CAP && !map.has(key)) {
    const first = map.keys().next().value;
    if (first !== undefined) map.delete(first);
  }
  map.set(key, value);
}

function mergeSession(sessionID, patch) {
  if (!sessionID) return;
  const prev = sessionCtx.get(sessionID) || {};
  setCapped(sessionCtx, sessionID, { ...prev, ...patch });
}

// ---------------------------------------------------------------------------
// MCP classification
//
// Tool ids look like `{server}_{tool}`. Splitting on "_" is ambiguous
// (`basic-memory_list_memory_projects` is server `basic-memory` + tool
// `list_memory_projects`), so the configured server set is the authority and
// we match the longest prefix. Permission payloads may instead carry the
// self-identifying `mcp:<server>:*` namespace.
// ---------------------------------------------------------------------------
function mcpArgNames(args) {
  try {
    if (!args || typeof args !== "object" || Array.isArray(args)) return null;
    const names = [];
    // Object.keys only (own enumerable). The loop never reads a value, so no
    // argument value can reach the database or the log by construction.
    for (const k of Object.keys(args)) {
      if (names.length >= MCP_ARG_MAX) break;
      names.push(String(k).slice(0, MCP_ARG_NAME_MAX));
    }
    return names.length ? JSON.stringify(names) : null;
  } catch {
    return null;
  }
}

function serverFromNamespace(value) {
  const m = /^mcp:([^:]+)/.exec(String(value || ""));
  return m && m[1] ? m[1] : null;
}

function classify(toolId, opts) {
  if (typeof toolId !== "string" || !toolId) return null;
  if (toolId === SKILL_TOOL) return { kind: "skill" };

  // MCP first: its namespace is explicit, and the configured server set is the
  // authority for the ambiguous `{server}_{tool}` ids. Still fail closed —
  // an id we cannot positively place is never guessed to be MCP.
  //
  // Runs even when MCP recording is disabled, so an MCP id is still recognised
  // here and does not fall through to the plugin branch below.
  {
    const ns = serverFromNamespace(toolId);
    if (ns) return { kind: "mcp", server: ns, tool: null };
    if (mcpServers.size) {
      let best = null;
      for (const s of mcpServers) {
        if (toolId.length > s.length + 1 && toolId.startsWith(s + "_")) {
          if (!best || s.length > best.length) best = s;
        }
      }
      if (best) {
        return { kind: "mcp", server: best, tool: toolId.slice(best.length + 1) || null };
      }
    }
  }

  // Plugin-provided tools fail *open*: the builtin allowlist is the only thing
  // separating them from the rest of the registry, so anything outside it that
  // is neither skill nor MCP is attributed to a plugin — by name when the init
  // scan resolved it, otherwise to UNKNOWN_PLUGIN.
  if (PLUGIN_DISABLED) return null;
  if (builtinToolSet().has(toolId)) return null;
  const owner = toolToPlugin.get(toolId) || null;
  // Permission payloads carry permission *kinds* ("mcp", "read", ...) that are
  // not tool ids at all, so that path accepts only a positively resolved tool
  // and never the UNKNOWN_PLUGIN fallback.
  if (!owner && opts && opts.strict) return null;
  return {
    kind: "plugin",
    itemKind: "tool",
    item: toolId.slice(0, PLUGIN_ITEM_MAX),
    plugin: owner || UNKNOWN_PLUGIN,
  };
}

// Permission payloads are not uniform: some carry the tool id in
// type/action, others only a `mcp:<server>:*` pattern. Accept both shapes.
function classifyPermission(perm) {
  if (!perm) return null;
  const direct = classify(perm.type || perm.action, { strict: true });
  if (direct) return direct;
  const patterns = Array.isArray(perm.pattern) ? perm.pattern : [perm.pattern];
  for (const p of [perm.type, perm.action, ...patterns]) {
    const server = serverFromNamespace(p);
    if (server) return { kind: "mcp", server, tool: null };
  }
  return null;
}

// ---------------------------------------------------------------------------
// MCP server discovery — runs once at init, never in the hot path.
// ---------------------------------------------------------------------------
function withTimeout(promise, ms) {
  return new Promise((resolve) => {
    const timer = setTimeout(() => resolve(null), ms);
    if (timer && typeof timer.unref === "function") timer.unref();
    Promise.resolve(promise).then(
      (v) => {
        clearTimeout(timer);
        resolve(v);
      },
      () => {
        clearTimeout(timer);
        resolve(null);
      }
    );
  });
}

// Reads an OpenCode config file, tolerating JSONC comments. Returns null when
// the file is missing or unparsable; callers treat that as "nothing here".
function readJsonConfig(file) {
  try {
    if (!fs.existsSync(file)) return null;
    const text = fs.readFileSync(file, "utf8");
    try {
      return JSON.parse(text);
    } catch {
      // Tolerate JSONC: strip block then line comments, then retry once.
      return JSON.parse(
        text.replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|[^:])\/\/.*$/gm, "$1")
      );
    }
  } catch {
    return null;
  }
}

// Global config first, then the project-level files. Shared by MCP and plugin
// discovery so the two never drift apart.
function configFiles() {
  const files = [
    path.join(CFG_DIR, "opencode.json"),
    path.join(CFG_DIR, "opencode.jsonc"),
  ];
  if (pluginDir) {
    files.push(
      path.join(pluginDir, "opencode.json"),
      path.join(pluginDir, ".opencode/opencode.json")
    );
  }
  return files;
}

function readMcpKeysFromConfig(file) {
  const json = readJsonConfig(file);
  const mcp = json && json.mcp;
  return mcp && typeof mcp === "object" && !Array.isArray(mcp) ? Object.keys(mcp) : [];
}

async function fetchMcpServersFromClient(client) {
  try {
    const mcpApi = client && client.mcp;
    if (!mcpApi || typeof mcpApi.status !== "function") return [];
    const call = mcpApi.status.call(
      mcpApi,
      pluginDir ? { query: { directory: pluginDir } } : {}
    );
    const result = await withTimeout(call, MCP_STATUS_TIMEOUT_MS);
    if (!result || typeof result !== "object") return [];
    // The SDK may wrap the payload (`{ data: {...} }`) or return it directly.
    const payload = result.data && typeof result.data === "object" ? result.data : result;
    return Object.keys(payload)
      .slice(0, MCP_SERVER_CAP)
      .filter((k) => typeof k === "string" && k.length > 0);
  } catch (e) {
    debug("mcp status lookup failed: " + errMsg(e));
    return [];
  }
}

async function loadMcpServers(client) {
  // Discovery runs even when recording is disabled. `classify` needs the server
  // names to recognise an MCP tool id; without them an MCP id falls through to
  // the plugin branch and is stored as a bogus "(unknown)" plugin call.
  // Suppressing the write is recordMcpUsage()'s job, not this function's.
  if (MCP_DISABLED) {
    log("info", "mcp recording disabled via OPENCODE_SKILL_TRACKER_MCP_DISABLE");
  }

  // 1. An explicit override always wins — even when empty — so tests never
  //    read the developer's real configuration. Read at call time (not at
  //    module load) so __selftest can set it before the factory runs.
  const envServers = process.env.OPENCODE_SKILL_TRACKER_MCP_SERVERS;
  if (envServers !== undefined) {
    mcpServers = new Set(
      envServers
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean)
    );
    log("info", `mcp servers from env: ${mcpServers.size}`);
    return;
  }

  // 2. The live registry. Covers project-level config and runtime-added servers.
  const fromClient = await fetchMcpServersFromClient(client);
  if (fromClient.length) {
    mcpServers = new Set(fromClient);
    log("info", `mcp servers from client: ${mcpServers.size}`);
    return;
  }

  // 3. Configuration files.
  const found = new Set();
  for (const f of configFiles()) {
    for (const k of readMcpKeysFromConfig(f)) found.add(k);
  }
  mcpServers = found;
  if (mcpServers.size) {
    log("info", `mcp servers from config: ${mcpServers.size}`);
  } else {
    log("info", "mcp servers: none detected (mcp recording disabled)");
  }
}

// ---------------------------------------------------------------------------
// Plugin discovery — also runs once at init, never in the hot path.
//
// OpenCode exposes no API that says which plugin registered a tool (the tool
// list carries only id/description/parameters), so attribution is best effort:
// each installed plugin's bundle is scanned for the tools and commands it
// registers. A plugin whose registration shape we cannot parse is simply not
// attributed — the observed tool then falls back to UNKNOWN_PLUGIN rather than
// being credited to the wrong plugin.
// ---------------------------------------------------------------------------
const PACKAGES_DIR =
  process.env.OPENCODE_SKILL_TRACKER_PACKAGES_DIR ||
  path.join(HOME, ".cache/opencode/packages");

// Keys that occur inside tool definitions but are never tool ids. Without this
// the scanner would list `description`/`parameters` as tools, which is
// harmless for attribution (only observed ids are ever looked up) but
// misleading in the inventory.
const SCAN_KEY_DENYLIST = new Set([
  "description", "parameters", "execute", "args", "inputSchema", "schema",
  "title", "metadata", "type", "required", "properties", "tool",
]);

function builtinToolSet() {
  if (!builtinTools) {
    const raw = process.env.OPENCODE_SKILL_TRACKER_BUILTIN_TOOLS || DEFAULT_BUILTIN_TOOLS;
    builtinTools = new Set(
      raw
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean)
    );
  }
  return builtinTools;
}

// "@scope/name@1.2.3" -> { base: "@scope/name", version: "1.2.3" }
// "name@1.2.3"        -> { base: "name",         version: "1.2.3" }
// "name"              -> { base: "name",         version: null }
function pluginSpecParts(spec) {
  const at = spec.indexOf("@", 1); // index 1: a leading @ is part of the scope
  return at === -1
    ? { base: spec, version: null }
    : { base: spec.slice(0, at), version: spec.slice(at + 1) || null };
}

function readPackageMain(dir) {
  try {
    const pkg = JSON.parse(fs.readFileSync(path.join(dir, "package.json"), "utf8"));
    return typeof pkg.main === "string" && pkg.main ? pkg.main : null;
  } catch {
    return null;
  }
}

// A `file://…` or absolute spec is used as given; a `./` / `../` spec is relative
// to the **config directory**, which is what OpenCode documents and what people
// write next to `~/.config/opencode/opencode.json`. It is deliberately *not*
// relative to `process.cwd()`: OpenCode starts inside whichever project the user is
// in, so reading it from the CWD looked for the plugin inside that project, found
// nothing, and recorded a real plugin with no source and no surface.
// Returns null for anything that is not a path spec (i.e. an npm name).
function localPluginPath(spec) {
  if (spec.startsWith("file://")) return path.resolve(spec.slice("file://".length));
  if (path.isAbsolute(spec)) return spec;
  if (spec.startsWith(".")) return path.resolve(CFG_DIR, spec);
  return null;
}

// Resolves a config `plugin` entry to its package directory and entry file.
// Returns null when nothing usable is found — the caller then records the
// plugin without a surface rather than guessing.
function resolvePluginEntry(spec) {
  const file = localPluginPath(spec);
  if (file !== null) {
    return fs.existsSync(file)
      ? { dir: path.dirname(file), entry: file, name: file, version: null, source: "local" }
      : null;
  }

  const { base, version } = pluginSpecParts(spec);
  const inner = path.join(base, "package.json");
  const candidates = [];
  // Prefer the exact pinned version; only then fall back to whatever is cached.
  if (version) {
    candidates.push(path.join(PACKAGES_DIR, `${base}@${version}`, "node_modules", inner));
  }
  let dirs = [];
  try {
    dirs = fs.readdirSync(PACKAGES_DIR);
  } catch {
    return null;
  }
  for (const d of dirs.sort()) {
    if (d !== base && !d.startsWith(`${base}@`)) continue;
    candidates.push(path.join(PACKAGES_DIR, d, "node_modules", inner));
  }

  for (const pj of candidates) {
    if (!fs.existsSync(pj)) continue;
    const dir = path.dirname(pj);
    const main = readPackageMain(dir);
    const entry = main ? path.join(dir, main) : path.join(dir, "dist/index.js");
    return {
      dir,
      entry: fs.existsSync(entry) ? entry : null,
      name: version ? `${base}@${version}` : base,
      version,
      source: "npm",
    };
  }
  return null;
}

// Inside a `tool: { … }` object the tool ids are the keys at the shallowest level
// that has any. Anything deeper belongs to one tool's own definition:
// `args: { query: … }` names a parameter, and a `… ? NOISE : …` inside a
// description string is not a key at all. A regex could not tell those apart —
// and because the old one stopped capturing at the first `}`, a tool written
// *after* a nested object was lost as well. Sticky, so a match can only start
// where the walk says a key may begin.
const TOOL_OBJECT_RE = /\btool\s*:\s*\{/g;
const TOOL_KEY_RE = /([A-Za-z_][\w]*)\s*:/y;
const CMD_TEMPLATE_RE = /"([A-Za-z][\w:.-]+)"\s*:\s*\{\s*template\s*:/gs;
const CMD_BRACKET_RE = /command\s*\[\s*"([^"]+)"\s*\]\s*=/gs;
// `from "./x.js"` and `import("./x.js")` — the two ways a bundle shim reaches
// the chunk that actually registers the surface.
const RELATIVE_IMPORT_RE = /(?:\bfrom|\bimport)\s*\(?\s*["'](\.\/[^"']+)["']/g;

// One hop from the entry file, and no more. A published plugin's `main` is
// increasingly a shim that re-exports the chunk holding the surface
// (`opencode-mem@2.28.1`'s `dist/plugin.js` is 407 bytes:
// `const { OpenCodeMemPlugin } = await import("./index.js")`), so scanning only
// the entry reports `tools=[]` for a plugin that does register a tool — and every
// call it makes then lands in `(unknown)`. Crawling further would mean walking a
// dependency tree that is not ours to size, so the hop is single and bounded. The
// entry itself is never size-limited: DCP's bundle is 300 KiB and was already
// being read in full, and a cap that also applied to it silently loses a plugin
// that works today.
const PLUGIN_SCAN_HOPS_MAX = 3;
const PLUGIN_SCAN_HOP_MAX_BYTES = 256 * 1024;

// Index just past the closing quote of the string literal starting at `i`. The
// `${…}` of a template literal is not tracked: whatever it contains is inside a
// description, which is exactly the text we must not read keys out of.
function skipQuoted(src, i) {
  const quote = src[i];
  let j = i + 1;
  while (j < src.length) {
    const c = src[j];
    if (c === "\\") { j += 2; continue; }
    if (c === quote) return j + 1;
    j++;
  }
  return j;
}

// The tool ids in `src`. Inside a `tool: { … }` object the shallowest level that
// has keys at all is the registry; anything below it belongs to an individual
// tool's own definition (`args: { query: … }` names a parameter, and a `? :` in a
// description string is not a key). Shallowest-rather-than-first matters because
// DCP registers `tool:{ ...cond && { compress: … } }`, one level deeper.
function scanToolBlocks(src, tools) {
  let from = 0;
  for (;;) {
    TOOL_OBJECT_RE.lastIndex = from;
    const open = TOOL_OBJECT_RE.exec(src);
    if (!open) return;
    const byDepth = new Map();
    let depth = 1;
    let i = open.index + open[0].length;      // just past the `{` that opens it
    while (i < src.length && depth > 0) {
      const ch = src[i];
      if (ch === '"' || ch === "'" || ch === "`") { i = skipQuoted(src, i); continue; }
      if (ch === "{") { depth++; i++; continue; }
      if (ch === "}") { depth--; i++; continue; }
      TOOL_KEY_RE.lastIndex = i;
      const k = TOOL_KEY_RE.exec(src);
      if (k) {
        if (!SCAN_KEY_DENYLIST.has(k[1])) {
          if (!byDepth.has(depth)) byDepth.set(depth, []);
          byDepth.get(depth).push(k[1]);
        }
        i += k[0].length;
        continue;
      }
      i++;
    }
    const shallowest = Math.min(...byDepth.keys());
    if (byDepth.size) for (const t of byDepth.get(shallowest)) tools.add(t);
    from = i;   // the block's own contents hold no registry of their own
  }
}

function scanSurfaceText(src, tools, commands) {
  scanToolBlocks(src, tools);
  for (const m of src.matchAll(CMD_TEMPLATE_RE)) commands.add(m[1]);
  for (const m of src.matchAll(CMD_BRACKET_RE)) commands.add(m[1]);
  // Keep only namespaced command ids: the `template` shape alone also matches
  // unrelated objects that happen to carry a template key.
  for (const c of [...commands]) {
    if (!c.includes(":") && !c.includes("-")) commands.delete(c);
  }
}

// Best-effort static scan for a plugin's registered surface. Verified against
// the plugins installed here: DCP yields tool `compress` and command
// `dcp-compress`, conductor yields its six `conductor:*` commands, opencode-mem
// yields `memory` through its shim, and the notifier yields nothing. An
// unparsable shape yields nothing.
function scanPluginSurface(entry) {
  const tools = new Set();
  const commands = new Set();
  if (!entry) return { tools, commands };
  let src;
  try {
    src = fs.readFileSync(entry, "utf8");
  } catch {
    return { tools, commands };
  }
  scanSurfaceText(src, tools, commands);

  const root = path.resolve(path.dirname(entry));
  const visited = new Set([path.resolve(entry)]);
  let hops = 0;
  for (const m of src.matchAll(RELATIVE_IMPORT_RE)) {
    if (hops >= PLUGIN_SCAN_HOPS_MAX) break;
    const next = path.resolve(path.dirname(entry), m[1]);
    // Only inside the entry's own directory subtree — `../` leaves it, and a
    // sibling package in the same cache is not this plugin's surface to read.
    if (!next.startsWith(root + path.sep) || visited.has(next)) continue;
    visited.add(next);
    hops++;
    let extra;
    try {
      const st = fs.statSync(next);
      if (!st.isFile() || st.size > PLUGIN_SCAN_HOP_MAX_BYTES) continue;
      extra = fs.readFileSync(next, "utf8");
    } catch {
      continue;
    }
    scanSurfaceText(extra, tools, commands);
  }
  return { tools, commands };
}

// The local plugin directory is not in the config `plugin` array; OpenCode
// loads it implicitly, so it has to be enumerated here.
function localPluginSpecs() {
  try {
    return fs
      .readdirSync(path.join(CFG_DIR, "plugin"))
      .filter((f) => f.endsWith(".js") || f.endsWith(".mjs") || f.endsWith(".ts"))
      .sort()
      .map((f) => `file://${path.join(CFG_DIR, "plugin", f)}`);
  } catch {
    return [];
  }
}

// This module's own path, so the tracker can exclude itself structurally rather
// than by filename (renaming the file must not make it start recording itself).
const SELF_PATH =
  typeof import.meta.path === "string" && import.meta.path
    ? import.meta.path
    : import.meta.url && import.meta.url.startsWith("file://")
      ? import.meta.url.slice("file://".length)
      : "";

// Compares two paths through symlinks: the installed plugin directory is a
// symlink farm back to this repo, so a plain path comparison would miss the
// tracker's own file and it would start recording itself.
function samePath(a, b) {
  if (!a || !b) return false;
  try {
    return fs.realpathSync(a) === fs.realpathSync(b);
  } catch {
    try {
      return path.resolve(a) === path.resolve(b);
    } catch {
      return false;
    }
  }
}

function isSkippedPlugin(spec, resolved, exclusions) {
  const target = (resolved && resolved.entry) || spec;
  if (SELF_PATH && target && samePath(target, SELF_PATH)) return true;
  const hay = `${spec} ${(resolved && resolved.name) || ""}`;
  return exclusions.some((e) => e && hay.includes(e));
}

// Where a spec was read from, which is what decides whether its later absence
// means anything. Every OpenCode session reads the same global config and the
// same local plugin directory, so a row from those that this init did not see is
// a plugin the user removed. A project config is only read when OpenCode runs in
// that project, so those rows are never deleted from elsewhere — and an
// env-overridden spec set (tests, sandboxes) proves nothing about the real
// config, so it is never pruned either.
const SCOPE_GLOBAL = "global";
const SCOPE_LOCAL = "localdir";
const SCOPE_PROJECT = "project";
const SCOPE_OVERRIDE = "override";
// Rows written before `scope` existed are NULL. They are treated as global: the
// common case is right, and a wrong deletion costs only the inventory row — the
// usage rows stay, and the next session from that project re-adds the entry.
const SCOPE_PRUNABLE_SQL = "(scope IS NULL OR scope IN ('global','localdir'))";

function upsertInventory(name, resolved, skipped, surface, scope) {
  if (!db) return;
  try {
    db.query(UPSERT_PLUGIN_INVENTORY_SQL).run(
      String(name).slice(0, PLUGIN_NAME_MAX),
      resolved && resolved.version ? resolved.version : null,
      resolved ? resolved.source : null,
      skipped ? 1 : 0,
      JSON.stringify([...surface.tools].sort()),
      JSON.stringify([...surface.commands].sort()),
      scope
    );
  } catch (e) {
    log("err", "upsertInventory: " + errMsg(e));
  }
}

// `CREATE TABLE IF NOT EXISTS` never adds a column to a table that already
// exists, and on an upgrade this is the writer that runs first — so the column is
// added here as well as in `skill_db.ensure_schema()`. A second migrator winning
// the race shows up as "duplicate column name", which is success.
function ensureInventoryScope() {
  try {
    const cols = db.prepare("PRAGMA table_info(plugin_inventory)").all();
    if (cols.some((c) => c && c.name === "scope")) return;
    db.exec("ALTER TABLE plugin_inventory ADD COLUMN scope TEXT");
    log("info", "inventory: added the scope column");
  } catch (e) {
    log("err", "inventory scope: " + errMsg(e));
  }
}

// Specs, plus where each one came from. A config file that is missing or
// unparsable makes no claim at all: it contributes nothing, and `globalReadable`
// stays false — which is what stops a broken config from emptying the inventory.
function configPluginEntries() {
  const entries = [];
  const cfgRoot = path.resolve(CFG_DIR) + path.sep;
  let globalReadable = false;
  for (const file of configFiles()) {
    const json = readJsonConfig(file);
    if (!json) continue;
    const scope = path.resolve(file).startsWith(cfgRoot) ? SCOPE_GLOBAL : SCOPE_PROJECT;
    if (scope === SCOPE_GLOBAL) globalReadable = true;
    const list = Array.isArray(json.plugin) ? json.plugin : [];
    for (const s of list) if (typeof s === "string" && s) entries.push({ spec: s, scope });
  }
  return { entries, globalReadable };
}

// The least prunable label wins: a spec that a project config also lists must not
// become deletable just because the global config lists it too.
const SCOPE_RANK = { [SCOPE_PROJECT]: 3, [SCOPE_LOCAL]: 2, [SCOPE_GLOBAL]: 1 };
function mergeScopedEntries(entries) {
  const bySpec = new Map();
  for (const { spec, scope } of entries) {
    const held = bySpec.get(spec);
    if (held === undefined || (SCOPE_RANK[scope] || 0) > (SCOPE_RANK[held] || 0)) {
      bySpec.set(spec, scope);
    }
  }
  return bySpec;
}

// Drop the rows for plugins this session's *global* config and local plugin
// directory no longer list. Both are read by every session, so their absence here
// is the same as the user removing the plugin. Names go in as parameters, never
// interpolated.
function pruneInventory(keepNames) {
  if (!db) return;
  try {
    const keep = [...keepNames];
    const sql =
      `DELETE FROM plugin_inventory WHERE ${SCOPE_PRUNABLE_SQL}` +
      (keep.length ? ` AND plugin_name NOT IN (${keep.map(() => "?").join(",")})` : "");
    const res = db.query(sql).run(...keep);
    const n = res && typeof res.changes === "number" ? res.changes : 0;
    if (n) log("info", `inventory: pruned ${n} row(s) no longer listed by the global config`);
  } catch (e) {
    log("err", "inventory prune: " + errMsg(e));
  }
}

async function loadPlugins() {
  if (PLUGIN_DISABLED) {
    log("info", "plugin recording disabled via OPENCODE_SKILL_TRACKER_PLUGIN_DISABLE");
    return;
  }

  // 1. An explicit override always wins — even when empty — so tests never
  //    read the developer's real configuration. Read at call time (not at
  //    module load) so __selftest can set it before the factory runs.
  const envSpecs = process.env.OPENCODE_SKILL_TRACKER_PLUGINS;
  let entries, prunable;
  if (envSpecs !== undefined) {
    entries = envSpecs
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean)
      .map((spec) => ({ spec, scope: SCOPE_OVERRIDE }));
    prunable = false;   // an override says nothing about what is installed
  } else {
    const cfg = configPluginEntries();
    entries = cfg.entries;
    for (const spec of localPluginSpecs()) entries.push({ spec, scope: SCOPE_LOCAL });
    prunable = cfg.globalReadable;
  }

  const exclusions = [...PLUGIN_EXCLUDE_DEFAULT, ...PLUGIN_EXCLUDE_EXTRA];
  pluginSurface = new Map();
  toolToPlugin = new Map();
  commandToPlugin = new Map();

  let skippedCount = 0;
  const seenNames = new Set();
  for (const [spec, scope] of mergeScopedEntries(entries)) {
    const resolved = resolvePluginEntry(spec);
    const name = (resolved && resolved.name) || spec;
    seenNames.add(name);
    if (isSkippedPlugin(spec, resolved, exclusions)) {
      skippedCount++;
      upsertInventory(name, resolved, 1, { tools: new Set(), commands: new Set() }, scope);
      continue;
    }
    const surface = scanPluginSurface(resolved && resolved.entry);
    for (const t of surface.tools) if (!toolToPlugin.has(t)) toolToPlugin.set(t, name);
    for (const c of surface.commands) if (!commandToPlugin.has(c)) commandToPlugin.set(c, name);
    pluginSurface.set(name, surface);
    upsertInventory(name, resolved, 0, surface, scope);
  }

  if (prunable) pruneInventory(seenNames);

  log(
    "info",
    `plugins: ${pluginSurface.size} monitored, ${skippedCount} excluded, ` +
      `${toolToPlugin.size} tool(s), ${commandToPlugin.size} command(s)`
  );
}

// ---------------------------------------------------------------------------
// Frontmatter parser (hand-rolled, no yaml dependency)
// ---------------------------------------------------------------------------
export function parseFrontmatter(text) {
  const out = {};
  if (typeof text !== "string" || text.length === 0) return out;
  const src = text.replace(/^\uFEFF/, "");
  const m = /^---\r?\n([\s\S]*?)\r?\n---(?:\r?\n|$)/.exec(src);
  if (!m) return out;
  const lines = m[1].split(/\r?\n/);
  for (let i = 0; i < lines.length; i++) {
    const mm = /^([A-Za-z0-9_-]+)\s*:\s*(.*)$/.exec(lines[i]);
    if (!mm) continue;
    const key = mm[1];
    let val = mm[2].trim();
    if (val === ">" || val === "|" || val === ">-" || val === "|-") {
      // Folded / literal block scalar: consume following indented lines.
      const buf = [];
      while (i + 1 < lines.length && /^\s+/.test(lines[i + 1])) {
        buf.push(lines[++i].trim());
      }
      val = buf.join(" ");
    } else if (
      (val.startsWith('"') && val.endsWith('"')) ||
      (val.startsWith("'") && val.endsWith("'"))
    ) {
      val = val.slice(1, -1);
    }
    out[key] = val;
  }
  return out;
}

// ---------------------------------------------------------------------------
// Sanitization — applied to every free-text field that reaches the DB
// (permission `title` and tool `error`). Message bodies are never stored at
// all, so there is nothing of theirs to sanitize.
//
// Replace -> collapse -> truncate -> replace again (catches a secret that
// straddles the truncation boundary). Raw text is never persisted or logged.
//
// This is best-effort defence in depth: no pattern list is ever complete, so
// the real guarantee comes from only ever storing metadata, never message or
// tool-argument content.
// ---------------------------------------------------------------------------
const SECRET_PATTERNS = [
  /\bsk-[A-Za-z0-9_-]{8,}/g, // OpenAI-style
  /\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{10,}/g, // Stripe
  /\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}/g, // GitHub tokens
  /\bgithub_pat_[A-Za-z0-9_]{20,}/g, // GitHub fine-grained PAT
  /\bark-[A-Za-z0-9-]{16,}/g, // Volcengine (a live key exists in opencode.json)
  /\bAIza[A-Za-z0-9_-]{35}\b/g, // Google API key
  /\bxox[abprs]-[A-Za-z0-9-]{10,}/g, // Slack
  /\b(?:AKIA|ASIA)[A-Z0-9]{16}\b/g, // AWS access key id
  /\bBearer\s+[A-Za-z0-9._~+/-]{8,}=*/gi, // bearer tokens
  /\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*/g, // JWT
  /\b[A-Fa-f0-9]{32,}\b/g, // long hex
  /\b[A-Za-z0-9+/]{40,}={0,2}\b/g, // long base64
  /\b[A-Za-z0-9_-]{40,}\b/g, // long base64url (opaque tokens)
  /\b[a-z][a-z0-9+.-]*:\/\/[^\s/@:]+:[^\s/@]+@/gi, // URL with embedded credentials
  /-----BEGIN [A-Z ]*PRIVATE KEY-----/g, // PEM private key header
  // KEY=VALUE / KEY: VALUE for secret-looking keys. The `\b` before the key
  // is what keeps benign names such as `max_tokens` from matching.
  /\b[A-Za-z0-9_]*(?:SECRET|PASSWORD|PASSWD|API[_-]?KEY|APIKEY|PRIVATE[_-]?KEY)[A-Za-z0-9_]*\s*[=:]\s*\S+/gi,
  /\b(?:ACCESS[_-]?TOKEN|AUTH[_-]?TOKEN|REFRESH[_-]?TOKEN|TOKEN)\s*[=:]\s*\S+/gi,
];

export function sanitize(input, max = SANITIZE_MAX) {
  if (!input) return "";
  let t = String(input);
  for (const re of SECRET_PATTERNS) t = t.replace(re, "[REDACTED]");
  t = t.replace(/\s+/g, " ").trim();
  if (t.length > max) t = t.slice(0, max);
  for (const re of SECRET_PATTERNS) t = t.replace(re, "[REDACTED]");
  return t;
}

// ---------------------------------------------------------------------------
// Skill directory scan
// ---------------------------------------------------------------------------
function walkSkillFiles(root) {
  const out = [];
  const stack = [root];
  while (stack.length) {
    const dir = stack.pop();
    let entries;
    try {
      entries = fs.readdirSync(dir, { withFileTypes: true });
    } catch {
      continue;
    }
    for (const e of entries) {
      const full = path.join(dir, e.name);
      if (e.isDirectory()) stack.push(full);
      else if (e.isFile() && e.name === "SKILL.md") out.push(full);
    }
  }
  return out;
}

function scanSkills() {
  if (!db) return 0;
  let files = [];
  try {
    files = walkSkillFiles(SKILLS_DIR);
  } catch (e) {
    log("err", "scan walk: " + errMsg(e));
  }

  const upsert = db.query(
    `INSERT INTO skills (name, category, path, description) VALUES (?, ?, ?, ?)
     ON CONFLICT(path) DO UPDATE SET
       name = excluded.name,
       category = excluded.category,
       description = excluded.description,
       updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')`
  );

  let count = 0;
  let noName = 0;
  for (const file of files) {
    try {
      const dir = path.dirname(file);
      const fm = parseFrontmatter(fs.readFileSync(file, "utf8"));
      const rel = path.relative(SKILLS_DIR, dir);
      const category = rel.split(path.sep)[0] || null;
      const name = fm.name || path.basename(dir);
      if (!fm.name) noName++;
      upsert.run(name, category, dir, fm.description || null);
      count++;
    } catch (e) {
      log("err", `scan ${file}: ${errMsg(e)}`);
    }
  }

  refreshSkillIdCache();

  log("info", `scanned ${count} skills (${noName} without frontmatter name) from ${SKILLS_DIR}`);
  return count;
}

// Rebuild the name -> id cache used to resolve `skill_id` on writes. Called at
// the end of a scan and again if a write fails its foreign-key check, because
// the skills rows can disappear underneath a running session (e.g. a
// `skillt clear --all`).
function refreshSkillIdCache() {
  if (!db) return;
  try {
    skillIdByName = new Map();
    for (const row of db.query("SELECT id, name FROM skills").all()) {
      skillIdByName.set(row.name, row.id);
    }
  } catch (e) {
    log("err", "skill id cache: " + errMsg(e));
  }
}

// ---------------------------------------------------------------------------
// SQLite bootstrap
// ---------------------------------------------------------------------------
async function loadSqlite() {
  if (Database || sqliteUnavailable) return;
  try {
    const mod = await import("bun:sqlite");
    Database = mod.Database;
  } catch (e) {
    sqliteUnavailable = true;
    log("fatal", "bun:sqlite unavailable, tracker disabled: " + errMsg(e));
  }
}

async function init() {
  if (initDone) return;
  if (initInFlight) return initInFlight;
  initInFlight = doInit();
  try {
    await initInFlight;
  } finally {
    // Clear even on failure so a later call can retry instead of awaiting
    // this already-settled promise forever. `initDone` is set only by a
    // successful doInit(), so a transient failure stays retryable.
    initInFlight = null;
  }
}

async function doInit() {
  await loadSqlite();
  if (!Database) return;

  try {
    fs.mkdirSync(path.dirname(DB_PATH), { recursive: true, mode: 0o700 });
  } catch (e) {
    log("err", "mkdir db dir: " + errMsg(e));
  }

  try {
    db = new Database(DB_PATH, { create: true });
  } catch (e) {
    log("fatal", "cannot open db " + DB_PATH + ": " + errMsg(e));
    db = null;
    return;
  }

  for (const pragma of [
    "PRAGMA journal_mode = WAL",
    "PRAGMA busy_timeout = " + BUSY_TIMEOUT_MS,
    "PRAGMA synchronous = NORMAL",
    "PRAGMA foreign_keys = ON",
  ]) {
    try {
      db.exec(pragma);
    } catch (e) {
      log("err", `pragma "${pragma}": ` + errMsg(e));
    }
  }

  try {
    db.exec(TABLES_SQL);
  } catch (e) {
    // Without the base tables every later query throws. Disable the tracker
    // instead of letting the failure escape into OpenCode's plugin loader.
    log("fatal", "schema creation failed, tracker disabled: " + errMsg(e));
    try {
      db.close();
    } catch {
      /* ignore */
    }
    db = null;
    return;
  }

  ensureInventoryScope();

  try {
    db.exec(VIEWS_SQL);
  } catch (e) {
    log("err", "view creation failed (tables still usable): " + errMsg(e));
  }

  // The sibling opencode.db is world-readable (0644); do not inherit that.
  try {
    fs.chmodSync(DB_PATH, 0o600);
  } catch {
    /* best effort */
  }

  try {
    scanSkills();
  } catch (e) {
    log("err", "scanSkills: " + errMsg(e));
  }

  try {
    await loadMcpServers(pluginClient);
  } catch (e) {
    log("err", "loadMcpServers: " + errMsg(e));
  }

  try {
    await loadPlugins();
  } catch (e) {
    log("err", "loadPlugins: " + errMsg(e));
  }

  initDone = true;
  log(
    "info",
    `initialized (db=${DB_PATH}, tool="${SKILL_TOOL}", mcp_servers=${mcpServers.size}, ` +
      `plugins=${pluginSurface.size})`
  );
}

// ---------------------------------------------------------------------------
// Git branch resolution (push from vcs.branch.updated, lazy pull via git)
// ---------------------------------------------------------------------------
async function resolveBranch(dir) {
  if (!dir) return null;
  if (branchByDir.has(dir)) return branchByDir.get(dir);

  let branch = null;
  const ac = new AbortController();
  const timer = setTimeout(() => ac.abort(), GIT_TIMEOUT_MS);
  try {
    const proc = spawnGit(
      ["git", "-C", dir, "rev-parse", "--abbrev-ref", "HEAD"],
      ac.signal
    );
    const exitCode = await proc.exited;
    if (exitCode === 0) {
      const t = String((await new Response(proc.stdout).text()) || "").trim();
      if (t && t !== "HEAD") branch = t;
    }
  } catch (e) {
    debug("branch resolve failed: " + errMsg(e));
  } finally {
    // Always clear the timer: on success it is stale, on timeout the abort
    // has already killed the child (exitCode 143).
    clearTimeout(timer);
  }

  // Cache failures as null too, so a broken dir is never retried in a hot loop.
  setCapped(branchByDir, dir, branch);
  return branch;
}

// ---------------------------------------------------------------------------
// Write helpers — used by every detection path
// ---------------------------------------------------------------------------
async function resolveWriteContext(sessionID, projectPath) {
  const ctx = (sessionID && sessionCtx.get(sessionID)) || {};
  const dir = projectPath || ctx.directory || pluginDir || null;
  const branch = await resolveBranch(dir); // resolved before the write
  return { ctx, dir, branch };
}

function buildMetadata(ctx, branch, callID, source, toolId, meta) {
  // Metadata only — never message bodies and never tool-argument values.
  // `title` and `error` are free text that routinely echoes inputs (commands,
  // stderr, file contents), so both go through sanitize().
  const metadata = {
    tool: toolId,
    call_id: callID ?? null,
    agent: ctx.agent ?? null,
    model: ctx.model ?? null,
    branch: branch ?? null,
    source,
  };
  if (meta && meta.title) metadata.title = sanitize(meta.title, META_TEXT_MAX);
  if (meta && meta.error) metadata.error = sanitize(meta.error, META_TEXT_MAX);
  return metadata;
}

function isForeignKeyError(e) {
  return /FOREIGN KEY constraint failed/i.test(errMsg(e));
}

async function recordUsage(
  { skillName, sessionID, projectPath, triggerType, status, callID, durationMs, meta }
) {
  if (!db) return;
  const write = async () => {
    const { ctx, dir, branch } = await resolveWriteContext(sessionID, projectPath);
    const metadata = buildMetadata(
      ctx,
      branch,
      callID,
      (meta && meta.source) || triggerType,
      SKILL_TOOL,
      meta
    );
    db.query(UPSERT_USAGE_SQL).run(
      skillIdByName.get(skillName) ?? null,
      skillName || "unknown",
      sessionID ?? null,
      dir,
      triggerType,
      status,
      durationMs ?? null,
      callID ?? null,
      JSON.stringify(metadata)
    );
  };

  try {
    await write();
    debug(`recorded ${skillName} (${triggerType}/${status}) session=${sessionID} call=${callID}`);
  } catch (e) {
    // The skill-id cache is filled at init. If the `skills` rows vanish while
    // this session is still running (a `skillt clear --all`, or a TUI delete),
    // every subsequent insert fails its FK check and used to be swallowed —
    // silently dropping all further usage for the session. Rescan and retry.
    if (isForeignKeyError(e)) {
      refreshSkillIdCache();
      try {
        await write();
        debug(`recorded ${skillName} after skill-id rescan`);
        return;
      } catch (e2) {
        log("err", "recordUsage retry: " + errMsg(e2));
        return;
      }
    }
    log("err", "recordUsage: " + errMsg(e));
  }
}

// Sibling of recordUsage for the MCP table. `tool` may be null on the
// permission paths, which only know the server; the '*' sentinel keeps the
// column NOT NULL and lets a later tool-level observation upgrade it.
async function recordMcpUsage(
  { server, tool, sessionID, projectPath, triggerType, status, callID, durationMs, argNames, meta }
) {
  if (!db || MCP_DISABLED || !server) return;
  try {
    const { ctx, dir, branch } = await resolveWriteContext(sessionID, projectPath);
    const toolName = tool || "*";
    const metadata = buildMetadata(
      ctx,
      branch,
      callID,
      (meta && meta.source) || triggerType,
      `${server}_${toolName}`,
      meta
    );

    db.query(UPSERT_MCP_SQL).run(
      server,
      toolName,
      sessionID ?? null,
      dir,
      triggerType,
      status,
      durationMs ?? null,
      callID ?? null,
      argNames ?? null,
      JSON.stringify(metadata)
    );
    debug(`recorded mcp ${server}/${toolName} (${triggerType}/${status}) session=${sessionID} call=${callID}`);
  } catch (e) {
    log("err", "recordMcpUsage: " + errMsg(e));
  }
}

// Sibling of recordMcpUsage for the plugin table. `plugin` is always set by
// classify(): either a resolved plugin name or UNKNOWN_PLUGIN, never null.
async function recordPluginUsage(
  { plugin, kind, item, sessionID, projectPath, triggerType, status, callID, durationMs, meta }
) {
  if (!db || PLUGIN_DISABLED || !item) return;
  try {
    const { ctx, dir, branch } = await resolveWriteContext(sessionID, projectPath);
    const pluginName = String(plugin || UNKNOWN_PLUGIN).slice(0, PLUGIN_NAME_MAX);
    const itemName = String(item).slice(0, PLUGIN_ITEM_MAX);
    const metadata = buildMetadata(
      ctx,
      branch,
      callID,
      (meta && meta.source) || triggerType,
      `${pluginName}/${itemName}`,
      meta
    );

    db.query(UPSERT_PLUGIN_SQL).run(
      pluginName,
      kind,
      itemName,
      sessionID ?? null,
      dir,
      triggerType,
      status,
      durationMs ?? null,
      callID ?? null,
      JSON.stringify(metadata)
    );
    debug(
      `recorded plugin ${pluginName}/${itemName} (${triggerType}/${status}) session=${sessionID} call=${callID}`
    );
  } catch (e) {
    log("err", "recordPluginUsage: " + errMsg(e));
  }
}

function errMsg(e) {
  return e && e.message ? e.message : String(e);
}

// Wrap a hook body so it can never throw into OpenCode.
function safe(name, fn) {
  return async (...args) => {
    try {
      return await fn(...args);
    } catch (e) {
      log("err", `${name}: ${errMsg(e)}`);
    }
  };
}

// ---------------------------------------------------------------------------
// Plugin entry point
// ---------------------------------------------------------------------------
async function skillTrackerPlugin(input) {
  const { directory, worktree, client } = input || {};
  pluginDir = directory || worktree || null;
  pluginClient = client || null;

  if (DISABLED) {
    log("info", "disabled via OPENCODE_SKILL_TRACKER_DISABLE");
    return {};
  }

  // The factory must never reject: a throwing plugin load can disrupt OpenCode.
  try {
    await init();
  } catch (e) {
    log("err", "init: " + errMsg(e));
  }
  if (!db) {
    log("info", "no database available; hooks are no-ops");
    return {};
  }

  return {
    // -- primary path -------------------------------------------------------
    "tool.execute.before": safe("tool.execute.before", async (hookInput, hookOutput) => {
      if (!hookInput) return;
      const c = classify(hookInput.tool);
      if (!c) return;
      const args = hookOutput && hookOutput.args;
      const name =
        c.kind === "skill" && args && typeof args.name === "string" ? args.name : null;
      debug(`before ${c.kind} callID=${hookInput.callID} name=${name || c.tool}`);
      setCapped(callCtx, callKey(hookInput.sessionID, hookInput.callID), {
        sessionID: hookInput.sessionID,
        startMs: Date.now(),
        kind: c.kind,
        skillName: name,
        server: c.server ?? null,
        tool: c.tool ?? null,
        item: c.item ?? null,
        plugin: c.plugin ?? null,
        argNames: c.kind === "mcp" ? mcpArgNames(args) : null,
      });
    }),

    "tool.execute.after": safe("tool.execute.after", async (hookInput) => {
      if (!hookInput) return;
      const c = classify(hookInput.tool);
      if (!c) return;
      const key = callKey(hookInput.sessionID, hookInput.callID);
      const ctx = callCtx.get(key);
      const duration = ctx && ctx.startMs ? Date.now() - ctx.startMs : null;
      callCtx.delete(key);

      if (c.kind === "mcp") {
        await recordMcpUsage({
          server: c.server,
          tool: c.tool,
          sessionID: hookInput.sessionID,
          triggerType: "tool_call",
          status: "success",
          callID: hookInput.callID,
          durationMs: duration,
          argNames: (ctx && ctx.argNames) || mcpArgNames(hookInput.args),
          meta: { source: "hook" },
        });
        return;
      }

      if (c.kind === "plugin") {
        await recordPluginUsage({
          plugin: c.plugin,
          kind: c.itemKind,
          item: c.item,
          sessionID: hookInput.sessionID,
          triggerType: "tool_call",
          status: "success",
          callID: hookInput.callID,
          durationMs: duration,
          meta: { source: "hook" },
        });
        return;
      }

      const name =
        (hookInput.args && typeof hookInput.args.name === "string"
          ? hookInput.args.name
          : null) ||
        (ctx && ctx.skillName) ||
        "unknown";
      await recordUsage(
        {
          skillName: name,
          sessionID: hookInput.sessionID,
          triggerType: "tool_call",
          status: "success",
          callID: hookInput.callID,
          durationMs: duration,
          meta: { source: "hook" },
        }
      );
    }),

    // -- permission-level denials ------------------------------------------
    "permission.ask": safe("permission.ask", async (hookInput, hookOutput) => {
      if (!hookInput) return;
      const c = classifyPermission(hookInput);
      if (!c) return;
      if (!hookOutput || hookOutput.status !== "deny") return;

      if (c.kind === "mcp") {
        await recordMcpUsage({
          server: c.server,
          tool: c.tool,
          sessionID: hookInput.sessionID,
          triggerType: "permission_denied",
          status: "denied",
          callID: hookInput.callID ?? null,
          durationMs: null,
          argNames: null,
          meta: { source: "permission.ask", title: hookInput.title },
        });
        return;
      }

      if (c.kind === "plugin") {
        await recordPluginUsage({
          plugin: c.plugin,
          kind: c.itemKind,
          item: c.item,
          sessionID: hookInput.sessionID,
          triggerType: "permission_denied",
          status: "denied",
          callID: hookInput.callID ?? null,
          durationMs: null,
          meta: { source: "permission.ask", title: hookInput.title },
        });
        return;
      }

      const raw = hookInput.pattern || (hookInput.metadata && hookInput.metadata.name) || "unknown";
      const name = Array.isArray(raw) ? raw[0] : raw;
      await recordUsage(
        {
          skillName: String(name),
          sessionID: hookInput.sessionID,
          triggerType: "permission_denied",
          status: "denied",
          callID: hookInput.callID ?? null,
          durationMs: null,
          meta: { source: "permission.ask", title: hookInput.title },
        }
      );
    }),

    // Plugin-provided commands are namespaced by the plugin that registers them
    // (conductor:status, dcp-compress). There is no command.execute.after, so a
    // command can only ever be recorded as 'unknown' status — and only when the
    // init scan positively resolved its owner, which is what keeps OpenCode's
    // own builtin commands out of the table.
    "command.execute.before": safe("command.execute.before", async (hookInput) => {
      if (!hookInput) return;
      const cmd = hookInput.command;
      if (typeof cmd !== "string" || !cmd) return;
      const plugin = commandToPlugin.get(cmd);
      if (!plugin) return;
      await recordPluginUsage({
        plugin,
        kind: "command",
        item: cmd.slice(0, PLUGIN_ITEM_MAX),
        sessionID: hookInput.sessionID,
        triggerType: "command_call",
        status: "unknown",
        // NULL on purpose. SQLite treats NULLs as distinct in UNIQUE
        // (session_id, call_id), so the upsert degrades to a plain INSERT and
        // every invocation gets its own row — which is what "count how often
        // this command ran" requires. A synthetic id here would collapse them.
        callID: null,
        durationMs: null,
        meta: { source: "command.execute.before" },
      });
    }),

    // -- fallback + context -------------------------------------------------
    event: safe("event", async ({ event }) => {
      if (!event || !event.type) return;
      const props = event.properties || {};

      switch (event.type) {
        case "session.created":
        case "session.updated": {
          const info = props.info;
          if (info && info.id) {
            mergeSession(info.id, {
              directory: info.directory || undefined,
              title: info.title,
            });
          }
          return;
        }

        case "vcs.branch.updated": {
          const branch = props.branch ?? null;
          if (pluginDir) setCapped(branchByDir, pluginDir, branch);
          return;
        }

        case "message.part.updated": {
          const part = props.part;
          if (!part || part.type !== "tool") return;
          const c = classify(part.tool);
          if (!c) return;
          const st = part.state || {};
          const inputName =
            st.input && typeof st.input.name === "string" ? st.input.name : null;

          if (st.status === "pending" || st.status === "running") {
            const key = callKey(part.sessionID, part.callID);
            if (!callCtx.has(key)) {
              setCapped(callCtx, key, {
                sessionID: part.sessionID,
                startMs: st.time && st.time.start ? st.time.start : Date.now(),
                kind: c.kind,
                skillName: inputName,
                server: c.server ?? null,
                tool: c.tool ?? null,
                item: c.item ?? null,
                plugin: c.plugin ?? null,
                argNames: c.kind === "mcp" ? mcpArgNames(st.input) : null,
              });
            }
            return;
          }

          if (st.status === "completed" || st.status === "error") {
            // The pending/running branch above may have parked an entry; drop it
            // here so a long session does not accumulate finished calls.
            callCtx.delete(callKey(part.sessionID, part.callID));
            const duration =
              st.time && st.time.start && st.time.end ? st.time.end - st.time.start : null;
            const status = st.status === "completed" ? "success" : "error";
            const error = st.status === "error" ? st.error : undefined;

            if (c.kind === "mcp") {
              await recordMcpUsage({
                server: c.server,
                tool: c.tool,
                sessionID: part.sessionID,
                triggerType: "event_detected",
                status,
                callID: part.callID,
                durationMs: duration,
                argNames: mcpArgNames(st.input),
                meta: { source: "event", error },
              });
              return;
            }

            if (c.kind === "plugin") {
              await recordPluginUsage({
                plugin: c.plugin,
                kind: c.itemKind,
                item: c.item,
                sessionID: part.sessionID,
                triggerType: "event_detected",
                status,
                callID: part.callID,
                durationMs: duration,
                meta: { source: "event", error },
              });
              return;
            }

            await recordUsage(
              {
                skillName: inputName || "unknown",
                sessionID: part.sessionID,
                triggerType: "event_detected",
                status,
                callID: part.callID,
                durationMs: duration,
                meta: { source: "event", error },
              }
            );
          }
          return;
        }

        // OpenCode 1.18.33 emits `permission.asked`; `permission.updated` is the
        // name the SDK types declare. Both are parked: a rejection can only be
        // attributed once the reply arrives, and a builtin refusal must stay
        // unrecorded — so parking cannot depend on the payload classifying.
        //
        // The call being refused is at `tool.callID` (an object) on the 1.18.33
        // event and at `callID` on the SDK's shape; read both.
        case "permission.asked":
        case "permission.updated": {
          const perm = props; // properties is the Permission object
          const id = perm.id ?? perm.permissionID;
          if (!id) return;
          const c = classifyPermission(perm);
          const raw =
            perm.pattern ?? perm.patterns ??
            (perm.metadata && perm.metadata.name) ?? "unknown";
          const name = String(Array.isArray(raw) ? raw[0] : raw);
          setCapped(pendingPerms, id, {
            kind: c ? c.kind : null,
            itemKind: c && c.itemKind ? c.itemKind : "tool",
            name: name,
            server: (c && c.server) ?? null,
            tool: (c && c.tool) ?? null,
            item: (c && c.item) ?? null,
            plugin: (c && c.plugin) ?? null,
            callID: (perm.tool && perm.tool.callID) ?? perm.callID ?? null,
            sessionID: perm.sessionID ?? null,
          });
          return;
        }

        case "permission.replied": {
          // 1.18.33 replies with `requestID` + `reply`; the older spelling is
          // `permissionID` + `response`. A miss here used to be silent, which is
          // how every real rejection went unrecorded.
          const id = props.requestID ?? props.permissionID;
          const parked = id ? pendingPerms.get(id) : null;
          if (!parked) return;
          pendingPerms.delete(id);
          const response = String(props.reply ?? props.response ?? "").toLowerCase();
          const denied = DENIED_REPLIES.includes(response);
          debug(`permission.replied response=${response} denied=${denied}`);
          if (!denied) return;

          const sid = props.sessionID ?? parked.sessionID ?? null;
          const perm = attributeDenial(parked, sid);
          if (!perm || !perm.kind) return; // builtin, or nothing attributable

          // Terminal: the tool never ran, so `tool.execute.after` will not come
          // and the parked start time has no further use.
          if (parked.callID) callCtx.delete(callKey(sid, parked.callID));

          if (perm.kind === "mcp") {
            await recordMcpUsage({
              server: perm.server,
              tool: perm.tool,
              sessionID: sid,
              triggerType: "permission_denied",
              status: "denied",
              callID: perm.callID,
              durationMs: null,
              argNames: null,
              meta: { source: "permission.replied" },
            });
            return;
          }
          if (perm.kind === "plugin") {
            await recordPluginUsage({
              plugin: perm.plugin,
              kind: perm.itemKind || "tool",
              item: perm.item,
              sessionID: sid,
              triggerType: "permission_denied",
              status: "denied",
              callID: perm.callID,
              durationMs: null,
              meta: { source: "permission.replied" },
            });
            return;
          }
          await recordUsage(
            {
              skillName: perm.name,
              sessionID: sid,
              triggerType: "permission_denied",
              status: "denied",
              callID: perm.callID,
              durationMs: null,
              meta: { source: "permission.replied" },
            }
          );
          return;
        }

        default:
          return;
      }
    }),

    // -- context only (never written directly) ------------------------------
    // Captures agent/model for the session. The message text is deliberately
    // not read at all: storing even a sanitized snippet of it would contradict
    // the "no message bodies" contract the README makes.
    "chat.message": safe("chat.message", async (hookInput) => {
      const sid = hookInput && hookInput.sessionID;
      if (!sid) return;
      const model = hookInput.model
        ? `${hookInput.model.providerID}/${hookInput.model.modelID}`
        : undefined;
      const prev = sessionCtx.get(sid) || {};
      setCapped(sessionCtx, sid, {
        ...prev,
        agent: hookInput.agent ?? prev.agent,
        model: model ?? prev.model,
      });
    }),

    "chat.params": safe("chat.params", async (hookInput) => {
      const sid = hookInput && hookInput.sessionID;
      if (!sid || !hookInput.model) return;
      const model = `${hookInput.model.providerID ?? ""}/${
        hookInput.model.id ?? hookInput.model.modelID ?? ""
      }`;
      mergeSession(sid, { model });
    }),

    dispose: safe("dispose", async () => {
      sessionCtx.clear();
      callCtx.clear();
      branchByDir.clear();
      pendingPerms.clear();
      mcpServers = new Set();
      pluginSurface = new Map();
      toolToPlugin = new Map();
      commandToPlugin = new Map();
      builtinTools = null;
      try {
        if (db) db.close();
      } catch {
        /* ignore */
      }
      db = null;
      initDone = false;
      log("info", "disposed");
    }),
  };
}

// OpenCode's plugin loader treats EVERY exported function in this module as a
// plugin factory — it only stops enumerating when the default export is an
// object carrying `server`. This file also exports parseFrontmatter / sanitize /
// __selftest, so a bare `export default skillTrackerPlugin` made the loader call
// `__selftest` as a factory; its isolation guard threw and the whole plugin
// failed to load (nothing was ever recorded). The `{ id, server }` form is the
// SDK's multi-export contract: the loader detects `server` and calls only that.
export default { id: "skill-tracker", server: skillTrackerPlugin };

// ---------------------------------------------------------------------------
// Self-test — run without OpenCode:
//   OPENCODE_SKILL_TRACKER_DB=/tmp/st.db \
//     bun -e 'const m=await import("<path>/skill-tracker.js"); process.exit((await m.__selftest())?0:1)'
// ---------------------------------------------------------------------------
export async function __selftest() {
  // Isolation guard: a selftest must NEVER touch the production database.
  // Without this, running the selftest with no env var writes synthetic rows
  // into the user's real usage history.
  const defaultDb = path.join(HOME, ".local", "share", "opencode", "skill-usage.db");
  const envDb = process.env.OPENCODE_SKILL_TRACKER_DB;
  if (!envDb || path.resolve(envDb) === path.resolve(defaultDb)) {
    throw new Error(
      "Selftest requires isolated database: set OPENCODE_SKILL_TRACKER_DB to a temp .db path"
    );
  }

  const results = [];
  const assert = (cond, label) => {
    results.push({ ok: !!cond, label });
    if (!cond) log("err", "selftest FAIL: " + label);
  };

  // Pin the MCP server set so detection never reads the developer's real
  // opencode.json. Two servers, one a prefix of the other, to exercise the
  // longest-prefix match.
  process.env.OPENCODE_SKILL_TRACKER_MCP_SERVERS = "test-server,test-server-extra";

  // Pin the plugin set to a throwaway local plugin so the init scan is
  // exercised hermetically — never the developer's real plugins, and never
  // their real `plugin` config array. The file is removed at the end.
  const selftestPluginPath = path.join(
    process.env.TMPDIR || "/tmp",
    `skill-tracker-selftest-${process.pid}.js`
  );
  fs.writeFileSync(
    selftestPluginPath,
    'export const SelftestPlugin = async () => ({\n' +
      '  tool: { selftest_tool: { description: "x", parameters: {}, execute: async () => {} } },\n' +
      '  command: { "selftest:run": { template: "run" } },\n' +
      '});\n'
  );
  process.env.OPENCODE_SKILL_TRACKER_PLUGINS = `file://${selftestPluginPath}`;

  // Stub git: never spawn a subprocess, always report a branch.
  const prevSpawnGit = spawnGit;
  spawnGit = () => ({
    stdout: new Response("test-branch").body,
    exited: Promise.resolve(0),
  });

  const plugin = await skillTrackerPlugin({
    directory: "/tmp/selftest-proj",
    worktree: "/tmp/selftest-proj",
    client: {},
  });

  assert(db != null, "db initialized");
  if (!db) {
    // Bail before any db.query() so a failed init reports cleanly instead of
    // throwing out of the selftest.
    spawnGit = prevSpawnGit;
    try {
      fs.unlinkSync(selftestPluginPath);
    } catch {
      /* ignore */
    }
    console.log("FATAL: no database available; selftest aborted");
    return false;
  }

  const tables = db
    .query("SELECT name FROM sqlite_master WHERE type='table'")
    .all()
    .map((r) => r.name);
  assert(
    tables.includes("skills") && tables.includes("skill_usage") && tables.includes("mcp_usage"),
    "tables exist"
  );
  assert(
    tables.includes("plugin_usage") && tables.includes("plugin_inventory"),
    "plugin tables exist"
  );

  const fm = parseFrontmatter("---\nname: foo\ndescription: bar: baz\n---\nbody");
  assert(fm.name === "foo" && fm.description === "bar: baz", "frontmatter parse");

  const s = sanitize("key ark-00000000-0000-0000-0000-000000000000 and sk-abcdefgh12345678");
  assert(s.includes("[REDACTED]") && !s.includes("ark-00000000") && s.length <= 120, "sanitize redacts");

  // Common token formats that the original pattern list missed.
  const s2 = sanitize(
    [
      "gho_" + "A".repeat(30), // GitHub OAuth token
      "AIza" + "B".repeat(35), // Google API key
      "xoxb-1234567890-abcdef", // Slack
      "SECRET_KEY=hunter2", // .env-style
      "AKIAIOSFODNN7EXAMPLE", // AWS access key id
    ].join(" ")
  );
  assert(
    !/gho_|AIza|xoxb-|hunter2|AKIA/.test(s2),
    "sanitize covers GitHub/Google/Slack/.env/AWS token formats"
  );

  const skillCount = db.query("SELECT COUNT(*) AS c FROM skills").get().c;
  assert(skillCount > 0, `skills scanned (${skillCount})`);

  const sid = "sess-test-1";
  const cid = "call-test-1";
  // The text part is passed on purpose: the handler must ignore message
  // content entirely, so nothing from it may reach the DB (asserted below).
  await plugin["chat.message"](
    { sessionID: sid, agent: "build", model: { providerID: "p", modelID: "m" } },
    { parts: [{ type: "text", text: "please use the ark-00000000-0000-0000-0000-000000000000 skill" }] }
  );
  await plugin["tool.execute.before"]({ tool: "skill", sessionID: sid, callID: cid }, { args: { name: "brainstorming" } });
  await plugin["tool.execute.after"](
    { tool: "skill", sessionID: sid, callID: cid, args: { name: "brainstorming" } },
    { title: "", output: "", metadata: {} }
  );

  let rows = db.query("SELECT * FROM skill_usage WHERE session_id=? AND call_id=?").all(sid, cid);
  assert(rows.length === 1, "before+after => exactly 1 row");
  assert(rows[0].status === "success" && rows[0].trigger_type === "tool_call", "row is success/tool_call");
  assert(rows[0].duration_ms != null, "duration recorded");
  const meta = JSON.parse(rows[0].metadata);
  assert(!("summary" in meta), "message body must never be stored in metadata");
  assert(!rows[0].metadata.includes("ark-00000000"), "no message text may reach the DB");
  assert(meta.branch === "test-branch", "branch captured via git");
  assert(meta.agent === "build" && meta.model === "p/m", "agent+model captured");

  // Event fallback for the SAME callID must not create a second row.
  await plugin.event({
    event: {
      type: "message.part.updated",
      properties: {
        part: {
          id: "p1", sessionID: sid, messageID: "m1", type: "tool", callID: cid, tool: "skill",
          state: { status: "completed", input: { name: "brainstorming" }, output: "", title: "", metadata: {}, time: { start: 1000, end: 1500 } },
        },
      },
    },
  });
  rows = db.query("SELECT * FROM skill_usage WHERE session_id=? AND call_id=?").all(sid, cid);
  assert(rows.length === 1, "event fallback dedups => still 1 row");

  // Event-only path (no hooks fired) for a new call, in error state.
  const cid2 = "call-test-2";
  await plugin.event({
    event: {
      type: "message.part.updated",
      properties: {
        part: {
          id: "p2", sessionID: sid, messageID: "m2", type: "tool", callID: cid2, tool: "skill",
          state: { status: "error", input: { name: "docker-env-normalization" }, error: "boom sk-abcdefgh12345678", time: { start: 2000, end: 2500 } },
        },
      },
    },
  });
  const r2 = db.query("SELECT * FROM skill_usage WHERE call_id=?").get(cid2);
  assert(
    r2 && r2.status === "error" && r2.trigger_type === "event_detected" && r2.duration_ms === 500,
    "event error path"
  );
  // Tool errors echo inputs, so they are sanitized before being stored.
  const errMeta = JSON.parse(r2.metadata);
  assert(
    errMeta.error && errMeta.error.includes("[REDACTED]") && !errMeta.error.includes("sk-abcdefgh"),
    "error text is sanitized before storage"
  );

  // Permission denial via hook.
  await plugin["permission.ask"](
    { type: "skill", sessionID: sid, callID: "call-perm-1", pattern: "java-knowledge-sharded-audit", title: "Skill" },
    { status: "deny" }
  );
  const r3 = db.query("SELECT * FROM skill_usage WHERE call_id=?").get("call-perm-1");
  assert(r3 && r3.status === "denied" && r3.trigger_type === "permission_denied", "permission deny recorded");

  // Permission denial via user rejection event.
  await plugin.event({
    event: {
      type: "permission.updated",
      properties: { id: "perm-9", type: "skill", sessionID: sid, messageID: "m3", callID: "call-perm-2", title: "Skill", metadata: {}, time: { created: 0 }, pattern: "resume-evidence-chain" },
    },
  });
  await plugin.event({
    event: { type: "permission.replied", properties: { sessionID: sid, permissionID: "perm-9", response: "reject" } },
  });
  const r4 = db.query("SELECT * FROM skill_usage WHERE call_id=?").get("call-perm-2");
  assert(r4 && r4.status === "denied", "permission.replied rejection recorded");

  // --- MCP ---------------------------------------------------------------
  const mSid = "sess-test-mcp";

  // before+after on an MCP tool: server/tool split, arg NAMES only.
  const mCid = "call-mcp-1";
  const mArgs = { identifier: "ARGVALUE-ONE", query: "ARGVALUE-TWO" };
  await plugin["tool.execute.before"](
    { tool: "test-server_read_note", sessionID: mSid, callID: mCid },
    { args: mArgs }
  );
  await plugin["tool.execute.after"](
    { tool: "test-server_read_note", sessionID: mSid, callID: mCid, args: mArgs },
    { title: "", output: "", metadata: {} }
  );
  const m1 = db.query("SELECT * FROM mcp_usage WHERE session_id=? AND call_id=?").get(mSid, mCid);
  assert(m1 != null, "mcp before+after => 1 row");
  assert(
    m1 && m1.server_name === "test-server" && m1.tool_name === "read_note",
    "mcp server+tool split"
  );
  assert(
    m1 && m1.status === "success" && m1.trigger_type === "tool_call",
    "mcp row is success/tool_call"
  );
  assert(m1 && m1.duration_ms != null, "mcp duration recorded");
  assert(m1 && m1.arg_names === '["identifier","query"]', "mcp records arg names only");
  const m1raw = m1 ? JSON.stringify(m1) : "";
  assert(
    !m1raw.includes("ARGVALUE-ONE") && !m1raw.includes("ARGVALUE-TWO"),
    "mcp never stores arg values"
  );

  // Longest-prefix match: `test-server-extra` must beat `test-server`.
  await plugin["tool.execute.before"](
    { tool: "test-server-extra_ping", sessionID: mSid, callID: "call-mcp-2" },
    { args: {} }
  );
  await plugin["tool.execute.after"](
    { tool: "test-server-extra_ping", sessionID: mSid, callID: "call-mcp-2", args: {} },
    {}
  );
  const m2 = db.query("SELECT * FROM mcp_usage WHERE call_id=?").get("call-mcp-2");
  assert(
    m2 && m2.server_name === "test-server-extra" && m2.tool_name === "ping",
    "mcp longest server prefix wins"
  );

  // A tool name that itself contains underscores must survive intact.
  await plugin["tool.execute.before"](
    { tool: "test-server_list_memory_projects", sessionID: mSid, callID: "call-mcp-3" },
    { args: { project: "x" } }
  );
  await plugin["tool.execute.after"](
    { tool: "test-server_list_memory_projects", sessionID: mSid, callID: "call-mcp-3", args: { project: "x" } },
    {}
  );
  const m3 = db.query("SELECT * FROM mcp_usage WHERE call_id=?").get("call-mcp-3");
  assert(
    m3 && m3.server_name === "test-server" && m3.tool_name === "list_memory_projects",
    "mcp tool name keeps its underscores"
  );

  // Builtin tools must never land in mcp_usage.
  await plugin["tool.execute.before"](
    { tool: "bash", sessionID: mSid, callID: "call-builtin" },
    { args: { command: "echo hi" } }
  );
  await plugin["tool.execute.after"](
    { tool: "bash", sessionID: mSid, callID: "call-builtin", args: { command: "echo hi" } },
    {}
  );
  const builtinRows = db.query("SELECT COUNT(*) AS c FROM mcp_usage WHERE call_id=?").get("call-builtin").c;
  assert(builtinRows === 0, "builtin tools are not recorded as mcp");

  // Event-only MCP path in error state.
  await plugin.event({
    event: {
      type: "message.part.updated",
      properties: {
        part: {
          id: "pm1", sessionID: mSid, messageID: "mm1", type: "tool",
          callID: "call-mcp-ev", tool: "test-server_write_note",
          state: { status: "error", input: { title: "t" }, error: "boom", time: { start: 10, end: 30 } },
        },
      },
    },
  });
  const mEv = db.query("SELECT * FROM mcp_usage WHERE call_id=?").get("call-mcp-ev");
  assert(
    mEv && mEv.status === "error" && mEv.trigger_type === "event_detected" && mEv.duration_ms === 20,
    "mcp event error path"
  );

  // MCP permission denial, expressed as the mcp:<server>:* namespace.
  await plugin["permission.ask"](
    { type: "mcp:test-server:*", sessionID: mSid, callID: "call-mcp-perm", pattern: "mcp:test-server:*", title: "MCP" },
    { status: "deny" }
  );
  const mPerm = db.query("SELECT * FROM mcp_usage WHERE call_id=?").get("call-mcp-perm");
  assert(
    mPerm && mPerm.status === "denied" && mPerm.server_name === "test-server" && mPerm.tool_name === "*",
    "mcp permission deny recorded"
  );

  // Skill rows must not leak into mcp_usage, and vice versa. Expressed as an
  // overlap check so the assertion holds even against a reused database.
  const overlap = db
    .query(
      `SELECT COUNT(*) AS c FROM skill_usage s
       JOIN mcp_usage m ON m.call_id = s.call_id AND m.session_id = s.session_id`
    )
    .get().c;
  assert(overlap === 0, "skill and mcp rows stay separate");

  // --- Plugins -----------------------------------------------------------
  const pSid = "sess-test-plugin";

  // The init scan resolved `selftest_tool` to the throwaway plugin, so a
  // before+after pair must be attributed to it by name.
  await plugin["tool.execute.before"](
    { tool: "selftest_tool", sessionID: pSid, callID: "call-plugin-1" },
    { args: { secret: "PLUGINVALUE" } }
  );
  await plugin["tool.execute.after"](
    {
      tool: "selftest_tool",
      sessionID: pSid,
      callID: "call-plugin-1",
      args: { secret: "PLUGINVALUE" },
    },
    {}
  );
  const p1 = db.query("SELECT * FROM plugin_usage WHERE call_id=?").get("call-plugin-1");
  assert(p1 != null, "plugin tool before+after => 1 row");
  assert(
    p1 && p1.plugin_name === selftestPluginPath && p1.kind === "tool",
    "plugin tool attributed by the init scan"
  );
  assert(
    p1 && p1.status === "success" && p1.trigger_type === "tool_call",
    "plugin tool row is success/tool_call"
  );
  assert(
    !JSON.stringify(p1 || {}).includes("PLUGINVALUE"),
    "plugin never stores arg values"
  );

  // The inventory records the scanned surface, and the plugin is not skipped.
  const pInv = db
    .query("SELECT * FROM plugin_inventory WHERE plugin_name=?")
    .get(selftestPluginPath);
  assert(pInv != null && pInv.skipped === 0, "plugin inventory row written");
  assert(
    pInv && pInv.tools === '["selftest_tool"]' && pInv.commands === '["selftest:run"]',
    "plugin inventory surface parsed"
  );

  // A builtin tool is not a plugin tool.
  await plugin["tool.execute.before"](
    { tool: "bash", sessionID: pSid, callID: "call-plugin-builtin" },
    { args: { command: "echo hi" } }
  );
  await plugin["tool.execute.after"](
    { tool: "bash", sessionID: pSid, callID: "call-plugin-builtin", args: { command: "echo hi" } },
    {}
  );
  assert(
    db
      .query("SELECT COUNT(*) AS c FROM plugin_usage WHERE call_id=?")
      .get("call-plugin-builtin").c === 0,
    "builtin tools are not recorded as plugins"
  );

  // An unattributed tool fails OPEN: recorded as (unknown), never dropped —
  // otherwise plugin usage would be silently lost.
  await plugin["tool.execute.before"](
    { tool: "mystery_tool", sessionID: pSid, callID: "call-plugin-unknown" },
    { args: {} }
  );
  await plugin["tool.execute.after"](
    { tool: "mystery_tool", sessionID: pSid, callID: "call-plugin-unknown", args: {} },
    {}
  );
  const pUnknown = db
    .query("SELECT * FROM plugin_usage WHERE call_id=?")
    .get("call-plugin-unknown");
  assert(pUnknown && pUnknown.plugin_name === "(unknown)", "unattributed tool => (unknown)");

  // A later resolved write must upgrade the sentinel without resurrecting a
  // denied call. Driven through the real SQL because the scan is deterministic.
  db.query(UPSERT_PLUGIN_SQL).run(
    "(unknown)", "tool", "upgrade_probe", "sess-probe", null, "tool_call", "denied", null,
    "call-probe-upgrade", null
  );
  db.query(UPSERT_PLUGIN_SQL).run(
    "real-plugin", "tool", "upgrade_probe", "sess-probe", null, "tool_call", "success", 12,
    "call-probe-upgrade", null
  );
  const pUp = db
    .query("SELECT plugin_name, status FROM plugin_usage WHERE call_id=?")
    .get("call-probe-upgrade");
  assert(
    pUp && pUp.plugin_name === "real-plugin" && pUp.status === "denied",
    "(unknown) upgrades to the resolved owner"
  );

  // A scanned command is recorded from `command.execute.before`, with status
  // "unknown": OpenCode exposes no command completion hook.
  await plugin["command.execute.before"]({
    command: "selftest:run",
    sessionID: pSid,
    arguments: [],
  });
  const pCmd = db.query("SELECT * FROM plugin_usage WHERE item_name=?").get("selftest:run");
  assert(pCmd != null, "scanned command is recorded");
  assert(
    pCmd &&
      pCmd.kind === "command" &&
      pCmd.trigger_type === "command_call" &&
      pCmd.status === "unknown",
    "command row is command/command_call/unknown"
  );

  // An unattributed command fails CLOSED: there is no builtin-command
  // allowlist, so fail-open would flood the table with /init and friends.
  await plugin["command.execute.before"]({ command: "init", sessionID: pSid, arguments: [] });
  assert(
    db.query("SELECT COUNT(*) AS c FROM plugin_usage WHERE item_name=?").get("init").c === 0,
    "unattributed commands are not recorded"
  );

  // Plugin rows must not collide with the other two streams.
  const crossPlugin = db
    .query(
      `SELECT COUNT(*) AS c FROM skill_usage s
       JOIN plugin_usage p ON p.call_id = s.call_id AND p.session_id = s.session_id`
    )
    .get().c;
  assert(crossPlugin === 0, "skill and plugin rows stay separate");

  // Views must be queryable.
  for (const view of [
    "v_skill_totals",
    "v_skill_last30",
    "v_skill_history",
    "v_mcp_totals",
    "v_mcp_last30",
    "v_mcp_history",
    "v_plugin_totals",
    "v_plugin_last30",
    "v_plugin_history",
  ]) {
    try {
      db.query(`SELECT * FROM ${view} LIMIT 1`).all();
      assert(true, `${view} queryable`);
    } catch (e) {
      assert(false, `${view} queryable: ${errMsg(e)}`);
    }
  }

  spawnGit = prevSpawnGit;
  try {
    fs.unlinkSync(selftestPluginPath);
  } catch {
    /* ignore */
  }
  const failed = results.filter((r) => !r.ok);
  for (const r of results) console.log(`${r.ok ? "PASS" : "FAIL"}  ${r.label}`);
  console.log(`\n${results.length - failed.length}/${results.length} passed`);
  return failed.length === 0;
}
