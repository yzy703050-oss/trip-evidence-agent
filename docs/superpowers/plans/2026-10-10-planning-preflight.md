# Planning Preflight Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans. 用户明确选择当前分支单代理执行，最终整体自审，不派任务子代理。

**Goal:** 实现先答复的两层意图与逐段任务编排、到达日期查询、局部修改及trip.md长期旅行记忆。

**Architecture:** 主Agent提出任务骨架并按结果决定补全、查询和草稿；Harness管理默认值、版本、作用域、预算和最终校验。信息获取复用既有工具循环；trip.md是详细WorkflowStore的可读投影。

**Tech Stack:** Python 3.11、AgentScope、pytest、现有Provider接口。

**Spec:** [已确认设计](../specs/2026-10-10-planning-preflight-design.md)，并沿用其引用的多目的地设计。项目未初始化OpenSpec，人工逐项核对，不声称运行verify/archive。

## Global Constraints

- 单代理在当前分支执行，RAG行为不变；不增加酒店估价或活动规划。
- 首次缺个人条件返回部分方案；只有修改范围不明时询问。
- 默认首段日期为北京时间今天+7，来源default；恢复不漂移，不覆盖到达或固定日期。
- 信息获取每任务最多3次、每次最多6次模型及10次业务工具调用，整轮时间600秒；封装中的真实外部请求计入预算。
- 价格与库存3600秒时效；未知事实阻止completed，但不阻止保存部分draft。
- 用户已于本轮明确“开始修改”，设计和执行方式均已确认，先保存此具体计划随后实施，不重复请求开始许可。
- 用户授权模拟失效火车接口及必要时酒店接口用于测试：模拟Provider显式注入，标注simulation来源，生产默认不自动回退到模拟。

## Review Focus

- 只换酒店不能换火车，任务版本变化后未改变组件必须保留并可重新绑定证据（Task 1/3）。
- 不满意不等于缺字段，条件完整时应换候选而不是清空用户条件（Task 1/5）。
- 跨日和中途站历时、日期筛选不完整时不能宣称可行（Task 2）。
- 缺出发地时仍查酒店并回复，不能生成假的交通或停止其他独立任务（Task 3）。
- completed旅行在新会话也能定位，摘要版本落后不能回滚权威状态（Task 4）。

### Task 1: 任务骨架、默认日期与局部修改协议

**Files:** `agents/workflow_contracts.py`、新增`agents/planning_conditions.py`和`agents/travel_updates.py`；`tests/test_planning_conditions.py`、`tests/test_travel_updates.py`。

**Interfaces:** `prepare_task(workflow, task_id, current_time) -> dict`返回有效条件、来源、缺项；`apply_travel_update(workflow, update) -> dict`执行组件/任务/路线更新，既有`apply_user_update`继续兼容。

- [ ] 先写P01/P02/P09/P13/P17/P18的默认、未知起点、目的与返程酒店测试；P23/P24/P25/P28的作用域、拒绝候选和版本测试。
- [ ] RED：`../../.venv/Scripts/python.exe -m pytest tests/test_planning_conditions.py tests/test_travel_updates.py -q`，预期因接口缺失或新字段拒绝失败。
- [ ] 最小实现骨架校验、到达条件、程序日期建议、组件更新与保留未改变证据；完成条件来源及下游重检。
- [ ] GREEN：上面命令及`tests/test_workflow_contracts.py`通过；只调整被本次已确认规格取代的旧断言。
- [ ] 保存Task 1验证证据并本地提交。

### Task 2: 到达日期查询、历时与模拟Provider

**Files:** 新增`travel_data/arrival_search.py`、`evals/simulated_travel.py`；修改`travel_data/contracts.py`、`travel_data/juhe_train.py`、`travel_data/tools.py`；新增`tests/test_arrival_search.py`和`tests/test_simulated_travel.py`。

**Interfaces:** `search_by_arrival(provider, fields, *, request_budget) -> AgentDataResult`在有界日期范围查询并过滤；模拟train/hotel Provider符合现有search契约，所有source显式simulation。

- [ ] 先写P03/P05/P06/P07/P14测试：同日/跨日/>24小时/跨年、只有时刻未知、接口失败不盲目重试、内部请求预算、模拟来源与酒店无报价。
- [ ] RED：`../../.venv/Scripts/python.exe -m pytest tests/test_arrival_search.py tests/test_simulated_travel.py -q`，预期缺新接口/历时未解析。
- [ ] 解析区间duration并核对到达时刻，工具参数增加arrival查询与预算；保留旧出发查询。
- [ ] GREEN：上述测试及`tests/test_juhe_train_provider.py tests/test_information_tools.py`通过。
- [ ] 保存Task 2证据并提交。

### Task 3: 先答复的任务补全与ReAct编排

**Files:** `agents/workflow_runner.py`、`agents/workflow_guard.py`、`agents/workflow_queries.py`、`.claude/skills/query-info/script/agent.py`；新增`tests/test_preflight_workflow.py`。

**Interfaces:** 原`dispatch`增加mode，补全请求携带已有task身份；部分draft可保存但最终完成校验不放宽；Runner使用Task 1条件整理和Task 2工具。

- [ ] 先写P02/P11/P19/P21/P22/P29：缺起点酒店仍返回、不询问必填、到达日期补全结果复用、下游可靠日期继承、次数共享、部分draft不能completed。
- [ ] RED：`../../.venv/Scripts/python.exe -m pytest tests/test_preflight_workflow.py -q`。
- [ ] 最小实现完整/不足条件分支、程序处理有来源日期提案、逐段结果观察、独立查询推进和修改后组件复用。
- [ ] GREEN：上述测试及`tests/test_workflow_runner.py tests/test_workflow_guard.py tests/test_workflow_queries.py`通过。
- [ ] 保存Task 3证据并提交。

### Task 4: trip.md长期摘要、恢复与幂等迁移

**Files:** 新增`context/trip_memory.py`；修改`context/memory_manager.py`、`context/workflow_store.py`、`context/long_term_memory.py`、必要的记忆查询适配；新增`tests/test_trip_memory.py`。

**Interfaces:** 从已保存workflows构建/更新trip.md，`get_known_workflows`按需返回活动及最近已完成概览；JSON为恢复依据，摘要失败可重建。

- [ ] 先写P30/P31/P32/P33：partial首轮记录、新会话completed查询、同ID覆盖、旧trips保留、过时摘要重建、失败不重放API。
- [ ] RED：`../../.venv/Scripts/python.exe -m pytest tests/test_trip_memory.py -q`。
- [ ] 程序原子更新用户隔离Markdown投影，摘要带ID/revision/状态/目的/安排/来源/缺项，不从Markdown推断事实。
- [ ] GREEN：上述测试及`tests/test_workflow_store.py`和相关记忆测试通过。
- [ ] 保存Task 4证据并提交。

### Task 5: 主Agent完整意图与模拟接口端到端评测

**Files:** `agents/main_agent.py`、`agents/contracts.py`、`agents/execution_harness.py`、技能文档与README；新增`tests/test_planning_intents.py`、`tests/test_preflight_end_to_end.py`、`evals/run_preflight_simulated.py`及评估报告。

**Interfaces:** 主Agent支持设计10.1全部意图和请求级作用域更新；Harness执行Task 1更新并加载Task 4状态，既有非规划查询/偏好/RAG路由保持。

- [ ] 先写P08/P10/P12/P20/P26/P27/P28及新旅行、补充、换火车/酒店、整段/全程重生成、路线修改、接受选择、暂停取消；不支持交易明确回复。
- [ ] RED：`../../.venv/Scripts/python.exe -m pytest tests/test_planning_intents.py tests/test_preflight_end_to_end.py -q`。
- [ ] 更新模型结构与指南，接入查询条件、目标范围和长期摘要；模型结构无效仍有界修复，不编造证据。
- [ ] GREEN：相关测试通过，再运行`../../.venv/Scripts/python.exe -m pytest -q -rs`和`git diff --check`。
- [ ] 真实模型+显式模拟Provider评测至少覆盖设计所列真实模型用例与修改回合；记录模型调用与simulation来源，不声称真实票价/库存验证。
- [ ] 整体自审、P01–P33规格核对、更新本计划状态及正式设计实施状态；报告任何测试失败或真实接口缺口。
- [ ] 本地提交并保留当前分支，不擅自push/merge。
