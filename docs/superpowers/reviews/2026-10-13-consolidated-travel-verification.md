# 主规划与程序编排整合验证

2026-10-13，单代理执行与整体自审；按用户要求在当前目录、codex/readme-agent-diagrams 分支实施。范围包含原立即项及原后续 AgentScope 移除、TripPlan 只读查询。

## 验证结果

最终全量 pytest 的实际结果见下方完成记录；基线为 737 passed、3 skipped。旧协议替换了 27 个绑定查询子 Agent/独立收尾模型的测试函数，其功能覆盖通过新入口测试与原事实/存储/供应商测试继续保留，没有用 skip/xfail 隐藏失败，详见 [测试迁移记录](../../development/2026-10-13-test-protocol-migration.md)。

新增 Python 文件完整 Ruff 检查通过；全仓 F821/F822/F823/F811 检查通过，git diff --check 通过。完整 Ruff 的历史债务仍存在：HEAD 镜像基线 1224 项、首次整体自审工作区 1167 项；未声称完整 Ruff 零错误。未在本变更中扩大到仓库全部格式修复。

OpenSpec strict validate 通过。已检查 CLI 帮助，当前没有 verify 命令；没有声称运行 OpenSpec verify。以下是人工规格、设计、任务和测试证据核对，validate 仅作文档结构检查。

## 规格与任务证据

| 能力 / OpenSpec task | 实现 | 主要验证 |
| --- | --- | --- |
| main-agent-preference-updates / 2.1 | memory_tools、long_term_memory 事务更新 | test_memory_tools、test_request_orchestration；原意、撤回、隔离、失败及规划前重读 |
| history-preference-analysis / 2.2 | preference_analysis、Runner 前置 | test_preference_analysis；三份唯一有效计划首次总结、完成标记、十份不重复、重开入口与实际 stage/turn 关联 |
| travel-policy-preflight / 2.3 | policy_preflight、workflow_guard | test_policy_preflight、test_policy_workflow_guards、test_enterprise_policy_repair；真实引用、单位、城市/日期、审批缺口、强制校验 |
| simplified-trip-state-machine / 3.6 | workflow_state、Store/Index | test_simplified_workflow_states、sqlite/travel_updates/matching；2/3/3 状态、派生等待、旧 querying 防重发及暂停语义 |
| priority-tool-scheduling / 3.1 | batch_execution、workflow_batches | test_direct_query_batch；事件屏障证明并行、依赖失败不越过、取消保留已完成结果，版本/额度/隔离回归 |
| bounded-travel-tool-results / 3.4 | result_views、ToolExecutor、context_projection | test_bounded_tool_results、workflow_queries/plan_guard；完整缓存、有界模型视图、真实地点时间证据 |
| saved-trip-amendment / 3.5 | workflow_amendments、Runner | test_saved_trip_amendment、test_amendment_recovery、workflow_runner/preflight；全缓存筛选、过期判断、组件保留、原方案保留、未知恢复零重放、目标刷新 |
| consolidated-travel-execution / 3.2 | Runner 全请求、Harness 转交、Main.select_batch | test_consolidated_travel_run、test_request_orchestration、test_main_batch_protocol；正常两次、独立查询一次、纯记忆零次、模板程序兜底 |
| 静默旧模型 / 3.3 | Runner 普通工具入口、评测/校准脚本迁移 | 禁止 Registry 加载的入口测试、旧 information 模型调用为零；源代码保留供未来设计 |
| project-model-runtime / 5.1 | runtime、openai_model、CLI/旧技能导入 | test_project_model_runtime、telemetry/model_io/CLI；片段合并、非法 JSON/中断不执行、usage、屏蔽 agentscope 导入的新进程启动 |
| saved-trip-read-only / 5.2 | workflow_reads、Runner._read_request | test_saved_trip_read_only、test_request_orchestration；12 条分 5/5/2、归属、版本、历史标识、零供应商/零业务写入 |
| agent-context-isolation / progressive-context-disclosure | 有界批量任务视图、普通记忆工具和只读入口 | test_agent_context_isolation、test_compact_context、test_main_batch_protocol 等既有上下文回归 |

## 回放与实际边界

[离线资源报告](../../evals/2026-10-13-consolidated-resource-calibration.json) 使用生产 MainAgent、Runner 和工具入口；普通旅游两次主模型，已有计划匹配合并进首次决策，中段冲突修复四次主模型，信息子模型零次。估算 context 最大 8370，不能当作供应商实际 token 花费，更不能把每次上下文估算相加当作总费用；报告的 suggested_limit 没有自动写入配置。

企业冲突测试检索上海 500 元/晚规则，第一次报价超标被程序过滤；主模型识别实际缺口，仅刷新酒店，火车不重查，取得合规报价后通过。政策提取一次，检索一次，火车两项、酒店两次查询；引用、原始检索审计和报价历史保留。测试数据明确模拟，不是线上采购报价。多城市企业资料夹具仍为静态扩展场景，未声称全部自动回放或在线验证。

自审修复：工具清洗保留校验所需 URL/时间/地点证据；批量修复后清除旧反馈；旧 querying 转 empty 保留开始身份；政策城市/生效日期延后核对；酒店关键词先筛完整缓存；暂存失败不覆盖原方案；指定刷新不扩大查询范围；所有政策/偏好模型用量关联实际 turn。保持 RAG 底层、供应商能力及失败策略的边界。

最后一轮规格核对新增四项状态/依赖/日期回归：旧 completed 证据失效降为 running 并记录重验；有效旧记录保留 completed；恢复时已完成的当前版本查询满足依赖且不重放；未知抵达日期不能生成酒店报价日期。新增失败测试确认三项缺口后修正。轻量索引不执行完整证据校验，61 项相关入口、状态、索引和工具回归通过。

## 完成记录

后续实际模型测试另见 [实测记录](../../evals/2026-10-13-live-policy-measurement/README.md)。该轮三次行程均被结构校验拒绝，暴露主规划/政策提取的输出契约缺口，尚未修复；本报告的 766 项结果是当时离线回归证据，不能解读为上述真实模型场景已通过。

最终全量 pytest：766 passed、3 skipped，55.57 秒；新增文件 Ruff、全仓 F821/F822/F823/F811、git diff --check 通过。三项 skip 为既有跳过项。OpenSpec strict validate 通过；人工规格核对完成。OpenSpec archive 已成功，归档为 2026-10-13-consolidate-travel-model-calls，正式规格新增32项、修改3项、删除0项。分支集成等待用户选择。工作未自动提交、推送或合并；当前目录的代码和文档可审阅。
