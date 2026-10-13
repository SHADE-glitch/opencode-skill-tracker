# 维护清单

配套文档：[README.md](README.md)（英文首页）与
[README.zh-CN.md](README.zh-CN.md)（完整参考）。本文件是**操作**文档：查什么、按什么顺序、
「健康」长什么样，以及哪些缺陷是刻意保留的。

英文版：[MAINTENANCE.md](MAINTENANCE.md)。

长篇维护资产索引在
[`docs/maintenance/README.md`](docs/maintenance/README.md)——版本矩阵、宿主接口清单、
测量方法——本仓库的工作规则在 [`AGENTS.md`](AGENTS.md)。本文件是**操作清单**而不是它们的指针：
它的编号小节是稳定锚点，被 `AGENTS.md`、两份 README 与下面的文档引用，所以改编号等于改名。
不要把 `docs/maintenance/` 里的表抄进这里——被复制的那个事实就是会漂移的那个。

| 文档 | 什么时候读 |
|---|---|
| [`compatibility.md`](docs/maintenance/compatibility.md) | OpenCode、Textual 或 Python 版本动了，或某个 pin 需要重验 |
| [`opencode-interface.md`](docs/maintenance/opencode-interface.md) | 新增或改名一个宿主依赖 |
| [`measurements.md`](docs/maintenance/measurements.md) | 要重述某个体积、速率或时长 |

## 0. 最后一次验证所在机器的事实

机器身份（OpenCode、解释器、textual）在
[`docs/maintenance/compatibility.md`](docs/maintenance/compatibility.md) §1；本项目依赖的宿主
接口按名字列在 [`docs/maintenance/opencode-interface.md`](docs/maintenance/opencode-interface.md)；
下面每一个体积 / 速率 / 时长都是一次**运行**，所以它们连命令一起住在
[`docs/maintenance/measurements.md`](docs/maintenance/measurements.md)，不在这里。清单里的数字会被
永远当成现状读，所以本文件只写形状，不写体量。

| 项 | 值 |
|---|---|
| OpenCode | **在跑哪个版本是一次读数，不是本文件的事实**——compatibility §1 记录 pin；`ocut sync` 读的是今天 `~/.local/share/opencode/opencode.db` 里有什么 |
| 宿主数据库 | `~/.local/share/opencode/opencode.db`——以 `mode=ro` 打开并设 `PRAGMA query_only = ON`，**从不写入** |
| 宿主配置 | `~/.config/opencode`——只读；解析失败绝不当作「空配置」 |
| Tracker 数据库 | `~/.local/share/opencode-usage-tracker/usage.db`，WAL，只由 `ocut` 写入 |
| 插件 sidecar | `~/.local/share/opencode-usage-tracker/plugin-events.jsonl`，由 `plugin/ocut-hook.js` 追加，由 `ocut sync` 摄取 |
| TUI venv | `./.venv`（textual `>=8.2,<9`） |
| 设置 | `~/.config/ocut/config.toml`（或 `OCUT_CONFIG`）——优先级：环境 > 文件 > 默认 |
| Schema | `ocut_db.py` 里的 `SCHEMA_VERSION`；一次升级会清空同步游标，强制一次干净的重读 |
| 快照 | `~/.local/share/opencode-usage-tracker/backups/`，保留最新 `BACKUP_KEEP`（5）份 |
| 安装布局 | 两个软链接：`~/.local/bin/ocut -> <repo>/bin/ocut` 与 `~/.config/opencode/plugin/ocut-hook.js -> <repo>/plugin/ocut-hook.js` |

安装布局——两个都是**指回本仓库的软链接**，所以在这里改仓库就是实时的（无需重装），
只是必须重启 OpenCode 才会重新加载 hook：

```
~/.local/bin/ocut                        -> <repo>/bin/ocut
~/.config/opencode/plugin/ocut-hook.js   -> <repo>/plugin/ocut-hook.js
```

验证：

```bash
readlink -f ~/.local/bin/ocut ~/.config/opencode/plugin/ocut-hook.js
ocut health
```

若其中任何一个成了**普通拷贝**，它会静默地不再跟随仓库。`install.sh` 拒绝覆盖非软链接；
只有在确认那份拷贝里没有你需要的东西之后，才用 `--force` 重跑。

## 1. 不变量——永不破坏

下面每一条都有测试守着。若某个测试红了，变的是不变量，不是测试。

| 不变量 | 守卫 |
|---|---|
| OpenCode 自己的数据库与配置**从不被写**。宿主库以 `mode=ro` 打开并设 `PRAGMA query_only = ON`；本项目只写 tracker 自己的库。 | `scripts/tests/test_ocut_sync.py`、`test_the_host_database_is_never_opened_for_writing`、`test_open_source_refuses_to_create_a_host` |
| 消息与工具的**散文从不被读**，因此从不被存。`tool` part 的 `output`/`title`/`metadata`/`error`、`text`/`reasoning` 正文、会话 `title` 都不被点名。 | `scripts/tests/test_ocut_sync.py`、`test_no_prose_field_is_copied_out_of_the_host` |
| hook `plugin/ocut-hook.js` 是 `plugin_usage` 的**唯一写入方**，且从不打开 OpenCode 的数据库。 | `scripts/tests/test_ocut_hook.py`、`test_the_hook_records_a_surface_and_only_plugin_owned_calls` |
| hook 只以 `export default { id, server }` 暴露工厂。具名导出会让 OpenCode 把它当工厂调用，从而中断整个插件加载。 | `scripts/tests/test_ocut_hook.py`、`test_default_export_is_the_loader_contract_and_nothing_else` |
| 两个 token 粒度——每步的 `model_calls` 与宿主的按会话 rollup `sessions`——是同一测量的两个层级，**绝不相加**。 | `scripts/tests/test_ocut_sync.py`、`test_two_granularities_of_the_same_measurement_agree`；`scripts/tests/test_ocut_tui.py`、`test_no_panel_repeats_the_input_number_under_a_second_label` |
| 读取器用不了的记录会被**计数**，绝不丢弃：未知 part 类型、解析不了的 JSON blob、解析不了的 sidecar 行，各自落进 `unparsed`。 | `scripts/tests/test_ocut_sync.py`、`test_a_json_blob_that_does_not_parse_is_counted_not_skipped`、`test_an_unknown_part_type_is_counted_per_type`、`test_an_unparseable_sidecar_line_is_counted_not_skipped` |
| 改动 schema 或视图必须升 `SCHEMA_VERSION`，而升级会**清空同步游标**，好让新形状被重读而不是被信任。 | `scripts/tests/test_ocut_windows.py`、`test_a_version_bump_clears_the_cursors_so_nothing_is_read_again` |
| 由**更新**版本写下的数据库会被原样留下（只确保表，不碰视图）并报为 newer，绝不重写。 | `scripts/tests/test_ocut_windows.py`、`test_a_database_from_a_future_build_is_left_alone_and_says_so` |
| 设置是一个注册表：**环境 > 文件 > 默认**，坏值是以点名键的硬错误，零配置不创建任何东西。 | `scripts/tests/test_ocut_settings.py`、`test_environment_beats_the_file_and_the_file_beats_the_default`、`test_an_unknown_key_is_refused_and_names_the_real_ones`、`test_no_file_is_the_normal_case_and_creates_nothing` |
| 推送前测试全绿。 | `AGENTS.md` |

## 2. 每日（在相信数字之前）

```bash
ocut health
ocut sync
```

健康意味着：

- `ocut health` 找得到宿主数据库（`ok, N MiB`）与宿主配置（`ok`）。
- `ocut health` 打印的 `schema version` 等于 `ocut_db.py` 里的 `SCHEMA_VERSION`。
- 每条 `cursor` 行显示的游标不超过宿主自己的 `host max`，且没有 `note`——有 note 意味着
  宿主的表被裁剪或重新编号过；这本来就会发生，但应当被读到，而不是被忽略。
- `ocut health` 打印 `unparsed 0 — every record type was claimed`。非零行就是 OpenCode
  持久化格式变了的信号；reason 字符串会点名它。
- `ocut sync` 结束时没有 `UNPARSED … this run` 行，且 `tool_calls` / `sessions` 计数合理。

## 3. 每周

```bash
ocut sync --full          # 计数看起来不对时从头重建（重建前先快照）
ocut backup               # 任何有风险的操作之前，主动做一份快照
ocut plugins              # 已安装 ∪ 已使用；某插件 0 uses 说明 hook 什么都没看到
ocut format               # OpenCode 格式面，从解析器注册表读出
```

- `ocut sync --full` 会清空 tracker 表并重建。它会先给数据库做快照，但**宿主**才是事实来源：
  OpenCode 已经删掉的会话无法从中恢复。
- 插件用量**只**由 hook 记录，且只在 OpenCode 重启之后。若每个插件都显示 `0 uses`，检查
  `~/.config/opencode/plugin/ocut-hook.js` 是否存在、`OCUT_HOOK_DISABLE` 是否未设、以及 sidecar
  `~/.local/share/opencode-usage-tracker/plugin-events.jsonl` 是否在被追加。`0` 是「没有任何记录」，
  不是「没用过」。
- `ocut format` 打印读取器依赖的宿主名字。任何 OpenCode 升级之后，把它和
  [`docs/maintenance/opencode-interface.md`](docs/maintenance/opencode-interface.md) 对照。

## 4. 每月 / 任何改动之后

```bash
sqlite3 -readonly ~/.local/share/opencode-usage-tracker/usage.db "PRAGMA integrity_check;"
python3 -m pytest scripts/tests -q                     # 不需要 venv
.venv/bin/python -m pytest scripts/tests -q            # 完整套件，含 TUI
bash -n bin/ocut                                       # 分发器仍能解析
```

第二、三条是同一套件在两个解释器上跑；没装 textual 时 TUI 文件会被跳过，所以两条都要跑。

## 5. 升级 OpenCode 之后

先读 [`docs/maintenance/compatibility.md`](docs/maintenance/compatibility.md) §2——它列出本项目每条
说法属于哪个版本——再读 [`docs/maintenance/opencode-interface.md`](docs/maintenance/opencode-interface.md)，
那是本项目按名字读取的全部宿主名字、载荷路径与目录。然后：

1. `opencode --version`——记下版本号。
2. `ocut sync`——若打印 `UNPARSED … this run` 行，说明宿主格式变了。`ocut health` 会列出原因
   （`unknown_part_type:<type>`、`unparseable_part_data`……），每条点名读取器用不了什么。
3. 重读格式注册表：`ocut format`。它由 `scripts/ocut-sync.py` 的常量生成，所以描述不了代码已不再
   读取的面。
4. 若某个宿主字段改名，在 `scripts/ocut-sync.py` 里修好，并在**同一次改动**里把声明加进注册表；
   `scripts/tests/test_ocut_sync.py` 锁住两个方向——没注册的字段不许读，注册了的字段不许不读。
5. `ocut sync --full` 在新形状下重建（重建前先快照）。
6. 重启 OpenCode，让 hook 重新加载并重扫插件面。

## 6. 刻意偏离

记在这里，好让下一个读者不会「顺手」把它们改回坏掉的样子。

- **宿主只读，永不写。** `open_source()` 以 `file:<host>?mode=ro` 打开并设
  `PRAGMA query_only = ON`，文件缺失时**抛错**而不是创建一个空库。这正是读取器无法修复或回填
  OpenCode 的原因；`ocut sync --full` 只重建 tracker 自己的那份。
- **插件用量来自 sidecar，不来自宿主。** OpenCode 不标明一次工具调用属于哪个插件，对命令则完全不记，
  所以 `plugin_usage` 是唯一一张数据来自宿主库之外的表——由 `plugin/ocut-hook.js` 追加到
  `plugin-events.jsonl`。这个 hook 从不打开 OpenCode 的数据库，也从不读工具的参数、输出或任何消息
  文本。`OCUT_HOOK_DISABLE=1` 关掉它；`OCUT_HOOK_EVENTS` 移动 sidecar，hook 与 `ocut sync` 必须
  对它达成一致。
- **hook 的归属是一次尽力而为的静态扫描。** 它读每个已安装插件的源码，找 `tool: { … }` 对象和两种
  命令注册形状，再往 bundle 的相对 import 跳一层。扫描解析不出某个插件的面时，它就什么都不记——
  猜一个归属比缺一行更糟。
- **插件调用没有状态。** 工具的 after-hook 不带状态，命令则根本没有 after-hook，所以
  `plugin_usage.status` 是 NULL，界面打印 `-` 而不是 `0`。它的 `ts` 是 hook 自己的时钟，不是宿主的。
- **sidecar 有上限、靠截断轮转。** `ocut-hook.js` 在超过 8 MiB 时截断文件；`ocut sync` 发现它
  （文件比字节偏移游标短）就从开头重读——幂等，因为每条记录以自己的 id 为键。`OCUT_HOOK_DISABLE`、
  `OCUT_HOOK_EVENTS`、`OCUT_HOOK_PACKAGES`、`OCUT_OPENCODE_CONFIG` 是 hook 的旋钮。
- **读取器给它用不了的东西计数。** `unparsed` 按 reason 累加，而不是丢掉一个未知 part 类型或一个
  解析不了的 blob；一次「什么都没认领」的运行照样打印 "done"，而这个计数才是让格式变化变得可见的
  东西。`ocut health` 连 0 的情况也打印，好让那一行本身证明检查跑过。
- **两个 token 粒度并排显示、绝不相加。** `model_calls` 是每步记录；`sessions` 是宿主的按会话
  rollup。它们是同一测量的两个层级，相加会把每个 token 数两遍。
- **MCP 按名字归属，最长前缀优先。** 没有哪个已配置服务认领的工具名保持 `builtin`，并作为
  「server 形状但无主」报出，而不是被猜进某个服务。
- **设置在宿主的目录树之外。** `~/.config/ocut/config.toml`（或 `OCUT_CONFIG`），绝不是
  `~/.config/opencode/**` 下任何东西——那是宿主的。解析不了的文件是以点名键的错误，不是静默回退。
- **备份是手动的。** `ocut backup` 是那个刻意的动作；`ocut sync --full`、schema 升级与
  `ocut restore` 各自会先做快照。**没有 systemd** timer 了，保留策略只留最新 `BACKUP_KEEP`（5）份
  托管快照，从不碰手工的 `usage.db.bak-*` 副本。
- **schema 只有一份，在 `ocut_db.py` 里。** `TABLES_SQL` 建表、`VIEWS_SQL` 建视图；
  视图一改就升 `SCHEMA_VERSION`，而升级会清空 `sync_state`，让游标被重读而不是被信任。
  `plugin_surface`（hook 上报的工具/命令 id）在每次文件系统重写之后被折回 `inventory`，
  所以那些面能存活下来。

## 7. 已知限制

M1–M12，含复现说明：
[README.zh-CN.md §9](README.zh-CN.md#-9-已知限制) /
[README.md](README.md#-known-limitations)。
维护时最容易踩到的几条：**M1**（宿主是事实来源，它从没落盘的调用就是不可见）、**M3**（插件用量需要
hook **以及**一次 OpenCode 重启）、**M4**（扫描是尽力而为的）、**M6**（`unparsed` 是格式变化的信号）、
**M10**（sidecar 靠截断轮转）。

## 8. 暂缓——按代价排序，附不做的理由

| 项 | 代价 | 为什么暂缓 |
|---|---|---|
| 回填 hook 存在之前漏掉的插件用量 | 中 | 宿主不标明一次工具调用属于哪个插件，所以没有可回填的**来源**；恢复它等于猜一个归属 |
| 归属扫描解析不出的插件命令 | 中 | 猜一个归属比缺一行更糟（M4）；诚实的答案是 `0` |
| 按模型的 cost 图 | 小 | `sessions.cost` 与 `model_calls.cost` 是同一笔钱数两遍；任何地方只显示其中一个 |

## 9. 回滚

每次改动一个 commit，代码在文档之前（Conventional Commits）。要撤掉一个阶段：

```bash
git log --oneline -12
git revert <sha>          # 优先 revert 而不是 reset；套件必须保持全绿
```

`ocut restore NAME` 是从坏的数据库状态回去的路：它在替换前先给在用的数据库做快照，所以恢复本身可逆。
先关掉 TUI——它把文件开着。

## 10. 每个阶段的报告格式

这里每次改动都用同样五项汇报，好让读者不用重新推导就能接受或拒绝：

1. **改了什么**——行为，不是代码。
2. **怎么看**——能演示它的确切命令或按键。
3. **测试增量**——前 → 后计数，附任何先红后绿的证据。
4. **偏离**——任何与批准方案不同的做法，以及为什么。
5. **回滚**——要 revert 的 commit。

## 11. 一个 `green` 是怎么产生的

只有当跑它的那条命令**本来就能大声失败**时，套件才算证据。四个坑，都在这台机器上付过学费：

- **串联会吞掉失败。** `a && b` 只报 `b` 的状态，`pytest -q | grep -c passed` 报 `grep` 的。
  一次检查一条命令、各自的状态、不接管道：
  `python3 -m pytest scripts/tests -q > /tmp/pt.log 2>&1; echo "exit=$?"`。
- **`${PIPESTATUS[0]}` 是 bash。** 本项目由 **zsh** 驱动，那里的数组是 `$pipestatus` 且从 1 开始；
  bash 写法会展开成空，于是失败的 `git push` 打印出 `push exit=`——一个看起来像检查跑过了的空白状态。
  取状态时不要接管道。
- **`tail` 不是结论。** 本仓库没有会改变输出的 pytest 配置——没有 `addopts`，没有第二个 `-q` 能压掉的摘要行
  （`scripts/tests/conftest.py` 只放 fixture）——所以计数总是在：读日志文件的最后一行，而不是屏幕的片段。
- **一次 red-check 会留下金丝雀。** 证明一个守卫能失败，意味着要临时改实现或 fixture 一下，而这一下
  如果活过了编辑，就是一个假绿。被跟踪源码里的 `TEMP-` 标记会被
  `test_no_canary_or_scratch_marker_survives_in_tracked_source` 拒绝。恢复靠重读文件，而不是相信最后
  一次编辑撤销了第一次。
