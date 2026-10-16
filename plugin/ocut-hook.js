// ocut-hook.js — the one OpenCode plugin the tracker ships.
//
// `ocut` (scripts/ocut-*.py) is a PASSIVE reader of OpenCode's own database.
// That is enough for skills, MCP tools, models and tokens, but it can never see
// a plugin run: OpenCode records a plugin's tool under the tool's own name,
// with nothing naming the plugin, and records nothing at all for a plugin
// command. This file is the only thing that fills that gap.
//
// It does two things, both local and both best-effort:
//   1. at load, it scans the installed plugins' sources for the tool ids and
//      command ids each one registers, and appends one `surface` record per
//      plugin;
//   2. on a tool or command call it can attribute to a plugin, it appends one
//      `call` record.
//
// Both land in an append-only JSONL sidecar that `ocut sync` ingests. This
// plugin never opens OpenCode's database, never writes under ~/.config/opencode,
// and never reads a tool's arguments, output or any message text: a call record
// carries only the tool name, the owning plugin, the session/call ids, a status
// and a duration.
//
// OCUT_HOOK_DISABLE=1 turns it off. OCUT_HOOK_EVENTS moves the sidecar.
//
// Loader contract (hard): OpenCode treats every exported function in a plugin
// module as a plugin factory, and only stops when the default export is an
// object carrying `server`. So this file exports its factory ONLY as
// `export default { id, server }` and adds no named export.

import fs from "node:fs";
import path from "node:path";

// --- locations -------------------------------------------------------------

const HOME = process.env.HOME || "";
const CFG_DIR =
  process.env.OCUT_OPENCODE_CONFIG || path.join(HOME, ".config/opencode");
const PACKAGES_DIR =
  process.env.OCUT_HOOK_PACKAGES || path.join(HOME, ".cache/opencode/packages");
const EVENTS =
  process.env.OCUT_HOOK_EVENTS ||
  path.join(HOME, ".local/share/opencode-usage-tracker/plugin-events.jsonl");
const DISABLED = process.env.OCUT_HOOK_DISABLE === "1";

// Caps, all in one place. A name past its cap is truncated rather than dropped:
// the row is still evidence that the call happened.
const NAME_MAX = 120;
const ITEM_MAX = 120;
const MAP_CAP = 5000;                      // FIFO guard for the in-memory maps
const EVENTS_MAX_BYTES = 8 * 1024 * 1024;  // sidecar size that triggers rotation
const HOOKS_MAX = 20000;                   // start-time entries kept for durations

// --- the static scan -------------------------------------------------------
//
// A plugin registers its tools and commands in its own source. OpenCode gives a
// plugin no API to ask another plugin what it registered, so the only way to
// attribute a call to a plugin is to read that source. The scan is deliberately
// best-effort and shape-based: it looks for the `tool: { … }` object literal and
// for the two command-registration shapes, one hop into a bundle's relative
// imports. A plugin whose surface it cannot parse simply records nothing, which
// is honest — a guessed owner would be worse than a missing row.

// Plugins the scan must never read: this hook's own file. After `install.sh`, the
// hook sits in the very plugin directory `localPluginSpecs()` enumerates, and its
// own source carries the literal `tool:{ … { compress: … } }` in a comment
// (explaining the scan) — which the shape-based scanner read as a real `compress`
// tool and attributed to `ocut-hook.js`. A plugin cannot own a tool it merely
// mentions. Every other plugin is scanned, the notifier included: a plugin that
// registers nothing simply yields an empty surface, which is honest.
const PLUGIN_EXCLUDE_DEFAULT = ["ocut-hook"];

// Inside a `tool: { … }` object the tool ids are the keys at the shallowest
// level that has any. Anything deeper belongs to one tool's own definition
// (`args: { query: … }` names a parameter, and a `… ? NOISE : …` inside a
// description string is not a key at all).
const SCAN_KEY_DENYLIST = new Set([
  "description", "parameters", "execute", "args", "inputSchema", "schema",
  "title", "metadata", "type", "required", "properties", "tool",
]);
const TOOL_OBJECT_RE = /\btool\s*:\s*\{/g;
const TOOL_KEY_RE = /([A-Za-z_][\w]*)\s*:/y;
const CMD_TEMPLATE_RE = /"([A-Za-z][\w:.-]+)"\s*:\s*\{\s*template\s*:/gs;
const CMD_BRACKET_RE = /command\s*\[\s*"([^"]+)"\s*\]\s*=/gs;
// `from "./x.js"` and `import("./x.js")` — the two ways a bundle shim reaches
// the chunk that actually registers the surface.
const RELATIVE_IMPORT_RE = /(?:\bfrom|\bimport)\s*\(?\s*["'](\.\/[^"']+)["']/g;
const PLUGIN_SCAN_HOPS_MAX = 3;
const PLUGIN_SCAN_HOP_MAX_BYTES = 256 * 1024;

// "@scope/name@1.2.3" -> { base: "@scope/name", version: "1.2.3" }
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
// to the config directory, which is what OpenCode documents. Returns null for
// anything that is not a path spec (i.e. an npm name).
function localPluginPath(spec) {
  if (spec.startsWith("file://")) return path.resolve(spec.slice("file://".length));
  if (path.isAbsolute(spec)) return spec;
  if (spec.startsWith(".")) return path.resolve(CFG_DIR, spec);
  return null;
}

// Resolves a config `plugin` entry to its package directory and entry file.
// Returns null when nothing usable is found.
function resolvePluginEntry(spec) {
  const file = localPluginPath(spec);
  if (file !== null) {
    return fs.existsSync(file)
      ? { dir: path.dirname(file), entry: file, name: file, version: null }
      : null;
  }
  const { base, version } = pluginSpecParts(spec);
  const inner = path.join(base, "package.json");
  const candidates = [];
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
    };
  }
  return null;
}

// Index just past the closing quote of the string literal starting at `i`.
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

// The tool ids in `src`. Shallowest-level-wins, so DCP's
// `tool:{ ...cond && { compress: … } }` still yields `compress`.
function scanToolBlocks(src, tools) {
  let from = 0;
  for (;;) {
    TOOL_OBJECT_RE.lastIndex = from;
    const open = TOOL_OBJECT_RE.exec(src);
    if (!open) return;
    const byDepth = new Map();
    let depth = 1;
    let i = open.index + open[0].length; // just past the `{` that opens it
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
    from = i;
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
    // Only inside the entry's own directory subtree — `../` leaves it.
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

// Reads an OpenCode config file, tolerating JSONC comments. Returns null when
// the file is missing or unparsable.
function readJsonConfig(file) {
  try {
    if (!fs.existsSync(file)) return null;
    const text = fs.readFileSync(file, "utf8");
    try {
      return JSON.parse(text);
    } catch {
      return JSON.parse(
        text.replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|[^:])\/\/.*$/gm, "$1")
      );
    }
  } catch {
    return null;
  }
}

function configFiles(pluginDir) {
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

function configPluginEntries(pluginDir) {
  const entries = [];
  for (const file of configFiles(pluginDir)) {
    const json = readJsonConfig(file);
    if (!json) continue;
    const list = Array.isArray(json.plugin) ? json.plugin : [];
    for (const s of list) if (typeof s === "string" && s) entries.push(s);
  }
  return entries;
}

function isSkippedPlugin(spec) {
  return PLUGIN_EXCLUDE_DEFAULT.some((name) => spec.includes(name));
}

// --- the sidecar -----------------------------------------------------------

function writeEvent(record) {
  try {
    fs.mkdirSync(path.dirname(EVENTS), { recursive: true });
    let size = 0;
    try {
      size = fs.statSync(EVENTS).size;
    } catch {
      size = 0;
    }
    // Rotation is a truncation, and the reader notices it the same way it
    // notices a renumbered host table: the file is now shorter than the cursor,
    // so the cursor resets and the file is re-read from the start (idempotent —
    // every record is keyed by its own id).
    if (size > EVENTS_MAX_BYTES) {
      fs.writeFileSync(EVENTS, JSON.stringify({ t: "reset" }) + "\n");
    }
    fs.appendFileSync(EVENTS, JSON.stringify(record) + "\n");
  } catch {
    /* the sidecar must never break OpenCode */
  }
}

function clip(value, max) {
  return typeof value === "string" ? value.slice(0, max) : null;
}

// --- the plugin ------------------------------------------------------------

async function ocutHookPlugin(input) {
  if (DISABLED) return {};

  const pluginDir = (input && (input.directory || input.worktree)) || null;
  const toolToPlugin = new Map();   // tool id     -> plugin name
  const commandToPlugin = new Map(); // command id -> plugin name
  const started = new Map();        // session/call -> start ms, for durations

  // Load the installed plugins and their surfaces. Wrapped so a scan failure
  // leaves the hook a no-op rather than disrupting OpenCode's own load.
  try {
    const specs = configPluginEntries(pluginDir).concat(localPluginSpecs());
    const seen = new Set();
    for (const spec of specs) {
      if (seen.has(spec)) continue;
      seen.add(spec);
      const resolved = resolvePluginEntry(spec);
      const name = (resolved && resolved.name) || spec;
      const surface = isSkippedPlugin(spec)
        ? { tools: new Set(), commands: new Set() }
        : scanPluginSurface(resolved && resolved.entry);
      for (const t of surface.tools) if (!toolToPlugin.has(t)) toolToPlugin.set(t, name);
      for (const c of surface.commands) if (!commandToPlugin.has(c)) commandToPlugin.set(c, name);
      writeEvent({
        t: "surface",
        plugin: clip(name, NAME_MAX),
        tools: [...surface.tools].map((x) => clip(x, ITEM_MAX)),
        commands: [...surface.commands].map((x) => clip(x, ITEM_MAX)),
      });
    }
  } catch {
    /* a scan that throws records nothing; it must not take OpenCode down */
  }

  function capKey(map, key, value) {
    if (map.size >= MAP_CAP) {
      const oldest = map.keys().next().value;
      map.delete(oldest);
    }
    map.set(key, value);
  }

  function callKey(sessionID, callID) {
    return `${sessionID || ""}:${callID || ""}`;
  }

  return {
    "tool.execute.before": async (hookInput) => {
      try {
        if (!hookInput || typeof hookInput.tool !== "string") return;
        if (!toolToPlugin.has(hookInput.tool)) return;
        capKey(started, callKey(hookInput.sessionID, hookInput.callID), Date.now());
      } catch {
        /* never throw */
      }
    },

    "tool.execute.after": async (hookInput) => {
      try {
        if (!hookInput || typeof hookInput.tool !== "string") return;
        const plugin = toolToPlugin.get(hookInput.tool);
        if (!plugin) return;
        const key = callKey(hookInput.sessionID, hookInput.callID);
        const startMs = started.get(key);
        started.delete(key);
        writeEvent({
          t: "call",
          ts: Date.now(),
          sid: hookInput.sessionID || null,
          cid: hookInput.callID || null,
          plugin: clip(plugin, NAME_MAX),
          kind: "tool",
          item: clip(hookInput.tool, ITEM_MAX),
          status: null,
          ms: typeof startMs === "number" ? Date.now() - startMs : null,
        });
      } catch {
        /* never throw */
      }
    },

    // Plugin commands are namespaced by their plugin (conductor:status,
    // dcp-compress). There is no command.execute.after, so a command records no
    // status; the call id is synthetic, because two invocations of the same
    // command must stay two rows.
    "command.execute.before": async (hookInput) => {
      try {
        if (!hookInput || typeof hookInput.command !== "string") return;
        const plugin = commandToPlugin.get(hookInput.command);
        if (!plugin) return;
        const now = Date.now();
        writeEvent({
          t: "call",
          ts: now,
          sid: hookInput.sessionID || null,
          cid: `cmd:${hookInput.sessionID || ""}:${now}`,
          plugin: clip(plugin, NAME_MAX),
          kind: "command",
          item: clip(hookInput.command, ITEM_MAX),
          status: null,
          ms: null,
        });
      } catch {
        /* never throw */
      }
    },
  };
}

export default { id: "ocut-hook", server: ocutHookPlugin };
