## Context

见 proposal.md 和 specs/runtime-file-logging/spec.md。SYSTEM_CONFIG 已有 log_level，但 CLI 没有统一 FileHandler。MeteredModel 通过回调写 runs；批次工具执行最终合并审计记录；CLI process_query 的 finally 写 query_run。

## Goals / Non-Goals

**Goals:** 普通日志可直接打开；业务状态与文件日志各自保持既有职责；离线可验证；随本次 Harness 修复按用户所选执行方式实施。

**Non-Goals:** 不删除 runs 表，不迁移数据，不新增日志 UI/命令，不实现此前结构整理，不把原始大载荷复制到日志。

## Decisions

1. 使用标准库 logging.handlers.RotatingFileHandler。新增 configure_runtime_logging(log_dir=None, *, level='INFO', max_bytes=5*1024*1024, backup_count=5) -> Path；默认路径以该模块 __file__ 定位项目根目录。允许测试传入 tmp_path 与小上限。
2. 配置项目拥有的 root 文件 handler，标记并识别已有 handler；重复同路径配置不追加副本，切换路径时只关闭/替换本功能的 handler，不改测试或第三方 handler。外部库日志维持 WARNING，项目模块按配置级别输出，避免 HTTP 细节淹没日志。
3. 使用专用 Formatter 输出 Asia/Shanghai 时间。普通输出为可读文本，既有 logger.exception 保留堆栈；不改已有业务异常处理。
4. CLI main 在 health 分支前配置日志。启动/退出由入口和 finally 记录；process_query 记录状态、轮次与耗时，不记录 user_input。MeteredModel._write 追加同一摘要的 INFO 日志，保留原数据库回调。批次工具合并时追加工具元信息的 INFO 日志，不输出 arguments/result.items。
5. 轮转以 5 MiB/5 份为默认，tests 使用小上限验证真实轮转。写入配置失败明确报错，不能显示已启用但实际未写日志。
6. 在 BudgetedModel.check 及主会话压缩入口记录非正文诊断：phase、agent_scope、计数方式与 estimated、tokens/budget_tokens、摘要调用数、完整请求前后大小、covered_through_seq 和停止原因。文件和数据库共用数值来源但独立写入，不能因数据库回调异常就跳过文件摘要。不会读取日志全文来累加 usage，未知用量显示未知。

## Risks / Trade-offs

- 原有 logger 警告可能包含诊断片段 → 本次新增正常摘要不记录原文，不额外扩大记录内容；不声称对所有第三方异常进行完整脱敏。
- root handler 重复安装会导致重复日志 → 明确识别项目 handler，测试重复配置与路径切换。
- 模型取消/异常及工具缓存命中也需可定位 → 验证既有错误传播、缓存计数和 runs 写入不变。

## Migration Plan

2026-10-14 用户将普通日志纳入 Harness 与压缩修复。统一执行 docs/superpowers/plans/2026-10-14-fix-harness-amendments-and-compaction.md 的日志任务，原 runtime-file-logging 实施计划保留作背景，任务不重复执行。先测试再实现，自审及规格核对后归档。没有数据库迁移；回滚仅取消入口配置和新增文件摘要。

## Local Maintenance Record

第一次跨全部用户的删除被自动审批拦截，未执行。用户随后明确选择“删除本项目所有本地用户数据（含测试用户和旧备份）”。在解析绝对路径、确认目标位于项目内且不存在目录连接后，使用原生 PowerShell 清空 data/memory，删除 69 个文件并验证剩余 0 条；未创建恢复备份。其他目录未清理。
