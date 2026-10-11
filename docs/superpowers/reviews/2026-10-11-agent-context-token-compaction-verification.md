# 上下文隔离与 token 压缩验证记录

正式依据：`openspec/changes/isolate-agent-context-token-compaction/`（归档后位于 `openspec/changes/archive/2026-10-11-isolate-agent-context-token-compaction/`）的 specs、design、tasks，以及 `docs/superpowers/plans/2026-10-11-agent-context-token-compaction.md`。本文件仅记录实施和验证证据，不另定义需求。

执行方式：用户选择单代理；当前代理实施并整体自审，未派独立评审子代理。工作分支 `codex/agent-context-token-compaction`，基线 `ad8a5b9`。

## 规格逐项核对

| OpenSpec requirement | 实现依据 | 实际验证 |
| --- | --- | --- |
| 子 Agent 每次委派使用独立临时上下文 | 信息 Agent 局部 messages；project_delegation_context；Harness/runner 显式任务资料，刷新已确认事实摘要 | reused_child_starts_fresh_messages_and_never_compacts、delegation_receives_task_facts_without_main_transcript；information_agent_loop 工具配对回归 |
| 主 Agent 接收可验证的子结果摘要 | context_projection 白名单；MainAgent 全部入口；workflow 当前任务候选视图；业务对象独立保留 | child_raw_tool_marker_absent_from_main_requests_and_summary、child_projection_preserves_candidate_ids_and_sources_without_mutation；sourced_itinerary/workflow_queries/travel_updates |
| 审计日志与模型上下文分离 | project_session_message 与主压缩投影；short_term 文本兼容入口也投影 | main_result_history_projects_reply_without_tool_payload；工具保存/未泄漏、pending 和 clear 回归 |
| token 阈值决定压缩时机 | count_input_tokens 覆盖实际 messages/tools；CLI BudgetedModel；MainAgent 动态构建请求；无生产轮数截断 | input_count_includes_instructions_business_and_tools、more_than_ten_small_turns_remain_visible、main_request_rebuilds_from_compacted_complete_old_history、compaction_count_includes_projected_business_summary |
| 只在完整用户轮次之间切分 | complete_prefix_boundaries 连续完整旧轮次；最新轮保护，未完成旧轮阻断 | active_turn_stages_never_form_session_cut、unfinished_old_turn_blocks_prefix、completed_turn_cut_keeps_recent_turn_intact |
| 摘要覆盖与恢复连续 | 覆盖后全部主消息；非空摘要与重算通过才写游标；旧 checkpoint 只读 | legacy_mid_turn_summary_keeps_uncovered_messages、summary_failure_keeps_coverage、compaction_persists_checkpoint_and_preserves_original_events；CLI resume 绑定回归 |
| 不可压缩当前上下文超限停止并建议分段 | 请求前/完整输出后检查；main/child/summary 作用域；typed exception 传播至轮边界；保存已有对象并落 final；停止后无回复/补查模型 | 按用户明确要求未编写或运行此场景专项测试、未手工触发。只做代码路径核对及 compileall，不能声称实测通过 |

## 命令和结果

- TDD：Task 1 RED 4 failed → GREEN 17 passed；Task 2 RED 4 failed → GREEN 26 passed；Task 3 RED 2 failed → GREEN 85 passed/1 skipped。后续业务摘要计数、兼容文本投影及聊天快照隔离也分别确认失败后修复。
- 最终全量：`.\.venv\Scripts\python.exe -m pytest -q --basetemp=.b` → **663 passed, 3 skipped, 1 warning**（51.69 秒）。警告来自既有 dashscope Assistants API 弃用；未使用实时外部供应商测评作为验收。
- Windows 默认长临时路径基线出现 16 个文件路径失败；使用项目内短临时目录后基线回归通过。不改产品存储逻辑来绕过测试目录长度问题。
- compileall：context、agents、CLI、config、renderer 和四个子脚本通过。
- OpenSpec strict validate：valid=true、issues=[]。
- 当前 OpenSpec CLI 未提供 verify 命令/工作流：**未运行 OpenSpec verify，已人工逐项核对**。validate 是文档结构验证，不能代替测试和实现规格核对。
- git diff --check 通过；分支整体差异使用 `git -c core.whitespace=cr-at-eol diff main --check`，保持既有技能脚本 CRLF 表示。
- 对所有变更 Python 文件执行项目 Ruff。新增模块/测试均无问题；旧文件仍有 **177 项既有风格问题**，对应基线180项，各文件无新增诊断。未做无关风格重写。详细对比保存在本任务本地 ledger 工作目录。

## 整体自审

完成 OpenSpec tasks 与实施计划的证据核对，读主请求、投影、完整轮次选择、日志/恢复及所有相关通用异常捕获路径。修复正常路径中的导入循环、子结果包装兼容、query_run 状态；最终自审修复预算遥测存储失败影响正常回复（RED→GREEN）和新查询错误继承旧结果元数据（RED→GREEN），40 项定向回归及最终全量通过。

子工具过程保持完整配对，实例重用不保留对话；最新已确认事实摘要是本次显式任务资料，不等同于继承上一子调用消息。主业务输出后需要压缩时复用已接收响应，不重发业务请求；摘要调用不递归摘要；预算停止异常不会进入模型修复或回复兜底。已发出的并发请求先收集完成结果，再停止下一批。

未发现尚未解决的重要规格偏差。独立评审未进行（用户选择整体自审）；模型估算误差、旧摘要不可逆及单轮超限测试豁免均已在正式设计中声明。

## 实施决定

- 沙箱不允许写入创建的 managed worktree，按技能回退在原项目独立分支实施；空 worktree 已归档。代价：未提交变更暂时共用原 checkout，已通过分批提交保存。
- Windows 使用 PowerShell/Python 保存同格式 ledger，不运行 bash 辅助脚本。代价：不生成辅助脚本的逐任务 brief；执行以原计划正文及 ledger 为准。
- 默认估算 ceil(UTF-8 JSON bytes/3)，明确 estimated=true；除以2的静态估算会令现有主指令本身超过6000初始值。代价：仍有模型相关估算误差，6000未校准，可配置并可注入兼容计数器。

