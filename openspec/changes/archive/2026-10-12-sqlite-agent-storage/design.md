## Context

动机及范围见 proposal.md；行为依据为本变更三份 specs。现有 WorkflowStore 的文件锁与 expected_revision 保护有效，应保留其语义；SessionStore 每次追加全量读 JSONL，MemoryManager 还重复扫描事件/工具配对；LongTermMemory 从内存旧画像整份覆盖。CLI 将含完整 workflow 的结果保存为最终事件。测试大量依赖路径和猴子补丁，因此必须区分业务断言与旧存储实现断言。

用户已选择数据库方向；本次以本地 Python 3.11 CLI 为约束。默认首次使用导入当前用户数据、保留源文件；不要求用户预先学习或部署数据库。OpenSpec propose 已包含探索，不再重复需求访谈；设计提交审阅后再进入技术复核与 writing-plans。

## Goals / Non-Goals

**Goals:** 明确权威源及共同提交边界，安全迁移，降低日志和上下文读放大，修复偏好及状态旧副本覆盖，保留既有行为和验证依据。

**Non-Goals:** 不重写 Agent 内核，不移除 AgentScope，不引入 ORM/Redis/PostgreSQL，不新增主模型详情读取动作，不拆分全部候选成关系表，不清理原始旧文件，不变更 RAG 实现、供应商策略或用户的超预算专项测试豁免。

## Decisions

### 1. 每用户一个 SQLite 数据库，保留现有存储门面

路径为 storage_root/<现有 user_storage_key>/agent.sqlite3，使用标准库 sqlite3。数据库 metadata 记录完整 user_id 与 schema_version，所有打开操作验证归属；同名用户大小写仍由现有哈希身份隔离。旧文件目录仅为迁移输入或显式导出目的地。

新增 context/sqlite_storage.py（连接、事务、schema 和用户身份）、context/storage_migration.py（只读源解析、校验、导入）。SessionStore、WorkflowStore、LongTermMemory 保持调用方所需 API 和返回结构，内部改用数据库；旧 events_path/profile_path 等不模拟可写镜像，改造调用方和测试。

短操作使用独立连接，显式 BEGIN IMMEDIATE / COMMIT / ROLLBACK，不共享跨线程 connection；开启 foreign_keys，timeout 为 5 秒，synchronous=FULL。初版沿用 SQLite 默认 DELETE 回滚日志模式，符合低并发本地场景；不引入 WAL 文件管理及 SQLite 版本相关风险。不得跨 await 或外部模型/供应商调用持有写事务。重试只针对明确未提交的数据库锁竞争，不重放业务查询。

替代方案：单一共享数据库便于全局统计，但扩大跨用户读写风险；文件锁补丁仍需自行协调跨文件事务；ORM 对当前门面迁移没有必要。采用每用户数据库和参数化 SQL。

### 2. 结构化查询字段与 JSON 业务内容并存

初版 schema 的职责如下，详细 DDL 在实施计划明确：

| 表 | 主要内容与约束 |
|---|---|
| metadata | owner、schema_version、迁移完成及清空标记 |
| profiles | 画像及统计补充字段；按字段更新，计数原子递增 |
| preferences | preference_type 主键，value_json、source、deleted；保留用户优先级 |
| sessions | session_id 主键，state_json、state_revision、事件及运行计数、标题和时间 |
| events | session_id + seq 主键，event_id 条件唯一，turn_id/scope/type/role、payload_json |
| tool_calls | session_id + call_id 唯一，关联调用/返回事件，按需查询未配对调用 |
| runs | session_id + run_seq 主键，turn_id、类型、task_id 等观测定位字段和 payload_json |
| trip_plans | id 主键，revision/status/created_at/updated_at、summary_json、content_json |
| travel_history | 兼容历史记录身份、版本、时间和 history_json |
| migration_sources | 来源路径/校验值与导入信息，和迁移完成标记共同提交 |

trip_plans 的 summary_json 和 content_json 同行保存，避免独立小结表同步。查询小结的 SELECT 不携带 content_json；候选、草稿、版本历史继续保存在完整 JSON 内，保留现有 query_views 等行为。为 sessions 更新时间、events 主消息序号、runs 轮次及 trip_plans 状态/更新时间建立必要索引。不要把数据库内部索引与应用的小结字段混为一谈。

### 3. 事务、版本与状态更新

WorkflowStore.save 在事务内比较 expected_revision，更新 revision 后一次保存完整内容和投影小结；新建重复 ID 或旧版本产生 WorkflowConflictError。小结构造/写入出错即回滚；只有 commit 成功才返回新版本。保留恢复前的小结版本、checkpoint 和任务/候选版本校验。

偏好 UPSERT 在事务内读取现存来源与删除标记，不使用实例 self.data 覆盖最新全部偏好。会话提供 update_state(patch, remove_keys=(), expected_revision=None) 替代业务路径的 read/整份 write；摘要写回携带生成前 state_revision，不通过则丢弃或重新计算，不推进 covered_through_seq。active_workflow_ids 的追加合并也在事务内完成。保留只读快照供调用方使用，消除显式依赖 store._lock 的协调方式。

事件序号和 run_seq 从 sessions 计数器在插入事务内分配，事件 ID 查重使用唯一索引，重复请求返回已保存事件。工具配对的唯一 ID 与返回关系同次更新，不因崩溃形成半配对。清空历史/上下文改用数据库中的删除或可见边界与状态更新；用户已迁移标记不随业务清空移除，避免源文件复活。delete_all 清空所有用户业务表并保留最小迁移禁重导标记，原始迁移源保留；不声称完成磁盘安全擦除。

### 4. 最终事件精简与有界读取

新增会话结果投影函数，不原地修改结果。最终事件保存 final_answer、status、finalization_method、workflow_id/workflow_revision 及现有 project_session_message 所需 travel_conditions/itinerary；仅从既有输出抽取必要摘要，排除 workflow、validated_plan、results、domain_results、candidate_cache 等完整执行负载。metadata 保持 main_result 兼容读取。工具审计和运行记录仍按各自 scope 存储，不增加完整 workflow 审计副本。

SessionStore.read_events 支持 after_seq/scope/type/role 等条件，read_runs 支持 turn_id 等过滤；完整导出兼容调用仍可显式读取全部。MemoryManager 启动和模型上下文仅获取覆盖边界后的主消息；原始工具事件不进入 ShortTermMemory。工具 ID 检查与 pending 调用用 tool_calls 表查询；跨会话摘要读取新增主消息区间；统计和会话列表从计数/元数据获得。不得以固定 limit 删除或截断未摘要主消息。

### 5. 迁移与派生文件

每个用户首次访问先读源清单，按既有身份规则确认归属；哈希目录中的权威文件优先，旧 user_id 目录和 <user_id>.json 仅补充可证明归属、未被新版数据覆盖的内容。识别既有 imported_sources、legacy_chat_cleared、cleared_before，延续旧历史去重和清空语义。

数据库初始化 schema 后，导入用户业务记录、会话序号、工具索引、画像、历史和行程，并重新计算小结；完成标记与导入使用同一事务。迁移前后比较源内容校验值，发现源变化则回滚并报告需在源写入停止时重试。JSONL 末尾不完整记录只在导入中排除并记录诊断，不调用会修改旧文件的 _read_jsonl；完整行损坏或身份冲突必须报告，不能带完成标记跳过整份数据。上述严格校验针对已经确认归属的来源：未哈希目录中无法证明属于当前用户的会话沿用既有跳过规则，不能仅凭目录名认领；哈希目录及其身份文件确定归属后，损坏不能作为“无数据”忽略。

源文件保持原字节；已有旧最终结果原样导入，以保证原审计保留，新的最终事件才开始精简。旧索引和 trip.md 不导入为权威源。迁移成功后不会自动重新导入变化的旧文件，禁止双写。trip.md 保留为可显式重建的数据库投影；写入失败只影响可读导出，不使已提交数据库行程变成“未保存”。运行期的小结不依赖 trip.md。

## Risks / Trade-offs

- 大型 TripPlan 单次更新仍重写 JSON → 本次避免过度拆表，主要解决事务及读放大；基准分别记录 SQL 访问和 JSON 序列化，不承诺整个行程写入变为常数成本。
- SQLite 同时只有一个写入者 → 短事务、5 秒锁超时和明确错误，多服务器服务化另选数据库。
- 原有测试检查文件实现细节 → 保存迁移夹具测试，业务测试改用 API，不删除有意义的隔离、竞争、损坏和恢复断言。
- 迁移源仍可能含敏感数据和旧重复快照 → 保留是安全迁移约束，忽略文件和文档说明数据库及源保留边界，后续独立评估清理；不自动删源。
- 多轮修改仍增长查询 history 和审计 → 这是保留证据语义，本次仅取消新最终事件中的完整 TripPlan 副本。

## Migration Plan

1. 开发及测试均使用临时用户数据库，先完成失败测试与相关回归；不以真实用户数据作为测试夹具。
2. 验证只读迁移、重复启动、注入回滚、源变化、完整行损坏、尾行截断和跨进程竞争；核对身份、版本、候选、覆盖边界及统计。
3. 新入口首次访问当前用户时迁移，其他用户按各自首次访问处理；明确输出成功/失败信息，失败不启动业务执行。
4. 保留原文件用于比对；数据库上线后新写入只进数据库。人工回退到旧程序只能得到迁移前数据；若需保留后续修改，必须停止写入并通过显式一致导出转换，禁止自动切换后端。
5. 用 sqlite3 backup API 生成一致数据库备份，提供存储维护接口，避免在有写入时简单复制文件；更高 schema_version 的库拒绝由旧代码写入。

## Technical review

实施复核补充：状态版本由 metadata.state_clock 在同一事务内分配，创建会话和更新状态都推进；清空/删除保留此计数，避免同名会话版本复用而接纳旧异步写入。read_runs 同时支持 after_run_seq/through_run_seq 区间；早期无 turn_id 的计量沿用已完成运行的序号区间归属。精简最终事件保留 error_code/error/stop_reason 以延续原错误诊断。迁移完成后不再依赖旧 identity.json，运行期只验证数据库 owner/schema。旧目录历史清空元数据仅在所属画像已确认时接受。SQLiteError 接入原有 CLI 恢复和行程保存错误分支，不重放外部查询。

技术复核记录见 docs/superpowers/reviews/2026-10-12-sqlite-storage-technical-review.md；实施细节见 docs/superpowers/plans/2026-10-12-sqlite-agent-storage.md。用户已确认本方案开始修改；执行方式在计划交付后由用户选择。
