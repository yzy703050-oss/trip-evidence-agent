# 四类意图与现有执行协议解耦

日期：2026-10-10。状态：用户已同意四类顶层意图方向并选择单代理；已实施并通过测试及人工规格核对。验证见[记录](../../evals/2026-10-10-compact-intent-routing-evaluation.md)。

当前有效项目为 `.superpowers/trip-evidence-agent`，分支为 `codex/information-acquisition-agent`；外层仓库的旧 IntentionAgent 不在本次修改范围。两个目录均没有 OpenSpec 初始化结构，因此不创建 `openspec/`，不声称运行 propose/apply/verify/archive。本文沿用项目已有正式设计目录；原规划前置、停留提案、起点断点、候选证据、版本与执行范围规则继续有效。

## 1. 目标与范围

用户要求：减少顶层意图类别，避免新增业务能力时继续增加意图；尽量复用代码，主要调整提示词。

主 Agent 只分类用户目的：`ask / plan / update / control`。领域、数据源、修改对象、执行动作和上下文状态继续使用现有结构表达，不再作为顶层分类标签。分类与首次执行决策在同一次模型调用完成，不新增模型调用或独立意图 Agent。

本次不新增业务能力或批量操作多个旅行的能力。保留同一请求中偏好更新与查询、规划或旅行修改的现有组合处理。

## 2. 修改前现状与技术复核

- `agents/main_agent.py` 的 `INTENTS` 有 20 个标签，混合查询来源、修改对象、状态与处理策略。
- `initialize` 提示词对非 workflow 输出 intents，但要求 workflow 省略 intents；输出没有统一的意图契约。
- `agents/contracts.py:validate_plan` 校验实际执行协议，没有验证 intents。
- `ExecutionHarness` 按 response_mode、agent_schedule、workflow_proposal、resume_workflow_id、travel_update 执行；并不按这 20 个标签分支。
- `travel_updates.py` 已实现组件替换、条件更新、重做、路线变更、采纳和暂停；它是执行协议，不必因分类缩减而重写。
- `known_workflows` 已含任务条件、断点和 saved_plan，可以支持上下文判断；无需增加状态系统。

结论：这是对识别协议和提示词的有限重构。保留执行引擎，增加少量契约校验与条件更新分类；不重构工具、Provider、RAG、记忆存储和任务循环。

## 3. 正式行为与验收场景

### R1：四类顶层意图

所有新主 Agent 初始化输出均包含 `intents`，格式为非空列表，例如 `[{"type":"ask"}]`，type 只允许 ask、plan、update、control。不输出置信度、长解释或旧细分标签。允许多个类别，不使用 multi_intent 类别。同类目的只保留一个类别；各具体动作仍由现有执行字段保留。

| 类别 | 用户目的 | 对象与执行结构 |
| --- | --- | --- |
| ask | 问答、查询、解释、查看状态 | agent_schedule 表达所需资料来源；direct 或 answer 表达回答方式；解释已有旅行读取 saved_plan |
| plan | 新建旅行方案 | workflow_proposal 表达整程任务 |
| update | 更新长期偏好、条件、已有旅行或候选 | preference 前置任务或 travel_update 表达实际动作；其 target 表达 workflow/task/components |
| control | 恢复、采纳、暂停、停止规划，以及表达当前不支持的交易诉求 | resume_workflow_id 或 travel_update 的 adopt/pause/cancel；不支持交易时直接说明能力范围，不执行 |

场景：天气、个人历史、企业制度都识别为 ask，但调用各自所需角色；换火车与换酒店都识别为 update，以 components 区分；不因未来增加领域而新增顶层类别。

已有上下文明确包含用户当前保存偏好时，ask 可以直接据此回答；需要上下文未含的历史再调度 memory_query。读取不得调度 preference 写入。测评同时核对回答与实际保存偏好一致，而不是机械要求每次读取都调用子 Agent。

### R2：沿用执行协议，避免重复信息

不增加第二套 action、source 或 target 字段。对象定位与操作继续放在 agent_schedule 和 travel_update；intents 只记录四类目的。response_mode 是执行模式，不是用户意图。

场景：`intents=[{"type":"update"}]` 与 `travel_update.update_type=replace`、`target.components=["hotel"]` 共同表达换酒店；仍保留原火车、日期和其他任务。缩减标签不代表取消 replace/change_route 等执行动作。

### R3：上下文决定条件补充或修改

用户给出条件时不再先分类为 supplement_conditions 或 change_conditions。主 Agent 提取明确更新，继续通过 travel_update 提交。对已定位的旅行和任务，程序依据已保存的用户确认条件将 supplement/change 规范化：所有写入叶字段此前未知时用 supplement；至少一个已知值发生变化时用 change；覆盖 proposal/default/derived 建议视为 change；完全相同的重复写入按 supplement 表达，沿用已有重复写入处理，不另创意图。

判断包括 origin/destination 等任务字段与 conditions，constraints 按叶字段比较，不把整个对象当成一个值。混合补充与修改用 change，但保留全部明确提供字段。用户可以覆盖建议或原值；分类不能替用户增加或删除字段。既有日期来源和受影响领域规则继续决定如何执行。

场景：起点断点回答“重庆”补同一 workflow/task 的未知 origin；原起点上海改重庆为 change；新加预算同时改日期为 change，不丢预算，不扩展到未授权任务。不存在或版本冲突目标仍由现有身份与版本校验拒绝。

### R4：处理策略不是意图

模糊反馈属于 update，但无法定位范围时继续 direct + feedback_scope 询问并保存断点；澄清不另设意图。购买、订房、退改签诉求识别为 control，由提示词与执行能力校验限制为直接解释，不生成交易动作。

场景：只说“不满意”不发查询、不输出空 change、不自行整程重做；“为什么选这家酒店”属于 ask，根据既有依据回答，不恢复执行或刷新；“暂停”属于 control，保存暂停状态且零查询。

### R5：组合请求与兼容

“以后优先全季，并规划这次旅行”输出 update 与 plan，先完成偏好更新再执行规划；“以后优先全季，这次也换酒店”可以只列 update，但必须同时保留 preference 调度和 hotel 替换 payload。

旧客户端、测试桩与历史保存记录可继续缺少 intents 或使用空列表。validate_plan 对缺失/空列表按已有执行字段推导四类标签；不读取历史记录并批量迁移，不修改历史存储结构。非空列表只接受四类的新契约，未知类别或畸形值应触发现有一次修复，修复仍失败则停止执行。新 MainAgent 提示词明确要求总是输出四类标签。

兼容推导顺序：preference 调度加入 update；workflow_proposal 或旧 itinerary 模式加入 plan；travel_update 的条件/替换/重做/路线更新加入 update，adopt/pause/cancel 加入 control；仅 resume_workflow_id 加入 control，含非空 legacy workflow_update 加入 update；非 workflow 的查询/直接回答加入 ask（纯偏好更新不额外加入 ask）。feedback_scope 属于 update。去重保留目的，推导不增加或更改任何执行动作。

## 4. 实施边界

1. contracts 添加共享四类常量与 intents 形状校验、缺失兼容推导；原执行合法性校验继续生效。
2. MainAgent 重写 initialize 的意图章节与例子，workflow 也输出 intents；保留条件、日期、建议来源、授权、查询与停止边界。
3. travel_updates 在 supplement/change 分支按加载的真实任务条件规范化类别；不改变候选失效、依赖重检与组件保留算法。真实多轮测评发现暂停保留断点却没有同步下一保存版本，会错误阻止恢复；停止时保留问题并同步断点版本，不放宽 CAS 校验。
4. 更新 README、契约与端到端回归，以及真实模型配模拟供应商的测评证据。

## 5. 验证与完成条件

契约测试证明四类输出、无效输出一次修复、缺失标签兼容，以及组合目的不丢动作；状态测试证明补充/修改由已有值决定且输入对象不被变更。集成测试证明查询不建旅行、解释/澄清/暂停零查询、定向换酒店保留火车、跨会话补起点复用旅行和日期晚数。

实际运行定向测试及全量 pytest；对提示词实际效果使用已配置真实模型和显式模拟火车/酒店供应商测评，记录结果、输出契约及耗时。模拟供应商通过不能证明真实业务接口可用。若真实模型调用失败，保存失败原因并明确报告尚未验证，不能用脚本模型的测试冒充识别质量。

逐项核对 R1–R5、测试与代码，完成整体自审；项目未初始化 OpenSpec，采用人工规格核对。用户已选择单代理执行并完成整体自审，随后选择提交、推送并创建 PR；本记录对应提交前验证。
