# 普通运行日志 Implementation Plan

2026-10-14 更新：用户将本项纳入 Harness 与压缩修复，实际执行改用 [统一实施计划](2026-10-14-fix-harness-amendments-and-compaction.md)，本文件保留背景；不得重复执行。正式日志依据继续使用 `openspec/changes/archive/2026-10-14-add-runtime-file-logging/`。

> **For agentic workers:** 使用 superpowers:executing-plans 单代理执行，TDD 后逐项更新 OpenSpec tasks.md；不派子代理。设计需先由用户审阅。

**Goal:** CLI 普通运行记录可直接在 data/logs/runtime.log 查看。

**Architecture:** logging + RotatingFileHandler；CLI 入口安装一次文件 handler，现有模型/工具审计增加文本摘要，SQLite 路径不变。

**Tech Stack:** Python 标准库、pytest/pytest-asyncio。

**Spec:** openspec/changes/archive/2026-10-14-add-runtime-file-logging/ 下 proposal.md、design.md、tasks.md、specs/runtime-file-logging/spec.md。

## Global Constraints

- 默认 INFO、UTF-8、上海时区、5 MiB、5 份备份。
- 普通摘要不记录完整用户输入、模型消息或供应商载荷。
- 保留数据库观测和业务行为；导入 CLI 不创建日志；测试不写真实 data/memory。
- 用户数据删除为已授权且已完成的本地维护，不为测试或实现重建被删除资料。

## Task 1: 文件输出和 CLI 生命周期（OpenSpec 2.1、2.2）

**Files:** 新增 utils/runtime_logging.py、tests/test_runtime_logging.py，修改 cli.py。

**Interface:** configure_runtime_logging(log_dir=None, *, level='INFO', max_bytes=5*1024*1024, backup_count=5) -> Path。main 在任何运行分支前配置；同文件重复调用不加 handler。

- [ ] 测试先写：tmp_path 下记录中文、级别及模块，flush 后确认实际文件内容；重复配置同一事件只出现一次；换路径不关闭非项目 handler；小 max_bytes 触发轮转，备份数不超过配置；mock CLI main 验证 health 与交互入口均调用配置函数，普通导入无实际文件副作用。
- [ ] 运行 `.venv/Scripts/python.exe -m pytest -q tests/test_runtime_logging.py`，确认失败来自未提供文件日志功能。
- [ ] 最小实现：handler 标记和替换、上海时区 formatter、绝对默认路径，入口记录启动/退出，process_query 的 finally 记录 status、turn_id、latency_ms，异常用 logger.exception 保留堆栈。
- [ ] 复跑新增测试及 tests/test_cli_onboarding.py、test_cli_prose.py、test_latency_report.py，全部通过后更新任务状态。

## Task 2: 模型/工具摘要与完整验收（OpenSpec 3.1、4.1）

**Files:** context/telemetry.py、travel_data/batch_execution.py、tests/test_runtime_logging.py、README.md。

**Interfaces:** MeteredModel._write 与 execute_query_batch 签名不变；在既有记录点追加 logger.info，不替代 SessionStore.append_run。

- [ ] 先写失败回归：模型成功/失败均有状态、token/耗时信息且原回调不变；工具成功/缓存命中均有名称、状态和耗时，不含完整 arguments/items；请求失败仍写日志并向原路径传播异常。
- [ ] 运行新增测试观察业务断言红灯，再按设计增加最小日志输出。
- [ ] 运行 `.venv/Scripts/python.exe -m pytest -q tests/test_runtime_logging.py tests/test_v0_telemetry.py tests/test_workflow_telemetry.py tests/test_direct_query_batch.py tests/test_cached_candidate_pagination.py`。
- [ ] 更新 README 的日志文件路径、轮转和数据库关系；执行全量 pytest，整体自审、对照每个规格场景核查，运行 `openspec validate add-runtime-file-logging --strict`。无 verify 工作流时明确人工核对。
- [ ] 仅在实现与验证完成后归档；本次不自动更新 GitHub PR 或合并。

## Execution Record

2026-10-14：准备设计及详细计划；用户数据清理已独立完成。日志产品代码尚未修改，等待设计/计划审阅。
