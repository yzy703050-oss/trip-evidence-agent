# SQLite Agent Storage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: 根据用户选择使用 superpowers:executing-plans 或 superpowers:subagent-driven-development，逐项实施。不得默认派子代理。步骤使用 checkbox 跟踪。

**Goal:** 用 SQLite 修复存储一致性、重复最终结果和历史全量扫描，同时安全迁移用户已有数据。

**Architecture:** 每用户一个数据库作为唯一运行期权威源，现有门面保留调用契约。完整 TripPlan 与小结同行提交；事件只追加，状态和偏好按字段事务更新；短期记忆从按需读取的记录投影组装。迁移独立只读解析来源并一次事务导入。

**Tech Stack:** Python 3.11、标准库 sqlite3、pytest、现有离线供应商与模型夹具。

**Spec:** openspec/changes/archive/2026-10-12-sqlite-agent-storage/{proposal.md,design.md,tasks.md,specs/transactional-agent-storage/spec.md,specs/workflow-summary-discovery/spec.md,specs/agent-context-isolation/spec.md}。技术复核：docs/superpowers/reviews/2026-10-12-sqlite-storage-technical-review.md。

## Global Constraints

- 数据库路径 storage_root/<现有 user_storage_key>/agent.sqlite3；metadata 校验完整 user_id 和 schema_version。
- journal_mode DELETE、synchronous FULL、foreign_keys ON、timeout 5 秒；每次操作独立连接，BEGIN IMMEDIATE 写事务。
- 不跨 await、模型调用或供应商请求持有事务；禁止运行期旧文件双写和静默 fallback。
- 迁移源保持原字节；完成标记与业务记录共同提交；清空后不重新导入。
- 保留全部有效候选、版本、断点、历史、工具配对、完整未摘要主消息；不改模型动作或供应商调用语义。
- 不触及独立 RAG、移除 AgentScope或新增用户详情查询。遵守已有超预算专项场景的测试豁免；正常上下文测试继续运行。
- 先失败测试、确认失败原因、最小实现、复测整理；每个交付任务维护 OpenSpec tasks 和本计划证据。提交仅包含本任务文件，工作区用户改动不顺带提交。

## Review Focus

1. Unicode/大小写用户和同名会话：绝不跨用户；数据库身份不符拒绝。任务 1、2 测试。
2. 已确认归属来源的损坏、末尾截断、导入中断和源变动：原文件不改，失败无完成标记；无法归属旧会话仍不认领。任务 2 测试。
3. 两个进程基于同一行程版本保存及同时追加事件：CAS 只成功一次，事件序号唯一，失败事务不留半数据。任务 3、4 测试。
4. 摘要 await 期间清空及并行更新不同状态字段：旧摘要拒绝，无关字段保留，历史不复活。任务 4、5 测试。
5. 大历史夹杂工具审计和完整旧结果：插入、会话列表和恢复不解码无关旧记录，未摘要主消息不截断。任务 4、5 测试。

## Files and dependencies

- 新建 context/sqlite_storage.py：连接、schema、事务、备份和维护接口；context/storage_migration.py：源读取、所有权、转换及原子导入。
- 改造 context/{session_store,workflow_store,workflow_index,long_term_memory,memory_manager,user_storage,trip_memory}.py：保留门面职责；user_storage 只保留需要的身份/兼容帮助函数；旧文件 API 不作为数据库镜像。
- 改造 agents/context_projection.py、agents/execution_harness.py、cli.py：精简最终事件、状态 patch、按需上下文。
- 接入 evals/{run_preflight_simulated.py,main_agent/multi_destination_runner.py,v0_memory/runner.py} 及 utils 中实际调用 read_runs 的校准入口，README.md、.gitignore、本变更文档。
- 新建 tests/test_sqlite_storage.py、test_sqlite_migration.py、test_sqlite_sessions.py、test_sqlite_memory.py、test_session_result_projection.py；调整现有存储/CLI/历史/小结测试中旧文件实现断言。
- 顺序：任务 1 → 2 → 3 → 4 → 5 → 6。任务 3、4 可在多子代理方式下独立实施，但共享门面文件发生冲突时串行；迁移只使用任务 1 的低层接口避免构造门面循环。

## Task 1: 数据库基础与维护边界

**Maps:** OpenSpec 2.1、5.1；统一权威存储与用户隔离。

**Files:** 新建 context/sqlite_storage.py、tests/test_sqlite_storage.py；修改 context/user_storage.py 的无副作用路径帮助接口。

**Interfaces:**
- SQLiteStorage(root: str | Path, user_id: str)，属性 path: Path、user_id: str。
- connection() -> ContextManager[sqlite3.Connection]：返回 Row 工厂、只供本操作使用的连接，关闭前验证身份/schema。
- transaction() -> ContextManager[sqlite3.Connection]：BEGIN IMMEDIATE，成功 commit，异常 rollback；异常原样传递。
- ensure_ready() -> None：schema 准备后调用任务 2 migrate_user，完成才允许门面业务操作。迁移使用已经打开的 storage，不再构造门面。
- backup_to(destination: str | Path) -> Path：使用 backup API，拒绝源目标同一文件；备份失败不替换已有备份。
- StateConflictError(ValueError)：状态 CAS 失败供任务 4、5 使用。

- [x] 写失败测试 test_transaction_rolls_back、test_database_rejects_foreign_owner_and_newer_schema、test_connections_are_independent、test_backup_restores_committed_records。断言回滚后 SELECT COUNT(*) = 0、错误操作未修改 metadata、两个线程连接身份不同、备份可读同样记录。
- [x] 运行 `python -m pytest tests/test_sqlite_storage.py -q`，确认失败来自缺少实现，不是环境。
- [x] 建立 design 表格定义的 metadata/profiles/preferences/sessions/events/tool_calls/runs/trip_plans/travel_history/migration_sources 表。sessions 保存 last_seq/last_run_seq/state_revision/title/updated_at；事件索引含 (session_id,scope,type,seq)、(session_id,event_id) 唯一；观测含 (session_id,turn_id,type,run_seq)；工具主键 (session_id,call_id)。trip_plans 同行 content_json/summary_json。DDL、owner/schema 初始化同事务，拒绝未知版本；表之间外键明确级联清空。
- [x] 实现以上接口，备份通过同目录临时文件完成后 replace，finally 清理自己的临时文件。
- [x] 复测任务测试，检查 PRAGMA 和数据库实际路径；完成证据记入 OpenSpec 2.1，备份证据归 5.1。

## Task 2: 只读、可重试的一次迁移

**Maps:** OpenSpec 2.2；非破坏性且可重复的旧数据迁移、原始证据与兼容行为。

**Files:** 新建 context/storage_migration.py、tests/test_sqlite_migration.py；复用 workflow_summary 纯函数；迁移测试夹具参考 test_user_storage_isolation.py、test_v0_session_store.py、test_unified_travel_history.py。

**Interfaces:**
- migrate_user(storage: SQLiteStorage) -> None：完成用户首次导入，已完成直接返回，不重新读取旧来源。
- read_legacy_jsonl(path: Path) -> tuple[list[dict], list[str]]：只读返回完整前缀和截断诊断；完整行损坏报含文件/行号 ValueError。
- 收集规范哈希目录 profile.json、workflows/*.json、sessions/*/{events.jsonl,state.json,runs.jsonl}、trips.json，以及所有权确认的旧用户目录和 <user_id>.json；hash/manifest 写 migration_sources。

- [x] 写失败测试 test_import_preserves_bytes_and_identity、test_repeat_start_does_not_reimport、test_import_failure_rolls_back_all_and_retries、test_truncated_tail_readonly_but_complete_corruption_fails、test_source_change_aborts、test_unowned_legacy_session_is_not_claimed。断言来源字节完全相同、workflow revision/候选/seq/summary 覆盖值不变、重启不增记录、失败 metadata 无完成标记且所有业务表无部分导入。
- [x] 运行 `python -m pytest tests/test_sqlite_migration.py -q`，确认各红灯来自尚未实现的迁移行为。
- [x] 源解析不调用 resolve_user_directory/_read_jsonl 等有写副作用入口；规范来源优先，复用纯转换规则保留 imported_sources/legacy_chat_cleared/cleared_before、无 ID 历史独立重复行及最高 revision 去重。旧完整结果原样导入，重新生成小结与工具索引。所有已归属对象严格验证 user/session/id/type，无法确认归属旧会话跳过。
- [x] 同一事务直接插入记录、同步 seq 计数及 state_revision、迁移诊断，验证来源校验值未变，最后写完成标记。并发首次启动在写事务内复核标记；失败不启动业务、不 fallback。报错可重试。
- [x] 复测迁移测试；跨进程两个首次打开只导入一次；将旧文件损坏/截断测试移为首次访问前准备来源，保留隔离断言。记录 OpenSpec 2.2 证据。

## Task 3: 行程、偏好与历史的权威存储

**Maps:** OpenSpec 3.1、3.2；行程共同提交、版本保护、workflow-summary-discovery 全部需求。

**Files:** context/workflow_store.py、workflow_index.py、long_term_memory.py、trip_memory.py；tests/test_sqlite_memory.py 及 test_workflow_store.py、test_workflow_summary_index.py、test_unified_travel_history.py、test_cli_onboarding.py、test_user_storage_isolation.py。

**Interfaces:**
- WorkflowStore.load(id)、save(workflow, *, expected_revision)、list_all/list_active/list_summaries、save_history/import_history/list_history/clear_history 保留现有参数和返回语义。
- WorkflowIndex 保留调用方需要的小结门面，list 查询 summary_json；rebuild() 为显式修复，正常读取不扫描 content_json。
- LongTermMemory.save_preference/get_preference/set_user_preference/delete_user_preference/add_hotel_brand/add_airline、统计/历史/清空门面保持调用 API；self.data 仅兼容视图，不能作为并发整体回写权威。

- [x] 写 test_workflow_summary_failure_preserves_previous_revision：故障注入小结生成/序列化，断言 load 与 list_summaries 都保持旧 revision 和内容；test_two_processes_only_one_cas_succeeds；test_two_instances_preserve_independent_preferences；test_user_tombstone_blocks_agent_restore；test_concurrent_list_preferences_merge；test_clear_history_keeps_preferences_and_never_reimports。保留历史同名 trip/workflow、无 ID 重复行与最高版本规则。
- [x] 运行 `python -m pytest tests/test_sqlite_memory.py tests/test_workflow_store.py tests/test_workflow_summary_index.py tests/test_unified_travel_history.py -q`，逐个确认新增测试正确失败。
- [x] 行程读写用任务 1 transaction、expected_revision 条件更新，先生成待提交小结和 JSON，同一行写入；冲突继续 WorkflowConflictError，不重复供应商调用。小结查询 SQL 不选择 content_json，用 trace 回调断言。显式小结修复记录失败，不返回错误旧值。
- [x] 偏好读最新字段、用户优先级与 tombstone 同次写入；列表追加及统计 increment 也在一个事务中。历史保持 legacy 非可恢复记录与完成行程投影的去重和 cutoff，清空保留偏好与 migration 标记，delete_all 保留最小禁重导元数据。
- [x] trip.md 只显式/必要 best-effort 导出，提交后故障不改变 save 成功结果；主上下文的小结直接来自数据库。
- [x] 上述测试通过并确认正常 save 不读其他完整行程；更新 OpenSpec 3.1、3.2。

## Task 4: 会话状态、事件和运行记录

**Maps:** OpenSpec 3.3（存储部分）、4.1；事件追加、幂等、状态保护、按需查询。

**Files:** context/session_store.py、agents/execution_harness.py；tests/test_sqlite_sessions.py、test_v0_session_store.py、test_cli_session_resume.py。

**Interfaces:**
- read_events(*, after_seq: int = 0, scope: str | None = None, event_type: str | None = None, role: str | None = None, turn_id: str | None = None) -> list[dict]；无过滤为完整审计。
- read_runs(*, turn_id: str | None = None, event_type: str | None = None) -> list[dict]；无过滤兼容完整审计。
- read_state() -> dict：增加 state_revision；update_state(patch: dict, *, remove_keys: tuple[str,...] = (), expected_revision: int | None = None) -> dict：字段 patch，与 revision 同事务；write_state 保留兼容入口，调用方改用 patch。
- add_active_workflow(workflow_id: str) -> dict：事务读取并合并 active_workflow_ids，不由外部 lock 协调。
- pending_tool_calls(*, after_seq: int = 0) -> list[dict]；latest_seq() -> int；list_sessions(root,user_id,limit=20) 仍返回原有展示元数据。
- append/append_run 保留参数返回结构；每个事件/观测归属字段及时间补全与现有格式一致。

- [x] 新测试断言重复 event_id 返回原值且 last_seq 不变、并发进程追加 seq 为 1..N；append/run/list_sessions monkeypatch read_events/read_runs 抛异常仍成功；工具重复 ID/无 pending/错 turn 或 scope 拒绝且事务无半记录；state 不同字段保留，旧 expected_revision 抛 StateConflictError。
- [x] 运行 `python -m pytest tests/test_sqlite_sessions.py -q`，确认红灯。
- [x] 实现 session 元数据创建/更新、计数分配与 INSERT 同事务；工具 calls 与结果索引同次事务，唯一性及匹配在存储层验证。重复事件优先返回持久化原事件。观测直接索引过滤，列表读取 metadata，不读事件 payload。
- [x] ExecutionHarness 的 pending 问题状态改 patch，不再依赖 store._lock。clear/latest/active 边界由事务保证；完成测试后记录 OpenSpec 4.1 与 3.3 的存储证据。

## Task 5: 精简最终事件和按需内存上下文

**Maps:** OpenSpec 3.3（异步摘要部分）、4.2、4.3；agent-context-isolation 和按需读取历史。

**Files:** agents/context_projection.py、context/memory_manager.py、cli.py；tests/test_session_result_projection.py、test_sqlite_memory.py 及现有 compaction/context-isolation/CLI 测试。

**Interfaces:**
- project_session_result(result: dict) -> dict：纯函数复制 final_answer/status/finalization_method/workflow_id/workflow_revision/travel_conditions/itinerary，复用既有 facts 清洗；禁止嵌入 workflow/results/domain_results/validated_plan/candidate_cache。
- MemoryManager 保留业务 API；初始化和 get_compacted_context 使用 after_seq=max(cleared,covered)、scope='session'、event_type='message'；短期记忆只缓存主消息投影。
- _save_compaction 使用 observed state_revision 做 CAS，冲突不应用旧 summary；跨会话摘要类似保护。模型调用在 transaction 外。

- [x] 测试创建/修改/partial 三类最终结果：源对象 deep copy 前后相等，精简 JSON 无完整结果字段，投影回复和必要事实相同；构造 500 个候选断言事件小于原结果十分之一；旧完整事件仍可投影且原审计不变。
- [x] 测试恢复历史：旧覆盖事件和工具审计 payload 设置为无法解码，主上下文仍成功；所有边界后未摘要消息均存在，不用数量 limit 偷截；工具检查不调用 read_events；两个状态实例更新无关字段后摘要 CAS 不误覆盖。用可控制 await 模型在摘要生成时 clear，断言旧结果未保存且不会复活消息。
- [x] 运行 `python -m pytest tests/test_session_result_projection.py tests/test_sqlite_memory.py -q`，确认失败原因对应实现缺口。
- [x] CLI 仅改变 record_message 的保存入参，展示/last_result/返回对象保持完整。MemoryManager 记录主消息时投影后入缓存；子工具继续数据库审计，不进主内存计数。按 SQL filter 替换启动/压缩/工具/会话统计全扫描。
- [x] 跨会话摘要从 SessionStore.list_sessions 和 state 选择，读取未覆盖主消息；缓存依然完整轮次，CAS 冲突丢弃旧生成结果、下一次重新计算。长时统计由 SQL/权威历史查询获得，不还原全部审计。
- [x] 运行相关既有压缩、隔离、工具、CLI 测试，记录大小对比和 SQL 范围查询证据；完成 OpenSpec 3.3、4.2、4.3。

## Task 6: 接入、回归、自审与规格核对

**Maps:** OpenSpec 5.1、5.2、5.3、6.1。

**Files:** 实际 read_runs 校准/评测调用、README.md、.gitignore、存储/CLI 现有测试、OpenSpec tasks、实施证据 docs/development/2026-10-12-sqlite-storage-validation.md。

- [x] 调整指定轮次观测读取为 read_runs(turn_id=...,event_type='model_call')；完整审计评测可显式全量读取。不改独立 RAG 功能。
- [x] 补足数据库不可用不 fallback、备份读回、清空重启不复活、trip.md 导出失败仍保持已提交行程测试；直接检查文件的业务测试改门面断言，原文件夹具只用于迁移测试。不以删测试实现全绿。
- [x] 文档说明新存储路径、首次迁移/源保留/禁止旧后端同时写入、备份 API 用法和回退边界；忽略用户数据库及 journal 临时文件。路线图“立即修改”完成证据与未来事项分开。
- [x] 实际运行 `python -m pytest -q`；`python -m ruff check context agents/context_projection.py agents/execution_harness.py cli.py evals utils`（按已有项目基线处理，记录预存问题）；`python -m compileall -q context agents cli.py evals utils`；`git diff --check`；`openspec validate sqlite-agent-storage --strict`。不触发真实供应商或用户豁免的专项测试。所有通过才勾选；若环境缺工具，记录并使用已安装项目工具，不隐藏失败。
- [x] 单代理进行整体自审；多子代理按用户选择提供规格和质量独立评审。逐项表格对应 specs 需求/场景、实现文件、测试和实测证据；检查 plan 和 OpenSpec 全部任务。当前 schema 无 verify 工作流，明确记录人工核对，不声称执行 CLI verify。
- [ ] 前置全部通过后执行 OpenSpec archive 同步正式规格，核实 archive 文件；再依 superpowers:finishing-a-development-branch 展示合并/PR/保留分支选择，执行用户选择。归档不替代测试。

## Execution and evidence

- 执行方式：用户已选择单代理、当前工作区；连续实施并整体自审。
- 基线：实际 690 passed、3 skipped；最终 737 passed、3 skipped，详见存储验证记录。
- 计划自审：所有三份 specs 已映射，低层/迁移/门面接口没有构造循环；两份状态写入任务共用 StateConflictError；Review Focus 五项均有具体测试；规格与计划无新增产品行为差异。

实施补充接口：SessionStore.latest_user_turn/latest_message_seq/clear_context；read_runs 增加 after_run_seq/through_run_seq；SQLiteStorage.ensure_session/next_state_revision 使用保留的 state_clock 防止重建版本复用。复核依据与红绿测试见 docs/development/2026-10-12-sqlite-storage-validation.md。Task 6 归档及分支收尾步骤以实际记录为准，尚未执行时不得视为完成。
