# 主 Agent 本地缓存翻页 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: 使用 superpowers:executing-plans（单代理）或 superpowers:subagent-driven-development（多子代理）逐项执行。执行方式由用户选择，未选择前不实施；单代理模式仅整体自审，不自动派发评审子代理。

**Goal:** 主 Agent 首批候选不适合时，可在提交前自主调用既有查询工具读取后续缓存页，同时保证供应商请求数不增加。

**Architecture:** 沿用 schedule_queries/query_plan。程序生成可用本地下一页命令，校验后以内部 cache_only 模式执行；分页元数据贯穿工具、批量归并和模型视图。完整缓存用于最终引用与全程校验。

**Tech Stack:** Python 3.11、asyncio、pytest/pytest-asyncio、既有 OpenSpec CLI；不新增依赖。

**Spec:** openspec/changes/archive/2026-10-14-enable-main-agent-cached-pagination/ 下的 proposal.md、design.md、tasks.md、specs/bounded-travel-tool-results/spec.md、specs/consolidated-travel-execution/spec.md。正式行为以这些规格和已确认设计为准，本计划只展开实施。

## Global Constraints

- 默认每页 5 条，排序及过滤保持现状；同条件后页只能读取有效缓存。
- 保留 query_id、result_revision、task_revision、来源、获取时间与前页有效引用。
- 不开发供应商接口翻页，不增加报价能力，不因接口缺字段刷新。
- 不改变固定条件、范围、现有模型决策上限或最终全程校验。
- 本地读取不增加外部请求数，外部预算耗尽不阻止有效缓存后页；仍服从停止、时间及决策预算。
- 工作区已有其他未提交修改，逐文件核对差异，只提交本任务范围；执行前按 using-git-worktrees 检查隔离需求，不从默认分支丢掉当前上下文。
- 每项任务遵循失败测试→确认原因→最小实现→复测，凭实际证据更新 OpenSpec tasks.md。

## Review Focus

1. 同批重复翻同一查询会导致偏移竞态：任务 2 覆盖原子拒绝、工作流不被改写。
2. 已验证命令执行前缓存消失：任务 2 验证工具边界不退回供应商、不覆盖原查询证据。
3. 高德缓存键忽略住宿日期：任务 2 验证命令仍保留当前任务日期/人数，不能借共享地点缓存绕过条件。
4. 自定义工具执行器没有新关键字：任务 2 验证普通查询仍按原接口调用，仅本地读取传 cache_only。
5. 只改酒店及旧版本记录：任务 3 验证可翻页目标遵循 update_scope，不暴露失效或未授权组件。

## Task 1: 修复分页窗口在工具、批量归并和模型视图中的传递

**Mapping:** OpenSpec 2.1；有界结果规格的连续三页、前后页组合场景。

**Files:**
- Modify: travel_data/tools.py、travel_data/result_views.py、agents/context_projection.py、agents/workflow_queries.py
- Create: tests/test_cached_candidate_pagination.py
- Reuse: tests/test_workflow_queries.py 中 train_result、tests/workflow_support.py、travel_data/batch_execution.py

**Interfaces:**
- 保留 project_domain_result(value: dict, *, candidate_limit: int = 5, requirements=None) -> dict。
- build_tool_result_view 的 limits 增加内部 already_paged: bool，默认 false；true 时 items 从 0 切片，输出偏移取真实窗口，has_more 用真实偏移加实际返回条数计算。
- query_views 保持原签名，返回实际使用的 view_offset；ToolExecutor 的 query_records 快照必须包含更新后的偏移。
- 新测试辅助 seed_cached_train(count: int = 12) -> tuple[dict, RunState, ToolExecutor]：使用 workflow_support.workflow 和 train_result 构造有效记录及计数型离线供应商，不连网络。

- [x] **Step 1: 写失败测试** `test_batch_pagination_keeps_real_window_and_query_identity`：通过真实 ToolExecutor 和 execute_query_batch 连续读取 12 条候选的三页；断言如下，并保存首个候选供最终 resolve_selection 验证：

```python
assert [len(page['items']) for page in pages] == [5, 5, 2]
assert [page['view_offset'] for page in pages] == [0, 5, 10]
assert [page['has_more'] for page in pages] == [True, True, False]
assert len({page['query_id'] for page in pages}) == 1
assert run.external_request_count == 1
assert len(saved_query['items']) == 12
```

另写 `test_model_projection_preserves_page_without_slicing_twice` 和 `test_previous_page_selection_remains_valid`；投影不修改原对象、不嵌入完整缓存、来源/结果版本不变。

- [x] **Step 2: 运行红灯** `.venv/Scripts/python.exe -m pytest -q tests/test_cached_candidate_pagination.py`。预期因批量合并偏移丢失或模型投影 view_offset=0 失败，不接受导入或夹具错误作为业务红灯。
- [x] **Step 3: 最小实现**：写入 view_offset 后再复制 query_records；切片层显式区分已分页输入，工具 candidate_view 的实际偏移也进入结果；修正 query_views 显式 offset 覆盖的元数据。不改排序、候选内容和最终引用逻辑。
- [x] **Step 4: 运行绿灯** `.venv/Scripts/python.exe -m pytest -q tests/test_cached_candidate_pagination.py tests/test_workflow_queries.py tests/test_bounded_tool_results.py tests/test_information_tools.py tests/test_amap_hotel_flow.py`。全部通过才完成 OpenSpec 2.1。
- [x] **Step 5: 记录** 在本计划记录命令/结果并更新 tasks.md；核查差异后按执行环境允许的分支规则保存本任务变更，不夹带原有修改。

## Task 2: 可证明的本地翻页请求与外部预算隔离

**Mapping:** OpenSpec 2.2、3.1 的请求校验部分；缓存预算、伪装翻页、无进展场景。

**Files:**
- Create: agents/cached_pagination.py
- Modify: travel_data/tools.py、travel_data/batch_execution.py
- Test: tests/test_cached_candidate_pagination.py、tests/test_direct_query_batch.py

**Interfaces:**
- cached_page_options(workflow: dict, candidates: CandidateStore, *, limit: int) -> list[dict]：从当前有效记录及视图生成可读后页，元素含 query_id、result_revision、task_id、task_revision、domain、parameters（含下一 candidate_offset）；只含有效、授权且确有后页的目标。
- validate_cached_page_batch(batch: dict, workflow: dict, candidates: CandidateStore, *, limit: int) -> dict：匹配上述选项，验证完整参数、约束和可用缓存；拒绝重复目标、刷新、非下一页及不明查询；返回批量副本，请求带内部 cache_only=true。
- ToolExecutor.execute(..., *, call_id, _date_retry=False, cache_only=False) 保持原参数兼容，内部 _execute 接收相同开关。该开关不进入模型工具 schema。
- execute_query_batch 对 cache_only 请求预留外部预算 0，转发开关；普通请求不传新关键字，保留原外部预算与伪执行器兼容。

- [x] **Step 1: 写失败测试**：`test_cached_page_runs_with_zero_external_budget` 设置 run.workflow_tool_budget=0、外部 task 请求达到上限仍可读有效后页，读取前后请求数和预算均不变。`test_invalid_cached_batch_is_atomic` 参数化覆盖刷新、新日期/人数/关键词/约束、旧任务版本、重复目标、负数/布尔/重复/越界偏移、缺缓存及失败缓存：拒绝且工作流、缓存和供应商调用均不变。
- [x] 补充 `test_cache_disappears_before_read_never_calls_provider`：先验证后移除缓存，工具明确失败，原完整查询记录不被空错误覆盖；以及 `test_hotel_shared_cache_does_not_authorize_changed_stay`、`test_normal_batch_executor_signature_remains_compatible`。
- [x] **Step 2: 运行红灯** `.venv/Scripts/python.exe -m pytest -q tests/test_cached_candidate_pagination.py tests/test_direct_query_batch.py`。先确认有业务断言失败：预算为零不能读取、非法读取触发供应商或缺少受控验证。
- [x] **Step 3: 最小实现**：新增模块只负责生成/验证后页命令，沿用现有规范化查询和金额比较语义；有效性含 task_revision、needs_revalidation、查询状态。工具读取边界禁止 cache miss fetch 和 refresh，不产生空错误覆盖有效证据；批量归并维持来源、版本、窗口，实际 external_request_count 始终为 0。
- [x] **Step 4: 运行绿灯** `.venv/Scripts/python.exe -m pytest -q tests/test_cached_candidate_pagination.py tests/test_direct_query_batch.py tests/test_information_tools.py tests/test_workflow_queries.py`。新增和原外部请求预算/并发/超时测试全部通过后完成 OpenSpec 2.2。
- [x] **Step 5: 记录** 更新计划证据与 tasks.md，保存本任务范围变更。

## Task 3: 主决策开放本地翻页及集成验收

**Mapping:** OpenSpec 3.1、3.2、4.1；两个 delta 的全部场景。

**Files:**
- Modify: agents/workflow_runner.py、agents/travel_prompts.py、agents/context_projection.py（仅需要保留新范围字段时）
- Test: tests/test_cached_candidate_pagination.py、tests/test_stage_specific_prompts.py、tests/test_live_model_contracts.py、tests/test_consolidated_travel_run.py
- Update: README.md 的候选/查询说明（定位既有说明，小范围补充）；OpenSpec tasks.md

**Interfaces:**
- _batch_context 增加 cached_page_options（任务 2 生成的命令）及 query_scope='cached_pages_only'，仅在原 commit-only 状态且有后页时开放 commit_batch/schedule_queries。其他受控修复状态保留原语义，无后页保持 commit-only。
- _execute_queries(self, queries, *, cache_only=False)：受限路径在工作流副本调用 bind_query_plan，再验证整批缓存命令；普通首次/合法修复不变。
- 主循环在 cached_pages_only 下拒绝其他动作和非法命令并返回结构化 feedback，不能通过 dispatch 绕开限制；使用既有 no_progress 和决策上限。
- selection_prompt(context) 在本地范围说明自主按需翻页、找到组合即可提交、不可改参数或 refresh。工具 schema 继续使用 parameters.candidate_offset。

- [x] **Step 1: 写失败集成测试** `test_main_reads_cached_page_before_commit`：复用 BatchMain/OfflineProvider 测试结构，主 Agent 首次选后页、下一次提交（不插入失败草稿），通过生产 ExecutionHarness 入口验证选择来自第 6～10 条，query_id/版本不变，火车供应商仍 2 次、酒店仍 1 次，最终 status=completed 且 validation 有效。
- [x] 同时覆盖 `test_no_more_pages_keeps_commit_only`、`test_first_page_commit_adds_no_model_call`、`test_hotel_update_cannot_page_train`、`test_stale_results_not_offered_for_paging`、`test_repeated_page_action_stops_without_refetch`、`test_pagination_stops_at_decision_budget`。预算耗尽且仍有未读后页时，答复说明范围，不断言客观无解。
- [x] 提示测试断言本地 scope 保留真实 schema/分页选项；更新旧 test_runner_rejects_unnecessary_refresh_without_supplier_call 的预期（有后页时动作范围改变但 refresh 必须继续拒绝），保留无后页 commit_required 的单独夹具。不得删除原无效刷新回归。
- [x] **Step 2: 运行红灯** `.venv/Scripts/python.exe -m pytest -q tests/test_cached_candidate_pagination.py tests/test_stage_specific_prompts.py tests/test_live_model_contracts.py`。预期主循环拦截翻页、缺正确范围提示或新集成断言失败。
- [x] **Step 3: 最小实现** 使用任务 2 接口接入主循环，限制验证在供应商执行前完成；工具错误反馈保留原数据；正常首批直接提交保持两次主调用。不更改既有报价缺口/硬日期冲突处理。
- [x] **Step 4: 运行绿灯** `.venv/Scripts/python.exe -m pytest -q tests/test_cached_candidate_pagination.py tests/test_stage_specific_prompts.py tests/test_live_model_contracts.py tests/test_consolidated_travel_run.py tests/test_request_orchestration.py tests/test_workflow_batch_commit.py`。全部通过，更新 OpenSpec 3.1、3.2、4.1。
- [x] **Step 5: 记录** 更新计划证据与用户文档，保存本任务范围变更。

## Task 4: 完成审核、规格核对及归档收尾

**Mapping:** OpenSpec 4.2、4.3。此任务依赖全部实现完成，不把文档 ready 当作代码完成。

- [x] 运行 `.venv/Scripts/python.exe -m pytest -q tests`；读取全部结果。若有失败，分清本变更回归、环境问题或既有失败，重要失败处理完之前不归档或宣称完成。
- [x] 运行 `git diff --check`，审查本次差异与新文件。单代理执行时当前代理整体自审；多子代理执行时每项任务进行规格与质量独立评审并修复复审。检查完整证据、外部请求为零、固定参数、范围隔离和无进展停止。
- [x] 按两个 delta 的每个场景逐项关联测试结果，记录到 docs/superpowers/verification/2026-10-14-cached-pagination.md；任务完成必须有对应实现与测试证据。
- [x] 检查是否配置 OpenSpec verify 工作流；若无（当前 CLI 帮助未列出 verify，项目未发现对应工作流），明确记录人工规格核对，不能把 validate 说成 verify。执行 `openspec validate enable-main-agent-cached-pagination --strict`，预期 valid。
- [x] 全部通过后完成 OpenSpec 4.2；再运行 `openspec archive enable-main-agent-cached-pagination --yes`，核对同步后的两份规格、归档路径及工作分支中保存的实际差异，才完成 4.3 的归档部分。
- [x] 使用 finishing-a-development-branch，呈现合并、PR 或保留分支的适用选项并执行用户选择；不自动处理其他已有工作。

## Execution Record

- 2026-10-14：用户“开始修改”确认范围与设计；完成技术复核、详细计划和计划自审。尚未修改产品代码、未运行实现测试。执行方式待用户选择，OpenSpec 1.2 保持未完成。
- 自审覆盖：分页真实偏移、跨批量归并、前页引用、外部预算为零、缺缓存不回源、非法命令原子拒绝、用户修改范围、无后页及预算停止、正常路径模型调用数，均有任务归属。

2026-10-14：用户选择单代理。Task 1 红灯 3 项→相关绿灯 44 项；Task 2 红灯（预算门槛与缺 cache_only）→相关绿灯 44 项。变更保留工作区差异以免夹带原有修改。


2026-10-14：Task 3/4 完成。最终全量 890 passed, 3 skipped；整体自审与人工规格核对完成，未配置 OpenSpec verify；严格校验 valid。已执行 archive，同步 1 项新增和 1 项修改到正式规格，归档目录 2026-10-14-enable-main-agent-cached-pagination。分支集成选项已准备，等待用户选择；代码保留工作区，未提交推送或合并。

