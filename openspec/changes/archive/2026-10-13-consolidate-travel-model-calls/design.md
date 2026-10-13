## Context

2026-10-13最终要求为主规划、低频偏好分析、无模型记忆工具及出差政策前置。本版替代此前查询子Agent批量总结的3次方案，目标正常简单行程2次主模型调用。三层状态简化现已由未来记录提升为当前立即修改范围。本次仅维护文档，未实施产品代码；执行方式尚未选择。

依据现有main、Harness、Runner、ToolExecutor、偏好存储和上海实际日志。现有偏好Agent输出append/replace，调用方保存；LongTermMemory保护source=user及手动删除记录。ToolExecutor已验证任务条件并保存候选，主模型可通过投影直接判断。

## Goals / Non-Goals

**Goals:** 前置无模型偏好/历史工具、低频分析和必要出差政策；主决策安排任务，程序隔离并行查询并直接回传，末次主决策判断与回复。

**Non-Goals:** 不将HTTP、数据库、工具预算或全程校验搬进模型，不删除静默信息Agent源码，不重建RAG底层，不改变住宿/返程默认规则，不重构外部RAG项目。

## Decisions

### 0. 立即修改：三层业务状态简化

本项取代先前延期记录，纳入当前change。TripPlan.status只允许running/completed；task.status只允许pending/draft/validated；query_container.status只允许empty/ready/failed。running描述未完成的工作方案，不能作为不断轮询的调度条件；completed须当前任务与全程校验通过并保存成功。HTTP/provider/result和会话回复诊断状态沿用现有边界，不能混写到这三层业务status。

needs_input、等待依赖、失败原因及暂停/取消意图复用missing_fields、checkpoint、depends_on和stop_reason表达；程序从记录推导是否可执行。取消/暂停即使映射running也不进入自动恢复和活动可执行列表，用户明确恢复才按既有语义处理。活动小结包含必要等待/停止摘要，SQL发现及恢复守卫同步修改，保持先小结后加载。历史偏好有效样本筛选仍排除取消/失败/模拟记录。

执行请求记录至少保留request_id、task/条件版本、开始/完成信息及结果引用，调用前登记在途信息，完成后按身份归并；不将这些记录作为新的任务或容器状态枚举。崩溃后只有开始而无确定完成时保留原因并核对，不能因容器empty就自动重发，失败重试不超出现有策略与预算。

task正常pending→draft(query)→draft(selection)→validated。有效候选改选：工作task validated→draft→validated，容器ready保持；参数改变且无匹配证据：task回pending，容器旧引用转history后empty，再查询为ready/failed。查询成功空列表仍ready并留缺口；缺失酒店报价不等于预算合规。校验失败保留draft，不生成validated；未应用的失败修改保留上一已保存方案，合法工作变更已持久化且未完成时TripPlan为running。

旧顶层needs_input/partial/error→running，paused/cancelled→running并保存stop_reason/停止守卫；旧task.running/needs_input→pending，其他现有三态语义保持。旧容器stale/blocked/querying→empty并保留失效/缺口/请求记录，ready核验当前绑定，faild兼容映射failed。业务ID/CAS版本/查询历史不丢失，正常读取投影转换并在受版本保护的保存中同步权威快照与小结，不全库扫描重写。非法未知状态报告兼容缺口，不能猜测为可执行；旧completed/validated的校验有效性继续核对。

### 0.1 WorkflowRunner统一整个请求的流程控制

现状：ExecutionHarness并非底层工具执行器，run_turn/_run_turn目前承担主initialize、前置委派、旅程匹配/加载、Runner派发与最终回复，并设置请求总超时；WorkflowRunner.run要求已有workflow，只负责旅程内部循环且另设超时。实际供应商调用和单工具超时在travel_data/tools.py的ToolExecutor；归一化、候选校验与投影清洗分布在provider/candidates/guard/context_projection。并发隔离按本change的工具适配补齐，不能将现状误认为Harness已经独立承担全部底层能力。

目标：提升WorkflowRunner为唯一请求orchestrator，新增run_request(context, run)入口，workflow可尚不存在。Runner按需协调明确偏好更新/重读、首次历史分析及政策资料，安排主模型调用，定位/创建/加载TripPlan，派发独立查询与依赖任务，串行归并结果，推进三层状态，校验保存、局部修复及输出回复。覆盖纯记忆操作、独立查询、新规划、已有行程修改和澄清/恢复；纯记忆操作直接工具返回，不为了统一入口多调用模型。复杂意图仍由主Agent识别，不另加路由模型。缺资料保存checkpoint并结束本轮，不能把running理解为持续循环。

ExecutionHarness保留run_turn等现有调用接口，缩为入口兼容适配并转交Runner，不再独立执行initialize/finalize、前置Agent调度或第二个业务循环。原有上下文/返回值、审计关联保持兼容；请求级总超时、模型/工具总预算与用量累计只有Runner一处控制，单工具超时仍在ToolExecutor。旧Runner.run可作为已有workflow的兼容入口，共用内部实现与限制，不能与run_request串联重复执行。迁移现有协调逻辑，不新增一个模型Agent或通用编排框架。

工具执行层维持既有供应商/缓存/分页/单工具超时协议；batch_execution负责独立请求作用域、预算预留接口、并行执行与结果收集，result_views等纯程序负责有界清洗。Runner决定何时执行及如何推进业务状态，并通过既有helper/Store完成串行归并和事务保存；工具层不能另起业务调度或冲突决策循环。保存失败与取消沿用既有安全边界，不为此次职责迁移扩展供应商重试策略。无需把底层清洗搬到Harness，只对Harness原流程职责做必要转交，避免双重编排。

### 1. 规划前的偏好、历史和政策

明确偏好update/get与历史list/read改为无模型工具，priority最高的更新完成并重读后才继续。结构化操作命令和规则可可靠识别的长期表达直接增量保存完整自然语言；仅偏好读写不调用主或子模型，解析不明确时澄清。本次临时条件不持久化，保留source=user及删除保护；规划混合请求同样先执行明确更新。

旧preference/memory_query模型能力合为PreferenceAnalysisAgent，自动触发仅用于首次历史总结：默认3份有效唯一行程，按trip_plan_id去重，取消/失败/模拟不计。每用户持久化initial_summary_done和证据引用，合法结果保存成功才置为true；合法空总结也完成初始化。初始化后新增至6份、10份或恢复会话均只读取已有记忆，不再定期分析；用户明确要求重新根据历史总结才再次调用，明确偏好更新不触发分析也不重置标记。失败保留记忆与真实状态，本轮可继续，不无限重试。

偏好存为可增量的自然语言记忆集合。结构化的是必要外壳：id、text、source（explicit/history）、status与历史条目的evidence_trip_ids等元数据；不固定维度、倾向枚举或强制把条件转为键值。分析返回memories条目及covered_trip_ids，text保留适用条件、例外、软倾向和不确定性，输入仅有摘要、选择来源与现有记忆。历史推断与明确表达同属长期记忆但以source区分，推断不覆盖明确表达及删除保护，不把政策强制或助手建议误当用户意愿。

明确表达“以后出差都尽量给我选择高铁”时，程序识别长期记忆操作并直接保存完整text，无需解析所有语义条件、无需调用模型。add追加，replace/retract只针对可明确定位的id，歧义澄清；不同条件可并存，追加不重写整份记忆，不为语义去重偷偷调用分析。主Agent在已有规划调用内读取有效text并判断适用性；同一情境下明确表达优先于历史推断，政策与本次固定条件优先于软偏好。现有source=user和删除保护继续有效；旧固定键偏好可投影为自然语言读取视图，保留原记录，不将历史旧推断误标为已完成本次首次总结。

出差判断与资料准备前置于主规划：明确出差可由程序入口及已有目的标识判断；含糊目的澄清或必要分类，额外调用按实记录。TravelPolicyAgent继承现有rag_knowledge检索服务，完整负责“检索政策→理解适用条件与例外→提取约束→带来源JSON返回”，不重建索引。外部service现仅返回拼接answer和citations，应直接取引用片段进行一次模型理解与提取；旧本地路径将通用答复替换为结构化提取，不串联通用回答再转JSON。返回status/constraints/applicability/references/effective_date/missing_fields/conflicts；约束以稳定id、规则说明、适用条件、hard强度、可支持的field/operator/value/unit和evidence_ids表达，复杂规则保留文字及待核实状态。来源来自原检索身份、位置、可用版本/有效期，程序校验结构、引用、类型与适用性后再交主规划。主Agent只收紧凑约束和来源索引，原文证据由原有审计/存储保存，不能丢强制规则或捏造缺失定位。只在出差或明确制度要求时调用，适用政策强制，偏好非强制；制度未知不宣称合规，不假定审批。

### 2. 查询工具并行执行，直接返回主Agent

query_plan在新旅程以task_index绑定程序生成的任务ID，旧旅程以task_id/task_revision绑定；domain与parameters支持白名单条件字段引用。程序prepare_task后校验参数、范围及依赖，执行条件满足才派发，未满足时保留派生的缺口/依赖原因，不写容器blocked。独立火车酒店天气查询以request_id关联，不强制创建完整旅程。

工具执行服务是普通程序模块，通过ToolExecutor执行既有查询；没有model或AgentBase，也不调用InformationQueryAgent任何入口。information_query本阶段从调度映射退出但保留设计与代码；所有领域不再回调，天气并入工具，未迁移的网页/攻略入口明确未启用，不增加意外功能或隐藏降级模型。保持缓存、分页、审计、工具预算、超时和record_query。现有执行器使用current_task_id共享状态，不能直接跨任务gather。新增执行适配按request构造独立作用域及只读条件快照，供应商请求并行，结果携带task/query身份由协调器串行归并/保存；调用前统一预留总预算、处理缓存，失败释放未启动额度。保留task依赖：同一task的独立火车/酒店以及天气可并行，只有已具备条件的跨task请求并行，不要求串行等待无关任务。保存失败终止后续推进，不自动重放已启动请求。

旅程待执行队列存入workflow.execution_plan，逐项记录身份、命令、开始/完成时间、结果引用与必要缺口；待执行/完成/阻塞由这些记录派生，不增加三层业务状态枚举。主Agent输入按task_id/task_revision分组的有界task_results，非旅程按request_id分组；不同任务同为train不能相互覆盖。不发送完整候选缓存、URL/证据长文或工具往返消息作为主历史，仍保留模拟、过期及未知标志。

#### 2.1 接口返回清洗与上下文投影

现状不是原始响应直塞上下文：JuheTrainProvider逐车次/席别校验时间、价格和库存并转TrainOffer；AmapHotelProvider提取HotelPlace并按地点id去重；PublicQueryProvider.weather提取目标日期预报或当前天气。ToolExecutor复用candidate_view与guard_domain_result，context_projection再筛字段和默认5候选。完整归一化有效候选在CandidateStore/查询记录，模型视图与之分离。

已发现边界：candidate_view对weather直接返回，不在工具层限条数；description/message没有长度上限；主投影FACT_FIELDS未含当前temp_c，并删除query导致天气城市可能缺失；hotel unverified_constraints也不在DOMAIN_FIELDS中。合成输入证实7条重复预报的工具输出仍为7条，投影仅限制为5条而每条2000字符；当前22℃被投影漏掉。这些属于待实施修补，检查记录见docs/diagnostics/2026-10-13-tool-cleaning-and-policy-audit.md。

采用现有归一化→程序校验/按身份去重→约束筛选及排序→有界视图流程。提供共享纯程序入口build_tool_result_view(domain, result, *, limits, requirements=None)，复用既有候选分页及context_projection，新增travel_data/result_views.py集中领域白名单、去重键和自由文本长度，不增加模型摘要。火车去重保留不同席别、日期和报价身份；酒店地点与房型报价分开。先验证有效数据，完整归一化候选仍保存，模型用独立拷贝；不为此新增原始HTTP载荷副本。

默认train/hotel每页5个，继续candidate_offset并返回candidate_total/view_offset/has_more；weather每城市/日期请求去重后1条，多个日期分别绑定request_id。自由文本默认description 128字符、message 256字符、名称/地址256字符，可配置；有截断标记，只作用于展示文字，不裁id/金额/日期/引用/状态。保留匹配城市、请求日期、temp_c或min/max温度，不把不支持日期替换成当前天气。酒店地点保留未知房价/空房和unverified_constraints，报价保留入住范围与费用口径。

每个结果保留task/request/query身份与版本、source_id及fetched_at，去重的sources索引保留provider/证据定位所需身份，不重复URL全文；政策references另用专用投影避免现有通用清理删掉引用。候选在本轮主输入仅出现一次，既不发送照片/营销原文/完整缓存，也不在tool消息和task_results重复。相关必需规则/缺口不得为压缩消失，完整结果仍供程序校验与已有缓存分页。文本统计只用于离线比较，不冒充真实token usage。

### 3. 一次批量选择与自动校验回复

#### schedule与容器状态驱动

schedule是主Agent生成的旅程任务安排，对应现有workflow_proposal/TripPlan，不另造一套独立权威计划。去程旅游标签映射purpose=visit，返程purpose=return；程序按任务授权领域初始化query_containers，去程需住宿时为train和hotel，返程仅train。任务ID、版本与真实状态由程序维护。待写入是尚未接受的提案，持久化成功后task.status=pending，不把内存提案当已保存。

每个容器记录domain、parameters/field_sources、status、current_query_ref、history_refs和invalidation_reason。status仅empty（当前条件尚无结果）、ready（当前取得结果，包括明确无候选或部分数据）和failed（实际查询失败）。参数/时效失效时保存旧引用至history、解除current_query_ref并回empty；不再有stale。请求在途与等待依赖从执行记录和条件派生，不写querying/blocked。调用前记录请求身份与开始信息，恢复遇到未知完成的请求不自动重放，乱序返回按请求及条件版本校验后归并。有效ready还须通过来源时效、task/query版本与参数校验；不能仅看标签就复用。供应商成功空列表也是结果，不无限按empty重查。具体provider失败/默认日期重试继续沿用已有策略与预算。

状态推进由Runner执行：检查task标签及容器→执行/复用→按身份保存→该task形成查询草稿→推进依赖就绪的task；相互独立请求可并行。持久化task.status=draft时增加draft_stage=query标识，保存查询引用和缺口；此阶段draft_plan尚非最终选择，校验器不能将其当已选方案。末轮主选择通过后draft_stage=selection并复用正式draft_plan重建校验。旧draft有draft_plan但无draft_stage时按selection兼容读取；pending_write不写入权威任务状态。

正常路径所有task均达到查询草稿后一次调用select_batch；必要条件缺失、依赖阻塞或容器failed时保存缺口及checkpoint，TripPlan仍running，task为pending或已有draft，本轮返回可表达needs_input/partial但不能写成业务状态；执行队列耗尽可请求一次主决策处理缺口，不依赖全部draft硬等待。全程判断使用任务索引、有界候选和必要边界，主Agent判断合理性并选择，程序验证客观约束与引用后才completed。

修复继续通过repair_tasks的根任务、components和受控条件更新提案执行。模型可指出火车、酒店或日期问题，不能直接任意篡改标签或版本。程序更新条件和对应容器状态、计算依赖影响、保留无关有效容器；日期属于schedule/conditions组件，不创建日期查询容器。酒店修改不失效交通，日期修改按参数和实际边界影响失效相关容器；旧候选保留历史但不得未经重绑定校验直接使用。

MainAgent.select_batch接收任务索引、有界候选和缺口，输出commit_batch（预期workflow_revision、draft_tasks、reply_template），也可schedule_queries或采用既有受控修复/澄清。程序整批验证结构、版本及候选引用，保存有效草稿，复用check_task/check_workflow/finalize_workflow。校验未通过不发布completed模板。

回复继续使用reply_facts。预先给出可用占位符键目录；实际值与required键在提交后从真实校验结果重建，render_reply_template核对，不合法用程序事实兜底。Runner统一收尾，Harness仅转交最终结果，不再compose_workflow_reply或独立final_model，不额外决定validate/finish。

### 4. 路径、兼容与清理

正常简单行程：程序先更新/重读偏好、按需低频分析和出差政策→主initialize（schedule+查询安排）→程序执行独立工具→主select_batch（选择+模板）→程序校验展示。上海基线固定成功回放为2次主模型、3项查询、0次preference/information_query子模型。真实未满足依赖、冲突和非法输出允许追加决策。

程序工具阶段完整保留逐task状态机：首轮schedule→去程双容器查询及查询草稿→返程火车容器查询及查询草稿→末轮主判断。查询计划执行状态必须与容器/查询记录协调，由容器是否有效决定复用或重查；队列只是可恢复的执行索引，不能成为与容器竞争的第二套业务状态。

偏好-only为程序更新/读取及真实确认，0次模型；火车酒店-only也通过主query_plan和工具服务，不回旧子模型。information_query完整静默而非删除；memory_query改工具，旧偏好/记忆模型由低频分析替代；政策沿用rag_knowledge底层。旧数据不删除，注册入口及说明同步到当前角色边界。

复杂行程保持依赖和修复。恢复已有query/draft后仅执行缺失或过期查询，不重放有效请求。旧逐动作JSON可兼容异常修复，但正常入口只运行一次新路径。

### 5. 已有TripPlan的后续修改调度

TripPlan同时是已保存方案、查询证据及依赖关系的权威记录，复杂机制主要支持后续定向修改；无需模型主动选择“启动复杂流程”。每轮主意图识别合并目标定位和修改范围判断，程序据动作自动检查影响。首轮新规划继续按前述安排→查询→选择执行，本节不改变已确认的首次规划边界。

入口：主识别amend并给workflow/task/components及授权新条件→程序校验小结观测版本并加载所选快照/相关证据→主提交受控patch与回复模板（已有上下文足够时与识别合并）→程序检查/保存并回复。不要额外按识别、目标、分类、派发、validate、finish分别请求模型。只见小结时允许定位和修改两次主调用，目标与必要候选已知时普通修改目标1次；外部工具可以0次，内部数据库读/投影/校验仍执行。目标歧义先澄清，不加载所有旅行。

修改前程序计算证据覆盖：把新条件分成查询参数变化、候选筛选/排序变化、选择或建议安排变化。旧报价与新城市/日期/人数不匹配时不能复用；预算/软倾向变化可对完整有效缓存重筛，而不是以第一页无候选判断需要外查。同一查询有效时换选只改变selection和校验，保持query_container为ready；任务新版本的旧引用须经新条件/政策校验后受控重绑定，不能只修改task_revision数字。

主patch包含workflow_id/expected_workflow_revision、目标task_id/expected_task_revision、受授权condition_changes、已有selection_refs、允许的建议安排变更及reply_template，不生成程序版本或状态。使用真实候选构建草稿，不能把旧车次时间或报价日期改成用户想要的外部事实。每次本地改选照常调用check_task/check_workflow，按实际抵达/住宿边界检查依赖，未受影响的结果与选择保留。有效政策JSON在员工/项目/城市/日期等适用条件未变时复用，不重新检索总结；资料缺口、变更或刷新必要时按实际范围取得。

能直接完成则校验后事务保存权威方案与小结，程序渲染真实reply_facts，无新查询、摘要或收尾模型。不能完成则保留上次已保存方案与未应用提案/缺口；进入修复时延续受控checkpoint，缺证据的领域才查询，已有缓存足够时仍先重组。整个多段框架作为程序执行/检查机制，普通改选不重走逐task询问模型循环。新用户条件只影响本次方案，未明确长期更新不写偏好。

现有基础：travel_updates.apply_travel_update已有adopt路径、按组件保留与_rebind，workflow_queries.resolve_selection校验候选引用，Harness先小结后加载。但普通change/replace常清选择并重开pending，schedule/timing可能扩大到train/hotel，随后进入Runner；尚未有完整“先查证据覆盖再直接patch提交”的入口。新设计延伸既有模块，保留旧复杂修复兼容，不另造第二套计划状态。

### 6. 原后续项目纳入当前实施

用户明确要求后续修改也实施。使用项目runtime模块提供Msg和AgentBase轻量接口，模型适配使用已安装官方openai.AsyncOpenAI，兼容现有调用构造参数以降低迁移风险。适配完整消费SDK文本/工具流，以index聚合工具参数并校验后返回，usage映射真实输入/输出统计；单次SDK自动重试关闭，由现有外层重试负责，避免两层重试倍增。config_agentscope仅保留无操作过渡函数和配置，CLI移除框架初始化；所有旧技能/脚本/测试导入改项目协议。保留RAG检索和sqlite存储，不引入框架替代品。

只读能力通过Runner的read_trip动作接入，主Agent合并目标定位；WorkflowStore按用户加载并检查observed revision，提供read_saved_trip及read_saved_candidates纯读取接口。默认当前有效查询，每页5条，历史显式include_history；all通过分批页面展示，模型只接有界结果，避免一次展开完整缓存。读取不会保存业务状态或更新revision，明确刷新另走原查询路径。既有完整缓存用于候选，不增新数据源；将此处默认值写为当前规格和验收。

## Risks / Trade-offs

- 修改时错误复用旧参数报价 → 比对实际城市/日期/人数和证据有效性，选择失效不等于查询失效，变更预算用新条件重筛完整缓存。
- 模型未主动触发依赖修复 → 程序在每次patch后自动校验并计算影响，不依赖自然语言承诺。
- 同轮偏好与查询参数冲突 → 只使用程序接受的更新，本次明确条件优先，不能用长期偏好覆盖固定条件。
- 手动设置/删除保护 → 复用现有source=user限制，重新读取并确认实际值，不把模型建议伪装成已保存。
- 多任务输入增大 → 有界投影与按需补充，保留预算，不为大量原始结果增加默认摘要模型。
- 并发task作用域串数据 → 独立请求、统一额度预留及串行审计归并，不共享可变current_task_id。
- 恢复或保存失败 → 队列记录与权威query核对，保存失败终止，不自动重放。

## Migration Plan

先接通无模型偏好/历史工具及政策前置，并在工具调度前完成三层状态兼容门面/活动与恢复改造，再建立直接工具执行，最后接通批量选择/恢复和清理委派入口；离线回归验证后切换。复用现有SQLite记忆载荷保存条目和首次总结标记，保持删除保护和旧数据兼容。AgentScope移除及只读查询已一并纳入本次实施。

## Review Evidence

已核对偏好append/replace、保存保护、Harness的preference结果依赖与重复回复入口，以及ToolExecutor的任务作用域和候选写入。此前批量总结Agent方案废止；实施前按新边界复核计划并读取apply指导。详细执行计划仅展开本change，位于docs/superpowers/plans/2026-10-12-consolidate-travel-model-calls.md。

## Latest Revision

2026-10-13：最新角色、无模型偏好/历史、首次3份总结与条件性自然语言增量记忆、强制政策和独立查询并行已取代早期安排。上述80项现状测试是实施前基线，实际单代理实施及最终证据记录在 docs/superpowers/reviews/2026-10-13-consolidated-travel-verification.md。

实施细节复核：request_id/depends_on 使用依赖波次，完成且属于当前任务版本的记录可满足恢复后的依赖，未知完成记录禁止重放。完整快照加载/保存核对旧 completed 与 validated 的当前证据，小结仅规范状态，不加载全部行程。日期不足的酒店报价请求挂起；酒店地点查询无需报价日期，建议住宿日期仍须经实际抵达与全程校验。局部刷新使用 refresh_target.task_ids/components，失败修改保留原有效方案与 pending_amendment，恢复先处理暂存修改并核对在途记录。政策适用城市/日期可在主规划给出任务后补充核对，用量关联同一请求 turn。
