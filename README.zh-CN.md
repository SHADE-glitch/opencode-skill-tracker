<p align="right"><a href="README.md">English</a> | <a href="README.zh-CN.md"><b>简体中文</b></a></p>

# 🎯 OpenCode 用量追踪器（`ocut`）

![OpenCode](https://img.shields.io/badge/OpenCode-1.18.x-blue)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![SQLite](https://img.shields.io/badge/storage-SQLite-003B57?logo=sqlite)
![License: MIT](https://img.shields.io/badge/license-MIT-green)
[![Repository](https://img.shields.io/badge/repository-GitHub-black?logo=github)](https://github.com/SHADE-glitch/opencode-usage-tracker)

`ocut` 告诉你 OpenCode 实际是怎么被用的：每一次工具调用、每个 skill 与子 agent、每个
MCP 工具、每一步模型的 token、以及每个会话——谁被调用、什么时候、有没有跑完、耗时多久、
属于哪个项目。

它是一个**被动读取器**。OpenCode 把自己的会话存进一个 SQLite 文件
（`~/.local/share/opencode/opencode.db`）；`ocut` 把这个文件索引进自己的库
（`~/.local/share/opencode-usage-tracker/usage.db`），**从不写回** OpenCode 的目录。
可选的插件 hook 只补上宿主无法暴露的那一项——一次工具调用属于哪个插件。所有数据都在本地：
不联网、不上传、不记录消息正文。

---

## 🤔 为什么

OpenCode 已经把你需要的东西落盘了——只是那个形状是为「恢复会话」而设的，不是为回答
「我到底在用哪些 skill」而设的。

- 每次工具调用都是 `opencode.db` 里的一条 `tool` part，无论工具是什么：内置工具、`skill`
  工具、启动子 agent 的 `task` 工具、还是某个 MCP 服务的工具。
- 每一步模型是 `step-finish` part，带着那一步的 token 与 cost；模型挂在它的**父 message**
  上，不在这一步里。
- 会话、项目、目录是 `session` 表里的行。
- **装了什么**——skills、agents、slash 命令、MCP 服务、插件——在配置目录
  （`~/.config/opencode`）里，不在数据库里。

`ocut` 把上面这些**只读**读出来，变成面板和报表。它刻意**不做**两件事：

- **绝不写 OpenCode 的数据库或配置。** 宿主库以 `mode=ro` 打开并设 `PRAGMA query_only = ON`，
  所以本项目的 bug 也无法让这句话变成假的。
- **绝不存消息或工具的散文。** 一条 `tool` part 带着 `output`、`title`、`metadata`、`error`；
  一条 `text`/`reasoning` part 装着你的提示词和模型的回答；一条会话行带着 OpenCode 从你
  第一条消息生成的 `title`。这些**一个都不读**。落进 `usage.db` 的只有标识符、状态、时长、
  token 数与计数。

设计约束（刻意遵守）：

- **不修改 OpenCode 源码**——一个被动读取器加一个公开的插件 hook；
- **不要求在 `SKILL.md` 里加任何埋点**；
- 一个属于 tracker 自己的数据库 + 一个入口 + 一个可选 hook，尽量少依赖。

### 🧩 被动读取器唯一看不见的东西

OpenCode 把插件的工具记在工具自己的名字下，没有任何东西标明是哪个插件；对插件的**命令**则
完全不记。所以单靠 `ocut` 永远无法归属一次插件调用。可选的 `plugin/ocut-hook.js` 补上这道缝：
加载时静态扫描每个已安装插件的源码，找出它注册的工具 id 和命令 id；在它能归属的一次调用上，
往一个本地 JSONL sidecar 追加一条记录，由 `ocut sync` 摄取。这个 hook 是 `plugin_usage` 的
**唯一**写入方，而且它从不打开 OpenCode 的数据库。

---

## 📋 环境要求

| | |
|---|---|
| 操作系统 | Linux——开发与验证环境为 Ubuntu；其他发行版**未验证** |
| OpenCode | 1.18.x——本项目读取其数据库与配置的宿主（见 [compatibility](docs/maintenance/compatibility.md)） |
| Python | CLI 与 TUI 需要 3.11+；CI 在 **3.11 与 3.14** 上跑测试 |
| Textual | `textual>=8.2,<9`——只有交互式 TUI 需要，由 `install.sh` 装进 venv |
| `uv` | 可选，但推荐用来创建 venv；普通的 `python3 -m venv` 同样可行 |

所有无头子命令（`sync`、`stats`、`tools`、`health`、`export`、`backup`、`restore`、
`format`……）**只需要 Python 标准库**。hook 跑在 OpenCode 自己的 Bun 运行时里，不需要另装任何东西。

---

## 📥 安装

### 📦 一键安装

```bash
git clone https://github.com/SHADE-glitch/opencode-usage-tracker.git
cd opencode-usage-tracker
./install.sh
```

然后**重启 OpenCode** 让它加载 hook，并验证：

```bash
ocut health    # 库能读吗？宿主库找得到吗？
ocut sync      # 把 OpenCode 目前写下的索引一遍
ocut           # 启动交互式 TUI（八个 tab）
```

`install.sh` 是幂等的。它会：

1. 创建 `./.venv` 并把 `requirements.txt`（textual）装进去；
2. 把**两个**安装位置链回本仓库，因此在这里 `git pull` 就能更新线上安装：

   | 安装位置 | 指向 |
   |---|---|
   | `~/.config/opencode/plugin/ocut-hook.js` | `plugin/ocut-hook.js` |
   | `~/.local/bin/ocut` | `bin/ocut` |

   `bin/ocut` 从它自己所在的仓库解析 `scripts/` 目录，所以脚本从不被复制到用户路径。如果某个
   位置已经有真实文件或目录，脚本会**拒绝覆盖**并告诉你该把什么挪走；加 `--force` 才会替换。

3. 跑一次 `ocut health` 并打印后续步骤。

> 确保 `~/.local/bin` 在 `PATH` 里。若不在，把 `export PATH="$HOME/.local/bin:$PATH"` 加进你的 shell 配置。

### 🛠️ 手动安装

同一件事，逐条写出——适合你想把仓库放到别处时：

```bash
git clone https://github.com/SHADE-glitch/opencode-usage-tracker.git
cd opencode-usage-tracker

# 1. 给 TUI 的 venv
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python -r requirements.txt

# 2. 把两个安装位置指向本仓库
mkdir -p ~/.config/opencode/plugin ~/.local/bin
ln -sf "$PWD/plugin/ocut-hook.js" ~/.config/opencode/plugin/ocut-hook.js
ln -sf "$PWD/bin/ocut"            ~/.local/bin/ocut
```

### 📟 仅无头安装

不需要 Textual、不需要 venv——只要读取器和报表：

```bash
mkdir -p ~/.local/bin
ln -sf "$PWD/bin/ocut" ~/.local/bin/ocut
```

之后 `ocut` 的每个子命令都会回退到系统 `python3`；只有启动裸 TUI 才需要 venv。你也可以用
`OCUT_VENV` 把启动器指向自己的 venv。

---

## 🧭 使用

### 🖥️ 交互式 TUI

```bash
ocut
```

需要真正的终端（`stdin`/`stdout`/`stderr` 三者都必须是 TTY）。页面：
**Dashboard / Tools / Skills / Agents / Plugins / MCP / Tokens / Usage**（八个 tab，顺序如上）。
tab 栏与页脚就是全部按键面：

| 按键 | 作用 |
|---|---|
| `q` | 退出 |
| `r` | 刷新——先跑一次增量同步，再重载当前屏 |
| `s` | 切换自动同步（那个 30 秒的后台索引器） |
| `tab` | 下一个 tab |
| `shift+tab` | 上一个 tab |
| `enter` / 双击 | 打开选中行的详情屏 |
| `escape` / `q` | 从推入的详情屏返回 |

**Dashboard** 是三行面板。第一行是头条计数与 token 合计，每个标签都印在它的数字旁边：

| 面板 | 标签 |
|---|---|
| Usage | `Tool calls`、`Skills`、`Agents`、`MCP`、`Plugins inst.` |
| Tokens | `Requests`、`Input`、`Output`、`API Total`、`Prompt Total` |
| Cache | `Cache read`、`Cache write`、`Hit rate` |
| Runtime | `Completed`、`Failed`、`Avg tool time`、`Sessions`、`Projects` |

下面坐着 `Top models` 与 `Top tools` 两个排行榜，以及一张 `Calls per day` 趋势图。
`Plugins inst.` 数的是**已安装**的东西，绝不是「用过的」——被动读取器没有任何证据说明某个插件
或命令真的跑过。

**Tools / Skills / Agents / Plugins / MCP** 五个 tab 一行一个实体，列出调用次数、成功数、
平均耗时与最近使用；在某行按 `enter` 打开该实体的最近调用历史。**Tokens** tab 是宿主自己
每步记录汇总出的按模型 token 合计，**Usage** tab 是你所选窗口的请求日志。每个 tab 各自保留
自己的时间窗口（`7d` 这类预设，或一段自定义区间），改一个 tab 的窗口不会动另一个。

hook 记下的插件调用其 status 是 NULL——工具 after-hook 不带状态，命令则根本没有 after-hook——
所以插件行的状态列显示 `-`，不是 `0`。

### ⚡ 无头子命令（不需要 textual）

```bash
ocut sync    [--full] [--quiet] [--db PATH] [--host-db PATH] [--host-config PATH] [--plugin-events PATH]
ocut stats   [--kind K] [--db PATH]
ocut tools   [--limit N]
ocut skills  [--limit N]
ocut agents  [--limit N]
ocut mcp     [--limit N]
ocut plugins
ocut models  [--limit N]
ocut show    <tool|skill|agent|mcp> <name> [--limit N]
ocut recent  [--limit N]
ocut inventory [--kind K]
ocut export
ocut health
ocut backup
ocut restore [NAME]
ocut format  [--write]
```

- `sync`：把 OpenCode 的数据库索引进 tracker 自己的库。`--full` 从头重建（重建前先做快照）；
  `--quiet` 是 TUI 后台 worker 跑的模式。它是增量的：rowid 游标消费新的 `part` 行，而 `session`
  每次整表重读，因为宿主会重写它的 rollup。
- `stats`：所有类别的总览——会话、项目、工具调用、skill / agent / MCP 调用、**已安装**的插件与
  命令、模型步数与 token。
- `tools` / `skills` / `agents` / `mcp`：某一类别的表。`plugins` 是「已安装 ∪ 已使用」的并集，
  并带上归属插件的版本与声明路径。
- `models`：宿主自己 `step-finish` 记录汇总出的按模型 token 合计，外加最近若干步。`Total` 是宿主
  自报的那一步总量（如果它给了）；cache read/write 单独显示，**不**算进它里面。
- `show <kind> <name>`：某个实体的最近调用。`recent`：最近的任意工具调用。`inventory`：装了什么、
  有什么可用。
- `export`：把每个视图与表 dump 成 JSON。
- `health`：只读报告——库路径、宿主库与配置、解析后的设置、schema 版本、每个同步游标对照宿主自己的
  计数、已索引的合计，以及这个命令存在的意义——`unparsed` 计数。一次「什么都没认领」的运行照样会
  打印 "done"；`health` 是格式变化变得可见的地方。
- `backup` / `restore`：给库做快照，或列出并恢复一份快照（见[备份](#-备份)）。
- `format`：打印 OpenCode 格式依赖面，从解析器的注册表读出。`--write` 刷新 `docs/maintenance/`
  里那个生成块。

全局取值参数（`--db`、`--kind`、`--limit`、`--host-db`、`--host-config`、`--plugin-events`）可以
放在子命令**之前或之后**：`ocut --limit 5 tools` 与 `ocut tools --limit 5` 等价。命令名绝不会
被误当成某个参数的值。

## ⚙️ 设置

**零配置是一种被支持的配置**：没有文件时每个键都取默认值，什么都不会被创建。设置文件是
**`~/.config/ocut/config.toml`**（用 `OCUT_CONFIG` 改路径；`XDG_CONFIG_HOME` 会被尊重），
用标准库的 `tomllib` 读取。

**优先级：环境变量 → 文件 → 默认值。**

| 键 | 默认 | 环境变量 | 管什么 |
|---|---|---|---|
| `top_n` | `5` | `OCUT_TOP_N` | Dashboard 的 Top models / Top tools 列几行 |
| `log_limit` | `100` | `OCUT_LOG_LIMIT` | Usage tab 的请求行数 |
| `detail_limit` | `200` | `OCUT_DETAIL_LIMIT` | 历史 / 模型响应屏的行数 |
| `name_cap` | `28` | `OCUT_NAME_CAP` | 表格单元格渲染多宽，超过就把名字裁掉 |
| `refresh_secs` | `5` | `OCUT_REFRESH_SECS` | TUI 重绘定时器 |
| `sync_secs` | `30` | `OCUT_SYNC_SECS` | TUI 自动同步定时器 |

读不了的文件是**错误**，不是回退：未知键、TOML 解析失败、类型不对、或取值越界，都是硬失败，
并点名那个路径（或变量）和那个键。改了 `config.toml` 却被静默忽略，是这一层能产生的最坏结果。

### 🌱 环境变量总表

| 变量 | 作用 |
|---|---|
| `OCUT_DB` | tracker 数据库（默认 `~/.local/share/opencode-usage-tracker/usage.db`） |
| `OCUT_OPENCODE_DATA` | OpenCode 的数据目录（默认 `~/.local/share/opencode`） |
| `OCUT_OPENCODE_CONFIG` | OpenCode 的配置目录（默认 `~/.config/opencode`）——读取端与 hook **两侧**都用 |
| `OCUT_HOST_DB` | 要读的宿主数据库（默认 `<数据目录>/opencode.db`） |
| `OCUT_HOOK_EVENTS` | 插件用量 sidecar（默认 `~/.local/share/opencode-usage-tracker/plugin-events.jsonl`）——hook 与 `ocut sync` **两侧**都用 |
| `OCUT_CONFIG` | 设置文件（默认 `~/.config/ocut/config.toml`） |
| `XDG_CONFIG_HOME` | 默认设置路径的基目录 |
| `OCUT_HOOK_DISABLE` | `1` 完全关掉 hook |
| `OCUT_HOOK_PACKAGES` | hook 静态扫描去哪儿找 npm 插件源码（默认 `~/.cache/opencode/packages`） |
| `OCUT_SCRIPTS` | 覆盖启动器使用的 `scripts/` 目录 |
| `OCUT_VENV` | 让启动器指向某个 venv 目录**或** python 可执行文件 |
| `OCUT_CONFIG_DIR` | 仅安装脚本：要链接进去的 OpenCode 配置目录（默认 `~/.config/opencode`） |
| `OCUT_BIN_DIR` | 仅安装脚本：`ocut` 启动器链到哪儿（默认 `~/.local/bin`） |
| `HOME` | 继承而来，不是旋钮：上面每个默认路径都由它拼出来 |

---

## 🔬 工作原理

`ocut` **只读** OpenCode 自己的状态，把需要的东西复制进自己的数据库；绝不写回 OpenCode。

```
OpenCode 自己的状态（只读——从不以写入方式打开）
  ~/.local/share/opencode/opencode.db    session · message · part
  ~/.config/opencode/                    已安装的 skills · agents · commands · mcp · plugin
        │
        │  ocut sync   → scripts/ocut-sync.py   （以 mode=ro 打开宿主）
        ▼
~/.local/share/opencode-usage-tracker/usage.db   （SQLite——tracker 自己的库）
        │
        │  读取
        ├── ocut                 → scripts/ocut-tui.py    （Textual TUI，八个 tab，需要 venv）
        ├── ocut stats|tools|…   → scripts/ocut-stats.py  （无头报表，仅标准库）
        └── ocut backup|restore|format

  ~/.local/share/opencode-usage-tracker/plugin-events.jsonl
        ▲  只由 plugin/ocut-hook.js 写入，由 ocut sync 摄取
```

- **来源——只读。** OpenCode 的 `opencode.db`（`session`、`message`、`part`）以及
  `~/.config/opencode` 下已安装的清单。宿主库以 `mode=ro` 打开并设 `PRAGMA query_only = ON`，
  文件不存在是错误，不是「空成功」。
- **同步。** `ocut sync` 把需要的东西复制进 tracker 自己的库；`--full` 从头重建（重建前先快照）。
- **存储。** `~/.local/share/opencode-usage-tracker/usage.db`——tracker 自己的 SQLite，只由
  `ocut` 写入，WAL 模式、5 秒 busy timeout。
- **读取方。** `ocut` 的所有命令。TUI 需要 Textual（venv）；无头报表只需要标准库。

### 🗄️ 数据库

表：`tool_calls`、`model_calls`、`skill_usage`、`agent_usage`、`mcp_usage`、`plugin_usage`、
`plugin_surface`、`sessions`、`inventory`、`unparsed`、`sync_state`。
视图：`v_tools`、`v_skills`、`v_agents`、`v_mcp`、`v_plugins`。

- `tool_calls`：每条 `tool` part 一行，无论工具是什么。`category` 是
  `builtin | skill | agent | mcp`；`status` 是宿主自己的 `state.status` 原样。
- `skill_usage`：`skill` 工具的调用；skill 名读自 `state.input.name`，那是本项目允许看的两个
  参数名之一。
- `agent_usage`：`task` 工具的调用（启动了一个子 agent），按 `state.input.subagent_type` 归类。
- `mcp_usage`：MCP 工具调用，按名字（`<server>_<tool>`）对照宿主配置里列出的服务解析，
  **最长前缀**优先。
- `model_calls`：每条 `step-finish` part 一行；token 与 cost 在这里，模型来自父 message。
- `sessions`：每个 OpenCode 会话一行（含子会话），带宿主自己的按会话 rollup。`sessions` 与
  `model_calls` 是同一测量的两个粒度，**绝不相加**。
- `inventory`：装了什么，读自宿主的目录与配置：skills、agents、commands、MCP 服务、插件、
  provider，以及每个插件注册的工具 id。
- `plugin_usage`：hook 看到的每次插件工具/命令调用一行。这是唯一一张数据来自宿主库之外的表。
- `plugin_surface`：每个插件加载时注册的工具与命令 id，由 hook 上报；`ocut sync` 在文件系统重写
  之后把它折回 `inventory`。
- `sync_state`：读取器跟踪的每张宿主表一行：rowid 游标，加上宿主自己的 `count(*)`/`max(rowid)`，
  以及一个 `note`——被裁剪或重新编号的表在这里说话。游标会在批次里第一条非终态的 `running`
  tool part 处**回退**，好让在行插入之后才结束的调用被重读；回退有上限，所以一个被中断的会话不会
  永远钉住游标。
- `unparsed`：索引器看到但用不了的记录，按 reason 归类。这是「OpenCode 持久化格式变了」的运行时信号。

直接查询：

```bash
sqlite3 ~/.local/share/opencode-usage-tracker/usage.db \
  "SELECT tool_name, calls, errors FROM v_tools ORDER BY calls DESC LIMIT 10;"
sqlite3 ~/.local/share/opencode-usage-tracker/usage.db \
  "SELECT skill, calls, last_ts FROM v_skills ORDER BY calls DESC LIMIT 10;"
```

---

## 💾 备份

```bash
ocut backup            # 现在做一份快照
ocut restore           # 列出可用快照
ocut restore NAME      # 恢复其中一份（先关掉 TUI）
```

- 快照写到 `~/.local/share/opencode-usage-tracker/backups/`，命名
  `usage-YYYYMMDD-HHMMSS-<reason>.db`，用 SQLite 的在线备份 API 生成——不是文件拷贝，因为在
  WAL 下最新的已提交帧可能还在 `-wal` 里，拷出来的主文件会是一份陈旧的库。
- 保留最新的 `BACKUP_KEEP`（5）份托管快照；更旧的按 mtime 清理，不按名字，因为同一秒可能落下好几份。
- `ocut sync --full`、schema 版本升级、`ocut restore` 都会**先**做一份快照，所以每一步破坏性操作
  都可撤销。
- 恢复会保住原来那份库：它在替换前先给在用的文件做快照，所以一次错误的恢复本身也可逆。
- 库旁边手工做的 `usage.db.bak-*` 会作为可恢复来源列出，但**永不**被清理——本工具没创建它们，
  也就无权删除。

**没有 systemd timer**：备份是手动的、只在破坏性操作之前。`ocut backup` 是一个刻意的动作。

---

## 🔧 故障排查

| 现象 | 原因 / 处理 |
|---|---|
| `no database at … — run: ocut sync` | tracker 库还没建。跑 `ocut sync`。 |
| `no OpenCode database at …` | `ocut` 找不到 `~/.local/share/opencode/opencode.db`。确认 OpenCode 跑过，或用 `OCUT_HOST_DB` 指过去。 |
| 插件用量一直空 / 某个插件显示 `0 uses` | 插件用量**只**由 hook 记录，且只在 OpenCode 重启之后。确认 `~/.config/opencode/plugin/ocut-hook.js` 存在（它是个软链接）、重启 OpenCode、并检查 `OCUT_HOOK_DISABLE` 没被设。`0 uses` 的意思是「hook 没记下任何东西」，不是「这个插件没被用过」。 |
| `ocut: this database is older than this build` | 库早于当前 schema。跑 `ocut sync` 把它前滚。 |
| `ocut health` 出现 `UNPARSED` 行 | 读取器遇到了它不认识的记录类型或字段——通常在 OpenCode 升级之后。reason 字符串会点名它；见 [compatibility](docs/maintenance/compatibility.md)。 |
| `ocut health` 报 `settings NOT READ` | `~/.config/ocut/config.toml` 有未知键、TOML 坏了、或取值越界。消息会点名那个键。 |
| TUI 拒绝启动 | 它需要真正的终端和带 textual 的 venv。跑 `./install.sh`，或把 `OCUT_VENV` 指向一个带 textual 的 python。 |

---

## 🚧 已知限制

下面这份清单覆盖 **M1–M14**。

- **M1** OpenCode 自己的数据库是**事实来源**。`ocut` 只看得见宿主写下的东西；宿主从没为某次调用
  写下 `part`，它就是不可见，而不是被记错。
- **M2** 宿主数据库与配置只读、**从不写入宿主**——以 `mode=ro` 打开并设 `PRAGMA query_only = ON`。
  因此 `ocut` 无法修复或回填宿主，只能索引它。
- **M3** 插件用量需要那个可选的 hook **以及**一次 OpenCode 重启。在那之前 `plugin_usage` 是空的，
  Plugins 页上的 `0 uses` 意思是**没有任何记录**，不是「没用过」。
- **M4** hook 的归属是一次尽力而为的**静态扫描**，扫每个插件的源码。扫描解析不出某个插件的面时，
  它就什么都不记——这是诚实的，因为猜一个归属比缺一行更糟。新增或改名的插件需要重启。
- **M5** 插件调用的 `ts` 是 **hook 自己的时钟**，不是宿主的；它的 status 是 NULL——工具的 after-hook
  不带状态，命令则根本没有 after-hook。
- **M6** 当 OpenCode 的持久化格式变了，读取器把这条记录计成 `unparsed` 而不是去猜。升级之后
  `ocut health` 的 `UNPARSED` 行跳变，说明格式变了，不是你不用某个工具了。
- **M7** 从不存消息或工具的散文。part 的 `output`、`title`、`metadata`、`error`，`text`/`reasoning`
  正文，以及会话 `title`，读取器都不点它们的名字；落库的只有标识符、状态、时长与计数。**消息正文**
  永不落盘。
- **M8** 每步的 `model_calls` 与宿主的按会话 rollup `sessions` 是同一测量的两个粒度。它们并排显示，
  **绝不相加**。
- **M9** MCP 调用按名字对照宿主自己的配置归属，**最长前缀**优先。没有哪个已配置服务认领的工具名不会
  被猜进某个服务；它保持 `builtin`，并作为「server 形状但无主」被报出来。
- **M10** 插件用量 sidecar 有上限、靠**截断**轮转（8 MiB）。读取器发现文件比游标短，就从开头重读——
  幂等，因为每条记录都以自己的 id 为键。
- **M11** Plugins 页是**已安装**与「有用量」的并集，按名字作键。装过但没用的插件显示 0 计数；有用量
  但没有 inventory 行的插件照样出现。
- **M12** 备份是手动的、只在破坏性操作之前。**没有 systemd** timer 了；保留策略只留最新五份托管快照，
  从不碰手工副本。
- **M13** Tokens 与 Usage 两张表**不携带** cache *miss* 列，也**不携带**第二个总计列，因为宿主根本没有
  这个字段：一条 `step-finish` part 只公布 `tokens.cache.read` 和 `tokens.cache.write`，中间没有东西。
  补一列 miss 只会得到一整列看起来像测量值的 0；而 `Prompt Total` 已经占了第二个总计本该在的位置。
- **M14** `state` 列在没有相反**没有证据**时读作 `installed`，在 Plugins 页上这就是它的含义。那一页的
  用量只有那个可选的 hook 会写（M3），所以「装了、hook 什么也没记到」的插件，与「装了、从没被碰过」的
  插件拿到的是同一个标签。这个标签刻意只讲 inventory 行，不讲用量：这一列里没有任何一个字在说
  「它跑过」。

---

## 📦 仓库结构

| 路径 | 作用 |
|---|---|
| `bin/ocut` | bash 分发器（位置无关，靠软链接自解析） |
| `plugin/ocut-hook.js` | 唯一一个可选 OpenCode 插件——写 sidecar，从不打开宿主库 |
| `scripts/ocut_db.py` | 共享数据层：schema、设置、查询、快照 |
| `scripts/ocut-sync.py` | 被动读取器：宿主库 → tracker 库 |
| `scripts/ocut-tui.py` | Textual TUI（八个 tab） |
| `scripts/ocut-stats.py` | 无头报表（仅标准库） |
| `scripts/tests/` | pytest 测试 |
| `docs/maintenance/` | 三份参考：`compatibility.md`（每条说法属于哪个版本）、`opencode-interface.md`（按名字读取的每个宿主名词）、`measurements.md`（任何体积 / 速率 / 时长背后的命令） |
| `~/.local/share/opencode-usage-tracker/usage.db` | 主数据库（WAL 模式） |
| `~/.local/share/opencode-usage-tracker/plugin-events.jsonl` | 插件用量 sidecar |

> **运行这套东西之前先读 `MAINTENANCE.zh-CN.md`**（英文：`MAINTENANCE.md`）：该查什么、每条不变量
> 对应哪个测试、以及哪些缺陷是**刻意不修**的（别顺手改回去）。它后面那三份参考是活得比一次编辑更久的部分。

---

## 🧹 卸载

```bash
rm ~/.local/bin/ocut
rm ~/.config/opencode/plugin/ocut-hook.js

# venv（如果你装过）
rm -rf .venv

# 数据——删前先备份
rm ~/.local/share/opencode-usage-tracker/usage.db*
rm -rf ~/.local/share/opencode-usage-tracker/backups
rm ~/.local/share/opencode-usage-tracker/plugin-events.jsonl
```

`ocut` 除了 `install.sh` 那一个软链接之外，从不往 `~/.config/opencode` 写任何东西，所以删掉那个
链接和启动器就是一次完整卸载。

---

## 🤝 参与贡献

欢迎提交 Issue 与 Pull Request。提交前请确保测试全绿：

```bash
python3 -m pytest scripts/tests -q     # 仅标准库；TUI 测试需要 textual
```

改动请保持聚焦；若改动了已记录的行为，请**同时**更新 `README.md` 与 `README.zh-CN.md`——
`scripts/tests/test_readme.py` 会校验两份 README 载有同样的限制、同样的 tab 与同样的按键。

---

## ⚖️ 许可证

[MIT](LICENSE) © 2026 SHADE-glitch
