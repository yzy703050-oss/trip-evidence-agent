---
name: plan-trip
description: 主 Agent 根据结构化条件和有来源的 domain_results 生成行程的专属指南。
---

MainAgent 负责行程模型调用。本指南仅在主 Agent 行程阶段加载，harness 先按需执行
偏好、记忆、RAG 和信息获取任务。读取 original_query、effective_preferences、
travel_conditions 和 domain_results。首次缺用户条件返回已有部分方案及缺口；接口缺字段、失败或候选不合要求时说明已查范围，不为同一缺口主动 needs_requery。
独立脚本复用 cli.TripEvidenceCLI 的统一入口，本能力不作为子 Agent 注册。

## 旧接口兼容输出（workflow模式不使用）

只生成安排建议：每日日期、时间、活动地点引用。城市只使用 city_ref="guide_destination"（展示查询中的目的地）；活动只使用 location_ref="guide:<攻略事实下标>"，引用有来源的 place/poi/attraction/location 类型事实。没有查到具体地点时，只能使用固定枚举 suggestion:city_walk、suggestion:museum、suggestion:meal、suggestion:rest，展示地点待核实的通用活动框架。不得输出自由文本 city/location 或自行构造展示名。火车和酒店选择必须引用 domain_results 中的报价 ID（selected_train_id、selected_hotel_id）；不存在可用候选项时返回 null。
价格、车次、房型、库存、取消规则、天气、开放时间与预约规则均由程序从对应查询结果重建。不得根据季节、常识或网页摘要补齐这些事实，不生成 estimated_budget 或预算内保证。
输出 JSON：{"itinerary":{"daily_plans":[{"day":1,"date":"2026-10-08","activities":[{"time":"09:00-12:00","location_ref":"suggestion:city_walk"}]}]},"selected_train_id":null,"selected_hotel_id":null,"planning_complete":true}。
活动时间仅是安排建议；实际开放与预约仍须查看有来源的攻略事实，未知信息保持待核实。

## 多目的地交通住宿工作流

读取用户原文、完整任务概览、当前任务条件、条件来源、候选查询视图、前段边界和执行反馈。
condition_strengths记录hard/soft，field_sources只记录来源；用户明确尽量/最好可为soft，明确固定条件与政策仍hard，系统建议可调整。模型协调软出发、住宿和返程日期，程序检查边界；不让工具自行顺延日期。
结果返回后，接口缺房价、库存、距离等只说明未查到，不反复补查。真实日期安排问题可用repair_tasks.task_updates提出soft日期变化，程序只查新日期受影响领域，旧报价不能直接改日期。
已证明hard条件不可兼容且已有候选或软调整不能解决时保存问题并等待用户决定；查询失败、空列表和未知事实不自动证明不可行。
每段只安排火车和住宿，不生成 activities、景点、天气或市内通勤时间。
候选选择引用 query_id/result_revision/candidate_id；requires_hotel=false时酒店引用为null，详情由程序重建。
一段草稿不等于全程成功，后段可能要求重检前段；所有当前版本草稿经 validate_workflow 才能完成。
当前版本workflow.status=completed且validation.valid=true时直接finish completed，不反复校验。completed是规划建议完成，不是预订或用户日期确认。
建议日期、模拟来源、酒店地点价格库存未知须披露；无硬报价或预算要求且仅有blocking=false提示时，不应因此返回partial。阻塞缺口仍返回partial。
抵达日期只采用可靠证据，未知保持 null；拟入住/离店日期与用户固定日期、推导、提案分开。
后段边界可靠时可以推导日期或提出带来源的建议，不修改固定条件。未知边界仍查询可执行酒店，不盲猜交通。
新旅行未指定首段出发城市时，Harness使用当前用户保存的常住城市作为默认起点，用户本次明确起点优先；没有可用起点时保存required_conditions断点，只询问出发城市，补充后恢复同一旅行和任务。其他缺省个人条件及重大冲突先返回已有方案和调整建议。用户要修改但目标范围不明时询问旅行/任务/组件。
首次拆分各住宿段时，大模型结合目的、路线、总时长及偏好提出nights，未指定的晚数标proposal；不存在程序默认晚数。hard入住离店日期和晚数必须遵守，明确可调整的soft条件可由模型协调，无酒店返程不要求停留时长。执行时依据已核实抵达时间衔接该停留方案，后段按离店及抵达边界调整，不能统一改成一晚。
新旅行默认往返，末段返回当前用户常住城市；没有常住城市则返回首段起点。明确单程、不返程或指定其他返回城市时遵守用户要求；已安排返程不重复添加。回程默认不需要酒店，未给回程日期时按前段建议离店日期衔接；用户固定日期不被覆盖。恢复或局部修改保留原路线范围。
去程查询失败时，实际抵达时间保持未知；可以结合计划日期和模型建议晚数提出拟入住离店日期，并按拟离店日继续查询回程。说明住宿日期随实际抵达调整，保留可用候选并给出核对车次等行动建议，不能编造车次、票价或实际发到时刻。
本段不完整也保存draft，再处理后段可独立查询；全部任务处理后全程校验，不通过返回partial。draft不表示可行或已执行。
任务目的purpose与requires_hotel独立；返程默认不订酒店，但明确要求住宿时允许查询。仅安排火车与酒店推荐，不补活动或酒店估价。
读preflight条件与query_requests；到达条件用train_search_by_arrival，不把抵达日当出发日。只换酒店时原火车引用必须保留。
酒店地点只有地址和位置等参考事实，价格、库存与入住规则未知；有硬报价或预算要求时不能 completed。
全程费用按每段人数和报价合计，未知不能算零。无新资料或达到上限时 partial，保留已知安排与缺口。
最终旅行回复由主模型组织中文句式，事实通过程序提供的占位符引用，每项必要事实出现一次。默认隐藏来源、接口链接、查询时间及内部草稿诊断；酒店地点的房价、空房和入住退改规则集中提醒一次。回复生成失败或不合格时使用简洁后备正文，不重放供应商查询。
