# SQLite 存储改造实施记录

计划：docs/superpowers/plans/2026-10-12-sqlite-agent-storage.md。规格：openspec/changes/archive/2026-10-12-sqlite-agent-storage。

用户选择：单代理、当前工作区；最终整体自审，不派子代理。

基线：2026-10-12 实际运行 `D:/python_3.11.9/python.exe -m pytest -q --basetemp=.test-tmp/sqlite-baseline-20261012 --tb=short`，690 passed、3 skipped；默认 TEMP 长路径引起的 16 项失败在短路径消失。

接口预检：任务 1→2 共用 SQLiteStorage 事务；2 不构造门面避免循环；1→3/4 共用独立连接；4→5 共用状态版本和范围过滤。无接口冲突。

执行决定：计划偏好测试引用了不存在的 test_user_preferences.py，改为现有 test_cli_onboarding.py、test_user_storage_isolation.py 和新增 SQLite 测试。单代理遵守用户 AGENTS.md 进行整体自审，取代 skill 默认独立 reviewer。Windows 使用本记录维护任务证据，取代依赖 shell 的辅助脚本；不改变 TDD 或验收。

## 任务证据

- 任务 1：6 项新测试先失败（尚无 SQLiteStorage），实现后 6 passed；发现 Windows 备份连接未关闭导致 replace 失败，显式 closing 后通过。
- 任务 2：6 项迁移测试先失败（尚无迁移模块），实现后与基础合计 12 passed；验证原字节、版本/覆盖序号、重复初始化、数据库 trigger 注入回滚、截断诊断与源变动回滚。
- 任务 3、4：新增业务测试复现小结失败仍更新行程、偏好旧副本覆盖、用户优先级失效和列表追加丢失；会话新测试复现整日志读取与跨进程重复 seq。实现及调用方接入后，基础/迁移/业务/会话/workflow 合计 34 passed。
- 任务 5：恢复测试用不可解码的旧覆盖和工具负载证明读取过滤生效；精简结果测试覆盖 completed/partial/needs_input，保留错误码；工具检查禁止全历史读取；摘要 await 中 clear 并验证返回的是最新状态。与既有压缩/上下文隔离/行程测试合计 50 passed。
- 兼容决定：精简最终事件额外保留 error_code/error/stop_reason，保持既有错误诊断用途；不保存完整执行或候选。状态 clear_context 在一个事务内获取清空序号并推进边界，避免跨操作读取序号的竞态。

## 最终自审和补充修复

按用户选择由当前代理对 schema、迁移、门面、上下文、CLI、评测和规格整体自审，没有派独立评审代理。

- 清空/删除后同名会话重建会复用版本：失败测试复现后改为数据库级 state_clock，保留计数和迁移标记。
- 旧 identity.json 仍影响已完成迁移用户：失败测试复现后仅迁移时验证来源，运行期验证数据库 owner/schema。
- 工具返回与 clear 并发仍可在存储层提交：失败测试复现后事务内检查 call_seq 与清空边界。
- 统计会读取完整已完成行程：失败测试使用不可解码 content_json 验证；改为小结/JSON 字段投影及 SQL COUNT，不解析事件审计。
- 小结 revision 与同行权威元数据不一致未拒绝：失败测试后查询同时校验 id/revision，并记录错误，显式重建。
- 旧目录历史清空标记与未补齐路线：失败测试后保留已确认归属的清空元数据，派生导出使用待补提示，迁移保留原行程不修改。
- SQLiteError 未进入原 OSError 行程错误分支：离线 Runtime 故障注入先失败，修复后返回 workflow_save_failed；CLI 数据库损坏恢复失败保留绑定。
- 早期计量无 turn_id：整合回归复现漏算，read_runs 增加运行序号区间，恢复原评测归属，不读其他轮次负载。

以上复核修复及迁移/计量重点合计 29 项测试通过；随后增加两个错误路径回归通过。最终完整运行：`D:/python_3.11.9/python.exe -m pytest -q --tb=short --basetemp=.test-tmp/sqlite-final`，**737 passed、3 skipped**，60.80 秒。相比基线新增 47 项存储/兼容回归；既有测试仅转换文件实现断言，保留各自行为验证。

## 规格与任务核对

当前 spec-driven schema 没有 verify 工作流；以下是人工一致性核对，不声称运行 OpenSpec verify。

| 需求/场景 | 实现 | 验证证据 |
| --- | --- | --- |
| 唯一权威源、用户隔离、不可用不 fallback | sqlite_storage、user_storage，各存储门面 | test_sqlite_storage、test_user_storage_isolation、test_database_unavailable_never_falls_back_to_legacy |
| 行程/小结共同提交、保存重启 | WorkflowStore 同行事务、WorkflowIndex | test_workflow_summary_failure_preserves_previous_revision、test_atomic_write_failure_keeps_original、test_restart_preserves_candidates_and_checkpoint |
| 旧版本写入拒绝、偏好字段/用户删除优先级 | workflow CAS、LongTermMemory 字段事务 | 两进程 CAS、两个实例偏好及列表追加、tombstone 测试 |
| 状态 patch、异步摘要竞争、同名会话重建 | SessionStore、MemoryManager、state_clock | test_state_patch_preserves_fields_and_rejects_stale_revision、两个 async clear 测试、clear/recreate 和 delete 状态版本测试 |
| 事件序号、幂等、工具配对同次提交 | sessions 计数、events/tool_calls/runs | test_append_is_idempotent_without_reading_history、test_process_append_has_unique_sequences、test_tool_pairing_is_atomic_and_filtered |
| 按需恢复、完整未摘要主消息、主上下文隔离 | _main_events SQL filter、短期投影、pending 索引 | test_restore_does_not_decode_covered_or_tool_payloads（保留 48 条）、test_tool_checks_do_not_read_history、既有 agent-context-isolation/compaction 回归 |
| 列表元数据、指定运行计量 | sessions 元数据、read_runs 条件/序号区间 | test_run_queries_and_metadata_do_not_decode_unrelated_records、test_eval_only_counts_model_calls_for_selected_turn |
| 一次只读迁移、完成共同提交、可重试 | storage_migration | 原字节/版本/覆盖、数据库 trigger 回滚、来源变化、JSONL 截断及完整行损坏、两个进程首次迁移测试 |
| 已清空来源不复活、候选/断点/工具/观测保留 | migration manifests、profile/history 标记、统一历史 | 旧目录 clear 标记、待完成工具与 run_seq=9→10、active 行程身份、统一历史及清空重启回归 |
| 新最终事件精简、旧完整事件投影、完整返回不变 | project_session_result、CLI 保存调用 | completed/partial/needs_input 投影和真实 CLI 测试，旧事件同投影，不变 last_result 和权威行程 |
| 小结先定位、仅恢复选中内容、显式修复 | WorkflowIndex SQL summary_json、Harness 现有版本检查 | 不可解码 content_json 下小结仍可查询、test_resume_loads_only_selected_workflow_after_summary_selection、显式修复/身份版本异常测试 |
| 派生导出、备份、接入 | TripMemory best-effort、backup_to、README/忽略规则 | 导出故障仍保留提交、一致备份 integrity_check、CLI/session/评测全回归 |

源码编译、Git diff 空白检查及本变更严格 OpenSpec validate 已运行；新存储与上下文投影模块 Ruff 检查通过。全范围 Ruff 仍有既有 CLI、Harness、telemetry 和评测脚本诊断，导出 HEAD 基线逐文件/规则比较后修正本次新增诊断，不将全项目 lint 描述为通过。未调用真实供应商或模型；遵守原专项测试豁免。RAG 代码、AgentScope 移除及用户行程详情查询不在本次实现范围。

所有产品任务已具备代码与测试证据；归档与分支收尾另记录实际结果。

最终检查输出：源码 compileall exit 0；存储模块/上下文投影 Ruff `All checks passed!`；git diff --check exit 0；OpenSpec change 严格校验 valid；数据库、journal 和备份样例均被 git check-ignore 命中。全范围 Ruff 为 261 项已有诊断，与导出的 HEAD 基线按文件/规则比较，没有增加诊断计数。

合成大小样例（非生产性能指标）：同一回复附带 500 个候选的完整结果为 **533,397 字节**，精简最终事件内容为 **219 字节**。EXPLAIN QUERY PLAN 实测：主消息区间使用 events_context 索引，指定轮次模型调用使用 runs_turn 索引。追加禁止读历史与恢复不可解码旧负载测试证明行为；没有把 SQLite 索引等同于行程小结。

## 归档与分支收尾

已执行 OpenSpec archive，归档至 openspec/changes/archive/2026-10-12-sqlite-agent-storage，同步正式规格。随后 openspec validate --all --strict：7 项通过、0 项失败。按用户选择发布 codex/readme-agent-diagrams，并创建指向 main 的 [PR #11](https://github.com/yzy703050-oss/trip-evidence-agent/pull/11)。命令行 GitHub 连接失败后改用连接器，快进更新分支；远端代码文件树 94f6c261bded0db187c2e417d92e416a93d3014f 与本地已测试提交完全一致。本次补记仅修改三个流程文档，不改变产品代码。
