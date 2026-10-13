# 保留已有差旅候选 Implementation Plan

> **For agentic workers:** 使用 superpowers:executing-plans 单代理逐项执行；用户已经选择当前目录实施，不重复询问，不自动提交。

**Goal:** 修正实际上海请求中无报价即放弃住宿、去程失败即放弃返程及误述没查到的问题。

**Architecture:** 批量选择提示明确保留可用部分；回复以当前查询视图区分证据缺口，现有guard保留硬约束。

**Tech Stack:** Python、pytest、SQLite只读日志、OpenSpec。

**Spec:** openspec/changes/archive/2026-10-13-retain-available-travel-candidates/{specs/consolidated-travel-execution/spec.md,design.md,tasks.md}。

## Global Constraints

单代理、当前目录；不改变清洗、分页和硬约束；不修改历史数据库；模型回放限一次，不调用供应商或RAG。

## Review Focus

成功空结果、条件排除、旧版本及刷新失败必须准确表达；未知抵达不编造衔接；普通酒店地点建议与硬报价要求分别验证。

### Task 1: 回复事实分类（OpenSpec 2.1）

**Files:** tests/test_partial_selection_evidence.py、tests/fixtures/shanghai_partial_selection.json、utils/workflow_reply.py。

**Interfaces:** build_reply_facts(result)消费workflow及reconstructed_tasks，产生不可改写的占位事实；query_views(workflow,task_id,limit=1,offset=0)提供当前候选总数。

- [x] 保存去标识化实际日志夹具；测试有候选未选择的车次/酒店不再写没查到，分别覆盖空、失败、条件排除、旧版本、待核实，且不改原数据。
- [x] 运行 `.venv/Scripts/python.exe -X utf8 -m pytest tests/test_partial_selection_evidence.py -q`，观察预期文案断言失败。
- [x] 最小实现缺口分类，复用筛选视图；不静默自动挑选候选。
- [x] 重跑上述测试及test_model_workflow_reply，确认通过并更新tasks。

### Task 2: 主Agent选择与完整验证（OpenSpec 2.2、2.3、3.1、3.2）

**Files:** agents/main_agent.py、evals/run_shanghai_selection_replay.py、docs/evals/2026-10-13-shanghai-selection-replay/。

**Interfaces:** MainAgent.select_batch(context)返回commit_batch，check_batch_submission(workflow,action)重建有来源安排；已有Capture记录usage和延时。

- [x] 离线先验证保存的三个null选择能复现缺口；选择酒店/返程时去程仍为空且校验保持partial，硬报价要求仍阻塞。
- [x] 最小修改select_batch共同提示：无附加硬要求则选酒店地点；已有返程独立选择；明确未知衔接和失败范围。
- [x] 只读重建实际上下文，限一次模型调用，断言酒店与返程引用非空、去程为空、停止补查、校验保留去程缺口；保存动作、结果和usage。不通过则如实记录并继续定位，不无限重试。
- [x] 运行全量 `.venv/Scripts/python.exe -X utf8 -m pytest -q`、ruff、git diff --check和OpenSpec严格校验。自审核对所有场景，CLI没有verify则人工核对并记录。
- [x] 完成任务记录及OpenSpec archive；分支保持未提交。
