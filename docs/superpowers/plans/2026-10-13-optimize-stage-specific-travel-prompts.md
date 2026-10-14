# 按阶段精简提示 Implementation Plan

> **For agentic workers:** 使用superpowers:executing-plans单代理执行。用户已选择当前目录，已要求开始优化并测试，不重复询问。

**Goal:** 减少提示与无关schema用量，保持最终行程合理、必要时继续披露。

**Architecture:** travel_prompts生成阶段提示；模型边界投影按allowed_actions提供契约。程序状态、候选、校验和修复通道不变。

**Tech Stack:** Python、pytest、OpenSpec、真实模型、模拟API。

**Spec:** openspec/changes/optimize-stage-specific-travel-prompts/{specs/progressive-context-disclosure/spec.md,design.md,tasks.md}。

## Global Constraints

单代理当前目录；完整缓存及默认5候选保留；用户硬条件和政策不降级；不限制所有请求必须两次模型；不运行真实供应商、不改原用户数据库。

## Review Focus

有历史时保留目标定位；无历史的修改请求澄清；无报价仍推荐普通酒店地点；去程失败保留后段；成功空结果soft改期与hard冲突保护。

### Task 1: 模型输入边界（OpenSpec 2.1）

**Files:** agents/context_projection.py、tests/test_stage_specific_prompts.py。

**Interfaces:** project_batch_decision_context(context)返回新dict，保留workflow/task_results/actions；无查询权限时只提供query_capabilities，查询开放时保留实际相关schema。

- [x] 写提交阶段无查询schema、开放查询时分页/参数仍合法、到达/天气相关性与输入不变性测试。
- [x] 运行pytest观察预期缺函数/投影不符失败；实现最小投影并复跑。

### Task 2: 阶段提示与评估（OpenSpec 2.2、2.3）

**Files:** agents/travel_prompts.py、agents/main_agent.py、agents/model_contracts.py、evals/run_prompt_optimization.py。

**Interfaces:** initial_prompt(context)、selection_prompt(context)返回带__MAIN_CONTEXT__的模板；workflow_contract_prompt(compact=True)提供紧凑结构。

- [x] 冻结旧首次及选择提示，不调用模型；将已有依赖全文schema的测试改为验证独立契约和实际输出，不伪造提示行为。
- [x] 最小替换提示构建与模型边界投影，运行首次/修改/匹配/分页/修复/政策回归。
- [x] 同夹具完整基线对比；优化后运行普通、固定、多段、失败及局部修改，保存最终回复与分阶段usage。另回放原实际数据、政策缺报价、soft改期和hard保护。每阶段组有调用上限，失败不自动重复。
- [x] 人工审查每份最终输出：引用事实、日期、金额、库存、住宿晚数、硬约束、缺口、局部保留与预订声明；将失败及修正原因记录，不用节省掩盖失败。

### Task 3: 完整验证与收尾（OpenSpec 3.1、3.2）

- [x] 全量pytest、ruff、git diff --check、OpenSpec strict、自审及人工verify，保留证据。
- [x] 更新任务和评估报告、同步归档，保持当前未提交目录；其他候选筛选提案保持独立。

实施记录：边界测试RED为缺函数6项失败，GREEN为6项通过；首次相关回归53项通过。真实模型首轮失败后仅补回字段层级/目的/日期/返程边界，保留首轮记录；最终5类请求及5项复杂回放符合预期。全量864通过、3跳过；核心变更ruff和diff检查通过。CLI无verify，按规格场景人工核对。详见docs/evals/2026-10-13-prompt-optimization/report.md。
