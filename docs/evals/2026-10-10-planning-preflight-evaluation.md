# 规划补全与真实模型耗时测评（2026-10-10）

本报告保留默认关闭推理之前的历史记录。当前运行已按用户要求默认关闭推理，最新结果见[关闭推理默认配置测评](2026-10-10-thinking-off-default-evaluation.md)。

模型为当前配置的 deepseek-flash；主流程使用供应商默认思考模式。火车、酒店使用显式注入的 simulation Provider，车次、价格、余票、酒店名称和地址均为测试数据。生产入口没有模拟回退。

最终默认配置测评：11/11；P01–P33可执行测试证据：33/33。

完整自动化回归：422项通过，3项旧在线脚本跳过；原有DashScope弃用告警1条。跳过的在线脚本不计入通过数。

部分方案不等于失败：缺出发地或未确定住宿时长时，验收要求已有候选和部分草稿、说明缺口、不强制追问；不能为了通过测评编造住宿天数。默认日期已核验，酒店价格和库存仍未知。
偏好测评按既有存储契约接受字符串或列表；本次实际保存“全季”，实际酒店查询也使用“全季”。原断言仅接受列表而误报失败，修正后保留原判断与原响应。

| 用例 | 结果 | 总耗时秒 | 模型调用 | 初始判断秒 | 循环判断秒 | 信息获取模型秒 | 模拟工具毫秒 |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 缺日期、重庆→上海 | partial | 55.8 | 8 | 7.8 | 43.7 | 4.0 | 3.20 |
| 缺出发地、去上海 | partial | 35.3 | 7 | 1.6 | 29.3 | 4.2 | 0.96 |
| 指定抵达日、跨夜列车 | partial | 104.5 | 8 | 14.2 | 85.5 | 4.5 | 3.17 |
| 只查火车价格 | ok | 4.6 | 3 | 1.9 | 0.0 | 2.6 | 1.06 |
| 更新偏好并规划 | partial | 47.6 | 8 | 7.7 | 31.2 | 5.1 | 3.20 |
| 上海→北京→杭州→上海 | completed | 85.0 | 17 | 13.5 | 53.6 | 17.2 | 10.08 |
| 换上海酒店，保留火车 | completed | 69.2 | 7 | 7.0 | 56.6 | 5.4 | 0.86 |
| 解释已有酒店 | ok | 5.6 | 1 | 5.6 | 0.0 | 0.0 | 0.00 |
| 不满意、询问修改范围 | needs_input | 5.0 | 1 | 5.0 | 0.0 | 0.0 | 0.00 |
| 暂停并保存 | paused | 2.2 | 1 | 2.2 | 0.0 | 0.0 | 0.00 |
| 补出发地、沿用日期 | partial | 82.4 | 7 | 8.6 | 68.7 | 5.0 | 2.59 |

本批串行测评中模型累计耗时占端到端耗时 99.6%，其中主Agent循环判断占模型累计耗时 74.5%。模拟工具为毫秒级，不能推断真实接口的网络延迟。

瓶颈主要是多轮主Agent调用，以及默认高强度思考下的长输出。DeepSeek官方说明默认开启思考、默认high，输出token可能包含思考内容：[思考模式文档](https://api-docs.deepseek.com/guides/thinking_mode/)。首响应块时间不是严格首token时间，也不是完整JSON交付时间。

主Agent并非只调一次：意图识别后，每段通常还要分派、草稿、校验，再结束；信息获取通常是工具选择和结果小结两次模型调用。各工具可并行，工具服务耗时之和不能直接当作总等待时间。原始模型调用日志保留input/output tokens、latency_ms、ttft_ms及task身份，CSV列出汇总。

格式修复、上下文与路由修正：一致的重复路线元数据在程序边界投影，矛盾仍拒绝；未知来源别名仅在未知值时归一化；上下文去除重复候选全文；截断初始JSON最多修复一次，DeepSeek修复调用关闭思考，正常调用仍使用原配置；明确travel_update必须进入执行流程，避免只口头暂停或继续索要个人字段。

第一轮实测曾出现结构错误和截断，并保存在 `data/evals/2026-10-10-preflight-simulated/` 与 `data/evals/2026-10-10-preflight-final/`。换酒店、暂停、补出发地的修复后结果在 `data/evals/2026-10-10-preflight-feedback-retest/`。其余用例保留原响应，由最终规格逐项重新核对；没有重写原始结果。

关闭思考模式的两条对照（仅测评开关，未修改生产默认）：
- 缺日期、重庆→上海：默认 55.8s，关闭思考 15.1s，partial，验收通过。
- 上海→北京→杭州→上海：默认 85.0s，关闭思考 41.0s，partial，验收未通过。

复杂三段对照实际已生成全部草稿并通过程序全程校验（validation.valid=true），酒店价格未知为非阻塞提示。主Agent仍重复校验，并因建议日期未确认、酒店无价格库存、模拟来源而选择finish partial；验收要求本场景completed，因此未通过的是完成状态判断，不能描述为未完成校验。模型共调用23次（默认17次）。本次单次对照不足以证明关闭思考必然降低规划质量，不把该实验算入默认配置验收通过率；生产默认未改。这些都是单次观测，不是稳定的性能承诺。

自审与验证：按用户选择由当前代理整体自审。修复了局部换酒店的其他组件保护、出发地补充保留酒店、下游推导日期重检、初始阶段整轮超时、近期旅行排序及同ID历史计数。RAG调用链未修改。项目未初始化OpenSpec，本次未运行verify/archive，采用下表人工规格核对及实际测试证据。

| 场景 | 测试证据 | 结果 |
| --- | --- | --- |
| P01 | test_default_week_later_is_not_confirmed_and_does_not_drift | 通过 |
| P02 | test_missing_origin_still_answers_and_queries_independent_later_leg | 通过 |
| P03 | test_arrival_search_finds_previous_day_departure_and_preserves_evidence | 通过 |
| P04 | test_ambiguous_day_has_explicit_nearest_future_proposal_and_keeps_original | 通过 |
| P05 | test_default_date_can_try_next_two_days_without_changing_user_date | 通过 |
| P06 | test_unavailable_provider_is_not_repeated_for_other_dates | 通过 |
| P07 | test_duration_adapter_calculates_cross_year_and_more_than_24_hours、test_inconsistent_duration_does_not_create_false_arrival_date | 通过 |
| P08 | test_price_only_request_queries_train_without_a_travel_workflow | 通过 |
| P09 | test_default_week_later_is_not_confirmed_and_does_not_drift | 通过 |
| P10 | test_preference_runs_once_before_first_workflow_query | 通过 |
| P11 | test_resume_second_task_preserves_first_and_reuses_unchanged_places | 通过 |
| P12 | test_resume_second_task_preserves_first_and_reuses_unchanged_places | 通过 |
| P13 | test_arrival_requirement_is_preserved_without_default_departure、test_global_fixed_start_date_cannot_be_silently_changed | 通过 |
| P14 | test_arrival_search_obeys_external_request_budget、test_tool_budget_is_cumulative_across_tasks | 通过 |
| P15 | test_hotel_place_cannot_verify_hard_budget | 通过 |
| P16 | test_route_skeleton_allows_unknown_origin_but_preserves_user_purpose | 通过 |
| P17 | test_three_and_five_tasks_have_drafts_then_global_validation[None]、test_three_and_five_tasks_have_drafts_then_global_validation[cities1] | 通过 |
| P18 | test_return_with_explicit_hotel_requirement_is_allowed、test_explicit_return_hotel_is_queried_and_validated | 通过 |
| P19 | test_three_and_five_tasks_have_drafts_then_global_validation[None]、test_three_and_five_tasks_have_drafts_then_global_validation[cities1] | 通过 |
| P20 | test_real_runtime_measures_actual_main_and_info_calls[False]、test_real_runtime_measures_actual_main_and_info_calls[True] | 通过 |
| P21 | test_downstream_derived_date_rechecks_new_boundary_but_preserves_user_date、test_second_leg_uses_reliable_previous_boundary_instead_of_week_default | 通过 |
| P22 | test_selection_requires_matching_task_query_revision_and_id、test_model_cannot_finish_without_check_or_spin_forever | 通过 |
| P23 | test_regenerate_task_preserves_requirements_and_only_rechecks_downstream | 通过 |
| P24 | test_completed_trip_hotel_replacement_new_session_preserves_train、test_only_hotel_replacement_preserves_train_and_other_tasks | 通过 |
| P25 | test_arrival_supplement_releases_only_proposed_departure_date、test_party_update_reuses_place_facts_not_old_train_pricing | 通过 |
| P26 | test_ambiguous_feedback_saves_checkpoint_without_replanning | 通过 |
| P27 | test_explain_saved_trip_does_not_replan_or_query | 通过 |
| P28 | test_new_trip_has_new_identity_and_preserves_previous_trip | 通过 |
| P29 | test_missing_origin_still_answers_and_queries_independent_later_leg | 通过 |
| P30 | test_partial_and_completed_are_known_across_sessions_and_summary_repairs | 通过 |
| P31 | test_completed_trip_hotel_replacement_new_session_preserves_train | 通过 |
| P32 | test_legacy_projection_is_idempotent_and_preserves_json、test_replanning_same_trip_does_not_duplicate_history_or_statistics | 通过 |
| P33 | test_partial_and_completed_are_known_across_sessions_and_summary_repairs、test_projection_failure_does_not_fail_canonical_save | 通过 |

外部验证限制：本次没有把失效的火车服务修成可用，也未声称验证真实票价、余票或酒店报价。区间历时使用可控接口payload验证跨夜、跨年与超过24小时；换供应商后仍需真实样本核对中途站语义。酒店只推荐地点，不估价或订房。

原始证据：`data/evals/preflight-consolidated.json`、`data/evals/preflight-latency.csv`、`data/evals/preflight-final-tests.log` 与 `data/evals/preflight-final-tests.xml`。
