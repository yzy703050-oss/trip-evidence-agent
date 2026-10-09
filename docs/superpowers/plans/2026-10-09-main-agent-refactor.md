# 差旅助手非 RAG 重构 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** 实现主 Agent 统一分派、信息获取工具循环，以及完整简单查询直返；保留 RAG 当前实现。

**Architecture:** MainAgent 负责初始任务决策和按需最终综合；harness 负责依赖、有效偏好、缓存、直返校验和有界反馈。信息获取 Agent 独立完成参数整理、五种工具调用与总结。行程能力归 MainAgent，事实仍从结构化来源重建。

**Tech Stack:** Python、AgentScope OpenAIChatModel、现有 MeteredModel、pytest/pytest-asyncio、现有 Provider、Decimal、Msg 和本地会话存储，不为本次改造新增产品依赖。

**Spec:** ../specs/2026-10-09-information-acquisition-agent-design.md（下称设计）。对应设计第 17 节 T1–T8；项目未初始化 OpenSpec，本计划只展开执行和验证细节，不另写产品规格。

**Status:** 用户已确认，单代理在当前分支执行；任务 1–7 已实施，正在进行任务 8 整体自审和规格核对。

## Global Constraints

- RAG 的 Agent、SKILL.md、模型/embedding/top_k 配置、数据及初始化行为不变；外部 MODULAR-RAG-MCP-SERVER 无本轮写入。
- 合法子 Agent 只有 preference、memory_query、rag_knowledge、information_query；主 Agent 和 harness 不加入子 Agent 注册表。
- 原始 query 由入口保存；偏好成功后按 append/replace 合并，本轮条件优先于旧偏好。
- 内部工具只有 train_search、hotel_search、travel_guide、weather_query、web_search，其 schema 只交给信息获取。
- 默认火车候选最多 5 个、酒店/房型候选最多 5 个；本轮全量池保留，不向聚合 API 发送 limit/page。
- 每次信息获取最多 6 次模型调用、10 次工具调用，工具超时 30 秒；全局反馈最多 1 次。
- 主 Agent 初始决策 1 次；直返时最终阶段 0 次，否则最终阶段最多 2 次。信息获取最多执行 2 次。
- 全部预算计算用 Decimal，未知报价不视为满足预算，报价、库存、地点和来源不由模型文本覆盖。
- finalization_mode 缺省 synthesize；forward 只对完整信息获取答案启用。本轮不新增 RAG/记忆答案直返。
- 自动测试不调用真实模型、外网和付费接口。真实请求已发出后，不重放整轮查询。

## Review Focus

1. SDK 累计流快照和中间 JSON 修复：完整流结束前不能执行工具；原始 JSON 无效时拒绝执行（任务 1、3）。
2. 本轮偏好变更加天气查询：信息获取读取新偏好，直返也不重复持久化或伪造最终模型记录（任务 4、7）。
3. 两个并发同参数工具及随后记录失败：真实 Provider 只执行一次，失败后 CLI 也不重放（任务 2、6）。
4. 天气日期不在接口实际返回范围：不能用当前天气冒充指定日期，也不能通过直返完整性检查（任务 2、4）。
5. 反馈中同候选 ID 被模型改报价、同工具调用 ID 再出现：报价从原池恢复，执行记录 ID 作用域唯一（任务 3、5）。

## 实施前状态与文件职责

当前分支：codex/information-acquisition-agent。产品基线 2dfb62d；设计文档已有提交，最新简单查询直返修订待提交。此前执行现有离线基线得到 210 passed、5 skipped，13.36 秒；这不是新实现的验证结果。

执行时先确认分支和工作树，保留任何用户新改动。推荐在当前工作分支单代理实施；如用户选择其他隔离方式，按其选择处理，不擅自切换项目到外层仓库。

| 文件 | 职责 |
| --- | --- |
| agents/contracts.py（新增） | 决策校验、合法角色、RunState 和可配置运行上限 |
| agents/model_io.py（新增） | 收齐模型响应、严格工具参数解析与协议消息转换 |
| agents/main_agent.py（新增） | plan/finalize 模型阶段 |
| agents/itinerary_module.py（新增） | 行程上下文、解析和现有事实保护适配，无独立模型 |
| agents/execution_harness.py、orchestration_agent.py | harness 的 run_turn、依赖、直返、反馈与持久化；后者是公开外壳 |
| travel_data/candidates.py（新增） | 本轮缓存、同参数并发去重、完整候选和视图 |
| travel_data/tools.py（新增） | 五个工具 schema、参数校验、Provider 执行和状态 |
| travel_data/public_query.py（新增） | 从旧 query-info 提取的天气/网页检索，返回材料，无 LLM |
| travel_data/result_guard.py（新增） | 信息获取结构化结果/来源校验与安全直返组装 |
| query-info 的 Agent | 有界工具循环、条件与摘要输出 |
| registry、SkillLoader、CLI、独立规划脚本 | 单一运行入口和按角色能力暴露 |
| context、evals 中受影响代码 | 会话/工具记录、实际模型用量、最终行程和评估 |

新增 public_query 和 result_guard 只是拆分已确认的检索与事实保护职责，不增加业务能力。

接口以 dict 为传输类型，沿用 Msg 和 JSON；dataclass 保存不可送入提示词的本轮状态。任务之间使用以下明确名称，不由每个实施者独立命名。

## Task 1: 主 Agent 契约、模型响应适配与行程模块

**映射：** T1；设计 3、4、7、8、9、12；验收 1、8、10。

**Files:** 新增 agents/contracts.py、agents/model_io.py、agents/main_agent.py、agents/itinerary_module.py、tests/conftest.py、tests/test_main_agent.py、tests/test_model_io.py。此任务暂不切换旧 CLI，避免未完成依赖导致入口损坏。

**Interfaces:**

- validate_plan(value: dict) -> dict、validate_final(value: dict) -> dict；不合法时抛 ValueError，harness 随后转换为业务错误。
- RunLimits：info_model_calls=6、info_tool_calls=10、feedback_rounds=1、tool_timeout=30.0、candidate_limit=5。
- RunState：turn_id、limits、effective_preferences、results、domain_results、travel_conditions、feedback_round、tool_requests、external_requests_started、candidates；results 采用原 result/data 包装，字段初始化不共享可变对象。
- ModelTurn：text: str、tool_calls: list[dict]，每个调用字段 id、name、arguments。
- await collect_model_turn(response: Any) -> ModelTurn；以 SDK 最终累计快照为准，raw_input 存在时用严格 json.loads，拒绝不完整 JSON。
- to_assistant_tool_message(turn: ModelTurn) -> dict、to_tool_message(call_id: str, result: dict) -> dict，使用 OpenAI messages 协议。
- MainAgent(model, skill_loader=None).plan(context: dict) -> dict；finalize(context: dict) -> dict。
- build_itinerary_context(context: dict) -> dict、guard_final_itinerary(plan: dict, domain_results: dict) -> dict；先通过内部适配调用现有 guard_itinerary，任务 5 接入完整上下文保护。

- [x] Step 1 写失败测试。构造 scripted_model_factory，让假模型保存每次 messages/kwargs；测试 plan 只暴露四个合法子 Agent，原始 query 保留；forward 缺省 synthesize；itinerary 不允许 forward。验证未知 Agent、NaN/bool 优先级和依赖循环被拒绝。测试模型累计 text 不重复拼接，工具累计快照仅产生一个完整调用，无效 raw_input 不被 SDK 修复值掩盖。
核心用例命名 test_main_plan_only_advertises_business_agents、test_stream_uses_final_snapshot、test_invalid_raw_tool_json_is_rejected；对最后完整快照的断言：

~~~python
assert len(turn.tool_calls) == 1
assert turn.tool_calls[0]['arguments']['departure_date'] == '2026-10-10'
assert 'tools' not in model.calls[0]['kwargs']  # 主 Agent 不持有查询工具
~~~

- [x] Step 2 运行 python -m pytest -q tests/test_main_agent.py tests/test_model_io.py，确认由于新接口缺失或行为断言失败，不能因假模型自身错误冒充红灯。
- [x] Step 3 实现上述接口。迁移旧意图理解和行程提示词，主 Agent 输入按角色白名单取摘要；plan 返回直接回答时校验空调度。完整流后才提取 tool_use，不把 thinking 内容当作答案。测试 fixture 使用确定响应，不请求真实服务。
- [x] Step 4 复跑 Step 2 命令，全部通过；测试 MainAgent 两个阶段均调用现有包装模型，行程模块没有自己的 model。
- [x] Step 5 更新对应设计任务进度（T1 尚有入口迁移，记录部分完成），仅提交本任务文件；提交说明 feat: add main agent decisions and model response adapter。

代表性断言：累计响应的两个快照分别含一个未完整和完整 train_search，最终 len(turn.tool_calls)==1，arguments 包含完整日期；在异步流关闭前执行器调用数为 0。

## Task 2: 查询工具与本轮候选池

**映射：** T3/T4；设计 10、11、13；验收 9、11–15、18。

**Files:** 新增 travel_data/candidates.py、tools.py、public_query.py、result_guard.py；修改 travel_data/agent_support.py；测试 tests/test_information_tools.py、tests/test_candidates.py。复用 contracts.py、providers.py、juhe_train.py，默认不扩展聚合参数。

**Interfaces:**

- CandidateStore()；async get_or_fetch(key: str, fetch: Callable[[], Awaitable[AgentDataResult]]) -> tuple[AgentDataResult, bool]，同 key 用锁/共享任务去重；失败结果也保留本轮请求事实。
- query_cache_key(domain: str, query: dict) -> str，仅包含真实查询参数，不包含 candidate_offset 和本地筛选。
- candidate_view(result: AgentDataResult, *, limit: int, offset: int, constraints: dict, preferences: dict) -> dict；items 是来源有效的候选视图，全量池不变。
- ToolExecutor(providers: dict, public_provider=None).schemas() -> list[dict]；async execute(name: str, arguments: dict, run: RunState, *, call_id: str) -> dict。
- PublicQueryProvider.weather(city: str, requested_date: str | None) -> AgentDataResult；web(query: str) -> AgentDataResult。
- guard_information_result(value: dict, requested_domains: list[str]) -> dict；过滤无效事实、判定请求覆盖，保留缺项与失败。

- [x] Step 1 写失败测试：缺酒店人数不调用 Provider；乘车人数缺省 1；日期与入住区间校验；无 Provider 返回 unavailable；超时保留其他成功结果。七个酒店候选的默认视图最多 5 个，offset 查看余下候选不新请求；高价、未知价和 Decimal 分位价格正确处理；两个并发同 key 仅一次 Provider 调用。
核心用例命名 test_missing_guests_does_not_request_provider、test_concurrent_same_query_fetches_once、test_candidate_window_does_not_refetch、test_weather_date_outside_response_is_incomplete。默认 5 个窗口与同参数并发的断言：

~~~python
assert len(first_view['items']) == 5
assert len(second_view['items']) == 2
assert len(provider.queries) == 1
~~~

- [x] Step 2 运行 python -m pytest -q tests/test_information_tools.py tests/test_candidates.py，确认预期红灯。
- [x] Step 3 实现工具和本轮池。关键字参数分离：真实查询、硬约束/排序、candidate_offset/刷新；本地参数不发 API。Provider 结果按现有类型校验，真实调用开始前标记 external_requests_started。天气按实际返回日期匹配请求日期，超范围标记不完整；网页只返回标题、snippet、URL，保留旧来源过滤和搜索回退，没有摘要模型。
- [x] Step 4 复跑 Step 2；追加现有 tests/test_juhe_train_provider.py、test_travel_data_contracts.py、test_travel_budget.py。断言 http_post 的 data 不含 limit/page/candidate_offset，凭据不进入返回和日志。
- [x] Step 5 记录 T3/T4 子项证据，提交 feat: add information tools and per-turn candidate cache。

工具 schema 的真实参数：火车 origin/destination/departure_date/passengers；酒店 city/check_in/check_out/guests；攻略 destination/visit_dates；天气 city/date（date 可为空表示当前）；网页 query。可选本地 constraints/preferences/candidate_offset 只用于程序筛选。已有硬约束不得被反馈重写，refresh 仍受工具预算约束。

## Task 3: 信息获取 Agent 的有界工具循环与总结

**映射：** T3；设计 8、9、12–14；验收 8–10、14、17、20。

**Files:** 修改 .claude/skills/query-info/script/agent.py、query-info/SKILL.md；新增 tests/test_information_agent_loop.py；使用任务 1 和任务 2 接口。

**Interfaces:** InformationQueryAgent(model, tool_executor=None, ...).run(context: dict, run: RunState) -> dict；reply(Msg) 继续作为现有调用外壳，harness 有状态执行走 run。输出字段按设计第 12 节，domain_results 由真实工具返回组装，不采用模型自产候选。

- [x] Step 1 写失败测试：假模型第一次返回天气/网页工具调用，第二次收到配对结果并给 summary；输出同时保留真实结果与摘要。多工具单项失败、未知工具、无效 JSON、重复 ID 和中途流异常均不误执行；6 次模型/10 次工具限制准确，超限保留成功数据。
核心用例命名 test_multiple_tool_results_reach_same_agent、test_stream_does_not_execute_partial_arguments、test_info_model_and_tool_limits、test_model_cannot_replace_domain_facts。正常工具回合的断言：

~~~python
assert info['domain_results']['weather']['items'] == expected_verified_items
assert {msg['tool_call_id'] for msg in tool_messages} == {'weather-1', 'web-1'}
assert len(model.calls) == 2  # 工具选择和同一 Agent 总结
~~~

- [x] Step 2 运行 python -m pytest -q tests/test_information_agent_loop.py，确认缺失循环行为的红灯。
- [x] Step 3 接入 collect_model_turn 和 ToolExecutor。只有信息获取传 tools；保留 assistant tool_calls 和每个 role=tool 返回。工具参数经校验成为条件，条件缺项仍结构化输出；最终模型仅提供 summary、条件补充和已有候选 ID，不覆盖程序保存的 domain_results。请求域未覆盖时整体不能是完整 ok。
- [x] Step 4 复跑 Step 2，并运行任务 1、2 的聚焦测试。循环用假模型断言模型收到所有成功与失败结果；没有旧网页工具内部第二个总结模型。内部调用用 model_stage 标记，执行记录 ID 加本轮/执行作用域，模型消息仍用原 ID。
- [x] Step 5 更新 T3 完成证据，提交 feat: make information agent collect query and summarize with tools。

代表性断言：完整天气工具请求后 info['domain_results']['weather']['items'] 等于经校验的实际 Provider 数据；模型最后输出伪造 domain_results 时不改变该结果。

## Task 4: harness 回合、偏好合并与完整答案直返

**映射：** T2、T7 输出分支；设计 2、4–8、12、15；验收 2、4–8、19、23–25。

**Files:** 修改 agents/orchestration_agent.py；新增 tests/test_main_harness.py、tests/test_answer_forwarding.py。保持 RAG/偏好/记忆 Agent 文件不变。

**Interfaces:**

- OrchestrationAgent(main_agent=None, agent_registry=None, memory_manager=None, ...).run_turn(context: dict, run: RunState) -> dict。
- normalize_schedule(schedule: list[dict]) -> list[dict]，白名单校验、合并重复信息获取请求域，按阶段与 depends_on 拓扑执行。
- merge_preference_updates(current: dict, changes: list[dict] | dict) -> dict，不修改输入对象。
- can_forward_answer(decision: dict, info: dict, run: RunState) -> bool；build_forward_result(info: dict, run: RunState) -> dict，经来源与事实保护。
- 最终 envelope：status、finalization_method、final_answer、results、domain_results、missing_fields、itinerary（无行程时不填）；results 沿用 agent_name/status/data 展示形式，内部 previous_results 继续 result/data 包装。

- [x] Step 1 写失败测试：偏好 append/replace 在信息获取前生效，依赖记忆后 RAG 使用原 Msg 协议；RAG 的 no_knowledge/error 不被当成功。假 MainAgent 的 finalize 调用计数验证完整天气 forward 为 0，synthesize/itinerary 为 1；partial、缺字段、空摘要、请求域缺失、待完成依赖或其他待综合答案都不直返。
核心用例命名 test_complete_weather_skips_finalize、test_partial_result_requires_finalize、test_pending_answer_requires_synthesis、test_effective_preferences_precede_info、test_rag_legacy_status_is_preserved。完整天气直返的断言：

~~~python
assert final['finalization_method'] == 'forward'
assert main.finalize_calls == 0
assert final['domain_results']['weather']['query']['date'] == '2026-10-10'
~~~

- [x] Step 2 运行 python -m pytest -q tests/test_main_harness.py tests/test_answer_forwarding.py，确认预期红灯。
- [x] Step 3 将原编排批次逻辑收敛到 run_turn。完成任务后才更新结果与偏好，并按真实依赖准备下轮输入。符合 forward 条件时由程序形成 envelope，不调用 finalize；多个工具已完成一个信息获取任务也允许直返。仅前置偏好/记忆资料不自动算作待综合答案，主 Agent 分派目标须区分前置资料与要回答的内容。
- [x] Step 4 复跑 Step 2 和任务 3 测试。验证 simple forecast 仍有主 Agent 初始 1 次和信息获取实际调用，未凭空记 main:finalize；来源不全不进入直返。RAG 初始化和业务状态通过原返回体传播，不修改检索实现。
- [x] Step 5 更新 T2 与直返部分证据，提交 feat: orchestrate main agent and forward complete simple answers。

断言例：final['finalization_method']=='forward'、main.finalize_calls==0、final['domain_results']['weather']['query']['date']=='2026-10-10'；将 info.status 改为 partial 后 finalize_calls==1。

## Task 5: 行程事实保护、定向反馈和运行限制

**映射：** T1/T4/T5；设计 11–13、15；验收 11–18、25。

**Files:** 修改 agents/itinerary_module.py、travel_data/plan_guard.py、travel_data/result_guard.py、agents/orchestration_agent.py；新增 tests/test_main_feedback.py；更新 tests/test_sourced_itinerary.py、tests/test_sourced_cli.py 中有效保护断言。

**Interfaces:** guard_final_itinerary(plan: dict, domain_results: dict) -> dict 接收真实候选，返回现有可展示选项与预算；validate_feedback(value: dict, conditions: dict) -> dict 校验 reason/domains/constraints，禁止覆盖已确认硬条件。

- [x] Step 1 写失败测试：fake 主 Agent 输出 needs_requery，只重查受影响域，缓存复用，不再调用偏好/记忆/RAG；第二次继续要求补查时停止。伪造 ID/价格/来源/地点被过滤或由原候选重建；直返摘要携带伪造报价也不进入展示。缺条件不形成已完成行程，预算未知不能当预算满足。
核心用例命名 test_feedback_only_requeries_affected_domain、test_feedback_limit_stops_second_requery、test_forwarding_does_not_display_forged_price、test_feedback_preserves_hard_constraints。一次反馈后再次要求补查的断言：

~~~python
assert info.executions == 2
assert main.finalize_calls == 2
assert preference.executions == memory.executions == rag.executions == 1
~~~

- [x] Step 2 运行 python -m pytest -q tests/test_main_feedback.py tests/test_sourced_itinerary.py，确认对应断言的红灯；保留现有 guard 的有效保护场景。
- [x] Step 3 接通 feedback 到同一个 RunState。累计信息获取最多两次，最终模型最多两次；没有新查询依据、跨硬约束、域不合法或上限时返回确定原因并保留结果。程序重建选项与预算；报价/库存/行程地点的展示采用结构化事实，不采纳未验证的自由文本。
- [x] Step 4 复跑 Step 2，加 test_answer_forwarding.py 和 test_candidates.py。验证候选池跨反馈保留，只有真实参数变化才调用 Provider，forward 不能绕过 guard。
- [x] Step 5 更新 T1/T4/T5 对应证据，提交 feat: guard itinerary facts and bound targeted query feedback。

## Task 6: 注册表、CLI 和旧入口迁移

**映射：** T6；设计 3、14–16；验收 1–3、18、20、21。

**Files:** 修改 agents/lazy_agent_registry.py、agents/__init__.py、utils/skill_loader.py、cli.py、config.py、.claude/skills/plan-trip/script/plan_trip_execution.py、plan-trip/SKILL.md。迁出后删除/停用 agents/intention_agent.py、旧 event-collection/train-search/hotel-search/travel-guide 和 plan-trip 的 Agent 入口；不删除来源数据、有效指南或用户文件。更新 tests/test_lazy_agent_registry.py、test_sourced_routing.py、test_final_fix_connections.py、test_juhe_cli_wiring.py。

**Interfaces:** LazyAgentRegistry 仅暴露四个合法子 Agent；给信息获取注入 ToolExecutor，RAG 构造参数保持原样。SkillLoader.get_skill_prompt(..., allowed_skills: set[str] | None = None) -> str，完整 get_skill_content 行为不变。CLI 初始化 MainAgent 和 harness，run_turn 使用一次 RunState。

- [x] Step 1 写失败测试：技能目录有旧脚本仍不能调度旧节点，raw Skill 名也不能绕过白名单；RAG 仍解析原类；仅 info 获得五个工具。CLI 模拟成功火车请求后 stage/final-message 写入失败，post 调用次数为 1；独立规划脚本不再加载旧事项收集和规划 Agent。
核心用例命名 test_registry_rejects_private_and_retired_nodes、test_only_information_receives_tool_executor、test_paid_query_not_replayed_after_record_failure、test_standalone_plan_uses_main_harness。注册与付费请求断言：

~~~python
assert set(registry.keys()) == {'preference', 'memory_query', 'rag_knowledge', 'information_query'}
assert 'train-search' not in registry
assert len(post_calls) == 1
~~~

- [x] Step 2 运行 python -m pytest -q tests/test_lazy_agent_registry.py tests/test_sourced_routing.py tests/test_juhe_cli_wiring.py tests/test_final_fix_connections.py，记录迁移前失败原因。
- [x] Step 3 切换 CLI 到单一回合入口，先保存 original_query 和 turn_id，再执行 MainAgent。配置增加本轮 limits，不触碰 RAG_CONFIG/LLM_CONFIG 的现有值。取消包住整轮编排的自动重试，或仅在同一 RunState 无真实请求时重试；付费请求发出后的任何失败都不重放。独立脚本复用同一初始化/回合逻辑。迁走有效逻辑后清除旧活跃入口，不保留两套实现。
- [x] Step 4 复跑 Step 2，并运行 test_deepseek_model_config.py、test_juhe_train_provider.py。检查旧模块引用仅剩明确历史说明，不剩运行时导入和当前评估预期。确认 RAG 目录、RAG_CONFIG 和外部项目无变更。
- [x] Step 5 更新 T6 证据，提交 refactor: route all travel entries through the main agent harness。

## Task 7: 最终输出、会话保存和评估

**映射：** T7；设计 12、15、18；验收 19、22–25。

**Files:** 修改 cli.py、context/memory_manager.py 中必要的工具/阶段记录代码、evals/v0_memory/runner.py、cases.json、trip_case.json、memory_case.json、README.md、.claude/skills/README.md；更新 tests/test_v0_memory_flow.py、test_v0_memory_evals.py、test_sourced_evals.py、test_v0_telemetry.py，增加 tests/test_main_output.py。

**Interfaces:** 主 Agent 最终阶段记录业务阶段 main_finalize，initial plan 继续记录 agent_plan；模型阶段名 main:plan、main:finalize、main:itinerary、agent:information_query 与其他现有子 Agent 阶段。finalization_method 记录到最终 envelope/query_run；工具使用 record_tool_call/record_tool_result 配对，并保留实际状态与缓存命中。

- [x] Step 1 写失败测试：forward 显示一次完整天气答案和来源，没有第二份子 Agent 最终回答；每轮仅一条 final assistant 消息；直返没有 main:finalize 记录及行程历史。完整行程从 travel_conditions 保存，needs_requery 中间结果不存。失败工具不能被评估为全部成功，finalize 缺省跳过也不能被评估为少执行了子 Agent。
核心用例命名 test_forward_records_one_final_message、test_forward_has_no_finalize_usage_or_trip_history、test_evaluation_detects_failed_internal_tool、test_completed_itinerary_uses_new_conditions。天气直返记录的断言：

~~~python
assert len(final_assistant_events) == 1
assert not any(record.get('stage') == 'main:finalize' for record in model_records)
assert saved_trips == []
~~~

- [x] Step 2 运行 python -m pytest -q tests/test_main_output.py tests/test_v0_memory_flow.py tests/test_v0_memory_evals.py tests/test_sourced_evals.py tests/test_v0_telemetry.py，确认新结构产生预期失败。
- [x] Step 3 单一展示入口分支消费 envelope，候选价格/库存/预算按 guard 输出展示。保存偏好和已完成行程各一次。评估分别识别初始计划子 Agent、信息获取执行和工具、按需主 Agent 最终阶段；读取实际模型记录而不是假定固定次数。保留 RAG 的既有检索评估依据与字段，不改其 Agent。
- [x] Step 4 复跑 Step 2 和所有 CLI/来源聚焦测试。文档用当前角色/字段说明简单天气“决策 → 信息获取 → 程序直返”和组合行程路径，明确酒店/攻略来源未配置。
- [x] Step 5 更新 T7 证据，提交 feat: record and present forwarded answers and main agent results。

## Task 8: 全量回归、自审和规格核对

**映射：** T8 与所有验收场景。

**Files:** 上述受影响文件、必要回归用例、本文和设计任务进度；不增加产品功能。

- [ ] Step 1 将设计第 18 节 25 个场景映射到具体测试；新增缺口用例并先验证能捕获对应错误，不能仅按任务勾选认定实现完成。
- [ ] Step 2 运行全部必要离线检查，在项目可用虚拟环境中执行：

~~~powershell
& '..\..\.venv\Scripts\python.exe' -m pytest -q tests
& '..\..\.venv\Scripts\python.exe' -m compileall -q agents context travel_data utils cli.py
& '..\..\.venv\Scripts\python.exe' -m compileall -q .claude/skills/query-info/script .claude/skills/preference/script .claude/skills/memory-query/script .claude/skills/ask-question/script .claude/skills/plan-trip/script/plan_trip_execution.py
git diff --check
git diff 2dfb62d -- .claude/skills/ask-question
~~~

预期 pytest 无失败，compileall 和 diff 检查退出 0，RAG 目录差异为空。若旧意图测试删除，同步 CI 命令并确认主 Agent 测试实际纳入。RAG_CONFIG、模型配置和知识库数据逐项人工比对；外部 RAG 项目没有本轮写入。

- [ ] Step 3 按所选执行方式审查。单代理由当前代理整体自审，不自动派独立审查代理；多子代理按每任务规格/质量评审并整体核对。修复重要发现后复跑受影响检查，只有必要才扩展测试。
- [ ] Step 4 人工核对设计所有场景和任务，记录命令、结果、变更差异及未做真实联调的限制。没有 OpenSpec verify 工作流，不声称已运行；不创建 OpenSpec 归档。
- [ ] Step 5 只有证据满足完成条件才勾选 T8/最终状态并提交验证记录。分支收尾向用户提供适用选择；不自行合并、发布或推送。

## 任务与正式设计对应

| 设计任务 | 完成证据来自实施任务 |
| --- | --- |
| T1 主 Agent/行程模块 | 1、5、6 |
| T2 harness/上下文/直返 | 4 |
| T3 信息获取循环与工具 | 2、3 |
| T4 候选/事实/预算 | 2、5 |
| T5 反馈与限额 | 5 |
| T6 注册/入口迁移 | 6 |
| T7 输出/记忆/评估 | 4、7 |
| T8 回归/规格核对 | 8 |

每项独立交付后记录红灯/绿灯、审查和提交证据。单个设计任务对应多个实施任务时，不提前勾成全部完成。

## 执行方式与审阅

推荐单代理在当前工作分支实施：这些任务共享上下文、候选和输出接口，连续实施衔接更直接，通常成本更低，最终由当前代理整体自审。

多子代理按任务使用干净上下文实施，并安排独立规格与质量评审，审查深度更高，但增加上下文成本及交接时间；本计划依赖较强，不能保证多子代理更快。

用户已选择单代理在当前分支实施及整体自审，无子代理派发。验证证据见 ../verification/2026-10-09-main-agent-refactor.md。
