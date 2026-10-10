# 模型停留提案与出发地反问测评

日期：2026-10-10。正式行为依据：[规划补全规格](../superpowers/specs/2026-10-10-planning-preflight-design.md)，实施记录：[计划](../superpowers/plans/2026-10-10-stay-proposals-origin-clarification.md)。

## 结果与边界

全量回归：**444 passed、3 skipped、1 条既有 DashScope 弃用提示，19.25s**。命令：`../../.venv/Scripts/python.exe -m pytest -q`，日志位于 `data/evals/stay-origin-regression.log`。整体自审及 Git whitespace 检查通过。

真实已配置模型、显式模拟火车和酒店供应商、隔离测试记忆，推理模式保持 disabled。12 个场景均有通过记录。首轮完整 12/12 通过；最终代码复测先完成 9 个场景，在模糊反馈处发现问题并中断；修复后复用保存的记忆，补测模糊反馈、暂停、补起点 3 个场景全部通过。不是声称中断的那次完整运行通过。

主要数据：[最终复测前9项](../../data/evals/2026-10-10-stay-origin-final/report.json)、[恢复测评3项](../../data/evals/2026-10-10-stay-origin-recovery/report.json)、[首轮12项](../../data/evals/2026-10-10-stay-origin/report.json)。各目录有每案例 JSON（含模型响应、结构化最终结果）、latency.csv 与会话运行记录。恢复测评使用 final/memory，保留了原来的 workflow/task 身份。

| 场景 | 结果 | 总耗时 | 模型次数 | 初始决策 | 主Agent循环 | 信息获取 |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| 未给日期，重庆→上海 | completed | 12.02s | 7 | 1.14s | 6.96s | 3.73s |
| 未给出发地 | needs_input | 1.13s | 1 | 1.10s | 0 | 0 |
| 仅到达日期，跨夜火车 | completed | 11.81s | 7 | 1.19s | 7.09s | 3.35s |
| 只查火车价格 | ok | 3.58s | 3 | 1.21s | 0 | 2.34s |
| 更新偏好再规划 | completed | 13.01s | 8 | 1.34s | 7.12s | 3.70s |
| 三段，每站明确一晚 | completed | 23.90s | 15 | 1.43s | 13.27s | 8.81s |
| 三段，未给停留晚数 | completed | 25.14s | 16 | 1.14s | 12.80s | 10.72s |
| 只换酒店 | completed | 13.61s | 8 | 1.09s | 8.75s | 3.58s |
| 解释已有方案 | ok | 1.35s | 1 | 1.33s | 0 | 0 |
| 不满意但范围不明（修复后） | needs_input | 1.01s | 1 | 0.99s | 0 | 0 |
| 暂停（恢复测评） | paused | 0.96s | 1 | 0.94s | 0 | 0 |
| 新会话回答“重庆” | completed | 10.66s | 7 | 0.96s | 6.19s | 3.32s |

偏好场景另有偏好模型调用0.64s。模型阶段之和与总耗时存在本地记录、保存和工具调用开销。模拟供应商耗时不能代表真实API网络耗时；这些延迟为单次样本，不是性能分位基准。

## 停留方案来自模型

未给晚数的上海→北京→杭州→上海案例，**首次 main:plan 模型响应**已给出北京3晚、杭州2晚，来源均为proposal；返程没有酒店及nights。不是后续程序填默认值：

```json
{
  "tasks": [
    {"origin": "上海", "destination": "北京", "requires_hotel": true,
     "conditions": {"departure_date": null, "nights": 3}, "field_sources": {"nights": "proposal"}},
    {"origin": "北京", "destination": "杭州", "requires_hotel": true,
     "conditions": {"nights": 2}, "field_sources": {"nights": "proposal"}},
    {"origin": "杭州", "destination": "上海", "requires_hotel": false, "conditions": {}}
  ]
}
```

最终安排北京10-17入住、10-20离店；杭州10-20入住、10-22离店；10-22返程。实际抵达来自模拟供应商的有日期证据，全程程序校验通过。该结果只说明此样本模型能完成分配与衔接，不说明所有目的地的停留长度都已做旅游质量评估。

程序检查每住宿任务有正整数nights或明确入住离店日期。遗漏最多请求模型修复一次，仍遗漏停止，不补默认晚数；default来源停留值也拒绝。用户给的固定日期、预算、人数和晚数继续经过现有全程校验；入住离店安排不是预订成功。

## 起点与反馈断点

缺起点时保存required_conditions，missing_fields=[origin]，只问“你准备从哪个城市出发？”，火车和酒店外部请求均为0。后续独立会话仅回答“重庆”即可恢复同一workflow/task，保留建议日期和晚数，再执行原来的火车及酒店查询。普通酒店和天气查询不要求起点。

补起点后的酒店范围扩展仅用于required_conditions恢复；用户单独换火车时不额外查酒店。酒店组件修改继续保留原火车引用。

最终复测中，模型曾把“不满意”输出为空condition_updates的change，错误地选择重做整段；测评又对checkpoint=null直接调用get而崩溃。修复为拒绝空change、把校验原因送入一次初始修复、明确模糊反馈须输出feedback_scope；测评空值按未通过处理，不能以崩溃掩盖验收失败。修复后的真实模型只询问修改范围、保存断点，零外部查询。

## 自审与正式规格核对

- 模型先提出停留方案，程序不提供默认晚数；不同晚数、返程范围、一次修复及修复失败都有测试证据。
- 起点反问、零查询、持久化版本及跨会话同ID恢复均验证；已有组件定向修改保持。
- 模型写source=user不足以创建酒店硬报价条件。新增条件检查用户原文的常见明确酒店报价表达或可信偏好；仅查火车价格、明确不需要酒店报价均拒绝，预算约束不删除。既有历史误标条件不自动迁移或清除。
- 全程日期、晚数、证据引用、预算和依赖校验仍决定completed，未知事实不当作成功查询结果。
- RAG、生产供应商、酒店估价、景点活动及实际交易能力未修改。
- 项目未初始化OpenSpec，本次人工逐项核对正式规格和计划，未运行或声称OpenSpec verify/archive。

生产火车查询失效尚未解决；本报告不代表真实API恢复，酒店真实价格和库存仍未知。代码保留在当前工作分支。
