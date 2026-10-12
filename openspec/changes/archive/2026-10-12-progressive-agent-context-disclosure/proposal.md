## Why

当前恢复入口与行程匹配先读取所有完整 workflow，主上下文还展开全部任务、候选来源和重复字段。用户确认保留 JSON 状态机和既有分页，改进披露内容与小结定位，并让模型选择跨段修复任务；最终上限在全部改动完成后根据成功测试路径确定。

## What Changes

- 独立保存每个 workflow 的轻量小结索引。识别恢复意图后先用索引选目标，再读目标 JSON；意图与定位可以合并一次模型请求。
- 火车、酒店分别采用精简字段视图，保留选择、衔接和版本所需事实；URL、完整来源、坐标及证据原文留在存储。未核实、过期和模拟状态不可丢。
- 保留少量当前候选、现有分页与缓存复用；不新增主模型原生读取循环。减少重复 workflow/tasks/current_task 与主到子上下文。
- 模型通过受控 repair_tasks JSON 选择冲突修复根任务，程序处理授权、版本、有效引用、依赖和恢复；完成前重新校验。
- task 调度预算控制实际业务决策步数，包括重复和修复；实际模型请求另行计数，子委派是辅助限制。
- 功能完成后按相同作用域成功测试最大值两倍校准 token、模型调用、调度及辅助委派上限，熔断、工具、超时和页大小独立。
- 使用离线/模拟样本，保留来源；不新增供应商失败排查或用户豁免的单 turn 超限专项测试。

## Capabilities

### New Capabilities

- `progressive-context-disclosure`: 领域字段裁剪、任务视图与双向传输。
- `workflow-summary-discovery`: 独立摘要索引，选中后加载权威 workflow。
- `cross-task-conflict-repair`: 模型选根任务，程序执行修复与全程校验。
- `successful-run-budget-calibration`: 成功路径资源观测及两倍上限。

### Modified Capabilities

无。沿用独立子上下文、完整旧轮次压缩和 reply_facts。

## Impact

涉及 context_projection、workflow_store、trip_memory、memory_manager、execution_harness、workflow_runner/contracts、主提示词、预算与遥测；新增摘要索引、修复及校准模块。保留 JSON 和现有选择引用，不增加外部服务。
