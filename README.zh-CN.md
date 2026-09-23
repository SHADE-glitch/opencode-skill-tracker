# OpenCode Skill Tracker (`skillt`)

记录并查询 OpenCode 里每个 skill 的使用情况：谁被调用、什么时候、成功还是失败、耗时多久、属于哪个项目。
数据全部落在本地一个 SQLite 文件里，不联网、不上传、不记录对话内容。

> **本仓库的安装方式见 [README.md](README.md)（英文）。** 本仓库是唯一来源，
> `~/.config/opencode/{plugin/skill-tracker.js,scripts,skill-tracker}` 与
> `~/.local/bin/skillt` 都是**指回本仓库的软链接**，执行 `./install.sh` 一键完成。
> 下文按**安装后的布局**描述；TUI 虚拟环境推荐放在 `./.venv`，旧的
> `~/.local/share/opencode/skillt-venv` 仍可用（启动器会自动回退）。

---

## 1. 项目介绍

OpenCode 目前**没有专门的 skill hook**。本工具通过通用的工具调用钩子来间接识别 skill 使用：

- OpenCode 把 skill 暴露为一个名为 `skill` 的工具（入参 `{ name }`）；
- 插件 `skill-tracker.js` 监听 `tool.execute.before` / `tool.execute.after` / `permission.ask` / `event`；
- 识别到工具名称为 `skill` 时，把调用写入 SQLite；
- `skillt` 负责查询、统计、导出、备份、体检。

设计约束（刻意遵守）：

- **不修改 OpenCode 源码**，只用公开的 Plugin Hook；
- **不要求在 SKILL.md 里加任何脚本或埋点**；
- 只记录结构化元数据（skill 名、状态、耗时、项目路径、session、模型/agent/分支），**不记录 secrets、不记录完整消息**；
- 单个插件文件 + 一个共享数据层 + 一个入口命令，尽量少依赖。

---

## 2. 架构

```
OpenCode 运行
   │  tool.execute.before/after, permission.ask, event
   ▼
~/.config/opencode/plugin/skill-tracker.js      (Bun 运行时, bun:sqlite)
   │  写入
   ▼
~/.local/share/opencode/skill-usage.db          (SQLite, WAL, 0600)
   │  读取
   ├── skillt                → skill-tui.py      (Textual TUI, 需要 venv)
   ├── skillt stats|top|...  → skill-stats.py    (旧版 CLI, 行为向后兼容)
   └── skillt insight|health|doctor|...  → skill-tui.py --cli  (无 textual 也能跑)
```

- **写入方**：只有插件（OpenCode 运行时）。所有写入都用 `UNIQUE(session_id, call_id)` 去重 + `ON CONFLICT` upsert。
- **读取方**：`skillt` 的所有命令。`health` 以只读方式打开数据库，绝不修改。
- **共享数据层**：`scripts/skill_db.py`，被 `skill-tui.py` 与测试共用；schema、迁移、查询、导出、备份都在这里。

---

## 3. 目录说明

| 路径 | 作用 |
|---|---|
| `~/.config/opencode/plugin/skill-tracker.js` | OpenCode 插件，唯一的写入方（**不要改捕获逻辑**） |
| `~/.config/opencode/scripts/skill_db.py` | 共享数据层（schema/迁移/查询/导出/备份） |
| `~/.config/opencode/scripts/skill-tui.py` | TUI + `--cli` 无头子命令 |
| `~/.config/opencode/scripts/skill-stats.py` | 旧版 CLI（命令/参数向后兼容，只读） |
| `~/.config/opencode/scripts/tests/` | pytest 测试 |
| `~/.config/opencode/skill-tracker/` | **本目录**：文档 + systemd 单元 |
| `~/.local/bin/skillt` | 统一入口（bash 分发器；`skill-tracker` 是它的软链接） |
| `~/.local/share/opencode/skill-usage.db` | 主数据库（WAL 模式，0600） |
| `~/.local/share/opencode/backups/` | 备份专用目录（0700），由 `auto-backup` 维护 |
| `~/.local/share/opencode/skillt-venv/` | 给 TUI 用的 Python 虚拟环境（含 textual） |

---

## 4. 命令说明

统一入口是 `skillt`。

### 4.1 交互式 TUI

```bash
skillt
```

需要真正的终端（`stdin`/`stdout`/`stderr` 三者都必须是 TTY，且 `TERM` 不能为空或 `dumb`）。
页面：**Dashboard / Skills / Recent / Categories / Data**。

**Dashboard 顶部的 5 张卡片**（每张卡片有名字，下面一行是数字）：

| 卡片 | 含义 |
|---|---|
| `Skills` | 库里已知的 skill 总数（`skills` 表行数） |
| `Uses` | 累计使用次数（`skill_usage` 行数） |
| `Today` | 今天（UTC）的使用次数 |
| `Personal` | 本地自建 skill 的数量（`category = personal`） |
| `OSS` | 来自开源上游的 skill 数量（`category = open-source`） |

卡片下方有图例说明；若 `Uses` 为 0，表格区会提示「还没有使用记录」。

| 按键 | 作用 |
|---|---|
| `Tab` | 切换页面 |
| `↑` `↓` / `j` `k` | 移动光标 |
| `Enter` | 查看选中 skill 详情 |
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
skillt auto-backup [--dry-run] [--json]
skillt doctor  [--json]
skillt cleanup-selftest [--yes]
```

- `insight`：最常使用 / 增长最快 / 长期未使用 / 失败率最高。
- `export`：导出 JSON（0600）；不指定 `--out` 则打印到 stdout。
- `sync`：重新扫描 `SKILL.md`，把内容变更记录进 `skill_versions`；`--dry-run` 只统计不写。
- `health`：把每个 skill 分成 **活跃（≤30 天）/ 沉寂（30–90 天）/ 未使用（>90 天或从未）**，并给出风险标记与建议。**只读、只建议、绝不自动删除。**
- `auto-backup`：在专用目录里创建备份并按保留策略清理旧备份（见 §6）。
- `doctor`：体检，输出 PASS/WARN/FAIL；**有 FAIL 时退出码为 1**。
- `cleanup-selftest`：清除 `__selftest()` 遗留的合成行（`project_path = /tmp/selftest-proj`）。默认 dry-run，`--yes` 才真删（先试跑一次确认有行可删，再自动备份后删除）。

参数校验：`--days ≥ 1`、`--min-uses ≥ 0`、`--limit ≥ 1`；非法值直接报错并以退出码 2 结束。
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
  `metadata` 是 JSON 文本（model / agent / branch / summary，已由插件清洗）。
- **`skill_versions`**：内容哈希历史，`UNIQUE(skill_name, content_hash)`。

### 5.2 视图

- `v_skill_totals`：每个 skill 的 total / success / errors / denied / last_used。
- `v_skill_last30`：近 30 天使用次数。
- `v_skill_history`：把 `metadata` 里的字段展开成列，便于查询。

### 5.3 运行参数

- `journal_mode = WAL`（读写可并发），`busy_timeout = 5000`，`synchronous = NORMAL`。
- 文件权限 `0600`；备份目录 `0700`。
- `source`（personal / open-source）是**派生值**，由 `category` 实时计算，**不落库**，避免重复存储。

### 5.4 直接查询示例

```bash
sqlite3 ~/.local/share/opencode/skill-usage.db \
  "SELECT skill_name, total, success, errors, last_used FROM v_skill_totals ORDER BY total DESC LIMIT 10;"
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

- 备份写入 `~/.local/share/opencode/backups/`（0700），命名固定为 `skill-usage-backup-YYYYMMDD-HHMMSS.db`。
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
| 时间对不上（差几小时） | 见「已知限制」M1：按天分桶使用 UTC。 |
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

以下问题在审计中确认存在。本轮修掉了界面 / 参数 / 编码相关的几项（M6、Data 页删除入口、参数校验、排序等），其余如实记录、**本次不修**。多数是边界情况，不影响日常使用。

### 数据与时间

- **M1 按天分桶使用 UTC。** `今日`、`按天趋势` 都以 UTC 计算。在 UTC+8 下，本地 00:00–08:00 的记录会被算进**前一天**（最多 8 小时/天的错配）。时间窗口本身（`-N days`）是正确的。如需本地日历，可自行用 `datetime(timestamp,'localtime')` 查询。
- **M2 纯读命令以读写方式打开 DB。** `insight`/`export`/`doctor` 用读写连接（`health` 已改为只读）。另外 `open_db(readonly=True)` 在**路径不存在**时会创建一个空库而不是报错。
- **M3 迁移的隐式提交。** `ensure_schema` 里的 `executescript` 会隐式提交当前挂起的事务；返回值里 `created_base` 从不置位、`created_versions` 恒为 `True`（仅影响该字典的语义，不影响实际建表）。

### 备份

- **M4 备份权限窗口。** `VACUUM INTO` 完成**之后**才 `chmod 0600`，存在极短的宽松权限窗口；若 VACUUM 失败，可能留下**未清理的半成品文件**（需手工删除）。

### 界面

- **M5 TUI 大库会假死。** `VACUUM`/备份/导出/刷新在 **UI 线程同步执行**，库很大时会短暂无响应。
- **M10 TUI 退出码。** `app.run()` 未包 `try/finally`，致命错误后进程退出码仍可能是 0。

### 插件

- **M7 git 子进程泄漏。** `resolveBranch` 的 500ms 超时**不杀 git 子进程、也不清定时器**；在坏挂载点上每次会泄漏一个进程。
- **M8 未做容量上限。** `skill_db.h()` 是死代码（0 调用）；`branchByDir` / `pendingSkillPerms` 没有容量上限，长期运行会缓慢增长。
- **M9 重试静默失效。** `initDone = true` 在 `init()` 完成**之前**置位；若 `init()` 抛错，后续重试会静默 no-op。

### 编码

- **M6 非 UTF-8 locale 乱码（已缓解）。** `--cli` 与 TUI 启动时会把 `stdout`/`stderr` 重设为 UTF-8（`errors="replace"`），因此**不再**抛 `UnicodeEncodeError`。但 `█ ⚠ … Δ` 等字符在极端 locale 下仍可能显示为替代字符；设 `LANG=C.UTF-8` 可完全避免。

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
