# OpenCode Skill + MCP + Plugin Tracker (`skillt`)

![OpenCode](https://img.shields.io/badge/OpenCode-1.18.x-blue)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![SQLite](https://img.shields.io/badge/storage-SQLite-003B57?logo=sqlite)
![License: MIT](https://img.shields.io/badge/license-MIT-green)
[![Repository](https://img.shields.io/badge/repository-GitHub-black?logo=github)](https://github.com/SHADE-glitch/opencode-skill-tracker)

记录并查询 OpenCode 里每个 skill、**MCP 工具和插件工具/命令**的使用情况：谁被调用、什么时候、成功还是失败、耗时多久、属于哪个项目。
数据全部落在本地一个 SQLite 文件里，不联网、不上传、不记录对话内容。

> **本仓库的安装方式见 [README.md](README.md)（英文）。** 本仓库是唯一来源，
> `~/.config/opencode/{plugin/skill-tracker.js,scripts,skill-tracker}` 与
> `~/.local/bin/skillt` 都是**指回本仓库的软链接**，执行 `./install.sh` 一键完成。
> 下文按**安装后的布局**描述；TUI 虚拟环境推荐放在 `./.venv`，旧的
> `~/.local/share/opencode/skillt-venv` 仍可用（启动器会自动回退）。
>
> 仅在 Linux 上测试，开发与验证环境为 Ubuntu 26.04；其他发行版/平台未验证。

---

## 1. 项目介绍

OpenCode 目前**没有专门的 skill hook**。本工具通过通用的工具调用钩子来间接识别 skill 使用：

- OpenCode 把 skill 暴露为一个名为 `skill` 的工具（入参 `{ name }`）；
- 插件 `skill-tracker.js` 监听 `tool.execute.before` / `tool.execute.after` / `permission.ask` / `command.execute.before` / `event`；
- 识别到工具名称为 `skill` 时写入 `skill_usage`；识别到已配置的 MCP 服务时写入 `mcp_usage`；其它非内置工具写入 `plugin_usage`；
- 插件命令用 `command.execute.before` 记录，但没有对应的完成 hook，所以状态保持 `unknown`；无法明确归属的插件命令不记录；
- `skillt` 负责查询、统计、导出、备份、体检。

**MCP 工具走的是同一套钩子。** 所有注册的工具（内置的和 MCP 的）都经过同一个包装器，
因此 MCP 调用会和 skill 一样触发这些 hook。MCP 工具 id 形如 `{server}_{tool}`
（例如 `basic-memory_read_note`）；由于服务名和工具名都可能含下划线，插件用
**已配置服务列表做最长前缀匹配**来切分，而不是简单 `split("_")`。

设计约束（刻意遵守）：

- **不修改 OpenCode 源码**，只用公开的 Plugin Hook；
- **不要求在 SKILL.md 里加任何脚本或埋点**；
- 只记录结构化元数据（skill 名、状态、耗时、项目路径、session、模型/agent/分支），**不记录 secrets、不记录完整消息**；
- MCP 调用**只记参数名，绝不记参数值**（`Object.keys()`，不读取属性）；
- 插件归属在初始化时通过已安装插件的静态扫描尽力解析；解析失败的工具归为 `(unknown)`，不猜具体插件；
- 默认排除监控插件自身和 `opencode-notifier`，但仍在插件清单中标为 `skipped`；
- 单个插件文件 + 一个共享数据层 + 一个入口命令，尽量少依赖。

---

## 2. 架构

```
OpenCode 运行
   │  tool.execute.before/after, permission.ask, event   (skill 与 MCP 共用)
   ▼
~/.config/opencode/plugin/skill-tracker.js      (Bun 运行时, bun:sqlite)
   │  写入
   ▼
~/.local/share/opencode/skill-usage.db          (SQLite, WAL, 0600)
   │  读取
   ├── skillt                → skill-tui.py      (Textual TUI, 需要 venv)
   ├── skillt stats|top|...  → skill-stats.py    (旧版 CLI, 行为向后兼容)
   └── skillt insight|health|doctor|mcp|...  → skill-tui.py --cli  (无 textual 也能跑)
```

- **写入方**：只有插件（OpenCode 运行时）。所有写入都用 `UNIQUE(session_id, call_id)` 去重 + `ON CONFLICT` upsert。
- **读取方**：`skillt` 的所有命令。`health` / `mcp` / `plugins` 以只读方式打开数据库，绝不修改。
- **共享数据层**：`scripts/skill_db.py`，被 `skill-tui.py` 与测试共用；schema、迁移、查询、导出、备份都在这里。skill、MCP、插件计数（`skill_usage` / `mcp_usage` / `plugin_usage`）是**三张独立的表**，互不影响。

---

## 3. 目录说明

| 路径 | 作用 |
|---|---|
| `~/.config/opencode/plugin/skill-tracker.js` | OpenCode 插件，唯一的写入方（**不要改捕获逻辑**） |
| `~/.config/opencode/scripts/skill_db.py` | 共享数据层（schema/迁移/查询/导出/备份） |
| `~/.config/opencode/scripts/skill-tui.py` | TUI + `--cli` 无头子命令 |
| `~/.config/opencode/scripts/skill-stats.py` | 旧版 CLI（命令/参数向后兼容，只读） |
| `~/.config/opencode/scripts/tests/` | pytest 测试 |

> **运行这套东西之前先读 `MAINTENANCE.zh-CN.md`**（英文：`MAINTENANCE.md`）：
> 每日 / 每周 / 每月该查什么、把 tracker 与 `opencode.db` 对账的方法、每条不变量对应哪个测试、
> 以及哪些缺陷是**刻意不修**的（别顺手改回去）。
| `~/.config/opencode/skill-tracker/` | **本目录**：文档 + systemd 单元 |
| `~/.local/bin/skillt` | 统一入口（bash 分发器；`skill-tracker` 是它的软链接） |
| `~/.local/share/opencode/skill-usage.db` | 主数据库（WAL 模式，0600） |
| `~/.local/share/opencode/backups/` | 备份专用目录（0700），`auto-backup`、`skillt backup`、TUI 的 `b`、以及 `--yes` 前的自动回滚备份**都写这里**（M19 已修，之前有两条路径写到了库旁边） |
| `~/.local/share/opencode/skillt-venv/` | 给 TUI 用的 Python 虚拟环境（含 textual） |

---

## 4. 命令说明

统一入口是 `skillt`。

### 4.1 交互式 TUI

```bash
skillt
```

需要真正的终端（`stdin`/`stdout`/`stderr` 三者都必须是 TTY，且 `TERM` 不能为空或 `dumb`）。
页面：**Dashboard / Skills / MCP / Plugins / Recent / Categories / Data**（Data 永远在最后）。

**Dashboard 顶部的 6 张卡片**（名字在下、数字在上）：

| 卡片 | 含义 |
|---|---|
| `Skills` | 库里已知的 skill 总数（`skills` 表行数） |
| `Skill calls` | 累计 skill 调用次数（`skill_usage` 行数） |
| `MCP calls` | 累计 MCP 工具调用次数（`mcp_usage` 行数） |
| `Plugin calls` | 累计插件调用次数（`plugin_usage` 行数） |
| `Today (all)` | 今天（本地日历天）三类调用总数 |
| `Skill success` | skill 总体成功率 |

卡片下方有图例行，把 `Today (all)` 按三类拆开，并显示**观察样本**（N 个 session、D 天）；三类计数**分开统计**，`Skill calls` 永远不会把 MCP/插件流量算进去。

`Last 7 days` 是三个**横向并排**的小图——skill、MCP、plugin 调用各一张，每张按自己的峰值缩放（MCP/插件量级通常小一个数量级，共用峰值会被压平）。每一行**固定 20 字符**、三张图**高度钉死一致**，所以三者永远落在**同一条水平线**上：以前计数不加补白，只要某张图出现大数就只有它自己折行，那一张的日子就和另外两张错开了。窄于 70 列时行就放不下了（实测：70 列格子恰为 20 宽，66 列只剩 18/19/19），此时三张一起裁切，而不是各切各的。超过 5 位的数字会被压缩（`123456` → `123k`），这正是行宽能保持恒定的原因。下面是三个 Top-10 表：**Top Skills**、**Top MCP tools**、**Top Plugins**。

**MCP 页**每个 `(server, tool)` 组合一行，显示调用次数、30 天调用、session 数、成功率、平均耗时、最近使用时间。Skills / MCP / Plugins 三张表**排序和过滤框各自独立**——`s` 只切换当前页的排序，`Enter` 打开该行的详情页。**Recent 页**把三类调用合成一条统一时间线，`Enter` 会按行类型跳到对应的 skill/MCP/插件详情页。切换到某一页时只重读**当前页**（`r` 才重读所有页），因此每页各自显示自己的 `data as of HH:MM:SS`，而不是一个全局时间戳。

**没有 Advisor 页**：这一页在 2026-10-03 按 owner 的要求移除了。它当时展示的只读聚合仍然存在，只是退回无头命令 `skillt agentos`（见下文命令一览）；那条命令现在是唯一打开那个库的入口，TUI 的任何一页都不再读它——这一点由 `test_tui_never_reads_the_advisor_store` 钉住。

**Plugins 页**每个 `(plugin, kind, item)` 组合一行，显示调用次数、成功率、平均耗时、最近使用时间；页面上方是 **Inventory**：初始化时扫描到的插件清单，每条带上工具/命令数量、**最后被看到的时刻**，以及它是被哪一份配置声明的（`global` / `localdir` / `project`）——只有全局配置和本地插件目录那两类会在每次启动时被刷新，某次启动没再见到的就会被删掉，所以这一列现在说的是"启动时加载了什么"，不再是"这个库建好以来曾经见过什么"。来自某个项目配置的清单单独一行，因为换个目录启动的会话看不见它。插件工具归属失败时显示 `(unknown)`；插件命令只在扫描明确归属时记录。

| 按键 | 作用 |
|---|---|
| `Tab` | 切换页面 |
| `↑` `↓` / `j` `k` | 移动光标 |
| `Enter` | 查看选中行的详情页 |
| `/` | 跳到 Skills 页并聚焦搜索框 |
| `s` / `ctrl+s` | 切换排序（次数 ↓ / 最近使用 ↓ / 成功率 ↓ / 名称 ↑） |
| `Esc` | 清空搜索并取消聚焦 |
| `r` / `ctrl+r` | 刷新 |
| `q` | 退出 |
| `d` | 删除**当前在 Skills 页选中**的 skill（需二次确认） |
| `b` `e` `v` | 备份 / 导出 / 压缩（VACUUM） |

> 搜索框获得焦点时，普通字母会被输入框吃掉。为此 `ctrl+s`（排序）与 `ctrl+r`（刷新）**始终可用**，不必先按 `Esc`。

Data 页另有按钮：备份、压缩（VACUUM）、导出、**健康 Health**、清空 usage。删除 skill 的入口**只**在 Skills 页。

> 删除类操作**必须**先在 Skills 页用 `j`/`k` 选中一行。在其它页面按 `d` 只会提示「请先切到 Skills 页」，不会弹确认框——这是刻意设计，防止误删隐藏表格里的第一行。

### 4.2 无头子命令（不需要 textual）

```bash
skillt insight [--days N] [--min-uses N] [--limit N] [--json]
skillt export  [--out FILE] [--pretty] [--force] [--skills-only]
skillt sync    [--dry-run] [--prune-orphans] [--json]
skillt health  [--json] [--limit N]
skillt mcp     [--json] [--limit N]
skillt plugins [--json] [--limit N]
skillt auto-backup [--dry-run] [--json]
skillt doctor  [--json] [--freshness-days N]
skillt cleanup-selftest [--yes]
skillt scrub-metadata [--yes] [--json] [--limit N]
skillt agentos   [--json] [--limit N]
```

- `insight`：最常使用 / 增长最快 / **从未使用** / 长期未使用 / 失败率最高，并附观察样本（session 数与天数）。
- `export`：导出 JSON（0600）；不指定 `--out` 则打印到 stdout。
- `sync`：重新扫描 `SKILL.md`，把内容变更记录进 `skill_versions`；`--dry-run` 只统计不写。
- `health`：把每个 skill 分成 **活跃（≤30 天）/ 沉寂（30–90 天）/ 未使用（>90 天或从未）**，并给出风险标记与建议。会显示**观察样本**（N 个 session、D 天）；样本不足 **20 个 session 或 14 天**时只输出一行"数据不足"，不给剪枝建议——在样本足够之前，"0 次使用"没有意义。**只读、只建议、绝不自动删除。**
- `mcp`：MCP 工具使用情况——按 server 汇总，再列出调用最多的工具（次数、成功率、最近使用）。**只读。**
- `plugins`：插件清单（来源、版本、工具/命令面、排除状态、`scope` 与最后被看到的时刻）以及按插件/类型/项目汇总的用量。**只读。** `--json` 输出 `inventory` 与 `items` 两组数据。
- `auto-backup`：在专用目录里创建备份并按保留策略清理旧备份（见 §6）。
- `doctor`：体检，输出 PASS/WARN/FAIL；**有 FAIL 时退出码为 1**。除结构性检查（库、skills、插件文件、环境、备份）外，还检查**采集链路本身**：`capture.freshness`（**三张表分别**报最新一条距今多少天，例如 `skill_usage 0.0d · mcp_usage 2.2d · plugin_usage 0.0d`，阈值 `--freshness-days`，默认 7；从没记过行的流报 `no rows`，不算停滞；`OPENCODE_SKILL_TRACKER_STREAMS=skill,plugin` 可以把某条流排除在判定之外，但它仍会被打印并标注 `(excluded)`）、`log.errors`（插件日志里 `[err]` 行的数量与最后一条）、`env.opencode_version`（当前 OpenCode 版本 vs 内置工具 allowlist 所对齐的版本，见 M13）。这三项**只 WARN、不 FAIL**——安静一周不是故障。
- `cleanup-selftest`：清除 `__selftest()` 遗留的合成行（`project_path = /tmp/selftest-proj`）。默认 dry-run，`--yes` 才真删（先试跑一次确认有行可删，再自动备份后删除）。
- `scrub-metadata`：把 `metadata` 里不该留的**自由文本键**（`summary`、`title`）从历史行中剥掉。**行本身保留**——用量是这张库的意义所在，泄露的文本不是。默认 dry-run 列出命中行，`--yes` 才改（先备份，改完再 checkpoint WAL，让文本真的从磁盘上消失——见 M14）。背景见 M14。

- `agentos`：**只读**聚合 AgentOS 顾问插件**它自己的库**。顾问**不注册任何工具**、也**不注册任何命令**，所以 `skill_usage` / `mcp_usage` / `plugin_usage` 里指名它的记录是 **0 行**，`plugin_inventory` 只有 1 行且工具列表与命令列表都是空的；这一项改读它的 `store/aos.db` 与 `store/loops/*.json`，而且现在是唯一读它的入口（TUI 已无 Advisor 页）。需要 `AGENT_OS_ROOT`（或 `OPENCODE_SKILL_TRACKER_AGENTOS_DB`），没配就明说"未聚合"。每个 loop 给出逐段状态与耗时、最慢段相对顾问单次预算是否超支、同一会话在 tracker 里到底产生过多少可度量的工具调用（这一列最有用），以及召回的记忆**是否真的进了提示**——显示成 `searched 3 / recalled 3 / reached the prompt 4 (1234 chars)`。`memory_ids` 与 `injected_memory_ids` 刻意分开：假设(hypothesis)可以单独被注入，合并成一个数就把这件事藏掉了。**从不写那个库**；字段是逐个白名单投影出来的，所以 loop 里的 `task_text`（任务原文）和各阶段 payload 一律读不到。见 M21。

参数校验：`--days ≥ 1`、`--min-uses ≥ 0`、`--limit ≥ 1`、`--freshness-days ≥ 1`；非法值直接报错并以退出码 2 结束。
所有无头子命令在 **stdout 非 TTY**（如管道、重定向）时也能正常运行，输出为纯文本/JSON。

### 4.3 旧版 CLI（委托给 `skill-stats.py`）

```bash
skillt stats | top [N] | show <skill> | recent [N] | delete <skill> | clear | backup | vacuum
skillt help
```

- 这些命令保持向后兼容：命令名、位置参数（如 `top 3`）与 `--json` 字段都没有变化。
- `top` / `recent` 的条数既可用位置参数（`top 3`），也可用 `--limit 3`，两者等价。
- `show <skill> --limit N` 控制历史条数（默认 20）。
- `backup [FILE]` 生成 `VACUUM INTO` 快照，权限 `0600`。
- 唯一可见的变化是**输出文案改为英文**（便于统一日志与脚本解析）。

---

## 5. 数据库说明

### 5.1 表

- **`skills`**：每个 SKILL.md 一行。`path` 有 `UNIQUE` 约束；`name` **没有**唯一约束。
  迁移时新增 `content_hash` 列（内容哈希，用于版本追踪）。
- **`skill_usage`**：每次调用一行。`UNIQUE(session_id, call_id)` 保证幂等去重。
  `status ∈ {success, error, denied, ask, unknown}`；`trigger_type ∈ {tool_call, event_detected, permission_denied, manual}`。
  `metadata` 是 JSON 文本（model / agent / branch，已由插件清洗）；消息正文与标题从不读取，因此不可能被存进来。
- **`mcp_usage`**：每次 MCP 工具调用一行，按 `(server_name, tool_name)` 区分。
  与 `skill_usage` 同样的 `UNIQUE(session_id, call_id)` 去重，同样的 status / trigger_type 取值。
  `tool_name = '*'` 表示只知道 server、不知道具体工具（权限拒绝路径）。
  `arg_names` 是调用**参数名**组成的 JSON 数组——参数**值**从不读取，因此不可能被存进来。
- **`plugin_usage`**：每次插件工具/命令调用一行，按 `(plugin_name, kind, item_name)` 汇总，使用同样的 `UNIQUE(session_id, call_id)` 去重。插件工具的未知归属显示为 `(unknown)`；插件命令 `trigger_type = command_call` 且状态通常是 `unknown`。
- **`plugin_inventory`**：插件初始化扫描清单，一行一个插件，保存版本、来源、静态发现的 tools/commands、`skipped` 排除标记，以及 `scope`——这一行是被哪份配置声明的（`global` / `localdir` / `project` / 早期遗留为 NULL，按 `global` 处理）。`loadPlugins()` 结束时删掉 `global`/`localdir` 里本次没再见到的行；`project` 行不删（换个目录启动的会话看不见它）。清空 usage 不会动这张表。
- **`skill_versions`**：内容哈希历史，`UNIQUE(skill_name, content_hash)`。

### 5.2 视图

- `v_skill_totals`：每个 skill 的 total / success / errors / denied / last_used。
- `v_skill_last30`：近 30 天使用次数。
- `v_skill_history`：把 `metadata` 里的字段展开成列，便于查询。
- `v_mcp_totals`：每个 `(server, tool)` 的 total / success / errors / denied / last_used。
- `v_mcp_last30`：近 30 天调用次数。
- `v_mcp_history`：MCP 版的历史视图，额外带 `arg_names`。
- `v_plugin_totals` / `v_plugin_last30` / `v_plugin_history`：插件用量总计、近 30 天和历史视图。

插件来源和归属是**初始化时的一次性静态扫描**：入口文件加上它相对 import 的那一层；工具 id 取 `tool: {` 里**最浅的有键那一层**（再深就是某个工具自己的 `args`，不是工具）。新增、升级、改名插件或其命令/工具面后，需要重启 OpenCode 才会刷新清单；扫描无法解析的工具只记 `(unknown)`，无法明确归属的命令直接不记。

### 5.3 运行参数

- `journal_mode = WAL`（读写可并发），`busy_timeout = 5000`，`synchronous = NORMAL`。
- 文件权限 `0600`；备份目录 `0700`。
- `source`（personal / open-source）是**派生值**，由 `category` 实时计算，**不落库**，避免重复存储。

### 5.4 直接查询示例

```bash
sqlite3 ~/.local/share/opencode/skill-usage.db \
  "SELECT skill_name, total, success, errors, last_used FROM v_skill_totals ORDER BY total DESC LIMIT 10;"
sqlite3 ~/.local/share/opencode/skill-usage.db \
  "SELECT server_name, tool_name, total, errors FROM v_mcp_totals ORDER BY total DESC LIMIT 10;"
```

---

## 6. 备份与恢复

### 6.1 手动备份

```bash
skillt backup        # 旧版 CLI：备份到 DB 同目录
```

或 TUI 里按 `b` / Data 页「备份」按钮。备份用 `VACUUM INTO` 生成一致性快照。
无论走哪条路径（旧版 CLI、TUI、`auto-backup`），生成的文件都会 `chmod 0600`。

### 6.2 自动备份与保留策略

```bash
skillt auto-backup --dry-run   # 先看会做什么
skillt auto-backup             # 真正执行
```

- 备份写入 `~/.local/share/opencode/backups/`（0700），命名固定为 `skill-usage-backup-YYYYMMDD-HHMMSS.db`；所有备份路径都指向这一个目录（M19）。
- 保留策略：**最近 30 个每日**（每天留最新一个）+ **最近 12 个月每月**（每月留最新一个），其余删除。
- 安全护栏：
  - 只处理**名称完全匹配** `skill-usage-backup-<8位日期>-<6位时间>.db` 的文件；
  - 只动这个专用目录，绝不碰主库、导出文件或手工副本；
  - 不删除**年龄小于 120 秒**的文件；
  - 名称解析失败的文件**永不删除**；
  - 同名（同一秒）冲突会**报错而不是覆盖**；
  - 有 `.lock` 文件防止并发运行（锁超过 10 分钟视为过期，可被接管）。

### 6.3 用 systemd timer 定时备份（可选，需你手动启用）

单元文件在 `~/.config/opencode/skill-tracker/systemd/`。启用步骤：

```bash
mkdir -p ~/.config/systemd/user
cp ~/.config/opencode/skill-tracker/systemd/skillt-auto-backup.* ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now skillt-auto-backup.timer

# 查看状态 / 触发一次
systemctl --user list-timers skillt-auto-backup.timer
systemctl --user start skillt-auto-backup.service

# 让定时器在未登录时也能运行（可选，通常需要）
loginctl enable-linger "$USER"
```

> 单元里的 `ExecStart` 使用**绝对路径**（systemd user 的 PATH 默认不含 `~/.local/bin`）。

### 6.4 恢复

```bash
# 1. 停掉正在使用该库的 OpenCode 会话（避免边写边恢复）
# 2. 先保住当前库
cp ~/.local/share/opencode/skill-usage.db ~/.local/share/opencode/skill-usage.db.before-restore
# 3. 用备份覆盖
cp ~/.local/share/opencode/backups/skill-usage-backup-YYYYMMDD-HHMMSS.db \
   ~/.local/share/opencode/skill-usage.db
# 4. 清理可能残留的 WAL/SHM（它们属于旧库）
rm -f ~/.local/share/opencode/skill-usage.db-wal ~/.local/share/opencode/skill-usage.db-shm
# 5. 校验
skillt doctor
```

备份是标准 SQLite 文件，也可以直接用 `sqlite3` 打开做逐表比对。

---

## 7. 故障排查

| 现象 | 原因 / 处理 |
|---|---|
| `skillt: not an interactive terminal` / `skillt: TERM is unset or 'dumb'` | `stdin`/`stdout`/`stderr` 未全部为 TTY，或 `TERM` 为空/`dumb`。改用无头命令：`skillt insight`、`skillt health`、`skillt doctor`。 |
| `skillt: TUI venv not found` | 建 venv：`uv venv --python 3.13 ~/.local/share/opencode/skillt-venv && uv pip install --python ~/.local/share/opencode/skillt-venv/bin/python textual`。 |
| 统计里没有新数据 | 插件只在 OpenCode 运行时写入。先看 OpenCode 运行日志里的 `failed to load plugin`：若出现 `skill-tracker.js`，说明插件加载失败（见「已知限制 → 插件导出契约」），hook 根本没注册，而不是技能没被调用。再确认 `skillt doctor` 里 `plugin.exists` / `plugin.hooks` 为 PASS。 |
| `failed to load plugin path=list error="Plugin export is not a function"` | 某个**项目级** `.opencode/opencode.json` 的 `plugin` 数组里写了不是插件的 npm 包名（例如 `"list"`）。删掉该条目即可；`path=list` 表示来源是配置里的插件列表而非 `plugin/` 目录。 |
| `doctor` 报 `db.wal` FAIL | 库文件损坏或不是 SQLite。用 `sqlite3 ... "PRAGMA integrity_check;"` 确认，必要时从备份恢复。 |
| 时间对不上（差几小时） | 已修复（M1）：按天分桶现用**本地日历天**。修复前写入的旧记录仍按当时的时间戳存储。 |
| 输出乱码 / `UnicodeEncodeError` | 见 M6：非 UTF-8 locale。设 `LANG=C.UTF-8` 或 `LC_ALL=C.UTF-8`。 |
| `backups.latest` WARN | 还没跑过 `auto-backup`，或最近 7 天没备份。跑一次 `skillt auto-backup`。 |

日志：插件把错误写到 `~/.config/opencode/logs/`（由 OpenCode 管理）。

---

## 8. 删除方法

按粒度从轻到重：

```bash
# 1) 只清空使用记录，保留 skills 列表
skillt clear                      # 旧版 CLI（需确认）
# 或 TUI Data 页「清空 usage」按钮

# 2) 删除某个 skill 的全部记录（usage + 版本 + skills 行）
skillt delete <name>              # 旧版 CLI
# 或 TUI：切到 Skills 页 → j/k 选中 → 按 d → 确认

# 3) 清除 __selftest 遗留的合成行
skillt cleanup-selftest           # dry-run
skillt cleanup-selftest --yes     # 真删（自动先备份）
```

完全移除本工具：

```bash
rm ~/.local/bin/skillt ~/.local/bin/skill-tracker
rm ~/.config/opencode/plugin/skill-tracker.js
rm -rf ~/.config/opencode/scripts ~/.config/opencode/skill-tracker
rm -rf ~/.local/share/opencode/skillt-venv
# 数据（谨慎，删前先备份）
rm ~/.local/share/opencode/skill-usage.db*
rm -rf ~/.local/share/opencode/backups
```

---

## 9. 已知限制

以下问题在审计中确认存在。此前几轮修掉了界面 / 参数 / 编码相关的几项（M6、Data 页删除入口、参数校验、排序等）；一轮又修掉了 **M1 / M7 / M8 / M9**（下文标注"已修复"）。**本轮（对采集与界面的全面实测）修掉了：dashboard 三张表 Enter 无效、Plugins 页对 `@scope/name` 型插件 Enter 无效、Recent 时间线对含 `:` 的名字 Enter 静默失效、导出泄露历史提示词、`on_mount` 开库无保护、一次失败让半屏数据停在旧值、以及测试对本机 skills 目录的依赖**；并给 `doctor` 加了 `capture.freshness` / `log.errors` / `env.opencode_version` 三项，新增 `skillt scrub-metadata`。其余如实记录、**本次不修**（M14–M18 为本轮新登记）。多数是边界情况，不影响日常使用。

### 数据与时间

- **M1 按天分桶曾使用 UTC（已修复）。** 旧版 `今日`、`按天趋势` 以 UTC 计算，在 UTC+8 下本地 00:00–08:00 的记录会被算进**前一天**（最多 8 小时/天的错配）。现改为按**本地日历天**分桶（`date(timestamp,'localtime')`）。时间窗口本身（`-N days`）一直是滚动窗口，未受影响。
- **M2 纯读命令以读写方式打开 DB。** `insight`/`export`/`doctor` 用读写连接（`health` 已改为只读）。另外 `open_db(readonly=True)` 在**路径不存在**时会创建一个空库而不是报错。
- **M3 迁移的隐式提交。** `ensure_schema` 里的 `executescript` 会隐式提交当前挂起的事务；返回值里 `created_base` 从不置位、`created_versions` 恒为 `True`（仅影响该字典的语义，不影响实际建表）。

### 备份

- **M4 备份权限窗口。** `VACUUM INTO` 完成**之后**才 `chmod 0600`，存在极短的宽松权限窗口；若 VACUUM 失败，可能留下**未清理的半成品文件**（需手工删除）。
- **M19 两种备份的存放位置不同，保留策略只覆盖其中一个（已修复）。** 旧版里 `auto-backup`（定时器走的那条）写进 `BACKUP_DIR`（`~/.local/share/opencode/backups/`）并按 30 每日 + 12 每月清理，而 `skillt backup`、TUI 的 `b`、以及 `scrub-metadata --yes` / `cleanup-selftest --yes` 前自动写的回滚备份用的是 `backup_db()` 的默认路径——**写在数据库旁边**。那些文件永远不会被保留策略碰到，而且 `doctor backups.latest` 只扫 `BACKUP_DIR`，于是"备份存在"和"体检说没有备份"同时成立。默认路径现已改为 `BACKUP_DIR`，并由 `test_default_backup_lands_in_the_retention_directory` 钉住。此前散落在外的旧文件仍需手工收拢：`mv ~/.local/share/opencode/skill-usage-backup-*.db ~/.local/share/opencode/backups/`。

### 界面

- **M5 TUI 大库会假死。** `VACUUM`/备份/导出/刷新在 **UI 线程同步执行**，库很大时会短暂无响应。
- **M10 TUI 退出码。** `app.run()` 未包 `try/finally`，致命错误后进程退出码仍可能是 0。

### 插件

- **M7 git 子进程泄漏（已修复）。** 旧版 `resolveBranch` 的 500ms 超时**不杀 git 子进程、也不清定时器**，在坏挂载点上每次会泄漏一个进程。现改用 `Bun.spawn` + `AbortController`：超时会真正终止子进程（exit 143），并在 `finally` 里 `clearTimeout`。
- **M8 未做容量上限（已修复）。** `branchByDir` / `pendingSkillPerms` 现与 `sessionCtx` / `callCtx` 一样走 `setCapped`（上限 `MAP_CAP`），不再随进程生命周期增长。另：`skill_db.h()` 仍是死代码（0 调用）。
- **M9 重试静默失效（已修复）。** 旧版 `initDone = true` 在 `init()` 完成**之前**置位，任何早退都会让后续重试静默 no-op。现在 `initDone` 仅在 schema 创建成功后置位，并发调用由 `initInFlight` 去重，失败后仍可重试。
- **M11 MCP 服务列表只在插件初始化时解析一次。** 新增或改名一个 MCP 服务后，必须**重启 OpenCode** 它的调用才会被记录；在那之前这些调用是**不可见**的（只是被跳过，**不会**记错）。检测**失败即关闭**：若一个服务都识别不出来，宁可不写，也不去猜哪些工具是 MCP。只记**参数名**、绝不记参数值。
- **M12 插件清单和归属只在初始化时解析一次。** 静态扫描尽力从插件源码识别工具/命令：读包入口文件，再**跟着它自己 import 的相对路径模块跳一层**（有上界：3 个跳转、每个 256 KiB——发布出去的 `main` 常常只是几百字节的转发壳，真正的注册在它 import 的 chunk 里）；工具 id 取 `tool: {` 里**最浅的有键那一层**，再深就是某个工具自己的 `args: { query: … }`，不是工具。新增、升级、改名插件或它注册的工具/命令后，必须**重启 OpenCode** 才会刷新。工具无法归属时记录为 `(unknown)`；命令无法明确归属时**不记录**（失败即关闭，避免把 `/init` 等内置命令误记成插件）。每一行还记下**是哪份配置声明了它**（`global` / `localdir` / `project`）：后一次启动再见不到的 `global`/`localdir` 行会被删掉，所以页面上那一列说的是"启动时加载了什么"，不再是"这个库建好以来曾经见过什么"；`project` 行不在别处删——换个目录启动的会话本来就看不见它。
- **M13 内置工具 allowlist 与 OpenCode 1.18.33 对齐并硬编码。** 如果 OpenCode 升级后新增了内置工具，而 tracker 尚未更新，它可能被当作 `(unknown)` 插件工具记录；可用 `OPENCODE_SKILL_TRACKER_BUILTIN_TOOLS` 覆盖 allowlist，或等待 tracker 更新。

### 编码

- **M6 非 UTF-8 locale 乱码（已缓解）。** `--cli` 与 TUI 启动时会把 `stdout`/`stderr` 重设为 UTF-8（`errors="replace"`），因此**不再**抛 `UnicodeEncodeError`。但 `█ ⚠ … Δ` 等字符在极端 locale 下仍可能显示为替代字符；设 `LANG=C.UTF-8` 可完全避免。

### 本轮审计新增（M14–M18）

- **M14 历史行里可能仍有用户提示词原文（写入与导出已封住；库里数据已清理，备份文件仍有）。** 更早的版本把会话摘要写进 `metadata.summary`，其中包含**用户提示词原文**。写入处已删除、`__selftest` 也断言不再写入、`export` 现在只输出白名单键（`tool/call_id/agent/model/branch/source/error`）。2026-10-01 已用 `skillt scrub-metadata --yes` 清掉生产库里命中的 **40 行**（skill 17 / mcp 10 / plugin 13，用量行保留、`error` 文本 46 条保留）。**但清理之前形成的备份文件里仍是原文**——包括本次 `--yes` 之前自动写的那份回滚备份。2026-10-01 已把 3 个仍含原文的散落备份删除，并对生产库做了 VACUUM；`grep` 你的原话在 tracker 的所有文件里已经搜不到。任何时候都可以重跑 `skillt scrub-metadata` 验证：应当报告 0 行命中。
  另一个容易漏掉的事实：**删掉值不等于删掉字节**。WAL 模式下被替换的旧内容会留在 `-wal` 文件里直到 checkpoint，所以 `--yes` 结束时执行 `wal_checkpoint(TRUNCATE)`；库正忙时会明确告知，此时关掉 OpenCode 再跑一次 `skillt vacuum`。检查方法见 MAINTENANCE §3。
- **M15 没跑完的调用完全不留痕。** 用量行只在 `tool.execute.after` 或 `message.part.updated` 落地；`tool.execute.before` 仅把开始时间放在内存里。因此被中断、崩溃、或 after 钩子没触发的调用**一行都不会写**——不是记错，是**看不见**。`trigger_type` 的含义是"哪条路径先写入了这行"，不是"这个调用是怎么被发现的"。
- **M16 一次 git 失败会把该目录的 branch 永久缓存成 null。** `branchByDir` 缓存失败结果以避免热循环重复 fork（M7/M8 的取舍），直到 `vcs.branch.updated` 事件或进程退出才刷新。实测生产库里 598 行中 571 行 `branch` 为 null（主因是这些会话的工作目录本身不是 git 仓库，但一次 500ms 超时会把真仓库也钉成 null）。
- **M17 插件日志不轮转。** `~/.config/opencode/logs/skill-tracker.log` 只增不减；实测约 69 行/天（每次 init/dispose 各一行）。它同时是 `doctor log.errors` 唯一的错误来源——**日志被删掉等于错误历史被删掉**，所以清了日志要说明是清的。
- **M21 顾问的单次预算是抄来的默认值。** tracker 没法跨那道缝读 `AOS_TIMEOUT_MS`，所以"是否超预算"用的是 AgentOS 文档里的默认值 1200ms，除非用 `OPENCODE_SKILL_TRACKER_AOS_TIMEOUT_MS` 覆盖。AgentOS 若改了默认而这边没跟着改，这个标记就会**安静地失效**——和 M13 同一形状，所以写在这里而不是藏在常量里。
- **M18 后到的错误文本可能被丢弃。** 三条 UPSERT 都用 `metadata = COALESCE(已存在, 新来的)`：若 `after` 钩子先写了一行、随后事件路径带着真正的报错文本到达，`status` 会被纠正为 `error`（单调规则），但 `metadata.error` **不会**被补进去。承载去重不变量，本轮不动。

- **M20 `denied` 行在 OpenCode 1.18.33 上不可能出现。** 拒绝事件确实会送到 tracker——走事件总线的 `permission.asked` 再接 `permission.replied`（字段是 `requestID` 与 `reply: "reject"`，被拒的调用在 `tool.callID`，id 在 `id`；2026-10-01 实测），现在也能被正确记录。但宿主只对五个权限类别设门（`edit`、`bash`、`webfetch`、`doom_loop`、`external_directory`），**这五类都不是本 tracker 度量的对象**：skill、MCP 工具、插件工具都不询问直接执行，被拒的 `bash` 又是刻意不记录的。所以这个版本上任何表都不会出现 denied。旧代码还监听 `permission.updated`、读 `permissionID`/`response`——1.18.33 从不发这些名字，该错配已修，并由 `test_permission_rejection_writes_a_denied_row` 用真实形状钉住；剩下的这条是**宿主边界**，不是 tracker 缺陷。

### 插件自测（重要）

- **`__selftest()` 必须使用隔离数据库。** 自测会在库里插入 `call-test-*` 合成行。现在它会在**打开任何数据库之前**检查环境变量：
  - 未设置 `OPENCODE_SKILL_TRACKER_DB`，或
  - 该变量解析后等于默认生产路径
  则直接抛出 `Selftest requires isolated database`。请始终这样跑自测：

  ```bash
  OPENCODE_SKILL_TRACKER_DB=/tmp/st-selftest-$$.db \
    bun -e 'import("~/.config/opencode/plugin/skill-tracker.js").then(m => m.__selftest())'
  ```

  如果历史上被污染过，用 `skillt cleanup-selftest --yes` 清理。

### 插件导出契约（重要）

- **`plugin/skill-tracker.js` 只能通过 `export default { id, server }` 暴露插件工厂。**
  OpenCode 1.18.32 的加载器会把插件模块里**每一个导出函数都当作插件工厂**来调用，只有 default 导出是含 `server` 的对象时才会停止遍历：

  ```js
  export default { id: "skill-tracker", server: skillTrackerPlugin };
  ```

- **不要新增具名 `export function`**（`parseFrontmatter` / `sanitize` / `__selftest` 已经是这样导出的，仅供测试与手动自测使用）。如果 default 退回成裸函数 `export default fn`，加载器就会去遍历这些具名导出——`__selftest` 的隔离守卫会 `throw`，导致**整个插件加载失败、所有 hook 都不注册、`skill_usage` 永远是空的**。
- 排查方式：加载失败**不会**写进本插件自己的日志，只会出现在 OpenCode 的运行日志里：

  ```bash
  grep 'failed to load plugin' ~/.local/share/opencode/log/opencode.log | tail
  ```

  看到 `path=file://.../plugin/skill-tracker.js` 即为插件加载失败。
- 相关回归测试：`scripts/tests/test_plugin.py` 里的 `test_loader_does_not_enumerate_exports`（复刻加载器逻辑，需要 `bun`）。

### 同名 skill（潜在）

- `skills.name` **没有唯一约束**（`path` 才有）。当前 37 个 skill 的名称全部唯一，所以暂未触发。
- 若将来出现两个 SKILL.md 同名：
  - TUI 的 Skills 表以 `path` 作行 key，**不会崩溃**；
  - 但 `delete_skill` 按 `name` 删除，会**同时删掉同名项**的记录。删除前请确认名称唯一。
  - 开源 skill 从上游重新同步后可能出现重名，届时请注意。

### 状态语义

- **event 的 `error` 可单向覆盖已记录的 `success`。** 这是刻意的修正：若一次 skill 调用先被 `tool.execute.after` 记为 success，随后 event 报错，则状态会被纠正为 `error`；反向（success 覆盖 error/denied）**不会**发生，`denied` 也不会被覆盖。event 的错误文本不会并入 `metadata`。

---

## 10. 参与贡献

欢迎提交 Issue 与 Pull Request。提交前请确保测试全绿：

```bash
python3 -m pytest scripts/tests -q     # 仅标准库；TUI 测试会被跳过
```

改动请保持聚焦；若改动了已记录的行为，请**同时**更新 `README.md` 与
`README.zh-CN.md` —— `scripts/tests/test_readme.py` 会校验中文文档覆盖了全部
已知限制（M1–M13）。

## 11. 许可证

[MIT](LICENSE) © 2026 SHADE-glitch
