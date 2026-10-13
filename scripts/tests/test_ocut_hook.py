"""Tests for `plugin/ocut-hook.js`, the one writer of plugin usage.

Skipped where Bun is not installed (the CI runner has no Bun), so this suite is a
local guard rather than a gate: the hook is JavaScript, and the only honest way to
check that it appends what `ocut sync` expects is to load it and call it. Each test
runs a Bun snippet that imports the hook, drives its handlers, and prints what it
saw; the sidecar it wrote is then read back as JSONL.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

BUN = shutil.which("bun")
requires_bun = pytest.mark.skipif(BUN is None, reason="bun is not installed")

PLUGIN = Path(__file__).resolve().parents[2] / "plugin" / "ocut-hook.js"


def run_bun(script: str, env: dict) -> subprocess.CompletedProcess:
    full = dict(os.environ)
    # Never let the hook reach the developer's real config or sidecar.
    full.pop("OCUT_HOOK_DISABLE", None)
    full["PLUGIN_PATH"] = str(PLUGIN)
    full.update(env)
    return subprocess.run([BUN, "-e", script], capture_output=True, text=True,
                          timeout=60, env=full)


def fixture_cfg(tmp_path, name="./plugins/demo.js", source=None):
    """A config dir with one local plugin that ships a tool and a command."""
    cfg = tmp_path / "cfg"
    (cfg / "plugins").mkdir(parents=True)
    (cfg / "opencode.json").write_text(json.dumps({"plugin": [name]}))
    if source is None:
        source = (
            "export const Demo = async () => ({\n"
            "  tool: {\n"
            "    compress: {\n"
            "      description: 'x',\n"
            "      args: { query: { type: 'string' } },\n"
            "      execute: async () => ({}),\n"
            "    },\n"
            "  },\n"
            "  command: { \"dcp-compress\": { template: 'do it' } },\n"
            "});\n"
        )
    (cfg / "plugins" / "demo.js").write_text(source)
    return cfg


def env_for(tmp_path, cfg):
    sidecar = tmp_path / "plugin-events.jsonl"
    return sidecar, {
        "OCUT_OPENCODE_CONFIG": str(cfg),
        "OCUT_HOOK_EVENTS": str(sidecar),
        # Point the npm-package scan at an empty dir so no test reads the machine.
        "OCUT_HOOK_PACKAGES": str(tmp_path / "no-packages"),
        "HOME": str(tmp_path),
    }


def records(sidecar: Path) -> list[dict]:
    if not sidecar.exists():
        return []
    return [json.loads(l) for l in sidecar.read_text().splitlines() if l.strip()]


@requires_bun
def test_default_export_is_the_loader_contract_and_nothing_else(tmp_path):
    """`default` must be `{ id, server }` and no other export may be a function.

    OpenCode calls every exported function as a plugin factory; a named export here
    would be invoked with the wrong `input`. The contract is what keeps the hook from
    being loaded twice or crashing the host's own plugin load.
    """
    cfg = fixture_cfg(tmp_path)
    _, env = env_for(tmp_path, cfg)
    r = run_bun(
        "const m = await import(process.env.PLUGIN_PATH);"
        "const d = m.default;"
        "if (!d || typeof d !== 'object' || typeof d.server !== 'function')"
        " { console.log('FAIL'); process.exit(1); }"
        "console.log(JSON.stringify({ id: d.id, exports: Object.keys(m).sort() }));",
        env)
    assert r.returncode == 0, r.stdout + r.stderr
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out == {"id": "ocut-hook", "exports": ["default"]}


@requires_bun
def test_the_hook_records_a_surface_and_only_plugin_owned_calls(tmp_path):
    """A `surface` per plugin, a `call` per plugin-owned tool/command, nothing else.

    `bash` is not owned by any plugin, so it must not produce a row — the hook is an
    attribution tool, not a second copy of the tool-call log.
    """
    cfg = fixture_cfg(tmp_path)
    sidecar, env = env_for(tmp_path, cfg)
    r = run_bun(
        "const m = await import(process.env.PLUGIN_PATH);"
        "const h = await m.default.server({ directory: process.env.OCUT_OPENCODE_CONFIG });"
        "await h['tool.execute.after']({ tool: 'compress', sessionID: 's1', callID: 'c1' });"
        "await h['command.execute.before']({ command: 'dcp-compress', sessionID: 's1' });"
        "await h['tool.execute.after']({ tool: 'bash', sessionID: 's1', callID: 'c2' });"
        "console.log('OK');",
        env)
    assert r.returncode == 0, r.stdout + r.stderr
    recs = records(sidecar)
    surfaces = [x for x in recs if x["t"] == "surface"]
    calls = [x for x in recs if x["t"] == "call"]
    assert len(surfaces) == 1, recs
    assert surfaces[0]["plugin"].endswith("demo.js"), surfaces
    assert surfaces[0]["tools"] == ["compress"], surfaces
    assert surfaces[0]["commands"] == ["dcp-compress"], surfaces
    assert {(c["kind"], c["item"]) for c in calls} == {
        ("tool", "compress"), ("command", "dcp-compress")}, calls
    assert all(c["plugin"].endswith("demo.js") for c in calls), calls


@requires_bun
def test_disable_turns_the_hook_into_a_no_op(tmp_path):
    """`OCUT_HOOK_DISABLE=1` is the off switch, and it writes nothing."""
    cfg = fixture_cfg(tmp_path)
    sidecar, env = env_for(tmp_path, cfg)
    env["OCUT_HOOK_DISABLE"] = "1"
    r = run_bun(
        "const m = await import(process.env.PLUGIN_PATH);"
        "const h = await m.default.server({ directory: process.env.OCUT_OPENCODE_CONFIG });"
        "if (h['tool.execute.after'])"
        " await h['tool.execute.after']({ tool: 'compress', sessionID: 's1', callID: 'c1' });"
        "console.log('OK');",
        env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert records(sidecar) == []
