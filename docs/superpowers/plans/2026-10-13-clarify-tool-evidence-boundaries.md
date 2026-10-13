# 工具事实与停止补查提示词实施计划

> **For agentic workers:** 使用 superpowers:executing-plans 单代理执行；用户已选择当前目录。2026-10-13 用户确认“也做”，按补充设计直接实施，不重复询问执行方式。

**Goal:** 按已返回事实规划，缺字段直接说明；模型调整软日期并定向验证，已证明硬日期冲突等待用户。

**Architecture:** 独立条件强度 helper 负责新旧字段和硬边界解释；冲突 helper 检查明确日期关系和完整有效候选，Runner 保存等待记录。扩展现有 repair_tasks 日期补丁并删除工具的自动顺延；清洗、供应商及状态枚举保持原样。

**Tech Stack:** Python、现有 MainAgent 模型适配、pytest、OpenSpec。

**Spec:** [变更规格](../../../openspec/changes/clarify-tool-evidence-boundaries/specs/consolidated-travel-execution/spec.md)、[技术设计](../../../openspec/changes/clarify-tool-evidence-boundaries/design.md)、[任务](../../../openspec/changes/clarify-tool-evidence-boundaries/tasks.md)。归档后更新链接。

## Global Constraints

- 按补充需求修改字段、日期修复和硬冲突分流；不改数据清洗或供应商，不实施未来信息获取 Agent。
- 固定条件、硬政策和真实事实保留；未知不补造，soft preference 不当硬约束。
- 首次查询、其他未执行的独立任务和用户后续新查询继续可用。
- 用户已选择单代理、自审、当前目录；不派子代理、不自行提交 Git。

## Review Focus

- 有地点无房价：停止补查，说明无法核实，不声称合规。
- 报价全部超标：说明当前范围不合规，不强制刷新，不声称全市场无酒店。
- 火车查询失败或到达日期未知：保留可用安排，不改固定日期或伪造抵达。
- 靠海等软偏好无证据：说明未核实，不自动补查或无故降级。
- 新一轮明确刷新与修改：初始化继续发出合法定向请求，停止规则不误伤首次执行。

## Task 1: 强度字段和硬日期冲突（OpenSpec 1.1–1.2）

**Files:** 新建 agents/condition_strengths.py、agents/condition_conflicts.py、tests/test_condition_strengths.py、tests/test_condition_conflicts.py；修改 workflow_contracts/model_contracts/context_projection/workflow_guard/workflow_runner。

**Interfaces:** validate_condition_strengths(value, conditions) 验证 date/nights 的 hard/soft；condition_is_hard(workflow, task, key) 统一旧来源与全局继承；find_hard_date_conflict(workflow) 返回具体冲突或 None，候选必须来自当前版本全量规范化缓存。

- [ ] 先写并运行失败测试：user来源soft可保留、非法枚举拒绝、旧记录hard/soft、硬全局不能降级、明确日期自相矛盾、全候选迟到、另有可行候选、未知到达不证明不可行。
- [ ] 最小实现字段与判定；Runner 在查询前/后及完整提交后使用 helper 保存 hard_condition_conflict checkpoint，返回 needs_input；不能跳过引用或政策校验。
- [ ] 定向验证字段和冲突测试，记录真实输出，更新任务。

## Task 2: 受控模型日期调整（OpenSpec 2.1–2.2）

**Files:** 修改 agents/workflow_repair.py、agents/planning_conditions.py、agents/workflow_batches.py、agents/travel_updates.py、agents/workflow_amendments.py、travel_data/tools.py；新建 tests/test_model_date_adjustment.py，协调 tests/test_preflight_workflow.py。

**Interfaces:** repair_tasks 新增可选 task_updates=[{task_id, conditions}]，仅相关软日期/nights可变；强度和硬边界保持。查询 ID、版本和失效沿用原实现，用户明确更新可修改硬条件。

- [ ] 先验证失败：工具只查原日期，模型可同时调整去返程软日期，user软日期可改、hard日期拒绝、全局硬期限拒绝、不相关任务拒绝、旧报价不得改日期沿用。
- [ ] 修改相关守卫和修复；重用未受影响缓存，仅补新日期需要的领域。保留旧记录兼容，不让 user/context 来源再次锁住显式 soft。
- [ ] 定向验证日期修复及已有旅程修改回归，更新任务。

## Task 3: 统一提示并验收（OpenSpec 3.1–3.3）

**Files:**
- Modify: agents/main_agent.py（initialize/select_batch/amend_saved_trip/step/finalize 相关提示）
- Modify: agents/model_contracts.py（batch_query_contract_prompt）
- Modify: .claude/skills/plan-trip/SKILL.md（主 Agent 实际加载的工作流指南）
- Modify: docs/development/2026-10-12-follow-up-context-and-workflow.md（此次实施记录）
- Create: docs/evals/2026-10-13-tool-evidence-boundaries/（效果与成本记录）

**Interfaces:** 提案添加全局/任务 condition_strengths，repair_tasks 包含受控日期补丁；模型阶段不变。能力缺口使用 commit_batch.allow_repair=false，合理软日期调整允许 repair_tasks，硬冲突保存问题等待用户。

- [ ] 基线：保留已记录的 5 次模型、24,449 token 重复查询失败记录，以及现有提示中的强制刷新指令，作为改动前证据。提示词属于可逆文本变更，不新增逐字断言或从提示中硬编码结果的测试。
- [ ] 修改 select_batch：已有证据重组优先；接口缺字段时写 unverified_requirements 并停止；可调整软日期的真实安排问题可用 repair_tasks，硬条件冲突反馈用户。
- [ ] 修改 batch_query_contract_prompt：移除 refresh_example 和超标必须刷新；保留工具能力、参数规范、每晚金额；增加软日期补丁说明，不引导同能力缺口反复查。
- [ ] 协调其余提示及指南：初始化保留必要首次查询和新轮刷新；step 不为已返回缺口重查；finalize 不因缺口输出 needs_requery；amend_saved_trip 不编造选择，也不引导模型用查询补齐未知字段。
- [ ] 定向回归：`.venv/Scripts/python.exe -m pytest tests/test_main_batch_protocol.py tests/test_workflow_main.py tests/test_live_model_contracts.py tests/test_consolidated_travel_run.py tests/test_main_output.py tests/test_main_feedback.py -q`。如旧测试从 refresh_example 人为生成查询动作，仅改为显式测试既有工具执行契约，不能把它当新提示实际服从证据。
- [ ] 真实模型单轮回放：复用已保存的地点缺报价、报价全部超标及正常候选上下文，各运行一次 select_batch，不调用供应商/RAG。记录实际 action、停止标记、缺口、usage 和延时；停止要求或固定事实不满足时记录失败并修订提示，不能只凭文字检查宣称有效。
- [ ] 全量回归：`.venv/Scripts/python.exe -m pytest -q --basetemp .superpowers/test-tmp/evidence-boundaries`；运行 git diff --check，读输出。
- [ ] 自审与规格核对：把结果分别对照所有场景；OpenSpec CLI 没有 verify 工作流则明确人工核对，使用 validate 验证文档。全部完成后归档，保留未提交变更供用户检查。

## 拟加入主 Agent 的核心指令（审阅稿）

```text
以当前上下文中各任务已返回的结果为事实依据，能完成的安排先完成。
现阶段，字段缺失、接口失败、能力不足或候选不满足要求，都直接说明具体限制；本轮不为补齐这些问题追加查询、刷新或重开任务。
酒店地点不代表房价、空房或海景房；没有证据不能声称已核实、符合政策或客观不存在。
优先从已有候选重组，保留用户固定安排和政策硬约束；无法满足时反馈冲突，不能偷偷改条件。
能力缺口或没有可执行调整时 commit_batch 使用 allow_repair=false；有依据的软日期调整可通过受控 repair_tasks 同时提出去程与返程日期，不能把未知票价或时刻补造为事实。
缺口不是用户条件缺失，不让用户授权替代事实；真实固定条件冲突可以说明可调整项，等待用户反馈。
首次必要查询、用户新请求及合法新日期所需定向查询不受能力缺口停止规则影响。
```

## 审阅与实施状态

2026-10-13：用户要求由模型调整去返程日期，并明确同意新日期定向查询；随后确认条件字段和冲突规则“也做”。已扩展设计并完成技术复核，进入单代理实施。
