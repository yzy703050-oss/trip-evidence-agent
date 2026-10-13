## Context

初始失败基线为 docs/evals/2026-10-13-live-policy-measurement/；8次实际模型请求13,451 token，行程均失败。首次提案产生根节点purpose/seat_class/金额，修正又产生purpose=tourism或关键词数组；政策applicability为字符串且误加报价缺口。既有提示虽有字段列表，但类型、枚举示例及席别null表述不完整。不是token额度或供应商故障。

## Goals / Non-goals

修复模型边界说明和可观察诊断，真实回放证明。保留已有拒绝策略、两次普通主调用、一次政策提取。无新模型或供应商能力。

## Decisions

1. 新增 model_contracts helper，生成紧凑提案schema和一个合法结构样例。condition字段、purpose、来源和约束由现有常量复用；首次和repair都注入。标明hotel_keywords字符串、单程与全程预算及null条件含义；新workflow可省query_plan让程序生成既有查询，独立查询仍保留。
2. 政策提示补顶层/约束对象示例、支持字段表、席别值与单位区别。当前生效与已排除例外合并到可执行硬约束的rule，不输出软偏好为hard。复杂未知强制规则仍保留无法校验缺口；缺报价不是政策missing_fields。
3. 政策校验补顶层applicability、status和列表元素类型检查，已有来源、单位、金额检查不放宽。Runner失败审计保留异常类型和最多240字错误，不记录秘密或请求参数。
4. 回归从原输出中固定失败结构，并验证prompt中的schema/sample通过既有create_workflow和工具绑定。真实验收使用原三场景、原模型设置，输出新目录，保留失败基线。真实高德无报价导致partial是正确业务结果；模拟报价冲突需实际模型识别并局部重查后completed，明确供应商模拟边界。

## Technical Review

不使用兼容层猜字段或把hotel_keywords数组自动拼接；这种自动接受会掩盖预算误改。不改全局SDK响应格式，避免影响非JSON入口。测试必须验证预算与引用拒绝，不能只断言提示文字；prompt样例走真实程序校验。RAG本地文件、语义检索、价格来源不变。用户已选择当前目录、单代理，继续沿用。

## 实测补充复核（同一模型边界问题）

首次契约修复后，三次实测均正确完成初始规划、政策提取和实际查询，但批量选择缺少工具参数契约，产生hotel_keywords/quote_required/max_nightly_cny等非法参数；真实供应商缺失时反复提交，三场景合计68,963 token。原始输出保存在 docs/evals/2026-10-13-live-policy-fixed/，不能删除或只保留成功样本。

5. 批量上下文直接复用 ToolExecutor.schemas() 中的实际function参数和酒店能力描述，限于流程能调度的train/hotel/weather工具；不另写供应商参数白名单。提示说明query_plan、parameters.keywords/constraints/candidate_offset/refresh的精确层级，禁止捏造报价接口，缺候选时整个selection=null。正常不发送API tools，仍是一次批量JSON决定。
6. 修正已有allow_repair字段的停止语义：明确false且批量校验失败时，保存草稿后结束partial；默认仍有限修复。不能跳过check_batch_submission，不能升级为completed。模型依据已知供应商能力识别不可修复缺口，不能靠上限触发才停止。
7. TDD先验证实际工具契约进入模型、非法参数仍拒绝、酒店刷新只调用酒店和完整引用版本、显式停止只用两次主调用；实测先单独运行复杂冲突，再运行真实供应商，记录每个版本全部费用。

8. 单独冲突实测的模型已经正确发送refresh和参数，但被工具误拒绝：Runner合并政策采用str(Decimal(...))保存"500"，ToolExecutor两处用Python500 != "500"判断成修改预算。此为确定性边界类型错误。两处共享严格校验后的金额Decimal比较，保留原固定值、未知键和非金额精确比较；不同金额或NaN/Infinity仍拒绝。离线复放同一个真实动作先证明失败后修复，避免再次盲跑收费请求。

9. 金额比较修复后的实测确实调用酒店两次并产生新引用版本，但模型在760元/两晚、840元/两晚合规候选出现后仍返回同样刷新动作。不能宣称已成功；日志保留final-conflict。推断模型混淆总额和每晚上限。补充报价视图derived_stay_nights/derived_nightly_cny，由原报价日期和金额计算，忽略供应商同名字段，缺价不算；提示非空候选已通过本地筛选，优先选择。完整金额和最终guard不变。新增离线测试证明派生金额与边界后再单次实测验证，拒绝靠提高决策上限耗token。

10. verified-conflict真实模型在酒店全部被过滤时直接partial，并在缺口中承认需要刷新却要求用户再次授权。根因边界信息不全：schema的报价模式仅写泛泛hotel查询，视图没有原返回候选数，模型把“被500元限额过滤为空”当成“供应商没有数据”。增加报价模式描述、returned_candidate_total/excluded_candidate_count两个有界数值，提示已授权重查必须先执行一次。此与原需求一致，不新增模型/供应商；保留此失败记录及10,427 token成本。

11. 用户追问“是否要限制模型输出”：新模型金额边界添加共享validate_model_money，constraints金额键和支持的政策金额value必须为有限非负JSON数字，拒绝字符串和bool；不修改历史读取/ToolExecutor接受十进制字符串的契约。主首次/修正/批量/修改与政策提取调用同一检查；原真实成功输出离线重放此检查，不额外重复收费运行。

12. 用户明确要求忽略供应商查询失败，使用模拟数据：后续验收显式注入模拟火车/酒店，真实模型和本地RAG继续；不在生产失败路径添加自动假数据兜底。已有复杂冲突实测复用末轮成功记录；补普通旅游2主调用、普通出差政策1+主2调用。原真实供应商partial记录保留但不继续重试，其缺口不阻止本次编排修复验收。

13. 模拟普通出差已有380/420元每晚合规候选，模型仍连续请求刷新，4调用15,961 token后no_progress。这是查询动作/样例始终开放引起的控制边界缺口，不能刷成功样本。Runner按每个必需领域是否存在可选候选及已有validation.issues生成allowed_actions：完整候选且无冲突先commit_batch；否则保留既有动作。Main在commit-only分支不展示查询样例/动作说明，Runner拒绝不允许动作，反馈commit_required而不发供应商请求；程序提交后的实际校验仍处理跨段与政策冲突。没有新状态/模型阶段。补TDD和同场景实测证明普通出差3调用，再复测冲突4调用确保查询未被关闭。

## Risks / Trade-offs

17. 原始高德模拟实测首次批量提交正确保留酒店缺口，但未输出allow_repair=false，程序再问模型导致5调用24,449 token和无进展。编排器在完整保存/校验之后，若所有阻塞问题仅来自已确认地点接口缺酒店报价（含对应政策金额证据/全程预算未知），即以provider_capability_missing结束partial，不依赖模型选择是否继续。显式false保持model_partial；其他时间/火车/未知强制规则冲突不进入此短路，报价供应商也不进入。先测试遗漏false的真实问题和混合阻塞不短路，再只重测此未通过案例。

14. 用户要求模拟数据类似实际API：新增仅用于eval的原始响应夹具，注入JuheTrainProvider.http_post与AmapHotelProvider.http_get；生产解析、候选过滤和上下文清洗照常执行。火车覆盖多席别、金额字符串、有/无/数字余票、跨夜时长及坏行；酒店覆盖POI字段、重复ID、坏坐标及business.cost，但不得把该cost当房价。原始响应和清洗结果保存，来源改为simulation:juhe-api/simulation:amap-api。酒店冲突报价另标quote-contract-fixture，不能声称来自高德。

15. latest live-model-verified显示初次query_plan不含name时，bind_query_plan把含arrival_date的查询固定绑定train_search，导致工具拒绝、模型无效重查酒店。默认工具名应依据prepare_task.query_mode选择到达工具，再回退领域默认；显式name仍严格验证、不自动改写。主提示明确新workflow的基本火车/酒店查询交给程序默认计划，避免重复编排；独立查询和明确依赖仍保留。先离线固定此失败再执行一轮原始API模拟验收，不提高调用上限。

16. 测量报告区分task_success（已completed且通过程序校验）和optimization_success（两/三/四调用及工具不重放），四次调用完成的普通出差不能被报告为任务失败。expected_behavior_success继续表示全部验收检查满足。历史失败报告保持原样，汇总须披露所有轮次消耗。

显式schema略增输入token，用真实usage衡量；模型仍可能不遵守，必须保留失败并有限停止。真实报价缺口不得追求completed；真实模型随机性不能用选择成功样本掩盖失败。
