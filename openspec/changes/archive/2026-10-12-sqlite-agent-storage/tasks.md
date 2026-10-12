## 1. 技术复核与实施计划

- [x] 1.1 审阅 proposal、三份 specs 和 design，对照现有存储接口、测试与迁移来源进行技术复核；证据：docs/superpowers/reviews/2026-10-12-sqlite-storage-technical-review.md。
- [x] 1.2 在设计确认后使用 writing-plans 生成详细实施计划；证据：docs/superpowers/plans/2026-10-12-sqlite-agent-storage.md；用户选择单代理、当前工作区。

## 2. 数据库基础与旧数据迁移

- [x] 2.1 建立 SQLite 连接、schema、用户归属、短事务、锁超时和 schema_version 管理；证据：test_sqlite_storage.py，6 passed，后续进程竞争见存储门面回归。
- [x] 2.2 实现源文件只读校验、画像/事件/状态/观测/行程/历史导入及共同提交完成标记；证据：test_sqlite_migration.py，6 passed；整合迁移夹具回归将在 5.2 核对。

## 3. 权威业务存储与状态一致性

- [x] 3.1 将 WorkflowStore 和小结查询切换到同行 JSON 与小结字段；证据：test_sqlite_memory.py、test_workflow_store.py，小结故障回滚、进程 CAS 与仅查询小结通过。
- [x] 3.2 将画像、偏好和历史门面切换到数据库；证据：test_sqlite_memory.py，并行字段/列表、用户 tombstone、清空/删除不重导通过。
- [x] 3.3 实现会话字段更新和异步摘要版本检查；证据：test_sqlite_sessions.py、test_sqlite_memory.py，字段保留与摘要 clear 竞争通过。

## 4. 事件、上下文与运行观测

- [x] 4.1 实现事件/观测直接插入、序号与幂等约束、工具调用配对和会话元数据；证据：test_sqlite_sessions.py，跨进程序号、配对事务、禁止历史读取与过滤查询通过。
- [x] 4.2 精简 CLI 新最终事件；证据：test_session_result_projection.py，三种状态、错误诊断、不变完整返回、旧事件投影一致，500 候选示例小于原大小十分之一。
- [x] 4.3 改造 MemoryManager 的启动、上下文压缩、工具检查、跨会话摘要与统计路径；证据：test_sqlite_memory.py 及既有上下文隔离/压缩测试，过滤旧负载并保留全部 48 条未摘要主消息通过。

## 5. 接入与整体验证

- [x] 5.1 更新 CLI、评测/校准脚本、trip.md 派生导出、忽略规则及 README；提供一致备份维护接口，验证导出失败不推翻数据库提交，数据库及其临时文件不进入 Git。
- [x] 5.2 调整旧文件实现断言并保留迁移夹具，按计划完成离线存储和业务回归、必要静态检查及跨模块代码自审；提交测试输出和各需求场景的覆盖证据，不触发真实供应商或用户豁免的超预算专项场景。
- [x] 5.3 核对所有任务的代码/测试证据和全部 specs 一致性，记录事件大小、追加及恢复读取行为；当前 schema 无 verify 工作流，人工逐项核对并运行 OpenSpec validate，不冒称运行 verify。

## 6. 归档与分支收尾

- [ ] 6.1 仅在任务、测试、评审和规格核对完成后执行 OpenSpec archive，同步正式规格并确认结果在工作分支；依用户选择执行分支收尾。

实施/测试/人工规格核对证据：docs/development/2026-10-12-sqlite-storage-validation.md；完整离线回归 737 passed、3 skipped。OpenSpec archive 已执行并同步正式规格；用户已选择推送当前分支并创建 PR，分支收尾待实际执行。
