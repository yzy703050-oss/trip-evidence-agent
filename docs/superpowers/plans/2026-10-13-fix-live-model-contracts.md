# 真实模型契约修复实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: 使用 superpowers:executing-plans 单代理实施；用户已选择当前目录，不派子代理。

**Goal:** 让主规划和政策输出遵守已有结构及预算语义，实际验证新链路。
**Architecture:** 单一紧凑结构契约供首次/修正提示复用；政策输出类型与缺口边界明确，程序严格拒绝与错误审计继续有效。
**Tech Stack:** 项目Python、现有SDK、pytest、Ruff、Milvus/本地embedding。
**Spec:** openspec/changes/fix-live-model-contracts/specs/、design.md、tasks.md。

## Global Constraints

保持源代码保留、静默旧Agent、两次普通主调用、一次政策提取。不得改用户固定条件、合并单程预算为全程、伪造报价/来源或删除复杂强制规则。所有新增行为以上述正式规格为准。

## Review Focus

- 原非法样本仍拒绝，预算保持train_max_total_cny。
- 样例schema对应现有条件与工具查询，普通路径不多调用模型。
- 政策对象作用域、seat_class字符串/null单位、来源及未知规则缺口均受检。
- 无审批只排除例外，不把软偏好/旧规则生成额外hard规则。
- 实际供应商能力不够时仍partial，模拟局部修复不能冒充真实报价。

### Task 1: 主规划与修正共用结构契约（OpenSpec 1.2）

Files: 新 agents/model_contracts.py、tests/test_live_model_contracts.py；改 agents/main_agent.py。
Interface: workflow_proposal_contract()返回紧凑schema/example；workflow_contract_prompt()返回同源模型说明。调用方API不变。

- [x] 写失败测试：通过首次与repair捕获实际prompt契约，解析sample用create_workflow和默认查询绑定证明有效；检查purpose枚举与关键词类型及单程预算不转换。原始非法样本仍被拒绝。
- [x] 运行 pytest tests/test_live_model_contracts.py -q，确认缺契约失败，再最小注入统一helper，不改validator接纳规则。
- [x] 运行新测试及 main_batch_protocol/main_output/planning_intents/request_orchestration；记录证据勾选1.2。

### Task 2: 政策类型、规则边界与诊断（OpenSpec 1.3）

Files: 改 agents/policy_preflight.py、agents/workflow_runner.py、tests/test_policy_preflight.py；扩 tests/test_live_model_contracts.py。
Interface: TravelPolicyAgent.query与validate_policy_context返回契约保持；增加严格结构检查及失败事件policy_validation_error。

- [x] 写失败测试：顶层字符串applicability拒绝、规则值类型与单位严格、复杂审批依旧缺口；实际query捕获prompt对象示例并用样例返回验证，确认报价不是政策缺口；Runner记录具体校验错误且不完成旅行。
- [x] 定向RED后补结构提示/样例、类型校验/有界审计；保持一轮政策模型，不能为非空缺口自动删除约束。
- [x] 重跑 policy_preflight/policy_workflow_guards/enterprise_policy_repair/request_orchestration；勾选1.3。

### Task 3: 批量边界与有限停止（OpenSpec 1.4）

Files: agents/model_contracts.py、agents/main_agent.py、agents/workflow_runner.py、tests/test_live_model_contracts.py；evals/run_live_policy_measurement.py仅增加单场景选择及准确验收记录。

Interface: 批量上下文增加query_tools，直接复用executor.schemas；commit_batch现有allow_repair=false经严格校验后保存partial，不增加模型阶段。默认修复语义不变。

实测补充：travel_data/tools.py两处硬约束比较改用共享的严格金额语义比较；测试500与"500"/500.0相等可刷新，501/NaN/未知键仍拒绝，保存金额不变。批量刷新测试使用生产政策合并后的字符串预算和模型数字预算，复現真实边界。

报价视图补充：travel_data/result_views.py与test_bounded_tool_results新增TDD，760元/两晚派生380元每晚，原始报价不变；无价/坏日期/非有限金额不派生，同名供应商字段不可覆盖。批量契约说明总额/每晚单位及已筛候选优先选择。

过滤说明补充：workflow_queries.query_views提供returned_candidate_total/excluded_candidate_count，result_views保留有界计数；实际schemas明确报价模式。批量刷新测试先断言原返回7、排除7并捕获报价描述，再让同契约动作完成刷新。已授权超标查询先执行一次，不再重复索求授权。

用户类型追问补充：共享validate_model_money只检查新模型输出，先写Main._initial_decision和批量动作拒绝字符串/bool/非有限数，以及TravelPolicyAgent实际query金额字符串拒绝的失败测试。保留历史预算字符串读取与工具语义比较；真实末轮原始输出离线重放新检查，无额外收费模型调用。

用户模拟数据要求补充：eval增加tourism_simulated/business_simulated，后续供应商使用显式隔离模拟，普通旅游2调用和普通出差3调用实測，复杂冲突复用已成功的4调用记录；不重复请求失败真实接口，不修改生产供应商故障策略。

动作限制补充：Runner._batch_context从必需领域候选/校验冲突生成allowed_actions；Main.select_batch资料有候选时仅展示提交契约，避免刷新样例诱导；Runner先拒绝不允许动作，不能调用供应商。先写普通数据返回仅commit允许动作、捕获prompt无刷新样例以及一次无依据query被拒绝且工具仍仅3次的测试；缺酒店候选分支仍开放查询并保持局部修复覆盖。

- [x] 写失败测试：真实Main.select_batch收到实际工具参数与hotel_places描述，输出定向refresh酒店并走ToolExecutor完成；有效火车仍2次。原实测未知参数拒绝。allow_repair=false缺报价提交只两次主调用，保存草稿仍running/partial。
- [x] RED后注入有界工具契约，精确说明query_plan及刷新/筛选层级；修正allow_repair=false分支，不改引用、政策或价格guard。
- [x] 定向batch、enterprise repair、runner和新增测试；单案例实测验证局部重查再运行真实供应商，保留所有失败记录。

### Task 4: 真实验收与整体复核（OpenSpec 2.1/2.2）

补充实施（OpenSpec 1.5/1.6，design 14–16）：
- [x] tests/test_api_shaped_simulation.py先断言原始HTTP响应经过生产解析器：多席别价格/余票、跨夜日期、坏行排除；酒店重复ID/坏坐标排除、business.cost不变成报价、来源明确模拟；requests.get/post禁止联网。新增evals/api_shaped_simulation.py，通过注入HTTP实现，不直接造最终候选；原始/解析结果可JSON导出。
- [x] tests/test_live_model_contracts.py新增初次无name到达日期查询用train_search_by_arrival、显式错误name仍被工具拒绝；先观察失败，再仅将默认工具名继承prepared.query_mode。
- [x] evals/run_live_policy_measurement.py使用新的原始API模拟供应商；business_api_simulated仅高德地点正确partial，business_simulated/conflict的酒店报价明确独立夹具；每个案例保存supplier-fixtures.json，task_success只评完成和校验，optimization_success独立评调用数。
- [x] 定向pytest上述测试及供应商/查询/批量回归；全量pytest。最后仅一轮真实模型+本地RAG重测（供应商不联网），新目录保存且保留全部历史费用。

能力缺口复核（OpenSpec 1.7，design17）：tests/test_live_model_contracts.py先写遗漏allow_repair时仅地点接口完整校验后只两主调用结束provider_capability_missing、保存draft；并测试未知火车时间仍保留后续修复。Runner补仅所有blocking issues由酒店报价缺口解释的判断（政策ID映射到酒店金额字段，未知硬规则不短路）；不改变guard、不完成旅行，显式false保持model_partial。定向回归后只复测business_api_simulated，其他已通过场景不重复收费。

Files: 实测脚本、docs/evals新报告、开发记录和OpenSpec归档。

- [x] 全量pytest与新增文件Ruff、全仓F821/F822/F823/F811、diff检查。
- [x] 使用 evals/run_live_policy_measurement.py --output docs/evals/2026-10-13-live-policy-fixed，保存全部调用/错误/usage与RAG来源；原始失败证据保留。实际输出出现新边界时先定位、补对应失败测试后修正，不盲重试。
- [x] 人工逐条spec核对、自审与严格validate，CLI无verify明确报告；只有代码回归与真实验收满足后archive，维护任务状态。提交/推送/合并不自动执行。

## 完成证据

最终801 passed/3 skipped，API模拟四场景2/3/4/3调用；本轮70,478 token含失败，全部十轮293,956 token。任务/测试/人工规格核对见docs/superpowers/reviews/2026-10-13-live-model-contracts-verification.md；实际归档结果随后记录。无提交/推送/合并。

实际归档完成：2026-10-13-fix-live-model-contracts，OpenSpec同步新增5项需求，源change移入archive。当前codex/readme-agent-diagrams分支保留，无提交、推送、合并。
