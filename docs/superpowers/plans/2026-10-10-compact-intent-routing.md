# 四类意图路由实施计划

> **For agentic workers:** 按用户选择使用 superpowers:executing-plans 单代理实施，或 superpowers:subagent-driven-development 多子代理实施。尚未选择，不默认派子代理。

**Goal:** 将顶层意图收敛为 ask、plan、update、control，复用现有执行框架。

**Architecture:** 同一次初始化模型调用完成四类目的识别与现有执行决策；contracts 验证类别并兼容缺失标签，travel_updates 根据实际保存条件区分补充或修改。对象、动作、检索来源继续由现有字段表达。

**Tech Stack:** Python 3.11、AgentScope、pytest/pytest-asyncio、现有 WorkflowRunner。

**Spec:** [正式增量设计](../specs/2026-10-10-compact-intent-routing-design.md)，特别是 R1–R5；前置设计为 [planning-preflight](../specs/2026-10-10-planning-preflight-design.md)。项目未初始化 OpenSpec，不存在本次 change 的 specs/design/tasks；不创建替代 OpenSpec 目录。本文只展开正式设计的实施步骤和证据。

## 全局约束

- 仅在 `.superpowers/trip-evidence-agent` 独立仓库实施，使用当前 `codex/information-acquisition-agent` 分支；用户已选择提交、推送并创建 PR。
- 四类意图仅 ask/plan/update/control；不新增独立识别 Agent，不增加初始化模型调用，不增加业务能力。
- 保留现有 response_mode、agent_schedule、workflow_proposal、resume_workflow_id、travel_update 与旧 workflow_update 兼容入口。
- 保留用户条件、停留建议、日期来源、起点断点、候选引用、版本与组件作用域校验。

## 评审重点

1. 模型只输出意图而漏执行 payload，或输出正确标签却绕过执行校验：任务1、2测试拒绝。
2. 用户偏好更新与当前查询/旅行修改混合，同类类别去重后仍要执行两件事：任务1、2测试。
3. origin 断点短回答、预算 constraints 混合新旧字段、任务继承全程条件：任务3测试。
4. 多个已有旅行、模糊“不满意”、购票诉求误触发旅行重查：任务2、4检查。
5. 兼容推导创建了额外任务，或相同更新破坏原候选/建议：任务1、3与现有回归核对。

## Task 1：四类输出契约与兼容（R1、R2、R5）

**Files:** 修改 `agents/contracts.py`；新建 `tests/test_intent_contracts.py`；复用 `tests/test_main_agent.py`、`tests/test_main_harness.py`。

**Interfaces:** contracts 定义 `INTENT_TYPES = frozenset({'ask','plan','update','control'})`；`normalize_intents(plan: dict) -> list[dict]` 只返回 type 字段列表；`validate_plan(value: dict) -> dict` 继续返回复制、校验后的执行决定，并写入标准化 intents。

- [x] 先写有意义的失败测试：`test_rejects_unknown_or_malformed_intents` 对未知 type、字符串、非对象项及畸形 type 抛 ValueError；`test_missing_intents_are_inferred_without_changing_execution` 验证旧查询得到 ask，其他执行字段未改；`test_combined_preference_and_planning_keeps_both_purposes` 得到 update/plan 且保留两种 payload；`test_intents_cannot_bypass_execution_validation` 仍拒绝空 change、无身份 workflow 和未授权业务角色。
- [x] 运行 `../../.venv/Scripts/python.exe -m pytest tests/test_intent_contracts.py -q`；预期失败原因是缺少类别验证和兼容推导，不能是依赖/导入环境失败。
- [x] 最小实现四类常量、列表/对象/type 验证、重复类别去重及 R5 缺失推导。推导只读决定，不新增动作；有标签也不能跳过原契约校验。额外历史描述字段只作为兼容输入，标准化输出保留 type。
- [x] 运行该文件及 `tests/test_main_agent.py tests/test_main_harness.py`，应全部通过；更新下面实施记录。

## Task 2：主 Agent 提示词与初始决定（R1、R2、R4、R5）

**Files:** 修改 `agents/main_agent.py`、`tests/test_planning_intents.py`、`tests/test_workflow_main.py`；新增场景到 `tests/test_main_end_to_end.py` 或本任务新测试文件。

**Interfaces:** `MainAgent.initialize(context: dict) -> dict` 保持接口及一次受限修复；INTENTS 只来自任务1的四类常量。workflow 和普通模式均输出 intents；travel_update 等字段不变。

- [x] 先用脚本模型覆盖 `test_prompt_uses_four_purposes_instead_of_feedback_catalog`：四类声明、检索源与动作分别表达、没有旧完整意图目录；新增实际决定断言确认 workflow 和组合请求有标准化 intents。
- [x] 写 `test_invalid_intent_gets_one_repair`，首答未知 type、次答合法，断言恰好两次调用；修复仍错则停止。补充 `test_explain_and_ambiguous_feedback_do_not_execute_queries` 与不支持购票零交易/查询测试。脚本模型只证明协议与执行链，不用于声称自然语言识别准确。
- [x] 运行 `../../.venv/Scripts/python.exe -m pytest tests/test_planning_intents.py tests/test_workflow_main.py tests/test_main_end_to_end.py -q`；新测试应因旧分类章节或缺少类别契约失败。
- [x] 重写 initialize 意图段落及示例，删除20标签目录与按对象命名意图。保留 ask 的各检索来源、update 的各执行操作、control 的能力限制，以及全部原日期/建议/范围约束。workflow 不再省略 intents，继续省略 rewritten_query/key_entities。
- [x] 调整被新规格覆盖的旧提示词断言，复跑以上测试并确认没有新增模型调用；更新实施记录。

## Task 3：由保存状态区分条件补充与修改（R3）

**Files:** 修改 `agents/travel_updates.py`、`tests/test_travel_updates.py`；复用 `tests/test_preflight_end_to_end.py`。

**Interfaces:** 新增 `_condition_update_type(tasks: list[dict], conditions: dict, task_fields: dict, confirmed_conditions: dict) -> str` 返回 supplement/change。`apply_travel_update(workflow, update, *, context=None)` 保持接口；仅在已有 supplement/change 分支按所选任务与全程有效条件规范化 kind 与 last_update，其他更新类型不动。

- [x] 先写 `test_condition_update_type_comes_from_saved_values`：空起点收到模型 change 规范化 supplement，已知起点变化收到 supplement 规范化 change；输入 workflow/update 不被修改。
- [x] 写叶字段与组合用例：新增 constraints 预算且改已有日期为 change；纯新字段为 supplement；覆盖建议值为 change；完全相同值为 supplement；继承 confirmed_conditions 的已知字段变化为 change；多个选中任务任一值变化为 change。
- [x] 运行 `../../.venv/Scripts/python.exe -m pytest tests/test_travel_updates.py -q`；预期因 last_update 仍信任模型分类失败，先确认失败再实现。
- [x] 在条件已验证、品牌已映射、真实任务已加载后比较有效旧值与明确新值，constraints 递归按叶比较。规范化 last_update，不改原更新对象；保留现有字段合并、失效范围、task/workflow ID、日期/晚数和依赖处理。
- [x] 运行 `../../.venv/Scripts/python.exe -m pytest tests/test_travel_updates.py tests/test_preflight_end_to_end.py tests/test_preflight_workflow.py -q`；全部通过且同ID补起点、定向换酒店保留火车等既有场景通过；更新实施记录。
- [x] 测评发现的恢复回归：先复现澄清断点→暂停→跨会话继续的版本冲突，停止保存时同步断点到下一 CAS 版本并保留原问题；复测恢复零查询，冲突检查不能删除。

## Task 4：真实识别测评、文档和最终审核（R1–R5）

**Files:** 修改 `README.md`、`evals/run_preflight_simulated.py` 及其 `tests/test_preflight_workflow.py` 或新测评评估测试；新建 `docs/evals/2026-10-10-compact-intent-routing-evaluation.md`。只在必要时补充 `tests/test_main_evals.py`。

**Interfaces:** 测评继续使用已配置真实模型、隔离 memory、显式模拟火车/酒店 Provider；agent_plan 决定已在遥测保存，不另造持久化协议。

- [x] 先为评估器增加失败测试：输出正确结果但顶层旧/缺失意图不能宣称四类识别通过；组合请求丢偏好、解释或暂停发起查询须判失败。
- [x] 更新已有12场景测评的意图断言，并补充控制恢复、条件变更及不支持交易场景；优先复用已有保存旅行及已有 assess，避免复制一套测评框架。README 改为四类表格，单独说明对象/来源/操作和上下文如何表达。
- [x] 运行定向回归后运行 `../../.venv/Scripts/python.exe -m pytest -q`，读取通过/失败/跳过输出；运行 `git diff --check`。
- [x] 运行 `../../.venv/Scripts/python.exe -m evals.run_preflight_simulated --output data/evals/2026-10-10-compact-intents`，记录模型实际输出、场景结果、调用次数与耗时。失败场景修复后按依赖重测；模型服务失败则明确记录未验证。
- [x] 对照正式设计 R1–R5逐项核对代码、测试和测评，完成当前代理整体自审；用户选择多子代理时按所选流程补独立评审。记录全部证据后呈现分支收尾选择；项目未初始化 OpenSpec，因此不运行 verify/archive，不宣称它们完成。

## 实施记录

- [x] Task 1：输出契约与兼容。先见29个预期失败，修改后定向53 passed；旧执行校验继续生效。
- [x] Task 2：主 Agent 提示词与识别分流。先见2个提示词预期失败，修改后定向52 passed；一次修复和零查询边界通过。
- [x] Task 3：状态驱动的条件补充/修改。先见11个预期失败，修改后定向37 passed；继承条件和局部修改回归通过。
- [x] Task 4：全量测试、真实模型测评、规格核对与代码自审。

用户已选择单代理，全部实施与验证完成。最终517 passed、3 skipped；17个真实模型配模拟供应商场景逐项通过，失败及定向复测见[验证记录](../../evals/2026-10-10-compact-intent-routing-evaluation.md)。用户已选择提交、推送并创建 PR；本记录对应提交前验证。
