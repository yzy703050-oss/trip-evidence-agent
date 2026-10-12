# 上下文、跨段修复与阈值校准文档入口

2026-10-12：原备忘中的跨段冲突修复和最终阈值校准已并入同一正式 OpenSpec 变更，本文不再定义平行规格。

- [变更范围](../../openspec/changes/archive/2026-10-12-progressive-agent-context-disclosure/proposal.md)
- [技术设计](../../openspec/changes/archive/2026-10-12-progressive-agent-context-disclosure/design.md)
- [实施任务](../../openspec/changes/archive/2026-10-12-progressive-agent-context-disclosure/tasks.md)
- [完整流程与示例](../../openspec/changes/archive/2026-10-12-progressive-agent-context-disclosure/walkthrough.md)
- [渐进式披露规格](../../openspec/changes/archive/2026-10-12-progressive-agent-context-disclosure/specs/progressive-context-disclosure/spec.md)
- [跨段冲突修复规格](../../openspec/changes/archive/2026-10-12-progressive-agent-context-disclosure/specs/cross-task-conflict-repair/spec.md)
- [成功路径阈值校准规格](../../openspec/changes/archive/2026-10-12-progressive-agent-context-disclosure/specs/successful-run-budget-calibration/spec.md)

已确认：继续 JSON 动作加程序状态机；沿用现有候选分页/缓存及精简视图，不新增主模型读取工具；通过独立 workflow 摘要索引定位后加载目标；模型选择修复目标，程序执行版本和依赖更新；全部改动完成后才确定 token、模型调用、实际 task 调度及辅助子 Agent 委派上限，分别取成功样本最大值两倍，熔断独立。

延期：供应商失败排查、失败自动补查和模拟策略改造；待接口额度压力解除再讨论。现有失败/模拟返回及来源标识保留。

保留用户豁免：不新增或运行单 turn 超限停止/提示将旅程分段的专项测试，也不手工触发。

实施与离线资源采样已经进行；完成证据见 [验证记录](../superpowers/reviews/2026-10-12-compact-travel-context-verification.md) 和 [资源报告](../evals/2026-10-12-resource-calibration.json)。
