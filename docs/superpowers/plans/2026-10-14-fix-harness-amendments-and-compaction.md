# Harness 修改、记忆压缩与普通日志 Implementation Plan

> **For agentic workers:** 用户已选择单代理执行，使用superpowers:executing-plans、TDD及整体自审，不派实施或评审子代理。保留用户最新temperature=0.3。

**Goal:** 修复真实模型评测中指定酒店选错、只读提前结束及可压缩历史仍超预算的问题，预算设为 20,000，并提供无需读数据库的普通运行日志。

**Architecture:** 继续由 WorkflowRunner 编排，保留定位与修改两阶段；修改局部读取真实缓存并校验明确目标；历史视图精简，完整请求验收驱动有限压缩；标准库文件日志复用现有观测点。

**Tech Stack:** Python 3.11、asyncio、SQLite、现有 OpenAI 兼容适配器、logging/RotatingFileHandler、pytest/pytest-asyncio。

**Spec:** `openspec/changes/archive/2026-10-14-fix-harness-amendments-and-compaction/{specs,design.md,tasks.md}` 与 `openspec/changes/archive/2026-10-14-add-runtime-file-logging/{specs,design.md,tasks.md}`。正式需求只有各自 OpenSpec 文档；本计划不重新定义产品行为。

## Global Constraints

- 当前turn内阶段压缩延期；不新增查询Agent或每轮模型摘要；按用户最新要求增加预算80%的提前压缩软阈值。
- 常规旧行程修改保留定位与补丁两次主调用；实际翻页与输出修正可增加调用但有界，不强求所有请求固定两次。
- 默认候选每页 5 条、完整有效缓存保留、本地分页零供应商请求；只读不变版本，未授权组件保持。
- 预算默认、本地及示例20,000，提前触发为80%（16,000）；软触发不替代硬预算。保留temperature及生成回退0.3，真实API usage与上下文估算分别统计。
- UTF-8/上海时间/INFO，runtime.log 单文件 5 MiB、5 份轮转；正常摘要不复制用户原文/模型载荷/供应商数据，SQLite 保留。
- 保留原始事件，历史断点只覆盖连续完整旧 turn，子 Agent 不压缩；不新增被既有用户要求排除的不可压缩当前轮专项测试。
- 不删除用户数据、不覆盖既有未提交改动；日志旧计划只执行本计划映射的任务一次。实施时按 using-git-worktrees 识别当前 checkout，若需隔离必须包含当前未提交基线，不能从远端默认分支漏掉现有分页改动。

## Review Focus

1. 只读/修改冲突修正失败时没有供应商调用和方案写入；普通偏好前置不被误判冲突。
2. 修改排除原酒店后页数和偏移变化，不能把缓存序号当用户目标；此前已展示引用仍可用。
3. 模型猜出存在但未展示的 ID，或者文字与真实候选错配，不能通过完成状态冒充成功。
4. 历史投影后用户更正、行程 ID/版本、未知事实仍存在；摘要候选必须实际覆盖新增轮次才能推进边界。
5. 摘要失败/并发状态变更/输出已取得时原始数据可恢复、不重复业务模型请求；普通日志 failure 路径仍可定位且不重复安装 handler。

## Task 1: 动作互斥与有限修正（修复 change 2.1）

**Files:** 修改 `agents/contracts.py`、`agents/travel_prompts.py`、`agents/main_agent.py`；新增 `tests/test_harness_amendment_routing.py`，回归 `tests/test_intent_contracts.py`、`tests/test_saved_trip_read_only.py`。

**Interfaces:** 保留 validate_plan(value)；初始化无效决定继续使用已有一次结构化修正，新增明确冲突原因。不改变纯 read_request 及偏好预处理协议。

- [x] 写 `test_read_and_amend_decision_is_rejected_before_effects`：用原始日志中同时包含 read_request/travel_update 的响应，断言校验失败、供应商0次、保存0次；写 `test_conflicting_model_decision_uses_one_repair`，第二次合法替换后继续修改，不先返回列表。
- [x] 写纯读和偏好混合请求的兼容用例。运行 `.venv/Scripts/python.exe -m pytest -q tests/test_harness_amendment_routing.py tests/test_intent_contracts.py tests/test_saved_trip_read_only.py`，预期首项因冲突决定被旧实现接受而失败。
- [x] 最小实现互斥和提示词同步；只修正无效响应，不改变用户目标。复跑同命令，通过后记录证据并勾选 OpenSpec 2.1。

## Task 2: 保存行程候选闭环与目标验收（修复 change 2.2）

**Files:** 新增 `agents/amendment_selection.py`、`tests/test_harness_amendment_selection.py`；修改 `agents/workflow_runner.py`、`agents/main_agent.py`、`agents/workflow_amendments.py`、`agents/travel_prompts.py`；必要时在 `agents/cached_pagination.py` 提取共用窗口校验。回归保存行程和现有分页测试。

**Interfaces:** validate_selection_requirements(value, original_query)；resolve_literal_selection_requirements(original_query, candidate_rows, proposed)；validate_amendment_selection(patch, requirements, shown_refs, workflow)。amend_saved_trip 可返回提交、read_cached_page 或澄清，无 action 的旧补丁兼容提交。局部循环维护窗口和跨页真实引用，不写未提交状态。

- [x] 写 12 条真实映射候选 fixture：B0MEM000010=门店11、B0MEM000011=门店12。先写 `test_named_target_uses_real_ref_not_number`，即使初次选择需求遗漏，明确“名称带模拟门店12”仍保留目标；选11补丁必须拒绝，原行程不变。
- [x] 写 `test_amendment_can_read_cached_later_page`、`test_existing_but_unseen_id_is_not_committed`、`test_first_page_reference_remains_usable`，断言页条数/偏移正确、供应商0次、两次普通修改模型路径兼容。
- [x] 写多义/不存在目标、重复页、过期查询、禁止刷新、任务版本冲突和保持火车日期的用例；目标不存在不能报告修改完成。
- [x] 运行 `.venv/Scripts/python.exe -m pytest -q tests/test_harness_amendment_selection.py tests/test_saved_trip_amendment.py tests/test_cached_candidate_pagination.py`，预期旧实现无后页修改决定和目标一致性校验而红灯。
- [x] 按 design 实现字面目标验证、完整缓存有界目标展示、后页动作循环及提交验收；不猜 ID，不把选择意图变成长期偏好。复跑上述命令后更新 OpenSpec 2.2。

## Task 3: 模型历史视图与事件分离（修复 change 3.1）

**Files:** 修改 `agents/context_projection.py`、`agents/main_agent.py`、`context/memory_manager.py`；新增 `tests/test_history_model_projection.py`，回归 `tests/test_agent_context_isolation.py`、`tests/test_v0_compaction.py`。

**Interfaces:** project_history_message(event) 与 project_history_messages(events) 返回只含角色、正文和必要业务摘要的模型视图；project_session_message 保持事件和断点用途。

- [x] 写带 seq/turn_id/scope/type/final/timestamp 的实际记录，断言它们不出现在模型历史中，角色、日期更正、未知项及 workflow_id/revision 保留；断言主消息与原 SQLite 事件没被改写。
- [x] 写 `_refresh_history` 与显式传 recent_messages 使用同视图、摘要输入使用同视图，以及重启后回忆引用保留的用例。
- [x] 运行 `.venv/Scripts/python.exe -m pytest -q tests/test_history_model_projection.py tests/test_agent_context_isolation.py tests/test_v0_compaction.py`，预期旧视图携带管理字段而失败。
- [x] 最小接入模型视图，避免刷新或投影再次切候选/丢业务字段；复跑并勾选 OpenSpec 3.1。

## Task 4: 完整请求驱动有限压缩（修复 change 3.2）

**Files:** 修改 `agents/main_agent.py`、`context/memory_manager.py`、`context/compaction.py`；新增 `tests/test_full_request_compaction.py`，回归 `tests/test_context_budget.py`、`tests/test_v0_compaction.py`、`tests/test_agent_context_isolation.py`。

**Interfaces:** 压缩接口增加可选 request_fits(summary, remaining_events)；MainAgent 使用实际 build_messages、tools、可选已有 response，整体 budget_tokens 和 counter 为单一验收口径。无 callback 的旧测试/调用者兼容。

- [x] 写 `test_full_request_over_budget_compacts_despite_event_sum_fit`，按旧日志形态构造完整旧轮次，刻意使局部历史合格而最终序列化请求超预算，业务模型只应在全请求合格后调用。
- [x] 写 `test_compaction_rechecks_rendered_request_and_expands_safe_prefix`，第一份摘要未达标，后续摘要覆盖新增完整轮次后才提交；不能仅扩大边界。用旧14,854预算和围绕20,000的两种形态参数化，避免只把预算调大让旧样本不触发。
- [x] 写不重复业务响应、摘要失败不推进候选覆盖、并发清空后不提交旧摘要，以及3次摘要上限和无进展停止的用例；仅覆盖可压缩旧历史及其失败，不新增当前轮不可压缩专项。
- [x] 运行 `.venv/Scripts/python.exe -m pytest -q tests/test_full_request_compaction.py tests/test_v0_compaction.py tests/test_context_budget.py tests/test_agent_context_isolation.py`，预期旧实现漏触发或首次压缩后仍失败。
- [x] 实现全请求 callback、安全切点候选、摘要输入预算/长度控制、增量扩大覆盖、3次上限和提交后复检；原始日志保持。完整业务 response 已取得时直接复用。复跑并勾选 OpenSpec 3.2。

## Task 5: 20,000预算配置（修复 change 3.3）

**Files:** 修改 `config.py`、`.env.example`、本地 `.env` 的单个预算键、README；新增 `tests/test_memory_budget_config.py`。

**Interfaces:** Settings.memory_context_budget_tokens 默认 20,000，显式合法配置覆盖能力不变。

2026-10-14用户补充：BudgetedModel提供compaction_trigger_tokens=int(budget_tokens*0.8)，新增达到80%且可压缩旧历史时提前触发的测试；无旧轮次且低于硬预算时正常执行。新增temperature与生成回退均为0.3的保护断言。本次不设置新的固定16000环境键，显式预算同比例计算。

- [x] 在隔离 env_file/环境变量条件下先写默认值=20000、合法显式值仍保留的测试，旧默认14854使首项失败。
- [x] 运行 `.venv/Scripts/python.exe -m pytest -q tests/test_memory_budget_config.py tests/test_deepseek_model_config.py`；最小修改默认及示例，然后仅替换/增加本地 MEMORY_CONTEXT_BUDGET_TOKENS。
- [x] 复跑；用 `.venv/Scripts/python.exe -c "from config import SYSTEM_CONFIG; print(SYSTEM_CONFIG['memory_context_budget_tokens'])"` 确认输出20000，不打印其他配置。更新 OpenSpec 3.3。

## Task 6: 普通文件日志（日志 change 2.1/2.2/3.1/3.2，修复 change 3.4）

**Files:** 新增 `utils/runtime_logging.py`、`tests/test_runtime_logging.py`；修改 `cli.py`、`context/telemetry.py`、`travel_data/batch_execution.py`、`context/context_budget.py`、`agents/main_agent.py`、`context/memory_manager.py`、README。

**Interfaces:** configure_runtime_logging(log_dir=None, *, level='INFO', max_bytes=5*1024*1024, backup_count=5) -> Path。CLI main 安装项目 handler 一次，已有 metering 回调和工具执行签名保留。诊断只记录计数/阶段/ID/状态和原因，不记录正文。

- [x] 写真实临时文件中文/上海时区/幂等配置/小容量轮转测试，包含切换路径不关闭外部 handler、导入CLI不产生文件、health和交互入口均配置。
- [x] 写模型成功/失败/usage未知、工具缓存命中、数据库回调失败时仍写普通摘要的测试；数据库记录与错误传播保持。
- [x] 写预算及压缩前后日志包含完整请求值/预算/estimated/覆盖位置/停止原因，敏感 marker 不出现，避免只测试 handler 存在。
- [x] 运行 `.venv/Scripts/python.exe -m pytest -q tests/test_runtime_logging.py`，预期因缺少功能或摘要记录而失败；按已有日志 design 最小实现。
- [x] 运行 `.venv/Scripts/python.exe -m pytest -q tests/test_runtime_logging.py tests/test_v0_telemetry.py tests/test_workflow_telemetry.py tests/test_cli_onboarding.py tests/test_cli_prose.py tests/test_latency_report.py tests/test_cached_candidate_pagination.py`，通过后分别更新两个 changes 任务。
- [x] 在隔离身份的CLI真实请求后查看实际runtime.log，确认不用数据库也能关联模型token、工具缓存和压缩结果；不删除或重建之前清理的用户资料。

## Task 7: 真实行为验收、规格核对和收尾（修复 change 4/5，日志 change 4.1）

**Files:** 新增 `evals/run_harness_memory_acceptance.py`、`docs/evals/2026-10-14-harness-memory-fix/` 报告及原始记录；两个change任务及技术自审/规格核对记录。更新开发文档为实际实现边界。

- [x] 先固定金标准及模拟供应商夹具，检查所有12条候选都能经过生产解析器取得。新工具ID使用不规则值，不能靠编号猜测；真实模型调用、usage与失败尝试逐次落盘。
- [x] 运行正常规划→普通换酒店→明确指定缓存后页目标→保持火车/日期→历史只读，核对实际保存选择而非只看回复关键词/status。正常修改保持两阶段；后页有需要才增加调用；禁止刷新时供应商增量为0。
- [x] 使用每轮真实调用的连续对话记录临时条件、明确更正、干扰性的正常请求，随后在压缩前后分别执行同一规划/修改任务；金标准只进入评分不塞给业务模型。测量压缩调用成本。需要较小预算才能稳定触发的评测必须明确标注覆盖值；另跑20,000可压缩边界，合成历史仅作单独边界复现。
- [x] 执行 `.venv/Scripts/python.exe -m pytest -q`（basetemp选择已存在的工作区父目录）；执行 `.venv/Scripts/python.exe -m ruff check agents context travel_data utils cli.py config.py tests/test_harness_amendment_routing.py tests/test_harness_amendment_selection.py tests/test_history_model_projection.py tests/test_full_request_compaction.py tests/test_memory_budget_config.py tests/test_runtime_logging.py evals/run_harness_memory_acceptance.py`。不修复无关历史问题，遇到检查失败明确区分基线和新增问题并处理本次缺陷。
- [x] 按用户所选方式完成整体代码自审或独立评审，逐项对照两个changes的需求/场景/任务/测试证据；所有重要偏差修复后复验。
- [x] 当前 spec-driven schema 不提供 verify artifact（已实际检查），执行并记录人工规格一致性审核。`openspec validate fix-harness-amendments-and-compaction --strict` 与 `openspec validate add-runtime-file-logging --strict` 是文档校验，不能称为OpenSpec verify。
- [x] 完成条件全部满足后，在实施工作分支依次 archive 两个 changes，再按 finishing-a-development-branch 给用户适用收尾选项；不自动合并或发布PR，不把未完成阶段当作已修复。

## Execution Record

2026-10-14：读取原始错误、规格、既有日志计划和开发文档；修改与压缩定向基线83 passed（2.21s）。第一次基线因basetemp父目录不存在有12个fixture错误，换到已有评测父目录后通过，未修改产品来绕过错误。

用户已明确要求预算20,000、修复编排与压缩，以及加入普通日志。阶段压缩继续延期。用户已选择单代理执行。任务1至6及真实验收已完成，按TDD修复最终审查发现，严格校验和人工规格核对完成。具体RED/GREEN、全量测试、真实调用与失败尝试见[最终验收](../../evals/2026-10-14-harness-memory-fix/README.md)及[自审记录](../verification/2026-10-14-harness-memory-fix.md)。temperature=0.3/max_tokens=1116保持；上下文估算限制如实记录。完成正式归档后保留当前工作区，由用户选择是否整合分支。
