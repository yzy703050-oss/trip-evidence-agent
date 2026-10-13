# 首次完整提案 Implementation Plan

> **For agentic workers:** 使用 superpowers:executing-plans 单代理执行；用户已选择单代理、当前目录，并明确将本提案纳入正在实施的修改。

**Goal:** 首次提示明确生成住宿时长，普通有效输出不再追加repair_proposal。

**Architecture:** 仅消除initialize日期与住宿规则的歧义，复用现有契约和严格校验。正常路径保持plan与select_batch；保留无效输出有限修正。

**Tech Stack:** Python、pytest、真实配置模型、模拟原始API响应。

**Spec:** openspec/changes/archive/2026-10-13-complete-trip-proposal-in-first-call/{specs/consolidated-travel-execution/spec.md,design.md,tasks.md}。

## Global Constraints

不硬编码晚数、不改已有硬日期、不编造供应商事实、不移除异常修正。与retain-available-travel-candidates独立规格、共同验证。

## Review Focus

明确住宿日期无需额外晚数；多段不能漏住宿；不住宿返程；无效首次输出不直接查询；建议晚数保持soft来源。

### Task 1: 首次提示及契约回归（OpenSpec 1.1、1.2、2.1）

**Files:** agents/main_agent.py、tests/test_first_call_stay_proposal.py。

**Interfaces:** MainAgent.initialize(context) → workflow_proposal；WorkflowRunner.run使用create_workflow(require_stay_proposals=True)，异常调用repair_proposal一次。

- [x] 回归普通往返、明确入住离店、多目的地、无效后修正成功及再次无效；通过真实MainAgent及Runner计数模型阶段/供应商请求，核对条件和来源。
- [x] 先运行测试确认现有严格校验及兜底行为。提示行为不以文本断言或根据提示分支的假模型证明；新增程序回归属于已有行为刻画，不人为制造红灯。
- [x] 修改initialize住宿规则：每个requires_hotel任务有合法正整数nights或完整日期；用户未给时长同次模型建议nights、proposal、soft；未知抵达继续留空。示例用N表示模型自行决定，不复制固定晚数。
- [x] 重跑回归测试，补充真实模型全流程证据来验证提示效果。不得把脚本模型的正确输出当成提示已验证。

### Task 2: 完整流程评估和收尾（OpenSpec 2.2、3.1、3.2）

**Files:** evals/run_first_call_proposal_measurement.py、docs/evals/2026-10-13-first-call-proposal/。

**Interfaces:** ExecutionHarness.run_turn(context,run_state)执行完整请求；Capture记录model_stage和usage，ApiShapedTrainProvider/ApiShapedHotelProvider模拟HTTP并使用生产解析器。

- [x] 三个真实模型请求：普通上海往返、明确14至16日入住离店、多目的地上海再杭州。分别隔离记忆，设置可信起点重庆，当前时间固定2026-10-13。
- [x] 普通有效路径严格验收仅plan与select_batch，首次提案每段住宿合法、建议与硬日期来源正确。每场景最多3模型调用，保留失败记录，不自动重试；输出各阶段/总token和端到端延时。
- [x] 运行相关及全量pytest、ruff、git diff --check、两份OpenSpec严格校验；自审与人工verify记录后分别归档，保留当前未提交分支。

## Technical review / Rulings

现有create_workflow严格住宿校验和Runner单次修正已经实现所需异常策略，无需新状态或默认晚数。此变更只有模型提示行为调整；TDD程序回归记录已有行为，真实模型评估验证新提示，不构造会读取提示内容来伪造输出的测试。此前实际日志的缺晚数输出是失败基线，保存原诊断而不改写历史输出。
