# 非 RAG 架构重构验证记录

对应 [正式设计](../specs/2026-10-09-information-acquisition-agent-design.md) 和 [实施计划](../plans/2026-10-09-main-agent-refactor.md)。用户确认单代理在 `codex/information-acquisition-agent` 当前分支实施及整体自审。

## 最终实现

- 主 Agent 在阶段 0 决策，按需在阶段 3 综合或规划行程。
- 阶段 1 的可选子任务是偏好、记忆和原 RAG；信息获取在阶段 2 读取本轮有效偏好与前序结果。
- 信息获取合并事项整理，以模型原生工具循环调用火车、酒店、攻略、天气和网页工具，并自行总结。
- 完整简单查询由 harness 校验后直返；缺项、组合任务和行程走主 Agent。
- 候选默认最多五个，全量池本轮保留；同参数请求、窗口和跨反馈复用缓存。
- 一次全局定向反馈，不重跑其他子任务。信息获取每次最多 6 次模型和 10 次工具，单工具 30 秒；主 Agent 决策一次、最终阶段最多两次。
- 一个最终 CLI 出口、一条最终助手消息；偏好保存一次，完整行程从 travel_conditions 保存。

## 实施与红绿证据

| 任务 | 实施证据 |
| --- | --- |
| 1 契约/主 Agent/模型响应 | 缺接口 11 项失败 → 11 项通过；当时全量 221 passed、5 skipped |
| 2 工具/候选池 | 缺接口 6 项及参数/记录 5 项失败 → 87 项工具/Provider/预算通过 |
| 3 信息获取循环 | 缺 run 3 项失败 → 25 项聚焦通过 |
| 4 harness/直返 | 缺 run_turn 8 项失败 → 22 项聚焦；当时全量 243 passed、5 skipped |
| 5 补查/行程保护 | 4 项反馈/完成性失败 → 34 项通过 |
| 6 注册/入口 | 2 项注册及 2 项 CLI 失败 → 53 项迁移检查及 63 项入口/配置/Provider 检查通过 |
| 7 输出/保存/评估/指南 | 保存 2 项、评估 2 项及指南 1 项失败 → 32 项通过 |
| 8 整体自审 | 前序资料、补查域/窗口、温度、摘要事实、硬条件、日志缓存、日期、酒店区间和异常预算均用失败用例复现后修复；实际主/子模型阶段由端到端测试验证 |

旧角色的在线演示及旧调度断言随接口迁移，因此各阶段用例数量不同；全量结果不能仅凭测试数量比较。保留了有效的价格、库存、预算、来源、地点、异常脱敏和记忆保护场景。

## 25 个验收场景核对

下表中的测试名位于 `tests/`，每行对应正式设计第 18 节同序号场景。

| 场景 | 代码/验证证据 |
| --- | --- |
| 1 五个业务 Agent 角色 | main_agent.py；test_lazy_agent_registry.py；test_main_plan_only_advertises_business_agents |
| 2 单项查询不额外规划 | test_complete_weather_skips_finalize；test_sourced_agents.py；test_information_agent_loop.py |
| 3 行程与独立入口 | test_standalone_plan_uses_main_harness；test_real_runtime_measures_actual_main_and_info_calls 的 itinerary 分支 |
| 4 旧 RAG 协议 | test_rag_legacy_status_is_preserved；test_rag_windows_db.py；ask-question 目录无差异 |
| 5 RAG 不补造来源/成功 | test_rag_windows_db.py；test_eval_missing_metrics_are_unknown_and_rag_miss_is_visible |
| 6 新偏好及一次保存 | test_effective_preferences_precede_info；test_preference_persisted_once_despite_feedback；原偏好提取 Agent 未改 |
| 7 依赖与并发 | test_sourced_routing.py；test_information_receives_completed_policy_results；asyncio.gather 同批执行 |
| 8 两种 query 与硬条件 | test_main_plan_only_advertises_business_agents；query-info 输入保留 original_query；test_feedback_does_not_change_confirmed_query_date |
| 9 缺字段/日期不查询 | test_missing_does_not_query；test_invalid_date_and_guest_count_do_not_query；test_missing_guests_does_not_request_provider |
| 10 流与工具协议 | test_model_io.py；test_multiple_tool_results_reach_same_agent；test_stream_failure_does_not_execute_partial_calls |
| 11 硬预算与其他候选 | test_decimal_budget_filters_before_window；test_feedback_preserves_hard_constraints；test_unknown_filter_does_not_silently_query |
| 12 多/少/空候选及席别 | test_candidate_window_does_not_refetch；test_valid_empty_response_is_ok；test_inventory_is_not_invented；candidate_view 保留全量并按车次去重排序 |
| 13 缓存及无假分页 | test_concurrent_same_query_fetches_once；test_feedback_window_reuses_cache_with_real_agent；test_documented_response_and_post_form |
| 14 不可用/超时/部分/空 | test_unavailable_timeout_and_empty；test_business_status_keeps_successful_sibling；test_weather_date_outside_response_is_incomplete |
| 15 来源与伪造事实保护 | test_sourced_itinerary.py；test_sourced_cli.py；test_itinerary_prose_cannot_introduce_false_money；test_hotel_offer_must_match_queried_stay |
| 16 定向反馈一次 | test_feedback_only_requeries_affected_domain；test_feedback_limit_stops_second_requery；test_feedback_cannot_query_unaffected_domain |
| 17 运行限额 | test_info_model_and_tool_limits；test_ten_tool_limit_returns_paired_overflow_result |
| 18 不重放付费请求 | test_cli_paid_train_is_not_replayed_after_stage_write_failure；CLI 单一 run_turn，无整轮自动重试；模型错误保留 RunState |
| 19 单一输出/历史保存 | test_forward_records_one_final_message；test_completed_itinerary_uses_new_conditions；test_preference_persisted_once_despite_feedback |
| 20 能力隔离/完整指南 | test_registry_rejects_private_and_retired_nodes；test_only_information_receives_tool_executor；test_role_summary_can_exclude_private_skills |
| 21 RAG 冻结 | git diff 2dfb62d -- .claude/skills/ask-question data 为空；配置 AST 比对；本轮未对外部 RAG 项目执行写入 |
| 22 回归与评估 | test_v0_memory_*.py、test_travel_budget.py、test_travel_data_contracts.py、test_rag_windows_db.py、test_main_evals.py |
| 23 天气直返调用与来源 | test_real_runtime_measures_actual_main_and_info_calls 的 forward 分支；test_forward_records_one_final_message |
| 24 不完整不能直返 | test_incomplete_result_requires_finalize；test_pending_answer_requires_synthesis；test_multiple_complete_tool_domains_can_forward |
| 25 必须综合的场景及直返保护 | test_main_feedback.py；端到端 itinerary 分支；test_financial_forwarding_rebuilds_display_from_real_offers；test_sourced_cli.py |

## 技术取舍记录

1. 用户选择当前分支和整体自审；本轮未创建新工作树或审查子代理。Windows 使用 PowerShell 记录代替技能的 Bash 记账脚本。代价：缺少独立审查视角，使用整体自审和回归降低风险。
2. 小型假模型保存在具体测试内，未创建无消费者的全局 conftest。代价：少量测试辅助代码重复。
3. 候选测试集中在 test_information_tools.py，用真实执行器验证缓存与窗口。代价：测试文件未按候选/工具再拆分。
4. 确定性执行逻辑放入 execution_harness.py，OrchestrationAgent 是公开外壳。代价：比文件职责表最初版本多一个模块，公开 run_turn 接口一致。
5. 旧在线演示和旧角色断言替换为新入口离线测试，保留仍有效的保护场景。代价：旧类的手动调用示例退出，本次不验证退休类。
6. 用户选择单代理，因此项目指南通过角色边界测试验证，未派压力测试子代理。代价：未证明真实模型始终遵循提示词，重要规则另由程序限制。
7. 金额、库存、天气和地点相关自由文本采用结构化事实组装，制度/记忆答案保留原结果；行程说明使用固定安全文案。代价：回答措辞较固定，避免再次汇总引入新事实。

整体自审的重要发现已修复；没有因次要改进扩大产品范围。

## 最终检查

2026-10-09 最终检查：**243 passed、3 skipped、1 warning，15.34 秒**。警告为既有 dashscope Assistants API 弃用提示。两组 compileall、工作区及相对基线 diff 检查退出码均为 0；RAG 和知识数据差异为空。整体自审及 25 项规格核对完成，无未解决的重要发现。检查命令：

```powershell
& '..\..\.venv\Scripts\python.exe' -m pytest -q tests
& '..\..\.venv\Scripts\python.exe' -m compileall -q agents context travel_data utils cli.py
& '..\..\.venv\Scripts\python.exe' -m compileall -q .claude/skills/query-info/script .claude/skills/preference/script .claude/skills/memory-query/script .claude/skills/ask-question/script .claude/skills/plan-trip/script/plan_trip_execution.py
git diff --check
git diff 2dfb62d -- .claude/skills/ask-question data
```

另外通过 AST 比较 `LLM_CONFIG`、`RAG_CONFIG`、`SYSTEM_CONFIG`、`RESILIENCE_CONFIG`，原值不变；仅新增独立 `RUN_LIMITS`。没有初始化 OpenSpec，没有运行 OpenSpec verify/archive；本表为人工规格一致性核对。

本轮自动测试未请求真实模型、外网或付费查询接口。3 个跳过项是既有旧版在线信息查询、记忆和 RAG 脚本。实际接口、真实模型工具选择及酒店/攻略来源接入仍需后续联调；不将离线测试声称为线上效果验证。

代码提交保留在用户选择的当前工作分支，未合并或推送。后续可以本地合并、推送创建 PR，或继续保留该分支。
