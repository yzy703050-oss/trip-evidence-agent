## Context

行为以 proposal.md、specs/ 为准。技术复核依据 CLI、MemoryManager、compaction、MainAgent、Harness、WorkflowRunner 和子 Agent 脚本，无需重复需求探索。现有生产输入按 5/10 轮截断；摘要读取所有 scope 并允许轮内切点；子 Agent 已有局部 messages，但完整结果 JSON 可重新进入主输入。多个通用异常捕获可能吞掉预算停止信号。

## Goals / Non-Goals

**Goals:** 区分主历史、子临时对话和完整业务依据；统一预算检查；兼容旧记录；超限建议分段。

**Non-Goals:** 不增加子对话持久化或摘要，不自动拆旅程，不修改提供商或候选校验规则。

## Decisions

### D1 — 主结果摘要投影

新增 `agents/context_projection.py`，用字段白名单统一子结果、工作流和历史最终回复投影。主模型收到结论、状态、缺口、必要候选事实及稳定来源引用；不携带工具原文、全部缓存、完整 execution/history。完整 RunState、workflow 和 API 数据保留供确定性校验和答复渲染。只传自由文本会丢失候选身份，因此采用结构化摘要。

### D2 — 子上下文限于一次委派

信息子 Agent 保留局部 messages 与工具配对；其他子 Agent 同样只组装本次请求。复用 Agent 实例/客户端/提供商缓存不复用对话。工具审计仍按 agent scope 保存，不创建子 Agent state、摘要游标或 compaction。不同请求累计 token 费用不计作主上下文增长。
委派不复制主 recent_messages/history_summary 聊天快照；由当前任务、改写需求、偏好与明确业务资料提供必要事实。已确认事实的 session_summary 可作为任务摘要显式传入，并从 MemoryManager 取压缩后的最新值，避免旧快照或上一子委派对话进入新调用。

### D3 — 完整轮次、连续主历史

既有 turn_id 标识用户轮次，最终 assistant 消息落盘后完成；摘要只读主会话投影。选取连续已完成旧轮次前缀，遇未完成轮即阻断；活动轮的阶段/工具结果不可切分。生产路径移除 max_turns、n_turns、keep_recent_turns 限制，人工展示接口的显式轮数查询可保留。
MemoryManager 返回摘要边界之后的全部主消息。旧轮内 checkpoint 只读兼容，剩余同轮消息保留；新写入只使用轮末边界。摘要先投影后计数，生成后重计数；必要时以更长完整前缀有界重试，失败不推进游标、不丢历史。

### D4 — 统一预算入口

`config.py` 显式配置 `memory_context_budget_tokens`，默认 6000（沿用初始值，尚未校准）。新增 `context/context_budget.py`，提供可注入 TokenCounter、输入计数和 ContextBudgetExceeded。预算覆盖实际消息、指令、业务投影和 tools，不把输出上限 8192 当上下文窗口。
计数器接口允许模型兼容 tokenizer；初始离线默认使用保守 UTF-8 估算，记录 estimated 与方法名，使用实际 usage 校准，不宣称精确 token 或绝对硬窗口保障。不在业务请求时下载分词器或调用网络预估。
默认按规范 JSON 的 UTF-8 字节数除以 3 向上取整；没有校准的模型 tokenizer 时仅作为估算。静态核对发现除以 2 会使现有主指令本身超过 6000 初始值，因此不采用该比例。模型 usage 与预算估算各自记录。
MainAgent 集中请求构建：组装真实提示词，若超预算则仅压缩完整旧主轮次、重建输入并复查。子 Agent 只检查，不压缩。实际 API 包装提供调用前最终检查和完整输出后的复查；流式快照只计最终完整输出，不累加快照。不同调用不累计输入费用作为上下文。
显式区分 main/child/summary 作用域；维护摘要调用不递归触发摘要，仍检查模型硬输入限制。摘要尝试次数有界；不能达到预算时保留原边界并结束本轮。

### D5 — 终止信号贯穿执行链

ContextBudgetExceeded 在子/主 Agent、Harness、runner 的通用捕获前传播，在用户轮边界转为 `status=partial`、`stop_reason=context_budget_exceeded`，保存已有状态，CLI 落盘 final 消息。query_run 记录明确原因。禁止超限后的补查、重试及自然回复模型调用；停止不计作供应商错误。
提示文案严格使用 specs 的分段建议。输出已发生不能撤回；完成消费后停止后续执行。并发工具已发出的请求保留其结果，不再启动下一批。状态保存冲突遵守现有 CAS 规则，不重放外部请求。

### D6 — 用户指定测试豁免

不编写或运行单轮超限停止及分段提示的专项测试，也不以其他测试名变相覆盖。以代码审查、调用链核对和静态检查确认接线；交付注明该场景未实测。隔离、计数、完整旧轮次压缩和恢复仍执行 TDD。

## Risks / Trade-offs

- token 估算误差 → 明确方法及实际 usage，6000 可调整，不宣称最优。
- 摘要缺少选择依据 → 保留必要候选事实和引用，回归校验来源与行程。
- 未完成旧轮阻断前缀 → 不跳过或丢弃，按不可压缩规则停止。
- 旧摘要不可逆 → 兼容保存，不声称历史工具细节已完全净化。
- 通用异常兜底继续调用 → 预算异常明确传播，静态逐项审查捕获点。
- 超限测试获用户豁免 → 限制验证结论，不声称此场景已通过测试。

## Migration Plan

保持用户/session 隔离及 events/runs/state 兼容，仅追加预算遥测字段。旧 checkpoint 只读，新摘要写轮末边界。探索草案改为本 change 引用，正式行为只在 specs 定义。先规划审阅与执行方式选择，再 apply。

初始化采用 tools=none，只创建 OpenSpec 文档结构；artifact 指导通过 instructions CLI 读取。当前 CLI 没有 verify 子命令；实施后若仍无 verify 工作流则人工核对并明确报告未运行 verify。validate 只验证文档结构，不能代替实现验证或测试。
