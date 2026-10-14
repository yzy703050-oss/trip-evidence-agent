## Context

动机见 proposal.md；保持的行为见 verification-scope.md 及其中引用的正式规格。

已核实：agents/ 30 个文件；context/ 15 个；travel_data/ 17 个；utils/ 6 个；tests/ 111 个；evals/ 23 个；docs/ 458 个，其中 docs/evals/ 394 个、Git 跟踪的评测文件 109 个。workflow_runner.py 为 1579 行。统计排除 __pycache__；文件数包含当前未提交的工作。

现有路径不是都能直接删除：CLI 经 ExecutionHarness 进入 WorkflowRunner；LazyAgentRegistry 从 .claude/skills/*/script/agent.py 加载实际 Agent；旧信息/偏好/记忆角色仍由测试或兼容调用使用。test_live_model_contracts 从 docs/evals 读取捕获响应，多个评测脚本把该目录同时作为输入和输出。config_agentscope.py 已是无框架初始化的兼容模块。

## Goals / Non-Goals

**Goals:** 按真实职责定位代码；当前文档与历史产物分开；生成文件有统一归属；旧入口、导入、业务数据与测试覆盖保持；每次迁移可追踪和验证。

**Non-Goals:** 不裁剪旧功能，不删除测试或历史证据，不新建 src/ 布局、前端或服务层，不移动 .env、.venv、SQLite、RAG 数据或 .claude/skills，不拆分所有长文件，不顺带实现其他活动 OpenSpec change。

## Decisions

### 1. 采用职责集中方案

比较三种方案：

- 只整理日志和文档：改动小，但模型角色与编排混杂仍存在。
- **集中编排模块并整理产物（选择）**：保留现有根入口与主要包，实际行程代码统一位于 workflow/，迁移范围可控。
- 全量改 src/trip_assistant 并拆分 CLI/存储/工具：一次影响所有导入、插件及脚本路径，兼容成本过大，不适合这次整理。

目标结构：

```text
cli.py                     # 保持启动命令
config.py                  # 当前配置
config_agentscope.py        # 保留兼容入口
agents/                    # 模型、角色、消息协议；旧路径的薄兼容转发
workflow/                  # 行程编排、状态、条件、修改、校验和缓存页
travel_data/               # 供应商、数据契约、工具与缓存
context/                   # 记忆、会话、SQLite、上下文预算
utils/                     # 现有通用辅助；本次不额外拆包
tests/                     # 保留测试及夹具
evals/                     # 可重复运行的评测代码
scripts/                   # 运行、初始化和维护入口
docs/
  README.md                # 当前文档入口
  architecture.md          # 当前职责、依赖与数据流
  development/             # 当前开发说明
  scenarios/               # 当前示例
  superpowers/             # 流程计划、评审和验证证据
  archive/evals/           # 历史评测报告及证据
  archive/diagnostics/     # 历史诊断
data/
  evals/                   # 新评测输出，既有 ignore 目录
  logs/history/            # 归置已有根目录日志
  memory/、models/、...    # 原位置不变
openspec/                  # 正式规格与变更历史
.claude/skills/            # 动态加载路径不变
```

不以文件总数下降作为成功标准：保留旧导入需要薄转发，代码应只有一个权威实现，不能为减少文件数把所有职责合成大文件。

### 2. 编排模块的迁移清单与兼容

| 当前实现 | 权威实现目标 |
| --- | --- |
| agents/workflow_runner.py | workflow/runner.py |
| agents/workflow_contracts.py | workflow/contracts.py |
| agents/workflow_state.py | workflow/state.py |
| agents/workflow_batches.py | workflow/batches.py |
| agents/workflow_queries.py | workflow/queries.py |
| agents/workflow_guard.py | workflow/guard.py |
| agents/workflow_repair.py | workflow/repair.py |
| agents/workflow_matching.py | workflow/matching.py |
| agents/workflow_reads.py | workflow/reads.py |
| agents/workflow_amendments.py | workflow/amendments.py |
| agents/planning_conditions.py | workflow/planning_conditions.py |
| agents/condition_strengths.py | workflow/condition_strengths.py |
| agents/condition_conflicts.py | workflow/condition_conflicts.py |
| agents/travel_updates.py | workflow/travel_updates.py |
| agents/cached_pagination.py | workflow/cached_pagination.py |

项目内部及新测试统一引用新权威模块。旧模块保留薄转发，使用同一模块对象别名，确保旧导入、类身份和 monkeypatch 作用到相同实现；不能复制原实现形成两套编排，也不通过复杂的全局 import hook 隐藏映射。兼容成本集中在这些显式薄文件，文档说明实际代码位置。

MainAgent、ExecutionHarness、LazyAgentRegistry、config_agentscope、CLI 命令和插件目录不迁移。agents/contracts.py、context_projection.py、model_* 等继续表示 Agent 输入输出/投影职责。workflow/ 可以消费这些协议；仅迁移职责和导入，不同时改算法或调用顺序。

1579 行 Runner 先归位，不在这轮同时拆成多层继承或多个共享可变状态对象；拆分请求协调与行程执行如确有必要，单列下一轮设计，避免迁移和行为重构混在同一个验收中。

### 3. 历史证据与生成物分开

将 docs/evals 与 docs/diagnostics 内容分别迁到 docs/archive/evals 与 docs/archive/diagnostics，保留层级和原内容。旧目录留简短 README 指向新位置；当前 README、评测脚本及维护文档指向新位置。历史报告正文不重写当时叙述；迁移时修复仍承诺可访问的相对附件链接，并生成旧路径→新路径清单，供历史绝对命令定位。

把依赖生产捕获响应的 3 份回归输入最小化收录到 tests/fixtures/，保留来源说明和原始有效内容，测试不再依赖一次性报告目录。历史完整响应继续留档；不能为了缩小数据修改让旧非法输入通过的业务断言。

评测脚本输出默认落入 data/evals/<run>/；需要复用旧报告时显式从 archive 定位。新报告摘要可人工选入 docs，避免每次评测把模型响应、SQLite 会话和日志变成当前文档。

### 4. 根目录日志非破坏归置

只枚举项目根目录中已有 *.log，移动到 data/logs/history/<迁移批次>/。记录数量及哈希，遇到同名目标不得覆盖。今后的维护/评测输出明确指定 data/ 下目标目录；不改变当前运行的用户会话、数据库、模型缓存及 secrets 路径。

Windows 每次目录移动前解析绝对源/目标并验证都位于本项目根目录，使用同一 PowerShell 进程的 LiteralPath 操作。不递归删除目录，不清空 .superpowers 或其他任务工作区。

### 5. 对当前未提交内容的处理

迁移前保存清单，区分 tracked、untracked、ignored 及本轮修改；以当前工作区内容作为迁移源，不能从 Git HEAD 还原造成此前本地翻页、提示等修改丢失。不自动提交其他变更。保持两项未实施的 OpenSpec change 原意与记录，实施阶段提示新路径映射，不趁机实施其需求。

## Risks / Trade-offs

- 导入环与猴补丁失效 → 先加入口、模块身份及核心路径回归，逐组迁移，始终只有一套实现。
- 兼容层使总文件数暂时增加 → 保留用户明确要求的兼容；减少权威代码散落与重复，文档明确阅读入口。
- 路径迁移使旧脚本或附件断链 → 统一活动脚本路径、保留来源与映射、检查所有引用和报告附件。
- 误动本地数据或未提交工作 → 不扫描 secrets 内容，迁移清单和哈希比对，不删除未知文件、不覆盖目标。
- 测试收集减少导致假通过 → 对比测试收集清单和既有覆盖，现有全量测试外增加迁移与兼容回归。

## Migration Plan

先确认本设计，再编写详细实施计划和确定执行方式。实施前保存迁移清单和测试基线；按编排模块、评测路径、历史文档/日志三组完成迁移并逐组验证。最后更新根 README 与 docs 导航，运行全量回归及规格人工核对，验证文件内容完整性，再归档本 change。

回滚仅反向使用本轮迁移映射，并回退本轮导入及路径修改；保留迁移后新增的运行产物，不重置整个工作区。

## Review Status

已完成必要探索、依赖和路径检查，未改运行代码，未移动或删除任何已有文件。此为待用户确认的结构设计；下一阶段按 AGENTS.md 做技术复核与 writing-plans，正式行为不另起一套规格。

