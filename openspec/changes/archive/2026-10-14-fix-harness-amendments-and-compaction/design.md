## Context

动机见 proposal.md。正式依据为本 change 的三份 specs；当前产品规格继续规定完整 turn 压缩、子 Agent 不压缩及只读行程不推进状态。当前工作区已有缓存分页等未提交改动，实施基于该工作区，不能用 HEAD 覆盖它们。

根因已由真实请求和离线完整提示词复算确认：

- `validate_plan` 接受 `read_request` 与 `travel_update` 同存，`WorkflowRunner._request_flow` 提前执行读取。
- `prepare_saved_trip_amendment` 只给默认 5 条候选，`amend_saved_trip` 只允许一次补丁输出；模型未看到门店 12，却提交了存在于完整缓存的门店 11 ID。
- `_compact_request` 传历史余额，`select_safe_prefix` 和 `_summary_fits` 对事件分别计数。原边界样本固定部分 1,737、历史份额 13,117、逐条历史 13,041，完整请求为 15,147；更长样本覆盖到 seq=38 后仍 15,058，推进至下一个安全边界 seq=40 即为 14,738。

## Goals / Non-Goals

目标：修复上述实际执行错误，让完整请求预算与压缩完成条件一致，预算设为 20,000，验收包含真实模型的任务正确性。

不实施当前 turn 内阶段摘要，不新增每轮摘要，不引入查询 Agent，不合并定位与修改模型调用，不改变供应商能力和排序，不重新初始化 OpenSpec，不改其他待实施变更。用户最新要求增加80%提前触发点，纳入本次设计。

## Decisions

### 1. 只读与修改互斥，错误使用现有有限修正

在 `agents/contracts.py:validate_plan` 检查 `read_request` 与创建/修改/补丁字段冲突，并同步 `travel_prompts.initial_prompt`。使用 `MainAgent.initialize` 已有一次结构化修正，不通过优先级静默丢弃任一动作。`WorkflowRunner` 所有内部绕过模型的入口也保持只读无修改。偏好预处理可以和后续业务意图共存，不能扩大互斥范围误伤它。

### 2. 保存行程修改使用本地候选读取循环

保持初次定位和加载后修改两阶段。`MainAgent.amend_saved_trip` 增加 `read_cached_page`/提交补丁/澄清的结构化决定；没有 action 的现有补丁继续识别为提交，旧调用者兼容。`WorkflowRunner._decide_amendment` 维护局部候选窗口及跨页已展示引用集合，分页读取既有归一化有效证据，由程序核对查询/任务/结果版本、授权领域和 offset，不调用供应商、不更新已提交 TripPlan。

复用 `agents/cached_pagination.py` 的版本、窗口和去重原则及 `query_views` 的分页视图；修改前排除已选酒店后候选窗口以该过滤结果为准，不能混用原缓存序号。新增每页后继续同一个修改目标，不回到初次定位模型。单次视图仍有界；已读前页引用保留可选。重复读页、非法版本或无新证据则停止；模型决策受当前 WorkflowLimits 的预算和整轮时间上限约束。

### 3. 本次明确目标以字面要求和真实候选映射验收

实际模型回归补充：翻页决定使用顶层字段；兼容模型返回的单一cached_page_options包装，但解包后仍逐字段匹配本次允许的真实下一页，禁止额外字段、错误版本和重复页。普通替换已有有效替代、且没有比较后页要求时allowed_actions仅commit_patch；明确名称唯一匹配时同样直接选择。需要比较或存在证据缺口时才开放翻页/澄清。初次定位阶段若返回旧amendment_patch形状，先校验版本再进入加载候选的第二阶段，不能绕过目标与已展示引用验收。

在修改决定中使用可选 `selection_requirements`，例如 `{"hotel":{"name_contains":"模拟门店12"}}`。字段仅表达本次目标，不写入长期偏好；字面值必须出现在当前用户原文，完整名称或已展示候选引用按精确映射处理。程序通过完整有效缓存筛选目标并核对补丁引用，不能从“第12家”算出 `B0MEM000011`。

增加 `agents/amendment_selection.py`：`validate_selection_requirements(value, original_query)`、`resolve_literal_selection_requirements(original_query, candidate_rows, proposed)`、`validate_amendment_selection(patch, requirements, shown_refs, workflow)`。解析范围限于完整候选名、带引号的明确名称，以及“名称带/名称包含”形式的字面目标；一般“换一家”不强加名称。初次模型漏掉明确字面目标时，程序仍从这些形式取得目标；语义模糊或多个匹配不能擅自择一。

已能唯一定位的名称先从完整缓存取出真实候选并作为有界证据展示，可避免无意义逐页查找；尚未定位的目标允许修改循环请求缓存后页。补丁必须满足目标及已展示真实引用，并保留用户授权范围和旧版本校验。模型猜中缓存里存在但没展示的 ID 不能直接通过。回复继续从提交事实重建；即使文字模板声称门店 12，引用门店 11 仍拒绝。

### 4. 模型历史与原始事件使用两种视图

真实回归还暴露旧修改被当成本轮请求执行。主请求在背景数据后单列original_query及本次修改授权/允许动作；amendment的task视图去除旧draft_plan和查询容器重复，candidate_views作为本轮可选证据。历史仍保留事实与引用，不能重放旧指令。初始提示明确仅记录/回忆时直接回答，规划时最新更正人数同时作用于火车与住宿。

保留现有 `project_session_message` 用于排序、断点与恢复；新增 `project_history_message`/`project_history_messages` 只用于模型提示词，保留 role、content、必要 result_summary 引用，不携带存储管理字段。`MainAgent._refresh_history`、摘要输入及任何显式 recent_messages 入口使用同一模型视图，避免刷新又把元数据塞回去。原 SQLite 事件、covered_through_seq 和原始工具审计不迁移、不删除。

### 5. 压缩接受完整请求验收函数

扩展 `MemoryManager.compact_if_needed_async` 和同步兼容接口，可选 `request_fits(summary, remaining_events)` 作为完整请求验收；未传时保持原调用者的局部预算行为。MainAgent 必须传该验收函数：用候选摘要和剩余历史替换视图后调用同一个 build_messages，再由 `count_input_tokens` 连同 tools 和已有完整 response 判断预算。

MainAgent 全请求已超限时，不能被事件累加的“无需压缩”结果阻断；按完整安全边界枚举候选前缀。优先用相同生成摘要尝试后续完整边界，但摘要只能覆盖实际送入总结模型的消息，禁止仅扩大 covered_through_seq 而没总结新增轮次。需要扩大前缀时以原摘要和新增已完成消息增量生成新的候选摘要。

摘要提交前完整验收，提交后刷新读取并再次检查，处理 SQLite 并发状态变更。至多 3 次摘要模型调用/业务请求，且同时受已有模型调用及整轮时间预算控制；每次必须扩大安全覆盖或降低完整请求 token，否则停止。3 次是工程循环上限，不是默认每轮摘要次数。摘要 max_tokens 根据剩余预算和现有生成上限限制；摘要请求先计数，必要时在旧轮次边界分批，总调用次数仍计入上限。

若剩余固定输入及当前轮无法容纳，沿用原受控停止，不能通过当前 turn 切分或子上下文摘要绕过限制。原先用户明确排除该停止规则的专项测试，本次新增测试只覆盖可压缩旧历史、完整请求验收、有限循环及失败状态保留；已有测试可继续回归。输出阶段保留完整 response 并复用，不重发业务调用。

### 6. 预算与配置

完整请求达到触发点（含恰好16000）即尝试压缩，候选目标低于软阈值；固定部分已无法达到软目标时使用硬预算验收。摘要调用的max_tokens仅在该次调用内按输入余量和原生成上限收紧，原模型配置1116不变。当前无模型专用tokenizer，count_input_tokens保持显式estimated标记，不把UTF-8估算声称为API精确token；连续字母边界夹具发现估算偏小，保留原始样本与报告，普通中文边界另行验证。

`config.Settings.memory_context_budget_tokens` 默认 20,000；`.env.example` 同步；本地 `.env` 只替换/增加 MEMORY_CONTEXT_BUDGET_TOKENS，该键现值 14,854，其余内容不输出不改写。显式测试配置仍可覆盖。不能声称仅把两个旧样本放进 20,000 即完成修复：保留旧 14,854 的精确回归，并新增围绕 20,000 的可压缩历史边界。

## Risks / Trade-offs

用户执行阶段补充80%软触发：BudgetedModel提供compaction_trigger_tokens=floor(budget_tokens*0.8)，只用于主会话压缩，check硬预算不变。达到软触发点尝试压缩，目标低于软触发点；无安全旧轮次或软目标不可达但仍在硬预算内时允许继续。默认20000对应16000，显式测试预算同比例生效。保留用户刚改的temperature=0.3及生成回退0.3，不改max_tokens/thinking_mode。

- 字面名称歧义 → 明确匹配范围、原文验证及多匹配澄清；不以开放字符串推断 ID。
- 修改翻页增加调用 → 普通修改仍两次，局部循环只在需要证据时发生，缓存读取不增加供应商请求。
- 历史投影丢业务事实 → 保留 result_summary、行程及版本引用，原文更正和未知项进行前后对照评测。
- 摘要反复失败 → 3 次上限、逐次进展、候选摘要提交前全请求验收，失败不推进该候选覆盖。
- 日志根目录与供应商 → 新评测使用独立测试身份与输出目录，模拟 HTTP 经生产解析器；真实模型无模拟替代。
- 工作区其他变更 → 不重置、不搬走、不批量提交；执行选择确认后由实施流程确定工作区策略。

## Migration Plan

不改存储 schema；旧补丁和默认压缩调用接口兼容。只新增模型决定字段和模型历史视图。报告记录字段与旧资料并存，已有完整日志可恢复。回退代码和预算配置即可恢复旧执行机制，原始事件与业务存储不依赖新视图。

## Coordinated Change

本次同步实施已有 `add-runtime-file-logging`，其 proposal/design/specs/tasks 是普通日志的正式依据。日志默认 `data/logs/runtime.log`、UTF-8、上海时间、INFO、5 MiB 与 5 份备份，原数据库保留。预算和压缩诊断按该日志 change 新增场景补充；正常摘要不写用户正文、模型请求/答复或供应商原始数据。共同实施计划只执行一次日志任务，不先运行旧日志计划再重复本计划。既有用户数据清理记录不属于本次动作，不能重新删除数据。
