# Unified Travel History Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans；按用户已有选择单代理实施、TDD 与整体自审，不派子代理。

**Goal:** 历史行程和旅行状态统一存储到 workflows，停止新增或改写 trips.json。

**Architecture:** WorkflowStore 提供统一历史视图及旧记录兼容入口，LongTermMemory 委托它；完整工作流不再重复存储为历史副本。

**Tech Stack:** Python 3.11、JSON 原子写入、已有文件锁与 pytest。

**Spec:** [已讨论方案与兼容约束](../specs/2026-10-11-unified-travel-history-design.md)。没有 OpenSpec change，因此不声称执行 propose/apply/verify/archive。

## Global Constraints

- 保留只读旧文件；迁移成功后标记，失败可重试；完整工作流优先。
- 每趟旅行仅最新方案；历史记录不能成为可执行工作流；保持用户隔离、版本校验及原子写入。
- 保留公开历史接口、清空行为和 trip.md；不新增供应商故障场景；不改模型协议。
- 保留已有 README 未提交修改。本次未获新的 GitHub 发布要求，不自动提交、合并或推送。

## Review Focus

- 迁移半途失败、重复初始化及并发导入不会丢失或重复旧记录。
- 旧历史包含已有 workflow_id 时不覆盖更高版本完整状态。
- 不完整记录、缺日期及可疑身份字段不进入恢复入口，不跨用户读取。
- 历史数量、排序、limit、目的地统计随最新方案更新且不重复计数。
- 清空历史后备份不会使数据复活，可恢复工作流不被删除。

### Task 1: 统一存储、迁移与历史接口

**Files:** `context/workflow_store.py`、`context/long_term_memory.py`；新增 `tests/test_unified_travel_history.py`，更新 `tests/test_v0_session_store.py` 的新存储预期。

**Interfaces:** WorkflowStore 提供历史保存、历史读取及旧数据导入接口；既有 load/save/list_all/list_active 保持可执行工作流语义。LongTermMemory 的 save_trip_history/get_trip_history/get_frequent_destinations/get_statistics/clear_history 保持调用兼容。

- [x] 先写新用户不生成 trips.json、直接保存 completed 工作流即可读取历史、旧记录迁移/重启/用户隔离、完整工作流去重优先、迁移失败重试、清空后不复活的测试；运行并确认失败源自现有独立存储。
- [x] 最小实现统一历史视图和区分历史记录的存储接口；实现幂等导入及完成标记、统计投影与历史清除标记。
- [x] 跑 `../../.venv/Scripts/python.exe -m pytest -q tests/test_unified_travel_history.py tests/test_v0_session_store.py tests/test_v0_memory_flow.py tests/test_workflow_store.py`，确认通过并记录证据。

### Task 2: 接通规划、摘要与用户入口

**Files:** `agents/execution_harness.py`、`context/trip_memory.py`、`README.md`、`tests/test_trip_memory.py`；视真实兼容需求调整 CLI 或记忆查询调用，保持原接口优先。

- [x] 先写真实 workflow 完成/重复完成/修改后的最新历史与统计测试；旧独立 itinerary 仍能保存历史、CLI history 和记忆查询仍能读取；摘要从统一存储呈现旧记录，旧文件不改写。
- [x] 移除 Harness 完成工作流后的重复历史副本写入，保留旧 itinerary 兼容保存；修改摘要数据来源，更新旧文件存在性测试及 README 文件分工与迁移说明。
- [x] 跑定向测试 `tests/test_trip_memory.py tests/test_main_output.py tests/test_main_end_to_end.py tests/test_unified_travel_history.py`；逐项完成整体自审，检查公开入口、迁移和运行恢复。

### Final Verification

- [x] 运行全量 `../../.venv/Scripts/python.exe -m pytest -q tests --tb=short`，阅读输出。
- [x] 运行适用 ruff、语法检查与 `git diff --check`。
- [x] 人工逐项核对设计验收与测试证据，维护任务状态；报告跳过、警告及实际修改范围。

## Execution Record

- 已完成读写链路与兼容测试探索。现有完成工作流会写入 trips.json，历史接口和统计仍依赖它；trip.md 读取旧历史文件。按以上任务统一这些调用。
- 用户已回复“开始”，方案及计划已批准，沿用单代理与整体自审。
- Task 1 RED：9 个新增测试实际全部失败，原因分别为新增 trips.json、完成状态不能直接查询、迁移未写入统一目录、旧副本优先及清空改写备份，符合预期。
- Task 1 GREEN：更新旧迁移测试的存储预期后，指定定向测试 31 passed、1 warning（既有 dashscope 弃用提示）。统一存储、可重试迁移、并发导入、隔离、最新版本统计和历史清空均已验证。
- Ruling：历史清除标记与来源迁移标记存于 workflows/.history-state.json；清空采用时间边界而非删除可恢复状态，保留后续重新完成旅行进入历史的能力。旧记录以哈希身份保存，避免外部 ID 变成路径。
- Task 2 RED：真实完成流程仍调用重复历史保存、摘要在备份不可用时丢失旧记录；两项新增测试失败。旧测试另有 trips.json 文件存在预期，按本次已批准契约更新。
- Task 2 GREEN：指定定向 28 passed、1 warning；真实 CLI history、主 Agent 上下文与记忆查询 Agent 均读取完成工作流，旧 standalone itinerary 保存入口仍通过现有端到端测试。
- 首轮全量回归 556 passed、3 skipped、1 warning，exit=0，42.02s。此后继续自审并补充边界测试，最终全量需重新运行。
- 自审 RED→GREEN：分别复现并修复无 ID 相同记录被误去重、workflow_id/trip_id 同值碰撞、过时用户备份复活已删除记录、自动历史 ID 更新生成重复、旧工作流多版本迁入时选到旧版。新增统一历史测试 17 passed；较早综合定向 54 passed。
- Ruling：沿用原加载优先级，有 trips.json 时以它为迁移来源，否则使用更早的用户 JSON；无 ID 记录用原列表位置区分并保持重试稳定；旧同身份版本选择最高 workflow_revision，同版本选择最后一条。不变更已确认用户行为。
- 规范检查：核心统一存储与新增测试 ruff 通过。Harness 和 trip_memory 存在原有 E701/E702 风格问题；未进行无关的全文件格式重写，最终报告会区分已有问题与本次新增代码检查。
- 最终全量验证：561 passed、3 skipped、1 warning，exit=0，38.15s。警告来自既有 dashscope 弃用提示；没有失败。
- 最终检查：核心存储与新增测试 ruff 通过；全部修改 Python 文件 compileall 通过；git diff --check 通过。使用 HEAD 原文件复核 lint 基线，Harness 原有 8 项、trip_memory 原有 9 项风格问题，并非本次新增。
- 最终整体自审及人工规格核对：每项验收有实现与测试对应；最新版本历史、统计去重、旧单文件/拆分文件迁移、异常重试、并发初始化、用户隔离、历史清空、恢复与只读备份均通过。旧独立规划入口及真实 CLI/记忆查询兼容通过。无未解决的重要偏差。
- 最终状态：两项实施任务完成，保留此前 README 简化。项目未初始化 OpenSpec，未运行 OpenSpec verify/archive。修改保留在当前 v0 工作分支，未提交、合并到 main 或推送。
