# 差旅助手架构修改设计：主 Agent、信息获取与内部工具

状态：用户于 2026-10-09 以“执行”确认本设计；技术复核已完成，详细实施计划待审阅，尚未实施产品代码。
日期：2026-10-09。
项目：当前独立仓库 trip-evidence-agent，产品代码基线 2dfb62d。

本文更新此前的信息获取重构设计，是本次变更的唯一设计依据，替代旧版保留独立意图识别和独立规划节点的方案。当前仓库没有初始化 OpenSpec，沿用 docs/superpowers/specs/，不创建 OpenSpec 目录，不声称已执行其 propose/apply/verify/archive 工作流。

## 1. 目标与本轮范围

先完成除 RAG 内部改造以外的架构整理：用户请求交给主 Agent，主 Agent 分派业务任务，子 Agent 返回结果，再由主 Agent 统一回答或生成行程。

已确认的业务方向：

- 原意图识别职责并入主 Agent，主 Agent 负责理解需求、分派、结果综合和最终回答。
- 行程规划作为主 Agent 的业务能力，不再单独调度一个规划子 Agent。
- 事项收集并入信息获取 Agent，统一整理条件、调用工具、检查结果、必要时补查并总结。
- 火车、酒店、攻略三个当前没有模型调用的包装节点改为信息获取的内部工具。
- 本轮更新的长期偏好应在相关查询前生效，临时要求只影响本轮。
- 保留真实候选、来源、价格精度、预算校验、记忆、执行记录和现有模型配置。
- RAG Agent 暂不改造，外部 MCP RAG 接入放在下一次独立变更。
- 用户已确认：信息获取完成简单查询且答案完整时，统一出口直接展示其答案和来源，不再调用主 Agent 模型重复总结；组合任务、行程或需要补查的请求仍由主 Agent 综合。

酒店和攻略未配置真实 Provider 时仍返回 unavailable，不使用演示数据补足。本文中的模块路径、动作契约和循环上限是拟实施设计，不代表现有代码已实现。

## 2. RAG 冻结边界

本轮不修改 .claude/skills/ask-question/ 下的 Agent、SKILL.md、检索实现和知识库数据；不修改 RAG 模型、embedding、top_k、初始化和检索配置；不新增 MCP 客户端、工具循环或回答生成链路。

G:/简历项目/MODULAR-RAG-MCP-SERVER/ 不属于本轮实施范围，不修改该项目，不导入或迁移文档。

新主 Agent 和 harness 只在调用方兼容现有 RAG：

1. 调度名仍为 rag_knowledge，懒加载映射仍指向 ask-question，模型注入方式不变。
2. 仍用 Msg 传递 JSON，保留 context.rewritten_query 和 previous_results 外层结构。新增 original_query 不要求 RAG 读取它。
3. 返回可能包含 answer、retrieved_documents、citations，或 no_knowledge、error、初始化失败。不要求 RAG 改成信息获取的 domain_results 格式。
4. 调用方检查内层业务状态，不能把外层调用成功当成制度检索成功。
5. 综合使用现有回答和已有来源；缺少来源时不补造引用，不声称已接通 MCP 或核对了制度版本、生效日期。
6. 冻结的是 RAG 自身实现与检索能力；最终出口改为主 Agent，所以主 Agent 可以重新组织现有结果的措辞。

当前注入 rag_service 的分支会直接拼接片段返回，不调用总结模型，本轮保留。默认本地检索路径可以调用现有回答模型；不能宣称所有 RAG 路径都会调用 LLM。

SkillLoader 对完整 ask-question 指南的读取继续可用，不能因全局摘要筛选破坏 get_skill_content("ask-question")。

## 3. 方案选择与角色

| 方案 | 取舍 |
| --- | --- |
| 只合并查询节点，保留独立意图和规划 Agent | 改动少，但没有落实主 Agent 统一收尾 |
| 主 Agent 分派和收尾，保留业务子 Agent | 本轮采用；职责与数据流明确，工具可独立验证 |
| 一个 Agent 挂载全部工具 | 角色少，但记忆、偏好、查询的工具和上下文混在一起，本轮不采用 |

本轮完成后保留五个业务 Agent 角色：

| 角色 | 职责 | 模型使用 |
| --- | --- | --- |
| MainAgent | 理解需求、分派、按需综合、决定补查、最终回答与行程 | 需要推理的阶段调用共享模型；简单查询可省略最终调用 |
| InformationQueryAgent | 条件整理、内部工具选择、检查、补查、摘要 | 可以多次调用共享模型 |
| PreferenceAgent | 长期偏好变更提取 | 保留现有调用和输出 |
| MemoryQueryAgent | 相关历史查询与回答 | 保留现有实现 |
| RAGKnowledgeAgent | 企业制度与知识问答 | 保留现有路径，见第 2 节 |

可以共用一个模型配置和 MeteredModel 包装器，各角色仍有自己的提示词和任务输入。不是每轮都要执行五个 Agent，也不要求每个 Agent 使用不同模型。

原 IntentionAgent 的能力迁入 MainAgent，不再维持独立意图节点。原 ItineraryPlanningAgent 的提示词、解析和事实保护迁为主 Agent 的行程模块，不保留第二个规划执行者。

## 4. 主 Agent 与 harness 的边界

主 Agent 决定业务上的做什么：选择子 Agent、给出目标、判断结果是否足够、决定回答或有依据的补查。

harness 负责运行机制：动作校验、依赖、并发、上下文、循环限制、查询缓存、超时、日志、来源保护、偏好和最终行程保存。它不调用 LLM 自行决定业务。

沿用 agents/orchestration_agent.py 承载 harness，可以保留 OrchestrationAgent 入口便于内部迁移；即使类名或 AgentBase 继承暂时保留，也不把它算成有模型推理的业务 Agent。不为改名建立第二套调度链。

行程模块归主 Agent 使用：组织规划提示词、解析结果和调用 guard；规划推理由 MainAgent 的模型调用完成。模块没有独立调度身份或模型循环。

拟提供 MainAgent.plan(context) 和 MainAgent.finalize(context) 两个阶段，由 harness 驱动。CLI 接入一个回合入口，两个阶段共享本轮需求和结果，不各自建立独立会话。

统一出口不等于每次都调用 finalize。主 Agent 初始决策可选择直接转交信息获取的完整答案，由 harness 校验并形成最终结果。信息获取已完成总结时，不再为简单天气、火车、酒店等查询强制调用一次主 Agent 模型。

## 5. 优先级与依赖

| 阶段 | 对象 | 说明 |
| --- | --- | --- |
| 0 | 主 Agent 初始决策 | 理解请求，确定需要的任务 |
| 1 | 偏好、记忆、RAG | 按需执行，无数据依赖时可以并行 |
| 2 | 信息获取 Agent | 使用已更新偏好及相关前序结果，执行内部工具 |
| 3 | 统一收尾；需要时调用主 Agent 综合或行程能力 | 简单查询直接展示完整答案；其余综合、补充提示或一次定向补查 |

数字表示执行阶段，不是能力等级。主 Agent 的阶段 0、3 不进入子 Agent 注册表。事项收集和三个领域工具不再占用顶层优先级。

实际依赖优先于数字：本轮更新偏好后查询，先提取并合并偏好；需要历史条件或政策约束的查询，等待对应结果。若 RAG 查询需要记忆结果，则先记忆后 RAG，仍使用其原输入协议。同属阶段 1 不意味着强制并行。

不相关节点可以省略。仅查制度不安排信息获取；只查火车不自动查酒店；天气、火车、酒店查询不会自动生成行程。资料完整的规划可以直接进入阶段 3。缺必要条件时返回补充提示。

## 6. 一轮请求的数据流

~~~mermaid
flowchart TD
    U[用户请求] --> M[主 Agent：理解与分派]
    M --> H[harness：校验与执行依赖]
    H --> P[偏好 Agent]
    H --> Q[记忆 Agent]
    H --> R[RAG Agent：现有实现]
    P --> C[本轮有效上下文]
    Q --> C
    R --> C
    C --> I[信息获取：按需执行]
    I --> T[火车 / 酒店 / 攻略 / 天气 / 网页工具]
    T --> I
    I --> F[主 Agent：综合与行程]
    I -->|简单查询且通过完整性检查| V
    C --> F
    F -->|有依据且未超限的补查| I
    F --> V[事实保护、展示、记录]
    V --> A[最终回答]
~~~

子 Agent 都是可选分支，不表示每轮全部执行。工具返回先交给信息获取的模型，信息获取结果交回 harness 的统一收尾流程；需要综合才传给主 Agent 模型。不能由最后完成的子 Agent 自动接管回复。无需子任务的问候或简单说明可以由主 Agent 直接回答。

## 7. 主 Agent 动作契约

初始决策包含 rewritten_query、intents、key_entities、agent_schedule、response_mode。response_mode 限定 direct、answer、itinerary；只有 direct 可以提供不依赖未执行任务的直接回答。

新增 finalization_mode，限定 forward、synthesize，缺省为 synthesize。forward 仅用于 response_mode=answer 且信息获取可以独立完成本轮问题的请求；direct 直接回答，itinerary 必须综合。模式由主 Agent 初始阶段选择，执行后由程序检查，不仅凭模型承诺直返。

子任务包含 agent_name、priority、reason、expected_output、depends_on。合法子 Agent 名仅为 preference、memory_query、rag_knowledge、information_query；harness 校验并去重，拒绝未知节点及依赖循环。

信息获取任务额外包含 requested_domains，取值为 train、hotel、guide、weather、web。它表达查询目标，不替代工具参数，也不要求调用所有工具。

| 最终 action | 输出 | harness 行为 |
| --- | --- | --- |
| answer | final_answer 与已有依据引用 | 校验后呈现 |
| itinerary | 候选 ID 和行程结构 | 经行程模块和事实保护后呈现 |
| needs_input | missing_fields 与补充提示 | 暂停依赖任务，等下一轮用户输入 |
| needs_requery | reason、domains、constraints | 校验后定向回到信息获取 |

needs_requery 仅支持信息获取覆盖的领域，本轮不新增 RAG 自主补检索循环。政策不足时说明缺失或询问用户，不绕过 RAG 冻结边界。无效格式或动作返回清楚的错误，不自动转成付费查询。

直返检查同时满足：初始模式为 forward；信息获取 status=ok、summary 非空、missing_fields 为空；所有请求领域都有成功且有效的结构化结果；无待完成任务、补查标记或需要一起回答的其他子任务。调用多个工具本身不禁止直返，关键是信息获取是否已独立完成问题。偏好或历史仅作前置资料时不强制再总结；需要合并其他 Agent 的回答时进入 synthesize。

直返不跳过来源、候选、预算、业务状态和输出保护。失败、部分成功、缺字段、空摘要、结果未覆盖请求或需跨 Agent 综合时，转入主 Agent 最终阶段；不能因不再调用模型就掩盖缺项。本轮仅直返信息获取的完整答案，不新增 RAG/记忆回答直返分支。

## 8. 上下文与本轮偏好

入口用程序字段保存 original_query，即用户原文，不由模型重建；rewritten_query 用于理解。主 Agent 和信息获取都能读用户 query，不是只有初始意图阶段能读。

主 Agent 最终阶段读取两种 query、response_mode、当前北京时间、相关会话资料、effective_preferences、各子任务结果、结构化条件、候选及此前反馈。

信息获取读取两种 query、任务目标、requested_domains、已确认条件、有效偏好、相关政策/记忆结果和补查要求。主 Agent 仅看到子 Agent 能力摘要，信息获取模型才收到五个查询工具的完整 schema。

PreferenceAgent 成功返回后，harness 按现有 append/replace 语义合并 effective_preferences，再准备后续输入。不等整轮结束持久化后再重读；本轮合并与最终持久化分开，反馈重入不能重复追加。

用户本轮明确要求优先于旧偏好。临时“这次住便宜一点”不自动变成长期偏好；家庭住址不自动成为出发地。偏好提取失败不覆盖已有偏好，本轮明确条件仍可从原文整理。

上下文按任务选择，不复制全量历史和原始工具响应。网页、记忆和 RAG 材料属于参考数据，不是新的系统指令。

## 9. 信息获取内部链路

继续使用 InformationQueryAgent 类名，显示名称改为“信息获取”。

读取需求 → 整理条件 → 选择工具和参数 → 程序校验 → 工具执行 → 真实结果交回同一个 Agent → 判断补查 → 整理候选与综合摘要。

条件提取不再是固定的独立事项收集模型调用，可以在选择工具过程中完成；仍返回结构化 travel_conditions，供工具、主 Agent 和记忆使用。

使用模型原生 tools/tool_choice 协议。流式参数收齐后执行，调用 ID 与结果一一对应；未知工具或无效参数不执行。无依赖的多个工具可并行，单项失败不吞掉其他成功结果。

技术复核补充：当前 AgentScope SDK 逐次返回累计内容快照，并可能修复尚未完整的工具 JSON。适配器只在流结束后读取完整调用；存在 raw_input 时严格解析原始 JSON，不能执行中间修复出来的参数。对话使用模型原调用 ID，执行记录使用本轮/本次执行作用域避免跨反馈 ID 冲突。

信息获取自己总结，不增加总结 Agent。模型解释和比较结果；候选价格、库存、时间、来源和状态来自程序校验的数据，不由摘要重写。

本地 SDK 支持 tools 不代表实际模型已联调。实施用假模型验证协议；真实配置不支持工具调用时返回能力错误，不退回编造查询结果。

## 10. 工具、条件与 Provider

五个内部工具：train_search、hotel_search、travel_guide、weather_query、web_search。前三者从 AgentBase 包装类迁为函数，不持有模型、不进顶层调度。天气和网页从现有实现提取为工具；网页只返回检索材料，摘要由信息获取生成。

复用 travel_data 的 TrainQuery、HotelQuery、GuideQuery、AgentDataResult、Source、Provider 和报价类型；拆解 agent_support 对旧事项收集 Msg/previous_results 的依赖。

- 火车要求起终点、日期，沿用乘车人数未知时默认一人的既有规则。
- 酒店要求城市、入住/离店日期、人数；不默认一人，不直接套用出发或返程日期。
- 攻略日期未知时只给不依赖日期的可验证资料，不声称当天开放或天气已确认。
- 缺字段、无效日期或区间时不调用对应 Provider，同时保留其他成功结果。

JuheTrainProvider 保持站到站查询和北京时间今天起 15 个日历日范围。接口没有 limit/page；车次类型、可订状态、出发时段如需过滤，只使用官方支持的参数，并纳入缓存键。

## 11. 候选、补查和价格保护

本轮保留完整已校验候选池；给模型和主 Agent 的候选视图默认火车最多 5 个、酒店/房型报价最多 5 个，可配置。不足时按实际数量返回。火车候选为车次加席别组合，默认排序避免同一车次的席别占满窗口。

先按明确硬约束筛选，再按偏好排序。攻略按相关性和来源组织，不机械按酒店条数截断依据。预算用 Decimal，区分每晚、入住总价、人数与全程预算；未知价格不算预算合格，部分价格不能宣称全程预算已核实。

同参数请求复用本轮候选池，查看下一批使用本地 candidate_offset/候选窗口，不传给聚合 API。真实接口参数变化或明确刷新才新查。全量数据留在程序，不反复塞入模型上下文。

首批超预算可以看其他候选或调整用户允许的查询条件；不能静默提高预算、换日期或降低标准。无合格结果时如实说明，由用户决定是否改变要求。

候选 ID 和来源保持稳定；行程只能引用已有 ID。事实保护重建价格、库存、来源和预算，丢弃模型伪造 ID 或事实字段。

## 12. 信息获取输出与行程输入

| 字段 | 内容 |
| --- | --- |
| status | ok / partial / needs_input / unavailable / error |
| summary | 基于工具结果的综合摘要 |
| travel_conditions | 已确认条件、本轮约束和必要缺项 |
| domain_results | train / hotel / guide / weather / web 分领域结果 |
| missing_fields | 待用户补充的字段 |

domain_results 只含本轮请求或实际执行的领域。前三者保留 query、items、missing_fields、source、fetched_at、message、status；天气和网页使用相同外层字段，items 是对应的带来源资料。

全部失败、部分成功、未配置来源、缺条件和空结果保持区别。空列表不自动等于无票/无房，只有来源确实表达此事实时才如此显示。整体状态由程序结合请求领域和实际返回判断，不只相信模型自述。

主 Agent 行程阶段读取两种 query、effective_preferences、travel_conditions、domain_results，以及相关的旧协议记忆/RAG 结果。summary 只辅助理解，不替代候选事实。

原 plan-trip 提示词、解析和 guard 迁为行程模块，模型调用由 MainAgent 发起。guard 读取嵌套 domain_results，必要时使用内部适配器复用现有事实重建，不重新注册领域 Agent。

缺必需用户条件时返回 needs_input；部分数据不可用时可以给明确标记待核实部分的建议。不能把未核实建议说成已确认事实。

## 13. 反馈循环与默认上限

信息获取负责工具层补查；主 Agent 可发现跨领域组合冲突，返回具体 needs_requery 原因、领域和可执行约束。

harness 只重跑受影响的信息获取任务，复用成功结果和缓存，再交回同一主 Agent。不重跑初始决策、偏好、记忆和 RAG，不将中间补查方案保存为最终行程。

| 项目 | 拟采用的可配置默认值 |
| --- | --- |
| 主 Agent 初始决策 | 1 次模型调用 |
| 主 Agent 最终阶段 | 简单查询通过直返检查时 0 次；其他请求最多 2 次，包含补查后综合 |
| 每次信息获取 | 最多 6 次模型调用、10 次工具调用 |
| 主 Agent → 信息获取全局反馈 | 每轮最多 1 次 |
| 单轮信息获取累计 | 最多 2 次执行，即 12 次模型调用、20 次工具调用 |
| 内部工具超时 | 沿用 30 秒边界 |

限制不包含偏好、记忆和冻结的 RAG 内部既有调用，但反馈不重复触发这些节点。工具层补查计入信息获取预算，不另开子 Agent 绕过上限。

无新查询依据、缺必要条件、触及硬约束或超限时停止，保留已知结果并解释未满足事项。纯问答不强制查询两轮。

## 14. Skill 与权限归属

文件夹本身不提供权限隔离。隔离由注册白名单、上下文装配和各模型实际收到的 tools 列表实现。

- query-info/SKILL.md 指导信息获取整理条件、选工具、补查和总结。
- plan-trip/SKILL.md 保留为主 Agent 按需行程指南，不广告为可调度子 Agent。
- preference、memory-query、ask-question 保留角色入口；本轮不改 ask-question 文件。
- 旧事项收集和三个查询 Skill 的有效规则迁入信息获取指南、参数契约或工具描述，旧 Agent 入口退出发现和加载。
- 不为每个简单工具新增专属 Skill，避免与工具 schema、程序校验重复规定参数。

SkillLoader 保留完整内容读取，但主 Agent 的摘要只列四个允许调度的子 Agent。五个内部工具 schema 仅给信息获取，主 Agent、偏好、记忆和 RAG 不获得这些工具。能读取文件不等于获得执行工具权限。

## 15. 输出、重试和记忆

CLI 只有一个最终回答出口，接收 harness 的最终结果：直返时展示信息获取的完整答案和来源；综合时展示主 Agent 的回答。仍按结构化数据展示候选、价格、来源、缺项和失败。不能逐个子 Agent 打印多份最终回复，也不能让模型自由文本覆盖已核验的价格。报价、库存和行程地点采用相同的事实保护，直返摘要也不能引入未验证的事实。

最终结果记录 finalization_method=direct、forward 或 synthesize。直返仍保存一条最终 assistant 消息、子 Agent/工具执行记录和来源；不记录实际上未发生的 main:finalize 模型调用，也不生成行程历史。信息获取工具和摘要的模型调用仍正常计费、记录。

只有用户请求且完成行程生成才保存最终行程；历史字段改读 travel_conditions 和最终选项，不依赖 event_collection。中间 needs_requery 不保存为最终行程，偏好持久化避免重入追加。

CLI 当前按顶层 train_search 判断能否重试的逻辑改读实际工具请求记录。真实请求已发出后，摘要、最终综合或日志失败不重放整轮查询。本轮候选和缓存跨反馈保留，不跨用户共享。

MeteredModel 记录主 Agent 决策/综合/行程、信息获取及现有子 Agent 的模型调用。内部工具记录名称、ID、状态、耗时和缓存命中，不误统计为有模型推理的 Agent。

主 Agent 最终模型失败时返回明确错误，保留可展示的已校验结果，不因此重发付费请求。外层运行状态与内层业务状态分别记录。

## 16. 拟修改文件

路径相对本仓库，表内产品文件尚未修改。

| 文件或模块 | 修改内容 |
| --- | --- |
| agents/main_agent.py（新增） | 初始决策、最终综合和模型行程能力 |
| agents/contracts.py、agents/model_io.py（新增） | 主 Agent 动作校验、本轮状态、完整模型响应适配 |
| agents/intention_agent.py、agents/__init__.py | 迁移能力和内部导入，退出独立意图节点 |
| agents/orchestration_agent.py | harness 回合、依赖、偏好合并、结果回传、反馈、保存 |
| agents/lazy_agent_registry.py | 子 Agent 白名单，保留原 RAG 映射和构造方式 |
| utils/skill_loader.py | 按角色筛选摘要，保留所需完整指南读取 |
| .claude/skills/query-info/script/agent.py | 条件整理、模型工具循环与摘要 |
| travel_data/tools.py、travel_data/candidates.py（新增） | 执行器、候选池、校验、超时、缓存 |
| travel_data/public_query.py、travel_data/result_guard.py（新增） | 提取天气/网页检索，以及直返和综合共用的来源保护 |
| travel_data/agent_support.py、contracts.py、providers.py、juhe_train.py | 复用来源与查询契约，解开旧节点耦合 |
| agents/itinerary_module.py（新增）、travel_data/plan_guard.py | 行程指南、解析与嵌套结果事实保护 |
| .claude/skills/plan-trip/script/agent.py | 有效逻辑迁出，退出独立规划 Agent |
| .claude/skills/plan-trip/script/plan_trip_execution.py | 改接统一回合入口，不再加载旧事项收集与规划节点 |
| 旧事项收集、火车、酒店、攻略入口 | 迁移后退出发现，消除第二套活跃路径 |
| cli.py、config.py、context 中受影响代码 | 单一最终出口、配置、注入、重试、记录 |
| query-info/SKILL.md、plan-trip/SKILL.md、Skill README、项目 README | 同步职责、示例与字段 |
| tests、evals/v0_memory 中受影响用例 | 更新路由、结果、事实保护、评估 |

偏好与记忆默认只调整调用方和必要适配，不重写内部能力。RAG 文件和数据不得进入本轮产品修改清单。

旧节点名不保留为可调度别名；CLI 用户入口与有效的来源、预算、记忆行为延续。若发现外部消费者依赖旧 Python 类或调度协议，先列影响讨论，不擅自建立长期双轨兼容层。

## 17. 设计级实施任务（未开始）

详细实施计划须引用本节，展开文件、接口、先失败测试、命令与完成证据，不重新定义产品行为。

- [ ] T1 主 Agent 与行程模块：迁移意图/规划能力，建立单一主 Agent。
- [ ] T2 harness 与上下文：依赖、阶段间偏好合并、结果回传、完整性检查和直返，兼容 RAG 旧协议。
- [ ] T3 信息获取工具循环：合并条件提取，迁移五个工具，验证模型调用协议。
- [ ] T4 候选与事实保护：本轮池/缓存、5 个候选视图、预算与行程重建。
- [ ] T5 反馈与上限：定向补查、跨反馈去重、缺条件和错误处理。
- [ ] T6 注册与入口：清理旧节点、Skill 摘要、CLI、独立规划脚本。
- [ ] T7 输出与记忆：统一直返/综合出口、最终行程保存、真实模型调用遥测和工具评估。
- [ ] T8 回归与核对：关键场景、RAG 冻结、测试和设计一致性。

任务有耦合：契约先明确，harness 和信息获取接通后再验证反馈，不默认全部并行，也不默认派发子代理。

## 18. 验收场景

1. 五个业务 Agent 角色；harness 和工具不计入有模型推理的 Agent 数量。
2. 只查火车/酒店/攻略/天气：信息获取工具正确，结果交回主 Agent，不生成额外行程。
3. 请求完整行程：主 Agent 分派后生成行程；独立规划脚本接入相同入口。
4. 制度单独问答：原 RAG 类与输入协议不变；主 Agent 接收成功、无知识和错误结果，不假定已接 MCP。
5. RAG 缺引用不补造来源，初始化失败不包装成已核实制度。
6. 偏好更新查询和反馈：下游看到新偏好，不重复追加；临时要求不持久化。
7. 同阶段真实依赖顺序正确，无依赖任务可并行/省略，不读取未完成结果。
8. 主 Agent 与信息获取读取原始 query，改写不丢掉预算、日期或硬约束。
9. 缺字段或无效日期不调用 Provider，保留其他成功结果并提示补充。
10. 多工具、流式分片、无效参数、未知工具：完整调用与结果配对，不执行无效调用。
11. 首批超预算可查看其他候选，不静默放宽条件，最终无合格结果时如实说明。
12. 候选多于/少于 5、不同席别、空列表：数量和来源正确，全量候选不丢失。
13. 同参数补查/反馈复用缓存，真实参数变化才新查，不发假分页参数。
14. 未配置、超时、空结果、部分成功：各层状态正确，不声称已取得未查询资料。
15. 伪造候选 ID、报价、库存、地点或预算：最终事实由真实数据重建，不采用伪造值。
16. 组合冲突只查受影响领域，不重跑偏好/记忆/RAG，全局反馈最多一次。
17. 达模型/工具上限停止，保留结果及未完成原因，不无界循环。
18. 真实请求后摘要、最终模型或写入失败：不重放整轮付费请求。
19. 一个最终回复出口，候选和来源可展示；最终行程正确保存，中间补查不写为最终计划。
20. 主 Agent 摘要仅列合法子 Agent；工具仅给信息获取；RAG 完整 Skill 读取仍正常。
21. RAG Agent、Skill、配置、数据相对实施基线无修改；外部 MCP 项目无本轮写入。
22. 记忆、预算、来源、遥测和 RAG 协议回归通过，评估区分 Agent 与内部工具。
23. 简单天气查询成功且完整：主 Agent 初始决策 1 次，最终综合 0 次；统一出口展示信息获取答案、日期和来源，保存一条最终消息。
24. forward 请求出现部分成功、缺字段、无摘要、未覆盖领域或需要组合其他 Agent 答案：不直返，交给主 Agent 处理。信息获取内部使用多个工具且已独立完成问题时可以直返。
25. 完整行程、跨 Agent 综合或补查：不能因某个信息获取结果成功就跳过主 Agent；直返路径伪造报价和来源也受保护。

## 19. 测试和完成条件

本次只做设计文档的内容与 diff 检查，不把它说成产品测试通过。

实施时用假模型、假 Provider、假 RAG Agent 和本地响应写有意义的失败测试，验证动作、依赖、实际调用次数、来源保护和最终展示；不调用付费 API。真实联调单独记录，未联调时明确说明。

已有测试重点包括 sourced_routing、orchestration、lazy_agent_registry、sourced_agents、sourced_itinerary、juhe_cli_wiring、v0_memory_flow、sourced_evals，新增主 Agent、工具循环与反馈测试。旧流程断言随设计迁移，保留其中仍有效的保护。

后续最终至少运行现有离线检查：

~~~powershell
python -m pytest -q tests --ignore=tests/test_intention_agent.py
python -m compileall -q agents context travel_data utils cli.py
git diff --check
~~~

当前 CI 忽略旧真实意图模型测试。新的 MainAgent 必须有纳入 CI 的假模型离线测试，不能借此不验证主 Agent。编译检查还需覆盖实际保留的 Skill 脚本，由详细计划明确路径。

完成条件：T1–T8 均有实现与验证证据，关键场景覆盖，必要测试通过，按用户选定方式完成审查，逐项核对本文，RAG 冻结无偏差。未启用 OpenSpec，人工逐项核对，不报告已运行 OpenSpec verify。

## 20. 后续顺序

本设计已审阅；详细实施计划保存到 docs/superpowers/plans/2026-10-09-main-agent-refactor.md。用户审阅计划并选择单代理或多子代理执行后实施；尚未开始修改产品代码。

其他改造完成后，再单独设计 RAG 的 MCP 接入、检索结果回到模型、必要补查和来源输出，不作为本轮完成前提。
