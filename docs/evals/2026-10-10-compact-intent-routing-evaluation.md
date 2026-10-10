# 四类意图路由验证记录

日期：2026-10-10（北京时间）。实际项目：`.superpowers/trip-evidence-agent`，工作分支：`codex/information-acquisition-agent`。

顶层意图已从20类收敛为 ask、plan、update、control。保留原执行引擎，修改集中在主 Agent 提示词、意图契约与状态比较；恢复测评另修复了暂停后断点版本过期的问题。

## 测试与检查

- 修改前基线：456 passed、3 skipped（26.55s）。
- 最终全量 pytest：517 passed、3 skipped（25.46s）。跳过来自既有环境相关测试；保留1条既有 DashScope 弃用提示。
- 四类契约先见29个预期失败，再通过；提示词、状态分类、澄清修复、评估器及跨会话断点均按失败→实现→通过顺序验证。
- 修改模块的 Ruff F 正确性检查通过；两个新增测试文件全规则 Ruff 通过；git diff --check 通过。原文件额外全规则 Ruff 探查存在既有压缩格式风格问题，未扩大格式整理范围。

## 真实模型配显式模拟供应商

使用项目已配置的真实模型、默认关闭推理、隔离用户记忆，以及 simulation:train / simulation:hotel Provider。最终17个场景逐项有通过证据；该结果来自完整测评与修复后的定向复测，不是宣称某一完整轮一次性17/17。

首轮11/17；提示词修复后完整轮14/17，剩余3项由澄清后暂停造成断点版本过期。修复后10项相关复测9/10，其中唯一失败是评估器误拒绝从已知上下文正确读取偏好；加入真实偏好答案一致性与禁止写入检查后，偏好/交易2项复测2/2。保留所有失败原始记录，未修改历史测评文件。

| 场景 | 结果 | 本轮耗时（秒） | 模型调用数 | 证据 |
| --- | --- | ---: | ---: | --- |
| default_date | 通过 | 11.66 | 7 | [原始输出](../../data/evals/2026-10-10-compact-intents-checkpoint/default_date.json) |
| unknown_origin | 通过 | 0.80 | 1 | [原始输出](../../data/evals/2026-10-10-compact-intents-final/unknown_origin.json) |
| arrival_overnight | 通过 | 11.53 | 7 | [原始输出](../../data/evals/2026-10-10-compact-intents-final/arrival_overnight.json) |
| query_price | 通过 | 3.97 | 3 | [原始输出](../../data/evals/2026-10-10-compact-intents-final/query_price.json) |
| preference_plan | 通过 | 12.37 | 8 | [原始输出](../../data/evals/2026-10-10-compact-intents-final/preference_plan.json) |
| multi_route | 通过 | 20.78 | 15 | [原始输出](../../data/evals/2026-10-10-compact-intents-final/multi_route.json) |
| multi_route_unspecified_stay | 通过 | 26.69 | 16 | [原始输出](../../data/evals/2026-10-10-compact-intents-final/multi_route_unspecified_stay.json) |
| hotel_replace | 通过 | 10.29 | 7 | [原始输出](../../data/evals/2026-10-10-compact-intents-checkpoint/hotel_replace.json) |
| explain | 通过 | 1.28 | 1 | [原始输出](../../data/evals/2026-10-10-compact-intents-checkpoint/explain.json) |
| ambiguous | 通过 | 0.83 | 1 | [原始输出](../../data/evals/2026-10-10-compact-intents-checkpoint/ambiguous.json) |
| pause | 通过 | 0.92 | 1 | [原始输出](../../data/evals/2026-10-10-compact-intents-checkpoint/pause.json) |
| supplement_origin | 通过 | 12.12 | 7 | [原始输出](../../data/evals/2026-10-10-compact-intents-final/supplement_origin.json) |
| memory_read | 通过 | 0.87 | 1 | [原始输出](../../data/evals/2026-10-10-compact-intents-memory/memory_read.json) |
| unsupported_purchase | 通过 | 1.28 | 1 | [原始输出](../../data/evals/2026-10-10-compact-intents-memory/unsupported_purchase.json) |
| resume | 通过 | 4.74 | 3 | [原始输出](../../data/evals/2026-10-10-compact-intents-checkpoint/resume.json) |
| change_stay | 通过 | 14.80 | 8 | [原始输出](../../data/evals/2026-10-10-compact-intents-checkpoint/change_stay.json) |
| preference_replace | 通过 | 13.31 | 8 | [原始输出](../../data/evals/2026-10-10-compact-intents-checkpoint/preference_replace.json) |

解释、模糊反馈、暂停、读取偏好、交易能力说明均检查零外部查询；换酒店与组合偏好更新检查火车保留，补起点检查同旅行及建议日期复用，暂停后继续检查断点版本与已有结果复用。

模拟结果不能证明生产火车/酒店接口可用；自然语言测评仅覆盖已列场景，不能保证所有表达都识别正确。

## 规格与整体自审

- R1：四类共享常量、所有模式 intents、新输出真实模型原文验证；不把操作或来源变成类别。
- R2：复用 schedule、travel_update 及其 target；没有新增第二套 action/source/target 或识别 Agent。
- R3：按真实任务与继承全程条件比较叶字段；输入不可变、混合补充修改与建议覆盖均有回归。
- R4：模糊反馈 direct+断点，已有方案解释 direct，购票不等于采纳；暂停恢复保留CAS校验。
- R5：组合目的及去重后动作保留、缺失/空标签兼容、旧非法标签受限修复。
- 按用户选择完成单代理整体代码自审；无未解决的重要发现。Provider、RAG内部、候选真实性校验及任务循环算法未重写。
- 项目未初始化 OpenSpec，人工逐项核对正式设计与代码/测试，未运行 propose/apply/verify/archive。
- 用户已选择提交、推送并创建 PR；本记录对应提交前验证。
