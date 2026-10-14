# Harness、记忆压缩和普通日志 PR 发布验证

日期：2026-10-14。用户在此前选择保留工作区后，追加授权“提pr”；本次创建新PR，不合并。

以最新远端main 3a16b6ef5561a414c2822ed50a391fa06a568240（tree b700a7b6994a68d2a8b891bda3797a0eb4ed020e，已合并PR14）为发布基线。通过GitHub连接器逐一核对513个main blob，建立独立本地快照，再叠加本次修复。没有用落后的本地main覆盖远端，也没有重写原工作分支。

发布范围：Harness读取/修改互斥、修改候选及目标闭环、当前请求与历史隔离、完整请求压缩/响应复用、20000预算及80%软触发、temperature0.3、普通运行日志、回归测试、复现脚本、正式规格与两个归档，以及去除原始载荷的评测汇总。原始会话、模型请求/答复记录、供应商载荷、用户数据库、本地.env及其他未实施提案保留本地。

验证：当前工作区938 passed、3 skipped；独立发布快照完整pytest：938 passed、3 skipped，66.59秒，退出0。实际输出记录于本计划本地发布账本。新文件Ruff检查及发布源码正确性检查通过。密钥/私钥及数据库路径检查通过。正式规格已由OpenSpec archive同步，spec-driven无verify artifact，已人工核对；发布时再次严格校验规格。

既有真实模型验收：最终16轮连续交互、23次API调用，12项任务检查通过；额外缓存/边界测试另记。受控缓存冷80%、热100%不当作自主任务命中率。token计数仍为明确标记的本地估算，ASCII偏小样本与实际API用量如实披露。详见docs/evals/2026-10-14-harness-memory-fix/README.md。

发布以GitHub连接器在已核对main tree上创建新tree、commit和codex/harness-memory-compaction-logging分支，再创建PR；核对发布blob与已测试快照一致。原工作区和未提交改动继续保留。

