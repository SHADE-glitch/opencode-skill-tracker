# 维护清单

与 [README.md](README.md)（功能）、[README.zh-CN.md](README.zh-CN.md)（详尽参考）配套的**运行**文档：该查什么、按什么顺序查、健康长什么样、以及哪些是故意不修的。
英文版：[MAINTENANCE.md](MAINTENANCE.md)。

## 0. 最后一次实测的机器事实

测量时间 2026-10-01。使用前请重新测量，不要相信这里的数字。

| 项 | 值 |
|---|---|
| OpenCode | 1.18.33（`opencode --version`） |
| 插件 SDK | `@opencode-ai/plugin` 1.18.4 |
| 插件运行时 | Bun（`~/.bun/bin/bun`，`bun:sqlite`） |
| TUI venv | `.venv`（Python 3.13.14，textual 8.2.8） |
| 数据库 | `~/.local/share/opencode/skill-usage.db`，0600，WAL，582 KiB |
| 行数 | 39 skills · 21 skill_usage · 550 mcp_usage · 48 plugin_usage · 76 skill_versions · 5 plugin_inventory |
| Schema | `PRAGMA user_version = 2`，`SCHEMA_VERSION = 2` |
| 导出文档 | `schema_version = 4`（4 = metadata 走白名单） |
| 测试 | 289 passed / 0 failed —— `python3 -m pytest scripts/tests -q` **和** `.venv/bin/python -m pytest scripts/tests -q` 两条路径都要绿；再用 `OPENCODE_SKILL_TRACKER_SKILLS_DIR=/tmp/does-not-exist` 跑一遍以证明测试是封闭的 |
| 插件日志 | `~/.config/opencode/logs/skill-tracker.log`，7.7 天 533 行，`[err]` 0 行 |
| 备份定时器 | **已启用** —— `systemctl --user is-enabled skillt-auto-backup.timer` → `enabled`，下次 00:09 CST 每日触发，`Linger=yes`；已实跑一次 service 验证（exit 0、生成备份、删除 0） |
| 散落备份（M19） | `~/.local/share/opencode/` 里有 5 个**在 `BACKUP_DIR` 之外**的文件，保留策略永不到达；其中 2026-10-01 之前的 4 个仍含 M14 原文 |
| 备份默认路径 | **已修**（M19）：`skillt backup`、TUI 的 `b`、以及 `--yes` 前的自动回滚备份现在都落进 `BACKUP_DIR`。2026-10-01 把最后一个散落在库旁边的文件收了进来，保留策略第一次看全了所有备份——它的 dry-run 报 `kept: 2, delete: 1`（09-23 同一天里较旧的那份），这个删除会在下一次夜间任务发生 |
| metadata 清理 | 2026-10-01 已执行 `scrub-metadata --yes`，剥掉 40 行（skill 17 / mcp 10 / plugin 13），用量行与 46 条 `error` 文本全部保留；另外删除 3 个仍含原文的散落备份，并对生产库做了 VACUUM。此后 `skillt scrub-metadata` 必须报 0 行，且 `grep -l '<一段已知原文>' ~/.local/share/opencode/skill-usage.db*` 必须什么都搜不到 |

安装布局——四个位置都是**指回本仓库的符号链接**，所以改仓库即生效、无需重装；但改插件必须**重启 OpenCode**：

```
~/.local/bin/skillt                          -> <repo>/bin/skillt
~/.config/opencode/scripts                   -> <repo>/scripts
~/.config/opencode/skill-tracker             -> <repo>/skill-tracker
~/.config/opencode/plugin/skill-tracker.js   -> <repo>/plugin/skill-tracker.js
```

核对：`skillt doctor`（`plugin.exists`、`env.*`），或
`readlink -f ~/.local/bin/skillt ~/.config/opencode/scripts`。
若其中某个变成了**普通副本**，它会静默地不再跟随仓库。`install.sh` 拒绝覆盖非符号链接；确认副本里没有你需要东西之后再 `--force`。

## 1. 不变量（永不可破）

每条都有测试守着。下列测试变红，说明**不变量被改了**，而不是测试写错了。

| 不变量 | 守护 |
|---|---|
| 插件是**唯一写入者**；`skill_db.py` / TUI / 旧版 CLI 绝不记录用量 | `AGENTS.md`、`test_plugin_db.py` |
| 导出契约 `export default { id, server }`。加具名 `export function` 或退回裸函数默认导出，会让**整个插件加载失败**、采集静默停止 | `test_loader_does_not_enumerate_exports` |
| 一次调用一行：`UNIQUE(session_id, call_id)` + `ON CONFLICT` upsert | `test_plugin_db.py`、`test_mcp_db.py` |
| 状态单调：观测到的 `error` 可覆盖乐观写入的 `success`，反向永不允许 | 三条 `UPSERT_*_SQL` 的 CASE |
| MCP 只记**参数名**。`mcpArgNames()` 只用 `Object.keys()`，绝不读值 | `test_plugin.py` |
| 绝不读取消息正文，因此也不会存储 | `test_readme.py`、`__selftest` 断言 |
| schema 有两份副本：插件 `TABLES_SQL`/`VIEWS_SQL` 与 Python `SCHEMA_SQL`，必须同一提交里一起改 | `test_plugin_and_python_schema_do_not_drift` |
| 改视图必须升 `SCHEMA_VERSION`，否则老库继续用旧视图（`CREATE VIEW IF NOT EXISTS` 不更新） | `test_migration.py`、`test_schema_sync.py` |
| 采集相关测试是封闭的：不得读真实 `~/.config/opencode/skills`，也不得读真实插件日志 | `temp_skills` fixture、`_isolated()` 环境变量、`test_doctor.py` 的 monkeypatch |
| 推送前测试全绿，**两条文档里的命令都要跑** | `AGENTS.md` |

## 2. 每日（在相信任何数字之前）

```bash
skillt doctor            # 期望：0 FAIL
```

至少应看到：

- `db.quick_check` → `ok`
- `plugin.exists` / `plugin.hooks` / `plugin.mcp_hooks` / `plugin.plugin_hooks` → PASS
- `capture.freshness` → PASS 并点名表和距今时长；**WARN 意味着插件停止写入了**，这正是这项检查存在的理由
- `log.errors` → `0 error line(s)`；非 0 说明有钩子抛过异常，详情就是最后那条 `[err]`
- `env.opencode_version` → 只有安装版本与 allowlist 对齐版本相同才 PASS
- 启用 §4 的定时器后，`backups.latest` → 约 1 天内

## 3. 每周

```bash
skillt cleanup-selftest          # 干跑：必须报告没有合成行
skillt scrub-metadata            # 干跑：必须报 0 行（M14 已闭环）
skillt sync --dry-run            # scanned == skills 行数，changed == 0
wc -l ~/.config/opencode/logs/skill-tracker.log    # 增长观察（M17）
```

清理删掉的是**值**；WAL 模式下旧字节会留在 `-wal` 里直到 checkpoint。这就是
`scrub-metadata --yes` 结束时执行 `wal_checkpoint(TRUNCATE)` 的原因，库忙它会明说。
用你确定写过的原文验证：

```bash
grep -l '一段你确定写过的原文' ~/.local/share/opencode/skill-usage.db*   # 必须无输出
```

再确认没有备份躲在保留策略够不着的目录外。M19 已修、旧散落文件已于 2026-10-01 处理，所以这里应当**一个都不剩**：

```bash
for f in ~/.local/share/opencode/skill-usage-backup-*.db; do
  printf '%s  summary_rows=%s\n' "$(basename "$f")" \
    "$(sqlite3 -readonly "file:$f?mode=ro" "SELECT (SELECT COUNT(*) FROM skill_usage WHERE json_extract(metadata,'\$.summary') IS NOT NULL)+(SELECT COUNT(*) FROM mcp_usage WHERE json_extract(metadata,'\$.summary') IS NOT NULL)+(SELECT COUNT(*) FROM plugin_usage WHERE json_extract(metadata,'\$.summary') IS NOT NULL);" 2>/dev/null || echo '未迁移的老库')"
done
```

然后做这份清单里**最有价值的一步**：拿 OpenCode 自己的记录对账。它能在别的检查全绿时抓到"采集悄悄变了形"。

```bash
# 1) tracker 自己记了什么（按 server，覆盖它自己的时间窗）
sqlite3 -readonly ~/.local/share/opencode/skill-usage.db \
  "SELECT server_name, COUNT(*), SUM(status='success'), SUM(status='error')
   FROM mcp_usage GROUP BY 1;"

# 2) 地面真相：opencode.db 的 part 表，同一时间窗
sqlite3 -readonly "file:$HOME/.local/share/opencode/opencode.db?mode=ro" \
  "WITH t AS (SELECT json_extract(data,'\$.tool') tool,
                    json_extract(data,'\$.state.status') st, time_created tc
             FROM part WHERE json_extract(data,'\$.type')='tool')
   SELECT substr(tool,1,instr(tool,'_')-1) server, COUNT(*), SUM(st='completed'), SUM(st='error')
   FROM t WHERE tc > strftime('%s','<tracker 覆盖起点>')*1000
        AND (tool LIKE 'playwright_%' OR tool LIKE 'basic-memory_%')
   GROUP BY 1;"
```

按 server 的次数必须对得上。**已知且可解释的差异**只有两类：会话后来被删掉（tracker 保留、`part` 消失）；以及早于 tracker 安装的调用。**出现无法解释的新缺口就是采集回归**——先看 `log.errors`，再确认 OpenCode 重启时插件有没有真的加载。

## 4. 每月 / 任何改动之后

```bash
systemctl --user is-enabled skillt-auto-backup.timer   # 必须输出：enabled
sqlite3 -readonly ~/.local/share/opencode/skill-usage.db "PRAGMA integrity_check;"
python3 -m pytest scripts/tests -q                     # 不需要 venv
.venv/bin/python -m pytest scripts/tests -q            # 含 TUI 全量
OPENCODE_SKILL_TRACKER_SKILLS_DIR=/tmp/nope python3 -m pytest scripts/tests -q   # 封闭性证明
bash -n bin/skillt                                     # dispatcher 语法
```

备份只有真的在跑才算数。一次性启用：

```bash
mkdir -p ~/.config/systemd/user
cp skill-tracker/systemd/skillt-auto-backup.* ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now skillt-auto-backup.timer
loginctl enable-linger "$USER"       # 没登录会话也照跑
```

保留策略（`skill_db.py`）：最近 30 个每日 + 12 个每月；**绝不**碰 120 秒内的文件，也**绝不**碰名字不匹配 `skill-usage-backup-<日期>-<时间>.db` 的文件。

## 5. 升级 OpenCode 之后

内置工具 allowlist 钉在某个版本（M13），MCP/插件清单只在 init 解析（M11/M12）。所以：

1. `opencode --version`，记下版本号。
2. `skillt doctor` → `env.opencode_version` 必须因为漂移而 WARN。
3. 拿真实列表：OpenCode server 在跑时 `curl -s localhost:<port>/experimental/tool/ids`。
4. 同时更新 `plugin/skill-tracker.js` 里的 `DEFAULT_BUILTIN_TOOLS` **和**同一行那句 `verified against OpenCode X.Y.Z` 注释——`doctor` 解析的就是那句注释，漏掉它等于重新制造它要检的漂移。
5. 重启 OpenCode，用 `skillt plugins` 确认清单已刷新。

6. 大版本升级后**重新测量事件载荷**。字段名已经咬过一次（M20：代码照 SDK 类型写，
   宿主发的却是另一套名字），所以这一步的原则是**观测，而不是相信文档**。做法：在
   `/tmp` 建一个一次性项目，放一个探针插件，只记录
   `permission.asked` / `permission.replied` / `command.execute.before` 的**键名与枚举值**，
   用 `OPENCODE_SKILL_TRACKER_DB` 指向临时库跑一次，再把到达的字段和 tracker 读取的
   字段逐一对。探针请只留在 `/tmp`：它会打印字段值，用在合成沙箱会话里可以，放在有
   真实文本的地方不行。

不想改插件就设 `OPENCODE_SKILL_TRACKER_BUILTIN_TOOLS`——但那样 `env.opencode_version` 会**故意**一直 WARN。

## 6. 刻意为之的偏离（别"顺手修回去"）

- **TUI 里没有后台定时器。** 屏幕在按键（5 秒节流）、切页、`r` 时重读，并显示 `data as of HH:MM:SS`。先试过 `set_interval`：在 textual 8.2.8 上，**只要在任何屏 mount 之后创建了 app 定时器**，`run_test` 收尾就会抛 `LookupError: <ContextVar name='active_app'>`——用空回调复现过，挂在 App 上和挂在 Screen 上都一样，`timer.stop()` 也救不回来。那会一次带走 47 个 TUI 测试。`test_tui_creates_no_app_timers` 把这条钉住。
- **行身份绝不从 row key 里 parse 回来。** 名字本身含分隔符（`@scope/pkg`、`conductor:newTrack`、带 `_` 的 server 名），所以每张表在渲染时把 `(kind, ...parts)` 注册进 `app.row_targets`。不要恢复 `split()`。
- **`unified_recent_rows` 额外返回详情页需要的列**（`skill_name`、`server_name`+`tool_name`、`plugin_name`+`item_kind`+`item_name`），显示用的 `name` 不是主键。
- **导出对 metadata 走白名单**（`EXPORT_METADATA_KEYS`）而不是整列倒出。文档形状因此升到 `schema_version = 4`；再改形状要升版本并同步 `test_export.py` / `test_plugin_db.py`。
- **`scrub-metadata` 原地改行而不是删行**，且 `--yes` 之前必先备份。

## 7. 已知限制

M1–M19 全文见 [README.zh-CN.md §9](#9-已知限制)（英文摘要在 [README.md](README.md#known-limitations)）。
维护时最容易咬人的几条：**M14**（历史行里的提示词原文——2026-10-01 已清理，但更早的备份里仍在）、**M19**（已修：备份曾有两个落点而保留策略只管一个——复查库旁边不该再出现 `skill-usage-backup-*.db`）、**M15**（没跑完的调用一行都不留）、**M16**（一次 git 失败会把该目录的 branch 永久钉成 null）、**M17**（日志不轮转）、**M18**（晚到的错误文本会被 `COALESCE` 丢掉）。

## 8. 暂缓（P2）——按性价比排序，并写明为什么不修

| 项 | 成本 | 为什么先不修 |
|---|---|---|
| `branchByDir` 负缓存无 TTL（M16） | 小 | 动采集路径；AGENTS.md 要求改采集必须带测试，且当前 null 的主因是会话目录本身不是 git 仓库 |
| `metadata` COALESCE 丢晚到错误文本（M18） | 中 | 位于承载去重不变量的 upsert 里 |
| init 里 MCP 探测阻塞约 1.5 秒（164 次 init 的 p90） | 中 | 调低 `MCP_STATUS_TIMEOUT_MS` 会误判服务列表，比启动慢更糟 |
| 日志不轮转（M17） | 小 | 需要先定策略（轮转 / 截断 / 交给 journald） |
| `plugin_inventory` 对本地插件显示绝对路径 | 观感 | 需要只显示层的短化 + 测试 |
| `skill_versions` 无上限增长 | 小 | 需要保留策略；目前没有任何清理 |

### 已用 live 验证结清（2026-10-01）

两条此前"只有单测"的采集路径，已在 `/tmp` 沙箱里对真实 `opencode run` 跑通（库用
`OPENCODE_SKILL_TRACKER_DB` 隔离、模型 `opencode/space-bunny-free`、提示词全合成、
用绝对路径调用二进制以绕开 shell alias）：

- **插件命令采集是通的。** `--command dcp-compress` 触发了
  `command.execute.before`，写下
  `@tarquinen/opencode-dcp@3.2.0 / command / dcp-compress / command_call / unknown`。
- **拒绝采集当时是坏的，现已修好。** OpenCode 1.18.33 发的是
  `permission.asked` → `permission.replied`：id 在 `id`、被拒调用在 `tool.callID`、
  答复是 `reply` 且用 `requestID` 关联。而 tracker 监听的是 `permission.updated`、
  读的是 `permissionID`/`response`——这个宿主从不发这些名字，于是**每一次真实拒绝都
  什么都没写**，生产里 `denied` 为 0 不是"没遇到"而是"记不下"。现在能正确落库，但
  M20 依然成立：宿主只对 `edit`/`bash`/`webfetch`/`doom_loop`/`external_directory`
  设门，这五类都不在度量范围内，所以仍应预期 `denied` 为 0 行。

## 9. 回滚

一次提交一个改动，代码先于文档（Conventional Commits）。退回某个阶段：

```bash
git log --oneline -12
git revert <sha>          # 优先 revert，不要 reset；测试必须保持全绿
```

`scrub-metadata --yes` 是本项目唯一的破坏性操作。它在应用**之前**会在数据库旁边写一个 `skill-usage-backup-*.db`，所以恢复方式是：先停 OpenCode，再
`cp ~/.local/share/opencode/skill-usage-backup-<ts>.db ~/.local/share/opencode/skill-usage.db`。

## 10. 每个阶段的报告格式

本项目的每次改动都按同样五项汇报，让读者不用重新推导就能验收或否决：

1. **改了什么**——说行为，不说代码。
2. **怎么看得到**——能演示出来的那条命令或那次按键。
3. **测试增减**——之前 → 之后的数字，以及"先红后绿"的证据。
4. **偏离之处**——与批准计划不同的地方及原因。
5. **回滚方式**——该 revert 哪个提交。
