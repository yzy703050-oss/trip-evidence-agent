# Agent 上下文隔离与 token 压缩实施计划

> **For agentic workers:** 按用户选择执行。单代理使用 superpowers:executing-plans，由当前代理连续实施并整体自审；多子代理使用 superpowers:subagent-driven-development，以干净任务上下文实施并独立评审。执行方式尚未选择，不默认派子代理。

**Goal:** 实现本次 OpenSpec 规定的子临时上下文、主结果摘要、完整用户轮次 token 压缩与超限分段提示。

**Architecture:** 主历史按事件投影恢复，完整业务对象留在程序状态，模型只获得必要摘要和事实引用。统一输入计数和模型调用保护，主历史可压缩，子上下文只检查不压缩。

**Tech Stack:** Python 3.11、AgentScope、现有 OpenAI 兼容模型、pytest、JSON/JSONL 本地存储。

**Spec:** `openspec/changes/isolate-agent-context-token-compaction/specs/agent-context-isolation/spec.md`、`specs/token-session-compaction/spec.md`；技术依据为该 change 的 `design.md`，任务状态在该 change 的 `tasks.md`。

## Global Constraints

- 只按 token 阈值触发，不按轮数淘汰生产上下文；主压缩只能覆盖连续完整已完成用户轮次。
- 子 Agent 每次委派临时上下文，不摘要、不持久化、不继承其他委派对话。
- 保留原始日志与完整业务依据，不改候选身份、来源校验或用户确认事实。
- 当前上下文超限提示将旅程分段，禁止超限后的模型补查/兜底/自动重试。
- 用户明确不做单轮超限停止与分段提示的专项测试。Task 4 不采用 TDD；只做代码审查、静态检查和非豁免已有回归，不变相增加此场景测试。
- 初始阈值沿用 6000，是可配置估算预算，不宣称精确模型窗口或已校准最优值；输出上限 8192 保持其独立语义。
- 当前规划阶段不修改产品代码；开始前读取 `openspec instructions apply --change isolate-agent-context-token-compaction --json` 和本计划。

## Review Focus

- 大量短轮次尚未压缩但超出旧 5/10 轮限制：Task 1 证明无空档。
- 原工具字段通过完整最终 JSON/摘要绕回主模型：Task 3 证明投影与摘要均不泄漏。
- 旧轮内摘要及未完成旧轮：Task 2 证明兼容剩余消息且不跨越未完成轮。
- 相同子 Agent 实例复用或补查：Task 3 证明局部对话独立、显式资料保留。
- 快照累计、预算异常被通用捕获吞掉：快照输入计数的纯函数检查归 Task 1；超限异常传播归 Task 4 静态审查，按用户要求不做超限场景测试。

## Files and Interfaces

- 新建 `context/context_budget.py`：TokenEstimate、可注入计数器、输入估算与预算异常/模型包装。
- 新建 `agents/context_projection.py`：主会话消息、子结果和工作流的明确字段投影。
- 修改 `context/compaction.py`、`context/memory_manager.py`、`context/short_term_memory.py`：完整轮次边界及连续上下文。
- 修改 `agents/main_agent.py`、`agents/execution_harness.py`、`agents/workflow_runner.py`：统一请求重建、结果投影、停止传播。
- 修改 `cli.py`、`config.py`、`context/telemetry.py`、`utils/response_renderer.py`：接线、预算配置、记录与提示。
- 审查并按需修改 `.claude/skills/{query-info,memory-query,preference,ask-question}/script/agent.py`：临时上下文和预算异常传播；不修改与任务无关的 RAG 行为。

### Task 1: 统一计数与主会话连续投影

**OpenSpec mapping:** tasks 1.1；token 阈值决定压缩时机、审计日志与模型上下文分离。

**Files:** 新建上述两个模块；修改 short_term_memory、memory_manager 和测试 `tests/test_context_budget.py`（新）、`tests/test_v0_memory_context.py`。

**Interfaces:**
- `TokenEstimate(tokens: int, method: str, estimated: bool)`；`count_input_tokens(messages: list[dict], tools: list[dict] | None = None, *, counter=None) -> TokenEstimate`。
- `project_session_message(event: dict) -> dict | None`：用户原文不改；assistant 的完整 main_result JSON 取 final_answer 与必要结果摘要；非 session message 返回 None。
- `MemoryManager.get_compacted_context() -> dict`：summary、全部未覆盖主 recent_messages、pending_tool_calls；生产调用不限制轮数。
- `ShortTermMemory(max_turns: int | None = None)`：None 不按轮数淘汰；展示用 `get_recent_context(n_turns)` 可保留。

- [ ] 先写失败测试：`test_more_than_ten_small_turns_remain_visible` 保存 12 个短完整轮次，断言主历史为全部 24 条；`test_main_result_history_projects_reply_without_tool_payload` 带唯一工具原文标记，断言主投影无标记、原 event 未变；`test_input_count_includes_instructions_business_and_tools` 注入确定性计数器核对各组成部分，无超限停止断言。
- [ ] RED：运行下列命令，确认失败来自旧轮数截取、完整 JSON 泄漏和缺少输入计数接口，不是导入环境错误。

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_context_budget.py tests/test_v0_memory_context.py -q
```

- [ ] 最小实现：使用模型兼容计数器注入接口；默认明确标识 UTF-8 估算方法及 estimated；规范序列化消息/tools，避免重复计算快照。主投影不读 agent scope；移除生产窗口淘汰并保持人工展示接口。
- [ ] GREEN：重跑上述测试和 `tests/test_v0_memory_flow.py`，读取输出；更新 OpenSpec 1.1 与本计划记录，再提交本任务代码/测试。

**完成条件:** 未压缩主消息全部可见；工具事件不计入主历史；输入计数覆盖真实组成且方法可追踪；原日志不改。

### Task 2: 完整用户轮次前缀压缩与恢复

**OpenSpec mapping:** tasks 1.2；完整轮次切分、摘要覆盖与恢复连续。

**Files:** 修改 compaction、memory_manager、`tests/test_v0_compaction.py`、`tests/test_v0_memory_context.py`、`tests/test_cli_session_resume.py`。

**Interfaces:**
- `select_safe_prefix(events: list[dict], token_budget: int, *, token_counter=estimate_tokens) -> int | None`：events 使用主投影，seq/turn_id 保留；不再提供保护固定轮数的生产参数。
- `compact_if_needed(token_budget, *, summarizer, token_counter=estimate_tokens)`、async 对应接口：不接受生产 keep_recent_turns；可接受固定输入估算作为剩余历史预算，实际请求重建接口由 Task 4 接入。
- 使用既有 covered_through_seq/cleared_through_seq；新 cutoff 永远是完整轮末，checkpoint 只兼容读取。

- [ ] 先写失败测试：替换旧 `test_active_turn_cut_uses_completed_stage_and_retains_pending_call` 为 `test_active_turn_stages_never_form_session_cut`，构造较长的完整旧轮次和低于阈值的短活动轮，断言 cutoff 为旧轮末而非活动轮阶段（不构造单轮超限停止场景）；`test_unfinished_old_turn_blocks_prefix`；`test_legacy_mid_turn_summary_keeps_uncovered_messages`；`test_summary_failure_keeps_coverage`；摘要后重启无空档。
- [ ] RED：确认旧阶段切点、固定轮数窗口及旧参数依赖分别导致失败。

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_v0_compaction.py tests/test_v0_memory_context.py tests/test_cli_session_resume.py -q
```

- [ ] 最小实现：顺序分组完整用户轮次，只选连续已完成旧前缀；摘要仅用主投影；生成后重计数并有界尝试更长完整前缀，失败不提交游标。旧摘要保留未覆盖同轮消息，禁止新轮内 checkpoint。
- [ ] GREEN：重跑上述命令并回归 clear/pending-tool 恢复；更新 1.2 和记录，提交任务。

**完成条件:** 新压缩不拆用户轮，摘要前后连续，失败不丢原消息，旧 state 可读取。

### Task 3: 子临时上下文与可验证结果摘要

**OpenSpec mapping:** tasks 2.1、2.2；agent-context-isolation 全部要求。

**Files:** 扩展 context_projection；修改 main_agent、execution_harness、workflow_runner、必要子 Agent 脚本；新增 `tests/test_agent_context_isolation.py`，修改 information-agent-loop/main-feedback/workflow-runner 相关测试。

**Interfaces:**
- `project_child_result(result: dict, *, candidate_limit: int = 5) -> dict`：返回状态、结论、缺项、必要候选事实/来源/查询与任务身份，不返回 raw/messages/tool_calls/candidate_cache/execution/history。
- `project_workflow_context(workflow: dict, *, task_id: str | None = None, candidate_limit: int = 5) -> dict`：保留确认条件、任务状态、选中候选及需要的有界资料。
- 完整 run.results/domain_results/workflow 保持现有业务形状；仅在送入模型前应用摘要投影，最终业务渲染读完整对象。

- [ ] 先写失败测试：`test_child_raw_tool_marker_absent_from_main_requests_and_summary` 检查 main initialize/step/finalize 及主历史摘要输入；`test_child_projection_preserves_candidate_ids_and_sources_without_mutation`；`test_reused_child_starts_fresh_messages` 两次同实例调用仅本次任务上下文；`test_child_calls_do_not_invoke_compaction` 使用会报错的摘要 spy；保留同次多工具结果成组进入子模型的既有测试。
- [ ] RED：捕获真实模型输入，确认失败来自完整结果传递或投影不足；不以只检查 helper 的测试替代运行接线。

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_agent_context_isolation.py tests/test_information_agent_loop.py tests/test_information_query_agent.py tests/test_main_harness.py tests/test_main_feedback.py tests/test_workflow_runner.py -q
```

- [ ] 最小实现：白名单投影必要事实/引用；检查主模型所有路径和历史最终 JSON；子 Agent 局部 messages 不存实例状态，不调用摘要。显式任务资料可传入，不继承其他委派对话；候选选择与程序校验仍读原对象。
- [ ] GREEN：重跑以上并运行 `tests/test_sourced_itinerary.py`、`tests/test_workflow_queries.py`、`tests/test_travel_updates.py`；分别更新 2.1/2.2 和记录，提交任务。

**完成条件:** 主输入无工具过程，子循环完整且任务间独立，来源与选择可验证。

### Task 4: 预算接线、停止传播和分段文案（用户豁免专项测试）

**OpenSpec mapping:** tasks 3.1；不可压缩的当前上下文超限时停止并建议分段。

**Files:** context_budget、main_agent、telemetry、cli、config、harness、runner、上述子脚本、response_renderer。

**Interfaces:**
- `ContextBudgetExceeded(Exception)`：携带 agent_scope、budget_tokens、input estimate，不能继承被普通业务校验捕获的 ValueError。
- `BudgetedModel(model, *, budget_tokens: int, counter=None)`：围绕已计量模型提供调用前和最终输出复查，保持模型接口和流式输出形状。
- `MainAgent._request(build_messages, *, context: dict, stage: str, **kwargs)`：集中 main 请求构建，重建最新主摘要/历史；允许调用预算维护，其他子调用不做 compaction。
- 嵌套模型只包一次；会话 resume 解开包装取得 raw model，再按新会话绑定计量和预算；不得将日志继续写进旧会话。
- 统一 stop_reason=context_budget_exceeded、status=partial，CLI final 消息结束轮次；分段文案取 specs 原文。

- [ ] 依用户指示不写 RED/GREEN 超限测试，不用模拟超限执行或手工触发该场景。
- [ ] 接入配置、输入重建、流式最终输出检查。明确 main/child/summary 作用域，summary 不递归调用摘要；单个 current 上下文不能通过旧轮压缩解决时向上传播异常。
- [ ] 静态逐项审查所有 `except Exception` 和回复兜底：优先传播预算异常，轮次最外层保存已有结果并停止；不启动后续工具、补查或回复模型。保留已发生的请求和结果，CAS 冲突不重跑。
- [ ] 静态核对 input/output 计数不累加独立请求及重复流式快照；超限文案必须建议分段规划。只确认代码路径，不声称实测通过。
- [ ] 编译变更文件，执行现有非超限模型/CLI/遥测回归；新增用例不得专门验证超限停止。

```powershell
.\.venv\Scripts\python.exe -m compileall -q context agents cli.py config.py utils/response_renderer.py .claude/skills/query-info/script .claude/skills/memory-query/script .claude/skills/preference/script .claude/skills/ask-question/script
.\.venv\Scripts\python.exe -m pytest tests/test_v0_telemetry.py tests/test_workflow_telemetry.py tests/test_cli_session_resume.py tests/test_model_io.py tests/test_main_output.py -q
```

- [ ] 更新 3.1，记录静态审查证据及“按用户要求未对超限场景测试”，提交任务。

**完成条件:** 代码审查确认接线、终止传播、已有状态保存与确切文案；编译和非豁免回归通过。超限行为不附实测结论。

### Task 5: 整体验证、文档和归档

**OpenSpec mapping:** tasks 4.1—4.3；全部需求的证据核对。

**Files:** README、tasks.md、本计划实施记录；完成后 OpenSpec archive 的同步规格和归档目录。

- [ ] 更新 README，明确生产历史不按轮数截取、子调用临时上下文、token 估算方法、6000 初始值及完整轮次边界；记录用户豁免。
- [ ] 运行 Tasks 1—3 定向测试及项目相关既有全量回归（不新建超限专项测试）；读取真实输出。

```powershell
.\.venv\Scripts\python.exe -m pytest -q
openspec validate isolate-agent-context-token-compaction --strict --no-interactive
git diff --check
```

- [ ] 仅对新增/变更文件执行项目 ruff 检查，遇既有风格问题不做无关批量修正；发现新失败先定位，修复后复测。
- [ ] 单代理模式整体自审；多子代理模式按用户选择安排规格和代码质量评审。逐项将规格、任务、代码及测试结果对照；测试豁免明确记载，不能把全量通过表述为超限已覆盖。
- [ ] 查可用 verify 工作流。若没有，报告“未运行 OpenSpec verify，已人工逐项核对”；不得把 validate 当作实现 verify。
- [ ] 非豁免任务/检查全部满足条件后更新 4.1、4.2，执行 `openspec archive isolate-agent-context-token-compaction --yes`，检查主规格和归档状态；将完成记录纳入工作分支。
- [ ] 更新 4.3，读取 finishing-a-development-branch 技能，呈现合并/PR/保留分支等适用选项并执行用户选择。

## 实施记录

2026-10-11：只完成 OpenSpec 初始化、正式规划与技术自审。OpenSpec strict validate 通过。尚未修改产品代码，未执行产品测试；执行方式等待用户选择。无需再次需求探索，后续计划修改必须先协调正式 OpenSpec 文档。
2026-10-11：用户选择单代理。Task 1 RED 4 failed / GREEN 17 passed。使用短临时目录规避 Windows 路径长度限制；工作分支 codex/agent-context-token-compaction。
Task 2：RED 4 failed，GREEN 26 passed（compaction、memory_context、cli_session_resume）；切点只允许连续完整旧轮末，摘要后核算预算再写游标。
Task 3：RED 2 failed/1 passed，GREEN 85 passed/1 skipped。真实 main initialize/step/finalize 与主摘要输入不含工具原文标记；同实例第二次子调用不继承首任务，来源/选择回归通过。短目录全量复核 Task 1 时 652 passed/3 skipped。
