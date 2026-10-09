"""User settings: one registry, one precedence rule, and zero required config.

Precedence is **flag > config file > environment > default**, and the tool is fully
usable with none of them: every key has a default that is the behaviour the
screens already show. The file lives in the tracker's own data directory
(`~/.local/share/opencode/skillt-config.json`) — never under `~/.config/opencode/`,
which belongs to OpenCode.

Two rules this module exists to enforce:

* **A setting nobody can name is not a setting.** `REGISTRY` carries the plain-language
  explanation of every key in both documentation languages, and
  `scripts/tests/test_settings_documented.py` fails if a key, its env name or its
  flag is missing from either README. That is the gate whose absence let six env
  vars ship undocumented.
* **A broken config file must never fall back silently.** A typo'd key or a value
  of the wrong type is reported by `read_file` as a *problem* alongside the values
  it did accept, and `skillt doctor` shows it. Reading a file, ignoring half of it
  and reporting the numbers as if the user's settings applied is the failure this
  whole layer is meant to avoid.
"""

from __future__ import annotations

import difflib
import json
import os

CONFIG_NAME = "skillt-config.json"
CONFIG_PATH_ENV = "OPENCODE_SKILL_TRACKER_CONFIG"


def _spec(key, default, minimum, flag, en, zh):
    return {
        "key": key,
        "default": default,
        "minimum": minimum,
        "flag": flag,
        "en": en,
        "zh": zh,
    }


# The whole surface, in one list. Order is the order `skillt config list` prints.
REGISTRY = (
    _spec(
        "view.days", 30, 1, "--days",
        "How many days of history the headless reports cover (`skillt insight`, "
        "`skillt claude-mem`). The interactive screens use fixed windows instead — "
        "7-day trends and 30-day totals — because their layout is pinned to them.",
        "无头报告（`skillt insight`、`skillt claude-mem`）覆盖多少天历史。交互页面用的是固定"
        "窗口——7 天趋势、30 天合计——因为它们的排版按这两个数字钉死。",
    ),
    _spec(
        "view.limit", 10, 1, "--limit",
        "How many rows a ranked table lists before it stops. Headless only "
        "(`skillt insight`); the interactive tables hold every row and scroll.",
        "排名表格列多少行就停。**只对无头命令生效**（`skillt insight`）；交互表格全部列出、"
        "靠滚动看。",
    ),
    _spec(
        "view.recent_rows", 100, 1, "--recent-rows",
        "How many events the interactive Recent timeline lists. The rest stay in "
        "the database; this only shortens what the screen holds.",
        "交互界面 Recent 时间线列出多少条事件。其余仍在库里，这里只决定屏幕装多少。",
    ),
    _spec(
        "view.min_uses", 3, 0, "--min-uses",
        "Minimum calls before a skill counts as worth advising about. Headless only "
        "(`skillt insight`): the interactive screen has no advisor table any more.",
        "一个 skill 至少被调用多少次才进入建议范围。**只对无头命令生效**（`skillt insight`）："
        "交互界面已没有顾问页。",
    ),
    _spec(
        "doctor.freshness_days", 7, 1, "--freshness-days",
        "How old the newest recorded call may get before `doctor` says capture looks "
        "stalled. A quiet week and a dead tracker look identical from inside.",
        "最新一条记录超过多少天，`doctor` 就提示采集可能停了。一周没动静和采集器坏了，"
        "从库里看是一样的。",
    ),
    _spec(
        "log.max_bytes", 4 * 1024 * 1024, 1024, "--max-bytes",
        "How big the plugin's own log may get before `skillt rotate-log` moves it "
        "aside. It is the same number the reader uses (`TRACKER_LOG_BYTES_CAP`), on "
        "purpose: a line in the active log that `doctor` cannot see would be a "
        "silent loss, and the cap exists to stop exactly that.",
        "插件自己的日志长到多少字节后由 `skillt rotate-log` 挪走。**故意**与读取端用同一个数"
        "（`TRACKER_LOG_BYTES_CAP`）：活日志里有 `doctor` 看不见的一行就是静默丢失，而上界要挡的"
        "正是这件事。",
    ),
    _spec(
        "log.keep_files", 5, 1, "--keep-files",
        "How many rotated logs to keep besides the live one. Rotation is a rename, so "
        "the lines move rather than disappear; this is how far back that history goes.",
        "除当前那份以外保留多少个轮转后的日志。轮转是**改名**，行不会消失；这个数字决定那段历史"
        "往回留多久。",
    ),
    _spec(
        "retention.usage_days", 0, 0, "--keep-days",
        "Delete usage rows older than this many days. **0 = off: nothing is ever "
        "deleted.** `skillt prune-usage` lists the rows it would remove and only "
        "removes them with `--yes`, which first writes a backup and stops if that "
        "backup fails.",
        "删除早于这么多天的用量行。**0=关闭：什么都不删。** `skillt prune-usage` 先列出要删什么，"
        "只有加了 `--yes` 才动手，而 `--yes` 会先写一份备份，备份失败就停下来。",
    ),
    _spec(
        "retention.max_skill_versions", 0, 0, "--keep-versions",
        "Keep at most this many `skill_versions` rows per skill (0 = off). The table "
        "gains one row every time a SKILL.md's content changes and never loses one.",
        "每个 skill 最多保留多少条 `skill_versions`（0=关闭）。这张表每次 SKILL.md 内容变化就多"
        "一行，从不减少。",
    ),
)

_BY_KEY = {s["key"]: s for s in REGISTRY}


def spec(key: str) -> dict:
    return _BY_KEY[key]


def keys() -> tuple[str, ...]:
    return tuple(_BY_KEY)


def env_name(key: str) -> str:
    """The environment counterpart of a key, by rule rather than by listing.

    `view.days` -> `OPENCODE_SKILL_TRACKER_VIEW_DAYS`. One stated pattern beats six
    hand-written names: the READMEs document the rule and the doc test checks it,
    so a new key is documented the moment it exists.
    """
    return "OPENCODE_SKILL_TRACKER_" + key.upper().replace(".", "_")


def flag_of(key: str) -> str | None:
    return _BY_KEY[key]["flag"]


def key_for_flag(flag: str) -> str | None:
    for s in REGISTRY:
        if s["flag"] == flag:
            return s["key"]
    return None


def default_path(home: str | None = None) -> str:
    root = home if home is not None else os.path.expanduser("~")
    return os.path.join(root, ".local", "share", "opencode", CONFIG_NAME)


def config_path(env=None) -> str:
    env = os.environ if env is None else env
    return env.get(CONFIG_PATH_ENV) or default_path()


def coerce(key: str, raw, origin: str):
    """Validate one value. Returns (value, problem_or_None).

    Every path a value can arrive by (file, environment, flag) goes through here,
    so a bad one is reported the same way wherever it was typed.
    """
    s = _BY_KEY[key]
    if isinstance(raw, bool) or not isinstance(raw, (int, str)):
        return s["default"], f"{key}: {origin} value {raw!r} is not a number; using {s['default']}"
    try:
        value = int(str(raw).strip())
    except ValueError:
        return s["default"], f"{key}: {origin} value {raw!r} is not a number; using {s['default']}"
    if value < s["minimum"]:
        return (s["default"],
                f"{key}: {origin} value {value} is below the minimum {s['minimum']}; "
                f"using {s['default']}")
    return value, None


def _read(path: str, env) -> tuple[dict, dict, list[str]]:
    """The file as (accepted, rejected, problems) — the detail `effective` needs.

    A missing file is the normal case and reports nothing. An unreadable or
    unparsable file is a problem, and none of its keys are applied: the alternative
    is a screen full of default numbers the user believes are theirs.
    """
    if not os.path.isfile(path):
        return {}, {}, []
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        return {}, {}, [f"{path}: cannot read ({e}); defaults are in force"]
    try:
        data = json.loads(text)
    except ValueError as e:
        return {}, {}, [f"{path}: not valid JSON ({e}); defaults are in force"]
    if not isinstance(data, dict):
        return {}, {}, [f"{path}: expected a JSON object, got "
                        f"{type(data).__name__}; defaults are in force"]

    values, rejected, problems = {}, {}, []
    for key, raw in sorted(data.items()):
        if key not in _BY_KEY:
            # A typo here would otherwise read as "my setting had no effect".
            problems.append(f"{path}: unknown key {key!r} (ignored)")
            continue
        value, problem = coerce(key, raw, "config file")
        if problem:
            problems.append(problem)
            rejected[key] = raw
        else:
            values[key] = value
    return values, rejected, problems


def read_file(path=None, env=None) -> tuple[dict, list[str]]:
    """The public shape: accepted values and the problems found reading the file."""
    env = os.environ if env is None else env
    values, _rejected, problems = _read(path or config_path(env), env)
    return values, problems


def _close_key(key: str) -> str | None:
    """Nearest registry key, for the typo hint. Stdlib `difflib`, no new dependency."""
    matches = difflib.get_close_matches(key, list(_BY_KEY), n=1, cutoff=0.7)
    return matches[0] if matches else None


def effective(flag_values=None, path=None, env=None) -> tuple[dict, list[str]]:
    """Every key resolved to (value, origin), plus the problems found on the way.

    `flag_values` is what the command line supplied; anything it does not mention
    falls through to file, then environment, then default. A rejected value keeps
    its own origin labelled `(rejected)` rather than quietly becoming "default":
    an owner who typed `view.days: "thirty"` must be able to see, from
    `skillt config list`, that the 30 on screen is not theirs.
    """
    flag_values = flag_values or {}
    env_map = os.environ if env is None else env
    path = path or config_path(env_map)
    file_values, file_rejected, problems = _read(path, env_map)
    out = {}
    for s in REGISTRY:
        key = s["key"]
        flag = s["flag"]
        if flag and key in flag_values:
            value, problem = coerce(key, flag_values[key], f"flag {flag}")
            out[key] = (value if not problem else s["default"],
                        f"{f'flag {flag}'}" if not problem else f"flag {flag} (rejected)")
            if problem:
                problems.append(problem)
            continue
        if key in file_values:
            out[key] = (file_values[key], "config file")
            continue
        if key in file_rejected:
            # The file said something and it was refused; the environment does not
            # get to answer a question the owner already asked in the wrong form.
            out[key] = (s["default"], "config file (rejected)")
            continue
        if env_name(key) in env_map:
            value, problem = coerce(key, env_map[env_name(key)], f"env {env_name(key)}")
            if problem:
                problems.append(problem)
                out[key] = (s["default"], f"env {env_name(key)} (rejected)")
            else:
                out[key] = (value, f"env {env_name(key)}")
            continue
        out[key] = (s["default"], "default")
    return out, problems


def values(flag_values=None, path=None, env=None) -> dict:
    """Just the resolved values, for callers that cannot use the provenance."""
    return {k: v for k, (v, _) in effective(flag_values, path=path, env=env)[0].items()}


def value(key: str, flag_values=None, path=None, env=None):
    return effective(flag_values, path=path, env=env)[0][key][0]


def write_value(key: str, raw, path=None, env=None) -> tuple[int, str]:
    """Set one key in the file. Returns (exit code, message) — never raises.

    The file is written through a temp + `os.replace` under mode 0600: it lives
    beside the database in a directory the owner shares with other tools, and a
    half-written JSON file would be read back as "not valid JSON" next time.
    """
    if key not in _BY_KEY:
        near = _close_key(key)
        hint = f" (did you mean {near}?)" if near else ""
        return 2, f"unknown setting {key!r}{hint} — list them with: skillt config list"
    value, problem = coerce(key, raw, "value")
    if problem:
        return 2, problem
    path = path or config_path(env)
    current, problems = read_file(path, env)
    if problems:
        # Rewriting here would rebuild the file from the keys that parsed, silently
        # deleting the owner's typo and the rejected value beside it. The problem is
        # the thing to fix, and it is one line of their file.
        return 2, f"refusing to rewrite {path} while it reports problems: {problems[0]}"
    merged = {**current, key: value}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(dict(sorted(merged.items())), f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return 0, f"{key} = {value}  ({path})"


def reset_value(key: str, path=None, env=None) -> tuple[int, str]:
    """Drop one key from the file so its default (or env) applies again."""
    if key not in _BY_KEY:
        return 2, f"unknown setting {key!r}"
    path = path or config_path(env)
    if not os.path.isfile(path):
        return 0, f"{key} is not in {path}; it already uses its default"
    current, problems = read_file(path, env)
    if problems:
        return 2, f"refusing to rewrite {path} while it reports problems: {problems[0]}"
    merged = {k: v for k, v in current.items() if k != key}
    if not merged:
        os.remove(path)
        return 0, f"{key} removed; {path} is empty now, so every default applies"
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(dict(sorted(merged.items())), f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return 0, f"{key} removed from {path}"
