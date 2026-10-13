# 主规划、低频偏好分析与政策前置 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: 单代理使用superpowers:executing-plans；多子代理使用superpowers:subagent-driven-development。由用户选择，未经选择不派子代理。Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 统一主规划、无模型偏好/历史工具、首次3份有效行程总结一次的分析Agent和可增量自然语言记忆、强制政策前置以及独立工具并行，保留多段容器和局部修复。

**Architecture:** WorkflowRunner作为整个请求的唯一程序orchestrator，统一前置资料、主模型调用、任务调度、状态推进与局部修复；ExecutionHarness保留兼容入口并转交。工具层执行隔离并行查询、单工具超时与清洗。主Agent生成schedule和批量选择，Runner调用程序校验、保存与渲染。

**Tech Stack:** Python 3.11、现有模型/RAG接口、asyncio、SQLite、pytest。

**Spec:** `openspec/changes/archive/2026-10-13-consolidate-travel-model-calls/specs/`、`design.md`、`tasks.md`。2026-10-13版本替代此前每轮模型偏好提取、memory_query模型、信息Agent部分保留与串行工具安排，不重新定义正式行为。

## Global Constraints

- Runner覆盖无workflow的新请求、纯记忆工具操作、独立查询、规划/修改及澄清恢复；Harness不得保留第二套initialize/finalize或前置调度。请求总超时、总预算与用量单一控制，工具独立超时不迁移；本次职责整合不新增编排模型。
- 明确偏好更新最高优先，更新/查询/历史工具0模型，规划前重读实际有效值；歧义澄清而非隐藏模型兜底。
- 首次3份唯一有效行程触发历史总结，保存每用户initial_summary_done及证据；完成后不因新增行程再分析，仅明确重分析请求特例调用。内容为条件性自然语言条目，来源标识区分推断与明确表达。
- 政策Agent复用检索后直接理解并提取带来源JSON，正常证据齐全路径一次提取，不先通用回答再转JSON；适用政策强制，未知审批不假定批准，真实补检索/修复调用另计。
- 工具清洗0模型；train/hotel默认每页5个，weather每城市/日期请求去重后1条；description默认128字符、message/名称/地址256字符，有截断标志，id/金额/日期/状态/引用不裁剪；保留城市、temp_c、获取时间及未核实约束。
- information_query代码及设计保留，所有入口不调用不加载；天气迁为工具，未迁移网页/攻略不得回调它。
- priority/depends_on保留；火车酒店天气独立请求并行，依赖未满足不抢跑；请求隔离、额度协调和串行结果归并。
- 三层状态简化为立即修改：TripPlan running/completed，task pending/draft/validated，容器empty/ready/failed；needs_input/等待依赖/在途派生，旧停止意图/错误原因保留。供应商与会话诊断状态不混写业务status。
- 保留schedule/task/容器、draft_stage、版本、缓存分页、恢复、局部修复及程序全程校验；成功空结果ready，参数/时效失效旧引用转历史并回empty。
- 后续修改合并意图/目标定位，先重筛完整有效缓存；改选/安排变化不自动失效查询，外部缺证据或刷新必要才局部查。每次patch自动检查依赖，不能等模型主动触发；已有充分上下文的正常修改目标1次主调用、0外部查询，小结-only目标允许加载后再决策。
- 旅游普通成功路径2次主模型；偏好分析、政策及真实冲突另计。当前仅文档，实施前选择方式并读取apply指导。

## Review Focus

- 自然语言偏好含糊或仅本次条件：Task 1只识别长期操作及保留完整text，操作/修改目标含糊时澄清；不把条件句强行拆成固定枚举，不用模型兜底。
- 重复旅程和政策迫使选择污染偏好：Task 2首次完成标记、样本去重、证据分类与软倾向；第6/10份及重启不自动重分析。
- 政策失败和缺审批不能放宽限制：Task 3校验带来源JSON及版本/适用性、返回冲突/缺口，不宣称合规；不把原文answer和citations重复送主模型。
- 状态归并误恢复暂停/取消、重复在途请求或误计历史：Task 4兼容门面/停止守卫/执行记录及有效历史测试；缺信息用checkpoint恢复。
- 并发乱序、共享task_id、超额和缓存竞争：Task 5独立请求与统一协调，使用Task 4的派生在途/依赖守卫。
- 清洗压缩丢温度/城市/未核实约束或重复长文本：Task 5保留决策字段及来源并去重限长，不改完整有效缓存。
- 修改误清有效候选或把旧报价改日期：Task 6对新参数/政策检查覆盖、重筛完整缓存、验证真实引用并拒绝虚构事实。
- 容器修复和旧入口偷偷重启静默Agent：Task 7局部重查及调度禁用。

## File Structure

新增`agents/workflow_state.py`（三层状态归一化与派生守卫）、`agents/memory_tools.py`（无模型读写/规则解析）、`agents/preference_analysis.py`（首次/特殊触发与模型分析）、`agents/policy_preflight.py`（复用检索、理解并提取JSON约束）、`agents/workflow_batches.py`（状态与提交）、`agents/workflow_amendments.py`（已有旅程证据覆盖与受控patch）、`travel_data/batch_execution.py`（隔离并行工具）、`travel_data/result_views.py`（无模型领域去重/白名单/限长）。更新contracts、main、Harness、Runner、context_projection、memory接口、注册器、配置与能力文档；各任务创建独立测试文件。RAG底层不搬迁，information_query源码不删除。

### Task 1: 无模型偏好及历史工具

**Mapping:** OpenSpec 2.1；main-agent-preference-updates。
**Files:** 新memory_tools.py、tests/test_memory_tools.py；改workflow_runner.py、execution_harness.py入口适配、contracts.py、必要memory门面。
**Interfaces:** `parse_memory_command(text: str) -> dict | None`识别明确长期操作和原文片段，不枚举内容语义；`apply_preference_updates(memory, updates: list[dict]) -> dict`处理`{op: add|replace|retract, id?, text?}`并返回真实applied/rejected/effective_memories；`read_memory(memory, request: dict) -> dict`返回有效自然语言条目或有界历史，无model参数。替换/撤回要求可定位目标，追加不改其他条目。存储id/text/source/status等必要外壳，复用source=user及删除保护，按来源区分明确/历史记忆；旧固定键数据保持兼容，可生成读取视图但不删除原记录。更新和重读是前置屏障，写入不重置首次总结标记。

- [x] 写失败测试：`test_preference_only_updates_and_reads_without_model`断言model=0；`test_conditional_text_preserved_and_does_not_trigger_analysis`断言“以后出差都尽量高铁”原意保存、立即生效且分析0；`test_append_preserves_other_conditions_and_initialization_flag`断言并存及不重写/重置；`test_targeted_replace_and_retract_preserve_other_memories`断言定位和删除保护；`test_ambiguous_or_temporary_command_does_not_persist`断言操作/目标歧义澄清、临时条件不落长期；`test_update_precedes_planning_and_query`用timeline断言先保存重读；`test_protection_and_failed_save_report_actual_result`、`test_history_read_is_user_scoped`及旧键兼容测试覆盖保护、失败和隔离。
- [x] 运行 `.\.venv\Scripts\python.exe -m pytest tests/test_memory_tools.py -q`确认缺工具能力而失败，再实现最小增量接口及Runner请求入口/记忆前置分支；Harness只转交该分支，其余迁移在Task 7完成，不能新建一份Harness前置逻辑。不启动主模型处理纯读写，不额外做模型语义去重。
- [x] 重跑通过，运行现有`tests/test_main_harness.py`及相关存储测试，记录输出并更新tasks 2.1，提交仅本任务文件。

### Task 2: 首次历史偏好总结与特殊重分析

**Mapping:** OpenSpec 2.2；history-preference-analysis。
**Files:** 新preference_analysis.py、tests/test_preference_analysis.py；改配置、memory保存/读取分析记录接口、lazy_agent_registry.py。
**Interfaces:** `get_analysis_trigger(history: list[dict], state: dict, *, explicit_reanalysis_requested: bool = False, threshold: int = 3) -> str | None`返回bootstrap/user_requested/None，首次状态优先阻止常规重复触发；`select_analysis_samples(history: list[dict]) -> list[dict]`筛选唯一有效样本并构造有界摘要；`PreferenceAnalysisAgent.analyze(context: dict) -> dict`返回memories（text、来源及evidence_trip_ids）/covered_trip_ids；`save_preference_analysis(memory, result: dict, trigger: str) -> dict`增量保存推断与首次完成标记，不覆盖明确偏好/撤回。合法结果保存成功才完成初始化，合法空总结也完成；失败不改完成状态、不在本轮无限重试。明确重分析仅更新历史推断，不启动周期性调度。

- [x] 写失败测试：`test_two_samples_no_call_three_samples_one_bootstrap`断言2不调/3首次调一次；`test_six_ten_samples_and_restart_do_not_reanalyze`断言持久化完成标记阻止重复调用；`test_duplicate_plan_revisions_not_new_samples`断言ID去重；`test_explicit_update_no_analysis_manual_reanalysis_allowed`区分无模型立即保存与明确重分析；`test_natural_language_conditions_and_evidence_preserved`断言可增量text及证据；`test_policy_forced_or_agent_suggested_choice_not_explicit_preference`断言来源分离；`test_inferred_memories_cannot_override_user_retraction_or_policy`断言软倾向及保护；`test_empty_summary_completes_bootstrap_save_failure_does_not`断言空结果完成、失败无虚假完成。
- [x] 运行 `.\.venv\Scripts\python.exe -m pytest tests/test_preference_analysis.py -q`确认新能力失败，再实现首次/明确重分析触发、自然语言条目输出验证及原子保存；主规划在已有调用内理解有效记忆，不增加每轮总结模型。
- [x] 重跑并核对旧数据无迁移破坏、用户隔离与标记恢复，维护tasks 2.2证据及提交。

### Task 3: 政策检索、理解与带来源JSON提取

**Mapping:** OpenSpec 2.3；travel-policy-preflight全部要求。
**Files:** 新`agents/policy_preflight.py`、`tests/test_policy_preflight.py`；改`agents/workflow_runner.py`、`agents/contracts.py`、`agents/context_projection.py`与`.claude/skills/ask-question/script/agent.py`必要检索适配；RAG核心/索引保留。
**Interfaces:** `retrieve_policy_evidence(context: dict) -> dict`复用现有service.search或search_knowledge，仅取片段/检索身份，不调用旧通用答复；`TravelPolicyAgent.query(context: dict) -> dict`在取得证据后一次模型理解与约束提取，返回status/constraints/applicability/references/effective_date/missing_fields/conflicts；`validate_policy_context(result: dict, user_context: dict, evidence: dict) -> dict`验证证据id存在、字段类型/单位/范围、有效期及审批适用性；`project_policy_context(result: dict) -> dict`保留全部相关规则、冲突/缺口与紧凑来源索引，不重复原文。可执行规则映射既有支持的约束与校验器，复杂规则保留待核实状态，不能冒充已自动验证。Runner将有效JSON交主规划，不把政策写成偏好。

- [x] 写失败测试：`test_service_citations_extracted_directly_once_to_json`断言检索一次后直接提取，无通用answer→JSON串联；`test_policy_precedes_business_schedule`断言JSON先于主initialize；`test_tourism_does_not_query_policy`断言0委派；`test_constraint_sources_units_and_applicability_validated`断言捏造引用/错误类型和单位不可用；`test_policy_projection_preserves_rules_without_duplicate_raw_evidence`断言主视图有规则/evidence_ids/定位但不含answer或片段全文；`test_conflicting_policies_and_unsupported_rule_remain_gaps`断言冲突、复杂未支持规则不会静默放行；`test_soft_preference_cannot_exceed_policy_limit`、`test_missing_knowledge_or_approval_not_permission`、`test_existing_rag_backend_still_used`覆盖强制性、审批和复用。
- [x] 运行 `.\.venv\Scripts\python.exe -m pytest tests/test_policy_preflight.py -q`确认因缺少提取/验证契约失败，再实现检索→理解/提取→验证/投影；失败或补检索按真实模型请求计数，不重建索引。
- [x] 重跑定向测试；现有RAG测试需先核对是否依赖外部服务，离线mock验证检索适配，不为此请求生产索引或模型。记录证据并更新tasks 2.3。

### Task 4: 立即简化三层状态与兼容恢复

**Mapping:** OpenSpec 3.6；simplified-trip-state-machine全部要求。
**Files:** 新`agents/workflow_state.py`、`tests/test_simplified_workflow_states.py`；改`agents/workflow_contracts.py`、`agents/workflow_runner.py`、`agents/execution_harness.py`、`agents/travel_updates.py`、`agents/workflow_repair.py`、`agents/workflow_guard.py`、`agents/workflow_queries.py`、`agents/context_projection.py`、`context/workflow_store.py`与实际小结/有效历史统计门面；Task 2分析样本入口复核停止原因。
**Interfaces:** `normalize_workflow_states(workflow: dict) -> dict`纯程序兼容转换三层枚举并保留身份/版本/原因/历史，未知状态报缺口；`derive_dispatchability(workflow: dict, task_id: str, execution_records: list[dict]) -> dict`返回派生缺字段/依赖/停止/在途及是否可执行，不增加持久化业务状态；`invalidate_query_binding(container: dict, reason: str) -> dict`旧引用转history、解除current_query_ref并回empty；完成/恢复/校验通过统一状态门面推进。请求记录保留request_id/条件版本/开始完成信息/结果引用，调用前登记；活动小结和停止守卫使用同一归一化规则，同CAS事务保存快照与小结，正常发现不扫描全部完整行程。provider/会话诊断状态保持独立，不混写业务status。

- [x] 写失败测试：`test_only_two_three_three_business_statuses_written`断言新写仅2/3/3枚举且模型不能设状态；`test_needs_input_derived_checkpoint_resumes_same_pending_task`断言保存问题后结束本轮、补充恢复原身份；`test_inflight_and_unknown_completion_do_not_repeat_request`断言容器empty也不能重复派发、重启不盲重放；`test_invalidation_moves_reference_to_history_and_empty`断言旧证据解除绑定；`test_success_empty_and_place_only_ready_not_validated`断言ready不等于有报价/合规；`test_legacy_states_preserve_reasons_and_stop_guards`覆盖旧顶层/task/容器映射、旧faild及暂停/取消不自动恢复；`test_summary_atomicity_and_cas_after_state_normalization`断言小结快照一致、事务失败/过期版本不覆盖；`test_effective_history_excludes_stopped_failed_simulated_plans`断言有效样本不因新running映射被误计；`test_validated_reselection_and_missing_evidence_transitions`覆盖本地改选与缺证据流转。
- [x] 运行 `.\.venv\Scripts\python.exe -m pytest tests/test_simplified_workflow_states.py -q`确认现有旧状态写入及缺失兼容门面导致预期失败，再实现归一化/派生和必要恢复守卫，逐处替换三层状态写入，不修改供应商失败策略或新增模型分类步骤。
- [x] 重跑新测试及`.\.venv\Scripts\python.exe -m pytest tests/test_travel_updates.py tests/test_workflow_repair.py tests/test_workflow_matching.py tests/test_workflow_queries.py tests/test_sqlite_storage.py -q`；更新旧测试的状态断言但保留澄清/版本/停止/恢复行为覆盖。记录证据更新tasks 3.6，再进入Task 5并行查询集成。

### Task 5: 容器状态与隔离并行工具

**Mapping:** OpenSpec 3.1/3.4；priority-tool-scheduling、bounded-travel-tool-results及查询容器要求。
**Files:** 新`agents/workflow_batches.py`、`travel_data/batch_execution.py`、`travel_data/result_views.py`、`tests/test_direct_query_batch.py`、`tests/test_bounded_tool_results.py`；改`travel_data/tools.py`、`travel_data/candidates.py`、`travel_data/public_query.py`、`agents/context_projection.py`、`agents/contracts.py`与配置，复用provider归一化。
**Interfaces:** `bind_query_plan(query_plan: list[dict], workflow: dict | None, context: dict) -> dict`返回可执行请求及派生缺口/依赖原因（不写容器blocked）；`initialize_query_containers(workflow: dict) -> dict`创建授权train/hotel，天气是可选辅助；`execute_query_batch(batch: dict, run, executor, *, after_tool) -> dict`对独立请求隔离执行，由协调器归并保存；`mark_query_draft(workflow: dict, task_id: str) -> dict`推进query阶段草稿。`build_tool_result_view(domain: str, result: dict, *, limits: dict, requirements=None) -> dict`集中无模型领域白名单、去重与展示文字限长；ToolExecutor返回及context_projection均用此契约，不重写完整候选。完整归一化记录先校验后保存，按领域身份去重而保留不同席别/房型/日期。默认candidate_limit=5，weather每城市/日期请求1条；description 128、message/名称/地址256字符，有截断标志；保留temp_c/城市/日期、来源索引/fetched_at和unverified_constraints。request/task版本归属、额度预留、完整有效缓存和分页均由程序协调。

- [x] 写失败测试：`test_independent_train_hotel_weather_overlap`用asyncio.Event屏障证明三项都已开始才允许结束，不能只比较脆弱耗时；`test_preferences_and_policy_are_dependency_barriers`断言查询不越过前置；`test_unknown_arrival_blocks_dependent_hotel_only`断言独立请求仍执行；`test_out_of_order_results_keep_task_identity`断言任务容器正确；`test_total_budget_and_cache_reserved_once`断言无超额/重复；`test_save_failure_does_not_replay_started_requests`断言无重放；`test_three_container_states_and_query_draft_distinct`断言empty/ready/failed、失效回empty且解除绑定、复用/重查和未选草稿。
- [x] 补充清洗失败测试：`test_current_weather_keeps_city_date_and_temp_c`断言上海22℃不丢；`test_duplicate_weather_and_long_text_are_bounded_without_model`断言7重复→1条、2000字符→128且截断标记/model=0；`test_large_train_hotel_results_preserve_cache_and_paging`断言50条缓存完整、每页5、下一页无供应商重复请求；`test_different_seats_rooms_dates_remain_distinct`断言领域去重不吞合法选择；`test_place_only_keeps_unknown_prices_and_unverified_constraints`断言不虚构合规；`test_projection_keeps_sources_status_and_immutable_facts`断言来源时间、缺口、金额/id不裁剪且不修改原对象；`test_tool_views_not_duplicated_in_main_context`断言候选仅出现一次、无raw/photos/营销长文。
- [x] 运行 `.\.venv\Scripts\python.exe -m pytest tests/test_direct_query_batch.py tests/test_bounded_tool_results.py -q`确认并行缺能力和清洗投影缺口的失败原因，再实现隔离作用域/统一协调与result_views，禁止共享current_task_id直接gather；规则清洗不调用模型。
- [x] 通过后运行`tests/test_information_tools.py tests/test_workflow_queries.py tests/test_compact_context.py tests/test_agent_context_isolation.py`，另运行`tests/test_juhe_train_provider.py tests/test_amap_hotel.py`；记录证据与tasks 3.1/3.4状态。

### Task 6: 已有旅程修改优先复用与直接提交

**Mapping:** OpenSpec 3.5；saved-trip-amendment全部要求，以及既有版本/发现/跨段修复约束。
**Files:** 新`agents/workflow_amendments.py`、`tests/test_saved_trip_amendment.py`；改`agents/main_agent.py`、`agents/contracts.py`、`agents/travel_updates.py`、`agents/workflow_queries.py`、`agents/workflow_runner.py`、`agents/context_projection.py`，复用`agents/workflow_guard.py`和WorkflowStore事务接口。
**Interfaces:** 主initialize合并修改意图与workflow/task/components定位，有资料时可同时给`amendment_patch`；`prepare_saved_trip_amendment(workflow: dict, intent: dict) -> dict`按新条件核对证据覆盖并返回有界任务/候选、可复用政策与实际缺口，完整缓存重筛不请求供应商；`MainAgent.amend_saved_trip(context: dict) -> dict`在定位后必要时生成同一patch契约；`apply_amendment_patch(workflow: dict, patch: dict, context: dict) -> dict`返回candidate_workflow/validation/query_requests/unapplied_patch/gaps。patch固定包含workflow_id/expected_workflow_revision、task_id/expected_task_revision、condition_changes、selection_refs、受控建议安排与reply_template；字段和版本校验后基于真实引用组装方案，重新按新条件重绑定并自动检查依赖。仅验证通过的修改作为已生效保存；缺证据保留未应用提案并将相关query_requests交Task 5执行，不重走整趟首次规划。未修改任务保持身份及有效选择，长期记忆不隐式更新。

- [x] 写失败测试：`test_known_target_candidate_change_one_main_call_zero_provider_calls`断言有充分上下文时一次改选与模板提交，无意图/分类/收尾额外模型；`test_summary_only_locates_then_loads_one_trip`断言小结先定位，仅加载目标、按需一次修改决策，不扫描完整历史；`test_ambiguous_target_does_not_mutate_or_query`断言先澄清；`test_lower_budget_refilters_full_cache_without_query`断言新预算重筛和新约束验证；`test_later_page_candidate_does_not_trigger_provider`断言第6项已有合格项时只用缓存；`test_reselection_keeps_query_ready_and_unrelated_tasks`断言同参数改选保持ready、不清交通；`test_new_stay_dates_without_matching_quote_require_hotel_only`断言只有相关缺口进入工具；`test_model_cannot_rewrite_supplier_time_price_or_inventory`断言外部事实不能直接改；`test_program_checks_new_boundaries_without_model_repair_request`断言实际依赖自动检测；`test_valid_policy_reused_and_explicit_refresh_scoped`断言有效政策0重检索、显式最新查询按范围刷新；`test_stale_patch_or_failed_save_keeps_previous_plan`断言CAS/事务失败不确认成功；`test_trip_only_change_does_not_write_long_term_preferences`断言本次修改不更新记忆。
- [x] 运行 `.\.venv\Scripts\python.exe -m pytest tests/test_saved_trip_amendment.py -q`确认因缺少直接patch/证据覆盖能力失败。实现prepare与apply的纯程序逻辑，再接通initialize/amend_saved_trip和前置目标加载；复用现有query/selection与校验器，不仅重写task_revision来绕过新条件校验。
- [x] 重跑新测试及`.\.venv\Scripts\python.exe -m pytest tests/test_travel_updates.py tests/test_workflow_queries.py tests/test_workflow_matching.py tests/test_workflow_repair.py tests/test_main_feedback.py -q`；记录真实输出并更新tasks 3.5。复杂修复与普通直接改选都保留，任务状态由程序推进。

### Task 7: 统一请求编排、主规划收尾与局部修复

**Mapping:** OpenSpec 3.2/3.3/4.1–4.3；consolidated-travel-execution的单一请求编排要求、合并收尾、恢复与角色边界。
**Files:** 改main、Harness、Runner、workflow_guard/repair/contracts、workflow_reply、注册器、README；新tests/test_workflow_batch_commit.py、tests/test_consolidated_travel_run.py。
**Interfaces:** `MainAgent.select_batch(context: dict) -> dict`返回commit_batch或受控schedule_queries/repair/ask/partial；`check_batch_submission(workflow: dict, action: dict) -> dict`校验引用/版本/政策及全程；`invalidate_query_containers(workflow: dict, task_ids: list[str], components: list[str], condition_changes: dict) -> dict`验证后仅失效关联组件及依赖。reply_facts渲染模板，无效程序兜底；旧draft有draft_plan无stage按selection兼容。

- [x] 扩展Task 1的`WorkflowRunner.run_request(context, run)`为全请求入口：复用Task 1–3的前置资料、主initialize及目标匹配/加载，再协调Task 4–6的状态/批次/修改，最后统一校验保存与输出。无需预先存在workflow；复杂意图在已有主调用内识别。`ExecutionHarness.run_turn(context, run)`保持返回契约但只转交，原_run_turn/_finalize/_final_model业务逻辑迁移或移除；旧Runner.run入口共用内部实现，不串联重复循环。请求级限制与用量合一，单工具超时仍由ToolExecutor控制，缺资料checkpoint后结束本轮。
- [x] 写编排失败测试：`test_harness_delegates_request_once`断言Harness不另调main或Agent；`test_runner_accepts_request_without_workflow`覆盖新规划/独立查询；`test_single_request_limit_and_usage_owner`断言总预算/用量只累计一次且单工具超时继续生效；`test_missing_input_ends_turn_and_resumes_checkpoint`断言running不持续轮询；已有修改测试从统一入口回归，不只测试helper。
- [x] 写失败测试：`test_normal_tourism_two_main_calls`断言无前置额外调用的固定普通场景主调用2次；`test_pure_memory_request_zero_calls`断言0模型；`test_all_information_query_routes_are_dormant`断言不加载/不调用且代码仍在；`test_legacy_memory_model_not_called`断言工具替代；`test_hotel_repair_preserves_transport_and_other_tasks`、`test_policy_conflict_cannot_complete`、`test_bad_template_has_no_rewrite_call`、`test_resume_reuses_valid_queries`覆盖局部修复、政策、模板与恢复。
- [x] 定向失败后接通新路径和恢复，Runner独占编排，Harness不同时执行旧循环；information_query所有路径静默，未迁移领域明确未启用，无隐藏模型降级。工具执行/清洗保留在工具层，Runner通过既有helper串行推进及事务保存，不另写供应商实现。
- [x] 运行两份新测试、现有`tests/test_main_harness.py`入口兼容回归及 `.\.venv\Scripts\python.exe -m pytest -q`、`.\.venv\Scripts\python.exe -m ruff check .`，记录实际输出；按任务证据而非文字报告勾选。
- [x] 按用户选定方式评审，执行启用的verify或明确未启用并人工逐项核对，运行`openspec validate consolidate-travel-model-calls --strict`仅作文档检查；实际usage统计主/分析/政策调用，不预报固定降费比例。
- [x] Task 8/9完成后进入Task 10整体审核，不在本任务提前归档。

## Self-Review / Execution Record

2026-10-13：更新全部正式文档和唯一实施计划。Task 1→2/3→4（状态兼容）→5（工具）→6（修改）→7（集成）依赖明确；Review Focus已映射。已有旅程修改优先证据复用，直接patch与按需查询分流，程序自动检查依赖。新增政策复用检索后理解并提取JSON，工具补齐有界清洗，不重构RAG底层；首次历史总结与特殊重分析替代旧偏好/记忆模型；明确更新保存条件性自然语言，不重新生成整组记忆。当时产品和测试代码未修改；现已按用户选择单代理、当前目录执行，完成证据见文末执行记录。本次文档与链接校验，并检查已有代码与运行80项现状定向测试；三层状态已从延期移入立即修改，新政策/清洗/状态能力仍待TDD实施。

2026-10-13请求编排边界复核：Runner提升为全请求orchestrator，Harness仅兼容转交；Task 1接入记忆入口、Task 3政策前置、Task 6修改协调均归Runner，Task 7完成其余迁移及单一限制/用量与无重复流程验收。具体工具调用和单工具超时保持ToolExecutor，有界清洗与隔离并发按Task 5补齐；没有新增编排模型。以上为实施前记录；现已接通。

### Task 8: 官方SDK与项目模型协议

**Mapping:** OpenSpec 5.1；project-model-runtime；以design第6节为准。
**Files:** 新agents/runtime.py、agents/openai_model.py、tests/test_project_model_runtime.py；改CLI、config_agentscope、utils/llm_resilience、所有旧技能/辅助脚本/测试框架导入及requirements。
**Interfaces:** Msg(name,content,role)、AgentBase(name)保留消息边界；OpenAIChatModel兼容现有constructor和async __call__，内部AsyncOpenAI、complete text/tool snapshot和usage。
- [x] 写流片段工具合并/中断/非法JSON/usage及无框架启动的失败测试，先运行确认缺能力。
- [x] 最小适配及导入迁移；SDK自动重试0、沿用外层重试；不改变RAG检索。
- [x] 运行新测试、model_io/telemetry/CLI/懒加载回归，记录证据并更新5.1。

### Task 9: 已保存旅程只读详情与候选分页

**Mapping:** OpenSpec 5.2；saved-trip-read-only；以design第6节为准。
**Files:** 新agents/workflow_reads.py、tests/test_saved_trip_read_only.py；改Runner、main/contracts、投影/CLI展示，复用WorkflowStore查询。
**Interfaces:** read_saved_trip(workflow, request) 与 render_saved_trip(result)；Runner _read_request 先经用户归属与版本校验加载，再调用纯读取函数。request 含 task_id/domain/offset/limit/include_history/all，不保存 workflow。
- [x] 写用户隔离/版本/12条分页/历史标识/状态不变和统一入口无外查的失败测试。
- [x] 实现权威读取及有界视图，all分批显示，普通主上下文不全量展开；缺数据如实返回。
- [x] 运行新测试及小结/WorkflowStore回归，记录并更新5.2。

### Task 10: 全范围审核、测试与归档

**Mapping:** OpenSpec 4.1–4.3；前述全部规格/设计/任务。
**Files:** 测试、能力文档、实施记录及OpenSpec归档；不改未授权产品范围。
- [x] 实际运行完整pytest、ruff和调用数/并发/来源回放，逐项核对Task 1–9和spec场景覆盖。
- [x] 按用户单代理要求整体自审，不自动派独立评审子代理；verify若CLI不提供则明确报告并逐条人工核对，不能拿validate代替。
- [x] 仅全部通过后archive；分支/合并/PR按用户选择，不自动推送。

## 执行与验证记录（2026-10-13）

用户选择单代理、当前目录；未派子代理。实现文件和测试证据对应 OpenSpec tasks，见 [最终验证记录](../reviews/2026-10-13-consolidated-travel-verification.md)。Task 5 的 request_id/depends_on 依赖波次在 batch_execution 执行；Task 6 增补 test_amendment_recovery.py 覆盖暂存失败和恢复防重发。旧调用链测试迁移说明见 [协议记录](../../development/2026-10-13-test-protocol-migration.md)。正式规格不因测试迁移改变。

全仓 Ruff 有既有债务，HEAD 基线 1224 项，首次整体自审现工作区 1167 项；新增文件完整 Ruff 检查通过，全仓未发现 F821/F822/F823/F811。必要检查以新增文件 Ruff、全仓正确性规则和全量 pytest 为准，不在本变更中顺带修复全部历史格式问题。OpenSpec CLI 没有 verify 命令，采用逐项人工规格核对并明确记录，validate 仅检查文档。

自审补充：刷新使用 refresh_target.task_ids/components 限定范围；原有“按目标范围刷新”要求不变。新增 test_workflow_runner 的局部刷新回归及 test_preference_analysis 的真实入口 bootstrap/重开回归均通过。

最终审核：766 passed、3 skipped（55.57s）。最后补充旧 completed/validated 证据核验、已完成依赖恢复、未知抵达时挂起酒店报价；轻量小结不执行完整证据检查。历史统计测试夹具补齐苏州新路线查询及选择引用，保留原统计断言。计划中的测试名称为验收意图，实际文件及场景对应验证记录；本次采用整体实施，未逐任务提交，提交/合并/推送等待分支收尾选择。

归档完成：2026-10-13-consolidate-travel-model-calls，正式规格同步新增32项、修改3项。分支收尾菜单已提供，集成决定等待用户；未自动提交、推送、合并。
