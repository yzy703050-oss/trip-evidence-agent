# SQLite 存储变更技术复核

依据：openspec/changes/archive/2026-10-12-sqlite-agent-storage/proposal.md、design.md、tasks.md 及三份 specs。此记录展开实现风险，不定义第二套需求。

已核对 SessionStore、WorkflowStore、WorkflowIndex、LongTermMemory、MemoryManager、user_storage、context_projection、CLI、ExecutionHarness、评测读取入口，以及会话、用户隔离、统一历史、小结索引相关测试。

| 问题 | 设计决定与实施约束 | 验证归属 |
| --- | --- | --- |
| 哈希路径解析函数会复制旧文件；JSONL 读取函数会修复尾行 | 数据库路径直接使用现有身份哈希；迁移独立只读解析，不复用有写副作用的旧加载入口 | 计划任务 1、2 |
| 未哈希目录可能含其他用户的日志 | 不由目录名推断归属；无法确认的旧会话跳过，已确认来源损坏报错；已迁移数据库身份不匹配立即拒绝 | 任务 1、2 |
| 导入期间使用门面可能再次初始化迁移，产生循环依赖或多次提交 | 迁移通过原始 SQL 写入一个事务，纯小结/数据转换函数不创建存储实例；完成标记最后写入共同提交 | 任务 2 |
| 行程数据很大而小结需要查询 | 同行 content_json 与 summary_json，仅定位查询 SELECT summary_json；显式重建才加载全量内容 | 任务 3 |
| 不同偏好同时保存、同一列表追加都会丢更新 | 字段 upsert 与列表追加在读最新值的事务中完成；用户覆盖及删除标记保持现有含义 | 任务 3 |
| 会话摘要模型调用期间可能发生 clear | read_state 返回状态版本；短事务 patch；摘要带 expected_revision 写入，冲突不得使用旧结果恢复历史 | 任务 4、5 |
| 工具调用查重和结果配对目前读全日志 | append 与 tool_calls 表共同提交；数据库约束及事务内验证，不能仅依赖 MemoryManager 预检查 | 任务 4 |
| CLI 保存完整结果；内存启动读全部原始事件 | 精简事件保存保持完整返回对象；恢复 SQL 同时限制 seq、scope、type，再做既有投影，不能用数量 limit 截断未摘要消息 | 任务 5 |
| 老测试直接改文件或在初始化后才放迁移源 | 迁移夹具在首次打开前创建；业务断言通过门面或故障注入，保留原隔离/截断/回滚场景而非删除测试 | 任务 2、6 |
| 清空历史/删除后保留原文件会复活数据 | 保留最小迁移完成元数据；清空历史继续保留偏好与现有行程历史过滤语义 | 任务 3、6 |
| 派生 trip.md 写入失败影响权威保存 | 事务提交后 best-effort 导出；恢复和小结查询不依赖文件 | 任务 3、6 |

边界已明确：不调整 RAG，不移除 AgentScope，不增加面向用户的 TripPlan 详情查询；这些仍是独立后续事项。SQLite 使用 Python 标准库，DELETE journal、FULL synchronous、5 秒锁等待，连接不跨线程共享；不跨模型/供应商调用持有事务。

复核阶段补充了原有所有权规则在损坏来源判断时的适用范围，没有改变用户可见验收标准；没有重复进行 brainstorming。用户随后选择单代理、当前工作区执行。实施后自审、补充设计决定及测试证据见 docs/development/2026-10-12-sqlite-storage-validation.md。
