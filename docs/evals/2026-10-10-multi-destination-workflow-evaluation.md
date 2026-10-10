# 多目的地火车酒店工作流实施与验证

日期：2026-10-10，北京时间。执行方式：用户确认的当前分支单代理实施、整体自审。状态：实现、回归与规格人工核对完成；线上完整行程成功场景受供应商能力限制，未覆盖。

依据：[正式设计](../superpowers/specs/2026-10-09-multi-destination-train-hotel-workflow-design.md)、[已批准实施计划](../superpowers/plans/2026-10-10-multi-destination-train-hotel-workflow.md)。仓库未初始化 OpenSpec，本次采用逐条人工规格核对，没有运行 OpenSpec verify/archive。

## 已实现的链路

`Main.initialize → 前置偏好/记忆/制度（仅按需一次）→ 完整任务列表 → Main.step → Info 工具循环 → 保存查询事实 → Main 草稿 → 单段检查 → 下一任务 → 全程检查 → 最终输出`。

- 任务和消息有稳定 ID、版本及关联字段。单段 `draft` 不表示成功，全部当前版本通过确定性全程校验才成为 `validated/completed`。
- 火车、酒店结果按任务和查询 ID 保存，支持成功刷新后的结果版本；完整候选池持久化，每次模型视图默认 5 个，翻页复用本地资料。
- 重大冲突保存询问断点后返回；用户修改哪一段，就从该段恢复并重检下游。修改第二段保留第一段；默认人数变更重新核实相关费用，酒店地点缓存可复用。
- 快照位于 `data/memory/<user>/workflows/<workflow_id>.json`，原子写入加跨进程锁和版本比较。日志用于审计，恢复使用快照，不重放已完成付费查询。
- 价格和库存证据默认 3600 秒后需要重新核实，可配置；酒店地点不受报价有效期规则影响。失效事实不计入当前已核实预算。
- 用户确认日期与模型建议日期分开；首段显式改期同步全程开始日期，住宿晚数必须匹配要求。未知抵达日期不靠时钟推断，未知酒店费用不当成零。
- 主循环、信息获取及工具调用均有预算、超时和无进展停止条件。非法动作有一次有限反馈机会；初始结构提案可在工具执行前修正一次，最终仍由程序校验。
- CLI 与保存结果使用同一份校验结果。旧单项查询仍走原路径；本次不改变 RAG 实现，不增加活动安排或 JSON 模型 API 模式。

## 离线验证

最终代码全量命令：`..\..\.venv\Scripts\python.exe -m pytest -q -rs`。

结果：**355 passed，3 skipped，1 warning**。基线为 277 passed、3 skipped。跳过项是原有手动联网信息查询、LLM 记忆和 RAG 脚本；警告为既有 DashScope Assistants 弃用提示。

验收命令：`python -m evals.main_agent.multi_destination_runner --offline --output data/evals/2026-10-10-workflow-offline-final.json`。执行 81 项相关测试，S01–S22 均有实际执行且通过的测试证据。完整报告、JUnit XML 与输出分别保存为同名 `.json/.xml/.log`。

三段和五段的完整 `completed` 正向路径使用可控模型和带可靠抵达时间证据的离线 Provider。它证明编排与校验可以完成，不代表线上接口已提供相同证据。

| Scenario | Executed tests | Result |
| --- | --- | --- |
| S01 | `test_three_and_five_tasks_have_drafts_then_global_validation[None]`, `test_three_and_five_tasks_have_drafts_then_global_validation[cities1]` | PASS |
| S02 | `test_three_and_five_tasks_have_drafts_then_global_validation[None]`, `test_three_and_five_tasks_have_drafts_then_global_validation[cities1]` | PASS |
| S03 | `test_three_and_five_tasks_have_drafts_then_global_validation[None]`, `test_three_and_five_tasks_have_drafts_then_global_validation[cities1]`, `test_model_cannot_finish_without_check_or_spin_forever` | PASS |
| S04 | `test_queries_do_not_overwrite_the_route_and_five_is_only_a_view` | PASS |
| S05 | `test_verified_cross_day_cannot_check_in_before_arrival`, `test_unknown_arrival_day_cannot_be_invented_in_draft` | PASS |
| S06 | `test_later_departure_cannot_precede_previous_stay`, `test_unknown_arrival_pauses_with_persisted_checkpoint` | PASS |
| S07 | `test_global_party_change_is_confirmed_but_default_was_not`, `test_party_update_reuses_place_facts_not_old_train_pricing` | PASS |
| S08 | `test_resume_second_task_preserves_first_and_reuses_unchanged_places` | PASS |
| S09 | `test_restart_preserves_candidates_and_checkpoint`, `test_resume_second_task_preserves_first_and_reuses_unchanged_places` | PASS |
| S10 | `test_return_empty_success_is_distinct_from_unavailable` | PASS |
| S11 | `test_return_unavailable_is_not_claimed_as_no_trains`, `test_unavailable_provider_cannot_be_refreshed_repeatedly` | PASS |
| S12 | `test_hotel_place_cannot_verify_hard_budget`, `test_decimal_budget_includes_every_train_and_hotel` | PASS |
| S13 | `test_revision_change_invalidates_global_check`, `test_cli_displays_the_same_validated_route_without_old_guard` | PASS |
| S14 | `test_invalid_action_cannot_start_provider`, `test_selection_requires_matching_task_query_revision_and_id` | PASS |
| S15 | `test_tool_budget_is_cumulative_across_tasks`, `test_model_cannot_finish_without_check_or_spin_forever` | PASS |
| S16 | `test_workflow_info_only_executes_train_hotel_and_does_not_mutate_confirmed`, `test_step_has_full_task_overview_and_no_activities_guide` | PASS |
| S17 | `test_preference_runs_once_before_first_workflow_query`, `test_real_runtime_measures_actual_main_and_info_calls[False]`, `test_real_runtime_measures_actual_main_and_info_calls[True]` | PASS |
| S18 | `test_defaults_are_not_confirmed_and_three_tasks_have_stable_dependencies` | PASS |
| S19 | `test_conditions_merge_without_overwriting_input_or_forcing_brand` | PASS |
| S20 | `test_first_departure_can_inherit_explicit_start_date`, `test_global_fixed_start_date_cannot_be_silently_changed` | PASS |
| S21 | `test_missing_date_can_still_query_hotel_places` | PASS |
| S22 | `test_party_update_reuses_place_facts_not_old_train_pricing` | PASS |

## 真实模型与接口

使用产品 CLI 初始化和实际 Harness、信息获取、Juhe 与高德。没有替换为模拟报价，没有更改模型或推理参数。测试账号与记忆目录隔离，日期按当日北京时间生成。

指标分别记录：契约/边界检查、外部查询是否取得带来源候选、整趟规划是否完成。`partial/needs_input/unavailable` 的边界处理通过，不等于对应业务正向场景完成。

| Case | Status / Stop reason | Contract checks | LLM / Tools / External attempts | Query evidence | Planning complete |
| --- | --- | --- | --- | --- | --- |
| three_return | `partial` / `model_partial` | True | 7 / 2 / 2 | True | False |
| five_return | `partial` / `model_partial` | True | 6 / 2 / 2 | True | False |
| resume_second | `partial` / `model_partial` | True | 6 / 2 / 2 | True | False |
| hotel_budget | `partial` / `model_partial` | True | 9 / 3 / 2 | True | False |
| train_single | `unavailable` / `None` | True | 4 / 1 / 1 | False | False |
| hotel_single | `ok` / `None` | True | 4 / 1 / 1 | True | False |

## 自审发现与处理

上表按案例保留最近验证结果：三段、五段、修改第二段及单项火车来自 `workflow-live-20261010-113737`；预算与单项酒店来自前一轮 `workflow-live-20261010-112737`。前一轮五段的动作类型失败保留在原报告，未改写；修复后的五段复测通过边界检查。

最终复测的 Juhe 火车返回 `unavailable`，酒店查询成功。Query evidence 为 true 在这些案例中表示取得了带来源的酒店地点，**不表示火车查询成功**。此前 `workflow-live-20261010-112737` 的三段首段实际取得 230 个火车席别候选，五段首段取得 77 个；但可靠抵达日期仍未知，完整行程未能核实。单项火车在最终两轮均不可用，成功查询的线上回归正向场景未覆盖；没有推断不可用的供应商具体原因。

恢复案例保持同一工作流 ID，第一段保留 revision=1，第二段和返程变为 revision=2；查询只针对第二段，未重查第一段。六例契约与诚实失败边界检查均通过，线上完整多段 completed 数为 **0**。

1. 真实初始提案曾耗尽输出预算、使用旧 itinerary 标签、重复路线元数据或把 constraints 写成文字数组。精简提案提示；已有工作流载荷转入工作流路径；冗余元数据先核对后标准化；无效结构最多一次修正，不能绕过硬约束。
2. 循环动作缺少顶层 action，询问建议误用字典。提示中补齐五种动作完整 JSON 示例和字段类型；接口仍严格拒绝非法动作，不靠自由文本猜执行意图。
3. 高德“北京市”与任务“北京”比较误拒绝。同城允许末尾“市”的名称差异，异地候选仍拒绝，并增加正反测试。
4. 修订后模型决策可能基于旧快照；增加读取时版本与决策所见版本比较，旧提交不执行工具。跨进程 CAS、保存失败不重放均有回归。
5. 本地候选翻页窗口曾未传到主模型；保留实际 offset。核对主模型和信息获取结果视图最多 5 个，持久化完整池不截断。
6. 恢复旧价格和库存时标记证据过期，刷新失败仍保留旧事实供审计，但不作为新的费用/库存保证。预算与 CLI 均按同一标记处理。
7. 补测建议住宿日期可调整、确认日期保护、住宿晚数、首段显式改期、明确目的地修改、人数变更、恢复与来源隔离。
8. 测评器分别报告查询成功和全程完成；用户询问暂停不会自动冒充查询成功，但实际已查到的候选也不会因暂停被抹掉。

审查范围包含所有当前变更文件及 S01–S22 对应场景。RAG 文件和外部 RAG 项目未修改；模型配置中的模型名、输出预算、温度未改变。`git diff --check` 未发现补丁格式错误。

## 当前能力边界

Juhe 当前适配提供查询日期和发车时刻，但没有可靠的完整抵达日期证据，因此不能保证跨日住宿和下一段衔接。高德只提供酒店地点，不提供房价、空房或入住规则；总预算或报价/空房核实请求只能保留缺口。供应商错误或超时表示未核实，不表示当天没有火车。

模型批准建议或用户同意调整计划不能把缺少来源的抵达日期变成已核实事实。真实完整多段成功场景若未取得这些证据，仍标记未覆盖；不能通过放宽校验把结果改成 completed。

## 复现与记录

- 全量回归：`python -m pytest -q -rs`。
- 离线验收：`python -m evals.main_agent.multi_destination_runner --offline`。
- 实际测评：`python -m evals.main_agent.multi_destination_runner --live --output <报告路径>`；可用多个 `--case <id>` 定向复测。
- 每例保存业务响应 JSON、CLI 文本、模型业务 JSON/工具请求、`events.jsonl`、`runs.jsonl`、任务快照。报告中的 result_path/trace_path 可定位具体记录。
- 前期失败日志保留于 `data/evals/workflow-live-*`，不把后续通过覆盖成“从未失败”。正式交付摘要见同目录的评估 JSON。
