# 主 Agent 本地缓存翻页验证记录

日期：2026-10-14。执行方式：用户明确选择单代理，当前代理实施并整体自审，未派子代理。

## 实现与验证结论

主 Agent 可在首次提交前通过 schedule_queries 请求当前有效查询的下一本地页。每页仍为 5 条，保留现有过滤与排序。受限路径整批验证后执行内部 cache_only 读取，缺缓存不回源，前页有效引用仍可提交。未读完即达到决策预算时，用户答复说明未浏览范围。

完整测试：`.venv/Scripts/python.exe -m pytest -q tests --tb=short --basetemp=.superpowers/sdd/2026-10-14-enable-main-agent-cached-pagination/tmp-full-final`，退出码 0，**890 passed, 3 skipped in 46.55s**。

针对性最终回归：分页、提示、模型契约、批量提交、请求编排、旧工作流及修复共 **106 passed in 8.59s**。`git diff --check` 未发现空白错误。`openspec validate enable-main-agent-cached-pagination --strict` 返回 valid。

测试为确定性离线测试，覆盖生产入口和实际解析/视图/工具调用路径；未执行真实供应商请求或真实大模型验收，不声称实际模型每次都会主动翻页。

## TDD 证据

- Task 1：新增 3 项回归先失败，分别证明批量归并缺 view_offset、模型投影重置为 0、显式窗口缺真实偏移；修复后相关 44 项通过。
- Task 2：先证明外部预算为零阻止本地读取以及工具没有 cache_only 边界；新增缓存校验及读取保护后相关 44 项通过。
- Task 3：集成测试先证明主决策没有缓存页范围，提示没有按需翻页指导；接入后生产入口的火车/酒店后页选择均通过且供应商请求数不增加。
- 全量首跑发现 5 个旧工作流兼容回归：新动作拦截过宽导致 legacy draft_task/validate_workflow/finish 无法推进；收窄拦截对象后兼容回归通过。
- 自审增加 test_failed_cached_batch_preserves_current_query_container，先失败证明缓存消失后容器引用被清空，再修复失败归并。最终全量通过。

## 规格逐项核对

当前 CLI 未提供 verify 命令，项目未启用对应 verify 工作流；本次执行人工逐项规格核对及 OpenSpec 严格格式校验，**未声称运行 OpenSpec verify**。

| 需求/场景 | 实现或测试证据 |
| --- | --- |
| 12 条候选连续三页 5/5/2、偏移 0/5/10、has_more true/true/false | test_batch_pagination_keeps_real_window_and_query_identity |
| 模型投影不重复切片，不修改完整证据 | test_model_projection_preserves_page_without_slicing_twice、test_query_views_explicit_window_has_correct_offset |
| 翻后页后仍能使用前页候选 | test_main_reads_cached_page_before_commit 两个参数分支、resolve_selection 断言 |
| 外部预算耗尽仍可读缓存 | test_cached_page_runs_with_zero_external_budget，外部请求数保持 1 |
| 主 Agent 未提交失败草稿即可翻页 | test_main_reads_cached_page_before_commit，主调用 3 次、外部供应商仍火车 2 次/酒店 1 次 |
| 高德地点后页可选择 | test_main_pages_hotel_places_without_supplier_call，选中 place-5，无新增供应商请求 |
| 首批已合适不强制翻页 | test_normal_tourism_two_main_calls_and_information_agent_never_loaded，主调用仍为 2 |
| 没后页继续先提交 | test_no_more_pages_keeps_commit_only |
| 刷新、改条件、错版本、重复目标及非法偏移被拒绝 | test_invalid_cached_batch_is_atomic 的 10 个参数分支、test_runner_rejects_unnecessary_refresh_without_supplier_call 的 3 个分支 |
| 酒店共享地点缓存不意味着可以改住宿条件 | test_hotel_shared_cache_does_not_authorize_changed_stay |
| 缓存消失不回源也不破坏旧证据/当前容器 | test_cache_disappears_before_read_never_calls_provider、test_failed_cached_batch_preserves_current_query_container |
| 只改酒店不得翻交通页、旧版本不开放 | test_hotel_update_cannot_page_train_and_stale_results_not_offered |
| 无进展及决策上限停止 | test_repeated_page_action_stops_without_refetch、test_pagination_stops_at_decision_budget |
| 缺字段、无报价不自动刷新；金额与硬条件语义保留 | 原 test_live_model_contracts、test_workflow_repair、test_workflow_runner 及全量回归 |
| 本地分页不变成供应商接口分页 | cache_only 工具边界禁止调用 fetch；AmapHotelProvider 的 page_num=1、page_size=25 未改 |

## 代码自审

按已确认方案检查模块边界、完整数据流、异常、版本、授权范围、缓存键、预算、并发归并及返回渲染。修复缓存读取失败清空容器的重要问题后，未发现本变更范围内未解决的重要问题。未安排独立评审，符合用户单代理选择。

## 实施决定与交付边界

- 当前非主分支为 codex/consolidate-travel-execution，保留原 checkout 实施：工作区已有未提交提示和投影修改，新工作树不会复制这些必要内容。代价是差异需按文件/变更核对，不能自动把所有既有改动一并提交。
- Windows 使用 PowerShell 维护 ledger，代替 Bash 辅助脚本；测试证据及逐任务状态保留。
- 保留旧动作兼容处理，仅拦截越界查询/修复/询问；否则会破坏原有继续规划流程。
- 未浏览范围采用程序状态在统一回复渲染末端附加，防止被 fallback 回复覆盖。
- 实现保持工作区差异，尚未提交、推送或合并。最终分支集成由用户选择，不擅自将其他修改夹带到提交。
