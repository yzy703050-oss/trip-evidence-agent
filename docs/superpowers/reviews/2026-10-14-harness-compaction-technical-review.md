# Harness、压缩与普通日志技术复核

日期：2026-10-14。复核范围：fix-harness-amendments-and-compaction 全部 proposal/specs/design/tasks，add-runtime-file-logging 全部文档，当前代码/接口/测试、原始实测日志及开发文档。此记录不另定义产品行为。

## Findings and Decisions

1. 用户希望保留定位/补丁两次模型调用；既有单次可提交兼容入口不主动移除，常规自然语言旧行程修改不做一次调用优化。
2. 不能只增加提示词禁止猜ID：保存修改须维护真实显示过的引用与明确本次名称目标，目标错配即使存在于完整缓存也不能提交。
3. 命名目标需要可靠原文证据；通过字面name/contains契约及完整缓存可靠映射，模型遗漏明确“名称包含”目标时程序仍解析。一般换一家不硬编码品牌或目标，多义澄清。
4. 只读与修改应在validate_plan拒绝，而非调整分支顺序任意丢动作；沿用一次初始化输出修正即可，普通偏好前置仍允许混合。
5. 当前阶段压缩被用户明确延期，不能为解决历史预算问题修改主turn边界或子Agent隔离规则。
6. 在历史累加与完整输入之间加固定比例不能保证正确；采用实际build_messages和tools/response组成的验收函数，提交摘要前和重读状态后检查。摘要实际输入覆盖哪些轮次，边界就只能覆盖哪些轮次。
7. 元数据只从模型投影移除；保留原事件及业务引用防止丢失断点或行程身份。压缩callback可选以兼容当前同步/异步测试和历史调用者。
8. 预算不仅改默认：当前.env仍覆盖为14854，本次须只更新该键，示例和默认一致。验证同时保留旧边界预算避免把提高阈值误当修复。
9. 普通日志已有正式change，统一计划协调两份tasks；不重复定义日志规格或执行旧日志计划。预算和压缩诊断新增到已有日志spec，SQLite不变、此前清理数据不再次删除。
10. 工作区有既有未提交变更。实施前只读保存基线和确定checkout策略；不重置/覆盖其他修改，单代理不自动引入评审子代理。

## Validation Baseline

运行 intent_contracts、saved_trip_amendment、saved_trip_read_only、cached_candidate_pagination、v0_compaction、context_budget、agent_context_isolation 七组测试：83 passed in 2.21s。未实施修复，此结果不证明实测缺陷消失。第一次运行因不存在的basetemp父目录失败，后续使用项目已有评测目录完成基线。

OpenSpec spec-driven的verify artifact不可用，实际instructions命令返回Artifact 'verify' not found；未来按用户流程人工核对，不声称已运行verify。

## Plan Review

详细计划每项映射OpenSpec任务，覆盖失败复现、最小实现、验证与完成条件；任务顺序为路由→候选闭环→历史投影→完整请求压缩→预算配置→普通日志→真实行为验收。日志和压缩共享观测点，采用先实现业务、后接日志避免两个任务并发改同一文件。

用户审阅与执行方式尚待确认。无额外产品行为歧义需要在本次既有讨论之外重复访谈；后续实施发现新的用户可见缺口时先更新正式文档。

## Planning Validation

已实际运行：`openspec validate fix-harness-amendments-and-compaction --strict` 与 `openspec validate add-runtime-file-logging --strict`，均返回 valid；本次文档范围 `git diff --check` 通过。统一实施计划包含7项交付任务，映射两份正式changes。当前分支为 codex/consolidate-travel-execution，存在既有工作区修改。用户执行选择已发起，尚未收到回答；无产品代码或本地预算变更。

## 实施后记录

以上为实施前技术复核记录；用户随后选择单代理，已按统一计划实施并整体自审。最终938 passed、3 skipped，新文件Ruff通过，真实连续12/12和另2类边界/缓存验收通过。temperature=0.3保持，预算20000/软触发16000。详见[最终自审](../verification/2026-10-14-harness-memory-fix.md)，不能把前文等待状态当作当前未实施状态。
