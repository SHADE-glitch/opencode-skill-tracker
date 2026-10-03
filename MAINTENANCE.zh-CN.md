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
| 数据库 | `~/.local/share/opencode/skill-usage.db`，0600，WAL，576 KiB |
| 行数 | 39 skills · 23 skill_usage · 550 mcp_usage · 111 plugin_usage · 76 skill_versions · 7 plugin_inventory |
| Schema | `PRAGMA user_version = 2`，`SCHEMA_VERSION = 2` |
| 导出文档 | `schema_version = 4`（4 = metadata 走白名单） |
| 测试 | 335 passed / 0 failed —— `python3 -m pytest scripts/tests -q` **和** `.venv/bin/python -m pytest scripts/tests -q` 两条路径都要绿；再用 `OPENCODE_SKILL_TRACKER_SKILLS_DIR=/tmp/does-not-exist` 跑一遍以证明测试是封闭的 |
| AgentOS 顾问存储（2026-10-03 实测；那是另一个进程的状态，会变） | `/home/shade/Public/AgentOS/store/aos.db`——22 条 telemetry、51 条召回（覆盖 8 条记忆）、14 条记忆、**83 个 loop 文件**。阶段耗时已经不是小数：最新的 loop 里 `validate` 跑到 31–37 秒，是 1200ms 预算的约 30 倍。**TUI 里已经没有 Advisor 页**（2026-10-03 按 owner 的要求移除）；`skillt agentos` 现在是唯一打开那个库的入口，`test_tui_never_reads_the_advisor_store` 钉住没有任何页签会去读它。`skillt agentos` 需要运行环境里有 `AGENT_OS_ROOT`；2026-10-01 起它已经 `export` 在 `~/.zshrc` 里，所以交互式 shell 有，非交互环境（cron、systemd、`env -i`）得自己设。某个 loop 是 live 测试样本还是真实用量，不由本项目代答；这里对那个库只读，最新的 loop 自己带着 `model` 标签。 |
| 插件日志 | `~/.config/opencode/logs/skill-tracker.log`，10.0 天 894 行（首行 2026-09-23），`[err]` 0 行 |
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
| 表格列宽由 `fit_columns()` 在渲染时算定；Textual 自己的自动列宽跑在 `_on_idle`，永远晚一帧，第一帧每列都只有表头那么宽 | `test_tui_dashboard_tables_fit_their_content_before_idle`、`test_tui_page_tables_cover_every_tab` |
| 推送前测试全绿，**两条文档里的命令都要跑** | `AGENTS.md` |

## 2. 每日（在相信任何数字之前）

```bash
skillt doctor            # 期望：0 FAIL
```

至少应看到：

- `db.quick_check` → `ok`
- `plugin.exists` / `plugin.hooks` / `plugin.mcp_hooks` / `plugin.plugin_hooks` → PASS
- `capture.freshness` → PASS 时**三条流各自**报距今时长；**WARN 会点名停滞的那条**——这正是这项检查存在的理由。以前它取三张表的最大值，只要还有一路活着就永远绿：某一路断了它不会误报，而是**该报的时候不报**。现在按流判定；从没记过行的流算 `no rows` 不算停滞，`OPENCODE_SKILL_TRACKER_STREAMS` 可以排除某条流但仍会打印它的年龄
- `log.errors` → `0 error line(s)`；非 0 说明有钩子抛过异常，详情就是最后那条 `[err]`
- `env.opencode_version` → 只有安装版本与 allowlist 对齐版本相同才 PASS
- 启用 §4 的定时器后，`backups.latest` → 约 1 天内

## 3. 每周

```bash
skillt cleanup-selftest          # 干跑：必须报告没有合成行
skillt scrub-metadata            # 干跑：必须报 0 行（M14 已闭环）
skillt sync --dry-run            # scanned == skills 行数，changed == 0
skillt agentos                   # 顾问 loop：超预算阶段、错误、召回是否真的进了提示、
                                 # 与可度量用量的连接（需要环境里有 AGENT_OS_ROOT）
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

- **TUI 里没有后台定时器。** 刷新是按页、事件驱动的：按键（5 秒节流）和切页只重读当前页，`r` 与首次绘制重读所有页；每页各自显示自己的 `data as of HH:MM:SS`。先试过 `set_interval`：在 textual 8.2.8 上，**只要在任何屏 mount 之后创建了 app 定时器**，`run_test` 收尾就会抛 `LookupError: <ContextVar name='active_app'>`——用空回调复现过，挂在 App 上和挂在 Screen 上都一样，`timer.stop()` 也救不回来。那会把整个 TUI 文件一次带走——今天 62 个用例，最初实测时是 47 个。`test_tui_creates_no_app_timers` 把这条钉住。
- **图形断言不许去问被测对象自己的辅助函数“返回了啥”。** Dashboard 排名柱那版把每根渲染出来的柱都和 `rank_bar_width(...)` 的返回值比，于是把阶梯压平成常数之后测试全绿——从被测对象推导期望值的守卫不是守卫。那一列后来按他的要求撤掉了，但这条教训留下：`test_tui_trend_rows_are_a_fixed_width` 在同一句里把绝对行宽（20）也写死了，不只是断言各行彼此相等。
- **TUI 的任何一页都不许打开顾问那个库。** Advisor 页在 2026-10-03 按 owner 的要求移除了；`skill_db.agentos_*` 和 `skillt agentos` 留着，所以那个库仍然能无头读，只是不能从页面上读。`test_tui_never_reads_the_advisor_store` 给唯一的入口装了探针，把 mount、`refresh_all` 和每一个页签都走一遍，调用数必须是 0。
- **顾问的三个召回计数绝不合并。** `retrieved` 是引擎自报，`recalled` 是
  `len(memory_ids)`，`injected` 是 `len(injected_memory_ids)`；假说可以被单独注入，
  合并就把一件真实的事藏起来了。以前在写着"recall"的列里打印 `retrieved` 是**报告错误**，
  不是风格问题。`test_the_recall_counts_surface_and_the_query_dict_does_not`
  在投影出来的 dict 上钉住这三个数，fixture loop 故意让三者互不相等，因为只有一个
  loop 时三个数恰好相等，任何测试都分辨不出来。
- **分派是显式的 kind 列表，以 `else: return` 收尾。** 它以前收尾在插件调用上，于是任何它没听过的
  kind 都会被塞进三个它没有的位置参数，处理器抛 `IndexError`。新增 kind 要加分支，不许落到
  `else`——Advisor 的 kind 跟着那一页一起删了，`test_tui_row_dispatch_ignores_unknown_kinds`
  仍然把这条钉住。行的身份绝不从 key 字符串里读出来：key 只是个查表用的记号
  （`dash-skill:<name>`、`recent#<n>`），真正的目标是 `app.row_targets` 里那个元组，因为把一个
  名字从 key（或从渲染出来的单元格）里拆回来，就等于让一个含分隔符的值变成另一行。
- **顾问的库只能从 `skill_db.agentos_*` 进，而且只以投影的形式出来。** 聚合层列
  `store/loops/*.json`，一次 `json.load` 一个文件，交出去的是 `_project_loop` 点过名的字段——
  调用方永远拿不到 `task_text`、召回 `query` 和阶段 payload。id 取自文件内容而不是文件名，
  所以不会用存储给来的文本拼路径。守卫是
  `test_task_text_and_payloads_are_never_read` 和
  `test_a_text_valued_count_field_is_never_counted`。以前那条规矩还需要一个 screen 来守，
  那个 screen 连同扫源码的用例一起被删掉了。
- **`bar(value, peak, width)`：`value > peak` 就会溢出，传 `None` 就抛异常。**
  顾问的逐阶段图是在真数据上学会这条的：峰值只取阶段时，一个真实 loop（最慢阶段 18ms、
  预算 1200ms）会画出 **1467 格**并把整页折行；fixture 里那个 4000ms 的阶段把问题完全遮住了。
  那张图跟着 Advisor 页一起没了。Dashboard 的趋势图还在调同一个函数，它们对这两条都安全，
  理由也是同一个：峰值就是它自己要画的那些值的 `max()`，而没有调用的那一天是 `0`，
  绝不是 `None`。
- **双击不需要新代码，但落点必须在一行之上。** Textual 在点击落在“已经持有行光标的那一格”时发
  `RowSelected`，也就是第二击；不引入点击链计时器，所以“不许用 app timer”仍然成立。
  在 `#dash-top`（120x40、5 行）上重新实测过：`(4, 1)`、`(2, 1)`、`(60, 1)`、`(3, 5)`
  这些落在行里的坐标都会打开那一行的详情页，而 `(4, 0)`——表头——什么也不打开。
  `test_tui_dashboard_row_double_click_opens_detail` 先把表头这一条断言掉，正例才不会是空的。
  给搬旧数字的人留一句：Advisor 表上那个 `(4, 2)` 的反例是**fixture 只有一行**造成的，
  行数多于一个时 y=2 就是一行，它会打开页面。
- **插件表面的扫描只跳一层，而且入口文件永远不受大小上限约束。** 发布出去的 `main` 经常只是个壳：
  `opencode-mem@2.28.1` 的 `dist/plugin.js` 只有 407 字节、什么都不注册，真正的工具在它
  `await import("./index.js")` 拉进来的 chunk 里。只扫入口于是把一个确实注册了工具的插件列成
  `tools=[]`，它每次调用都落进 `(unknown)`。现在扫描跟着入口自己的相对 import：
  `PLUGIN_SCAN_HOPS_MAX`（3 个）、`PLUGIN_SCAN_HOP_MAX_BYTES`（每个 256 KiB）、只在入口所在目录里，
  绝不递归——因为发现过程跑在 `init()` 里，宿主是盯着截止时间的。这两条边界都是靠踩坏另一条找出来的：
  第一版把大小上限也加在入口上，于是悄悄丢了 DCP 的 `compress`（一个 300 KiB 的 bundle，以前一直是整份读的）；
  把工具键规则收紧成 `id: {` 又丢一次，因为 DCP 写的是 `compress: cond ? a() : b()`。
  所以键规则保持宽松，改成过滤全大写的键——描述字符串里的 `MATCH USER LANGUAGE: ${…}`
  在正则眼里就是个键（`TOOL_KEY_NOISE_RE`）。守卫是
  `test_scan_follows_the_entry_shim_one_hop`、`test_scan_hops_are_bounded`、
  `test_scan_reads_a_large_entry_file_in_full`。三个真实插件上整个 `init()` 的实测成本：
  改前中位 45.1 ms，改后 44.7 ms。
- **表格列宽在这里算，不交给 Textual 去发现。** `DataTable` 在 `_on_idle` 里重算自动列宽，
  所以第一帧每列恰好是表头的宽度（`frozen-gnome-fork-maintenance` 显示成 `froze`），之后每一帧
  都滞后一帧的内容。`fit_columns()` 用手里的单元格算宽并把 `auto_width` 关掉；`_PAGE_TABLES`
  声明每个页拥有哪些表，于是只重测刚画过的那一页。Advisor 那页撤掉之后在 2026-10-03
  用生产库的只读快照重测过：**八张表合计约 3 ms**（四次跑出来 2.7–3.1 ms），其中 100 行的
  Recent 约占 1.5 ms，外圈整个 `refresh_all()` 是 21–24 ms；`plain_len` 的
  “没有方括号就不解析”快路是当年把这一趟从约 21 ms 压到约 3 ms 的原因。
  用例读宽度时**故意不**先 `pilot.pause()`——一 pause，idle 就把那一帧修好了，坏代码也会绿。
- **手画的图表行，宽度不许依赖数据。** 趋势行固定为 `TREND_ROW_WIDTH`（20），
  `.trend` 把高度钉到 `TREND_LINES`，`fmt_count` 把计数压到 5 字符以内。两条腿都不是摆设：
  只撤一条时另一条会把错位藏起来；钉高是**故意用"裁切"换"错位"**——窄于 70 列时三张一起裁（这个 70 是实测的，不是估的）。
  三个序列各自取峰值这一点仍是有意为之。守卫是
  `test_tui_trend_charts_share_one_line_when_a_series_is_huge`、
  `test_tui_trend_css_height_matches_the_line_count` 和
  `test_tui_trend_rows_are_a_fixed_width`；最后这条量的是**剥掉标记后**的文本，
  因为 `Static.content` 返回的原串带着标记。
- **行身份绝不从 row key 里 parse 回来。** 名字本身含分隔符（`@scope/pkg`、`conductor:newTrack`、带 `_` 的 server 名），所以每张表在渲染时把 `(kind, ...parts)` 注册进 `app.row_targets`。不要恢复 `split()`。
- **`unified_recent_rows` 额外返回详情页需要的列**（`skill_name`、`server_name`+`tool_name`、`plugin_name`+`item_kind`+`item_name`），显示用的 `name` 不是主键。
- **导出对 metadata 走白名单**（`EXPORT_METADATA_KEYS`）而不是整列倒出。文档形状因此升到 `schema_version = 4`；再改形状要升版本并同步 `test_export.py` / `test_plugin_db.py`。
- **`scrub-metadata` 原地改行而不是删行**，且 `--yes` 之前必先备份。

## 7. 已知限制

M1–M21 全文见 [README.zh-CN.md §9](#9-已知限制)（英文摘要在 [README.md](README.md#known-limitations)）。
维护时最容易咬人的几条：**M14**（历史行里的提示词原文——2026-10-01 已清理，但更早的备份里仍在）、**M19**（已修：备份曾有两个落点而保留策略只管一个——复查库旁边不该再出现 `skill-usage-backup-*.db`）、**M15**（没跑完的调用一行都不留）、**M16**（一次 git 失败会把该目录的 branch 永久钉成 null）、**M17**（日志不轮转）、**M18**（晚到的错误文本会被 `COALESCE` 丢掉）。

## 8. 暂缓（P2）——按性价比排序，并写明为什么不修

| 项 | 成本 | 为什么先不修 |
|---|---|---|
| `branchByDir` 负缓存无 TTL（M16） | 小 | 动采集路径；AGENTS.md 要求改采集必须带测试，且当前 null 的主因是会话目录本身不是 git 仓库 |
| `metadata` COALESCE 丢晚到错误文本（M18） | 中 | 位于承载去重不变量的 upsert 里 |
| init 里 MCP 探测阻塞约 1.5 秒（164 次 init 的 p90） | 中 | 调低 `MCP_STATUS_TIMEOUT_MS` 会误判服务列表，比启动慢更糟 |
| 日志不轮转（M17） | 小 | 需要先定策略（轮转 / 截断 / 交给 journald） |
| 每次调用 `skillt agentos` 都会重读 `store/loops/` 下的 loop 文件（`--limit N` 限制的是投影多少条，不是列多少条） | 小 | 那个库里目前只有 5 个 loop；文件名与 loop_id 之间没有可用约定，做缓存等于替别人持有第二份状态。以前让这件事变成“每次按键都要付”的是 Advisor 页，页没了，现在只有跑命令时才付 |
| 顾问聚合层读的是 AgentOS 的阶段字段名 | 小 | `retrieved` / `injection_chars` 属于引擎内部约定；改名只会让那几个数变空，不会连累别处，而且 `_count_only` 拒绝把文本当计数。原本有数字的地方变成 `-` 就是信号 |
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

## 11. 「全绿」是怎么被产出来的

测试只有在"跑它的那条命令本可以大声失败"时才算证据。下面四个坑，每一个都在这台机器上付过代价：

- **串联会把失败吞掉。** `a && b` 只报 `b` 的状态，`pytest -q | grep -c passed` 报的是
  `grep` 的。一次检查一条命令，取它自己的退出码，不要经过管道：
  `python3 -m pytest scripts/tests -q > /tmp/pt.log 2>&1; echo "exit=$?"`。
- **`${PIPESTATUS[0]}` 是 bash 的写法。** 这个项目是在 **zsh** 里驱动的，那里数组叫
  `$pipestatus` 而且**下标从 1 开始**；用 bash 的写法会展开成空字符串——于是一次失败的
  `git push` 打印出 `push exit=`，一个看起来像"检查过了"的空状态。本机实测：`false | true`
  之后 `${pipestatus[1]}` = `1`，而 `${PIPESTATUS[0]}` = **空**。取状态不要穿过管道。
- **看尾巴不等于看结论。** 一次 `tail -3` 把一个还在磁盘上的备份说成已被清理。这个仓库里
  根本没有 pytest 配置文件——没有 `addopts`，也不存在第二个 `-q` 能压掉的摘要行——所以计数
  一定在：读日志文件的最后一行，而不是屏幕上的碎片。
- **验红会留下探针。** 证明一个守卫能失败，就要短暂改一下实现或 fixture；而这个"短暂"要是
  留在了文件里，结果就是假绿。被跟踪的源码里出现 `TEMP-` 标记会被
  `test_no_canary_or_scratch_marker_survives_in_tracked_source` 拒绝——一个留在 conftest
  fixture 里的探针曾经带着 318 个通过跑完全程，所以这条不是洁癖。还原要重新读一遍文件确认，
  而不是假定最后一次编辑抵消了第一次。还原这条命令自己也会中途停：这台机器把 `rm` 和
  `cp` 别名成了交互模式，所以 `cp a b && pytest …` 卡在一个没人回答的提问上——canary 还
  留在文件里，而它后面那次“全绿”根本没跑。用 `command cp -f` / `command rm -f`，还原后
  再打一次 `git diff --stat`：空 diff 才是证明，一条被跳过的命令的 `exit=0` 不是。
