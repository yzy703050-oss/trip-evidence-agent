# 行程摘要定位、上下文精简与跨段修复实施计划

> **For agentic workers:** 使用 superpowers:executing-plans 单代理逐项实施；按下面任务执行 TDD，不派逐任务子代理。

**Goal:** 先以独立摘要定位行程，再加载目标 JSON，减少模型暴露内容并支持受控修复和成功路径资源校准。

**Architecture:** 权威 workflow JSON、查询引用和状态机保持。增加可重建摘要索引、领域投影和修复协调模块；预算采样与熔断分离。

**Tech Stack:** Python 3.11、pytest/pytest-asyncio、现有 AgentScope 适配与 JSON 文件锁。

**Spec:** [proposal](../../../openspec/changes/archive/2026-10-12-progressive-agent-context-disclosure/proposal.md)、[design](../../../openspec/changes/archive/2026-10-12-progressive-agent-context-disclosure/design.md)、[specs](../../../openspec/changes/archive/2026-10-12-progressive-agent-context-disclosure/specs/)、[tasks](../../../openspec/changes/archive/2026-10-12-progressive-agent-context-disclosure/tasks.md)。

## Global Constraints

- 用户已确认单代理执行并授权开始；2026-10-12 补充摘要索引改进也一并实施。
- 不新增主模型原生读取工具，不改供应商失败/模拟策略，不进行新增真实供应商调用。
- 不测试或手工触发单 turn 超限停止/分段提示场景。
- token/模型/调度/辅助委派最终值等全部功能完成后取成功路径同作用域最大值两倍；熔断、工具、超时、候选页大小独立。
- 当前工作目录受权限约束，原生工作区不可写；使用 codex/compact-travel-context 分支。
- 基线 663 passed / 3 skipped；Windows 使用短 basetemp，长路径导致的初次失败不是产品回归。

## Review Focus

- 嵌套 source.url、校验重建结果和多个候选入口不得绕过主投影。
- 到达日期未知、酒店仅地点、模拟和过期信息不能被精简成已验证。
- 已有索引的正常初始化、匹配和保存不得全量读取 workflow。
- 修复重开不得破坏固定条件、有效酒店/交通引用或恢复 checkpoint。
- 调度、模型调用和子委派计数不可混用，失败与熔断不纳入成功预算。

### Task 1: 领域候选与阶段视图（OpenSpec 2.1、2.2）

**Files:** 修改 agents/context_projection.py、agents/workflow_runner.py；测试 tests/test_compact_context.py。

**Interfaces:** project_domain_result(value, candidate_limit=5, requirements=None)、project_child_result、project_model_context、project_delegation_context 保持兼容；返回值不修改输入。

- [x] 写失败测试：真实主请求无 URL/坐标/证据原文，保留候选 ID、价格、模拟 provider、未知日期和酒店地点类型；存储深拷贝不变。测试当前候选只展开一次、委派不含其他任务。
- [x] 运行 `D:/python_3.11.9/python.exe -m pytest tests/test_compact_context.py -q`，预期因现有宽白名单和重复字段 FAIL。
- [x] 实现领域字段白名单、来源投影、需要时保留评分/距离/取消；Runner 提供轻量任务和校验视图，委派字段限定。
- [x] 运行该测试及 agent_context_isolation/workflow_runner/model_workflow_reply 回归，预期 PASS；记录证据并更新 tasks。

### Task 2: 摘要索引与目标加载（OpenSpec 3.1、3.2）

**Files:** 新增 context/workflow_index.py；修改 workflow_store.py、trip_memory.py、memory_manager.py、execution_harness.py；测试 tests/test_workflow_summary_index.py。

**Interfaces:** WorkflowStore.list_summaries() 返回身份/日期/任务小结，不含候选与草稿；保存增量更新索引。缺失/损坏索引显式 rebuild，正常使用不调用 list_all。

- [x] 写失败测试：有索引时 get_known_workflows 和 Harness 恢复不读取无关 JSON，目标读取有版本检查；单个保存不全量加载；索引损坏一次重建、用户隔离和旧摘要修复。
- [x] 运行新测试，预期现有 list_all 扫描导致 FAIL。
- [x] 原子锁内写索引，正常 trip.md 从摘要渲染；兼容显式重建与旧历史接口；Harness 初始化/match 用摘要，resume 后只加载目标。
- [x] 运行新测试和 trip_memory/workflow_matching/unified_travel_history，预期 PASS，维护文档进度。

### Task 3: 跨段修复（OpenSpec 4.1、4.2）

**Files:** 新增 agents/workflow_repair.py；修改 workflow_contracts.py、workflow_runner.py、main_agent.py；测试 tests/test_workflow_repair.py。

**Interfaces:** validate_repair(action, workflow)、apply_repair(workflow, action)、advance_repair(workflow, task_id)；repair_tasks 的字段由 design 固定，返回新 workflow 不修改原输入。

- [x] 写失败测试：实际阻塞冲突可选择旧根任务，拒绝无关/过期/任意条件写入；保留用户条件与有效组件；日期变化使不合适依赖重开，边界不变保留依赖；checkpoint 恢复后重验。
- [x] 运行新测试，预期现有动作合约拒绝 repair_tasks 或模块缺失 FAIL。
- [x] 实现受控根任务重开、非用户推导日期清理、引用版本处理和依赖校验；Runner 调度修复并强制 check_workflow；更新主提示词。
- [x] 运行新测试及 travel_updates/workflow_guard/workflow_runner 回归，预期 PASS。

### Task 4: 调度及成功资源计数（OpenSpec 4.3）

**Files:** 新增 context/resource_budget.py；修改 telemetry/context_budget、Runner、contracts、query-info 子 Agent、config；测试 tests/test_resource_calibration.py。

**Interfaces:** calibration_mode()、资源观测上下文、SuccessBudget；显式 main_decisions_per_turn 保留旧构造额度兼容。

- [x] 写失败测试：实际合法调度动作计数且修复不清零；子请求成功后计数；失败记录与熔断分离；校准上下文解除待测额度而其他边界不变。
- [x] 运行新测试，预期无成功计数/校准模式 FAIL。
- [x] 接入实际请求遥测、调度与辅助委派成功统计；不以 Runner 步数伪装模型请求。
- [x] 运行计数测试与 telemetry/information_agent_loop/workflow_runner，预期 PASS。

### Task 5: 最终采样、全量验证与归档（OpenSpec 5.1—5.3）

**Files:** 新增 scripts/calibrate_resources.py、docs/evals/2026-10-12-resource-calibration.json 与验证记录；更新 config 和正式任务。

**Interfaces:** 校准报告列出 scope、计量方法、来源、成功场景、最大值和两倍配置映射。

- [x] 实现可重复离线采样：单目的地默认返程、多段、历史匹配、恢复修改、跨段修复及最终回复；使用真实 MainAgent 请求与 SDK 形状响应替身，公开替身来源。
- [x] 全部功能完成后运行采样命令，预期所有指定路径完成且有非零测量；独立最大值乘二写回，不提前设置最终值。
- [x] 使用最终配置运行 `D:/python_3.11.9/python.exe -m pytest -q --basetemp "$env:TEMP/ct26b"`，预期全量 PASS；运行必要 lint/compileall 与 git diff --check。
- [x] 单代理整体自审，逐条核对规格/测试证据；本安装无 verify 工作流，明确人工核对。完成后 openspec archive，保留工作分支，不自动 PR/合并。

## 执行记录

实施证据追加在 docs/superpowers/reviews/2026-10-12-compact-travel-context-verification.md；用户新要求需先协调正式规格再调整本计划。
