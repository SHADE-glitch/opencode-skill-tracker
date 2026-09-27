/**
 * OpenCode Skill Usage Tracker
 * ---------------------------------------------------------------------------
 * Records every `skill` tool invocation into a SQLite database.
 *
 * Verified against OpenCode 1.18.32 / @opencode-ai/plugin 1.18.4:
 *   - Skills are exposed to the model as a tool named exactly "skill"
 *     (input schema `{ name: string }`). There is NO dedicated skill hook.
 *   - Detection therefore uses `tool.execute.before` / `tool.execute.after`
 *     as the primary path, `event` -> `message.part.updated` as a fallback,
 *     and `permission.ask` for config-level denials.
 *
 * Hard rule: no hook may ever throw into OpenCode, and no hook mutates the
 * `output` object it receives. Every body is wrapped in try/catch, and
 * `bun:sqlite` is imported dynamically so a load failure degrades to a no-op.
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

const SUMMARY_MAX = 120;
const BUSY_TIMEOUT_MS = 5000;
const MAP_CAP = 5000; // FIFO eviction guard for in-memory maps
const GIT_TIMEOUT_MS = 500;

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
       json_extract(u.metadata,'$.branch')  AS branch,
       json_extract(u.metadata,'$.summary') AS summary
FROM skill_usage u
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

// ---------------------------------------------------------------------------
// Module state
// ---------------------------------------------------------------------------
let Database = null; // bun:sqlite Database constructor, resolved dynamically
let db = null; // open connection
let initDone = false;
let initInFlight = null; // dedupes concurrent init() calls
let sqliteUnavailable = false;
let pluginDir = null; // PluginInput.directory / worktree fallback

let skillIdByName = new Map(); // skill name -> skills.id
const sessionCtx = new Map(); // sessionID -> { directory, model, agent, summary, title }
const callCtx = new Map(); // callID    -> { sessionID, startMs, skillName }
const branchByDir = new Map(); // dir       -> branch | null
const pendingSkillPerms = new Map(); // permissionID -> { name, callID }

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
// Sanitization — applied only to the user-message text used as `summary`.
// Replace -> collapse -> truncate -> replace again (catches a secret that
// straddles the truncation boundary). Raw text is never persisted or logged.
// ---------------------------------------------------------------------------
const SECRET_PATTERNS = [
  /\bsk-[A-Za-z0-9_-]{8,}/g, // OpenAI-style
  /\bghp_[A-Za-z0-9]{20,}/g, // GitHub PAT
  /\bark-[A-Za-z0-9-]{16,}/g, // Volcengine (a live key exists in opencode.json)
  /\bBearer\s+[A-Za-z0-9._~+/-]{8,}=*/gi, // bearer tokens
  /\b[A-Fa-f0-9]{32,}\b/g, // long hex
  /\b[A-Za-z0-9+/]{40,}={0,2}\b/g, // long base64
  /\b(?:api[_-]?key|apikey|access[_-]?token|token|password|passwd|secret)\s*[=:]\s*\S+/gi,
];

export function sanitize(input) {
  if (!input) return "";
  let t = String(input);
  for (const re of SECRET_PATTERNS) t = t.replace(re, "[REDACTED]");
  t = t.replace(/\s+/g, " ").trim();
  if (t.length > SUMMARY_MAX) t = t.slice(0, SUMMARY_MAX);
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

  // Rebuild name -> id cache (also used to resolve skill_id on writes).
  try {
    skillIdByName = new Map();
    for (const row of db.query("SELECT id, name FROM skills").all()) {
      skillIdByName.set(row.name, row.id);
    }
  } catch (e) {
    log("err", "skill id cache: " + errMsg(e));
  }

  log("info", `scanned ${count} skills (${noName} without frontmatter name) from ${SKILLS_DIR}`);
  return count;
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
  initDone = true;
  log("info", `initialized (db=${DB_PATH}, tool="${SKILL_TOOL}")`);
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
// Write helper — used by every detection path
// ---------------------------------------------------------------------------
async function recordUsage(
  { skillName, sessionID, projectPath, triggerType, status, callID, durationMs, meta }
) {
  if (!db) return;
  try {
    const ctx = (sessionID && sessionCtx.get(sessionID)) || {};
    const dir = projectPath || ctx.directory || pluginDir || null;
    const branch = await resolveBranch(dir); // resolved before the write

    const metadata = {
      tool: SKILL_TOOL,
      call_id: callID ?? null,
      agent: ctx.agent ?? null,
      model: ctx.model ?? null,
      branch: branch ?? null,
      summary: ctx.summary ?? null,
      source: (meta && meta.source) || triggerType,
    };
    if (meta && meta.title) metadata.title = meta.title;
    if (meta && meta.error) metadata.error = String(meta.error).slice(0, 200);

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
    debug(`recorded ${skillName} (${triggerType}/${status}) session=${sessionID} call=${callID}`);
  } catch (e) {
    log("err", "recordUsage: " + errMsg(e));
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
  const { directory, worktree } = input || {};
  pluginDir = directory || worktree || null;

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
      if (!hookInput || hookInput.tool !== SKILL_TOOL) return;
      const name =
        hookOutput && hookOutput.args && typeof hookOutput.args.name === "string"
          ? hookOutput.args.name
          : null;
      debug(`before skill callID=${hookInput.callID} name=${name}`);
      setCapped(callCtx, hookInput.callID, {
        sessionID: hookInput.sessionID,
        startMs: Date.now(),
        skillName: name,
      });
    }),

    "tool.execute.after": safe("tool.execute.after", async (hookInput) => {
      if (!hookInput || hookInput.tool !== SKILL_TOOL) return;
      const ctx = callCtx.get(hookInput.callID);
      const name =
        (hookInput.args && typeof hookInput.args.name === "string"
          ? hookInput.args.name
          : null) ||
        (ctx && ctx.skillName) ||
        "unknown";
      const duration = ctx && ctx.startMs ? Date.now() - ctx.startMs : null;
      callCtx.delete(hookInput.callID);
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
      const type = hookInput.type || hookInput.action;
      if (type !== SKILL_TOOL) return;
      if (!hookOutput || hookOutput.status !== "deny") return;
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
          if (!part || part.type !== "tool" || part.tool !== SKILL_TOOL) return;
          const st = part.state || {};
          const inputName =
            st.input && typeof st.input.name === "string" ? st.input.name : null;

          if (st.status === "pending" || st.status === "running") {
            if (!callCtx.has(part.callID)) {
              setCapped(callCtx, part.callID, {
                sessionID: part.sessionID,
                startMs: st.time && st.time.start ? st.time.start : Date.now(),
                skillName: inputName,
              });
            }
            return;
          }

          if (st.status === "completed" || st.status === "error") {
            const duration =
              st.time && st.time.start && st.time.end ? st.time.end - st.time.start : null;
            await recordUsage(
              {
                skillName: inputName || "unknown",
                sessionID: part.sessionID,
                triggerType: "event_detected",
                status: st.status === "completed" ? "success" : "error",
                callID: part.callID,
                durationMs: duration,
                meta: { source: "event", error: st.status === "error" ? st.error : undefined },
              }
            );
          }
          return;
        }

        case "permission.updated": {
          const perm = props; // properties is the Permission object
          const type = perm && (perm.type || perm.action);
          if (type !== SKILL_TOOL) return;
          const raw = perm.pattern || (perm.metadata && perm.metadata.name) || "unknown";
          setCapped(pendingSkillPerms, perm.id, {
            name: String(Array.isArray(raw) ? raw[0] : raw),
            callID: perm.callID ?? null,
          });
          return;
        }

        case "permission.replied": {
          const perm = pendingSkillPerms.get(props.permissionID);
          if (!perm) return;
          pendingSkillPerms.delete(props.permissionID);
          const response = String(props.response || "").toLowerCase();
          const denied = ["reject", "rejected", "deny", "denied", "no", "cancel", "cancelled"].includes(
            response
          );
          debug(`permission.replied response=${response} denied=${denied}`);
          if (denied) {
            await recordUsage(
              {
                skillName: perm.name,
                sessionID: props.sessionID,
                triggerType: "permission_denied",
                status: "denied",
                callID: perm.callID,
                durationMs: null,
                meta: { source: "permission.replied" },
              }
            );
          }
          return;
        }

        default:
          return;
      }
    }),

    // -- context only (never written directly) ------------------------------
    "chat.message": safe("chat.message", async (hookInput, hookOutput) => {
      const sid = hookInput && hookInput.sessionID;
      if (!sid) return;
      const parts = (hookOutput && hookOutput.parts) || [];
      const text = parts
        .filter((p) => p && p.type === "text" && typeof p.text === "string")
        .map((p) => p.text)
        .join(" ");
      const summary = sanitize(text);
      const model = hookInput.model
        ? `${hookInput.model.providerID}/${hookInput.model.modelID}`
        : undefined;
      const prev = sessionCtx.get(sid) || {};
      setCapped(sessionCtx, sid, {
        ...prev,
        agent: hookInput.agent ?? prev.agent,
        model: model ?? prev.model,
        summary: summary || prev.summary,
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
      pendingSkillPerms.clear();
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
    console.log("FATAL: no database available; selftest aborted");
    return false;
  }

  const tables = db
    .query("SELECT name FROM sqlite_master WHERE type='table'")
    .all()
    .map((r) => r.name);
  assert(tables.includes("skills") && tables.includes("skill_usage"), "tables exist");

  const fm = parseFrontmatter("---\nname: foo\ndescription: bar: baz\n---\nbody");
  assert(fm.name === "foo" && fm.description === "bar: baz", "frontmatter parse");

  const s = sanitize("key ark-00000000-0000-0000-0000-000000000000 and sk-abcdefgh12345678");
  assert(s.includes("[REDACTED]") && !s.includes("ark-00000000") && s.length <= 120, "sanitize redacts");

  const skillCount = db.query("SELECT COUNT(*) AS c FROM skills").get().c;
  assert(skillCount > 0, `skills scanned (${skillCount})`);

  const sid = "sess-test-1";
  const cid = "call-test-1";
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
  assert(
    meta.summary && meta.summary.includes("[REDACTED]") && !meta.summary.includes("ark-00000000"),
    "summary sanitized in db"
  );
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
          state: { status: "error", input: { name: "docker-env-normalization" }, error: "boom", time: { start: 2000, end: 2500 } },
        },
      },
    },
  });
  const r2 = db.query("SELECT * FROM skill_usage WHERE call_id=?").get(cid2);
  assert(
    r2 && r2.status === "error" && r2.trigger_type === "event_detected" && r2.duration_ms === 500,
    "event error path"
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

  // Views must be queryable.
  for (const view of ["v_skill_totals", "v_skill_last30", "v_skill_history"]) {
    try {
      db.query(`SELECT * FROM ${view} LIMIT 1`).all();
      assert(true, `${view} queryable`);
    } catch (e) {
      assert(false, `${view} queryable: ${errMsg(e)}`);
    }
  }

  spawnGit = prevSpawnGit;
  const failed = results.filter((r) => !r.ok);
  for (const r of results) console.log(`${r.ok ? "PASS" : "FAIL"}  ${r.label}`);
  console.log(`\n${results.length - failed.length}/${results.length} passed`);
  return failed.length === 0;
}
