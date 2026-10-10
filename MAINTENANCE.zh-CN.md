# 维护清单

与 [README.md](README.md)（功能）、[README.zh-CN.md](README.zh-CN.md)（详尽参考）配套的**运行**文档：该查什么、按什么顺序查、健康长什么样、以及哪些是故意不修的。
英文版：[MAINTENANCE.md](MAINTENANCE.md)。

## 0. 最后一次实测的机器事实

机器身份（OpenCode、Bun、解释器、textual）在
[`docs/maintenance/compat-matrix.md`](docs/maintenance/compat-matrix.md) §1；本项目按名字
依赖的宿主接口在
[`docs/maintenance/opencode-interface.md`](docs/maintenance/opencode-interface.md)；下面这些
体积、速率、时长全部是**一次运行的结果**，所以搬到了带命令的
[`docs/maintenance/measurements.md`](docs/maintenance/measurements.md)，不再留在这张表里。
搬家的理由：清单里的数字会被永久读成「现在」，而这张表原先的两个数字搬家时**已经错了**
（「约 69 行/天」实测 74.4；「8.2–15.8 ms」实测 12.2–14.0）。

| 项 | 值 |
|---|---|
| OpenCode | **跑的是哪个版本是一条读数，不是这份文件能替它固定的东西**——compat-matrix §1 记那四个 pin 和主机当前版本，`skillt doctor` 的 `env.opencode_version` 拿运行版本去比对内置白名单的 pin（`PIN_TOOL_IDS`）。内置白名单 2026-10-03 已从宿主端重读，那十四个 id 没变 |
| 插件 SDK | `@opencode-ai/plugin` 1.18.4 —— 即 `PIN_SDK_TYPES`，记下来是**因为它和运行的宿主不一致**（M20），不是因为谁 import 它 |
| 插件运行时 | Bun（`~/.bun/bin/bun`，`bun:sqlite`） |
| TUI venv | `.venv`（Python 3.13.14，textual 8.2.8） |
| 数据库 | `~/.local/share/opencode/skill-usage.db`，0600，WAL——体积、`-wal` 与各表行数：measurements §2 |
| 行数 | 各表计数与 `metadata` 键普查：measurements §2、§3。这张表里**稳定**的是形状——七张表，`skills` 加四条用量流——以及 `title` 普查读到 **0 个键**这件事（那是 S1 的承诺，由 `test_the_writer_never_names_a_permission_title` 守着）。至于「12 个 surface 里 10 个从没被调用」那条库存读数，最后一次是 2026-10-09 用 `skillt plugins` 读的；要重读就去那条命令，别在这里抄 |
| Schema | `PRAGMA user_version = 2`，`SCHEMA_VERSION = 2`。2026-10-04 加 `subagent_usage` 时**没有** bump：这个计数器是为视图存在的（`CREATE VIEW IF NOT EXISTS` 改不了已有视图），而这次加的是表，`ensure_schema()` 每次都会幂等建表 |
| 导出文档 | `schema_version = 6`（4 = metadata 走白名单，5 = 多了 `plugin_inventory.scope`，6 = 新增 `subagent_usage` 这个键）。`PRAGMA user_version` / `SCHEMA_VERSION` 仍是 **2**：这个计数器存在，是因为 `CREATE VIEW IF NOT EXISTS` 改不了已有的视图，而这一轮加的是**表**、不是视图——`ensure_schema()` 每次都会跑建表 DDL，所以不该 bump |
| 测试 | 三条命令、按这个顺序、各自证明什么：measurements §10。通过数是一次**运行**的结果（系统解释器、venv、CI 三个数不一样，而 AGENTS.md 禁止把这种总数抄进文档），所以这里不再写它——这张行写下时是 530，现在早就不是 530。稳定的部分：shipped `__selftest()` 的下限由 `test_selftest_is_green_end_to_end` 钉住，配方在 measurements §5，而第三条命令（`OPENCODE_SKILL_TRACKER_SKILLS_DIR=/tmp/does-not-exist`）才是证明这轮绿没有拿本机真实文件换来的那一条 |
| AgentOS 顾问存储（那是另一个进程的状态，会变） | `/home/shade/Public/AgentOS/store/aos.db` 加 `store/loops/*.json`，只经 `skill_db_agentos` 打开、只读 URI、绝不上重画路径——规则与理由在 compat-matrix §4 和 `test_module_boundaries.py`。它的事件数/行数/loop 数、各阶段耗时都是读数，所以不存这里，要就用 `skillt agentos` 重读。**稳定的是**：TUI 里已经没有 Advisor 页（2026-10-03 按 owner 的要求移除），`test_tui_never_reads_the_advisor_store` 钉住没有任何页签会去读它；`skillt agentos` 需要运行环境里有 `AGENT_OS_ROOT`，2026-10-01 起它已经 `export` 在 `~/.zshrc` 里，所以交互式 shell 有，非交互环境（cron、systemd、`env -i`）得自己设。某个 loop 是 live 测试样本还是真实用量，不由本项目代答；最新的 loop 自己带着 `model` 标签 |
| claude-mem 邻居（那是另一个进程的状态，会变） | `~/.claude-mem/`——它账本的计数、`inject-trace.log` 的行数、worker 日志的分级计数都是**对别人正在追加的文件做一次读数**，所以跟着命令走：读取代价在 measurements §6，读数本身在 `skillt claude-mem`。稳定的是：读取端按 mtime 挑**最新那个带日期**的 worker 日志、其余不动（`test_only_the_dated_worker_log_is_read`）、外来日志行只数不读、`~/.claude-mem/settings.json` 永不打开、`startToken` 永不读。读这三样的代价就是 Plugins 页那一行 dim 文本每次重画要付的钱，也是为什么 `claude_mem_http` 绝不在这条路上被调用——这个数随邻居日志的大小移动，所以要重测而不是引用。它的库存行与活动行说的是两件事：`claude-mem-inject.js` 注册的那些工具可以一直是 0 次调用，而注入行仍然反映当前状态 |
| 插件日志 | `~/.config/opencode/logs/skill-tracker.log`。体积、行数、行/天、错误行数——都是增长读数，配方在 measurements §4。这里留它**内容的形状**。里面有 **4 行 `[err]`，全是 `selftest FAIL: …`，是本项目的自测在 2026-10-04 07:32Z 写进去的**——那时子 agent 的断言还是红的。`doctor` 的 `log.errors` 会因此 WARN，而它们是测试残留、不是采集故障，这就是它们还在、也仍然被报出来的原因。**它们暴露的缺口已经修掉**（2026-10-04）：`__selftest()` 现在会把自己的日志改写到临时库旁边的 `skill-tracker-selftest.log`，除非用 `OPENCODE_SKILL_TRACKER_LOG` 明确指定别的路径——所以文档里那条手工配方再也碰不到这个文件；2026-10-09 照 measurements §5 重跑过一次并对比这份日志的 sha256，前后一致。别手工去删活日志里的行 |
| 备份定时器 | **截至 2026-10-10 00:07 是「没装」** —— `systemctl --user is-enabled skillt-auto-backup.timer` → `not-found`，`~/.config/systemd`、`/etc/systemd`、`~/.local/share/systemd` 下都搜不到任何 `skillt*` 单元文件，而 `journalctl --user -u skillt-auto-backup.timer` 停在 `Stopped … 2026-10-07 21:48:34`。service 最后一次成功运行是 **2026-10-07 00:18**，所以 `backups.latest` 现在是 3.0 天，并在 **2026-10-14** 前后越过它的 7 天 WARN 线。`Linger=yes` 仍然开着，也就是说会话这边没有任何东西阻止这个单元——它就是没了。**这一轮没人删它，这一轮也没人把它装回去**：装 systemd 单元是本项目之外的改动，而 S7 主人拍板的是 `install.sh` 把它藏在 `--with-timer` 后面。要重新上弦就 `./install.sh --with-timer`，然后看 `systemctl --user status skillt-auto-backup.timer` 和 `skillt doctor` 的 `backups.latest`。关于这个单元本身**已验证**的是：`systemd-analyze verify --user` 接受这两个文件，两条 `ExecStart` 的完整命令行用已安装的启动器手工跑过（`below the cap; nothing to rotate`，日志 sha256 未变）。另外，手工起 service 还会按保留策略真的清理备份，所以那不是例检 |
| `doctor` 以前看不见、现在能看见的东西 | 直到 2026-10-10，**没有任何检查在问「定时器装了吗」**，这一行就是证据：连续三天 `backups.latest` 报 PASS——而且报得没错，备份确实才 3.0 天、阈值是 7 天——而那个负责产生备份的东西已经被移除了。**计划不等于文件。** 现在 `backups.scheduled` 会去问 `systemctl --user is-enabled skillt-auto-backup.timer`，只要不是 `enabled` 就 WARN，**包括「问不到 systemd」**（没有用户 systemd 的机器是「未知」，不是「健康」）。它永不 FAIL，因为「不装这个单元」本来就是主人做的合法选择。这两条检查**就是要能不一致**：本机今天的读数正是 `backups.latest` PASS 而 `backups.scheduled` WARN，这一对不一致本身就是那条发现 |
| 散落备份（M19） | **这一行原先描述的那部分已经了结**——2026-10-10 重读：`ls ~/.local/share/opencode/` 里 `BACKUP_DIR` **之外**已经没有任何 `skill-usage-backup-*.db`，六份备份全在里面，也就是保留策略能扫到的地方。还剩下的一个散落文件不是备份：`skill-usage-export-20260923-100620.json`，`schema_version = 1`，40,390 字节。对它做一次完整键遍历（32 个不同键、每个列表的每一个元素）后**找不到** `summary`、`title`、`error`、`text`、`prompt`、`body` 任一键，所以它不带 M14 那些 metadata 原文——但它确实带着 skill 的 `description`、`path` 和导出那台机器的 `db_path`，而一份 skill 库存导出本来就该带这些。`BACKUP_DIR` 里每一份备份也用同样方式查过 `metadata` 含 `summary`/`title` 的行：**六份全是 0** |
| 备份默认路径 | **已修**（M19）：`skillt backup`、TUI 的 `b`、以及 `--yes` 前的自动回滚备份现在都落进 `BACKUP_DIR`。2026-10-01 把最后一个散落在库旁边的文件收了进来，保留策略第一次看全了所有备份——它的 dry-run 报 `kept: 2, delete: 1`（09-23 同一天里较旧的那份）。**这个删除并没有发生**，因为 2026-10-07 起停掉的就是那个会跑它的定时器；两份备份今天都还在，这正是上面「计划不等于文件」那一行的证据 |
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
| 绝不读取消息正文，因此也不会存储；session 的 `title` 同样不读（宿主用它的首条消息生成它）。写入端只留任何代码真正会读的字段 | `test_readme.py`、`test_the_session_handler_keeps_only_the_fields_it_uses`、`__selftest` 断言 |
| 权限事件的 `title` 写入端同样**从不读取**——不是靠事后 scrub 才没落库。`skillt scrub-metadata` 名单里留着 `title`，只为处理老版本已经写下的行 | `test_the_writer_never_names_a_permission_title`、`test_a_denied_call_stores_no_title_but_still_stores_the_denial`、`__selftest` |
| schema 有两份副本：插件 `TABLES_SQL`/`VIEWS_SQL` 与 Python `SCHEMA_SQL`，必须同一提交里一起改 | `test_plugin_and_python_schema_do_not_drift` |
| 改视图必须升 `SCHEMA_VERSION`，否则老库继续用旧视图（`CREATE VIEW IF NOT EXISTS` 不更新） | `test_migration.py`、`test_schema_sync.py` |
| 采集相关测试是封闭的：不得读真实 `~/.config/opencode/skills`，也不得读真实插件日志 | `temp_skills` fixture、`_isolated()` 环境变量、`test_doctor.py` 的 monkeypatch |
| 表格列宽由 `fit_columns()` 在渲染时算定；Textual 自己的自动列宽跑在 `_on_idle`，永远晚一帧，第一帧每列都只有表头那么宽 | `test_tui_dashboard_tables_fit_their_content_before_idle`、`test_tui_page_tables_cover_every_tab` |
| **排班要按排班查**。`backups.latest` 很新**不能**证明下一次备份会来；「问不到 systemd」是未知，永远不算健康 | `test_a_removed_timer_is_warned_about_even_though_the_backup_is_recent`、`test_the_timer_check_never_fails_and_never_shells_out_uninvited` |
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
- `backups.scheduled` → `skillt-auto-backup.timer enabled`。**这一条和上面那条说的是两件
  不同的事**：`not-found` 配上 `backups.latest` 的 PASS，正是一台「定时器已被移除、而最后
  一份备份看上去还很新」的机器的状态。如果它说「问不到 systemd」，那是**未知**、不是健康——
  没有用户 systemd 的机器上，请自己决定 `auto-backup` 与 `rotate-log` 由谁来跑，别把沉默当成通过

## 3. 每周

```bash
skillt cleanup-selftest          # 干跑：必须报告没有合成行
skillt scrub-metadata            # 干跑：必须报 0 行（M14 已闭环）
skillt sync --dry-run            # scanned == skills 行数，changed == 0
skillt claude-mem                # claude-mem 自己的账本：它后台采集有多新鲜、token 合计、
                                 # 观察器连续失败数（那个库不存在时 doctor 一句不提）。
                                 # 账本下面还会打印它为自己写的两个日志文件（注入了多少、
                                 # ERROR 多少行）与 `worker.pid` 的存活，然后才是 worker
                                 # 那三个 HTTP 回答。`worker log ERROR …` 这一行才是
                                 # 「账本看着挺新鲜、但后台同步一直在失败」的信号。
skillt agentos                   # 顾问 loop：超预算阶段、错误、召回是否真的进了提示、
                                 # 与可度量用量的连接（需要环境里有 AGENT_OS_ROOT）
# 增长观察（M17）：体积、行数、行/天、错误行数、有没有被上界截断——一份配方、一个地方：
# docs/maintenance/measurements.md §4。别在这里用一条裸的 `wc -l` 重新推导：
# 没有与上界对照的行数分不清"总数"和"下限"，而那正是 M17 唯一要紧的差别。
skillt rotate-log                # 同一个上界落到写的一侧：长到 4 MiB 就把日志**改名**挪走，
                                 # 保留 log.keep_files（默认 5）代。是改名、绝不是截断——
                                 # doctor 要数的那些 `[err]` 行因此一直都在盘上。加 `--yes`
                                 # 才动手；装了备份 timer 时每天由它替你做这件事
                                 # （本机现在没装——见 §0「备份定时器」那一行）
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
                                 # 输出 `not-found` 就是「没有任何东西在排班」：本机
                                 # §0 的「备份定时器」那一行正是这个情况的活样本
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

先看 [`docs/maintenance/compat-matrix.md`](docs/maintenance/compat-matrix.md) §2——它列出本项目每条说法属于哪个 OpenCode 版本、以及我们已经背下的后果；再看 [`docs/maintenance/opencode-interface.md`](docs/maintenance/opencode-interface.md),那是本项目读取的全部宿主名词、载荷字段路径与目录清单。声明处有两处：`scripts/opencode_compat.py`（Python 侧）与写入端的 `CONTRACT` 块。内置工具 allowlist 钉在某个版本（M13），MCP/插件清单只在 init 解析（M11/M12）。所以：

1. `opencode --version`，记下版本号。
2. `skillt doctor` → `env.opencode_version` 必须因为漂移而 WARN。
3. 拿真实列表：`curl -s http://127.0.0.1:<端口>/experimental/tool/ids`。注意在跑的 TUI 不一定开着这个端点——你查到的端口可能属于**某个插件自己的** web server，不是宿主。所以起一个什么都不加载的：`~/.opencode/bin/opencode serve --pure --port 14096 --hostname 127.0.0.1`，curl 完就 kill。`--pure` 是它能在别的会话活着时安全执行的原因：不加载任何插件，于是不采集、也不写任何东西。但这仍然要 owner 点头——再起一个宿主实例是他的决定，不是例行检查。
   **别**拿 `strings` 读安装包代替：它里面的 TUI 视图表还带着 `batch`、`list`、`lsp`、`plan_exit`，而这四个宿主根本不报成 tool id（2026-10-03 实测——就在这个推断已经被当成事实写进一份报告之后）。
4. 同时更新 `plugin/skill-tracker.js` 里的 `DEFAULT_BUILTIN_TOOLS` **和**同一行那句 `verified against OpenCode X.Y.Z` 注释——`doctor` 解析的就是那句注释，漏掉它等于重新制造它要检的漂移。`scripts/opencode_compat.py` 里有一份同样的声明（`PIN_TOOL_IDS`，另有 `PIN_LOADER` / `PIN_PERMISSION_SHAPE`）；注释与 `PIN_TOOL_IDS` 不一致时 `test_compat.py` 会红，所以两处要一起改。
5. 重启 OpenCode，用 `skillt plugins` 确认清单已刷新。

6. 大版本升级后**重新测量事件载荷**。字段名已经咬过一次（M20：代码照 SDK 类型写，
   宿主发的却是另一套名字），所以这一步的原则是**观测，而不是相信文档**。做法：在
   `/tmp` 建一个一次性项目，放一个探针插件，只记录
   `permission.asked` / `permission.replied` / `command.execute.before` 的**键名与枚举值**，
   用 `OPENCODE_SKILL_TRACKER_DB` 指向临时库跑一次，再把到达的字段和 tracker 读取的
   字段逐一对。探针请只留在 `/tmp`：它会打印字段值，用在合成沙箱会话里可以，放在有
   真实文本的地方不行。

   真的发现改名，就是**一次提交里改两个文件**：`plugin/skill-tracker.js` 的 `CONTRACT` 块，
   和 `scripts/opencode_compat.py`。别处不许再放 hook 名或事件类型——`test_compat.py` 按**值**
   比对这两份清单，还会检查写入端确实是经由 `CONTRACT` 注册的，所以只改一半会让构建变红，
   而不是安静地什么都不记。

不想改插件就设 `OPENCODE_SKILL_TRACKER_BUILTIN_TOOLS`——但那样 `env.opencode_version` 会**故意**一直 WARN。

## 6. 刻意为之的偏离（别"顺手修回去"）

- **TUI 里没有后台定时器。** 刷新是按页、事件驱动的：按键（5 秒节流）和切页只重读当前页，`r` 与首次绘制重读所有页；每页各自显示自己的 `data as of HH:MM:SS`。先试过 `set_interval`：在 textual 8.2.8 上，**只要在任何屏 mount 之后创建了 app 定时器**，`run_test` 收尾就会抛 `LookupError: <ContextVar name='active_app'>`——用空回调复现过，挂在 App 上和挂在 Screen 上都一样，`timer.stop()` 也救不回来。那会把整个 TUI 文件一次带走——**一个** app 定时器就足以让 `test_tui.py` 里所有用例同时失败，因为收尾属于 app，不属于添加定时器的那条用例。`test_tui_creates_no_app_timers` 把这条钉住。
- **TUI 关闭了 Textual 的动画。** `SkillTUI.animation_level` 是 `"none"`，tab 栏下划线是瞬间到位而不是滑动。Textual 的 `Tabs` 会对它做 0.3 秒动画，级别是 `"basic"`（`_tabs.py` 的 `_highlight_active`），所以全局设成 `"basic"` **挡不住**它——只有 `"none"` 能。实测（44 个 skill 的数据库）：Dashboard→Skills 一次切换带动画约 500 ms、关掉约 190 ms，其中动画占 ~305 ms，而页面自身重读只有 ~70 ms、Textual 布局/渲染地板 ~125 ms。`test_tui_disables_textual_animations` 把这条钉住。不要以「只是外观」为由把它打开——它是切页最大的单项开销。
- **图形断言不许去问被测对象自己的辅助函数“返回了啥”。** Dashboard 排名柱那版把每根渲染出来的柱都和 `rank_bar_width(...)` 的返回值比，于是把阶梯压平成常数之后测试全绿——从被测对象推导期望值的守卫不是守卫。那一列后来按他的要求撤掉了，但这条教训留下：`test_tui_trend_rows_are_a_fixed_width` 在同一句里把绝对行宽（20）也写死了，不只是断言各行彼此相等。
- **TUI 的任何一页都不许打开顾问那个库。** Advisor 页在 2026-10-03 按 owner 的要求移除了；`skill_db_agentos.agentos_*`（独立模块）和 `skillt agentos` 留着，所以那个库仍然能无头读，只是不能从页面上读。`test_tui_never_reads_the_advisor_store` 给唯一的入口装了探针，把 mount、`refresh_all` 和每一个页签都走一遍，调用数必须是 0。
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
- **顾问的库只能从 `skill_db_agentos.agentos_*` 进，而且只以投影的形式出来。** 聚合层列
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
  守卫是 `test_scan_follows_the_entry_shim_one_hop`、`test_scan_hops_are_bounded`、
  `test_scan_reads_a_large_entry_file_in_full`。三个真实插件上整个 `init()` 的实测成本：
  改前中位 45.1 ms，改后 44.7 ms。
- **工具 id 取"最浅的有键那一层"，不是"第一层"。** 以前那块是用一个停在第一个 `}` 的正则读的，
  于是同时犯了两头的错：把工具自己 `args: { query: … }` 里的参数名当成工具（claude-mem 因此在
  真正的 `claude_mem_search` 旁边多出一个 `query`），以及把写在嵌套对象**之后**的工具整个漏掉。
  现在的路径会跳过字符串字面量，取最浅的那一层——DCP 的 `tool:{ ...cond && { compress: … } }`
  和 opencode-mem 的 `tool: { memory: tool({ args: {…} }) }` 在这条规则下都是对的。
  先试过的"过滤全大写的键"已经删掉：它治的是症状，而且真有一个工具引用常量命名的话会被它误杀。
  守卫是 `test_scan_reports_tool_ids_not_parameter_names`、
  `test_scan_finds_a_tool_registered_behind_a_spread`。
- **清单是并集，四种来源里只有两种可删。** `scope` 记下这一行是哪份配置声明的：`global` 和
  `localdir` 每个会话都会读，所以本次启动没再见到的就是用户已经删掉的插件，`loadPlugins()`
  结束时删掉——页面那一列于是说的真是"上次启动加载了什么"，不再是"这个库建好以来见过什么"。
  `project` 行绝不在别处删（换个目录启动的会话看不见它）；建列之前的旧行按 `global` 处理——
  猜错的代价只是这一行，用量还在，那个项目的下次启动会把它加回来。另有两道闸，每道都是靠把它
  关掉来证明有效的：`OPENCODE_SKILL_TRACKER_PLUGINS` 覆盖时一律不删；全局配置文件一个都没解析成功
  时一律不删——配置坏了或没了，看起来和"没装插件"一模一样。守卫是
  `test_the_inventory_prunes_a_plugin_the_global_config_dropped`、
  `test_a_project_scoped_plugin_survives_a_session_started_elsewhere`、
  `test_the_env_spec_override_never_prunes`、`test_an_unreadable_global_config_never_prunes`、
  `test_a_project_scope_row_is_never_downgraded`。
- **表格列宽在这里算，不交给 Textual 去发现。** `DataTable` 在 `_on_idle` 里重算自动列宽，
  所以第一帧每列恰好是表头的宽度（`frozen-gnome-fork-maintenance` 显示成 `froze`），之后每一帧
  都滞后一帧的内容。`fit_columns()` 用手里的单元格算宽并把 `auto_width` 关掉；`_PAGE_TABLES`
  声明每个页拥有哪些表，于是只重测刚画过的那一页。加上 Agents 页之后在 2026-10-04
  用生产库的只读快照重测过：**九张表合计约 1 ms**（四次跑出来 0.7–2.0 ms），外圈整个
  `refresh_all()` 是 8.9–14.8 ms。2026-10-03 那组数（八张表 2.7–3.1 ms、整趟 21–24 ms）
  是在 Recent 还压着 100 行的时候测的；owner 清空用量后重跑，两趟都变便宜是因为数据变少，
  不是因为代码变快；`plain_len` 的
  “没有方括号就不解析”快路是当年把这一趟从约 21 ms 压到约 3 ms 的原因。
  用例读宽度时**故意不**先 `pilot.pause()`——一 pause，idle 就把那一帧修好了，坏代码也会绿。
- **自测隔离的是日志，不只是数据库。** `logPath()` 是一个 `let`，`__selftest()` 把它挪到临时库旁边的 `skill-tracker-selftest.log`，并在**两个出口**都还原——正常结束和中途放弃。还原这一步不能省：自测和真实会话可能共用同一个进程里的同一个模块实例，覆盖要是留着，之后每一条真正的 `[err]` 都会写到 `doctor` 看不见的地方。显式设置的 `OPENCODE_SKILL_TRACKER_LOG` 会被尊重（测试脚手架一直设着一个，这正是 CI 从没暴露此问题的原因）；只有落在共享默认路径上时才改写。证明方式见 README「插件自测（重要）」里那条配方：照它跑成功一次、再**故意跑失败一次**，两次都对比共享日志的 sha256。
- **手画的图表行，宽度不许依赖数据。** 趋势行固定为 `TREND_ROW_WIDTH`（20），
  `.trend` 把高度钉到 `TREND_LINES`，`fmt_count` 把计数压到 5 字符以内。两条腿都不是摆设：
  只撤一条时另一条会把错位藏起来；钉高是**故意用"裁切"换"错位"**——窄于 70 列时三张一起裁（这个 70 是实测的，不是估的）。
  三个序列各自取峰值这一点仍是有意为之。守卫是
  `test_tui_trend_charts_share_one_line_when_a_series_is_huge`、
  `test_tui_trend_css_height_matches_the_line_count` 和
  `test_tui_trend_rows_are_a_fixed_width`；最后这条量的是**剥掉标记后**的文本，
  因为 `Static.content` 返回的原串带着标记。
- **子 agent 的两个角色分两列。** `Spawned` 是这个 agent 启动了几次，`Ran as` 是这个名字被别人当子 agent 跑了几次；两者绝不合成一个数，也都不进 `Total`——`Total` 在每一行上都仍等于 skill + MCP + plugin，由 `test_spawns_and_runs_are_two_columns_and_never_inside_total` **逐行**钉住，而不是只对着今天恰好存在的总数核。纯子 agent 行的 `Success rate` 按它自己的运行次数算（那是它唯一有的结果），并带 `⟨sub⟩` 标记，免得被读成「一个很闲的主 agent」。
- **注入分组不许丢数，日子按本地算。** `by_project` 与 `by_day` 只用日志行本来就带的信息；归不进组的行落进 `projectless`、`older_days` 或 `undated`，绝不静默消失，`test_grouping_never_loses_a_count` 断言各组之和加得回总量。分日跟 `fmt_time` 的本地日历一致，因为趋势图也这么分（M1）；`project=` 的值只有**长得像 slug** 才准入（不含空格、不含路径），所以那个外来字段里出现一句话时，它变成一个计数，不会变成一个标签。
- **行身份绝不从 row key 里 parse 回来。** 名字本身含分隔符（`@scope/pkg`、`conductor:newTrack`、带 `_` 的 server 名），所以每张表在渲染时把 `(kind, ...parts)` 注册进 `app.row_targets`。不要恢复 `split()`。
- **`unified_recent_rows` 额外返回详情页需要的列**（`skill_name`、`server_name`+`tool_name`、`plugin_name`+`item_kind`+`item_name`），显示用的 `name` 不是主键。
- **导出对 metadata 走白名单**（`EXPORT_METADATA_KEYS`）而不是整列倒出，文档形状因此升到 `schema_version = 4`；后来给 `plugin_inventory` 加 `scope` 又把它推到 5，新增 `subagent_usage` 这个键把它推到 6。再改形状要升版本并同步 `test_export.py` / `test_plugin_db.py`——形状用例现在把**整个键集合**钉死了，新键没法悄悄混进来。
- **`scrub-metadata` 原地改行而不是删行**，且 `--yes` 之前必先备份。
- **邻居的日志从文件开头扫，不读尾部。** 它的 worker 日志里有 **57 行 ERROR**，全都排在文件**前半段**；只读尾部 64 KB 会报成 0。所以 `CLAUDE_MEM_LOG_BYTES_CAP` 切的是文件**结尾**，切到了结果里就写 `truncated`，此时计数是**下界**不是总量。计划里那句「整份读也就 1–2 ms」是**没测就写进去的**，实测三个文件要 **8.2–15.8 ms**（2026-10-04）——2026-10-09 用同一条命令重测是 **12.2–14.0 ms**（measurements §6），而这两个读数之间的差**恰恰就是重点**：这笔开销是邻居日志的大小，不是我们的代码。无论哪个数，都是 HTTP 探针不许上重画路径的原因。
- **级别在第 2 个方括号，类别在第 3 个。** `[时间] [级别] [类别] 正文`：把 ERROR 当类别数，一条也数不到；反过来只数类别，也会把「一天全在失败」说成「一天很干净」。`_LOG_LINE_RE` 就是按这个位置写的，匹配不上的行进 `unparsed` 计数，不静默丢掉。
- **文件在前、HTTP 在最后，而 HTTP 绝不进 TUI。** `claude_mem_worker()` 读 `worker.pid` 再 `os.kill(pid, 0)`——那是个 syscall，所以存活与端口只花微秒，而且 worker 没起来时照样能回答。三个端点只在 `skillt claude-mem` 里问，共用**一个**总截止时间（`clock` 是注入进来的，所以这段算术不靠睡觉就能测），投影同时按字段名**和**类型：`database.path`、`worker.version` 和 chroma 的自由文本 `details` 被丢掉，理由是它们不是整数也不是布尔，而不是某条规则点了它们的名。守卫是 `test_the_http_probe_shares_one_deadline`、`test_no_path_or_free_text_crosses_the_http_whitelist`、`test_no_http_is_reachable_from_the_tui_refresh_path`。
- **doctor 保持离线，worker 不在也不算警告。** worker 是按需启动的进程，它不在是一种**状态**不是**故障**，所以 `claude_mem.capture` 只会写 `worker down` 然后继续 PASS。真正触发警告的是日志里的 ERROR 计数——那是账本给不了的唯一信号，因为同步器可以每次都失败，而它最新一行的时间戳照样好看。
- **Agents 页没有排序是有意做的。** `action_cycle_sort` 对 `tab-agents` 直接 return；落到 `else` 会在用户站在 Agents 页时悄悄改掉 *Skills* 的排序，而屏幕上看不出任何变化。它那一行 `(unknown)` 照实显示，并在页脚解释（M23）。
- **Plugins 页用的是并集，`plugin_stats_rows` 不是。** 那一页和 `skillt plugins` 读的是 `plugin_surface_rows`（清单 ∪ 用量）：一个注册了却从没人调过的工具原本**一行都不产生**——11 个界面只画出 1 行，owner 因此判定「采集坏了」。dashboard 的 Top Plugins 仍然用只含用量的那个视图：一排 0 不叫排行。被排除的插件不给它造 0 行，它本来就没有界面。`registered` 与 `ever_called` 保持两个字段，消费方没法把它们合成一个含义不明的 0。
- **子 agent 记成「事件」，绝不当第四种流。** `Spawned` 排在 `Total` 旁边而不是塞进去：`task` 是内置工具，它那一轮里可能一次被测调用都没有——并进去会让同一列一会儿是「调用」、一会儿是「调用加事件」，全看那次会不会 delegate。由 `test_agent_rows_gain_a_spawned_count_and_total_stays_the_three_streams` 钉住。
- **TUI 的自述与几何都是算出来的，不是抄出来的。** 四处手写版本被实测证明确实错过，现在全部由代码生成：
  `MainScreen.key_help()` 从 `BINDINGS` 生成 Data 页的按键清单（旧文案只写了 `b`/`e`/`v`，
  而 Footer 用 `show=False` 藏起来的 `d`、`j`/`k` 一个字都没提）；`MainScreen.CARD_LABELS`
  是六张卡片标签的唯一来源，因为 `_card_columns()` 需要其中最长的那条才能选出不会折行的网格；
  `_empty_note()` 把「表格为什么是空的」写进这一页自己的状态行——新鲜度后缀本来就住在那里，
  于是同一页不会同时挂出两句互相打架的数据声明；状态块**每行只有一个平铺的 `[dim]` 区间**，
  被 `[dim]` 包着的行里再嵌一个 `[dim]` 时刻尾，正是 Plugins 那块读起来像两种信息的起因，
  也正是这条规则能被正则检查的前提。守卡：`test_a_new_binding_documents_itself_in_the_help`、
  `test_the_data_page_key_help_is_generated_from_the_bindings`、
  `test_every_table_page_status_line_is_dimmed_throughout`、
  `test_the_card_grid_switches_where_the_layout_actually_fits`。
- **窄终端的数字是量出来的，别拿 CSS 去「修正」它。** 卡片网格比屏幕窄 4 列，而 Textual 分余数时不平均分，
  所以由**最窄那张卡**决定：六列并排要 111 列，三列要 57 列，两列是底线（低于 40 列最长标签就会折行——
  这是主动选择，代价不让一个本来就在滚动的仪表板再长高一倍）。`_card_columns()` 第一版把这个内缩量算成 2，
  于是在 55 列选了 3 列、在 109 列选了 6 列，而这两档上最窄的卡片只有 16 列，13 列的标签必然折行。
  确认框现在是 `width: 100%; max-width: 60`：原来是 `width: 60`，48 列的终端会拿到一个钉在 `x=0` 的 60 列框，
  `Confirm (y)` 一直延伸到 `x=56`。
- **TUI 里不再有任何命令式颜色。** `#data-result.ok` / `.bad` 解析到 `$success` / `$error`，
  所以换主题能跟着走；被它替掉的命令式赋值做不到。`test_the_result_colour_is_a_theme_token_and_not_a_literal`
  既查类名、也查 CSS 规则，还扫源码里有没有命令式赋值回来了。
- **状态行是在布局之后画的，不是在布局之中。** 还没布局的 `DataTable` 报的是 `region.width == 0`，
  此时那句「有几列在屏幕外」会说成整张表都在屏幕外——那是对仪器下结论，不是对页面下结论，
  所以 `_offscreen_note` 跳过宽度为 0 的表，而 `_refresh_pages` 用 `call_after_refresh` 补一次重画。
  `fit_columns` 存在的理由也是这个。这条提示只解决「看不看得见」：`right` 本来就能把剩下的列滚进来
  （48 列实测 `virtual_size.width` 96 对 `region.width` 44、`max_scroll_x` 52），
  所以表格**没有**加 `min_width`——那是给一个不存在的缺陷开药。
- **两条写路径都接上，因为宿主对内置工具到底触发哪一条并不知道、也不赌。** hook 一对和 `message.part.updated` 各自调用写入端，`UNIQUE(parent_session_id, call_id)` 配「只补空、不覆盖」的规则把两次观测收成一行：事件补上 hook 拿不到的子会话 id，也不会盖掉已有名字。`__selftest` 两条都驱动并断言只有一行。**真实宿主走哪一条仍未验证**——要重启后跑一次真会话才行；而答案不会改这段代码。
- **`subagent_type` 是按形状准入，不是按含义。** `SUBAGENT_LABEL_RE` 要求不含空格、不含路径、至多 40 字符；不合格就计成 `(unnamed)`。这是一条**上界**，不是散文探测器——一个 39 字符、连字符拼的 token 是能过的，这一轮我自己的哨兵值就是这样混过去的，直到把用例的值改成一句带空格的话才暴露。载荷里的散文字段（`prompt`、`description`、`title`、`output`、`error`）在写入端一个都不点名；`test_the_subagent_writer_never_names_a_text_field` 就是去读那三个函数体来保证这一点——而一个会连自己注释一起误伤的扫描，是没人改得动的扫描。

## 7. 已知限制

M1–M24 全文见 [README.zh-CN.md §9](README.zh-CN.md#-9-已知限制)（英文摘要在 [README.md](README.md#-known-limitations)）。
维护时最容易咬人的几条：**M14**（历史行里的提示词原文——2026-10-01 已清理，但更早的备份里仍在）、**M19**（已修：备份曾有两个落点而保留策略只管一个——复查库旁边不该再出现 `skill-usage-backup-*.db`）、**M15**（没跑完的调用一行都不留）、**M16**（一次 git 失败会把该目录的 branch 永久钉成 null）、**M17**（日志会长；现在读写两侧都上了界——两端都用解析后的 `log.max_bytes`，文件本身由 `skillt rotate-log` 改名）、**M18**（晚到的错误文本会被 `COALESCE` 丢掉）以及 **M23**（同一个 `COALESCE` 会把 `agent` 冻住，所以 Agents 页统计的是一行的 session **首次**报出的 agent，而 `(unknown)` 量的是采集顺序，不是「无主的调用」）。

## 8. 暂缓（P2）——按性价比排序，并写明为什么不修

| 项 | 成本 | 为什么先不修 |
|---|---|---|
| `branchByDir` 负缓存无 TTL（M16） | 小 | 动采集路径；AGENTS.md 要求改采集必须带测试，且当前 null 的主因是会话目录本身不是 git 仓库 |
| `metadata` COALESCE 丢晚到错误文本（M18） | 中 | 位于承载去重不变量的 upsert 里 |
| init 里 MCP 探测阻塞约 1.5 秒（164 次 init 的 p90） | 中 | 调低 `MCP_STATUS_TIMEOUT_MS` 会误判服务列表，比启动慢更糟 |
| 每次调用 `skillt agentos` 都会重读 `store/loops/` 下的 loop 文件（`--limit N` 限制的是投影多少条，不是列多少条） | 小 | 那个库里目前只有 5 个 loop；文件名与 loop_id 之间没有可用约定，做缓存等于替别人持有第二份状态。以前让这件事变成“每次按键都要付”的是 Advisor 页，页没了，现在只有跑命令时才付 |
| 顾问聚合层读的是 AgentOS 的阶段字段名 | 小 | `retrieved` / `injection_chars` 属于引擎内部约定；改名只会让那几个数变空，不会连累别处，而且 `_count_only` 拒绝把文本当计数。原本有数字的地方变成 `-` 就是信号 |
| Plugins 页每次重画都整份重读 claude-mem 的 worker 日志——实测 8.2–15.8 ms（2026-10-04）、12.2–14.0 ms（2026-10-09），命令在 measurements §6 | 小 | 上限由 `CLAUDE_MEM_LOG_BYTES_CAP` 兜住，而这一页只在切页、或按键且已过 `REFRESH_STALE_AFTER_S` 时才重画。按 mtime/size 缓存会让这行文本正好在它存在的那个场景上变陈——「发现后台同步开始失败」；这点开销也没到冻住界面的程度。相隔五天两次读数差了 4 ms，动的是邻居的日志，不是读取端 |
| `plugin_inventory` 对本地插件显示绝对路径 | 观感 | 需要只显示层的短化 + 测试 |
| 行级保留只用手跑（M17 已结；`retention.usage_days` 默认 0） | 小 | 把它装上就等于让已记录的历史按一个主人没选过的时间表被删，所以默认关；`skillt prune-usage --yes` 才是那个决定点。`skill_versions` 由 `retention.max_skill_versions` 管，同样默认关 |
| 从 OpenCode 自己的 `part` 表回填 `subagent_usage`（重启前那 84 次：`explore` 61、`general` 13、`auditor` 5、`researcher` 3、`reviewer` 2、`verifier` 1） | 小——一次只读扫描、8 条白名单 json 路径、按 `(parent_session_id, call_id)` 幂等 | **owner 在 2026-10-04 明确不做**：为一个不是天天问的历史问题，要把一个正在被写的库（OpenCode 自己的，WAL，会话跑着的时候也在写）加进本项目的读集合。于是 `Ran as` 说的是「自上次启动以来」，M24 也在页面上这么讲。等真有需要那些旧数字的问题时再提。 |

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
