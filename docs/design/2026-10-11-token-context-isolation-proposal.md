# Agent 上下文隔离与 token 压缩：探索记录

2026-10-11 用户已授权初始化 OpenSpec。探索草案已迁入正式 change：

- [提案](../../openspec/changes/isolate-agent-context-token-compaction/proposal.md)
- [规格](../../openspec/changes/isolate-agent-context-token-compaction/specs/)
- [技术设计](../../openspec/changes/isolate-agent-context-token-compaction/design.md)
- [任务](../../openspec/changes/isolate-agent-context-token-compaction/tasks.md)
- [实施计划](../superpowers/plans/2026-10-11-agent-context-token-compaction.md)

本文件不再定义产品行为。正式行为以 change 的 specs 为准：子 Agent 独立临时上下文，
不压缩、不跨调用保存；主会话按 token 阈值压缩完整已完成用户轮次；单轮超限停止并
建议将旅程分段规划。用户明确不编写或运行最后一项的专项测试。
